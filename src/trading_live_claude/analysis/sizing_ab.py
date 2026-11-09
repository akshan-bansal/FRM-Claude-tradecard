"""Phase 3 of ``POSITION_SIZING_SCOPE.md``: the same trades, sized two ways, on one shared book.

Arm A is today's sizing: a volatility target (``target_vol / annual_vol``, capped per position), with the
shared budget scaled pro rata when a day's entries exceed it. Arm B multiplies the same base by a tilt from
the forward read and, when the budget binds, fills it from the highest read first. Both arms take exactly
the same trades on the same days, so a difference can only come from the sizes.

Research, plus ``tilt_for``, which the phase 4 shadow (``monitor/forward_read_shadow.py``) uses to log a size.
"""
from __future__ import annotations

import math
from collections.abc import Sequence
from dataclasses import dataclass, field

import numpy as np
import pandas as pd

from ..backtest.metrics import compute_metrics
from ..scoring.objective import ObjectiveInput, sortino_over_dd


@dataclass(frozen=True)
class SizedTrade:
    strategy: str
    symbol: str
    entry: pd.Timestamp                 # entered at this day's close
    days: tuple[pd.Timestamp, ...]      # each held day after entry, the last being the exit
    returns: tuple[float, ...]          # gross return of each held day; they compound to the trade's gross
    round_trip_cost: float
    vol: float | None                   # annualized, from bars before entry
    edge: float | None                  # forward read; None when the trade had too little evidence


@dataclass(frozen=True)
class Rules:
    target_vol: float = 0.15
    max_position: float = 1.0           # the sizer's max_leverage, per position
    max_gross: float = 1.0              # shared budget: gross exposure as a share of equity
    tilt_cap: float = 2.0
    weak_read: float = 0.5              # a read with edge <= 0 takes this share of the base (decision 1)


@dataclass
class ArmResult:
    name: str
    equity: pd.Series
    returns: pd.Series
    exposure: pd.Series
    traded: float                       # sum of entry and exit notional, as a share of equity at the time
    costs: float                        # sum of costs, as a share of equity at the time
    entry_days: int = 0
    bound_days: int = 0
    skipped: int = 0                    # trades with no volatility estimate, unsized in both arms
    weights: list[float] = field(default_factory=list)


def base_weight(vol: float | None, rules: Rules) -> float:
    return 0.0 if not vol or vol <= 0 else min(rules.target_vol / vol, rules.max_position)


def tilts(trades: Sequence[SizedTrade], rules: Rules) -> list[float]:
    """Tilt per trade. The reference ratio is the median positive read-to-risk of reads made before it."""
    order = sorted(range(len(trades)), key=lambda i: trades[i].entry)
    out = [1.0] * len(trades)
    seen: list[float] = []
    pending: list[tuple[pd.Timestamp, float]] = []
    for i in order:
        t = trades[i]
        while pending and pending[0][0] < t.entry:         # reads strictly earlier join the reference
            seen.append(pending.pop(0)[1])
        if t.edge is None or not t.vol:
            continue
        out[i] = tilt_for(t.edge, t.vol, float(np.median(seen)) if seen else None, rules)
        if t.edge > 0:
            pending.append((t.entry, t.edge / t.vol))
    return out


def tilt_for(edge: float | None, vol: float | None, reference: float | None, rules: Rules) -> float:
    """The arm-B multiplier on today's size: 1.0 without a read or a reference, ``weak_read`` for edge <= 0."""
    if edge is None or not vol:
        return 1.0
    if edge <= 0:
        return rules.weak_read
    if not reference:
        return 1.0
    return float(np.clip((edge / vol) / reference, 0.0, rules.tilt_cap))


def simulate(trades: Sequence[SizedTrade], weights: Sequence[float], *, rank: Sequence[float] | None,
             rules: Rules, name: str) -> ArmResult:
    """Mark the book at each close. ``rank`` None scales a binding budget pro rata; otherwise fills it by rank."""
    by_entry: dict[pd.Timestamp, list[int]] = {}
    for i, t in enumerate(trades):
        by_entry.setdefault(t.entry, []).append(i)
    days = sorted({d for t in trades for d in (t.entry, *t.days)})
    cash, equity_prev = 1.0, 1.0
    open_: dict[int, list[float | int]] = {}         # trade -> [value, next held-day index]
    eq, ret, expo = [], [], []
    res = ArmResult(name, pd.Series(dtype=float), pd.Series(dtype=float), pd.Series(dtype=float), 0.0, 0.0)
    for d in days:
        for i, pos in list(open_.items()):
            t, k = trades[i], int(pos[1])
            if k < len(t.days) and t.days[k] == d:
                pos[0] = float(pos[0]) * (1.0 + t.returns[k])
                pos[1] = k + 1
        equity = cash + sum(float(p[0]) for p in open_.values())
        for i in [i for i, p in open_.items() if int(p[1]) == len(trades[i].days)]:
            value = float(open_.pop(i)[0])
            fee = value * trades[i].round_trip_cost / 2.0
            cash += value - fee
            res.traded += value / equity
            res.costs += fee / equity
        equity = cash + sum(float(p[0]) for p in open_.values())
        new = [i for i in by_entry.get(d, []) if trades[i].days]
        if new:
            want = {i: weights[i] * equity for i in new}
            headroom = max(0.0, rules.max_gross * equity - sum(float(p[0]) for p in open_.values()))
            total = sum(want.values())
            res.entry_days += 1
            if total > headroom + 1e-12:
                res.bound_days += 1
                if rank is None:
                    want = {i: w * headroom / total for i, w in want.items()}
                else:
                    left, filled = headroom, {}
                    for i in sorted(new, key=lambda j: (-rank[j], trades[j].symbol, trades[j].strategy)):
                        filled[i] = min(want[i], left)
                        left -= filled[i]
                    want = filled
            for i, notional in want.items():
                if notional <= 0:
                    continue
                fee = notional * trades[i].round_trip_cost / 2.0
                cash -= notional + fee
                open_[i] = [notional, 0]
                res.traded += notional / equity
                res.costs += fee / equity
                res.weights.append(notional / equity)
            equity = cash + sum(float(p[0]) for p in open_.values())
        eq.append(equity)
        ret.append(equity / equity_prev - 1.0)
        expo.append(sum(float(p[0]) for p in open_.values()) / equity if equity > 0 else 0.0)
        equity_prev = equity
    idx = pd.DatetimeIndex(days)
    res.equity, res.returns, res.exposure = pd.Series(eq, idx), pd.Series(ret, idx), pd.Series(expo, idx)
    res.skipped = sum(1 for w in weights if w <= 0)
    return res


def objective(returns: pd.Series, periods: int, n_trades: int) -> float:
    equity = (1.0 + returns).cumprod()
    m = compute_metrics(equity, returns, [], returns != 0, bars_per_year=periods)
    return sortino_over_dd(ObjectiveInput(sharpe=m.sharpe, max_drawdown=m.max_drawdown, sortino=m.sortino,
                                          num_trades=n_trades))


def summary(r: ArmResult, periods: int, n_trades: int) -> dict[str, float]:
    m = compute_metrics(r.equity, r.returns, [], r.exposure > 0, bars_per_year=periods)
    years = max(len(r.returns) / periods, 1e-9)
    return {"sortino_over_dd": objective(r.returns, periods, n_trades), "sortino": m.sortino,
            "total_return": m.total_return, "cagr": m.cagr, "max_drawdown": m.max_drawdown,
            "time_in_market": float((r.exposure > 0).mean()), "mean_gross": float(r.exposure.mean()),
            "turnover_per_year": r.traded / years, "cost_drag_per_year": r.costs / years,
            "budget_bound_share": r.bound_days / r.entry_days if r.entry_days else 0.0}


def month_block_p(a: pd.Series, b: pd.Series, periods: int, n_trades: int, *, n_boot: int = 2000,
                  seed: int = 7) -> tuple[float, float]:
    """Share of month-block resamples (same months for both arms) in which B does not beat A."""
    frame = pd.DataFrame({"a": a, "b": b})
    months = [g for _, g in frame.groupby([frame.index.year, frame.index.month])]
    rng = np.random.default_rng(seed)
    diffs = []
    for _ in range(n_boot):
        pick = pd.concat([months[j] for j in rng.integers(0, len(months), len(months))], ignore_index=True)
        diffs.append(objective(pick["b"], periods, n_trades) - objective(pick["a"], periods, n_trades))
    d = np.array(diffs)
    return float((1 + np.sum(d <= 0)) / (n_boot + 1)), float(np.median(d))


def held_returns(closes: Sequence[float], gross: float) -> tuple[float, ...]:
    """Close-to-close returns of each held day, with the last one set so they compound to ``gross``."""
    path = [closes[k] / closes[k - 1] - 1.0 for k in range(1, len(closes))]
    if not path:
        return ()
    before = math.prod(1.0 + x for x in path[:-1])
    path[-1] = (1.0 + gross) / before - 1.0
    return tuple(path)
