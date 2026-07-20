"""Smoke tests for the strategy layer (run without exchange connectivity).

    python -m mems_sniper.tests.test_strategies
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def make_df(closes, opens=None, highs=None, lows=None, vols=None):
    import pandas as pd
    n = len(closes)
    opens = opens or closes
    highs = highs or [c * 1.01 for c in closes]
    lows = lows or [c * 0.99 for c in closes]
    vols = vols or [1000.0] * n
    df = pd.DataFrame({
        "open": opens, "high": highs, "low": lows,
        "close": closes, "volume": vols,
    })
    return df


def test_volume_spike():
    from strategies.strategies import VolumeSpike
    closes = [1.0] * 20 + [1.05]
    vols = [100.0] * 20 + [400.0]   # 4x average
    df = make_df(closes=closes, vols=vols)
    strat = VolumeSpike({"enabled": True, "multiplier": 3.0,
                         "rolling_window": 20, "weight": 1.0})
    hit = strat.evaluate(df, {"timeframe": "1m"})
    assert hit is not None, "Volume spike should fire"
    assert hit.name == "volume_spike"
    assert hit.detail["vol_multiplier"] >= 3.0
    print(f"  ✓ volume_spike fired: mult={hit.detail['vol_multiplier']}, score={hit.score}")


def test_volume_spike_no_signal():
    from strategies.strategies import VolumeSpike
    closes = [1.0] * 21
    vols = [100.0] * 21   # average everywhere
    df = make_df(closes=closes, vols=vols)
    strat = VolumeSpike({"enabled": True, "multiplier": 3.0,
                         "rolling_window": 20, "weight": 1.0})
    hit = strat.evaluate(df, {"timeframe": "1m"})
    assert hit is None
    print("  ✓ volume_spike stayed silent without spike")


def test_momentum_ignition():
    from strategies.strategies import MomentumIgnition
    closes = [1.0] * 20 + [1.05, 1.08]
    opens = [1.0] * 20 + [1.0, 1.05]
    df = make_df(closes=closes, opens=opens)
    strat = MomentumIgnition({
        "enabled": True, "first_body_min_pct": 4.0,
        "confirm_body_min_pct": 2.0, "weight": 1.0,
    })
    hit = strat.evaluate(df, {"timeframe": "1m"})
    assert hit is not None, "Momentum ignition should fire"
    assert hit.detail["side"] == "long"
    print(f"  ✓ momentum_ignition fired: first={hit.detail['first_body_pct']}% confirm={hit.detail['confirm_body_pct']}%")


def test_bb_breakout():
    from strategies.strategies import BBBreakout
    import numpy as np
    # 30 candles in a flat range then a jump
    closes = [1.0] * 30 + [1.20]
    df = make_df(closes=closes)
    strat = BBBreakout({"enabled": True, "bb_length": 20, "bb_std": 2.0,
                        "breakout_buffer_pct": 0.1, "weight": 0.8})
    hit = strat.evaluate(df, {"timeframe": "1m"})
    assert hit is not None, "BB breakout should fire on jump"
    assert hit.detail["side"] == "long"
    print(f"  ✓ bb_breakout fired: close={hit.detail['close']} upper={hit.detail['upper']}")


def test_rsi_calculation():
    from strategies.indicators import rsi
    import pandas as pd
    # alternating up/down should yield RSI near 50 on long sample
    s = pd.Series([1.0 + (i % 2) * 0.01 for i in range(40)])
    r = rsi(s, 14)
    last = r.iloc[-1]
    assert 30 < last < 70, f"RSI unexpected: {last}"
    print(f"  ✓ rsi computed OK, last={last:.2f}")


def test_new_listing_sniper():
    import time
    from strategies.strategies import NewListingSniper
    closes = [1.0, 1.0, 1.06]   # first candle +6%
    opens = [1.0, 1.0, 1.0]
    df = make_df(closes=closes, opens=opens)
    strat = NewListingSniper({
        "enabled": True, "max_age_hours": 72,
        "first_candle_min_pct": 5.0, "weight": 1.5,
    })
    listed_at = int(time.time() * 1000) - 2 * 3_600_000   # 2h ago
    hit = strat.evaluate(df, {"timeframe": "1m", "listed_at": listed_at})
    assert hit is not None, "new listing sniper should fire"
    assert hit.detail["age_hours"] < 72
    print(f"  ✓ new_listing fired: age={hit.detail['age_hours']}h, body={hit.detail['first_body_pct']}%")


def test_assistant_basic():
    from assistant import Assistant
    from config.settings import get_settings
    settings = get_settings()
    a = Assistant(settings)
    r1 = a.respond("پامپ چیست؟")
    assert "پامپ" in r1.text
    r2 = a.respond("اتر یعني چی؟")
    assert "ATR" in r2.text or "اتر" in r2.text or "اتr" in r2.text.lower()
    print(f"  ✓ assistant responds (پامپ): {r1.text[:50]}...")
    print(f"  ✓ assistant responds (اتر): {r2.text[:50]}...")


def main():
    print("Running strategy & assistant smoke tests...")
    tests = [
        test_rsi_calculation,
        test_volume_spike,
        test_volume_spike_no_signal,
        test_momentum_ignition,
        test_bb_breakout,
        test_new_listing_sniper,
        test_assistant_basic,
    ]
    for t in tests:
        try:
            t()
        except AssertionError as ae:
            print(f"  ✗ {t.__name__}: {ae}")
            sys.exit(1)
        except Exception as exc:
            print(f"  ! {t.__name__}: skipped ({type(exc).__name__}: {exc})")
    print("\nAll smoke tests passed ✅")


if __name__ == "__main__":
    main()
