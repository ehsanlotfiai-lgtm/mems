"""LIT Liquidity Engine - Liquidity Pool Detection & Sweep Analysis.

Identifies where resting liquidity accumulates and detects when it gets swept:
  - Equal highs/lows (clustered stop-losses)
  - Session highs/lows (Asian/London/NY session extremes)
  - Swing liquidity (above/below swing points)
  - Stop clusters (dense zones of likely stops)
  - Sweep detection (wick through + close back inside)
  - Inducement detection (minor sweep that traps traders)

This is Layer 2 of the LIT architecture.
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Dict, List, Optional, Tuple

import numpy as np

from strategies.lit_structure import SwingPoint, StructureState


# ─── Enums & Data Models ─────────────────────────────────────

class LiquidityType(str, Enum):
    EQUAL_HIGH = "equal_high"
    EQUAL_LOW = "equal_low"
    SWING_HIGH = "swing_high"
    SWING_LOW = "swing_low"
    SESSION_HIGH = "session_high"
    SESSION_LOW = "session_low"
    RANGE_HIGH = "range_high"
    RANGE_LOW = "range_low"


class LiquiditySide(str, Enum):
    BUY_SIDE = "buy_side"    # Liquidity above price (buy stops)
    SELL_SIDE = "sell_side"  # Liquidity below price (sell stops)


class SweepQuality(str, Enum):
    STRONG = "strong"      # Deep wick + close fully reclaimed + displacement
    MODERATE = "moderate"  # Wick pierced + close back inside
    WEAK = "weak"          # Just barely swept, no real displacement


@dataclass
class LiquidityPool:
    """A zone where resting liquidity (stops) accumulates."""
    price: float
    kind: LiquidityType
    side: LiquiditySide
    strength: int           # touches / confirmations
    index_formed: int       # bar index when first detected
    index_last_touch: int   # last bar that touched/respected this level
    swept: bool = False
    swept_at_index: int = -1
    sweep_quality: Optional[SweepQuality] = None
    sweep_wick_size: float = 0.0  # how far price went past the level
    reclaimed: bool = False       # did price close back inside after sweep?


@dataclass
class SweepEvent:
    """A confirmed liquidity sweep event."""
    index: int              # bar where sweep happened
    pool: LiquidityPool     # which liquidity was swept
    wick_depth: float       # how far past the level (in price)
    wick_depth_atr: float   # wick depth normalized by ATR
    reclaim_candle: bool    # did the same candle close back inside?
    displacement_after: float  # displacement strength after sweep (0 if none yet)
    quality: SweepQuality
    side: LiquiditySide


@dataclass
class InducementZone:
    """A minor internal liquidity sweep that traps traders.
    
    Inducement = a small sweep of internal liquidity BEFORE the real move.
    Smart money uses this to build positions against trapped traders.
    """
    index: int
    price: float
    side: LiquiditySide    # which side was induced
    trapped_direction: str  # "long" or "short" - the trapped traders
    parent_pool: Optional[LiquidityPool] = None  # the REAL target
    triggered: bool = False


@dataclass
class LiquidityMap:
    """Complete liquidity landscape at a given moment."""
    buy_side_pools: List[LiquidityPool]   # Above price (targets for bearish moves)
    sell_side_pools: List[LiquidityPool]  # Below price (targets for bullish moves)
    sweeps: List[SweepEvent]
    inducements: List[InducementZone]
    nearest_buy_side: Optional[LiquidityPool] = None
    nearest_sell_side: Optional[LiquidityPool] = None


# ─── Liquidity Engine ────────────────────────────────────────

class LiquidityEngine:
    """Detects liquidity pools, sweeps, and inducement zones.
    
    Core principles:
    - Liquidity = clusters of stop-loss orders
    - Equal highs/lows = obvious stop clusters (easy targets)
    - Smart money sweeps liquidity to fill large orders
    - After a sweep, price should reverse (if legitimate)
    - Inducement = mini-sweep that traps before real move
    """

    def __init__(
        self,
        equal_tolerance_pct: float = 0.08,
        min_touches: int = 2,
        sweep_min_wick_atr: float = 0.3,
        strong_sweep_wick_atr: float = 0.7,
        session_bars_lookback: int = 48,  # ~12h on 15m
    ):
        self.equal_tolerance_pct = equal_tolerance_pct
        self.min_touches = min_touches
        self.sweep_min_wick_atr = sweep_min_wick_atr
        self.strong_sweep_wick_atr = strong_sweep_wick_atr
        self.session_bars_lookback = session_bars_lookback

    def analyze(
        self,
        highs: np.ndarray,
        lows: np.ndarray,
        closes: np.ndarray,
        opens: np.ndarray,
        structure: StructureState,
        atr: float,
        current_price: float,
    ) -> LiquidityMap:
        """Build complete liquidity map.
        
        Args:
            highs/lows/closes/opens: OHLC data
            structure: output from StructureEngine
            atr: current ATR value
            current_price: latest close
        """
        n = len(highs)

        # Step 1: Identify all liquidity pools
        pools = self._find_all_pools(highs, lows, closes, structure, n)

        # Step 2: Detect sweeps on each pool
        sweeps = self._detect_sweeps(pools, highs, lows, closes, opens, atr, n)

        # Step 3: Detect inducement zones
        inducements = self._detect_inducements(pools, sweeps, highs, lows, closes, atr, n)

        # Step 4: Classify into buy/sell side relative to current price
        buy_side = [p for p in pools if p.price > current_price and p.side == LiquiditySide.BUY_SIDE]
        sell_side = [p for p in pools if p.price < current_price and p.side == LiquiditySide.SELL_SIDE]

        # Sort by distance to current price
        buy_side.sort(key=lambda p: p.price - current_price)
        sell_side.sort(key=lambda p: current_price - p.price)

        nearest_buy = buy_side[0] if buy_side else None
        nearest_sell = sell_side[0] if sell_side else None

        return LiquidityMap(
            buy_side_pools=buy_side,
            sell_side_pools=sell_side,
            sweeps=sweeps,
            inducements=inducements,
            nearest_buy_side=nearest_buy,
            nearest_sell_side=nearest_sell,
        )

    # ─── Pool Detection ──────────────────────────────────────

    def _find_all_pools(
        self,
        highs: np.ndarray,
        lows: np.ndarray,
        closes: np.ndarray,
        structure: StructureState,
        n: int,
    ) -> List[LiquidityPool]:
        """Find all types of liquidity pools."""
        pools: List[LiquidityPool] = []

        # 1. Equal highs (buy-side liquidity)
        pools.extend(self._find_equal_levels(highs, "high", n))

        # 2. Equal lows (sell-side liquidity)
        pools.extend(self._find_equal_levels(lows, "low", n))

        # 3. Swing-based liquidity from structure
        pools.extend(self._pools_from_structure(structure))

        # 4. Range extremes (if price is ranging)
        pools.extend(self._find_range_liquidity(highs, lows, closes, n))

        # Deduplicate nearby pools
        pools = self._deduplicate_pools(pools)

        return pools

    def _find_equal_levels(
        self, data: np.ndarray, kind: str, n: int
    ) -> List[LiquidityPool]:
        """Find equal highs or equal lows (clustered stops)."""
        pools = []
        # Use rolling window to find clusters
        window = min(50, n)
        recent = data[-window:]

        # Find prices that appear multiple times within tolerance
        for i in range(len(recent)):
            count = 0
            indices = [i]
            for j in range(i + 1, len(recent)):
                if abs(recent[i] - recent[j]) / max(recent[i], 1e-10) < self.equal_tolerance_pct / 100:
                    count += 1
                    indices.append(j)

            if count >= self.min_touches - 1:
                level_price = float(np.mean([recent[idx] for idx in indices]))
                offset = n - window

                if kind == "high":
                    pool = LiquidityPool(
                        price=level_price,
                        kind=LiquidityType.EQUAL_HIGH,
                        side=LiquiditySide.BUY_SIDE,
                        strength=count + 1,
                        index_formed=offset + indices[0],
                        index_last_touch=offset + indices[-1],
                    )
                else:
                    pool = LiquidityPool(
                        price=level_price,
                        kind=LiquidityType.EQUAL_LOW,
                        side=LiquiditySide.SELL_SIDE,
                        strength=count + 1,
                        index_formed=offset + indices[0],
                        index_last_touch=offset + indices[-1],
                    )
                pools.append(pool)

        return pools

    def _pools_from_structure(self, structure: StructureState) -> List[LiquidityPool]:
        """Convert structural swing points to liquidity pools."""
        pools = []

        for sh in structure.swing_highs:
            if sh.valid:  # Only unbroken swings hold liquidity
                pools.append(LiquidityPool(
                    price=sh.price,
                    kind=LiquidityType.SWING_HIGH,
                    side=LiquiditySide.BUY_SIDE,
                    strength=sh.strength,
                    index_formed=sh.index,
                    index_last_touch=sh.index,
                ))

        for sl in structure.swing_lows:
            if sl.valid:
                pools.append(LiquidityPool(
                    price=sl.price,
                    kind=LiquidityType.SWING_LOW,
                    side=LiquiditySide.SELL_SIDE,
                    strength=sl.strength,
                    index_formed=sl.index,
                    index_last_touch=sl.index,
                ))

        return pools

    def _find_range_liquidity(
        self,
        highs: np.ndarray,
        lows: np.ndarray,
        closes: np.ndarray,
        n: int,
    ) -> List[LiquidityPool]:
        """Detect range boundaries as liquidity pools.
        
        If price has been consolidating, the range high/low become
        strong liquidity targets.
        """
        pools = []
        lookback = min(self.session_bars_lookback, n)
        recent_highs = highs[-lookback:]
        recent_lows = lows[-lookback:]
        recent_closes = closes[-lookback:]

        # Check if price is ranging (ATR compression)
        range_high = float(np.max(recent_highs))
        range_low = float(np.min(recent_lows))
        range_size = range_high - range_low
        mid_price = (range_high + range_low) / 2

        if range_size <= 0 or mid_price <= 0:
            return pools

        # Range ratio: how much of the range is actually used
        close_std = float(np.std(recent_closes))
        if close_std / mid_price < 0.02:  # Tight consolidation
            offset = n - lookback
            pools.append(LiquidityPool(
                price=range_high,
                kind=LiquidityType.RANGE_HIGH,
                side=LiquiditySide.BUY_SIDE,
                strength=3,
                index_formed=offset,
                index_last_touch=n - 1,
            ))
            pools.append(LiquidityPool(
                price=range_low,
                kind=LiquidityType.RANGE_LOW,
                side=LiquiditySide.SELL_SIDE,
                strength=3,
                index_formed=offset,
                index_last_touch=n - 1,
            ))

        return pools

    # ─── Sweep Detection ─────────────────────────────────────

    def _detect_sweeps(
        self,
        pools: List[LiquidityPool],
        highs: np.ndarray,
        lows: np.ndarray,
        closes: np.ndarray,
        opens: np.ndarray,
        atr: float,
        n: int,
    ) -> List[SweepEvent]:
        """Detect sweep events on each liquidity pool.
        
        A sweep = price wicks THROUGH a level but closes BACK inside.
        This shows smart money collecting stops then reversing.
        """
        sweeps = []
        lookback = min(10, n)  # Only check recent candles

        for pool in pools:
            if pool.swept:
                continue

            for i in range(n - lookback, n):
                sweep = self._check_single_sweep(pool, highs, lows, closes, opens, i, atr)
                if sweep is not None:
                    pool.swept = True
                    pool.swept_at_index = i
                    pool.sweep_quality = sweep.quality
                    pool.reclaimed = sweep.reclaim_candle
                    sweeps.append(sweep)
                    break  # One sweep per pool

        return sweeps

    def _check_single_sweep(
        self,
        pool: LiquidityPool,
        highs: np.ndarray,
        lows: np.ndarray,
        closes: np.ndarray,
        opens: np.ndarray,
        idx: int,
        atr: float,
    ) -> Optional[SweepEvent]:
        """Check if a specific candle sweeps a liquidity pool."""
        h = float(highs[idx])
        l = float(lows[idx])
        c = float(closes[idx])
        o = float(opens[idx])

        if atr <= 0:
            return None

        if pool.side == LiquiditySide.BUY_SIDE:
            # Buy-side sweep: wick ABOVE the level, close BELOW
            if h > pool.price and c < pool.price:
                wick_depth = h - pool.price
                wick_atr = wick_depth / atr

                if wick_atr < self.sweep_min_wick_atr:
                    return None

                # Determine quality
                reclaim = c < pool.price  # Closed back below
                if wick_atr >= self.strong_sweep_wick_atr and reclaim:
                    quality = SweepQuality.STRONG
                elif reclaim:
                    quality = SweepQuality.MODERATE
                else:
                    quality = SweepQuality.WEAK

                # Check for displacement after sweep (next 1-3 candles)
                displacement = 0.0
                for j in range(idx + 1, min(idx + 4, len(closes))):
                    body = abs(float(closes[j]) - float(opens[j]))
                    disp_candidate = body / atr
                    if float(closes[j]) < float(opens[j]):  # Bearish candle after buy-side sweep
                        displacement = max(displacement, disp_candidate)

                return SweepEvent(
                    index=idx,
                    pool=pool,
                    wick_depth=wick_depth,
                    wick_depth_atr=wick_atr,
                    reclaim_candle=reclaim,
                    displacement_after=displacement,
                    quality=quality,
                    side=LiquiditySide.BUY_SIDE,
                )

        elif pool.side == LiquiditySide.SELL_SIDE:
            # Sell-side sweep: wick BELOW the level, close ABOVE
            if l < pool.price and c > pool.price:
                wick_depth = pool.price - l
                wick_atr = wick_depth / atr

                if wick_atr < self.sweep_min_wick_atr:
                    return None

                reclaim = c > pool.price
                if wick_atr >= self.strong_sweep_wick_atr and reclaim:
                    quality = SweepQuality.STRONG
                elif reclaim:
                    quality = SweepQuality.MODERATE
                else:
                    quality = SweepQuality.WEAK

                displacement = 0.0
                for j in range(idx + 1, min(idx + 4, len(closes))):
                    body = abs(float(closes[j]) - float(opens[j]))
                    disp_candidate = body / atr
                    if float(closes[j]) > float(opens[j]):  # Bullish candle after sell-side sweep
                        displacement = max(displacement, disp_candidate)

                return SweepEvent(
                    index=idx,
                    pool=pool,
                    wick_depth=wick_depth,
                    wick_depth_atr=wick_atr,
                    reclaim_candle=reclaim,
                    displacement_after=displacement,
                    quality=quality,
                    side=LiquiditySide.SELL_SIDE,
                )

        return None

    # ─── Inducement Detection ────────────────────────────────

    def _detect_inducements(
        self,
        pools: List[LiquidityPool],
        sweeps: List[SweepEvent],
        highs: np.ndarray,
        lows: np.ndarray,
        closes: np.ndarray,
        atr: float,
        n: int,
    ) -> List[InducementZone]:
        """Detect inducement zones.
        
        Inducement = a small internal liquidity sweep that:
        1. Breaks a minor swing (not the main liquidity)
        2. Traps breakout traders
        3. Then reverses toward the REAL liquidity target
        
        Pattern: Minor sweep → trap → move to major target
        """
        inducements = []

        # Find minor pools (low strength, recent)
        minor_pools = [p for p in pools if p.strength <= 2 and not p.swept]
        major_pools = [p for p in pools if p.strength >= 3]

        for minor in minor_pools:
            # Check if there's a major pool in the OPPOSITE direction
            if minor.side == LiquiditySide.BUY_SIDE:
                # Minor buy-side pool broken → traps longs
                # Real target should be sell-side (below)
                real_targets = [m for m in major_pools if m.side == LiquiditySide.SELL_SIDE]
                if real_targets:
                    inducements.append(InducementZone(
                        index=minor.index_last_touch,
                        price=minor.price,
                        side=LiquiditySide.BUY_SIDE,
                        trapped_direction="long",
                        parent_pool=real_targets[0],
                    ))
            else:
                real_targets = [m for m in major_pools if m.side == LiquiditySide.BUY_SIDE]
                if real_targets:
                    inducements.append(InducementZone(
                        index=minor.index_last_touch,
                        price=minor.price,
                        side=LiquiditySide.SELL_SIDE,
                        trapped_direction="short",
                        parent_pool=real_targets[0],
                    ))

        return inducements

    # ─── Utilities ───────────────────────────────────────────

    def _deduplicate_pools(self, pools: List[LiquidityPool]) -> List[LiquidityPool]:
        """Remove duplicate/overlapping pools, keeping strongest."""
        if not pools:
            return []

        sorted_pools = sorted(pools, key=lambda p: p.price)
        result = [sorted_pools[0]]

        for pool in sorted_pools[1:]:
            prev = result[-1]
            # If within 0.2% of previous, keep the stronger one
            if abs(pool.price - prev.price) / max(prev.price, 1e-10) < 0.002:
                if pool.strength > prev.strength:
                    result[-1] = pool
            else:
                result.append(pool)

        return result


# ─── Helper Functions ────────────────────────────────────────

def get_recent_sweep(liq_map: LiquidityMap, lookback_bars: int = 5, current_idx: int = -1) -> Optional[SweepEvent]:
    """Get the most recent sweep within lookback window."""
    for sweep in reversed(liq_map.sweeps):
        if current_idx > 0 and (current_idx - sweep.index) > lookback_bars:
            break
        return sweep
    return None


def get_sweeps_by_quality(liq_map: LiquidityMap, min_quality: SweepQuality = SweepQuality.MODERATE) -> List[SweepEvent]:
    """Filter sweeps by minimum quality."""
    quality_order = {SweepQuality.WEAK: 0, SweepQuality.MODERATE: 1, SweepQuality.STRONG: 2}
    min_val = quality_order[min_quality]
    return [s for s in liq_map.sweeps if quality_order.get(s.quality, 0) >= min_val]


def has_buy_side_liquidity(liq_map: LiquidityMap) -> bool:
    """Check if there's meaningful buy-side liquidity above."""
    return len(liq_map.buy_side_pools) > 0


def has_sell_side_liquidity(liq_map: LiquidityMap) -> bool:
    """Check if there's meaningful sell-side liquidity below."""
    return len(liq_map.sell_side_pools) > 0
