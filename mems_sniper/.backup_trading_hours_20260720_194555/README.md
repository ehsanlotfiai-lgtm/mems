# 🎯 MemeCoin Sniper — شکارچی پامپ میم‌کوین

> ابزار چند-صرافی، چند-تایم‌فریمی، همیشه-آنلاین برای شناسایی و شکار پامپ میم‌کوین‌ها
> با **بک‌تست**، **فوروارد تست (Paper Trading)**، **داشبورد وب گرافیکی زنده** و **دستیار فارسی**.

این پروژه با **Python 3.12** نوشته شده و برای اجرای دائمی روی سرور **Ubuntu 24.04** به‌صورت سرویس **systemd** طراحی شده است.

---

## ✨ ویژگی‌ها

| دسته | ویژگی |
|---|---|
| صرافی‌ها (CEX) | Binance و Bybit (و هر صرافی ccxt از طریق کلاینت ccxt) |
| صرافی‌ها (DEX) | Pump.fun, Raydium, PancakeSwap, Uniswap (از طریق DexScreener + GeckoTerminal) |
| داده‌ها | REST + WebSocket (CEX) / REST Polling (DEX) |
| استراتژی‌ها | ۸ روش شکار حرفه‌ای (پایین را ببینید) |
| تایم‌فریم | چند-تایم‌فریمی با وزن‌دهی Confluence (1m / 5m / 15m / 1h) |
| بک‌تست | موتور Event-driven با محاسبه Sharpe / Sortino / PF / MDD |
| فوروارد تست | اجرای زنده WebSocket + مدیریت پوزیشن کاغذی (Paper) |
| ریسک | Stop-Loss، Take-Profit، Trailing Stop، Position Sizing، Daily Loss Limit |
| رابط گرافیکی | داشبورد وب RTL فارسی با Chart.js، WebSocket زنده |
| دستیار | چت‌بات فارسی روی داشبورد: توضیح سیگنال، هشدار ریسک، آموزش |
| آگاهی | ارسال سیگنال به تلگرام (اختیاری) |
| Persist | SQLite (WAL) برای سیگنال‌ها، تریدها و لاگ دستیار |
| استقرار | اسکریپت `install.sh` یک‌خطی برای اوبونتو 24.04 + systemd + ابزار `sniper` CLI |

---

## 📊 ۸ روش شکار میم‌کوین

| # | روش | توضیح کوتاه |
| - | --- | --- |
| 1 | **New Listing Sniper** | کوین‌های تازه‌لیست‌شده (Resources تا 72 ساعت) — بیشترین آلفا |
| 2 | **Volume Spike** | حجم چند برابر میانگین با امتیازدهی z-score |
| 3 | **Order Book Imbalance** | نسبت حجم Bid/Ask با عمق سفارش‌گذاری |
| 4 | **Liquidity Grab / Spring** | شکستن سقف/کف و برگشت به داخل (Spring/Upthrust) |
| 5 | **Momentum Ignition** | کندل اولیه + کندل تأیید پامپ/دامپ |
| 6 | **RSI Divergence** | واگرایی گاو/خرسی RSI با 锺 lows پشت سرهم |
| 7 | **BB Breakout** | شکست باند بولینگر با بافر دقیق |
| 8 | **Funding + OI Spike** | برای فیوچرز: اسپایک نرخ تأمین + Open Interest |

هر روش یک کلاس کوتاه در `strategies/strategies.py` است و در موتور `ConfluenceEngine` با وزن تایم‌فریم ادغام می‌شود.

---

## 🔗 لایه DEX — شکار میم‌کوین‌های تازه‌launch شده

میم‌کوین‌های واقعی **قبل از لیست شدن در صرافی‌های متمرکز** روی DEXها عرضه می‌شوند.
این لایه توکن‌های تازه را از منابع زیر کشف و ردیابی می‌کند:

| منبع | زنجیره | توضیح |
|---|---|---|
| **Pump.fun** | Solana | گراند-صفر میم‌کوین‌ها — تازه‌ترین launch ها |
| **Raydium** | Solana | بزرگترین DEX سولانا |
| **PancakeSwap** | BNB Chain | DEX زنجیره BNB |
| **Uniswap** | Ethereum | DEX اصلی اتریوم |
| **DexScreener** | همه | آگریگاتور ۹۰+ DEX |
| **GeckoTerminal** | همه | داده OHLCV کندل برای DEX |

### نحوه کار

1. **Discovery Loop** (هر ۵ دقیقه): توکن‌های جدید از Pump.fun + DexScreener کشف → فیلتر بر اساس نقدینگی، حجم و سن
2. **Snipe Loop** (هر 30 ثانیه): قیمت توکن‌های ردیابی‌شده رفرش → ارزیابی با ۸ استراتژی → سیگنال
3. توکن‌های DEX از همان مسیر کد استراتژی (ConfluenceEngine) عبور می‌کنند — هیچ کد تکراری نیست
4. سیگنال‌های DEX در داشبورد با برچسب 🔗 DEX و نام زنجیره نمایش داده می‌شوند

تنظیمات در `config.yaml` بخش `dex:` — از جمله حداقل نقدینگی، حداقل حجم، حداکثر سن، و لیست DEXهای فعال.

---

## 📁 ساختار پروژه

```
mems_sniper/
├── config/
│   ├── config.yaml             # تمام تنظیمات (CEX + DEX + ریسک + ...)
│   └── settings.py             # loader ترکیب‌یافته با .env
├── core/
│   ├── exchange.py             # ccxt + WebSocket
│   ├── dex.py                  # DEX: DexScreener, Pump.fun, GeckoTerminal
│   ├── universe.py             # ساخت واچ‌لیست میم‌کوین‌ها
│   ├── forward_engine.py       # موتور زنده همیشه-آنلاین (+ DEX loops)
│   ├── risk.py                 # ریسک‌منجمنت کاغذی
│   ├── storage.py              # SQLite (async)
│   ├── models.py               # Signal / Position / Candle
│   └── logging_setup.py        # loguru
├── strategies/
│   ├── indicators.py           # RSI / BB / ATR / MFI ...
│   ├── strategies.py           # ۸ روش شکار
│   └── strategy_engine.py      # Confluence تب چند تایم‌فریم
├── backtest/
│   └── engine.py               # موتور بک‌تست
├── notify/
│   └── telegram_bot.py         # آگاهی تلگرام
├── assistant/
│   └── __init__.py             # دستیار فارسی
├── frontend/
│   ├── templates/index.html    # داشبورد RTL
│   └── static/{dashboard.css, dashboard.js}
├── deploy/
│   ├── memecoin-sniper.service # فایل systemd
│   └── sniper                  # ابزار CLI مدیریت
├── scripts/
│   └── run_backtest.py         # بک‌تست CLI
├── server.py                   # FastAPI + WS
├── main.py                     # ورودی اصلی
├── install.sh                  # ⭐ اسکریپت نصب یک‌خطی اوبونتو
├── requirements.txt
├── .env.example
└── README.md
```

---

## 🚀 نصب روی سرور اوبونتو 24.04

### روش آسان (یک دستور)

```bash
# 1) فایل‌ها را به سرور منتقل کنید (یا git clone کنید)
scp -r mems_sniper/ root@YOUR_SERVER:/tmp/mems_sniper

# 2) روی سرور اجرا کنید
ssh root@YOUR_SERVER
cd /tmp/mems_sniper
sudo ./install.sh
```

اسکریپت `install.sh` به‌صورت خودکار انجام می‌دهد:
- ✅ نصب Python 3.12 + TA-Lib + وابستگی‌ها
- ✅ ساخت کاربر اختصاصی `memecoin`
- ✅ کپی پروژه به `/opt/memecoin-sniper`
- ✅ ساخت venv و نصب پکیج‌ها
- ✅ ایجاد فایل `.env` از الگو
- ✅ ساخت و فعال‌سازی سرویس systemd
- ✅ باز کردن پورت در فایروال
- ✅ نصب ابزار CLI `sniper`

### بعد از نصب

```bash
# ۱) کلیدهای API را تنظیم کنید
sniper edit

# ۲) سرویس را ریستارت کنید
sniper restart

# ۳) تست اتصال صرافی‌ها
sniper test

# ۴) داشبورد را باز کنید
# http://YOUR_SERVER_IP:8080
```

### ابزار CLI `sniper`

| دستور | توضیح |
|---|---|
| `sniper status` | وضعیت سرویس |
| `sniper logs` | لاگ زنده |
| `sniper logs-recent 1h` | لاگ یک ساعت اخیر |
| `sniper restart` | ریستارت |
| `sniper stop` | توقف |
| `sniper start` | شروع |
| `sniper edit` | ویرایش .env |
| `sniper test` | تست اتصال صرافی‌ها |
| `sniper backtest` | اجرای بک‌تست |

### نصب دستی (بدون install.sh)

```bash
# ۱) وابستگی‌ها
sudo apt update
sudo apt install python3.12 python3.12-venv python3-pip build-essential git ufw

# ۲) پروژه
sudo mkdir -p /opt/memecoin-sniper
sudo cp -r . /opt/memecoin-sniper/
cd /opt/memecoin-sniper

# ۳) venv + پکیج‌ها
python3.12 -m venv venv
source venv/bin/activate
pip install -r requirements.txt

# ۴) .env
cp .env.example .env
nano .env

# ۵) systemd
sudo cp deploy/memecoin-sniper.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable memecoin-sniper
sudo systemctl start memecoin-sniper

# ۶) فایروال
sudo ufw allow 8080/tcp
```

---

## 🧪 اجرای بک‌تست (CLI)

برای تست تک‌نمادی قبل از فعال‌سازی استراتژی:

```bash
# از طریق ابزار CLI
sniper backtest --exchange binance --symbol PEPE/USDT --limit 3000

# یا مستقیم
python -m mems_sniper.scripts.run_backtest --exchange binance --symbol PEPE/USDT --limit 3000
```

نتیجه در `data/backtest_binance_PEPE_USDT.json` ذخیره می‌شود.

بک‌تست از همین کد مسیر استراتژی استفاده می‌کند (نه یک loop جدا)، پس اعداد رابطه‌ی نزدیکی با رفتار زنده دارد.

---

## 🌐 داشبورد وب

بخش‌ها:
- **📡 سیگنال‌ها** — کارت‌های زنده با امتیاز، روش‌های فعال، ورود/TP/SL و توضیح فارسی
- **📂 پوزیشن‌ها** — جدول کامل تریدهای کاغذی و PnL
- **📈 نمودار** — نمودار زنده قیمت هر نماد هر تایم‌فریم
- **🧪 بک‌تست** — اجرای بک‌تست از داخل مرورگر با منحنی سرمایه و جدول تریدها
- **👁️ واچ‌لیست** — لیست نمادهای فعال برای شکار
- **🤖 دستیار** — چت‌بات فارسی

---

## 🤖 دستیار فارسی

چت‌بات رو-داشبورد مفاهیم را توضیح می‌دهد:
- «پامپ چیست؟»
- «اتر یعنی چی؟»
- «وضعیت پوزیشن‌هام چطوره؟» (با خواندن RiskEngine واقعی)
- «چطور استفاده کنم؟»

تنظیم نحوه مکالمه در `config.yaml` بخش `assistant.verbosity`.

---

## ⚙️ تنظیم اصلی (`config/config.yaml`)

تنظیم‌هایی که احتمالاً می‌خواهید تغییر دهید:

```yaml
universe:
  meme_keywords: [DOGE, SHIB, PEPE, FLOKI, BONK, WIF, MEME, TURBO, NEIRO, BOOK]
  include_new_listed_hours: 72
  min_quote_volume_24h: 1_000_000

timeframes: ["1m", "5m", "15m", "1h"]
trigger_timeframe: "1m"
min_signal_score: 0.55

risk:
  risk_per_trade_pct: 1.0
  initial_paper_balance: 10000.0
  stop_loss_atr_mult: 1.5
  take_profit_atr_mult: 3.0
  trailing_activate_atr_mult: 2.0
  max_open_positions: 8
  daily_max_loss_pct: 5.0
```

یلزن ThnFrs …

---

## 🔐 امنیت

- **هیچ کلیدی در `config.yaml` نباشد** — `.env` را با `cp .env.example .env` بسازید.
- `.env` در `.gitignore` وجود دارد — کلیدها در گیت نمی‌روند.
- دیفالت **Paper Trading** است و هیچ سفارش واقعی ثبت نمی‌شود. برای فعال‌سازی ترید واقعی لازم است کد بومی در `core/forward_engine.py` و `core/risk.py` تغییر کند و قانوناً مسئولیت با شماست.

---

## 🧠 فلسفه طراحی

- **همان کد مسیر استراتژی برای بک‌تست و فوروارد** — اعداد قابل اعتماد.
- **همیشه-آنلاین** — موتور در پس‌زمینه systemd اجرا می‌شود و در صورت قطعی خودکار restart می‌شود.
- **WebSocket اول** — در حالت پایدار REST فقط برای bootstrap universe استفاده می‌شود و حداقل rate limit مصرف می‌شود.
- **ریسک-اول** — حداکثر ضرر روزانه، حداکثر پوزیشن باز، position sizing از بدنه‌ی распоряж‌ی.

---

## ⚠️ ریسک‌های واقعی و بیانیه

این ابزار فقط سیگنال می‌دهد و در حالت دیفالت هیچ پول واقعی ترید نمی‌کند. پامپ میم‌کوین
ریسک بسیار بالایی دارد — اکثریت پروژه‌ها به صفر می‌رسند. از این ابزار فقط به‌عنوان
کمک‌تحلیل استفاده کنید و هرگز با پولی که تاب از دست دادن آن را ندارید ترید نکنید. مسئولیت
کامل تصمیم‌های تجاری با شماست.

---

## 🛣️ بهبودهای آینده (پلن)

- [x] اتصال DEX (Pump.fun, Raydium, PancakeSwap, Uniswap از طریق DexScreener)
- [ ] اتصال از Twitter/Telegram public scraping برای Social Momentum
- [ ] لاگ‌کنندهی کامل داده (Parquet) برای تحلیل آفلاین
- [ ] رابط موبایل نیتیو (PWA)
- [ ] LLM اسیستانس (با op-in API)
