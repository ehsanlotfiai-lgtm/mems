"""LIT (Liquidity Inducement Theorem) Strategy Engine — v2.

5-Layer Architecture:
  Layer 1: Structure Engine  (lit_structure.py)  — BOS/CHoCH, trend state
  Layer 2: Liquidity Engine  (lit_liquidity.py)  — pools, sweeps, inducement
  Layer 3: Setup Detection   (lit_setups.py)     — 3 rule-based setups
  Layer 4: Risk Engine       (lit_risk.py)       — SL/TP, sizing, R:R filter
  Layer 5: Score Engine      (lit_score.py)      — multi-dimensional scoring

Three Setups:
  1. Sweep-Reversal       — sweep major liquidity + reclaim + displacement
  2. Inducement-Continuation — internal sweep + BOS continuation in HTF direction
  3. Range-to-Expansion   — compression + sweep one side + expand other

Core Rule: NO ENTRY without HTF bias alignment.

This file is the main orchestrator that the forward_engine calls.
Interface preserved: LITEngine.analyze(df, symbol, exchange, htf_df) -> Optional[LITSignal]
"""
from __future__ import annotations

import time
import uuid
from dataclasses import dataclass, field
from typing import Dict, List, Optional

import numpy as np
import pandas as pd

from core.logging_setup import logger
from strategies.lit_structure import StructureEngine, StructureState, TrendState
from strategies.lit_liquidity import LiquidityEngine, LiquidityMap, SweepQuality
from strategies.lit_setups import SetupDetector, SetupCandidate, SetupType
from strategies.lit_risk import LITRiskEngine, RiskProfile
from strategies.lit_score import LITScoreEngine, ScoreBreakdown



# ─── LITSignal (preserved interface) ─────────────────────────

@dataclass
class LITSignal:
    """A complete LIT trading signal — interface preserved for forward_engine."""
    id: str
    symbol: str
    exchange: str
    side: str
    entry: float
    stop_loss: float
    take_profit: float
    take_profit_2: float
    score: float
    strategy: str
    timestamp: float
    reasoning: str
    zones: List[dict] = field(default_factory=list)
    liquidity_levels: List[dict] = field(default_factory=list)
    fvg_zones: List[dict] = field(default_factory=list)
    order_blocks: List[dict] = field(default_factory=list)
    # Extended v2 fields
    setup_type: str = ""
    htf_bias: str = ""
    ltf_structure: str = ""
    score_breakdown: Dict = field(default_factory=dict)
    risk_reward: float = 0.0
    leverage_suggested: int = 3
    confidence: str = "medium"
    # Backtest tracking
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
            "setup_type": self.setup_type,
            "htf_bias": self.htf_bias,
            "ltf_structure": self.ltf_structure,
            "score_breakdown": self.score_breakdown,
            "risk_reward": self.risk_reward,
            "leverage_suggested": self.leverage_suggested,
            "confidence": self.confidence,
            "actual_entry_time": self.actual_entry_time,
            "actual_exit_time": self.actual_exit_time,
            "actual_exit_price": self.actual_exit_price,
            "actual_pnl_pct": self.actual_pnl_pct,
            "actual_pnl_usdt": self.actual_pnl_usdt,
            "exit_reason": self.exit_reason,
            "hit_tp": self.hit_tp,
            "hit_sl": self.hit_sl,
        }



# ─── Main LIT Engine ─────────────────────────────────────────

class LITEngine:
    """Main orchestrator — 5-layer LIT analysis pipeline.
    
    Pipeline:
    1. Parse OHLC data from DataFrames
    2. Run Structure Engine on HTF → get bias
    3. Run Structure Engine on LTF → get local structure
    4. Run Liquidity Engine → get pools, sweeps, inducements
    5. Run Setup Detector → get candidates
    6. Run Risk Engine → validate SL/TP/sizing
    7. Run Score Engine → score and filter
    8. Build LITSignal with full reasoning
    """

    def __init__(self, config: dict = None):
        cfg = config or {}

        # Layer 1: Structure
        self.structure_engine = StructureEngine(
            swing_lookback=int(cfg.get("swing_lookback", 5)),
            min_swing_distance=int(cfg.get("min_swing_distance", 3)),
        )

        # Layer 2: Liquidity
        self.liquidity_engine = LiquidityEngine(
            equal_tolerance_pct=float(cfg.get("equal_tolerance_pct", 0.08)),
            min_touches=int(cfg.get("min_touches", 2)),
            sweep_min_wick_atr=float(cfg.get("sweep_min_wick_atr", 0.3)),
            strong_sweep_wick_atr=float(cfg.get("strong_sweep_wick_atr", 0.7)),
        )

        # Layer 3: Setups
        self.setup_detector = SetupDetector(cfg.get("setups", {}))

        # Layer 4: Risk
        self.risk_engine = LITRiskEngine(
            max_risk_pct=float(cfg.get("risk_per_trade_pct", 1.0)),
            min_rr=float(cfg.get("min_rr", 2.0)),
            balance=float(cfg.get("initial_balance", 10000)),
            sl_buffer_atr=float(cfg.get("sl_buffer_atr", 0.2)),
        )

        # Layer 5: Scoring
        self.score_engine = LITScoreEngine(cfg.get("scoring", {}))

        # Config
        self.min_score = float(cfg.get("min_score", 0.50))

        logger.info(
            f"LIT Engine v2 initialized: "
            f"min_RR={self.risk_engine.min_rr}, "
            f"min_score={self.min_score}, "
            f"risk={self.risk_engine.max_risk_pct}%"
        )

    def analyze(
        self,
        df: pd.DataFrame,
        symbol: str,
        exchange: str = "binance",
        htf_df: Optional[pd.DataFrame] = None,
    ) -> Optional[LITSignal]:
        """Full 5-layer LIT analysis.
        
        Args:
            df: OHLCV DataFrame (trigger TF, e.g. 15m or 5m)
            symbol: trading pair (e.g. "BTC/USDT")
            exchange: exchange name
            htf_df: higher timeframe DataFrame (e.g. 1h or 4h) for bias
        
        Returns:
            LITSignal if a valid setup passes all filters, else None
        """
        if df is None or df.empty or len(df) < 30:
            return None

        # ── Parse OHLC arrays ──
        opens = df["open"].values.astype(float)
        highs = df["high"].values.astype(float)
        lows = df["low"].values.astype(float)
        closes = df["close"].values.astype(float)
        volumes = df["volume"].values.astype(float) if "volume" in df.columns else np.ones(len(opens))

        current_price = float(closes[-1])
        n = len(closes)

        # ── Calculate ATR ──
        atr = self._calc_atr(highs, lows, closes, 14)
        if atr <= 0:
            return None

        # ═══ Layer 1: STRUCTURE ═══
        # HTF structure → bias
        htf_structure = self._analyze_htf_structure(htf_df)
        htf_bias = htf_structure.trend if htf_structure else TrendState.RANGING

        # LTF structure → local context
        ltf_structure = self.structure_engine.analyze(highs, lows, closes, opens)

        # Gate: no entry without HTF bias (except range-expansion)
        # Relaxed: allow if LTF has clear structure even when HTF is ranging
        if htf_bias == TrendState.RANGING:
            # Allow through — LTF structure will drive Range-to-Expansion setups
            pass

        # ═══ Layer 2: LIQUIDITY ═══
        liq_map = self.liquidity_engine.analyze(
            highs, lows, closes, opens, ltf_structure, atr, current_price
        )

        # ═══ Layer 3: SETUP DETECTION ═══
        candidates = self.setup_detector.detect_all(
            highs, lows, closes, opens, atr,
            htf_bias, ltf_structure, liq_map, current_price
        )

        if not candidates:
            return None

        # ═══ Layer 4+5: RISK + SCORE each candidate ═══
        best_signal: Optional[LITSignal] = None
        best_score = 0.0

        for candidate in candidates:
            # Risk validation
            risk_profile = self.risk_engine.calculate_risk(candidate, atr, current_price)
            if not risk_profile.is_valid:
                continue

            # Score
            score_breakdown = self.score_engine.score(
                candidate, risk_profile, htf_structure, ltf_structure, liq_map
            )
            if not self.score_engine.passes_filter(score_breakdown):
                continue

            # Build signal if this is the best one
            if score_breakdown.final_score > best_score:
                best_score = score_breakdown.final_score
                best_signal = self._build_signal(
                    candidate, risk_profile, score_breakdown,
                    htf_bias, ltf_structure, liq_map,
                    symbol, exchange, atr
                )

        return best_signal



    # ─── Internal Helpers ────────────────────────────────────

    def _analyze_htf_structure(self, htf_df: Optional[pd.DataFrame]) -> StructureState:
        """Analyze HTF DataFrame for bias determination."""
        if htf_df is None or htf_df.empty or len(htf_df) < 20:
            # Fallback: return ranging state
            return StructureState(
                trend=TrendState.RANGING,
                swing_highs=[], swing_lows=[],
                last_valid_high=None, last_valid_low=None,
                events=[],
            )

        htf_opens = htf_df["open"].values.astype(float)
        htf_highs = htf_df["high"].values.astype(float)
        htf_lows = htf_df["low"].values.astype(float)
        htf_closes = htf_df["close"].values.astype(float)

        return self.structure_engine.analyze(htf_highs, htf_lows, htf_closes, htf_opens)

    def _calc_atr(self, highs: np.ndarray, lows: np.ndarray, closes: np.ndarray, length: int) -> float:
        """Calculate ATR (Average True Range)."""
        if len(highs) < length + 1:
            return float(np.mean(highs - lows)) if len(highs) > 0 else 0.0

        tr = np.maximum(
            highs[1:] - lows[1:],
            np.maximum(
                np.abs(highs[1:] - closes[:-1]),
                np.abs(lows[1:] - closes[:-1]),
            ),
        )
        # Wilder smoothing
        atr_values = np.zeros(len(tr))
        atr_values[length - 1] = np.mean(tr[:length])
        for i in range(length, len(tr)):
            atr_values[i] = (atr_values[i - 1] * (length - 1) + tr[i]) / length

        return float(atr_values[-1]) if len(atr_values) > 0 else 0.0

    def _build_signal(
        self,
        candidate: SetupCandidate,
        risk_profile: RiskProfile,
        score: ScoreBreakdown,
        htf_bias: TrendState,
        ltf_structure: StructureState,
        liq_map: LiquidityMap,
        symbol: str,
        exchange: str,
        atr: float,
    ) -> LITSignal:
        """Build the final LITSignal with full reasoning and zones."""

        # ── Reasoning (Persian, educational) ──
        reasoning = self._build_reasoning(candidate, risk_profile, score, htf_bias, atr)

        # ── Zones for chart display ──
        zones = self._build_zones(candidate, risk_profile, liq_map)

        # ── Liquidity levels for display ──
        liq_levels = []
        for pool in liq_map.buy_side_pools[:3]:
            liq_levels.append({
                "price": pool.price, "side": "buy_side",
                "kind": pool.kind.value, "strength": pool.strength,
            })
        for pool in liq_map.sell_side_pools[:3]:
            liq_levels.append({
                "price": pool.price, "side": "sell_side",
                "kind": pool.kind.value, "strength": pool.strength,
            })

        # ── FVG zones ──
        fvg_zones_data = []
        for fvg in candidate.fvg_zones:
            fvg_zones_data.append({
                "top": fvg.top, "bottom": fvg.bottom,
                "direction": fvg.direction,
            })

        # ── Score breakdown dict ──
        score_dict = {
            "htf_alignment": round(score.htf_alignment, 3),
            "sweep_quality": round(score.sweep_quality, 3),
            "confirmation": round(score.confirmation_quality, 3),
            "structure": round(score.structure_clarity, 3),
            "rr_quality": round(score.rr_quality, 3),
            "liquidity_target": round(score.liquidity_target, 3),
            "final": round(score.final_score, 4),
            "bonuses": score.bonuses,
            "penalties": score.penalties,
        }

        return LITSignal(
            id="LIT_" + uuid.uuid4().hex[:8],
            symbol=symbol,
            exchange=exchange,
            side=candidate.side,
            entry=round(risk_profile.entry, 8),
            stop_loss=round(risk_profile.stop_loss, 8),
            take_profit=round(risk_profile.take_profit_1, 8),
            take_profit_2=round(risk_profile.take_profit_2, 8),
            score=score.final_score,
            strategy=candidate.setup_type.value,
            timestamp=time.time(),
            reasoning=reasoning,
            zones=zones,
            liquidity_levels=liq_levels,
            fvg_zones=fvg_zones_data,
            order_blocks=[],
            setup_type=candidate.setup_type.value,
            htf_bias=htf_bias.value,
            ltf_structure=ltf_structure.trend.value,
            score_breakdown=score_dict,
            risk_reward=risk_profile.rr_ratio_1,
            leverage_suggested=risk_profile.leverage_suggested,
            confidence=score.confidence_label,
        )



    def _build_reasoning(
        self,
        candidate: SetupCandidate,
        risk_profile: RiskProfile,
        score: ScoreBreakdown,
        htf_bias: TrendState,
        atr: float,
    ) -> str:
        """Build Persian educational reasoning string."""
        setup_names = {
            SetupType.SWEEP_REVERSAL: "Sweep-Reversal (شکار و برگشت)",
            SetupType.INDUCEMENT_CONTINUATION: "Inducement-Continuation (تله و ادامه)",
            SetupType.RANGE_EXPANSION: "Range-to-Expansion (محدوده به انبساط)",
        }
        bias_fa = {"bullish": "صعودی 🟢", "bearish": "نزولی 🔴", "ranging": "خنثی ➡️"}
        side_fa = "خرید (LONG) 🟢" if candidate.side == "long" else "فروش (SHORT) 🔴"
        conf_label = {"high": "بالا ⭐", "medium": "متوسط", "low": "پایین"}

        parts = []
        parts.append(f"📐 Setup: {setup_names.get(candidate.setup_type, candidate.setup_type.value)}")
        parts.append(f"📊 HTF Bias: {bias_fa.get(htf_bias.value, htf_bias.value)}")
        parts.append(f"🎯 جهت: {side_fa}")
        parts.append("")

        # Setup-specific reasoning
        for line in candidate.reasoning_parts:
            parts.append(f"  • {line}")
        parts.append("")

        # Confirmation details
        if candidate.confirmation:
            conf_types_fa = {
                "reclaim": "بازپس‌گیری سطح",
                "displacement": "جابجایی قوی",
                "fvg_created": "FVG ایجاد شد",
                "fvg_retest": "Retest FVG",
                "ob_retest": "Retest Order Block",
                "bos_confirmed": "تایید BOS",
            }
            conf_str = " + ".join([
                conf_types_fa.get(c.value, c.value)
                for c in candidate.confirmation.confirmations
            ])
            parts.append(f"✅ تایید ورود: {conf_str}")
            parts.append(f"  Displacement: {candidate.confirmation.displacement_strength:.1f}x ATR")
        parts.append("")

        # Risk
        parts.append(f"⚖️ R:R = {risk_profile.rr_ratio_1:.1f}:1")
        parts.append(f"💰 اهرم پیشنهادی: {risk_profile.leverage_suggested}x")
        parts.append(f"🎯 اعتماد: {conf_label.get(score.confidence_label, score.confidence_label)}")

        # Score breakdown
        if score.bonuses:
            parts.append(f"➕ بونوس: {' | '.join(score.bonuses)}")
        if score.penalties:
            parts.append(f"➖ جریمه: {' | '.join(score.penalties)}")

        return "\n".join(parts)

    def _build_zones(
        self,
        candidate: SetupCandidate,
        risk_profile: RiskProfile,
        liq_map: LiquidityMap,
    ) -> List[dict]:
        """Build chart zones for dashboard display."""
        zones = []

        # Entry/SL/TP zones
        zones.append({"type": "entry", "price": risk_profile.entry, "label": "ورود", "color": "#3b82f6"})
        zones.append({"type": "sl", "price": risk_profile.stop_loss, "label": "استاپ لاس", "color": "#ef4444"})
        zones.append({"type": "tp", "price": risk_profile.take_profit_1, "label": "TP1", "color": "#10b981"})
        zones.append({"type": "tp2", "price": risk_profile.take_profit_2, "label": "TP2", "color": "#059669"})
        zones.append({"type": "tp3", "price": risk_profile.take_profit_3, "label": "TP3", "color": "#047857"})

        # Sweep level
        if candidate.sweep_event:
            zones.append({
                "type": "liquidity_level",
                "price": candidate.sweep_event.pool.price,
                "label": f"Swept: {candidate.sweep_event.pool.kind.value}",
                "color": "#f59e0b",
            })

        # FVG zones
        for fvg in candidate.fvg_zones:
            color = "#a855f7" if fvg.direction == "bullish" else "#ec4899"
            zones.append({
                "type": "fvg",
                "top": fvg.top,
                "bottom": fvg.bottom,
                "label": f"FVG ({fvg.direction})",
                "color": color,
            })

        # Nearest liquidity targets
        if liq_map.nearest_buy_side:
            zones.append({
                "type": "liquidity_target",
                "price": liq_map.nearest_buy_side.price,
                "label": "Buy-side نقدینگی",
                "color": "#6366f1",
            })
        if liq_map.nearest_sell_side:
            zones.append({
                "type": "liquidity_target",
                "price": liq_map.nearest_sell_side.price,
                "label": "Sell-side نقدینگی",
                "color": "#6366f1",
            })

        return zones
