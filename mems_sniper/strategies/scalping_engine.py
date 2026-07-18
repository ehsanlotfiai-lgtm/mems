"""Scalping engine — dedicated confluence engine for 1m/5m scalp signals.

Focused on high-volume coins only, with tighter SL/TP and faster evaluation.
Only signals in the direction of higher timeframe trend (4h/1d).
"""
from __future__ import annotations

import time
import uuid
from typing import Awaitable, Callable, Dict, List, Optional

import numpy as np
import pandas as pd

from config.settings import Settings
from core.exchange import SymbolInfo
from core.logging_setup import logger
from core.models import Signal, Side, StrategyHit, now_sec
from strategies.indicators import atr as atr_indicator
import strategies.indicators as ind
from strategies.scalping_strategies import build_scalp_strategies, BaseScalpStrategy
from strategies.strategies import _candles_to_df


CandleProvider = Callable[[str, str], Awaitable[Dict[str, "pd.DataFrame | list"]]]


class ScalpingEngine:
    """Aggregate scalping strategy hits into fast scalp signals.
    
    Only emits signals in the direction of the higher timeframe trend (4h/1d).
    """

    def __init__(
        self,
        settings: Settings,
        candle_provider: CandleProvider,
        orderbook_provider: Optional[Callable[[str, str], Awaitable[dict]]] = None,
    ) -> None:
        self.s = settings
        self.provider = candle_provider
        self.ob_provider = orderbook_provider
        scalp_cfg = settings.raw.get("scalping", {})
        self.strategies: List[BaseScalpStrategy] = build_scalp_strategies(
            scalp_cfg.get("strategies", {})
        )
        self.timeframes = scalp_cfg.get("timeframes", ["1m", "5m"])
        self.trigger_tf = scalp_cfg.get("trigger_timeframe", "1m")
        self.min_score = float(scalp_cfg.get("min_signal_score", 0.35))
        self.weights: Dict[str, float] = scalp_cfg.get("confluence_weights", {"1m": 0.6, "5m": 0.4})
        self.sl_atr_mult = float(scalp_cfg.get("sl_atr_mult", 1.0))
        self.tp_atr_mult = float(scalp_cfg.get("tp_atr_mult", 1.5))
        self.atr_length = int(scalp_cfg.get("atr_length", 7))
        # Higher timeframe trend filter
        self.use_htf_filter = bool(scalp_cfg.get("use_htf_trend_filter", True))
        self.htf_timeframes = scalp_cfg.get("htf_timeframes", ["4h", "1d"])
        self.min_unique_strategies = int(scalp_cfg.get("min_unique_strategies", 1))
        self.min_volume_ratio = float(scalp_cfg.get("min_volume_ratio", 1.0))
        logger.info(
            f"ScalpingEngine ready with {len(self.strategies)} strategies, TFs={self.timeframes}, "
            f"HTF filter={'ON' if self.use_htf_filter else 'OFF'} ({self.htf_timeframes})"
        )

    async def evaluate_symbol(
        self,
        exchange: str,
        info: SymbolInfo,
    ) -> Optional[Signal]:
        multi = await self.provider(exchange, info.symbol)
        dfs: Dict[str, pd.DataFrame] = {}
        for tf in self.timeframes:
            candles = multi.get(tf, [])
            df = _candles_to_df(candles)
            if not df.empty:
                df = df.astype({"open": float, "high": float, "low": float,
                                 "close": float, "volume": float})
            dfs[tf] = df

        trigger_df = dfs.get(self.trigger_tf)
        if trigger_df is None or trigger_df.empty or len(trigger_df) < 5:
            return None

        # ──── Higher Timeframe Trend Filter (4h / 1d) ────
        htf_trend = "neutral"
        if self.use_htf_filter:
            for htf in self.htf_timeframes:
                htf_candles = multi.get(htf, [])
                htf_df = _candles_to_df(htf_candles)
                if not htf_df.empty and len(htf_df) >= 30:
                    htf_close = htf_df["close"].astype(float)
                    htf_trend = ind.higher_tf_trend(htf_close, 30)
                    if htf_trend != "neutral":
                        break

        # Orderbook (best-effort)
        ob = None
        if self.ob_provider is not None:
            try:
                ob = await self.ob_provider(exchange, info.symbol)
            except Exception:
                pass

        all_hits: List[StrategyHit] = []
        tf_breakdown: Dict[str, float] = {tf: 0.0 for tf in self.timeframes}

        for tf, df in dfs.items():
            ctx = {
                "timeframe": tf,
                "orderbook": ob if tf == self.trigger_tf else None,
            }
            for strat in self.strategies:
                try:
                    hit = strat.evaluate(df, ctx)
                except Exception as exc:
                    logger.debug(f"scalp strategy {strat.name} error on {info.symbol} {tf}: {exc}")
                    hit = None
                if hit is not None:
                    all_hits.append(hit)

        if not all_hits:
            return None

        # Gate: enough different strategies must fire
        unique_strategies = len(set(h.name for h in all_hits))
        if unique_strategies < self.min_unique_strategies:
            return None

        # Gate: volume check — reject low-volume spikes (soft check)
        vol_avg = trigger_df["volume"].astype(float).rolling(20).mean().iloc[-1]
        vol_now = float(trigger_df["volume"].astype(float).iloc[-1])
        if vol_avg > 0 and vol_now / vol_avg < self.min_volume_ratio:
            # Allow through if multiple strategies agree strongly
            if unique_strategies < 3:
                return None

        # Gate: at least 1 timeframe must agree
        tfs_with_hits = set(h.timeframe for h in all_hits)
        if len(tfs_with_hits) < 1:
            return None

        # Direction by weighted vote
        long_votes = sum(h.score * h.weight for h in all_hits if h.detail.get("side") in (None, "long"))
        short_votes = sum(h.score * h.weight for h in all_hits if h.detail.get("side") == "short")
        side = Side.LONG if long_votes >= short_votes else Side.SHORT
        if abs(long_votes - short_votes) < 1e-6:
            return None

        # ──── HTF Trend Alignment Filter ────
        # Reject counter-trend signals
        if self.use_htf_filter and htf_trend != "neutral":
            if htf_trend == "bearish" and side == Side.LONG:
                logger.debug(f"Scalp REJECTED {info.symbol}: LONG vs bearish HTF trend")
                return None
            if htf_trend == "bullish" and side == Side.SHORT:
                logger.debug(f"Scalp REJECTED {info.symbol}: SHORT vs bullish HTF trend")
                return None

        # Per-TF weighted score
        for tf in self.timeframes:
            hits_tf = [h for h in all_hits if h.timeframe == tf]
            if not hits_tf:
                tf_breakdown[tf] = 0.0
                continue
            s = sum(h.score * h.weight for h in hits_tf) / sum(h.weight for h in hits_tf)
            tf_breakdown[tf] = float(np.clip(s, 0, 1))

        # Final score — use max of weighted average or single-TF score
        active_tfs = [tf for tf in self.timeframes if tf_breakdown.get(tf, 0) > 0]
        total_active_w = sum(float(self.weights.get(tf, 0)) for tf in active_tfs) or 1.0
        weighted_score = sum(tf_breakdown.get(tf, 0) * float(self.weights.get(tf, 0)) for tf in active_tfs)
        final = float(weighted_score / total_active_w)

        # Boost for more strategies
        if unique_strategies >= 4:
            final = min(1.0, final + 0.10)
        elif unique_strategies >= 3:
            final = min(1.0, final + 0.06)
        elif unique_strategies >= 2:
            final = min(1.0, final + 0.04)

        # Boost: aligned with HTF trend
        if htf_trend == "bullish" and side == Side.LONG:
            final = min(1.0, final + 0.08)
        elif htf_trend == "bearish" and side == Side.SHORT:
            final = min(1.0, final + 0.08)

        if final < self.min_score:
            return None

        # Risk fields — tight SL/TP for scalping
        atr_tf = self.trigger_tf
        atr_df = dfs.get(atr_tf, trigger_df)
        prices = trigger_df["close"].astype(float)
        highs = atr_df["high"].astype(float)
        lows = atr_df["low"].astype(float)
        atr_close = atr_df["close"].astype(float)
        atr_val = float(atr_indicator(highs, lows, atr_close, length=self.atr_length).iloc[-1])
        if not np.isfinite(atr_val) or atr_val <= 0:
            return None

        entry = float(prices.iloc[-1])
        # Multi-TP levels for scalping — default ATR-based
        tp1_mult = self.tp_atr_mult * 0.5   # TP1 = half of TP distance (risk-free)
        tp2_mult = self.tp_atr_mult * 0.8   # TP2 = 80% of TP distance (partial close)
        tp3_mult = self.tp_atr_mult         # TP3 = full TP distance (full close)
        if side == Side.LONG:
            sl = entry - self.sl_atr_mult * atr_val
            tp1 = entry + tp1_mult * atr_val
            tp2 = entry + tp2_mult * atr_val
            tp3 = entry + tp3_mult * atr_val
            tp = tp1  # take_profit field = TP1
        else:
            sl = entry + self.sl_atr_mult * atr_val
            tp1 = entry - tp1_mult * atr_val
            tp2 = entry - tp2_mult * atr_val
            tp3 = entry - tp3_mult * atr_val
            tp = tp1  # take_profit field = TP1

        # ── Structural SL/TP override for PRO BTB and SP2L ──
        # These strategies compute their own SL (behind the spike / Wave A)
        # and TP (R:R based) directly from market structure. Use those
        # instead of the generic ATR-based levels, since a structural stop
        # is far more meaningful than an arbitrary ATR multiple — and this
        # guarantees SL/entry/TP are always on the logically correct side.
        side_hits = [h for h in all_hits if h.detail.get("side") == ("long" if side == Side.LONG else "short")]
        btb_hit = next((h for h in side_hits if h.name == "scalp_pro_btb"), None)
        sp2l_hit = next((h for h in side_hits if h.name == "scalp_sp2l"), None)
        micromap_hit = next((h for h in side_hits if h.name == "scalp_micromap"), None)

        def _valid_long(sl_v, tp_v):
            return sl_v < entry < tp_v

        def _valid_short(sl_v, tp_v):
            return tp_v < entry < sl_v

        if btb_hit is not None and "sl" in btb_hit.detail and "tp" in btb_hit.detail:
            d_sl, d_tp = float(btb_hit.detail["sl"]), float(btb_hit.detail["tp"])
            ok = _valid_long(d_sl, d_tp) if side == Side.LONG else _valid_short(d_sl, d_tp)
            if ok:
                sl = d_sl
                tp1 = tp = d_tp
                risk = abs(entry - sl)
                tp2 = entry + risk * (3.0 if side == Side.LONG else -3.0)
                tp3 = entry + risk * (4.0 if side == Side.LONG else -4.0)
        elif sp2l_hit is not None and "sl" in sp2l_hit.detail and "tp1" in sp2l_hit.detail:
            d_sl = float(sp2l_hit.detail["sl"])
            d_tp1 = float(sp2l_hit.detail["tp1"])
            d_tp2 = float(sp2l_hit.detail.get("tp2", d_tp1))
            ok = _valid_long(d_sl, d_tp1) if side == Side.LONG else _valid_short(d_sl, d_tp1)
            if ok:
                sl = d_sl
                tp1 = tp = d_tp1
                tp2 = d_tp2
                risk = abs(entry - sl)
                tp3 = entry + risk * (3.0 if side == Side.LONG else -3.0)
        elif micromap_hit is not None and "sl" in micromap_hit.detail and "tp" in micromap_hit.detail:
            # MicroMap's SL is the reference micro-channel candle's own
            # low/high (structural), TP is the configured min R:R target —
            # both far more meaningful than a generic ATR multiple.
            d_sl, d_tp = float(micromap_hit.detail["sl"]), float(micromap_hit.detail["tp"])
            ok = _valid_long(d_sl, d_tp) if side == Side.LONG else _valid_short(d_sl, d_tp)
            if ok:
                sl = d_sl
                tp1 = tp = d_tp
                risk = abs(entry - sl)
                tp2 = entry + risk * (2.5 if side == Side.LONG else -2.5)
                tp3 = entry + risk * (3.5 if side == Side.LONG else -3.5)

        risk_pct = float(self.s.risk.get("risk_per_trade_pct", 1.0)) / 100.0
        size_usdt = float(self.s.risk.get("initial_paper_balance", 10000)) * risk_pct

        # ── Commission filter: reject if TP1 profit < 2x commission cost ──
        commission_pct = 0.075  # Binance futures taker fee (0.075% per side = 0.15% round trip)
        tp1_profit_pct = abs(tp - entry) / max(entry, 1e-10) * 100
        round_trip_commission = commission_pct * 2  # entry + exit
        if tp1_profit_pct <= round_trip_commission * 2:
            # Profit would be eaten by fees — skip
            return None

        # ── Leverage calculation based on SL distance and score ──
        sl_distance_pct = abs(entry - sl) / max(entry, 1e-10) * 100
        if sl_distance_pct > 0:
            # Max leverage = keep loss per trade under risk_pct of balance
            max_safe_leverage = min(int(risk_pct * 100 / sl_distance_pct * 5), 20)
        else:
            max_safe_leverage = 3
        # Suggest leverage based on score
        if final >= 0.70:
            suggested_leverage = min(max_safe_leverage, 10)
        elif final >= 0.50:
            suggested_leverage = min(max_safe_leverage, 7)
        else:
            suggested_leverage = min(max_safe_leverage, 5)
        suggested_leverage = max(suggested_leverage, 2)

        # ── Identify primary setup name from hits ──
        setup_names_priority = ["scalp_micromap", "scalp_pro_btb", "scalp_sp2l",
                                "scalp_vwap_rejection", "scalp_momentum_burst", "scalp_volume_climax",
                                "scalp_squeeze_release", "scalp_engulfing", "scalp_rsi_extreme",
                                "scalp_stoch_extreme", "scalp_ema_ribbon", "scalp_bb_touch", "scalp_order_flow"]
        primary_setup = "scalp"
        for sn in setup_names_priority:
            if any(h.name == sn for h in all_hits):
                primary_setup = sn
                break

        htf_label = {"bullish": "🟢 صعودی", "bearish": "🔴 نزولی", "neutral": "➡️ خنثی"}
        rationale = self._explain(all_hits, final, tf_breakdown, side, atr_val, htf_trend, htf_label)
        # Append leverage and setup info to rationale
        rationale += f" | ستاپ: {primary_setup} | اهرم: {suggested_leverage}x"

        signal = Signal(
            id="SCP_" + uuid.uuid4().hex[:8],
            created_at=now_sec(),
            exchange=exchange,
            symbol=info.symbol,
            side=side,
            price=entry,
            score=round(final, 4),
            hits=all_hits,
            confluence_tf_breakdown={k: round(v, 4) for k, v in tf_breakdown.items()},
            entry=entry,
            stop_loss=float(sl),
            take_profit=float(tp),
            trailing_atr=self.sl_atr_mult * atr_val,
            atr=atr_val,
            position_size_usdt=round(size_usdt, 2),
            risk_pct=risk_pct * 100,
            rationale=rationale,
            base=info.base,
            tp2=float(tp2),
            tp3=float(tp3),
        )
        return signal

    @staticmethod
    def _explain(hits, final, breakdown, side, atr_val, htf_trend="neutral", htf_label=None) -> str:
        top = sorted(hits, key=lambda h: h.score * h.weight, reverse=True)[:3]
        method_label = {
            "scalp_vwap_rejection": "واپس ریجکشن",
            "scalp_rsi_extreme": "RSI اکستریم",
            "scalp_momentum_burst": "انفجار مومنتوم",
            "scalp_stoch_extreme": "استوکاستیک اکستریم",
            "scalp_ema_ribbon": "ریبون EMA",
            "scalp_bb_touch": "لمس بولینگر",
            "scalp_volume_climax": "کلایمکس حجم",
            "scalp_order_flow": "جریان سفارش",
            "scalp_squeeze_release": "ریلیز Squeeze",
            "scalp_engulfing": "الگوی انگالفینگ",
        }
        bits = []
        for h in top:
            label = method_label.get(h.name, h.name)
            bits.append(f"{label} ({h.timeframe}, {h.score:.2f})")
        breakdown_str = "، ".join(f"{tf}: {v:.2f}" for tf, v in breakdown.items() if v > 0)
        side_fa = "خرید (LONG)" if side == Side.LONG else "فروش (SHORT)"
        if htf_label is None:
            htf_label = {"bullish": "🟢 صعودی", "bearish": "🔴 نزولی", "neutral": "➡️ خنثی"}
        htf_str = htf_label.get(htf_trend, "نامشخص")
        # Leverage recommendation for scalp
        if final >= 0.65:
            lev_rec = "اهرم: 5x-10x"
        elif final >= 0.45:
            lev_rec = "اهرم: 3x-5x"
        else:
            lev_rec = "اسپات یا 2x-3x"
        return (
            f"⚡ اسکلپ {side_fa} | امتیاز: {final:.2f} | "
            f"روش‌ها: {'، '.join(bits)} | TFها: {breakdown_str} | "
            f"روند ۴س/۱روز: {htf_str} | {lev_rec} | ATR={atr_val:.6f}"
        )
