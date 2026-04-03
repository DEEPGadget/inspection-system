# Server Inspection System

멀티워커 기반 DeepGadget 서버(dg5W/dg5R/dg5W-TT 외 단종 제품) SW 설치 및 출고 전 검수 자동화 시스템.
FastAPI + Celery + Redis + PostgreSQL + NFS.

## 핵심 원칙
**"LLM은 판단에만, 실행은 코드가"**
- 정상 플로우: 에이전트 미호출, 토큰 0
- 에이전트 호출 3가지: 실행 에러(Inspect), 경계값(Verify), 비정형 SW 요구(SW Planner)
- 자세한 규칙 → `.claude/rules/agents.md`

## Architecture v2

```
[User: WebGUI/API] → job 제출 (서버정보 + 기대스펙 + H/W수동검수 + SW요구사항.md)
        |
        v
[System: Preflight Runner] baseline 설치 → 설치비의존 항목 점검  [q_inspect]
        | 실행 에러 시에만 → [Inspect Agent]
        v
[System: SW Install Runner] SW요구사항.md 기반 설치             [q_sw_install]
        | 비정형/실패 시에만 → [SW Planner Agent]   (SW 요구사항 없으면 skip)
        v
[System: Post-install Runner] stress_tools 설치 → 본검수 + Stress  [q_inspect]
        v
[System: Rule Validator] threshold 기반 PASS/FAIL               [q_validate]
        | 경계값/복합WARN 시에만 → [Verify Agent]
        v
[System: Cleanup Runner] 검수 전용 도구 제거                    [q_inspect]
        v
[System: Report Generator] Jinja2 → PDF/XLSX                   [q_report]
```

Flower: `:5555` / PostgreSQL: `:5432` / WebSocket: `:8000/ws`

## 검수/설치 순서 정책

- **SW 요구사항 있음**: Preflight → SW Install → Post-install (신규 출고)
- **SW 요구사항 없음**: Preflight → Post-install (RMA / 재검수)

## Phase 체계

- **preflight**: 드라이버/SW 없이 실행 가능 (HW 인식, OS 상태)
- **sw_install**: 유저 요구사항 기반 SW 설치
- **post_install**: 드라이버/SW 의존 검수 + Stress
- **collect**: 로그 수집

Phase 1 (H/W 수동 검수 8항목)은 GUI 직접 입력, 시스템 자동화 범위 밖.

## Directory

```
api/
  routers/           jobs.py, reports.py
  schemas.py         Pydantic (JobCreate: SecretStr + sw_requirements + install_policy)
  models.py          SQLAlchemy ORM (Job에 sw_requirements Text 컬럼 포함)
  database.py        async engine + session
  websocket.py       실시간 노티
workers/
  app.py             Celery app (q_inspect, q_sw_install, q_validate, q_report)
  inspect.py         q_inspect — preflight/post-install + cleanup
  validate.py        q_validate — rule validator 우선, 에이전트 fallback
  report.py          q_report — Jinja2 PDF/XLSX
  ssh_client.py      SSH 접속 관리 (SecretStr, 접속 후 pw 폐기)
  rule_validator.py  validation.rules 기반 PASS/FAIL (토큰 0)
  agent_gateway.py   에이전트 호출 판단 + compact input
  sw_install.py      q_sw_install — 설치 실행/검증/재시도
  sw_planner.py      SW 요구사항 파싱 + 계획 JSON 오케스트레이션
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
alembic/             DB 마이그레이션
tests/               pytest (test_api/, test_workers/, test_checks/)
```

상세 규칙 → `.claude/rules/`

## Key Design Decisions

- Job ID: UUID v4
- NFS base: `/srv/inspection/` (`results/`, `logs/`, `checks/`)
- 결과 경로: `/srv/inspection/results/{job_id}/inspect_raw/*.json`
- SW 요구사항: DB `jobs.sw_requirements` (Text) + NFS `{job_id}/sw_requirements.md`
- Celery: `q_inspect(4)`, `q_sw_install(2)`, `q_validate(2)`, `q_report(2)`
- `task_acks_late=True`
- stress timeout: soft `7200s`, hard `7500s`

상세 → `.claude/rules/workers.md`, `.claude/rules/profiles.md`

## Commands

```bash
docker compose up -d                           # 전체 기동
docker compose up -d --scale worker_inspect=4  # 워커 스케일
docker compose exec api alembic upgrade head   # DB 마이그레이션
docker compose logs -f worker_inspect          # 로그
celery -A workers.app inspect active           # 실행 중 태스크
redis-cli LLEN q_inspect                       # 큐 depth
pytest tests/ -x -q                            # 테스트
ruff check . && ruff format --check .          # lint
python3 checks/base/preflight/sw_gpu_hw.py | python3 -m json.tool
rtk gain                                       # RTK 토큰 절감량
```

## 완료 워크플로우

1. `pytest tests/ -x -q` + `ruff check . && ruff format --check .` 통과 필수
2. `git add <관련파일>` → `git commit` → `git push -u origin <브랜치>`
3. `gh pr create --fill --base main` (PR 생성까지 자동 수행, merge는 사용자 결정)

main 직접 push 금지. 브랜치 명명: `feature/`, `fix/`, `chore/`

## 알려진 이슈

- Alembic ENUM: `postgresql.ENUM(..., create_type=False)` + `DO $$ EXCEPTION WHEN duplicate_object $$` 패턴 필수
- DB 초기화: `alembic_version` 테이블도 함께 DROP 후 재마이그레이션
- password: DB 미저장, 로그 마스킹, SSH 접속 후 즉시 폐기

## 환경변수

`.env` 참조. 필수: `REDIS_URL`, `DATABASE_URL`, `ANTHROPIC_API_KEY`

## 현재 구현 상태

### v1 완료
- [x] 프로젝트 스캐폴딩 + Docker Compose
- [x] Celery 큐 분리 (q_inspect, q_validate, q_report)
- [x] DB 모델 (Job, CheckResult, Report)
- [x] Alembic 초기 마이그레이션
- [x] Jobs API (POST/GET/DELETE) + Reports API + WebSocket
- [x] Inspect Worker SSH (pre_install, per-script timeout)
- [x] 검수 스크립트 12개 — checks/base/ (전체 Python)
- [x] Validate Worker (전체 결과 → Claude, v2에서 교체 예정)
- [x] Report Worker PDF/XLSX
- [x] 일일 코드 품질 cron

### v2 리팩토링 필요
- [ ] `checks/base/` 재구성: preflight/ + post_install/ + collect/
  - `sw_gpu` → `sw_gpu_hw.py` + `sw_gpu_sw.py` 분리
  - `sw_storage` → `sw_storage_hw.py` + `sw_storage_sw.py` 분리
- [ ] `checks/profiles/gpu_server.json`: v2 프로파일 구조 재작성
- [ ] `workers/rule_validator.py`: validation.rules 기반 PASS/FAIL
- [ ] `workers/agent_gateway.py`: 에이전트 호출 판단
- [ ] `workers/validate.py`: rule validator 우선 + 에이전트 fallback
- [ ] `workers/ssh_client.py`: SecretStr 지원 + pw 폐기
- [ ] `workers/sw_planner.py` + `workers/sw_install.py`: SW 설치 파이프라인
- [ ] `workers/inspect.py`: preflight/post-install 단계 분리 + cleanup
- [ ] `workers/app.py`: q_sw_install 큐 추가
- [x] `api/schemas.py`: sw_requirements + SecretStr 필드 (`install_policy` 제거됨 — sw_requirements 유무로만 분기)
- [x] `api/models.py`: Job.sw_requirements Text 컬럼 + JobStatus 8개 확장
- [x] Alembic 마이그레이션: sw_requirements + JobStatus 상태 추가 (`a1b2c3d4`)
- [ ] `config/logging.py`: structlog 민감필드 마스킹
- [ ] WebGUI 프론트엔드
