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
from typing import Awaitable, Callable, Deque, Dict, List, Optional, Set

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
        self._signal_cooldown_seconds: float = float(settings.forward.get("signal_cooldown_seconds", 1800))  # 30 min default
        # News/Fundamentals tracker
        self.news_tracker: Optional[Any] = None
        self._news_score_cache: Dict[str, Any] = {}
        # Meme hunter (۴ استراتژی شکار میم‌کوین)
        self.meme_hunter_enabled = bool(settings.meme_hunter.get("enabled", True))
        self.meme_hunter: Optional[MemeHunter] = None
        self.meme_hunter_results: Dict[str, Any] = {}  # latest scan results

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
        # Prune ticks periodically
        self._tasks.append(asyncio.create_task(self._tick_prune_loop()))
        # DEX layer — poll Pump.fun / Raydium / PancakeSwap / Uniswap
        if self.dex_enabled:
            self.dex_mgr = get_dex_manager(self.s.dex)
            self._tasks.append(asyncio.create_task(self._dex_discovery_loop()))
            self._tasks.append(asyncio.create_task(self._dex_snipe_loop()))
            logger.info("DEX layer enabled (Pump.fun / Raydium / PancakeSwap / Uniswap)")
        # Meme Hunter — ۴ استراتژی شکار میم‌کوین
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
            logger.info("ScalpingEngine enabled: 10 strategies on high-volume coins")
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
        # Refresh news data if stale
        try:
            await self.news_tracker.refresh()
        except Exception:  # noqa: BLE001
            pass
        score_dict = self.news_tracker.get_dict()
        if not score_dict:
            return {}
        # Check if this symbol is trending
        is_trending = self.news_tracker.is_trending(symbol.split("/")[0] if "/" in symbol else symbol)
        result = {"fundamental_score": score_dict}
        if is_trending:
            result["fundamental_score"]["is_trending"] = True
        return result

    async def _scalp_candle_provider(self, exchange: str, symbol: str) -> Dict[str, List[Candle]]:
        """Candle provider for scalping engine — returns scalp TFs (1m, 5m) + HTF TFs (4h, 1d)."""
        out: Dict[str, List[Candle]] = {}
        scalp_tfs = set(self.s.scalping.get("timeframes", ["1m", "5m"]))
        htf_tfs = set(self.s.scalping.get("htf_timeframes", ["4h", "1d"]))
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
        # If memory empty (cold start), fetch via REST once.
        tfs = self.s.timeframes
        out: Dict[str, List[Candle]] = {}
        for tf in tfs:
            cached = self.store.get(exchange, symbol, tf)
            if len(cached) < 30:
                # bootstrap with REST
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
        """Candle source for synthetic `DEX:{chain}:{address}` symbols.

        DEX tokens have no WS kline stream, so we pull OHLCV via
        GeckoTerminal (through DEXManager) with a short local cache to
        avoid hammering the API every evaluation tick.
        """
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
            # DEX has no order-book depth in the CEX sense; skip that strategy.
            return {}
        return await self.em.fetch_order_book(exchange, symbol, limit=int(self.s.strategies.get("orderbook_imbalance", {}).get("depth_levels", 20)))

    async def _ws_loop(self, ex_name: str, symbols: List[str], tf: str) -> None:
        # Binance/Bybit handle their own reconnects inside ExchangeManager.
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
        # Cooldown: skip if this symbol had a signal recently
        now = time.time()
        if now - self._signal_cooldowns.get(symbol, 0) < self._signal_cooldown_seconds:
            return
        # find SymbolInfo from universe
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
        # Always update open positions on the new trigger close price.
        closed = self.risk.update_with_price(symbol, candle.close)
        for pos in closed:
            await self.storage.update_paper_close(pos)
            # Signal status is auto-updated in storage.update_paper_close via signal_id
        if sig is None:
            return
        await self.storage.save_signal(sig)
        self._signal_cooldowns[symbol] = time.time()  # register cooldown
        logger.info(f"SIGNAL {sig.exchange} {sig.symbol} {sig.side.value} score={sig.score:.2f} -> {sig.rationale}")
        # Try to open paper position.
        pos = self.risk.open_from_signal(sig)
        if pos is not None:
            await self.storage.open_paper(pos, signal_id=sig.id)
            self._emit_dashboard({"type": "signal_opened", "signal": sig.to_dict(), "position": pos.to_dict()})
        # notify via Telegram
        if self.notify is not None:
            try:
                await self.notify.send_signal(sig)
            except Exception as exc:  # noqa: BLE001
                logger.warning(f"telegram send failed: {exc}")
        # Dashboard push handled via callbacks registered below.
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
                # Restart WS streams? Symbol set rarely changes daily; we skip
                # re-subscribing for simplicity. New listings handled on next poll.
            except asyncio.CancelledError:
                break
            except Exception as exc:  # noqa: BLE001
                logger.exception(f"universe refresh: {exc}")

    async def _refresh_universes(self) -> None:
        try:
            async with self.universe_lock:
                self.universes = await build_universes(self.em, self.s, futures_only=True)
                self.last_universe_refresh = time.time()
                # Also write to dashboard feed for "watchlist" view.
                cbs = getattr(self, '_instance_universe_callbacks', ForwardEngine._universe_callbacks)
                for cb in cbs:
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
        """Periodically discover fresh meme coins on Pump.fun/Raydium/
        PancakeSwap/Uniswap and add them to the tracked-token watchlist."""
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
                cbs = getattr(self, '_instance_universe_callbacks', ForwardEngine._universe_callbacks)
                for cb in cbs:
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
        """Periodically refresh prices for tracked DEX tokens and run the
        confluence engine on each — same evaluation path as CEX symbols."""
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
                        # Feed the latest price into the risk engine for SL/TP/trailing.
                        info = self.dex_infos.get(key)
                        if info is None:
                            continue
                        closed = self.risk.update_with_price(info.symbol, t.price_usd)
                        for pos in closed:
                            await self.storage.update_paper_close(pos)
                            # Signal status auto-updated via signal_id
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
        # Cooldown: skip if this token had a signal recently
        now = time.time()
        token_key = f"{token.chain}:{token.address}"
        if now - self._signal_cooldowns.get(token_key, 0) < self._signal_cooldown_seconds:
            return
        # Price change filter: only evaluate DEX tokens with recent movement
        price_change = getattr(token, 'price_change_5m_pct', 0) or 0
        if abs(price_change) < 1.0:
            return  # skip completely flat tokens
        exchange = token.dex or "dex"
        try:
            sig = await self.confluence.evaluate_symbol(exchange, info)
        except Exception as exc:  # noqa: BLE001
            logger.exception(f"DEX evaluate error {exchange} {info.symbol}: {exc}")
            return
        if sig is None:
            return
        await self.storage.save_signal(sig)
        self._signal_cooldowns[token_key] = time.time()  # register cooldown
        logger.info(
            f"DEX SIGNAL {sig.exchange} {token.symbol} ({token.chain}) {sig.side.value} "
            f"score={sig.score:.2f} -> {sig.rationale}"
        )
        pos = self.risk.open_from_signal(sig)
        if pos is not None:
            await self.storage.open_paper(pos, signal_id=sig.id)
            self._emit_dashboard({"type": "signal_opened", "signal": sig.to_dict(), "position": pos.to_dict()})
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

    # ---------------------------------------------------- meme hunter loop
    async def _meme_hunter_loop(self) -> None:
        """Periodically run the 10-strategy meme hunter scan and push
        results to the dashboard."""
        interval = float(self.s.meme_hunter.get("scan_interval_seconds", 30))
        while not self.stop_event.is_set():
            try:
                if self.dex_mgr is not None and self.meme_hunter is not None:
                    results = await self.meme_hunter.scan(self.dex_mgr)
                    self.meme_hunter_results = results
                    # Push to dashboard
                    summary = self.meme_hunter.get_summary()
                    daily_picks = self.meme_hunter.get_daily_picks(limit=5)
                    self._emit_dashboard({
                        "type": "meme_hunter",
                        "data": {
                            "summary": summary,
                            "hits": results,
                            "daily_picks": daily_picks,
                        },
                    })
                    if summary["total_unique"] > 0:
                        strat_counts = summary.get("by_strategy", {})
                        logger.info(
                            f"MemeHunter scan: {summary['total_unique']} opportunities "
                            f"({', '.join(f'{k}={v}' for k, v in strat_counts.items() if v)})"
                        )
            except asyncio.CancelledError:
                break
            except Exception as exc:  # noqa: BLE001
                logger.exception(f"MemeHunter loop error: {exc}")
            try:
                await asyncio.sleep(interval)
            except asyncio.CancelledError:
                break

    async def _live_price_loop(self) -> None:
        """Fetch live prices every 10 seconds for open positions + push to dashboard."""
        while not self.stop_event.is_set():
            try:
                open_pos = self.risk.open_positions_list()
                # Fetch prices for all open position symbols
                prices: Dict[str, float] = {}
                for pos in open_pos:
                    if pos.symbol not in prices:
                        try:
                            ticker = await self.em.fetch_ticker(pos.exchange, pos.symbol)
                            if ticker and "last" in ticker and ticker["last"] is not None:
                                prices[pos.symbol] = float(ticker["last"])
                        except Exception as exc:
                            logger.debug(f"fetch_ticker failed for {pos.exchange} {pos.symbol}: {exc}")
                # Update positions with live prices + check SL/TP
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
                # Push live prices to dashboard (always, even if empty)
                if prices:
                    self._emit_dashboard({
                        "type": "live_prices",
                        "data": prices,
                    })
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
        """Periodically check prices of previously detected tokens
        to calculate success rate and win/loss stats."""
        interval = 120  # check every 2 minutes
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

    # ---------------------------------------------------- scalping loop
    async def _scalp_loop(self) -> None:
        """Periodically evaluate top-volume coins for scalp signals."""
        scalp_cfg = self.s.scalping
        interval = float(scalp_cfg.get("evaluation_interval_seconds", 60))
        top_n = int(scalp_cfg.get("top_volume_limit", 30))
        cooldown = float(scalp_cfg.get("signal_cooldown_seconds", 300))

        while not self.stop_event.is_set():
            try:
                # Get top-volume symbols from Binance
                em = self.em
                if not em.clients:
                    await asyncio.sleep(10)
                    continue

                # Fetch top volume symbols from Binance
                top_symbols = await self._get_top_volume_symbols("binance", top_n)
                logger.info(f"Scalp loop: evaluating {len(top_symbols)} symbols")

                scalp_evaluated = 0
                scalp_signals = 0
                for info in top_symbols:
                    now = time.time()
                    if now - self._scalp_cooldowns.get(info.symbol, 0) < cooldown:
                        continue

                    scalp_evaluated += 1

                    # Feed recent candles into memory store for the scalp engine
                    # Include scalp TFs + HTF trend filter TFs (4h, 1d)
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
                        logger.debug(f"scalp {info.symbol}: no signal (score below threshold or filters)")
                        continue

                    await self.storage.save_signal(sig)
                    self._scalp_cooldowns[info.symbol] = time.time()
                    scalp_signals += 1
                    logger.info(
                        f"SCALP SIGNAL {sig.exchange} {sig.symbol} {sig.side.value} "
                        f"score={sig.score:.2f} -> {sig.rationale}"
                    )

                    # Open paper position (respect scalp max_open_positions)
                    pos = self.risk.open_from_signal(sig)
                    if pos is not None:
                        await self.storage.open_paper(pos, signal_id=sig.id)
                        self._emit_dashboard({"type": "signal_opened", "signal": sig.to_dict(), "position": pos.to_dict()})

                    # Notify
                    if self.notify is not None:
                        try:
                            await self.notify.send_signal(sig)
                        except Exception as exc:
                            logger.warning(f"telegram send failed: {exc}")

                    # Dashboard push
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

    async def _get_top_volume_symbols(self, exchange: str, limit: int = 30):
        """Get top N USDT symbols by 24h volume from exchange, futures only."""
        from core.exchange import SymbolInfo
        try:
            if exchange not in self.em.clients:
                return []
            client = self.em.clients[exchange]
            # Get futures-enabled symbols set
            futures_set = self.em.get_futures_symbols(exchange)
            tickers = await client.fetch_tickers()
            # Filter USDT pairs with futures and sort by quote volume
            usdt_pairs = []
            for sym, ticker in tickers.items():
                if not sym.endswith("/USDT"):
                    continue
                if sym not in futures_set:
                    continue  # skip non-futures symbols
                vol = ticker.get("quoteVolume", 0) or 0
                if vol <= 0:
                    continue
                usdt_pairs.append((sym, vol, ticker))
            usdt_pairs.sort(key=lambda x: x[1], reverse=True)
            result = []
            for sym, vol, ticker in usdt_pairs[:limit]:
                base = sym.split("/")[0]
                result.append(SymbolInfo(
                    symbol=sym, base=base, quote="USDT",
                    listed_at=None, has_futures=True,
                ))
            return result
        except Exception as exc:
            logger.debug(f"top volume fetch error: {exc}")
            return []

    # ---------------------------------------------------- dashboard plumbing
    _dashboard_callbacks: List[Callable[[dict], Awaitable[None]]] = []
    _universe_callbacks: List[Callable[[dict], Awaitable[None]]] = []

    def __init_subclass__(cls, **kwargs):
        super().__init_subclass__(**kwargs)
        cls._dashboard_callbacks = []
        cls._universe_callbacks = []

    def register_dashboard(self, cb_signals: Callable[[dict], Awaitable[None]],
                           cb_universe: Callable[[dict], Awaitable[None]]) -> None:
        if not hasattr(self, '_instance_dashboard_callbacks'):
            self._instance_dashboard_callbacks = list(ForwardEngine._dashboard_callbacks)
            self._instance_universe_callbacks = list(ForwardEngine._universe_callbacks)
        self._instance_dashboard_callbacks.append(cb_signals)
        self._instance_universe_callbacks.append(cb_universe)

    def _emit_dashboard(self, event: dict) -> None:
        cbs = getattr(self, '_instance_dashboard_callbacks', ForwardEngine._dashboard_callbacks)
        for cb in cbs:
            asyncio.create_task(cb(event))

    # ═════════════════════════════════════════════════════════════════════════
    # LIT Engine Loop — Liquidity Inducement Theorem
    # ═════════════════════════════════════════════════════════════════════════
    async def _lit_loop(self) -> None:
        from strategies.lit_engine import LITSignal
        import random as _rnd

        lit_cfg = self.s.raw.get("lit", {})
        cooldown_sec = int(lit_cfg.get("cooldown_seconds", 1800))
        eval_interval = int(lit_cfg.get("evaluation_interval_seconds", 300))
        min_score = float(lit_cfg.get("min_score", 0.70))
        max_open = int(lit_cfg.get("max_open_positions", 3))
        lit_tfs = lit_cfg.get("timeframes", ["15m", "1h", "4h"])
        tf_seconds = {"1m": 60, "5m": 300, "15m": 900, "1h": 3600, "4h": 14400, "1d": 86400}
        wait_time = max(tf_seconds.get(lit_tfs[0], 900), 60)

        await asyncio.sleep(wait_time)
        while not self.stop_event.is_set():
            try:
                # Get LIT universe (top N crypto + special symbols)
                all_symbols = list(self.universes.get("scalp", self.universes.get("default", [])))
                special = lit_cfg.get("special_symbols", [])
                for sym in special:
                    if sym not in all_symbols:
                        all_symbols.append(sym)
                limit = lit_cfg.get("universe_size", 20)
                lit_symbols = all_symbols[:limit]

                signals: List[LITSignal] = []
                for sym in lit_symbols:
                    if self.stop_event.is_set():
                        break
                    last_time = self._lit_cooldowns.get(sym, 0)
                    if (time.time() - last_time) < cooldown_sec:
                        continue
                    try:
                        # Fetch candles via REST for trigger TF
                        trigger_tf = lit_tfs[0]
                        try:
                            fresh = await self.em.fetch_ohlcv("binance", sym, trigger_tf, limit=200)
                            for c in fresh:
                                self.store.push("binance", sym, trigger_tf, c)
                        except Exception:
                            pass
                        cached = self.store.get("binance", sym, trigger_tf)
                        if not cached or len(cached) < 30:
                            continue
                        # Convert to DataFrame
                        cand_df = pd.DataFrame([{"open": c.open, "high": c.high, "low": c.low, "close": c.close, "volume": c.volume, "timestamp": c.timestamp} for c in cached])
                        # HTF bias
                        htf = "1h" if trigger_tf in ("1m", "5m", "15m") else "4h"
                        try:
                            fresh_htf = await self.em.fetch_ohlcv("binance", sym, htf, limit=100)
                            for c in fresh_htf:
                                self.store.push("binance", sym, htf, c)
                        except Exception:
                            pass
                        cached_htf = self.store.get("binance", sym, htf)
                        htf_df = None
                        if cached_htf and len(cached_htf) >= 20:
                            htf_df = pd.DataFrame([{"open": c.open, "high": c.high, "low": c.low, "close": c.close, "volume": c.volume, "timestamp": c.timestamp} for c in cached_htf])
                        signal = self.lit_engine.analyze(cand_df, sym, "binance", htf_df)
                        if signal and signal.score >= min_score:
                            signals.append(signal)
                            self._lit_cooldowns[sym] = time.time()
                    except Exception as e:
                        logger.warning(f"LIT loop error {sym}: {e}")
                    await asyncio.sleep(0.1)

                signals.sort(key=lambda s: s.score, reverse=True)
                selected = signals[:max_open]
                for signal in selected:
                    await self._handle_lit_signal(signal)
                if selected:
                    logger.info(f"LIT loop: {len(selected)} signals selected from {len(lit_symbols)} symbols")
            except Exception as e:
                logger.error(f"LIT loop error: {e}")
            await asyncio.sleep(eval_interval)

    async def _handle_lit_signal(self, signal: LITSignal) -> None:
        storage: Storage = self.storage
        risk: RiskEngine = self.risk
        from config.settings import get_settings
        s = get_settings()
        try:
            last_signal = await storage.get_last_signal(signal.symbol, minutes=15)
            if last_signal:
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
            position_size = round((balance * risk_pct / 100) / sl_distance, 6)
            if position_size <= 0:
                return

            # Record signal
            signal_record = Signal(
                id=signal.id, symbol=signal.symbol, side=signal.side,
                strategy=signal.strategy, entry=entry, stop_loss=sl,
                take_profit=tp, score=signal.score, confidence=signal.score,
                reasoning=signal.reasoning, created_at=time.time(),
                expires_at=time.time() + 1800,
                hits=[], tf_breakdown={},
                tp2=tp2, exchange="binance", market_type="futures",
                position_size_usdt=balance * risk_pct / 100,
            )
            await storage.save_signal(signal_record)
            await self._emit({"type": "signal", "signal": signal_record.to_dict()})

            # Paper position
            pos = risk.open_from_signal(signal_record)
            if pos:
                await self._emit({"type": "open", "position": pos.to_dict()})

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
