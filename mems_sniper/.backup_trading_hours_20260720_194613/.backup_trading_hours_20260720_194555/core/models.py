"""Data models used across the snipers, risk engine and dashboard."""
from __future__ import annotations

import time
from dataclasses import dataclass, field, asdict
from enum import Enum
from typing import Dict, List, Optional


class Side(str, Enum):
    LONG = "long"
    SHORT = "short"
    FLAT = "flat"


class SignalType(str, Enum):
    NEW_LISTING = "new_listing"
    VOLUME_SPIKE = "volume_spike"
    ORDERBOOK_IMBALANCE = "orderbook_imbalance"
    LIQUIDITY_GRAB = "liquidity_grab"
    MOMENTUM_IGNITION = "momentum_ignition"
    RSI_DIVERGENCE = "rsi_divergence"
    BB_BREAKOUT = "bb_breakout"
    FUNDING_OI_SPIKE = "funding_oi_spike"
    SOCIAL_MOMENTUM = "social_momentum"
    NEWS_SENTIMENT = "news_sentiment"


@dataclass
class StrategyHit:
    """A single strategy detecting a setup on a given timeframe."""

    name: str
    timeframe: str
    score: float            # 0..1 confidence of this individual method
    weight: float           # how much this method counts in confluence
    detail: Dict[str, float] = field(default_factory=dict)

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class Signal:
    """A fully assembled, confluence-weighted trading signal."""

    id: str
    created_at: float                      # unix seconds
    exchange: str
    symbol: str
    side: Side
    price: float
    score: float                           # final confidence 0..1
    hits: List[StrategyHit]               # which strategies fired, on which TF
    confluence_tf_breakdown: Dict[str, float]
    entry: float
    stop_loss: float
    take_profit: float                     # TP1 (primary)
    trailing_atr: float
    atr: float
    position_size_usdt: float
    risk_pct: float
    rationale: str                         # human-readable explanation
    chart_url: Optional[str] = None
    status: str = "open"                   # open | tp | sl | trailing | closed
    base: Optional[str] = None             # human-readable ticker (for DEX tokens)
    # Multi-TP levels
    tp2: Optional[float] = None            # Take Profit 2
    tp3: Optional[float] = None            # Take Profit 3

    def to_dict(self) -> dict:
        out = asdict(self)
        out["side"] = self.side.value
        out["hits"] = [h.to_dict() if hasattr(h, "to_dict") else h for h in self.hits]
        return out


@dataclass
class PaperPosition:
    id: str
    opened_at: float
    exchange: str
    symbol: str
    side: Side
    entry: float
    stop_loss: float
    take_profit: float                     # TP1 (primary)
    trailing_atr: float
    atr: float
    size_usdt: float
    qty: float
    status: str = "open"                   # open | closed
    closed_at: Optional[float] = None
    exit_price: Optional[float] = None
    pnl_usdt: Optional[float] = None
    pnl_pct: Optional[float] = None
    close_reason: Optional[str] = None     # tp1 | tp2 | tp3 | sl | trailing | manual
    base: Optional[str] = None             # human-readable ticker (for DEX tokens)
    leverage: float = 1.0                  # leverage multiplier (1.0 = spot)
    market_type: str = "spot"              # spot | futures | dex
    signal_id: Optional[str] = None        # link to triggering signal
    fee_usdt: float = 0.0                  # trading fee in USDT
    slippage_usdt: float = 0.0             # slippage cost in USDT
    # Multi-TP levels
    tp2: Optional[float] = None            # Take Profit 2
    tp3: Optional[float] = None            # Take Profit 3
    tp1_hit: bool = False                  # TP1 already triggered
    risk_free: bool = False                # SL moved to breakeven after TP1
    current_price: Optional[float] = None  # last known price
    unrealized_pnl_pct: Optional[float] = None  # live unrealized PnL

    def to_dict(self) -> dict:
        out = asdict(self)
        out["side"] = self.side.value
        return out


@dataclass
class Candle:
    """A single OHLCV candle (normalized)."""
    timestamp: int      # unix ms
    open: float
    high: float
    low: float
    close: float
    volume: float


def now_ms() -> int:
    return int(time.time() * 1000)


def now_sec() -> float:
    return time.time()
