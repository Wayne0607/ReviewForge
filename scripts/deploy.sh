#!/bin/bash
# ReviewForge Deploy Script
# Run on the server after git pull
set -euo pipefail

APP_DIR="/opt/reviewforge"
BACKEND_DIR="$APP_DIR/backend"
FRONTEND_DIR="$APP_DIR/frontend"

echo "=== ReviewForge Deploy ==="
echo "Commit: $(git rev-parse --short HEAD)"

# Load the same environment used by the systemd service so spec-check sees
# production secrets without baking them into the script.
if [ -f "$APP_DIR/.env" ]; then
    set -a
    # shellcheck disable=SC1091
    . "$APP_DIR/.env"
    set +a
fi

# 1. Build frontend (if Node.js available)
if command -v node &> /dev/null && [ -d "$FRONTEND_DIR" ]; then
    echo "--- Building frontend ---"
    cd "$FRONTEND_DIR"
    npm ci --ignore-scripts
    npm run build
    echo "Frontend built to backend/src/reviewforge/static/"
else
    echo "--- Skipping frontend build (Node.js not found or frontend/ missing) ---"
fi

# 2. Install/sync Python dependencies
echo "--- Syncing Python dependencies ---"
cd "$BACKEND_DIR"
~/.local/bin/uv sync --frozen

# 3. Spec check
echo "--- Running spec-check ---"
~/.local/bin/uv run reviewforge spec-check

# 4. Restart service
echo "--- Restarting reviewforge service ---"
sudo systemctl restart reviewforge

# 5. Health check
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
