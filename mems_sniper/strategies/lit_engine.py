"""LIT (Liquidity Inducement Theorem) Strategy Engine.

Implements core ICT/LIT concepts for intraday trading:
  1. Liquidity Sweep Detection (equal highs/lows, session highs/lows)
  2. Fair Value Gap (FVG) identification and trading
  3. Order Block (OB) detection and retest entries
  4. Power of Three (AMD): Accumulation → Manipulation → Distribution
  5. Market Structure Shift (BOS/CHoCH) confirmation
  6. Vector Candle detection (aggressive displacement)

Designed for top 20 crypto + gold (PAXG) + oil + Dow Jones.
Focus on educational display with exact TP/SL placement.
"""
from __future__ import annotations

import time
import uuid
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

import numpy as np
import pandas as pd

from core.logging_setup import logger
from core.models import Signal, Side, StrategyHit, now_sec


# ─── Data Structures ─────────────────────────────────────────

@dataclass
class LiquidityLevel:
    """A significant high/low where liquidity rests."""
    price: float
    kind: str          # "equal_high" | "equal_low" | "session_high" | "session_low" | "swing_high" | "swing_low"
    strength: int      # number of touches
    swept: bool = False
    swept_at: Optional[float] = None
    sweep_wick: Optional[float] = None


@dataclass
class FairValueGap:
    """A 3-candle price imbalance zone."""
    top: float
    bottom: float
    direction: str     # "bullish" | "bearish"
    timestamp: float
    filled: bool = False
    fill_pct: float = 0.0
    mitigation_candle: Optional[int] = None


@dataclass
class OrderBlock:
    """Institutional order block zone."""
    top: float
    bottom: float
    direction: str     # "bullish" | "bearish"
    timestamp: float
    displacement_size: float  # size of the impulse candle
    tested: bool = False
    test_count: int = 0
    mitigated: bool = False


@dataclass
class LITSignal:
    """A complete LIT trading signal with educational data."""
    id: str
    symbol: str
    exchange: str
    side: str           # "long" | "short"
    entry: float
    stop_loss: float
    take_profit: float
    take_profit_2: float
    score: float
    strategy: str       # "liquidity_sweep" | "fvg" | "order_block" | "power_of_three" | "vector_candle"
    timestamp: float
    # Educational data
    reasoning: str      # human-readable explanation in Persian
    zones: List[dict] = field(default_factory=list)  # zones to draw on chart
    liquidity_levels: List[dict] = field(default_factory=list)
    fvg_zones: List[dict] = field(default_factory=list)
    order_blocks: List[dict] = field(default_factory=list)
    # Backtest data
    actual_entry_time: Optional[float] = None
    actual_exit_time: Optional[float] = None
    actual_exit_price: Optional[float] = None
    actual_pnl_pct: Optional[float] = None
    actual_pnl_usdt: Optional[float] = None
    exit_reason: Optional[str] = None
    hit_tp: bool = False
    hit_sl: bool = False

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "symbol": self.symbol,
            "exchange": self.exchange,
            "side": self.side,
            "entry": self.entry,
            "stop_loss": self.stop_loss,
            "take_profit": self.take_profit,
            "take_profit_2": self.take_profit_2,
            "score": self.score,
            "strategy": self.strategy,
            "timestamp": self.timestamp,
            "reasoning": self.reasoning,
            "zones": self.zones,
            "liquidity_levels": self.liquidity_levels,
            "fvg_zones": self.fvg_zones,
            "order_blocks": self.order_blocks,
            "actual_entry_time": self.actual_entry_time,
            "actual_exit_time": self.actual_exit_time,
            "actual_exit_price": self.actual_exit_price,
            "actual_pnl_pct": self.actual_pnl_pct,
            "actual_pnl_usdt": self.actual_pnl_usdt,
            "exit_reason": self.exit_reason,
            "hit_tp": self.hit_tp,
            "hit_sl": self.hit_sl,
        }


# ─── Core Detection Functions ────────────────────────────────

class LiquidityDetector:
    """Detects liquidity levels (equal highs/lows, session extremes)."""

    @staticmethod
    def find_equal_highs(highs: np.ndarray, tolerance_pct: float = 0.1) -> List[LiquidityLevel]:
        """Find equal highs (buy-side liquidity)."""
        levels = []
        n = len(highs)
        for i in range(n):
            touches = 1
            for j in range(i + 1, n):
                if abs(highs[i] - highs[j]) / max(highs[i], 1e-10) < tolerance_pct / 100:
                    touches += 1
            if touches >= 2:
                levels.append(LiquidityLevel(
                    price=float(highs[i]),
                    kind="equal_high",
                    strength=touches,
                ))
        # Deduplicate nearby levels
        return LiquidityDetector._dedupe(levels)

    @staticmethod
    def find_equal_lows(lows: np.ndarray, tolerance_pct: float = 0.1) -> List[LiquidityLevel]:
        """Find equal lows (sell-side liquidity)."""
        levels = []
        n = len(lows)
        for i in range(n):
            touches = 1
            for j in range(i + 1, n):
                if abs(lows[i] - lows[j]) / max(lows[i], 1e-10) < tolerance_pct / 100:
                    touches += 1
            if touches >= 2:
                levels.append(LiquidityLevel(
                    price=float(lows[i]),
                    kind="equal_low",
                    strength=touches,
                ))
        return LiquidityDetector._dedupe(levels)

    @staticmethod
    def find_swing_highs(highs: np.ndarray, lookback: int = 5) -> List[LiquidityLevel]:
        """Find swing highs."""
        levels = []
        for i in range(lookback, len(highs) - lookback):
            if highs[i] == max(highs[i - lookback:i + lookback + 1]):
                levels.append(LiquidityLevel(
                    price=float(highs[i]),
                    kind="swing_high",
                    strength=1,
                ))
        return LiquidityDetector._dedupe(levels)

    @staticmethod
    def find_swing_lows(lows: np.ndarray, lookback: int = 5) -> List[LiquidityLevel]:
        """Find swing lows."""
        levels = []
        for i in range(lookback, len(lows) - lookback):
            if lows[i] == min(lows[i - lookback:i + lookback + 1]):
                levels.append(LiquidityLevel(
                    price=float(lows[i]),
                    kind="swing_low",
                    strength=1,
                ))
        return LiquidityDetector._dedupe(levels)

    @staticmethod
    def _dedupe(levels: List[LiquidityLevel], tolerance_pct: float = 0.15) -> List[LiquidityLevel]:
        """Remove duplicate levels within tolerance."""
        if not levels:
            return []
        sorted_lvls = sorted(levels, key=lambda l: l.price)
        result = [sorted_lvls[0]]
        for lvl in sorted_lvls[1:]:
            prev = result[-1]
            if abs(lvl.price - prev.price) / max(prev.price, 1e-10) < tolerance_pct / 100:
                if lvl.strength > prev.strength:
                    result[-1] = lvl
            else:
                result.append(lvl)
        return result

    @staticmethod
    def detect_sweep(
        current_high: float,
        current_low: float,
        current_close: float,
        level: LiquidityLevel,
    ) -> Optional[str]:
        """Detect if a liquidity level has been swept.
        Returns 'sweep_high' or 'sweep_low' or None.
        """
        wick_above = current_high - level.price
        wick_below = level.price - current_low
        avg_range = (current_high - current_low) if current_high > current_low else 1e-10

        if level.kind in ("equal_high", "swing_high", "session_high"):
            # Buy-side liquidity sweep: wick above but close below
            if wick_above > 0 and wick_above / avg_range > 0.3 and current_close < level.price:
                return "sweep_high"

        if level.kind in ("equal_low", "swing_low", "session_low"):
            # Sell-side liquidity sweep: wick below but close above
            if wick_below > 0 and wick_below / avg_range > 0.3 and current_close > level.price:
                return "sweep_low"

        return None


class FVGDetector:
    """Detects Fair Value Gaps (3-candle imbalances)."""

    @staticmethod
    def find_fvgs(
        opens: np.ndarray,
        highs: np.ndarray,
        lows: np.ndarray,
        closes: np.ndarray,
        min_gap_pct: float = 0.05,
    ) -> List[FairValueGap]:
        """Find all FVGs in the data.
        
        Bullish FVG: candle3.low > candle1.high (gap between wicks going up)
        Bearish FVG: candle1.low > candle3.high (gap between wicks going down)
        """
        fvgs = []
        for i in range(2, len(opens)):
            c1_high = highs[i - 2]
            c1_low = lows[i - 2]
            c2_high = highs[i - 1]
            c2_low = lows[i - 1]
            c3_high = highs[i]
            c3_low = lows[i]
            mid_price = (c2_high + c2_low) / 2

            # Bullish FVG: gap between c1.high and c3.low
            if c3_low > c1_high:
                gap_size = c3_low - c1_high
                gap_pct = (gap_size / mid_price) * 100 if mid_price > 0 else 0
                if gap_pct >= min_gap_pct:
                    fvgs.append(FairValueGap(
                        top=c3_low,
                        bottom=c1_high,
                        direction="bullish",
                        timestamp=float(i),
                    ))

            # Bearish FVG: gap between c3.high and c1.low
            if c1_low > c3_high:
                gap_size = c1_low - c3_high
                gap_pct = (gap_size / mid_price) * 100 if mid_price > 0 else 0
                if gap_pct >= min_gap_pct:
                    fvgs.append(FairValueGap(
                        top=c1_low,
                        bottom=c3_high,
                        direction="bearish",
                        timestamp=float(i),
                    ))

        return fvgs

    @staticmethod
    def check_fvg_fill(
        fvg: FairValueGap,
        current_price: float,
    ) -> Tuple[bool, float]:
        """Check if price has entered/filled the FVG.
        Returns (filled, fill_percentage).
        """
        if fvg.top <= fvg.bottom:
            return False, 0.0
        gap_range = fvg.top - fvg.bottom
        if fvg.direction == "bullish":
            if current_price <= fvg.top and current_price >= fvg.bottom:
                fill = (fvg.top - current_price) / gap_range
                return True, min(fill, 1.0)
        else:
            if current_price >= fvg.bottom and current_price <= fvg.top:
                fill = (current_price - fvg.bottom) / gap_range
                return True, min(fill, 1.0)
        return False, 0.0


class OrderBlockDetector:
    """Detects Order Blocks (institutional zones)."""

    @staticmethod
    def find_order_blocks(
        opens: np.ndarray,
        highs: np.ndarray,
        lows: np.ndarray,
        closes: np.ndarray,
        volumes: np.ndarray,
        min_displacement_ratio: float = 1.5,
    ) -> List[OrderBlock]:
        """Find order blocks — last opposing candle before a large displacement.
        
        Bullish OB: last bearish candle before a strong bullish displacement
        Bearish OB: last bullish candle before a strong bearish displacement
        """
        obs = []
        avg_range = pd.Series(highs - lows).rolling(20).mean().values

        for i in range(2, len(opens)):
            if not np.isfinite(avg_range[i - 1]) or avg_range[i - 1] <= 0:
                continue

            current_range = highs[i] - lows[i]
            displacement = current_range / avg_range[i - 1]

            if displacement < min_displacement_ratio:
                continue

            # Direction of displacement
            is_bullish_displacement = closes[i] > opens[i] and (closes[i] - opens[i]) > (highs[i] - lows[i]) * 0.6
            is_bearish_displacement = closes[i] < opens[i] and (opens[i] - closes[i]) > (highs[i] - lows[i]) * 0.6

            if is_bullish_displacement:
                # Look back for last bearish candle (bearish OB)
                for j in range(i - 1, max(i - 5, 0), -1):
                    if closes[j] < opens[j]:
                        obs.append(OrderBlock(
                            top=highs[j],
                            bottom=lows[j],
                            direction="bullish",
                            timestamp=float(j),
                            displacement_size=displacement,
                        ))
                        break

            elif is_bearish_displacement:
                # Look back for last bullish candle (bearish OB)
                for j in range(i - 1, max(i - 5, 0), -1):
                    if closes[j] > opens[j]:
                        obs.append(OrderBlock(
                            top=highs[j],
                            bottom=lows[j],
                            direction="bearish",
                            timestamp=float(j),
                            displacement_size=displacement,
                        ))
                        break

        return obs


class MarketStructure:
    """Detects BOS (Break of Structure) and CHoCH (Change of Character)."""

    @staticmethod
    def find_swings(highs: np.ndarray, lows: np.ndarray, lookback: int = 5) -> Tuple[List[float], List[float]]:
        """Find swing highs and lows."""
        swing_highs = []
        swing_lows = []
        for i in range(lookback, len(highs) - lookback):
            if highs[i] == max(highs[i - lookback:i + lookback + 1]):
                swing_highs.append((i, float(highs[i])))
            if lows[i] == min(lows[i - lookback:i + lookback + 1]):
                swing_lows.append((i, float(lows[i])))
        return swing_highs, swing_lows

    @staticmethod
    def detect_bos(
        swing_highs: List[Tuple[int, float]],
        swing_lows: List[Tuple[int, float]],
        current_price: float,
        current_idx: int,
    ) -> Optional[str]:
        """Detect Break of Structure.
        Returns 'bullish_bos', 'bearish_bos', or None.
        """
        # Bullish BOS: price breaks above last swing high
        for idx, price in reversed(swing_highs):
            if idx < current_idx and current_price > price:
                return "bullish_bos"
            break

        # Bearish BOS: price breaks below last swing low
        for idx, price in reversed(swing_lows):
            if idx < current_idx and current_price < price:
                return "bearish_bos"
            break

        return None

    @staticmethod
    def detect_choch(
        swing_highs: List[Tuple[int, float]],
        swing_lows: List[Tuple[int, float]],
        current_price: float,
        current_idx: int,
        trend: str = "bullish",
    ) -> Optional[str]:
        """Detect Change of Character (first break against trend).
        Returns 'bullish_choch', 'bearish_choch', or None.
        """
        if trend == "bullish":
            # Bearish CHoCH: price breaks below last swing low in uptrend
            for idx, price in reversed(swing_lows):
                if idx < current_idx and current_price < price:
                    return "bearish_choch"
                break
        else:
            # Bullish CHoCH: price breaks above last swing high in downtrend
            for idx, price in reversed(swing_highs):
                if idx < current_idx and current_price > price:
                    return "bullish_choch"
                break
        return None


class PowerOfThree:
    """Detects AMD cycle: Accumulation → Manipulation → Distribution."""

    @staticmethod
    def detect_amd(
        opens: np.ndarray,
        highs: np.ndarray,
        lows: np.ndarray,
        closes: np.ndarray,
        lookback: int = 20,
        manip_wick_ratio: float = 0.6,
    ) -> Optional[str]:
        """Detect if current candle pattern matches AMD.
        
        Bullish AMD:
          1. Accumulation: tight range (small bodies)
          2. Manipulation: wick down below accumulation (Judas swing)
          3. Distribution: strong bullish close above accumulation
        
        Bearish AMD:
          1. Accumulation: tight range
          2. Manipulation: wick up above accumulation
          3. Distribution: strong bearish close below accumulation
        """
        if len(opens) < lookback + 3:
            return None

        # Recent candles
        acc_opens = opens[-lookback - 3:-3]
        acc_highs = highs[-lookback - 3:-3]
        acc_lows = lows[-lookback - 3:-3]
        acc_closes = closes[-lookback - 3:-3]

        # Manipulation candle
        manip_open = opens[-3]
        manip_high = highs[-3]
        manip_low = lows[-3]
        manip_close = closes[-3]
        manip_range = manip_high - manip_low

        # Distribution candle
        dist_open = opens[-1]
        dist_high = highs[-1]
        dist_low = lows[-1]
        dist_close = closes[-1]

        if manip_range <= 0:
            return None

        # Accumulation range
        acc_high = np.max(acc_highs)
        acc_low = np.min(acc_lows)
        acc_range = acc_high - acc_low
        if acc_range <= 0:
            return None

        # Check for manipulation (wick that exceeds accumulation)
        upper_wick = manip_high - max(manip_open, manip_close)
        lower_wick = min(manip_open, manip_close) - manip_low

        # Bullish AMD: manipulation wick goes below acc_low, then distribution closes above acc_high
        if lower_wick / manip_range > manip_wick_ratio:
            if manip_low < acc_low and dist_close > acc_high and dist_close > dist_open:
                return "bullish_amd"

        # Bearish AMD: manipulation wick goes above acc_high, then distribution closes below acc_low
        if upper_wick / manip_range > manip_wick_ratio:
            if manip_high > acc_high and dist_close < acc_low and dist_close < dist_open:
                return "bearish_amd"

        return None


class VectorCandleDetector:
    """Detects vector candles (aggressive displacement by smart money)."""

    @staticmethod
    def detect(
        opens: np.ndarray,
        highs: np.ndarray,
        lows: np.ndarray,
        closes: np.ndarray,
        volumes: np.ndarray,
        min_body_ratio: float = 0.7,
        min_range_ratio: float = 2.0,
    ) -> List[dict]:
        """Detect vector candles — large aggressive candles with minimal wicks.
        
        A vector candle has:
        - Body > 70% of total range (small wicks)
        - Range > 2x average range (displacement)
        """
        vectors = []
        avg_range = pd.Series(highs - lows).rolling(20).mean().values

        for i in range(20, len(opens)):
            if not np.isfinite(avg_range[i]) or avg_range[i] <= 0:
                continue

            body = abs(closes[i] - opens[i])
            total_range = highs[i] - lows[i]
            if total_range <= 0:
                continue

            body_ratio = body / total_range
            range_ratio = total_range / avg_range[i]

            if body_ratio >= min_body_ratio and range_ratio >= min_range_ratio:
                direction = "bullish" if closes[i] > opens[i] else "bearish"
                vectors.append({
                    "index": i,
                    "direction": direction,
                    "top": float(highs[i]),
                    "bottom": float(lows[i]),
                    "body_top": float(max(opens[i], closes[i])),
                    "body_bottom": float(min(opens[i], closes[i])),
                    "range_ratio": round(range_ratio, 2),
                    "volume": float(volumes[i]),
                })

        return vectors


# ─── Main LIT Engine ─────────────────────────────────────────

class LITEngine:
    """Main LIT strategy engine — combines all detectors for signal generation."""

    def __init__(self, config: dict = None):
        cfg = config or {}
        self.liquidity_detector = LiquidityDetector()
        self.fvg_detector = FVGDetector()
        self.ob_detector = OrderBlockDetector()
        self.ms = MarketStructure()
        self.amd = PowerOfThree()
        self.vector_detector = VectorCandleDetector()

        # Config
        self.min_score = float(cfg.get("min_score", 0.65))
        self.sl_atr_mult = float(cfg.get("sl_atr_mult", 1.0))
        self.tp_atr_mult = float(cfg.get("tp_atr_mult", 2.0))
        self.tp2_atr_mult = float(cfg.get("tp2_atr_mult", 3.0))

    def analyze(
        self,
        df: pd.DataFrame,
        symbol: str,
        exchange: str = "binance",
        htf_df: Optional[pd.DataFrame] = None,
    ) -> Optional[LITSignal]:
        """Full LIT analysis on a symbol.
        
        Args:
            df: OHLCV DataFrame (trigger timeframe, e.g. 15m)
            symbol: trading pair
            exchange: exchange name
            htf_df: higher timeframe data for bias (optional, e.g. 1h or 4h)
        """
        if df.empty or len(df) < 30:
            return None

        opens = df["open"].values.astype(float)
        highs = df["high"].values.astype(float)
        lows = df["low"].values.astype(float)
        closes = df["close"].values.astype(float)
        volumes = df["volume"].values.astype(float) if "volume" in df.columns else np.ones(len(opens))

        current_price = float(closes[-1])
        current_idx = len(closes) - 1

        # ATR for SL/TP
        tr = np.maximum(
            highs[1:] - lows[1:],
            np.maximum(
                np.abs(highs[1:] - closes[:-1]),
                np.abs(lows[1:] - closes[:-1]),
            ),
        )
        atr = float(np.mean(tr[-14:])) if len(tr) >= 14 else float(np.mean(tr))

        # ─── Higher TF bias ───
        htf_bias = "neutral"
        if htf_df is not None and not htf_df.empty and len(htf_df) >= 20:
            htf_closes = htf_df["close"].values.astype(float)
            ema20 = pd.Series(htf_closes).ewm(span=20).mean().values
            ema50 = pd.Series(htf_closes).ewm(span=50).mean().values if len(htf_closes) >= 50 else ema20
            if htf_closes[-1] > ema20[-1]:
                htf_bias = "bullish"
            elif htf_closes[-1] < ema20[-1]:
                htf_bias = "bearish"

        # ─── Detect all patterns ───
        signals = []

        # 1. Liquidity Sweep
        sweep_signal = self._check_liquidity_sweep(
            highs, lows, closes, current_price, current_idx, atr, symbol, exchange, htf_bias
        )
        if sweep_signal:
            signals.append(sweep_signal)

        # 2. FVG entry
        fvg_signal = self._check_fvg_entry(
            opens, highs, lows, closes, current_price, atr, symbol, exchange, htf_bias
        )
        if fvg_signal:
            signals.append(fvg_signal)

        # 3. Order Block
        ob_signal = self._check_order_block_entry(
            opens, highs, lows, closes, volumes, current_price, atr, symbol, exchange, htf_bias
        )
        if ob_signal:
            signals.append(ob_signal)

        # 4. Power of Three
        amd_signal = self._check_amd(
            opens, highs, lows, closes, current_price, atr, symbol, exchange, htf_bias
        )
        if amd_signal:
            signals.append(amd_signal)

        # 5. Vector Candle + BOS
        vector_signal = self._check_vector_bos(
            opens, highs, lows, closes, volumes, current_price, current_idx, atr, symbol, exchange, htf_bias
        )
        if vector_signal:
            signals.append(vector_signal)

        if not signals:
            return None

        # Return the highest-scoring signal
        best = max(signals, key=lambda s: s.score)
        return best

    def _check_liquidity_sweep(
        self, highs, lows, closes, price, idx, atr, symbol, exchange, htf_bias,
    ) -> Optional[LITSignal]:
        """Check for liquidity sweep setup."""
        if idx < 20:
            return None

        swing_highs = self.ms.find_swings(highs, lows, lookback=5)[0]
        swing_lows = self.ms.find_swings(highs, lows, lookback=5)[1]
        eq_highs = self.liquidity_detector.find_equal_highs(highs[-20:])
        eq_lows = self.liquidity_detector.find_equal_lows(lows[-20:])

        all_levels = []
        for idx_h, p in swing_highs[-5:]:
            all_levels.append(LiquidityLevel(p, "swing_high", 1))
        for idx_l, p in swing_lows[-5:]:
            all_levels.append(LiquidityLevel(p, "swing_low", 1))
        all_levels.extend(eq_highs[-3:])
        all_levels.extend(eq_lows[-3:])

        for level in all_levels:
            sweep = self.liquidity_detector.detect_sweep(
                float(highs[-1]), float(lows[-1]), float(closes[-1]), level
            )
            if sweep is None:
                continue

            # Check for MSS (market structure shift) after sweep
            ms_before = "bullish" if htf_bias != "bearish" else "bearish"

            if sweep == "sweep_low":
                # Sell-side liquidity swept → expect bullish reversal
                if htf_bias == "bearish":
                    continue  # Don't trade against HTF
                # Require strong sweep: wick must be significant
                wick_ratio = (float(lows[-1]) - level.price) / max(atr, 1e-10)
                if wick_ratio < 0.3:
                    continue
                entry = price
                sl = price - self.sl_atr_mult * atr
                tp = price + self.tp_atr_mult * atr
                tp2 = price + self.tp2_atr_mult * atr
                score = 0.75 + (level.strength * 0.05)

                reasoning = (
                    f"🔵 LIT: شکار نقدینگی فروش\n"
                    f"• سطح نقدینگی: {level.price:.4f} ({level.kind})\n"
                    f"• ویک پایین قیمت از سطح عبور کرد ولی کندل بالای سطح بسته شد\n"
                    f"• این نشانه شکار استاپ‌های فروشندگان توسط پول هوشمند است\n"
                    f"• انتظار: صعود قیمت پس از جمع‌آوری نقدینگی"
                )
                if htf_bias == "bullish":
                    reasoning += f"\n• ✅ جهت HTF هم صعودی است — هم‌راستایی کامل"

                zones = [
                    {"type": "liquidity_level", "price": level.price, "label": f"نقدینگی {level.kind}", "color": "#ef4444"},
                    {"type": "entry", "price": entry, "label": "ورود"},
                    {"type": "sl", "price": sl, "label": "استاپ لاس"},
                    {"type": "tp", "price": tp, "label": "تارگت ۱"},
                    {"type": "tp2", "price": tp2, "label": "تارگت ۲"},
                ]

                return LITSignal(
                    id="LIT_" + uuid.uuid4().hex[:8],
                    symbol=symbol,
                    exchange=exchange,
                    side="long",
                    entry=round(entry, 8),
                    stop_loss=round(sl, 8),
                    take_profit=round(tp, 8),
                    take_profit_2=round(tp2, 8),
                    score=min(score, 1.0),
                    strategy="liquidity_sweep",
                    timestamp=time.time(),
                    reasoning=reasoning,
                    zones=zones,
                )

            elif sweep == "sweep_high":
                # Buy-side liquidity swept → expect bearish reversal
                if htf_bias == "bullish":
                    continue
                wick_ratio = (level.price - float(highs[-1])) / max(atr, 1e-10)
                if wick_ratio < 0.3:
                    continue
                entry = price
                sl = price + self.sl_atr_mult * atr
                tp = price - self.tp_atr_mult * atr
                tp2 = price - self.tp2_atr_mult * atr
                score = 0.75 + (level.strength * 0.05)

                reasoning = (
                    f"🔴 LIT: شکار نقدینگی خرید\n"
                    f"• سطح نقدینگی: {level.price:.4f} ({level.kind})\n"
                    f"• ویک بالا از سطح عبور کرد ولی کندل زیر سطح بسته شد\n"
                    f"• این نشانه شکار استاپ‌های خریداران توسط پول هوشمند است\n"
                    f"• انتظار: نزول قیمت پس از جمع‌آوری نقدینگی"
                )
                if htf_bias == "bearish":
                    reasoning += f"\n• ✅ جهت HTF هم نزولی است — هم‌راستایی کامل"

                zones = [
                    {"type": "liquidity_level", "price": level.price, "label": f"نقدینگی {level.kind}", "color": "#ef4444"},
                    {"type": "entry", "price": entry, "label": "ورود"},
                    {"type": "sl", "price": sl, "label": "استاپ لاس"},
                    {"type": "tp", "price": tp, "label": "تارگت ۱"},
                    {"type": "tp2", "price": tp2, "label": "تارگت ۲"},
                ]

                return LITSignal(
                    id="LIT_" + uuid.uuid4().hex[:8],
                    symbol=symbol,
                    exchange=exchange,
                    side="short",
                    entry=round(entry, 8),
                    stop_loss=round(sl, 8),
                    take_profit=round(tp, 8),
                    take_profit_2=round(tp2, 8),
                    score=min(score, 1.0),
                    strategy="liquidity_sweep",
                    timestamp=time.time(),
                    reasoning=reasoning,
                    zones=zones,
                )

        return None

    def _check_fvg_entry(
        self, opens, highs, lows, closes, price, atr, symbol, exchange, htf_bias,
    ) -> Optional[LITSignal]:
        """Check for FVG retest entry."""
        fvgs = self.fvg_detector.find_fvgs(opens, highs, lows, lows)
        if not fvgs:
            return None

        # Check if price is currently inside a recent FVG
        for fvg in fvgs[-5:]:
            filled, fill_pct = self.fvg_detector.check_fvg_fill(fvg, price)
            if not filled or fill_pct < 0.3:
                continue

            # Only trade FVGs that align with HTF
            if fvg.direction == "bullish" and htf_bias == "bearish":
                continue
            if fvg.direction == "bearish" and htf_bias == "bullish":
                continue

            mid_fvg = (fvg.top + fvg.bottom) / 2

            if fvg.direction == "bullish":
                # Require volume confirmation
                if len(volumes) >= 10:
                    avg_vol = float(np.mean(volumes[-10:]))
                    if float(volumes[-1]) < avg_vol * 1.0:
                        continue
                entry = mid_fvg
                sl = fvg.bottom - 0.3 * atr
                tp = entry + self.tp_atr_mult * atr
                tp2 = entry + self.tp2_atr_mult * atr
                score = 0.80

                reasoning = (
                    f"🟣 FVG: شکاف ارزش منصفانه\n"
                    f"• ناحیه FVG: {fvg.bottom:.4f} — {fvg.top:.4f} (صعودی)\n"
                    f"• قیمت در حال پر کردن شکاف است — ورود در نیمه شکاف\n"
                    f"• پول هوشمند این ناحیه رو ایجاد کرده و قیمت برمی‌گردد\n"
                    f"• انتظار: ادامه روند صعودی پس از پر شدن شکاف"
                )

                zones = [
                    {"type": "fvg", "top": fvg.top, "bottom": fvg.bottom, "label": "FVG صعودی", "color": "#a855f7"},
                    {"type": "entry", "price": entry, "label": "ورود"},
                    {"type": "sl", "price": sl, "label": "استاپ لاس"},
                    {"type": "tp", "price": tp, "label": "تارگت ۱"},
                    {"type": "tp2", "price": tp2, "label": "تارگت ۲"},
                ]

                return LITSignal(
                    id="LIT_" + uuid.uuid4().hex[:8],
                    symbol=symbol,
                    exchange=exchange,
                    side="long",
                    entry=round(entry, 8),
                    stop_loss=round(sl, 8),
                    take_profit=round(tp, 8),
                    take_profit_2=round(tp2, 8),
                    score=score,
                    strategy="fvg",
                    timestamp=time.time(),
                    reasoning=reasoning,
                    zones=zones,
                    fvg_zones=[{"top": fvg.top, "bottom": fvg.bottom, "direction": fvg.direction}],
                )
            else:
                entry = mid_fvg
                sl = fvg.top + 0.3 * atr
                tp = entry - self.tp_atr_mult * atr
                tp2 = entry - self.tp2_atr_mult * atr
                score = 0.80

                reasoning = (
                    f"🟣 FVG: شکاف ارزش منصفانه\n"
                    f"• ناحیه FVG: {fvg.bottom:.4f} — {fvg.top:.4f} (نزولی)\n"
                    f"• قیمت در حال پر کردن شکاف است — ورود در نیمه شکاف\n"
                    f"• پول هوشمند این ناحیه رو ایجاد کرده و قیمت برمی‌گردد\n"
                    f"• انتظار: ادامه روند نزولی پس از پر شدن شکاف"
                )

                zones = [
                    {"type": "fvg", "top": fvg.top, "bottom": fvg.bottom, "label": "FVG نزولی", "color": "#a855f7"},
                    {"type": "entry", "price": entry, "label": "ورود"},
                    {"type": "sl", "price": sl, "label": "استاپ لاس"},
                    {"type": "tp", "price": tp, "label": "تارگت ۱"},
                    {"type": "tp2", "price": tp2, "label": "تارگت ۲"},
                ]

                return LITSignal(
                    id="LIT_" + uuid.uuid4().hex[:8],
                    symbol=symbol,
                    exchange=exchange,
                    side="short",
                    entry=round(entry, 8),
                    stop_loss=round(sl, 8),
                    take_profit=round(tp, 8),
                    take_profit_2=round(tp2, 8),
                    score=score,
                    strategy="fvg",
                    timestamp=time.time(),
                    reasoning=reasoning,
                    zones=zones,
                    fvg_zones=[{"top": fvg.top, "bottom": fvg.bottom, "direction": fvg.direction}],
                )

        return None

    def _check_order_block_entry(
        self, opens, highs, lows, closes, volumes, price, atr, symbol, exchange, htf_bias,
    ) -> Optional[LITSignal]:
        """Check for Order Block retest entry."""
        obs = self.ob_detector.find_order_blocks(opens, highs, lows, closes, volumes)
        if not obs:
            return None

        for ob in obs[-5:]:
            if ob.mitigated:
                continue

            # Check if price is retesting the OB
            in_ob = ob.bottom <= price <= ob.top
            if not in_ob:
                continue

            if ob.direction == "bullish" and htf_bias == "bearish":
                continue
            if ob.direction == "bearish" and htf_bias == "bullish":
                continue

            if ob.direction == "bullish":
                # Require strong displacement
                if ob.displacement_size < 1.2:
                    continue
                entry = ob.top
                sl = ob.bottom - 0.2 * atr
                tp = entry + self.tp_atr_mult * atr
                tp2 = entry + self.tp2_atr_mult * atr
                score = 0.85 + min(ob.displacement_size * 0.01, 0.10)

                reasoning = (
                    f"🟢 Order Block: ناحیه سفارش نهادی\n"
                    f"• ناحیه OB: {ob.bottom:.4f} — {ob.top:.4f} (صعودی)\n"
                    f"• اندازه جابجایی: {ob.displacement_size:.1f}x میانگین\n"
                    f"• قیمت در حال retest ناحیه سفارش نهادی است\n"
                    f"• انتظار: برگشت صعودی از این ناحیه"
                )

                zones = [
                    {"type": "order_block", "top": ob.top, "bottom": ob.bottom, "label": "Order Block صعودی", "color": "#10b981"},
                    {"type": "entry", "price": entry, "label": "ورود"},
                    {"type": "sl", "price": sl, "label": "استاپ لاس"},
                    {"type": "tp", "price": tp, "label": "تارگت ۱"},
                    {"type": "tp2", "price": tp2, "label": "تارگت ۲"},
                ]

                return LITSignal(
                    id="LIT_" + uuid.uuid4().hex[:8],
                    symbol=symbol,
                    exchange=exchange,
                    side="long",
                    entry=round(entry, 8),
                    stop_loss=round(sl, 8),
                    take_profit=round(tp, 8),
                    take_profit_2=round(tp2, 8),
                    score=min(score, 1.0),
                    strategy="order_block",
                    timestamp=time.time(),
                    reasoning=reasoning,
                    zones=zones,
                    order_blocks=[{"top": ob.top, "bottom": ob.bottom, "direction": ob.direction}],
                )
            else:
                if ob.displacement_size < 1.2:
                    continue
                entry = ob.bottom
                sl = ob.top + 0.2 * atr
                tp = entry - self.tp_atr_mult * atr
                tp2 = entry - self.tp2_atr_mult * atr
                score = 0.85 + min(ob.displacement_size * 0.01, 0.10)

                reasoning = (
                    f"🔴 Order Block: ناحیه سفارش نهادی\n"
                    f"• ناحیه OB: {ob.bottom:.4f} — {ob.top:.4f} (نزولی)\n"
                    f"• اندازه جابجایی: {ob.displacement_size:.1f}x میانگین\n"
                    f"• قیمت در حال retest ناحیه سفارش نهادی است\n"
                    f"• انتظار: برگشت نزولی از این ناحیه"
                )

                zones = [
                    {"type": "order_block", "top": ob.top, "bottom": ob.bottom, "label": "Order Block نزولی", "color": "#ef4444"},
                    {"type": "entry", "price": entry, "label": "ورود"},
                    {"type": "sl", "price": sl, "label": "استاپ لاس"},
                    {"type": "tp", "price": tp, "label": "تارگت ۱"},
                    {"type": "tp2", "price": tp2, "label": "تارگت ۲"},
                ]

                return LITSignal(
                    id="LIT_" + uuid.uuid4().hex[:8],
                    symbol=symbol,
                    exchange=exchange,
                    side="short",
                    entry=round(entry, 8),
                    stop_loss=round(sl, 8),
                    take_profit=round(tp, 8),
                    take_profit_2=round(tp2, 8),
                    score=min(score, 1.0),
                    strategy="order_block",
                    timestamp=time.time(),
                    reasoning=reasoning,
                    zones=zones,
                    order_blocks=[{"top": ob.top, "bottom": ob.bottom, "direction": ob.direction}],
                )

        return None

    def _check_amd(
        self, opens, highs, lows, closes, price, atr, symbol, exchange, htf_bias,
    ) -> Optional[LITSignal]:
        """Check for Power of Three (AMD) pattern."""
        amd = self.amd.detect_amd(opens, highs, lows, closes)
        if amd is None:
            return None

        if amd == "bullish_amd" and htf_bias == "bearish":
            return None
        if amd == "bearish_amd" and htf_bias == "bullish":
            return None

        if amd == "bullish_amd":
            if len(closes) < 20:
                return None
            entry = price
            sl = price - self.sl_atr_mult * atr
            tp = price + self.tp_atr_mult * atr
            tp2 = price + self.tp2_atr_mult * atr
            score = 0.83

            reasoning = (
                f"⚡ Power of Three: AMD صعودی\n"
                f"• تجمع (Accumulation): بازار در محدوده باریک نوسان کرده\n"
                    f"• دستکاری (Manipulation): ویک پایین — شکار استاپ‌ها\n"
                    f"• توزیع (Distribution): کندل صعودی قوی بالای محدوده بسته شده\n"
                    f"• این الگوی کلاسیک پول هوشمند است"
            )

            zones = [
                {"type": "entry", "price": entry, "label": "ورود"},
                {"type": "sl", "price": sl, "label": "استاپ لاس"},
                {"type": "tp", "price": tp, "label": "تارگت ۱"},
                {"type": "tp2", "price": tp2, "label": "تارگت ۲"},
            ]

            return LITSignal(
                id="LIT_" + uuid.uuid4().hex[:8],
                symbol=symbol, exchange=exchange, side="long",
                entry=round(entry, 8), stop_loss=round(sl, 8),
                take_profit=round(tp, 8), take_profit_2=round(tp2, 8),
                score=score, strategy="power_of_three", timestamp=time.time(),
                reasoning=reasoning, zones=zones,
            )
        else:
            if len(closes) < 20:
                return None
            entry = price
            sl = price + self.sl_atr_mult * atr
            tp = price - self.tp_atr_mult * atr
            tp2 = price - self.tp2_atr_mult * atr
            score = 0.83

            reasoning = (
                f"⚡ Power of Three: AMD نزولی\n"
                f"• تجمع (Accumulation): بازار در محدوده باریک نوسان کرده\n"
                f"• دستکاری (Manipulation): ویک بالا — شکار استاپ‌ها\n"
                f"• توزیع (Distribution): کندل نزولی قوی زیر محدوده بسته شده\n"
                f"• این الگوی کلاسیک پول هوشمند است"
            )

            zones = [
                {"type": "entry", "price": entry, "label": "ورود"},
                {"type": "sl", "price": sl, "label": "استاپ لاس"},
                {"type": "tp", "price": tp, "label": "تارگت ۱"},
                {"type": "tp2", "price": tp2, "label": "تارگت ۲"},
            ]

            return LITSignal(
                id="LIT_" + uuid.uuid4().hex[:8],
                symbol=symbol, exchange=exchange, side="short",
                entry=round(entry, 8), stop_loss=round(sl, 8),
                take_profit=round(tp, 8), take_profit_2=round(tp2, 8),
                score=score, strategy="power_of_three", timestamp=time.time(),
                reasoning=reasoning, zones=zones,
            )

    def _check_vector_bos(
        self, opens, highs, lows, closes, volumes, price, idx, atr, symbol, exchange, htf_bias,
    ) -> Optional[LITSignal]:
        """Check for Vector Candle + BOS confluence."""
        vectors = self.vector_detector.detect(opens, highs, lows, closes, volumes)
        if not vectors:
            return None

        # Check last few vector candles
        for vec in vectors[-3:]:
            if vec["index"] < idx - 3:
                continue

            swing_highs, swing_lows = self.ms.find_swings(highs, lows, lookback=3)
            bos = self.ms.detect_bos(swing_highs, swing_lows, price, idx)

            if vec["direction"] == "bullish" and bos == "bullish_bos":
                if htf_bias == "bearish":
                    continue
                if vec.get("range_ratio", 0) < 1.5:
                    continue
                entry = price
                sl = vec["bottom"] - 0.2 * atr
                tp = entry + self.tp_atr_mult * atr
                tp2 = entry + self.tp2_atr_mult * atr
                score = 0.84

                reasoning = (
                    f"🔥 Vector + BOS: کندل تهاجمی + شکست ساختار\n"
                    f"• کندل وکتور: {vec['range_ratio']}x میانگین رنج — حرکت تهاجمی\n"
                    f"• جهت: صعودی — حجم بالا: {vec['volume']:.0f}\n"
                    f"• BOS: شکست سقف ساختار — تأیید ادامه روند\n"
                    f"• ترکیب وکتور + BOS = ورود قوی"
                )

                zones = [
                    {"type": "vector", "top": vec["top"], "bottom": vec["bottom"], "label": "Vector Candle", "color": "#f59e0b"},
                    {"type": "entry", "price": entry, "label": "ورود"},
                    {"type": "sl", "price": sl, "label": "استاپ لاس"},
                    {"type": "tp", "price": tp, "label": "تارگت ۱"},
                    {"type": "tp2", "price": tp2, "label": "تارگت ۲"},
                ]

                return LITSignal(
                    id="LIT_" + uuid.uuid4().hex[:8],
                    symbol=symbol, exchange=exchange, side="long",
                    entry=round(entry, 8), stop_loss=round(sl, 8),
                    take_profit=round(tp, 8), take_profit_2=round(tp2, 8),
                    score=score, strategy="vector_bos", timestamp=time.time(),
                    reasoning=reasoning, zones=zones,
                )

            elif vec["direction"] == "bearish" and bos == "bearish_bos":
                if htf_bias == "bullish":
                    continue
                if vec.get("range_ratio", 0) < 1.5:
                    continue
                entry = price
                sl = vec["top"] + 0.2 * atr
                tp = entry - self.tp_atr_mult * atr
                tp2 = entry - self.tp2_atr_mult * atr
                score = 0.84

                reasoning = (
                    f"🔥 Vector + BOS: کندل تهاجمی + شکست ساختار\n"
                    f"• کندل وکتور: {vec['range_ratio']}x میانگین رنج — حرکت تهاجمی\n"
                    f"• جهت: نزولی — حجم بالا: {vec['volume']:.0f}\n"
                    f"• BOS: شکست کف ساختار — تأیید ادامه روند\n"
                    f"• ترکیب وکتور + BOS = ورود قوی"
                )

                zones = [
                    {"type": "vector", "top": vec["top"], "bottom": vec["bottom"], "label": "Vector Candle", "color": "#f59e0b"},
                    {"type": "entry", "price": entry, "label": "ورود"},
                    {"type": "sl", "price": sl, "label": "استاپ لاس"},
                    {"type": "tp", "price": tp, "label": "تارگت ۱"},
                    {"type": "tp2", "price": tp2, "label": "تارگت ۲"},
                ]

                return LITSignal(
                    id="LIT_" + uuid.uuid4().hex[:8],
                    symbol=symbol, exchange=exchange, side="short",
                    entry=round(entry, 8), stop_loss=round(sl, 8),
                    take_profit=round(tp, 8), take_profit_2=round(tp2, 8),
                    score=score, strategy="vector_bos", timestamp=time.time(),
                    reasoning=reasoning, zones=zones,
                )

        return None
