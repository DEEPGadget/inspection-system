---
name: deploy
description: main을 검수서버에 배포 (docker compose)
allowed-tools: Bash
---

main 브랜치를 검수서버에 배포합니다.

```bash
git checkout main
git pull origin main
docker compose build --parallel

# 마이그레이션 선적용 (migration.md 원칙: 코드 배포 전 반드시 먼저)
docker compose stop api worker_inspect worker_sw_install worker_validate worker_report
docker compose run --rm api alembic upgrade head

docker compose up -d
sleep 3
curl -sf http://localhost:8000/health && echo "API OK" || echo "API FAIL"
```
