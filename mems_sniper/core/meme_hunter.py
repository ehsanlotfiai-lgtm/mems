"""Meme Coin Hunter — شکار میم‌کوین‌های پامپی با ۴ استراتژی واقعی.

منابع داده:
  - DexScreener  — قیمت/حجم/نقدینگی از ۹۰+ DEX
  - Pump.fun     — تازه‌ترین لانچ‌ها روی سولانا
  - GMGN.ai      — ردیابی پول هوشمند، تشخیص Bundle، فعالیت Insider
  - Birdeye      — تحلیل دقیق قیمت سولانا

استراتژی‌ها:
  1. Pre-Pump Hunter   — شکار قبل از پامپ (سن < ۳۰ دقیقه، حجم رشد‌یافته)
  2. Post-Migration    — بعد از Migration (منتقل شده از bonding curve)
  3. Smart Money       — دنبال‌کردن کیف پول‌های هوشمند از طریق GMGN
  4. Narrative Hunter  — شکار روایت‌های داغ (AI, Trump, Grok, ...)
"""
from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Set

from core.dex import DEXManager, DEXToken, GMGNClient
from core.logging_setup import logger
from core.hunter_tracker import DetectionRecord, get_hunter_tracker


# ==========================================================
# Data models
# ==========================================================

@dataclass
class HunterHit:
    """A single hunter strategy detecting a meme coin opportunity."""
    strategy: str          # pre_pump | post_migration | smart_money | narrative
    token_key: str         # "chain:address"
    token: DEXToken
    score: float           # 0..1 confidence
    signals: List[str]     # which sub-signals triggered
    risk_flags: List[str] = field(default_factory=list)  # danger signals
    detail: Dict[str, Any] = field(default_factory=dict)
    detected_at: float = field(default_factory=time.time)

    def to_dict(self) -> dict:
        return {
            "strategy": self.strategy,
            "token_key": self.token_key,
            "symbol": self.token.symbol,
            "name": self.token.name,
            "chain": self.token.chain,
            "dex": self.token.dex,
            "address": self.token.address,
            "price_usd": self.token.price_usd,
            "liquidity_usd": self.token.liquidity_usd,
            "volume_1h_usd": self.token.volume_1h_usd,
            "volume_24h_usd": self.token.volume_24h_usd,
            "mcap": self.token.mcap,
            "age_seconds": self.token.age_seconds,
            "price_change_5m_pct": self.token.price_change_5m_pct,
            "price_change_1h_pct": self.token.price_change_1h_pct,
            "price_change_24h_pct": self.token.price_change_24h_pct,
            "txns_24h": self.token.txns_24h,
            "score": round(self.score, 4),
            "signals": self.signals,
            "risk_flags": self.risk_flags,
            "detail": self.detail,
            "detected_at": self.detected_at,
            "url": self.token.url,
            "is_brand_new": self.token.is_brand_new,
            "is_fresh": self.token.is_fresh,
        }


@dataclass
class HunterConfig:
    """Configuration for the meme hunter."""
    # Pre-pump
    pre_pump_max_age_seconds: int = 1800
    pre_pump_min_volume_1h: float = 500
    pre_pump_min_liquidity_usd: float = 500
    pre_pump_max_dev_pct: float = 30.0

    # Post-migration
    post_migration_min_age_seconds: int = 3600
    post_migration_max_age_seconds: int = 86400
    post_migration_min_volume_1h: float = 5000
    post_migration_min_liquidity: float = 5000
    post_migration_min_txns: int = 20

    # Smart money
    smart_money_min_wallets: int = 2
    smart_money_known_wallets: List[str] = field(default_factory=list)

    # Narrative
    narrative_keywords: List[str] = field(default_factory=lambda: [
        "AI", "GROK", "TRUMP", "ELON", "MUSK", "DOGE", "PEPE", "SHIB",
        "CAT", "DOG", "FROG", "MONKEY", "BEAR", "BULL", "ROBOT",
        "MEME", "PUMP", "MOON", "ROCKET", "DEGEN", "WIF", "BONK",
        "NEIRO", "TURBO", "BOOK", "FLOKI", "SPX", "GIGA",
    ])
    narrative_min_score: float = 0.3

    # General
    scan_interval_seconds: int = 30
    max_results_per_strategy: int = 20


def config_from_yaml(d: Dict[str, Any]) -> HunterConfig:
    c = HunterConfig()
    if not d:
        return c
    for field_name in [
        "pre_pump_max_age_seconds", "pre_pump_min_volume_1h",
        "pre_pump_min_liquidity_usd", "pre_pump_max_dev_pct",
        "post_migration_min_age_seconds", "post_migration_max_age_seconds",
        "post_migration_min_volume_1h", "post_migration_min_liquidity",
        "post_migration_min_txns", "smart_money_min_wallets",
        "narrative_min_score", "scan_interval_seconds", "max_results_per_strategy",
    ]:
        if field_name in d:
            setattr(c, field_name, type(getattr(c, field_name))(d[field_name]))
    if "smart_money_known_wallets" in d:
        c.smart_money_known_wallets = list(d["smart_money_known_wallets"])
    if "narrative_keywords" in d:
        c.narrative_keywords = list(d["narrative_keywords"])
    return c


# ==========================================================
# Strategy 1: Pre-Pump Hunter — شکار قبل از پامپ
# ==========================================================

class PrePumpHunter:
    """پیدا کردن توکن‌های تازه قبل از انفجار قیمت.

    فیلترهای واقعی:
    ۱. عمر < ۳۰ دقیقه
    ۲. حجم ۱ ساعته در حال رشد (مقایسه با میانگین)
    ۳. تعداد تراکنش بالا نسبت به سن
    ۴. نقدینگی قفل شده (pump.fun bonding curve = auto-locked)
    ۵. توسعه‌دهنده سهم زیادی نگه نداشته باشد (از GMGN)
    ۶. Bundle نباشد (خرید همزمان از چند کیف پول مشکوک)
    """

    def __init__(self, config: HunterConfig, gmgn: GMGNClient) -> None:
        self.cfg = config
        self.gmgn = gmgn

    async def evaluate(self, tokens: List[DEXToken]) -> List[HunterHit]:
        hits: List[HunterHit] = []
        now = time.time()

        for t in tokens:
            age = t.age_seconds if t.age_seconds > 0 else (now - t.created_at if t.created_at else 999999)
            if age > self.cfg.pre_pump_max_age_seconds:
                continue

            # ──── Hard gates: reject tokens with insufficient quality ────
            if t.liquidity_usd > 0 and t.liquidity_usd < self.cfg.pre_pump_min_liquidity_usd:
                continue  # hard filter: liquidity too low
            if t.volume_1h_usd < 500:
                continue  # hard filter: no meaningful volume
            # If token is older than 10 min, require higher volume and transactions
            if age > 600 and t.volume_1h_usd < 2000:
                continue  # old tokens must have significant volume
            if t.txns_24h < 10:
                continue  # hard filter: need real trading activity
            if t.price_change_5m_pct > 80:
                continue  # too late — pump already happened, buying the top
            if t.price_change_1h_pct > 200:
                continue  # massive pump already done

            signals: List[str] = []
            risk_flags: List[str] = []
            score_parts: List[float] = []

            # ──── Signal 1: Token freshness ────
            if age < 300:        # < 5 min
                signals.append("🔥 خیلی تازه (< ۵ دقیقه)")
                score_parts.append(0.35)
            elif age < 600:      # < 10 min
                signals.append("🆕 تازه (< ۱۰ دقیقه)")
                score_parts.append(0.30)
            elif age < 900:      # < 15 min
                signals.append("⏰ خیلی تازه (< ۱۵ دقیقه)")
                score_parts.append(0.25)
            elif age < 1800:     # < 30 min
                signals.append("⏱️ تازه (< ۳۰ دقیقه)")
                score_parts.append(0.15)

            # ──── Signal 2: Volume relative to age ────
            if t.volume_1h_usd > 0 and age > 0:
                vol_per_min = t.volume_1h_usd / max(age / 60, 1)
                if vol_per_min > 50:      # > $50/min = $3k/hr
                    signals.append(f"📊 حجم بالا (${t.volume_1h_usd:,.0f}/h)")
                    score_parts.append(min(0.30, vol_per_min / 200))
                elif vol_per_min > 10:    # > $10/min = $600/hr
                    signals.append(f"📈 حجم متوسط (${t.volume_1h_usd:,.0f}/h)")
                    score_parts.append(0.10)

            # ──── Signal 3: Transaction activity ────
            if t.txns_24h > 0 and age > 0:
                txns_per_min = t.txns_24h / max(age / 60, 1)
                if txns_per_min > 5:
                    signals.append(f"🔥 تراکنش بالا ({t.txns_24h} تراکنش)")
                    score_parts.append(min(0.20, txns_per_min / 20))
                elif txns_per_min > 2:
                    signals.append(f"📈 تراکنش فعال ({t.txns_24h} تراکنش)")
                    score_parts.append(0.08)

            # ──── Signal 4: Price momentum — catch EARLY pumps ────
            if t.price_change_5m_pct > 5 and t.price_change_5m_pct < 40:
                signals.append(f"🔥 مومنتوم اولیه ({t.price_change_5m_pct:+.1f}%)")
                score_parts.append(min(0.15, t.price_change_5m_pct / 200))
            elif t.price_change_5m_pct > 2:
                signals.append(f"📈 مومنتوم مثبت ({t.price_change_5m_pct:+.1f}%)")
                score_parts.append(0.08)
            elif t.price_change_5m_pct < -30:
                risk_flags.append(f"⚠️ ریزش شدید ۵ دقیقه ({t.price_change_5m_pct:+.1f}%)")
                score_parts.append(-0.20)

            # ──── Signal 5: Liquidity ────
            if t.liquidity_usd >= self.cfg.pre_pump_min_liquidity_usd:
                if t.liquidity_usd >= 10000:
                    signals.append(f"💧 نقدینگی خوب (${t.liquidity_usd:,.0f})")
                    score_parts.append(0.10)
                else:
                    signals.append(f"💧 نقدینگی (${t.liquidity_usd:,.0f})")
                    score_parts.append(0.05)
            elif t.liquidity_usd > 0:
                risk_flags.append(f"⚠️ نقدینگی پایین (${t.liquidity_usd:,.0f})")

            # ──── Signal 6: On Pump.fun (mild alpha) ────
            if t.dex == "pumpfun":
                signals.append("🎯 Pump.fun native")
                score_parts.append(0.10)

            # ──── Signal 7: Solana chain ────
            if t.chain == "solana":
                score_parts.append(0.05)

            # ──── Signal 8: GMGN smart money check (async, best-effort) ────
            if t.address and t.chain == "solana":
                try:
                    security = await self.gmgn.get_token_security("solana", t.address)
                    if security:
                        # Check dev holdings
                        dev_pct = float(security.get("creator_percent", 0) or 0)
                        if dev_pct > self.cfg.pre_pump_max_dev_pct:
                            risk_flags.append(f"⚠️ توسعه‌دهنده {dev_pct:.1f}% نگه داشته")
                            score_parts.append(-0.15)
                        elif dev_pct > 0:
                            signals.append(f"👤 توسعه‌دهنده {dev_pct:.1f}%")
                            score_parts.append(0.05)

                        # Check for bundle
                        is_bundle = security.get("is_bundle", False)
                        if is_bundle:
                            risk_flags.append("🚨 Bundle detected (خرید همزمان مشکوک)")
                            score_parts.append(-0.30)

                        # Check top holder concentration
                        top_pct = float(security.get("top_10_holder_rate", 0) or 0)
                        if top_pct > 50:
                            risk_flags.append(f"⚠️ تمرکز بالا: ۱۰ کیف پول اول {top_pct:.1f}%")
                            score_parts.append(-0.10)
                except Exception:  # noqa: BLE001
                    pass  # GMGN check is best-effort

            if not signals or len(signals) < 4:
                continue

            raw = sum(score_parts)
            score = max(0.0, min(1.0, raw))
            if score < 0.55:
                continue

            hits.append(HunterHit(
                strategy="pre_pump",
                token_key=f"{t.chain}:{t.address}",
                token=t,
                score=score,
                signals=signals,
                risk_flags=risk_flags,
                detail={
                    "age_seconds": round(age),
                    "age_display": _fmt_age(age),
                    "liquidity_usd": t.liquidity_usd,
                    "volume_1h_usd": t.volume_1h_usd,
                    "txns_24h": t.txns_24h,
                    "price_usd": t.price_usd,
                },
            ))

        hits.sort(key=lambda h: h.score, reverse=True)
        return hits[:self.cfg.max_results_per_strategy]


# ==========================================================
# Strategy 2: Post-Migration Hunter — بعد از Migration
# ==========================================================

class PostMigrationHunter:
    """توکن‌هایی که از bonding curve پامپ‌فان خارج شده‌اند.

    وقتی توکن به آستانه مشخص رسید و به استخر نقدینگی Raydium منتقل شد،
    اگر حجم خرید همچنان بالا باشد، احتمال ادامه روند بیشتر است.

    مزایا:
    - ریسک راگ‌پول کمتر (نقدینگی قفل شده)
    - نقدینگی بیشتر
    - ورود نهنگ‌ها قابل مشاهده‌تر
    """

    def __init__(self, config: HunterConfig) -> None:
        self.cfg = config

    def evaluate(self, tokens: List[DEXToken]) -> List[HunterHit]:
        hits: List[HunterHit] = []
        now = time.time()

        for t in tokens:
            age = t.age_seconds if t.age_seconds > 0 else (now - t.created_at if t.created_at else 999999)
            signals: List[str] = []
            risk_flags: List[str] = []
            score_parts: List[float] = []

            # ──── Filter: age window ────
            if age < self.cfg.post_migration_min_age_seconds:
                continue
            if age > self.cfg.post_migration_max_age_seconds:
                continue

            # ──── Must have migrated (not on bonding curve) ────
            # Post-migration tokens have real liquidity on Raydium/Jupiter
            if t.liquidity_usd < self.cfg.post_migration_min_liquidity:
                continue

            # ──── Hard filter: no tokens with heavy price dump ────
            if t.price_change_1h_pct < -20:
                continue  # reject tokens dumping > 20% in 1 hour

            # Too late — already pumped massively
            if t.price_change_5m_pct > 60 or t.price_change_1h_pct > 150:
                continue  # buying the top

            # ──── Hard filter: minimum transactions ────
            if t.txns_24h < self.cfg.post_migration_min_txns:
                continue

            # ──── Signal 1: Liquidity depth ────
            if t.liquidity_usd >= 50000:
                signals.append(f"💧💧 نقدینگی عمیق (${t.liquidity_usd:,.0f})")
                score_parts.append(0.20)
            elif t.liquidity_usd >= 20000:
                signals.append(f"💧 نقدینگی خوب (${t.liquidity_usd:,.0f})")
                score_parts.append(0.15)
            else:
                signals.append(f"💧 نقدینگی (${t.liquidity_usd:,.0f})")
                score_parts.append(0.05)

            # ──── Signal 2: Volume sustained post-migration ────
            if t.volume_1h_usd >= 50000:
                signals.append(f"🔥🔥 حجم بالا (${t.volume_1h_usd:,.0f}/h)")
                score_parts.append(0.30)
            elif t.volume_1h_usd >= self.cfg.post_migration_min_volume_1h:
                signals.append(f"📊 حجم خوب (${t.volume_1h_usd:,.0f}/h)")
                score_parts.append(0.20)
            elif t.volume_1h_usd > 1000:
                signals.append(f"📈 حجم (${t.volume_1h_usd:,.0f}/h)")
                score_parts.append(0.05)
            else:
                continue

            # ──── Signal 3: Transaction count ────
            if t.txns_24h >= 200:
                signals.append(f"🔥 تراکنش خیلی بالا ({t.txns_24h})")
                score_parts.append(0.15)
            elif t.txns_24h >= self.cfg.post_migration_min_txns:
                signals.append(f"📈 تراکنش فعال ({t.txns_24h})")
                score_parts.append(0.08)

            # ──── Signal 4: Price action — detect EARLY growth, not after massive pump ────
            if t.price_change_1h_pct > 10 and t.price_change_1h_pct < 35:
                signals.append(f"🔥 رشد اولیه ({t.price_change_1h_pct:+.1f}%)")
                score_parts.append(0.15)
            elif t.price_change_1h_pct > 5:
                signals.append(f"📈 رشد مثبت ({t.price_change_1h_pct:+.1f}%)")
                score_parts.append(0.10)
                score_parts.append(0.08)
            elif t.price_change_1h_pct < -40:
                risk_flags.append(f"⚠️ ریزش سنگین ({t.price_change_1h_pct:+.1f}%)")
                score_parts.append(-0.20)
            elif t.price_change_1h_pct < -20:
                risk_flags.append(f"⚠️ ریزش ({t.price_change_1h_pct:+.1f}%)")
                score_parts.append(-0.10)

            # ──── Signal 5: Market cap sweet spot ────
            if 50000 < t.mcap < 1000000:
                signals.append(f"🎯 MC کوچک ($50K-$1M)")
                score_parts.append(0.10)
            elif 1000000 <= t.mcap < 10000000:
                signals.append(f"📊 MC متوسط ($1M-$10M)")
                score_parts.append(0.08)
            elif t.mcap >= 10000000:
                signals.append(f"💰 MC بالا (${t.mcap/1e6:.1f}M)")

            # ──── Signal 6: Volume/MCap ratio (high = lots of trading) ────
            if t.mcap > 0 and t.volume_1h_usd > 0:
                vol_mc_ratio = t.volume_1h_usd / t.mcap
                if vol_mc_ratio > 0.5:
                    signals.append(f"🔥 نسبت حجم/MC بالا ({vol_mc_ratio:.1f}x/h)")
                    score_parts.append(0.15)
                elif vol_mc_ratio > 0.2:
                    signals.append(f"📈 نسبت حجم/MC خوب ({vol_mc_ratio:.1f}x/h)")
                    score_parts.append(0.08)

            if not signals or len(signals) < 2:
                continue

            raw = sum(score_parts)
            score = max(0.0, min(1.0, raw))
            if score < 0.55:
                continue

            hits.append(HunterHit(
                strategy="post_migration",
                token_key=f"{t.chain}:{t.address}",
                token=t,
                score=score,
                signals=signals,
                risk_flags=risk_flags,
                detail={
                    "age_seconds": round(age),
                    "age_display": _fmt_age(age),
                    "liquidity_usd": t.liquidity_usd,
                    "volume_1h_usd": t.volume_1h_usd,
                    "mcap": t.mcap,
                    "txns_24h": t.txns_24h,
                    "price_change_1h_pct": t.price_change_1h_pct,
                },
            ))

        hits.sort(key=lambda h: h.score, reverse=True)
        return hits[:self.cfg.max_results_per_strategy]


# ==========================================================
# Strategy 3: Smart Money Tracker — ردیابی پول هوشمند
# ==========================================================

class SmartMoneyTracker:
    """دنبال‌کردن کیف پول‌های هوشمند از طریق GMGN.ai.

    به جای دنبال کردن اینفلوئنسرها:
    - کیف پول تریدرهای موفق را پیدا کن
    - ورودهای آن‌ها را مانیتور کن
    - اگر چند Smart Wallet روی یک توکن وارد شدند، آن را بررسی کن

    این روش از سیگنال‌های تلگرام قابل اعتمادتر است.
    """

    def __init__(self, config: HunterConfig, gmgn: GMGNClient) -> None:
        self.cfg = config
        self.gmgn = gmgn
        self._smart_money_tokens: List[dict] = []  # cached from GMGN

    async def refresh_smart_money_data(self) -> None:
        """Fetch latest smart money activity from GMGN."""
        try:
            self._smart_money_tokens = await self.gmgn.get_smart_money_tokens("solana", limit=50)
            logger.debug(f"GMGN smart money: {len(self._smart_money_tokens)} tokens")
        except Exception as exc:  # noqa: BLE001
            logger.debug(f"GMGN smart money refresh failed: {exc}")

    async def evaluate(self, tokens: List[DEXToken]) -> List[HunterHit]:
        hits: List[HunterHit] = []

        # Build lookup of GMGN smart money tokens
        smart_tokens_map: Dict[str, dict] = {}
        for st in self._smart_money_tokens:
            addr = st.get("address", st.get("token_address", ""))
            if addr:
                smart_tokens_map[addr] = st

        for t in tokens:
            signals: List[str] = []
            risk_flags: List[str] = []
            score_parts: List[float] = []

            # ──── Hard gate: GMGN smart money data required ────
            gmgn_data = smart_tokens_map.get(t.address)
            if not gmgn_data:
                continue  # must have GMGN smart money confirmation
            if gmgn_data:
                signals.append("🐋 Smart Money از GMGN")
                score_parts.append(0.30)

                # Number of smart wallets buying
                smart_count = int(gmgn_data.get("smart_money_count", 0) or
                                  gmgn_data.get("holder_count", 0) or 0)
                if smart_count >= 5:
                    signals.append(f"🔥 {smart_count} کیف پول هوشمند")
                    score_parts.append(0.20)
                elif smart_count >= 2:
                    signals.append(f"📊 {smart_count} کیف پول هوشمند")
                    score_parts.append(0.10)

            # ──── High volume density = smart money likely ────
            if t.age_seconds > 0 and t.volume_24h_usd > 0:
                vol_per_second = t.volume_24h_usd / max(t.age_seconds, 1)
                if vol_per_second > 20:  # > $20/sec
                    signals.append(f"💰 حجم بالا نسبت به سن (${t.volume_24h_usd:,.0f}/h)")
                    score_parts.append(min(0.25, vol_per_second / 100))

            # ──── Strong price + volume = smart buy ────
            if t.price_change_1h_pct > 20 and t.volume_1h_usd > 10000:
                signals.append(f"🚀 خرید هوشمند ({t.price_change_1h_pct:+.1f}%, ${t.volume_1h_usd:,.0f}/h)")
                score_parts.append(0.20)
            elif t.price_change_1h_pct > 10 and t.volume_1h_usd > 5000:
                signals.append(f"📈 علاقه هوشمند ({t.price_change_1h_pct:+.1f}%)")
                score_parts.append(0.10)

            # ──── Fresh + high activity = early smart entry ────
            if t.age_seconds < 3600 and t.txns_24h > 100:
                signals.append(f"⚡ ورود اولیه ({t.txns_24h} تراکنش در {int(t.age_seconds/60)} دقیقه)")
                score_parts.append(0.20)

            # ──── Has base liquidity ────
            if t.liquidity_usd > 10000:
                signals.append(f"💧 نقدینگی کافی (${t.liquidity_usd:,.0f})")
                score_parts.append(0.05)

            # ──── Known wallet overlap ────
            known_count = 0
            for wallet in self.cfg.smart_money_known_wallets:
                # In real implementation, check GMGN wallet activity
                if wallet and t.address:
                    known_count += 1
            if known_count > 0:
                signals.append(f"🎯 {known_count} کیف پول شناخته‌شده")
                score_parts.append(min(0.25, 0.15 * known_count))

            if not signals or len(signals) < 4:
                continue

            raw = sum(score_parts)
            score = max(0.0, min(1.0, raw))
            if score < 0.40:
                continue

            hits.append(HunterHit(
                strategy="smart_money",
                token_key=f"{t.chain}:{t.address}",
                token=t,
                score=score,
                signals=signals,
                risk_flags=risk_flags,
                detail={
                    "age_seconds": round(t.age_seconds),
                    "age_display": _fmt_age(t.age_seconds),
                    "volume_1h_usd": t.volume_1h_usd,
                    "txns_24h": t.txns_24h,
                    "gmgn_smart_count": len(smart_tokens_map),
                },
            ))

        hits.sort(key=lambda h: h.score, reverse=True)
        return hits[:self.cfg.max_results_per_strategy]


# ==========================================================
# Strategy 4: Narrative Hunter — شکار روایت‌ها
# ==========================================================

class NarrativeHunter:
    """میم‌کوین‌هایی که با ترندهای روز همسو هستند — با فیلترهای پیشرفته.

    بیشترین پامپ‌ها معمولاً روی روایت‌های داغ بازار اتفاق می‌افتند:
    AI، ربات‌ها، ایلان ماسک، Grok، ترامپ، PEPE، سگ‌ها، گربه‌ها، رویدادهای خبری

    فیلترهای اضافی برای افزایش نرخ موفقیت:
    ۱. بررسی امنیتی GMGN (Bundle، توسعه‌دهنده، تمرکز بالا)
    ۲. تشخیص شتاب حجم (رشد حجم ۵ دقیقه نسبت به میانگین)
    ۳. حداقل نقدینگی برای جلوگیری از راگ‌پول
    ۴. MCsweet spot — توکن‌های کوچک پتانسیل رشد بیشتری دارند
    ۵. همپوشانی با استراتژی‌های دیگر (Smart Money + Narrative = قوی‌تر)
    ۶. حجم نسبت به MC — نسبت بالا = توجه زیاد به توکن
    """

    NARRATIVE_MAP: Dict[str, List[str]] = {
        "AI":       ["AI", "ARTIFICIAL", "GPT", "OPENAI", "CHATGPT", "NEURAL", "DEEP", "AGENT"],
        "GROK":     ["GROK", "XAI", "GROKAI"],
        "TRUMP":    ["TRUMP", "MAGA", "PRESIDENT", "ELECTION"],
        "ELON":     ["ELON", "MUSK", "TESLA", "SPACEX"],
        "PEPE":     ["PEPE", "FROG"],
        "DOG":      ["DOGE", "DOG", "SHIB", "FLOKI", "BONK", "WIF", "NEIRO"],
        "CAT":      ["CAT", "KITTY", "MEOW"],
        "GIGA":     ["GIGA", "GIGACHAD", "CHAD"],
        "SPX":      ["SPX", "STONKS"],
    }

    # حداقل نقدینگی برای جلوگیری از راگ‌پول
    MIN_LIQUIDITY_USD: float = 2000.0
    # حداقل MC برای فیلتر توکن‌های خیلی ریسکی
    MIN_MCAP: float = 5000.0

    def __init__(self, config: HunterConfig, gmgn: 'GMGNClient' | None = None) -> None:
        self.cfg = config
        self.gmgn = gmgn
        self._keyword_set: Set[str] = set()
        for kw in config.narrative_keywords:
            self._keyword_set.add(kw.upper())
            for key, expanded in self.NARRATIVE_MAP.items():
                if kw.upper() == key.upper():
                    self._keyword_set.update(e.upper() for e in expanded)

        self._trending = {"AI", "GROK", "TRUMP", "ELON", "MUSK", "ROBOT", "GIGA", "SPX"}

    async def evaluate(self, tokens: List[DEXToken]) -> List[HunterHit]:
        hits: List[HunterHit] = []

        for t in tokens:
            signals: List[str] = []
            risk_flags: List[str] = []
            score_parts: List[float] = []

            # Check symbol and name against keywords
            sym_upper = t.symbol.upper()
            name_upper = t.name.upper()
            matched: List[str] = []

            for kw in self._keyword_set:
                if kw in sym_upper or kw in name_upper:
                    matched.append(kw)

            if not matched:
                continue

            # ──── فیلتر اولیه: نقدینگی حداقلی ────
            if 0 < t.liquidity_usd < self.MIN_LIQUIDITY_USD:
                risk_flags.append(f"🚨 نقدینگی خیلی پایین (${t.liquidity_usd:,.0f}) — احتمال راگ‌پول")
                score_parts.append(-0.30)
            elif 0 < t.mcap < self.MIN_MCAP:
                risk_flags.append(f"🚨 MC خیلی پایین (${t.mcap:,.0f}) — ریسک بالا")
                score_parts.append(-0.20)

            # ──── فیلتر اولیه: ریزش شدید ────
            if t.price_change_5m_pct < -50:
                risk_flags.append(f"🚨 ریزش سنگین ({t.price_change_5m_pct:+.1f}%) — احتمال راگ")
                score_parts.append(-0.25)
                continue
            if t.price_change_5m_pct > 60 or t.price_change_1h_pct > 150:
                continue  # pump already done

            # ──── Signal 1: Keyword match ────
            signals.append(f"📖 روایت: {', '.join(matched[:3])}")
            score_parts.append(min(0.30, 0.10 * len(matched)))

            # ──── Signal 2: Trending narrative bonus ────
            trending_hits = [kw for kw in matched if kw in self._trending]
            if trending_hits:
                signals.append(f"🔥 ترند: {', '.join(trending_hits)}")
                score_parts.append(0.18)

            # ──── Signal 3: Momentum confirms narrative — detect EARLY, not late ────
            if t.price_change_5m_pct > 10 and t.price_change_5m_pct < 40:
                signals.append(f"🔥 مومنتوم اولیه ({t.price_change_5m_pct:+.1f}%)")
                score_parts.append(0.15)
            elif t.price_change_5m_pct > 2:
                signals.append(f"📈 مومنتوم مثبت ({t.price_change_5m_pct:+.1f}%)")
                score_parts.append(0.08)
            elif t.price_change_5m_pct < -20:
                risk_flags.append(f"⚠️ ریزش ({t.price_change_5m_pct:+.1f}%)")
                score_parts.append(-0.10)

            # ──── Signal 4: Volume confirms interest ────
            if t.volume_1h_usd > 100000:
                signals.append(f"🔥🔥 حجم خیلی بالا (${t.volume_1h_usd:,.0f}/h)")
                score_parts.append(0.18)
            elif t.volume_1h_usd > 50000:
                signals.append(f"🔥 حجم بالا (${t.volume_1h_usd:,.0f}/h)")
                score_parts.append(0.14)
            elif t.volume_1h_usd > 10000:
                signals.append(f"📊 حجم (${t.volume_1h_usd:,.0f}/h)")
                score_parts.append(0.08)
            elif t.volume_1h_usd > 0:
                score_parts.append(0.03)

            # ──── Signal 5: Fresh = early narrative entry ────
            if t.age_seconds < 600:
                signals.append("⚡ خیلی تازه (< ۱۰ دقیقه)")
                score_parts.append(0.18)
            elif t.age_seconds < 1800:
                signals.append("⚡ ورود اولیه (< ۳۰ دقیقه)")
                score_parts.append(0.14)
            elif t.age_seconds < 3600:
                signals.append("🆕 ورود زود (< ۱ ساعت)")
                score_parts.append(0.08)

            # ──── Signal 6: Liquidity depth ────
            if t.liquidity_usd >= 50000:
                signals.append(f"💧💧 نقدینگی عمیق (${t.liquidity_usd:,.0f})")
                score_parts.append(0.10)
            elif t.liquidity_usd >= 20000:
                signals.append(f"💧 نقدینگی خوب (${t.liquidity_usd:,.0f})")
                score_parts.append(0.07)
            elif t.liquidity_usd >= 5000:
                signals.append(f"💧 نقدینگی (${t.liquidity_usd:,.0f})")
                score_parts.append(0.04)

            # ──── Signal 7: Market cap sweet spot ────
            if t.mcap > 0:
                if 10000 < t.mcap < 200000:
                    signals.append(f"🎯 MC خیلی کوچک (${t.mcap/1e3:.0f}K) — پتانسیل بالا")
                    score_parts.append(0.12)
                elif 200000 <= t.mcap < 1000000:
                    signals.append(f"🎯 MC کوچک ($200K-$1M)")
                    score_parts.append(0.10)
                elif 1000000 <= t.mcap < 5000000:
                    signals.append(f"📊 MC متوسط ($1M-$5M)")
                    score_parts.append(0.06)
                elif t.mcap >= 10000000:
                    signals.append(f"💰 MC بالا (${t.mcap/1e6:.1f}M)")

            # ──── Signal 8: Volume/MCap ratio (high = lots of trading interest) ────
            if t.mcap > 0 and t.volume_1h_usd > 0:
                vol_mc_ratio = t.volume_1h_usd / t.mcap
                if vol_mc_ratio > 1.0:
                    signals.append(f"🔥🔥 نسبت حجم/MC خیلی بالا ({vol_mc_ratio:.1f}x/h)")
                    score_parts.append(0.12)
                elif vol_mc_ratio > 0.5:
                    signals.append(f"🔥 نسبت حجم/MC بالا ({vol_mc_ratio:.1f}x/h)")
                    score_parts.append(0.08)
                elif vol_mc_ratio > 0.2:
                    signals.append(f"📈 نسبت حجم/MC ({vol_mc_ratio:.1f}x/h)")
                    score_parts.append(0.04)

            # ──── Signal 9: Volume per transaction (smart money indicator) ────
            if t.txns_24h > 0 and t.volume_24h_usd > 0:
                vol_per_txn = t.volume_24h_usd / t.txns_24h
                if vol_per_txn > 500:
                    signals.append(f"🐋 حجم بالا/تراکنش (${vol_per_txn:,.0f}) — احتمال نهنگ")
                    score_parts.append(0.08)
                elif vol_per_txn > 200:
                    signals.append(f"📊 حجم خوب/تراکنش (${vol_per_txn:,.0f})")
                    score_parts.append(0.04)

            # ──── Signal 10: Transaction density ────
            if t.age_seconds > 0 and t.txns_24h > 0:
                txns_per_min = t.txns_24h / max(t.age_seconds / 60, 1)
                if txns_per_min > 10:
                    signals.append(f"🔥 تراکنش خیلی بالا ({txns_per_min:.0f}/دقیقه)")
                    score_parts.append(0.10)
                elif txns_per_min > 5:
                    signals.append(f"📈 تراکنش فعال ({txns_per_min:.0f}/دقیقه)")
                    score_parts.append(0.06)

            # ──── Signal 11: Price action 1h confirmation ────
            if t.price_change_1h_pct > 100:
                signals.append(f"🚀🚀 پامپ بزرگ ۱ ساعته ({t.price_change_1h_pct:+.1f}%)")
                score_parts.append(0.15)
            elif t.price_change_1h_pct > 30:
                signals.append(f"🚀 رشد خوب ۱ ساعته ({t.price_change_1h_pct:+.1f}%)")
                score_parts.append(0.10)
            elif t.price_change_1h_pct < -40:
                risk_flags.append(f"⚠️ ریزش سنگین ۱ ساعته ({t.price_change_1h_pct:+.1f}%)")
                score_parts.append(-0.15)

            # ──── Signal 12: GMGN security check (async, best-effort) ────
            if self.gmgn and t.address and t.chain == "solana":
                try:
                    security = await self.gmgn.get_token_security("solana", t.address)
                    if security:
                        # Check dev holdings
                        dev_pct = float(security.get("creator_percent", 0) or 0)
                        if dev_pct > 30:
                            risk_flags.append(f"⚠️ توسعه‌دهنده {dev_pct:.1f}% نگه داشته")
                            score_parts.append(-0.15)
                        elif dev_pct > 0 and dev_pct < 10:
                            signals.append(f"👤 توسعه‌دهنده {dev_pct:.1f}% — پراکنده")
                            score_parts.append(0.05)

                        # Check for bundle
                        is_bundle = security.get("is_bundle", False)
                        if is_bundle:
                            risk_flags.append("🚨 Bundle detected (خرید همزمان مشکوک)")
                            score_parts.append(-0.30)

                        # Check top holder concentration
                        top_pct = float(security.get("top_10_holder_rate", 0) or 0)
                        if top_pct > 50:
                            risk_flags.append(f"⚠️ تمرکز بالا: ۱۰ کیف پول اول {top_pct:.1f}%")
                            score_parts.append(-0.12)
                        elif top_pct < 30 and top_pct > 0:
                            signals.append(f"✅ پراکندگی خوب ({top_pct:.1f}%)")
                            score_parts.append(0.05)
                except Exception:  # noqa: BLE001
                    pass  # GMGN check is best-effort

            # ──── چک نهایی: حداقل سیگنال کافی نباشد رد شود ────
            if len(risk_flags) >= 3:
                continue  # توکن با ۳ ریسک یا بیشتر رد می‌شود

            if not signals or len(signals) < 2:
                continue

            raw = sum(score_parts)
            score = max(0.0, min(1.0, raw))

            if score < self.cfg.narrative_min_score:
                continue

            hits.append(HunterHit(
                strategy="narrative",
                token_key=f"{t.chain}:{t.address}",
                token=t,
                score=score,
                signals=signals,
                risk_flags=risk_flags,
                detail={
                    "matched_keywords": matched,
                    "trending_matches": trending_hits,
                    "age_display": _fmt_age(t.age_seconds),
                    "price_change_5m_pct": t.price_change_5m_pct,
                    "price_change_1h_pct": t.price_change_1h_pct,
                    "volume_1h_usd": t.volume_1h_usd,
                    "liquidity_usd": t.liquidity_usd,
                    "mcap": t.mcap,
                    "vol_mc_ratio": round(t.volume_1h_usd / t.mcap, 2) if t.mcap > 0 else 0,
                },
            ))

        hits.sort(key=lambda h: h.score, reverse=True)
        return hits[:self.cfg.max_results_per_strategy]


# ==========================================================
# Strategy 5: Contract Safety Analyzer — تحلیل امنیتی قرارداد
# ==========================================================

class ContractSafetyAnalyzer:
    """تحلیل عمیق امنیتی قرارداد هوشمند توکن.

    بررسی‌ها:
    ۱. Proxy contract — آیا قرارداد پروکسی است؟ (قابل تغییر)
    ۲. Mint authority — آیا توسعه‌دهنده می‌تواند توکن جدید بسازد؟
    ۳. Freeze authority — آیا معاملات قفل می‌شود؟
    ۴. Blacklist — آیا کیف پول‌ها بلاک می‌شوند؟
    ۵. Owner controls — آیا مالک کنترل کامل دارد؟
    ۶. Contract verified — آیا کد قرارداد عمومی است؟
    """

    def __init__(self, config: HunterConfig, gmgn: GMGNClient) -> None:
        self.cfg = config
        self.gmgn = gmgn

    async def evaluate(self, tokens: List[DEXToken]) -> List[HunterHit]:
        hits: List[HunterHit] = []
        for t in tokens:
            if not t.address or t.chain not in ("solana", "bsc", "ethereum"):
                continue

            signals: List[str] = []
            risk_flags: List[str] = []
            score_parts: List[float] = []

            # Fetch security data from GMGN
            security = {}
            try:
                security = await self.gmgn.get_token_security(t.chain, t.address)
            except Exception:  # noqa: BLE001
                continue  # need security data for this strategy

            if not security:
                continue

            # ──── Check 1: Proxy contract ────
            is_proxy = security.get("is_proxy", False)
            if is_proxy:
                risk_flags.append("🚨 قرارداد پروکسی — قابل تغییر")
                score_parts.append(-0.25)
            else:
                signals.append("✅ قرارداد اصلی (غیر پروکسی)")
                score_parts.append(0.10)

            # ──── Check 2: Mint authority ────
            has_mint = security.get("has_mint_authority", False)
            if has_mint:
                risk_flags.append("🚨 مجازیت Mint — توسعه‌دهنده می‌تواند توکن چاپ کند")
                score_parts.append(-0.35)
            else:
                signals.append("✅ بدون Mint authority")
                score_parts.append(0.15)

            # ──── Check 3: Freeze authority ────
            has_freeze = security.get("has_freeze_authority", False)
            if has_freeze:
                risk_flags.append("⚠️ مجازیت Freeze — معاملات قفل می‌شود")
                score_parts.append(-0.20)
            else:
                signals.append("✅ بدون Freeze authority")
                score_parts.append(0.10)

            # ──── Check 4: Dev holdings ────
            dev_pct = float(security.get("creator_percent", 0) or 0)
            if dev_pct > 30:
                risk_flags.append(f"🚨 توسعه‌دهنده {dev_pct:.1f}% نگه داشته")
                score_parts.append(-0.20)
            elif dev_pct > 15:
                risk_flags.append(f"⚠️ توسعه‌دهنده {dev_pct:.1f}%")
                score_parts.append(-0.10)
            elif dev_pct > 0 and dev_pct < 5:
                signals.append(f"👤 توسعه‌دهنده {dev_pct:.1f}% — پراکنده")
                score_parts.append(0.08)

            # ──── Check 5: Top holder concentration ────
            top_pct = float(security.get("top_10_holder_rate", 0) or 0)
            if top_pct > 60:
                risk_flags.append(f"🚨 تمرکز بالا: ۱۰ کیف پول اول {top_pct:.1f}%")
                score_parts.append(-0.20)
            elif top_pct > 40:
                risk_flags.append(f"⚠️ تمرکز متوسط: {top_pct:.1f}%")
                score_parts.append(-0.08)
            elif 0 < top_pct < 25:
                signals.append(f"✅ پراکندگی خوب: top10={top_pct:.1f}%")
                score_parts.append(0.10)

            # ──── Check 6: Bundle detection ────
            is_bundle = security.get("is_bundle", False)
            if is_bundle:
                risk_flags.append("🚨 Bundle detected (خرید همزمان مشکوک)")
                score_parts.append(-0.30)

            # ──── Check 7: LP locked ────
            lp_locked = security.get("is_lp_locked", False)
            if lp_locked:
                signals.append("✅ نقدینگی قفل شده")
                score_parts.append(0.15)
            elif t.liquidity_usd > 0:
                risk_flags.append("⚠️ نقدینگی قفل نشده")
                score_parts.append(-0.05)

            # ──── Final scoring ────
            if not signals and not risk_flags:
                continue

            has_critical_risk = any("🚨" in rf for rf in risk_flags)
            if has_critical_risk:
                continue  # reject tokens with critical safety issues

            raw = sum(score_parts)
            score = max(0.0, min(1.0, raw))
            if score < 0.3:
                continue

            hits.append(HunterHit(
                strategy="contract_safety",
                token_key=f"{t.chain}:{t.address}",
                token=t,
                score=score,
                signals=signals,
                risk_flags=risk_flags,
                detail={
                    "security_data": {k: v for k, v in security.items()
                                       if k in ("is_proxy", "has_mint_authority",
                                                 "has_freeze_authority", "creator_percent",
                                                 "top_10_holder_rate", "is_bundle",
                                                 "is_lp_locked")},
                    "dev_pct": dev_pct,
                    "top_holder_pct": top_pct,
                },
            ))

        hits.sort(key=lambda h: h.score, reverse=True)
        return hits[:self.cfg.max_results_per_strategy]



# ==========================================================
# Strategy 5: Rugpull Detector — تشخیص راگ‌پول
# ==========================================================

class RugpullDetector:
    """تشخیص احتمال راگ‌پول بر اساس رفتار قیمت و حجم.

    سیگنال‌ها (همه از DexScreener — نیازی به GMGN نیست):
    ۱. افت شدید قیمت در ۵ دقیقه ( Dump )
    ۲. حجم بسیار بالا نسبت به نقدینگی (دستکاری)
    ۳. نقدینگی خیلی پایین (ریسک بالا)
    ۴. توکن خیلی جدید + حجم بالا = احتمال Honeypot
    ۵. قیمت صعودی یکطرفه بدون اصلاح = P&D pattern
    """

    def __init__(self, config: HunterConfig) -> None:
        self.cfg = config

    def evaluate(self, tokens: List[DEXToken]) -> List[HunterHit]:
        hits: List[HunterHit] = []

        for t in tokens:
            if not t.address:
                continue

            signals: List[str] = []
            risk_flags: List[str] = []
            score_parts: List[float] = []

            # ──── Check 1: Sharp price dump = rug indicator ────
            if t.price_change_5m_pct < -50:
                risk_flags.append(f"🚨 Dump سنگین ({t.price_change_5m_pct:+.1f}%) — احتمال راگ")
                score_parts.append(-0.30)
                continue  # skip dumped tokens
            elif t.price_change_5m_pct < -25:
                risk_flags.append(f"⚠️ ریزش ({t.price_change_5m_pct:+.1f}%)")
                score_parts.append(-0.15)

            # ──── Check 2: Volume/Liquidity ratio too high = manipulation ────
            if t.liquidity_usd > 0 and t.volume_1h_usd > 0:
                vol_liq = t.volume_1h_usd / t.liquidity_usd
                if vol_liq > 20:
                    risk_flags.append(f"🚨 حجم {vol_liq:.0f}x نقدینگی — دستکاری")
                    score_parts.append(-0.25)
                elif vol_liq > 5:
                    risk_flags.append(f"⚠️ حجم {vol_liq:.1f}x نقدینگی — مشکوک")
                    score_parts.append(-0.10)
                elif vol_liq > 1.5:
                    signals.append(f"🔥 حجم/نقدینگی بالا ({vol_liq:.1f}x)")
                    score_parts.append(0.15)

            # ──── Check 3: Extremely low liquidity = rug risk ────
            if 0 < t.liquidity_usd < 1000:
                risk_flags.append(f"🚨 نقدینگی خیلی پایین (${t.liquidity_usd:,.0f})")
                score_parts.append(-0.20)
            elif 0 < t.liquidity_usd < 5000:
                risk_flags.append(f"⚠️ نقدینگی پایین (${t.liquidity_usd:,.0f})")
                score_parts.append(-0.08)

            # ──── Check 4: Healthy token = SAFE ────
            is_safe = False
            if t.liquidity_usd >= 20000:
                signals.append(f"✅ نقدینگی کافی (${t.liquidity_usd:,.0f})")
                score_parts.append(0.15)
                is_safe = True

            if t.age_seconds > 3600 and t.txns_24h > 30:
                signals.append(f"✅ عمر {int(t.age_seconds/3600)}س + {t.txns_24h} تراکنش")
                score_parts.append(0.10)
                is_safe = True

            if 0 < t.price_change_5m_pct < 30 and t.volume_1h_usd > 5000:
                signals.append(f"📈 رشد سالم ({t.price_change_5m_pct:+.1f}%)")
                score_parts.append(0.10)
                is_safe = True

            # MC/Liq ratio (healthy range)
            if t.mcap > 0 and t.liquidity_usd > 0:
                mc_liq = t.mcap / t.liquidity_usd
                if mc_liq < 10:
                    signals.append(f"✅ MC/نقدینگی عالی ({mc_liq:.1f}x)")
                    score_parts.append(0.12)
                    is_safe = True
                elif mc_liq < 30:
                    signals.append(f"📊 MC/نقدینگی خوب ({mc_liq:.1f}x)")
                    score_parts.append(0.06)

            # ──── Gate: must have at least 2 positive signals ────
            if len(signals) < 2:
                continue

            has_critical = any("🚨" in rf for rf in risk_flags)
            if has_critical:
                continue

            raw = sum(score_parts)
            score = max(0.0, min(1.0, raw))
            if score < 0.55:
                continue

            hits.append(HunterHit(
                strategy="contract_safety",
                token_key=f"{t.chain}:{t.address}",
                token=t,
                score=score,
                signals=signals,
                risk_flags=risk_flags,
                detail={
                    "liquidity_usd": t.liquidity_usd,
                    "mc_liq_ratio": round(t.mcap / t.liquidity_usd, 2) if t.liquidity_usd > 0 and t.mcap > 0 else 0,
                },
            ))

        hits.sort(key=lambda h: h.score, reverse=True)
        return hits[:self.cfg.max_results_per_strategy]


# ==========================================================
# Strategy 6: Whale Activity Tracker — ردیابی فعالیت نهنگ‌ها
# ==========================================================

class WhaleActivityTracker:
    """شناسایی نهنگ‌های در حال انباشت — فقط با DexScreener data.

    سیگنال‌ها:
    ۱. حجم بالا/تراکنش (تراکنش‌های بزرگ)
    ۲. حجم بالا نسبت به نقدینگی
    ۳. تراکنش خیلی بالا در دقیقه
    ۴. توکن جدید + فعالیت شدید = ورود زودهنگام نهنگ‌ها
    ۵. رشد قیمت + حجم بالا = تأیید خرید نهنگ
    """

    def __init__(self, config: HunterConfig) -> None:
        self.cfg = config

    def evaluate(self, tokens: List[DEXToken]) -> List[HunterHit]:
        hits: List[HunterHit] = []

        for t in tokens:
            if not t.address:
                continue
            if t.liquidity_usd < 10000 or t.volume_1h_usd < 5000:
                continue

            signals: List[str] = []
            risk_flags: List[str] = []
            score_parts: List[float] = []

            # ──── Signal 1: High volume per transaction (whale-sized) ────
            if t.txns_24h > 0 and t.volume_24h_usd > 0:
                vol_per_txn = t.volume_24h_usd / t.txns_24h
                if vol_per_txn > 5000:
                    signals.append(f"🐋🐋 حجم خیلی بالا/تراکنش (${vol_per_txn:,.0f})")
                    score_parts.append(min(0.30, vol_per_txn / 15000))
                elif vol_per_txn > 1000:
                    signals.append(f"🐋 حجم بالا/تراکنش (${vol_per_txn:,.0f}) — نهنگ")
                    score_parts.append(min(0.25, vol_per_txn / 8000))
                elif vol_per_txn > 300:
                    signals.append(f"📊 حجم خوب/تراکنش (${vol_per_txn:,.0f})")
                    score_parts.append(0.10)

            # ──── Signal 2: Volume density (high volume vs liquidity) ────
            if t.liquidity_usd > 0 and t.volume_1h_usd > 0:
                vol_liq_ratio = t.volume_1h_usd / t.liquidity_usd
                if vol_liq_ratio > 3.0:
                    signals.append(f"🔥🔥 حجم/نقدینگی خیلی بالا ({vol_liq_ratio:.1f}x)")
                    score_parts.append(min(0.25, vol_liq_ratio / 10))
                elif vol_liq_ratio > 1.0:
                    signals.append(f"🔥 حجم/نقدینگی بالا ({vol_liq_ratio:.1f}x)")
                    score_parts.append(0.12)

            # ──── Signal 3: Fresh + high activity = early whale entry ────
            if t.age_seconds > 0 and t.age_seconds < 7200:
                activity_score = t.volume_1h_usd / max(t.age_seconds / 60, 1)
                if activity_score > 200:
                    signals.append(f"⚡ ورود زودهنگام نهنگ‌ها (${activity_score:,.0f}/دقیقه)")
                    score_parts.append(min(0.25, activity_score / 800))
                elif activity_score > 50:
                    signals.append(f"📈 فعالیت اولیه بالا (${activity_score:,.0f}/دقیقه)")
                    score_parts.append(0.12)

            # ──── Signal 4: Transaction clustering ────
            if t.age_seconds > 0 and t.txns_24h > 0:
                txns_per_min = t.txns_24h / max(t.age_seconds / 60, 1)
                if txns_per_min > 30 and t.volume_1h_usd > 20000:
                    signals.append(f"🔥🔥 تراکنش خیلی بالا ({txns_per_min:.0f}/د) + حجم بالا")
                    score_parts.append(0.20)
                elif txns_per_min > 10 and t.volume_1h_usd > 5000:
                    signals.append(f"🔥 تراکنش فعال ({txns_per_min:.0f}/د)")
                    score_parts.append(0.12)

            # ──── Signal 5: Price + Volume = confirmed whale buy ────
            if t.price_change_1h_pct > 20 and t.volume_1h_usd > 15000:
                signals.append(f"🚀 خرید نهنگ تأیید شده (+{t.price_change_1h_pct:.1f}%, ${t.volume_1h_usd:,.0f}/h)")
                score_parts.append(0.20)
            elif t.price_change_1h_pct > 10 and t.volume_1h_usd > 8000:
                signals.append(f"📈 علاقه خرید (+{t.price_change_1h_pct:.1f}%)")
                score_parts.append(0.12)

            # ──── Signal 6: Volume 24h vs average (accumulation) ────
            if t.volume_24h_usd > 0 and t.volume_1h_usd > 0:
                hourly_avg = t.volume_24h_usd / 24
                if hourly_avg > 0:
                    vol_spike = t.volume_1h_usd / hourly_avg
                    if vol_spike > 3.0:
                        signals.append(f"🔥 حجم ساعتی {vol_spike:.1f}x میانگین — انباشت")
                        score_parts.append(0.15)

            # ──── Gate: must have whale signals ────
            if not signals or len(signals) < 2:
                continue

            raw = sum(score_parts)
            score = max(0.0, min(1.0, raw))
            if score < 0.55:
                continue

            hits.append(HunterHit(
                strategy="whale_activity",
                token_key=f"{t.chain}:{t.address}",
                token=t,
                score=score,
                signals=signals,
                risk_flags=risk_flags,
                detail={
                    "volume_24h": t.volume_24h_usd,
                    "txns_24h": t.txns_24h,
                    "vol_per_txn": t.volume_24h_usd / t.txns_24h if t.txns_24h > 0 else 0,
                },
            ))

        hits.sort(key=lambda h: h.score, reverse=True)
        return hits[:self.cfg.max_results_per_strategy]


# ==========================================================
# Strategy 7: Liquidity Health Analyzer — تحلیل سلامت نقدینگی
# ==========================================================

class LiquidityHealthAnalyzer:
    """تحلیل سلامت نقدینگی — فقط با DexScreener data.

    فاکتورها:
    ۱. نسبت حجم به نقدینگی (بالا = توجه زیاد)
    ۲. عمق نقدینگی (حداقل $X)
    ۳. نسبت MC به نقدینگی (MC/Liq ratio)
    ۴. نسبت حجم به تراکنش (عمق بازار)
    ۵. ترکیب نقدینگی + حجم + رشد قیمت
    """

    def __init__(self, config: HunterConfig) -> None:
        self.cfg = config

    def evaluate(self, tokens: List[DEXToken]) -> List[HunterHit]:
        hits: List[HunterHit] = []

        for t in tokens:
            if t.liquidity_usd <= 0:
                continue

            signals: List[str] = []
            risk_flags: List[str] = []
            score_parts: List[float] = []

            # ──── Signal 1: Volume/Liquidity ratio ────
            if t.volume_1h_usd > 0:
                vol_liq = t.volume_1h_usd / t.liquidity_usd
                if vol_liq > 5.0:
                    signals.append(f"🔥🔥 حجم/نقدینگی خیلی بالا ({vol_liq:.1f}x/h)")
                    score_parts.append(min(0.30, vol_liq / 15))
                elif vol_liq > 2.0:
                    signals.append(f"🔥 حجم/نقدینگی بالا ({vol_liq:.1f}x/h)")
                    score_parts.append(0.20)
                elif vol_liq > 0.5:
                    signals.append(f"📊 حجم/نقدینگی خوب ({vol_liq:.1f}x/h)")
                    score_parts.append(0.12)
                elif vol_liq > 0.1:
                    signals.append(f"📈 حجم/نقدینگی ({vol_liq:.1f}x/h)")
                    score_parts.append(0.05)

            # ──── Signal 2: Absolute liquidity depth ────
            if t.liquidity_usd >= 100000:
                signals.append(f"💧💧💧 نقدینگی عمیق (${t.liquidity_usd:,.0f})")
                score_parts.append(0.20)
            elif t.liquidity_usd >= 50000:
                signals.append(f"💧💧 نقدینگی خوب (${t.liquidity_usd:,.0f})")
                score_parts.append(0.15)
            elif t.liquidity_usd >= 20000:
                signals.append(f"💧 نقدینگی قابل قبول (${t.liquidity_usd:,.0f})")
                score_parts.append(0.08)
            elif t.liquidity_usd < 5000:
                risk_flags.append(f"⚠️ نقدینگی پایین (${t.liquidity_usd:,.0f})")
                score_parts.append(-0.10)

            # ──── Signal 3: MC/Liquidity ratio ────
            if t.mcap > 0 and t.liquidity_usd > 0:
                mc_liq = t.mcap / t.liquidity_usd
                if mc_liq < 5:
                    signals.append(f"✅ MC/نقدینگی عالی ({mc_liq:.1f}x)")
                    score_parts.append(0.15)
                elif mc_liq < 15:
                    signals.append(f"📊 MC/نقدینگی خوب ({mc_liq:.1f}x)")
                    score_parts.append(0.08)
                elif mc_liq > 100:
                    risk_flags.append(f"⚠️ MC/نقدینگی بالا ({mc_liq:.1f}x)")
                    score_parts.append(-0.08)

            # ──── Signal 4: Liquidity per transaction (market depth) ────
            if t.txns_24h > 0 and t.liquidity_usd > 0:
                liq_per_txn = t.liquidity_usd / t.txns_24h
                if liq_per_txn > 5000:
                    signals.append(f"✅ عمق خوب بازار (${liq_per_txn:,.0f}/تراکنش)")
                    score_parts.append(0.10)
                elif liq_per_txn > 1000:
                    signals.append(f"📊 عمق بازار (${liq_per_txn:,.0f}/تراکنش)")
                    score_parts.append(0.06)

            # ──── Signal 5: Liquidity + Growth combo ────
            if t.liquidity_usd >= 20000 and t.price_change_1h_pct > 15 and t.volume_1h_usd > 10000:
                signals.append(f"🚀 نقدینگی قوی + رشد ({t.price_change_1h_pct:+.1f}%)")
                score_parts.append(0.18)
            elif t.liquidity_usd >= 10000 and t.price_change_1h_pct > 10 and t.volume_1h_usd > 5000:
                signals.append(f"📈 نقدینگی + رشد ({t.price_change_1h_pct:+.1f}%)")
                score_parts.append(0.10)

            # ──── Signal 6: LP likely locked (older + high liq + stable) ────
            if t.age_seconds > 86400 and t.liquidity_usd >= 30000:
                signals.append("🔒 احتمال قفل نقدینگی (بیش از ۲۴ ساعت + نقدینگی بالا)")
                score_parts.append(0.10)

            if not signals:
                continue

            raw = sum(score_parts)
            score = max(0.0, min(1.0, raw))
            if score < 0.50:
                continue

            hits.append(HunterHit(
                strategy="liquidity_health",
                token_key=f"{t.chain}:{t.address}",
                token=t,
                score=score,
                signals=signals,
                risk_flags=risk_flags,
                detail={
                    "liquidity_usd": t.liquidity_usd,
                    "volume_1h": t.volume_1h_usd,
                    "mc_liq_ratio": round(t.mcap / t.liquidity_usd, 2) if t.liquidity_usd > 0 and t.mcap > 0 else 0,
                },
            ))

        hits.sort(key=lambda h: h.score, reverse=True)
        return hits[:self.cfg.max_results_per_strategy]


# ==========================================================
# Strategy 8: Holder Distribution Analyzer — تحلیل توزیع هولدرها
# ==========================================================

class HolderDistributionAnalyzer:
    """تحلیل توزیع هولدرها از طریق رفتار تراکنش — بدون GMGN.

    سیگنال‌ها (همه از DexScreener):
    ۱. تعداد تراکنش بالا نسبت به سن = توزیع خوب
    ۲. حجم بالا/تراکنش = هولدرهای بزرگ فعال
    ۳. رشد تدریجی قیت = توزیع سالم
    ۴. تراکنش پایدار = هولدرهای واقعی (نه bot)
    ۵. ترکیب حجم + تراکنش + سن = کیفیت هولدرها
    """

    def __init__(self, config: HunterConfig) -> None:
        self.cfg = config

    def evaluate(self, tokens: List[DEXToken]) -> List[HunterHit]:
        hits: List[HunterHit] = []

        for t in tokens:
            if not t.address or t.txns_24h <= 0:
                continue
            if t.liquidity_usd < 5000 or t.volume_1h_usd < 2000:
                continue

            signals: List[str] = []
            risk_flags: List[str] = []
            score_parts: List[float] = []

            # ──── Signal 1: Transaction density (many unique buyers) ────
            if t.age_seconds > 0:
                txns_per_min = t.txns_24h / max(t.age_seconds / 60, 1)
                if txns_per_min > 20:
                    signals.append(f"👥 تراکنش خیلی بالا ({txns_per_min:.1f}/د) — توزیع عالی")
                    score_parts.append(min(0.25, txns_per_min / 80))
                elif txns_per_min > 5:
                    signals.append(f"👥 تراکنش بالا ({txns_per_min:.1f}/د)")
                    score_parts.append(0.15)
                elif txns_per_min > 1:
                    signals.append(f"📊 تراکنش فعال ({txns_per_min:.1f}/د)")
                    score_parts.append(0.08)

            # ──── Signal 2: Volume per transaction (whale vs retail) ────
            if t.txns_24h > 0 and t.volume_24h_usd > 0:
                vol_per_txn = t.volume_24h_usd / t.txns_24h
                if vol_per_txn > 3000:
                    signals.append(f"🐋 هولدرهای بزرگ فعال (${vol_per_txn:,.0f}/تراکنش)")
                    score_parts.append(0.20)
                elif vol_per_txn > 500:
                    signals.append(f"📊 ترکیب خوب هولدرها (${vol_per_txn:,.0f}/تراکنش)")
                    score_parts.append(0.12)

            # ──── Signal 3: Steady growth = real holders (not bots) ────
            if (t.price_change_1h_pct > 5 and t.price_change_1h_pct < 80
                    and t.volume_1h_usd > 5000):
                signals.append(f"📈 رشد تدریجی ({t.price_change_1h_pct:+.1f}%) — هولدرهای واقعی")
                score_parts.append(0.15)

            # ──── Signal 4: High txns + moderate volume = distributed holders ────
            if t.txns_24h > 100 and t.volume_1h_usd > 3000:
                signals.append(f"🔥 {t.txns_24h} تراکنش — پراکندگی بالا")
                score_parts.append(0.15)
            elif t.txns_24h > 50 and t.volume_1h_usd > 2000:
                signals.append(f"📈 {t.txns_24h} تراکنش — توزیع خوب")
                score_parts.append(0.10)

            # ──── Signal 5: Healthy combo: age + volume + transactions ────
            if t.age_seconds > 1800 and t.txns_24h > 30 and t.volume_1h_usd > 5000:
                score_parts.append(0.10)
                signals.append(f"✅ توکن بالغ ({int(t.age_seconds/60)}د) + فعال")

            # ──── Signal 6: Fresh token with many buyers = early distribution ────
            if t.age_seconds < 1800 and t.txns_24h > 20:
                signals.append(f"⚡ توزیع اولیه ({t.txns_24h} تراکنش در {int(t.age_seconds/60)} دقیقه)")
                score_parts.append(0.18)

            # ──── Risk: Very few transactions = concentrated ────
            if t.txns_24h < 5 and t.age_seconds > 3600:
                risk_flags.append(f"⚠️ فقط {t.txns_24h} تراکنش — احتمال تمرکز")
                score_parts.append(-0.10)

            # ──── Gate ────
            if not signals or len(signals) < 2:
                continue

            raw = sum(score_parts)
            score = max(0.0, min(1.0, raw))
            if score < 0.50:
                continue

            hits.append(HunterHit(
                strategy="holder_distribution",
                token_key=f"{t.chain}:{t.address}",
                token=t,
                score=score,
                signals=signals,
                risk_flags=risk_flags,
                detail={
                    "txns_24h": t.txns_24h,
                    "volume_24h": t.volume_24h_usd,
                    "age_seconds": t.age_seconds,
                },
            ))

        hits.sort(key=lambda h: h.score, reverse=True)
        return hits[:self.cfg.max_results_per_strategy]


# ==========================================================
# Strategy 9: Volume Profile Sniper — شکار حجم مشکوک
# ==========================================================

class VolumeProfileSniper:
    """تشخیص الگوهای حجمی غیرعادی که قبل از پامپ ظاهر می‌شوند.

    سیگنال‌ها:
    ۱. افزایش ناگهانی حجم (volume spike)
    ۲. حجم پایدار بالا (accumulation pattern)
    ۳. نسبت حجم ۵ دقیقه به ۱ ساعته (volume burst)
    ۴. حجم بدون تغییر قیست = accumulation phase
    ۵. حجم + قیمت همزمان بالا = breakout confirmation
    """

    def __init__(self, config: HunterConfig) -> None:
        self.cfg = config

def evaluate(self, tokens: List[DEXToken]) -> List[HunterHit]:
        hits: List[HunterHit] = []

        for t in tokens:
            if t.volume_1h_usd < 5000 or t.liquidity_usd < 5000:
                continue  # hard gate: skip tokens with low activity

            signals: List[str] = []
            risk_flags: List[str] = []
            score_parts: List[float] = []

            if t.volume_1h_usd <= 0 or t.volume_24h_usd <= 0:
                continue

            # ──── Signal 1: Volume spike detection ────
            avg_hourly = t.volume_24h_usd / 24 if t.volume_24h_usd > 0 else 0
            if avg_hourly > 0:
                vol_spike = t.volume_1h_usd / avg_hourly
                if vol_spike > 5.0:
                    signals.append(f"🔥🔥 حجم ۵ برابر میانگین ({vol_spike:.1f}x)")
                    score_parts.append(min(0.30, vol_spike / 15))
                elif vol_spike > 2.0:
                    signals.append(f"🔥 حجم {vol_spike:.1f}x میانگین")
                    score_parts.append(0.15)
                elif vol_spike > 1.5:
                    signals.append(f"📈 حجم بالاتر از میانگین ({vol_spike:.1f}x)")
                    score_parts.append(0.08)

            # ──── Signal 2: Volume without price change = accumulation ────
            if t.volume_1h_usd > 5000:
                price_flat = abs(t.price_change_5m_pct) < 5
                if price_flat and t.volume_1h_usd > avg_hourly * 2:
                    signals.append("📊 حجم بالا بدون تغییر قیمت = مرحله انباشت")
                    score_parts.append(0.20)

            # ──── Signal 3: Volume + Price = Breakout ────
            if t.price_change_5m_pct > 20 and t.volume_1h_usd > avg_hourly * 3:
                signals.append(f"🚀 بریک‌اوت تأیید شده (قیمت +{t.price_change_5m_pct:.1f}%، حجم ۳x)")
                score_parts.append(0.25)
            elif t.price_change_5m_pct > 10 and t.volume_1h_usd > avg_hourly * 2:
                signals.append(f"📈 بریک‌اوت اولیه (+{t.price_change_5m_pct:.1f}%)")
                score_parts.append(0.15)

            # ──── Signal 4: Volume acceleration ────
            if t.volume_1h_usd > 0 and t.volume_24h_usd > 0:
                vol_concentration = t.volume_1h_usd / (t.volume_24h_usd / 24)
                if vol_concentration > 3.0:
                    signals.append(f"🔥 شتاب حجم ({vol_concentration:.1f}x)")
                    score_parts.append(0.12)

            # ──── Risk: Very low volume ────
            if t.volume_1h_usd < 1000:
                risk_flags.append(f"⚠️ حجم پایین (${t.volume_1h_usd:,.0f}/h)")
                score_parts.append(-0.10)

            if not signals:
                continue

            raw = sum(score_parts)
            score = max(0.0, min(1.0, raw))
            if score < 0.50:
                continue

            hits.append(HunterHit(
                strategy="volume_profile",
                token_key=f"{t.chain}:{t.address}",
                token=t,
                score=score,
                signals=signals,
                risk_flags=risk_flags,
                detail={
                    "volume_1h": t.volume_1h_usd,
                    "volume_24h": t.volume_24h_usd,
                    "avg_hourly": avg_hourly,
                },
            ))

        hits.sort(key=lambda h: h.score, reverse=True)
        return hits[:self.cfg.max_results_per_strategy]


# ==========================================================
# Strategy 10: Momentum Breakout Detector — تشخیص بریک‌اوت
# ==========================================================

class MomentumBreakoutDetector:
    """تشخیص الگوهای مومنتوم چند-تایم‌فریمی.

    سیگنال‌ها:
    ۱. رشد قیمت در تمام تایم‌فریم‌ها (۵ دقیقه، ۱ ساعته، ۲۴ ساعته)
    ۲. افزایش تدریجی قیست (accumulation curve)
    ۳. شکست مقاومت‌های کلیدی
    ۴. مومنتوم مثبت + حجم بالا = تأیید قوی
    """

    def __init__(self, config: HunterConfig) -> None:
        self.cfg = config

    def evaluate(self, tokens: List[DEXToken]) -> List[HunterHit]:
        hits: List[HunterHit] = []

        for t in tokens:
            if t.liquidity_usd < 10000 or t.volume_1h_usd < 5000:
                continue
            if t.price_change_5m_pct > 80:
                continue  # too late, pump already happened

            signals: List[str] = []
            risk_flags: List[str] = []
            score_parts: List[float] = []

            # ──── Signal 1: Multi-TF alignment ────
            tf_up = sum(1 for pct in [t.price_change_5m_pct, t.price_change_1h_pct, t.price_change_24h_pct]
                        if pct > 0)
            tf_down = sum(1 for pct in [t.price_change_5m_pct, t.price_change_1h_pct, t.price_change_24h_pct]
                          if pct < 0)

            if tf_up == 3:
                signals.append("🚀🚀 رشد در تمام تایم‌فریم‌ها (۵د/۱س/۲۴س)")
                score_parts.append(0.30)
            elif tf_up == 2 and t.price_change_5m_pct > 0:
                signals.append(f"📈 رشد در ۲ تایم‌فریم (+{t.price_change_5m_pct:.1f}%, +{t.price_change_1h_pct:.1f}%)")
                score_parts.append(0.18)
            elif tf_down == 3:
                risk_flags.append("⚠️ ریزش در تمام تایم‌فریم‌ها")
                score_parts.append(-0.20)

            # ──── Signal 2: Accelerating momentum ────
            if t.price_change_5m_pct > t.price_change_1h_pct / 12:
                # 5m rate > 1h rate per 12 candles = accelerating
                if t.price_change_5m_pct > 10:
                    signals.append(f"🔥 شتاب مومنتوم ({t.price_change_5m_pct:.1f}% در ۵ دقیقه)")
                    score_parts.append(0.20)

            # ──── Signal 3: Strong 1h candle ────
            if t.price_change_1h_pct > 50:
                signals.append(f"🚀🚀 پامپ بزرگ ۱ ساعته ({t.price_change_1h_pct:+.1f}%)")
                score_parts.append(0.25)
            elif t.price_change_1h_pct > 20:
                signals.append(f"🚀 پامپ خوب ۱ ساعته ({t.price_change_1h_pct:+.1f}%)")
                score_parts.append(0.15)
            elif t.price_change_1h_pct > 10:
                signals.append(f"📈 رشد ۱ ساعته ({t.price_change_1h_pct:+.1f}%)")
                score_parts.append(0.08)

            # ──── Signal 4: Fresh listing momentum ────
            if t.age_seconds < 1800 and t.price_change_5m_pct > 30:
                signals.append(f"⚡ مومنتوم تازه‌وارد ({t.price_change_5m_pct:+.1f}%)")
                score_parts.append(0.20)

            # ──── Signal 5: Volume confirmation of momentum ────
            if t.price_change_5m_pct > 15 and t.volume_1h_usd > 10000:
                signals.append(f"🔥 مومنتوم + حجم تأیید شده")
                score_parts.append(0.15)

            # ──── Risk: Sharp reversal ────
            if t.price_change_5m_pct < -30 and t.price_change_1h_pct > 20:
                risk_flags.append(f"⚠️ برگشت شدید ({t.price_change_5m_pct:+.1f}% از +{t.price_change_1h_pct:.1f}%)")
                score_parts.append(-0.15)

            if not signals:
                continue

            raw = sum(score_parts)
            score = max(0.0, min(1.0, raw))
            if score < 0.55:
                continue

            hits.append(HunterHit(
                strategy="momentum_breakout",
                token_key=f"{t.chain}:{t.address}",
                token=t,
                score=score,
                signals=signals,
                risk_flags=risk_flags,
                detail={
                    "price_change_5m": t.price_change_5m_pct,
                    "price_change_1h": t.price_change_1h_pct,
                    "price_change_24h": t.price_change_24h_pct,
                    "tf_alignment": f"{tf_up}up/{tf_down}down",
                },
            ))

        hits.sort(key=lambda h: h.score, reverse=True)
        return hits[:self.cfg.max_results_per_strategy]


# ==========================================================
# Unified Meme Hunter
# ==========================================================

class MemeHunter:
    """Unified meme coin hunter — ۱۰ استراتژی شکار میم‌کوین.

    هر ۳۰ ثانیه:
      ۱. توکن‌های تازه از DexScreener/Pump.fun/GMGN دریافت می‌شوند
      ۲. هر ۱۰ استراتژی به‌صورت موازی اجرا می‌شوند
      ۳. نتایج رتبه‌بندی و به داشبورد ارسال می‌شوند
      ۴. هر تشخیص ذخیره می‌شه تا بعداً نتیجه‌اش سنجیده بشه

    استراتژی‌ها:
      ۱. Pre-Pump Hunter   — شکار قبل از پامپ
      ۲. Post-Migration    — بعد از Migration
      ۳. Smart Money       — ردیابی پول هوشمند
      ۴. Narrative Hunter  — شکار روایت‌ها
      ۵. Contract Safety   — تحلیل امنیتی قرارداد
      ۶. Whale Activity    — ردیابی نهنگ‌ها
      ۷. Liquidity Health  — تحلیل سلامت نقدینگی
      ۸. Holder Distribution — تحلیل توزیع هولدرها
      ۹. Volume Profile    — شکار حجم مشکوک
      ۱۰. Momentum Breakout — تشخیص بریک‌اوت
    """

    def __init__(self, config: Dict[str, Any] | None = None) -> None:
        self.cfg = config_from_yaml(config or {})
        self.gmgn = GMGNClient()
        # Original 4 strategies
        self.pre_pump = PrePumpHunter(self.cfg, self.gmgn)
        self.post_migration = PostMigrationHunter(self.cfg)
        self.smart_money = SmartMoneyTracker(self.cfg, self.gmgn)
        self.narrative = NarrativeHunter(self.cfg, self.gmgn)
        # 6 new advanced strategies
        self.contract_safety = RugpullDetector(self.cfg)
        self.whale_activity = WhaleActivityTracker(self.cfg)
        self.liquidity_health = LiquidityHealthAnalyzer(self.cfg)
        self.holder_distribution = HolderDistributionAnalyzer(self.cfg)
        self.volume_profile = VolumeProfileSniper(self.cfg)
        self.momentum_breakout = MomentumBreakoutDetector(self.cfg)
        self.tracker = get_hunter_tracker()
        self._last_scan: float = 0.0
        self._all_hits: List[HunterHit] = []
        self._by_strategy: Dict[str, List[HunterHit]] = {}
        # Time-based dedup: token_key -> last detection timestamp
        self._detected_recently: Dict[str, float] = {}
        # News tracker (for trending boost)
        self._news_tracker: Any = None
        try:
            from core.news import get_news_tracker
            self._news_tracker = get_news_tracker()
        except Exception:  # noqa: BLE001
            pass
        self._dedup_seconds: float = 900  # 15 min cooldown between re-detections

    def _save_detections(self, hits: List[HunterHit]) -> None:
        """Save new detections to tracker DB for success monitoring."""
        for h in hits:
            try:
                rec = DetectionRecord(
                    token_key=h.token_key,
                    symbol=h.token.symbol,
                    chain=h.token.chain,
                    dex=h.token.dex,
                    address=h.token.address,
                    strategy=h.strategy,
                    score=h.score,
                    price_at_detection=h.token.price_usd,
                    mcap_at_detection=h.token.mcap,
                    volume_1h_at_detection=h.token.volume_1h_usd,
                    liquidity_at_detection=h.token.liquidity_usd,
                    detected_at=h.detected_at,
                    signals=str(h.signals),
                    risk_flags=str(h.risk_flags),
                )
                self.tracker.save_detection(rec)
                logger.info(f"Tracker saved: {h.token.symbol} ({h.strategy}) score={h.score:.2f}")
            except Exception as exc:  # noqa: BLE001
                logger.warning(f"Tracker save failed for {h.token_key}: {exc}")

    async def scan(self, dex_mgr: DEXManager) -> Dict[str, List[dict]]:
        """Run a full scan across all 10 strategies."""
        now = time.time()
        if now - self._last_scan < self.cfg.scan_interval_seconds:
            return {k: [h.to_dict() for h in v] for k, v in self._by_strategy.items()}

        try:
            # Fetch fresh tokens from multiple sources
            tokens = await dex_mgr.discover_tokens(limit=300)
            if tokens:
                logger.info(f"MemeHunter: {len(tokens)} tokens discovered, "
                           f"sample: {tokens[0].symbol} (${tokens[0].price_usd:.8f})" if tokens else "")
            else:
                logger.warning("MemeHunter: 0 tokens discovered — check API connectivity")
        except Exception as exc:
            logger.error(f"MemeHunter token fetch failed: {exc}")
            return {}

        # Run all 10 strategies
        pre_pump_hits = await self.pre_pump.evaluate(tokens)
        post_mig_hits = self.post_migration.evaluate(tokens)
        smart_hits = []  # disabled: GMGN data too unreliable
        narrative_hits = await self.narrative.evaluate(tokens)
        contract_hits = self.contract_safety.evaluate(tokens)
        whale_hits = self.whale_activity.evaluate(tokens)
        liquidity_hits = self.liquidity_health.evaluate(tokens)
        holder_hits = self.holder_distribution.evaluate(tokens)
        volume_hits = self.volume_profile.evaluate(tokens)
        momentum_hits = self.momentum_breakout.evaluate(tokens)

        self._by_strategy = {
            "pre_pump": pre_pump_hits,
            "post_migration": post_mig_hits,
            "smart_money": smart_hits,
            "narrative": narrative_hits,
            "contract_safety": contract_hits,
            "whale_activity": whale_hits,
            "liquidity_health": liquidity_hits,
            "holder_distribution": holder_hits,
            "volume_profile": volume_hits,
            "momentum_breakout": momentum_hits,
        }

        # Dedup across strategies + cross-strategy boost + time-based cooldown
        now_ts = time.time()
        token_strategies: Dict[str, Set[str]] = {}  # token_key -> set of strategies that hit it
        all_candidates: Dict[str, HunterHit] = {}   # token_key -> best (highest score) hit
        all_strategy_lists = [
            pre_pump_hits, post_mig_hits, smart_hits, narrative_hits,
            contract_hits, whale_hits, liquidity_hits, holder_hits,
            volume_hits, momentum_hits,
        ]
        for strat_hits in all_strategy_lists:
            for h in strat_hits:
                token_strategies.setdefault(h.token_key, set()).add(h.strategy)
                if h.token_key not in all_candidates or h.score > all_candidates[h.token_key].score:
                    all_candidates[h.token_key] = h

        self._all_hits = []
        for token_key, hit in all_candidates.items():
            # Time-based dedup: skip recently detected tokens
            last_seen = self._detected_recently.get(token_key, 0)
            if now_ts - last_seen < self._dedup_seconds:
                continue

            # Cross-strategy boost — count DIFFERENT signal types, not circular volume signals
            volume_strats = {"whale_activity", "liquidity_health", "holder_distribution",
                              "volume_profile", "momentum_breakout", "contract_safety"}
            tok_strats = token_strategies.get(token_key, set())
            # Volume-based strategies count as at most 1 slot
            vol_has = bool(tok_strats & volume_strats)
            non_vol_count = len(tok_strats - volume_strats)
            strat_count = non_vol_count + (1 if vol_has else 0)

            if strat_count >= 4:
                hit.score = min(1.0, hit.score + 0.30)
                hit.signals.append(f"🔥🔥🔥 تأیید {strat_count} نوع استراتژی — سیگنال خیلی قوی")
            elif strat_count >= 3:
                hit.score = min(1.0, hit.score + 0.20)
                hit.signals.append(f"🔥🔥 تأیید {strat_count} نوع استراتژی — سیگنال قوی")
            elif strat_count >= 2:
                hit.score = min(1.0, hit.score + 0.10)
                hit.signals.append(f"📈 تأیید {strat_count} نوع استراتژی")

            # Trending boost: if token is on CoinGecko trending
            if self._news_tracker and self._news_tracker.is_trending(hit.token.symbol):
                hit.score = min(1.0, hit.score + 0.15)
                hit.signals.append("🔥 CoinGecko Trending — محبوبیت بالا")

            # Register detection time
            self._detected_recently[token_key] = now_ts
            self._all_hits.append(hit)

        # Prune old entries from dedup cache (older than 30 min)
        cutoff = now_ts - 1800
        self._detected_recently = {k: v for k, v in self._detected_recently.items() if v > cutoff}

        # Save detections for success tracking
        self._save_detections(self._all_hits)

        self._last_scan = now
        strat_counts = {k: len(v) for k, v in self._by_strategy.items()}
        logger.info(
            f"MemeHunter scan complete: {len(self._all_hits)} unique | "
            f"strategies: {strat_counts}"
        )
        return {k: [h.to_dict() for h in v] for k, v in self._by_strategy.items()}

    def get_all_hits(self) -> List[dict]:
        return [h.to_dict() for h in self._all_hits]

    def get_strategy_hits(self, strategy: str) -> List[dict]:
        return [h.to_dict() for h in self._by_strategy.get(strategy, [])]

    def get_daily_picks(self, limit: int = 5) -> List[dict]:
        """انتخاب ۴-۵ توکن با احتمال بالا برای پامپ روزانه.

        فیلترها:
        - امتیاز >= 0.70
        - تأیید حداقل ۲ استراتژی (cross-strategy)
        - حجم معقول (> $1000)
        - نقدینگی > $5000
        """
        candidates = []
        for hit in self._all_hits:
            if hit.score < 0.70:
                continue
            # Count how many strategies hit this token
            strat_count = len(self._by_strategy.get(hit.token_key, set())
                             if hasattr(self._by_strategy.get(hit.token_key), '__len__')
                             else [h for h in self._all_hits if h.token_key == hit.token_key])
            # Count unique strategies from _all_hits for this token
            unique_strats = set()
            for h in self._all_hits:
                if h.token_key == hit.token_key:
                    unique_strats.add(h.strategy)
            if len(unique_strats) < 2:
                continue
            # Require minimum liquidity and volume
            if hit.token.liquidity_usd < 5000:
                continue
            if hit.token.volume_1h_usd < 1000:
                continue
            # Risk flag filter — reject tokens with critical risk flags
            has_critical_risk = any(
                "دستکاری" in f or "Honeypot" in f or "Dump" in f
                for f in hit.risk_flags
            )
            if has_critical_risk:
                continue
            candidates.append({
                "symbol": hit.token.symbol,
                "name": hit.token.name,
                "chain": hit.token.chain,
                "dex": hit.token.dex,
                "address": hit.token.address,
                "price_usd": hit.token.price_usd,
                "mcap": hit.token.mcap,
                "liquidity_usd": hit.token.liquidity_usd,
                "volume_1h_usd": hit.token.volume_1h_usd,
                "volume_24h_usd": hit.token.volume_24h_usd,
                "age_seconds": hit.token.age_seconds,
                "score": round(hit.score, 3),
                "strategies": list(unique_strats),
                "strategy_count": len(unique_strats),
                "signals": hit.signals,
                "risk_flags": hit.risk_flags,
                "detected_at": hit.detected_at,
                "price_change_5m_pct": hit.token.price_change_5m_pct,
                "price_change_1h_pct": hit.token.price_change_1h_pct,
                "price_change_24h_pct": hit.token.price_change_24h_pct,
                "txns_24h": hit.token.txns_24h,
            })
        # Sort by score (highest first), then by strategy count
        candidates.sort(key=lambda x: (x["score"], x["strategy_count"]), reverse=True)
        # Deduplicate by token_key
        seen = set()
        picks = []
        for c in candidates:
            key = f"{c['chain']}:{c['address']}"
            if key in seen:
                continue
            seen.add(key)
            picks.append(c)
            if len(picks) >= limit:
                break
        return picks

    def get_summary(self) -> dict:
        # Get success rate stats from tracker
        try:
            strategy_stats = self.tracker.get_all_stats()
            overall = self.tracker.get_overall_stats()
            success_data = {
                "overall": overall,
                "by_strategy": {k: v.to_dict() for k, v in strategy_stats.items()},
            }
        except Exception:  # noqa: BLE001
            success_data = {"overall": {}, "by_strategy": {}}

        # Use _by_strategy for counts (not _all_hits which is dedup-filtered)
        all_strat_counts = {k: len(v) for k, v in self._by_strategy.items()}
        total = sum(all_strat_counts.values())

        # Build flat list for avg_score and top_tokens
        all_flat = []
        if self._all_hits:
            all_flat = list(self._all_hits)
        else:
            for strat, hits in self._by_strategy.items():
                for h in hits:
                    all_flat.append(h)
        all_flat.sort(key=lambda h: h.score, reverse=True)

        top_tokens = [
            {
                "symbol": h.token.symbol,
                "chain": h.token.chain,
                "score": round(h.score, 3),
                "strategy": h.strategy,
            }
            for h in all_flat[:10]
        ]

        return {
            "total_unique": total,
            "by_strategy": all_strat_counts,
            "last_scan": self._last_scan,
            "avg_score": (
                sum(h.score for h in all_flat) / len(all_flat)
                if all_flat else 0
            ),
            "success": success_data,
            "top_tokens": top_tokens,
        }


# ==========================================================
# Helpers
# ==========================================================

def _fmt_age(seconds: float) -> str:
    if seconds < 60:
        return f"{int(seconds)} ثانیه"
    if seconds < 3600:
        return f"{int(seconds / 60)} دقیقه"
    if seconds < 86400:
        return f"{int(seconds / 3600)} ساعت"
    return f"{int(seconds / 86400)} روز"


# Singleton
_hunter: Optional[MemeHunter] = None


def get_meme_hunter(config: Dict[str, Any] | None = None) -> MemeHunter:
    global _hunter
    if _hunter is None:
        _hunter = MemeHunter(config)
    return _hunter
