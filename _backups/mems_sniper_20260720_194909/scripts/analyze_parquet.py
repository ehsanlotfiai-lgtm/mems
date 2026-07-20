"""Offline analysis script for Parquet data.

Usage:
    python -m mems_sniper.scripts.analyze_parquet --start 2026-07-01 --end 2026-07-10
    python -m mems_sniper.scripts.analyze_parquet --exchange binance --symbol PEPE/USDT
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import pandas as pd

from config.settings import get_settings
from core.parquet_logger import ParquetLogger


def analyze_trades(logger: ParquetLogger, start: str, end: str) -> None:
    """Print trade analysis summary."""
    df = logger.query_trades(start, end)
    if df.empty:
        print("⚠️  No trades found for this date range.")
        return

    print(f"\n📊 Trade Analysis ({start} → {end})")
    print("=" * 60)
    print(f"Total trades: {len(df)}")

    if "pnl_usdt" in df.columns:
        wins = df[df["pnl_usdt"] > 0]
        losses = df[df["pnl_usdt"] <= 0]
        print(f"Winning trades: {len(wins)} ({len(wins)/len(df)*100:.1f}%)")
        print(f"Losing trades: {len(losses)}")
        print(f"Total PnL: ${df['pnl_usdt'].sum():.2f}")
        print(f"Average PnL: ${df['pnl_usdt'].mean():.2f}")
        print(f"Best trade: ${df['pnl_usdt'].max():.2f}")
        print(f"Worst trade: ${df['pnl_usdt'].min():.2f}")

    if "close_reason" in df.columns:
        print(f"\nClose reasons:")
        for reason, count in df["close_reason"].value_counts().items():
            print(f"  {reason}: {count}")

    if "market_type" in df.columns:
        print(f"\nMarket types:")
        for mkt, count in df["market_type"].value_counts().items():
            print(f"  {mkt}: {count}")

    if "leverage" in df.columns:
        print(f"\nAverage leverage: {df['leverage'].mean():.1f}x")

    # Per-symbol breakdown
    if "symbol" in df.columns:
        print(f"\nPer-symbol breakdown:")
        for sym, grp in df.groupby("symbol"):
            pnl = grp["pnl_usdt"].sum() if "pnl_usdt" in grp.columns else 0
            wr = len(grp[grp["pnl_usdt"] > 0]) / len(grp) * 100 if "pnl_usdt" in grp.columns else 0
            print(f"  {sym}: {len(grp)} trades, PnL=${pnl:.2f}, WR={wr:.0f}%")


def analyze_signals(logger: ParquetLogger, start: str, end: str) -> None:
    """Print signal analysis summary."""
    df = logger.query_signals(start, end)
    if df.empty:
        print("⚠️  No signals found for this date range.")
        return

    print(f"\n📡 Signal Analysis ({start} → {end})")
    print("=" * 60)
    print(f"Total signals: {len(df)}")

    if "status" in df.columns:
        print(f"\nStatus breakdown:")
        for status, count in df["status"].value_counts().items():
            print(f"  {status}: {count}")

    if "score" in df.columns:
        print(f"\nScore statistics:")
        print(f"  Mean: {df['score'].mean():.3f}")
        print(f"  Median: {df['score'].median():.3f}")
        print(f"  Min: {df['score'].min():.3f}")
        print(f"  Max: {df['score'].max():.3f}")

    if "side" in df.columns:
        print(f"\nSide breakdown:")
        for side, count in df["side"].value_counts().items():
            print(f"  {side}: {count}")


def analyze_candles(logger: ParquetLogger, exchange: str, symbol: str,
                    start: str, end: str) -> None:
    """Print candle data summary."""
    df = logger.query_candles(exchange, symbol, start, end)
    if df.empty:
        print(f"⚠️  No candle data found for {exchange}/{symbol}.")
        return

    print(f"\n📈 Candle Analysis: {exchange}/{symbol} ({start} → {end})")
    print("=" * 60)
    print(f"Total candles: {len(df)}")

    if "close" in df.columns:
        print(f"Price range: ${df['close'].min():.6f} — ${df['close'].max():.6f}")
        print(f"Total volume: {df['volume'].sum():.0f}")
        if len(df) > 1:
            returns = df["close"].pct_change().dropna()
            print(f"Volatility (daily): {returns.std() * 100:.2f}%")
            print(f"Total return: {(df['close'].iloc[-1] / df['close'].iloc[0] - 1) * 100:.2f}%")


def main():
    parser = argparse.ArgumentParser(description="Analyze Parquet data offline")
    parser.add_argument("--start", default="2026-01-01", help="Start date (YYYY-MM-DD)")
    parser.add_argument("--end", default="2026-12-31", help="End date (YYYY-MM-DD)")
    parser.add_argument("--exchange", default=None, help="Exchange for candle analysis")
    parser.add_argument("--symbol", default=None, help="Symbol for candle analysis")
    parser.add_argument("--trades", action="store_true", help="Analyze trades")
    parser.add_argument("--signals", action="store_true", help="Analyze signals")
    parser.add_argument("--all", action="store_true", help="Analyze everything")
    args = parser.parse_args()

    settings = get_settings()
    logger = ParquetLogger(settings)
    logger.enabled = True  # force enable for analysis

    if args.all or args.trades:
        analyze_trades(logger, args.start, args.end)
    if args.all or args.signals:
        analyze_signals(logger, args.start, args.end)
    if args.all and args.exchange and args.symbol:
        analyze_candles(logger, args.exchange, args.symbol, args.start, args.end)
    elif args.exchange and args.symbol:
        analyze_candles(logger, args.exchange, args.symbol, args.start, args.end)

    if not (args.all or args.trades or args.signals or (args.exchange and args.symbol)):
        print("Usage: python -m mems_sniper.scripts.analyze_parquet --all")
        print("       python -m mems_sniper.scripts.analyze_parquet --trades --start 2026-07-01")
        print("       python -m mems_sniper.scripts.analyze_parquet --exchange binance --symbol PEPE/USDT")


if __name__ == "__main__":
    main()
