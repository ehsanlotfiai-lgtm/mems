"""Telegram notifier (best-effort, never blocks the engine)."""
from __future__ import annotations

from typing import Optional

from config.settings import Settings
from core.logging_setup import logger
from core.models import Signal


class TelegramNotifier:
    def __init__(self, settings: Settings) -> None:
        self.s = settings
        self.token = settings.telegram_token
        self.chat_id = settings.telegram_chat_id
        self.parse_mode = settings.telegram.get("parse_mode", "HTML")
        self.enabled = bool(settings.telegram.get("enabled")) and bool(self.token) and bool(self.chat_id)
        self.bot = None

    async def start(self) -> None:
        if not self.enabled:
            logger.info("Telegram notifier: disabled")
            return
        try:
            from telegram import Bot
            self.bot = Bot(token=self.token)
            await self.bot.send_message(chat_id=self.chat_id, text="🟢 <b>MemeCoin Sniper</b> آنلاین شد.")
            logger.info("Telegram notifier ready")
        except Exception as exc:  # noqa: BLE001
            logger.error(f"Telegram start failed: {exc}")
            self.enabled = False

    async def stop(self) -> None:
        if self.bot is not None:
            try:
                await (self.bot.updater.shutdown() if hasattr(self.bot, "updater") and self.bot.updater else None)
            except Exception:  # noqa: BLE001
                pass

    async def send_signal(self, sig: Signal) -> None:
        if not self.enabled or self.bot is None:
            return
        side_emoji = "🟢 خرید (LONG)" if sig.side.value == "long" else "🔴 فروش (SHORT)"
        msg = (
            f"<b>{side_emoji}</b> {sig.exchange} | <code>{sig.symbol}</code>\n"
            f"امتیاز: <b>{sig.score:.2f}</b>\n"
            f"قیمت ورود: <code>{sig.entry:.6f}</code>\n"
            f"🎯 TP: <code>{sig.take_profit:.6f}</code>  |  🛑 SL: <code>{sig.stop_loss:.6f}</code>\n"
            f"اندازه (USDT): <b>{sig.position_size_usdt:.2f}</b>  (ریسک {sig.risk_pct:.1f}%)\n"
            f"ATR: {sig.atr:.6f}\n\n"
            f"📊 <i>{sig.rationale}</i>"
        )
        try:
            await self.bot.send_message(chat_id=self.chat_id, text=msg, parse_mode=self.parse_mode)
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"Telegram signal send failed: {exc}")

    async def send_text(self, text: str) -> None:
        if not self.enabled or self.bot is None:
            return
        try:
            await self.bot.send_message(chat_id=self.chat_id, text=text, parse_mode=self.parse_mode)
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"Telegram text send failed: {exc}")
