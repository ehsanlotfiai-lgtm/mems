"""LIT Risk Engine - Structure-Based Risk Management.

Handles:
  - Stop-loss placement based on structure (beyond swing/sweep wick)
  - ATR-based fallback when structure is unclear
  - Position sizing with fixed-risk percentage
  - Minimum R:R filter (reject setups below threshold)
  - Multi-TP levels with trailing logic
  - Risk validation and sanity checks

This is Layer 5 of the LIT architecture.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional, Tuple

import numpy as np

from strategies.lit_setups import SetupCandidate, SetupType


# ─── Data Models ─────────────────────────────────────────────

@dataclass
class RiskProfile:
    """Complete risk profile for a trade."""
    entry: float
    stop_loss: float
    take_profit_1: float
    take_profit_2: float
    take_profit_3: float
    risk_distance: float        # |entry - SL| in price
    risk_pct: float             # risk as % of entry
    reward_1: float             # |entry - TP1| in price
    reward_2: float             # |entry - TP2| in price
    rr_ratio_1: float           # TP1 reward / risk
    rr_ratio_2: float           # TP2 reward / risk
    position_size_usdt: float   # dollar amount to risk
    position_size_qty: float    # quantity (size / entry)
    leverage_suggested: int     # suggested leverage for futures
    is_valid: bool              # passes all risk checks
    rejection_reason: str = ""  # why rejected if invalid



# ─── Risk Engine ─────────────────────────────────────────────

class LITRiskEngine:
    """Structure-based risk management for LIT setups.
    
    Principles:
    - Stop-loss is ALWAYS structural (beyond sweep wick or swing)
    - Never risk more than max_risk_pct per trade
    - Minimum R:R of 2.0 required (configurable)
    - TP levels based on liquidity targets + structure
    - Position size = (balance * risk_pct) / SL_distance
    """

    def __init__(
        self,
        max_risk_pct: float = 1.0,
        min_rr: float = 2.0,
        balance: float = 10000.0,
        sl_buffer_atr: float = 0.2,
        tp3_rr_mult: float = 4.0,
        max_sl_atr_mult: float = 3.0,
        min_sl_atr_mult: float = 0.5,
    ):
        self.max_risk_pct = max_risk_pct
        self.min_rr = min_rr
        self.balance = balance
        self.sl_buffer_atr = sl_buffer_atr
        self.tp3_rr_mult = tp3_rr_mult
        self.max_sl_atr_mult = max_sl_atr_mult
        self.min_sl_atr_mult = min_sl_atr_mult

    def calculate_risk(
        self,
        candidate: SetupCandidate,
        atr: float,
        current_price: float,
    ) -> RiskProfile:
        """Calculate complete risk profile for a setup candidate.
        
        Steps:
        1. Validate/adjust stop-loss (structural + ATR buffer)
        2. Validate/adjust take-profit levels
        3. Check R:R ratio
        4. Calculate position size
        5. Suggest leverage
        """
        entry = candidate.entry_price
        sl = candidate.stop_loss_price
        tp1 = candidate.tp1_price
        tp2 = candidate.tp2_price
        side = candidate.side

        # ── Step 1: Validate and adjust stop-loss ──
        sl = self._validate_stop_loss(entry, sl, atr, side)

        # ── Step 2: Calculate risk distance ──
        if side == "long":
            risk_distance = entry - sl
        else:
            risk_distance = sl - entry

        if risk_distance <= 0:
            return self._invalid_profile(entry, sl, tp1, tp2, "Risk distance <= 0")

        # ── Step 3: Validate SL is within acceptable ATR range ──
        sl_atr_ratio = risk_distance / max(atr, 1e-10)
        if sl_atr_ratio > self.max_sl_atr_mult:
            # SL too far — tighten to max allowed
            if side == "long":
                sl = entry - self.max_sl_atr_mult * atr
            else:
                sl = entry + self.max_sl_atr_mult * atr
            risk_distance = abs(entry - sl)

        if sl_atr_ratio < self.min_sl_atr_mult:
            # SL too tight — widen to min
            if side == "long":
                sl = entry - self.min_sl_atr_mult * atr
            else:
                sl = entry + self.min_sl_atr_mult * atr
            risk_distance = abs(entry - sl)

        # ── Step 4: Validate/adjust TP levels ──
        tp1, tp2, tp3 = self._validate_targets(entry, sl, tp1, tp2, side, risk_distance)

        # ── Step 5: Calculate R:R ──
        if side == "long":
            reward_1 = tp1 - entry
            reward_2 = tp2 - entry
        else:
            reward_1 = entry - tp1
            reward_2 = entry - tp2

        rr_1 = reward_1 / max(risk_distance, 1e-10)
        rr_2 = reward_2 / max(risk_distance, 1e-10)

        # ── Step 6: R:R filter ──
        if rr_1 < self.min_rr:
            return self._invalid_profile(
                entry, sl, tp1, tp2,
                f"R:R too low: {rr_1:.1f} < {self.min_rr}"
            )

        # ── Step 7: Position sizing ──
        risk_amount = self.balance * (self.max_risk_pct / 100.0)
        position_size_usdt = risk_amount / (risk_distance / max(entry, 1e-10))
        position_size_qty = position_size_usdt / max(entry, 1e-10)

        # ── Step 8: Leverage suggestion ──
        leverage = self._suggest_leverage(rr_1, sl_atr_ratio, candidate.setup_type)

        risk_pct_of_entry = (risk_distance / max(entry, 1e-10)) * 100

        return RiskProfile(
            entry=entry,
            stop_loss=sl,
            take_profit_1=tp1,
            take_profit_2=tp2,
            take_profit_3=tp3,
            risk_distance=risk_distance,
            risk_pct=risk_pct_of_entry,
            reward_1=reward_1,
            reward_2=reward_2,
            rr_ratio_1=round(rr_1, 2),
            rr_ratio_2=round(rr_2, 2),
            position_size_usdt=round(position_size_usdt, 2),
            position_size_qty=round(position_size_qty, 6),
            leverage_suggested=leverage,
            is_valid=True,
        )

    # ─── Internal Methods ────────────────────────────────────

    def _validate_stop_loss(
        self, entry: float, sl: float, atr: float, side: str
    ) -> float:
        """Ensure stop-loss has ATR buffer and is on correct side."""
        buffer = self.sl_buffer_atr * atr

        if side == "long":
            # SL must be BELOW entry
            if sl >= entry:
                sl = entry - atr  # Fallback
            # Add buffer below structural SL
            sl = sl - buffer
        else:
            # SL must be ABOVE entry
            if sl <= entry:
                sl = entry + atr
            sl = sl + buffer

        return sl

    def _validate_targets(
        self,
        entry: float,
        sl: float,
        tp1: float,
        tp2: float,
        side: str,
        risk_distance: float,
    ) -> Tuple[float, float, float]:
        """Validate and adjust TP levels to ensure minimum R:R."""
        min_tp1_distance = risk_distance * self.min_rr
        tp3_distance = risk_distance * self.tp3_rr_mult

        if side == "long":
            # TPs must be ABOVE entry
            if tp1 <= entry:
                tp1 = entry + min_tp1_distance
            if tp2 <= tp1:
                tp2 = tp1 + risk_distance
            # Ensure minimum R:R
            if (tp1 - entry) < min_tp1_distance:
                tp1 = entry + min_tp1_distance
                tp2 = entry + min_tp1_distance * 1.5
            tp3 = entry + tp3_distance
        else:
            if tp1 >= entry:
                tp1 = entry - min_tp1_distance
            if tp2 >= tp1:
                tp2 = tp1 - risk_distance
            if (entry - tp1) < min_tp1_distance:
                tp1 = entry - min_tp1_distance
                tp2 = entry - min_tp1_distance * 1.5
            tp3 = entry - tp3_distance

        return tp1, tp2, tp3

    def _suggest_leverage(
        self, rr: float, sl_atr_ratio: float, setup_type: SetupType
    ) -> int:
        """Suggest appropriate leverage based on setup quality.
        
        Conservative:
        - Tight SL (< 1 ATR) + high RR -> higher leverage OK
        - Wide SL (> 2 ATR) -> lower leverage
        - Sweep-Reversal with strong quality -> slightly higher
        """
        base_lev = 3

        # Tighter SL allows higher leverage
        if sl_atr_ratio < 1.0:
            base_lev += 2
        elif sl_atr_ratio < 1.5:
            base_lev += 1

        # Better RR allows slightly higher
        if rr >= 3.0:
            base_lev += 1
        elif rr >= 4.0:
            base_lev += 2

        # Setup type bonus
        if setup_type == SetupType.SWEEP_REVERSAL:
            base_lev += 1  # Highest edge setup

        # Cap at reasonable levels
        return min(max(base_lev, 2), 10)

    def _invalid_profile(
        self, entry: float, sl: float, tp1: float, tp2: float, reason: str
    ) -> RiskProfile:
        """Return an invalid risk profile with rejection reason."""
        return RiskProfile(
            entry=entry,
            stop_loss=sl,
            take_profit_1=tp1,
            take_profit_2=tp2,
            take_profit_3=tp2,
            risk_distance=0.0,
            risk_pct=0.0,
            reward_1=0.0,
            reward_2=0.0,
            rr_ratio_1=0.0,
            rr_ratio_2=0.0,
            position_size_usdt=0.0,
            position_size_qty=0.0,
            leverage_suggested=1,
            is_valid=False,
            rejection_reason=reason,
        )
