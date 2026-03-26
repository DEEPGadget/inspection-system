# Server Inspection System

GPU 서버 출고 전 검수를 자동화하는 멀티워커 파이프라인 시스템.
대상 서버에 SSH로 접속하여 셸스크립트를 실행하고, Claude API로 결과를 판독한 뒤 PDF/XLSX 리포트를 생성합니다.

---

## 전체 아키텍처

```
┌─────────────────────────────────────────────────────────────────────┐
│                          Client / CI                                 │
│                    REST API  /  WebSocket                            │
└───────────────────────────────┬─────────────────────────────────────┘
                                │ HTTP :8000
                                ▼
┌─────────────────────────────────────────────────────────────────────┐
│                        FastAPI  (api/)                               │
│                                                                      │
│  POST /api/jobs/          GET /api/jobs/{id}/                        │
│  GET  /api/reports/{id}/pdf    GET /api/reports/{id}/xlsx            │
│  WS   /ws/jobs/{id}       ← 실시간 상태 푸시                         │
└───────┬───────────────────────────────────────┬─────────────────────┘
        │ Celery task dispatch                  │ asyncpg
        ▼                                       ▼
┌───────────────┐                    ┌─────────────────────┐
│  Redis :6379  │                    │  PostgreSQL :5432    │
│  (broker /    │                    │  jobs               │
│   result)     │                    │  check_results       │
└───┬───────────┘                    │  reports             │
    │                                └─────────────────────┘
    │ q_inspect                              ▲  ▲  ▲
    ▼                                        │  │  │
┌─────────────────────────────────────────┐  │  │  │
│        worker_inspect  (×4)             │  │  │  │
│                                         │  │  │  │
│  1. SSH 접속 (asyncssh)                 │  │  │  │
│  2. 스크립트 SFTP 전송                   │  │  │  │
│  3. bash 실행 → JSON 수집               │──┘  │  │
│  4. NFS 저장 + DB 저장                  │     │  │
│  5. q_validate 에 체인                  │     │  │
└──────────────────┬──────────────────────┘     │  │
                   │ q_validate                  │  │
                   ▼                             │  │
┌─────────────────────────────────────────┐     │  │
│        worker_validate  (×2)            │     │  │
│                                         │     │  │
│  1. NFS에서 JSON 결과 읽기              │     │  │
│  2. Claude API 판독 (Anthropic SDK)     │─────┘  │
│  3. pass/fail/warn 판정                 │        │
│  4. DB 업데이트 + q_report 에 체인      │        │
└──────────────────┬──────────────────────┘        │
                   │ q_report                       │
                   ▼                                │
┌─────────────────────────────────────────┐        │
│        worker_report  (×2)              │        │
│                                         │        │
│  1. DB에서 결과 조회                    │        │
│  2. PDF 생성 (WeasyPrint + Jinja2)      │────────┘
│  3. XLSX 생성 (openpyxl)               │
│  4. NFS 저장 + DB 업데이트              │
└─────────────────────────────────────────┘

NFS  /srv/inspection/
  results/{job_id}/inspect_raw/*.json   ← 스크립트 원본 출력
  results/{job_id}/report.pdf
  results/{job_id}/report.xlsx

SSH Keys  /etc/inspection/ssh_keys/
  default          ← 범용 키 (ed25519)
  {host}           ← 호스트 전용 키 (우선)
```

---

## 컴포넌트 설명

### FastAPI (`api/`)

| 파일 | 역할 |
|---|---|
| `main.py` | 앱 초기화, 라우터 등록, lifespan |
| `routers/jobs.py` | Job CRUD — 생성/조회/삭제 |
| `routers/reports.py` | 리포트 메타 조회 + PDF/XLSX 다운로드 |
| `websocket.py` | WebSocket — 상태 변경 실시간 푸시 |
| `models.py` | SQLAlchemy ORM (Job, CheckResult, Report) |
| `schemas.py` | Pydantic 요청/응답 모델 |
| `database.py` | asyncpg 세션 팩토리 |

**Job 상태 전이**
```
pending → inspecting → validating → reporting → pass / fail
                                              ↘ error
```

**주요 API**
```
POST   /api/jobs/                   검수 job 생성 → Celery 즉시 디스패치
GET    /api/jobs/                   전체 job 목록
GET    /api/jobs/{job_id}/          job 상세 (check_results 포함)
DELETE /api/jobs/{job_id}/          job 삭제
GET    /api/reports/{job_id}/       리포트 메타
GET    /api/reports/{job_id}/pdf    PDF 다운로드
GET    /api/reports/{job_id}/xlsx   XLSX 다운로드
WS     /ws/jobs/{job_id}            실시간 상태 구독
```

---

### Celery Workers (`workers/`)

#### worker_inspect — SSH 검수 실행

- 큐: `q_inspect` / concurrency: 4
- `checks/profiles/{profile}.json` 에서 실행할 스크립트 목록 로드
- asyncssh로 대상 서버에 SFTP 전송 후 `bash` 실행
- 결과 JSON을 NFS(`inspect_raw/`) 및 DB(`check_results`)에 저장
- 완료 후 `validate_results` 태스크를 `q_validate`에 체인

#### worker_validate — Claude API 판독

- 큐: `q_validate` / concurrency: 2 (API rate limit 대응)
- NFS에서 JSON 읽어 `config/prompts/validation_gpu_server.txt` 프롬프트와 함께 Claude API 호출
- 모델별 pass/fail/warn 판정 + claude_verdict 저장
- 완료 후 `generate_report` 태스크를 `q_report`에 체인

#### worker_report — PDF/XLSX 생성

- 큐: `q_report` / concurrency: 2
- DB에서 job + check_results 조회
- Jinja2 템플릿으로 PDF(WeasyPrint), XLSX(openpyxl) 생성
- NFS 저장 + Report 레코드 DB 기록

---

### 검수 스크립트 (`checks/base/`)

스크립트는 SSH를 통해 **대상 서버**에서 실행됩니다.
모든 스크립트는 stdout에 JSON 한 줄만 출력합니다.

```json
{"check": "sw_gpu", "status": "pass", "detail": "..."}
```

| Phase | 스크립트 | 검사 항목 |
|---|---|---|
| phase2 | `sw_cpu.sh` | CPU 모델·코어·주파수·온도 |
| phase2 | `sw_gpu.sh` | GPU 모델·VRAM·온도·전력·ECC·NVLink |
| phase2 | `sw_memory.sh` | 메모리 용량·DIMM·ECC·NUMA |
| phase2 | `sw_storage.sh` | 디스크 목록·NVMe 상태·사용률 |
| phase2 | `sw_network.sh` | NIC 링크·속도·MTU |
| phase3 | `sw_power_mgmt.sh` | sleep.target masked·CPU governor·C-state |
| phase3 | `sw_auto_update.sh` | unattended-upgrades 비활성화 확인 |
| phase4 | `stress_gpu.sh` | GPU burn-in (nvidia-smi dmon, 기본 300s) |
| phase4 | `stress_cpu.sh` | CPU 부하 테스트 (stress-ng, 기본 120s) |
| phase5 | `nccl_bandwidth.sh` | AllReduce 대역폭 (all_reduce_perf / torchrun) |
| phase6 | `sw_os_version.sh` | OS·커널·필수 패키지 버전 |
| phase7 | `collect_all_logs.sh` | dmesg·syslog 수집 |

**판정 임계값**
| 항목 | 기준 |
|---|---|
| GPU 최고 온도 | > 87°C → FAIL |
| CPU 최고 온도 | > 100°C → FAIL |
| NCCL 4GPU AllReduce busbw | < 5 GB/s → FAIL |
| NCCL 2GPU NVLink busbw | < 30 GB/s → FAIL |
| sleep.target | masked 아님 → FAIL |
| unattended-upgrades | 활성화 → FAIL |

---

### 프로파일 (`checks/profiles/`)

어떤 스크립트를 실행할지, 환경변수 파라미터는 무엇인지 정의합니다.

```json
{
  "profile_name": "gpu_server",
  "phases": {
    "phase4_stress": {
      "enabled": true,
      "scripts": ["stress_gpu", "stress_cpu"],
      "env": { "GPU_BURNIN_DURATION": "300" }
    }
  }
}
```

새 제품군은 `checks/profiles/{name}.json`을 추가하고 `POST /api/jobs/`의 `product_profile` 필드에 지정합니다.

---

### 인프라

| 서비스 | 이미지 | 포트 | 역할 |
|---|---|---|---|
| `redis` | redis:7.2-alpine | 6379 | Celery 브로커 + 결과 백엔드 + WebSocket pub/sub |
| `db` | postgres:16-alpine | 5432 | Job·결과·리포트 영속화 |
| `api` | (빌드) | 8000 | REST API + WebSocket |
| `worker_inspect` | (빌드) | — | SSH 검수 워커 |
| `worker_validate` | (빌드) | — | Claude 판독 워커 |
| `worker_report` | (빌드) | — | 리포트 생성 워커 |
| `flower` | (빌드) | 5555 | Celery 태스크 모니터링 |

---

## 빠른 시작

### 1. 환경변수 설정

```bash
cp .env.example .env
# .env 열어서 ANTHROPIC_API_KEY 입력
```

### 2. SSH 키 설정

```bash
# 볼륨 경로 확인
KEYDIR=$(sudo docker volume inspect inspection-system_ssh_keys --format '{{.Mountpoint}}')

# 키 생성
sudo ssh-keygen -t ed25519 -f $KEYDIR/default -N ""
sudo chmod 600 $KEYDIR/default

# 대상 서버에 공개키 등록
sudo cat $KEYDIR/default.pub | ssh <user>@<target> \
  "mkdir -p ~/.ssh && cat >> ~/.ssh/authorized_keys && chmod 600 ~/.ssh/authorized_keys"
```

### 3. 스택 기동

```bash
sudo docker compose up -d

# DB 마이그레이션 (최초 1회)
sudo docker compose exec api alembic upgrade head
```

### 4. 검수 job 생성

```bash
curl -sL -X POST http://localhost:8000/api/jobs/ \
  -H "Content-Type: application/json" \
  -d '{
    "target_host": "10.100.1.5",
    "target_user": "deepgadget",
    "product_profile": "gpu_server"
  }' | python3 -m json.tool
```

### 5. 상태 확인

```bash
JOB_ID=<위에서 반환된 id>

# REST 폴링
curl -sL http://localhost:8000/api/jobs/$JOB_ID/ | python3 -m json.tool

# WebSocket 실시간 구독
websocat ws://localhost:8000/ws/jobs/$JOB_ID

# 리포트 다운로드 (완료 후)
curl -sLO http://localhost:8000/api/reports/$JOB_ID/pdf
curl -sLO http://localhost:8000/api/reports/$JOB_ID/xlsx
```

### 6. 워커 스케일

```bash
sudo docker compose up -d --scale worker_inspect=4
```

---

## 운영 명령어

```bash
# 전체 로그
sudo docker compose logs -f

# 특정 워커 로그
sudo docker compose logs -f worker_inspect

# Celery 상태 확인
sudo docker compose exec worker_inspect celery -A workers.app inspect active
sudo docker compose exec worker_inspect celery -A workers.app inspect ping

# Redis 큐 depth
sudo docker compose exec redis redis-cli LLEN q_inspect

# DB 마이그레이션
sudo docker compose exec api alembic upgrade head

# 코드 품질 검사 (수동)
bash scripts/daily_check.sh

# Flower 모니터링
open http://localhost:5555
```

---

## 개발

```bash
# 테스트
pytest tests/ -x -q

# Lint / Format
ruff check . && ruff format --check .

# 스크립트 단독 검증
bash checks/base/phase2_sw_basic/sw_gpu.sh | python3 -m json.tool
```

**브랜치 전략**
- `main` — 항상 배포 가능 상태
- `feature/<name>` / `fix/<name>` / `chore/<name>` — 작업 브랜치

---

## 환경변수

| 변수 | 필수 | 기본값 | 설명 |
|---|---|---|---|
| `ANTHROPIC_API_KEY` | ✅ | — | Claude API 키 |
| `DATABASE_URL` | ✅ | `postgresql+asyncpg://...` | PostgreSQL 접속 URL |
| `REDIS_URL` | ✅ | `redis://redis:6379/0` | Redis 접속 URL |
| `NFS_BASE_PATH` | | `/srv/inspection` | 결과 파일 저장 경로 |
| `SSH_KEY_DIR` | | `/etc/inspection/ssh_keys` | SSH 키 디렉토리 |
| `CLAUDE_MODEL` | | `claude-sonnet-4-20250514` | 사용할 Claude 모델 |
| `CLAUDE_MAX_TOKENS` | | `4096` | Claude 응답 최대 토큰 |

전체 목록은 `.env.example` 참조.
