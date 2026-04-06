# Server Inspection System

DeepGadget 서버(dg5W / dg5R / dg5W-TT 외 단종 제품) 출고 전 SW 설치 및 검수 자동화 시스템.
FastAPI + Celery + Redis + PostgreSQL + NFS 기반 멀티워커 파이프라인.

---

## 핵심 원칙

**"LLM은 판단에만, 실행은 코드가"**

- 정상 플로우: 에이전트 미호출, 토큰 0
- 에이전트 호출 3가지 (예외 처리 전용):
  - **Inspect Agent** — SSH 실패·스크립트 에러 진단 및 수정 액션 반환
  - **SW Planner Agent** — 비정형 SW 요구사항 파싱·설치 계획 JSON 생성
  - **Verify Agent** — 경계값(agent_zone) 및 복합 WARN 종합 판단

---

## 전체 아키텍처

```
[User: WebGUI / API]
  │  job 제출 (서버정보 + 기대스펙 + H/W 수동검수 + SW 요구사항.md)
  ▼
[Preflight Runner]     q_inspect(×4)   baseline 설치 → 설치 비의존 HW/OS 점검
  │  실행 에러 시만 → [Inspect Agent]
  ▼
[SW Install Runner]    q_sw_install(×2) SW 요구사항.md 기반 설치
  │  비정형/실패 시만 → [SW Planner Agent]
  │  SW 요구사항 없으면 skip
  ▼
[Post-install Runner]  q_inspect(×4)   stress_tools 설치 → 본검수 + Stress
  │  실행 에러 시만 → [Inspect Agent]
  ▼
[Rule Validator]       q_validate(×2)  threshold 기반 PASS/FAIL (토큰 0)
  │  agent_zone / 복합 WARN 시만 → [Verify Agent]
  ▼
[Cleanup Runner]       q_inspect(×4)   검수 전용 도구 제거
  ▼
[Report Generator]     q_report(×2)    Jinja2 → PDF / XLSX
```

---

## 검수 플로우 분기

| 케이스 | 플로우 |
|--------|--------|
| SW 요구사항 있음 (신규 출고) | Preflight → SW Install → Post-install → Validate → Cleanup → Report |
| SW 요구사항 없음 (RMA / 재검수) | Preflight → Post-install → Validate → Cleanup → Report |

H/W 수동 검수 8항목(Phase 1)은 GUI 직접 입력 — 시스템 자동화 범위 밖.

---

## Phase 체계

| Phase | 내용 |
|-------|------|
| **preflight** | 드라이버·SW 없이 실행 — HW 인식, OS 상태 점검 |
| **sw_install** | 유저 SW 요구사항 기반 설치 (없으면 skip) |
| **post_install** | 드라이버·SW 의존 검수 + Stress 테스트 |
| **collect** | 로그 수집 |

---

## Job 상태 전이

```
pending → preflight → sw_install* → rebooting* → post_install → validating → cleanup → reporting → pass
                                                                                                  ↘ failed
                                                                                                  ↘ rejected      (Verify Agent 불합격)
                                                                                                  ↘ report_failed (리포트 생성 실패)
* sw_install: SW 요구사항 있을 때만 / rebooting: nvidia-driver 설치 또는 GRUB 파라미터 적용 시만
```

| 상태 | 의미 |
|------|------|
| `pending` | 생성됨, 미시작 |
| `preflight` | Preflight Runner 실행 중 |
| `sw_install` | SW Install Runner 실행 중 |
| `rebooting` | SW Install 중 reboot 필요 시 (nvidia-driver 설치 또는 GRUB 파라미터 적용 후) |
| `post_install` | Post-install Runner 실행 중 |
| `validating` | Rule Validator / Verify Agent 실행 중 |
| `cleanup` | Cleanup Runner 실행 중 |
| `reporting` | Report Generator 실행 중 |
| `pass` | 검수 통과 |
| `failed` | 시스템·스크립트 오류 또는 Rule Validator FAIL 판정 |
| `rejected` | Verify Agent 불합격 판정 (경계값 종합 판단 결과) |
| `report_failed` | 리포트 생성 실패 — 검수 결과 자체는 유효 |

---

## 판정 기준

| 판정 | 의미 |
|------|------|
| **PASS** | 모든 rule threshold 통과 |
| **FAIL** | 하나 이상의 rule threshold 위반 |
| **WARN** | threshold 미만이나 agent_zone 내 경계값 |
| **claude_verdict** | Verify Agent가 반환한 최종 판정 |

### 임계값 (gpu_server 프로파일 기준)

| 항목 | FAIL 기준 | Agent Zone (WARN 트리거) |
|------|-----------|--------------------------|
| GPU 최고 온도 | > 87°C | > 75°C |
| CPU 최고 온도 | > 100°C | > 85°C |
| NCCL 2GPU NVLink busbw | < 30 GB/s | < 25 GB/s |
| NCCL 4GPU AllReduce busbw | < 5 GB/s | < 3 GB/s |
| sleep.target | masked 아님 | — |
| unattended-upgrades | 활성화 | — |

---

## 컴포넌트

### FastAPI (`api/`)

| 엔드포인트 | 역할 |
|-----------|------|
| `POST /api/jobs/` | 검수 job 생성 → Celery 즉시 디스패치 |
| `GET /api/jobs/` | 전체 job 목록 |
| `GET /api/jobs/{job_id}/` | job 상세 (check_results 포함) |
| `DELETE /api/jobs/{job_id}/` | job 삭제 |
| `GET /api/reports/{job_id}/` | 리포트 메타 조회 |
| `GET /api/reports/{job_id}/pdf` | PDF 다운로드 |
| `GET /api/reports/{job_id}/xlsx` | XLSX 다운로드 |
| `WS /ws/jobs/{job_id}` | 실시간 상태 구독 |

### Celery Workers

| 워커 | 큐 | concurrency | 역할 |
|------|----|------------|------|
| `worker_inspect` | `q_inspect` | 4 | Preflight / Post-install / Cleanup — SSH 실행 |
| `worker_sw_install` | `q_sw_install` | 2 | SW 설치 파이프라인 |
| `worker_validate` | `q_validate` | 2 | Rule Validator + Verify Agent fallback |
| `worker_report` | `q_report` | 2 | PDF / XLSX 생성 |

### 검수 스크립트 (`checks/base/`)

대상 서버에서 SSH로 실행. 모든 스크립트는 Python stdlib만 사용.
표준 출력: `{"check": "<name>", "status": "pass|fail|warn", "detail": "..."}` 한 줄.

**preflight/**

| 스크립트 | 검사 항목 |
|---------|---------|
| `sw_gpu_hw.py` | GPU 인식·VRAM·ECC (드라이버 불필요) |
| `sw_cpu.py` | CPU 모델·코어·주파수·온도 |
| `sw_memory.py` | 메모리 용량·DIMM·ECC·NUMA |
| `sw_storage_hw.py` | 디스크 목록·NVMe 상태 (드라이버 불필요) |
| `sw_network.py` | NIC 링크·속도·MTU |
| `sw_os_version.py` | OS·커널·필수 패키지 버전 |
| `sw_power_mgmt.py` | sleep.target masked·CPU governor·C-state |
| `sw_auto_update.py` | unattended-upgrades 비활성화 확인 |

**post_install/**

| 스크립트 | 검사 항목 |
|---------|---------|
| `sw_gpu_sw.py` | GPU 드라이버·CUDA·ECC·NVLink (드라이버 필요) |
| `sw_storage_sw.py` | NVMe 펌웨어·SMART 상태 |
| `stress_gpu.py` | GPU burn-in (기본 300s) |
| `stress_cpu.py` | CPU 부하 테스트 (기본 120s) |
| `nccl_bandwidth.py` | AllReduce 대역폭 |

**collect/**

| 스크립트 | 검사 항목 |
|---------|---------|
| `collect_all_logs.py` | dmesg·syslog 수집 |

### 프로파일 (`checks/profiles/`)

어떤 스크립트를 어떤 순서로 실행할지, timeout·env 파라미터를 정의.
전 제품군 공통: `gpu_server.json`.

---

## 인프라

| 서비스 | 이미지 | 포트 | 역할 |
|--------|--------|------|------|
| `api` | (빌드) | 8000 | REST API + WebSocket |
| `worker_inspect` | (빌드) | — | SSH 검수 워커 |
| `worker_sw_install` | (빌드) | — | SW 설치 워커 |
| `worker_validate` | (빌드) | — | Rule Validator + Agent fallback |
| `worker_report` | (빌드) | — | 리포트 생성 워커 |
| `db` | postgres:16-alpine | 5432 | Job·결과·리포트 영속화 |
| `redis` | redis:7.2-alpine | 6379 | Celery 브로커 + result backend + pub/sub |
| `flower` | (빌드) | 5555 | Celery 태스크 모니터링 |

NFS 공유 (검수 시스템 → 엔지니어 노트북·보고서 서버):

```
/srv/inspection/results  → 결과 JSON + 리포트 PDF/XLSX (10.100.1.0/24 read/write)
/srv/inspection/logs     → 내부 전용 (민감 에러 로그)
```

---

## 빠른 시작

### 1. 환경변수 설정

```bash
cp .env.example .env
# ANTHROPIC_API_KEY, DATABASE_URL, REDIS_URL 필수 입력
```

### 2. 스택 기동

```bash
docker compose up -d
docker compose exec api alembic upgrade head   # 최초 1회 — DB 마이그레이션
```

### 3. 검수 job 생성

```bash
curl -sL -X POST http://localhost:8000/api/jobs/ \
  -H "Content-Type: application/json" \
  -d '{
    "target_host": "10.100.1.5",
    "target_user": "deepgadget",
    "sudo_password": "deepgadget",
    "product_profile": "gpu_server",
    "sw_requirements": "nvidia-driver-560\ncuda-toolkit-12-6\ntorch==2.4.0"
  }' | python3 -m json.tool
```

SW 요구사항이 없는 경우 (RMA): `sw_requirements` 필드 생략.

### 4. 상태 확인

```bash
JOB_ID=<반환된 id>

# REST 폴링
curl -sL http://localhost:8000/api/jobs/$JOB_ID/ | python3 -m json.tool

# WebSocket 실시간 구독
websocat ws://localhost:8000/ws/jobs/$JOB_ID

# 리포트 다운로드 (pass/fail 후)
curl -sLO http://localhost:8000/api/reports/$JOB_ID/pdf
curl -sLO http://localhost:8000/api/reports/$JOB_ID/xlsx
```

---

## 운영 명령어

```bash
# 스택 관리
docker compose up -d
docker compose down
docker compose up -d --scale worker_inspect=4   # 워커 스케일 아웃

# 로그
docker compose logs -f worker_inspect
docker compose logs -f worker_validate

# Celery 상태
docker compose exec worker_inspect celery -A workers.app inspect active
docker compose exec worker_inspect celery -A workers.app inspect ping

# 큐 depth
redis-cli LLEN q_inspect
redis-cli LLEN q_sw_install
redis-cli LLEN q_validate

# DB 마이그레이션
docker compose exec api alembic upgrade head

# 코드 품질
pytest tests/ -x -q
ruff check . && ruff format --check .

# 스크립트 단독 검증 (로컬)
python3 checks/base/preflight/sw_gpu_hw.py | python3 -m json.tool

# Flower 모니터링
open http://localhost:5555

# RTK 토큰 절감 현황
rtk gain
```

---

## 환경변수

| 변수 | 필수 | 설명 |
|------|------|------|
| `ANTHROPIC_API_KEY` | ✅ | Claude API 키 |
| `DATABASE_URL` | ✅ | PostgreSQL 접속 URL (`postgresql+asyncpg://...`) |
| `REDIS_URL` | ✅ | Redis 접속 URL (`redis://redis:6379/0`) |
| `NFS_BASE_PATH` | | 결과·로그 루트 경로 (기본: `/srv/inspection`) |
| `CLAUDE_MODEL` | | 사용 모델 (기본: `claude-sonnet-4-6`) |
| `CLAUDE_MAX_TOKENS` | | 에이전트 최대 토큰 fallback (기본: `4096`). 에이전트별 값(Inspect/SW Planner: 1024, Verify: 512)이 우선 — `.claude/rules/agents.md` 참조 |
| `WS_ENABLED` | | WebSocket 활성화 (기본: `true`) |

전체 목록: `.env.example` 참조.

---

## 알려진 이슈

| 항목 | 내용 |
|------|------|
| Alembic ENUM | `postgresql.ENUM(..., create_type=False)` + `DO $$ EXCEPTION WHEN duplicate_object $$` 패턴 필수 |
| DB 초기화 | `alembic_version` 테이블도 함께 DROP 후 재마이그레이션 |
| password 처리 | DB 미저장, 로그 마스킹, SSH 접속 후 즉시 폐기. 재검사 시 유저 재입력 필요 |
| stress timeout | soft 7200s / hard 7500s |
| reboot 처리 | nvidia-driver 설치 후 필수 — 300s SSH 재접속 폴링, 동일 task가 재접속 후 이어서 실행 |

---

## 디렉토리 구조

```
api/                    FastAPI (routers/, schemas.py, models.py, websocket.py)
workers/
  inspect.py            q_inspect — preflight / post-install / cleanup
  sw_install.py         q_sw_install — SW 설치 파이프라인
  validate.py           q_validate — rule validator 우선, agent fallback
  report.py             q_report — PDF / XLSX 생성
  rule_validator.py     threshold 기반 PASS/FAIL (토큰 0)
  agent_gateway.py      에이전트 호출 판단 + compact input
  sw_planner.py         SW 요구사항 파싱 + 설치 계획
  ssh_client.py         SSH 접속 관리 (SecretStr, 접속 후 pw 폐기)
checks/
  base/
    preflight/          sw_gpu_hw, sw_cpu, sw_memory, sw_storage_hw,
                        sw_network, sw_os_version, sw_power_mgmt, sw_auto_update
    post_install/       sw_gpu_sw, sw_storage_sw, stress_gpu, stress_cpu, nccl_bandwidth
    collect/            collect_all_logs
  profiles/             gpu_server.json
config/                 settings.py, celeryconfig.py, prompts/, logging.py
templates/              Jinja2 리포트 템플릿
alembic/                DB 마이그레이션
tests/                  pytest (test_api/, test_workers/, test_checks/)
scripts/                deploy.sh
```

상세 규칙 → `.claude/rules/`
