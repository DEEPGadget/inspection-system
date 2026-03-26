#!/bin/bash
# daily_check.sh — 코드 품질 자동 검사 + GitHub 이슈 생성
# crontab: 3 9 * * * /opt/inspection-system/inspection-system/scripts/daily_check.sh
set -euo pipefail

REPO="DEEPGadget/inspection-system"
WORKDIR="/opt/inspection-system/inspection-system"
LABEL="automated-check"
DATE=$(date +%Y-%m-%d)
TITLE="Daily Check 실패 ${DATE}"
FAILURES=()
OUTPUT=""

cd "$WORKDIR"

# ── 라벨 사전 생성 ──────────────────────────────────────────────────────────
gh label create "$LABEL" --color "E4E669" \
    --description "자동 코드 검사 이슈" --repo "$REPO" 2>/dev/null || true

# ── 1. ruff lint ──────────────────────────────────────────────────────────────
RUFF_OUT=$(ruff check . 2>&1 || true)
if echo "$RUFF_OUT" | grep -q "Found"; then
    FAILURES+=("ruff check")
    OUTPUT+="### ruff check\n\`\`\`\n${RUFF_OUT}\n\`\`\`\n\n"
fi

# ── 2. ruff format ────────────────────────────────────────────────────────────
FMT_OUT=$(ruff format --check . 2>&1 || true)
if echo "$FMT_OUT" | grep -q "would reformat"; then
    FAILURES+=("ruff format")
    OUTPUT+="### ruff format --check\n\`\`\`\n${FMT_OUT}\n\`\`\`\n\n"
fi

# ── 3. shellcheck ─────────────────────────────────────────────────────────────
SC_OUT=$(find checks/ -name "*.sh" -exec shellcheck {} + 2>&1 || true)
if [[ -n "$SC_OUT" ]]; then
    FAILURES+=("shellcheck")
    OUTPUT+="### shellcheck\n\`\`\`\n${SC_OUT}\n\`\`\`\n\n"
fi

# ── 4. pytest ─────────────────────────────────────────────────────────────────
PYTEST_OUT=$(~/.local/bin/pytest tests/ -q --tb=short 2>&1 || true)
if echo "$PYTEST_OUT" | grep -qE "failed|error"; then
    FAILURES+=("pytest")
    OUTPUT+="### pytest\n\`\`\`\n${PYTEST_OUT}\n\`\`\`\n\n"
fi

# ── 모두 통과 시 종료 ────────────────────────────────────────────────────────
if [[ ${#FAILURES[@]} -eq 0 ]]; then
    echo "[${DATE}] 모든 검사 통과 — 이슈 생성 없음"
    exit 0
fi

# ── 중복 이슈 확인 ────────────────────────────────────────────────────────────
EXISTING=$(gh issue list --repo "$REPO" --state open \
    --label "$LABEL" --json title -q '.[].title' 2>/dev/null \
    | grep -F "$TITLE" || true)
if [[ -n "$EXISTING" ]]; then
    echo "[${DATE}] 이미 동일 이슈 존재 — 생성 건너뜀"
    exit 0
fi

# ── GitHub 이슈 생성 ─────────────────────────────────────────────────────────
FAIL_LIST=$(IFS=", "; echo "${FAILURES[*]}")
BODY="## 자동 코드 검사 실패 리포트

**실행 시각:** $(date '+%Y-%m-%d %H:%M:%S %Z')
**실패 항목:** ${FAIL_LIST}

---

${OUTPUT}
---
> 이 이슈는 \`scripts/daily_check.sh\`에 의해 자동 생성됩니다."

gh issue create \
    --repo "$REPO" \
    --title "$TITLE" \
    --label "$LABEL" \
    --body "$BODY"

echo "[${DATE}] 이슈 생성 완료: ${FAIL_LIST}"
