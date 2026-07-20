#!/usr/bin/env bash
# ============================================================
#  MemeCoin Sniper — اسکریپت نصب یک‌خطی روی اوبونتو 24.04
#
#  نحوه استفاده:
#    chmod +x install.sh && sudo ./install.sh
#
#  این اسکریپت انجام می‌دهد:
#    1. نصب وابستگی‌های سیستمی (Python 3.11, TA-Lib, و ...)
#    2. ساخت کاربر اختصاصی memecoin
#    3. کپی پروژه به /opt/memecoin-sniper
#    4. ساخت venv و نصب پکیج‌ها
#    5. ایجاد فایل .env از .env.example
#    6. ساخت و فعال‌سازی سرویس systemd
#    7. باز کردن پورت فایروال
# ============================================================

set -euo pipefail

# ---------- تنظیمات قابل تغییر ----------
APP_NAME="memecoin-sniper"
APP_USER="memecoin"
APP_DIR="/opt/${APP_NAME}"
VENV_DIR="${APP_DIR}/venv"
SERVICE_FILE="/etc/systemd/system/${APP_NAME}.service"
WEB_PORT=8080
PYTHON_VERSION="3.12"          # اوبونتو 24.04 پیش‌فرض Python 3.12 دارد
# -----------------------------------------

RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
CYAN='\033[0;36m'
NC='\033[0m'

info()  { echo -e "${CYAN}[INFO]${NC}  $*"; }
ok()    { echo -e "${GREEN}[OK]${NC}    $*"; }
warn()  { echo -e "${YELLOW}[WARN]${NC}  $*"; }
err()   { echo -e "${RED}[ERROR]${NC} $*"; }

# ---------- بررسی root ----------
if [[ $EUID -ne 0 ]]; then
    err "لطفاً با sudo اجرا کنید:  sudo ./install.sh"
    exit 1
fi

# ---------- بررسی اوبونتو ----------
if ! grep -qi "ubuntu" /etc/os-release 2>/dev/null; then
    warn "این اسکریپت برای اوبونتو 24.04 طراحی شده. ادامه با مسئولیت خودتان..."
fi

echo ""
echo -e "${CYAN}╔══════════════════════════════════════════════════╗${NC}"
echo -e "${CYAN}║   🎯 MemeCoin Sniper — اسکریپت نصب خودکار    ║${NC}"
echo -e "${CYAN}║   اوبونتو 24.04 LTS                           ║${NC}"
echo -e "${CYAN}╚══════════════════════════════════════════════════╝${NC}"
echo ""

# ============================================================
# مرحله ۱: نصب وابستگی‌های سیستمی
# ============================================================
info "مرحله ۱/۸: نصب وابستگی‌های سیستمی..."

apt-get update -qq

# Python 3.12 + venv + pip
apt-get install -y -qq \
    python3 \
    python3-pip \
    python3-venv \
    python3-dev \
    build-essential \
    curl \
    git \
    ufw \
    htop \
    tmux \
    libffi-dev \
    libssl-dev \
    libjpeg-dev \
    zlib1g-dev \
    libbz2-dev \
    libreadline-dev \
    libsqlite3-dev \
    libncursesw5-dev \
    xz-utils \
    tk-dev \
    libxml2-dev \
    libxmlsec1-dev \
    libyaml-dev

ok "وابستگی‌های سیستمی نصب شدند"

# ============================================================
# مرحله ۲: نصب TA-Lib (اختیاری — اگر نشد، pandas-ta جایگزین است)
# ============================================================
info "مرحله ۲/۸: نصب TA-Lib (اختیاری)..."

if python3 -c "import talib" 2>/dev/null; then
    ok "TA-Lib از قبل نصب است — رد شد"
else
    # روش ۱: از سورس با ابزارهای کامل
    ORIG_DIR="$(pwd)"
    cd /tmp
    if [[ ! -f ta-lib-0.4.0-src.tar.gz ]]; then
        curl -sL "https://prdownloads.sourceforge.net/ta-lib/ta-lib-0.4.0-src.tar.gz" -o ta-lib-0.4.0-src.tar.gz
    fi
    tar -xzf ta-lib-0.4.0-src.tar.gz 2>/dev/null || true

    if [[ -d ta-lib ]]; then
        cd ta-lib
        # اطمینان از وجود autotools
        apt-get install -y -qq libtool automake autoconf 2>/dev/null || true
        ./configure --prefix=/usr/local >/dev/null 2>&1 || true
        make clean 2>/dev/null || true
        BUILD_OK=true
        make -j"$(nproc)" 2>/dev/null || BUILD_OK=false
        if $BUILD_OK && make install 2>/dev/null; then
            ldconfig
            ok "TA-Lib C library از سورس نصب شد"
        else
            warn "TA-Lib از سورس کامپایل نشد — پروژه با pandas-ta ادامه می‌دهد"
            warn "  (استراتژی‌ها همچنان کار می‌کنند، فقط TA-Lib سریع‌تر است)"
        fi
        cd "${ORIG_DIR}"
    else
        cd "${ORIG_DIR}"
        warn "TA-Lib: خرابی فایل"
    fi
    rm -rf /tmp/ta-lib /tmp/ta-lib-0.4.0-src.tar.gz 2>/dev/null || true
fi

# نصب pip wrapper TA-Lib (فقط اگر C library نصب باشد)
if python3 -c "import talib" 2>/dev/null; then
    ok "TA-Lib Python wrapper آماده است"
else
    warn "TA-Lib نصب نشد — مشکلی نیست، pandas-ta جایگزین است"
fi

# ============================================================
# مرحله ۳: ساخت کاربر اختصاصی
# ============================================================
info "مرحله ۳/۸: ساخت کاربر ${APP_USER}..."

if id "${APP_USER}" &>/dev/null; then
    ok "کاربر ${APP_USER} از قبل وجود دارد"
else
    useradd --system --no-create-home --shell /usr/sbin/nologin "${APP_USER}"
    ok "کاربر ${APP_USER} ساخته شد"
fi

# ============================================================
# مرحله ۴: کپی پروژه
# ============================================================
info "مرحله ۴/۸: کپی پروژه به ${APP_DIR}..."

# مسیر اسکریپت فعلی = محلی که install.sh در آن است
# روش قابل اعتماد: هم از BASH_SOURCE و هم از pwd استفاده کن
REAL_PATH="$(readlink -f "${BASH_SOURCE[0]}" 2>/dev/null || echo "${BASH_SOURCE[0]}")"
SCRIPT_DIR="$(cd "$(dirname "${REAL_PATH}")" && pwd)"

# اگر هنوز مسیر درست نبود، از پوشه فعلی shell استفاده کن
if [[ ! -f "${SCRIPT_DIR}/requirements.txt" ]]; then
    # سعی کن از پوشه‌ای که کاربر در آن هست پیدا کن
    if [[ -f "$(pwd)/requirements.txt" ]]; then
        SCRIPT_DIR="$(pwd)"
    elif [[ -f "$(pwd)/install.sh" ]]; then
        SCRIPT_DIR="$(pwd)"
    fi
fi

info "مسیر اسکریپت: ${SCRIPT_DIR}"

# بررسی وجود فایل‌های حیاتی
if [[ ! -f "${SCRIPT_DIR}/requirements.txt" ]] || [[ ! -f "${SCRIPT_DIR}/main.py" ]]; then
    err "فایل‌های پروژه یافت نشدند در ${SCRIPT_DIR}"
    err "  لطفاً install.sh را از داخل پوشه پروژه اجرا کنید"
    err "  مثال:  cd /root/mems_sniper && sudo ./install.sh"
    err "  لیست فایل‌های موجود در مسیر جاری: $(ls)"
    exit 1
fi

if [[ "${SCRIPT_DIR}" == "${APP_DIR}" ]]; then
    ok "پروژه در ${APP_DIR} قرار دارد — نیازی به کپی نیست"
else
    mkdir -p "${APP_DIR}"
    # کپی همه فایل‌ها (بدون __pycache__)
    rsync -a --exclude='__pycache__' --exclude='.git' --exclude='venv' \
          --exclude='data/*.sqlite' --exclude='.env' \
          "${SCRIPT_DIR}/" "${APP_DIR}/"

    # بررسی کپی شدن فایل‌های حیاتی
    if [[ ! -f "${APP_DIR}/requirements.txt" ]]; then
        err "requirements.txt کپی نشد! تلاش دوباره..."
        cp "${SCRIPT_DIR}/requirements.txt" "${APP_DIR}/requirements.txt"
    fi
    if [[ ! -f "${APP_DIR}/main.py" ]]; then
        err "main.py کپی نشد! تلاش دوباره..."
        cp "${SCRIPT_DIR}/main.py" "${APP_DIR}/main.py"
    fi
    ok "پروژه کپی شد ($(ls ${APP_DIR} | wc -l) فایل/پوشه)"
fi

# نصب ابزار مدیریت CLI
if [[ -f "${APP_DIR}/deploy/sniper" ]]; then
    cp "${APP_DIR}/deploy/sniper" "/usr/local/bin/sniper"
    chmod +x "/usr/local/bin/sniper"
    ok "ابزار CLI 'sniper' نصب شد — از هر جا قابل اجراست"
fi

# ============================================================
# مرحله ۵: ساخت venv و نصب پکیج‌ها
# ============================================================
info "مرحله ۵/۸: ساخت محیط مجازی و نصب وابستگی‌ها..."

# بررسی وجود requirements.txt
if [[ ! -f "${APP_DIR}/requirements.txt" ]]; then
    err "فایل requirements.txt در ${APP_DIR} یافت نشد!"
    err "  لیست فایل‌های موجود:"
    ls -la "${APP_DIR}/" | head -20
    exit 1
fi

if [[ ! -d "${VENV_DIR}" ]]; then
    python3 -m venv "${VENV_DIR}"
fi

# آپدیت pip
"${VENV_DIR}/bin/pip" install --quiet --upgrade pip setuptools wheel

# نصب requirements
"${VENV_DIR}/bin/pip" install --quiet -r "${APP_DIR}/requirements.txt"

ok "وابستگی‌های پایتون نصب شدند"

# ============================================================
# مرحله ۶: ایجاد .env
# ============================================================
info "مرحله ۶/۸: آماده‌سازی فایل .env..."

if [[ ! -f "${APP_DIR}/.env" ]]; then
    cp "${APP_DIR}/.env.example" "${APP_DIR}/.env"
    chmod 600 "${APP_DIR}/.env"
    warn "فایل .env ساخته شد — لطفاً کلیدهای API را پر کنید:"
    echo "    nano ${APP_DIR}/.env"
else
    ok "فایل .env از قبل وجود دارد"
fi

# ساخت پوشه data
mkdir -p "${APP_DIR}/data"
chmod 750 "${APP_DIR}/data"

# ============================================================
# مرحله ۷: تنظیم مالکیت و دسترسی
# ============================================================
info "مرحله ۷/۸: تنظیم مالکیت فایل‌ها..."

chown -R "${APP_USER}:${APP_USER}" "${APP_DIR}"
chmod -R u=rwX,g=rX,o= "${APP_DIR}"
chmod 600 "${APP_DIR}/.env"
chmod 750 "${APP_DIR}/data"

ok "مالکیت تنظیم شد"

# ============================================================
# مرحله ۸: سرویس systemd
# ============================================================
info "مرحله ۸/۸: نصب سرویس systemd..."

# کپی فایل service
if [[ -f "${APP_DIR}/deploy/memecoin-sniper.service" ]]; then
    cp "${APP_DIR}/deploy/memecoin-sniper.service" "${SERVICE_FILE}"
else
    # اگر فایل deploy نبود، inline بساز
    cat > "${SERVICE_FILE}" << SVCEOF
[Unit]
Description=MemeCoin Sniper — Pump Hunter
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=${APP_USER}
Group=${APP_USER}
WorkingDirectory=${APP_DIR}
ExecStart=${VENV_DIR}/bin/python main.py
Restart=always
RestartSec=10
EnvironmentFile=${APP_DIR}/.env
Environment=PYTHONUNBUFFERED=1
Environment=PYTHONDONTWRITEBYTECODE=1
NoNewPrivileges=true
ProtectSystem=strict
ProtectHome=true
ReadWritePaths=${APP_DIR}/data ${APP_DIR}/logs
PrivateTmp=true
LimitNOFILE=65536
MemoryMax=512M
StandardOutput=journal
StandardError=journal
SyslogIdentifier=${APP_NAME}

[Install]
WantedBy=multi-user.target
SVCEOF
fi

systemctl daemon-reload
systemctl enable "${APP_NAME}" 2>/dev/null
systemctl restart "${APP_NAME}"
sleep 2

if systemctl is-active --quiet "${APP_NAME}"; then
    ok "سرویس ${APP_NAME} فعال شد ✅"
else
    warn "سرویس هنوز فعال نشده — ممکن است نیاز به تنظیم .env باشد"
    echo "    systemctl status ${APP_NAME}"
    echo "    journalctl -u ${APP_NAME} -f"
fi

# ============================================================
# فایروال (UFW)
# ============================================================
if command -v ufw &>/dev/null; then
    ufw allow "${WEB_PORT}/tcp" comment "MemeCoin Sniper Dashboard" 2>/dev/null || true
    ufw --force enable 2>/dev/null || true
    ok "پورت ${WEB_PORT} در فایروال باز شد"
fi

# ============================================================
# پیام پایانی
# ============================================================
echo ""
echo -e "${GREEN}╔══════════════════════════════════════════════════════════╗${NC}"
echo -e "${GREEN}║          ✅  نصب MemeCoin Sniper تمام شد!             ║${NC}"
echo -e "${GREEN}╚══════════════════════════════════════════════════════════╝${NC}"
echo ""
echo -e "  🌐 داشبورد:"
echo -e "     http://$(hostname -I | awk '{print $1}'):${WEB_PORT}"
echo ""
echo -e "  📝 فایل تنظیمات:"
echo -e "     ${APP_DIR}/.env"
echo ""
echo -e "  🔧 دستورات مفید:"
echo -e "     ${CYAN}sniper status${NC}                — وضعیت سرویس"
echo -e "     ${CYAN}sniper restart${NC}               — ریستارت"
echo -e "     ${CYAN}sniper stop${NC}                  — توقف"
echo -e "     ${CYAN}sniper logs${NC}                  — لاگ زنده"
echo -e "     ${CYAN}sniper logs-recent 30m${NC}      — لاگ ۳۰ دقیقه اخیر"
echo -e "     ${CYAN}sniper edit${NC}                  — ویرایش .env"
echo -e "     ${CYAN}sniper test${NC}                  — تست اتصال صرافی‌ها"
echo -e "     ${CYAN}sniper backtest${NC}              — اجرای بک‌تست"
echo ""
echo -e "  📂 مسیر پروژه:"
echo -e "     ${APP_DIR}"
echo ""
echo -e "  ⚡ برای شروع:"
echo -e "     1) کلیدهای API را در .env پر کنید:"
echo -e "        ${CYAN}sniper edit${NC}"
echo -e "     2) سرویس را ریستارت کنید:"
echo -e "        ${CYAN}sniper restart${NC}"
echo -e "     3) تست اتصال را اجرا کنید:"
echo -e "        ${CYAN}sniper test${NC}"
echo -e "     4) داشبورد را باز کنید:"
echo -e "        ${CYAN}http://$(hostname -I | awk '{print $1}'):${WEB_PORT}${NC}"
echo ""
echo -e "  🎯 موفق باشید در شکار میم‌کوین‌ها!"
echo ""
