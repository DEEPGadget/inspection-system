---
name: git-workflow
description: >
  코드 생성/수정 요청 시 자동 적용.
  "구현해줘", "만들어줘", "추가해줘", "수정해줘", "fix" 등.
---

# Git 워크플로우

## 작업 시작 시 (필수)

1. 현재 브랜치 확인: `git branch --show-current`
2. main에 있으면 반드시 feature branch 생성:
   ```bash
   git checkout -b feature/{module}-{description}
   ```
3. 이미 feature branch에 있으면 그대로 진행

## 브랜치 네이밍

- `feature/v2-rule-validator` — 새 기능
- `feature/v2-inspect-phases` — v2 리팩토링
- `fix/validate-json-parse` — 버그 수정
- `chore/update-dependencies` — 유지보수

## main 브랜치 규칙

- main에서 직접 코드 수정 금지
- main은 항상 배포 가능 상태 유지
- merge는 PR을 통해서만

## 작업 완료 시 순서

1. `pytest tests/ -x -q` 통과 확인
2. `ruff check . && ruff format --check .` 통과 확인
3. 실패 시 수정 후 1번부터 재시작
4. 관련 파일만 `git add` → `git commit -m "feat: ..."` → `git push -u origin <브랜치>`
5. `gh pr create --fill --base main`

PR 생성까지 자동 수행. merge는 사용자가 결정.
