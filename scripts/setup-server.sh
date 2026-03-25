#!/bin/bash
# 검수서버에 최초 1회 실행
# 사용법: ssh inspector-server 'bash -s' < scripts/setup-server.sh
set -euo pipefail

echo "=== 1. Docker + Compose 확인 ==="
if ! command -v docker &>/dev/null; then
    echo "Docker 미설치. 설치 진행..."
    curl -fsSL https://get.docker.com | sh
    sudo usermod -aG docker "$USER"
fi
docker compose version || { echo "docker compose plugin 필요"; exit 1; }

echo "=== 2. NFS 디렉토리 생성 ==="
sudo mkdir -p /srv/inspection/{results,logs/{inspect,validate,report},checks}
sudo chown -R "$USER:$USER" /srv/inspection

echo "=== 3. SSH 키 디렉토리 ==="
sudo mkdir -p /etc/inspection/ssh_keys
sudo chown "$USER:$USER" /etc/inspection/ssh_keys
chmod 700 /etc/inspection/ssh_keys

echo "=== 4. 리포 클론 ==="
REPO_URL="${1:-git@github.com:your-org/inspection-system.git}"
if [ ! -d /opt/inspection-system ]; then
    sudo mkdir -p /opt/inspection-system
    sudo chown "$USER:$USER" /opt/inspection-system
    git clone "$REPO_URL" /opt/inspection-system
fi

echo "=== 5. .env 생성 ==="
if [ ! -f /opt/inspection-system/.env ]; then
    cp /opt/inspection-system/.env.example /opt/inspection-system/.env
    echo ">>> .env 파일 수정 필요: /opt/inspection-system/.env"
fi

echo "=== 6. Webhook 수신 (optional) ==="
# 간단한 webhook receiver — GitHub webhook → deploy.sh 트리거
cat > /opt/inspection-system/scripts/webhook-receiver.sh << 'WH'
#!/bin/bash
# systemd 서비스로 등록하거나 간단한 nc 루프
# 운영 환경에서는 caddy/nginx + webhook 사용 권장
while true; do
    echo -e "HTTP/1.1 200 OK\r\n" | nc -l -p 9000 -q 1
    /opt/inspection-system/scripts/deploy.sh main
done
WH
chmod +x /opt/inspection-system/scripts/webhook-receiver.sh

echo "=== 완료 ==="
echo "다음 단계:"
echo "  1. /opt/inspection-system/.env 편집 (ANTHROPIC_API_KEY 등)"
echo "  2. 검수 대상 서버 SSH 키를 /etc/inspection/ssh_keys/ 에 배치"
echo "  3. cd /opt/inspection-system && docker compose up -d"
