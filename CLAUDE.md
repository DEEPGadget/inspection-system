# Server Inspection System

멀티워커 기반 DeepGadget 서버(dg5W/dg5R/dg5W-TT 외 단종 제품) SW 설치 및 출고 전 검수 자동화 시스템.
FastAPI + Celery + Redis + PostgreSQL + NFS.

> `.claude/rules/` 12개 파일 전체가 system-reminder로 자동 로드됨. 아래 "→ rules/X" 표기는 위치 안내용이며 별도 파일 읽기 불필요.

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
[System: Preflight Runner] baseline 설치 → sys-config 적용 → 설치비의존 항목 점검  [q_inspect]
        | sys-config: GRUB + 임시 driver 설치 시 단일 재부팅
        | 실행 에러 시에만 → [Inspect Agent]
        v
[System: SW Install Runner] SW요구사항.md 기반 설치             [q_sw_install]
        | 비정형/실패 시에만 → [SW Planner Agent]   (SW 요구사항 없으면 skip)
        v
[System: Post-install Runner] stress_tools + 임시 CUDA 설치 → 본검수 + Stress  [q_inspect]
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

용어 정의 → `context/glossary.md` (Phase, Phase 1, JobStatus, preflight, post_install, collect, sw_install 구분 포함)

## Directory

> v2 리팩토링 진행 중. 현재 실재 파일 구조 → `handoff/current-state.md`

```
api/
  routers/           jobs.py, reports.py
  schemas.py         Pydantic (JobCreate: SecretStr password + sw_requirements)
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

> 상세 진행 현황·WARNING 목록·다음 작업 → `~/workspace/handoff/current-state.md`

### v1 완료
- [x] 프로젝트 스캐폴딩 + Docker Compose
- [x] Celery 큐 분리 (q_inspect, q_validate, q_report)
- [x] DB 모델 (Job, CheckResult, Report) + Alembic 마이그레이션
- [x] Jobs/Reports API + WebSocket
- [x] 검수 스크립트 12개 (checks/base/, 전체 Python)
- [x] Validate/Report Worker

### v2 진행 중 (블로커 순)
- [x] `api/schemas.py`: sw_requirements + SecretStr 필드
- [x] `api/models.py`: Job.sw_requirements + JobStatus 8개 확장
- [x] Alembic 마이그레이션 `a1b2c3d4`
- [ ] `checks/base/` 재구성: preflight/ + post_install/ + collect/
- [ ] `workers/inspect.py`: preflight/post-install 단계 분리 + cleanup
- [ ] `workers/ssh_client.py`: SecretStr + pw 폐기
- [ ] `workers/rule_validator.py` + `agent_gateway.py` + `validate.py`
- [ ] `workers/sw_planner.py` + `sw_install.py`
- [ ] `workers/app.py`: q_sw_install 큐 추가
- [ ] `config/logging.py`: structlog 민감필드 마스킹
- [ ] WebGUI 프론트엔드
