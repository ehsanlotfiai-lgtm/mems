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
        slippage_usdt REAL DEFAULT 0.0,
        tp2 REAL,
        tp3 REAL,
        tp1_hit INTEGER DEFAULT 0,
        risk_free INTEGER DEFAULT 0,
        unrealized_pnl_pct REAL
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
            "SELECT * FROM signals WHERE id NOT LIKE 'SCP_%' AND id NOT LIKE 'LIT_%' AND exchange NOT IN ('dex', 'pumpfun', 'raydium', 'pancakeswap', 'uniswap') ORDER BY created_at DESC LIMIT ?", (limit,)
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

    async def has_unresolved_signal_for_symbol(
        self, symbol: str, like_prefix: Optional[str] = None,
        max_age_seconds: float = 21600,
    ) -> bool:
        """True if `symbol` already has a signal sitting at status='open'
        within the recent window.

        This is the DB-level half of the duplicate-signal fix: a signal's
        status stays 'open' forever whenever RiskEngine.open_from_signal()
        rejects opening an actual paper position for it (e.g. max_open_positions
        already full, or the entry-slippage guard rejects it) — save_signal()
        is always called BEFORE that open attempt, so the row exists
        regardless of whether a position was ever tracked. Combined with a
        cooldown that only blocks re-evaluation for a short window, the same
        market setup kept re-triggering a fresh near-identical signal every
        cooldown cycle once the old one's cooldown expired, producing exact
        duplicate entry/SL/TP rows repeating indefinitely (the reported bug).

        like_prefix: e.g. 'SCP_%' for scalp signals, 'LIT_%' for LIT signals,
        or None to check the main confluence-engine signals (which have no
        prefix at all — matched by NOT LIKE both other prefixes, matching the
        existing convention in recent_signals()).
        """
        cutoff = time.time() - max_age_seconds
        if like_prefix:
            cur = await self.db.execute(
                "SELECT 1 FROM signals WHERE symbol=? AND status='open' "
                "AND id LIKE ? AND created_at > ? LIMIT 1",
                (symbol, like_prefix, cutoff),
            )
        else:
            cur = await self.db.execute(
                "SELECT 1 FROM signals WHERE symbol=? AND status='open' "
                "AND id NOT LIKE 'SCP_%' AND id NOT LIKE 'LIT_%' AND created_at > ? LIMIT 1",
                (symbol, cutoff),
            )
        row = await cur.fetchone()
        return row is not None

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
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
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

    # ------------------------------------------ time-based win rates (ALL trades)
    async def get_time_win_rates(self) -> dict:
        """Win rates for SIGNAL tab only (exclude SCP_ and LIT_)."""
        now = time.time()
        windows = {
            "hourly": 3600,
            "4hour": 14400,
            "daily": 86400,
            "weekly": 604800,
        }
        result = {}
        for label, seconds in windows.items():
            cutoff = now - seconds
            cur = await self.db.execute(
                """SELECT pnl_pct, pnl_usdt, close_reason, opened_at
                   FROM paper_trades 
                   WHERE opened_at >= ? 
                   AND signal_id NOT LIKE 'SCP_%' 
                   AND signal_id NOT LIKE 'LIT_%'""",
                (cutoff,)
            )
            rows = await cur.fetchall()
            closed_rows = [(p, pnl, r, o) for p, pnl, r, o in rows if p is not None]
            wins = sum(1 for p, _, _, _ in closed_rows if p > 0)
            losses = sum(1 for p, _, _, _ in closed_rows if p < 0)
            risk_free = sum(1 for _, _, r, _ in closed_rows if r and 'risk_free' in str(r))
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
                "risk_free": risk_free,
                "win_rate": round(win_rate, 1),
                "avg_pnl_pct": round(avg_pnl, 2),
                "avg_pnl_usdt": round(avg_pnl_usdt, 2),
                "total_pnl_usdt": round(total_pnl_usdt, 2),
            }

        cur = await self.db.execute(
            """SELECT pnl_pct, pnl_usdt FROM paper_trades
               WHERE pnl_pct IS NOT NULL
               AND signal_id NOT LIKE 'SCP_%'
               AND signal_id NOT LIKE 'LIT_%'"""
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
        sig_ids = []
        for r in rows:
            d = dict(zip(cols, r))
            d["hits"] = json.loads(d.pop("hits_json") or "[]")
            d["tf_breakdown"] = json.loads(d.pop("tf_breakdown_json") or "{}")
            # entry_time == when the signal/trade actually opened (== created_at,
            # kept as an explicit alias so the frontend never has to guess which
            # field means "entry" vs. just "when the row was inserted").
            d["entry_time"] = d.get("created_at")
            d["exit_time"] = None
            d["exit_price"] = None
            d["close_reason"] = None
            d["pnl_pct"] = None
            out.append(d)
            sig_ids.append(d["id"])

        if sig_ids:
            placeholders = ",".join("?" for _ in sig_ids)
            trade_cur = await self.db.execute(
                f"""SELECT signal_id, opened_at, closed_at, exit_price, close_reason, pnl_pct
                    FROM paper_trades WHERE signal_id IN ({placeholders})
                    ORDER BY opened_at DESC""",
                sig_ids,
            )
            trade_rows = await trade_cur.fetchall()
            by_signal = {}
            for sig_id, opened_at, closed_at, exit_price, close_reason, pnl_pct in trade_rows:
                if sig_id not in by_signal:  # keep the most recent trade per signal
                    by_signal[sig_id] = (opened_at, closed_at, exit_price, close_reason, pnl_pct)
            for d in out:
                t = by_signal.get(d["id"])
                if t:
                    opened_at, closed_at, exit_price, close_reason, pnl_pct = t
                    if opened_at:
                        d["entry_time"] = opened_at  # actual paper-trade open time
                    d["exit_time"] = closed_at
                    d["exit_price"] = exit_price
                    d["close_reason"] = close_reason
                    d["pnl_pct"] = pnl_pct
        return out

    async def get_signal_by_id(self, signal_id: str) -> Optional[dict]:
        cur = await self.db.execute("SELECT * FROM signals WHERE id=?", (signal_id,))
        row = await cur.fetchone()
        if row is None:
            return None
        cols = [d[0] for d in cur.description]
        d = dict(zip(cols, row))
        d["hits"] = json.loads(d.pop("hits_json") or "[]")
        d["tf_breakdown"] = json.loads(d.pop("tf_breakdown_json") or "{}")
        return d

    async def get_paper_trades_by_signal(self, signal_id: str) -> List[dict]:
        """All paper trades (open or closed) linked to a signal, most recent first."""
        cur = await self.db.execute(
            "SELECT * FROM paper_trades WHERE signal_id=? ORDER BY opened_at DESC", (signal_id,)
        )
        rows = await cur.fetchall()
        cols = [d[0] for d in cur.description]
        return [dict(zip(cols, r)) for r in rows]

    async def get_lit_win_rates(self) -> dict:
        """Win-rate stats for LIT signals, computed from signals.status (NOT
        paper_trades — LIT positions frequently never get a tracked
        PaperPosition because they share the same global max_open_positions
        cap with the main/scalp engines, so paper_trades often has no row
        for a LIT signal at all). api_lit_signals() resolves+persists the
        real outcome onto signals.status by checking candle history, and
        this function reads that persisted status."""
        now = time.time()
        windows = {"last_hour": 3600, "last_4h": 14400, "today": 86400}
        result = {}
        win_statuses = ("tp", "tp1", "tp2", "tp3")
        loss_statuses = ("sl", "sl_risk_free")
        for label, seconds in windows.items():
            cutoff = now - seconds
            cur = await self.db.execute(
                "SELECT status FROM signals WHERE id LIKE 'LIT_%' AND created_at >= ?",
                (cutoff,),
            )
            rows = await cur.fetchall()
            statuses = [r[0] or "open" for r in rows]
            wins = sum(1 for st in statuses if st in win_statuses)
            losses = sum(1 for st in statuses if st in loss_statuses)
            open_count = sum(1 for st in statuses if st == "open")
            total = wins + losses
            result[label] = {
                "total": total,
                "open": open_count,
                "wins": wins,
                "losses": losses,
                "win_rate": round((wins / total * 100) if total > 0 else 0, 1),
            }

        cur = await self.db.execute("SELECT status FROM signals WHERE id LIKE 'LIT_%'")
        rows = await cur.fetchall()
        statuses = [r[0] or "open" for r in rows]
        wins = sum(1 for st in statuses if st in win_statuses)
        losses = sum(1 for st in statuses if st in loss_statuses)
        total = wins + losses
        result["all"] = {
            "total": total,
            "wins": wins,
            "losses": losses,
            "win_rate": round((wins / total * 100) if total > 0 else 0, 1),
        }
        return result

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
            risk_free = sum(1 for _, _, r, _ in closed_rows if r and 'risk_free' in str(r))
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
                "risk_free": risk_free,
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
