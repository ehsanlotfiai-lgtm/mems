"""Social momentum tracker — monitors Twitter/Telegram for meme coin hype.

Uses public scraping (no API keys required) via httpx:
  - Twitter: Nitter instances for mention counts
  - Telegram: Public group message frequency

Produces a SocialScore per tracked token that can be fed into the
confluence engine as an additional signal source.
"""
from __future__ import annotations

import asyncio
import re
import time
from dataclasses import dataclass, field
from typing import Dict, List, Optional

import httpx

from config.settings import Settings
from core.logging_setup import logger


@dataclass
class SocialScore:
    """Aggregated social hype score for a token."""
    symbol: str
    twitter_mentions: int = 0
    twitter_sentiment: float = 0.0       # -1..1
    telegram_messages: int = 0
    telegram_sentiment: float = 0.0      # -1..1
    momentum_score: float = 0.0          # 0..1 combined
    trend: str = "stable"                # rising | stable | falling
    updated_at: float = 0.0

    def to_dict(self) -> dict:
        return {
            "symbol": self.symbol,
            "twitter_mentions": self.twitter_mentions,
            "twitter_sentiment": round(self.twitter_sentiment, 3),
            "telegram_messages": self.telegram_messages,
            "telegram_sentiment": round(self.telegram_sentiment, 3),
            "momentum_score": round(self.momentum_score, 3),
            "trend": self.trend,
            "updated_at": self.updated_at,
        }


# Simple sentiment keywords
POSITIVE_WORDS = {
    "bullish", "moon", "pump", "buy", "long", "hodl", "diamond", "rocket",
    "gain", "profit", "bull", "surge", "pump", "hype", "gem", "100x",
    "صعود", "خرید", "سود", "پامپ", "ماه", "الماس", "راکت",
}
NEGATIVE_WORDS = {
    "bearish", "dump", "sell", "short", "scam", "rug", "rekt", "crash",
    "loss", "bear", "plunge", "dead", "exit",
    "نزول", "فروش", "ضرر", "کلاهبرداری", "سقوط", "مرده",
}


class SocialTracker:
    """Track social momentum for meme coins via public scraping."""

    def __init__(self, settings: Settings) -> None:
        self.s = settings
        self.social_cfg = settings.raw.get("social", {})
        self.enabled = bool(self.social_cfg.get("enabled", False))
        self.scores: Dict[str, SocialScore] = {}
        self._prev_counts: Dict[str, int] = {}
        self._client: Optional[httpx.AsyncClient] = None

    async def start(self) -> None:
        if not self.enabled:
            return
        self._client = httpx.AsyncClient(timeout=15, follow_redirects=True)
        logger.info("SocialTracker started")

    async def stop(self) -> None:
        if self._client:
            await self._client.aclose()

    async def fetch_twitter_mentions(self, symbol: str) -> int:
        """Scrape Nitter for mention count of a symbol."""
        if not self._client or not self.enabled:
            return 0
        base = symbol.split("/")[0] if "/" in symbol else symbol
        query = f"${base} OR #{base}"
        nitter_instances = self.social_cfg.get("nitter_instances", [
            "https://nitter.privacydev.net",
            "https://nitter.poast.org",
        ])
        total = 0
        for base_url in nitter_instances[:2]:
            try:
                url = f"{base_url}/search"
                resp = await self._client.get(url, params={"q": query, "f": "tweets"})
                if resp.status_code == 200:
                    # Count tweet items in HTML
                    total += resp.text.count('class="tweet-content')
            except Exception:  # noqa: BLE001
                pass
        return total

    def _simple_sentiment(self, text: str) -> float:
        """Rule-based sentiment score from -1 to 1."""
        words = set(text.lower().split())
        pos = len(words & POSITIVE_WORDS)
        neg = len(words & NEGATIVE_WORDS)
        total = pos + neg
        if total == 0:
            return 0.0
        return (pos - neg) / total

    async def update_score(self, symbol: str) -> SocialScore:
        """Fetch and compute social score for a symbol."""
        twitter_mentions = await self.fetch_twitter_mentions(symbol)
        prev = self._prev_counts.get(symbol, twitter_mentions)
        self._prev_counts[symbol] = twitter_mentions

        # Trend detection
        if twitter_mentions > prev * 1.5 and twitter_mentions > 5:
            trend = "rising"
        elif twitter_mentions < prev * 0.5 and prev > 5:
            trend = "falling"
        else:
            trend = "stable"

        # Momentum score: normalized log scale
        import math
        raw = math.log1p(twitter_mentions) / math.log1p(100)  # 0..1-ish
        momentum = min(1.0, raw)

        score = SocialScore(
            symbol=symbol,
            twitter_mentions=twitter_mentions,
            momentum_score=round(momentum, 4),
            trend=trend,
            updated_at=time.time(),
        )
        self.scores[symbol] = score
        return score

    async def scan_batch(self, symbols: List[str], max_concurrent: int = 5) -> Dict[str, SocialScore]:
        """Update scores for a batch of symbols with concurrency limit."""
        sem = asyncio.Semaphore(max_concurrent)

        async def _inner(sym: str) -> SocialScore:
            async with sem:
                return await self.update_score(sym)

        tasks = [_inner(s) for s in symbols]
        results = await asyncio.gather(*tasks, return_exceptions=True)
        out = {}
        for sym, res in zip(symbols, results):
            if isinstance(res, SocialScore):
                out[sym] = res
        return out

    def get_score(self, symbol: str) -> Optional[SocialScore]:
        return self.scores.get(symbol)


# singleton
_social_tracker: Optional[SocialTracker] = None


def get_social_tracker(settings: Optional[Settings] = None) -> SocialTracker:
    global _social_tracker
    if _social_tracker is None:
        from config.settings import get_settings
        _social_tracker = SocialTracker(settings or get_settings())
    return _social_tracker
