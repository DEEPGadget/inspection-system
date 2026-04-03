---
name: pr
description: PR 생성 또는 merge
allowed-tools: Bash
---

현재 브랜치에서 PR을 관리합니다.

$ARGUMENTS 가 비어있거나 "create"이면:
```bash
BRANCH=$(git branch --show-current)
if [ "$BRANCH" = "main" ]; then
    echo "main 브랜치에서는 PR 생성 불가"
    exit 1
fi
gh pr create --fill --base main
```

$ARGUMENTS 가 "merge"이면:
```bash
gh pr merge --squash --delete-branch
git checkout main
git pull
```

$ARGUMENTS 가 "status"이면:
```bash
gh pr list
gh pr checks
```
