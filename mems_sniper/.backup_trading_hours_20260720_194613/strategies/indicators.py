"""Technical-indicator helpers computed with pandas/numpy only.

We avoid heavy pandas_ta mandatory dependency at init-time; these are
fast vectorized implementations of the few indicators our strategies
actually need. If pandas_ta is available we still offer passthrough.
"""
from __future__ import annotations

import numpy as np
import pandas as pd


def rsi(close: pd.Series, length: int = 14) -> pd.Series:
    delta = close.diff()
    gain = delta.clip(lower=0)
    loss = -delta.clip(upper=0)
    avg_gain = gain.ewm(alpha=1 / length, adjust=False).mean()
    avg_loss = loss.ewm(alpha=1 / length, adjust=False).mean()
    rs = avg_gain / avg_loss.replace(0, np.nan)
    return (100 - 100 / (1 + rs))


def mfi(high: pd.Series, low: pd.Series, close: pd.Series, volume: pd.Series,
        length: int = 14) -> pd.Series:
    tp = (high + low + close) / 3.0
    rmf = tp * volume
    up = tp.diff().clip(lower=0)
    down = -tp.diff().clip(upper=0)
    mr_up = up.rolling(length).sum()
    mr_down = down.rolling(length).sum()
    mfr = (mr_up / mr_down.replace(0, np.nan))
    return 100 - 100 / (1 + mfr)


def bollinger_bands(close: pd.Series, length: int = 20, std: float = 2.0):
    mid = close.rolling(length).mean()
    sd = close.rolling(length).std(ddof=0)
    upper = mid + std * sd
    lower = mid - std * sd
    return mid, upper, lower, sd


def atr(high: pd.Series, low: pd.Series, close: pd.Series, length: int = 14) -> pd.Series:
    prev_close = close.shift(1)
    tr = pd.concat(
        [(high - low),
         (high - prev_close).abs(),
         (low - prev_close).abs()],
        axis=1,
    ).max(axis=1)
    # Wilder smoothing
    return tr.ewm(alpha=1 / length, adjust=False).mean()


def rolling_mean(series: pd.Series, window: int) -> pd.Series:
    return series.rolling(window).mean()


def rolling_std(series: pd.Series, window: int) -> pd.Series:
    return series.rolling(window).std(ddof=0)


def zscore(series: pd.Series, window: int) -> pd.Series:
    mu = series.rolling(window).mean()
    sd = series.rolling(window).std(ddof=0)
    return (series - mu) / sd.replace(0, np.nan)


def swing_high(high: pd.Series, lookback: int) -> pd.Series:
    return high.rolling(lookback, min_periods=1).max().shift(1)


def swing_low(low: pd.Series, lookback: int) -> pd.Series:
    return low.rolling(lookback, min_periods=1).min().shift(1)


def body_pct(close: pd.Series, open_: pd.Series) -> pd.Series:
    rng = (close - open_).abs()
    denom = open_.replace(0, np.nan)
    return 100.0 * (close - open_) / denom.abs() * np.sign(close - open_)


def true_body_pct(open_: pd.Series, high: pd.Series, low: pd.Series, close: pd.Series) -> pd.Series:
    """Body size as % of candle range."""
    body = (close - open_).abs()
    rng = (high - low).replace(0, np.nan)
    return 100.0 * body / rng


# ============================================================
# Professional indicators — VWAP, EMA, ADX, OBV
# ============================================================

def ema(close: pd.Series, length: int = 21) -> pd.Series:
    """Exponential Moving Average."""
    return close.ewm(span=length, adjust=False).mean()


def ema_cross(fast: pd.Series, slow: pd.Series) -> pd.Series:
    """EMA crossover signal: 1 = bullish cross, -1 = bearish cross, 0 = no cross."""
    prev_diff = (fast.shift(1) - slow.shift(1))
    curr_diff = (fast - slow)
    cross = pd.Series(0, index=fast.index)
    cross[(prev_diff <= 0) & (curr_diff > 0)] = 1
    cross[(prev_diff >= 0) & (curr_diff < 0)] = -1
    return cross


def adx(high: pd.Series, low: pd.Series, close: pd.Series, length: int = 14) -> pd.Series:
    """Average Directional Index — trend strength (0..100)."""
    plus_dm = high.diff()
    minus_dm = -low.diff()
    plus_dm = plus_dm.where((plus_dm > minus_dm) & (plus_dm > 0), 0)
    minus_dm = minus_dm.where((minus_dm > plus_dm) & (minus_dm > 0), 0)

    tr = pd.concat([
        high - low,
        (high - close.shift(1)).abs(),
        (low - close.shift(1)).abs(),
    ], axis=1).max(axis=1)

    atr_val = tr.ewm(alpha=1 / length, adjust=False).mean()
    plus_di = 100 * plus_dm.ewm(alpha=1 / length, adjust=False).mean() / atr_val
    minus_di = 100 * minus_dm.ewm(alpha=1 / length, adjust=False).mean() / atr_val
    dx = 100 * (plus_di - minus_di).abs() / (plus_di + minus_di).replace(0, np.nan)
    return dx.ewm(alpha=1 / length, adjust=False).mean()


def obv(close: pd.Series, volume: pd.Series) -> pd.Series:
    """On-Balance Volume — momentum indicator."""
    direction = np.sign(close.diff())
    return (volume * direction).cumsum()


def vwap(high: pd.Series, low: pd.Series, close: pd.Series, volume: pd.Series) -> pd.Series:
    """Volume Weighted Average Price (session-based approximation)."""
    tp = (high + low + close) / 3.0
    cum_tp_vol = (tp * volume).cumsum()
    cum_vol = volume.cumsum()
    return cum_tp_vol / cum_vol.replace(0, np.nan)


def volume_profile(high: pd.Series, low: pd.Series, close: pd.Series,
                   volume: pd.Series, bins: int = 20) -> pd.Series:
    """Volume at Price — returns relative volume at current price level (0..1)."""
    price_min = low.min()
    price_max = high.max()
    if price_max <= price_min:
        return pd.Series(0.5, index=close.index)
    bin_size = (price_max - price_min) / bins
    # Bin the close prices
    bin_idx = ((close - price_min) / bin_size).clip(0, bins - 1).astype(int)
    # Sum volume per bin
    vol_per_bin = volume.groupby(bin_idx).sum()
    total_vol = vol_per_bin.sum()
    if total_vol <= 0:
        return pd.Series(0.5, index=close.index)
    # Map back to series
    return bin_idx.map(vol_per_bin / total_vol).fillna(0.5)


def squeeze_detector(high: pd.Series, low: pd.Series, close: pd.Series,
                     bb_length: int = 20, kc_length: int = 20, kc_mult: float = 1.5) -> pd.Series:
    """Squeeze Momentum — BB inside KC = squeeze (1), BB outside = expansion (0)."""
    mid, upper, lower, _ = bollinger_bands(close, bb_length, 2.0)
    atr_val = atr(high, low, close, kc_length)
    kc_upper = ema(close, kc_length) + kc_mult * atr_val
    kc_lower = ema(close, kc_length) - kc_mult * atr_val
    squeeze = (lower > kc_lower) & (upper < kc_upper)
    return squeeze.astype(float)


# ============================================================
# NEW: MACD, Stochastic RSI, Support/Resistance, Volume Trend
# ============================================================

def macd(close: pd.Series, fast: int = 12, slow: int = 26, signal: int = 9):
    """MACD indicator. Returns (macd_line, signal_line, histogram)."""
    fast_ema = close.ewm(span=fast, adjust=False).mean()
    slow_ema = close.ewm(span=slow, adjust=False).mean()
    macd_line = fast_ema - slow_ema
    signal_line = macd_line.ewm(span=signal, adjust=False).mean()
    histogram = macd_line - signal_line
    return macd_line, signal_line, histogram


def stochastic_rsi(close: pd.Series, rsi_length: int = 14, stoch_length: int = 14,
                   k_smooth: int = 3, d_smooth: int = 3):
    """Stochastic RSI. Returns (%K, %D)."""
    rsi_val = rsi(close, rsi_length)
    rsi_min = rsi_val.rolling(stoch_length).min()
    rsi_max = rsi_val.rolling(stoch_length).max()
    stoch_rsi = (rsi_val - rsi_min) / (rsi_max - rsi_min).replace(0, np.nan)
    k = stoch_rsi.rolling(k_smooth).mean() * 100
    d = k.rolling(d_smooth).mean()
    return k, d


def obv_trend(close: pd.Series, volume: pd.Series, length: int = 20) -> pd.Series:
    """OBV slope — positive = accumulation, negative = distribution."""
    obv_val = obv(close, volume)
    obv_sma = obv_val.rolling(length).mean()
    return (obv_val - obv_sma) / obv_sma.abs().replace(0, np.nan)


def volume_trend(volume: pd.Series, short_window: int = 5, long_window: int = 20) -> pd.Series:
    """Volume trend — ratio of short-term avg to long-term avg.
    > 1 = increasing volume, < 1 = decreasing."""
    short_avg = volume.rolling(short_window).mean()
    long_avg = volume.rolling(long_window).mean()
    return short_avg / long_avg.replace(0, np.nan)


def support_resistance(high: pd.Series, low: pd.Series, close: pd.Series,
                       lookback: int = 20, num_levels: int = 3):
    """Simple support/resistance levels from recent swing highs/lows.
    Returns (support_levels, resistance_levels) as lists of floats."""
    h = high.iloc[-lookback:]
    l = low.iloc[-lookback:]
    c = close.iloc[-1]

    # Find swing points
    supports = []
    resistances = []
    for i in range(2, len(h) - 2):
        # Swing low = support
        if l.iloc[i] <= l.iloc[i-1] and l.iloc[i] <= l.iloc[i-2] and \
           l.iloc[i] <= l.iloc[i+1] and l.iloc[i] <= l.iloc[i+2]:
            supports.append(l.iloc[i])
        # Swing high = resistance
        if h.iloc[i] >= h.iloc[i-1] and h.iloc[i] >= h.iloc[i-2] and \
           h.iloc[i] >= h.iloc[i+1] and h.iloc[i] >= h.iloc[i+2]:
            resistances.append(h.iloc[i])

    # Cluster nearby levels
    supports = _cluster_levels(supports, c, num_levels)
    resistances = _cluster_levels(resistances, c, num_levels, below=False)
    return supports, resistances


def _cluster_levels(levels, current_price, num=3, below=True):
    """Cluster nearby price levels and return the most significant ones."""
    if not levels:
        return []
    if below:
        levels = [l for l in levels if l < current_price]
    else:
        levels = [l for l in levels if l > current_price]
    if not levels:
        return []
    # Sort by distance from current price
    levels.sort(key=lambda l: abs(l - current_price))
    return levels[:num]


def price_near_level(close: float, supports: list, resistances: list,
                     threshold_pct: float = 2.0) -> dict:
    """Check if price is near a support or resistance level.
    Returns {near_support: bool, near_resistance: bool, support_dist: float, resistance_dist: float}"""
    result = {"near_support": False, "near_resistance": False,
              "support_dist_pct": 100.0, "resistance_dist_pct": 100.0}
    if supports:
        closest_sup = min(supports, key=lambda s: abs(close - s))
        result["support_dist_pct"] = abs(close - closest_sup) / close * 100
        result["near_support"] = result["support_dist_pct"] < threshold_pct
    if resistances:
        closest_res = min(resistances, key=lambda r: abs(close - r))
        result["resistance_dist_pct"] = abs(close - closest_res) / close * 100
        result["near_resistance"] = result["resistance_dist_pct"] < threshold_pct
    return result


def higher_tf_trend(close: pd.Series, length: int = 50) -> str:
    """Determine higher timeframe trend using 50-period SMA.
    Returns 'bullish', 'bearish', or 'neutral'."""
    if len(close) < length:
        return "neutral"
    sma = close.rolling(length).mean()
    current = float(close.iloc[-1])
    sma_val = float(sma.iloc[-1])
    if np.isnan(sma_val):
        return "neutral"
    pct_diff = (current - sma_val) / sma_val * 100
    if pct_diff > 2:
        return "bullish"
    elif pct_diff < -2:
        return "bearish"
    return "neutral"


# ============================================================
# Aliases for backward compatibility
# ============================================================
ema_indicator = ema
adx_indicator = adx
vwap_indicator = vwap
