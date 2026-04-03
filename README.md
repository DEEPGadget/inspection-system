# Server Inspection System

멀티워커 기반 GPU 서버(DG4/DG5, H200NVL, A100X) SW 설치 및 출고 전 검수 자동화 시스템.

**핵심 원칙: "LLM은 판단에만, 실행은 코드가"**
- 정상 플로우: 에이전트 미호출, 토큰 0
- 에이전트 호출 3가지 경우에만: 실행 에러(Inspect), 경계값(Verify), 비정형 SW 요구(SW Planner)

---

## 전체 아키텍처 (v2)

```mermaid
flowchart TD
    Client(["Client / WebGUI\n서버정보 + 기대스펙 + SW요구사항.md"])

    subgraph api["FastAPI  :8000"]
        API["POST /api/jobs/\nGET  /api/jobs/{id}/\nGET  /api/reports/{id}/pdf|xlsx\nWS   /ws/jobs/{id}"]
    end

    subgraph infra["Infrastructure"]
        Redis["Redis :6379\nbroker / result\npub-sub(WS)"]
        PG["PostgreSQL :5432\njobs / check_results / reports"]
        NFS["NFS  /srv/inspection/\nresults/{job_id}/inspect_raw/*.json\nresults/{job_id}/sw_requirements.md\nresults/{job_id}/report.pdf|xlsx"]
    end

    subgraph normal["정상 플로우  (토큰 0)"]
        direction TB
        PRE["Preflight Runner  q_inspect ×4\nbaseline 패키지 설치\npreflight/ 스크립트 실행\nJSON → NFS + DB"]
        SWI["SW Install Runner  q_sw_install ×2\nsw_requirements.md 파싱\ndriver / cuda / torch 설치\n설치 검증 + 재시도"]
        POST["Post-install Runner  q_inspect ×4\nstress_tools 설치\npost_install/ 스크립트 실행\nJSON → NFS + DB"]
        RV["Rule Validator  q_validate ×2\nvalidation.rules threshold 비교\n명확한 PASS / FAIL → 토큰 0"]
        CL["Cleanup Runner  q_inspect ×4\nstress-ng 등 검수전용 도구 제거\n/opt/gpu-burn 등 디렉토리 정리"]
        RPT["Report Generator  q_report ×2\nJinja2 → PDF  WeasyPrint\nXLSX  openpyxl\nNFS 저장 + DB 기록"]

        PRE -->|SW요구사항 있을 때| SWI
        PRE -->|SW요구사항 없을 때| POST
        SWI --> POST
        POST --> RV
        RV -->|PASS / FAIL 확정| CL
        CL --> RPT
    end

    subgraph agents["Agent Layer  (예외 경로만 호출)"]
        direction TB
        IA["🤖 Inspect Agent\nSSH실패·스크립트에러·JSON파싱에러\n에러 진단 + 수정 액션 JSON 반환\nmax_tokens 1024"]
        SPA["🤖 SW Planner Agent\n비정형 SW요구·버전 호환 판정 불가·설치실패\n요구사항 구조화 + 설치계획 JSON 생성\nmax_tokens 1024"]
        VA["🤖 Verify Agent\nagent_zone 경계값·복합 WARN 3개 초과\n경계값 종합 판단 + 복합 WARN 분석\nmax_tokens 512"]
        GW["agent_gateway.py\n호출 조건 판단\ncompact input 구성\n결과 → 시스템 액션 변환"]

        IA --> GW
        SPA --> GW
        VA --> GW
    end

    Client -->|HTTP :8000| API
    API -->|Celery dispatch| Redis
    API -->|asyncpg| PG
    Redis --> PRE

    PRE -.->|"실행 에러 시에만 ──────────────────────────────────────"| IA
    SWI -.->|"비정형 요구·설치 실패 시에만 ─────────────────────────"| SPA
    RV  -.->|"경계값·복합 WARN 시에만 ──────────────────────────────"| VA
    GW  -.->|복구 액션 반환| PRE
    GW  -.->|설치계획 JSON 반환| SWI
    GW  -.->|판정 결과 반환| RV

    PRE --> PG
    PRE --> NFS
    SWI --> PG
    POST --> PG
    POST --> NFS
    RV --> PG
    RPT --> PG
    RPT --> NFS
```

### Job 상태 전이

```
pending → preflight → sw_install* → post_install → validating → cleanup → reporting → pass | fail
                                                                                     ↘ error
* SW 요구사항 있을 때만
```

### Job 상태 전이

```
pending
  → preflight
  → sw_install      (SW요구사항 있을 때만)
  → post_install
  → validating
  → cleanup
  → reporting
  → pass | fail | error
```

### SW 요구사항 유무에 따른 플로우 분기

| 케이스 | 플로우 |
|--------|--------|
| 신규 출고 (SW 요구사항 있음) | Preflight → SW Install → Post-install → Validate → Cleanup → Report |
| RMA / 재검수 (SW 요구사항 없음) | Preflight → Post-install → Validate → Cleanup → Report |

---

## 컴포넌트 상세

### FastAPI (`api/`)

| 파일 | 역할 |
|------|------|
| `main.py` | 앱 초기화, 라우터 등록, lifespan |
| `routers/jobs.py` | Job CRUD — POST/GET/DELETE |
| `routers/reports.py` | 리포트 메타 조회 + PDF/XLSX 다운로드 |
| `websocket.py` | Redis pub/sub → WebSocket 실시간 상태 푸시, race window 처리 |
| `models.py` | SQLAlchemy ORM (Job, CheckResult, Report) |
| `schemas.py` | Pydantic 요청/응답 모델 — SecretStr password 자동 마스킹 |
| `database.py` | asyncpg async engine + session factory |

주요 스키마 필드:
- `sudo_password: SecretStr` — 로그에 `**********` 출력
- `sw_requirements: str | None` — 자유 형식 Markdown SW 요구사항
- `install_policy: str` — `auto | skip | force`

---

### Workers (`workers/`)

#### `inspect.py` — Preflight / Post-install / Cleanup Runner
- 큐: `q_inspect` / concurrency: 4
- `phase` 파라미터로 preflight·post_install·cleanup 분기
- `checks/profiles/{profile}.json`에서 스크립트 목록·timeout·env 로드
- SSH 접속 → SFTP 스크립트 전송 → `python3` 실행 → JSON 수집
- 결과 NFS(`inspect_raw/`) + DB(`check_results`) 저장
- 실행 에러 시 `agent_gateway.py`를 통해 Inspect Agent 호출 후 복구 재시도

#### `sw_install.py` — SW Install Runner
- 큐: `q_sw_install` / concurrency: 2 (rate limit 대응)
- `sw_planner.py`가 생성한 설치계획 JSON 기반 실행
- driver/cuda/torch 순서 보장, 설치 검증, 실패 시 재시도
- 비정형 요구·실패 시 `agent_gateway.py`를 통해 SW Planner Agent 호출

#### `sw_planner.py` — SW 설치 계획 오케스트레이션
- `jobs.sw_requirements` (자유 형식 MD) → 구조화된 설치계획 JSON 변환
- driver-cuda-torch 버전 매트릭스 결정
- SW Planner Agent 호출 조건 판단

#### `validate.py` — Rule Validator + Verify Agent 오케스트레이터
- 큐: `q_validate` / concurrency: 2
- `rule_validator.py` 먼저 실행 (토큰 0)
- 경계값·복합WARN 시에만 `agent_gateway.py`를 통해 Verify Agent 호출

#### `rule_validator.py` — Threshold 판정 (토큰 0)
- `validation.rules` 배열 순회, threshold 비교
- `fail_above/below/if/if_not` 키 평가 → PASS/FAIL 직접 판정
- `agent_zone` 해당 항목 및 `warn_count > threshold` → Verify Agent 트리거 반환

#### `agent_gateway.py` — 에이전트 호출 판단
- 에이전트 종류·호출 조건 판단
- 실패/경계 항목만 추려 compact input 구성 (토큰 최소화)
- 호출 결과 → 시스템 액션 JSON 변환

#### `ssh_client.py` — SSH 접속 관리
- asyncssh 기반, SecretStr 지원
- 접속 완료 후 password 즉시 폐기
- key 우선 (`/etc/inspection/ssh_keys/{host}` → `default`), password fallback

#### `report.py` — PDF/XLSX 생성
- 큐: `q_report` / concurrency: 2
- Jinja2 + WeasyPrint → PDF
- openpyxl → XLSX (스타일 포함)
- NFS 저장 + DB `reports` 레코드 기록

---

### 에이전트 3종 (`agent_gateway.py` 경유)

| 에이전트 | 트리거 | 역할 | max_tokens |
|---------|--------|------|------------|
| **Inspect Agent** | SSH 실패, 스크립트 에러, JSON 파싱 에러 | 에러 진단 + 수정 액션 JSON 반환, 복구 불가 시 FAIL 사유 | 1024 |
| **Verify Agent** | `agent_zone` 경계값 해당, warn_count > 3 | 경계값 종합 판단, 복합 WARN 분석 | 512 |
| **SW Planner Agent** | 비정형 SW 요구, 버전 호환 판정 불가, 설치 실패 | 요구사항 구조화, 설치계획 JSON 생성, 대체·복구안 | 1024 |

**목표 토큰 사용량**: 정상 플로우 0 tokens / 전체 job 평균 ~400 tokens

---

### 검수 스크립트 (`checks/base/`)

대상 서버에서 SSH로 실행. stdout에 JSON 한 줄만 출력. stdlib만 사용.

```json
{"check": "sw_gpu_hw", "status": "pass|fail|warn", "detail": "key=val|key2=val2"}
```

#### preflight/ — 드라이버 설치 전 실행 가능한 항목

| 스크립트 | 검사 항목 |
|---------|---------|
| `sw_gpu_hw.py` | lspci — GPU 수량·PCIe width·speed (driver 불필요) |
| `sw_cpu.py` | /proc/cpuinfo — 모델·코어·주파수·온도 |
| `sw_memory.py` | /proc/meminfo — 용량·DIMM·ECC·NUMA |
| `sw_storage_hw.py` | lsblk — 디스크 목록·용량·RAID (nvme-cli 불필요) |
| `sw_network.py` | NIC 링크·속도·MTU |
| `sw_os_version.py` | OS·커널·필수 패키지 버전 |
| `sw_power_mgmt.py` | sleep.target masked·CPU governor·C-state |
| `sw_auto_update.py` | unattended-upgrades 비활성화 확인 |

#### post_install/ — 드라이버/SW 설치 후 실행

| 스크립트 | 검사 항목 |
|---------|---------|
| `sw_gpu_sw.py` | nvidia-smi — driver·VRAM·온도·ECC·NVLink |
| `sw_storage_sw.py` | nvme-cli/smartctl — NVMe 헬스·SMART |
| `stress_gpu.py` | GPU burn-in (기본 300s, nvidia-smi dmon) |
| `stress_cpu.py` | CPU 부하 테스트 (stress-ng/python3 fallback, 기본 120s) |
| `nccl_bandwidth.py` | AllReduce 대역폭 (all_reduce_perf / torchrun) |

#### collect/ — 로그 수집

| 스크립트 | 검사 항목 |
|---------|---------|
| `collect_all_logs.py` | dmesg·journalctl·XID 수집 |

#### 판정 임계값

| 항목 | FAIL 기준 | agent_zone (Verify 트리거) |
|------|----------|--------------------------|
| GPU 최고 온도 | > 87°C | > 75°C |
| CPU 최고 온도 | > 100°C | > 85°C |
| ECC uncorrected 에러 | > 0 | — |
| NCCL 2GPU NVLink busbw | < 30 GB/s | < 25 GB/s |
| NCCL 4GPU AllReduce busbw | < 5 GB/s | < 3 GB/s |
| sleep.target | masked 아님 | — |
| unattended-upgrades | 활성화 | — |

---

### 프로파일 (`checks/profiles/`)

스크립트 실행 목록·환경변수·패키지 설치·threshold·cleanup 정책 정의.

```json
{
  "profile_name": "gpu_server",
  "pre_install": {
    "baseline": ["pciutils", "nvme-cli", "ipmitool", "lm-sensors"],
    "stress_tools": ["stress-ng"]
  },
  "phases": {
    "preflight": { "scripts": ["sw_gpu_hw", "sw_cpu", ...] },
    "post_install": {
      "scripts": ["sw_gpu_sw", "stress_gpu", "nccl_bandwidth", ...],
      "timeout": 7200,
      "env": { "GPU_BURNIN_DURATION": "300" }
    },
    "collect": { "scripts": ["collect_all_logs"] }
  },
  "validation": {
    "rules": [
      {"check": "sw_gpu_sw", "metric": "gpu_max_temp_c", "fail_above": 87, "agent_zone_above": 75},
      ...
    ],
    "agent_trigger": { "warn_count_threshold": 3 }
  },
  "cleanup": {
    "remove_packages": ["stress-ng"],
    "remove_dirs": ["/opt/gpu-burn", "/opt/nccl-tests"],
    "on_failure": "warn"
  }
}
```

---

### 인프라

| 서비스 | 이미지 | 포트 | 역할 |
|--------|--------|------|------|
| `redis` | redis:7.2-alpine | 6379 | Celery broker/result + WebSocket pub/sub |
| `db` | postgres:16-alpine | 5432 | Job·결과·리포트 영속화 |
| `api` | 빌드 | 8000 | REST API + WebSocket |
| `worker_inspect` | 빌드 | — | Preflight / Post-install / Cleanup |
| `worker_sw_install` | 빌드 | — | SW 설치 파이프라인 |
| `worker_validate` | 빌드 | — | Rule Validator + Agent fallback |
| `worker_report` | 빌드 | — | PDF/XLSX 생성 |
| `flower` | 빌드 | 5555 | Celery 태스크 모니터링 |

---

## v2 구현 순서

### Phase 1 — 검수 스크립트 재구성 (블로커)
> 모든 worker 변경이 새 디렉토리 구조를 전제로 함. 가장 먼저 완료해야 함.

- `checks/base/` → `preflight/` + `post_install/` + `collect/` 디렉토리 재구성
- `sw_gpu.py` → `sw_gpu_hw.py` (preflight) + `sw_gpu_sw.py` (post_install) 분리
- `sw_storage.py` → `sw_storage_hw.py` + `sw_storage_sw.py` 분리
- `checks/profiles/gpu_server.json` v2 구조로 재작성 (validation.rules 포함)

### Phase 2 — DB/API 스키마 확장
> Phase 1과 병렬 가능. Phase 3 이전 완료 필요.

- `api/models.py`: `Job.sw_requirements` Text 컬럼 + JobStatus 상태 추가 (`preflight`, `sw_install`, `post_install`, `cleanup`)
- `api/schemas.py`: `JobCreate`에 `sw_requirements`, `install_policy` 필드 추가
- `alembic/versions/`: 마이그레이션 파일 생성

### Phase 3 — Workers 공통 인프라
> Phase 2 완료 후 진행. Phase 4의 전제 조건.

- `workers/ssh_client.py`: SecretStr 지원, 접속 후 password 즉시 폐기
- `workers/rule_validator.py`: `validation.rules` 기반 threshold 판정 (토큰 0)
- `workers/agent_gateway.py`: 에이전트 호출 판단 + compact input 구성
- `workers/app.py`: `q_sw_install` 큐 추가
- `config/logging.py`: structlog 민감필드 마스킹 (`password`, `token`, `api_key`)

### Phase 4 — Worker 로직 재작성
> Phase 1, 3 완료 후 진행.

- `workers/inspect.py`: `phase` 파라미터 기반 preflight/post_install/cleanup 분리, ssh_client 교체
- `workers/sw_planner.py`: sw_requirements MD → 설치계획 JSON 오케스트레이션
- `workers/sw_install.py`: 설치 실행/검증/재시도 (q_sw_install)
- `workers/validate.py`: rule_validator 우선 실행, agent_gateway fallback으로 교체

### Phase 5 — 마무리
> Phase 4 완료 후 진행.

- `workers/report.py`: v2 Job 상태·스키마 반영
- `tests/`: Phase 1~4 변경사항 커버 테스트 추가/수정
- 통합 테스트: 전체 파이프라인 E2E 검증

### Phase 6 — WebGUI (낮은 우선순위)
- 프론트엔드 미착수. 현재는 REST API + WebSocket으로 운영.

---

## 빠른 시작

```bash
# 환경변수 설정
cp .env.example .env
# .env에서 ANTHROPIC_API_KEY 입력

# 스택 기동
docker compose up -d
docker compose exec api alembic upgrade head

# 검수 job 생성
curl -sL -X POST http://localhost:8000/api/jobs/ \
  -H "Content-Type: application/json" \
  -d '{
    "target_host": "10.100.1.5",
    "target_user": "deepgadget",
    "product_profile": "gpu_server",
    "sudo_password": "password",
    "sw_requirements": "# SW Requirements\n- CUDA 12.4\n- PyTorch 2.3"
  }'

# 상태 확인
curl -sL http://localhost:8000/api/jobs/{job_id}/
websocat ws://localhost:8000/ws/jobs/{job_id}

# 리포트 다운로드
curl -sLO http://localhost:8000/api/reports/{job_id}/pdf
curl -sLO http://localhost:8000/api/reports/{job_id}/xlsx
```

---

## 운영 명령어

```bash
docker compose up -d
docker compose up -d --scale worker_inspect=4
docker compose exec api alembic upgrade head
docker compose logs -f worker_inspect
docker compose exec worker_inspect celery -A workers.app inspect active
docker compose exec redis redis-cli LLEN q_inspect
```

---

## 개발

```bash
pytest tests/ -x -q
ruff check . && ruff format --check .
python3 checks/base/preflight/sw_gpu_hw.py | python3 -m json.tool
rtk gain
```

---

## 환경변수

| 변수 | 필수 | 기본값 | 설명 |
|------|------|--------|------|
| `ANTHROPIC_API_KEY` | ✅ | — | Claude API 키 |
| `DATABASE_URL` | ✅ | `postgresql+asyncpg://...` | PostgreSQL 접속 URL |
| `REDIS_URL` | ✅ | `redis://redis:6379/0` | Redis 접속 URL |
| `NFS_BASE_PATH` | | `/srv/inspection` | 결과 파일 저장 경로 |
| `SSH_KEY_DIR` | | `/etc/inspection/ssh_keys` | SSH 키 디렉토리 |
| `CLAUDE_MODEL` | | `claude-sonnet-4-6` | 사용 모델 |
| `CLAUDE_MAX_TOKENS` | | `4096` | 최대 토큰 |

전체 목록: `.env.example`
