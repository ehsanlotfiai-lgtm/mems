#!/usr/bin/env bash
# ============================================================
#  MemeCoin Sniper - Ubuntu installer
#  Creates venv, installs deps, sets up systemd service, opens firewall.
# ============================================================
set -euo pipefail

APP_DIR="/opt/mems_sniper"
SERVICE_NAME="mems-sniper"
PYTHON_BIN="${PYTHON_BIN:-python3}"
SVC_FILE="/etc/systemd/system/${SERVICE_NAME}.service"

if [[ $EUID -ne 0 ]]; then
  echo "این اسکریپت را با sudo اجرا کنید: sudo bash install_ubuntu.sh"
  exit 1
fi

echo "==> نصب پیش‌نیازهای سیستمی..."
apt-get update -y
apt-get install -y python3 python3-venv python3-pip python3-dev build-essential \
                  libssl-dev libffi-dev libta-lib0 2>/dev/null || true

echo "==> ساخت پوشه ${APP_DIR}"
mkdir -p "${APP_DIR}"
# Copy project files (assumes script run from repo root)
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(dirname "${SCRIPT_DIR}")"
rsync -a --delete \
    --exclude ".git" --exclude "logs" --exclude "data" --exclude "__pycache__" \
    --exclude ".venv" --exclude "*.sqlite*" \
    "${PROJECT_ROOT}/" "${APP_DIR}/"
chown -R "$SUDO_USER":"$SUDO_USER" "${APP_DIR}"

echo "==> ساخت محیط مجازی..."
sudo -u "$SUDO_USER" "${PYTHON_BIN}" -m venv "${APP_DIR}/.venv"
sudo -u "$SUDO_USER" "${APP_DIR}/.venv/bin/python" -m pip install --upgrade pip
sudo -u "$SUDO_USER" "${APP_DIR}/.venv/bin/python" -m pip install -r "${APP_DIR}/requirements.txt"

echo "==> ساخت فایل .env اگر وجود ندارد..."
if [[ ! -f "${APP_DIR}/.env" ]]; then
  cp "${APP_DIR}/.env.example" "${APP_DIR}/.env"
  echo "  -> ${APP_DIR}/.env بسازید و API keyها را بنویسید."
fi

echo "==> ساخت فایل سرویس systemd..."
cat > "${SVC_FILE}" <<EOF
[Unit]
Description=MemeCoin Sniper - multi-exchange pump hunter
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=${SUDO_USER}
WorkingDirectory=${APP_DIR}
Environment="PYTHONUNBUFFERED=1"
Environment="PYTHONPATH=${APP_DIR}"
ExecStart=${APP_DIR}/.venv/bin/python ${APP_DIR}/main.py
Restart=on-failure
RestartSec=10
StandardOutput=append:${APP_DIR}/logs/systemd.log
StandardError=append:${APP_DIR}/logs/systemd.log
# gentle resource limits
MemoryMax=2G
CPUQuota=150%

[Install]
WantedBy=multi-user.target
EOF

mkdir -p "${APP_DIR}/logs"
chown -R "$SUDO_USER":"$SUDO_USER" "${APP_DIR}/logs"

systemctl daemon-reload
systemctl enable "${SERVICE_NAME}"

echo "==> باز کردن پورت داشبورد در UFW (اگر فعال است)..."
if command -v ufw >/dev/null && ufw status | grep -q "Status: active"; then
  ufw allow 8080/tcp || true
fi

echo "==> درخواست باز کردن فایروال انجام شد."
cat <<EOF

✅ نصب کامل شد.

دستورات مفید:
   sudo systemctl start ${SERVICE_NAME}     # شروع
   sudo systemctl status ${SERVICE_NAME}    # وضعیت
   sudo systemctl restart ${SERVICE_NAME}   # ری‌استارت
   sudo journalctl -u ${SERVICE_NAME} -f    # مشاهده لاگ زنده

داشبورد مرورگر:
   http://<IP-SERVER>:8080
EOF
