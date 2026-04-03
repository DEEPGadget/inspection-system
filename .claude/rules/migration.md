# DB 마이그레이션 규칙

## 기본 원칙

- **배포 전 적용**: 코드 배포 전 반드시 마이그레이션 먼저 완료
- **서비스 중단 허용**: 마이그레이션 중 API 컨테이너 중지 가능
- **staging 없음**: 로컬 Docker DB에서 검증 후 운영 직접 적용
- **자동 롤백**: 마이그레이션 실패 시 PostgreSQL 트랜잭션이 자동으로 이전 상태로 복원

---

## 적용 순서 (배포 시 매번)

```bash
# 1. API/워커 중지 (worker_sw_install은 구현 후 추가)
docker compose stop api worker_inspect worker_validate worker_report

# 2. 마이그레이션 적용
docker compose run --rm api alembic upgrade head

# 3. 성공 확인 후 컨테이너 재기동
docker compose up -d
```

실패 시 PostgreSQL이 트랜잭션을 자동 롤백하므로 별도 처리 불필요.  
실패 원인 파악 후 마이그레이션 파일을 수정해 재시도.

---

## 마이그레이션 파일 작성 규칙

### ENUM 타입 추가/변경

PostgreSQL ENUM은 트랜잭션 내에서 변경 불가. 반드시 아래 패턴 사용.

**신규 ENUM 타입 생성:**
```python
op.execute("""
    DO $$ BEGIN
        CREATE TYPE job_status AS ENUM ('pending', 'pass', ...);
    EXCEPTION WHEN duplicate_object THEN null;
    END $$
""")
```

**기존 ENUM에 값 추가:**
```python
# ALTER TYPE은 트랜잭션 밖에서 실행해야 함
op.execute("ALTER TYPE job_status ADD VALUE IF NOT EXISTS 'preflight'")
op.execute("ALTER TYPE job_status ADD VALUE IF NOT EXISTS 'sw_install'")
# ... 값마다 별도 실행
```

`IF NOT EXISTS`로 멱등성 보장. 같은 마이그레이션을 여러 번 실행해도 오류 없음.

**ENUM 컬럼 생성 시:**
```python
postgresql.ENUM(..., name="job_status", create_type=False)
```
`create_type=False` 필수 — ENUM 타입은 위에서 이미 생성했으므로.

### 컬럼 추가

```python
op.add_column("jobs", sa.Column("sw_requirements", sa.Text, nullable=True))
```

- nullable=True 로 추가 (기존 rows에 기본값 없어도 오류 안 남)
- 코드에서 None 처리 포함해서 배포

### downgrade() 작성

모든 마이그레이션에 downgrade() 구현 필수.

```python
def downgrade() -> None:
    # 컬럼 제거 순서: 추가한 역순
    op.drop_column("jobs", "sw_requirements")
    # ENUM 값 제거는 PostgreSQL에서 직접 지원 안 함
    # ENUM 자체를 교체해야 할 경우: 컬럼 재생성 방식 사용
```

ENUM 값 제거가 필요한 경우:
```python
# 1. 임시 컬럼 추가 (새 ENUM 타입으로)
# 2. 데이터 복사
# 3. 원본 컬럼 삭제
# 4. 임시 컬럼 rename
# 이 패턴이 필요한 경우 별도 검토
```

---

## v2 마이그레이션 내용

### 브랜치: `chore/v2-migration`

v2 리팩토링에서 필요한 스키마 변경을 하나의 마이그레이션 파일로 묶음.

**변경 사항:**

1. `job_status` ENUM 확장
   - 추가: `preflight`, `sw_install`, `rebooting`, `post_install`, `cleanup`, `failed`, `rejected`, `report_failed`
   - 기존 유지: `pending`, `validating`, `reporting`, `pass`
   - 기존 deprecated: `inspecting`, `error` (값은 유지, 코드에서 미사용)

2. `jobs` 테이블 컬럼 추가
   - `sw_requirements TEXT` — SW 요구사항 원문 (nullable)

**마이그레이션 파일 위치:** `alembic/versions/`

---

## 로컬 검증 절차

운영 적용 전 로컬 Docker에서 반드시 확인:

```bash
# 현재 상태에서 upgrade 적용
docker compose exec api alembic upgrade head

# revision 확인
docker compose exec api alembic current

# downgrade 검증 (한 단계 내려갔다 올라오기)
docker compose exec api alembic downgrade -1
docker compose exec api alembic upgrade head
```

---

## DB 초기화 (개발 환경 전용)

운영에서 절대 실행 금지.

```bash
docker compose exec db psql -U inspector -d inspection -c "
    DROP TABLE IF EXISTS reports, check_results, jobs CASCADE;
    DROP TABLE IF EXISTS alembic_version;
    DROP TYPE IF EXISTS job_status;
    DROP TYPE IF EXISTS check_status;
"
docker compose exec api alembic upgrade head
```

`alembic_version` 테이블도 반드시 함께 삭제. 남아 있으면 Alembic이 이미 적용된 것으로 인식해 재마이그레이션 불가.

---

## 파일 명명 규칙

```
alembic/versions/{short_hash}_{slug}.py
```

예시:
- `69c4beca_initial_schema.py`
- `a1b2c3d4_v2_job_status_and_sw_requirements.py`

`alembic revision --autogenerate -m "v2 job_status and sw_requirements"` 실행 시 자동 생성.  
autogenerate 결과는 반드시 수동 검토 후 커밋 (ENUM 변경은 autogenerate가 감지 못함).
