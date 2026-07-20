"""LIT Structure Engine - Market Structure Analysis.

Deterministic detection of:
  - Valid swing highs/lows (fractal-based)
  - Break of Structure (BOS) - continuation
  - Change of Character (CHoCH) - reversal signal
  - Trend state machine (bullish/bearish/ranging)
  - HTF/LTF alignment

This is Layer 1 of the LIT architecture and must be completely
deterministic (no lookahead bias).
"""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import List, Optional, Tuple

import numpy as np


# ─── Enums & Data Models ─────────────────────────────────────

class TrendState(str, Enum):
    BULLISH = "bullish"
    BEARISH = "bearish"
    RANGING = "ranging"


class StructureBreak(str, Enum):
    BULLISH_BOS = "bullish_bos"
    BEARISH_BOS = "bearish_bos"
    BULLISH_CHOCH = "bullish_choch"
    BEARISH_CHOCH = "bearish_choch"


@dataclass
class SwingPoint:
    """A validated swing high or low."""
    index: int
    price: float
    kind: str           # "high" | "low"
    strength: int       # number of bars on each side that confirm it
    valid: bool = True  # False if broken (invalidated by BOS)
    broken_at: int = -1


@dataclass
class StructureEvent:
    """A BOS or CHoCH event."""
    kind: StructureBreak
    index: int          # bar index where it occurred
    price: float        # price at which structure broke
    swing_ref: SwingPoint  # the swing point that was broken
    displacement: float = 0.0  # body size of breaking candle vs ATR


@dataclass
class StructureState:
    """Complete market structure state at a given moment."""
    trend: TrendState
    swing_highs: List[SwingPoint]
    swing_lows: List[SwingPoint]
    last_valid_high: Optional[SwingPoint]
    last_valid_low: Optional[SwingPoint]
    events: List[StructureEvent]
    # Internal tracking
    last_higher_high: Optional[SwingPoint] = None
    last_higher_low: Optional[SwingPoint] = None
    last_lower_high: Optional[SwingPoint] = None
    last_lower_low: Optional[SwingPoint] = None


# ─── Structure Engine ────────────────────────────────────────

class StructureEngine:
    """Deterministic market structure analyzer.
    
    Algorithm:
    1. Find all swing points using fractal method (N bars left, N bars right)
    2. Classify swings as HH/HL/LH/LL based on sequence
    3. Track BOS (break of last swing in trend direction)
    4. Track CHoCH (break of last swing AGAINST trend direction)
    5. Maintain trend state machine
    """

    def __init__(self, swing_lookback: int = 5, min_swing_distance: int = 3):
        self.swing_lookback = swing_lookback
        self.min_swing_distance = min_swing_distance

    def analyze(
        self,
        highs: np.ndarray,
        lows: np.ndarray,
        closes: np.ndarray,
        opens: np.ndarray,
    ) -> StructureState:
        """Full structure analysis on OHLC data.
        
        Returns StructureState with all swings, events, and current trend.
        """
        n = len(highs)
        if n < self.swing_lookback * 2 + 5:
            return StructureState(
                trend=TrendState.RANGING,
                swing_highs=[], swing_lows=[],
                last_valid_high=None, last_valid_low=None,
                events=[],
            )

        # Step 1: Find all swing points
        swing_highs = self._find_swing_highs(highs, self.swing_lookback)
        swing_lows = self._find_swing_lows(lows, self.swing_lookback)

        # Step 2: Determine structure sequence and trend
        trend, events = self._build_structure(
            swing_highs, swing_lows, highs, lows, closes, opens
        )

        # Step 3: Find last valid (unbroken) swings
        last_valid_high = None
        last_valid_low = None
        for sh in reversed(swing_highs):
            if sh.valid:
                last_valid_high = sh
                break
        for sl in reversed(swing_lows):
            if sl.valid:
                last_valid_low = sl
                break

        return StructureState(
            trend=trend,
            swing_highs=swing_highs,
            swing_lows=swing_lows,
            last_valid_high=last_valid_high,
            last_valid_low=last_valid_low,
            events=events,
        )

    def get_htf_bias(
        self,
        highs: np.ndarray,
        lows: np.ndarray,
        closes: np.ndarray,
        opens: np.ndarray,
    ) -> TrendState:
        """Quick HTF bias determination without full event tracking."""
        state = self.analyze(highs, lows, closes, opens)
        return state.trend

    # ─── Internal Methods ────────────────────────────────────

    def _find_swing_highs(self, highs: np.ndarray, lookback: int) -> List[SwingPoint]:
        """Find swing highs using fractal method.
        
        A swing high is confirmed when `lookback` bars on each side
        have lower highs.
        """
        swings = []
        n = len(highs)
        last_idx = -self.min_swing_distance - 1

        for i in range(lookback, n - lookback):
            is_swing = True
            for j in range(1, lookback + 1):
                if highs[i - j] >= highs[i] or highs[i + j] >= highs[i]:
                    is_swing = False
                    break

            if is_swing and (i - last_idx) >= self.min_swing_distance:
                strength = self._calc_swing_strength(highs, i, "high")
                swings.append(SwingPoint(
                    index=i,
                    price=float(highs[i]),
                    kind="high",
                    strength=strength,
                ))
                last_idx = i

        return swings

    def _find_swing_lows(self, lows: np.ndarray, lookback: int) -> List[SwingPoint]:
        """Find swing lows using fractal method."""
        swings = []
        n = len(lows)
        last_idx = -self.min_swing_distance - 1

        for i in range(lookback, n - lookback):
            is_swing = True
            for j in range(1, lookback + 1):
                if lows[i - j] <= lows[i] or lows[i + j] <= lows[i]:
                    is_swing = False
                    break

            if is_swing and (i - last_idx) >= self.min_swing_distance:
                strength = self._calc_swing_strength(lows, i, "low")
                swings.append(SwingPoint(
                    index=i,
                    price=float(lows[i]),
                    kind="low",
                    strength=strength,
                ))
                last_idx = i

        return swings

    def _calc_swing_strength(self, data: np.ndarray, idx: int, kind: str) -> int:
        """Calculate how many bars on each side confirm this swing."""
        n = len(data)
        strength = 0
        for offset in range(1, min(20, n - idx, idx)):
            if kind == "high":
                if data[idx - offset] < data[idx] and data[idx + offset] < data[idx]:
                    strength += 1
                else:
                    break
            else:
                if data[idx - offset] > data[idx] and data[idx + offset] > data[idx]:
                    strength += 1
                else:
                    break
        return max(strength, 1)

    def _build_structure(
        self,
        swing_highs: List[SwingPoint],
        swing_lows: List[SwingPoint],
        highs: np.ndarray,
        lows: np.ndarray,
        closes: np.ndarray,
        opens: np.ndarray,
    ) -> Tuple[TrendState, List[StructureEvent]]:
        """Build structure events (BOS/CHoCH) and determine trend.
        
        Logic:
        - Start with RANGING
        - If we see HH + HL pattern -> BULLISH
        - If we see LH + LL pattern -> BEARISH
        - BOS = price breaks the LAST swing in trend direction
        - CHoCH = price breaks swing AGAINST trend (first counter-break)
        """
        events: List[StructureEvent] = []
        n = len(closes)

        if len(swing_highs) < 2 or len(swing_lows) < 2:
            return TrendState.RANGING, events

        # Merge all swings into chronological order
        all_swings = sorted(
            [(s.index, s) for s in swing_highs] + [(s.index, s) for s in swing_lows],
            key=lambda x: x[0]
        )

        # Determine initial trend from first few swings
        trend = TrendState.RANGING
        prev_high: Optional[SwingPoint] = None
        prev_low: Optional[SwingPoint] = None

        for _, swing in all_swings:
            if swing.kind == "high":
                if prev_high is not None:
                    if swing.price > prev_high.price:
                        # Higher high
                        if trend == TrendState.BEARISH:
                            # Potential CHoCH - need to check if this breaks structure
                            pass
                        if prev_low and prev_low.price > (prev_low.price if not hasattr(prev_low, '_prev_price') else 0):
                            trend = TrendState.BULLISH
                    else:
                        # Lower high
                        if trend == TrendState.BULLISH:
                            pass  # First sign of weakness
                prev_high = swing

            elif swing.kind == "low":
                if prev_low is not None:
                    if swing.price < prev_low.price:
                        # Lower low
                        if prev_high and prev_high.price < (prev_high.price if prev_high else float('inf')):
                            trend = TrendState.BEARISH
                    else:
                        # Higher low
                        pass
                prev_low = swing

        # Now scan for BOS/CHoCH events using the candle data
        # A BOS happens when a candle CLOSES beyond the last valid swing
        current_trend = TrendState.RANGING

        # Track last significant swings
        last_sh: Optional[SwingPoint] = None
        last_sl: Optional[SwingPoint] = None

        for _, swing in all_swings:
            if swing.kind == "high":
                last_sh = swing
            else:
                last_sl = swing

        # Scan candles for structure breaks
        for i in range(max(s.index for _, s in all_swings) + 1, n):
            # Check if candle breaks above last swing high (bullish BOS)
            if last_sh and closes[i] > last_sh.price:
                if current_trend == TrendState.BEARISH:
                    # CHoCH - first break against trend
                    event_kind = StructureBreak.BULLISH_CHOCH
                    current_trend = TrendState.BULLISH
                else:
                    event_kind = StructureBreak.BULLISH_BOS
                    current_trend = TrendState.BULLISH

                disp = self._calc_displacement(opens, closes, highs, lows, i)
                events.append(StructureEvent(
                    kind=event_kind,
                    index=i,
                    price=float(closes[i]),
                    swing_ref=last_sh,
                    displacement=disp,
                ))
                last_sh.valid = False
                last_sh.broken_at = i

                # Find next swing high after this one
                for _, s in all_swings:
                    if s.kind == "high" and s.index > last_sh.index and s.valid:
                        last_sh = s
                        break

            # Check if candle breaks below last swing low (bearish BOS)
            if last_sl and closes[i] < last_sl.price:
                if current_trend == TrendState.BULLISH:
                    event_kind = StructureBreak.BEARISH_CHOCH
                    current_trend = TrendState.BEARISH
                else:
                    event_kind = StructureBreak.BEARISH_BOS
                    current_trend = TrendState.BEARISH

                disp = self._calc_displacement(opens, closes, highs, lows, i)
                events.append(StructureEvent(
                    kind=event_kind,
                    index=i,
                    price=float(closes[i]),
                    swing_ref=last_sl,
                    displacement=disp,
                ))
                last_sl.valid = False
                last_sl.broken_at = i

                for _, s in all_swings:
                    if s.kind == "low" and s.index > last_sl.index and s.valid:
                        last_sl = s
                        break

        # Final trend determination from events
        if events:
            last_event = events[-1]
            if last_event.kind in (StructureBreak.BULLISH_BOS, StructureBreak.BULLISH_CHOCH):
                current_trend = TrendState.BULLISH
            elif last_event.kind in (StructureBreak.BEARISH_BOS, StructureBreak.BEARISH_CHOCH):
                current_trend = TrendState.BEARISH

        return current_trend, events

    def _calc_displacement(
        self,
        opens: np.ndarray,
        closes: np.ndarray,
        highs: np.ndarray,
        lows: np.ndarray,
        idx: int,
    ) -> float:
        """Calculate displacement strength of the breaking candle.
        
        Displacement = body size / average range over last 14 bars.
        Higher = more aggressive / institutional.
        """
        body = abs(float(closes[idx]) - float(opens[idx]))
        # Average true range of last 14 bars
        start = max(0, idx - 14)
        ranges = highs[start:idx] - lows[start:idx]
        avg_range = float(np.mean(ranges)) if len(ranges) > 0 else 1e-10
        if avg_range <= 0:
            avg_range = 1e-10
        return body / avg_range


# ─── Utility Functions ───────────────────────────────────────

def is_bullish_structure(state: StructureState) -> bool:
    """Quick check if structure is bullish."""
    return state.trend == TrendState.BULLISH


def is_bearish_structure(state: StructureState) -> bool:
    """Quick check if structure is bearish."""
    return state.trend == TrendState.BEARISH


def get_recent_bos(state: StructureState, lookback_bars: int = 20, current_idx: int = -1) -> Optional[StructureEvent]:
    """Get the most recent BOS within lookback window."""
    for event in reversed(state.events):
        if current_idx > 0 and (current_idx - event.index) > lookback_bars:
            break
        if event.kind in (StructureBreak.BULLISH_BOS, StructureBreak.BEARISH_BOS):
            return event
    return None


def get_recent_choch(state: StructureState, lookback_bars: int = 20, current_idx: int = -1) -> Optional[StructureEvent]:
    """Get the most recent CHoCH within lookback window."""
    for event in reversed(state.events):
        if current_idx > 0 and (current_idx - event.index) > lookback_bars:
            break
        if event.kind in (StructureBreak.BULLISH_CHOCH, StructureBreak.BEARISH_CHOCH):
            return event
    return None
