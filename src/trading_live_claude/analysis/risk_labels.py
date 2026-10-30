"""Risk targets for the lenses: forward 5-day drawdown over trailing volatility, per symbol per day.

Revision 1 of the lens redefinition (NEXT_SESSION.md, 2026-10-12): a lens predicts near-term risk, not
return. For day D (anchored at D's close, the bar the feature store's cutoff precedes):

    dd_5d      = max(0, 1 - min(low[D+1 .. D+5]) / close[D])     worst drop over the next 5 bars
    vol_20d    = stdev of daily log returns over the 20 bars ending at D (sample, ddof=1)
    target     = dd_5d / (vol_20d * sqrt(5))                     drawdown in units of expected 5-day move

Volatility is only the yardstick of the target; it is never a lens input (user rule, 2026-10-12).
Raw dd_5d and vol_20d are kept on the row for the backtest's economics.

A label is unknown (no row) until all 5 forward bars exist; ``matured_date`` is the day it became known,
which the lens audit's embargo uses. Bars are trading days of the series itself (crypto: calendar days).

Contract, as for the rest of ``analysis``: outcomes are targets only, never features, and nothing in the
trading path imports this module.
"""
from __future__ import annotations

import math
from collections.abc import Iterator, Sequence
from dataclasses import dataclass
from datetime import date

HORIZON = 5
VOL_WINDOW = 20


@dataclass(frozen=True)
class Bar:
    day: date
    low: float
    close: float


def labels(bars: Sequence[Bar], *, horizon: int = HORIZON, vol_window: int = VOL_WINDOW) -> Iterator[dict[str, object]]:
    """One label per bar that has a full trailing vol window behind it and ``horizon`` bars ahead.

    Bars must be one per day, oldest first. Non-positive prices are skipped: they are data errors, and a
    log return through them would be meaningless.
    """
    bars = [b for b in bars if b.close > 0 and b.low > 0]
    rets = [math.log(bars[i].close / bars[i - 1].close) for i in range(1, len(bars))]
    for i in range(vol_window, len(bars) - horizon):
        window = rets[i - vol_window:i]  # rets[k] is the return INTO bar k+1, so this ends at bar i
        mean = sum(window) / vol_window
        vol = math.sqrt(sum((r - mean) ** 2 for r in window) / (vol_window - 1))
        worst = min(b.low for b in bars[i + 1:i + 1 + horizon])
        dd = max(0.0, 1.0 - worst / bars[i].close)
        yield {
            "date": bars[i].day.isoformat(),
            "close": bars[i].close,
            "dd_5d": round(dd, 6),
            "vol_20d": round(vol, 6),
            "target": round(dd / (vol * math.sqrt(horizon)), 6) if vol > 0 else None,
            "horizon": horizon,
            "matured_date": bars[i + horizon].day.isoformat(),
        }
