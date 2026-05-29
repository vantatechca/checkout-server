#!/usr/bin/env bash
# ============================================================
# Checkout Server — VPS Bootstrap Script
# Tested on Ubuntu 24.04 LTS
#
# Run as root:  bash scripts/setup_vps.sh
# ============================================================
set -euo pipefail

APP_USER="ubuntu"
APP_DIR="/home/$APP_USER/checkout-server"
PYTHON_VERSION="3.12"

echo "=================================================="
echo "  Checkout Server — VPS Setup"
echo "=================================================="

# ─── 1. System packages ─────────────────────────────────────
echo "[1/9] Installing system packages..."
apt-get update -qq
apt-get install -y \
    python3 python3-pip python3-venv \
    nginx \
    mariadb-server mariadb-client \
    redis-server \
    certbot python3-certbot-nginx \
    git curl wget unzip \
    supervisor \
    ufw

# ─── 2. MariaDB setup ───────────────────────────────────────
echo "[2/9] Configuring MariaDB..."
systemctl enable mariadb
systemctl start mariadb

# Secure installation (non-interactive)
mysql -u root << 'SQL'
ALTER USER 'root'@'localhost' IDENTIFIED BY 'CHANGE_ROOT_PASSWORD_HERE';
DELETE FROM mysql.user WHERE User='';
DELETE FROM mysql.user WHERE User='root' AND Host NOT IN ('localhost', '127.0.0.1', '::1');
DROP DATABASE IF EXISTS test;
DELETE FROM mysql.db WHERE Db='test' OR Db='test\\_%';
FLUSH PRIVILEGES;
SQL

mysql -u root -p'CHANGE_ROOT_PASSWORD_HERE' << 'SQL'
CREATE DATABASE IF NOT EXISTS checkout_db CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci;
CREATE USER IF NOT EXISTS 'checkout_user'@'localhost' IDENTIFIED BY 'CHANGE_DB_PASSWORD_HERE';
GRANT ALL PRIVILEGES ON checkout_db.* TO 'checkout_user'@'localhost';
FLUSH PRIVILEGES;
SQL

echo "  ✅ MariaDB configured."

# ─── 3. Redis ───────────────────────────────────────────────
echo "[3/9] Configuring Redis..."
systemctl enable redis-server
systemctl start redis-server
# Bind to localhost only
sed -i 's/^bind 127.0.0.1 -::1/bind 127.0.0.1/' /etc/redis/redis.conf
systemctl restart redis-server
echo "  ✅ Redis running."

# ─── 4. Python virtualenv ────────────────────────────────────
echo "[4/9] Setting up Python environment..."
cd "$APP_DIR"
python3 -m venv venv
source venv/bin/activate
pip install --upgrade pip -q
pip install -r requirements.txt -q
echo "  ✅ Python venv ready."

# ─── 5. MariaDB schema ──────────────────────────────────────
echo "[5/9] Applying database schema..."
mysql -u checkout_user -p'CHANGE_DB_PASSWORD_HERE' checkout_db < scripts/schema.sql
echo "  ✅ Schema applied."

# ─── 6. Environment file ────────────────────────────────────
echo "[6/9] Setting up .env..."
if [ ! -f "$APP_DIR/.env" ]; then
    cp "$APP_DIR/.env.example" "$APP_DIR/.env"
    echo "  ⚠  .env created from example — EDIT IT NOW before starting the app!"
else
    echo "  .env already exists, skipping."
fi

# ─── 7. Systemd services ────────────────────────────────────
echo "[7/9] Installing systemd services..."

# FastAPI app
cat > /etc/systemd/system/checkout-api.service << EOF
[Unit]
Description=Checkout Server FastAPI
After=network.target mariadb.service redis.service

[Service]
User=$APP_USER
WorkingDirectory=$APP_DIR
ExecStart=$APP_DIR/venv/bin/uvicorn main:app --host 127.0.0.1 --port 8000 --workers 4 --proxy-headers
Restart=always
RestartSec=5
Environment="PYTHONPATH=$APP_DIR"
EnvironmentFile=$APP_DIR/.env

[Install]
WantedBy=multi-user.target
EOF

# Celery worker
cat > /etc/systemd/system/checkout-worker.service << EOF
[Unit]
Description=Checkout Server Celery Worker
After=network.target redis.service

[Service]
User=$APP_USER
WorkingDirectory=$APP_DIR
ExecStart=$APP_DIR/venv/bin/celery -A tasks.celery_app worker --loglevel=info --concurrency=4
Restart=always
RestartSec=10
Environment="PYTHONPATH=$APP_DIR"
EnvironmentFile=$APP_DIR/.env

[Install]
WantedBy=multi-user.target
EOF

# Celery beat (scheduler)
cat > /etc/systemd/system/checkout-beat.service << EOF
[Unit]
Description=Checkout Server Celery Beat Scheduler
After=network.target redis.service

[Service]
User=$APP_USER
WorkingDirectory=$APP_DIR
ExecStart=$APP_DIR/venv/bin/celery -A tasks.celery_app beat --loglevel=info --scheduler celery.beat:PersistentScheduler
Restart=always
RestartSec=10
Environment="PYTHONPATH=$APP_DIR"
EnvironmentFile=$APP_DIR/.env

[Install]
WantedBy=multi-user.target
EOF

systemctl daemon-reload
systemctl enable checkout-api checkout-worker checkout-beat
echo "  ✅ Systemd services installed."

# ─── 8. Nginx ───────────────────────────────────────────────
echo "[8/9] Configuring Nginx..."
cp "$APP_DIR/scripts/nginx.conf" /etc/nginx/sites-available/checkout
ln -sf /etc/nginx/sites-available/checkout /etc/nginx/sites-enabled/checkout
rm -f /etc/nginx/sites-enabled/default
nginx -t && systemctl reload nginx
echo "  ✅ Nginx configured."

# ─── 9. Firewall ────────────────────────────────────────────
echo "[9/9] Configuring UFW firewall..."
ufw --force reset
ufw default deny incoming
ufw default allow outgoing
ufw allow ssh
ufw allow 'Nginx Full'
ufw --force enable
echo "  ✅ Firewall active."

echo ""
echo "=================================================="
echo "  Setup complete!"
echo "=================================================="
echo ""
echo "  Next steps:"
echo "  1. Edit $APP_DIR/.env with your real credentials"
echo "  2. Run Gmail OAuth setup:"
echo "     source venv/bin/activate"
echo "     python services/interac_watcher.py --setup"
echo "  3. Get SSL certificates:"
echo "     certbot --nginx -d checkout.store1.com -d checkout.store2.com"
echo "  4. Start all services:"
echo "     systemctl start checkout-api checkout-worker checkout-beat"
echo "  5. Install BTCPay Server (see scripts/install_btcpay.sh)"
echo "  6. Set BTCPay webhook URL to: https://checkout.store1.com/webhooks/btcpay"
echo ""
