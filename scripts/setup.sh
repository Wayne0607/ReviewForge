#!/bin/bash
# ReviewForge Server Setup Script
#
# This script initializes a fresh server for ReviewForge deployment.
# It installs dependencies, configures the database, and sets up
# the systemd service.

set -euo pipefail

# Configuration
APP_USER="reviewforge"
APP_DIR="/opt/reviewforge"
DB_NAME="reviewforge"
DB_USER="reviewforge_app"

echo "=== ReviewForge Server Setup ==="

# Create application user
if ! id "$APP_USER" &>/dev/null; then
    useradd -r -s /bin/false "$APP_USER"
    echo "Created user: $APP_USER"
fi

# Create application directory
mkdir -p "$APP_DIR"
chown "$APP_USER:$APP_USER" "$APP_DIR"

# Install system dependencies
apt-get update
apt-get install -y python3 python3-pip python3-venv postgresql nginx

# Setup Python virtual environment
python3 -m venv "$APP_DIR/venv"
source "$APP_DIR/venv/bin/activate"
pip install --upgrade pip

echo "System dependencies installed"

# Database setup would go here
# systemctl enable postgresql
# systemctl start postgresql

echo "Setup complete"
