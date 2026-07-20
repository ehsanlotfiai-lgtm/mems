"""LIT Execution Engine — Entry/Exit/Risk Computation.

Handles:
  - Entry zone calculation (FVG midpoint, OB zone)
  - Stop-loss placement (structural + ATR buffer)
  - Take-profit levels (TP1/TP2/TP3 from liquidity targets)
  - Risk:Reward validation
  - Position sizing
  - Scoring model
  - No-trade filters
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, List, Optional

import numpy as np

from strategies.lit_structure import TrendState, StructureBreakType
from strategies.lit_liquidity import LiquidityMap, SweepQuality
from strategies.lit_patterns import SetupCandidate, SetupType, EntryMode, SignalStatus, FVG


# ─── Data Models ─────────────────────────────────────────────

@dataclass
class ExecutionPlan:
    """Complete execution plan for a signal."""
    entry_zone_low: float
    entry_zone_high: float
    ideal_entry: float
    market_price: float
    stop_loss: float
    stop_loss_buffered: float
    stop_distance_pct: float
    stop_distance_atr: float
    take_profit_1: float
    take_profit_2: float
    take_profit_3: float
    rr_tp1: float
    rr_tp2: float
    rr_tp3: float
    invalidation_level: float
    position_size_usdt: float
    leverage_suggested: int
    partials: Dict[str, float] = field(default_factory=lambda: {"tp1": 0.5, "tp2": 0.3, "tp3": 0.2})
    is_valid: bool = True
    rejection_reason: str = ""


@dataclass
class ScoreBreakdown:
    """Detailed confidence score breakdown."""
    htf_alignment: float = 0.0
    liquidity_quality: float = 0.0
    sweep_quality: float = 0.0
    displacement_strength: float = 0.0
    structure_confirmation: float = 0.0
    fvg_ob_confluence: float = 0.0
    rr_quality: float = 0.0
    session_quality: float = 0.0
    invalidation_cleanliness: float = 0.0
    # Penalties
    choppy_penalty: float = 0.0
    weak_reclaim_penalty: float = 0.0
    conflicting_bias_penalty: float = 0.0
    # Final
    total: float = 0.0
    explanation: List[str] = field(default_factory=list)


# ─── Execution Engine ────────────────────────────────────────

class ExecutionEngine:
    """Computes entry/exit levels, validates R:R, scores setup quality."""

    def __init__(self, config: dict = None):
        cfg = config or {}
        self.min_rr = float(cfg.get("min_rr", 2.0))
        self.atr_stop_buffer_mult = float(cfg.get("atr_stop_buffer_mult", 0.2))
        self.risk_pct = float(cfg.get("risk_per_trade_pct", 1.0))
        self.balance = float(cfg.get("initial_balance", 10000))
        self.min_score = float(cfg.get("min_score", 0.50))

    def compute(
        self,
        candidate: SetupCandidate,
        atr: float,
        current_price: float,
        liq_map: LiquidityMap,
    ) -> ExecutionPlan:
        """Compute full execution plan for a setup candidate."""
        side = candidate.side

        # ── Entry Zone ──
        entry_zone_low, entry_zone_high, ideal_entry = self._calc_entry_zone(candidate, current_price)

        # ── Stop Loss ──
        raw_stop = self._calc_stop_loss(candidate, atr, side)
        buffer = atr * self.atr_stop_buffer_mult
        if side == "long":
            stop_buffered = raw_stop - buffer
        else:
            stop_buffered = raw_stop + buffer

        # ── CRITICAL: Force SL on correct side of ideal_entry ──
        min_sl_pct = 0.005  # Minimum 0.5% distance
        min_sl_distance = ideal_entry * min_sl_pct
        if side == "long":
            # SL MUST be BELOW entry for long
            if stop_buffered >= ideal_entry:
                stop_buffered = ideal_entry - max(1.5 * atr, min_sl_distance)
            # Enforce minimum distance
            if (ideal_entry - stop_buffered) < min_sl_distance:
                stop_buffered = ideal_entry - min_sl_distance
        else:
            # SL MUST be ABOVE entry for short
            if stop_buffered <= ideal_entry:
                stop_buffered = ideal_entry + max(1.5 * atr, min_sl_distance)
            # Enforce minimum distance
            if (stop_buffered - ideal_entry) < min_sl_distance:
                stop_buffered = ideal_entry + min_sl_distance

        # ── Risk Distance ──
        risk_distance = abs(ideal_entry - stop_buffered)
        if risk_distance <= 0:
            return self._invalid("Risk distance <= 0")

        # ── Minimum distance check (must cover 3x commission) ──
        commission_pct = 0.075  # Binance futures taker fee per side
        min_profit_pct = commission_pct * 2 * 3  # Need 3x round-trip commission = 0.45%
        min_risk_distance = ideal_entry * (min_profit_pct / 100) / self.min_rr
        if risk_distance < min_risk_distance:
            # Widen SL to minimum viable distance
            if side == "long":
                stop_buffered = ideal_entry - min_risk_distance
            else:
                stop_buffered = ideal_entry + min_risk_distance
            risk_distance = min_risk_distance

        stop_pct = (risk_distance / max(ideal_entry, 1e-10)) * 100
        stop_atr = risk_distance / max(atr, 1e-10)

        # ── Take Profits ──
        tp1, tp2, tp3 = self._calc_targets(candidate, ideal_entry, risk_distance, liq_map, side)

        # ── R:R Ratios ──
        rr1 = abs(tp1 - ideal_entry) / max(risk_distance, 1e-10)
        rr2 = abs(tp2 - ideal_entry) / max(risk_distance, 1e-10)
        rr3 = abs(tp3 - ideal_entry) / max(risk_distance, 1e-10)

        # ── R:R Validation ──
        if rr2 < self.min_rr:
            return self._invalid(f"RR to TP2 ({rr2:.1f}) < min ({self.min_rr})")

        # ── Commission profitability check ──
        tp1_profit_pct = abs(tp1 - ideal_entry) / max(ideal_entry, 1e-10) * 100
        round_trip_commission_pct = commission_pct * 2  # 0.15%
        if tp1_profit_pct <= round_trip_commission_pct * 2:
            return self._invalid(f"TP1 profit ({tp1_profit_pct:.3f}%) too small vs commission ({round_trip_commission_pct:.3f}%)")

        # ── Position Sizing ──
        risk_amount = self.balance * (self.risk_pct / 100.0)
        risk_fraction = risk_distance / max(ideal_entry, 1e-10)
        position_size = risk_amount / max(risk_fraction, 1e-10)

        # ── Leverage ──
        leverage = self._suggest_leverage(rr2, stop_atr, candidate)

        # ── Invalidation ──
        invalidation = stop_buffered  # Beyond this, setup is invalid

        return ExecutionPlan(
            entry_zone_low=entry_zone_low,
            entry_zone_high=entry_zone_high,
            ideal_entry=ideal_entry,
            market_price=current_price,
            stop_loss=raw_stop,
            stop_loss_buffered=stop_buffered,
            stop_distance_pct=round(stop_pct, 3),
            stop_distance_atr=round(stop_atr, 2),
            take_profit_1=tp1,
            take_profit_2=tp2,
            take_profit_3=tp3,
            rr_tp1=round(rr1, 2),
            rr_tp2=round(rr2, 2),
            rr_tp3=round(rr3, 2),
            invalidation_level=invalidation,
            position_size_usdt=round(position_size, 2),
            leverage_suggested=leverage,
        )

    def score(
        self, candidate: SetupCandidate, plan: ExecutionPlan, atr: float,
        volume_ratio: Optional[float] = None, choppiness: Optional[float] = None,
    ) -> ScoreBreakdown:
        """Score the setup quality (0-1).

        volume_ratio: displacement candle's volume / recent average volume
            (None if volume data unavailable — falls back to neutral score).
        choppiness: recent price range / ATR over a short lookback — high
            values mean the market has been range-bound/noisy right before
            this signal, which historically produces more false breakouts.
        """
        s = ScoreBreakdown()

        # HTF alignment (0.20 weight)
        if candidate.htf_bias != TrendState.RANGING:
            if (candidate.side == "long" and candidate.htf_bias == TrendState.BULLISH) or \
               (candidate.side == "short" and candidate.htf_bias == TrendState.BEARISH):
                s.htf_alignment = 1.0
                s.explanation.append("HTF fully aligned")
            elif candidate.htf_bias == TrendState.RANGING:
                s.htf_alignment = 0.5
            else:
                s.htf_alignment = 0.2
                s.conflicting_bias_penalty = 0.1
                s.explanation.append("HTF conflicting — penalty applied")
        else:
            s.htf_alignment = 0.5

        # Liquidity quality (0.15 weight)
        if candidate.sweep and candidate.sweep.pool:
            strength = min(candidate.sweep.pool.strength / 4.0, 1.0)
            s.liquidity_quality = strength
            s.explanation.append(f"Pool strength: {candidate.sweep.pool.strength}")
        else:
            s.liquidity_quality = 0.3

        # Sweep quality (0.15 weight)
        if candidate.sweep:
            quality_map = {SweepQuality.MAJOR: 1.0, SweepQuality.MEDIUM: 0.7, SweepQuality.MINOR: 0.4}
            s.sweep_quality = quality_map.get(candidate.sweep.quality, 0.3)
            s.explanation.append(f"Sweep: {candidate.sweep.quality.value}")
        else:
            s.sweep_quality = 0.3

        # Displacement (0.15 weight)
        if candidate.displacement:
            disp_score = min(candidate.displacement.body_atr_ratio / 2.5, 1.0)
            s.displacement_strength = disp_score
            s.explanation.append(f"Displacement: {candidate.displacement.body_atr_ratio:.1f}x ATR")
        else:
            s.displacement_strength = 0.2

        # Structure confirmation (0.15 weight)
        if candidate.structure_break:
            if candidate.structure_break.kind in (StructureBreakType.BULLISH_BOS, StructureBreakType.BEARISH_BOS):
                s.structure_confirmation = 0.9
                s.explanation.append("BOS confirmed")
            else:
                s.structure_confirmation = 0.7
                s.explanation.append("CHoCH confirmed")
        else:
            s.structure_confirmation = 0.3

        # FVG/OB confluence (0.10 weight)
        has_fvg = candidate.fvg is not None
        has_ob = candidate.order_block is not None
        if has_fvg and has_ob:
            s.fvg_ob_confluence = 1.0
            s.explanation.append("FVG + OB confluence")
        elif has_fvg:
            s.fvg_ob_confluence = 0.7
            s.explanation.append("FVG present")
        elif has_ob:
            s.fvg_ob_confluence = 0.6
        else:
            s.fvg_ob_confluence = 0.3

        # RR quality (0.05 weight)
        s.rr_quality = min(plan.rr_tp2 / 4.0, 1.0)

        # Volume confirmation (0.05 weight) — replaces the old hardcoded
        # "session_quality" placeholder (which was always 0.7 regardless of
        # anything real). A displacement candle on genuinely elevated
        # volume is far more likely to be an institutional move rather than
        # noise; low-volume "displacement" is a common source of fakeouts.
        if volume_ratio is not None:
            s.session_quality = float(np.clip(volume_ratio / 2.0, 0.2, 1.0))
            if volume_ratio >= 1.5:
                s.explanation.append(f"Volume confirmed: {volume_ratio:.1f}x avg")
            elif volume_ratio < 0.8:
                s.explanation.append(f"Weak volume: {volume_ratio:.1f}x avg — caution")
        else:
            s.session_quality = 0.5  # neutral when volume data unavailable

        # Choppy-market penalty — real implementation (previously a dead
        # field that was declared but never assigned). Uses recent
        # range/ATR compression right before the signal: a market that's
        # been chopping sideways just before this setup is more likely to
        # produce a fakeout than a market coming out of a clean trend.
        if choppiness is not None:
            if choppiness < 1.5:
                s.choppy_penalty = 0.15
                s.explanation.append(f"⚠️ Choppy pre-signal price action ({choppiness:.1f}x ATR range) — penalty applied")
            elif choppiness < 2.5:
                s.choppy_penalty = 0.05

        # Weak reclaim penalty — a sweep that only just barely cleared the
        # minimum penetration threshold is a weaker signal than a decisive one.
        if candidate.sweep is not None and candidate.sweep.penetration_atr < 0.35:
            s.weak_reclaim_penalty = 0.05
            s.explanation.append("Weak sweep penetration — minor penalty")

        # Weighted total
        s.total = round(
            s.htf_alignment * 0.20 +
            s.liquidity_quality * 0.15 +
            s.sweep_quality * 0.15 +
            s.displacement_strength * 0.15 +
            s.structure_confirmation * 0.15 +
            s.fvg_ob_confluence * 0.10 +
            s.rr_quality * 0.05 +
            s.session_quality * 0.05 -
            s.choppy_penalty -
            s.weak_reclaim_penalty -
            s.conflicting_bias_penalty,
            4
        )
        s.total = max(0.0, min(1.0, s.total))
        return s

    def passes_filters(self, candidate: SetupCandidate, plan: ExecutionPlan, score: ScoreBreakdown) -> bool:
        """No-trade filter check."""
        if not plan.is_valid:
            return False
        if score.total < self.min_score:
            return False
        if plan.rr_tp2 < self.min_rr:
            return False
        # Must have at least displacement
        if candidate.displacement is None:
            return False
        # Hard reject setups formed right after very choppy/compressed price
        # action — previously this was ONLY a soft score penalty, meaning a
        # setup with every other box checked could still slip through with
        # a mediocre-but-passing score despite forming in pure noise.
        if score.choppy_penalty >= 0.15:
            return False
        return True

    # ─── Internal ────────────────────────────────────────────

    def _calc_entry_zone(self, candidate: SetupCandidate, current_price: float):
        """Calculate entry zone from FVG/OB."""
        if candidate.fvg:
            fvg = candidate.fvg
            return fvg.bottom, fvg.top, fvg.midpoint
        elif candidate.order_block:
            ob = candidate.order_block
            return ob.bottom, ob.top, (ob.top + ob.bottom) / 2
        else:
            # Fallback: tight zone around current price
            spread = current_price * 0.002  # 0.2% spread
            return current_price - spread, current_price + spread, current_price

    def _calc_stop_loss(self, candidate: SetupCandidate, atr: float, side: str) -> float:
        """Structural stop placement — GUARANTEED on correct side of entry."""
        entry = candidate.fvg.midpoint if candidate.fvg else 0
        if entry <= 0:
            entry = candidate.order_block.bottom if candidate.order_block and side == "long" else \
                    candidate.order_block.top if candidate.order_block else 0

        # Try structural stop from sweep
        sl = 0.0
        if candidate.sweep and candidate.sweep.pool:
            if side == "long":
                sl = candidate.sweep.pool.price - candidate.sweep.max_excursion
            else:
                sl = candidate.sweep.pool.price + candidate.sweep.max_excursion

        # Try FVG boundary
        if sl == 0.0 and candidate.fvg:
            if side == "long":
                sl = candidate.fvg.bottom - 0.3 * atr
            else:
                sl = candidate.fvg.top + 0.3 * atr

        # ATR fallback
        if sl == 0.0 and entry > 0:
            if side == "long":
                sl = entry - 1.5 * atr
            else:
                sl = entry + 1.5 * atr

        # ── CRITICAL: Validate SL is on CORRECT side of entry ──
        if entry > 0:
            if side == "long" and sl >= entry:
                # SL above entry for long = WRONG! Fix it
                sl = entry - 1.5 * atr
            elif side == "short" and sl <= entry:
                # SL below entry for short = WRONG! Fix it
                sl = entry + 1.5 * atr

        # Ensure minimum distance (at least 0.4% from entry for commission coverage)
        if entry > 0:
            min_sl_distance = entry * 0.004  # 0.4%
            if side == "long" and (entry - sl) < min_sl_distance:
                sl = entry - min_sl_distance
            elif side == "short" and (sl - entry) < min_sl_distance:
                sl = entry + min_sl_distance

        return sl

    def _calc_targets(
        self, candidate: SetupCandidate, entry: float,
        risk: float, liq_map: LiquidityMap, side: str,
    ):
        """Calculate TP1/TP2/TP3 with minimum profitability guarantee.

        TP2 previously ALWAYS took the max()/min() of the real liquidity
        target and a fixed 2.5R fallback — which means if the actual
        nearest liquidity pool was CLOSER than 2.5R, the code silently
        pushed TP2 further away, past the real target, into empty space
        with no liquidity backing it. That's a big driver of low realized
        win rate: TP2 was frequently unreachable because it didn't
        correspond to any real level. Now: if a real target pool exists
        and sits at >= min_rr (so R:R filters still pass), USE the real
        target honestly instead of always inflating outward.
        """
        min_tp_distance = entry * 0.008  # At least 0.8% from entry
        effective_risk = max(risk, min_tp_distance / 2.0)
        min_rr_distance = risk * self.min_rr

        if side == "long":
            tp1 = entry + max(effective_risk * 1.5, min_tp_distance)
            if candidate.target_pool and candidate.target_pool.price > entry:
                target_dist = candidate.target_pool.price - entry
                if target_dist >= min_rr_distance:
                    tp2 = candidate.target_pool.price  # honest real target
                else:
                    tp2 = entry + effective_risk * 2.5  # real target too close -> fallback
            else:
                tp2 = entry + effective_risk * 2.5
            tp3 = entry + effective_risk * 4.0
            tp2 = max(tp2, tp1 + effective_risk * 0.5)
            tp3 = max(tp3, tp2 + effective_risk * 0.5)
        else:
            tp1 = entry - max(effective_risk * 1.5, min_tp_distance)
            if candidate.target_pool and candidate.target_pool.price < entry:
                target_dist = entry - candidate.target_pool.price
                if target_dist >= min_rr_distance:
                    tp2 = candidate.target_pool.price  # honest real target
                else:
                    tp2 = entry - effective_risk * 2.5
            else:
                tp2 = entry - effective_risk * 2.5
            tp3 = entry - effective_risk * 4.0
            tp2 = min(tp2, tp1 - effective_risk * 0.5)
            tp3 = min(tp3, tp2 - effective_risk * 0.5)
        return tp1, tp2, tp3

    def _suggest_leverage(self, rr: float, stop_atr: float, candidate: SetupCandidate) -> int:
        """Suggest leverage based on setup quality."""
        base = 3
        if stop_atr < 1.0:
            base += 2
        elif stop_atr < 1.5:
            base += 1
        if rr >= 3.0:
            base += 1
        if candidate.setup_type == SetupType.SWEEP_REVERSAL:
            base += 1
        return min(max(base, 2), 10)

    def _invalid(self, reason: str) -> ExecutionPlan:
        """Return invalid execution plan."""
        return ExecutionPlan(
            entry_zone_low=0, entry_zone_high=0, ideal_entry=0,
            market_price=0, stop_loss=0, stop_loss_buffered=0,
            stop_distance_pct=0, stop_distance_atr=0,
            take_profit_1=0, take_profit_2=0, take_profit_3=0,
            rr_tp1=0, rr_tp2=0, rr_tp3=0,
            invalidation_level=0, position_size_usdt=0,
            leverage_suggested=1, is_valid=False, rejection_reason=reason,
        )
