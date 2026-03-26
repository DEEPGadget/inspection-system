#!/bin/bash
# GitHub branch protection 설정
# gh auth login 완료 후 실행

REPO=$(gh repo view --json nameWithOwner --jq .nameWithOwner 2>/dev/null)
if [ -z "$REPO" ]; then
    echo "gh repo 감지 실패. gh auth login 먼저 실행하세요."
    exit 1
fi

echo "Setting branch protection for $REPO..."

gh api repos/$REPO/branches/main/protection \
  --method PUT \
  --field required_status_checks='{"strict":true,"contexts":["test"]}' \
  --field enforce_admins=false \
  --field required_pull_request_reviews='{"required_approving_review_count":1}' \
  --field restrictions=null \
  2>/dev/null && echo "Branch protection 설정 완료" || echo "설정 실패 (권한 확인)"
