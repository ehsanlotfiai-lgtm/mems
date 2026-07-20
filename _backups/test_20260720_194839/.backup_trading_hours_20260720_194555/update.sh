#!/usr/bin/env bash
# ============================================================
#  MemeCoin Sniper — اسکریپت آپدیت سرور
#
#  نحوه استفاده:
#    1) فایل‌های جدید را روی سرور آپلود کنید
#    2) chmod +x update.sh && sudo ./update.sh
# ============================================================

set -euo pipefail

# ---------- تنظیمات ----------
APP_DIR="/root/mems_sniper"
VENV_DIR="${APP_DIR}/venv"
SERVICE_NAME="memecoin-sniper"
# -------------------------------

RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'; CYAN='\033[0;36m'; NC='\033[0m'
info()  { echo -e "${CYAN}[INFO]${NC}  $*"; }
ok()    { echo -e "${GREEN}[OK]${NC}    $*"; }
warn()  { echo -e "${YELLOW}[WARN]${NC}  $*"; }
err()   { echo -e "${RED}[ERROR]${NC} $*"; }

echo ""
echo -e "${CYAN}╔══════════════════════════════════════════════════╗${NC}"
echo -e "${CYAN}║   🎯 MemeCoin Sniper — آپدیت خودکار            ║${NC}"
echo -e "${CYAN}╚══════════════════════════════════════════════════╝${NC}"
echo ""

# ---------- پیدا کردن مسیر پروژه ----------
# اول از جایی که اسکریپت در آن است، بعد از APP_DIR
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

if [[ -f "${SCRIPT_DIR}/main.py" ]]; then
    PROJECT_DIR="${SCRIPT_DIR}"
elif [[ -f "$(pwd)/main.py" ]]; then
    PROJECT_DIR="$(pwd)"
elif [[ -f "${APP_DIR}/main.py" ]]; then
    PROJECT_DIR="${APP_DIR}"
else
    err "مسیر پروژه پیدا نشد!"
    err "  لطفاً اسکریپت را از داخل پوشه پروژه اجرا کنید"
    err "  مثال:  cd /root/mems_sniper && sudo ./update.sh"
    exit 1
fi

info "مسیر پروژه: ${PROJECT_DIR}"
cd "${PROJECT_DIR}"

# ---------- مرحله ۱: بک‌آپ ----------
TOTAL_STEPS=5
info "مرحله ۱/${TOTAL_STEPS}: بک‌آپ..."
BACKUP_DIR="${PROJECT_DIR}.bak.$(date +%Y%m%d_%H%M%S)"
if [[ "${PROJECT_DIR}" != "/" ]]; then
    cp -r "${PROJECT_DIR}" "${BACKUP_DIR}" 2>/dev/null || true
    ok "بک‌آپ: ${BACKUP_DIR}"
fi

# ---------- مرحله ۲: فعال‌سازی venv ----------
info "مرحله ۲/۵: فعال‌سازی محیط مجازی..."
if [[ -d "${VENV_DIR}" ]]; then
    source "${VENV_DIR}/bin/activate"
    ok "venv فعال شد"
elif [[ -d "${PROJECT_DIR}/venv" ]]; then
    source "${PROJECT_DIR}/venv/bin/activate"
    ok "venv فعال شد"
else
    warn "venv پیدا نشد - ساخت..."
    python3 -m venv venv
    source venv/bin/activate
    ok "venv ساخته شد"
fi

# ---------- مرحله ۳: نصب پکیج‌ها ----------
info "مرحله ۳/۵: نصب/آپدیت وابستگی‌ها..."
pip install -q --upgrade pip
pip install -q -r requirements.txt
ok "وابستگی‌ها نصب شد"

# ---------- مرحله ۴: مایگریشن دیتابیس ----------
info "مرحله ۴/۵: مایگریشن دیتابیس..."
python3 -c "
import sqlite3, os, glob
# پیدا کردن دیتابیس
db_paths = glob.glob('data/sniper.sqlite') + glob.glob('**/sniper.sqlite', recursive=True)
if not db_paths:
    print('  ⚠️ دیتابیس پیدا نشد (با اجرای اول ساخته می‌شود)')
else:
    for db_path in db_paths:
        db = sqlite3.connect(db_path)
        cols_added = 0
        for col, typ, default in [
            ('leverage', 'REAL', '1.0'),
            ('market_type', 'TEXT', '\"spot\"'),
            ('fee_usdt', 'REAL', '0.0'),
            ('slippage_usdt', 'REAL', '0.0'),
        ]:
            try:
                db.execute(f'ALTER TABLE paper_trades ADD COLUMN {col} {typ} DEFAULT {default}')
                cols_added += 1
            except: pass
        db.commit(); db.close()
        if cols_added:
            print(f'  ✅ {cols_added} ستون جدید اضافه شد')
        else:
            print(f'  ✅ دیتابیس از قبل به‌روز است')
"
ok "مایگریشن تمام شد"

# ---------- مرحله ۵: کپی فایل‌ها به /opt (اگر لازم باشد) ----------
if [[ "${PROJECT_DIR}" != "${APP_DIR}" ]] && [[ -d "${APP_DIR}" ]]; then
    info "مرحله ۵/۶: کپی فایل‌ها به ${APP_DIR}..."
    rsync -a --exclude='__pycache__' --exclude='.git' --exclude='venv' \
          --exclude='data/*.sqlite' --exclude='.env' \
          --exclude='*.bak.*' --exclude='node_modules' \
          "${PROJECT_DIR}/" "${APP_DIR}/"
    ok "فایل‌ها به ${APP_DIR} کپی شد"
    TOTAL_STEPS=6
else
    TOTAL_STEPS=5
fi

# ---------- مرحله ۶: ریستارت سرویس ----------
info "مرحله ${TOTAL_STEPS}/${TOTAL_STEPS}: ریستارت سرویس..."
if systemctl is-active --quiet "${SERVICE_NAME}" 2>/dev/null; then
    sudo systemctl restart "${SERVICE_NAME}"
    sudo systemctl status "${SERVICE_NAME}" --no-pager -l
    ok "سرویس ریستارت شد"
elif systemctl is-enabled --quiet "${SERVICE_NAME}" 2>/dev/null; then
    sudo systemctl restart "${SERVICE_NAME}"
    ok "سرویس ریستارت شد"
else
    warn "سرویس systemd پیدا نشد"
    warn "اگر با tmux/screen اجرا می‌کنید، پروسه قبلی را بکشید:"
    warn "  pkill -f 'python.*main.py'"
    warn "  cd ${PROJECT_DIR} && source venv/bin/activate && python3 main.py"
fi

# ---------- نتیجه ----------
echo ""
echo -e "${GREEN}╔══════════════════════════════════════════════════╗${NC}"
echo -e "${GREEN}║   ✅ آپدیت با موفقیت تمام شد!                  ║${NC}"
echo -e "${GREEN}╚══════════════════════════════════════════════════╝${NC}"
echo ""
echo -e "  📊 داشبورد: http://$(hostname -I | awk '{print $1}'):8080"
echo -e "  📁 پروژه:   ${PROJECT_DIR}"
echo -e "  🔧 بک‌آپ:   ${BACKUP_DIR}"
echo ""
echo -e "  ${YELLOW}برای بررسی لاگ‌ها:${NC}"
echo -e "    sudo journalctl -u ${SERVICE_NAME} -f"
echo ""
