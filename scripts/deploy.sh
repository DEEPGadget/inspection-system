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

echo "=== Stopping API and workers ==="
docker compose stop api worker_inspect worker_sw_install worker_validate worker_report

echo "=== Running migrations ==="
docker compose run --rm api alembic upgrade head

echo "=== Building containers ==="
docker compose build --parallel

echo "=== Rolling restart ==="
docker compose up -d --no-deps api
docker compose up -d --no-deps --scale worker_inspect=4 worker_inspect
docker compose up -d --no-deps --scale worker_sw_install=2 worker_sw_install
docker compose up -d --no-deps --scale worker_validate=2 worker_validate
docker compose up -d --no-deps worker_report
docker compose up -d --no-deps flower

echo "=== Health check ==="
sleep 5
curl -sf http://localhost:8000/health && echo " API OK" || echo " API FAIL"
docker compose exec worker_inspect celery -A workers.app inspect ping 2>/dev/null && echo "Workers OK" || echo "Workers FAIL"

echo "=== Done ==="
