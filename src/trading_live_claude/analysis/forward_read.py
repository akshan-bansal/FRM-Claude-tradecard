"""Evidence for forward-read sizing: does a signal's past net edge say anything about its next trade?

Phases 0 to 2 of ``POSITION_SIZING_SCOPE.md``. A read for a trade uses only trades that closed at least
``purge_days`` before it opened, and is shrunk from the strategy-and-symbol cell towards the strategy, then zero, so a
thin cell reads near no edge. :func:`skill` scores reads against realized net returns, with a null that rotates reads
through time so that any drift they share with returns is not counted as skill.

Research only. Nothing on the trading path imports it, and a read is never a strategy feature.
"""
from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta

import numpy as np
from scipy.stats import spearmanr


@dataclass(frozen=True)
class TradeRow:
    strategy: str
    symbol: str
    entry: datetime
    exit: datetime
    net_return: float
    bars: int


@dataclass(frozen=True)
class Read:
    trade: TradeRow
    edge: float
    n_cell: int          # earlier closed trades of this strategy on this symbol
    n_strategy: int      # earlier closed trades of this strategy on any symbol


@dataclass(frozen=True)
class Skill:
    n: int
    rank_ic: float
    p_value: float       # one-sided, rotation null
    bucket_means: tuple[float, ...]   # mean net return from the lowest read to the highest


def net_return(gross: float, *, per_side_bps: float) -> float:
    """The engine's ``pnl_pct`` is gross, so the round trip is charged here."""
    return gross - 2.0 * per_side_bps / 10_000.0


def causal_reads(trades: Sequence[TradeRow], *, shrink: float = 10.0, purge_days: float = 5.0,
                 min_strategy: int = 5) -> list[Read]:
    """A read per trade that has enough earlier evidence. Trades without it get none rather than a guess."""
    ordered = sorted(trades, key=lambda t: (t.entry, t.strategy, t.symbol))
    purge = timedelta(days=purge_days)
    out: list[Read] = []
    for t in ordered:
        known = [p for p in ordered if p.strategy == t.strategy and p.exit + purge <= t.entry]
        if len(known) < min_strategy:
            continue
        cell = [p.net_return for p in known if p.symbol == t.symbol]
        pool = [p.net_return for p in known]
        parent = float(np.mean(pool)) * len(pool) / (len(pool) + shrink)
        edge = (sum(cell) + shrink * parent) / (len(cell) + shrink)
        out.append(Read(t, edge, len(cell), len(pool)))
    return out


def skill(reads: Sequence[Read], *, buckets: int = 5, n_rotations: int = 2000, seed: int = 7) -> Skill | None:
    """Spearman of read against net return, against every circular shift of the reads in time order."""
    if len(reads) < max(buckets * 2, 30):
        return None
    ordered = sorted(reads, key=lambda r: r.trade.entry)
    edge = np.array([r.edge for r in ordered])
    realized = np.array([r.trade.net_return for r in ordered])
    observed = float(spearmanr(edge, realized).statistic)
    rng = np.random.default_rng(seed)
    shifts = rng.integers(1, len(edge), n_rotations)
    null = np.array([spearmanr(np.roll(edge, int(s)), realized).statistic for s in shifts])
    p = float((1 + np.sum(null >= observed)) / (n_rotations + 1))
    groups = np.array_split(np.argsort(edge, kind="stable"), buckets)
    return Skill(len(edge), observed, p, tuple(float(realized[g].mean()) for g in groups))
