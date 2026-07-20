"""The 8 hunting methods (each a small, testable strategy class).

A strategy returns a `StrategyHit` (or None) on each evaluation, given a
DataFrame of candles + context. Strategies are intentionally decoupled
from each other so the `StrategyEngine` can recombine them at any
timeframe and re-weight into a confluence score.
"""
from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Optional, Dict, Any, List

import numpy as np
import pandas as pd

from core.models import StrategyHit, SignalType
from strategies import indicators as ind


# ==========================================================
# Base
# ==========================================================
class BaseStrategy:
    name: str = "base"
    default_weight: float = 1.0

    def __init__(self, params: Dict[str, Any], weight: float | None = None) -> None:
        self.params = params
        self.weight = weight if weight is not None else params.get("weight", self.default_weight)
        self.enabled = bool(params.get("enabled", True))

    def evaluate(self, df: pd.DataFrame, ctx: Dict[str, Any]) -> Optional[StrategyHit]:
        raise NotImplementedError


def _candles_to_df(candles) -> pd.DataFrame:
    if isinstance(candles, pd.DataFrame):
        return candles
    if not candles:
        return pd.DataFrame()
    return pd.DataFrame([{
        "timestamp": c.timestamp, "open": c.open, "high": c.high,
        "low": c.low, "close": c.close, "volume": c.volume,
    } for c in candles])


# ==========================================================
# 1) New listing sniper
# ==========================================================
class NewListingSniper(BaseStrategy):
    name = SignalType.NEW_LISTING.value
    default_weight = 1.5

    def evaluate(self, df: pd.DataFrame, ctx: Dict[str, Any]) -> Optional[StrategyHit]:
        if not self.enabled:
            return None
        listed_at = ctx.get("listed_at")          # ms
        if listed_at is None:
            return None
        max_age_h = float(self.params.get("max_age_hours", 72))
        age_h = (time.time() * 1000 - listed_at) / 3_600_000
        if age_h > max_age_h:
            return None
        first_pct = float(self.params.get("first_candle_min_pct", 5.0))
        if len(df) < 2:
            return None
        first = df.iloc[0]
        body = 100 * (first.close - first.open) / (first.open or np.nan) * np.sign(first.close - first.open)
        if abs(body) < first_pct:
            return None
        side = "long" if body > 0 else "short"
        # newer + bigger first candle = higher score
        age_score = max(0.0, 1.0 - age_h / max_age_h)
        size_score = min(1.0, abs(body) / (first_pct * 4))
        score = 0.4 + 0.4 * age_score + 0.2 * size_score
        return StrategyHit(
            name=self.name,
            timeframe=ctx.get("timeframe", "?"),
            score=round(float(np.clip(score, 0, 1)), 4),
            weight=self.weight,
            detail={"age_hours": round(age_h, 2), "first_body_pct": round(float(body), 3)},
        )


# ==========================================================
# 2) Volume spike
# ==========================================================
class VolumeSpike(BaseStrategy):
    name = SignalType.VOLUME_SPIKE.value
    default_weight = 1.0

    def evaluate(self, df: pd.DataFrame, ctx: Dict[str, Any]) -> Optional[StrategyHit]:
        if not self.enabled or len(df) < 5:
            return None
        mult = float(self.params.get("multiplier", 3.0))
        window = int(self.params.get("rolling_window", 20))
        if len(df) < window + 1:
            window = max(5, len(df) - 1)
        vol = df["volume"]
        avg = vol.iloc[-(window + 1):-1].mean()
        if avg <= 0:
            return None
        last = vol.iloc[-1]
        if last < mult * avg:
            return None
        # z-score for confidence
        rolling = vol.iloc[-(window + 1):-1]
        z = (last - rolling.mean()) / (rolling.std(ddof=0) or 1.0)
        score = float(np.clip(0.3 + 0.2 * z, 0, 1))
        direction = 1 if df["close"].iloc[-1] >= df["open"].iloc[-1] else -1
        return StrategyHit(
            name=self.name,
            timeframe=ctx.get("timeframe", "?"),
            score=round(score, 4),
            weight=self.weight,
            detail={
                "vol_multiplier": round(last / avg, 2),
                "z_score": round(float(z), 3),
                "direction": "long" if direction > 0 else "short",
            },
        )


# ==========================================================
# 3) Order book imbalance
# ==========================================================
class OrderBookImbalance(BaseStrategy):
    name = SignalType.ORDERBOOK_IMBALANCE.value
    default_weight = 1.0

    def evaluate(self, df: pd.DataFrame, ctx: Dict[str, Any]) -> Optional[StrategyHit]:
        if not self.enabled:
            return None
        ob = ctx.get("orderbook")
        if not ob:
            return None
        bids = ob.get("bids", [])[: int(self.params.get("depth_levels", 20))]
        asks = ob.get("asks", [])[: int(self.params.get("depth_levels", 20))]
        bid_vol = sum(b[1] for b in bids if b and len(b) >= 2)
        ask_vol = sum(a[1] for a in asks if a and len(a) >= 2)
        if ask_vol <= 0 or bid_vol <= 0:
            return None
        ratio = bid_vol / ask_vol
        thr = float(self.params.get("bid_ask_ratio_threshold", 2.2))
        if ratio < thr and ratio > 1 / thr:
            return None
        side = "long" if ratio > 1 else "short"
        score = float(np.clip((abs(np.log(ratio)) / np.log(thr)), 0, 1))
        return StrategyHit(
            name=self.name,
            timeframe=ctx.get("timeframe", "?"),
            score=round(score, 4),
            weight=self.weight,
            detail={
                "bid_vol": round(bid_vol, 3),
                "ask_vol": round(ask_vol, 3),
                "ratio": round(ratio, 3),
                "side": side,
            },
        )


# ==========================================================
# 4) Liquidity grab (spring / upthrust)
# ==========================================================
class LiquidityGrab(BaseStrategy):
    name = SignalType.LIQUIDITY_GRAB.value
    default_weight = 1.0

    def evaluate(self, df: pd.DataFrame, ctx: Dict[str, Any]) -> Optional[StrategyHit]:
        if not self.enabled or len(df) < 10:
            return None
        lookback = int(self.params.get("swing_lookback", 50))
        wick_pct = float(self.params.get("wick_pierce_pct", 0.2))
        if len(df) < lookback + 1:
            lookback = len(df) - 1
        high_pre = df["high"].iloc[-(lookback + 1):-1]
        low_pre = df["low"].iloc[-(lookback + 1):-1]
        swing_h = high_pre.max()
        swing_l = low_pre.min()
        last = df.iloc[-1]
        # Bullish spring: wick pierces below swing low then closes inside (above).
        piercing_low = last.low < swing_l * (1 - wick_pct / 100.0)
        closed_back_low = last.close > swing_l
        # Bearish upthrust: pierces above swing high then closes back below.
        piercing_high = last.high > swing_h * (1 + wick_pct / 100.0)
        closed_back_high = last.close < swing_h
        if piercing_low and closed_back_low:
            wick = (swing_l - last.low) / swing_l * 100
            return StrategyHit(self.name, ctx.get("timeframe", "?"),
                               score=round(float(np.clip(0.5 + 0.05 * wick, 0, 1)), 4),
                               weight=self.weight,
                               detail={"type": "spring", "swing_low": swing_l, "wick_pct": round(wick, 3)})
        if piercing_high and closed_back_high:
            wick = (last.high - swing_h) / swing_h * 100
            return StrategyHit(self.name, ctx.get("timeframe", "?"),
                               score=round(float(np.clip(0.5 + 0.05 * wick, 0, 1)), 4),
                               weight=self.weight,
                               detail={"type": "upthrust", "swing_high": swing_h, "wick_pct": round(wick, 3)})
        return None


# ==========================================================
# 5) Momentum ignition
# ==========================================================
class MomentumIgnition(BaseStrategy):
    name = SignalType.MOMENTUM_IGNITION.value
    default_weight = 1.0

    def evaluate(self, df: pd.DataFrame, ctx: Dict[str, Any]) -> Optional[StrategyHit]:
        if not self.enabled or len(df) < 3:
            return None
        first_pct = float(self.params.get("first_body_min_pct", 4.0))
        confirm_pct = float(self.params.get("confirm_body_min_pct", 2.0))
        a, b = df.iloc[-2], df.iloc[-1]
        ba = 100 * (a.close - a.open) / (a.open or np.nan)
        bb = 100 * (b.close - b.open) / (b.open or np.nan)
        if abs(ba) < first_pct or abs(bb) < confirm_pct:
            return None
        if np.sign(ba) != np.sign(bb):
            return None
        side = "long" if ba > 0 else "short"
        score = float(np.clip(0.5 + 0.05 * (abs(ba) + abs(bb)), 0, 1))
        return StrategyHit(self.name, ctx.get("timeframe", "?"),
                           score=round(score, 4), weight=self.weight,
                           detail={"first_body_pct": round(float(ba), 3),
                                   "confirm_body_pct": round(float(bb), 3), "side": side})


# ==========================================================
# 6) RSI divergence (bullish/bearish)
# ==========================================================
class RSIDivergence(BaseStrategy):
    name = SignalType.RSI_DIVERGENCE.value
    default_weight = 0.8

    def evaluate(self, df: pd.DataFrame, ctx: Dict[str, Any]) -> Optional[StrategyHit]:
        if not self.enabled or len(df) < 30:
            return None
        length = int(self.params.get("rsi_length", 14))
        lookback = int(self.params.get("lookback", 50))
        rsi_series = ind.rsi(df["close"], length)
        if rsi_series.isna().all():
            return None
        n = min(len(df), lookback)
        price = df["close"].iloc[-n:]
        rsi = rsi_series.iloc[-n:].reset_index(drop=True)
        # locate last two swing lows/highs naively (argmin/argmax chunked)
        def _swings(series, find="low", chunks=4):
            window = len(series) // chunks if len(series) >= 8 else 2
            if window < 2:
                window = 2
            pts = []
            i = 0
            while i + window <= len(series):
                seg = series.iloc[i:i + window]
                if find == "low":
                    idx = seg.idxmin()
                else:
                    idx = seg.idxmax()
                pts.append((idx, series.iloc[idx]))
                i += window
            return pts

        # bullish divergence: price lower low, RSI higher low
        lows = _swings(price * -1, find="low")  # minima of price
        price_lows = [(i, -p) for i, p in lows]
        rsi_lows = _swings(rsi * -1, find="low")
        if len(price_lows) >= 2 and len(rsi_lows) >= 2:
            p1, p2 = price_lows[-2], price_lows[-1]
            r1, r2 = rsi_lows[-2], rsi_lows[-1]
            if p2[1] < p1[1] and r2[1] > r1[1]:
                # Dynamic score: stronger RSI divergence = higher score
                rsi_gap = abs(r2[1] - r1[1])
                price_gap_pct = abs(p2[1] - p1[1]) / max(p1[1], 1e-9) * 100
                dyn_score = min(0.85, 0.45 + rsi_gap * 0.02 + price_gap_pct * 0.01)
                return StrategyHit(self.name, ctx.get("timeframe", "?"),
                                   score=dyn_score, weight=self.weight,
                                   detail={"type": "bullish_div",
                                           "price_low1": round(float(p1[1]), 6),
                                           "price_low2": round(float(p2[1]), 6),
                                           "rsi1": round(float(r1[1]), 2),
                                           "rsi2": round(float(r2[1]), 2),
                                           "divergence_strength": round(rsi_gap, 2),
                                           "side": "long"})
        # bearish divergence: price higher high, RSI lower high
        price_highs = _swings(price, find="high")
        rsi_highs = _swings(rsi, find="high")
        if len(price_highs) >= 2 and len(rsi_highs) >= 2:
            p1, p2 = price_highs[-2], price_highs[-1]
            r1, r2 = rsi_highs[-2], rsi_highs[-1]
            if p2[1] > p1[1] and r2[1] < r1[1]:
                rsi_gap = abs(r2[1] - r1[1])
                price_gap_pct = abs(p2[1] - p1[1]) / max(p1[1], 1e-9) * 100
                dyn_score = min(0.85, 0.45 + rsi_gap * 0.02 + price_gap_pct * 0.01)
                return StrategyHit(self.name, ctx.get("timeframe", "?"),
                                   score=dyn_score, weight=self.weight,
                                   detail={"type": "bearish_div",
                                           "price_high1": round(float(p1[1]), 6),
                                           "price_high2": round(float(p2[1]), 6),
                                           "rsi1": round(float(r1[1]), 2),
                                           "rsi2": round(float(r2[1]), 2),
                                           "divergence_strength": round(rsi_gap, 2),
                                           "side": "short"})
        return None


# ==========================================================
# 7) Bollinger breakout
# ==========================================================
class BBBreakout(BaseStrategy):
    name = SignalType.BB_BREAKOUT.value
    default_weight = 0.8

    def evaluate(self, df: pd.DataFrame, ctx: Dict[str, Any]) -> Optional[StrategyHit]:
        if not self.enabled or len(df) < 25:
            return None
        length = int(self.params.get("bb_length", 20))
        k = float(self.params.get("bb_std", 2.0))
        buf = float(self.params.get("breakout_buffer_pct", 0.1))
        mid, upper, lower, _sd = ind.bollinger_bands(df["close"], length, k)
        last = df.iloc[-1]
        if last.close > upper.iloc[-1] * (1 + buf / 100):
            # Dynamic score: stronger breakout = higher score
            breakout_pct = (last.close - upper.iloc[-1]) / max(upper.iloc[-1], 1e-9) * 100
            dyn_score = min(0.90, 0.50 + breakout_pct * 0.08)
            return StrategyHit(self.name, ctx.get("timeframe", "?"),
                               score=dyn_score, weight=self.weight,
                               detail={"side": "long", "upper": round(float(upper.iloc[-1]), 6),
                                       "close": round(float(last.close), 6),
                                       "breakout_pct": round(breakout_pct, 2)})
        if last.close < lower.iloc[-1] * (1 - buf / 100):
            breakout_pct = (lower.iloc[-1] - last.close) / max(lower.iloc[-1], 1e-9) * 100
            dyn_score = min(0.90, 0.50 + breakout_pct * 0.08)
            return StrategyHit(self.name, ctx.get("timeframe", "?"),
                               score=dyn_score, weight=self.weight,
                               detail={"side": "short", "lower": round(float(lower.iloc[-1]), 6),
                                       "close": round(float(last.close), 6),
                                       "breakout_pct": round(breakout_pct, 2)})
        return None


# ==========================================================
# 8) Funding rate + open interest spike (futures flag)
# ==========================================================
class FundingOISpike(BaseStrategy):
    name = SignalType.FUNDING_OI_SPIKE.value
    default_weight = 1.0

    def evaluate(self, df: pd.DataFrame, ctx: Dict[str, Any]) -> Optional[StrategyHit]:
        if not self.enabled:
            return None
        oi_hist = ctx.get("open_interest_history")  # list[(ts, oi)]
        funding = ctx.get("funding_rate")
        if not oi_hist or funding is None:
            return None
        mult = float(self.params.get("oi_spike_multiplier", 1.5))
        thr = float(self.params.get("funding_change_threshold", 0.0005))
        if len(oi_hist) < 20:
            return None
        ois = [o for _, o in oi_hist]
        avg = sum(ois[:-1]) / max(1, len(ois) - 1)
        if avg <= 0:
            return None
        if ois[-1] < mult * avg:
            return None
        if abs(funding) < thr:
            return None
        # negative funding + OI spike -> potential squeeze (bullish bias)
        side = "long" if funding < 0 else "short"
        score = float(np.clip(0.55 + 0.1 * np.log(ois[-1] / avg), 0, 1))
        return StrategyHit(self.name, ctx.get("timeframe", "?"),
                           score=round(score, 4), weight=self.weight,
                           detail={"oi_now": ois[-1], "oi_avg": avg,
                                   "funding": round(float(funding), 6), "side": side})


# ==========================================================
# 9. Social Momentum — social hype detection
# ==========================================================
class SocialMomentum(BaseStrategy):
    """Signal based on social media hype (Twitter mentions, Telegram activity).

    Requires `ctx["social_score"]` to be a dict with keys:
      momentum_score (float 0..1), trend (str), twitter_mentions (int)
    """
    name = "social_momentum"
    signal_type = SignalType.SOCIAL_MOMENTUM if hasattr(SignalType, 'SOCIAL_MOMENTUM') else "social_momentum"

    def __init__(self, params: Dict[str, Any]) -> None:
        super().__init__(params)
        self.min_momentum = float(params.get("min_momentum", 0.4))
        self.min_mentions = int(params.get("min_mentions", 10))

    def evaluate(self, df: pd.DataFrame, ctx: dict) -> Optional[StrategyHit]:
        social = ctx.get("social_score")
        if not social or not isinstance(social, dict):
            return None
        momentum = float(social.get("momentum_score", 0))
        mentions = int(social.get("twitter_mentions", 0))
        trend = social.get("trend", "stable")
        if momentum < self.min_momentum or mentions < self.min_mentions:
            return None
        # Rising trend boosts score
        trend_mult = 1.2 if trend == "rising" else 1.0 if trend == "stable" else 0.7
        score = float(np.clip(momentum * trend_mult, 0, 1))
        if score < 0.3:
            return None
        # Social momentum is typically bullish
        side = "long"
        return StrategyHit(self.name, ctx.get("timeframe", "?"),
                           score=round(score, 4), weight=self.weight,
                           detail={"momentum": round(momentum, 4),
                                   "mentions": mentions, "trend": trend, "side": side})


# ==========================================================
# 10. EMA Cross — trend-following crossover
# ==========================================================
class EMACross(BaseStrategy):
    """EMA crossover signal — fast EMA crosses slow EMA."""
    name = "ema_cross"

    def __init__(self, params: Dict[str, Any]) -> None:
        super().__init__(params)
        self.fast_len = int(params.get("fast_length", 9))
        self.slow_len = int(params.get("slow_length", 21))

    def evaluate(self, df: pd.DataFrame, ctx: dict) -> Optional[StrategyHit]:
        if len(df) < self.slow_len + 5:
            return None
        close = df["close"].astype(float)
        fast = ind.ema_indicator(close, self.fast_len)
        slow = ind.ema_indicator(close, self.slow_len)
        cross = ind.ema_cross(fast, slow)
        last_cross = float(cross.iloc[-1])
        if last_cross == 0:
            return None
        # Score based on ADX trend strength
        adx_val = float(ind.adx_indicator(
            df["high"].astype(float), df["low"].astype(float), close, 14
        ).iloc[-1]) if len(df) > 20 else 30.0
        adx_boost = min(1.0, adx_val / 50.0)  # higher ADX = stronger trend
        score = float(np.clip(0.45 + 0.3 * adx_boost, 0, 1))
        side = "long" if last_cross > 0 else "short"
        return StrategyHit(self.name, ctx.get("timeframe", "?"),
                           score=round(score, 4), weight=self.weight,
                           detail={"fast_ema": round(float(fast.iloc[-1]), 6),
                                   "slow_ema": round(float(slow.iloc[-1]), 6),
                                   "adx": round(adx_val, 2), "side": side})


# ==========================================================
# 11. ADX Trend — strong trend detection
# ==========================================================
class ADXTrend(BaseStrategy):
    """Strong directional trend detection using ADX."""
    name = "adx_trend"

    def __init__(self, params: Dict[str, Any]) -> None:
        super().__init__(params)
        self.adx_threshold = float(params.get("adx_threshold", 25))
        self.length = int(params.get("adx_length", 14))

    def evaluate(self, df: pd.DataFrame, ctx: dict) -> Optional[StrategyHit]:
        if len(df) < self.length + 10:
            return None
        high = df["high"].astype(float)
        low = df["low"].astype(float)
        close = df["close"].astype(float)
        adx_val = float(ind.adx_indicator(high, low, close, self.length).iloc[-1])
        if not np.isfinite(adx_val) or adx_val < self.adx_threshold:
            return None
        # Determine direction from recent price action
        sma = close.rolling(10).mean()
        if close.iloc[-1] > sma.iloc[-1]:
            side = "long"
        else:
            side = "short"
        # Score: higher ADX = stronger trend = higher score
        score = float(np.clip(0.4 + 0.4 * ((adx_val - self.adx_threshold) / 50), 0, 1))
        return StrategyHit(self.name, ctx.get("timeframe", "?"),
                           score=round(score, 4), weight=self.weight,
                           detail={"adx": round(adx_val, 2), "threshold": self.adx_threshold, "side": side})


# ==========================================================
# 12. Squeeze — BB inside KC breakout
# ==========================================================
class SqueezeMomentum(BaseStrategy):
    """Squeeze momentum — volatility contraction before expansion."""
    name = "squeeze_momentum"

    def __init__(self, params: Dict[str, Any]) -> None:
        super().__init__(params)
        self.bb_length = int(params.get("bb_length", 20))
        self.kc_length = int(params.get("kc_length", 20))
        self.kc_mult = float(params.get("kc_mult", 1.5))

    def evaluate(self, df: pd.DataFrame, ctx: dict) -> Optional[StrategyHit]:
        if len(df) < max(self.bb_length, self.kc_length) + 10:
            return None
        high = df["high"].astype(float)
        low = df["low"].astype(float)
        close = df["close"].astype(float)
        squeeze = ind.squeeze_detector(high, low, close, self.bb_length, self.kc_length, self.kc_mult)
        # Check if squeeze just released (was squeezing, now expanding)
        if len(squeeze) < 3:
            return None
        was_squeezing = float(squeeze.iloc[-3]) == 1.0
        is_expanding = float(squeeze.iloc[-1]) == 0.0
        if not (was_squeezing and is_expanding):
            return None
        # Direction from recent candles
        recent_return = float((close.iloc[-1] - close.iloc[-3]) / close.iloc[-3])
        side = "long" if recent_return > 0 else "short"
        score = float(np.clip(0.55 + 0.2 * abs(recent_return) * 100, 0, 1))
        return StrategyHit(self.name, ctx.get("timeframe", "?"),
                           score=round(score, 4), weight=self.weight,
                           detail={"squeeze_released": True, "return_3c": round(recent_return * 100, 3), "side": side})


# ==========================================================
# 13. VWAP — Volume Weighted Average Price
# ==========================================================
class VWAPStrategy(BaseStrategy):
    """Price crossing VWAP with volume confirmation."""
    name = "vwap"

    def __init__(self, params: Dict[str, Any]) -> None:
        super().__init__(params)
        self.deviation_pct = float(params.get("deviation_pct", 0.5))

    def evaluate(self, df: pd.DataFrame, ctx: dict) -> Optional[StrategyHit]:
        if len(df) < 30:
            return None
        high = df["high"].astype(float)
        low = df["low"].astype(float)
        close = df["close"].astype(float)
        volume = df["volume"].astype(float)
        vwap_val = ind.vwap_indicator(high, low, close, volume)
        if vwap_val.iloc[-1] <= 0 or not np.isfinite(vwap_val.iloc[-1]):
            return None
        current = float(close.iloc[-1])
        vwap_now = float(vwap_val.iloc[-1])
        deviation = (current - vwap_now) / vwap_now * 100
        # Price above VWAP with volume surge = bullish
        vol_avg = volume.rolling(20).mean().iloc[-1]
        vol_ratio = float(volume.iloc[-1] / vol_avg) if vol_avg > 0 else 1.0
        if abs(deviation) < self.deviation_pct:
            return None
        side = "long" if deviation > 0 else "short"
        score = float(np.clip(0.5 + 0.2 * abs(deviation) + 0.15 * min(vol_ratio, 3) / 3, 0, 1))
        return StrategyHit(self.name, ctx.get("timeframe", "?"),
                           score=round(score, 4), weight=self.weight,
                           detail={"vwap": round(vwap_now, 6), "deviation_pct": round(deviation, 3),
                                   "vol_ratio": round(vol_ratio, 2), "side": side})


# ==========================================================
# 14. MACD Crossover — trend-following momentum
# ==========================================================
class MACDCrossover(BaseStrategy):
    """MACD line crosses signal line — strong trend confirmation."""
    name = "macd_crossover"

    def __init__(self, params: Dict[str, Any]) -> None:
        super().__init__(params)
        self.fast = int(params.get("fast", 12))
        self.slow = int(params.get("slow", 26))
        self.signal_len = int(params.get("signal", 9))

    def evaluate(self, df: pd.DataFrame, ctx: dict) -> Optional[StrategyHit]:
        if len(df) < self.slow + self.signal_len + 5:
            return None
        close = df["close"].astype(float)
        macd_line, signal_line, hist = ind.macd(close, self.fast, self.slow, self.signal_len)
        if len(hist) < 3:
            return None
        # Bullish crossover: hist goes from negative to positive
        prev_hist = float(hist.iloc[-2])
        curr_hist = float(hist.iloc[-1])
        if prev_hist <= 0 and curr_hist > 0:
            # Strength based on histogram magnitude
            avg_vol = df["volume"].astype(float).mean()
            vol_ratio = float(df["volume"].iloc[-1]) / max(avg_vol, 1)
            strength = min(1.0, abs(curr_hist) / (close.iloc[-1] * 0.001 + 1e-10))
            score = float(np.clip(0.5 + 0.2 * strength + 0.1 * min(vol_ratio, 3) / 3, 0, 1))
            return StrategyHit(self.name, ctx.get("timeframe", "?"),
                               score=round(score, 4), weight=self.weight,
                               detail={"side": "long", "histogram": round(curr_hist, 8),
                                       "macd": round(float(macd_line.iloc[-1]), 8),
                                       "signal": round(float(signal_line.iloc[-1]), 8)})
        # Bearish crossover
        if prev_hist >= 0 and curr_hist < 0:
            avg_vol = df["volume"].astype(float).mean()
            vol_ratio = float(df["volume"].iloc[-1]) / max(avg_vol, 1)
            strength = min(1.0, abs(curr_hist) / (close.iloc[-1] * 0.001 + 1e-10))
            score = float(np.clip(0.5 + 0.2 * strength + 0.1 * min(vol_ratio, 3) / 3, 0, 1))
            return StrategyHit(self.name, ctx.get("timeframe", "?"),
                               score=round(score, 4), weight=self.weight,
                               detail={"side": "short", "histogram": round(curr_hist, 8),
                                       "macd": round(float(macd_line.iloc[-1]), 8),
                                       "signal": round(float(signal_line.iloc[-1]), 8)})
        return None


# ==========================================================
# 15. Stochastic RSI — overbought/oversold reversal
# ==========================================================
class StochasticRSI(BaseStrategy):
    """Stochastic RSI crossing from oversold/overbought zones."""
    name = "stoch_rsi"

    def __init__(self, params: Dict[str, Any]) -> None:
        super().__init__(params)
        self.rsi_length = int(params.get("rsi_length", 14))
        self.stoch_length = int(params.get("stoch_length", 14))
        self.oversold = float(params.get("oversold", 20))
        self.overbought = float(params.get("overbought", 80))

    def evaluate(self, df: pd.DataFrame, ctx: dict) -> Optional[StrategyHit]:
        if len(df) < self.rsi_length + self.stoch_length + 10:
            return None
        close = df["close"].astype(float)
        k, d = ind.stochastic_rsi(close, self.rsi_length, self.stoch_length)
        if k.isna().all() or d.isna().all():
            return None
        k_now, k_prev = float(k.iloc[-1]), float(k.iloc[-2])
        d_now, d_prev = float(d.iloc[-1]), float(d.iloc[-2])
        # Bullish: K crosses above D from oversold zone
        if k_prev <= d_prev and k_now > d_now and k_now < 30:
            score = float(np.clip(0.45 + 0.2 * ((30 - k_now) / 30), 0, 1))
            return StrategyHit(self.name, ctx.get("timeframe", "?"),
                               score=round(score, 4), weight=self.weight,
                               detail={"side": "long", "k": round(k_now, 2),
                                       "d": round(d_now, 2), "zone": "oversold"})
        # Bearish: K crosses below D from overbought zone
        if k_prev >= d_prev and k_now < d_now and k_now > 70:
            score = float(np.clip(0.45 + 0.2 * ((k_now - 70) / 30), 0, 1))
            return StrategyHit(self.name, ctx.get("timeframe", "?"),
                               score=round(score, 4), weight=self.weight,
                               detail={"side": "short", "k": round(k_now, 2),
                                       "d": round(d_now, 2), "zone": "overbought"})
        return None


# ==========================================================
# 16. OBV Divergence — volume confirms price
# ==========================================================
class OBVDivergence(BaseStrategy):
    """On-Balance Volume trend confirms or diverges from price."""
    name = "obv_divergence"

    def __init__(self, params: Dict[str, Any]) -> None:
        super().__init__(params)
        self.length = int(params.get("length", 20))

    def evaluate(self, df: pd.DataFrame, ctx: dict) -> Optional[StrategyHit]:
        if len(df) < self.length + 10:
            return None
        close = df["close"].astype(float)
        volume = df["volume"].astype(float)
        obv_slope = ind.obv_trend(close, volume, self.length)
        if obv_slope.isna().all():
            return None
        slope_now = float(obv_slope.iloc[-1])
        price_change = (close.iloc[-1] - close.iloc[-5]) / close.iloc[-5] * 100 if len(close) > 5 else 0
        # Bullish divergence: price falling but OBV rising (accumulation)
        if slope_now > 0.05 and price_change < -1:
            score = float(np.clip(0.5 + 0.3 * slope_now, 0, 1))
            return StrategyHit(self.name, ctx.get("timeframe", "?"),
                               score=round(score, 4), weight=self.weight,
                               detail={"side": "long", "obv_slope": round(slope_now, 4),
                                       "price_change_pct": round(price_change, 2)})
        # Bearish divergence: price rising but OBV falling (distribution)
        if slope_now < -0.05 and price_change > 1:
            score = float(np.clip(0.5 + 0.3 * abs(slope_now), 0, 1))
            return StrategyHit(self.name, ctx.get("timeframe", "?"),
                               score=round(score, 4), weight=self.weight,
                               detail={"side": "short", "obv_slope": round(slope_now, 4),
                                       "price_change_pct": round(price_change, 2)})
        return None


# ==========================================================
# 17. Support/Resistance Bounce
# ==========================================================
class SupportResistanceBounce(BaseStrategy):
    """Price bouncing off key support/resistance levels."""
    name = "sr_bounce"

    def __init__(self, params: Dict[str, Any]) -> None:
        super().__init__(params)
        self.lookback = int(params.get("lookback", 30))
        self.threshold_pct = float(params.get("threshold_pct", 1.5))

    def evaluate(self, df: pd.DataFrame, ctx: dict) -> Optional[StrategyHit]:
        if len(df) < self.lookback + 5:
            return None
        high = df["high"].astype(float)
        low = df["low"].astype(float)
        close = df["close"].astype(float)
        supports, resistances = ind.support_resistance(high, low, close, self.lookback)
        current = float(close.iloc[-1])
        prev = float(close.iloc[-2])
        levels = ind.price_near_level(current, supports, resistances, self.threshold_pct)
        # Bullish bounce: near support, price bouncing up
        if levels["near_support"] and current > prev:
            dist = levels["support_dist_pct"]
            score = float(np.clip(0.55 + 0.2 * (self.threshold_pct - dist) / self.threshold_pct, 0, 1))
            return StrategyHit(self.name, ctx.get("timeframe", "?"),
                               score=round(score, 4), weight=self.weight,
                               detail={"side": "long", "level": "support",
                                       "dist_pct": round(dist, 2)})
        # Bearish rejection: near resistance, price rejecting down
        if levels["near_resistance"] and current < prev:
            dist = levels["resistance_dist_pct"]
            score = float(np.clip(0.55 + 0.2 * (self.threshold_pct - dist) / self.threshold_pct, 0, 1))
            return StrategyHit(self.name, ctx.get("timeframe", "?"),
                               score=round(score, 4), weight=self.weight,
                               detail={"side": "short", "level": "resistance",
                                       "dist_pct": round(dist, 2)})
        return None


# ==========================================================
# 18. Volume Trend — sustained volume increase
# ==========================================================
class VolumeTrend(BaseStrategy):
    """Sustained volume increase confirms trend."""
    name = "volume_trend"

    def __init__(self, params: Dict[str, Any]) -> None:
        super().__init__(params)
        self.short_window = int(params.get("short_window", 5))
        self.long_window = int(params.get("long_window", 20))
        self.min_ratio = float(params.get("min_ratio", 1.5))

    def evaluate(self, df: pd.DataFrame, ctx: dict) -> Optional[StrategyHit]:
        if len(df) < self.long_window + 5:
            return None
        volume = df["volume"].astype(float)
        close = df["close"].astype(float)
        vt = ind.volume_trend(volume, self.short_window, self.long_window)
        if vt.isna().all():
            return None
        ratio = float(vt.iloc[-1])
        if ratio < self.min_ratio:
            return None
        # Direction from recent price
        recent_return = float((close.iloc[-1] - close.iloc[-3]) / close.iloc[-3]) if len(close) > 3 else 0
        side = "long" if recent_return > 0 else "short"
        score = float(np.clip(0.45 + 0.2 * min(ratio / 3, 1) + 0.1 * min(abs(recent_return) * 50, 1), 0, 1))
        return StrategyHit(self.name, ctx.get("timeframe", "?"),
                           score=round(score, 4), weight=self.weight,
                           detail={"side": side, "vol_ratio": round(ratio, 2),
                                   "recent_return_pct": round(recent_return * 100, 2)})


# ==========================================================
# Registry
# ==========================================================
# ==========================================================
# 19) News Sentiment — اخبار و تحلیل بنیادی
# ==========================================================
class NewsSentiment(BaseStrategy):
    """Signal based on market sentiment (Fear & Greed, BTC trend, CoinGecko trending).

    Requires `ctx["fundamental_score"]` to be a dict with keys:
      fear_greed_value (int 0-100), overall_sentiment (float -1..1),
      trending_coins (list[str]), btc_24h_change (float)
    """
    name = "news_sentiment"
    signal_type = SignalType.NEWS_SENTIMENT if hasattr(SignalType, 'NEWS_SENTIMENT') else "news_sentiment"
    default_weight = 1.2

    def evaluate(self, df: pd.DataFrame, ctx: dict) -> Optional[StrategyHit]:
        if not self.enabled:
            return None
        fund = ctx.get("fundamental_score")
        if not fund or not isinstance(fund, dict):
            return None

        fg_value = int(fund.get("fear_greed_value", 50))
        fg_label = fund.get("fear_greed_label", "Neutral")
        fg_change = int(fund.get("fear_greed_change", 0))
        sentiment = float(fund.get("overall_sentiment", 0.0))
        btc_change = float(fund.get("btc_24h_change", 0.0))
        trending_coins = fund.get("trending_coins", [])

        signals = []
        score_parts = []

        # Contrarian signal: Extreme Fear = buy opportunity
        if fg_value <= 20:
            signals.append(f"🔥 ترس شدید ({fg_value}) — فرصت خرید")
            score_parts.append(0.30)
        elif fg_value <= 35:
            signals.append(f"😰 ترس ({fg_value})")
            score_parts.append(0.15)
        elif fg_value >= 80:
            signals.append(f"⚠️ طمع شدید ({fg_value}) — احتیاط")
            score_parts.append(-0.20)
        elif fg_value >= 65:
            signals.append(f"🤑 طمع ({fg_value})")
            score_parts.append(-0.10)

        # Fear & Greed direction change
        if fg_change >= 10:
            signals.append(f"📈 رشد اعتماد ({fg_change:+d})")
            score_parts.append(0.10)
        elif fg_change <= -10:
            signals.append(f"📉 افت اعتماد ({fg_change:+d})")
            score_parts.append(-0.05)

        # BTC trend
        if btc_change > 3:
            signals.append(f"🚀 BTC صعودی ({btc_change:+.1f}%)")
            score_parts.append(0.15)
        elif btc_change > 1:
            signals.append(f"📈 BTC مثبت ({btc_change:+.1f}%)")
            score_parts.append(0.08)
        elif btc_change < -3:
            signals.append(f"🔻 BTC نزولی ({btc_change:+.1f}%)")
            score_parts.append(-0.15)
        elif btc_change < -1:
            signals.append(f"📉 BTC منفی ({btc_change:+.1f}%)")
            score_parts.append(-0.08)

        if not signals:
            return None

        raw = sum(score_parts)
        score = float(np.clip(raw, 0, 1))
        if score < 0.1:
            return None

        # Determine side from sentiment
        side = "long" if sentiment >= 0 else "short"

        return StrategyHit(self.name, ctx.get("timeframe", "?"),
                           score=round(score, 4), weight=self.weight,
                           detail={"side": side,
                                   "fear_greed": fg_value,
                                   "fear_greed_label": fg_label,
                                   "btc_change": round(btc_change, 2),
                                   "sentiment": round(sentiment, 3),
                                   "trending_count": len(trending_coins)})


STRATEGY_REGISTRY = {
    SignalType.NEW_LISTING.value: NewListingSniper,
    SignalType.VOLUME_SPIKE.value: VolumeSpike,
    SignalType.ORDERBOOK_IMBALANCE.value: OrderBookImbalance,
    SignalType.LIQUIDITY_GRAB.value: LiquidityGrab,
    SignalType.MOMENTUM_IGNITION.value: MomentumIgnition,
    SignalType.RSI_DIVERGENCE.value: RSIDivergence,
    SignalType.BB_BREAKOUT.value: BBBreakout,
    SignalType.FUNDING_OI_SPIKE.value: FundingOISpike,
    "social_momentum": SocialMomentum,
    "ema_cross": EMACross,
    "adx_trend": ADXTrend,
    "squeeze_momentum": SqueezeMomentum,
    "vwap": VWAPStrategy,
    "macd_crossover": MACDCrossover,
    "stoch_rsi": StochasticRSI,
    "obv_divergence": OBVDivergence,
    "sr_bounce": SupportResistanceBounce,
    "volume_trend": VolumeTrend,
    "news_sentiment": NewsSentiment,
}


def build_strategies(strategy_params: Dict[str, Dict[str, Any]]) -> List[BaseStrategy]:
    """Instantiate all enabled strategies from config."""
    out: List[BaseStrategy] = []
    for name, cls in STRATEGY_REGISTRY.items():
        params = strategy_params.get(name, {}) or {}
        instance = cls(params)
        if instance.enabled:
            out.append(instance)
    return out
