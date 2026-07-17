"""LIT Structure Engine — Market Structure Detection.

Deterministic detection of:
  - Valid swing highs/lows (pivot-based with configurable left/right bars)
  - HH, HL, LH, LL classification
  - Break of Structure (BOS) — continuation
  - Change of Character (CHoCH) — reversal warning
  - Trend state machine (bullish/bearish/ranging)
  - Displacement detection (impulsive moves)

Only uses CONFIRMED CLOSED candles. No repainting.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import List, Optional, Tuple

import numpy as np


# ─── Enums ───────────────────────────────────────────────────

class TrendState(str, Enum):
    BULLISH = "bullish"
    BEARISH = "bearish"
    RANGING = "ranging"


class SwingType(str, Enum):
    HH = "HH"  # Higher High
    HL = "HL"  # Higher Low
    LH = "LH"  # Lower High
    LL = "LL"  # Lower Low
    UNCLASSIFIED = "unclassified"


class StructureBreakType(str, Enum):
    BULLISH_BOS = "bullish_bos"
    BEARISH_BOS = "bearish_bos"
    BULLISH_CHOCH = "bullish_choch"
    BEARISH_CHOCH = "bearish_choch"


# ─── Data Models ─────────────────────────────────────────────

@dataclass
class SwingPoint:
    """A confirmed swing high or low."""
    index: int
    price: float
    kind: str              # "high" | "low"
    classification: SwingType = SwingType.UNCLASSIFIED
    left_bars: int = 5
    right_bars: int = 5
    valid: bool = True     # False if invalidated by structure break
    broken_at: int = -1
    timestamp: float = 0.0


@dataclass
class StructureBreak:
    """A BOS or CHoCH event."""
    kind: StructureBreakType
    index: int             # candle index where break confirmed
    price: float           # close price of breaking candle
    swing_broken: SwingPoint  # which swing was broken
    displacement: float = 0.0  # body/ATR of breaking candle
    timestamp: float = 0.0


@dataclass
class Displacement:
    """An impulsive displacement candle/sequence."""
    index: int
    direction: str         # "bullish" | "bearish"
    body_size: float       # absolute body
    body_atr_ratio: float  # body / ATR
    body_range_ratio: float  # body / total range (wick ratio)
    creates_fvg: bool = False
    breaks_structure: bool = False


@dataclass
class StructureState:
    """Complete market structure snapshot."""
    trend: TrendState
    swing_highs: List[SwingPoint]
    swing_lows: List[SwingPoint]
    events: List[StructureBreak]
    displacements: List[Displacement]
    last_valid_high: Optional[SwingPoint] = None
    last_valid_low: Optional[SwingPoint] = None
    last_hh: Optional[SwingPoint] = None
    last_hl: Optional[SwingPoint] = None
    last_lh: Optional[SwingPoint] = None
    last_ll: Optional[SwingPoint] = None


# ─── Structure Engine ────────────────────────────────────────

class StructureEngine:
    """Deterministic market structure analyzer using pivot logic."""

    def __init__(self, left_bars: int = 5, right_bars: int = 5, min_displacement_atr: float = 1.5, min_body_ratio: float = 0.6):
        self.left_bars = left_bars
        self.right_bars = right_bars
        self.min_displacement_atr = min_displacement_atr
        self.min_body_ratio = min_body_ratio

    def analyze(
        self,
        opens: np.ndarray,
        highs: np.ndarray,
        lows: np.ndarray,
        closes: np.ndarray,
        timestamps: Optional[np.ndarray] = None,
    ) -> StructureState:
        """Full structure analysis. Returns StructureState."""
        n = len(closes)
        if n < (self.left_bars + self.right_bars + 5):
            return StructureState(trend=TrendState.RANGING, swing_highs=[], swing_lows=[], events=[], displacements=[])

        # Step 1: Find confirmed swing points
        swing_highs = self._find_swing_highs(highs, timestamps)
        swing_lows = self._find_swing_lows(lows, timestamps)

        # Step 2: Classify swings (HH/HL/LH/LL)
        self._classify_swings(swing_highs, swing_lows)

        # Step 3: Detect structure breaks (BOS/CHoCH)
        events = self._detect_structure_breaks(swing_highs, swing_lows, closes, timestamps)

        # Step 4: Determine trend
        trend = self._determine_trend(swing_highs, swing_lows, events)

        # Step 5: Detect displacements
        atr = self._calc_atr(highs, lows, closes, 14)
        displacements = self._detect_displacements(opens, highs, lows, closes, atr, timestamps)

        # Step 6: Find last valid swings
        last_valid_high = next((s for s in reversed(swing_highs) if s.valid), None)
        last_valid_low = next((s for s in reversed(swing_lows) if s.valid), None)

        return StructureState(
            trend=trend,
            swing_highs=swing_highs,
            swing_lows=swing_lows,
            events=events,
            displacements=displacements,
            last_valid_high=last_valid_high,
            last_valid_low=last_valid_low,
            last_hh=next((s for s in reversed(swing_highs) if s.classification == SwingType.HH), None),
            last_hl=next((s for s in reversed(swing_lows) if s.classification == SwingType.HL), None),
            last_lh=next((s for s in reversed(swing_highs) if s.classification == SwingType.LH), None),
            last_ll=next((s for s in reversed(swing_lows) if s.classification == SwingType.LL), None),
        )

    def _find_swing_highs(self, highs: np.ndarray, timestamps: Optional[np.ndarray]) -> List[SwingPoint]:
        """Find swing highs using pivot logic (left_bars + right_bars)."""
        swings = []
        n = len(highs)
        for i in range(self.left_bars, n - self.right_bars):
            is_pivot = True
            for j in range(1, self.left_bars + 1):
                if highs[i - j] > highs[i]:
                    is_pivot = False
                    break
            if not is_pivot:
                continue
            for j in range(1, self.right_bars + 1):
                if highs[i + j] > highs[i]:
                    is_pivot = False
                    break
            if is_pivot:
                ts = float(timestamps[i]) if timestamps is not None else 0.0
                swings.append(SwingPoint(
                    index=i, price=float(highs[i]), kind="high",
                    left_bars=self.left_bars, right_bars=self.right_bars,
                    timestamp=ts,
                ))
        return swings

    def _find_swing_lows(self, lows: np.ndarray, timestamps: Optional[np.ndarray]) -> List[SwingPoint]:
        """Find swing lows using pivot logic."""
        swings = []
        n = len(lows)
        for i in range(self.left_bars, n - self.right_bars):
            is_pivot = True
            for j in range(1, self.left_bars + 1):
                if lows[i - j] < lows[i]:
                    is_pivot = False
                    break
            if not is_pivot:
                continue
            for j in range(1, self.right_bars + 1):
                if lows[i + j] < lows[i]:
                    is_pivot = False
                    break
            if is_pivot:
                ts = float(timestamps[i]) if timestamps is not None else 0.0
                swings.append(SwingPoint(
                    index=i, price=float(lows[i]), kind="low",
                    left_bars=self.left_bars, right_bars=self.right_bars,
                    timestamp=ts,
                ))
        return swings

    def _classify_swings(self, swing_highs: List[SwingPoint], swing_lows: List[SwingPoint]) -> None:
        """Classify each swing as HH/HL/LH/LL relative to previous."""
        # Classify highs
        for i in range(1, len(swing_highs)):
            prev = swing_highs[i - 1]
            curr = swing_highs[i]
            if curr.price > prev.price:
                curr.classification = SwingType.HH
            else:
                curr.classification = SwingType.LH

        # Classify lows
        for i in range(1, len(swing_lows)):
            prev = swing_lows[i - 1]
            curr = swing_lows[i]
            if curr.price > prev.price:
                curr.classification = SwingType.HL
            else:
                curr.classification = SwingType.LL

    def _detect_structure_breaks(
        self, swing_highs: List[SwingPoint], swing_lows: List[SwingPoint],
        closes: np.ndarray, timestamps: Optional[np.ndarray],
    ) -> List[StructureBreak]:
        """Detect BOS and CHoCH events."""
        events = []
        n = len(closes)

        # Track current assumed trend for CHoCH detection
        current_trend = TrendState.RANGING

        # Check for breaks of swing highs (bullish BOS or bullish CHoCH)
        for sh in swing_highs:
            if not sh.valid:
                continue
            # Scan candles after the swing for a close above it
            start = sh.index + self.right_bars + 1
            for i in range(start, n):
                if closes[i] > sh.price:
                    ts = float(timestamps[i]) if timestamps is not None else 0.0
                    # Determine if BOS or CHoCH
                    if current_trend == TrendState.BEARISH:
                        kind = StructureBreakType.BULLISH_CHOCH
                    else:
                        kind = StructureBreakType.BULLISH_BOS
                    current_trend = TrendState.BULLISH

                    # Displacement of breaking candle
                    body = abs(closes[i] - opens[i]) if i < len(closes) else 0
                    avg_range = float(np.mean(highs[max(0, i-14):i] - lows[max(0, i-14):i])) if i > 14 else 1.0
                    disp = body / max(avg_range, 1e-10)

                    events.append(StructureBreak(
                        kind=kind, index=i, price=float(closes[i]),
                        swing_broken=sh, displacement=disp, timestamp=ts,
                    ))
                    sh.valid = False
                    sh.broken_at = i
                    break

        # Check for breaks of swing lows (bearish BOS or bearish CHoCH)
        for sl in swing_lows:
            if not sl.valid:
                continue
            start = sl.index + self.right_bars + 1
            for i in range(start, n):
                if closes[i] < sl.price:
                    ts = float(timestamps[i]) if timestamps is not None else 0.0
                    if current_trend == TrendState.BULLISH:
                        kind = StructureBreakType.BEARISH_CHOCH
                    else:
                        kind = StructureBreakType.BEARISH_BOS
                    current_trend = TrendState.BEARISH

                    body = abs(closes[i] - opens[i]) if i < len(closes) else 0
                    avg_range = float(np.mean(highs[max(0, i-14):i] - lows[max(0, i-14):i])) if i > 14 else 1.0
                    disp = body / max(avg_range, 1e-10)

                    events.append(StructureBreak(
                        kind=kind, index=i, price=float(closes[i]),
                        swing_broken=sl, displacement=disp, timestamp=ts,
                    ))
                    sl.valid = False
                    sl.broken_at = i
                    break

        # Sort events chronologically
        events.sort(key=lambda e: e.index)
        return events

    def _determine_trend(
        self, swing_highs: List[SwingPoint], swing_lows: List[SwingPoint],
        events: List[StructureBreak],
    ) -> TrendState:
        """Determine current trend from structure events."""
        if not events:
            # Fallback: use swing classification
            recent_highs = swing_highs[-3:] if swing_highs else []
            recent_lows = swing_lows[-3:] if swing_lows else []
            hh_count = sum(1 for s in recent_highs if s.classification == SwingType.HH)
            ll_count = sum(1 for s in recent_lows if s.classification == SwingType.LL)
            if hh_count >= 2:
                return TrendState.BULLISH
            if ll_count >= 2:
                return TrendState.BEARISH
            return TrendState.RANGING

        # Use last structure event
        last = events[-1]
        if last.kind in (StructureBreakType.BULLISH_BOS, StructureBreakType.BULLISH_CHOCH):
            return TrendState.BULLISH
        if last.kind in (StructureBreakType.BEARISH_BOS, StructureBreakType.BEARISH_CHOCH):
            return TrendState.BEARISH
        return TrendState.RANGING

    def _detect_displacements(
        self, opens: np.ndarray, highs: np.ndarray, lows: np.ndarray,
        closes: np.ndarray, atr: float, timestamps: Optional[np.ndarray],
    ) -> List[Displacement]:
        """Find displacement candles (impulsive moves)."""
        disps = []
        n = len(closes)
        for i in range(1, n):
            body = abs(float(closes[i]) - float(opens[i]))
            total_range = float(highs[i]) - float(lows[i])
            if total_range <= 0:
                continue

            body_atr_ratio = body / max(atr, 1e-10)
            body_range_ratio = body / total_range

            if body_atr_ratio >= self.min_displacement_atr and body_range_ratio >= self.min_body_ratio:
                direction = "bullish" if closes[i] > opens[i] else "bearish"

                # Check if creates FVG
                creates_fvg = False
                if i >= 2:
                    if direction == "bullish" and float(lows[i]) > float(highs[i - 2]):
                        creates_fvg = True
                    elif direction == "bearish" and float(highs[i]) < float(lows[i - 2]):
                        creates_fvg = True

                disps.append(Displacement(
                    index=i, direction=direction,
                    body_size=body, body_atr_ratio=body_atr_ratio,
                    body_range_ratio=body_range_ratio,
                    creates_fvg=creates_fvg,
                ))
        return disps

    @staticmethod
    def _calc_atr(highs: np.ndarray, lows: np.ndarray, closes: np.ndarray, length: int = 14) -> float:
        """Calculate current ATR value."""
        n = len(highs)
        if n < length + 1:
            return float(np.mean(highs - lows)) if n > 0 else 0.001

        tr = np.maximum(
            highs[1:] - lows[1:],
            np.maximum(
                np.abs(highs[1:] - closes[:-1]),
                np.abs(lows[1:] - closes[:-1]),
            ),
        )
        # Wilder smoothing
        atr_val = float(np.mean(tr[-length:]))
        return max(atr_val, 1e-10)


# ─── Utility ─────────────────────────────────────────────────

def get_recent_choch(state: StructureState, max_bars_ago: int = 20, current_idx: int = -1) -> Optional[StructureBreak]:
    """Get most recent CHoCH within lookback."""
    for event in reversed(state.events):
        if current_idx > 0 and (current_idx - event.index) > max_bars_ago:
            break
        if event.kind in (StructureBreakType.BULLISH_CHOCH, StructureBreakType.BEARISH_CHOCH):
            return event
    return None


def get_recent_bos(state: StructureState, max_bars_ago: int = 20, current_idx: int = -1) -> Optional[StructureBreak]:
    """Get most recent BOS within lookback."""
    for event in reversed(state.events):
        if current_idx > 0 and (current_idx - event.index) > max_bars_ago:
            break
        if event.kind in (StructureBreakType.BULLISH_BOS, StructureBreakType.BEARISH_BOS):
            return event
    return None


def get_recent_displacement(state: StructureState, direction: str, max_bars_ago: int = 10, current_idx: int = -1) -> Optional[Displacement]:
    """Get most recent displacement in given direction."""
    for disp in reversed(state.displacements):
        if current_idx > 0 and (current_idx - disp.index) > max_bars_ago:
            break
        if disp.direction == direction:
            return disp
    return None
