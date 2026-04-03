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
docker compose up -d
sleep 3
curl -sf http://localhost:8000/health && echo "API OK" || echo "API FAIL"
```
