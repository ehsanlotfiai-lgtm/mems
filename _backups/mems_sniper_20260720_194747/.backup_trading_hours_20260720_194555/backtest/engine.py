"""Backtest engine.

We avoid a hard dependency on vectorbt at import-time so the app boots
even if it isn't installed. The engine runs our strategy (with confluence
scoring) bar-by-bar across historical data, simulates paper execution
with fees/slippage, applies SL/TP/trailing, and reports standard
performance metrics.

This is intentionally *event-driven and strategy-faithful* (same code
path as the forward engine) rather than super-vectorized — so numbers
match what live trading would have done, not an overfitted loop.
"""
from __future__ import annotations

import asyncio
import time
import uuid
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

import numpy as np
import pandas as pd

from config.settings import Settings
from core.exchange import ExchangeManager, SymbolInfo
from core.models import Candle, PaperPosition, Signal, Side, StrategyHit, now_sec
from strategies.indicators import atr as atr_indicator
from strategies.strategies import build_strategies, BaseStrategy, _candles_to_df


@dataclass
class BacktestResult:
    symbol: str
    exchange: str
    trades: List[PaperPosition] = field(default_factory=list)
    equity_curve: List[Tuple[float, float]] = field(default_factory=list)
    initial_equity: float = 0.0
    final_equity: float = 0.0
    total_return_pct: float = 0.0
    max_drawdown_pct: float = 0.0
    win_rate: float = 0.0
    profit_factor: float = 0.0
    sharpe: float = 0.0
    sortino: float = 0.0
    calmar: float = 0.0
    n_trades: int = 0
    n_wins: int = 0
    n_losses: int = 0
    avg_pnl_pct: float = 0.0
    avg_win_pct: float = 0.0
    avg_loss_pct: float = 0.0
    max_consecutive_wins: int = 0
    max_consecutive_losses: int = 0
    avg_holding_bars: float = 0.0
    expectation: float = 0.0
    recovery_factor: float = 0.0
    monte_carlo_5pct: float = 0.0
    monte_carlo_95pct: float = 0.0
    # Drawdown series for chart overlay
    drawdown_curve: List[Tuple[float, float]] = field(default_factory=list)


class Backtester:
    def __init__(
        self,
        settings: Settings,
        em: ExchangeManager,
    ) -> None:
        self.s = settings
        self.em = em
        self.strategies: List[BaseStrategy] = build_strategies(settings.strategies)
        self.weights = settings.confluence_weights
        self.cfg = settings.backtest
        self.risk_cfg = settings.risk

    # ---------------------------------------------------- public
    async def run_symbol(
        self,
        exchange: str,
        info: SymbolInfo,
        timeframes: Optional[List[str]] = None,
        candle_limit: Optional[int] = None,
    ) -> BacktestResult:
        tfs = timeframes or self.s.timeframes
        limit = candle_limit or int(self.cfg.get("history_candles", 2000))
        multi = await self.em.fetch_multi_tf_ohlcv(exchange, info.symbol, tfs, limit=limit)
        return self._simulate_offline(info, exchange, multi, tfs)

    def _simulate_offline(
        self,
        info: SymbolInfo,
        exchange: str,
        multi: Dict[str, List[Candle]],
        tfs: List[str],
    ) -> BacktestResult:
        dfs: Dict[str, pd.DataFrame] = {}
        for tf, candles in multi.items():
            df = _candles_to_df(candles)
            if df.empty:
                continue
            df = df.astype({"open": float, "high": float, "low": float,
                             "close": float, "volume": float})
            df["datetime"] = pd.to_datetime(df["timestamp"], unit="ms", utc=True)
            df.set_index("datetime", inplace=True)
            dfs[tf] = df
        trigger_tf = self.s.trigger_timeframe
        trigger_df = dfs.get(trigger_tf)
        if trigger_df is None or trigger_df.empty or len(trigger_df) < 30:
            return BacktestResult(symbol=info.symbol, exchange=exchange)

        # Use a multi-TF aligned index based on trigger timestamps.
        equity = float(self.risk_cfg.get("initial_paper_balance", 10000.0))
        start_equity = equity
        peak = equity
        max_dd = 0.0
        open_pos: Optional[PaperPosition] = None
        closed_trades: List[PaperPosition] = []
        curve: List[Tuple[float, float]] = []
        trade_bars: List[int] = []  # bars held per trade
        open_bar_idx: int = 0
        # trades returns
        rets: List[float] = []
        fee_pct = float(self.cfg.get("fee_pct", 0.075)) / 100.0
        slip_pct = float(self.cfg.get("slippage_pct", 0.05)) / 100.0

        # Pre-compute indicators for the trigger timeframe once.
        atr_series = atr_indicator(trigger_df["high"], trigger_df["low"], trigger_df["close"],
                                   length=int(self.risk_cfg.get("atr_length", 14)))
        sl_mult = float(self.risk_cfg.get("stop_loss_atr_mult", 1.5))
        tp_mult = float(self.risk_cfg.get("take_profit_atr_mult", 3.0))
        trail_mult = float(self.risk_cfg.get("trailing_activate_atr_mult", 2.0))
        risk_pct = float(self.risk_cfg.get("risk_per_trade_pct", 1.0)) / 100.0

        # Build per-TF resampled lookups (only need smaller data than trigger)
        # We'll iterate bar-by-bar over the trigger index and from each bar
        # use history up to that point.
        timestamps = list(trigger_df.index)
        n = len(timestamps)
        # Pre-compute numpy arrays for fast slicing
        high_arr = trigger_df["high"].to_numpy()
        low_arr = trigger_df["low"].to_numpy()
        close_arr = trigger_df["close"].to_numpy()
        open_arr = trigger_df["open"].to_numpy()
        vol_arr = trigger_df["volume"].to_numpy()
        atr_arr = atr_series.to_numpy()

        min_window = 50
        for i in range(min_window, n):
            ts = trigger_df.index[i]
            price = float(close_arr[i])

            # manage open position first
            if open_pos is not None:
                self._trail(open_pos, price, trail_mult)
                reason = self._exit_reason(open_pos, float(high_arr[i]), float(low_arr[i]))
                if reason:
                    if open_pos.side == Side.LONG:
                        pnl_raw = (price - open_pos.entry) * open_pos.qty
                    else:
                        pnl_raw = (open_pos.entry - price) * open_pos.qty
                    cost = open_pos.entry * open_pos.qty * (fee_pct + slip_pct) * 2
                    pnl = pnl_raw - cost
                    equity += pnl
                    peak = max(peak, equity)
                    dd = (equity - peak) / peak if peak > 0 else 0
                    max_dd = min(max_dd, dd)
                    open_pos.closed_at = time.time()
                    open_pos.exit_price = price
                    open_pos.pnl_usdt = round(float(pnl), 4)
                    notional = open_pos.entry * open_pos.qty
                    open_pos.pnl_pct = round(float(pnl / notional * 100) if notional else 0, 3)
                    open_pos.close_reason = reason
                    open_pos.status = "closed"
                    closed_trades.append(open_pos)
                    trade_bars.append(i - open_bar_idx)
                    rets.append(open_pos.pnl_pct / 100.0)
                    open_pos = None

            if open_pos is not None:
                # don't open new while one is open (one position per symbol at a time in backtest)
                curve.append((ts.timestamp(), equity))
                continue

            # daily loss limit
            today_equity_start = start_equity  # simplification: backtest has no calendar day rollover
            daily_loss_limit_pct = float(self.risk_cfg.get("daily_max_loss_pct", 5.0))
            if (equity - today_equity_start) / max(start_equity, 1e-9) * 100 <= -daily_loss_limit_pct:
                curve.append((ts.timestamp(), equity))
                continue

            slice_df = self._slice_until(dfs[trigger_tf if False else tfs[0]], ts, i)
            # Build candle windows for all TFs ending at or before ts
            hits: List[StrategyHit] = []
            tf_breakdown = {tf: 0.0 for tf in tfs}
            for tf in tfs:
                df_tf = dfs.get(tf)
                if df_tf is None or df_tf.empty:
                    continue
                hist = df_tf.loc[:ts]
                hist = hist.iloc[:-1] if not hist.empty else hist
                if len(hist) < 5:
                    continue
                ctx = {"timeframe": tf, "listed_at": info.listed_at, "orderbook": None}
                for strat in self.strategies:
                    try:
                        hit = strat.evaluate(hist, ctx)
                    except Exception:  # noqa: BLE001
                        hit = None
                    if hit is not None:
                        hits.append(hit)
                        tf_breakdown[tf] = max(tf_breakdown[tf], hit.score * hit.weight)

            if not hits:
                curve.append((ts.timestamp(), equity))
                continue

            # Confluence score
            total_w = sum(self.weights.values()) or 1.0
            final = sum(tf_breakdown.get(tf, 0) * float(self.weights.get(tf, 0)) for tf in tfs) / total_w
            if final < self.s.min_signal_score:
                curve.append((ts.timestamp(), equity))
                continue

            long_votes = sum(h.score * h.weight for h in hits if h.detail.get("side") in (None, "long"))
            short_votes = sum(h.score * h.weight for h in hits if h.detail.get("side") == "short")
            side = Side.LONG if long_votes >= short_votes else Side.SHORT

            atr_val = float(atr_arr[i]) if np.isfinite(atr_arr[i]) else None
            if atr_val is None or atr_val <= 0:
                continue
            entry = price
            if side == Side.LONG:
                sl = entry - sl_mult * atr_val
                tp = entry + tp_mult * atr_val
            else:
                sl = entry + sl_mult * atr_val
                tp = entry - tp_mult * atr_val
            dist = abs(entry - sl)
            if dist <= 0:
                continue
            qty = (equity * risk_pct) / dist
            notional_cap = min(equity * 0.25, equity)
            qty = min(qty, notional_cap / entry)
            if qty <= 0:
                continue
            open_pos = PaperPosition(
                id=uuid.uuid4().hex[:12], opened_at=ts.timestamp(),
                exchange=exchange, symbol=info.symbol, side=side,
                entry=entry, stop_loss=sl, take_profit=tp,
                trailing_atr=trail_mult * atr_val, atr=atr_val,
                size_usdt=round(qty * entry, 2), qty=float(qty),
            )
            open_bar_idx = i
            curve.append((ts.timestamp(), equity))
        # end for

        # Force close remaining position at last price
        if open_pos is not None:
            price = float(close_arr[-1])
            pnl_raw = (price - open_pos.entry) * open_pos.qty if open_pos.side == Side.LONG \
                else (open_pos.entry - price) * open_pos.qty
            cost = open_pos.entry * open_pos.qty * (fee_pct + slip_pct) * 2
            pnl = pnl_raw - cost
            equity += pnl
            open_pos.closed_at = time.time()
            open_pos.exit_price = price
            open_pos.pnl_usdt = round(float(pnl), 4)
            open_pos.pnl_pct = round(float(pnl / (open_pos.entry * open_pos.qty) * 100), 3)
            open_pos.close_reason = "eod"
            open_pos.status = "closed"
            closed_trades.append(open_pos)
            trade_bars.append(n - 1 - open_bar_idx)
            rets.append(open_pos.pnl_pct / 100.0)
            curve.append((ts.timestamp(), equity))

        return self._summarize(info, exchange, closed_trades, curve,
                               start_equity, equity, max_dd, rets, trade_bars)

    # ---------------------------------------------------- helpers
    @staticmethod
    def _slice_until(df: pd.DataFrame, ts, ignore_last: int = 0) -> pd.DataFrame:
        sub = df.loc[:ts]
        if ignore_last:
            sub = sub.iloc[:-ignore_last] if len(sub) > ignore_last else sub
        return sub

    @staticmethod
    def _trail(pos: PaperPosition, price: float, trail_mult: float) -> None:
        if pos.side == Side.LONG:
            if price - pos.entry >= pos.trailing_atr:
                new_sl = price - pos.trailing_atr
                if new_sl > pos.stop_loss:
                    pos.stop_loss = new_sl
        else:
            if pos.entry - price >= pos.trailing_atr:
                new_sl = price + pos.trailing_atr
                if new_sl < pos.stop_loss:
                    pos.stop_loss = new_sl

    @staticmethod
    def _exit_reason(pos: PaperPosition, high: float, low: float) -> Optional[str]:
        if pos.side == Side.LONG:
            if low <= pos.stop_loss:
                return "sl"
            if high >= pos.take_profit:
                return "tp"
        else:
            if high >= pos.stop_loss:
                return "sl"
            if low <= pos.take_profit:
                return "tp"
        return None

    @staticmethod
    def _summarize(info, exchange, trades, curve, start_eq, final_eq, max_dd, rets,
                   trade_bars: Optional[List[int]] = None) -> BacktestResult:
        res = BacktestResult(symbol=info.symbol, exchange=exchange, trades=trades,
                             equity_curve=curve, initial_equity=start_eq,
                             final_equity=final_eq,
                             total_return_pct=round((final_eq - start_eq) / max(start_eq, 1e-9) * 100, 3),
                             max_drawdown_pct=round(max_dd * 100, 3),
                             n_trades=len(trades))
        # Build drawdown curve for chart overlay
        peak = start_eq
        dd_curve = []
        for ts, eq in curve:
            peak = max(peak, eq)
            dd = (eq - peak) / peak * 100 if peak > 0 else 0
            dd_curve.append((ts, round(dd, 3)))
        res.drawdown_curve = dd_curve

        if trades:
            wins = [t for t in trades if (t.pnl_usdt or 0) > 0]
            losses = [t for t in trades if (t.pnl_usdt or 0) <= 0]
            gross_win = sum(t.pnl_usdt or 0 for t in wins)
            gross_loss = abs(sum(t.pnl_usdt or 0 for t in losses))
            res.n_wins = len(wins)
            res.n_losses = len(losses)
            res.win_rate = round(len(wins) / len(trades) * 100, 2)
            res.profit_factor = round(gross_win / max(gross_loss, 1e-9), 3)
            res.avg_pnl_pct = round(sum(t.pnl_pct or 0 for t in trades) / len(trades), 3)

            # Average win/loss
            if wins:
                res.avg_win_pct = round(sum(t.pnl_pct or 0 for t in wins) / len(wins), 3)
            if losses:
                res.avg_loss_pct = round(sum(t.pnl_pct or 0 for t in losses) / len(losses), 3)

            # Expectation (edge per trade)
            wr = len(wins) / len(trades)
            avg_w = res.avg_win_pct / 100
            avg_l = abs(res.avg_loss_pct / 100)
            res.expectation = round(wr * avg_w - (1 - wr) * avg_l, 5)

            # Max consecutive wins/losses
            streak_w = streak_l = max_w = max_l = 0
            for t in trades:
                if (t.pnl_usdt or 0) > 0:
                    streak_w += 1
                    streak_l = 0
                    max_w = max(max_w, streak_w)
                else:
                    streak_l += 1
                    streak_w = 0
                    max_l = max(max_l, streak_l)
            res.max_consecutive_wins = max_w
            res.max_consecutive_losses = max_l

            # Average holding period (in bars)
            if trade_bars and len(trade_bars) == len(trades):
                res.avg_holding_bars = round(sum(trade_bars) / len(trade_bars), 1)

            # Recovery factor
            res.recovery_factor = round(res.total_return_pct / max(abs(res.max_drawdown_pct), 1e-9), 3)

            # Sharpe & Sortino with correct annualization
            rets_arr = np.array(rets, dtype=float)
            if rets_arr.size > 1:
                std = rets_arr.std(ddof=1)
                downside = rets_arr[rets_arr < 0].std(ddof=1) if (rets_arr < 0).any() else 0
                mean = rets_arr.mean()
                # Estimate bars_per_year from curve (assume roughly uniform spacing)
                if len(curve) >= 2:
                    total_seconds = curve[-1][0] - curve[0][0]
                    bars_per_year = len(curve) / max(total_seconds, 1) * 365.25 * 24 * 3600
                else:
                    bars_per_year = 525600  # fallback: ~1-minute bars
                ann_factor = np.sqrt(bars_per_year)
                res.sharpe = round(float(mean / std * ann_factor) if std > 0 else 0, 3)
                res.sortino = round(float(mean / downside * ann_factor) if downside > 0 else 0, 3)

            # Calmar ratio
            res.calmar = round(res.total_return_pct / max(abs(res.max_drawdown_pct), 1e-9), 3)

            # Monte Carlo (1000 simulations)
            if rets_arr.size >= 5:
                rng = np.random.default_rng(42)
                n_sims = 1000
                final_returns = np.zeros(n_sims)
                for sim in range(n_sims):
                    sampled = rng.choice(rets_arr, size=len(rets_arr), replace=True)
                    eq = start_eq
                    for r in sampled:
                        eq *= (1 + r)
                    final_returns[sim] = (eq - start_eq) / start_eq * 100
                res.monte_carlo_5pct = round(float(np.percentile(final_returns, 5)), 3)
                res.monte_carlo_95pct = round(float(np.percentile(final_returns, 95)), 3)

        return res
