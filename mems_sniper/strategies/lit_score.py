"""LIT Score Engine - Setup Quality Scoring.

Scores each setup candidate on multiple dimensions:
  - HTF alignment quality (trend strength, structure clarity)
  - Sweep quality (wick depth, reclaim speed, displacement)
  - Confirmation quality (FVG created, retest clean, displacement strong)
  - Structure clarity (recent BOS/CHoCH, trend consistency)
  - R:R quality (higher = better)
  - Liquidity target clarity (clear target on other side)

Final score = weighted combination of all dimensions (0.0 - 1.0).
Only setups with score >= min_score proceed to signal generation.

This is the scoring/filtering layer of the LIT architecture.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, List, Optional

from strategies.lit_structure import (
    StructureState, TrendState, StructureBreak,
)
from strategies.lit_liquidity import (
    LiquidityMap, SweepEvent, SweepQuality, LiquiditySide,
)
from strategies.lit_setups import (
    SetupCandidate, SetupType, ConfirmationType, EntryConfirmation,
)
from strategies.lit_risk import RiskProfile



# ─── Data Models ─────────────────────────────────────────────

@dataclass
class ScoreBreakdown:
    """Detailed score breakdown for transparency."""
    htf_alignment: float = 0.0       # 0..1 HTF trend alignment quality
    sweep_quality: float = 0.0       # 0..1 quality of the liquidity sweep
    confirmation_quality: float = 0.0 # 0..1 entry confirmation strength
    structure_clarity: float = 0.0   # 0..1 market structure clarity
    rr_quality: float = 0.0          # 0..1 risk:reward ratio quality
    liquidity_target: float = 0.0    # 0..1 clarity of TP target
    final_score: float = 0.0         # weighted combination
    confidence_label: str = ""       # "high" | "medium" | "low"
    penalties: List[str] = field(default_factory=list)
    bonuses: List[str] = field(default_factory=list)


# ─── Score Engine ────────────────────────────────────────────

class LITScoreEngine:
    """Multi-dimensional scoring for LIT setup candidates.
    
    Weights (configurable):
      - HTF alignment:      25%
      - Sweep quality:      25%
      - Confirmation:       20%
      - Structure clarity:  15%
      - R:R quality:        10%
      - Liquidity target:    5%
    """

    def __init__(self, config: Dict = None):
        cfg = config or {}
        self.weights = {
            "htf_alignment": float(cfg.get("w_htf", 0.25)),
            "sweep_quality": float(cfg.get("w_sweep", 0.25)),
            "confirmation": float(cfg.get("w_confirmation", 0.20)),
            "structure": float(cfg.get("w_structure", 0.15)),
            "rr": float(cfg.get("w_rr", 0.10)),
            "liquidity_target": float(cfg.get("w_target", 0.05)),
        }
        self.min_score = float(cfg.get("min_score", 0.60))

    def score(
        self,
        candidate: SetupCandidate,
        risk_profile: RiskProfile,
        htf_structure: StructureState,
        ltf_structure: StructureState,
        liq_map: LiquidityMap,
    ) -> ScoreBreakdown:
        """Score a setup candidate across all dimensions."""
        breakdown = ScoreBreakdown()

        # 1. HTF Alignment
        breakdown.htf_alignment = self._score_htf_alignment(
            candidate, htf_structure
        )

        # 2. Sweep Quality
        breakdown.sweep_quality = self._score_sweep_quality(candidate)

        # 3. Confirmation Quality
        breakdown.confirmation_quality = self._score_confirmation(candidate)

        # 4. Structure Clarity
        breakdown.structure_clarity = self._score_structure(
            candidate, ltf_structure
        )

        # 5. R:R Quality
        breakdown.rr_quality = self._score_rr(risk_profile)

        # 6. Liquidity Target
        breakdown.liquidity_target = self._score_liquidity_target(
            candidate, liq_map
        )

        # Weighted combination
        w = self.weights
        raw = (
            breakdown.htf_alignment * w["htf_alignment"] +
            breakdown.sweep_quality * w["sweep_quality"] +
            breakdown.confirmation_quality * w["confirmation"] +
            breakdown.structure_clarity * w["structure"] +
            breakdown.rr_quality * w["rr"] +
            breakdown.liquidity_target * w["liquidity_target"]
        )

        # Apply bonuses and penalties
        bonus = 0.0
        penalty = 0.0

        # Bonus: multiple confirmations stacking
        if candidate.confirmation:
            n_conf = len(candidate.confirmation.confirmations)
            if n_conf >= 4:
                bonus += 0.08
                breakdown.bonuses.append("4+ تایید همزمان (+8%)")
            elif n_conf >= 3:
                bonus += 0.05
                breakdown.bonuses.append("3 تایید همزمان (+5%)")

        # Bonus: HTF + LTF fully aligned
        if (candidate.htf_bias == TrendState.BULLISH and
                candidate.ltf_structure == TrendState.BULLISH and
                candidate.side == "long"):
            bonus += 0.05
            breakdown.bonuses.append("HTF+LTF هم‌جهت (+5%)")
        elif (candidate.htf_bias == TrendState.BEARISH and
                candidate.ltf_structure == TrendState.BEARISH and
                candidate.side == "short"):
            bonus += 0.05
            breakdown.bonuses.append("HTF+LTF هم‌جهت (+5%)")

        # Penalty: weak displacement
        if candidate.confirmation and candidate.confirmation.displacement_strength < 1.0:
            penalty += 0.08
            breakdown.penalties.append("Displacement ضعیف (-8%)")

        # Penalty: no FVG created
        if candidate.confirmation and not candidate.fvg_zones:
            penalty += 0.05
            breakdown.penalties.append("بدون FVG (-5%)")

        # Penalty: LTF structure conflicts with trade direction
        if (candidate.side == "long" and candidate.ltf_structure == TrendState.BEARISH):
            penalty += 0.07
            breakdown.penalties.append("LTF نزولی vs Long (-7%)")
        elif (candidate.side == "short" and candidate.ltf_structure == TrendState.BULLISH):
            penalty += 0.07
            breakdown.penalties.append("LTF صعودی vs Short (-7%)")

        final = max(0.0, min(1.0, raw + bonus - penalty))
        breakdown.final_score = round(final, 4)

        # Confidence label
        if final >= 0.80:
            breakdown.confidence_label = "high"
        elif final >= 0.65:
            breakdown.confidence_label = "medium"
        else:
            breakdown.confidence_label = "low"

        return breakdown

    def passes_filter(self, breakdown: ScoreBreakdown) -> bool:
        """Check if score meets minimum threshold."""
        return breakdown.final_score >= self.min_score

    # ─── Dimension Scorers ───────────────────────────────────

    def _score_htf_alignment(
        self, candidate: SetupCandidate, htf_structure: StructureState
    ) -> float:
        """Score HTF trend alignment (0..1).
        
        Best: clear trend with recent BOS + trade aligned
        Worst: ranging or conflicting
        """
        if candidate.htf_bias == TrendState.RANGING:
            # Range-Expansion setup is OK with ranging HTF
            if candidate.setup_type == SetupType.RANGE_EXPANSION:
                return 0.5
            return 0.2

        # Check if HTF has recent structure events confirming trend
        recent_events = [
            e for e in htf_structure.events[-5:]
        ] if htf_structure.events else []

        trend_events = 0
        for event in recent_events:
            if (candidate.htf_bias == TrendState.BULLISH and
                    event.kind in (StructureBreak.BULLISH_BOS, StructureBreak.BULLISH_CHOCH)):
                trend_events += 1
            elif (candidate.htf_bias == TrendState.BEARISH and
                    event.kind in (StructureBreak.BEARISH_BOS, StructureBreak.BEARISH_CHOCH)):
                trend_events += 1

        # More confirming events = higher score
        base = 0.6
        if trend_events >= 2:
            base = 0.9
        elif trend_events >= 1:
            base = 0.75

        # Trade direction alignment
        if (candidate.htf_bias == TrendState.BULLISH and candidate.side == "long"):
            return min(base + 0.1, 1.0)
        elif (candidate.htf_bias == TrendState.BEARISH and candidate.side == "short"):
            return min(base + 0.1, 1.0)

        return base * 0.5  # Misaligned

    def _score_sweep_quality(self, candidate: SetupCandidate) -> float:
        """Score the quality of the liquidity sweep (0..1).
        
        Factors: sweep quality enum, wick depth, reclaim, displacement after
        """
        sweep = candidate.sweep_event
        if sweep is None:
            # Inducement-Continuation may not have direct sweep
            if candidate.setup_type == SetupType.INDUCEMENT_CONTINUATION:
                return 0.5  # Neutral — other factors matter more
            return 0.2  # No sweep = low score

        quality_map = {
            SweepQuality.STRONG: 0.9,
            SweepQuality.MODERATE: 0.65,
            SweepQuality.WEAK: 0.3,
        }
        base = quality_map.get(sweep.quality, 0.3)

        # Bonus for deep wick
        if sweep.wick_depth_atr >= 1.0:
            base = min(base + 0.1, 1.0)

        # Bonus for displacement after sweep
        if sweep.displacement_after >= 1.5:
            base = min(base + 0.1, 1.0)
        elif sweep.displacement_after >= 1.0:
            base = min(base + 0.05, 1.0)

        # Bonus for pool strength (more touches = more liquidity collected)
        if sweep.pool.strength >= 3:
            base = min(base + 0.05, 1.0)

        return base

    def _score_confirmation(self, candidate: SetupCandidate) -> float:
        """Score entry confirmation quality (0..1).
        
        Best: reclaim + displacement + FVG + retest (all 4)
        Good: reclaim + displacement + FVG (3)
        Minimum: reclaim + displacement (2)
        """
        conf = candidate.confirmation
        if conf is None:
            return 0.3  # No explicit confirmation

        n_confirmations = len(conf.confirmations)
        base = min(0.4 + n_confirmations * 0.15, 0.9)

        # Displacement strength bonus
        if conf.displacement_strength >= 2.0:
            base = min(base + 0.1, 1.0)
        elif conf.displacement_strength >= 1.5:
            base = min(base + 0.05, 1.0)

        # FVG retest quality
        if conf.retest_quality >= 0.5:
            base = min(base + 0.08, 1.0)
        elif conf.retest_quality > 0:
            base = min(base + 0.03, 1.0)

        # FVG exists = better
        if conf.fvg is not None:
            base = min(base + 0.05, 1.0)

        return base

    def _score_structure(
        self, candidate: SetupCandidate, ltf_structure: StructureState
    ) -> float:
        """Score market structure clarity (0..1).
        
        Clear = consistent trend with BOS events
        Unclear = choppy, conflicting events
        """
        events = ltf_structure.events
        if not events:
            return 0.3

        # Count recent events in trade direction
        aligned = 0
        conflicting = 0
        for event in events[-5:]:
            if candidate.side == "long":
                if event.kind in (StructureBreak.BULLISH_BOS, StructureBreak.BULLISH_CHOCH):
                    aligned += 1
                elif event.kind in (StructureBreak.BEARISH_BOS, StructureBreak.BEARISH_CHOCH):
                    conflicting += 1
            else:
                if event.kind in (StructureBreak.BEARISH_BOS, StructureBreak.BEARISH_CHOCH):
                    aligned += 1
                elif event.kind in (StructureBreak.BULLISH_BOS, StructureBreak.BULLISH_CHOCH):
                    conflicting += 1

        if aligned == 0 and conflicting == 0:
            return 0.4

        # Ratio of aligned to total
        total = aligned + conflicting
        ratio = aligned / max(total, 1)

        # Strong displacement in events = more reliable
        avg_disp = 0.0
        if events:
            displacements = [e.displacement for e in events[-5:] if e.displacement > 0]
            if displacements:
                avg_disp = sum(displacements) / len(displacements)

        base = 0.3 + ratio * 0.5
        if avg_disp >= 1.5:
            base = min(base + 0.15, 1.0)
        elif avg_disp >= 1.0:
            base = min(base + 0.08, 1.0)

        return base

    def _score_rr(self, risk_profile: RiskProfile) -> float:
        """Score R:R quality (0..1).
        
        2.0 = minimum (0.5)
        3.0 = good (0.75)
        4.0+ = excellent (0.95)
        """
        if not risk_profile.is_valid:
            return 0.0

        rr = risk_profile.rr_ratio_1
        if rr >= 4.0:
            return 0.95
        elif rr >= 3.5:
            return 0.85
        elif rr >= 3.0:
            return 0.75
        elif rr >= 2.5:
            return 0.65
        elif rr >= 2.0:
            return 0.50
        else:
            return 0.2

    def _score_liquidity_target(
        self, candidate: SetupCandidate, liq_map: LiquidityMap
    ) -> float:
        """Score clarity of take-profit target (0..1).
        
        Best: clear opposing liquidity pool as target
        Worst: no clear target, relying on R:R multiples only
        """
        if candidate.side == "long":
            # Target is buy-side liquidity (above)
            if liq_map.nearest_buy_side:
                # Has clear target
                strength = liq_map.nearest_buy_side.strength
                if strength >= 3:
                    return 0.9
                elif strength >= 2:
                    return 0.7
                return 0.5
        else:
            if liq_map.nearest_sell_side:
                strength = liq_map.nearest_sell_side.strength
                if strength >= 3:
                    return 0.9
                elif strength >= 2:
                    return 0.7
                return 0.5

        return 0.3  # No clear liquidity target
