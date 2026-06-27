#!/bin/bash
# ReviewForge Deploy Script
# Run on the server after git pull
set -e

APP_DIR="/opt/reviewforge"
BACKEND_DIR="$APP_DIR/backend"

echo "=== ReviewForge Deploy ==="
echo "Commit: $(git rev-parse --short HEAD)"

# 1. Install/sync dependencies
echo "--- Syncing Python dependencies ---"
cd "$BACKEND_DIR"
~/.local/bin/uv sync --frozen

# 2. Spec check
echo "--- Running spec-check ---"
~/.local/bin/uv run reviewforge spec-check

# 3. Restart service
echo "--- Restarting reviewforge service ---"
sudo systemctl restart reviewforge

# 4. Health check
echo "--- Health check ---"
sleep 2
for i in $(seq 1 10); do
    if curl -sf http://127.0.0.1:8000/health > /dev/null 2>&1; then
        echo "Health check passed"
        exit 0
    fi
    sleep 0.5
done
echo "WARNING: Health check failed after 10 attempts"
exit 1
