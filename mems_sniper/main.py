"""Entry point: starts the ForwardEngine (always sniffing) and the
FastAPI dashboard together in one event loop, on a single process.

Usage:
    python -m mems_sniper.main            # from repo root
    python main.py                        # from inside mems_sniper/

Make sure to:
    cp .env.example .env  (and fill API keys)
"""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path

# allow running from either project root or inside mems_sniper/
ROOT = Path(__file__).resolve().parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import uvicorn

from config.settings import get_settings, reload_settings
from core.exchange import ExchangeManager, get_exchange_manager
from core.forward_engine import ForwardEngine
from core.logging_setup import setup_logging, logger
from core.models import now_sec
from core.risk import RiskEngine
from core.storage import get_storage
from core.forward_engine import ForwardEngine as FE  # noqa: F401  (alias clarity)
from notify.telegram_bot import TelegramNotifier
from server import create_app
import server as server_module


async def lifespan() -> None:
    settings = reload_settings()
    logger.info(f"=== MemeCoin Sniper v{settings.raw.get('project', {}).get('version', '1.0.0')} starting ===")

    # shared singletons
    em = get_exchange_manager()
    risk = RiskEngine(settings)
    storage = get_storage()

    # Build FastAPI app with shared singletons
    app = create_app(em=em, risk=risk, storage=storage)

    notify = TelegramNotifier(settings) if settings.telegram.get("enabled") else None
    if notify is not None:
        await notify.start()

    # Forward engine (always-on)
    eng = ForwardEngine(settings, em, risk, storage, notify)
    # Register dashboard callbacks so engine pushes events to UI/WS clients.
    async def push_signals(evt: dict):
        await server_module.broadcast(evt)
    async def push_universe(universe):
        await server_module.broadcast({"type": "universe", "data": universe})
        # Merge rather than overwrite: CEX refresh sends {exchange: [...]},
        # DEX discovery sends {"dex": [...]} — both must coexist.
        server_module.get_state().setdefault("universe", {}).update(universe)
    eng.register_dashboard(push_signals, push_universe)
    # Also keep app_state["universe"] updated for /api/state consumers.
    await eng.start()
    server_module.get_state()["forward"] = eng

    # Uvicorn server (in-process, no extra worker to keep single event loop)
    config = uvicorn.Config(
        app,
        host=settings.web.get("host", "0.0.0.0"),
        port=int(settings.web.get("port", 8080)),
        log_level="warning",
        access_log=False,
    )
    server = uvicorn.Server(config)

    logger.info(f"Dashboard listening on http://{config.host}:{config.port}")

    try:
        await server.serve()
    finally:
        await eng.stop()
        if storage._db is not None:
            await storage.close()
        if notify is not None:
            await notify.stop()


def main() -> None:
    setup_logging()
    try:
        asyncio.run(lifespan())
    except KeyboardInterrupt:
        logger.info("Shutdown requested by user.")


if __name__ == "__main__":
    main()
