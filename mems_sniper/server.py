"""FastAPI dashboard backend.

Exposes:
  * GET  /                  -> dashboard HTML
  * GET  /api/state         -> JSON snapshot (signals, positions, risk)
  * GET  /api/signals       -> recent signals (DB)
  * GET  /api/trades        -> recent paper trades (DB)
  * POST /api/assistant     -> ask assistant, returns reply
  * GET  /api/chart/{ex}/{sym}/{tf}  -> simple OHLCV JSON for charts
  * POST /api/backtest      -> run a single-symbol backtest from web
  * WS   /ws                -> push live signals / position updates

The dashboard is intentionally RTL (Persian) and uses only vanilla JS
plus Chart.js from a CDN — no build step needed.
"""
from __future__ import annotations

import asyncio
import json
from typing import List, Optional

from fastapi import FastAPI, HTTPException, Query, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from config.settings import get_settings, PROJECT_ROOT
from core.exchange import ExchangeManager
from core.logging_setup import logger, setup_logging
from core.models import Signal
from core.risk import RiskEngine
from core.storage import Storage, get_storage
from assistant import Assistant
from backtest.engine import Backtester


# singletons (built lazily)
_app_state: dict = {}


def get_state() -> dict:
    return _app_state


# ==========================================================
# Strategy explanation builder — توضیح فارسی استراتژی‌ها
# ==========================================================

STRATEGY_FA = {
    "new_listing": {"name": "🆕 تازه‌لیست", "desc": "کوین تازه لیست شده با کندل اولیه قوی. فرصت ورود زودهنگام."},
    "volume_spike": {"name": "🔊 اسپایک حجم", "desc": "حجم معاملات به‌طور ناگهانی ۳ برابر میانگین افزایش یافته. نشان‌گر ورود پول جدید."},
    "orderbook_imbalance": {"name": "⚖️ عدم تعادل اردربوک", "desc": "فشار خرید یا فروش در دفتر سفارشات به‌طور نامتقارنی بالا است."},
    "liquidity_grab": {"name": "🌊 شکار نقدینگی", "desc": "قیمت به سطوح کلیدی نفوذ کرده و برگشته. نشانه‌ای از حرکت نهنگ‌ها."},
    "momentum_ignition": {"name": "🔥 احتراق مومنتوم", "desc": "دو کندل متوالی با بدنه بزرگ در یک جهت. شروع یک روند جدید."},
    "rsi_divergence": {"name": "📊 واگرایی RSI", "desc": "قیمت و RSI در خلاف جهت هم حرکت کرده‌اند. نشانه بالقوه بازگشت روند."},
    "bb_breakout": {"name": "🎯 شکست بولینگر", "desc": "قیمت از باندهای بولینگر خارج شده. نشانه‌ای از افزایش نوسان و شروع روند."},
    "funding_oi_spike": {"name": "💸 اسپایک funding/OI", "desc": "افزایش ناگهانی open interest + تغییر funding rate. نشانه از فیوچرز."},
    "social_momentum": {"name": "📱 مومنتوم سوشال", "desc": "افزایش ذکرها در شبکه‌های اجتماعی + مومنتوم قیمتی."},
    "ema_cross": {"name": "📈 تقاطع EMA", "desc": "EMA سریع از EMA کند عبور کرده. سیگنال following روند."},
    "adx_trend": {"name": "📊 روند ADX", "desc": "روند قوی تشخیص داده شده. ADX بالاتر از ۲۵ نشان‌دهنده روند قوی است."},
    "squeeze_momentum": {"name": "💎 Squeeze", "desc": "فشرده‌سازی بولینگر داخل کتلر و سپس آزادسازی. نشانه انفجار قیمتی."},
    "vwap": {"name": "📐 VWAP", "desc": "قیمت از VWAP فاصله گرفته. برگشت به میانگین احتمال دارد."},
    "macd_crossover": {"name": "📊 MACD", "desc": "تقاطع هیستوگرام MACD از صفر. نشان‌دهنده تغییر مومنتوم."},
    "stoch_rsi": {"name": "📊 Stochastic RSI", "desc": "تقاطع K از D در مناطق اشباع فروش/خرید. سیگنال بازگشت."},
    "obv_divergence": {"name": "📊 OBV", "desc": "واگرایی OBV با قیمت. volume در خلاف جهت قیمت حرکت می‌کند."},
    "sr_bounce": {"name": "🎯 S/R", "desc": "قیمت از سطوح حمایت/مقاومت بازگشته. سطح کلیدی حفظ شده."},
    "volume_trend": {"name": "📊 روند حجم", "desc": "حجم کوتاه‌مدت از بلندمدت بالاتر است. حجم در حال افزایش است."},
}


def _build_strategy_explanations(sig: dict) -> list:
    """Build Persian explanations for each strategy hit in the signal."""
    explanations = []
    for hit in sig.get("hits", []):
        name = hit.get("name", "")
        tf = hit.get("timeframe", "?")
        score = hit.get("score", 0)
        detail = hit.get("detail", {})
        side = detail.get("side", "")

        info = STRATEGY_FA.get(name, {"name": name, "desc": ""})
        side_fa = "🟢 خرید (LONG)" if side in ("long", None) else "🔴 فروش (SHORT)"

        detail_parts = []
        for k, v in detail.items():
            if k in ("side", "type"):
                continue
            if isinstance(v, (int, float)) and abs(v) < 1e9:
                detail_parts.append(f"{k}={round(v, 4)}")
            elif v:
                detail_parts.append(f"{k}={v}")
        detail_str = " | ".join(detail_parts[:4])

        explanations.append({
            "name": name,
            "name_fa": info["name"],
            "description": info["desc"],
            "timeframe": tf,
            "score": round(score, 3),
            "weight": hit.get("weight", 1.0),
            "side": side,
            "side_fa": side_fa,
            "detail": detail_str,
        })
    return explanations


async def _ensure_storage(s: Storage) -> None:
    """FIX: centralised helper — avoids repeating `if s._db is None` everywhere."""
    if not s.is_connected:
        await s.connect()


def create_app(
    em: Optional[ExchangeManager] = None,
    risk: Optional[RiskEngine] = None,
    storage: Optional[Storage] = None,
) -> FastAPI:
    setup_logging()
    settings = get_settings()
    app = FastAPI(title="MemeCoin Sniper Dashboard", version="1.0.0")
    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.web.get("cors_origins", ["*"]),
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )
    static_dir = PROJECT_ROOT / "frontend" / "static"
    if static_dir.exists():
        app.mount("/static", StaticFiles(directory=static_dir), name="static")

    # Wire singletons into shared state.
    _app_state["settings"] = settings
    _app_state["risk"] = risk or RiskEngine(settings)
    _app_state["storage"] = storage or get_storage()
    _app_state["em"] = em or ExchangeManager(settings)
    _app_state["assistant"] = Assistant(settings, storage=_app_state["storage"], risk=_app_state["risk"])
    _app_state["backtester"] = Backtester(settings, _app_state["em"])
    _app_state["ws_clients"] = set()
    _app_state["universe"] = {}

    # ---------------------------------------------------- routes

    @app.get("/", response_class=HTMLResponse)
    async def index() -> HTMLResponse:
        path = PROJECT_ROOT / "frontend" / "templates" / "index.html"
        return HTMLResponse(path.read_text(encoding="utf-8"))

    @app.get("/api/state")
    async def api_state() -> dict:
        risk_state = _app_state["risk"].snapshot()
        return {
            "risk": {
                "equity": risk_state.equity,
                "open_count": risk_state.open_count,
                "daily_pnl_pct": risk_state.daily_pnl_pct,
                "blocked": risk_state.blocked_until_tomorrow,
            },
            "positions": [p.to_dict() for p in _app_state["risk"].open_positions_list()],
            "universe": _app_state.get("universe", {}),
        }

    @app.get("/api/signals")
    async def api_signals(limit: int = Query(100, ge=1, le=500)) -> dict:
        s: Storage = _app_state["storage"]
        await _ensure_storage(s)
        return {"signals": await s.recent_signals(limit)}

    @app.get("/api/trades")
    async def api_trades(limit: int = Query(100, ge=1, le=500)) -> dict:
        s: Storage = _app_state["storage"]
        await _ensure_storage(s)
        return {"trades": await s.recent_trades(limit)}

    @app.get("/api/trades/win-rates")
    async def api_trades_win_rates() -> dict:
        s: Storage = _app_state["storage"]
        await _ensure_storage(s)
        return {"win_rates": await s.get_time_win_rates()}

    @app.get("/api/market/sentiment")
    async def api_market_sentiment() -> dict:
        try:
            from core.news import get_news_tracker
            tracker = get_news_tracker(_app_state["settings"].fundamentals)
            await tracker.refresh()
            return {"sentiment": tracker.get_dict()}
        except Exception as exc:  # noqa: BLE001
            return {"sentiment": {}, "error": str(exc)}

    @app.post("/api/assistant")
    async def api_assistant(payload: dict) -> dict:
        text = (payload or {}).get("text", "")
        state = _app_state["risk"].snapshot()
        reply = _app_state["assistant"].respond(text, risk_state=state)
        s: Storage = _app_state["storage"]
        await _ensure_storage(s)
        if text.strip():
            await s.assistant_log("user", text)
        await s.assistant_log("assistant", reply.text)
        return {"text": reply.text, "suggestion": reply.action_suggestion}

    @app.get("/api/assistant/log")
    async def api_assistant_log(limit: int = 50) -> dict:
        s: Storage = _app_state["storage"]
        await _ensure_storage(s)
        return {"log": await s.recent_assistant_log(limit)}

    @app.get("/api/chart/{exchange}/{symbol}/{tf}")
    async def api_chart(exchange: str, symbol: str, tf: str, limit: int = 300) -> dict:
        em: ExchangeManager = _app_state["em"]
        sym = symbol.replace("_", "/")
        if exchange == "dex" or sym.startswith("DEX:"):
            return {"symbol": sym, "exchange": exchange, "timeframe": tf, "candles": []}
        if exchange not in em.clients:
            try:
                await em.start()
            except Exception as exc:  # noqa: BLE001
                raise HTTPException(500, f"صرافی {exchange} در دسترس نیست: {exc}")
        if exchange not in em.clients:
            raise HTTPException(500, f"صرافی {exchange} متصل نیست")
        try:
            candles = await em.fetch_ohlcv(exchange, sym, tf, limit=limit)
        except KeyError:
            raise HTTPException(500, f"صرافی {exchange} پشتیبانی نمی‌شود")
        except Exception as exc:  # noqa: BLE001
            raise HTTPException(500, str(exc))
        return {
            "symbol": sym, "exchange": exchange, "timeframe": tf,
            "candles": [c.__dict__ for c in candles],
        }

    @app.get("/api/signal/{signal_id}/chart")
    async def api_signal_chart(signal_id: str) -> dict:
        s: Storage = _app_state["storage"]
        await _ensure_storage(s)
        cur = await s.db.execute(
            "SELECT * FROM signals WHERE id = ?", (signal_id,)
        )
        row = await cur.fetchone()
        if not row:
            raise HTTPException(404, "سیگنال پیدا نشد")
        cols = [d[0] for d in cur.description]
        sig = dict(zip(cols, row))
        import json as _json
        sig["hits"] = _json.loads(sig.pop("hits_json") or "[]")
        sig["tf_breakdown"] = _json.loads(sig.pop("tf_breakdown_json") or "{}")

        em: ExchangeManager = _app_state["em"]
        exchange = sig["exchange"]
        symbol = sig["symbol"]
        is_dex = symbol.startswith("DEX:")

        candles_by_tf = {}
        if not is_dex:
            sym = symbol.replace("_", "/")
            if exchange not in em.clients:
                await em.start()
            for tf in ["1m", "5m", "15m", "1h"]:
                try:
                    candles = await em.fetch_ohlcv(exchange, sym, tf, limit=200)
                    candles_by_tf[tf] = [c.__dict__ for c in candles]
                except Exception:  # noqa: BLE001
                    candles_by_tf[tf] = []

        strategy_explanations = _build_strategy_explanations(sig)
        side = sig["side"]
        entry = sig["entry"]
        stop_loss = sig["stop_loss"]
        take_profit = sig["take_profit"]
        atr = sig.get("atr", 0) or 0

        return {
            "signal": sig,
            "candles_by_tf": candles_by_tf,
            "markers": {
                "entry": entry,
                "stop_loss": stop_loss,
                "take_profit": take_profit,
                "atr": atr,
                "side": side,
                "created_at": sig["created_at"],
            },
            "strategy_explanations": strategy_explanations,
        }

    @app.post("/api/backtest")
    async def api_backtest(payload: dict) -> dict:
        bt: Backtester = _app_state["backtester"]
        em: ExchangeManager = _app_state["em"]
        exchange = payload.get("exchange", "binance")
        symbol = payload.get("symbol", "DOGE/USDT")
        if exchange not in em.clients:
            await em.start()
        try:
            from core.exchange import SymbolInfo
            info = SymbolInfo(symbol=symbol, base=symbol.split("/")[0], quote="USDT", listed_at=None)
            res = await bt.run_symbol(exchange, info)
        except Exception as exc:  # noqa: BLE001
            raise HTTPException(500, str(exc))
        return {
            "symbol": res.symbol, "exchange": res.exchange,
            "n_trades": res.n_trades, "win_rate": res.win_rate,
            "profit_factor": res.profit_factor, "total_return_pct": res.total_return_pct,
            "max_drawdown_pct": res.max_drawdown_pct,
            "sharpe": res.sharpe, "sortino": res.sortino, "calmar": res.calmar,
            "avg_pnl_pct": res.avg_pnl_pct,
            "avg_win_pct": res.avg_win_pct, "avg_loss_pct": res.avg_loss_pct,
            "max_consecutive_wins": res.max_consecutive_wins,
            "max_consecutive_losses": res.max_consecutive_losses,
            "avg_holding_bars": res.avg_holding_bars,
            "expectation": res.expectation, "recovery_factor": res.recovery_factor,
            "monte_carlo_5pct": res.monte_carlo_5pct,
            "monte_carlo_95pct": res.monte_carlo_95pct,
            "initial_equity": res.initial_equity, "final_equity": res.final_equity,
            "trades": [t.to_dict() for t in res.trades],
            "equity_curve": [{"t": t, "v": v} for t, v in res.equity_curve],
            "drawdown_curve": [{"t": t, "d": d} for t, d in res.drawdown_curve],
        }

    # ---------------------------------------------------- Settings API
    @app.get("/api/settings")
    async def api_get_settings() -> dict:
        s = _app_state["settings"]
        risk = s.risk
        strats = s.strategies
        return {
            "binance_key": "***" if s.env.get("BINANCE_API_KEY") else "",
            "binance_secret": "***" if s.env.get("BINANCE_API_SECRET") else "",
            "bybit_key": "***" if s.env.get("BYBIT_API_KEY") else "",
            "bybit_secret": "***" if s.env.get("BYBIT_API_SECRET") else "",
            "telegram_enabled": s.telegram.get("enabled", False),
            "tg_token": "***" if s.telegram_token else "",
            "tg_chat": s.telegram_chat_id or "",
            "risk_per_trade_pct": risk.get("risk_per_trade_pct", 1.0),
            "initial_balance": risk.get("initial_paper_balance", 10000),
            "max_positions": risk.get("max_open_positions", 8),
            "daily_loss_pct": risk.get("daily_max_loss_pct", 5.0),
            "sl_mult": risk.get("stop_loss_atr_mult", 1.5),
            "tp_mult": risk.get("take_profit_atr_mult", 3.0),
            "trail_mult": risk.get("trailing_activate_atr_mult", 2.0),
            "min_signal_score": s.min_signal_score,
            "st_new_listing": strats.get("new_listing_sniper", {}).get("enabled", True),
            "st_volume": strats.get("volume_spike", {}).get("enabled", True),
            "st_orderbook": strats.get("orderbook_imbalance", {}).get("enabled", True),
            "st_liquidity": strats.get("liquidity_grab", {}).get("enabled", True),
            "st_momentum": strats.get("momentum_ignition", {}).get("enabled", True),
            "st_rsi": strats.get("rsi_divergence", {}).get("enabled", True),
            "st_bb": strats.get("bb_breakout", {}).get("enabled", True),
            "st_funding": strats.get("funding_oi_spike", {}).get("enabled", False),
            "st_social": strats.get("social_momentum", {}).get("enabled", False),
            "st_ema": strats.get("ema_cross", {}).get("enabled", True),
            "st_adx": strats.get("adx_trend", {}).get("enabled", True),
            "st_squeeze": strats.get("squeeze_momentum", {}).get("enabled", True),
            "st_vwap": strats.get("vwap", {}).get("enabled", True),
            "st_macd": strats.get("macd_crossover", {}).get("enabled", True),
            "st_stoch_rsi": strats.get("stoch_rsi", {}).get("enabled", True),
            "st_obv": strats.get("obv_divergence", {}).get("enabled", True),
            "st_sr": strats.get("sr_bounce", {}).get("enabled", True),
            "st_vol_trend": strats.get("volume_trend", {}).get("enabled", True),
            "dex_enabled": s.dex.get("enabled", True),
            "dex_min_liquidity": s.dex.get("min_liquidity_usd", 10000),
            "dex_max_age": s.dex.get("max_age_hours", 24),
            "llm_enabled": s.assistant.get("use_llm", False),
            "llm_model": s.assistant.get("llm_model", "gpt-4o-mini"),
            "llm_key": "***" if s.llm_api_key else "",
            "llm_url": s.llm_base_url,
        }

    @app.post("/api/settings")
    async def api_save_settings(payload: dict) -> dict:
        try:
            import yaml
            settings_path = PROJECT_ROOT / "config" / "config.yaml"
            env_path = PROJECT_ROOT / ".env"

            env_lines = []
            if env_path.exists():
                env_lines = env_path.read_text(encoding="utf-8").splitlines()

            def set_env(key, value):
                nonlocal env_lines
                found = False
                for i, line in enumerate(env_lines):
                    if line.startswith(f"{key}="):
                        env_lines[i] = f"{key}={value}"
                        found = True
                        break
                if not found:
                    env_lines.append(f"{key}={value}")

            if payload.get("binance_key") and payload["binance_key"] != "***":
                set_env("BINANCE_API_KEY", payload["binance_key"])
            if payload.get("binance_secret") and payload["binance_secret"] != "***":
                set_env("BINANCE_API_SECRET", payload["binance_secret"])
            if payload.get("bybit_key") and payload["bybit_key"] != "***":
                set_env("BYBIT_API_KEY", payload["bybit_key"])
            if payload.get("bybit_secret") and payload["bybit_secret"] != "***":
                set_env("BYBIT_API_SECRET", payload["bybit_secret"])
            if payload.get("tg_token") and payload["tg_token"] != "***":
                set_env("TELEGRAM_BOT_TOKEN", payload["tg_token"])
            if payload.get("tg_chat"):
                set_env("TELEGRAM_CHAT_ID", payload["tg_chat"])
            if payload.get("llm_key") and payload["llm_key"] != "***":
                set_env("LLM_API_KEY", payload["llm_key"])
            if payload.get("llm_url"):
                set_env("LLM_BASE_URL", payload["llm_url"])

            env_path.write_text("\n".join(env_lines) + "\n", encoding="utf-8")

            with settings_path.open("r", encoding="utf-8") as f:
                cfg = yaml.safe_load(f) or {}

            cfg.setdefault("telegram", {})
            cfg["telegram"]["enabled"] = payload.get("telegram_enabled", False)

            cfg.setdefault("risk", {})
            cfg["risk"]["risk_per_trade_pct"] = payload.get("risk_per_trade_pct", 1.0)
            cfg["risk"]["initial_paper_balance"] = payload.get("initial_balance", 10000)
            cfg["risk"]["max_open_positions"] = payload.get("max_positions", 8)
            cfg["risk"]["daily_max_loss_pct"] = payload.get("daily_loss_pct", 5.0)
            cfg["risk"]["stop_loss_atr_mult"] = payload.get("sl_mult", 1.5)
            cfg["risk"]["take_profit_atr_mult"] = payload.get("tp_mult", 3.0)
            cfg["risk"]["trailing_activate_atr_mult"] = payload.get("trail_mult", 2.0)

            cfg["min_signal_score"] = payload.get("min_signal_score", 0.55)
            cfg.setdefault("strategies", {})
            for key, field in [
                ("new_listing_sniper", "st_new_listing"), ("volume_spike", "st_volume"),
                ("orderbook_imbalance", "st_orderbook"), ("liquidity_grab", "st_liquidity"),
                ("momentum_ignition", "st_momentum"), ("rsi_divergence", "st_rsi"),
                ("bb_breakout", "st_bb"), ("funding_oi_spike", "st_funding"),
                ("social_momentum", "st_social"), ("ema_cross", "st_ema"),
                ("adx_trend", "st_adx"), ("squeeze_momentum", "st_squeeze"),
                ("vwap", "st_vwap"),
                ("macd_crossover", "st_macd"), ("stoch_rsi", "st_stoch_rsi"),
                ("obv_divergence", "st_obv"), ("sr_bounce", "st_sr"),
                ("volume_trend", "st_vol_trend"),
            ]:
                cfg["strategies"].setdefault(key, {})
                cfg["strategies"][key]["enabled"] = payload.get(field, True)

            cfg.setdefault("dex", {})
            cfg["dex"]["enabled"] = payload.get("dex_enabled", True)
            cfg["dex"]["min_liquidity_usd"] = payload.get("dex_min_liquidity", 10000)
            cfg["dex"]["max_age_hours"] = payload.get("dex_max_age", 24)

            cfg.setdefault("assistant", {})
            cfg["assistant"]["use_llm"] = payload.get("llm_enabled", False)
            cfg["assistant"]["llm_model"] = payload.get("llm_model", "gpt-4o-mini")

            with settings_path.open("w", encoding="utf-8") as f:
                yaml.dump(cfg, f, allow_unicode=True, default_flow_style=False)

            from config.settings import reload_settings
            reload_settings()

            return {"ok": True, "message": "تنظیمات ذخیره شد"}
        except Exception as exc:
            return {"ok": False, "error": str(exc)}

    # ---------------------------------------------------- Meme Hunter API
    @app.get("/api/hunter/results")
    async def api_hunter_results() -> dict:
        eng = _app_state.get("forward")
        if eng is None or not hasattr(eng, 'meme_hunter') or eng.meme_hunter is None:
            return {"summary": {}, "hits": {}}
        return {
            "summary": eng.meme_hunter.get_summary(),
            "hits": eng.meme_hunter_results,
        }

    @app.get("/api/hunter/summary")
    async def api_hunter_summary() -> dict:
        eng = _app_state.get("forward")
        if eng is None or not hasattr(eng, 'meme_hunter') or eng.meme_hunter is None:
            return {"total_unique": 0, "by_strategy": {}, "last_scan": 0, "daily_picks": []}
        return {
            **eng.meme_hunter.get_summary(),
            "daily_picks": eng.meme_hunter.get_daily_picks(limit=5),
        }

    @app.get("/api/hunter/strategy/{strategy}")
    async def api_hunter_strategy(strategy: str) -> dict:
        eng = _app_state.get("forward")
        if eng is None or not hasattr(eng, 'meme_hunter') or eng.meme_hunter is None:
            return {"strategy": strategy, "hits": []}
        return {
            "strategy": strategy,
            "hits": eng.meme_hunter.get_strategy_hits(strategy),
        }

    @app.get("/api/hunter/success")
    async def api_hunter_success(min_score: float = 0.0, max_score: float = 1.0) -> dict:
        try:
            from core.hunter_tracker import get_hunter_tracker
            tracker = get_hunter_tracker()
            stats = tracker.get_all_stats(min_score=min_score, max_score=max_score)
            overall = tracker.get_overall_stats(min_score=min_score, max_score=max_score)
            return {
                "overall": overall,
                "by_strategy": {k: v.to_dict() for k, v in stats.items()},
                "filter": {"min_score": min_score, "max_score": max_score},
            }
        except Exception as exc:
            return {"overall": {}, "by_strategy": {}, "error": str(exc)}

    @app.get("/api/hunter/recent")
    async def api_hunter_recent(limit: int = 50) -> dict:
        try:
            from core.hunter_tracker import get_hunter_tracker
            tracker = get_hunter_tracker()
            detections = tracker.get_recent_detections(limit)
            return {"detections": [d.to_dict() for d in detections]}
        except Exception as exc:
            return {"detections": [], "error": str(exc)}

    @app.post("/api/hunter/test")
    async def api_hunter_test() -> dict:
        results = {"tests": []}

        try:
            from core.dex import DexScreenerClient
            ds = DexScreenerClient(timeout=10)
            trending = await ds.get_trending(limit=5)
            await ds.close()
            results["tests"].append({
                "name": "DexScreener trending",
                "status": "ok" if trending else "empty",
                "count": len(trending),
                "sample": trending[0].to_dict() if trending else None,
            })
        except Exception as e:
            results["tests"].append({"name": "DexScreener trending", "status": "error", "error": str(e)})

        try:
            from core.dex import DexScreenerClient
            ds = DexScreenerClient(timeout=10)
            search = await ds.search("pump solana", limit=5)
            await ds.close()
            results["tests"].append({
                "name": "DexScreener search 'pump solana'",
                "status": "ok" if search else "empty",
                "count": len(search),
                "sample": search[0].to_dict() if search else None,
            })
        except Exception as e:
            results["tests"].append({"name": "DexScreener search", "status": "error", "error": str(e)})

        try:
            from core.dex import PumpFunClient
            pf = PumpFunClient(timeout=10)
            pf_tokens = await pf.get_new_tokens(limit=5)
            await pf.close()
            results["tests"].append({
                "name": "Pump.fun new tokens",
                "status": "ok" if pf_tokens else "empty",
                "count": len(pf_tokens),
                "sample": pf_tokens[0].to_dict() if pf_tokens else None,
            })
        except Exception as e:
            results["tests"].append({"name": "Pump.fun", "status": "error", "error": str(e)})

        try:
            from core.dex import DexScreenerClient
            ds = DexScreenerClient(timeout=10)
            latest = await ds.get_latest_pairs(chain="solana", limit=5)
            await ds.close()
            results["tests"].append({
                "name": "DexScreener latest Solana pairs",
                "status": "ok" if latest else "empty",
                "count": len(latest),
                "sample": latest[0].to_dict() if latest else None,
            })
        except Exception as e:
            results["tests"].append({"name": "DexScreener latest pairs", "status": "error", "error": str(e)})

        try:
            from core.dex import get_dex_manager
            from config.settings import get_settings
            s = get_settings()
            dm = get_dex_manager(s.dex)
            tokens = await dm.discover_tokens(limit=10)
            await dm.close()
            results["tests"].append({
                "name": "Full discovery (DEXManager)",
                "status": "ok" if tokens else "empty",
                "count": len(tokens),
                "sample": tokens[0].to_dict() if tokens else None,
            })
        except Exception as e:
            results["tests"].append({"name": "Full discovery", "status": "error", "error": str(e)})

        try:
            from core.hunter_tracker import get_hunter_tracker
            tracker = get_hunter_tracker()
            total = tracker.get_overall_stats()
            results["tests"].append({
                "name": "Hunter Tracker DB",
                "status": "ok",
                "total_detections": total.get("total", total.get("total_detections", 0)),
            })
        except Exception as e:
            results["tests"].append({"name": "Tracker DB", "status": "error", "error": str(e)})

        return results

    @app.post("/api/test.telegram")
    async def api_test_telegram() -> dict:
        try:
            from notify.telegram_bot import TelegramNotifier
            s = _app_state["settings"]
            if not s.telegram_token:
                return {"ok": False, "error": "توکن تلگرام تنظیم نشده"}
            notifier = TelegramNotifier(s)
            await notifier.start()
            await notifier.send_text("✅ <b>MemeCoin Sniper</b> — تست اتصال تلگرام موفق بود!")
            await notifier.stop()
            return {"ok": True}
        except Exception as exc:
            return {"ok": False, "error": str(exc)}

    @app.post("/api/test.exchange")
    async def api_test_exchange() -> dict:
        try:
            em: ExchangeManager = _app_state["em"]
            if not em.clients:
                await em.start()
            ticker = await em.fetch_ticker("binance", "BTC/USDT")
            return {"ok": True, "price": ticker.get("last", 0)}
        except Exception as exc:
            return {"ok": False, "error": str(exc)}

    # ---------------------------------------------------- Scalping API
    @app.get("/api/scalping/signals")
    async def api_scalp_signals(limit: int = Query(100, ge=1, le=500)) -> dict:
        s: Storage = _app_state["storage"]
        await _ensure_storage(s)
        return {"signals": await s.recent_scalp_signals(limit)}

    @app.get("/api/scalping/win-rates")
    async def api_scalp_win_rates() -> dict:
        s: Storage = _app_state["storage"]
        await _ensure_storage(s)
        return {"win_rates": await s.get_scalp_win_rates()}

    @app.get("/api/scalping/stats")
    async def api_scalp_stats() -> dict:
        s: Storage = _app_state["storage"]
        await _ensure_storage(s)
        win_rates = await s.get_scalp_win_rates()
        signals = await s.recent_scalp_signals(200)
        total = len(signals)
        active = sum(1 for sig in signals if sig.get("status") == "open")
        tp = sum(1 for sig in signals if sig.get("status") == "tp")
        sl = sum(1 for sig in signals if sig.get("status") == "sl")
        return {
            "total_signals": total,
            "active": active,
            "tp": tp,
            "sl": sl,
            "win_rates": win_rates,
        }

    @app.post("/api/scalping/test")
    async def api_scalp_test() -> dict:
        try:
            from strategies.scalping_engine import ScalpingEngine
            from core.exchange import SymbolInfo
            em: ExchangeManager = _app_state["em"]
            if not em.clients:
                await em.start()
            settings = _app_state["settings"]

            async def candle_provider(exchange, symbol):
                tfs = settings.raw.get("scalping", {}).get("timeframes", ["1m", "5m"])
                out = {}
                for tf in tfs:
                    try:
                        candles = await em.fetch_ohlcv(exchange, symbol, tf, limit=100)
                        out[tf] = candles
                    except Exception:
                        out[tf] = []
                return out

            engine = ScalpingEngine(settings, candle_provider)
            info = SymbolInfo(symbol="BTC/USDT", base="BTC", quote="USDT", listed_at=None)
            sig = await engine.evaluate_symbol("binance", info)
            if sig:
                return {"ok": True, "signal": sig.to_dict()}
            return {"ok": True, "signal": None, "msg": "سیگنالی تولید نشد (امتیاز کافی نبود)"}
        except Exception as exc:
            return {"ok": False, "error": str(exc)}

    @app.post("/api/reset/scalping")
    async def api_reset_scalping() -> dict:
        try:
            s: Storage = _app_state["storage"]
            await _ensure_storage(s)
            await s.db.execute("DELETE FROM signals WHERE id LIKE 'SCP_%'")
            await s.db.execute("DELETE FROM paper_trades WHERE signal_id LIKE 'SCP_%'")
            await s.db.commit()
            return {"ok": True, "msg": "سیگنال‌های اسکلپ پاک شد"}
        except Exception as exc:
            return {"ok": False, "error": str(exc)}

    # ---------------------------------------------------- Reset APIs
    @app.post("/api/reset/hunter")
    async def api_reset_hunter() -> dict:
        try:
            eng = _app_state.get("forward")
            if eng and hasattr(eng, 'meme_hunter') and eng.meme_hunter:
                eng.meme_hunter._all_hits = []
                eng.meme_hunter._by_strategy = {}
                eng.meme_hunter._last_scan = 0
            return {"ok": True, "msg": "شکارچی ریست شد — اسکن مجدد در اسکن بعدی"}
        except Exception as exc:
            return {"ok": False, "error": str(exc)}

    @app.post("/api/reset/signals")
    async def api_reset_signals() -> dict:
        try:
            s: Storage = _app_state["storage"]
            await _ensure_storage(s)
            await s.db.execute("DELETE FROM signals")
            await s.db.commit()
            return {"ok": True, "msg": "تاریخچه سیگنال‌ها پاک شد"}
        except Exception as exc:
            return {"ok": False, "error": str(exc)}

    @app.post("/api/reset/positions")
    async def api_reset_positions() -> dict:
        try:
            risk: RiskEngine = _app_state["risk"]
            closed_count = 0
            for pos in list(risk.open_positions.values()):
                risk._close(pos, pos.entry, "manual_reset")
                closed_count += 1
            risk.equity = float(risk.risk.get("initial_paper_balance", 10000.0))
            risk.start_of_day_equity = risk.equity
            risk.realized_pnl_today = 0.0
            s: Storage = _app_state["storage"]
            await _ensure_storage(s)
            await s.db.execute("UPDATE paper_trades SET status='closed', close_reason='reset' WHERE status='open'")
            await s.db.commit()
            return {"ok": True, "msg": f"{closed_count} پوزیشن بسته شد و موجودی ریست شد"}
        except Exception as exc:
            return {"ok": False, "error": str(exc)}

    @app.post("/api/reset/trades")
    async def api_reset_trades() -> dict:
        try:
            s: Storage = _app_state["storage"]
            await _ensure_storage(s)
            await s.db.execute("DELETE FROM paper_trades")
            await s.db.commit()
            risk: RiskEngine = _app_state["risk"]
            risk.equity = float(risk.risk.get("initial_paper_balance", 10000.0))
            risk.start_of_day_equity = risk.equity
            risk.realized_pnl_today = 0.0
            risk.open_positions = {}
            return {"ok": True, "msg": "تاریخچه تریدها پاک شد و موجودی ریست شد"}
        except Exception as exc:
            return {"ok": False, "error": str(exc)}

    @app.post("/api/reset/success")
    async def api_reset_success() -> dict:
        try:
            from core.hunter_tracker import get_hunter_tracker
            tracker = get_hunter_tracker()
            tracker._conn.execute("DELETE FROM hunter_detections")
            tracker._conn.commit()
            return {"ok": True, "msg": "آمار موفقیت ریست شد"}
        except Exception as exc:
            return {"ok": False, "error": str(exc)}

    @app.post("/api/reset/universe")
    async def api_reset_universe() -> dict:
        try:
            _app_state["universe"] = {}
            return {"ok": True, "msg": "واچ‌لیست ریست شد"}
        except Exception as exc:
            return {"ok": False, "error": str(exc)}

    @app.post("/api/reset/settings")
    async def api_reset_settings() -> dict:
        try:
            from config.settings import Settings
            s = Settings()
            _app_state["settings"] = s
            return {"ok": True, "msg": "تنظیمات به حالت پیش‌فرض برگشت — ریستارت سرویس نیاز است"}
        except Exception as exc:
            return {"ok": False, "error": str(exc)}

    # ---------------------------------------------------- WebSocket
    @app.websocket("/ws")
    async def ws_endpoint(ws: WebSocket) -> None:
        await ws.accept()
        _app_state["ws_clients"].add(ws)
        try:
            # FIX: greeting message was garbled — fixed to proper Persian
            await ws.send_json({"type": "hi", "msg": "به MemeCoin Sniper Dashboard خوش آمدید"})
            while True:
                try:
                    msg = await asyncio.wait_for(ws.receive_text(), timeout=120)
                except asyncio.TimeoutError:
                    await ws.send_json({"type": "ping"})
                    continue
                try:
                    data = json.loads(msg)
                except json.JSONDecodeError:
                    continue
                if data.get("type") == "ping":
                    await ws.send_json({"type": "pong"})
        except WebSocketDisconnect:
            pass
        finally:
            _app_state["ws_clients"].discard(ws)

    # ──────────────────────────── LIT Strategy Endpoints
    try:
        from strategies.lit_engine import LITEngine
        _lit_engine = LITEngine(settings.raw.get("lit", {}))
        _lit_lit_cfg = settings.raw.get("lit", {})

        try:
            import pandas as _pd
            _HAS_PANDAS = True
        except ImportError:
            _HAS_PANDAS = False

        class _SimpleSeries:
            """Minimal pandas Series wrapper."""
            def __init__(self, data):
                self._data = [float(x) for x in data]
            @property
            def values(self):
                import numpy as _np
                return _np.array(self._data)
            def __float__(self):
                return self._data[-1] if self._data else 0.0

        class _SimpleDF:
            """Minimal pandas DataFrame wrapper for lit_engine."""
            def __init__(self, data_dict):
                self._data = data_dict
                self.columns = list(data_dict.keys())
                self.empty = all(len(v) == 0 for v in data_dict.values())
            def __getitem__(self, key):
                return _SimpleSeries(self._data.get(key, []))
            def __len__(self):
                return max((len(v) for v in self._data.values()), default=0)
            def __contains__(self, key):
                return key in self._data

        def _to_df(ohlcv_list):
            data = {
                "open": [float(c.open) for c in ohlcv_list],
                "high": [float(c.high) for c in ohlcv_list],
                "low": [float(c.low) for c in ohlcv_list],
                "close": [float(c.close) for c in ohlcv_list],
                "volume": [float(c.volume) for c in ohlcv_list],
                "timestamp": [int(c.timestamp) for c in ohlcv_list],
            }
            return _SimpleDF(data)

        @app.get("/api/lit/signals")
        async def api_lit_signals(days: int = 10) -> dict:
            s: Storage = _app_state["storage"]
            await _ensure_storage(s)
            try:
                import time as _time
                cutoff = _time.time() - (days * 86400)
                cur = await s.db.execute(
                    "SELECT * FROM signals WHERE id LIKE 'LIT_%' AND created_at > ? ORDER BY created_at DESC LIMIT 500",
                    (cutoff,)
                )
                rows = await cur.fetchall()
                cols = [d[0] for d in cur.description]
                import json as _json
                signals = []
                for r in rows:
                    d = dict(zip(cols, r))
                    for k in ("hits_json", "tf_breakdown_json"):
                        if k in d:
                            val = d.pop(k)
                            new_key = k.replace("_json", "")
                            d[new_key] = _json.loads(val or "[]")
                    # Extract strategy from rationale or id
                    rationale = d.get("rationale", "") or ""
                    if "Sweep-Reversal" in rationale or "sweep_reversal" in rationale:
                        d["strategy"] = "sweep_reversal"
                    elif "Inducement" in rationale or "inducement" in rationale:
                        d["strategy"] = "inducement_continuation"
                    elif "Range" in rationale or "range" in rationale:
                        d["strategy"] = "range_expansion"
                    elif "FVG" in rationale:
                        d["strategy"] = "fvg_retest"
                    elif "displacement" in rationale.lower():
                        d["strategy"] = "displacement_entry"
                    else:
                        d["strategy"] = "lit_structure"
                    # Add timeframe info
                    d["timeframe"] = "15m"  # LIT trigger TF
                    signals.append(d)
                return {"signals": signals, "count": len(signals)}
            except Exception as exc:
                return {"signals": [], "error": str(exc)}

        @app.get("/api/lit/candles/{symbol:path}")
        async def api_lit_candles(symbol: str, timeframe: str = "15m", limit: int = 200) -> dict:
            if "-" in symbol and "/" not in symbol:
                p = symbol.split("-")
                if len(p) == 2:
                    symbol = f"{p[0]}/{p[1]}"
            em: ExchangeManager = _app_state["em"]
            try:
                ohlcv = await em.fetch_ohlcv("binance", symbol, timeframe, limit=min(limit, 500))
                if not ohlcv:
                    return {"candles": [], "volumes": []}
                candles = [{"time": int(c.timestamp), "open": float(c.open), "high": float(c.high), "low": float(c.low), "close": float(c.close)} for c in ohlcv]
                volumes = [{"time": int(c.timestamp), "value": float(c.volume), "color": "rgba(34,197,94,0.3)" if c.close >= c.open else "rgba(239,68,68,0.3)"} for c in ohlcv]
                return {"candles": candles, "volumes": volumes, "symbol": symbol, "timeframe": timeframe}
            except Exception as exc:
                return {"candles": [], "volumes": [], "error": str(exc)}

        @app.get("/api/lit/backtest/{symbol:path}")
        async def api_lit_backtest(symbol: str, timeframe: str = "15m", limit: int = 500) -> dict:
            if "-" in symbol and "/" not in symbol:
                p = symbol.split("-")
                if len(p) == 2:
                    symbol = f"{p[0]}/{p[1]}"
            em: ExchangeManager = _app_state["em"]
            try:
                ohlcv = await em.fetch_ohlcv("binance", symbol, timeframe, limit=min(limit, 1000))
                if not ohlcv:
                    return {"error": "No data", "trades": [], "candles": [], "volumes": []}

                candles = [{"time": int(c.timestamp), "open": float(c.open), "high": float(c.high), "low": float(c.low), "close": float(c.close)} for c in ohlcv]
                volumes = [{"time": int(c.timestamp), "value": float(c.volume), "color": "rgba(34,197,94,0.3)" if c.close >= c.open else "rgba(239,68,68,0.3)"} for c in ohlcv]

                closes = [float(c.close) for c in ohlcv]
                highs = [float(c.high) for c in ohlcv]
                lows = [float(c.low) for c in ohlcv]

                htf = "1h" if timeframe in ("1m", "5m", "15m") else "4h"
                htf_data = await em.fetch_ohlcv("binance", symbol, htf, limit=200)
                htf_df = _to_df(htf_data) if htf_data else None

                trades = []
                min_score = float(_lit_lit_cfg.get("min_score", 0.70))
                cooldown_candles = 30
                window = 80

                for i in range(window, len(closes)):
                    if i + 2 >= len(closes):
                        break
                    w_start = max(0, i - window)
                    w_ohlcv = ohlcv[w_start:i+1]
                    w_df = _to_df(w_ohlcv)

                    try:
                        signal = _lit_engine.analyze(w_df, symbol, "binance", htf_df)
                    except Exception:
                        continue

                    if signal and signal.score >= min_score:
                        entry = signal.entry
                        sl = signal.stop_loss
                        tp = signal.take_profit
                        tp2 = signal.take_profit_2

                        exit_price = None
                        exit_reason = None
                        exit_candle = i
                        hit_tp1 = False
                        hit_tp2 = False

                        for j in range(i+1, min(i+51, len(closes))):
                            if signal.side == "long":
                                if not hit_tp1 and highs[j] >= tp:
                                    hit_tp1 = True
                                if hit_tp1 and highs[j] >= tp2:
                                    exit_price = tp2; exit_reason = "tp2"; exit_candle = j; break
                                if lows[j] <= sl:
                                    exit_price = sl; exit_reason = "sl" if not hit_tp1 else "sl_risk_free"; exit_candle = j; break
                            else:
                                if not hit_tp1 and lows[j] <= tp:
                                    hit_tp1 = True
                                if hit_tp1 and lows[j] <= tp2:
                                    exit_price = tp2; exit_reason = "tp2"; exit_candle = j; break
                                if highs[j] >= sl:
                                    exit_price = sl; exit_reason = "sl" if not hit_tp1 else "sl_risk_free"; exit_candle = j; break

                        if exit_price is None:
                            exit_price = closes[min(i+50, len(closes)-1)]
                            exit_reason = "timeout"
                            exit_candle = min(i+50, len(closes)-1)

                        if signal.side == "long":
                            pnl_pct = round((exit_price - entry) / entry * 100, 2)
                        else:
                            pnl_pct = round((entry - exit_price) / entry * 100, 2)

                        risk_dist = abs(entry - sl) if abs(entry - sl) > 1e-10 else entry * 0.01
                        r_mult = round(pnl_pct / (risk_dist / entry * 100), 2) if abs(risk_dist) > 1e-10 else 0

                        ec = min(exit_candle, len(ohlcv)-1)
                        trades.append({
                            "signal_id": signal.id, "symbol": symbol, "side": signal.side,
                            "strategy": signal.strategy,
                            "entry_time": int(ohlcv[i].timestamp), "entry_price": round(entry, 8),
                            "entry_reasoning": signal.reasoning,
                            "exit_time": int(ohlcv[ec].timestamp), "exit_price": round(exit_price, 8),
                            "exit_reason": exit_reason,
                            "stop_loss": round(sl, 8), "take_profit_1": round(tp, 8), "take_profit_2": round(tp2, 8),
                            "pnl_pct": pnl_pct, "r_multiple": r_mult,
                            "hit_tp1": hit_tp1, "hit_tp2": hit_tp2, "hit_sl": exit_reason and "sl" in exit_reason,
                            "zones": signal.zones, "entry_candle_idx": i, "exit_candle_idx": exit_candle,
                        })

                filtered = []
                last_exit = -cooldown_candles
                for t in trades:
                    if t["entry_candle_idx"] > last_exit + cooldown_candles:
                        filtered.append(t)
                        last_exit = t["exit_candle_idx"]

                total = len(filtered)
                wins = sum(1 for t in filtered if t["pnl_pct"] > 0)
                losses = total - wins

                strat_stats = {}
                for st in set(t["strategy"] for t in filtered):
                    st_trades = [t for t in filtered if t["strategy"] == st]
                    st_w = sum(1 for t in st_trades if t["pnl_pct"] > 0)
                    strat_stats[st] = {"total": len(st_trades), "wins": st_w, "win_rate": round(st_w/len(st_trades)*100,1) if st_trades else 0, "avg_pnl": round(sum(t["pnl_pct"] for t in st_trades)/len(st_trades),2) if st_trades else 0, "avg_r": round(sum(t["r_multiple"] for t in st_trades)/len(st_trades),2) if st_trades else 0}

                return {
                    "symbol": symbol, "timeframe": timeframe,
                    "start_time": int(ohlcv[0].timestamp), "end_time": int(ohlcv[-1].timestamp),
                    "total_trades": total, "wins": wins, "losses": losses,
                    "win_rate": round(wins/total*100,1) if total else 0,
                    "avg_pnl_pct": round(sum(t["pnl_pct"] for t in filtered)/total,2) if total else 0,
                    "total_pnl_pct": round(sum(t["pnl_pct"] for t in filtered),2),
                    "avg_r_multiple": round(sum(t["r_multiple"] for t in filtered)/total,2) if total else 0,
                    "best_trade": max((t["pnl_pct"] for t in filtered), default=0),
                    "worst_trade": min((t["pnl_pct"] for t in filtered), default=0),
                    "strategy_stats": strat_stats,
                    "trades": filtered, "candles": candles, "volumes": volumes,
                }
            except Exception as exc:
                import traceback as _tb
                logger.error(f"LIT backtest error: {_tb.format_exc()}")
                return {"error": str(exc), "trades": [], "candles": [], "volumes": []}

        @app.get("/api/lit/analyze/{symbol:path}")
        async def api_lit_analyze(symbol: str, timeframe: str = "15m") -> dict:
            """Analyze symbol and return full LIT data for chart display.
            Always returns structure/liquidity/FVG data even if no signal fires."""
            if "-" in symbol and "/" not in symbol:
                p = symbol.split("-")
                if len(p) == 2:
                    symbol = f"{p[0]}/{p[1]}"
            em: ExchangeManager = _app_state["em"]
            try:
                ohlcv = await em.fetch_ohlcv("binance", symbol, timeframe, limit=200)
                if not ohlcv:
                    return {"error": "No data"}

                import numpy as np
                opens = np.array([c.open for c in ohlcv], dtype=float)
                highs = np.array([c.high for c in ohlcv], dtype=float)
                lows = np.array([c.low for c in ohlcv], dtype=float)
                closes = np.array([c.close for c in ohlcv], dtype=float)
                timestamps = np.array([c.timestamp for c in ohlcv], dtype=float)

                # Try full LIT signal first
                df = _to_df(ohlcv)
                # Fetch HTF data for better bias
                htf_ohlcv = await em.fetch_ohlcv("binance", symbol, "1h", limit=100)
                htf_df = _to_df(htf_ohlcv) if htf_ohlcv else None

                signal = _lit_engine.analyze(df, symbol, "binance", htf_df)
                if signal:
                    return signal.to_dict()

                # Even without a signal, return market structure data for chart
                from strategies.lit_structure import StructureEngine
                from strategies.lit_liquidity import LiquidityEngine
                from strategies.lit_patterns import FVGDetector, OBDetector

                se = StructureEngine(left_bars=3, right_bars=3, min_displacement_atr=1.0, min_body_ratio=0.5)
                structure = se.analyze(opens, highs, lows, closes, timestamps)
                atr = se._calc_atr(highs, lows, closes, 14)
                current_price = float(closes[-1])

                le = LiquidityEngine()
                liq_map = le.analyze(opens, highs, lows, closes, structure, atr, current_price, timeframe, timestamps)

                fd = FVGDetector()
                fvgs = fd.find_fvgs(opens, highs, lows, closes, timestamps)

                od = OBDetector()
                obs = od.find_order_blocks(opens, highs, lows, closes, timestamps)

                # Build response with analysis data (no trade signal)
                return {
                    "message": "No trade signal — showing market analysis",
                    "bias": structure.trend.value,
                    "structure_data": {
                        "trend": structure.trend.value,
                        "swing_highs": len(structure.swing_highs),
                        "swing_lows": len(structure.swing_lows),
                        "events": len(structure.events),
                        "displacements": len(structure.displacements),
                    },
                    "liquidity_levels": [
                        {"price": p.price, "side": p.side.value, "kind": p.kind.value, "strength": p.strength}
                        for p in (liq_map.buy_side_pools[:5] + liq_map.sell_side_pools[:5])
                    ],
                    "fvg_zones": [
                        {"top": f.top, "bottom": f.bottom, "direction": f.direction}
                        for f in fvgs[-5:]
                    ],
                    "order_blocks": [
                        {"top": ob.top, "bottom": ob.bottom, "direction": ob.direction}
                        for ob in obs[-5:]
                    ],
                    "chart_annotations": [
                        {"type": "line", "tag": p.side.value, "price": p.price,
                         "text": f"{p.kind.value} (x{p.strength})",
                         "color": "#ef4444" if p.side.value == "buy_side" else "#22c55e"}
                        for p in (liq_map.buy_side_pools[:3] + liq_map.sell_side_pools[:3])
                    ],
                    "reasons": [
                        f"ساختار: {structure.trend.value}",
                        f"نقدینگی خرید: {len(liq_map.buy_side_pools)} سطح",
                        f"نقدینگی فروش: {len(liq_map.sell_side_pools)} سطح",
                        f"FVG: {len(fvgs)} شکاف",
                        f"OB: {len(obs)} بلاک",
                        f"Sweep: {len(liq_map.sweeps)} جاروب",
                    ],
                    "atr": round(atr, 8),
                }
            except Exception as exc:
                import traceback
                return {"error": str(exc), "traceback": traceback.format_exc()}

        logger.info("LIT API endpoints registered")
    except Exception as _lit_err:
        import traceback as _tb
        logger.warning(f"LIT endpoints not loaded: {_lit_err}\n{_tb.format_exc()}")

    return app


async def broadcast(event: dict) -> None:
    """Push event to all dashboard WS clients."""
    clients = list(_app_state.get("ws_clients", set()))
    dead = []
    for ws in clients:
        try:
            await ws.send_json(event)
        except Exception:  # noqa: BLE001
            dead.append(ws)
    for d in dead:
        _app_state.get("ws_clients", set()).discard(d)
