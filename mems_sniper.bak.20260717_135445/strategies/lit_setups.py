"""LIT Setups - Three Rule-Based Trading Setups.

Implements the 3 core LIT setups + entry confirmation logic:
  1. Sweep-Reversal: liquidity swept + reclaim + displacement
  2. Inducement-Continuation: HTF bias + internal sweep + continuation break
  3. Range-to-Expansion: compression + liquidity build + breakout confirmation

Entry confirmation requirements (applies to all setups):
  - Reclaim of swept level (close back inside)
  - Displacement candle (body > 1.5x ATR)
  - FVG/Imbalance creation during displacement
  - Retest of FVG or order block zone

NO ENTRY without HTF bias alignment.

This is Layer 3+4 of the LIT architecture (setups + confirmation).
"""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Dict, List, Optional, Tuple

import numpy as np

from strategies.lit_structure import (
    StructureState, StructureEvent, StructureBreak,
    TrendState, SwingPoint, get_recent_bos, get_recent_choch,
)
from strategies.lit_liquidity import (
    LiquidityMap, LiquidityPool, SweepEvent, InducementZone,
    LiquiditySide, SweepQuality, get_recent_sweep, get_sweeps_by_quality,
)



# ─── Enums & Data Models ─────────────────────────────────────

class SetupType(str, Enum):
    SWEEP_REVERSAL = "sweep_reversal"
    INDUCEMENT_CONTINUATION = "inducement_continuation"
    RANGE_EXPANSION = "range_to_expansion"


class ConfirmationType(str, Enum):
    RECLAIM = "reclaim"
    DISPLACEMENT = "displacement"
    FVG_CREATED = "fvg_created"
    FVG_RETEST = "fvg_retest"
    OB_RETEST = "ob_retest"
    BOS_CONFIRMED = "bos_confirmed"


@dataclass
class FVGZone:
    """Fair Value Gap created during displacement."""
    top: float
    bottom: float
    direction: str       # "bullish" | "bearish"
    candle_index: int
    tested: bool = False
    fill_pct: float = 0.0


@dataclass
class EntryConfirmation:
    """Proof that entry conditions are met."""
    confirmations: List[ConfirmationType]
    displacement_strength: float   # body/ATR ratio of displacement candle
    fvg: Optional[FVGZone] = None
    retest_price: Optional[float] = None
    retest_quality: float = 0.0    # 0..1 how clean the retest was


@dataclass
class SetupCandidate:
    """A detected setup before final scoring/filtering."""
    setup_type: SetupType
    side: str                       # "long" | "short"
    entry_price: float
    stop_loss_price: float
    tp1_price: float
    tp2_price: float
    htf_bias: TrendState
    ltf_structure: TrendState
    sweep_event: Optional[SweepEvent] = None
    structure_event: Optional[StructureEvent] = None
    confirmation: Optional[EntryConfirmation] = None
    inducement: Optional[InducementZone] = None
    fvg_zones: List[FVGZone] = field(default_factory=list)
    reasoning_parts: List[str] = field(default_factory=list)
    raw_score: float = 0.0



# ─── Entry Confirmation Engine ───────────────────────────────

class ConfirmationEngine:
    """Validates that entry conditions are met after a setup is detected.
    
    Rules:
    - Reclaim: candle must close BACK inside after sweep
    - Displacement: aggressive candle (body > 1.5x ATR) in trade direction
    - FVG: imbalance zone created during displacement
    - Retest: price comes back to test FVG or displacement origin
    """

    def __init__(self, min_displacement_atr: float = 1.2, fvg_min_pct: float = 0.03):
        self.min_displacement_atr = min_displacement_atr
        self.fvg_min_pct = fvg_min_pct

    def check_confirmation(
        self,
        highs: np.ndarray,
        lows: np.ndarray,
        closes: np.ndarray,
        opens: np.ndarray,
        atr: float,
        sweep_index: int,
        expected_side: str,
    ) -> Optional[EntryConfirmation]:
        """Check for entry confirmation after a sweep event.
        
        Looks at candles AFTER the sweep for:
        1. Reclaim (same candle or next)
        2. Displacement candle
        3. FVG creation
        4. Retest of created FVG
        """
        n = len(closes)
        if sweep_index >= n - 1:
            return None

        confirmations: List[ConfirmationType] = []
        displacement_strength = 0.0
        fvg: Optional[FVGZone] = None
        retest_price: Optional[float] = None
        retest_quality = 0.0

        # Check candles after sweep (up to 5 bars)
        search_end = min(sweep_index + 6, n)

        # 1. Reclaim check (already handled by sweep detection, but verify)
        confirmations.append(ConfirmationType.RECLAIM)

        # 2. Displacement check
        for i in range(sweep_index, search_end):
            body = abs(float(closes[i]) - float(opens[i]))
            body_atr = body / max(atr, 1e-10)

            is_correct_direction = (
                (expected_side == "long" and closes[i] > opens[i]) or
                (expected_side == "short" and closes[i] < opens[i])
            )

            if body_atr >= self.min_displacement_atr and is_correct_direction:
                displacement_strength = body_atr
                confirmations.append(ConfirmationType.DISPLACEMENT)

                # 3. Check for FVG created by this displacement
                if i >= 2:
                    fvg = self._check_fvg_at(highs, lows, closes, opens, i, expected_side)
                    if fvg is not None:
                        confirmations.append(ConfirmationType.FVG_CREATED)
                break

        # 4. Retest check (price returns to FVG or displacement origin)
        if fvg is not None:
            for i in range(sweep_index + 2, search_end):
                if expected_side == "long":
                    # Bullish: price dips into FVG from above
                    if float(lows[i]) <= fvg.top and float(closes[i]) >= fvg.bottom:
                        retest_price = (fvg.top + fvg.bottom) / 2
                        fill = (fvg.top - float(lows[i])) / max(fvg.top - fvg.bottom, 1e-10)
                        retest_quality = min(fill, 1.0)
                        confirmations.append(ConfirmationType.FVG_RETEST)
                        fvg.tested = True
                        break
                else:
                    # Bearish: price rises into FVG from below
                    if float(highs[i]) >= fvg.bottom and float(closes[i]) <= fvg.top:
                        retest_price = (fvg.top + fvg.bottom) / 2
                        fill = (float(highs[i]) - fvg.bottom) / max(fvg.top - fvg.bottom, 1e-10)
                        retest_quality = min(fill, 1.0)
                        confirmations.append(ConfirmationType.FVG_RETEST)
                        fvg.tested = True
                        break

        if len(confirmations) < 2:
            return None  # Need at least reclaim + displacement

        return EntryConfirmation(
            confirmations=confirmations,
            displacement_strength=displacement_strength,
            fvg=fvg,
            retest_price=retest_price,
            retest_quality=retest_quality,
        )

    def _check_fvg_at(
        self,
        highs: np.ndarray,
        lows: np.ndarray,
        closes: np.ndarray,
        opens: np.ndarray,
        displacement_idx: int,
        side: str,
    ) -> Optional[FVGZone]:
        """Check if a displacement candle creates an FVG."""
        i = displacement_idx
        if i < 2:
            return None

        c1_high = float(highs[i - 2])
        c3_low = float(lows[i])
        c1_low = float(lows[i - 2])
        c3_high = float(highs[i])
        mid = (float(highs[i - 1]) + float(lows[i - 1])) / 2

        if side == "long":
            # Bullish FVG: gap between candle1.high and candle3.low
            if c3_low > c1_high:
                gap_pct = (c3_low - c1_high) / max(mid, 1e-10) * 100
                if gap_pct >= self.fvg_min_pct:
                    return FVGZone(
                        top=c3_low,
                        bottom=c1_high,
                        direction="bullish",
                        candle_index=i,
                    )
        else:
            # Bearish FVG: gap between candle3.high and candle1.low
            if c1_low > c3_high:
                gap_pct = (c1_low - c3_high) / max(mid, 1e-10) * 100
                if gap_pct >= self.fvg_min_pct:
                    return FVGZone(
                        top=c1_low,
                        bottom=c3_high,
                        direction="bearish",
                        candle_index=i,
                    )

        return None



# ─── Setup 1: Sweep-Reversal ────────────────────────────────

class SweepReversalSetup:
    """Sweep-Reversal: price sweeps major liquidity and reverses.
    
    LIT Cycle: Build → Induce (sweep) → Expand (reversal)
    
    Conditions:
    1. HTF bias exists (not ranging)
    2. Liquidity pool identified at key level
    3. Price sweeps the pool (wick through + close back)
    4. Displacement candle in reversal direction
    5. FVG created or structure break confirms
    
    Entry: On retest of FVG or displacement origin
    Stop: Beyond the sweep wick
    Target: Next liquidity pool on opposite side
    """

    def __init__(self, min_sweep_quality: SweepQuality = SweepQuality.MODERATE):
        self.min_sweep_quality = min_sweep_quality
        self.confirmation_engine = ConfirmationEngine()

    def detect(
        self,
        highs: np.ndarray,
        lows: np.ndarray,
        closes: np.ndarray,
        opens: np.ndarray,
        atr: float,
        htf_bias: TrendState,
        ltf_structure: StructureState,
        liq_map: LiquidityMap,
        current_price: float,
    ) -> Optional[SetupCandidate]:
        """Detect Sweep-Reversal setup."""
        # Gate: must have HTF bias
        if htf_bias == TrendState.RANGING:
            return None

        # Get recent sweeps that meet quality threshold
        quality_order = {SweepQuality.WEAK: 0, SweepQuality.MODERATE: 1, SweepQuality.STRONG: 2}
        min_q = quality_order[self.min_sweep_quality]
        valid_sweeps = [
            s for s in liq_map.sweeps
            if quality_order.get(s.quality, 0) >= min_q
        ]

        if not valid_sweeps:
            return None

        # Take the most recent valid sweep
        sweep = valid_sweeps[-1]

        # Alignment check: sweep must be AGAINST the HTF bias
        # (Smart money sweeps counter-trend liquidity then moves with trend)
        if htf_bias == TrendState.BULLISH and sweep.side != LiquiditySide.SELL_SIDE:
            return None  # In bullish bias, we want sell-side sweeps (longs)
        if htf_bias == TrendState.BEARISH and sweep.side != LiquiditySide.BUY_SIDE:
            return None  # In bearish bias, we want buy-side sweeps (shorts)

        # Determine side
        if sweep.side == LiquiditySide.SELL_SIDE:
            side = "long"
        else:
            side = "short"

        # Check entry confirmation
        confirmation = self.confirmation_engine.check_confirmation(
            highs, lows, closes, opens, atr, sweep.index, side
        )
        if confirmation is None:
            return None

        # Build entry/SL/TP
        reasoning = []

        if side == "long":
            entry = current_price
            # Stop below the sweep wick
            sl = float(np.min(lows[max(0, sweep.index - 1):sweep.index + 2])) - 0.2 * atr
            # TP1: nearest buy-side liquidity or 2:1 RR
            risk = entry - sl
            tp1 = entry + risk * 2.0
            tp2 = entry + risk * 3.0
            if liq_map.nearest_buy_side:
                tp1 = min(tp1, liq_map.nearest_buy_side.price)
                tp2 = tp1 + risk

            reasoning.append(f"HTF صعودی — sell-side نقدینگی شکار شد")
            reasoning.append(f"سطح: {sweep.pool.price:.4f} ({sweep.pool.kind.value})")
            reasoning.append(f"کیفیت sweep: {sweep.quality.value}")
            reasoning.append(f"Displacement: {confirmation.displacement_strength:.1f}x ATR")
        else:
            entry = current_price
            sl = float(np.max(highs[max(0, sweep.index - 1):sweep.index + 2])) + 0.2 * atr
            risk = sl - entry
            tp1 = entry - risk * 2.0
            tp2 = entry - risk * 3.0
            if liq_map.nearest_sell_side:
                tp1 = max(tp1, liq_map.nearest_sell_side.price)
                tp2 = tp1 - risk

            reasoning.append(f"HTF نزولی — buy-side نقدینگی شکار شد")
            reasoning.append(f"سطح: {sweep.pool.price:.4f} ({sweep.pool.kind.value})")
            reasoning.append(f"کیفیت sweep: {sweep.quality.value}")
            reasoning.append(f"Displacement: {confirmation.displacement_strength:.1f}x ATR")

        if confirmation.fvg:
            reasoning.append(f"FVG ایجاد شده: {confirmation.fvg.bottom:.4f}-{confirmation.fvg.top:.4f}")
        if ConfirmationType.FVG_RETEST in confirmation.confirmations:
            reasoning.append(f"✅ Retest تایید شد")

        return SetupCandidate(
            setup_type=SetupType.SWEEP_REVERSAL,
            side=side,
            entry_price=entry,
            stop_loss_price=sl,
            tp1_price=tp1,
            tp2_price=tp2,
            htf_bias=htf_bias,
            ltf_structure=ltf_structure.trend,
            sweep_event=sweep,
            confirmation=confirmation,
            fvg_zones=[confirmation.fvg] if confirmation.fvg else [],
            reasoning_parts=reasoning,
        )



# ─── Setup 2: Inducement-Continuation ───────────────────────

class InducementContinuationSetup:
    """Inducement-Continuation: pullback sweeps internal liquidity, then continues.
    
    LIT Cycle: Trend → Pullback → Induce (internal sweep) → Continue
    
    Conditions:
    1. HTF bias is clear (strong trend)
    2. Price pulls back against trend
    3. Internal (minor) liquidity is swept during pullback
    4. BOS in trend direction confirms continuation
    5. Entry on retest of the BOS level or FVG
    
    This is the most common institutional pattern:
    "Sweep internal liquidity to fill orders, then continue the trend"
    """

    def __init__(self):
        self.confirmation_engine = ConfirmationEngine(min_displacement_atr=1.0)

    def detect(
        self,
        highs: np.ndarray,
        lows: np.ndarray,
        closes: np.ndarray,
        opens: np.ndarray,
        atr: float,
        htf_bias: TrendState,
        ltf_structure: StructureState,
        liq_map: LiquidityMap,
        current_price: float,
    ) -> Optional[SetupCandidate]:
        """Detect Inducement-Continuation setup."""
        # Gate: must have clear HTF trend
        if htf_bias == TrendState.RANGING:
            return None

        # Need inducement zones detected
        if not liq_map.inducements:
            return None

        # Look for inducement that aligns with HTF
        target_inducement: Optional[InducementZone] = None
        for ind in reversed(liq_map.inducements):
            if htf_bias == TrendState.BULLISH and ind.trapped_direction == "short":
                # Bullish trend: shorts got trapped (sell-side induced) → continue up
                target_inducement = ind
                break
            elif htf_bias == TrendState.BEARISH and ind.trapped_direction == "long":
                # Bearish trend: longs got trapped (buy-side induced) → continue down
                target_inducement = ind
                break

        if target_inducement is None:
            return None

        # Need a recent BOS in the trend direction (continuation confirmed)
        recent_bos = get_recent_bos(ltf_structure, lookback_bars=10, current_idx=len(closes) - 1)
        if recent_bos is None:
            return None

        # BOS must align with HTF
        if htf_bias == TrendState.BULLISH and recent_bos.kind != StructureBreak.BULLISH_BOS:
            return None
        if htf_bias == TrendState.BEARISH and recent_bos.kind != StructureBreak.BEARISH_BOS:
            return None

        # Check displacement of the BOS candle
        if recent_bos.displacement < 1.0:
            return None  # Weak BOS, skip

        # Build setup
        reasoning = []
        n = len(closes)

        if htf_bias == TrendState.BULLISH:
            side = "long"
            # Entry at pullback to BOS level or FVG
            entry = current_price
            # Stop below inducement level
            sl = target_inducement.price - 0.5 * atr
            risk = entry - sl
            if risk <= 0:
                return None
            tp1 = entry + risk * 2.0
            tp2 = entry + risk * 3.5
            # If parent pool exists, use as target
            if target_inducement.parent_pool:
                tp2 = target_inducement.parent_pool.price

            reasoning.append("HTF صعودی — pullback internal liquidity را شکار کرد")
            reasoning.append(f"Inducement: {target_inducement.price:.4f}")
            reasoning.append(f"تله‌شدگان: فروشندگان (shortها trap شدند)")
            reasoning.append(f"BOS صعودی تایید: displacement {recent_bos.displacement:.1f}x")
            reasoning.append("ادامه روند صعودی انتظار می‌رود")
        else:
            side = "short"
            entry = current_price
            sl = target_inducement.price + 0.5 * atr
            risk = sl - entry
            if risk <= 0:
                return None
            tp1 = entry - risk * 2.0
            tp2 = entry - risk * 3.5
            if target_inducement.parent_pool:
                tp2 = target_inducement.parent_pool.price

            reasoning.append("HTF نزولی — pullback internal liquidity را شکار کرد")
            reasoning.append(f"Inducement: {target_inducement.price:.4f}")
            reasoning.append(f"تله‌شدگان: خریداران (longها trap شدند)")
            reasoning.append(f"BOS نزولی تایید: displacement {recent_bos.displacement:.1f}x")
            reasoning.append("ادامه روند نزولی انتظار می‌رود")

        return SetupCandidate(
            setup_type=SetupType.INDUCEMENT_CONTINUATION,
            side=side,
            entry_price=entry,
            stop_loss_price=sl,
            tp1_price=tp1,
            tp2_price=tp2,
            htf_bias=htf_bias,
            ltf_structure=ltf_structure.trend,
            structure_event=recent_bos,
            inducement=target_inducement,
            reasoning_parts=reasoning,
        )



# ─── Setup 3: Range-to-Expansion ────────────────────────────

class RangeExpansionSetup:
    """Range-to-Expansion: consolidation builds liquidity, then explodes.
    
    LIT Cycle: Accumulate/Distribute → Build liquidity → Sweep one side → Expand other
    
    Conditions:
    1. Price has been ranging (low ATR, tight closes)
    2. Liquidity builds on BOTH sides of range (equal highs + equal lows)
    3. One side gets swept (fake breakout)
    4. CHoCH or BOS confirms expansion direction
    5. Entry after reclaim with displacement
    
    This captures the classic "stop hunt before the real move" pattern.
    """

    def __init__(self, range_min_bars: int = 15, compression_threshold: float = 0.6):
        self.range_min_bars = range_min_bars
        self.compression_threshold = compression_threshold
        self.confirmation_engine = ConfirmationEngine()

    def detect(
        self,
        highs: np.ndarray,
        lows: np.ndarray,
        closes: np.ndarray,
        opens: np.ndarray,
        atr: float,
        htf_bias: TrendState,
        ltf_structure: StructureState,
        liq_map: LiquidityMap,
        current_price: float,
    ) -> Optional[SetupCandidate]:
        """Detect Range-to-Expansion setup."""
        n = len(closes)
        if n < self.range_min_bars + 10:
            return None

        # Step 1: Detect range/compression
        is_ranging, range_high, range_low = self._detect_compression(
            highs, lows, closes, atr, n
        )
        if not is_ranging:
            return None

        # Step 2: Need liquidity on at least one side (preferably both)
        has_buy_liq = len(liq_map.buy_side_pools) > 0
        has_sell_liq = len(liq_map.sell_side_pools) > 0
        if not (has_buy_liq or has_sell_liq):
            return None

        # Step 3: Check for sweep of one side (fake breakout)
        recent_sweeps = [s for s in liq_map.sweeps if s.index >= n - 8]
        if not recent_sweeps:
            return None

        sweep = recent_sweeps[-1]

        # Step 4: Determine expansion direction
        # If sell-side swept → expansion UP (bullish)
        # If buy-side swept → expansion DOWN (bearish)
        if sweep.side == LiquiditySide.SELL_SIDE:
            expansion_side = "long"
        else:
            expansion_side = "short"

        # Step 5: HTF alignment preferred (but ranging HTF is acceptable for this setup)
        if htf_bias == TrendState.BULLISH and expansion_side == "short":
            return None  # Don't fight strong HTF
        if htf_bias == TrendState.BEARISH and expansion_side == "long":
            return None

        # Step 6: Confirmation
        confirmation = self.confirmation_engine.check_confirmation(
            highs, lows, closes, opens, atr, sweep.index, expansion_side
        )
        if confirmation is None:
            return None

        # Build setup
        reasoning = []
        range_size = range_high - range_low

        if expansion_side == "long":
            entry = current_price
            sl = range_low - 0.3 * atr  # Below range low
            risk = entry - sl
            if risk <= 0:
                return None
            # Target: range size projection above range high
            tp1 = range_high + range_size * 0.5
            tp2 = range_high + range_size * 1.0
            # If buy-side pools exist, use them as targets
            if liq_map.buy_side_pools:
                tp1 = min(tp1, liq_map.buy_side_pools[0].price)

            reasoning.append("Range شناسایی شد — liquidity هر دو طرف ساخته شده")
            reasoning.append(f"Range: {range_low:.4f} — {range_high:.4f}")
            reasoning.append(f"Sell-side sweep انجام شد (fake breakdown)")
            reasoning.append(f"Expansion صعودی تایید: displacement {confirmation.displacement_strength:.1f}x")
            reasoning.append("انتظار: خروج از بالای range")
        else:
            entry = current_price
            sl = range_high + 0.3 * atr
            risk = sl - entry
            if risk <= 0:
                return None
            tp1 = range_low - range_size * 0.5
            tp2 = range_low - range_size * 1.0
            if liq_map.sell_side_pools:
                tp1 = max(tp1, liq_map.sell_side_pools[0].price)

            reasoning.append("Range شناسایی شد — liquidity هر دو طرف ساخته شده")
            reasoning.append(f"Range: {range_low:.4f} — {range_high:.4f}")
            reasoning.append(f"Buy-side sweep انجام شد (fake breakout)")
            reasoning.append(f"Expansion نزولی تایید: displacement {confirmation.displacement_strength:.1f}x")
            reasoning.append("انتظار: خروج از پایین range")

        return SetupCandidate(
            setup_type=SetupType.RANGE_EXPANSION,
            side=expansion_side,
            entry_price=entry,
            stop_loss_price=sl,
            tp1_price=tp1,
            tp2_price=tp2,
            htf_bias=htf_bias,
            ltf_structure=ltf_structure.trend,
            sweep_event=sweep,
            confirmation=confirmation,
            fvg_zones=[confirmation.fvg] if confirmation.fvg else [],
            reasoning_parts=reasoning,
        )

    def _detect_compression(
        self,
        highs: np.ndarray,
        lows: np.ndarray,
        closes: np.ndarray,
        atr: float,
        n: int,
    ) -> Tuple[bool, float, float]:
        """Detect if price is in a compression/range phase.
        
        Returns (is_ranging, range_high, range_low).
        """
        lookback = min(self.range_min_bars, n - 1)
        recent_highs = highs[-lookback:]
        recent_lows = lows[-lookback:]
        recent_closes = closes[-lookback:]

        range_high = float(np.max(recent_highs))
        range_low = float(np.min(recent_lows))
        range_size = range_high - range_low

        if range_size <= 0 or atr <= 0:
            return False, 0.0, 0.0

        # Compression = range is tight relative to ATR
        # Normal range = ~3-5x ATR over the lookback
        # Compressed = < 2x ATR
        range_atr_ratio = range_size / atr
        if range_atr_ratio > 3.5:
            return False, 0.0, 0.0  # Not compressed enough

        # Additional check: closes cluster near middle
        mid = (range_high + range_low) / 2
        close_deviation = float(np.std(recent_closes)) / max(range_size, 1e-10)
        if close_deviation > self.compression_threshold:
            return False, 0.0, 0.0  # Closes are too spread out

        return True, range_high, range_low



# ─── Setup Detector (orchestrates all 3 setups) ─────────────

class SetupDetector:
    """Runs all 3 LIT setups and returns the best candidate.
    
    Priority order:
    1. Sweep-Reversal (highest edge when quality is strong)
    2. Range-to-Expansion (clear structure, tight stops)
    3. Inducement-Continuation (trend following, safest)
    """

    def __init__(self, config: Dict = None):
        cfg = config or {}
        self.sweep_reversal = SweepReversalSetup(
            min_sweep_quality=SweepQuality.MODERATE,
        )
        self.inducement_continuation = InducementContinuationSetup()
        self.range_expansion = RangeExpansionSetup(
            range_min_bars=int(cfg.get("range_min_bars", 15)),
            compression_threshold=float(cfg.get("compression_threshold", 0.6)),
        )

    def detect_all(
        self,
        highs: np.ndarray,
        lows: np.ndarray,
        closes: np.ndarray,
        opens: np.ndarray,
        atr: float,
        htf_bias: TrendState,
        ltf_structure: StructureState,
        liq_map: LiquidityMap,
        current_price: float,
    ) -> List[SetupCandidate]:
        """Run all setups and return all valid candidates."""
        candidates: List[SetupCandidate] = []

        # Setup 1: Sweep-Reversal
        sr = self.sweep_reversal.detect(
            highs, lows, closes, opens, atr,
            htf_bias, ltf_structure, liq_map, current_price,
        )
        if sr is not None:
            candidates.append(sr)

        # Setup 2: Inducement-Continuation
        ic = self.inducement_continuation.detect(
            highs, lows, closes, opens, atr,
            htf_bias, ltf_structure, liq_map, current_price,
        )
        if ic is not None:
            candidates.append(ic)

        # Setup 3: Range-to-Expansion
        re = self.range_expansion.detect(
            highs, lows, closes, opens, atr,
            htf_bias, ltf_structure, liq_map, current_price,
        )
        if re is not None:
            candidates.append(re)

        return candidates
