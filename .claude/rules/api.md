# API 규칙

## 엔드포인트 목록

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

## 파일별 역할

| 파일 | 역할 |
|------|------|
| `main.py` | 앱 초기화, 라우터 등록, lifespan |
| `routers/jobs.py` | Job CRUD |
| `routers/reports.py` | 리포트 메타 조회 + 다운로드 |
| `websocket.py` | WebSocket — subscribe 후 DB 재확인 (race window 처리) |
| `models.py` | SQLAlchemy ORM (Job, CheckResult, Report) |
| `schemas.py` | Pydantic 요청/응답 모델 |
| `database.py` | asyncpg async engine + session factory |

## Pydantic 스키마 패턴

```python
class JobCreate(BaseModel):
    target_host: str
    target_user: str
    product_profile: str
    sudo_password: SecretStr          # 자동 마스킹
    sw_requirements: str | None = None  # 자유 형식 MD (있으면 SW Install 단계 실행)
```

- `str | None` 형식 사용 (`Optional` 금지)
- `SecretStr`로 password 자동 마스킹

## Python 코드 규칙

- ruff lint/format (line-length=100)
- type hint 필수
- API 레이어: async 함수 우선
- import 순서: stdlib → 3rd party → local
- 구체 예외 catch, bare `except` 금지
- f-string 사용 (`.format()` 금지)

## 의존성 관리

- 새 패키지: `pyproject.toml` dependencies 반드시 반영
- venv에만 설치하고 `pyproject.toml` 미반영 금지
