"""Standalone CLI backtester.

Usage:
    python -m mems_sniper.scripts.run_backtest --exchange binance --symbol DOGE/USDT
    python -m mems_sniper.scripts.run_backtest --exchange binance --symbol PEPE/USDT --limit 3000

Outputs a short summary and writes a JSON file under data/backtest_<symbol>.json.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from config.settings import get_settings
from core.exchange import ExchangeManager
from core.exchange import SymbolInfo
from core.logging_setup import setup_logging, logger
from backtest.engine import Backtester


async def main_async(args) -> None:
    setup_logging()
    s = get_settings()
    em = ExchangeManager(s)
    await em.start()
    try:
        bt = Backtester(s, em)
        info = SymbolInfo(symbol=args.symbol, base=args.symbol.split("/")[0], quote="USDT", listed_at=None)
        logger.info(f"Backtesting {args.exchange} {args.symbol} ...")
        res = await bt.run_symbol(args.exchange, info, candle_limit=args.limit)
        print("=" * 60)
        print(f"صرافی: {res.exchange}  نماد: {res.symbol}")
        print(f"تریدها: {res.n_trades}  برنده: {res.n_wins}  بازنده: {res.n_losses}")
        print(f"Win Rate: {res.win_rate}%  Profit Factor: {res.profit_factor}")
        print(f"بازده کل: {res.total_return_pct}%  Max DD: {res.max_drawdown_pct}%")
        print(f"Sharpe: {res.sharpe}  Sortino: {res.sortino}  میانگین PnL: {res.avg_pnl_pct}%")
        print(f"سرمایه اولیه: {res.initial_equity:.2f}  نهایی: {res.final_equity:.2f}")
        print("=" * 60)
        # Save artifacts
        out_dir = ROOT / "data"
        out_dir.mkdir(parents=True, exist_ok=True)
        out_path = out_dir / f"backtest_{args.exchange}_{args.symbol.replace('/', '_')}.json"
        out_path.write_text(json.dumps({
            "symbol": res.symbol, "exchange": res.exchange,
            "n_trades": res.n_trades, "win_rate": res.win_rate,
            "profit_factor": res.profit_factor,
            "total_return_pct": res.total_return_pct,
            "max_drawdown_pct": res.max_drawdown_pct,
            "sharpe": res.sharpe, "sortino": res.sortino,
            "avg_pnl_pct": res.avg_pnl_pct,
            "initial_equity": res.initial_equity,
            "final_equity": res.final_equity,
            "equity_curve": [{"t": t, "v": v} for t, v in res.equity_curve],
            "trades": [t.to_dict() for t in res.trades],
        }, ensure_ascii=False, indent=2), encoding="utf-8")
        logger.info(f"Backtest saved to {out_path}")
    finally:
        await em.stop()


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--exchange", default="binance")
    p.add_argument("--symbol", required=True, help="e.g. DOGE/USDT")
    p.add_argument("--limit", type=int, default=2000)
    args = p.parse_args()
    asyncio.run(main_async(args))


if __name__ == "__main__":
    main()
