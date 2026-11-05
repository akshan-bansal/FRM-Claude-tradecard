"""Strategy calibration: does each input turn into an honest probability that the next trade wins, net of costs?

Three inputs are scored for each strategy, walk-forward, against that strategy's own prior win rate:

* **Composite:** how many of a panel of strategies fired an entry on the bar before this one.
* **Greek:** ``BSMDigital.probability``, the chance of finishing above the round-trip breakeven over the strategy's
  usual holding time, at volatility known before entry. No skew and no carry are assumed.
* **Graph:** the confidence-weighted ``sentiment_on`` reading in the 24 hours before entry. Its history is short, so
  this input usually reports too few trades, and says so.

A raw input is mapped to a probability by the engine's ``fit_probit``, fitted only on trades closed at least
``purge_days`` before the one being scored. Positive Brier skill means better than quoting the prior win rate.

Research only. Nothing on the trading path imports it.
"""
from __future__ import annotations

import math
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

import numpy as np
import pandas as pd
from scipy.stats import spearmanr

from trading_live_claude.intel.graph import Edge
from trading_live_claude.risk.spectral_var import load_engine

MIN_TRAIN = 30
MIN_SCORED = 30
VOL_BARS = 63


@dataclass(frozen=True)
class CalibrationTrade:
    strategy: str
    symbol: str
    entry: datetime
    exit: datetime
    net_return: float
    composite: float | None
    greek: float | None
    graph: float | None


@dataclass(frozen=True)
class InputScore:
    strategy: str
    input: str
    n: int
    brier: float | None
    brier_base: float | None
    skill: float | None      # 1 - brier / brier_base; None when too few trades were scored
    rank_ic: float | None
    p_value: float | None    # one-sided, circular shifts of the input in time order


def prior_vol(close: pd.Series, i: int, periods: float) -> float | None:
    """Annualized volatility of the returns that closed before bar ``i``."""
    rets = np.log(close.iloc[max(0, i - VOL_BARS - 1):i]).diff().dropna()
    if len(rets) < 20:
        return None
    vol = float(rets.std(ddof=0)) * math.sqrt(periods)
    return vol if vol > 0 else None


def greek_probability(vol: float, hold_bars: float, periods: float, round_trip_cost: float) -> float:
    """P(price ends above breakeven) from the BSM digital, in units of the entry price."""
    be = load_engine()
    model = be.BSMDigital(strike=1.0 + round_trip_cost, expiry=max(hold_bars, 1.0) / periods, rate=0.0, dividend=0.0,
                          skew=0.0)
    return float(model.probability(1.0, vol))


def graph_reading(edges: Iterable[Edge], symbol: str, entry: datetime, *, hours: float = 24.0) -> float | None:
    """Confidence-weighted sentiment known before ``entry``, or None when nothing was scored in the window."""
    start = entry - timedelta(hours=hours)
    total = weighted = 0.0
    for e in edges:
        if e.predicate != "sentiment_on" or e.object != ("symbol", symbol):
            continue
        at = pd.Timestamp(e.as_of).to_pydatetime()
        if start <= at < entry:
            total += float(e.weight)
            weighted += float(e.weight) * float(e.meta["score"])
    return weighted / total if total > 0 else None


def _to_prob(raw: float, name: str) -> float:
    if name == "graph":          # a reading in [-1, 1]
        return min(0.95, max(0.05, (raw + 1.0) / 2.0))
    return min(0.95, max(0.05, raw))


def score_input(trades: Sequence[CalibrationTrade], name: str, strategy: str, *, purge_days: float = 5.0,
                refit_every: int = 10, n_rotations: int = 500, seed: int = 7) -> InputScore:
    """Walk-forward Brier skill of one input for one strategy."""
    be = load_engine()
    rows = sorted((t for t in trades if getattr(t, name) is not None), key=lambda t: t.entry)
    purge = timedelta(days=purge_days)
    preds: list[float] = []
    bases: list[float] = []
    ys: list[float] = []
    xs: list[float] = []
    theta: Any = None
    fitted_at = -refit_every
    for i, t in enumerate(rows):
        known = [p for p in rows[:i] if p.exit + purge <= t.entry]
        if len(known) < MIN_TRAIN:
            continue
        y_known = np.array([p.net_return > 0 for p in known], float)
        if theta is None or i - fitted_at >= refit_every:
            theta = be.fit_probit([_to_prob(getattr(p, name), name) for p in known], y_known)
            fitted_at = i
        preds.append(float(be.apply_probit(_to_prob(getattr(t, name), name), theta)))
        bases.append(float(y_known.mean()))
        ys.append(float(t.net_return > 0))
        xs.append(float(getattr(t, name)))
    if len(ys) < MIN_SCORED:
        return InputScore(strategy, name, len(ys), None, None, None, None, None)
    y = np.array(ys)
    brier = float(np.mean((np.array(preds) - y) ** 2))
    base = float(np.mean((np.array(bases) - y) ** 2))
    x = np.array(xs)
    returns = np.array([t.net_return for t in rows[-len(ys):]])
    if np.ptp(x) == 0:
        return InputScore(strategy, name, len(ys), brier, base, 1.0 - brier / base if base else None, None, None)
    ic = float(spearmanr(x, returns).statistic)
    rng = np.random.default_rng(seed)
    null = [spearmanr(np.roll(x, int(s)), returns).statistic for s in rng.integers(1, len(x), n_rotations)]
    p = float((1 + np.sum(np.array(null) >= ic)) / (n_rotations + 1))
    return InputScore(strategy, name, len(ys), brier, base, 1.0 - brier / base if base else None, ic, p)


__all__ = ["CalibrationTrade", "InputScore", "graph_reading", "greek_probability", "prior_vol", "score_input"]
