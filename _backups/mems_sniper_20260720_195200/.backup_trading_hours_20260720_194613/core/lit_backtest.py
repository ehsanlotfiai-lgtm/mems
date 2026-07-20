"""LIT Backtest Engine — runs LIT strategies on historical data with full trade tracking.

Provides exact entry/exit times, TP/SL levels, and educational breakdown
for each trade. Results displayed on interactive charts.
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

import numpy as np
import pandas as pd

from core.logging_setup import logger
from strategies.lit_engine import (
    LITEngine, LITSignal, FairValueGap, OrderBlock, LiquidityLevel,
)


@dataclass
class BacktestTrade:
    """A single backtested trade with full educational data."""
    signal_id: str
    symbol: str
    side: str
    strategy: str
    # Entry
    entry_time: float        # timestamp
    entry_price: float
    entry_reasoning: str
    # Exit
    exit_time: Optional[float] = None
    exit_price: Optional[float] = None
    exit_reason: Optional[str] = None  # "tp1" | "tp2" | "sl" | "timeout" | "trailing"
    # Risk levels
    stop_loss: float = 0.0
    take_profit_1: float = 0.0
    take_profit_2: float = 0.0
    # Results
    pnl_pct: float = 0.0
    pnl_usdt: float = 0.0
    r_multiple: float = 0.0
    hit_tp1: bool = False
    hit_tp2: bool = False
    hit_sl: bool = False
    # Chart data
    zones: List[dict] = field(default_factory=list)
    # Candle index for chart placement
    entry_candle_idx: int = 0
    exit_candle_idx: Optional[int] = None

    def to_dict(self) -> dict:
        return {
            "signal_id": self.signal_id,
            "symbol": self.symbol,
            "side": self.side,
            "strategy": self.strategy,
            "entry_time": self.entry_time,
            "entry_price": self.entry_price,
            "entry_reasoning": self.entry_reasoning,
            "exit_time": self.exit_time,
            "exit_price": self.exit_price,
            "exit_reason": self.exit_reason,
            "stop_loss": self.stop_loss,
            "take_profit_1": self.take_profit_1,
            "take_profit_2": self.take_profit_2,
            "pnl_pct": self.pnl_pct,
            "pnl_usdt": self.pnl_usdt,
            "r_multiple": self.r_multiple,
            "hit_tp1": self.hit_tp1,
            "hit_tp2": self.hit_tp2,
            "hit_sl": self.hit_sl,
            "zones": self.zones,
            "entry_candle_idx": self.entry_candle_idx,
            "exit_candle_idx": self.exit_candle_idx,
        }


@dataclass
class BacktestResult:
    """Complete backtest results for a symbol."""
    symbol: str
    timeframe: str
    start_time: float
    end_time: float
    trades: List[BacktestTrade] = field(default_factory=list)
    # Stats
    total_trades: int = 0
    wins: int = 0
    losses: int = 0
    win_rate: float = 0.0
    avg_pnl_pct: float = 0.0
    total_pnl_pct: float = 0.0
    avg_r_multiple: float = 0.0
    best_trade: float = 0.0
    worst_trade: float = 0.0
    # Per-strategy breakdown
    strategy_stats: Dict[str, dict] = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {
            "symbol": self.symbol,
            "timeframe": self.timeframe,
            "start_time": self.start_time,
            "end_time": self.end_time,
            "total_trades": self.total_trades,
            "wins": self.wins,
            "losses": self.losses,
            "win_rate": self.win_rate,
            "avg_pnl_pct": self.avg_pnl_pct,
            "total_pnl_pct": self.total_pnl_pct,
            "avg_r_multiple": self.avg_r_multiple,
            "best_trade": self.best_trade,
            "worst_trade": self.worst_trade,
            "strategy_stats": self.strategy_stats,
            "trades": [t.to_dict() for t in self.trades],
        }


class LITBacktester:
    """Runs LIT strategies on historical OHLCV data with trade simulation."""

    def __init__(self, config: dict = None):
        self.lit_engine = LITEngine(config)
        cfg = config or {}
        self.sl_atr_mult = float(cfg.get("sl_atr_mult", 1.0))
        self.tp_atr_mult = float(cfg.get("tp_atr_mult", 2.0))
        self.tp2_atr_mult = float(cfg.get("tp2_atr_mult", 3.0))
        self.risk_per_trade = float(cfg.get("risk_per_trade_pct", 1.0)) / 100
        self.initial_balance = float(cfg.get("initial_balance", 10000))
        self.max_hold_bars = int(cfg.get("max_hold_bars", 50))

    def run(
        self,
        df: pd.DataFrame,
        symbol: str,
        exchange: str = "binance",
        htf_df: Optional[pd.DataFrame] = None,
    ) -> BacktestResult:
        """Run backtest on historical data.
        
        Args:
            df: OHLCV DataFrame with columns: open, high, low, close, volume, timestamp
            symbol: trading pair
            exchange: exchange name
            htf_df: higher timeframe data for bias
        """
        result = BacktestResult(
            symbol=symbol,
            timeframe="unknown",
            start_time=float(df["timestamp"].iloc[0]) if "timestamp" in df.columns else time.time(),
            end_time=float(df["timestamp"].iloc[-1]) if "timestamp" in df.columns else time.time(),
        )

        if df.empty or len(df) < 50:
            return result

        opens = df["open"].values.astype(float)
        highs = df["high"].values.astype(float)
        lows = df["low"].values.astype(float)
        closes = df["close"].values.astype(float)
        volumes = df["volume"].values.astype(float) if "volume" in df.columns else np.ones(len(opens))

        # ATR
        tr = np.maximum(
            highs[1:] - lows[1:],
            np.maximum(
                np.abs(highs[1:] - closes[:-1]),
                np.abs(lows[1:] - closes[:-1]),
            ),
        )

        # Scan through data with a rolling window
        window = 50
        open_trades: List[BacktestTrade] = []
        equity = self.initial_balance

        for i in range(window, len(df)):
            # ─── Check open trades ───
            closed_indices = []
            for trade in open_trades:
                current_price = closes[i]
                current_high = highs[i]
                current_low = lows[i]

                if trade.side == "long":
                    # Check TP1
                    if not trade.hit_tp1 and current_high >= trade.take_profit_1:
                        trade.hit_tp1 = True
                        # Move SL to breakeven
                        trade.stop_loss = trade.entry_price

                    # Check TP2
                    if trade.hit_tp1 and current_high >= trade.take_profit_2:
                        trade.hit_tp2 = True
                        trade.exit_time = float(df["timestamp"].iloc[i]) if "timestamp" in df.columns else time.time()
                        trade.exit_price = trade.take_profit_2
                        trade.exit_reason = "tp2"
                        trade.exit_candle_idx = i
                        risk = trade.entry_price - trade.stop_loss if trade.stop_loss < trade.entry_price else trade.entry_price * 0.01
                        trade.pnl_pct = round((trade.take_profit_2 - trade.entry_price) / trade.entry_price * 100, 2)
                        trade.pnl_usdt = round(equity * self.risk_per_trade * (trade.take_profit_2 - trade.entry_price) / max(risk, 1e-10), 2)
                        trade.r_multiple = round((trade.take_profit_2 - trade.entry_price) / max(risk, 1e-10), 2)
                        equity += trade.pnl_usdt
                        closed_indices.append(trade)
                        continue

                    # Check SL
                    if current_low <= trade.stop_loss:
                        trade.hit_sl = True
                        trade.exit_time = float(df["timestamp"].iloc[i]) if "timestamp" in df.columns else time.time()
                        trade.exit_price = trade.stop_loss
                        trade.exit_reason = "sl" if not trade.hit_tp1 else "sl_risk_free"
                        trade.exit_candle_idx = i
                        if trade.hit_tp1:
                            trade.pnl_pct = 0.0
                            trade.pnl_usdt = 0.0
                            trade.r_multiple = 0.0
                        else:
                            risk = trade.entry_price - (trade.stop_loss if trade.stop_loss < trade.entry_price else trade.entry_price * 0.01)
                            trade.pnl_pct = round((trade.stop_loss - trade.entry_price) / trade.entry_price * 100, 2)
                            trade.pnl_usdt = round(-equity * self.risk_per_trade, 2)
                            trade.r_multiple = -1.0
                            equity += trade.pnl_usdt
                        closed_indices.append(trade)
                        continue

                elif trade.side == "short":
                    # Check TP1
                    if not trade.hit_tp1 and current_low <= trade.take_profit_1:
                        trade.hit_tp1 = True
                        trade.stop_loss = trade.entry_price

                    # Check TP2
                    if trade.hit_tp1 and current_low <= trade.take_profit_2:
                        trade.hit_tp2 = True
                        trade.exit_time = float(df["timestamp"].iloc[i]) if "timestamp" in df.columns else time.time()
                        trade.exit_price = trade.take_profit_2
                        trade.exit_reason = "tp2"
                        trade.exit_candle_idx = i
                        risk = (trade.stop_loss - trade.entry_price) if trade.stop_loss > trade.entry_price else trade.entry_price * 0.01
                        trade.pnl_pct = round((trade.entry_price - trade.take_profit_2) / trade.entry_price * 100, 2)
                        trade.pnl_usdt = round(equity * self.risk_per_trade * (trade.entry_price - trade.take_profit_2) / max(risk, 1e-10), 2)
                        trade.r_multiple = round((trade.entry_price - trade.take_profit_2) / max(risk, 1e-10), 2)
                        equity += trade.pnl_usdt
                        closed_indices.append(trade)
                        continue

                    # Check SL
                    if current_high >= trade.stop_loss:
                        trade.hit_sl = True
                        trade.exit_time = float(df["timestamp"].iloc[i]) if "timestamp" in df.columns else time.time()
                        trade.exit_price = trade.stop_loss
                        trade.exit_reason = "sl" if not trade.hit_tp1 else "sl_risk_free"
                        trade.exit_candle_idx = i
                        if trade.hit_tp1:
                            trade.pnl_pct = 0.0
                            trade.pnl_usdt = 0.0
                            trade.r_multiple = 0.0
                        else:
                            risk = (trade.stop_loss - trade.entry_price) if trade.stop_loss > trade.entry_price else trade.entry_price * 0.01
                            trade.pnl_pct = round((trade.entry_price - trade.stop_loss) / trade.entry_price * 100, 2)
                            trade.pnl_usdt = round(-equity * self.risk_per_trade, 2)
                            trade.r_multiple = -1.0
                            equity += trade.pnl_usdt
                        closed_indices.append(trade)
                        continue

                # Timeout check
                if i - trade.entry_candle_idx >= self.max_hold_bars:
                    trade.exit_time = float(df["timestamp"].iloc[i]) if "timestamp" in df.columns else time.time()
                    trade.exit_price = current_price
                    trade.exit_reason = "timeout"
                    trade.exit_candle_idx = i
                    if trade.side == "long":
                        trade.pnl_pct = round((current_price - trade.entry_price) / trade.entry_price * 100, 2)
                    else:
                        trade.pnl_pct = round((trade.entry_price - current_price) / trade.entry_price * 100, 2)
                    trade.pnl_usdt = round(equity * self.risk_per_trade * trade.pnl_pct / 100, 2)
                    trade.r_multiple = round(trade.pnl_pct / (self.sl_atr_mult * 0.5), 2)
                    equity += trade.pnl_usdt
                    closed_indices.append(trade)

            # Remove closed trades
            for t in closed_indices:
                open_trades.remove(t)
                result.trades.append(t)

            # Don't open new trades if we already have one for this symbol
            if len(open_trades) >= 1:
                continue

            # ─── Look for new signals ───
            window_df = df.iloc[max(0, i - window):i + 1].copy()
            window_df = window_df.reset_index(drop=True)

            htf_window = None
            if htf_df is not None and not htf_df.empty:
                htf_window = htf_df.iloc[:min(len(htf_df), i // 4 + 20)].copy()

            signal = self.lit_engine.analyze(window_df, symbol, exchange, htf_window)
            if signal is None:
                continue

            # Create backtest trade
            trade = BacktestTrade(
                signal_id=signal.id,
                symbol=symbol,
                side=signal.side,
                strategy=signal.strategy,
                entry_time=float(df["timestamp"].iloc[i]) if "timestamp" in df.columns else time.time(),
                entry_price=signal.entry,
                entry_reasoning=signal.reasoning,
                stop_loss=signal.stop_loss,
                take_profit_1=signal.take_profit,
                take_profit_2=signal.take_profit_2,
                entry_candle_idx=i,
                zones=signal.zones,
            )
            open_trades.append(trade)

        # Close any remaining open trades
        for trade in open_trades:
            trade.exit_time = result.end_time
            trade.exit_price = float(closes[-1])
            trade.exit_reason = "backtest_end"
            trade.exit_candle_idx = len(df) - 1
            if trade.side == "long":
                trade.pnl_pct = round((trade.exit_price - trade.entry_price) / trade.entry_price * 100, 2)
            else:
                trade.pnl_pct = round((trade.entry_price - trade.exit_price) / trade.entry_price * 100, 2)
            trade.pnl_usdt = round(equity * self.risk_per_trade * trade.pnl_pct / 100, 2)
            trade.r_multiple = round(trade.pnl_pct / (self.sl_atr_mult * 0.5), 2)
            result.trades.append(trade)

        # Calculate stats
        result.total_trades = len(result.trades)
        if result.total_trades > 0:
            result.wins = sum(1 for t in result.trades if t.pnl_pct > 0)
            result.losses = sum(1 for t in result.trades if t.pnl_pct <= 0)
            result.win_rate = round(result.wins / result.total_trades * 100, 1)
            result.avg_pnl_pct = round(sum(t.pnl_pct for t in result.trades) / result.total_trades, 2)
            result.total_pnl_pct = round(sum(t.pnl_pct for t in result.trades), 2)
            result.avg_r_multiple = round(sum(t.r_multiple for t in result.trades) / result.total_trades, 2)
            result.best_trade = max(t.pnl_pct for t in result.trades)
            result.worst_trade = min(t.pnl_pct for t in result.trades)

            # Per-strategy breakdown
            strategies = set(t.strategy for t in result.trades)
            for strat in strategies:
                strat_trades = [t for t in result.trades if t.strategy == strat]
                strat_wins = sum(1 for t in strat_trades if t.pnl_pct > 0)
                result.strategy_stats[strat] = {
                    "total": len(strat_trades),
                    "wins": strat_wins,
                    "win_rate": round(strat_wins / len(strat_trades) * 100, 1) if strat_trades else 0,
                    "avg_pnl": round(sum(t.pnl_pct for t in strat_trades) / len(strat_trades), 2) if strat_trades else 0,
                    "avg_r": round(sum(t.r_multiple for t in strat_trades) / len(strat_trades), 2) if strat_trades else 0,
                }

        logger.info(
            f"LIT Backtest {symbol}: {result.total_trades} trades, "
            f"WR={result.win_rate}%, avg={result.avg_pnl_pct}%, "
            f"total={result.total_pnl_pct}%"
        )
        return result
