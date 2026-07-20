"""Risk management - position sizing, SL/TP/trailing tracking, daily PnL guard.

Sized against paper-equity; never executes a real order in this codebase.
The forward engine relies on this module to open, update, and close paper
positions as new ticks arrive.
"""
from __future__ import annotations

import time
import uuid
from dataclasses import dataclass
from typing import Dict, List, Optional, Tuple

from config.settings import Settings
from core.logging_setup import logger
from core.models import PaperPosition, Signal, Side


@dataclass
class RiskState:
    equity: float
    open_count: int
    daily_pnl_pct: float
    blocked_until_tomorrow: bool


class RiskEngine:
    def __init__(self, settings: Settings) -> None:
        self.s = settings
        self.risk = settings.risk
        self.equity = float(self.risk.get("initial_paper_balance", 10000.0))
        self.start_of_day_equity = self.equity
        self.today = time.gmtime().tm_yday
        self.open_positions: Dict[str, PaperPosition] = {}    # by position id
        self.realized_pnl_today: float = 0.0
        self.daily_loss_limit_pct = float(self.risk.get("daily_max_loss_pct", 5.0))
        self.max_position_age = int(self.risk.get("max_position_age_seconds", 3600))
        # Minimum seconds a position must be open before SL/TP is even
        # checked — prevents "entry_time == exit_time" instant stop-outs
        # caused by a stale/gapped price tick landing right after open.
        self.min_position_age_before_exit = float(self.risk.get("min_position_age_before_exit_seconds", 5))
        # If a single price update would produce a loss more than this many
        # multiples of the intended SL distance, treat it as an anomalous/
        # stale price tick rather than a legitimate stop-out — skip this
        # update and let the NEXT tick (hopefully sane) decide.
        self.max_gap_multiple_of_sl = float(self.risk.get("max_gap_multiple_of_sl_distance", 3.0))

    # ------------------------------------ load open positions from DB on startup
    def load_open_positions(self, rows: list) -> None:
        """Restore open positions from DB after restart."""
        for r in rows:
            pos = PaperPosition(
                id=r["id"],
                opened_at=r["opened_at"],
                exchange=r["exchange"],
                symbol=r["symbol"],
                side=Side(r["side"]),
                entry=r["entry"],
                stop_loss=r["stop_loss"] or 0,
                take_profit=r["take_profit"] or 0,
                trailing_atr=0,
                atr=0,
                size_usdt=r["size_usdt"] or 0,
                qty=r["qty"] or 0,
                status="open",
                base=r.get("base"),
                leverage=r.get("leverage", 1.0) or 1.0,
                market_type=r.get("market_type", "spot") or "spot",
                signal_id=r.get("signal_id"),
                fee_usdt=r.get("fee_usdt", 0) or 0,
                slippage_usdt=r.get("slippage_usdt", 0) or 0,
                tp2=r.get("tp2"),
                tp3=r.get("tp3"),
                tp1_hit=bool(r.get("tp1_hit", 0)),
                risk_free=bool(r.get("risk_free", 0)),
            )
            self.open_positions[pos.id] = pos
            logger.info(f"RESTORED open position {pos.symbol} {pos.side.value} entry={pos.entry} TP1={pos.take_profit} TP2={pos.tp2} TP3={pos.tp3}")

    # ---------------------------------------------------- day rollover
    def _rollover_day_if_needed(self) -> None:
        day = time.gmtime().tm_yday
        if day != self.today:
            self.today = day
            self.start_of_day_equity = self.equity
            self.realized_pnl_today = 0.0

    # ---------------------------------------------------- open
    def can_open(self) -> bool:
        self._rollover_day_if_needed()
        pnl_pct = (self.equity - self.start_of_day_equity) / max(self.start_of_day_equity, 1e-9) * 100.0
        if pnl_pct <= -self.daily_loss_limit_pct:
            logger.warning(
                f"Risk: daily loss guard hit ({pnl_pct:.2f}%). Blocking new entries."
            )
            return False
        return True

    def open_from_signal(self, sig: Signal, live_price: Optional[float] = None) -> Optional[PaperPosition]:
        if not self.can_open():
            return None
        size = float(sig.position_size_usdt)
        if size <= 0 or self.equity <= 0:
            return None

        # ── Slippage guard ──
        # sig.entry is the last CLOSED candle's close, computed potentially
        # several seconds before this call. If the caller supplies a fresh
        # live_price and it has already gapped too far from sig.entry (thin
        #/illiquid symbols can move several % between candle-close and the
        # moment we actually open the paper position), reject the trade
        # instead of opening a position that's already effectively past its
        # own stop-loss (this was the exact cause of the TRADOOR/USDT trade
        # that had entry_time == exit_time with a huge instant loss).
        max_slippage_pct = float(self.risk.get("max_entry_slippage_pct", 0.5))
        if live_price is not None and live_price > 0 and sig.entry > 0:
            slippage_pct = abs(live_price - sig.entry) / sig.entry * 100
            if slippage_pct > max_slippage_pct:
                logger.warning(
                    f"Risk: REJECTED {sig.symbol} — entry slippage {slippage_pct:.2f}% "
                    f"(signal={sig.entry:.6g}, live={live_price:.6g}) exceeds "
                    f"max {max_slippage_pct}%"
                )
                return None
            # Use the live price as the real fill price (more honest than the
            # stale candle close) but only when it's within tolerance.
            sig.entry = live_price
        risk_pct = float(self.risk.get("risk_per_trade_pct", 1.0)) / 100.0
        # position size = equity * risk_pct / distance-to-SL ratio
        dist = abs(sig.entry - sig.stop_loss)
        if dist <= 0:
            return None
        qty = (self.equity * risk_pct) / dist
        # but cap qty so notional doesn't exceed size (limit leverage in paper)
        notional_max = min(size, self.equity * 0.10)
        qty_max = notional_max / sig.entry
        qty = min(qty, qty_max)
        if qty <= 0:
            return None
        # Determine market type and leverage from signal
        is_dex = sig.symbol.startswith("DEX:")
        market_type = "dex" if is_dex else "futures"
        leverage = max(1.0, round(notional_max / (qty * sig.entry), 1)) if qty * sig.entry > 0 else 1.0

        pos = PaperPosition(
            id=uuid.uuid4().hex[:12],
            opened_at=time.time(),
            exchange=sig.exchange,
            symbol=sig.symbol,
            side=sig.side,
            entry=sig.entry,
            stop_loss=sig.stop_loss,
            take_profit=sig.take_profit,
            trailing_atr=sig.trailing_atr,
            atr=sig.atr,
            size_usdt=round(qty * sig.entry, 2),
            qty=float(qty),
            base=sig.base,
            leverage=leverage,
            market_type=market_type,
            signal_id=sig.id,
            tp2=sig.tp2,
            tp3=sig.tp3,
        )
        self.open_positions[pos.id] = pos
        logger.info(
            f"OPENED paper {pos.side.value.upper()} {pos.symbol} "
            f"qty={pos.qty:.6f} entry={pos.entry:.6f} SL={pos.stop_loss:.6f} "
            f"TP1={pos.take_profit:.6f} TP2={pos.tp2} TP3={pos.tp3} notional={pos.size_usdt:.2f}"
        )
        return pos

    # ---------------------------------------------------- update on tick
    def update_with_price(self, symbol: str, price: float) -> List[PaperPosition]:
        """Check SL/TP/trailing/timeout for all open positions matching symbol. Return closed list."""
        closed: List[PaperPosition] = []
        now = time.time()
        for pos in list(self.open_positions.values()):
            if pos.symbol != symbol:
                continue
            # Time-based exit: close stale positions
            if self.max_position_age > 0 and (now - pos.opened_at) > self.max_position_age:
                hit = self._close(pos, price, "timeout")
                closed.append(hit)
                continue
            # Grace period — skip SL/TP checks entirely for the first few
            # seconds after open, so a stale/laggy price feed can't produce
            # an "instant" stop-out before a real live quote has arrived.
            if (now - pos.opened_at) < self.min_position_age_before_exit:
                continue
            # Anomaly guard — reject a single tick that implies a loss far
            # beyond the position's own SL distance (e.g. a bad/gapped
            # ticker read on a thin altcoin). Skip this update; a sane
            # subsequent tick will close it normally if the move is real.
            sl_distance = abs(pos.entry - pos.stop_loss)
            if sl_distance > 0:
                implied_move = abs(price - pos.entry)
                if implied_move > sl_distance * self.max_gap_multiple_of_sl:
                    logger.warning(
                        f"Risk: ANOMALOUS price for {pos.symbol} — price={price:.6g} "
                        f"entry={pos.entry:.6g} SL={pos.stop_loss:.6g} implies "
                        f"{implied_move/sl_distance:.1f}x SL distance in one tick — "
                        f"skipping this update"
                    )
                    continue
            self._maybe_update_trailing(pos, price)
            hit = self._check_exit(pos, price)
            if hit is not None:
                closed.append(hit)
        return closed

    def _maybe_update_trailing(self, pos: PaperPosition, price: float) -> None:
        """Progressive trailing stop: locks in profit as price moves in our favour."""
        atr = pos.trailing_atr if pos.trailing_atr > 0 else pos.atr
        if atr <= 0:
            return
        if pos.side == Side.LONG:
            favourable = price - pos.entry
            if favourable >= atr:
                new_sl = price - atr
                if new_sl > pos.stop_loss:
                    pos.stop_loss = new_sl
        else:
            favourable = pos.entry - price
            if favourable >= atr:
                new_sl = price + atr
                if new_sl < pos.stop_loss or pos.stop_loss == 0:
                    pos.stop_loss = new_sl

    def _check_exit(self, pos: PaperPosition, price: float) -> Optional[PaperPosition]:
        """Exit logic: SL/TP check with immediate close on TP1.

        For scalp/signal positions:
        - TP1 hit → CLOSE immediately (record as win)
        - SL hit → CLOSE immediately (record as loss)
        
        This ensures win-rate tracking works properly.
        Trailing stop still active if price runs further before close.
        """
        if pos.side == Side.LONG:
            # --- TP1 hit: CLOSE with profit ---
            if price >= pos.take_profit:
                return self._close(pos, price, "tp1")
            # --- SL hit ---
            if price <= pos.stop_loss:
                reason = "sl_risk_free" if pos.risk_free else "sl"
                return self._close(pos, price, reason)
        else:  # SHORT
            # --- TP1 hit: CLOSE with profit ---
            if price <= pos.take_profit:
                return self._close(pos, price, "tp1")
            # --- SL hit ---
            if price >= pos.stop_loss:
                reason = "sl_risk_free" if pos.risk_free else "sl"
                return self._close(pos, price, reason)

        # Update unrealized PnL
        if pos.side == Side.LONG:
            pos.current_price = price
            pos.unrealized_pnl_pct = round((price - pos.entry) / pos.entry * 100, 2)
        else:
            pos.current_price = price
            pos.unrealized_pnl_pct = round((pos.entry - price) / pos.entry * 100, 2)

        return None

    def _close(self, pos: PaperPosition, price: float, reason: str) -> PaperPosition:
        pos.closed_at = time.time()
        pos.exit_price = float(price)
        pos.close_reason = reason
        if pos.side == Side.LONG:
            pnl = (price - pos.entry) * pos.qty
        else:
            pnl = (pos.entry - price) * pos.qty
        pos.pnl_usdt = round(float(pnl), 4)
        pos.pnl_pct = round(float(pnl / (pos.entry * pos.qty) * 100) if pos.entry * pos.qty else 0, 3)
        pos.status = "closed"
        self.equity += pnl
        self.realized_pnl_today += pnl
        self.open_positions.pop(pos.id, None)
        logger.info(
            f"CLOSED paper {pos.symbol} reason={reason} exit={price:.6f} "
            f"pnl={pnl:.4f} USDT ({pos.pnl_pct:.2f}%)"
        )
        return pos

    # ---------------------------------------------------- manual close
    def close_position(self, pos_id: str, price: float) -> Optional[PaperPosition]:
        pos = self.open_positions.get(pos_id)
        if pos is None:
            return None
        return self._close(pos, price, reason="manual")

    # ---------------------------------------------------- snapshot
    def snapshot(self) -> RiskState:
        self._rollover_day_if_needed()
        pnl_pct = (self.equity - self.start_of_day_equity) / max(self.start_of_day_equity, 1e-9) * 100.0
        return RiskState(
            equity=round(self.equity, 2),
            open_count=len(self.open_positions),
            daily_pnl_pct=round(pnl_pct, 3),
            blocked_until_tomorrow=pnl_pct <= -self.daily_loss_limit_pct,
        )

    def open_positions_list(self) -> List[PaperPosition]:
        return list(self.open_positions.values())

    def has_open_position_for_symbol(self, symbol: str) -> bool:
        """True if a symbol already has an active paper position.

        Used to stop the signal-generation loops from creating a brand new
        signal for a symbol whose previous trade hasn't resolved yet — this
        was the main driver of the 'duplicate signals' bug where the exact
        same entry/SL/TP kept reappearing every cooldown cycle because the
        prior signal's position was still open (or its cooldown had simply
        expired while the underlying setup hadn't changed).
        """
        return any(p.symbol == symbol for p in self.open_positions.values())
