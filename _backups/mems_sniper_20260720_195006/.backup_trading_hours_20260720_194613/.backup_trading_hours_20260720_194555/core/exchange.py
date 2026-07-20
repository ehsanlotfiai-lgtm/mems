"""Multi-exchange connectivity using ccxt (REST) + WebSocket streams.

The `ExchangeManager` keeps one ccxt client per enabled exchange and
provides normalized helpers:

  * fetch all USDT spot symbols
  * fetch historical OHLCV
  * fetch order book top levels
  * stream kline ticks via WebSocket (binance & bybit supported)

It is intentionally lightweight: caching and the heavy lifting happens
in the strategy / forward-trading modules, this just feeds data.
"""
from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Set, Callable, Awaitable

import ccxt.async_support as ccxt_async
import websockets

from config.settings import get_settings, Settings
from core.logging_setup import logger
from core.models import Candle


@dataclass
class SymbolInfo:
    symbol: str             # normalized CCXT symbol, e.g. "DOGE/USDT"
    base: str
    quote: str
    listed_at: Optional[int]   # unix ms when listing flagged (best-effort)
    has_futures: bool = False  # True if this symbol has USDT-M perpetual futures


class ExchangeManager:
    """Unified multi-exchange REST + WS access (async)."""

    def __init__(self, settings: Optional[Settings] = None) -> None:
        self.s = settings or get_settings()
        self.clients: Dict[str, ccxt_async.Exchange] = {}
        self._ws_tasks: List[asyncio.Task] = []

    # ---------------------------------------------------- lifecycle
    async def start(self) -> None:
        for name, conf in self.s.exchanges.items():
            if not conf.get("enable", True):
                continue
            try:
                client = self._build_client(name, conf)
                await client.load_markets()
                self.clients[name] = client
                logger.info(f"Connected to exchange: {name} ({len(client.symbols)} symbols)")
            except Exception as exc:  # noqa: BLE001
                logger.error(f"Failed to connect to {name}: {exc}")

    async def stop(self) -> None:
        for task in self._ws_tasks:
            task.cancel()
        for name, client in self.clients.items():
            try:
                await client.close()
                logger.info(f"Closed exchange client: {name}")
            except Exception as exc:  # noqa: BLE001
                logger.warning(f"Error closing {name}: {exc}")
        self._ws_tasks.clear()

    # ---------------------------------------------------- client factory
    def _build_client(self, name: str, conf: Dict[str, Any]) -> ccxt_async.Exchange:
        opts = conf.get("options", {})
        if name == "binance":
            keys = self.s.binance_keys
            client = ccxt_async.binance({
                "apiKey": keys["apiKey"] or None,
                "secret": keys["secret"] or None,
                "enableRateLimit": conf.get("rate_limit", True),
                "options": opts,
            })
            if conf.get("sandbox"):
                client.set_sandbox_mode(True)
            return client
        if name == "bybit":
            keys = self.s.bybit_keys
            client = ccxt_async.bybit({
                "apiKey": keys["apiKey"] or None,
                "secret": keys["secret"] or None,
                "enableRateLimit": conf.get("rate_limit", True),
                "options": opts,
            })
            if conf.get("sandbox"):
                client.set_sandbox_mode(True)
            return client
        raise ValueError(f"Unsupported exchange: {name}")

    # ---------------------------------------------------- REST: symbols
    async def list_usdt_spot_symbols(self, exchange: str) -> List[SymbolInfo]:
        """Return all USDT-quoted spot symbols for the given exchange."""
        client = self.clients[exchange]
        infos: List[SymbolInfo] = []
        # Pre-compute futures set for this exchange
        futures_set = self.get_futures_symbols(exchange)
        for sym in client.symbols:
            market = client.market(sym)
            if not market.get("spot", True):
                continue
            base = market.get("base")
            quote = market.get("quote")
            if quote != "USDT":
                continue
            listed = market.get("info", {}).get("onboardDate") \
                or market.get("info", {}).get("listingDate")
            try:
                listed_int = int(listed) if listed else None
            except (TypeError, ValueError):
                listed_int = None
            has_futures = sym in futures_set
            infos.append(SymbolInfo(symbol=sym, base=base, quote=quote,
                                    listed_at=listed_int, has_futures=has_futures))
        return infos

    async def fetch_top_volume_symbols(
        self, exchange: str, limit: int = 80
    ) -> List[SymbolInfo]:
        """Top N USDT spot symbols by 24h quote volume."""
        client = self.clients[exchange]
        try:
            tickers = await client.fetch_tickers()
        except Exception as exc:  # noqa: BLE001
            logger.error(f"{exchange}: fetch_tickers failed: {exc}")
            return []
        rows = []
        for sym, tkr in tickers.items():
            try:
                market = client.market(sym)
            except Exception:  # noqa: BLE001
                continue
            if not (market.get("spot", True) and market.get("quote") == "USDT"):
                continue
            qv = tkr.get("quoteVolume") or 0
            rows.append((sym, qv))
        rows.sort(key=lambda x: x[1], reverse=True)
        top = [self._symbol_info_from(client, s) for s, _ in rows[:limit]]
        return [t for t in top if t is not None]

    def _symbol_info_from(self, client: ccxt_async.Exchange, sym: str) -> Optional[SymbolInfo]:
        try:
            market = client.market(sym)
        except Exception:  # noqa: BLE001
            return None
        listed = market.get("info", {}).get("onboardDate") \
            or market.get("info", {}).get("listingDate")
        try:
            listed_int = int(listed) if listed else None
        except (TypeError, ValueError):
            listed_int = None
        has_futures = self._has_futures(client, sym)
        return SymbolInfo(
            symbol=sym, base=market.get("base"), quote=market.get("quote"),
            listed_at=listed_int, has_futures=has_futures,
        )

    def _has_futures(self, client: ccxt_async.Exchange, spot_sym: str) -> bool:
        """Check if a spot symbol has a corresponding USDT-M perpetual futures market."""
        try:
            market = client.market(spot_sym)
            base = market.get("base", "")
            if not base:
                return False
            # Binance: BTC/USDT:USDT  — Bybit: BTC/USDT:USDT
            swap_sym = f"{base}/USDT:USDT"
            swap_market = client.markets.get(swap_sym)
            if swap_market and swap_market.get("swap"):
                return True
            # Also check BTC/USDT:USDC pattern
            swap_sym2 = f"{base}/USDT"
            for mk in client.markets.values():
                if mk.get("base") == base and mk.get("quote") == "USDT" and mk.get("swap"):
                    return True
            return False
        except Exception:  # noqa: BLE001
            return False

    def get_futures_symbols(self, exchange: str) -> Set[str]:
        """Return set of spot symbols (e.g. 'BTC/USDT') that have perpetual futures."""
        client = self.clients.get(exchange)
        if not client:
            return set()
        futures_set: set = set()
        for mk in client.markets.values():
            if mk.get("swap") and mk.get("quote") == "USDT":
                base = mk.get("base", "")
                spot_sym = f"{base}/USDT"
                futures_set.add(spot_sym)
        return futures_set

    # ---------------------------------------------------- REST: OHLCV
    async def fetch_ohlcv(
        self, exchange: str, symbol: str, timeframe: str, limit: int = 500
    ) -> List[Candle]:
        """Fetch historical candles (normalized to our Candle model)."""
        if exchange not in self.clients:
            logger.warning(f"Exchange '{exchange}' not connected — no candle data")
            return []
        client = self.clients[exchange]
        try:
            raw = await client.fetch_ohlcv(symbol, timeframe=timeframe, limit=limit)
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"{exchange} {symbol} {timeframe} OHLCV error: {exc}")
            return []
        return [Candle(ts, o, h, l, c, v) for ts, o, h, l, c, v in raw]

    async def fetch_multi_tf_ohlcv(
        self,
        exchange: str,
        symbol: str,
        timeframes: List[str],
        limit: int = 500,
    ) -> Dict[str, List[Candle]]:
        """Fetch candles for multiple timeframes concurrently."""
        tasks = [self.fetch_ohlcv(exchange, symbol, tf, limit) for tf in timeframes]
        results = await asyncio.gather(*tasks, return_exceptions=True)
        out: Dict[str, List[Candle]] = {}
        for tf, res in zip(timeframes, results):
            if isinstance(res, Exception):
                out[tf] = []
            else:
                out[tf] = res
        return out

    # ---------------------------------------------------- REST: order book
    async def fetch_order_book(self, exchange: str, symbol: str, limit: int = 50) -> Dict[str, Any]:
        client = self.clients[exchange]
        try:
            ob = await client.fetch_order_book(symbol, limit=limit)
            return {
                "bids": ob.get("bids", [])[:limit],
                "asks": ob.get("asks", [])[:limit],
                "timestamp": ob.get("timestamp"),
                "nonce": ob.get("nonce"),
            }
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"{exchange} {symbol} orderbook error: {exc}")
            return {"bids": [], "asks": [], "timestamp": None, "nonce": None}

    async def fetch_ticker(self, exchange: str, symbol: str) -> Dict[str, Any]:
        client = self.clients[exchange]
        return await client.fetch_ticker(symbol)

    # ---------------------------------------------------- WebSocket stream
    async def stream_klines(
        self,
        exchange: str,
        symbols: List[str],
        timeframe: str,
        on_candle: Callable[[str, str, Candle], Awaitable[None]],
        stop_event: Optional[asyncio.Event] = None,
    ) -> None:
        """Stream closed klines via WebSocket, invoking callback on each closed candle."""
        if exchange == "binance":
            await self._stream_binance(symbols, timeframe, on_candle, stop_event)
        elif exchange == "bybit":
            await self._stream_bybit(symbols, timeframe, on_candle, stop_event)
        else:
            logger.error(f"WebSocket streaming not implemented for {exchange}")

    async def _stream_binance(
        self,
        symbols: List[str],
        timeframe: str,
        on_candle: Callable[[str, str, Candle], Awaitable[None]],
        stop_event: Optional[asyncio.Event],
    ) -> None:
        # Binance futures multi-stream: <symbol>@kline_<interval>
        streams = "/".join(
            f"{s.replace('/', '').lower()}@kline_{timeframe}" for s in symbols
        )
        url = f"wss://fstream.binance.com/stream?streams={streams}"
        while stop_event is None or not stop_event.is_set():
            try:
                async with websockets.connect(url, ping_interval=15) as ws:
                    logger.info(f"Binance WS connected: {len(symbols)} symbols / {timeframe}")
                    async for msg in ws:
                        if stop_event is not None and stop_event.is_set():
                            break
                        data = json.loads(msg)
                        payload = data.get("data") or data
                        k = payload.get("k")
                        if not k or not k.get("x"):  # only closed candle
                            continue
                        sym = payload.get("s")
                        symbol = self._binance_sym_to_ccxt(sym)
                        candle = Candle(
                            timestamp=int(k["t"]),
                            open=float(k["o"]),
                            high=float(k["h"]),
                            low=float(k["l"]),
                            close=float(k["c"]),
                            volume=float(k["v"]),
                        )
                        await on_candle(symbol, timeframe, candle)
            except Exception as exc:  # noqa: BLE001
                logger.warning(f"Binance WS disconnected: {exc}; reconnecting in 5s")
                await asyncio.sleep(5)

    async def _stream_bybit(
        self,
        symbols: List[str],
        timeframe: str,
        on_candle: Callable[[str, str, Candle], Awaitable[None]],
        stop_event: Optional[asyncio.Event],
    ) -> None:
        url = "wss://stream.bybit.com/v5/public/spot"
        while stop_event is None or not stop_event.is_set():
            try:
                async with websockets.connect(url, ping_interval=15) as ws:
                    args = [f"kline.{timeframe}.{s.replace('/', '')}" for s in symbols]
                    sub = {"op": "subscribe", "args": args}
                    await ws.send(json.dumps(sub))
                    logger.info(f"Bybit WS subscribed: {len(symbols)} symbols / {timeframe}")
                    async for msg in ws:
                        if stop_event is not None and stop_event.is_set():
                            break
                        data = json.loads(msg)
                        topic = data.get("topic", "")
                        if not topic.startswith("kline."):
                            continue
                        # topic: kline.<tf>.<SYMBOL>
                        _, tf, sym = topic.split(".")
                        symbol = self._ccxt_symbol(sym)
                        items = data.get("data", [])
                        for k in items:
                            if not k.get("confirm", False):
                                continue
                            candle = Candle(
                                timestamp=int(k["start"]),
                                open=float(k["open"]),
                                high=float(k["high"]),
                                low=float(k["low"]),
                                close=float(k["close"]),
                                volume=float(k["volume"]),
                            )
                            await on_candle(symbol, tf, candle)
            except Exception as exc:  # noqa: BLE001
                logger.warning(f"Bybit WS disconnected: {exc}; reconnecting in 5s")
                await asyncio.sleep(5)

    # ---------------------------------------------------- helpers
    @staticmethod
    def _binance_sym_to_ccxt(sym: str) -> str:
        # e.g. "DOGEUSDT" -> "DOGE/USDT"
        if sym.endswith("USDT"):
            return f"{sym[:-4]}/USDT"
        return sym

    @staticmethod
    def _ccxt_symbol(sym: str) -> str:
        if sym.endswith("USDT"):
            return f"{sym[:-4]}/USDT"
        return sym


# singleton --------------------------------------------------
_mgr: Optional[ExchangeManager] = None


def get_exchange_manager() -> ExchangeManager:
    global _mgr
    if _mgr is None:
        _mgr = ExchangeManager()
    return _mgr
