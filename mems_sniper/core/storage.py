"""SQLite persistence (async via aiosqlite).

Tables:
  signals       - all emitted signals
  paper_trades  - opened/closed paper positions + PnL
  ticks         - thin log of WS ticks used by dashboard history
  assistant_log - chat / suggestion log
"""
from __future__ import annotations

import json
import time
from typing import Any, List, Optional

import aiosqlite

from config.settings import Settings, get_settings
from core.models import PaperPosition, Signal


SCHEMA = [
    """
    CREATE TABLE IF NOT EXISTS signals (
        id TEXT PRIMARY KEY,
        created_at REAL NOT NULL,
        exchange TEXT NOT NULL,
        symbol TEXT NOT NULL,
        side TEXT NOT NULL,
        price REAL NOT NULL,
        score REAL NOT NULL,
        entry REAL NOT NULL,
        stop_loss REAL NOT NULL,
        take_profit REAL NOT NULL,
        atr REAL NOT NULL,
        size_usdt REAL NOT NULL,
        rationale TEXT,
        hits_json TEXT,
        tf_breakdown_json TEXT,
        status TEXT DEFAULT 'open'
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS paper_trades (
        id TEXT PRIMARY KEY,
        opened_at REAL NOT NULL,
        closed_at REAL,
        exchange TEXT NOT NULL,
        symbol TEXT NOT NULL,
        side TEXT NOT NULL,
        entry REAL NOT NULL,
        exit_price REAL,
        qty REAL NOT NULL,
        size_usdt REAL NOT NULL,
        stop_loss REAL NOT NULL,
        take_profit REAL NOT NULL,
        pnl_usdt REAL,
        pnl_pct REAL,
        close_reason TEXT,
        status TEXT DEFAULT 'open',
        signal_id TEXT,
        leverage REAL DEFAULT 1.0,
        market_type TEXT DEFAULT 'spot',
        fee_usdt REAL DEFAULT 0.0,
        slippage_usdt REAL DEFAULT 0.0
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS ticks (
        ts REAL NOT NULL,
        exchange TEXT NOT NULL,
        symbol TEXT NOT NULL,
        timeframe TEXT,
        price REAL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS assistant_log (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        ts REAL NOT NULL,
        kind TEXT NOT NULL,
        text TEXT NOT NULL
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_signals_created ON signals(created_at DESC)",
    "CREATE INDEX IF NOT EXISTS idx_trades_opened ON paper_trades(opened_at DESC)",
    "CREATE INDEX IF NOT EXISTS idx_ticks_ts ON ticks(ts DESC)",
]

# Migration columns — run once after base schema
_MIGRATIONS_PAPER = [
    "tp2 REAL",
    "tp3 REAL",
    "tp1_hit INTEGER DEFAULT 0",
    "risk_free INTEGER DEFAULT 0",
    "unrealized_pnl_pct REAL",
]
_MIGRATIONS_SIGNALS = [
    "tp2 REAL",
    "market_type TEXT DEFAULT 'futures'",
]


class Storage:
    def __init__(self, settings: Settings) -> None:
        self.s = settings
        self._db: Optional[aiosqlite.Connection] = None

    @property
    def is_connected(self) -> bool:
        """Public guard used by server helpers instead of touching _db directly."""
        return self._db is not None

    async def connect(self) -> None:
        if self._db is not None:
            return  # idempotent — safe to call multiple times
        self._db = await aiosqlite.connect(str(self.s.sqlite_path))
        await self._db.executescript(";".join(SCHEMA))
        await self._db.commit()
        await self._db.execute("PRAGMA journal_mode=WAL;")
        await self._db.commit()
        # FIX: migrations with explicit logging instead of silent pass
        for col in _MIGRATIONS_PAPER:
            try:
                await self._db.execute(f"ALTER TABLE paper_trades ADD COLUMN {col}")
                await self._db.commit()
            except aiosqlite.OperationalError:
                pass  # column already exists — expected
        for col in _MIGRATIONS_SIGNALS:
            try:
                await self._db.execute(f"ALTER TABLE signals ADD COLUMN {col}")
                await self._db.commit()
            except aiosqlite.OperationalError:
                pass  # column already exists — expected

    async def close(self) -> None:
        if self._db is not None:
            await self._db.close()
            self._db = None

    @property
    def db(self) -> aiosqlite.Connection:
        if self._db is None:
            raise RuntimeError("Storage.connect() has not been awaited yet")
        return self._db

    # ------------------------------------------ signals
    async def save_signal(self, sig: Signal) -> None:
        await self.db.execute(
            """INSERT OR REPLACE INTO signals
               (id, created_at, exchange, symbol, side, price, score, entry, stop_loss,
                take_profit, atr, size_usdt, rationale, hits_json, tf_breakdown_json, status, tp2, market_type)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                sig.id, sig.created_at, sig.exchange, sig.symbol, sig.side.value,
                sig.price, sig.score, sig.entry, sig.stop_loss, sig.take_profit,
                sig.atr, sig.position_size_usdt, sig.rationale,
                json.dumps([h.to_dict() for h in sig.hits], ensure_ascii=False),
                json.dumps(sig.confluence_tf_breakdown, ensure_ascii=False),
                sig.status, sig.tp2, getattr(sig, 'market_type', 'futures'),
            ),
        )
        await self.db.commit()

    async def get_last_signal(self, symbol: str, minutes: int = 15) -> Optional[dict]:
        cutoff = time.time() - (minutes * 60)
        cur = await self.db.execute(
            "SELECT * FROM signals WHERE symbol=? AND created_at>? ORDER BY created_at DESC LIMIT 1",
            (symbol, cutoff),
        )
        row = await cur.fetchone()
        if row is None:
            return None
        cols = [d[0] for d in cur.description]
        return dict(zip(cols, row))

    async def recent_signals(self, limit: int = 100) -> List[dict]:
        cur = await self.db.execute(
            "SELECT * FROM signals WHERE id NOT LIKE 'SCP_%' ORDER BY created_at DESC LIMIT ?", (limit,)
        )
        rows = await cur.fetchall()
        cols = [d[0] for d in cur.description]
        out = []
        for r in rows:
            d = dict(zip(cols, r))
            d["hits"] = json.loads(d.pop("hits_json") or "[]")
            d["tf_breakdown"] = json.loads(d.pop("tf_breakdown_json") or "{}")
            out.append(d)
        return out

    async def update_signal_status(self, signal_id: str, status: str) -> None:
        await self.db.execute(
            "UPDATE signals SET status=? WHERE id=?", (status, signal_id)
        )
        await self.db.commit()

    # ------------------------------------------ paper trades
    async def open_paper(self, pos: PaperPosition, signal_id: Optional[str] = None) -> None:
        if signal_id:
            pos.signal_id = signal_id
        await self.db.execute(
            """INSERT OR REPLACE INTO paper_trades
               (id, opened_at, closed_at, exchange, symbol, side, entry, exit_price, qty,
                size_usdt, stop_loss, take_profit, pnl_usdt, pnl_pct, close_reason, status,
                signal_id, leverage, market_type, fee_usdt, slippage_usdt,
                tp2, tp3, tp1_hit, risk_free, unrealized_pnl_pct)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (pos.id, pos.opened_at, pos.closed_at, pos.exchange, pos.symbol, pos.side.value,
             pos.entry, pos.exit_price, pos.qty, pos.size_usdt, pos.stop_loss,
             pos.take_profit, pos.pnl_usdt, pos.pnl_pct, pos.close_reason, pos.status,
             signal_id, pos.leverage, pos.market_type, pos.fee_usdt, pos.slippage_usdt,
             pos.tp2, pos.tp3, int(pos.tp1_hit), int(pos.risk_free), pos.unrealized_pnl_pct),
        )
        await self.db.commit()

    async def update_paper_close(self, pos: PaperPosition) -> None:
        await self.db.execute(
            """UPDATE paper_trades SET closed_at=?, exit_price=?, pnl_usdt=?, pnl_pct=?,
               close_reason=?, status=?, tp1_hit=?, risk_free=?,
               stop_loss=?, unrealized_pnl_pct=? WHERE id=?""",
            (pos.closed_at, pos.exit_price, pos.pnl_usdt, pos.pnl_pct,
             pos.close_reason, pos.status, int(pos.tp1_hit), int(pos.risk_free),
             pos.stop_loss, pos.unrealized_pnl_pct, pos.id),
        )
        await self.db.commit()
        if pos.signal_id and pos.close_reason:
            await self.update_signal_status(pos.signal_id, pos.close_reason)

    async def recent_trades(self, limit: int = 100) -> List[dict]:
        cur = await self.db.execute(
            "SELECT * FROM paper_trades ORDER BY opened_at DESC LIMIT ?", (limit,)
        )
        rows = await cur.fetchall()
        cols = [d[0] for d in cur.description]
        return [dict(zip(cols, r)) for r in rows]

    async def load_open_paper_trades(self) -> List[dict]:
        cur = await self.db.execute(
            "SELECT * FROM paper_trades WHERE status = 'open'"
        )
        rows = await cur.fetchall()
        cols = [d[0] for d in cur.description]
        return [dict(zip(cols, r)) for r in rows]

    # ------------------------------------------ ticks
    async def add_tick(self, exchange: str, symbol: str, price: float,
                       timeframe: Optional[str] = None) -> None:
        await self.db.execute(
            "INSERT INTO ticks (ts, exchange, symbol, timeframe, price) VALUES (?,?,?,?,?)",
            (time.time(), exchange, symbol, timeframe, price),
        )
        await self.db.commit()

    async def prune_ticks(self, keep_seconds: float = 6 * 3600) -> None:
        cutoff = time.time() - keep_seconds
        await self.db.execute("DELETE FROM ticks WHERE ts < ?", (cutoff,))
        await self.db.commit()

    # ------------------------------------------ assistant
    async def assistant_log(self, kind: str, text: str) -> None:
        await self.db.execute(
            "INSERT INTO assistant_log (ts, kind, text) VALUES (?, ?, ?)",
            (time.time(), kind, text),
        )
        await self.db.commit()

    async def recent_assistant_log(self, limit: int = 50) -> List[dict]:
        cur = await self.db.execute(
            "SELECT ts, kind, text FROM assistant_log ORDER BY id DESC LIMIT ?", (limit,)
        )
        rows = await cur.fetchall()
        rows = list(reversed(rows))
        return [{"ts": r[0], "kind": r[1], "text": r[2]} for r in rows]

    # ------------------------------------------ time-based win rates
    async def get_time_win_rates(self) -> dict:
        now = time.time()
        windows = {
            "daily": 86400,
            "hourly": 3600,
            "4hour": 14400,
        }
        result = {}
        for label, seconds in windows.items():
            cutoff = now - seconds
            cur = await self.db.execute(
                """SELECT pnl_pct, pnl_usdt, close_reason, opened_at
                   FROM paper_trades WHERE opened_at >= ?""",
                (cutoff,)
            )
            rows = await cur.fetchall()
            closed_rows = [(p, pnl, r, o) for p, pnl, r, o in rows if p is not None]
            wins = sum(1 for p, _, _, _ in closed_rows if p > 0)
            losses = sum(1 for p, _, _, _ in closed_rows if p < 0)
            open_count = sum(1 for p, _, _, _ in rows if p is None)
            total = len(closed_rows)
            total_pnl_pct = sum(p for p, _, _, _ in closed_rows)
            total_pnl_usdt = sum(pnl for _, pnl, _, _ in closed_rows)
            win_rate = (wins / total * 100) if total > 0 else 0
            avg_pnl = (total_pnl_pct / total) if total > 0 else 0
            avg_pnl_usdt = (total_pnl_usdt / total) if total > 0 else 0

            result[label] = {
                "total": total,
                "open": open_count,
                "wins": wins,
                "losses": losses,
                "win_rate": round(win_rate, 1),
                "avg_pnl_pct": round(avg_pnl, 2),
                "avg_pnl_usdt": round(avg_pnl_usdt, 2),
                "total_pnl_usdt": round(total_pnl_usdt, 2),
            }

        cur = await self.db.execute(
            "SELECT pnl_pct, pnl_usdt FROM paper_trades WHERE pnl_pct IS NOT NULL"
        )
        closed_rows = await cur.fetchall()
        total = len(closed_rows)
        wins = sum(1 for p, _ in closed_rows if p > 0)
        losses = sum(1 for p, _ in closed_rows if p < 0)
        total_pnl_usdt = sum(pnl for _, pnl in closed_rows)
        total_pnl_pct = sum(p for p, _ in closed_rows)
        result["all"] = {
            "total": total,
            "wins": wins,
            "losses": losses,
            "win_rate": round((wins / total * 100) if total > 0 else 0, 1),
            "avg_pnl_pct": round((total_pnl_pct / total) if total > 0 else 0, 2),
            "avg_pnl_usdt": round((total_pnl_usdt / total) if total > 0 else 0, 2),
            "total_pnl_usdt": round(total_pnl_usdt, 2),
        }

        return result

    # ------------------------------------------ scalping signals
    async def recent_scalp_signals(self, limit: int = 100) -> List[dict]:
        cur = await self.db.execute(
            "SELECT * FROM signals WHERE id LIKE 'SCP_%' ORDER BY created_at DESC LIMIT ?",
            (limit,),
        )
        rows = await cur.fetchall()
        cols = [d[0] for d in cur.description]
        out = []
        for r in rows:
            d = dict(zip(cols, r))
            d["hits"] = json.loads(d.pop("hits_json") or "[]")
            d["tf_breakdown"] = json.loads(d.pop("tf_breakdown_json") or "{}")
            out.append(d)
        return out

    async def get_scalp_win_rates(self) -> dict:
        now = time.time()
        windows = {
            "last_hour": 3600,
            "last_4h": 14400,
            "today": 86400,
        }
        result = {}
        for label, seconds in windows.items():
            cutoff = now - seconds
            cur = await self.db.execute(
                """SELECT pnl_pct, pnl_usdt, close_reason, opened_at
                   FROM paper_trades WHERE signal_id LIKE 'SCP_%' AND opened_at >= ?""",
                (cutoff,),
            )
            rows = await cur.fetchall()
            closed_rows = [(p, pnl, r, o) for p, pnl, r, o in rows if p is not None]
            wins = sum(1 for p, _, _, _ in closed_rows if p > 0)
            losses = sum(1 for p, _, _, _ in closed_rows if p < 0)
            open_count = sum(1 for p, _, _, _ in rows if p is None)
            total = len(closed_rows)
            total_pnl_pct = sum(p for p, _, _, _ in closed_rows)
            total_pnl_usdt = sum(pnl for _, pnl, _, _ in closed_rows)
            win_rate = (wins / total * 100) if total > 0 else 0
            avg_pnl = (total_pnl_pct / total) if total > 0 else 0

            result[label] = {
                "total": total,
                "open": open_count,
                "wins": wins,
                "losses": losses,
                "win_rate": round(win_rate, 1),
                "avg_pnl_pct": round(avg_pnl, 2),
                "total_pnl_usdt": round(total_pnl_usdt, 2),
            }

        cur = await self.db.execute(
            "SELECT pnl_pct, pnl_usdt FROM paper_trades WHERE signal_id LIKE 'SCP_%' AND pnl_pct IS NOT NULL"
        )
        closed_rows = await cur.fetchall()
        total = len(closed_rows)
        wins = sum(1 for p, _ in closed_rows if p > 0)
        losses = sum(1 for p, _ in closed_rows if p < 0)
        total_pnl_usdt = sum(pnl for _, pnl in closed_rows)
        total_pnl_pct = sum(p for p, _ in closed_rows)
        result["all"] = {
            "total": total,
            "wins": wins,
            "losses": losses,
            "win_rate": round((wins / total * 100) if total > 0 else 0, 1),
            "avg_pnl_pct": round((total_pnl_pct / total) if total > 0 else 0, 2),
            "total_pnl_usdt": round(total_pnl_usdt, 2),
        }

        return result


# singleton ------------------------------------------------
_storage: Optional[Storage] = None


def get_storage() -> Storage:
    global _storage
    if _storage is None:
        _storage = Storage(get_settings())
    return _storage
