# Server Inspection System

멀티워커 기반 GPU 서버 출고 검수 자동화 시스템.
FastAPI + Celery + Redis + PostgreSQL + NFS.
검수 대상 서버에 SSH 접속 → 셸스크립트 실행 → Claude API 판독 → 리포트 생성.

## Architecture
```
[WebGUI/API :8000] → [Redis :6379] → [Inspect Worker] → NFS /srv/inspection/results/{job_id}/
                                                              ↓ callback
                                                       [Validate Worker + Claude API]
                                                              ↓ pass
                                                       [Report Worker] → PDF/XLSX
```
Flower 모니터링: :5555 / PostgreSQL: :5432

## Directory
```
api/              FastAPI REST + WebSocket
  routers/        jobs.py, reports.py
  schemas.py      Pydantic models
  models.py       SQLAlchemy ORM
  websocket.py    실시간 노티
workers/          Celery tasks
  app.py          Celery app 인스턴스
  inspect.py      q_inspect — SSH 검수 실행
  validate.py     q_validate — Claude API 판독
  report.py       q_report — PDF/XLSX 생성
checks/           검수 셸스크립트
  base/           phase2~7 공통 스크립트
  custom/         고객사별 커스텀
  profiles/       제품별 JSON 프로파일 (어떤 check를 돌릴지)
config/           settings.py, celeryconfig.py, prompts/
templates/        Jinja2 리포트 템플릿
scripts/          deploy.sh, setup-server.sh
tests/            pytest (test_api/, test_workers/, test_checks/)
```

## Tech Stack
- Python 3.12, FastAPI, Celery 5.4, Redis 7, PostgreSQL 16
- asyncssh (SSH), anthropic SDK (Claude API)
- WeasyPrint (PDF), openpyxl (XLSX), Jinja2
- Docker Compose로 전체 스택 운용
- structlog JSON 로깅

## Code Conventions

### Python
- ruff로 lint/format (line-length=100)
- type hint 필수. `str | None` 형식 사용 (Optional 아님)
- async 함수 우선 (API, SSH). Celery task 내부는 sync 허용
- import 순서: stdlib → 3rd party → local. 빈 줄로 구분
- 에러 처리: 구체적 예외 catch. bare except 금지
- f-string 사용. .format() 사용 금지

### Shell (checks/ 스크립트)
- shebang: `#!/bin/bash`
- `set -euo pipefail` 필수
- stdout은 JSON만. 디버그는 stderr로
- 출력 규격: `{"check":"name","status":"pass|fail|warn","detail":"..."}`
- 외부 의존성 최소화 (curl, jq 정도만)
- POSIX + bash 확장 최소화 (RHEL/Ubuntu 호환)

### 검수 스크립트 작성 시
- checks/base/ 기존 스크립트 스타일 참조
- 새 스크립트 추가 시 반드시: 1) JSON 출력 검증 2) shellcheck 통과 3) checks/profiles/ 에 등록
- 파라미터는 환경변수로 받음 (예: GPU_BURNIN_DURATION, REQUIRED_SW)

### Git
- branch: feature/, fix/, chore/
- commit: conventional commits (feat:, fix:, docs:, test:)
- PR 시 tests/ 포함 필수

## Key Design Decisions
- Job ID: UUID v4
- NFS base path: `/srv/inspection/` (results/, logs/, checks/)
- SSH 키: `/etc/inspection/ssh_keys/` (600 권한)
- 검수 결과 경로: `/srv/inspection/results/{job_id}/inspect_raw/*.json`
- Celery 큐 분리: q_inspect(4 concurrency), q_validate(2), q_report(2)
- validate concurrency=2인 이유: Claude API rate limit 대응
- task_acks_late=True: 워커 crash 시 재할당 보장
- stress test timeout: soft 7200s, hard 7500s (GPU burn-in 최대 2h)

## Validation Rules (Claude API 판독 기준)
상세 기준: config/prompts/validation_gpu_server.txt 참조
핵심 threshold:
- GPU max temp > 87°C → FAIL
- CPU max temp > 100°C → FAIL
- NCCL 4GPU bandwidth < 5 GB/s → FAIL
- NCCL 2GPU NVLink bandwidth < 30 GB/s → FAIL
- sleep.target not masked → FAIL
- unattended-upgrades enabled → FAIL

## Delegation Rules
병렬 위임: 검수 스크립트 작성 + 보안 리뷰 동시 진행
순차 위임: 스크립트 → 보안 리뷰 통과 → 테스트 → 프로파일 등록
서브에이전트 기본 모델: sonnet / security-reviewer만 opus

## Commands
```bash
docker compose up -d                        # 전체 기동
docker compose up -d --scale worker_inspect=4  # 워커 스케일
docker compose logs -f worker_inspect       # 로그
celery -A workers.app inspect active        # 실행 중 태스크
celery -A workers.app inspect ping          # 워커 응답 확인
redis-cli LLEN q_inspect                    # 큐 depth
pytest                                      # 테스트
ruff check . && ruff format --check .       # lint
bash checks/base/phase2_sw_basic/sw_gpu.sh | python3 -m json.tool  # 스크립트 검증
```

## 환경변수
.env 파일 참조. 필수: REDIS_URL, DATABASE_URL, ANTHROPIC_API_KEY
나머지: .env.example에 기본값과 설명 포함

## 현재 구현 상태
- [x] 프로젝트 스캐폴딩
- [x] Docker Compose 구성
- [x] Celery 큐 분리 설정
- [ ] DB 모델 (Job, CheckResult)
- [ ] Inspect Worker SSH 로직
- [ ] 검수 스크립트 (phase2~7)
- [ ] Validate Worker Claude API 연동
- [ ] WebGUI 제출 폼
- [ ] Report Worker PDF 생성
- [ ] WebSocket 실시간 노티