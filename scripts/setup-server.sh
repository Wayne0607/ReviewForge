#!/bin/bash
# ReviewForge Server Setup Script
# Run this on your Alibaba Cloud server (Ubuntu/Debian)
set -e

echo "=== ReviewForge Server Setup ==="

# Check if running as root
if [ "$EUID" -ne 0 ]; then
    echo "Please run as root: sudo bash scripts/setup-server.sh"
    exit 1
fi

# 1. Clean up old Docker containers (if any)
echo "--- Cleaning up old Docker containers ---"
docker ps -aq --filter "name=reviewforge" | xargs -r docker rm -f 2>/dev/null || true
docker ps -aq --filter "ancestor=reviewforge" | xargs -r docker rm -f 2>/dev/null || true

# 2. Install system dependencies
echo "--- Installing system dependencies ---"
apt-get update -qq
apt-get install -y -qq python3 python3-pip python3-venv nginx curl git

# 3. Install uv (Python package manager)
echo "--- Installing uv ---"
if ! command -v uv &> /dev/null; then
    curl -LsSf https://astral.sh/uv/install.sh | sh
    export PATH="$HOME/.local/bin:$PATH"
fi

# 4. Create deploy user (if not exists)
echo "--- Setting up deploy user ---"
id -u reviewforge &>/dev/null || useradd -m -s /bin/bash reviewforge

# 5. Create app directory
echo "--- Creating app directory ---"
mkdir -p /opt/reviewforge
chown reviewforge:reviewforge /opt/reviewforge

# 6. Setup systemd service
echo "--- Installing systemd service ---"
cat > /etc/systemd/system/reviewforge.service << 'EOF'
[Unit]
Description=ReviewForge API Server
After=network.target

[Service]
Type=simple
User=reviewforge
Group=reviewforge
WorkingDirectory=/opt/reviewforge/backend
EnvironmentFile=/opt/reviewforge/.env
ExecStart=/home/reviewforge/.local/bin/uv run reviewforge serve --host 127.0.0.1 --port 8000
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
EOF

systemctl daemon-reload
systemctl enable reviewforge

# 7. Setup nginx
echo "--- Configuring nginx ---"
cat > /etc/nginx/sites-available/reviewforge << 'EOF'
server {
    listen 80;
    server_name _;

    location / {
        proxy_pass http://127.0.0.1:8000;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
    }
}
EOF

ln -sf /etc/nginx/sites-available/reviewforge /etc/nginx/sites-enabled/
rm -f /etc/nginx/sites-enabled/default
nginx -t && systemctl reload nginx

echo ""
echo "=== Setup complete ==="
echo "Next steps:"
echo "  1. Clone the repo to /opt/reviewforge"
echo "  2. Copy .env.example to .env and fill in your tokens"
echo "  3. Run: cd /opt/reviewforge/backend && uv sync"
echo "  4. Run: systemctl start reviewforge"
echo "  5. Configure GitHub webhook to http://YOUR_SERVER/webhook/github"
