"""Parquet data logger — persists candles, signals, trades for offline analysis.

Stores data in partitioned Parquet files organized by date:
  data/parquet/candles/{exchange}/{symbol}/{YYYY-MM-DD}.parquet
  data/parquet/signals/{YYYY-MM-DD}.parquet
  data/parquet/trades/{YYYY-MM-DD}.parquet
  data/parquet/social/{YYYY-MM-DD}.parquet

Uses pyarrow for Parquet I/O. Designed for append-friendly daily partitioning.
"""
from __future__ import annotations

import json
import os
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

import pandas as pd

from config.settings import Settings, get_settings
from core.logging_setup import logger
from core.models import Candle, PaperPosition, Signal


class ParquetLogger:
    """Log trading data to Parquet files for offline analysis."""

    def __init__(self, settings: Settings) -> None:
        self.s = settings
        self.enabled = bool(settings.raw.get("parquet", {}).get("enabled", False))
        self.base_dir = Path(settings.raw.get("project", {}).get("data_dir", "data")) / "parquet"
        self._buffers: Dict[str, List[dict]] = {}  # key -> pending rows
        self._flush_interval = 60  # seconds between flushes
        self._last_flush = time.time()

    def _ensure_dir(self, path: Path) -> None:
        path.mkdir(parents=True, exist_ok=True)

    def _date_str(self, ts: Optional[float] = None) -> str:
        dt = datetime.fromtimestamp(ts or time.time(), tz=timezone.utc)
        return dt.strftime("%Y-%m-%d")

    # ------------------------------------------ candles
    def log_candle(self, exchange: str, symbol: str, candle: Candle) -> None:
        """Append a candle to the daily buffer."""
        if not self.enabled:
            return
        safe_sym = symbol.replace("/", "_").replace(":", "_")
        key = f"candle|{exchange}|{safe_sym}"
        row = {
            "timestamp": candle.timestamp,
            "open": candle.open, "high": candle.high,
            "low": candle.low, "close": candle.close,
            "volume": candle.volume,
            "date": self._date_str(candle.timestamp / 1000),
        }
        self._buffers.setdefault(key, []).append(row)
        self._maybe_flush()

    # ------------------------------------------ signals
    def log_signal(self, signal: Signal) -> None:
        """Append a signal to the daily buffer."""
        if not self.enabled:
            return
        key = f"signal|{self._date_str(signal.created_at)}"
        row = {
            "id": signal.id,
            "created_at": signal.created_at,
            "exchange": signal.exchange,
            "symbol": signal.symbol,
            "side": signal.side.value,
            "price": signal.price,
            "score": signal.score,
            "entry": signal.entry,
            "stop_loss": signal.stop_loss,
            "take_profit": signal.take_profit,
            "atr": signal.atr,
            "status": signal.status,
            "hits_json": json.dumps([h.to_dict() for h in signal.hits], ensure_ascii=False),
            "tf_breakdown_json": json.dumps(signal.confluence_tf_breakdown, ensure_ascii=False),
            "rationale": signal.rationale,
            "base": signal.base or "",
        }
        self._buffers.setdefault(key, []).append(row)
        self._maybe_flush()

    # ------------------------------------------ trades
    def log_trade(self, trade: PaperPosition) -> None:
        """Append a trade to the daily buffer."""
        if not self.enabled:
            return
        key = f"trade|{self._date_str(trade.opened_at)}"
        row = {
            "id": trade.id,
            "opened_at": trade.opened_at,
            "closed_at": trade.closed_at,
            "exchange": trade.exchange,
            "symbol": trade.symbol,
            "side": trade.side.value,
            "entry": trade.entry,
            "exit_price": trade.exit_price,
            "qty": trade.qty,
            "size_usdt": trade.size_usdt,
            "pnl_usdt": trade.pnl_usdt,
            "pnl_pct": trade.pnl_pct,
            "close_reason": trade.close_reason or "",
            "leverage": trade.leverage,
            "market_type": trade.market_type,
            "fee_usdt": trade.fee_usdt,
            "slippage_usdt": trade.slippage_usdt,
        }
        self._buffers.setdefault(key, []).append(row)
        self._maybe_flush()

    # ------------------------------------------ social
    def log_social(self, symbol: str, score: dict) -> None:
        """Append a social score snapshot."""
        if not self.enabled:
            return
        key = f"social|{self._date_str()}"
        row = {"symbol": symbol, **score}
        self._buffers.setdefault(key, []).append(row)
        self._maybe_flush()

    # ------------------------------------------ flush
    def _maybe_flush(self) -> None:
        now = time.time()
        if now - self._last_flush < self._flush_interval:
            return
        self.flush()

    def flush(self) -> None:
        """Write all buffered rows to Parquet files."""
        if not self._buffers:
            return
        for key, rows in list(self._buffers.items()):
            if not rows:
                continue
            parts = key.split("|", 2)
            kind = parts[0]
            try:
                if kind == "candle":
                    self._write_candle_parquet(parts[1], parts[2], rows)
                elif kind == "signal":
                    self._write_daily_parquet("signals", parts[1], rows)
                elif kind == "trade":
                    self._write_daily_parquet("trades", parts[1], rows)
                elif kind == "social":
                    self._write_daily_parquet("social", parts[1], rows)
            except Exception as exc:  # noqa: BLE001
                logger.debug(f"Parquet flush error {key}: {exc}")
        self._buffers.clear()
        self._last_flush = time.time()

    def _write_candle_parquet(self, exchange: str, symbol: str, rows: List[dict]) -> None:
        date_str = rows[0].get("date", self._date_str())
        path = self.base_dir / "candles" / exchange / symbol
        self._ensure_dir(path)
        filepath = path / f"{date_str}.parquet"
        df = pd.DataFrame(rows).drop(columns=["date"], errors="ignore")
        if filepath.exists():
            existing = pd.read_parquet(filepath)
            df = pd.concat([existing, df], ignore_index=True).drop_duplicates(subset=["timestamp"])
        df.to_parquet(filepath, index=False, engine="pyarrow")

    def _write_daily_parquet(self, category: str, date_str: str, rows: List[dict]) -> None:
        path = self.base_dir / category
        self._ensure_dir(path)
        filepath = path / f"{date_str}.parquet"
        df = pd.DataFrame(rows)
        if filepath.exists():
            existing = pd.read_parquet(filepath)
            df = pd.concat([existing, df], ignore_index=True).drop_duplicates(subset=["id"], keep="last")
        df.to_parquet(filepath, index=False, engine="pyarrow")

    # ------------------------------------------ query (for offline analysis)
    def query_candles(self, exchange: str, symbol: str,
                      start_date: str, end_date: str) -> pd.DataFrame:
        """Load candles for a date range."""
        safe_sym = symbol.replace("/", "_").replace(":", "_")
        path = self.base_dir / "candles" / exchange / safe_sym
        if not path.exists():
            return pd.DataFrame()
        dfs = []
        for f in sorted(path.glob("*.parquet")):
            if start_date <= f.stem <= end_date:
                dfs.append(pd.read_parquet(f))
        return pd.concat(dfs, ignore_index=True) if dfs else pd.DataFrame()

    def query_signals(self, start_date: str, end_date: str) -> pd.DataFrame:
        """Load signals for a date range."""
        return self._query_daily_range("signals", start_date, end_date)

    def query_trades(self, start_date: str, end_date: str) -> pd.DataFrame:
        """Load trades for a date range."""
        return self._query_daily_range("trades", start_date, end_date)

    def query_social(self, start_date: str, end_date: str) -> pd.DataFrame:
        """Load social scores for a date range."""
        return self._query_daily_range("social", start_date, end_date)

    def _query_daily_range(self, category: str, start: str, end: str) -> pd.DataFrame:
        path = self.base_dir / category
        if not path.exists():
            return pd.DataFrame()
        dfs = []
        for f in sorted(path.glob("*.parquet")):
            if start <= f.stem <= end:
                dfs.append(pd.read_parquet(f))
        return pd.concat(dfs, ignore_index=True) if dfs else pd.DataFrame()


# singleton
_parquet_logger: Optional[ParquetLogger] = None


def get_parquet_logger(settings: Optional[Settings] = None) -> ParquetLogger:
    global _parquet_logger
    if _parquet_logger is None:
        _parquet_logger = ParquetLogger(settings or get_settings())
    return _parquet_logger
