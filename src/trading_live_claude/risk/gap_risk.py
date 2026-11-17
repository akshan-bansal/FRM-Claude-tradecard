"""Gap-risk gate: size an entry for the price jump between one session's close and the next one's open.

A stop is checked when the venue is polled, and a closed venue is not polled, so a stop that is gapped
through fills at the next open, past the stop. Sizing by stop distance alone then understates what a
position held across a close can lose, and so does the heat cap.

From a symbol's daily bars, the adverse gap is ``1 - open[t] / close[t-1]``, weekends included. Its
empirical ``level`` quantile is the loss per unit of notional this gate plans for. For an entry it:

1. trims the size so that loss stays within the per-trade risk budget (``risk_pct * equity``);
2. reports the gap loss at the trimmed size, so the Router can count it in portfolio heat when it
   exceeds the stop's risk.

Entries only, and it may only trim or reject. A venue that never closes (crypto) has no gap and passes
untouched. Too few gaps, or bars that cannot be read, fail closed, as the spectral VaR gate does.
"""
from __future__ import annotations

import math
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from ..logging_setup import get_logger
from ..venues import venue_for

log = get_logger(__name__)

Bars = tuple[Sequence[float], Sequence[float]]      # (opens, closes), oldest first


@dataclass(frozen=True)
class GapCheck:
    """One evaluation. ``reason`` set = reject; else ``max_shares`` is the fit and ``gap_usd`` the loss at ``shares``."""

    reason: str | None
    max_shares: float | None
    summary: str
    observed: dict[str, Any]

    def gap_usd(self, shares: float, entry: float) -> float:
        return shares * entry * float(self.observed.get("loss_fraction", 0.0))


def adverse_gaps(opens: Sequence[float], closes: Sequence[float]) -> list[float]:
    """``1 - open[t] / close[t-1]``, positive when the open is below the previous close."""
    out = []
    for prev, op in zip(closes[:-1], opens[1:], strict=True):
        if prev > 0 and op > 0 and math.isfinite(prev) and math.isfinite(op):
            out.append(1.0 - op / prev)
    return out


def gap_quantile(gaps: Sequence[float], level: float) -> float:
    """The ``level`` quantile of adverse gaps, never below zero (a gap up is no loss to a long)."""
    ordered = sorted(gaps)
    pos = level * (len(ordered) - 1)
    lo, hi = math.floor(pos), math.ceil(pos)
    q = ordered[lo] + (ordered[hi] - ordered[lo]) * (pos - lo)
    return max(q, 0.0)


def summary_text(level: float, loss_fraction: float, gap_usd: float, budget_usd: float) -> str:
    """Shared with ``trading audit replay`` so a replay reproduces it."""
    return (f"{level:.0%} adverse gap {loss_fraction:.2%} of notional, ${gap_usd:,.0f} "
            f"vs per-trade budget ${budget_usd:,.0f}")


def short_reason(n: int, symbol: str, min_gaps: int) -> str:
    return f"gap risk: {n} session gaps for {symbol}, need {min_gaps}"


def unavailable_reason(symbol: str, error: str) -> str:
    return f"gap risk unavailable for {symbol}: {error}"


class GapRiskGate:
    """Router gate. ``bars_for(symbol)`` returns daily (opens, closes), oldest first."""

    def __init__(self, bars_for: Callable[[str], Bars], *, risk_pct: float, level: float = 0.99,
                 min_gaps: int = 250) -> None:
        if not 0.0 < risk_pct < 1.0:
            raise ValueError("risk_pct is a fraction of equity in (0, 1)")
        if not 0.5 < level < 1.0:
            raise ValueError("level must be in (0.5, 1)")
        self.bars_for = bars_for
        self.risk_pct = risk_pct
        self.level = level
        self.min_gaps = int(min_gaps)
        self._cache: dict[tuple[str, int, float], tuple[float, int]] = {}

    def thresholds(self) -> dict[str, Any]:
        return {"risk_pct": self.risk_pct, "level": self.level, "min_gaps": self.min_gaps}

    def _loss(self, symbol: str) -> tuple[float, int]:
        opens, closes = self.bars_for(symbol)
        key = (symbol, len(closes), float(closes[-1]) if len(closes) else 0.0)
        if key not in self._cache:
            gaps = adverse_gaps(opens, closes)
            self._cache = {k: v for k, v in self._cache.items() if k[0] != symbol}
            self._cache[key] = (gap_quantile(gaps, self.level) if len(gaps) >= self.min_gaps else -1.0, len(gaps))
        return self._cache[key]

    def check(self, symbol: str, *, shares: float, entry: float, equity: float) -> GapCheck:
        if venue_for(symbol)[0].always_open:
            return GapCheck(None, None, "", {"exempt": "venue never closes"})
        try:
            loss, n = self._loss(symbol)
        except Exception as e:                       # fetch failure: fail closed
            log.warning("gap_risk.unavailable", symbol=symbol, error=str(e))
            return GapCheck(unavailable_reason(symbol, str(e)), None, "", {"error": str(e)})
        if loss < 0:
            return GapCheck(short_reason(n, symbol, self.min_gaps), None, "", {"n_gaps": n})
        budget = self.risk_pct * equity
        per_unit = entry * loss
        max_shares = budget / per_unit if per_unit > 0 else shares
        observed = {"loss_fraction": loss, "n_gaps": n, "budget_usd": budget,
                    "gap_usd": shares * per_unit, "max_shares": max_shares}
        return GapCheck(None, max_shares, summary_text(self.level, loss, shares * per_unit, budget), observed)


def bars_from_broker(broker: Any, *, days: int = 1100) -> Callable[[str], Bars]:
    def bars_for(symbol: str) -> Bars:
        end = datetime.now(UTC)
        bars = sorted(broker.candles(symbol, end - timedelta(days=days), end, "OneDay"), key=lambda b: b.start)
        return [float(b.open) for b in bars], [float(b.close) for b in bars]
    return bars_for


def attach_gap_gate(router: Any, broker: Any, *, level: float | None, risk_pct: float,
                    min_gaps: int = 250) -> GapRiskGate | None:
    """Turn the gate on for ``router``; ``level`` None leaves it off."""
    if level is None:
        return None
    gate = GapRiskGate(bars_from_broker(broker), risk_pct=risk_pct, level=level, min_gaps=min_gaps)
    router.gap_gate = gate
    return gate
