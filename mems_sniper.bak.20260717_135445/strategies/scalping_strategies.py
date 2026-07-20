"""Scalping-specific strategies — optimized for 1m/5m timeframes on high-volume coins.

Each strategy targets quick, high-probability entries with tight SL/TP.
Strategies are designed for rapid profit-taking (1-3 minutes hold time).
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional, Dict, Any

import numpy as np
import pandas as pd

from core.models import StrategyHit
from strategies import indicators as ind


@dataclass
class ScalpHit:
    name: str
    timeframe: str
    score: float
    weight: float
    detail: Dict[str, Any]


# ==========================================================
# Base
# ==========================================================
class BaseScalpStrategy:
    name: str = "base_scalp"
    default_weight: float = 1.0

    def __init__(self, params: Dict[str, Any], weight: float | None = None) -> None:
        self.params = params
        self.weight = weight if weight is not None else params.get("weight", self.default_weight)
        self.enabled = bool(params.get("enabled", True))

    def evaluate(self, df: pd.DataFrame, ctx: Dict[str, Any]) -> Optional[StrategyHit]:
        raise NotImplementedError


# ==========================================================
# 1) Scalp VWAP Rejection — price rejects VWAP level sharply
# ==========================================================
class ScalpVWAPRejection(BaseScalpStrategy):
    """Price touches VWAP and rejects with a strong wick — mean reversion scalp."""
    name = "scalp_vwap_rejection"
    default_weight = 1.2

    def evaluate(self, df: pd.DataFrame, ctx: Dict[str, Any]) -> Optional[StrategyHit]:
        if not self.enabled or len(df) < 30:
            return None

        high = df["high"].astype(float)
        low = df["low"].astype(float)
        close = df["close"].astype(float)
        volume = df["volume"].astype(float)

        vwap_val = ind.vwap(high, low, close, volume)
        if vwap_val.iloc[-1] <= 0 or not np.isfinite(vwap_val.iloc[-1]):
            return None

        last = df.iloc[-1]
        vwap_now = float(vwap_val.iloc[-1])
        deviation_pct = float(self.params.get("deviation_pct", 0.15))
        wick_ratio = float(self.params.get("wick_ratio", 0.6))

        body = abs(last.close - last.open)
        total_range = last.high - last.low
        if total_range <= 0:
            return None

        # Bullish rejection: price touched VWAP from below, closed above with long lower wick
        touched_vwap_low = last.low <= vwap_now * (1 + deviation_pct / 100)
        closed_above = last.close > vwap_now
        lower_wick = min(last.close, last.open) - last.low
        if touched_vwap_low and closed_above and body > 0:
            wick_pct = lower_wick / total_range
            if wick_pct >= wick_ratio:
                vol_avg = volume.rolling(20).mean().iloc[-1]
                vol_ratio = float(volume.iloc[-1] / vol_avg) if vol_avg > 0 else 1.0
                score = float(np.clip(0.5 + 0.2 * wick_pct + 0.1 * min(vol_ratio, 3) / 3, 0, 1))
                return StrategyHit(self.name, ctx.get("timeframe", "?"),
                                   score=round(score, 4), weight=self.weight,
                                   detail={"side": "long", "vwap": round(vwap_now, 6),
                                           "wick_pct": round(wick_pct, 3),
                                           "vol_ratio": round(vol_ratio, 2)})

        # Bearish rejection: price touched VWAP from above, closed below with long upper wick
        touched_vwap_high = last.high >= vwap_now * (1 - deviation_pct / 100)
        closed_below = last.close < vwap_now
        upper_wick = last.high - max(last.close, last.open)
        if touched_vwap_high and closed_below and body > 0:
            wick_pct = upper_wick / total_range
            if wick_pct >= wick_ratio:
                vol_avg = volume.rolling(20).mean().iloc[-1]
                vol_ratio = float(volume.iloc[-1] / vol_avg) if vol_avg > 0 else 1.0
                score = float(np.clip(0.5 + 0.2 * wick_pct + 0.1 * min(vol_ratio, 3) / 3, 0, 1))
                return StrategyHit(self.name, ctx.get("timeframe", "?"),
                                   score=round(score, 4), weight=self.weight,
                                   detail={"side": "short", "vwap": round(vwap_now, 6),
                                           "wick_pct": round(wick_pct, 3),
                                           "vol_ratio": round(vol_ratio, 2)})

        return None


# ==========================================================
# 2) Scalp RSI Extreme Reversal — RSI at extremes + reversal candle
# ==========================================================
class ScalpRSIExtreme(BaseScalpStrategy):
    """RSI hits extreme levels (< 20 or > 80) and shows reversal candle."""
    name = "scalp_rsi_extreme"
    default_weight = 1.1

    def evaluate(self, df: pd.DataFrame, ctx: Dict[str, Any]) -> Optional[StrategyHit]:
        if not self.enabled or len(df) < 30:
            return None

        close = df["close"].astype(float)
        rsi_length = int(self.params.get("rsi_length", 7))
        oversold = float(self.params.get("oversold", 20))
        overbought = float(self.params.get("overbought", 80))

        rsi_val = ind.rsi(close, rsi_length)
        if rsi_val.isna().all():
            return None

        rsi_now = float(rsi_val.iloc[-1])
        rsi_prev = float(rsi_val.iloc[-2])
        last = df.iloc[-1]
        body = last.close - last.open

        # Bullish reversal: RSI was oversold, now turning up, bullish candle
        if rsi_now < oversold and rsi_now > rsi_prev and body > 0:
            strength = (oversold - rsi_now) / oversold
            vol = df["volume"].astype(float)
            vol_avg = vol.rolling(20).mean().iloc[-1]
            vol_ratio = float(vol.iloc[-1] / vol_avg) if vol_avg > 0 else 1.0
            score = float(np.clip(0.5 + 0.25 * strength + 0.1 * min(vol_ratio, 3) / 3, 0, 1))
            return StrategyHit(self.name, ctx.get("timeframe", "?"),
                               score=round(score, 4), weight=self.weight,
                               detail={"side": "long", "rsi": round(rsi_now, 2),
                                       "rsi_prev": round(rsi_prev, 2),
                                       "vol_ratio": round(vol_ratio, 2)})

        # Bearish reversal: RSI was overbought, now turning down, bearish candle
        if rsi_now > overbought and rsi_now < rsi_prev and body < 0:
            strength = (rsi_now - overbought) / (100 - overbought)
            vol = df["volume"].astype(float)
            vol_avg = vol.rolling(20).mean().iloc[-1]
            vol_ratio = float(vol.iloc[-1] / vol_avg) if vol_avg > 0 else 1.0
            score = float(np.clip(0.5 + 0.25 * strength + 0.1 * min(vol_ratio, 3) / 3, 0, 1))
            return StrategyHit(self.name, ctx.get("timeframe", "?"),
                               score=round(score, 4), weight=self.weight,
                               detail={"side": "short", "rsi": round(rsi_now, 2),
                                       "rsi_prev": round(rsi_prev, 2),
                                       "vol_ratio": round(vol_ratio, 2)})

        return None


# ==========================================================
# 3) Scalp Momentum Burst — sudden large candle with volume surge
# ==========================================================
class ScalpMomentumBurst(BaseScalpStrategy):
    """Large candle body with volume spike — ride the momentum for 1-2 candles."""
    name = "scalp_momentum_burst"
    default_weight = 1.0

    def evaluate(self, df: pd.DataFrame, ctx: Dict[str, Any]) -> Optional[StrategyHit]:
        if not self.enabled or len(df) < 10:
            return None

        close = df["close"].astype(float)
        open_ = df["open"].astype(float)
        high = df["high"].astype(float)
        low = df["low"].astype(float)
        volume = df["volume"].astype(float)

        body_min_pct = float(self.params.get("body_min_pct", 0.3))
        vol_mult = float(self.params.get("volume_multiplier", 2.0))

        last = df.iloc[-1]
        body_pct = abs(last.close - last.open) / last.open * 100 if last.open > 0 else 0

        if body_pct < body_min_pct:
            return None

        # Volume surge check
        vol_avg = volume.rolling(20).mean().iloc[-1]
        vol_ratio = float(volume.iloc[-1] / vol_avg) if vol_avg > 0 else 1.0
        if vol_ratio < vol_mult:
            return None

        # Must be a strong body (not just wicks)
        total_range = last.high - last.low
        if total_range <= 0:
            return None
        body_strength = abs(last.close - last.open) / total_range
        if body_strength < 0.6:
            return None

        side = "long" if last.close > last.open else "short"
        score = float(np.clip(0.45 + 0.2 * min(vol_ratio / 4, 1) + 0.15 * body_strength, 0, 1))

        return StrategyHit(self.name, ctx.get("timeframe", "?"),
                           score=round(score, 4), weight=self.weight,
                           detail={"side": side, "body_pct": round(body_pct, 3),
                                   "vol_ratio": round(vol_ratio, 2),
                                   "body_strength": round(body_strength, 3)})


# ==========================================================
# 4) Scalp Stochastic Extreme — Stochastic K/D cross from extreme zones
# ==========================================================
class ScalpStochasticExtreme(BaseScalpStrategy):
    """Stochastic crossing from oversold/overbought on 1m — quick reversal scalp."""
    name = "scalp_stoch_extreme"
    default_weight = 1.0

    def evaluate(self, df: pd.DataFrame, ctx: Dict[str, Any]) -> Optional[StrategyHit]:
        if not self.enabled or len(df) < 20:
            return None

        close = df["close"].astype(float)
        rsi_length = int(self.params.get("rsi_length", 7))
        stoch_length = int(self.params.get("stoch_length", 7))
        oversold = float(self.params.get("oversold", 15))
        overbought = float(self.params.get("overbought", 85))

        k, d = ind.stochastic_rsi(close, rsi_length, stoch_length)
        if k.isna().all() or d.isna().all():
            return None

        k_now, k_prev = float(k.iloc[-1]), float(k.iloc[-2])
        d_now, d_prev = float(d.iloc[-1]), float(d.iloc[-2])

        # Bullish: K crosses above D from oversold zone
        if k_prev <= d_prev and k_now > d_now and k_now < 25:
            score = float(np.clip(0.5 + 0.2 * ((25 - k_now) / 25), 0, 1))
            return StrategyHit(self.name, ctx.get("timeframe", "?"),
                               score=round(score, 4), weight=self.weight,
                               detail={"side": "long", "k": round(k_now, 2),
                                       "d": round(d_now, 2), "zone": "oversold"})

        # Bearish: K crosses below D from overbought zone
        if k_prev >= d_prev and k_now < d_now and k_now > 75:
            score = float(np.clip(0.5 + 0.2 * ((k_now - 75) / 25), 0, 1))
            return StrategyHit(self.name, ctx.get("timeframe", "?"),
                               score=round(score, 4), weight=self.weight,
                               detail={"side": "short", "k": round(k_now, 2),
                                       "d": round(d_now, 2), "zone": "overbought"})

        return None


# ==========================================================
# 5) Scalp EMA Ribbon — fast EMAs (3/5/8) alignment on 1m
# ==========================================================
class ScalpEMARibbon(BaseScalpStrategy):
    """Fast EMA ribbon (3/5/8/13) alignment for momentum scalping."""
    name = "scalp_ema_ribbon"
    default_weight = 0.9

    def evaluate(self, df: pd.DataFrame, ctx: Dict[str, Any]) -> Optional[StrategyHit]:
        if not self.enabled or len(df) < 20:
            return None

        close = df["close"].astype(float)
        ema_fast = int(self.params.get("fast", 3))
        ema_mid = int(self.params.get("mid", 5))
        ema_slow = int(self.params.get("slow", 8))

        e_fast = ind.ema(close, ema_fast)
        e_mid = ind.ema(close, ema_mid)
        e_slow = ind.ema(close, ema_slow)

        if e_fast.isna().all() or e_mid.isna().all() or e_slow.isna().all():
            return None

        f_now = float(e_fast.iloc[-1])
        m_now = float(e_mid.iloc[-1])
        s_now = float(e_slow.iloc[-1])
        f_prev = float(e_fast.iloc[-2])
        m_prev = float(e_mid.iloc[-2])

        # Bullish ribbon: fast > mid > slow, and just crossed
        if f_now > m_now > s_now and f_prev <= m_prev:
            adx_val = float(ind.adx(df["high"].astype(float), df["low"].astype(float), close, 10).iloc[-1]) if len(df) > 15 else 25.0
            score = float(np.clip(0.45 + 0.2 * min(adx_val / 50, 1), 0, 1))
            return StrategyHit(self.name, ctx.get("timeframe", "?"),
                               score=round(score, 4), weight=self.weight,
                               detail={"side": "long", "ema3": round(f_now, 6),
                                       "ema5": round(m_now, 6), "ema8": round(s_now, 6),
                                       "adx": round(adx_val, 2)})

        # Bearish ribbon: fast < mid < slow, and just crossed
        if f_now < m_now < s_now and f_prev >= m_prev:
            adx_val = float(ind.adx(df["high"].astype(float), df["low"].astype(float), close, 10).iloc[-1]) if len(df) > 15 else 25.0
            score = float(np.clip(0.45 + 0.2 * min(adx_val / 50, 1), 0, 1))
            return StrategyHit(self.name, ctx.get("timeframe", "?"),
                               score=round(score, 4), weight=self.weight,
                               detail={"side": "short", "ema3": round(f_now, 6),
                                       "ema5": round(m_now, 6), "ema8": round(s_now, 6),
                                       "adx": round(adx_val, 2)})

        return None


# ==========================================================
# 6) Scalp Bollinger Touch — price touches BB band and rejects
# ==========================================================
class ScalpBBTouch(BaseScalpStrategy):
    """Price touches Bollinger Band and shows rejection — mean reversion scalp."""
    name = "scalp_bb_touch"
    default_weight = 1.0

    def evaluate(self, df: pd.DataFrame, ctx: Dict[str, Any]) -> Optional[StrategyHit]:
        if not self.enabled or len(df) < 25:
            return None

        close = df["close"].astype(float)
        bb_length = int(self.params.get("bb_length", 15))
        bb_std = float(self.params.get("bb_std", 2.0))

        mid, upper, lower, _ = ind.bollinger_bands(close, bb_length, bb_std)
        if upper.isna().all() or lower.isna().all():
            return None

        last = df.iloc[-1]
        upper_now = float(upper.iloc[-1])
        lower_now = float(lower.iloc[-1])
        mid_now = float(mid.iloc[-1])

        body = abs(last.close - last.open)
        total_range = last.high - last.low
        if total_range <= 0:
            return None

        # Bullish: touched lower band, closed back inside with rejection wick
        if last.low <= lower_now and last.close > lower_now:
            wick = (min(last.close, last.open) - last.low) / total_range
            if wick > 0.3:
                score = float(np.clip(0.5 + 0.2 * wick, 0, 1))
                return StrategyHit(self.name, ctx.get("timeframe", "?"),
                                   score=round(score, 4), weight=self.weight,
                                   detail={"side": "long", "lower": round(lower_now, 6),
                                           "wick_ratio": round(wick, 3),
                                           "close": round(float(last.close), 6)})

        # Bearish: touched upper band, closed back inside with rejection wick
        if last.high >= upper_now and last.close < upper_now:
            wick = (last.high - max(last.close, last.open)) / total_range
            if wick > 0.3:
                score = float(np.clip(0.5 + 0.2 * wick, 0, 1))
                return StrategyHit(self.name, ctx.get("timeframe", "?"),
                                   score=round(score, 4), weight=self.weight,
                                   detail={"side": "short", "upper": round(upper_now, 6),
                                           "wick_ratio": round(wick, 3),
                                           "close": round(float(last.close), 6)})

        return None


# ==========================================================
# 7) Scalp Volume Climax — extreme volume bar with reversal
# ==========================================================
class ScalpVolumeClimax(BaseScalpStrategy):
    """Extreme volume bar (5x+ avg) that closes against the move — exhaustion."""
    name = "scalp_volume_climax"
    default_weight = 1.1

    def evaluate(self, df: pd.DataFrame, ctx: Dict[str, Any]) -> Optional[StrategyHit]:
        if not self.enabled or len(df) < 20:
            return None

        close = df["close"].astype(float)
        open_ = df["open"].astype(float)
        high = df["high"].astype(float)
        low = df["low"].astype(float)
        volume = df["volume"].astype(float)

        vol_mult = float(self.params.get("volume_multiplier", 4.0))
        vol_avg = volume.rolling(20).mean().iloc[-1]
        if vol_avg <= 0:
            return None

        vol_ratio = float(volume.iloc[-1] / vol_avg)
        if vol_ratio < vol_mult:
            return None

        last = df.iloc[-1]
        prev = df.iloc[-2]
        total_range = last.high - last.low
        if total_range <= 0:
            return None

        # Bullish climax: huge volume, price was falling, but closes near high (exhaustion of sellers)
        was_falling = prev.close < prev.open
        closes_near_high = (last.close - last.low) / total_range > 0.7
        if was_falling and closes_near_high:
            score = float(np.clip(0.55 + 0.15 * min(vol_ratio / 8, 1), 0, 1))
            return StrategyHit(self.name, ctx.get("timeframe", "?"),
                               score=round(score, 4), weight=self.weight,
                               detail={"side": "long", "vol_ratio": round(vol_ratio, 2),
                                       "wick_rejection": round(float((last.close - last.low) / total_range), 3)})

        # Bearish climax: huge volume, price was rising, but closes near low (exhaustion of buyers)
        was_rising = prev.close > prev.open
        closes_near_low = (last.high - last.close) / total_range > 0.7
        if was_rising and closes_near_low:
            score = float(np.clip(0.55 + 0.15 * min(vol_ratio / 8, 1), 0, 1))
            return StrategyHit(self.name, ctx.get("timeframe", "?"),
                               score=round(score, 4), weight=self.weight,
                               detail={"side": "short", "vol_ratio": round(vol_ratio, 2),
                                       "wick_rejection": round(float((last.high - last.close) / total_range), 3)})

        return None


# ==========================================================
# 8) Scalp Orderbook Imbalance — quick bid/ask pressure detection
# ==========================================================
class ScalpOrderFlowImbalance(BaseScalpStrategy):
    """Detect sudden order flow imbalance for quick entries."""
    name = "scalp_order_flow"
    default_weight = 1.0

    def evaluate(self, df: pd.DataFrame, ctx: Dict[str, Any]) -> Optional[StrategyHit]:
        if not self.enabled:
            return None

        ob = ctx.get("orderbook")
        if not ob:
            return None

        depth = int(self.params.get("depth_levels", 10))
        imbalance_threshold = float(self.params.get("imbalance_threshold", 2.5))

        bids = ob.get("bids", [])[:depth]
        asks = ob.get("asks", [])[:depth]

        bid_vol = sum(b[1] for b in bids if b and len(b) >= 2)
        ask_vol = sum(a[1] for a in asks if a and len(a) >= 2)

        if bid_vol <= 0 or ask_vol <= 0:
            return None

        ratio = bid_vol / ask_vol

        if ratio > imbalance_threshold:
            score = float(np.clip((ratio - 1) / (imbalance_threshold - 1) * 0.5 + 0.3, 0, 1))
            return StrategyHit(self.name, ctx.get("timeframe", "?"),
                               score=round(score, 4), weight=self.weight,
                               detail={"side": "long", "ratio": round(ratio, 3),
                                       "bid_vol": round(bid_vol, 3),
                                       "ask_vol": round(ask_vol, 3)})

        if ratio < 1 / imbalance_threshold:
            inv_ratio = ask_vol / bid_vol
            score = float(np.clip((inv_ratio - 1) / (imbalance_threshold - 1) * 0.5 + 0.3, 0, 1))
            return StrategyHit(self.name, ctx.get("timeframe", "?"),
                               score=round(score, 4), weight=self.weight,
                               detail={"side": "short", "ratio": round(ratio, 3),
                                       "bid_vol": round(bid_vol, 3),
                                       "ask_vol": round(ask_vol, 3)})

        return None


# ==========================================================
# 9) Scalp Squeeze Release — BB squeeze on 1m releasing
# ==========================================================
class ScalpSqueezeRelease(BaseScalpStrategy):
    """Bollinger Bands squeeze inside Keltner Channels then release — explosive move."""
    name = "scalp_squeeze_release"
    default_weight = 1.0

    def evaluate(self, df: pd.DataFrame, ctx: Dict[str, Any]) -> Optional[StrategyHit]:
        if not self.enabled or len(df) < 30:
            return None

        high = df["high"].astype(float)
        low = df["low"].astype(float)
        close = df["close"].astype(float)

        squeeze = ind.squeeze_detector(high, low, close, 15, 15, 1.5)
        if len(squeeze) < 5:
            return None

        # Check if squeeze just released (was squeezing 2 bars ago, now expanding)
        was_squeezing = float(squeeze.iloc[-3]) == 1.0
        is_expanding = float(squeeze.iloc[-1]) == 0.0

        if not (was_squeezing and is_expanding):
            return None

        # Direction from recent candles
        recent_return = float((close.iloc[-1] - close.iloc[-3]) / close.iloc[-3])
        side = "long" if recent_return > 0 else "short"

        vol = df["volume"].astype(float)
        vol_avg = vol.rolling(20).mean().iloc[-1]
        vol_ratio = float(vol.iloc[-1] / vol_avg) if vol_avg > 0 else 1.0

        score = float(np.clip(0.55 + 0.2 * abs(recent_return) * 100 + 0.1 * min(vol_ratio, 3) / 3, 0, 1))
        return StrategyHit(self.name, ctx.get("timeframe", "?"),
                           score=round(score, 4), weight=self.weight,
                           detail={"side": side, "squeeze_released": True,
                                   "return_3c": round(recent_return * 100, 3),
                                   "vol_ratio": round(vol_ratio, 2)})


# ==========================================================
# 10) Scalp Engulfing Pattern — bullish/bearish engulfing on 1m
# ==========================================================
class ScalpEngulfing(BaseScalpStrategy):
    """Bullish/bearish engulfing candle pattern with volume confirmation."""
    name = "scalp_engulfing"
    default_weight = 0.9

    def evaluate(self, df: pd.DataFrame, ctx: Dict[str, Any]) -> Optional[StrategyHit]:
        if not self.enabled or len(df) < 5:
            return None

        close = df["close"].astype(float)
        open_ = df["open"].astype(float)
        high = df["high"].astype(float)
        low = df["low"].astype(float)
        volume = df["volume"].astype(float)

        a = df.iloc[-2]  # previous candle
        b = df.iloc[-1]  # current candle

        a_body = abs(a.close - a.open)
        b_body = abs(b.close - b.open)

        if a_body <= 0 or b_body <= 0:
            return None

        # Current body must engulf previous body
        if b_body <= a_body:
            return None

        vol_avg = volume.rolling(20).mean().iloc[-1]
        vol_ratio = float(volume.iloc[-1] / vol_avg) if vol_avg > 0 else 1.0

        # Bullish engulfing: prev bearish, curr bullish, curr body engulfs prev
        if a.close < a.open and b.close > b.open:
            if b.open <= a.close and b.close >= a.open:
                score = float(np.clip(0.5 + 0.15 * min(vol_ratio, 3) / 3, 0, 1))
                return StrategyHit(self.name, ctx.get("timeframe", "?"),
                                   score=round(score, 4), weight=self.weight,
                                   detail={"side": "long", "vol_ratio": round(vol_ratio, 2),
                                           "body_ratio": round(b_body / a_body, 2)})

        # Bearish engulfing: prev bullish, curr bearish, curr body engulfs prev
        if a.close > a.open and b.close < b.open:
            if b.open >= a.close and b.close <= a.open:
                score = float(np.clip(0.5 + 0.15 * min(vol_ratio, 3) / 3, 0, 1))
                return StrategyHit(self.name, ctx.get("timeframe", "?"),
                                   score=round(score, 4), weight=self.weight,
                                   detail={"side": "short", "vol_ratio": round(vol_ratio, 2),
                                           "body_ratio": round(b_body / a_body, 2)})

        return None


# ==========================================================
# Registry
# ==========================================================
SCALP_STRATEGY_REGISTRY = {
    "scalp_vwap_rejection": ScalpVWAPRejection,
    "scalp_rsi_extreme": ScalpRSIExtreme,
    "scalp_momentum_burst": ScalpMomentumBurst,
    "scalp_stoch_extreme": ScalpStochasticExtreme,
    "scalp_ema_ribbon": ScalpEMARibbon,
    "scalp_bb_touch": ScalpBBTouch,
    "scalp_volume_climax": ScalpVolumeClimax,
    "scalp_order_flow": ScalpOrderFlowImbalance,
    "scalp_squeeze_release": ScalpSqueezeRelease,
    "scalp_engulfing": ScalpEngulfing,
}


def build_scalp_strategies(strategy_params: Dict[str, Dict[str, Any]]) -> list:
    """Instantiate all enabled scalping strategies from config."""
    out = []
    for name, cls in SCALP_STRATEGY_REGISTRY.items():
        params = strategy_params.get(name, {}) or {}
        instance = cls(params)
        if instance.enabled:
            out.append(instance)
    return out
