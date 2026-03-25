# Server Inspection System

멀티워커 기반 GPU 서버 출고 검수 자동화 시스템.
FastAPI + Celery + Redis + PostgreSQL + NFS.

## Architecture
```
[WebGUI] → [Redis] → [Inspect Worker] → NFS:/srv/inspection/results/{job_id}/
                                              ↓
                                       [Validate Worker + Claude API]
                                              ↓ pass
                                       [Report Worker] → PDF/XLSX
```

## Directory
- `api/` — FastAPI (REST + WebSocket)
- `workers/` — Celery tasks (inspect, validate, report)
- `checks/` — 검수 셸스크립트 + 제품 프로파일
- `config/` — 설정, Claude 프롬프트
- `templates/` — 리포트 Jinja2 템플릿
- `scripts/` — 배포, 서버 세팅 스크립트

## Key Rules
- 검수 스크립트 출력: `{"check":"name","status":"pass|fail|warn","detail":"..."}`
- NFS base: `/srv/inspection/`
- Job ID: UUID v4
- 로그: structlog JSON

## Delegation Rules
병렬: 검수 스크립트 작성 + 보안 리뷰
순차: 스크립트 → 리뷰 → 테스트 → 배포
서브에이전트 기본 모델: sonnet / security-reviewer만 opus

## Commands
- `docker compose up -d` — 전체 기동
- `celery -A workers.app inspect active` — 워커 상태
- `pytest` — 테스트
- `ruff check .` — lint
