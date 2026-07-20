"""News & Fundamental Analysis — تحلیل بنیادی و اخبار.

منابع رایگان:
  - Alternative.me Fear & Greed Index (بدون API key)
  - CoinGecko Trending (بدون API key)
  - CryptoPanic (اختیاری، با API key)

ترکیب این داده‌ها یه امتیاز sentiment کلی میده که روی سیگنال‌ها تأثیر میذاره.
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

import aiohttp

from core.logging_setup import logger


@dataclass
class NewsScore:
    """Combined news/sentiment score from multiple sources."""
    fear_greed_value: int = 50           # 0 (Extreme Fear) to 100 (Extreme Greed)
    fear_greed_label: str = "Neutral"
    fear_greed_change: int = 0           # change from yesterday
    trending_coins: List[str] = field(default_factory=list)  # top trending coin symbols
    trending_count: int = 0
    overall_sentiment: float = 0.0       # -1.0 (very bearish) to +1.0 (very bullish)
    btc_24h_change: float = 0.0          # BTC 24h price change %
    last_updated: float = 0.0

    def to_dict(self) -> dict:
        return {
            "fear_greed_value": self.fear_greed_value,
            "fear_greed_label": self.fear_greed_label,
            "fear_greed_change": self.fear_greed_change,
            "trending_coins": self.trending_coins,
            "trending_count": self.trending_count,
            "overall_sentiment": round(self.overall_sentiment, 3),
            "btc_24h_change": round(self.btc_24h_change, 2),
            "last_updated": self.last_updated,
        }


class FearGreedClient:
    """Fetch Fear & Greed Index from Alternative.me — free, no API key."""

    DEFAULT_URL = "https://api.alternative.me/fng/?limit=2"

    def __init__(self, url: str = "") -> None:
        self.url = url or self.DEFAULT_URL
        self._session: Optional[aiohttp.ClientSession] = None

    async def _get_session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=10))
        return self._session

    async def fetch(self) -> dict:
        """Return {'value': int, 'label': str, 'change': int} or empty dict."""
        try:
            session = await self._get_session()
            async with session.get(self.url) as resp:
                if resp.status != 200:
                    return {}
                data = await resp.json()
                entries = data.get("data", [])
                if not entries:
                    return {}
                current = entries[0]
                value = int(current.get("value", 50))
                label = current.get("value_classification", "Neutral")
                change = 0
                if len(entries) > 1:
                    prev = int(entries[1].get("value", value))
                    change = value - prev
                return {"value": value, "label": label, "change": change}
        except Exception as exc:  # noqa: BLE001
            logger.debug(f"FearGreed fetch error: {exc}")
            return {}


class CoinGeckoTrendingClient:
    """Fetch trending coins from CoinGecko — free tier, no API key."""

    DEFAULT_URL = "https://api.coingecko.com/api/v3/search/trending"

    def __init__(self, url: str = "") -> None:
        self.url = url or self.DEFAULT_URL
        self._session: Optional[aiohttp.ClientSession] = None

    async def _get_session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=10))
        return self._session

    async def fetch(self) -> dict:
        """Return {'coins': [symbol, ...], 'btc_change': float} or empty dict."""
        try:
            session = await self._get_session()
            async with session.get(self.url) as resp:
                if resp.status != 200:
                    return {}
                data = await resp.json()
                coins = []
                for item in data.get("coins", []):
                    coin = item.get("item", {})
                    sym = coin.get("symbol", "")
                    if sym:
                        coins.append(sym.upper())

                # Also try to get BTC data from coins list
                btc_change = 0.0
                for item in data.get("coins", []):
                    coin = item.get("item", {})
                    if coin.get("symbol", "").upper() == "BTC":
                        btc_change = coin.get("data", {}).get("price_change_percentage_24h", {}).get("usd", 0.0)
                        if btc_change is None:
                            btc_change = 0.0
                        break

                return {"coins": coins[:20], "btc_change": float(btc_change)}
        except Exception as exc:  # noqa: BLE001
            logger.debug(f"CoinGecko trending fetch error: {exc}")
            return {}


class NewsTracker:
    """Unified news/sentiment tracker — combines multiple free sources."""

    def __init__(self, config: Dict[str, Any] | None = None) -> None:
        cfg = config or {}
        self.enabled = bool(cfg.get("enabled", True))
        self.fear_greed = FearGreedClient(cfg.get("fear_greed_url", ""))
        self.trending = CoinGeckoTrendingClient(cfg.get("coingecko_trending_url", ""))
        self.scan_interval = int(cfg.get("scan_interval_seconds", 300))
        self.fear_greed_weight = float(cfg.get("fear_greed_weight", 0.15))
        self.trending_boost = float(cfg.get("trending_boost", 0.10))

        self._score: NewsScore = NewsScore()
        self._last_fetch: float = 0.0
        self._trending_set: set = set()  # quick lookup for trending symbols

    @property
    def score(self) -> NewsScore:
        return self._score

    @property
    def trending_set(self) -> set:
        return self._trending_set

    async def refresh(self) -> NewsScore:
        """Fetch all news sources and compute combined sentiment score."""
        now = time.time()
        if now - self._last_fetch < self.scan_interval:
            return self._score

        fg_data = await self.fear_greed.fetch()
        trend_data = await self.trending.fetch()

        score = NewsScore(last_updated=now)

        # Fear & Greed
        if fg_data:
            score.fear_greed_value = fg_data.get("value", 50)
            score.fear_greed_label = fg_data.get("label", "Neutral")
            score.fear_greed_change = fg_data.get("change", 0)

        # Trending coins
        if trend_data:
            score.trending_coins = trend_data.get("coins", [])
            score.trending_count = len(score.trending_coins)
            score.btc_24h_change = trend_data.get("btc_change", 0.0)

        # Compute overall sentiment (-1 to +1)
        # Fear & Greed: map 0-100 to -1..+1
        fg_sentiment = (score.fear_greed_value - 50) / 50.0  # 0→-1, 50→0, 100→+1

        # BTC trend: if BTC up → bullish, if down → bearish
        btc_sentiment = max(-1.0, min(1.0, score.btc_24h_change / 10.0))

        # Combined: weighted average
        score.overall_sentiment = (fg_sentiment * 0.6 + btc_sentiment * 0.4)

        self._score = score
        self._trending_set = set(s.upper() for s in score.trending_coins)
        self._last_fetch = now

        logger.info(
            f"NewsTracker: F&G={score.fear_greed_value} ({score.fear_greed_label}), "
            f"BTC={score.btc_24h_change:+.1f}%, trending={score.trending_count}, "
            f"sentiment={score.overall_sentiment:+.2f}"
        )
        return score

    def is_trending(self, symbol: str) -> bool:
        """Check if a coin symbol is in CoinGecko trending list."""
        return symbol.upper() in self._trending_set

    def get_dict(self) -> dict:
        """Return current score as dict for API responses."""
        return self._score.to_dict()


# Singleton
_news_tracker: Optional[NewsTracker] = None


def get_news_tracker(config: Dict[str, Any] | None = None) -> NewsTracker:
    global _news_tracker
    if _news_tracker is None:
        _news_tracker = NewsTracker(config)
    return _news_tracker
