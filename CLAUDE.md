# Server Inspection System

멀티워커 기반 GPU 서버(DG4/DG5, H200NVL, A100X) SW 설치 및 출고 전 검수 자동화 시스템.
FastAPI + Celery + Redis + PostgreSQL + NFS.

## 핵심 원칙
**"LLM은 판단에만, 실행은 코드가"**
- 스크립트 실행, 설치 실행, threshold 비교, 리포트 생성 → 시스템 (토큰 0)
- 에러 진단, 애매한 판정, SW 설치 실패 분석, 비정형 요구사항 해석 → 에이전트 (토큰 소비)
- 정상 플로우에서 에이전트가 호출되지 않을 수 있음

## Architecture v2

```
[User: WebGUI/API] → job 제출 (서버정보 + 기대스펙 + H/W수동검수 + SW요구사항.md)
        |
        v
[System: Scheduler] Celery 큐잉(q_inspect), 가용 워커 할당
        |
        v
[System: Preflight Runner] baseline 설치 → 설치비의존 항목 점검 (토큰 0)  [q_inspect]
        | 실행 에러 시에만
        +-> [Inspect Agent] 에러 진단 + 수정 액션 반환
        |
        v
[System: SW Install Runner] SW요구사항.md 기반 설치 실행 (토큰 0)  [q_sw_install]
        | 비정형 요구 / 호환 판정 / 설치 실패 시에만               SW요구사항 없으면 skip
        +-> [SW Planner Agent] 요구사항 구조화 + 버전 호환 결정 + 복구안
        |
        v
[System: Post-install Runner] stress_tools 설치 → 본검수 + Stress (토큰 0)  [q_inspect]
        |
        v
[System: Rule Validator] threshold 기반 PASS/FAIL (토큰 0)  [q_validate]
        | 애매한 판정만
        +-> [Verify Agent] 경계값, 복합 WARN 종합 판단
        |
        v
[System: Cleanup Runner] 검수 전용 도구 제거 (토큰 0)  [q_inspect]
        |
        v
[System: Report Generator] Jinja2 → PDF/XLSX (토큰 0)  [q_report]
```

Flower: `:5555` / PostgreSQL: `:5432` / WebSocket: `:8000/ws`

## 검수/설치 순서 정책

- **SW 요구사항 있음**: Preflight → SW Install → Post-install (신규 출고)
- **SW 요구사항 없음**: Preflight → Post-install (RMA / 재검수 — SW가 이미 설치된 서버)

## Phase 체계 (논리명)

- **preflight**: 드라이버/SW 없이 실행 가능한 항목 (HW 인식, OS 상태)
- **sw_install**: 유저 요구사항 기반 SW 설치 (nvidia-driver, cuda, torch 등)
- **post_install**: 드라이버/SW 의존 검수 + Stress (nvidia-smi, nccl, gpu_burn 등)
- **collect**: 로그 수집

Phase 1 (H/W 수동 검수 8항목)은 유저가 GUI에서 직접 입력하며 시스템 자동화 범위 밖.

## 역할 정의

### System (Python/Celery — 토큰 0)
- Job 큐잉, 스케줄링, 상태 관리
- SSH 접속 → 검수 스크립트 직접 실행
- `pre_install.baseline` 설치 (Preflight 전), `pre_install.stress_tools` 설치 (Post-install 전)
- SW 설치 계획 JSON 기반 실행 (allowlist 명령만)
- Rule-based validation (threshold 비교)
- 리포트 생성 (Jinja2)
- 에이전트 호출 판단 및 compact input 구성
- Cleanup (검수 전용 도구 제거)

### Inspect Agent (실행 에러 시에만)
- SSH 실패, 의존성 문제, 파싱 에러 진단
- 수정 액션 JSON 반환 → 시스템이 실행
- 복구 불가 시 FAIL 사유 반환
- 모델: `claude-sonnet-4-20250514` / `max_tokens: 1024`

### Verify Agent (애매한 판정 시에만)
- 프로파일 `validation.rules`의 `agent_zone` 구간에 해당하는 경계값 판정
- 복합 WARN (`warn_count_threshold` 초과) 종합 판단
- `special_notes` 반영 예외 처리
- 모델: `claude-sonnet-4-20250514` / `max_tokens: 512`

### SW Planner Agent (비정형 SW 요구 / 설치 실패 시에만)
- 자유 형식 `.md` 요구사항 구조화
- 설치 대상 SW 식별 및 버전 호환 조합 결정 (driver-cuda-torch 매트릭스)
- 실행계획 JSON 생성 (`install_order`, `version_pins`, `commands`, `verify_steps`)
- 설치 실패 시 대체안/복구안 반환
- 모델: `claude-sonnet-4-20250514` / `max_tokens: 1024`

## Directory

```
api/
  routers/           jobs.py, reports.py
  schemas.py         Pydantic (JobCreate: ServerAuth SecretStr + sw_requirements + install_policy)
  models.py          SQLAlchemy ORM (Job에 sw_requirements Text 컬럼 포함)
  database.py        async engine + session
  websocket.py       실시간 노티
workers/
  app.py             Celery app (q_inspect, q_sw_install, q_validate, q_report)
  inspect.py         q_inspect — preflight/post-install 실행 + cleanup task
  validate.py        q_validate — rule validator 우선, 에이전트 fallback
  report.py          q_report — Jinja2 PDF/XLSX
  ssh_client.py      SSH 접속 관리 (SecretStr 지원, 접속 후 pw 즉시 폐기)
  rule_validator.py  프로파일 validation.rules 기반 PASS/FAIL 판정 (토큰 0)
  agent_gateway.py   에이전트 호출 판단 + compact input 구성
  sw_install.py      q_sw_install — 설치 실행/검증/재시도
  sw_planner.py      SW 요구사항 파싱 + 계획 JSON 생성 오케스트레이션
checks/
  base/
    preflight/       sw_gpu_hw, sw_cpu, sw_memory, sw_storage_hw, sw_network,
                     sw_os_version, sw_power_mgmt, sw_auto_update
    post_install/    sw_gpu_sw, sw_storage_sw, stress_gpu, stress_cpu, nccl_bandwidth
    collect/         collect_all_logs
  custom/            고객사별 커스텀
  profiles/          제품별 JSON 프로파일
config/              settings.py, celeryconfig.py, prompts/, logging.py
templates/           Jinja2 리포트 템플릿
scripts/             deploy.sh, setup-server.sh, daily_check.sh
alembic/             DB 마이그레이션
tests/               pytest (test_api/, test_workers/, test_checks/)
```

## 검수 스크립트 목록

| Phase | 스크립트 | 설명 |
|-------|---------|------|
| preflight | `sw_gpu_hw.py` | lspci — GPU 존재/수량/PCIe width·speed (driver 불필요) |
| preflight | `sw_cpu.py` | /proc/cpuinfo, 온도 |
| preflight | `sw_memory.py` | /proc/meminfo, DIMM, NUMA, ECC |
| preflight | `sw_storage_hw.py` | lsblk — 디스크 목록·용량·RAID (nvme-cli 불필요) |
| preflight | `sw_network.py` | NIC 링크·속도·MTU |
| preflight | `sw_os_version.py` | OS·커널·필수 패키지 |
| preflight | `sw_power_mgmt.py` | sleep.target·CPU governor·C-state |
| preflight | `sw_auto_update.py` | unattended-upgrades 비활성화 확인 |
| post_install | `sw_gpu_sw.py` | nvidia-smi — driver·VRAM·온도·ECC·NVLink |
| post_install | `sw_storage_sw.py` | nvme-cli/smartctl — NVMe 헬스·SMART |
| post_install | `stress_gpu.py` | GPU burn-in (기본 300s) |
| post_install | `stress_cpu.py` | CPU 부하 테스트 (기본 120s) |
| post_install | `nccl_bandwidth.py` | AllReduce 대역폭 |
| collect | `collect_all_logs.py` | dmesg·journalctl·XID 수집 |

## 프로파일 구조 (gpu_server.json 기준)

```json
{
  "profile_name": "gpu_server",
  "pre_install": {
    "baseline": ["pciutils", "nvme-cli", "ipmitool", "lm-sensors", "smartctl"],
    "stress_tools": ["stress-ng"]
  },
  "phases": {
    "preflight": {
      "scripts": ["sw_gpu_hw", "sw_cpu", "sw_memory", "sw_storage_hw",
                  "sw_network", "sw_os_version", "sw_power_mgmt", "sw_auto_update"]
    },
    "post_install": {
      "scripts": ["sw_gpu_sw", "sw_storage_sw", "stress_gpu", "stress_cpu", "nccl_bandwidth"],
      "timeout": 7200,
      "env": {
        "GPU_BURNIN_DURATION": "300",
        "CPU_BURNIN_DURATION": "120"
      }
    },
    "collect": {
      "scripts": ["collect_all_logs"]
    }
  },
  "validation": {
    "rules": [
      {"check": "sw_gpu_hw",       "metric": "gpu_count",       "fail_if_not_equal": "expected_gpu_count"},
      {"check": "sw_gpu_sw",       "metric": "gpu_max_temp_c",  "fail_above": 87,  "agent_zone_above": 75},
      {"check": "sw_cpu",          "metric": "cpu_max_temp_c",  "fail_above": 100, "agent_zone_above": 85},
      {"check": "sw_gpu_sw",       "metric": "ecc_delta_uncorr","fail_above": 0},
      {"check": "nccl_bandwidth",  "metric": "bw_2gpu_gbs",     "fail_below": 30,  "agent_zone_below": 25},
      {"check": "nccl_bandwidth",  "metric": "bw_4gpu_gbs",     "fail_below": 5,   "agent_zone_below": 3},
      {"check": "sw_power_mgmt",   "metric": "sleep_target",    "fail_if_not": "masked"},
      {"check": "sw_auto_update",  "metric": "unattended_upgrades_active", "fail_if": "active"}
    ],
    "agent_trigger": {
      "warn_count_threshold": 3
    }
  },
  "cleanup": {
    "remove_packages": ["stress-ng"],
    "remove_dirs": ["/opt/gpu-burn", "/opt/nccl-tests"],
    "on_failure": "warn"
  }
}
```

## Tech Stack
- Python 3.12, FastAPI, Celery 5.4, Redis 7, PostgreSQL 16
- asyncssh (SSH), anthropic SDK (Claude API)
- WeasyPrint (PDF), openpyxl (XLSX), Jinja2
- psycopg2-binary (Alembic sync), asyncpg (런타임 async)
- Docker Compose 운영
- structlog JSON 로깅 (민감필드 자동 마스킹)

## Code Conventions

### Dependencies
- 새 패키지 설치 시 `pyproject.toml` dependencies 반드시 반영
- venv에만 설치하고 `pyproject.toml` 미반영 금지

### Python
- ruff로 lint/format (`line-length=100`)
- type hint 필수 (`str | None`, `Optional` 사용 금지)
- API/SSH는 async 우선, Celery task 내부 sync 허용
- import 순서: stdlib → 3rd party → local
- 구체 예외 처리, bare `except` 금지
- f-string 사용 (`.format()` 금지)

### 검수 스크립트 (checks/base/ — Python)
- shebang: `#!/usr/bin/env python3`
- stdout은 JSON 한 줄만, 디버그는 stderr
- 출력 규격: `{"check":"name","status":"pass|fail|warn","detail":"key=val|key2=val2"}`
- stdlib만 사용 (원격 서버 pip 설치 금지)
- 외부 명령: `subprocess.run(shell=True, capture_output=True, text=True, timeout=N)`
  - `shell=True` 이유: 파이프/리다이렉트가 필요한 시스템 명령 조합이 많고, 대상 서버에서 1회성 실행 후 폐기되는 스크립트이므로 injection 위험 없음
- 환경변수: `os.environ.get("VAR", "default")`로 수신 (sshd AcceptEnv 우회)
- 새 스크립트 추가 시: 1) JSON 출력 검증 2) 문법 확인 3) `checks/profiles/` 에 등록
- apt/sudo 필요 패키지는 스크립트 내부가 아닌 프로파일 `pre_install.baseline` 또는 `stress_tools`에 등록

### 인증 (사내 전용)
- 현재: password 방식 (`ServerAuth.method="password"`)
- `SecretStr`로 자동 마스킹 (repr/log)
- SSH 접속 후 `job_data`에서 password 즉시 제거, DB 미저장
- 재검사 시 유저 재입력 요청
- structlog 프로세서로 민감필드 마스킹 (`password`, `secret`, `token`, `api_key`)
- 추후 ssh_key 방식 확장 시 auth 스키마 backward-compatible 유지

### Git
- branch: `feature/`, `fix/`, `chore/`
- commit: conventional commits (`feat:`, `fix:`, `docs:`, `test:`)
- PR 시 tests 포함 필수

## Key Design Decisions
- Job ID: UUID v4
- NFS base: `/srv/inspection/` (`results/`, `logs/`, `checks/`)
- SSH 키: `/etc/inspection/ssh_keys/` (`600`)
- 결과 경로: `/srv/inspection/results/{job_id}/inspect_raw/*.json`
- SW 요구사항 원문: DB `jobs.sw_requirements` (Text) + NFS `{job_id}/sw_requirements.md` 이중 저장
- Celery 큐: `q_inspect(4)`, `q_sw_install(2)`, `q_validate(2)`, `q_report(2)`
  - `q_sw_install=2`, `q_validate=2`: 에이전트 호출 가능성 있는 큐, Claude API rate limit 대응
- `task_acks_late=True`: 워커 crash 시 재할당
- stress timeout: soft `7200s`, hard `7500s`
- per-script SSH timeout: 프로파일 phase별 `timeout` → `conn.run(timeout=N)` 적용
- pre_install 설치 타이밍:
  - `baseline` → Preflight 직전 (`conn.run(input=password\n)`)
  - `stress_tools` → Post-install 직전
- cleanup: 프로파일 `cleanup.remove_packages/remove_dirs` 명시 목록만 제거, 실패 시 warn
- SW 요구사항 없으면 SW Install 단계 skip (RMA/재검수 서버 대응)

## 토큰 최적화 설계

### 에이전트 호출 원칙
- 시스템이 스크립트 직접 실행 → JSON 수집 (토큰 0)
- Rule validator가 `validation.rules` 기반 명확한 PASS/FAIL 판정 (토큰 0)
- 에이전트 호출은 세 가지로 제한:
  1. **Inspect Agent**: 스크립트 실행 에러
  2. **Verify Agent**: `agent_zone` 경계값, `warn_count_threshold` 초과
  3. **SW Planner Agent**: 비정형 SW 요구 해석, 호환 버전 결정, 설치 실패
- 에이전트 입력은 compact 구성 (실패/애매 항목만 전달)
- 정상 플로우: 토큰 0 / 전체 job 평균: ~400 tokens

### 재검사 플로우
- 실패 항목만 시스템이 직접 재실행
- rule validator 재판정 후 여전히 애매하면 verify agent 재호출

### RTK (자동 적용)
PreToolUse hook으로 모든 Bash 명령이 rtk를 통해 실행됨.
- `git status` → `rtk git status`
- `pytest` → `rtk pytest`
- `docker compose ps` → `rtk docker compose ps`
- `rtk gain`으로 절감량 확인

### Context 관리
- 작업 단위 완료 시 `/compact` (60% 도달 전에)
- 다른 작업 전환 시 `/clear`
- 탐색/조사는 서브에이전트에 위임 (메인 컨텍스트 보호)
- `/compact` 시 지시: "현재 구현 중인 파일 목록과 미완성 TODO 보존"

### Compaction 보존 규칙
- 수정한 파일 목록과 변경 요약
- 미완료 TODO 항목
- 실패한 테스트와 원인
- DB 스키마 변경사항

## Validation Rules

### Rule Validator (시스템, 토큰 0) — 프로파일 `validation.rules` 기반
- GPU 수량 != expected → FAIL
- GPU max temp > 87°C → FAIL
- CPU max temp > 100°C → FAIL
- ECC uncorrected > 0 → FAIL
- NCCL 2GPU < 30 GB/s → FAIL
- NCCL 4GPU < 5 GB/s → FAIL
- sleep.target not masked → FAIL
- unattended-upgrades active → FAIL

### Verify Agent — 애매한 판정 (`agent_zone` 구간)
- GPU temp 75~87°C 경계값
- CPU temp 85~100°C 경계값
- NCCL 2GPU 25~30 GB/s 구간
- NCCL 4GPU 3~5 GB/s 구간
- 전체 warn 3개 이상 복합 판정
- `special_notes` 기반 의도적 변경 예외 처리

## Delegation Rules (Claude Code 위임)
- 병렬 위임: 검수 스크립트 작성 + 보안 리뷰 동시 진행
- 순차 위임: 스크립트 → 보안 리뷰 통과 → 테스트 → 프로파일 등록
- 서브에이전트 기본 모델: sonnet
- security-reviewer만 opus (allowlist/위험 명령 검증 전용)

## Commands

```bash
docker compose up -d                           # 전체 기동
docker compose up -d --scale worker_inspect=4  # 워커 스케일
docker compose exec api alembic upgrade head   # DB 마이그레이션
docker compose logs -f worker_inspect          # 로그
celery -A workers.app inspect active           # 실행 중 태스크
celery -A workers.app inspect ping             # 워커 응답 확인
redis-cli LLEN q_inspect                       # 큐 depth
pytest                                         # 테스트
ruff check . && ruff format --check .          # lint
python3 checks/base/preflight/sw_gpu_hw.py | python3 -m json.tool  # 스크립트 검증
bash scripts/daily_check.sh                    # 코드 품질 수동 실행
rtk gain                                       # RTK 토큰 절감량 확인
```

## 알려진 이슈 / 주의사항
- Alembic: `sa.Enum(..., create_type=False)`는 `_on_table_create`에서 무시됨
  → 반드시 `postgresql.ENUM(..., create_type=False)` + `DO $$ EXCEPTION WHEN duplicate_object $$` 패턴
- DB 초기화 시 `alembic_version` 테이블도 함께 DROP 후 재마이그레이션
- 비밀번호: DB 미저장, 로그 마스킹, SSH 접속 후 즉시 폐기, 추후 SSH 키 전환 예정

## 환경변수
`.env` 참조. 필수: `REDIS_URL`, `DATABASE_URL`, `ANTHROPIC_API_KEY`
기타: `.env.example`에 기본값/설명 포함

## 현재 구현 상태 (v1 기준)
- [x] 프로젝트 스캐폴딩 + Docker Compose
- [x] Celery 큐 분리 (q_inspect, q_validate, q_report)
- [x] DB 모델 (Job, CheckResult, Report)
- [x] Alembic 초기 마이그레이션
- [x] Jobs API (POST/GET/DELETE)
- [x] Inspect Worker SSH 로직 (pre_install, per-script timeout)
- [x] 검수 스크립트 12개 — checks/base/ (전체 Python)
- [x] Validate Worker (현재: 전체 결과를 Claude에 전달 — v2에서 rule_validator로 교체 예정)
- [x] Report Worker PDF/XLSX
- [x] Reports API
- [x] WebSocket 실시간 노티
- [x] 일일 코드 품질 cron

### v2 리팩토링 필요 항목
- [ ] `checks/base/` 재구성: `preflight/` + `post_install/` + `collect/`
  - `sw_gpu` → `sw_gpu_hw.py` (preflight) + `sw_gpu_sw.py` (post_install) 분리
  - `sw_storage` → `sw_storage_hw.py` + `sw_storage_sw.py` 분리
- [ ] `checks/profiles/gpu_server.json`: v2 프로파일 구조로 재작성
- [ ] `workers/rule_validator.py`: `validation.rules` 기반 PASS/FAIL (토큰 0)
- [ ] `workers/agent_gateway.py`: 에이전트 호출 판단 + compact input
- [ ] `workers/validate.py`: rule validator 우선, 에이전트 fallback으로 재작성
- [ ] `workers/ssh_client.py`: SecretStr 지원, 접속 후 pw 폐기
- [ ] `workers/sw_planner.py`: SW 요구사항 파싱 + 계획 JSON 생성
- [ ] `workers/sw_install.py`: `q_sw_install` — 설치 실행/검증/재시도
- [ ] `workers/inspect.py`: preflight/post-install 단계 분리 + cleanup task
- [ ] `workers/app.py`: `q_sw_install` 큐 추가
- [ ] `api/schemas.py`: `sw_requirements` + `install_policy` 필드 추가
- [ ] `api/models.py`: `Job.sw_requirements` Text 컬럼 + 상태 확장
- [ ] `config/logging.py`: structlog 민감필드 마스킹 프로세서
- [ ] Alembic 마이그레이션: sw_requirements 컬럼 + JobStatus 상태 추가
- [ ] WebGUI 프론트엔드

## 컴포넌트 완료 워크플로우

### 브랜치 전략
- `main`: 항상 배포 가능 상태, 직접 push 금지
- 작업 시작 시 feature 브랜치: `git checkout -b feature/<컴포넌트명>`
- 브랜치 명명: `feature/`, `fix/`, `chore/`

### 완료 시 순서
1. `pytest tests/ -x -q`
2. `ruff check . && ruff format --check .`
3. 실패 시 수정 후 1번부터 재시작
4. 관련 파일만 `git add` → `git commit` → `git push -u origin <브랜치명>`
5. `gh pr create --fill --base main`
6. merge 후: `git checkout main && git pull && git branch -d feature/<컴포넌트명>`

> 테스트 미통과 상태 push 금지. PR 생성까지 자동 수행 (사전 승인됨). merge는 사용자가 결정.
