# 데이터베이스 규칙

## 드라이버

- 런타임: `asyncpg` (async)
- Alembic 마이그레이션: `psycopg2-binary` (sync)

## Alembic 주의사항

- `sa.Enum(..., create_type=False)`는 `_on_table_create`에서 무시됨
  → 반드시 `postgresql.ENUM(..., create_type=False)` + `DO $$ EXCEPTION WHEN duplicate_object $$` 패턴 사용
- DB 초기화 시 `alembic_version` 테이블도 함께 DROP 후 재마이그레이션

## 모델 구조

| 테이블 | 역할 |
|--------|------|
| `jobs` | Job 상태, 설정, sw_requirements (Text) |
| `check_results` | 개별 스크립트 결과 JSON |
| `reports` | PDF/XLSX 경로, 메타 |

## 결과 저장 경로

- DB: `check_results` 테이블
- NFS: `/srv/inspection/results/{job_id}/inspect_raw/*.json`
- SW 요구사항 원문: DB `jobs.sw_requirements` (Text) + NFS `{job_id}/sw_requirements.md`

## 마이그레이션 명령

```bash
docker compose exec api alembic upgrade head    # 적용
docker compose exec api alembic revision --autogenerate -m "desc"  # 새 마이그레이션
```
