"""In-dashboard assistant — rule-based with optional LLM upgrade.

Default mode: rule-based, Persian-speaking, zero-latency, no API fees.
Optional: swap to OpenAI-compatible LLM via config (use_llm: true).

Features:
  * Explains signals in plain Farsi.
  * Warns on risk conditions (drawdown, over-trading).
  * Answers common questions (what is a meme-coin? what is ATR? etc).
  * Suggests next actions depending on engine state.
  * (LLM mode) Complex natural language queries, context-aware responses.
"""
from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass
from typing import Any, Dict, List, Optional

from config.settings import Settings
from core.models import PaperPosition, Signal, Side
from core.risk import RiskEngine


# Static knowledge base (FAQ) keyed by simple keyword match.
FAQ = [
    (r"(میم.?کوین|meme.?coin)", (
        "🪙 میم‌کوین‌ها توکن‌هایی با الهام از میم‌های اینترنتی هستند که قیمتشان بیشتر توسط "
        "هیجان اجتماعی و نقدینگی پایین تعیین می‌شود، نه فاندامنتال. سود بالقوه عالی ولی "
        "ریسک پامپ و دامپ بسیار زیاد است."
    )),
    (r"(پامپ|pump)", (
        "🚀 پامپ یعنی افزایش شدید قیمت و حجم در زمان کوتاه. اسنایپر از همین نوسان‌ها "
        "سیگنال می‌گیرد: حجم برابر میانگین > 3x، شکست باند بولینگر و لیست‌شدن تازه "
        "نشانه‌های اصلی هستند."
    )),
    (r"(اتر|atir|atr)", (
        "📏 ATR (Average True Range) میانگین دامنه‌ی حرکت هر کندل است. ما از آن برای "
        "تعیین حد ضرر و سود استفاده می‌کنیم: SL = ورود ـ 1.5×ATR، TP = ورود + 3×ATR."
    )),
    (r"(حد.?ضرر|stop.?loss|sl)", (
        "🛑 Stop Loss محدودترین قیمتی است که در آن با ضرر از معامله خارج می‌شویم تا از "
        "وخیم‌تر شدن ضرر جلوگیری شود. اسنایپر به‌صورت داینامیک و با Trailing Stop این حد را "
        "در سود بالا می‌برد."
    )),
    (r"(تایم.?فریم|timeframe|tf)", (
        "⏱️ ما چهار تایم‌فریم را هم‌زمان تحلیل می‌کنیم: 1m (ماشه‌ای / ورود)، 5m، 15m و 1h (تأیید روند)."
        " هر چه بیشتر تایم‌فریم‌ها هم‌نظر باشند امتیاز سیگنال بالاتر است."
    )),
    (r"(واگرایی|divergence|rsi)", (
        "📊 واگرایی یعنی قیمت سقف جدید می‌زند ولی اندیکاتور (مثل RSI) سقف پایین‌تر می‌سازد. "
        "این هشدار ضعف روند است و می‌تواند نشانه‌ی بازگشت باشد."
    )),
    (r"(بک.?تست|backtest)", (
        "🧪 بک‌تست اجرای استراتژی روی داده گذشته است تا ببینی سود / ضرر و Win Rate چقدر بوده. "
        "بک‌تست نشان می‌دهد که استراتژی در شرایط مختلف بازار چطور رفتار می‌کند."
    )),
    (r"(فوروارد|forward|paper)", (
        "🟡 فوروارد تست اجرای زنده روی داده‌ی لحظه‌ای ولی با پول مجازی (Paper) است. "
        "احساس واقعی بازار بدون ریسک پول واقعی."
    )),
    (r"(ریسک|risk)", (
        "⚠️ هر معامله بین 0.5% تا 1%از سرمایه را ریسک کنید. هرگز در یک پامپ همه‌ی سرمایه "
        "را نگذارید. قانون حداکثر ضرر روزانه 5%ای ما از حساب محافظت می‌کند."
    )),
    (r"(چطور|چگونه|how)", (
        "🤝 داشبورد چهار بخش دارد: سیگنال‌های زنده، پوزیشن‌های باز، تاریخچه و نمودار. "
        "هر سیگنال دارای ورود، حد ضرر و حد سود است. با تأیید خودتان دستی وارد شوید."
    )),
    (r"(درباره|about|چه کسی|who)", (
        "🤖 من دستیار اسنایپر میم‌کوین هستم — یک ابزار شکار پامپ برای بایننس و بای‌بیت "
        "که ۸+۱ روش حرفه‌ای (Volume Spike، New Listing، Liquidity Grab، Social Momentum و…) "
        "را به‌صورت چند-تایم‌فریمی ترکیب می‌کند."
    )),
    (r"(استراتژی|strategy|strategies)", (
        "📋 ۹ استراتژی فعال داریم:\n"
        "1️⃣ New Listing — کوین تازه لیست شده\n"
        "2️⃣ Volume Spike — اسپایک حجم\n"
        "3️⃣ Order Book Imbalance — عدم تعادل سفارشات\n"
        "4️⃣ Liquidity Grab — شکار نقدینگی\n"
        "5️⃣ Momentum Ignition — احتراق مومنتوم\n"
        "6️⃣ RSI Divergence — واگرایی RSI\n"
        "7️⃣ BB Breakout — شکست بولینگر\n"
        "8️⃣ Funding/OI Spike — اسپایک funding\n"
        "9️⃣ Social Momentum — هیجان شبکه اجتماعی"
    )),
    (r"(اهرم|leverage)", (
        "⚡ اهرم (Leverage) ضریبی است که با آن می‌توانید با سرمایه کمتر، پوزیشن بزرگتر باز کنید. "
        "مثلاً اهرم 10x یعنی با 100 دلار، پوزیشن 1000 دلاری باز می‌کنید. "
        "در حالت اسپات اهرم 1x است."
    )),
]


@dataclass
class AssistantReply:
    text: str
    action_suggestion: Optional[str] = None


class Assistant:
    def __init__(self, settings: Settings, storage=None, risk: Optional[RiskEngine] = None) -> None:
        self.s = settings
        self.storage = storage
        self.risk = risk
        self.verbosity = settings.assistant.get("verbosity", "normal")
        # LLM configuration
        self.use_llm = bool(settings.assistant.get("use_llm", False))
        self.llm_client = None
        self.llm_model = settings.assistant.get("llm_model", "gpt-4o-mini")
        self.llm_max_tokens = int(settings.assistant.get("llm_max_tokens", 500))
        self.llm_temperature = float(settings.assistant.get("llm_temperature", 0.7))
        if self.use_llm:
            self._init_llm()

    def _init_llm(self) -> None:
        """Initialize LLM client (OpenAI-compatible API)."""
        try:
            import httpx
            api_key = self.s.raw.get("llm_api_key", "")
            base_url = self.s.raw.get("llm_base_url", "https://api.openai.com/v1")
            if not api_key:
                self.use_llm = False
                return
            self._http_client = httpx.AsyncClient(
                base_url=base_url,
                headers={"Authorization": f"Bearer {api_key}"},
                timeout=30,
            )
            self.llm_client = True  # marker
        except ImportError:
            self.use_llm = False

    # ---------------------------------------------------- main responder
    def respond(self, user_text: str, risk_state=None) -> AssistantReply:
        text = user_text.strip()
        if not text:
            return AssistantReply("سؤالی ندادید. مثلاً بپرسید: «پامپ چیست؟» یا «ریسک پوزیشن‌هام چطوره؟»",
                                  "فهرست پوزیشن‌های باز")
        # 1) Risk-related live question
        if re.search(r"(پوزیشن|حساب|ریسک|وضعیت|status|positions|account|drawdown)", text, re.IGNORECASE):
            if risk_state is not None:
                return self._risk_report(risk_state)
            return AssistantReply("اطلاعات زنده حساب فعلاً وصل نیست. بعدها دوباره امتحان کنید.")

        # 2) FAQ regex matching (always available)
        low = text.lower()
        for pat, answer in FAQ:
            if re.search(pat, low, re.IGNORECASE):
                return AssistantReply(answer, self._suggestion_for(pat))

        # 3) Greetings
        if any(w in low for w in ("سلام", "hi", "hello", "hey", "درود")):
            return AssistantReply("درود! 👋 دستیار اسنایپر آماده‌ی پاسخ. بپرسید «پامپ چیست؟» یا «اتر یعنی چی؟»")

        # 4) LLM fallback (if enabled)
        if self.use_llm and self.llm_client:
            return self._llm_respond(text, risk_state)

        # 5) Help / fallback
        return AssistantReply(
            "سؤال شما را دقیق‌تر بپرس. می‌توانید درباره پامپ، میم‌کوین، ATR، "
            "حد ضرر، واگرایی RSI، بک‌تست، فوروارد تست، استراتژی‌ها، اهرم، "
            "یا وضعیت پوزیشن‌ها بپرسید."
        )

    def _llm_respond(self, text: str, risk_state=None) -> AssistantReply:
        """Generate response using LLM (OpenAI-compatible API)."""
        import asyncio

        system_prompt = (
            "تو دستیار فارسی یک سیستم شکار میم‌کوین هستی. "
            "به سؤالات کاربر به فارسی پاسخ بده. "
            "مختصر و مفید باش. "
            "از ایموجی استفاده کن. "
            "هشدار ریسک بده وقتی لازم باشد."
        )
        # Build context from risk state
        context = ""
        if risk_state:
            context = (
                f"\nوضعیت فعلی حساب:\n"
                f"- موجودی: {risk_state.equity:,.2f} USDT\n"
                f"- پوزیشن‌های باز: {risk_state.open_count}\n"
                f"- PnL روزانه: {risk_state.daily_pnl_pct:+.2f}%\n"
                f"- مسدود: {'بله' if risk_state.blocked_until_tomorrow else 'خیر'}\n"
            )

        messages = [
            {"role": "system", "content": system_prompt + context},
            {"role": "user", "content": text},
        ]

        try:
            # Use sync call to avoid async complexity in rule-based path
            import httpx
            api_key = self.s.raw.get("llm_api_key", "")
            base_url = self.s.raw.get("llm_base_url", "https://api.openai.com/v1")
            resp = httpx.post(
                f"{base_url}/chat/completions",
                headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
                json={
                    "model": self.llm_model,
                    "messages": messages,
                    "max_tokens": self.llm_max_tokens,
                    "temperature": self.llm_temperature,
                },
                timeout=30,
            )
            if resp.status_code == 200:
                data = resp.json()
                answer = data["choices"][0]["message"]["content"]
                return AssistantReply(answer)
            else:
                # Fallback to rule-based
                return AssistantReply(
                    "متأسفانه LLM در دسترس نیست. "
                    "سؤال خود را از لیست موضوعات زیر بپرسید: "
                    "پامپ، ATR، حد ضرر، واگرایی، بک‌تست، استراتژی‌ها، اهرم."
                )
        except Exception:
            return AssistantReply(
                "خطا در ارتباط با LLM. "
                "از سؤالات پشتیبانی شده استفاده کنید."
            )

    def explain_signal(self, sig: Signal) -> str:
        side_fa = "خرید (LONG)" if sig.side == Side.LONG else "فروش (SHORT)"
        return (
            f"📌 سیگنال {side_fa} روی <b>{sig.symbol}</b> ({sig.exchange}). "
            f"امتیاز: <b>{sig.score:.2f}</b>.\n"
            f"ورود: {sig.entry:.6f} | TP: {sig.take_profit:.6f} | SL: {sig.stop_loss:.6f}.\n"
            f"روش‌های فعال: {', '.join(sorted({h.name for h in sig.hits}))}.\n"
            f"توصیف: {sig.rationale}"
        )

    # ---------------------------------------------------- helpers
    def _risk_report(self, state) -> AssistantReply:
        blocked = getattr(state, "blocked_until_tomorrow", False)
        msg = (
            f"💰 موجودی کاغذی: <b>{state.equity:,.2f} USDT</b>\n"
            f"📂 پوزیشن‌های باز: {state.open_count}\n"
            f"📊 سود/ضرر روزانه: <b>{state.daily_pnl_pct:+.2f}%</b>"
        )
        if blocked:
            msg += "\n\n⛔ ورود جدید امروز بسته شده است (حد ضرر روزانه {}). لطفاً تا فردا صبر کنید.".format(
                f"{self.s.risk.get('daily_max_loss_pct', 5)}%"
            )
            return AssistantReply(msg, f"فردا دوباره فعال شود")
        if state.daily_pnl_pct < -2:
            msg += "\n\n⚠️ افت محسوس امروز پیش می‌آید. توصیه می‌کنم حجم جدید را نصف کنید."
        else:
            msg += "\n\n✅ وضعیت کنترل‌شده است."
        return AssistantReply(msg)

    def _suggestion_for(self, pat: str) -> Optional[str]:
        if "atir" in pat or "atr" in pat:
            return "نمایش نمودار ATR"
        if "pump" in pat:
            return "نمایش پامپ‌های اخیر"
        if "meme" in pat:
            return "نمایش لیست واچ"
        if "strategy" in pat or "استراتژی" in pat:
            return "نمایش استراتژی‌ها"
        if "leverage" in pat or "اهرم" in pat:
            return "تنظیمات ریسک"
        return None
