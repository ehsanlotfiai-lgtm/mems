"""Multi-timeframe confluence engine.

Given a symbol across exchanges, the engine:

  * Pulls OHLCV for every configured TF (from cache or live REST)
  * Runs every enabled strategy on every TF
  * Aggregates StrategyHit -> per-TF score -> final confluence score
  * Calls the RiskEngine to compute SL/TP/size/ATR
  * Emits a `Signal` when the weighted score passes config threshold.

This module consumes whatever data source provided (REST or WS in-memory
store) via the `candle_provider` callable so it can be reused by both
the backtester and the live forward engine.
"""
from __future__ import annotations

import time
import uuid
from typing import Awaitable, Callable, Dict, List, Optional, Tuple

import numpy as np
import pandas as pd

from config.settings import Settings
from core.exchange import SymbolInfo
from core.logging_setup import logger
from core.models import (Signal, Side, StrategyHit, now_sec)
from strategies.indicators import atr as atr_indicator
import strategies.indicators as ind
from strategies.strategies import build_strategies, BaseStrategy, _candles_to_df


# alias enum for "is this a new-listing hit"
class SignalTypeForName:
    NEW_LISTING = "new_listing"


# A candle provider returns {tf: List[Candle]} for a given (exchange, symbol).
# It can be REST-backed (live re-fetch) or memory-backed (WS cache).
CandleProvider = Callable[[str, str], Awaitable[Dict[str, "pd.DataFrame | list"]]]


class ConfluenceEngine:
    """Aggregate strategy hits across timeframes into final signals."""

    def __init__(
        self,
        settings: Settings,
        candle_provider: CandleProvider,
        orderbook_provider: Optional[Callable[[str, str], Awaitable[dict]]] = None,
        extra_context_provider: Optional[Callable[[str, str, str], Awaitable[dict]]] = None,
    ) -> None:
        self.s = settings
        self.provider = candle_provider
        self.ob_provider = orderbook_provider
        self.extra_ctx = extra_context_provider
        self.strategies: List[BaseStrategy] = build_strategies(settings.strategies)
        self.weights: Dict[str, float] = settings.confluence_weights
        logger.info(f"Confluence engine ready with {len(self.strategies)} strategies, TFs={settings.timeframes}")

    # ---------------------------------------------------- public API
    async def evaluate_symbol(
        self,
        exchange: str,
        info: SymbolInfo,
        timeframes: Optional[List[str]] = None,
    ) -> Optional[Signal]:
        tfs = timeframes or self.s.timeframes
        multi = await self.provider(exchange, info.symbol)
        # build dataframes per tf
        dfs: Dict[str, pd.DataFrame] = {}
        for tf in tfs:
            candles = multi.get(tf, [])
            df = _candles_to_df(candles)
            if not df.empty:
                df = df.astype({"open": float, "high": float, "low": float,
                                 "close": float, "volume": float})
            dfs[tf] = df

        trigger_tf = self.s.trigger_timeframe
        trigger_df = dfs.get(trigger_tf)
        if trigger_df is None or trigger_df.empty or len(trigger_df) < 5:
            return None

        # order book (best-effort)
        ob: Optional[dict] = None
        if self.ob_provider is not None:
            try:
                ob = await self.ob_provider(exchange, info.symbol)
            except Exception as exc:  # noqa: BLE001
                logger.debug(f"orderbook fetch failed for {info.symbol}: {exc}")

        all_hits: List[StrategyHit] = []
        tf_breakdown: Dict[str, float] = {tf: 0.0 for tf in tfs}

        # Fetch extra context (news/sentiment) once for all TFs
        extra_data = {}
        if self.extra_ctx is not None:
            try:
                extra_data = await self.extra_ctx(exchange, info.symbol, trigger_tf)
            except Exception:  # noqa: BLE001
                pass

        for tf, df in dfs.items():
            ctx = {
                "timeframe": tf,
                "listed_at": info.listed_at,
                "orderbook": ob if tf == trigger_tf else None,
            }
            ctx.update(extra_data)
            for strat in self.strategies:
                try:
                    hit = strat.evaluate(df, ctx)
                except Exception as exc:  # noqa: BLE001
                    logger.debug(f"strategy {strat.name} error on {info.symbol} {tf}: {exc}")
                    hit = None
                if hit is not None:
                    all_hits.append(hit)

        if not all_hits:
            return None

        # ──── Confirmation Gate 1: Minimum hit count ────
        # At least 2 different strategies must fire to avoid noise
        unique_strategies = len(set(h.name for h in all_hits))
        if unique_strategies < 2:
            return None

        # ──── Confirmation Gate 2: Multi-TF agreement ────
        # Prefer 2 TFs but allow single-TF if strategy confluence is strong
        tfs_with_hits = set(h.timeframe for h in all_hits)
        if len(tfs_with_hits) < 2 and unique_strategies < 3:
            return None

        # ──── Confirmation Gate 3: Higher-TF trend alignment ────
        # Use 2h or 4h data for trend, NOT the trigger TF
        htf_trend = "neutral"
        for htf in ["2h", "4h"]:
            if htf in dfs and len(dfs[htf]) >= 50:
                htf_trend = ind.higher_tf_trend(dfs[htf]["close"].astype(float), 50)
                break

        # ──── Confirmation Gate 4: Conflict filter ────
        # Determine overall side by vote weighted by strategy score.
        # BB breakout direction is determined by detail.side — no double-counting
        long_votes = sum(h.score * h.weight for h in all_hits
                         if h.detail.get("side") in (None, "long") or "spring" in str(h.detail.get("type", "")) )
        short_votes = sum(h.score * h.weight for h in all_hits
                          if h.detail.get("side") == "short" or "upthrust" in str(h.detail.get("type", "")) )
        # Direction of bb_breakout determined by detail keys
        long_votes += sum(h.score * h.weight for h in all_hits
                          if h.name == "bb_breakout" and h.detail.get("side") == "long")
        short_votes += sum(h.score * h.weight for h in all_hits
                           if h.name == "bb_breakout" and h.detail.get("side") == "short")
        side = Side.LONG if long_votes >= short_votes else Side.SHORT
        if abs(long_votes - short_votes) < 1e-6:
            return None
        # If more than 30% of strategies disagree on direction, reject
        total_votes = long_votes + short_votes
        if total_votes > 0:
            losing_votes = min(long_votes, short_votes) / total_votes
            if losing_votes > 0.30:
                return None

        # ──── Confirmation Gate 5: Volume confirmation (soft check, not rejection) ────
        # Volume strategies boost score but don't reject signal
        volume_strategies = {"volume_spike", "volume_trend", "squeeze_momentum", "new_listing_sniper"}
        has_volume = any(h.name in volume_strategies for h in all_hits)
        if not has_volume:
            # Don't reject — just apply penalty post-score
            pass

        # ──── Confirmation Boost/Penalty: Trend alignment ────
        trend_aligned = (htf_trend == "bullish" and side == Side.LONG) or \
                        (htf_trend == "bearish" and side == Side.SHORT)
        trend_conflicting = (htf_trend == "bullish" and side == Side.SHORT) or \
                           (htf_trend == "bearish" and side == Side.LONG)

        # Per-TF weighted score (averaged over strategies that fired there).
        for tf in tfs:
            hits_tf = [h for h in all_hits if h.timeframe == tf]
            if not hits_tf:
                tf_breakdown[tf] = 0.0
                continue
            s = sum(h.score * h.weight for h in hits_tf) / sum(h.weight for h in hits_tf)
            tf_breakdown[tf] = float(np.clip(s, 0, 1))

        # Final score = weighted combination across TFs (weights sum to 1).
        total_w = sum(self.weights.values()) or 1.0
        final = sum(tf_breakdown.get(tf, 0) * float(self.weights.get(tf, 0)) for tf in tfs)
        final = float(final / total_w if total_w else final)

        # Boost if new-listing fired (highest-alpha event).
        for h in all_hits:
            if h.name == SignalTypeForName.NEW_LISTING:
                final = min(1.0, final + 0.10)

        # ──── Confirmation Boost: Trend alignment ────
        if trend_aligned:
            final = min(1.0, final + 0.12)  # +12% if trend agrees
        elif trend_conflicting:
            final = max(0.0, final - 0.15)  # -15% if trend conflicts

        # ──── Confirmation Boost: Multi-strategy agreement ────
        # More unique strategies = higher confidence
        if unique_strategies >= 5:
            final = min(1.0, final + 0.08)
        elif unique_strategies >= 4:
            final = min(1.0, final + 0.05)
        elif unique_strategies >= 3:
            final = min(1.0, final + 0.03)

        # ──── Confirmation Boost: Volume confirmation ────
        if trigger_df is not None and len(trigger_df) >= 20:
            vol = trigger_df["volume"].astype(float)
            vol_ratio = float(vol.iloc[-1]) / max(float(vol.mean()), 1)
            if vol_ratio > 2:
                final = min(1.0, final + 0.05)
            elif vol_ratio < 0.5:
                final = max(0.0, final - 0.05)  # low volume = less reliable

        # ──── Volume strategy penalty: no volume strategy fired = -5% ────
        if not has_volume:
            final = max(0.0, final - 0.05)

        if final < self.s.min_signal_score:
            return None

        # Build risk fields — use 4h ATR for more stable SL/TP levels
        atr_tf = "4h" if "4h" in dfs and not dfs["4h"].empty else "2h" if "2h" in dfs and not dfs["2h"].empty else trigger_tf
        atr_df = dfs.get(atr_tf, trigger_df)
        prices = trigger_df["close"].astype(float)
        highs = atr_df["high"].astype(float)
        lows = atr_df["low"].astype(float)
        atr_close = atr_df["close"].astype(float)
        atr_val = float(atr_indicator(highs, lows, atr_close, length=int(self.s.risk.get("atr_length", 14))).iloc[-1])
        if not np.isfinite(atr_val) or atr_val <= 0:
            return None
        entry = float(prices.iloc[-1])
        sl_mult = float(self.s.risk.get("stop_loss_atr_mult", 2.0))
        tp1_mult = float(self.s.risk.get("tp1_atr_mult", 1.5))
        tp2_mult = float(self.s.risk.get("tp2_atr_mult", 3.0))
        tp3_mult = float(self.s.risk.get("tp3_atr_mult", 4.5))
        trail_mult = float(self.s.risk.get("trailing_activate_atr_mult", 2.0))
        if side == Side.LONG:
            sl = entry - sl_mult * atr_val
            tp1 = entry + tp1_mult * atr_val
            tp2 = entry + tp2_mult * atr_val
            tp3 = entry + tp3_mult * atr_val
        else:
            sl = entry + sl_mult * atr_val
            tp1 = entry - tp1_mult * atr_val
            tp2 = entry - tp2_mult * atr_val
            tp3 = entry - tp3_mult * atr_val
        risk_pct = float(self.s.risk.get("risk_per_trade_pct", 1.0)) / 100.0
        size_usdt = float(self.s.risk.get("initial_paper_balance", 10000)) * risk_pct
        rationale = self._explain(all_hits, final, tf_breakdown, side, atr_val, trigger_tf)

        signal = Signal(
            id=uuid.uuid4().hex[:12],
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
            take_profit=float(tp1),
            trailing_atr=trail_mult * atr_val,
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
    def _explain(hits, final, breakdown, side, atr_val, trigger_tf=None) -> str:
        top = sorted(hits, key=lambda h: h.score * h.weight, reverse=True)[:3]
        method_label = {
            "new_listing": "کوین تازه‌لیست‌شده",
            "volume_spike": "اسپایک حجم",
            "orderbook_imbalance": "عدم تعادل سفارش‌ها",
            "liquidity_grab": "شکار نقدینگی",
            "momentum_ignition": "احتراق مومنتوم",
            "rsi_divergence": "واگرایی RSI",
            "bb_breakout": "شکست بولینگر",
            "funding_oi_spike": "اسپایک funding/OI",
            "ema_cross": "تقاطع EMA",
            "adx_trend": "روند ADX",
            "squeeze_momentum": "Squeeze",
            "macd_crossover": "MACD",
            "stoch_rsi": "Stochastic RSI",
            "sr_bounce": "S/R",
            "volume_trend": "روند حجم",
            "news_sentiment": "سنتیمنت اخبار",
        }
        bits = []
        for h in top:
            label = method_label.get(h.name, h.name)
            bits.append(f"{label} ({h.timeframe}, امتیاز {h.score:.2f})")
        breakdown_str = "، ".join(f"{tf}: {v:.2f}" for tf, v in breakdown.items() if v > 0)
        side_fa = "خرید (LONG)" if side == Side.LONG else "فروش (SHORT)"
        trigger_note = f" تایم‌فریم مبنا: {trigger_tf}." if trigger_tf else ""
        # Recommend leverage based on score
        if final >= 0.75:
            leverage_rec = "اهرم پیشنهادی: 5x-10x"
        elif final >= 0.60:
            leverage_rec = "اهرم پیشنهادی: 3x-5x"
        else:
            leverage_rec = "اهرم پیشنهادی: 2x-3x"
        return (
            f"⚡ سیگنال فیوچرز {side_fa} با امتیاز {final:.2f}. "
            f"روش‌ها: {'، '.join(bits)}. "
            f"TFها: {breakdown_str}."
            f"{trigger_note} "
            f"{leverage_rec}. "
            f"ATR={atr_val:.6f}."
        )

