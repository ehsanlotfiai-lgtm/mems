"""Hunter Tracker — ردیابی نتایج شکار و محاسبه درصد موفقیت.

هر توکنی که شکارچی شناسایی می‌کنه رو ذخیره می‌کنه و بعداً قیمتش رو
چک می‌کنه تا بفهمیم شناسایی‌ها چقدر دقیق بودن.

  - ذخیره هر detection با قیمت و زمان
  - بررسی قیمت بعد از ۵ دقیقه، ۳۰ دقیقه، ۱ ساعت، ۴ ساعت
  - محاسبه win rate به تفکیک استراتژی
  - نمایش ROI متوسط هر استراتژی
  - تشخیص بهترین/بدترین استراتژی
"""
from __future__ import annotations

import asyncio
import sqlite3
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional

from core.dex import DEXManager, DEXToken
from core.logging_setup import logger


# ==========================================================
# Data models
# ==========================================================

@dataclass
class DetectionRecord:
    """یک توکن شناسایی‌شده توسط شکارچی."""
    id: Optional[int] = None
    token_key: str = ""         # "chain:address"
    symbol: str = ""
    chain: str = ""
    dex: str = ""
    address: str = ""
    strategy: str = ""          # pre_pump | post_migration | smart_money | narrative
    score: float = 0.0
    price_at_detection: float = 0.0
    mcap_at_detection: float = 0.0
    volume_1h_at_detection: float = 0.0
    liquidity_at_detection: float = 0.0
    detected_at: float = 0.0
    signals: str = ""           # JSON list of signals
    risk_flags: str = ""        # JSON list of risk flags
    # Price checkpoints (filled later)
    price_5m: Optional[float] = None
    price_30m: Optional[float] = None
    price_1h: Optional[float] = None
    price_4h: Optional[float] = None
    price_24h: Optional[float] = None
    # Results
    peak_price: Optional[float] = None
    peak_roi_pct: Optional[float] = None
    is_winner: Optional[bool] = None  # True if any checkpoint > +20%
    is_rugpull: Optional[bool] = None
    checked_at: Optional[float] = None

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "token_key": self.token_key,
            "symbol": self.symbol,
            "chain": self.chain,
            "dex": self.dex,
            "address": self.address,
            "strategy": self.strategy,
            "score": round(self.score, 4),
            "price_at_detection": self.price_at_detection,
            "mcap_at_detection": self.mcap_at_detection,
            "volume_1h_at_detection": self.volume_1h_at_detection,
            "liquidity_at_detection": self.liquidity_at_detection,
            "detected_at": self.detected_at,
            "signals": self.signals,
            "risk_flags": self.risk_flags,
            "price_5m": self.price_5m,
            "price_30m": self.price_30m,
            "price_1h": self.price_1h,
            "price_4h": self.price_4h,
            "price_24h": self.price_24h,
            "peak_price": self.peak_price,
            "peak_roi_pct": self.peak_roi_pct,
            "is_winner": self.is_winner,
            "is_rugpull": self.is_rugpull,
            "checked_at": self.checked_at,
        }


@dataclass
class StrategyStats:
    """آمار موفقیت یک استراتژی."""
    strategy: str = ""
    total_detections: int = 0
    checked: int = 0
    winners: int = 0          # tokens that pumped > +20%
    losers: int = 0           # tokens that rugged or dropped > -50%
    win_rate: float = 0.0
    avg_roi_pct: float = 0.0
    avg_peak_roi_pct: float = 0.0
    best_detection_symbol: str = ""
    best_detection_roi: float = 0.0
    worst_detection_symbol: str = ""
    worst_detection_roi: float = 0.0
    # ROI by time
    avg_roi_5m: float = 0.0
    avg_roi_30m: float = 0.0
    avg_roi_1h: float = 0.0
    avg_roi_4h: float = 0.0

    def to_dict(self) -> dict:
        return {
            "strategy": self.strategy,
            "total_detections": self.total_detections,
            "checked": self.checked,
            "winners": self.winners,
            "losers": self.losers,
            "win_rate": round(self.win_rate, 1),
            "avg_roi_pct": round(self.avg_roi_pct, 2),
            "avg_peak_roi_pct": round(self.avg_peak_roi_pct, 2),
            "best_detection_symbol": self.best_detection_symbol,
            "best_detection_roi": round(self.best_detection_roi, 2),
            "worst_detection_symbol": self.worst_detection_symbol,
            "worst_detection_roi": round(self.worst_detection_roi, 2),
            "avg_roi_5m": round(self.avg_roi_5m, 2),
            "avg_roi_30m": round(self.avg_roi_30m, 2),
            "avg_roi_1h": round(self.avg_roi_1h, 2),
            "avg_roi_4h": round(self.avg_roi_4h, 2),
        }


# ==========================================================
# Database
# ==========================================================

class HunterTrackerDB:
    """SQLite storage for hunter detections and results."""

    def __init__(self, db_path: Path) -> None:
        self.db_path = db_path
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._conn: Optional[sqlite3.Connection] = None

    def connect(self) -> None:
        self._conn = sqlite3.connect(str(self.db_path))
        self._conn.row_factory = sqlite3.Row
        self._create_tables()

    def close(self) -> None:
        if self._conn:
            self._conn.close()
            self._conn = None

    def _create_tables(self) -> None:
        self._conn.executescript("""
            CREATE TABLE IF NOT EXISTS hunter_detections (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                token_key TEXT NOT NULL,
                symbol TEXT,
                chain TEXT,
                dex TEXT,
                address TEXT,
                strategy TEXT,
                score REAL,
                price_at_detection REAL,
                mcap_at_detection REAL,
                volume_1h_at_detection REAL,
                liquidity_at_detection REAL,
                detected_at REAL,
                signals TEXT,
                risk_flags TEXT,
                price_5m REAL,
                price_30m REAL,
                price_1h REAL,
                price_4h REAL,
                price_24h REAL,
                peak_price REAL,
                peak_roi_pct REAL,
                is_winner INTEGER,
                is_rugpull INTEGER,
                checked_at REAL,
                UNIQUE(token_key, strategy)
            );
            CREATE INDEX IF NOT EXISTS idx_hunter_strategy ON hunter_detections(strategy);
            CREATE INDEX IF NOT EXISTS idx_hunter_detected ON hunter_detections(detected_at);
        """)
        self._conn.commit()

    def save_detection(self, rec: DetectionRecord) -> None:
        """Save a detection record. Dedup on (token_key, strategy) — only update if new score is higher."""
        try:
            self._conn.execute("""
                INSERT INTO hunter_detections
                (token_key, symbol, chain, dex, address, strategy, score,
                 price_at_detection, mcap_at_detection, volume_1h_at_detection,
                 liquidity_at_detection, detected_at, signals, risk_flags)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(token_key, strategy) DO UPDATE SET
                    score=excluded.score,
                    detected_at=excluded.detected_at,
                    price_at_detection=excluded.price_at_detection,
                    volume_1h_at_detection=excluded.volume_1h_at_detection,
                    liquidity_at_detection=excluded.liquidity_at_detection,
                    signals=excluded.signals,
                    risk_flags=excluded.risk_flags,
                    checked_at=NULL,
                    price_5m=NULL, price_30m=NULL, price_1h=NULL, price_4h=NULL, price_24h=NULL,
                    peak_price=NULL, peak_roi_pct=NULL, is_winner=NULL, is_rugpull=NULL
                WHERE excluded.score > hunter_detections.score
            """, (
                rec.token_key, rec.symbol, rec.chain, rec.dex, rec.address,
                rec.strategy, rec.score, rec.price_at_detection,
                rec.mcap_at_detection, rec.volume_1h_at_detection,
                rec.liquidity_at_detection, rec.detected_at, rec.signals,
                rec.risk_flags,
            ))
            self._conn.commit()
        except Exception as exc:
            logger.warning(f"HunterTracker save_detection error: {exc}")

    def get_unchecked_detections(self, older_than_seconds: int = 300) -> List[DetectionRecord]:
        """Get detections that need price checking."""
        cutoff = time.time() - older_than_seconds
        rows = self._conn.execute(
            "SELECT * FROM hunter_detections WHERE checked_at IS NULL AND detected_at < ?",
            (cutoff,)
        ).fetchall()
        return [self._row_to_record(r) for r in rows]

    def get_recent_detections(self, limit: int = 100) -> List[DetectionRecord]:
        rows = self._conn.execute(
            "SELECT * FROM hunter_detections ORDER BY detected_at DESC LIMIT ?",
            (limit,)
        ).fetchall()
        return [self._row_to_record(r) for r in rows]

    def get_detections_for_check(self, min_age_seconds: int = 300) -> List[DetectionRecord]:
        """Get detections that are old enough to check but not yet fully checked."""
        now = time.time()
        # Need price_5m check: detected > 5 min ago but price_5m is null
        # Need price_1h check: detected > 1 hour ago but price_1h is null
        # etc.
        rows = self._conn.execute(
            """SELECT * FROM hunter_detections
               WHERE (
                 (price_5m IS NULL AND detected_at < ?)
                 OR (price_30m IS NULL AND detected_at < ?)
                 OR (price_1h IS NULL AND detected_at < ?)
                 OR (price_4h IS NULL AND detected_at < ?)
                 OR (price_24h IS NULL AND detected_at < ?)
               )
               ORDER BY detected_at ASC LIMIT 50""",
            (now - 300, now - 1800, now - 3600, now - 14400, now - 86400)
        ).fetchall()
        return [self._row_to_record(r) for r in rows]

    def update_price_checkpoint(self, token_key: str, strategy: str,
                                 field: str, price: float) -> None:
        """Update a price checkpoint (price_5m, price_1h, etc)."""
        if field not in ("price_5m", "price_30m", "price_1h", "price_4h", "price_24h"):
            return
        self._conn.execute(
            f"UPDATE hunter_detections SET {field} = ? WHERE token_key = ? AND strategy = ?",
            (price, token_key, strategy)
        )
        self._conn.commit()

    def finalize_detection(self, token_key: str, strategy: str,
                           peak_price: float, peak_roi_pct: float,
                           is_winner: bool, is_rugpull: bool) -> None:
        """Mark detection as fully checked."""
        self._conn.execute(
            """UPDATE hunter_detections
               SET peak_price = ?, peak_roi_pct = ?, is_winner = ?,
                   is_rugpull = ?, checked_at = ?
               WHERE token_key = ? AND strategy = ?""",
            (peak_price, peak_roi_pct, 1 if is_winner else 0,
             1 if is_rugpull else 0, time.time(), token_key, strategy)
        )
        self._conn.commit()

    def get_strategy_stats(self, strategy: str, min_score: float = 0.0, max_score: float = 1.0) -> StrategyStats:
        """Calculate success stats for a strategy, optionally filtered by score range."""
        rows = self._conn.execute(
            "SELECT * FROM hunter_detections WHERE strategy = ? AND score >= ? AND score <= ?",
            (strategy, min_score, max_score)
        ).fetchall()
        return self._calc_stats(strategy, rows)

    def get_all_stats(self, min_score: float = 0.0, max_score: float = 1.0) -> Dict[str, StrategyStats]:
        """Get stats for all strategies, optionally filtered by score range."""
        strategies = ["pre_pump", "post_migration", "smart_money", "narrative",
                       "contract_safety", "whale_activity", "liquidity_health",
                       "holder_distribution", "volume_profile", "momentum_breakout"]
        return {s: self.get_strategy_stats(s, min_score=min_score, max_score=max_score)
                for s in strategies}

    def get_overall_stats(self, min_score: float = 0.0, max_score: float = 1.0) -> dict:
        """Overall stats across all strategies, optionally filtered by score range."""
        rows = self._conn.execute(
            "SELECT * FROM hunter_detections WHERE score >= ? AND score <= ?",
            (min_score, max_score)
        ).fetchall()
        if not rows:
            return {"total": 0, "checked": 0, "win_rate": 0, "avg_roi_pct": 0}
        stats = self._calc_stats("all", rows)
        return stats.to_dict()

    def _calc_stats(self, strategy: str, rows: list) -> StrategyStats:
        if not rows:
            return StrategyStats(strategy=strategy)

        s = StrategyStats(strategy=strategy)
        s.total_detections = len(rows)

        rois = []
        peak_rois = []
        roi_5m, roi_30m, roi_1h, roi_4h = [], [], [], []
        best_roi = -999
        worst_roi = 999
        best_sym = ""
        worst_sym = ""

        for r in rows:
            rec = self._row_to_record(r)
            if rec.checked_at:
                s.checked += 1
            if rec.is_winner:
                s.winners += 1
            if rec.is_rugpull:
                s.losers += 1
            if rec.peak_roi_pct is not None:
                peak_rois.append(rec.peak_roi_pct)
                if rec.peak_roi_pct > best_roi:
                    best_roi = rec.peak_roi_pct
                    best_sym = rec.symbol
                if rec.peak_roi_pct < worst_roi:
                    worst_roi = rec.peak_roi_pct
                    worst_sym = rec.symbol
            # ROI by time checkpoint
            if rec.price_at_detection and rec.price_at_detection > 0:
                base = rec.price_at_detection
                if rec.price_5m:
                    roi_5m.append((rec.price_5m - base) / base * 100)
                if rec.price_30m:
                    roi_30m.append((rec.price_30m - base) / base * 100)
                if rec.price_1h:
                    roi_1h.append((rec.price_1h - base) / base * 100)
                if rec.price_4h:
                    roi_4h.append((rec.price_4h - base) / base * 100)

        if s.checked > 0:
            s.win_rate = (s.winners / s.checked) * 100
        if peak_rois:
            s.avg_peak_roi_pct = sum(peak_rois) / len(peak_rois)
        if roi_5m:
            s.avg_roi_5m = sum(roi_5m) / len(roi_5m)
        if roi_30m:
            s.avg_roi_30m = sum(roi_30m) / len(roi_30m)
        if roi_1h:
            s.avg_roi_1h = sum(roi_1h) / len(roi_1h)
        if roi_4h:
            s.avg_roi_4h = sum(roi_4h) / len(roi_4h)
        if best_roi > -999:
            s.best_detection_symbol = best_sym
            s.best_detection_roi = best_roi
        if worst_roi < 999:
            s.worst_detection_symbol = worst_sym
            s.worst_detection_roi = worst_roi
        return s

    def _row_to_record(self, row) -> DetectionRecord:
        return DetectionRecord(
            id=row["id"], token_key=row["token_key"], symbol=row["symbol"],
            chain=row["chain"], dex=row["dex"], address=row["address"],
            strategy=row["strategy"], score=row["score"],
            price_at_detection=row["price_at_detection"],
            mcap_at_detection=row["mcap_at_detection"],
            volume_1h_at_detection=row["volume_1h_at_detection"],
            liquidity_at_detection=row["liquidity_at_detection"],
            detected_at=row["detected_at"], signals=row["signals"],
            risk_flags=row["risk_flags"],
            price_5m=row["price_5m"], price_30m=row["price_30m"],
            price_1h=row["price_1h"], price_4h=row["price_4h"],
            price_24h=row["price_24h"],
            peak_price=row["peak_price"], peak_roi_pct=row["peak_roi_pct"],
            is_winner=bool(row["is_winner"]) if row["is_winner"] is not None else None,
            is_rugpull=bool(row["is_rugpull"]) if row["is_rugpull"] is not None else None,
            checked_at=row["checked_at"],
        )


# ==========================================================
# Price Checker — بررسی قیمت بعد از تشخیص
# ==========================================================

class PriceChecker:
    """بررسی قیمت توکن‌های شناسایی‌شده در بازه‌های زمانی مختلف.

    بعد از ۵ دقیقه، ۳۰ دقیقه، ۱ ساعت، ۴ ساعت، ۲۴ ساعت
    قیمت رو چک می‌کنه و نتیجه رو ذخیره می‌کنه.
    """

    def __init__(self, tracker: HunterTrackerDB) -> None:
        self.tracker = tracker
        self._checking = False

    async def check_pending(self, dex_mgr: 'DEXManager') -> int:
        """Check all pending detections. Returns number checked."""
        if self._checking:
            return 0
        self._checking = True
        checked = 0

        try:
            pending = self.tracker.get_detections_for_check()
            if not pending:
                return 0

            # Group by chain for batch price lookup
            by_chain: Dict[str, List[DetectionRecord]] = {}
            for rec in pending:
                by_chain.setdefault(rec.chain, []).append(rec)

            now = time.time()

            for chain, records in by_chain.items():
                addrs = [r.address for r in records if r.address]
                if not addrs:
                    continue

                # Batch price lookup via DexScreener
                prices = await self._batch_price_lookup(dex_mgr, chain, addrs)

                for rec in records:
                    current_price = prices.get(rec.address, 0)
                    if current_price <= 0:
                        continue

                    age = now - rec.detected_at

                    # Update time-based checkpoints
                    updates = []
                    if rec.price_5m is None and age >= 300:
                        self.tracker.update_price_checkpoint(
                            rec.token_key, rec.strategy, "price_5m", current_price)
                        updates.append(("5m", current_price))

                    if rec.price_30m is None and age >= 1800:
                        self.tracker.update_price_checkpoint(
                            rec.token_key, rec.strategy, "price_30m", current_price)
                        updates.append(("30m", current_price))

                    if rec.price_1h is None and age >= 3600:
                        self.tracker.update_price_checkpoint(
                            rec.token_key, rec.strategy, "price_1h", current_price)
                        updates.append(("1h", current_price))

                    if rec.price_4h is None and age >= 14400:
                        self.tracker.update_price_checkpoint(
                            rec.token_key, rec.strategy, "price_4h", current_price)
                        updates.append(("4h", current_price))

                    if rec.price_24h is None and age >= 86400:
                        self.tracker.update_price_checkpoint(
                            rec.token_key, rec.strategy, "price_24h", current_price)
                        updates.append(("24h", current_price))

                    # Calculate peak ROI and win/loss
                    all_prices = [
                        p for p in [rec.price_5m, rec.price_30m, rec.price_1h,
                                    rec.price_4h, rec.price_24h, current_price]
                        if p and p > 0
                    ]
                    if all_prices and rec.price_at_detection > 0:
                        peak = max(all_prices)
                        peak_roi = (peak - rec.price_at_detection) / rec.price_at_detection * 100
                        is_winner = peak_roi >= 20  # +20% = winner
                        # Rugpull = dropped > 80% from detection price
                        is_rugpull = current_price < rec.price_at_detection * 0.2

                        self.tracker.finalize_detection(
                            rec.token_key, rec.strategy,
                            peak, peak_roi, is_winner, is_rugpull
                        )
                    checked += 1

        except Exception as exc:  # noqa: BLE001
            logger.error(f"PriceChecker error: {exc}")
        finally:
            self._checking = False

        if checked > 0:
            logger.info(f"PriceChecker: checked {checked} detections")
        return checked

    async def _batch_price_lookup(
        self, dex_mgr: 'DEXManager', chain: str, addresses: List[str]
    ) -> Dict[str, float]:
        """Batch lookup current prices via DexScreener."""
        prices: Dict[str, float] = {}
        client = await dex_mgr.ds._get_client()

        for i in range(0, len(addresses), 30):
            batch = addresses[i:i + 30]
            try:
                from core.dex import DEXSCREENER_BASE
                resp = await client.get(
                    f"{DEXSCREENER_BASE}/tokens/v1/{chain}/{','.join(batch)}",
                    timeout=10.0,
                )
                resp.raise_for_status()
                pairs = resp.json()
                parsed = dex_mgr.ds._parse_pairs(pairs)
                for t in parsed:
                    if t.address and t.price_usd > 0:
                        prices[t.address] = t.price_usd
            except Exception as exc:  # noqa: BLE001
                logger.debug(f"PriceChecker batch {chain}: {exc}")

        return prices


# ==========================================================
# Singleton
# ==========================================================

_tracker: Optional[HunterTrackerDB] = None
_checker: Optional[PriceChecker] = None


def get_hunter_tracker(db_path: Optional[Path] = None) -> HunterTrackerDB:
    global _tracker
    if _tracker is None:
        if db_path is None:
            from config.settings import get_settings
            s = get_settings()
            db_path = s.sqlite_path.parent / "hunter_tracker.sqlite"
        _tracker = HunterTrackerDB(db_path)
        _tracker.connect()
    return _tracker


def get_price_checker(tracker: Optional[HunterTrackerDB] = None) -> PriceChecker:
    global _checker
    if _checker is None:
        if tracker is None:
            tracker = get_hunter_tracker()
        _checker = PriceChecker(tracker)
    return _checker
