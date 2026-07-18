"""LIT Liquidity Engine — Liquidity Pool Detection & Sweep Analysis.

Detects resting liquidity and sweeps:
  - Equal highs/lows (clustered stops)
  - Previous day high/low
  - Session highs/lows (Asia/London/NY)
  - Swing highs/lows
  - Sweep detection with quality classification
  - Reclaim tracking

Only uses confirmed closed candles.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import List, Optional, Set

import numpy as np

from strategies.lit_structure import SwingPoint, StructureState


# ─── Enums ───────────────────────────────────────────────────

class LiquidityType(str, Enum):
    EQUAL_HIGH = "equal_high"
    EQUAL_LOW = "equal_low"
    SWING_HIGH = "swing_high"
    SWING_LOW = "swing_low"
    PDH = "previous_day_high"
    PDL = "previous_day_low"
    SESSION_HIGH = "session_high"
    SESSION_LOW = "session_low"


class LiquiditySide(str, Enum):
    BUY_SIDE = "buy_side"    # Above price (targets for bearish)
    SELL_SIDE = "sell_side"  # Below price (targets for bullish)


class SweepQuality(str, Enum):
    MAJOR = "major"      # Swept significant multi-touch level
    MEDIUM = "medium"    # Swept decent level with reclaim
    MINOR = "minor"      # Just barely swept


# ─── Data Models ─────────────────────────────────────────────

@dataclass
class LiquidityPool:
    """A zone where resting liquidity accumulates."""
    price: float
    kind: LiquidityType
    side: LiquiditySide
    strength: int            # number of touches/confirmations
    timeframe_source: str    # which TF identified this
    index_formed: int
    index_last_touch: int
    swept: bool = False
    swept_at_index: int = -1
    timestamp: float = 0.0


@dataclass
class SweepEvent:
    """A confirmed liquidity sweep."""
    index: int               # candle that swept
    pool: LiquidityPool      # which pool was swept
    side: LiquiditySide      # sell_side or buy_side
    quality: SweepQuality
    penetration: float       # how far past the level (in price)
    penetration_atr: float   # penetration / ATR
    reclaimed: bool          # did price close back inside?
    reclaim_index: int       # which candle reclaimed
    max_excursion: float     # max distance past level
    direction: str           # "bullish_sweep" (swept lows) or "bearish_sweep" (swept highs)
    timestamp: float = 0.0


@dataclass
class LiquidityMap:
    """Complete liquidity landscape."""
    buy_side_pools: List[LiquidityPool]   # Above current price
    sell_side_pools: List[LiquidityPool]  # Below current price
    sweeps: List[SweepEvent]
    nearest_buy_target: Optional[LiquidityPool] = None
    nearest_sell_target: Optional[LiquidityPool] = None


# ─── Liquidity Engine ────────────────────────────────────────

class LiquidityEngine:
    """Detects liquidity pools and sweeps."""

    def __init__(
        self,
        equal_tolerance_pct: float = 0.1,
        min_touches: int = 2,
        sweep_penetration_min_atr: float = 0.2,
        max_age_bars_after_sweep: int = 10,
    ):
        self.equal_tolerance_pct = equal_tolerance_pct
        self.min_touches = min_touches
        self.sweep_penetration_min_atr = sweep_penetration_min_atr
        self.max_age_bars = max_age_bars_after_sweep

    def analyze(
        self,
        opens: np.ndarray,
        highs: np.ndarray,
        lows: np.ndarray,
        closes: np.ndarray,
        structure: StructureState,
        atr: float,
        current_price: float,
        timeframe: str = "15m",
        timestamps: Optional[np.ndarray] = None,
    ) -> LiquidityMap:
        """Build complete liquidity map."""
        n = len(closes)

        # Find all pools
        pools: List[LiquidityPool] = []
        pools.extend(self._find_equal_highs(highs, n, timeframe))
        pools.extend(self._find_equal_lows(lows, n, timeframe))
        pools.extend(self._pools_from_swings(structure, timeframe))

        # Deduplicate
        pools = self._deduplicate(pools, atr)

        # Detect sweeps
        sweeps = self._detect_sweeps(pools, opens, highs, lows, closes, atr, n, timestamps)

        # Classify by side relative to current price
        buy_side = sorted(
            [p for p in pools if p.price > current_price and p.side == LiquiditySide.BUY_SIDE],
            key=lambda p: p.price
        )
        sell_side = sorted(
            [p for p in pools if p.price < current_price and p.side == LiquiditySide.SELL_SIDE],
            key=lambda p: p.price, reverse=True
        )

        return LiquidityMap(
            buy_side_pools=buy_side,
            sell_side_pools=sell_side,
            sweeps=sweeps,
            nearest_buy_target=buy_side[0] if buy_side else None,
            nearest_sell_target=sell_side[0] if sell_side else None,
        )

    def _find_equal_highs(self, highs: np.ndarray, n: int, tf: str) -> List[LiquidityPool]:
        """Find equal highs (buy-side liquidity)."""
        pools = []
        window = min(60, n)
        recent = highs[-window:]
        tol = self.equal_tolerance_pct / 100.0

        checked: Set[int] = set()
        for i in range(len(recent)):
            if i in checked:
                continue
            cluster = [i]
            for j in range(i + 1, len(recent)):
                if j in checked:
                    continue
                if abs(recent[i] - recent[j]) / max(recent[i], 1e-10) <= tol:
                    cluster.append(j)
                    checked.add(j)
            if len(cluster) >= self.min_touches:
                level = float(np.mean([recent[idx] for idx in cluster]))
                offset = n - window
                pools.append(LiquidityPool(
                    price=level, kind=LiquidityType.EQUAL_HIGH,
                    side=LiquiditySide.BUY_SIDE,
                    strength=len(cluster), timeframe_source=tf,
                    index_formed=offset + cluster[0],
                    index_last_touch=offset + cluster[-1],
                ))
        return pools

    def _find_equal_lows(self, lows: np.ndarray, n: int, tf: str) -> List[LiquidityPool]:
        """Find equal lows (sell-side liquidity)."""
        pools = []
        window = min(60, n)
        recent = lows[-window:]
        tol = self.equal_tolerance_pct / 100.0

        checked: Set[int] = set()
        for i in range(len(recent)):
            if i in checked:
                continue
            cluster = [i]
            for j in range(i + 1, len(recent)):
                if j in checked:
                    continue
                if abs(recent[i] - recent[j]) / max(recent[i], 1e-10) <= tol:
                    cluster.append(j)
                    checked.add(j)
            if len(cluster) >= self.min_touches:
                level = float(np.mean([recent[idx] for idx in cluster]))
                offset = n - window
                pools.append(LiquidityPool(
                    price=level, kind=LiquidityType.EQUAL_LOW,
                    side=LiquiditySide.SELL_SIDE,
                    strength=len(cluster), timeframe_source=tf,
                    index_formed=offset + cluster[0],
                    index_last_touch=offset + cluster[-1],
                ))
        return pools

    def _pools_from_swings(self, structure: StructureState, tf: str) -> List[LiquidityPool]:
        """Convert swing points to liquidity pools."""
        pools = []
        for sh in structure.swing_highs:
            if sh.valid:
                pools.append(LiquidityPool(
                    price=sh.price, kind=LiquidityType.SWING_HIGH,
                    side=LiquiditySide.BUY_SIDE,
                    strength=max(1, sh.left_bars // 2),
                    timeframe_source=tf,
                    index_formed=sh.index, index_last_touch=sh.index,
                    timestamp=sh.timestamp,
                ))
        for sl in structure.swing_lows:
            if sl.valid:
                pools.append(LiquidityPool(
                    price=sl.price, kind=LiquidityType.SWING_LOW,
                    side=LiquiditySide.SELL_SIDE,
                    strength=max(1, sl.left_bars // 2),
                    timeframe_source=tf,
                    index_formed=sl.index, index_last_touch=sl.index,
                    timestamp=sl.timestamp,
                ))
        return pools

    def _detect_sweeps(
        self, pools: List[LiquidityPool],
        opens: np.ndarray, highs: np.ndarray, lows: np.ndarray, closes: np.ndarray,
        atr: float, n: int, timestamps: Optional[np.ndarray],
    ) -> List[SweepEvent]:
        """Detect sweep events on liquidity pools."""
        sweeps = []
        lookback = min(self.max_age_bars, n - 1)

        for pool in pools:
            if pool.swept:
                continue

            for i in range(n - lookback, n):
                sweep = self._check_sweep(pool, opens, highs, lows, closes, i, atr, n, timestamps)
                if sweep:
                    pool.swept = True
                    pool.swept_at_index = i
                    sweeps.append(sweep)
                    break

        return sweeps

    def _check_sweep(
        self, pool: LiquidityPool,
        opens: np.ndarray, highs: np.ndarray, lows: np.ndarray, closes: np.ndarray,
        idx: int, atr: float, n: int, timestamps: Optional[np.ndarray],
    ) -> Optional[SweepEvent]:
        """Check if candle at idx sweeps the pool, AND that the reversal
        actually held for at least 1 subsequent candle (a same-candle close
        back over the level is easy to fake with a single wick — real
        institutional sweeps are confirmed by price staying on the reclaimed
        side afterward, not immediately continuing through)."""
        h = float(highs[idx])
        l = float(lows[idx])
        c = float(closes[idx])
        o = float(opens[idx])

        # A subsequent candle to confirm the reclaim held, if one exists yet
        # within the data (idx+1 may not exist for the very last candle —
        # in that case we accept the same-candle reclaim as provisional,
        # since we can't demand data from the future).
        confirm_idx = idx + 1 if idx + 1 < n else None

        if pool.side == LiquiditySide.BUY_SIDE:
            # Buy-side sweep: wick ABOVE pool, close BELOW
            if h > pool.price and c < pool.price:
                penetration = h - pool.price
                pen_atr = penetration / max(atr, 1e-10)
                if pen_atr < self.sweep_penetration_min_atr:
                    return None

                reclaimed = c < pool.price
                # Confirmation: the NEXT candle must not re-break back above
                # the pool level (i.e. the sweep must actually hold).
                if confirm_idx is not None:
                    next_high = float(highs[confirm_idx])
                    if next_high > pool.price:
                        return None  # reversal failed to hold -> not a real sweep
                quality = self._grade_sweep_quality(pool, pen_atr)
                ts = float(timestamps[idx]) if timestamps is not None else 0.0

                return SweepEvent(
                    index=idx, pool=pool, side=LiquiditySide.BUY_SIDE,
                    quality=quality, penetration=penetration,
                    penetration_atr=pen_atr, reclaimed=reclaimed,
                    reclaim_index=idx, max_excursion=penetration,
                    direction="bearish_sweep", timestamp=ts,
                )

        elif pool.side == LiquiditySide.SELL_SIDE:
            # Sell-side sweep: wick BELOW pool, close ABOVE
            if l < pool.price and c > pool.price:
                penetration = pool.price - l
                pen_atr = penetration / max(atr, 1e-10)
                if pen_atr < self.sweep_penetration_min_atr:
                    return None

                reclaimed = c > pool.price
                if confirm_idx is not None:
                    next_low = float(lows[confirm_idx])
                    if next_low < pool.price:
                        return None  # reversal failed to hold -> not a real sweep
                quality = self._grade_sweep_quality(pool, pen_atr)
                ts = float(timestamps[idx]) if timestamps is not None else 0.0

                return SweepEvent(
                    index=idx, pool=pool, side=LiquiditySide.SELL_SIDE,
                    quality=quality, penetration=penetration,
                    penetration_atr=pen_atr, reclaimed=reclaimed,
                    reclaim_index=idx, max_excursion=penetration,
                    direction="bullish_sweep", timestamp=ts,
                )

        return None

    def _grade_sweep_quality(self, pool: LiquidityPool, pen_atr: float) -> SweepQuality:
        """Grade sweep quality based on pool significance and penetration."""
        if pool.strength >= 3 and pen_atr >= 0.5:
            return SweepQuality.MAJOR
        if pool.strength >= 2 or pen_atr >= 0.4:
            return SweepQuality.MEDIUM
        return SweepQuality.MINOR

    def _deduplicate(self, pools: List[LiquidityPool], atr: float) -> List[LiquidityPool]:
        """Remove overlapping pools, keep strongest."""
        if not pools:
            return []
        threshold = atr * 0.3  # Pools within 0.3 ATR are duplicates
        sorted_pools = sorted(pools, key=lambda p: p.price)
        result = [sorted_pools[0]]
        for pool in sorted_pools[1:]:
            if abs(pool.price - result[-1].price) < threshold:
                if pool.strength > result[-1].strength:
                    result[-1] = pool
            else:
                result.append(pool)
        return result
