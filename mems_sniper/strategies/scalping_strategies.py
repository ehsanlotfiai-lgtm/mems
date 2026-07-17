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


# (Registry moved to end of file — after all class definitions)



# ==========================================================
# Shared helpers for spike / FVG detection (used by MicroMap, PRO BTB, SP2L)
# ==========================================================

def _find_fvg_bullish(open_, high, low, close, idx: int) -> Optional[tuple]:
    """Bullish FVG check anchored at candle `idx` (the middle/displacement candle):
    gap between candle[idx-1].high and candle[idx+1].low. Returns (top, bottom) or None.
    Uses classic 3-candle ICT definition: candle1=idx-1, candle2=idx, candle3=idx+1.
    """
    if idx - 1 < 0 or idx + 1 >= len(close):
        return None
    c1_high = float(high.iloc[idx - 1])
    c3_low = float(low.iloc[idx + 1])
    if c3_low > c1_high:
        return (c3_low, c1_high)  # (top, bottom)
    return None


def _find_fvg_bearish(open_, high, low, close, idx: int) -> Optional[tuple]:
    """Bearish FVG: gap between candle[idx-1].low and candle[idx+1].high."""
    if idx - 1 < 0 or idx + 1 >= len(close):
        return None
    c1_low = float(low.iloc[idx - 1])
    c3_high = float(high.iloc[idx + 1])
    if c1_low > c3_high:
        return (c1_low, c3_high)  # (top, bottom)
    return None


def _is_doji(open_val: float, high_val: float, low_val: float, close_val: float, max_body_ratio: float = 0.15) -> bool:
    """A doji has a tiny body relative to its range."""
    rng = high_val - low_val
    if rng <= 0:
        return True
    body = abs(close_val - open_val)
    return (body / rng) <= max_body_ratio


def _detect_spike_run(open_, close, high, low, atr, n, direction, min_bars, min_body_atr,
                       max_doji_ratio_in_spike=0.3, scan_window=15, scan_start=None):
    """Detect a spike: a run of >= min_bars candles moving in `direction` with
    body >= min_body_atr * ATR, tolerating a limited fraction of doji candles
    (per SP2L/PRO BTB 'Doji Tolerance' filter). Returns (start_idx, end_idx) of the
    run (end_idx = last spike candle, i.e. Wave A -> B point) or None.

    By default the search window is anchored to the END of the series (most
    recent `scan_window` bars) — used by SP2L which always wants the latest
    spike. Pass `scan_start` to anchor the window right AFTER a structural
    point instead (e.g. right after a swing high/low) — used by PRO BTB,
    which must find the spike that broke a specific level, not just any
    recent spike near the end of the array.
    """
    if scan_start is not None:
        range_start = max(0, scan_start)
        range_end = max(range_start, min(n - min_bars + 1, scan_start + scan_window))
    else:
        range_start = max(0, n - scan_window)
        range_end = n - min_bars + 1

    best = None
    for start in range(range_start, range_end):
        count = 0
        doji_count = 0
        end = start
        for i in range(start, min(start + 10, n)):
            o, h, l, c = float(open_.iloc[i]), float(high.iloc[i]), float(low.iloc[i]), float(close.iloc[i])
            body = abs(c - o)
            body_ratio = body / max(atr, 1e-10)
            is_dir = (c > o) if direction == "bullish" else (c < o)
            is_doji = _is_doji(o, h, l, c)

            if is_doji:
                doji_count += 1
                if doji_count / max(count + 1, 1) > max_doji_ratio_in_spike:
                    break
                end = i
                continue

            if body_ratio >= min_body_atr and is_dir:
                count += 1
                end = i
            else:
                break

        if count >= min_bars:
            if best is None or (end - start) >= (best[1] - best[0]):
                best = (start, end)
    return best


# ==========================================================
# 11) MicroMap — Institutional Breakout Zone (FVG) + Pullback Retest
# ==========================================================
class ScalpMicroMap(BaseScalpStrategy):
    """MicroMap Strategy: Institutional breakout-zone scalper (1-15m).

    Faithful to the 'Institutional Breakout Zone' model:
    1. Institutional Breakout Zone Detection — a displacement candle that
       creates a genuine imbalance (Fair Value Gap), not just an arbitrary
       ATR offset. The zone = the FVG itself (top/bottom of the gap).
    2. Multi-timeframe EMA filter — the breakout must align with the
       higher-level trend (EMA(60) on trigger TF acts as the bias filter).
    3. Strict Dual Confirmation Entry:
         a) Confirmed pullback retest of the validated FVG zone, OR
         b) A clean inside bar fully contained within the zone.
       Both must align with the breakout direction.
    4. Volume confirmation is a hard gate (not just a bonus) — the
       displacement candle must show above-average volume, matching the
       'institutional' premise of the strategy.

    Timeframe: 1m-15m (scalp).
    """
    name = "scalp_micromap"
    default_weight = 1.3

    def evaluate(self, df: pd.DataFrame, ctx: Dict[str, Any]) -> Optional[StrategyHit]:
        if not self.enabled or len(df) < 40:
            return None

        close = df["close"].astype(float)
        open_ = df["open"].astype(float)
        high = df["high"].astype(float)
        low = df["low"].astype(float)
        volume = df["volume"].astype(float)

        lookback = int(self.params.get("lookback", 20))
        min_displacement_atr = float(self.params.get("min_displacement_atr", 1.5))
        ema_period = int(self.params.get("ema_filter_period", 60))
        min_vol_ratio = float(self.params.get("min_volume_ratio", 1.3))

        atr_val = float(ind.atr(high, low, close, 14).iloc[-1])
        if atr_val <= 0:
            return None

        n = len(close)

        # ── EMA bias filter (multi-timeframe institutional filter) ──
        ema_val = ind.ema(close, min(ema_period, n - 1))
        htf_bias = "bullish" if float(close.iloc[-1]) > float(ema_val.iloc[-1]) else "bearish"

        # ── Step 1: Find the most recent displacement candle that created a real FVG ──
        displacement_idx = -1
        displacement_dir = ""
        fvg_zone = None
        search_start = max(2, n - lookback)
        for i in range(n - 2, search_start - 1, -1):
            o, h, l, c = float(open_.iloc[i]), float(high.iloc[i]), float(low.iloc[i]), float(close.iloc[i])
            body = abs(c - o)
            rng = h - l
            if rng <= 0:
                continue
            body_atr = body / atr_val
            body_range = body / rng
            if body_atr < min_displacement_atr or body_range < 0.55:
                continue

            direction = "bullish" if c > o else "bearish"
            # Reject if it goes against the higher-TF bias (institutional filter)
            if direction != htf_bias:
                continue

            fvg = _find_fvg_bullish(open_, high, low, close, i) if direction == "bullish" \
                else _find_fvg_bearish(open_, high, low, close, i)
            if fvg is None:
                continue  # No genuine imbalance -> not a valid institutional zone

            # Volume gate — hard requirement, not a bonus
            vol_avg = float(volume.iloc[max(0, i - 20):i].mean()) if i > 0 else 0
            if vol_avg <= 0 or float(volume.iloc[i]) / vol_avg < min_vol_ratio:
                continue

            displacement_idx = i
            displacement_dir = direction
            fvg_zone = fvg  # (top, bottom)
            break

        if displacement_idx < 0 or fvg_zone is None:
            return None

        zone_top, zone_bottom = fvg_zone
        if zone_top <= zone_bottom:
            return None

        # ── Step 2: Wait for pullback into the FVG zone ──
        current = float(close.iloc[-1])
        in_zone = zone_bottom <= current <= zone_top

        last = df.iloc[-1]
        prev = df.iloc[-2]
        confirmed = False

        if displacement_dir == "bullish":
            # a) Confirmed retest: price dips into the zone and closes back above it
            retest_ok = float(last.low) <= zone_top and float(last.close) > zone_bottom and float(last.close) >= float(last.open)
            # b) Inside bar fully inside the FVG zone
            inside_bar_ok = (float(prev.high) <= zone_top and float(prev.low) >= zone_bottom and
                             float(last.high) <= float(prev.high) and float(last.low) >= float(prev.low))
            confirmed = retest_ok or inside_bar_ok
        else:
            retest_ok = float(last.high) >= zone_bottom and float(last.close) < zone_top and float(last.close) <= float(last.open)
            inside_bar_ok = (float(prev.high) <= zone_top and float(prev.low) >= zone_bottom and
                             float(last.high) <= float(prev.high) and float(last.low) >= float(prev.low))
            confirmed = retest_ok or inside_bar_ok

        if not (in_zone or confirmed):
            return None
        if not confirmed:
            return None

        side = "long" if displacement_dir == "bullish" else "short"
        vol_avg2 = volume.rolling(20).mean().iloc[-1]
        vol_ratio = float(volume.iloc[-1] / vol_avg2) if vol_avg2 > 0 else 1.0

        # Score: base + EMA-alignment bonus + volume bonus + zone-tightness bonus
        zone_tightness = 1.0 - min((zone_top - zone_bottom) / max(atr_val, 1e-10) / 2.0, 0.5)
        score = float(np.clip(
            0.58 + 0.12 * min(vol_ratio / 2, 1) + 0.10 * zone_tightness + 0.08,
            0, 1,
        ))

        return StrategyHit(self.name, ctx.get("timeframe", "?"),
                           score=round(score, 4), weight=self.weight,
                           detail={"side": side, "zone_top": round(zone_top, 6),
                                   "zone_bottom": round(zone_bottom, 6),
                                   "htf_bias": htf_bias,
                                   "displacement_atr": round(min_displacement_atr, 2),
                                   "vol_ratio": round(vol_ratio, 2),
                                   "setup": "micromap"})


# ==========================================================
# 12) PRO BTB — Back To Breakeven (Break → Retest → Continuation)
# ==========================================================
class ScalpProBTB(BaseScalpStrategy):
    """PRO BTB Strategy (Poursamadi 'Back To Breakeven' methodology).

    Three-stage execution logic, faithfully implemented:
    1. Breakout of a valid support/resistance level via a SPIKE
       (a run of >=min_spike_bars displacement candles, doji-tolerant).
    2. Back-To-Breakeven: price returns to the broken level, which has
       flipped role (old resistance -> new support, or vice versa). The
       spike must have created a Fair Value Gap (imbalance) — this is
       the structural proof that the breakout was a genuine displacement,
       not a slow grind.
    3. Continuation confirmation: a candlestick reaction at the level
       (rejection wick / bullish-bearish engulfing) rather than a bare
       close price, matching the indicator's 'candlestick confirmation'
       requirement.

    SL is placed behind the breakout spike; TP uses >=1:2 R:R.
    Timeframe: multi-timeframe intraday (works 1m-15m here).
    """
    name = "scalp_pro_btb"
    default_weight = 1.4

    def evaluate(self, df: pd.DataFrame, ctx: Dict[str, Any]) -> Optional[StrategyHit]:
        if not self.enabled or len(df) < 40:
            return None

        close = df["close"].astype(float)
        open_ = df["open"].astype(float)
        high = df["high"].astype(float)
        low = df["low"].astype(float)
        volume = df["volume"].astype(float)

        min_spike_bars = int(self.params.get("min_spike_bars", 2))
        retest_tolerance_pct = float(self.params.get("retest_tolerance_pct", 0.15))
        min_rr = float(self.params.get("min_rr", 2.0))

        atr_val = float(ind.atr(high, low, close, 14).iloc[-1])
        if atr_val <= 0:
            return None

        n = len(close)
        lookback = min(40, n - 5)

        # ── Step 1: Locate structural swing highs/lows (candidate levels) ──
        swing_highs, swing_lows = [], []
        for i in range(3, lookback - 3):
            idx = n - lookback + i
            if idx < 3 or idx >= n - 3:
                continue
            if float(high.iloc[idx]) == float(high.iloc[idx-3:idx+4].max()):
                swing_highs.append((idx, float(high.iloc[idx])))
            if float(low.iloc[idx]) == float(low.iloc[idx-3:idx+4].min()):
                swing_lows.append((idx, float(low.iloc[idx])))

        if not swing_highs and not swing_lows:
            return None

        current = float(close.iloc[-1])
        signal = None

        # ── Step 2a: Bullish — level (resistance) broken by a SPIKE then retested ──
        for sh_idx, sh_price in reversed(swing_highs[-6:]):
            spike = _detect_spike_run(open_, close, high, low, atr_val, n, "bullish",
                                       min_spike_bars, min_body_atr=0.8,
                                       scan_start=sh_idx + 1, scan_window=20)
            if spike is None:
                continue
            spike_start, spike_end = spike
            if spike_start < sh_idx:
                continue  # spike must occur AFTER the level formed
            # The spike's close must have actually broken the level
            if float(close.iloc[spike_end]) <= sh_price:
                continue
            # Structural proof: the spike created a genuine FVG (imbalance)
            fvg = _find_fvg_bullish(open_, high, low, close, spike_end - 1) or \
                  _find_fvg_bullish(open_, high, low, close, spike_end)
            if fvg is None:
                continue

            tolerance = sh_price * (retest_tolerance_pct / 100)
            if not (sh_price - tolerance <= current <= sh_price + tolerance * 3):
                continue  # not currently retesting the broken level

            # Candlestick confirmation: rejection wick or bullish engulfing at the level
            last, prev = df.iloc[-1], df.iloc[-2]
            lower_wick = min(float(last.close), float(last.open)) - float(last.low)
            total_range = float(last.high) - float(last.low)
            rejection = total_range > 0 and (lower_wick / total_range) >= 0.4 and float(last.close) >= sh_price - tolerance
            engulfing = (float(prev.close) < float(prev.open) and float(last.close) > float(last.open) and
                         float(last.close) >= float(prev.open) and float(last.open) <= float(prev.close))
            if not (rejection or engulfing):
                continue

            sl = min(float(low.iloc[spike_start:spike_end + 1].min()), sh_price - tolerance) - 0.1 * atr_val
            risk = current - sl
            if risk <= 0:
                continue
            tp = current + risk * min_rr
            signal = {"side": "long", "level": sh_price, "sl": sl, "tp": tp,
                      "spike_start": spike_start, "spike_end": spike_end}
            break

        # ── Step 2b: Bearish — level (support) broken by a SPIKE then retested ──
        if signal is None:
            for sl_idx, sl_price in reversed(swing_lows[-6:]):
                spike = _detect_spike_run(open_, close, high, low, atr_val, n, "bearish",
                                           min_spike_bars, min_body_atr=0.8,
                                           scan_start=sl_idx + 1, scan_window=20)
                if spike is None:
                    continue
                spike_start, spike_end = spike
                if spike_start < sl_idx:
                    continue
                if float(close.iloc[spike_end]) >= sl_price:
                    continue
                fvg = _find_fvg_bearish(open_, high, low, close, spike_end - 1) or \
                      _find_fvg_bearish(open_, high, low, close, spike_end)
                if fvg is None:
                    continue

                tolerance = sl_price * (retest_tolerance_pct / 100)
                if not (sl_price - tolerance * 3 <= current <= sl_price + tolerance):
                    continue

                last, prev = df.iloc[-1], df.iloc[-2]
                upper_wick = float(last.high) - max(float(last.close), float(last.open))
                total_range = float(last.high) - float(last.low)
                rejection = total_range > 0 and (upper_wick / total_range) >= 0.4 and float(last.close) <= sl_price + tolerance
                engulfing = (float(prev.close) > float(prev.open) and float(last.close) < float(last.open) and
                             float(last.close) <= float(prev.open) and float(last.open) >= float(prev.close))
                if not (rejection or engulfing):
                    continue

                sl_price_stop = max(float(high.iloc[spike_start:spike_end + 1].max()), sl_price + tolerance) + 0.1 * atr_val
                risk = sl_price_stop - current
                if risk <= 0:
                    continue
                tp = current - risk * min_rr
                signal = {"side": "short", "level": sl_price, "sl": sl_price_stop, "tp": tp,
                          "spike_start": spike_start, "spike_end": spike_end}
                break

        if signal is None:
            return None

        vol_avg = volume.rolling(20).mean().iloc[-1]
        vol_ratio = float(volume.iloc[-1] / vol_avg) if vol_avg > 0 else 1.0

        score = float(np.clip(0.62 + 0.13 * min(vol_ratio / 2, 1) + 0.10, 0, 1))

        return StrategyHit(self.name, ctx.get("timeframe", "?"),
                           score=round(score, 4), weight=self.weight,
                           detail={"side": signal["side"],
                                   "level": round(signal["level"], 6),
                                   "sl": round(signal["sl"], 6),
                                   "tp": round(signal["tp"], 6),
                                   "vol_ratio": round(vol_ratio, 2),
                                   "setup": "pro_btb"})


# ==========================================================
# 13) SP2L — Spike + 2-Leg (AB=CD) Pullback Entry
# ==========================================================
class ScalpSP2L(BaseScalpStrategy):
    """SP2L Strategy (Poursamadi 'Spike-2Leg' methodology).

    Faithful sequence:
    1. Spike (Wave A->B): a run of consecutive large-bodied candles in one
       direction (the aggressive impulsive leg), doji-tolerant.
    2. Corrective 2-leg pullback: after the spike, price forms a structured
       retracement — consecutive Higher Lows (bullish) or Lower Highs
       (bearish) — mirroring the AB=CD internal anatomy of the move.
    3. Entry is triggered on RETEST of the most recent HL/LH (not on the
       spike itself) — i.e. the pullback into the structural level.
    4. Stop-Loss = origin of the spike (Wave A) with a small buffer.
    5. Take-Profit = tiered: TP1 at 1R, TP2 at 2R (matches indicator's
       tiered 1:1 / 1:2 target model).

    Timeframe: 1m-5m-15m scalp.
    """
    name = "scalp_sp2l"
    default_weight = 1.3

    def evaluate(self, df: pd.DataFrame, ctx: Dict[str, Any]) -> Optional[StrategyHit]:
        if not self.enabled or len(df) < 20:
            return None

        close = df["close"].astype(float)
        open_ = df["open"].astype(float)
        high = df["high"].astype(float)
        low = df["low"].astype(float)
        volume = df["volume"].astype(float)

        min_spike_bars = int(self.params.get("min_spike_bars", 2))
        min_body_atr = float(self.params.get("min_body_atr", 0.6))
        min_rr = float(self.params.get("min_rr", 1.0))  # TP1 = 1R per methodology

        atr_val = float(ind.atr(high, low, close, 14).iloc[-1])
        if atr_val <= 0:
            return None

        n = len(close)
        current = float(close.iloc[-1])

        bullish_spike = _detect_spike_run(open_, close, high, low, atr_val, n, "bullish",
                                           min_spike_bars, min_body_atr)
        bearish_spike = _detect_spike_run(open_, close, high, low, atr_val, n, "bearish",
                                           min_spike_bars, min_body_atr)

        signal = None

        # ── Bullish: Wave A (spike origin) -> B (spike end) -> HL retest entries ──
        if bullish_spike is not None:
            spike_start, spike_end = bullish_spike
            wave_a = float(low.iloc[spike_start])  # origin of the impulsive leg

            hls = [float(low.iloc[i]) for i in range(spike_end, min(spike_end + 6, n))]
            consecutive_hl = 0
            for i in range(1, len(hls)):
                if hls[i] > hls[i - 1]:
                    consecutive_hl += 1
                else:
                    break

            if consecutive_hl >= 1 and len(hls) >= 2:
                last_hl = hls[-1]
                tolerance = atr_val * 0.3
                retesting = (float(low.iloc[-1]) <= last_hl + tolerance and current > last_hl - tolerance)
                bullish_confirm = float(close.iloc[-1]) > float(open_.iloc[-1])

                if retesting and bullish_confirm and current > wave_a:
                    sl = wave_a - 0.15 * atr_val
                    risk = current - sl
                    if risk > 0:
                        tp1 = current + risk * min_rr
                        tp2 = current + risk * (min_rr * 2)
                        signal = {"side": "long", "wave_a": wave_a, "hl_count": consecutive_hl,
                                  "entry_level": last_hl, "sl": sl, "tp1": tp1, "tp2": tp2}

        # ── Bearish: Wave A (spike origin) -> B (spike end) -> LH retest entries ──
        if signal is None and bearish_spike is not None:
            spike_start, spike_end = bearish_spike
            wave_a = float(high.iloc[spike_start])

            lhs = [float(high.iloc[i]) for i in range(spike_end, min(spike_end + 6, n))]
            consecutive_lh = 0
            for i in range(1, len(lhs)):
                if lhs[i] < lhs[i - 1]:
                    consecutive_lh += 1
                else:
                    break

            if consecutive_lh >= 1 and len(lhs) >= 2:
                last_lh = lhs[-1]
                tolerance = atr_val * 0.3
                retesting = (float(high.iloc[-1]) >= last_lh - tolerance and current < last_lh + tolerance)
                bearish_confirm = float(close.iloc[-1]) < float(open_.iloc[-1])

                if retesting and bearish_confirm and current < wave_a:
                    sl = wave_a + 0.15 * atr_val
                    risk = sl - current
                    if risk > 0:
                        tp1 = current - risk * min_rr
                        tp2 = current - risk * (min_rr * 2)
                        signal = {"side": "short", "wave_a": wave_a, "hl_count": consecutive_lh,
                                  "entry_level": last_lh, "sl": sl, "tp1": tp1, "tp2": tp2}

        if signal is None:
            return None

        vol_avg = volume.rolling(20).mean().iloc[-1]
        vol_ratio = float(volume.iloc[-1] / vol_avg) if vol_avg > 0 else 1.0

        hl_bonus = min(signal.get("hl_count", 1) * 0.06, 0.18)
        score = float(np.clip(0.54 + hl_bonus + 0.12 * min(vol_ratio / 2, 1), 0, 1))

        return StrategyHit(self.name, ctx.get("timeframe", "?"),
                           score=round(score, 4), weight=self.weight,
                           detail={"side": signal["side"],
                                   "wave_a": round(signal["wave_a"], 6),
                                   "hl_count": signal.get("hl_count", 0),
                                   "entry_level": round(signal.get("entry_level", 0), 6),
                                   "sl": round(signal["sl"], 6),
                                   "tp1": round(signal["tp1"], 6),
                                   "tp2": round(signal["tp2"], 6),
                                   "vol_ratio": round(vol_ratio, 2),
                                   "setup": "sp2l"})


# ==========================================================
# Registry (MUST be at end of file — after all class definitions)
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
    "scalp_micromap": ScalpMicroMap,
    "scalp_pro_btb": ScalpProBTB,
    "scalp_sp2l": ScalpSP2L,
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
