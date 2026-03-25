#!/bin/bash
# 검수서버에서 실행하는 배포 스크립트
# GitHub webhook 또는 수동 실행
set -euo pipefail

REPO_DIR="/opt/inspection-system"
BRANCH="${1:-main}"

echo "=== Deploying ${BRANCH} ==="

cd "$REPO_DIR"
git fetch origin
git checkout "$BRANCH"
git pull origin "$BRANCH"

echo "=== Building containers ==="
docker compose build --parallel

echo "=== Rolling restart ==="
docker compose up -d --no-deps api
docker compose up -d --no-deps --scale worker_inspect=4 worker_inspect
docker compose up -d --no-deps --scale worker_validate=2 worker_validate
docker compose up -d --no-deps worker_report
docker compose up -d --no-deps flower

echo "=== Health check ==="
sleep 5
curl -sf http://localhost:8000/health && echo " API OK" || echo " API FAIL"
docker compose exec worker_inspect celery -A workers.app inspect ping 2>/dev/null && echo "Workers OK" || echo "Workers FAIL"

echo "=== Done ==="
