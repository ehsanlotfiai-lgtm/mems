"""LIT Patterns — FVG, Order Block, and Setup Detection.

Implements:
  - Fair Value Gap (FVG) detection and fill tracking
  - Order Block (OB) detection
  - Three LIT setups: Sweep-Reversal, Inducement-Continuation, Range-Expansion
  - Entry confirmation logic
"""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import List, Optional

import numpy as np

from strategies.lit_structure import (
    StructureState, StructureBreak, StructureBreakType,
    Displacement, TrendState, get_recent_choch, get_recent_bos, get_recent_displacement,
)
from strategies.lit_liquidity import (
    LiquidityMap, LiquidityPool, SweepEvent, SweepQuality, LiquiditySide,
)


# ─── Enums ───────────────────────────────────────────────────

class SetupType(str, Enum):
    SWEEP_REVERSAL = "sweep_reversal"
    INDUCEMENT_CONTINUATION = "inducement_continuation"
    RANGE_EXPANSION = "range_expansion"
    NONE = "none"


class EntryMode(str, Enum):
    AGGRESSIVE = "aggressive"      # After CHoCH + displacement + FVG retest
    CONSERVATIVE = "conservative"  # After CHoCH + retrace + BOS + retest
    NONE = "none"


class SignalStatus(str, Enum):
    READY = "ready"        # All confirmations aligned, ready to trade
    WATCHLIST = "watchlist" # Partial setup, watching for completion
    INVALID = "invalid"    # Failed quality filters


# ─── Data Models ─────────────────────────────────────────────

@dataclass
class FVG:
    """Fair Value Gap (3-candle imbalance)."""
    top: float
    bottom: float
    midpoint: float
    direction: str          # "bullish" | "bearish"
    index: int              # candle that created it
    filled: bool = False
    fill_pct: float = 0.0
    mitigated: bool = False
    timestamp: float = 0.0

    @property
    def size(self) -> float:
        return abs(self.top - self.bottom)


@dataclass
class OrderBlock:
    """Institutional Order Block."""
    top: float
    bottom: float
    direction: str          # "bullish" | "bearish"
    index: int
    displacement_size: float
    tested: bool = False
    mitigated: bool = False
    timestamp: float = 0.0


@dataclass
class SetupCandidate:
    """A detected LIT setup before final validation."""
    setup_type: SetupType
    side: str               # "long" | "short"
    entry_mode: EntryMode
    status: SignalStatus
    # Components
    sweep: Optional[SweepEvent] = None
    structure_break: Optional[StructureBreak] = None
    displacement: Optional[Displacement] = None
    fvg: Optional[FVG] = None
    order_block: Optional[OrderBlock] = None
    # Context
    htf_bias: TrendState = TrendState.RANGING
    entry_tf_structure: TrendState = TrendState.RANGING
    # Reasons and flags
    reasons: List[str] = field(default_factory=list)
    risk_flags: List[str] = field(default_factory=list)
    # Target
    target_pool: Optional[LiquidityPool] = None


# ─── FVG Detector ────────────────────────────────────────────

class FVGDetector:
    """Detect Fair Value Gaps."""

    def __init__(self, min_gap_pct: float = 0.03):
        self.min_gap_pct = min_gap_pct

    def find_fvgs(
        self, opens: np.ndarray, highs: np.ndarray, lows: np.ndarray, closes: np.ndarray,
        timestamps: Optional[np.ndarray] = None,
    ) -> List[FVG]:
        """Find all FVGs in data."""
        fvgs = []
        n = len(opens)
        for i in range(2, n):
            mid = (float(highs[i-1]) + float(lows[i-1])) / 2
            if mid <= 0:
                continue

            # Bullish FVG: candle[i].low > candle[i-2].high
            c1_high = float(highs[i - 2])
            c3_low = float(lows[i])
            if c3_low > c1_high:
                gap_pct = (c3_low - c1_high) / mid * 100
                if gap_pct >= self.min_gap_pct:
                    ts = float(timestamps[i]) if timestamps is not None else 0.0
                    fvgs.append(FVG(
                        top=c3_low, bottom=c1_high,
                        midpoint=(c3_low + c1_high) / 2,
                        direction="bullish", index=i, timestamp=ts,
                    ))

            # Bearish FVG: candle[i-2].low > candle[i].high
            c1_low = float(lows[i - 2])
            c3_high = float(highs[i])
            if c1_low > c3_high:
                gap_pct = (c1_low - c3_high) / mid * 100
                if gap_pct >= self.min_gap_pct:
                    ts = float(timestamps[i]) if timestamps is not None else 0.0
                    fvgs.append(FVG(
                        top=c1_low, bottom=c3_high,
                        midpoint=(c1_low + c3_high) / 2,
                        direction="bearish", index=i, timestamp=ts,
                    ))
        return fvgs

    def get_unmitigated_fvg(self, fvgs: List[FVG], direction: str, current_price: float) -> Optional[FVG]:
        """Get nearest unmitigated FVG in given direction relative to price."""
        candidates = [f for f in fvgs if f.direction == direction and not f.mitigated]
        if not candidates:
            return None
        if direction == "bullish":
            # Bullish FVG below price (for long entry on retest)
            below = [f for f in candidates if f.top <= current_price]
            return max(below, key=lambda f: f.bottom) if below else None
        else:
            # Bearish FVG above price (for short entry on retest)
            above = [f for f in candidates if f.bottom >= current_price]
            return min(above, key=lambda f: f.top) if above else None

    def check_price_in_fvg(self, fvg: FVG, price: float) -> bool:
        """Check if price is currently in or touching FVG."""
        return fvg.bottom <= price <= fvg.top


# ─── Order Block Detector ────────────────────────────────────

class OBDetector:
    """Detect Order Blocks."""

    def __init__(self, min_displacement_ratio: float = 1.5):
        self.min_displacement_ratio = min_displacement_ratio

    def find_order_blocks(
        self, opens: np.ndarray, highs: np.ndarray, lows: np.ndarray, closes: np.ndarray,
        timestamps: Optional[np.ndarray] = None,
    ) -> List[OrderBlock]:
        """Find order blocks: last opposing candle before strong displacement."""
        obs = []
        n = len(opens)
        avg_range = np.zeros(n)
        for i in range(20, n):
            avg_range[i] = float(np.mean(highs[i-20:i] - lows[i-20:i]))

        for i in range(2, n):
            if avg_range[i] <= 0:
                continue
            total_range = float(highs[i]) - float(lows[i])
            disp_ratio = total_range / avg_range[i]
            if disp_ratio < self.min_displacement_ratio:
                continue

            body = abs(float(closes[i]) - float(opens[i]))
            body_ratio = body / max(total_range, 1e-10)
            if body_ratio < 0.6:
                continue

            is_bullish = closes[i] > opens[i]
            is_bearish = closes[i] < opens[i]

            if is_bullish:
                # Look back for last bearish candle = bullish OB
                for j in range(i - 1, max(i - 5, 0), -1):
                    if closes[j] < opens[j]:
                        ts = float(timestamps[j]) if timestamps is not None else 0.0
                        obs.append(OrderBlock(
                            top=float(highs[j]), bottom=float(lows[j]),
                            direction="bullish", index=j,
                            displacement_size=disp_ratio, timestamp=ts,
                        ))
                        break
            elif is_bearish:
                for j in range(i - 1, max(i - 5, 0), -1):
                    if closes[j] > opens[j]:
                        ts = float(timestamps[j]) if timestamps is not None else 0.0
                        obs.append(OrderBlock(
                            top=float(highs[j]), bottom=float(lows[j]),
                            direction="bearish", index=j,
                            displacement_size=disp_ratio, timestamp=ts,
                        ))
                        break
        return obs


# ─── Setup Detector ──────────────────────────────────────────

class SetupDetector:
    """Detects the 3 LIT setups and validates them."""

    def __init__(self, config: dict = None):
        cfg = config or {}
        self.fvg_detector = FVGDetector(min_gap_pct=float(cfg.get("min_fvg_pct", 0.03)))
        self.ob_detector = OBDetector(min_displacement_ratio=float(cfg.get("min_ob_displacement", 1.5)))
        self.require_fvg = bool(cfg.get("require_fvg", True))
        self.require_ob_confluence = bool(cfg.get("require_ob_confluence", False))
        self.conservative_requires_bos = bool(cfg.get("conservative_requires_bos", False))
        self.max_age_bars = int(cfg.get("max_age_bars_after_sweep", 20))
        self.signal_mode = cfg.get("signal_mode", "strict")

    def detect(
        self,
        opens: np.ndarray, highs: np.ndarray, lows: np.ndarray, closes: np.ndarray,
        htf_bias: TrendState,
        entry_structure: StructureState,
        liq_map: LiquidityMap,
        atr: float,
        current_price: float,
        timestamps: Optional[np.ndarray] = None,
    ) -> Optional[SetupCandidate]:
        """Run all setup detectors and return best candidate."""
        n = len(closes)

        # Get FVGs and OBs
        fvgs = self.fvg_detector.find_fvgs(opens, highs, lows, closes, timestamps)
        obs = self.ob_detector.find_order_blocks(opens, highs, lows, closes, timestamps)

        # Try each setup in priority order
        candidate = self._try_sweep_reversal(
            htf_bias, entry_structure, liq_map, fvgs, obs, atr, current_price, n
        )
        if candidate and candidate.status == SignalStatus.READY:
            return candidate

        candidate = self._try_inducement_continuation(
            htf_bias, entry_structure, liq_map, fvgs, obs, atr, current_price, n
        )
        if candidate and candidate.status == SignalStatus.READY:
            return candidate

        candidate = self._try_range_expansion(
            opens, highs, lows, closes, htf_bias, entry_structure, liq_map, fvgs, obs, atr, current_price, n
        )
        if candidate and candidate.status == SignalStatus.READY:
            return candidate

        return None

    def _try_sweep_reversal(
        self, htf_bias: TrendState, structure: StructureState,
        liq_map: LiquidityMap, fvgs: List[FVG], obs: List[OrderBlock],
        atr: float, price: float, n: int,
    ) -> Optional[SetupCandidate]:
        """Detect Sweep-Reversal setup."""
        if not liq_map.sweeps:
            return None

        for sweep in reversed(liq_map.sweeps):
            # Check age
            if (n - 1 - sweep.index) > self.max_age_bars:
                continue

            # Determine direction
            if sweep.direction == "bullish_sweep":
                # Swept sell-side → expect bullish reversal
                side = "long"
                needed_structure = (StructureBreakType.BULLISH_CHOCH, StructureBreakType.BULLISH_BOS)
                needed_disp = "bullish"
                needed_fvg = "bullish"
                target_pools = liq_map.buy_side_pools
            else:
                side = "short"
                needed_structure = (StructureBreakType.BEARISH_CHOCH, StructureBreakType.BEARISH_BOS)
                needed_disp = "bearish"
                needed_fvg = "bearish"
                target_pools = liq_map.sell_side_pools

            # Gate: HTF alignment for reversals must be strong OR neutral
            if side == "long" and htf_bias == TrendState.BEARISH:
                continue
            if side == "short" and htf_bias == TrendState.BULLISH:
                continue

            reasons = []
            risk_flags = []
            reasons.append(f"{'Sell' if side=='long' else 'Buy'}-side liquidity swept ({sweep.pool.kind.value})")
            reasons.append(f"Sweep quality: {sweep.quality.value}")

            # Check structure confirmation (CHoCH or BOS after sweep)
            struct_confirmed = False
            struct_event = None
            for event in structure.events:
                if event.index > sweep.index and event.kind in needed_structure:
                    struct_confirmed = True
                    struct_event = event
                    reasons.append(f"{event.kind.value} confirmed at bar {event.index}")
                    break

            if not struct_confirmed:
                continue

            # Check displacement after sweep
            disp = get_recent_displacement(structure, needed_disp, max_bars_ago=self.max_age_bars, current_idx=n-1)
            if disp is None:
                risk_flags.append("No displacement after sweep")
                continue
            if disp.index < sweep.index:
                continue
            reasons.append(f"Displacement: {disp.body_atr_ratio:.1f}x ATR, body ratio: {disp.body_range_ratio:.0%}")

            # Check FVG
            fvg = self.fvg_detector.get_unmitigated_fvg(fvgs, needed_fvg, price)
            if fvg is None:
                continue
            reasons.append(f"FVG zone: {fvg.bottom:.6g} — {fvg.top:.6g}")

            # Check OB confluence (optional)
            ob_match = None
            if obs:
                for ob in reversed(obs):
                    if ob.direction == needed_fvg and not ob.mitigated:
                        ob_match = ob
                        break
            if ob_match:
                reasons.append(f"OB confluence: {ob_match.bottom:.6g} — {ob_match.top:.6g}")

            # Determine entry mode
            has_bos = any(
                e.kind in (StructureBreakType.BULLISH_BOS, StructureBreakType.BEARISH_BOS)
                for e in structure.events if e.index > sweep.index
            )
            if has_bos:
                entry_mode = EntryMode.CONSERVATIVE
            else:
                entry_mode = EntryMode.AGGRESSIVE

            # Target
            target = target_pools[0] if target_pools else None
            if target:
                reasons.append(f"Target: {target.kind.value} at {target.price:.6g}")

            return SetupCandidate(
                setup_type=SetupType.SWEEP_REVERSAL,
                side=side,
                entry_mode=entry_mode,
                status=SignalStatus.READY,
                sweep=sweep,
                structure_break=struct_event,
                displacement=disp,
                fvg=fvg,
                order_block=ob_match,
                htf_bias=htf_bias,
                entry_tf_structure=structure.trend,
                reasons=reasons,
                risk_flags=risk_flags,
                target_pool=target,
            )
        return None

    def _try_inducement_continuation(
        self, htf_bias: TrendState, structure: StructureState,
        liq_map: LiquidityMap, fvgs: List[FVG], obs: List[OrderBlock],
        atr: float, price: float, n: int,
    ) -> Optional[SetupCandidate]:
        """Detect Inducement-Continuation: HTF trend + internal sweep + BOS."""
        if htf_bias == TrendState.RANGING:
            return None
        if not liq_map.sweeps:
            return None

        for sweep in reversed(liq_map.sweeps):
            if (n - 1 - sweep.index) > self.max_age_bars:
                continue

            # For bullish continuation: need sell-side sweep (pullback swept internal lows)
            if htf_bias == TrendState.BULLISH and sweep.direction != "bullish_sweep":
                continue
            if htf_bias == TrendState.BEARISH and sweep.direction != "bearish_sweep":
                continue

            side = "long" if htf_bias == TrendState.BULLISH else "short"
            needed_bos = StructureBreakType.BULLISH_BOS if side == "long" else StructureBreakType.BEARISH_BOS
            needed_disp = "bullish" if side == "long" else "bearish"
            needed_fvg = "bullish" if side == "long" else "bearish"

            reasons = [f"HTF {htf_bias.value} — continuation setup"]
            reasons.append(f"Internal liquidity swept ({sweep.pool.kind.value})")

            # Require BOS (continuation confirmation)
            bos = get_recent_bos(structure, max_bars_ago=self.max_age_bars, current_idx=n-1)
            if bos is None or bos.kind != needed_bos:
                continue
            if bos.index < sweep.index:
                continue
            reasons.append(f"BOS confirmed at bar {bos.index}")

            # Displacement
            disp = get_recent_displacement(structure, needed_disp, max_bars_ago=self.max_age_bars, current_idx=n-1)
            if disp is None:
                continue
            reasons.append(f"Displacement: {disp.body_atr_ratio:.1f}x ATR")

            # FVG for entry
            fvg = self.fvg_detector.get_unmitigated_fvg(fvgs, needed_fvg, price)
            if fvg is None:
                continue
            reasons.append(f"Entry FVG: {fvg.bottom:.6g} — {fvg.top:.6g}")

            # Target: external liquidity in trend direction
            if side == "long":
                target_pools = liq_map.buy_side_pools
            else:
                target_pools = liq_map.sell_side_pools
            target = target_pools[0] if target_pools else None
            if target:
                reasons.append(f"Target: external {target.kind.value} at {target.price:.6g}")

            return SetupCandidate(
                setup_type=SetupType.INDUCEMENT_CONTINUATION,
                side=side,
                entry_mode=EntryMode.CONSERVATIVE,
                status=SignalStatus.READY,
                sweep=sweep,
                structure_break=bos,
                displacement=disp,
                fvg=fvg,
                htf_bias=htf_bias,
                entry_tf_structure=structure.trend,
                reasons=reasons,
                target_pool=target,
            )
        return None

    def _try_range_expansion(
        self, opens: np.ndarray, highs: np.ndarray, lows: np.ndarray, closes: np.ndarray,
        htf_bias: TrendState, structure: StructureState, liq_map: LiquidityMap,
        fvgs: List[FVG], obs: List[OrderBlock], atr: float, price: float, n: int,
    ) -> Optional[SetupCandidate]:
        """Detect Range-Expansion: consolidation → sweep one side → expand other."""
        # Detect range
        lookback = min(30, n - 1)
        recent_h = highs[-lookback:]
        recent_l = lows[-lookback:]
        range_high = float(np.max(recent_h))
        range_low = float(np.min(recent_l))
        range_size = range_high - range_low

        if range_size <= 0 or atr <= 0:
            return None
        # Must be compressed (range < 4x ATR for the lookback)
        if range_size > 4 * atr:
            return None

        # Need a sweep of one side
        if not liq_map.sweeps:
            return None

        sweep = liq_map.sweeps[-1]
        if (n - 1 - sweep.index) > self.max_age_bars:
            return None

        reasons = [f"Range detected: {range_low:.6g} — {range_high:.6g}"]
        reasons.append(f"Range/ATR ratio: {range_size/atr:.1f}")

        # Determine expansion direction
        if sweep.direction == "bullish_sweep":
            side = "long"
            reasons.append("Sell-side of range swept — expecting bullish expansion")
        else:
            side = "short"
            reasons.append("Buy-side of range swept — expecting bearish expansion")

        # Need displacement confirmation
        needed_disp = "bullish" if side == "long" else "bearish"
        disp = get_recent_displacement(structure, needed_disp, max_bars_ago=8, current_idx=n-1)
        if disp is None:
            return None
        reasons.append(f"Expansion displacement: {disp.body_atr_ratio:.1f}x ATR")

        # FVG
        needed_fvg = "bullish" if side == "long" else "bearish"
        fvg = self.fvg_detector.get_unmitigated_fvg(fvgs, needed_fvg, price)
        if fvg is None:
            return None
        reasons.append(f"FVG for entry: {fvg.bottom:.6g} — {fvg.top:.6g}")

        # Target: opposite side of range or external liquidity
        if side == "long":
            target_pools = liq_map.buy_side_pools
        else:
            target_pools = liq_map.sell_side_pools
        target = target_pools[0] if target_pools else None

        return SetupCandidate(
            setup_type=SetupType.RANGE_EXPANSION,
            side=side,
            entry_mode=EntryMode.AGGRESSIVE,
            status=SignalStatus.READY,
            sweep=sweep,
            displacement=disp,
            fvg=fvg,
            htf_bias=htf_bias,
            entry_tf_structure=structure.trend,
            reasons=reasons,
            target_pool=target,
        )
