"""Live forward-engine (paper trading).

Always-on loop:
  * Connects to WebSocket streams for the trigger timeframe on each
    enabled exchange.
  * For every *closed* candle, runs the ConfluenceEngine over a window
    pulled from an in-memory ring buffer (no extra REST hits in
    steady-state).
  * On a passing signal, opens a paper position through RiskEngine
    and stores it in SQLite.
  * On every trigger candle also evaluates open positions for SL/TP/
    trailing.

This is the module that systemd runs forever on the Ubuntu server.
"""
from __future__ import annotations

import asyncio
import time
from collections import defaultdict, deque
from dataclasses import dataclass
from typing import Any, Awaitable, Callable, Deque, Dict, List, Optional, Set

import pandas as pd

from config.settings import Settings
from core.dex import DEXManager, DEXToken, dex_token_to_symbol_info, get_dex_manager
from core.exchange import ExchangeManager, SymbolInfo, get_exchange_manager
from core.logging_setup import logger
from core.meme_hunter import MemeHunter, get_meme_hunter
from core.hunter_tracker import get_hunter_tracker, get_price_checker
from core.models import Candle, PaperPosition, Signal, Side
from core.risk import RiskEngine
from core.storage import Storage
from core.universe import build_universes
from notify.telegram_bot import TelegramNotifier
from strategies.strategy_engine import ConfluenceEngine
from strategies.scalping_engine import ScalpingEngine


@dataclass
class Tick:
    symbol: str
    timeframe: str
    candle: Candle


class InMemoryStore:
    """In-memory ring buffer of candles keyed by (exchange, symbol, timeframe)."""

    def __init__(self, max_per_key: int = 2000) -> None:
        self.max = max_per_key
        self.data: Dict[str, Deque[Candle]] = defaultdict(lambda: deque(maxlen=self.max))

    def push(self, exchange: str, symbol: str, tf: str, candle: Candle) -> None:
        self.data[f"{exchange}|{symbol}|{tf}"].append(candle)

    def get(self, exchange: str, symbol: str, tf: str) -> List[Candle]:
        return list(self.data.get(f"{exchange}|{symbol}|{tf}", []))

    async def fetch_multi_tf(self, exchange: str, symbol: str, timeframes: List[str]) -> Dict[str, List[Candle]]:
        return {tf: self.get(exchange, symbol, tf) for tf in timeframes}


class ForwardEngine:
    def __init__(
        self,
        settings: Settings,
        em: ExchangeManager,
        risk: RiskEngine,
        storage: Storage,
        notify: Optional[TelegramNotifier] = None,
    ) -> None:
        self.s = settings
        self.em = em
        self.risk = risk
        self.storage = storage
        self.notify = notify
        self.store = InMemoryStore()
        self.universes: Dict[str, List[SymbolInfo]] = {}
        self.universe_lock = asyncio.Lock()
        self.confluence: ConfluenceEngine = ConfluenceEngine(
            settings,
            candle_provider=self._candle_provider,
            orderbook_provider=self._orderbook_provider,
            extra_context_provider=self._extra_context_provider,
        )
        self.stop_event = asyncio.Event()
        self._tasks: List[asyncio.Task] = []
        # — per-instance callback lists (fix: avoid class-level shared state) —
        self._dashboard_callbacks: List[Callable[[dict], Awaitable[None]]] = []
        self._universe_callbacks: List[Callable[[dict], Awaitable[None]]] = []
        # last evaluated candles per (exchange, symbol, tf) to avoid re-evaluating
        self.last_eval_ts: Dict[str, int] = {}
        self.last_universe_refresh = 0.0
        # DEX layer
        self.dex_enabled = bool(settings.dex.get("enabled", True))
        self.dex_mgr: Optional[DEXManager] = None
        self.dex_tokens: Dict[str, DEXToken] = {}   # key = "chain:address"
        self.dex_infos: Dict[str, SymbolInfo] = {}  # key = "chain:address"
        self.dex_lock = asyncio.Lock()
        self._dex_candle_cache_ts: Dict[str, float] = {}  # "chain|pair|tf" -> last REST refresh time
        # Signal cooldown — prevent same symbol from firing again within N seconds
        self._signal_cooldowns: Dict[str, float] = {}  # symbol -> last_signal_timestamp
        self._signal_cooldown_seconds: float = float(settings.forward.get("signal_cooldown_seconds", 3600))
        # News/Fundamentals tracker
        self.news_tracker: Optional[Any] = None
        self._news_score_cache: Dict[str, Any] = {}
        # Meme hunter (۱۰ استراتژی شکار میم‌کوین)
        self.meme_hunter_enabled = bool(settings.meme_hunter.get("enabled", True))
        self.meme_hunter: Optional[MemeHunter] = None
        self.meme_hunter_results: Dict[str, Any] = {}

    # ---------------------------------------------------- public lifecycle
    async def start(self) -> None:
        await self.em.start()
        await self.storage.connect()
        # Restore open positions from DB after restart
        try:
            open_rows = await self.storage.load_open_paper_trades()
            if open_rows:
                self.risk.load_open_positions(open_rows)
                logger.info(f"Restored {len(open_rows)} open positions from DB")
        except Exception as exc:
            logger.debug(f"Failed to restore open positions: {exc}")
        await self._refresh_universes()
        # Initialize news tracker
        try:
            from core.news import get_news_tracker
            self.news_tracker = get_news_tracker(self.s.fundamentals)
            await self.news_tracker.refresh()
            self._news_score_cache = self.news_tracker.get_dict()
        except Exception as exc:  # noqa: BLE001
            logger.debug(f"News tracker init failed: {exc}")
        # Periodic universe refresh
        self._tasks.append(asyncio.create_task(self._universe_refresh_loop()))
        # WS streams per exchange for the trigger TF
        for ex_name, conf in self.s.exchanges.items():
            if not conf.get("enable", True):
                continue
            symbols = [i.symbol for i in self.universes.get(ex_name, [])]
            if not symbols:
                continue
            self._tasks.append(
                asyncio.create_task(self._ws_loop(ex_name, symbols, self.s.trigger_timeframe))
            )
        # REST-based signal polling as fallback (in case WS doesn't fire)
        self._tasks.append(asyncio.create_task(self._signal_poll_loop()))
        # Prune ticks periodically
        self._tasks.append(asyncio.create_task(self._tick_prune_loop()))
        # DEX layer — poll Pump.fun / Raydium / PancakeSwap / Uniswap
        if self.dex_enabled:
            self.dex_mgr = get_dex_manager(self.s.dex)
            self._tasks.append(asyncio.create_task(self._dex_discovery_loop()))
            self._tasks.append(asyncio.create_task(self._dex_snipe_loop()))
            logger.info("DEX layer enabled (Pump.fun / Raydium / PancakeSwap / Uniswap)")
        # Meme Hunter — ۱۰ استراتژی شکار میم‌کوین
        if self.meme_hunter_enabled and self.dex_enabled:
            self.meme_hunter = get_meme_hunter(self.s.meme_hunter)
            self._tasks.append(asyncio.create_task(self._meme_hunter_loop()))
            self._tasks.append(asyncio.create_task(self._price_checker_loop()))
            logger.info("MemeHunter enabled: 10 strategies + success tracking")
        # Scalping engine — اسکلپ روی ارزهای پرحجم
        self.scalp_enabled = bool(self.s.scalping.get("enabled", False))
        if self.scalp_enabled:
            self.scalp_engine = ScalpingEngine(
                self.s, self._scalp_candle_provider, self._orderbook_provider
            )
            self._scalp_cooldowns: Dict[str, float] = {}
            self._tasks.append(asyncio.create_task(self._scalp_loop()))
            logger.info("ScalpingEngine enabled: 10 strategies on top 200 coins (spot+futures)")
        # LIT Engine — استراتژی Liquidity Inducement
        self.lit_enabled = bool(self.s.raw.get("lit", {}).get("enabled", False))
        if self.lit_enabled:
            from strategies.lit_engine import LITEngine
            self.lit_engine = LITEngine(self.s.raw.get("lit", {}))
            self._lit_cooldowns: Dict[str, float] = {}
            self._tasks.append(asyncio.create_task(self._lit_loop()))
            logger.info("LIT Engine enabled: Liquidity Inducement Theorem strategies")
        # Live price ticker — قیمت لایو هر ۱۰ ثانیه
        self._tasks.append(asyncio.create_task(self._live_price_loop()))
        logger.info(f"ForwardEngine started. Universe sizes: { {k: len(v) for k, v in self.universes.items()} }")

    async def stop(self) -> None:
        self.stop_event.set()
        for t in self._tasks:
            t.cancel()
        await self.em.stop()
        if self.dex_mgr is not None:
            await self.dex_mgr.close()
        await self.storage.close()
        logger.info("ForwardEngine stopped.")

    # ---------------------------------------------------- runtime methods
    async def _extra_context_provider(self, exchange: str, symbol: str, tf: str) -> dict:
        """Provide news/sentiment data to the strategy engine."""
        if not self.news_tracker:
            return {}
        try:
            await self.news_tracker.refresh()
        except Exception:  # noqa: BLE001
            pass
        score_dict = self.news_tracker.get_dict()
        if not score_dict:
            return {}
        is_trending = self.news_tracker.is_trending(symbol.split("/")[0] if "/" in symbol else symbol)
        result = {"fundamental_score": score_dict}
        if is_trending:
            result["fundamental_score"]["is_trending"] = True
        return result

    async def _scalp_candle_provider(self, exchange: str, symbol: str) -> Dict[str, List[Candle]]:
        """Candle provider for scalping engine — returns scalp TFs (5m, 15m) + HTF TFs (1h, 4h)."""
        out: Dict[str, List[Candle]] = {}
        scalp_tfs = set(self.s.scalping.get("timeframes", ["5m", "15m"]))
        htf_tfs = set(self.s.scalping.get("htf_timeframes", ["1h", "4h"]))
        all_tfs = scalp_tfs | htf_tfs
        for tf in all_tfs:
            cached = self.store.get(exchange, symbol, tf)
            if len(cached) < 10:
                try:
                    fresh = await self.em.fetch_ohlcv(exchange, symbol, tf, limit=200)
                    for c in fresh:
                        self.store.push(exchange, symbol, tf, c)
                    cached = self.store.get(exchange, symbol, tf)
                except Exception as exc:
                    logger.debug(f"Scalp REST boot {exchange} {symbol} {tf}: {exc}")
            out[tf] = cached
        return out

    async def _candle_provider(self, exchange: str, symbol: str) -> Dict[str, List[Candle]]:
        if symbol.startswith("DEX:"):
            return await self._dex_candle_provider(exchange, symbol)
        tfs = self.s.timeframes
        out: Dict[str, List[Candle]] = {}
        for tf in tfs:
            cached = self.store.get(exchange, symbol, tf)
            if len(cached) < 30:
                try:
                    fresh = await self.em.fetch_ohlcv(exchange, symbol, tf, limit=500)
                    for c in fresh:
                        self.store.push(exchange, symbol, tf, c)
                    cached = self.store.get(exchange, symbol, tf)
                except Exception as exc:  # noqa: BLE001
                    logger.debug(f"REST boot {exchange} {symbol} {tf}: {exc}")
            out[tf] = cached
        return out

    async def _dex_candle_provider(self, exchange: str, symbol: str) -> Dict[str, List[Candle]]:
        _, chain, address = symbol.split(":", 2)
        token = self.dex_tokens.get(f"{chain}:{address}")
        pair_address = token.pair_address if token else ""
        tfs = self.s.timeframes
        out: Dict[str, List[Candle]] = {}
        now = time.time()
        min_refresh_seconds = float(self.s.dex.get("price_poll_interval_seconds", 30))
        for tf in tfs:
            cache_key = f"{chain}|{pair_address}|{tf}"
            cached = self.store.get(exchange, symbol, tf)
            last_refresh = self._dex_candle_cache_ts.get(cache_key, 0.0)
            if pair_address and (len(cached) < 30 or now - last_refresh > min_refresh_seconds):
                try:
                    fresh = await self.dex_mgr.get_token_candles(chain, pair_address, tf, count=200)
                    for c in fresh:
                        self.store.push(exchange, symbol, tf, c)
                    self._dex_candle_cache_ts[cache_key] = now
                    cached = self.store.get(exchange, symbol, tf)
                except Exception as exc:  # noqa: BLE001
                    logger.debug(f"DEX candle fetch {symbol} {tf}: {exc}")
            out[tf] = cached
        return out

    async def _orderbook_provider(self, exchange: str, symbol: str) -> dict:
        if symbol.startswith("DEX:"):
            return {}
        return await self.em.fetch_order_book(
            exchange, symbol,
            limit=int(self.s.strategies.get("orderbook_imbalance", {}).get("depth_levels", 20))
        )

    async def _ws_loop(self, ex_name: str, symbols: List[str], tf: str) -> None:
        async def on_candle(symbol: str, timeframe: str, candle: Candle) -> None:
            self.store.push(ex_name, symbol, timeframe, candle)
            await self.storage.add_tick(ex_name, symbol, candle.close, timeframe)
            await self._maybe_evaluate(ex_name, symbol, timeframe, candle)
        await self.em.stream_klines(ex_name, symbols, tf, on_candle, stop_event=self.stop_event)

    async def _maybe_evaluate(self, exchange: str, symbol: str, timeframe: str, candle: Candle) -> None:
        if timeframe != self.s.trigger_timeframe:
            return
        key = f"{exchange}|{symbol}|{timeframe}"
        if self.last_eval_ts.get(key) == candle.timestamp:
            return
        self.last_eval_ts[key] = candle.timestamp
        now = time.time()
        if now - self._signal_cooldowns.get(symbol, 0) < self._signal_cooldown_seconds:
            return
        info = None
        for i in (self.universes.get(exchange) or []):
            if i.symbol == symbol:
                info = i
                break
        if info is None:
            return
        try:
            sig = await self.confluence.evaluate_symbol(exchange, info)
        except Exception as exc:  # noqa: BLE001
            logger.exception(f"evaluate error {exchange} {symbol}: {exc}")
            return
        closed = self.risk.update_with_price(symbol, candle.close)
        for pos in closed:
            await self.storage.update_paper_close(pos)
        if sig is None:
            return
        # Duplicate-signal guard: skip if this symbol already has an open
        # position OR an unresolved signal row (status still 'open') — this
        # is the exact bug pattern reported (same entry/SL/TP repeating
        # every few minutes): save_signal() runs BEFORE we know if a paper
        # position will actually be opened, so a symbol whose previous
        # signal's position was rejected (max_open_positions full, slippage
        # guard, etc.) kept generating a fresh near-identical signal every
        # time its short cooldown expired, forever, since nothing ever
        # marked the old row as resolved.
        if self.risk.has_open_position_for_symbol(symbol):
            logger.debug(f"Signal SKIPPED {symbol}: already has an open position")
            return
        if await self.storage.has_unresolved_signal_for_symbol(symbol):
            logger.debug(f"Signal SKIPPED {symbol}: unresolved signal already pending")
            return
        if not self._allow_new_action("signals"):
            return
        await self.storage.save_signal(sig)
        self._signal_cooldowns[symbol] = time.time()
        logger.info(f"SIGNAL {sig.exchange} {sig.symbol} {sig.side.value} score={sig.score:.2f} -> {sig.rationale}")
        # candle.close is the just-closed live candle for this exact WS tick,
        # so it's a good live-price cross-check for the slippage guard.
        pos = self.risk.open_from_signal(sig, live_price=float(candle.close))
        if pos is not None:
            await self.storage.open_paper(pos, signal_id=sig.id)
            self._emit_dashboard({"type": "signal_opened", "signal": sig.to_dict(), "position": pos.to_dict()})
        else:
            # No paper position was actually opened (max_open_positions full,
            # slippage guard rejected it, etc.) — mark the signal as
            # "no_position" immediately instead of leaving it at 'open'
            # forever. Otherwise the duplicate-signal guard above would
            # block this symbol from ever generating a new signal again.
            await self.storage.update_signal_status(sig.id, "no_position")
        if self.notify is not None:
            try:
                await self.notify.send_signal(sig)
            except Exception as exc:  # noqa: BLE001
                logger.warning(f"telegram send failed: {exc}")
        for cb in self._dashboard_callbacks:
            try:
                await cb({"type": "signal", "data": sig.to_dict()})
            except Exception as exc:  # noqa: BLE001
                logger.debug(f"dashboard cb error: {exc}")

    # ---------------------------------------------------- universe loop
    async def _universe_refresh_loop(self) -> None:
        while not self.stop_event.is_set():
            wait = float(self.s.forward.get("universe_refresh_minutes", 15)) * 60
            try:
                await asyncio.sleep(wait)
                await self._refresh_universes()
            except asyncio.CancelledError:
                break
            except Exception as exc:  # noqa: BLE001
                logger.exception(f"universe refresh: {exc}")

    async def _refresh_universes(self) -> None:
        try:
            async with self.universe_lock:
                self.universes = await build_universes(self.em, self.s, futures_only=True)
                self.last_universe_refresh = time.time()
                for cb in self._universe_callbacks:
                    try:
                        await cb({ex: [
                            {"symbol": i.symbol, "base": i.base,
                             "listed_at": i.listed_at} for i in lst
                        ] for ex, lst in self.universes.items()})
                    except Exception as exc:  # noqa: BLE001
                        logger.debug(f"universe cb error: {exc}")
        except Exception as exc:  # noqa: BLE001
            logger.exception(f"refresh universes failed: {exc}")

    async def _tick_prune_loop(self) -> None:
        while not self.stop_event.is_set():
            await asyncio.sleep(600)
            try:
                await self.storage.prune_ticks(keep_seconds=6 * 3600)
            except Exception as exc:  # noqa: BLE001
                logger.debug(f"prune ticks: {exc}")

    # ---------------------------------------------------- DEX loops
    async def _dex_discovery_loop(self) -> None:
        interval = float(self.s.dex.get("discovery_interval_minutes", 5)) * 60
        max_tracked = int(self.s.dex.get("max_tracked_tokens", 200))
        while not self.stop_event.is_set():
            try:
                tokens = await self.dex_mgr.discover_tokens(limit=max_tracked)
                async with self.dex_lock:
                    fresh_map = {f"{t.chain}:{t.address}": t for t in tokens}
                    self.dex_tokens = fresh_map
                    self.dex_infos = {
                        key: dex_token_to_symbol_info(t) for key, t in fresh_map.items()
                    }
                for cb in self._universe_callbacks:
                    try:
                        await cb({"dex": [
                            {
                                "chain": t.chain, "dex": t.dex, "address": t.address,
                                "symbol": t.symbol, "name": t.name,
                                "price_usd": t.price_usd, "liquidity_usd": t.liquidity_usd,
                                "volume_1h_usd": t.volume_1h_usd, "mcap": t.mcap,
                                "age_seconds": t.age_seconds, "is_brand_new": t.is_brand_new,
                                "price_change_5m_pct": t.price_change_5m_pct,
                                "url": t.url,
                            } for t in tokens
                        ]})
                    except Exception as exc:  # noqa: BLE001
                        logger.debug(f"dex universe cb error: {exc}")
                logger.info(f"DEX discovery: tracking {len(tokens)} tokens")
            except asyncio.CancelledError:
                break
            except Exception as exc:  # noqa: BLE001
                logger.exception(f"DEX discovery loop error: {exc}")
            try:
                await asyncio.sleep(interval)
            except asyncio.CancelledError:
                break

    async def _dex_snipe_loop(self) -> None:
        interval = float(self.s.dex.get("price_poll_interval_seconds", 30))
        while not self.stop_event.is_set():
            try:
                async with self.dex_lock:
                    tokens = list(self.dex_tokens.values())
                if tokens:
                    updated = await self.dex_mgr.poll_prices(tokens)
                    async with self.dex_lock:
                        for key, t in updated.items():
                            if key in self.dex_tokens:
                                self.dex_tokens[key] = t
                    for key, t in updated.items():
                        info = self.dex_infos.get(key)
                        if info is None:
                            continue
                        closed = self.risk.update_with_price(info.symbol, t.price_usd)
                        for pos in closed:
                            await self.storage.update_paper_close(pos)
                        await self._dex_maybe_evaluate(t, info)
            except asyncio.CancelledError:
                break
            except Exception as exc:  # noqa: BLE001
                logger.exception(f"DEX snipe loop error: {exc}")
            try:
                await asyncio.sleep(interval)
            except asyncio.CancelledError:
                break

    async def _dex_maybe_evaluate(self, token, info: SymbolInfo) -> None:
        """DEX tokens are ONLY tracked for meme hunter / price updates.
        They do NOT generate signals in the main signals tab.
        Signals tab = CEX futures only (top 100).
        """
        # DEX tokens only update prices for open positions, no new signals
        return

    # ---------------------------------------------------- meme hunter loop
    def _trading_window_open(self, module: str) -> bool:
        cfg = self.s.raw.get("trading_hours", {}) or {}
        if not bool(cfg.get("enabled", True)):
            return True
        modules = cfg.get("modules", {}) or {}
        if not bool(modules.get(module, True)):
            return True
        ranges = cfg.get("ranges_utc", ["07:00-10:00", "13:00-17:00"]) or []
        now = time.gmtime()
        now_minutes = now.tm_hour * 60 + now.tm_min
        for item in ranges:
            try:
                start, end = str(item).strip().split("-", 1)
                sh, sm = (int(x) for x in start.strip().split(":", 1))
                eh, em = (int(x) for x in end.strip().split(":", 1))
                a = sh * 60 + sm
                b = eh * 60 + em
                if a == b:
                    continue
                if (a < b and a <= now_minutes < b) or (a > b and (now_minutes >= a or now_minutes < b)):
                    return True
            except Exception:
                logger.warning(f"Invalid trading-hours range ignored: {item!r}")
        return False

    def _allow_new_action(self, module: str) -> bool:
        if self._trading_window_open(module):
            return True
        logger.info(f"{module} blocked by UTC trading-hours window")
        return False

    def _tehran_now(self):
        return time.gmtime(time.time() + 12600)

    def _trading_window_open(self, module: str) -> bool:
        cfg = self.s.raw.get("trading_hours", {}) or {}
        if not bool(cfg.get("enabled", True)):
            return True
        modules = cfg.get("modules", {}) or {}
        if not bool(modules.get(module, True)):
            return True
        ranges = cfg.get("ranges", cfg.get("ranges_utc", ["07:00-10:00", "13:00-17:00"])) or []
        now = self._tehran_now()
        now_minutes = now.tm_hour * 60 + now.tm_min
        for item in ranges:
            try:
                start, end = str(item).strip().split("-", 1)
                sh, sm = (int(x) for x in start.strip().split(":", 1))
                eh, em = (int(x) for x in end.strip().split(":", 1))
                a = sh * 60 + sm
                b = eh * 60 + em
                if a == b:
                    continue
                if (a < b and a <= now_minutes < b) or (a > b and (now_minutes >= a or now_minutes < b)):
                    return True
            except Exception:
                logger.warning(f"Invalid trading-hours range ignored: {item!r}")
        return False

    def _allow_new_action(self, module: str) -> bool:
        if self._trading_window_open(module):
            return True
        logger.info(f"{module} blocked by Tehran trading-hours window")
        return False

    async def _meme_hunter_loop(self) -> None:
        """Periodically run the 10-strategy meme hunter scan and push
        results to the dashboard."""
        interval = float(self.s.meme_hunter.get("scan_interval_seconds", 30))
        # اولین push فوری پس از start — داشبرد خالی نماند
        await asyncio.sleep(5)
        while not self.stop_event.is_set():
            try:
                if self.dex_mgr is not None and self.meme_hunter is not None:
                    results = await self.meme_hunter.scan(self.dex_mgr)
                    self.meme_hunter_results = results
                    summary = self.meme_hunter.get_summary()
                    daily_picks = self.meme_hunter.get_daily_picks(limit=5)
                    # push always — even if empty (so dashboard doesn't stay blank)
                    self._emit_dashboard({
                        "type": "meme_hunter",
                        "data": {
                            "summary": summary,
                            "hits": results,
                            "daily_picks": daily_picks,
                        },
                    })
                    if summary.get("total_unique", 0) > 0:
                        strat_counts = summary.get("by_strategy", {})
                        logger.info(
                            f"MemeHunter scan: {summary['total_unique']} opportunities "
                            f"({', '.join(f'{k}={v}' for k, v in strat_counts.items() if v)})"
                        )
                    else:
                        logger.debug("MemeHunter scan: 0 opportunities (DEX data may be loading)")
                else:
                    # پوش خالی تا داشبرد بدوند بداند scan آماده نیست
                    self._emit_dashboard({
                        "type": "meme_hunter",
                        "data": {
                            "summary": {"total_unique": 0, "by_strategy": {}},
                            "hits": {},
                            "daily_picks": [],
                            "status": "initializing",
                        },
                    })
            except asyncio.CancelledError:
                break
            except Exception as exc:  # noqa: BLE001
                logger.exception(f"MemeHunter loop error: {exc}")
                # push error state to dashboard
                self._emit_dashboard({
                    "type": "meme_hunter",
                    "data": {
                        "summary": {"total_unique": 0, "by_strategy": {}},
                        "hits": {},
                        "daily_picks": [],
                        "status": "error",
                        "error": str(exc),
                    },
                })
            try:
                await asyncio.sleep(interval)
            except asyncio.CancelledError:
                break

    async def _live_price_loop(self) -> None:
        """Fetch live prices every 10 seconds for open positions + push to dashboard."""
        while not self.stop_event.is_set():
            try:
                open_pos = self.risk.open_positions_list()
                prices: Dict[str, float] = {}
                for pos in open_pos:
                    if pos.symbol not in prices:
                        try:
                            ticker = await self.em.fetch_ticker(pos.exchange, pos.symbol)
                            if ticker and "last" in ticker and ticker["last"] is not None:
                                prices[pos.symbol] = float(ticker["last"])
                        except Exception as exc:
                            logger.debug(f"fetch_ticker failed for {pos.exchange} {pos.symbol}: {exc}")
                for pos in open_pos:
                    price = prices.get(pos.symbol)
                    if price and price > 0:
                        closed = self.risk.update_with_price(pos.symbol, price)
                        for c in closed:
                            await self.storage.update_paper_close(c)
                            self._emit_dashboard({
                                "type": "signal_opened",
                                "signal": c.to_dict(),
                                "position": c.to_dict(),
                            })
                if prices:
                    self._emit_dashboard({"type": "live_prices", "data": prices})
                    logger.debug(f"Live prices pushed: {prices}")
                elif open_pos:
                    logger.warning(f"Live price loop: {len(open_pos)} open positions but 0 prices fetched")
            except asyncio.CancelledError:
                break
            except Exception as exc:  # noqa: BLE001
                logger.debug(f"LivePrice loop error: {exc}")
            try:
                await asyncio.sleep(10)
            except asyncio.CancelledError:
                break

    async def _price_checker_loop(self) -> None:
        interval = 120
        while not self.stop_event.is_set():
            try:
                if self.dex_mgr is not None:
                    checker = get_price_checker()
                    await checker.check_pending(self.dex_mgr)
            except asyncio.CancelledError:
                break
            except Exception as exc:  # noqa: BLE001
                logger.debug(f"PriceChecker loop error: {exc}")
            try:
                await asyncio.sleep(interval)
            except asyncio.CancelledError:
                break

    # ---------------------------------------------------- signal poll loop (REST fallback)
    async def _signal_poll_loop(self) -> None:
        """REST-based polling for main signals as WS fallback.
        
        Evaluates top futures symbols every 5 minutes using the ConfluenceEngine.
        This ensures signals are generated even if WS connection fails.
        """
        await asyncio.sleep(60)  # Wait for exchange to be ready
        poll_interval = float(self.s.forward.get("poll_interval_seconds", 300))

        while not self.stop_event.is_set():
            try:
                em = self.em
                if not em.clients:
                    await asyncio.sleep(30)
                    continue

                # Get symbols from universe (CEX futures only)
                all_symbols: List[SymbolInfo] = []
                for ex_name, sym_list in self.universes.items():
                    for info in sym_list:
                        all_symbols.append(info)
                        if len(all_symbols) >= 100:
                            break
                    if len(all_symbols) >= 100:
                        break

                if not all_symbols:
                    logger.warning("Signal poll: no symbols in universe — retrying in 60s")
                    await asyncio.sleep(60)
                    continue

                evaluated = 0
                signals_found = 0
                for info in all_symbols[:50]:  # Evaluate top 50 per cycle
                    if self.stop_event.is_set():
                        break
                    now = time.time()
                    if now - self._signal_cooldowns.get(info.symbol, 0) < self._signal_cooldown_seconds:
                        continue

                    evaluated += 1
                    # Fetch candles for all configured TFs
                    for tf in self.s.timeframes:
                        cached = self.store.get("binance", info.symbol, tf)
                        if len(cached) < 30:
                            try:
                                fresh = await em.fetch_ohlcv("binance", info.symbol, tf, limit=200)
                                for c in fresh:
                                    self.store.push("binance", info.symbol, tf, c)
                            except Exception:
                                pass

                    try:
                        sig = await self.confluence.evaluate_symbol("binance", info)
                    except Exception as exc:
                        logger.debug(f"Signal poll error {info.symbol}: {exc}")
                        continue

                    if sig is None:
                        continue

                    # Duplicate-signal guard — see _maybe_evaluate for full
                    # explanation of the bug this fixes.
                    if self.risk.has_open_position_for_symbol(info.symbol):
                        continue
                    if await self.storage.has_unresolved_signal_for_symbol(info.symbol):
                        continue
                    if not self._allow_new_action("signals"):
                        continue

                    await self.storage.save_signal(sig)
                    self._signal_cooldowns[info.symbol] = time.time()
                    signals_found += 1
                    logger.info(f"SIGNAL (poll) {sig.exchange} {sig.symbol} {sig.side.value} score={sig.score:.2f}")

                    live_price = None
                    try:
                        ticker = await em.fetch_ticker(sig.exchange, sig.symbol)
                        if ticker and ticker.get("last"):
                            live_price = float(ticker["last"])
                    except Exception:
                        pass

                    pos = self.risk.open_from_signal(sig, live_price=live_price)
                    if pos is not None:
                        await self.storage.open_paper(pos, signal_id=sig.id)
                        self._emit_dashboard({"type": "signal_opened", "signal": sig.to_dict(), "position": pos.to_dict()})
                    else:
                        await self.storage.update_signal_status(sig.id, "no_position")

                    if self.notify is not None:
                        try:
                            await self.notify.send_signal(sig)
                        except Exception:
                            pass

                    await asyncio.sleep(0.2)

                if evaluated > 0:
                    logger.info(f"Signal poll cycle: evaluated={evaluated}, signals={signals_found}")

            except asyncio.CancelledError:
                break
            except Exception as exc:
                logger.exception(f"Signal poll loop error: {exc}")
            try:
                await asyncio.sleep(poll_interval)
            except asyncio.CancelledError:
                break

    # ---------------------------------------------------- scalping loop
    async def _scalp_loop(self) -> None:
        """Periodically evaluate top 200 coins for scalp signals (spot + futures)."""
        scalp_cfg = self.s.scalping
        interval = float(scalp_cfg.get("evaluation_interval_seconds", 45))
        top_n = int(scalp_cfg.get("top_volume_limit", 200))
        cooldown = float(scalp_cfg.get("signal_cooldown_seconds", 180))
        include_spot = bool(scalp_cfg.get("include_spot", True))
        include_futures = bool(scalp_cfg.get("include_futures", True))

        # Wait for exchange to be ready
        await asyncio.sleep(15)

        while not self.stop_event.is_set():
            try:
                em = self.em
                if not em.clients:
                    try:
                        await em.start()
                    except Exception as exc:
                        logger.warning(f"Scalp: exchange start failed: {exc}")
                        await asyncio.sleep(30)
                        continue

                # Collect both spot and futures top-volume symbols
                all_symbols: List[SymbolInfo] = []
                seen_syms: Set[str] = set()

                if include_futures:
                    try:
                        fut_symbols = await self._get_top_volume_symbols("binance", top_n, futures=True)
                        for s in fut_symbols:
                            if s.symbol not in seen_syms:
                                all_symbols.append(s)
                                seen_syms.add(s.symbol)
                    except Exception as exc:
                        logger.warning(f"Scalp: futures symbols fetch failed: {exc}")

                if include_spot:
                    try:
                        spot_symbols = await self._get_top_volume_symbols("binance", top_n, futures=False)
                        for s in spot_symbols:
                            if s.symbol not in seen_syms:
                                all_symbols.append(s)
                                seen_syms.add(s.symbol)
                    except Exception as exc:
                        logger.warning(f"Scalp: spot symbols fetch failed: {exc}")

                if not all_symbols:
                    logger.warning("Scalp: no symbols found — retrying in 30s")
                    await asyncio.sleep(30)
                    continue

                logger.info(f"Scalp loop: evaluating {len(all_symbols)} symbols (spot+futures)")

                scalp_evaluated = 0
                scalp_signals = 0
                for info in all_symbols:
                    if self.stop_event.is_set():
                        break
                    now = time.time()
                    if now - self._scalp_cooldowns.get(info.symbol, 0) < cooldown:
                        continue

                    scalp_evaluated += 1

                    # Feed candles for trigger TF + HTF
                    scalp_tfs = set(self.scalp_engine.timeframes)
                    scalp_tfs.update(self.scalp_engine.htf_timeframes)
                    for tf in scalp_tfs:
                        cached = self.store.get("binance", info.symbol, tf)
                        if len(cached) < 10:
                            try:
                                fresh = await em.fetch_ohlcv("binance", info.symbol, tf, limit=100)
                                for c in fresh:
                                    self.store.push("binance", info.symbol, tf, c)
                            except Exception:
                                pass

                    try:
                        sig = await self.scalp_engine.evaluate_symbol("binance", info)
                    except Exception as exc:
                        logger.debug(f"scalp evaluate error {info.symbol}: {exc}")
                        continue

                    if sig is None:
                        continue

                    # Duplicate-signal guard — see _maybe_evaluate for full
                    # explanation of the bug this fixes.
                    if self.risk.has_open_position_for_symbol(info.symbol):
                        continue
                    if await self.storage.has_unresolved_signal_for_symbol(info.symbol, like_prefix="SCP_%"):
                        continue
                    if not self._allow_new_action("scalping"):
                        continue

                    await self.storage.save_signal(sig)
                    self._scalp_cooldowns[info.symbol] = time.time()
                    scalp_signals += 1
                    logger.info(
                        f"SCALP SIGNAL {sig.exchange} {sig.symbol} {sig.side.value} "
                        f"score={sig.score:.2f} -> {sig.rationale}"
                    )

                    # Fetch a fresh live price right before opening — sig.entry
                    # is the last CLOSED candle's close, which can already be
                    # stale/several seconds old by the time we get here. The
                    # slippage guard in open_from_signal will reject the trade
                    # if this live price has already gapped too far.
                    live_price = None
                    try:
                        ticker = await em.fetch_ticker(sig.exchange, sig.symbol)
                        if ticker and ticker.get("last"):
                            live_price = float(ticker["last"])
                    except Exception:
                        pass

                    pos = self.risk.open_from_signal(sig, live_price=live_price)
                    if pos is not None:
                        await self.storage.open_paper(pos, signal_id=sig.id)
                        self._emit_dashboard({"type": "signal_opened", "signal": sig.to_dict(), "position": pos.to_dict()})
                    else:
                        await self.storage.update_signal_status(sig.id, "no_position")

                    if self.notify is not None:
                        try:
                            await self.notify.send_signal(sig)
                        except Exception as exc:
                            logger.warning(f"telegram send failed: {exc}")

                    for cb in self._dashboard_callbacks:
                        try:
                            await cb({"type": "scalp_signal", "data": sig.to_dict()})
                        except Exception:
                            pass

                logger.info(f"Scalp cycle done: evaluated={scalp_evaluated}, signals={scalp_signals}")

            except asyncio.CancelledError:
                break
            except Exception as exc:
                logger.exception(f"Scalp loop error: {exc}")
            try:
                await asyncio.sleep(interval)
            except asyncio.CancelledError:
                break

    async def _get_top_volume_symbols(
        self, exchange: str, limit: int = 200, futures: bool = True
    ) -> List[SymbolInfo]:
        """Get top N USDT symbols by 24h volume.
        futures=True  → only symbols with perpetual futures (leverage)
        futures=False → spot symbols that do NOT have futures (complementary set)
        """
        from core.exchange import SymbolInfo
        try:
            if exchange not in self.em.clients:
                await self.em.start()
            if exchange not in self.em.clients:
                return []
            client = self.em.clients[exchange]

            # Get futures symbols set (spot-format: "BTC/USDT")
            futures_set = self.em.get_futures_symbols(exchange)

            # Fetch tickers — may return futures format (BTC/USDT:USDT) or spot format
            tickers = await client.fetch_tickers()
            usdt_pairs = []
            for sym, ticker in tickers.items():
                # Normalize symbol: accept both "BTC/USDT" and "BTC/USDT:USDT"
                base = ""
                if sym.endswith("/USDT"):
                    base = sym.split("/")[0]
                elif sym.endswith("/USDT:USDT"):
                    # Futures format — convert to spot format for consistency
                    base = sym.split("/")[0]
                    sym = f"{base}/USDT"
                else:
                    continue

                # Skip stablecoins
                if base in ("USDT", "USDC", "BUSD", "DAI", "TUSD", "FDUSD"):
                    continue

                has_fut = sym in futures_set or f"{base}/USDT" in futures_set
                if futures and not has_fut:
                    continue
                if not futures and has_fut:
                    continue

                vol = ticker.get("quoteVolume", 0) or 0
                if vol <= 0:
                    continue
                usdt_pairs.append((sym, vol, has_fut))

            usdt_pairs.sort(key=lambda x: x[1], reverse=True)
            result = []
            seen = set()
            for sym, vol, has_fut in usdt_pairs[:limit]:
                if sym in seen:
                    continue
                seen.add(sym)
                base = sym.split("/")[0]
                result.append(SymbolInfo(
                    symbol=sym, base=base, quote="USDT",
                    listed_at=None, has_futures=has_fut,
                ))
            return result
        except Exception as exc:
            logger.warning(f"top volume fetch error (futures={futures}): {exc}")
            return []

    # ---------------------------------------------------- dashboard plumbing
    def register_dashboard(
        self,
        cb_signals: Callable[[dict], Awaitable[None]],
        cb_universe: Callable[[dict], Awaitable[None]],
    ) -> None:
        self._dashboard_callbacks.append(cb_signals)
        self._universe_callbacks.append(cb_universe)

    def _emit_dashboard(self, event: dict) -> None:
        for cb in self._dashboard_callbacks:
            asyncio.create_task(cb(event))

    # ═══════════════════════════════════════════════════════════════════════════
    # LIT Engine Loop — Liquidity Inducement Theorem
    # ═══════════════════════════════════════════════════════════════════════════
    async def _lit_loop(self) -> None:
        from strategies.lit_engine import LITSignal

        lit_cfg = self.s.raw.get("lit", {})
        cooldown_sec = int(lit_cfg.get("cooldown_seconds", 1800))
        eval_interval = int(lit_cfg.get("evaluation_interval_seconds", 300))
        min_score = float(lit_cfg.get("min_score", 0.55))
        max_open = int(lit_cfg.get("max_open_positions", 3))
        lit_tfs = lit_cfg.get("timeframes", ["5m", "15m", "1h", "4h"])
        trigger_tf = lit_cfg.get("trigger_timeframe", "15m")
        htf_tf = lit_cfg.get("htf_timeframe", "1h")

        # Wait for exchange connection
        await asyncio.sleep(30)
        while not self.stop_event.is_set():
            try:
                # FIX: Get symbols from the ACTUAL universe keys (binance/bybit)
                all_symbols: List[str] = []
                for ex_name, sym_list in self.universes.items():
                    for info in sym_list:
                        if info.symbol not in all_symbols:
                            all_symbols.append(info.symbol)

                # If universes empty, try fetching top volume directly
                if not all_symbols:
                    try:
                        top_syms = await self._get_top_volume_symbols("binance", 30, futures=True)
                        all_symbols = [s.symbol for s in top_syms]
                    except Exception:
                        pass

                # Add special symbols
                special = lit_cfg.get("special_symbols", [])
                for sym in special:
                    if sym not in all_symbols:
                        all_symbols.append(sym)

                limit = int(lit_cfg.get("universe_size", 20))
                lit_symbols = all_symbols[:limit]

                if not lit_symbols:
                    logger.warning("LIT loop: no symbols in universe — retrying in 60s")
                    await asyncio.sleep(60)
                    continue

                logger.info(f"LIT loop: evaluating {len(lit_symbols)} symbols")

                signals: List[LITSignal] = []
                for sym in lit_symbols:
                    if self.stop_event.is_set():
                        break
                    last_time = self._lit_cooldowns.get(sym, 0)
                    if (time.time() - last_time) < cooldown_sec:
                        continue
                    try:
                        # Fetch trigger TF candles
                        try:
                            fresh = await self.em.fetch_ohlcv("binance", sym, trigger_tf, limit=200)
                            for c in fresh:
                                self.store.push("binance", sym, trigger_tf, c)
                        except Exception:
                            pass
                        cached = self.store.get("binance", sym, trigger_tf)
                        if not cached or len(cached) < 30:
                            continue
                        cand_df = pd.DataFrame([{"open": c.open, "high": c.high, "low": c.low, "close": c.close, "volume": c.volume, "timestamp": c.timestamp} for c in cached])

                        # Fetch HTF candles for bias
                        try:
                            fresh_htf = await self.em.fetch_ohlcv("binance", sym, htf_tf, limit=100)
                            for c in fresh_htf:
                                self.store.push("binance", sym, htf_tf, c)
                        except Exception:
                            pass
                        cached_htf = self.store.get("binance", sym, htf_tf)
                        htf_df = None
                        if cached_htf and len(cached_htf) >= 20:
                            htf_df = pd.DataFrame([{"open": c.open, "high": c.high, "low": c.low, "close": c.close, "volume": c.volume, "timestamp": c.timestamp} for c in cached_htf])

                        signal = self.lit_engine.analyze(cand_df, sym, "binance", htf_df)
                        if signal and signal.score >= min_score:
                            signals.append(signal)
                            self._lit_cooldowns[sym] = time.time()
                            logger.info(f"LIT candidate: {sym} score={signal.score:.2f} setup={signal.strategy}")
                        elif signal:
                            logger.debug(f"LIT {sym}: score {signal.score:.2f} below min {min_score}")
                    except Exception as e:
                        logger.warning(f"LIT loop error {sym}: {e}")
                    await asyncio.sleep(0.2)

                signals.sort(key=lambda s: s.score, reverse=True)
                selected = signals[:max_open]
                for signal in selected:
                    await self._handle_lit_signal(signal)
                if selected:
                    logger.info(f"LIT loop: {len(selected)} signals selected from {len(lit_symbols)} symbols")
                else:
                    logger.debug(f"LIT loop: 0 signals from {len(lit_symbols)} symbols")
            except Exception as e:
                logger.error(f"LIT loop error: {e}")
            await asyncio.sleep(eval_interval)

    async def _handle_lit_signal(self, signal) -> None:
        """Save LIT signal to DB and emit to dashboard."""
        storage: Storage = self.storage
        risk: RiskEngine = self.risk
        from config.settings import get_settings
        from core.models import Side
        s = get_settings()
        try:
            last_signal = await storage.get_last_signal(signal.symbol, minutes=15)
            if last_signal:
                return
            # Duplicate-signal guard — see _maybe_evaluate for full
            # explanation. LIT's 15-min get_last_signal check above only
            # covers a short window; also check for any still-unresolved
            # LIT signal on this symbol regardless of age, and skip if a
            # position is already open for it.
            if risk.has_open_position_for_symbol(signal.symbol):
                return
            if await storage.has_unresolved_signal_for_symbol(signal.symbol, like_prefix="LIT_%"):
                return
            if not self._allow_new_action("lit"):
                return

            entry = signal.entry
            sl = signal.stop_loss
            tp = signal.take_profit
            tp2 = signal.take_profit_2
            risk_pct = float(s.risk.get("risk_per_trade_pct", 1.0))
            balance = risk.equity
            sl_distance = abs(entry - sl)
            if sl_distance < 1e-10:
                return

            # Convert side string to Side enum
            side_enum = Side.LONG if signal.side == "long" else Side.SHORT

            signal_record = Signal(
                id=signal.id,
                created_at=time.time(),
                exchange="binance",
                symbol=signal.symbol,
                side=side_enum,
                price=entry,
                score=signal.score,
                hits=[],
                confluence_tf_breakdown={},
                entry=entry,
                stop_loss=sl,
                take_profit=tp,
                trailing_atr=0.0,
                atr=signal.debug.get("atr", 0) if hasattr(signal, 'debug') and signal.debug else 0,
                position_size_usdt=balance * risk_pct / 100,
                risk_pct=risk_pct,
                rationale=signal.reasoning,
                base=signal.symbol.split("/")[0] if "/" in signal.symbol else signal.symbol,
                tp2=tp2,
                tp3=getattr(signal, 'take_profit_3', None) or (tp2 * 1.3 if tp2 else None),
            )
            await storage.save_signal(signal_record)

            # Emit to dashboard with full LIT data (chart_annotations, etc.)
            lit_data = signal.to_dict() if hasattr(signal, 'to_dict') else {"id": signal.id}
            self._emit_dashboard({"type": "lit_signal", "signal": lit_data})

            # LIT's ideal_entry is computed from historical candle analysis
            # (can be several seconds/minutes old by the time we get here) —
            # fetch a fresh live price so the slippage guard can catch it if
            # the market has already moved past the signal's entry/SL.
            live_price = None
            try:
                ticker = await self.em.fetch_ticker("binance", signal.symbol)
                if ticker and ticker.get("last"):
                    live_price = float(ticker["last"])
            except Exception:
                pass

            pos = risk.open_from_signal(signal_record, live_price=live_price)
            if pos:
                self._emit_dashboard({"type": "open", "position": pos.to_dict()})
            else:
                await storage.update_signal_status(signal_record.id, "no_position")

            logger.info(
                f"LIT {signal.side.upper()} {signal.symbol} @ {entry:.6g} "
                f"SL={sl:.6g} TP1={tp:.6g} TP2={tp2:.6g} "
                f"[{signal.strategy}] score={signal.score:.2f}"
            )
        except Exception as e:
            logger.error(f"LIT handle signal error: {e}")


async def run_forever() -> None:
    """Entrypoint used by dashboard + systemd."""
    from config.settings import reload_settings
    from core.storage import get_storage
    settings = reload_settings()
    em = get_exchange_manager()
    risk = RiskEngine(settings)
    storage = get_storage()
    await storage.connect()
    notify = TelegramNotifier(settings) if settings.telegram.get("enabled") else None
    if notify is not None:
        await notify.start()
    eng = ForwardEngine(settings, em, risk, storage, notify)
    try:
        await eng.start()
        while not eng.stop_event.is_set():
            await asyncio.sleep(3600)
    finally:
        await eng.stop()
        if notify is not None:
            await notify.stop()
