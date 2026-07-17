"""LIT Engine v3 — Production Liquidity Inducement Theorem Engine.

5-Layer Pipeline:
  1. Structure (lit_structure.py) — swings, BOS/CHoCH, displacement
  2. Liquidity (lit_liquidity.py) — pools, sweeps
  3. Patterns  (lit_patterns.py) — FVG, OB, 3 setup families
  4. Execution (lit_execution.py) — entry/SL/TP, sizing, scoring
  5. Visuals   (lit_visuals.py) — chart annotations

Sequence: HTF bias → liquidity map → sweep → CHoCH/BOS → displacement → FVG/OB retest → entry

Interface: LITEngine.analyze(df, symbol, exchange, htf_df) -> Optional[LITSignal]
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
from strategies.lit_liquidity import LiquidityEngine, LiquidityMap
from strategies.lit_patterns import SetupDetector, SetupCandidate, SetupType, EntryMode, SignalStatus
from strategies.lit_execution import ExecutionEngine, ExecutionPlan, ScoreBreakdown
from strategies.lit_visuals import AnnotationBuilder, ChartAnnotation


# ─── Signal Output ───────────────────────────────────────────

@dataclass
class LITSignal:
    """Complete LIT signal — backward-compatible interface."""
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
    # Chart data
    zones: List[dict] = field(default_factory=list)
    liquidity_levels: List[dict] = field(default_factory=list)
    fvg_zones: List[dict] = field(default_factory=list)
    order_blocks: List[dict] = field(default_factory=list)
    chart_annotations: List[dict] = field(default_factory=list)
    # Extended v3 fields
    setup_type: str = ""
    entry_mode: str = ""
    status: str = "ready"
    confidence: float = 0.0
    bias: str = ""
    htf_context: Dict = field(default_factory=dict)
    liquidity_data: Dict = field(default_factory=dict)
    structure_data: Dict = field(default_factory=dict)
    execution: Dict = field(default_factory=dict)
    score_breakdown: Dict = field(default_factory=dict)
    reasons: List[str] = field(default_factory=list)
    risk_flags: List[str] = field(default_factory=list)
    debug: Dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {
            "id": self.id, "symbol": self.symbol, "exchange": self.exchange,
            "side": self.side, "entry": self.entry, "stop_loss": self.stop_loss,
            "take_profit": self.take_profit, "take_profit_2": self.take_profit_2,
            "score": self.score, "strategy": self.strategy, "timestamp": self.timestamp,
            "reasoning": self.reasoning,
            "zones": self.zones, "liquidity_levels": self.liquidity_levels,
            "fvg_zones": self.fvg_zones, "order_blocks": self.order_blocks,
            "chart_annotations": self.chart_annotations,
            "setup_type": self.setup_type, "entry_mode": self.entry_mode,
            "status": self.status, "confidence": self.confidence, "bias": self.bias,
            "htf_context": self.htf_context, "liquidity_data": self.liquidity_data,
            "structure_data": self.structure_data, "execution": self.execution,
            "score_breakdown": self.score_breakdown,
            "reasons": self.reasons, "risk_flags": self.risk_flags, "debug": self.debug,
        }


# ─── Main Engine ─────────────────────────────────────────────

class LITEngine:
    """Production LIT Engine — strict execution support for live scalp/intraday."""

    def __init__(self, config: dict = None):
        cfg = config or {}

        # Layer 1: Structure
        self.structure_engine = StructureEngine(
            left_bars=int(cfg.get("swing_left_bars", 3)),
            right_bars=int(cfg.get("swing_right_bars", 3)),
            min_displacement_atr=float(cfg.get("min_displacement_atr", 1.0)),
            min_body_ratio=float(cfg.get("min_body_ratio", 0.5)),
        )

        # Layer 2: Liquidity
        self.liquidity_engine = LiquidityEngine(
            equal_tolerance_pct=float(cfg.get("equal_level_tolerance_pct", 0.1)),
            min_touches=2,
            sweep_penetration_min_atr=float(cfg.get("sweep_min_penetration_atr", 0.2)),
            max_age_bars_after_sweep=int(cfg.get("max_age_bars_after_sweep", 15)),
        )

        # Layer 3: Patterns/Setups
        self.setup_detector = SetupDetector(cfg)

        # Layer 4: Execution
        self.execution_engine = ExecutionEngine(cfg)

        # Layer 5: Visuals
        self.annotation_builder = AnnotationBuilder()

        # Config
        self.min_score = float(cfg.get("min_score", 0.50))
        self.annotation_enabled = bool(cfg.get("annotation_enabled", True))

        logger.info(f"LIT Engine v3 ready — min_score={self.min_score}, min_RR={self.execution_engine.min_rr}")

    def analyze(
        self,
        df: pd.DataFrame,
        symbol: str,
        exchange: str = "binance",
        htf_df: Optional[pd.DataFrame] = None,
    ) -> Optional[LITSignal]:
        """Full LIT analysis pipeline.

        Args:
            df: OHLCV trigger-TF DataFrame (e.g. 15m or 5m)
            symbol: e.g. "BTC/USDT"
            exchange: e.g. "binance"
            htf_df: Higher-TF DataFrame (e.g. 1h or 4h) for bias

        Returns:
            LITSignal if valid setup found, else None
        """
        if df is None or df.empty or len(df) < 40:
            return None

        # ── Parse OHLC ──
        opens = df["open"].values.astype(float)
        highs = df["high"].values.astype(float)
        lows = df["low"].values.astype(float)
        closes = df["close"].values.astype(float)
        timestamps = df["timestamp"].values.astype(float) if "timestamp" in df.columns else None

        current_price = float(closes[-1])
        n = len(closes)

        # ── ATR ──
        atr = self.structure_engine._calc_atr(highs, lows, closes, 14)
        if atr <= 0:
            return None

        # ═══ LAYER 1: STRUCTURE ═══
        # HTF bias
        htf_bias = TrendState.RANGING
        htf_structure = None
        if htf_df is not None and not htf_df.empty and len(htf_df) >= 30:
            htf_o = htf_df["open"].values.astype(float)
            htf_h = htf_df["high"].values.astype(float)
            htf_l = htf_df["low"].values.astype(float)
            htf_c = htf_df["close"].values.astype(float)
            htf_ts = htf_df["timestamp"].values.astype(float) if "timestamp" in htf_df.columns else None
            htf_structure = self.structure_engine.analyze(htf_o, htf_h, htf_l, htf_c, htf_ts)
            htf_bias = htf_structure.trend

        # Entry TF structure
        entry_structure = self.structure_engine.analyze(opens, highs, lows, closes, timestamps)

        # ═══ LAYER 2: LIQUIDITY ═══
        liq_map = self.liquidity_engine.analyze(
            opens, highs, lows, closes, entry_structure, atr,
            current_price, timeframe="15m", timestamps=timestamps,
        )

        # ═══ LAYER 3: SETUP DETECTION ═══
        candidate = self.setup_detector.detect(
            opens, highs, lows, closes,
            htf_bias, entry_structure, liq_map, atr, current_price, timestamps,
        )

        if candidate is None:
            return None

        # ═══ LAYER 4: EXECUTION ═══
        plan = self.execution_engine.compute(candidate, atr, current_price, liq_map)
        if not plan.is_valid:
            logger.debug(f"LIT {symbol}: execution invalid — {plan.rejection_reason}")
            return None

        # Score
        score = self.execution_engine.score(candidate, plan, atr)

        # No-trade filters
        if not self.execution_engine.passes_filters(candidate, plan, score):
            logger.debug(f"LIT {symbol}: failed filters — score={score.total:.2f}")
            return None

        # ═══ LAYER 5: BUILD SIGNAL ═══
        # Annotations
        annotations = []
        if self.annotation_enabled:
            ann_objects = self.annotation_builder.build(candidate, plan, liq_map, entry_structure)
            annotations = [a.to_dict() for a in ann_objects]

        # Build reasoning text (Persian)
        reasoning = self._build_reasoning(candidate, plan, score, htf_bias, atr)

        # Build structured output
        signal = LITSignal(
            id="LIT_" + uuid.uuid4().hex[:8],
            symbol=symbol,
            exchange=exchange,
            side=candidate.side,
            entry=round(plan.ideal_entry, 8),
            stop_loss=round(plan.stop_loss_buffered, 8),
            take_profit=round(plan.take_profit_1, 8),
            take_profit_2=round(plan.take_profit_2, 8),
            score=score.total,
            strategy=candidate.setup_type.value,
            timestamp=time.time(),
            reasoning=reasoning,
            chart_annotations=annotations,
            setup_type=candidate.setup_type.value,
            entry_mode=candidate.entry_mode.value,
            status=candidate.status.value,
            confidence=score.total,
            bias=htf_bias.value,
            reasons=candidate.reasons,
            risk_flags=candidate.risk_flags,
            htf_context={
                "trend": htf_bias.value,
                "key_levels": [
                    {"price": p.price, "kind": p.kind.value}
                    for p in (liq_map.buy_side_pools[:2] + liq_map.sell_side_pools[:2])
                ],
            },
            liquidity_data={
                "swept_zone": {
                    "price": candidate.sweep.pool.price,
                    "kind": candidate.sweep.pool.kind.value,
                    "quality": candidate.sweep.quality.value,
                } if candidate.sweep else {},
                "target_zones": [
                    {"price": p.price, "kind": p.kind.value}
                    for p in (liq_map.buy_side_pools[:3] if candidate.side == "long" else liq_map.sell_side_pools[:3])
                ],
            },
            structure_data={
                "trend": entry_structure.trend.value,
                "choch": {"index": candidate.structure_break.index, "kind": candidate.structure_break.kind.value} if candidate.structure_break else None,
                "recent_swings": len(entry_structure.swing_highs) + len(entry_structure.swing_lows),
            },
            execution={
                "entry_zone_low": plan.entry_zone_low,
                "entry_zone_high": plan.entry_zone_high,
                "ideal_entry": plan.ideal_entry,
                "market_price": plan.market_price,
                "stop_loss": plan.stop_loss,
                "stop_loss_buffered": plan.stop_loss_buffered,
                "stop_distance_pct": plan.stop_distance_pct,
                "stop_distance_atr": plan.stop_distance_atr,
                "take_profit_1": plan.take_profit_1,
                "take_profit_2": plan.take_profit_2,
                "take_profit_3": plan.take_profit_3,
                "rr_tp1": plan.rr_tp1,
                "rr_tp2": plan.rr_tp2,
                "rr_tp3": plan.rr_tp3,
                "invalidation_level": plan.invalidation_level,
                "position_size_usdt": plan.position_size_usdt,
                "leverage_suggested": plan.leverage_suggested,
            },
            score_breakdown={
                "htf_alignment": score.htf_alignment,
                "liquidity_quality": score.liquidity_quality,
                "sweep_quality": score.sweep_quality,
                "displacement_strength": score.displacement_strength,
                "structure_confirmation": score.structure_confirmation,
                "fvg_ob_confluence": score.fvg_ob_confluence,
                "rr_quality": score.rr_quality,
                "total": score.total,
                "explanation": score.explanation,
            },
            fvg_zones=[{"top": candidate.fvg.top, "bottom": candidate.fvg.bottom, "direction": candidate.fvg.direction}] if candidate.fvg else [],
            order_blocks=[{"top": candidate.order_block.top, "bottom": candidate.order_block.bottom, "direction": candidate.order_block.direction}] if candidate.order_block else [],
            debug={
                "atr": round(atr, 8),
                "n_candles": n,
                "displacement_metrics": {
                    "body_atr": candidate.displacement.body_atr_ratio if candidate.displacement else 0,
                    "body_range": candidate.displacement.body_range_ratio if candidate.displacement else 0,
                } if candidate.displacement else {},
                "fvg_metrics": {"size": candidate.fvg.size, "midpoint": candidate.fvg.midpoint} if candidate.fvg else {},
            },
        )

        logger.info(
            f"LIT SIGNAL {symbol} {candidate.side.upper()} | "
            f"setup={candidate.setup_type.value} | score={score.total:.2f} | "
            f"RR={plan.rr_tp2:.1f} | entry={plan.ideal_entry:.6g} | "
            f"SL={plan.stop_loss_buffered:.6g} | TP2={plan.take_profit_2:.6g}"
        )
        return signal

    def _build_reasoning(
        self, candidate: SetupCandidate, plan: ExecutionPlan,
        score: ScoreBreakdown, htf_bias: TrendState, atr: float,
    ) -> str:
        """Build Persian educational reasoning."""
        setup_names = {
            SetupType.SWEEP_REVERSAL: "Sweep-Reversal (شکار و برگشت)",
            SetupType.INDUCEMENT_CONTINUATION: "Inducement-Continuation (تله و ادامه)",
            SetupType.RANGE_EXPANSION: "Range-Expansion (محدوده به انبساط)",
        }
        bias_fa = {"bullish": "صعودی 🟢", "bearish": "نزولی 🔴", "ranging": "خنثی ➡️"}
        side_fa = "خرید (LONG) 🟢" if candidate.side == "long" else "فروش (SHORT) 🔴"
        conf_fa = "بالا ⭐" if score.total >= 0.75 else "متوسط" if score.total >= 0.55 else "پایین"

        parts = [
            f"📐 Setup: {setup_names.get(candidate.setup_type, candidate.setup_type.value)}",
            f"📊 HTF Bias: {bias_fa.get(htf_bias.value, htf_bias.value)}",
            f"🎯 جهت: {side_fa}",
            f"⚖️ R:R = {plan.rr_tp2:.1f}:1 (TP2)",
            f"💰 اهرم پیشنهادی: {plan.leverage_suggested}x",
            f"🎯 اعتماد: {conf_fa} ({score.total:.0%})",
            "",
            "📋 دلایل ورود:",
        ]
        for reason in candidate.reasons:
            parts.append(f"  • {reason}")

        if candidate.risk_flags:
            parts.append("")
            parts.append("⚠️ ریسک‌ها:")
            for flag in candidate.risk_flags:
                parts.append(f"  • {flag}")

        parts.extend([
            "",
            f"📊 ATR: {atr:.8g}",
            f"📏 فاصله SL: {plan.stop_distance_pct:.2f}% ({plan.stop_distance_atr:.1f} ATR)",
        ])

        return "\n".join(parts)
