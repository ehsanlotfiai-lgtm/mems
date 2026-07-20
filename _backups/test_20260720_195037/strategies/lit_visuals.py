"""LIT Visuals — Chart Annotation Builder.

Produces structured annotation objects for frontend chart drawing:
  - Liquidity levels (PDH/PDL, equal highs/lows, session)
  - Swept zones with markers
  - CHoCH / BOS labels
  - Displacement candle highlight
  - FVG boxes
  - OB boxes
  - Entry zone
  - Stop loss / TP1 / TP2 / TP3 lines
  - Invalidation line
  - Session boxes
  - Text labels with concise reasons
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, List, Optional

from strategies.lit_structure import StructureState, StructureBreak, Displacement, StructureBreakType
from strategies.lit_liquidity import LiquidityMap, SweepEvent, LiquidityPool
from strategies.lit_patterns import SetupCandidate, FVG, OrderBlock
from strategies.lit_execution import ExecutionPlan, ScoreBreakdown


@dataclass
class ChartAnnotation:
    """A single annotation element for the frontend chart."""
    type: str       # "line" | "box" | "label" | "arrow" | "zone"
    tag: str        # semantic tag: "pdh" | "eql" | "sweep" | "choch" | "bos" | "fvg" | "ob" | "entry" | "sl" | "tp1" etc
    price: float
    price2: float = 0.0      # for boxes/zones
    time: float = 0.0        # unix timestamp
    time2: float = 0.0       # end time for boxes
    text: str = ""
    color: str = ""
    style: str = "solid"     # "solid" | "dashed" | "dotted"
    priority: int = 1        # 1=highest

    def to_dict(self) -> Dict[str, Any]:
        return {
            "type": self.type, "tag": self.tag,
            "price": self.price, "price2": self.price2,
            "time": self.time, "time2": self.time2,
            "text": self.text, "color": self.color,
            "style": self.style, "priority": self.priority,
        }


class AnnotationBuilder:
    """Builds chart annotations from a complete LIT signal."""

    def build(
        self,
        candidate: SetupCandidate,
        plan: ExecutionPlan,
        liq_map: LiquidityMap,
        structure: StructureState,
    ) -> List[ChartAnnotation]:
        """Build all annotations for chart display."""
        annotations: List[ChartAnnotation] = []

        # 1. Liquidity levels
        annotations.extend(self._liquidity_annotations(liq_map))

        # 2. Sweep marker
        if candidate.sweep:
            annotations.append(self._sweep_annotation(candidate.sweep))

        # 3. Structure events (CHoCH/BOS)
        if candidate.structure_break:
            annotations.append(self._structure_annotation(candidate.structure_break))

        # 4. Displacement highlight
        if candidate.displacement:
            annotations.append(self._displacement_annotation(candidate.displacement))

        # 5. FVG box
        if candidate.fvg:
            annotations.append(self._fvg_annotation(candidate.fvg))

        # 6. OB box
        if candidate.order_block:
            annotations.append(self._ob_annotation(candidate.order_block))

        # 7. Execution levels
        if plan.is_valid:
            annotations.extend(self._execution_annotations(plan, candidate.side))

        return annotations

    def _liquidity_annotations(self, liq_map: LiquidityMap) -> List[ChartAnnotation]:
        """Draw liquidity levels."""
        anns = []
        # Top 3 buy-side
        for pool in liq_map.buy_side_pools[:3]:
            anns.append(ChartAnnotation(
                type="line", tag="buy_side_liq",
                price=pool.price,
                text=f"BSL: {pool.kind.value} (x{pool.strength})",
                color="#ef4444", style="dashed", priority=3,
                time=pool.timestamp,
            ))
        # Top 3 sell-side
        for pool in liq_map.sell_side_pools[:3]:
            anns.append(ChartAnnotation(
                type="line", tag="sell_side_liq",
                price=pool.price,
                text=f"SSL: {pool.kind.value} (x{pool.strength})",
                color="#22c55e", style="dashed", priority=3,
                time=pool.timestamp,
            ))
        return anns

    def _sweep_annotation(self, sweep: SweepEvent) -> ChartAnnotation:
        """Mark the sweep event."""
        color = "#f59e0b" if sweep.quality.value == "major" else "#fbbf24"
        return ChartAnnotation(
            type="arrow", tag="sweep",
            price=sweep.pool.price,
            price2=sweep.pool.price - sweep.penetration if sweep.side.value == "sell_side" else sweep.pool.price + sweep.penetration,
            text=f"SWEEP ({sweep.quality.value}) — {sweep.pool.kind.value}",
            color=color, priority=1,
            time=sweep.timestamp,
        )

    def _structure_annotation(self, event: StructureBreak) -> ChartAnnotation:
        """Label CHoCH or BOS."""
        is_choch = event.kind in (StructureBreakType.BULLISH_CHOCH, StructureBreakType.BEARISH_CHOCH)
        tag = "choch" if is_choch else "bos"
        label = "CHoCH" if is_choch else "BOS"
        color = "#a855f7" if is_choch else "#3b82f6"
        direction = "↑" if "bullish" in event.kind.value else "↓"
        return ChartAnnotation(
            type="label", tag=tag,
            price=event.price,
            text=f"{direction} {label}",
            color=color, priority=1,
            time=event.timestamp,
        )

    def _displacement_annotation(self, disp: Displacement) -> ChartAnnotation:
        """Highlight displacement candle."""
        color = "#10b981" if disp.direction == "bullish" else "#ef4444"
        return ChartAnnotation(
            type="box", tag="displacement",
            price=disp.body_size,  # Will be overridden by frontend using index
            text=f"DISPLACEMENT {disp.body_atr_ratio:.1f}x ATR",
            color=color, style="solid", priority=2,
        )

    def _fvg_annotation(self, fvg: FVG) -> ChartAnnotation:
        """Draw FVG box."""
        color = "#8b5cf6" if fvg.direction == "bullish" else "#ec4899"
        return ChartAnnotation(
            type="box", tag="fvg",
            price=fvg.top, price2=fvg.bottom,
            text=f"FVG ({fvg.direction})",
            color=color, style="solid", priority=1,
            time=fvg.timestamp,
        )

    def _ob_annotation(self, ob: OrderBlock) -> ChartAnnotation:
        """Draw Order Block box."""
        color = "#06b6d4" if ob.direction == "bullish" else "#f97316"
        return ChartAnnotation(
            type="box", tag="ob",
            price=ob.top, price2=ob.bottom,
            text=f"OB ({ob.direction})",
            color=color, style="solid", priority=2,
            time=ob.timestamp,
        )

    def _execution_annotations(self, plan: ExecutionPlan, side: str) -> List[ChartAnnotation]:
        """Draw entry zone, SL, TP lines."""
        anns = []

        # Entry zone
        anns.append(ChartAnnotation(
            type="box", tag="entry_zone",
            price=plan.entry_zone_high, price2=plan.entry_zone_low,
            text="ENTRY ZONE", color="#3b82f6", style="solid", priority=1,
        ))

        # Ideal entry
        anns.append(ChartAnnotation(
            type="line", tag="entry",
            price=plan.ideal_entry,
            text=f"Entry: {plan.ideal_entry:.6g}", color="#3b82f6", style="solid", priority=1,
        ))

        # Stop loss
        anns.append(ChartAnnotation(
            type="line", tag="sl",
            price=plan.stop_loss_buffered,
            text=f"SL: {plan.stop_loss_buffered:.6g} ({plan.stop_distance_pct:.1f}%)",
            color="#ef4444", style="solid", priority=1,
        ))

        # Invalidation
        anns.append(ChartAnnotation(
            type="line", tag="invalidation",
            price=plan.invalidation_level,
            text="INVALIDATION", color="#dc2626", style="dashed", priority=2,
        ))

        # Take profits
        tp_color = "#10b981"
        anns.append(ChartAnnotation(
            type="line", tag="tp1",
            price=plan.take_profit_1,
            text=f"TP1 ({plan.rr_tp1:.1f}R) — 50%",
            color=tp_color, style="dashed", priority=1,
        ))
        anns.append(ChartAnnotation(
            type="line", tag="tp2",
            price=plan.take_profit_2,
            text=f"TP2 ({plan.rr_tp2:.1f}R) — 30%",
            color="#059669", style="dashed", priority=1,
        ))
        anns.append(ChartAnnotation(
            type="line", tag="tp3",
            price=plan.take_profit_3,
            text=f"TP3 ({plan.rr_tp3:.1f}R) — 20% runner",
            color="#047857", style="dashed", priority=2,
        ))

        return anns
