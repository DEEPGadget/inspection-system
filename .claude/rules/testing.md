# 테스트 규칙

## 명령

```bash
pytest tests/ -x -q          # 전체 (첫 실패에서 중단)
pytest tests/test_api/ -x -q  # API만
pytest tests/test_workers/ -x  # Workers만
pytest tests/test_checks/ -x   # 검수 스크립트만
```

## 구조

```
tests/
  test_api/      FastAPI 엔드포인트 테스트
  test_workers/  Celery task 단위 테스트
  test_checks/   검수 스크립트 JSON 출력 검증
```

## 완료 시 필수 순서

1. `pytest tests/ -x -q` — 통과 필수
2. `ruff check . && ruff format --check .` — lint 통과 필수
3. 실패 시 수정 후 1번부터 재시작
4. 통과 후에만 commit + push + PR

테스트 미통과 상태 push 금지.

## PR 요건

- PR 시 관련 tests/ 포함 필수
- 새 스크립트 추가 시 `tests/test_checks/`에 JSON 출력 검증 추가
