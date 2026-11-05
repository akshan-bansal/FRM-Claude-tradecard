"""Do composite agreement and the Greek probability help as an entry filter, or as an exit?

Both tests compare a rule with the trades a strategy took anyway, net of cost.

* **Entry filter.** An input becomes a percentile against earlier trades on the same symbol, and a trade is skipped
  below a threshold. The threshold, which may be zero (no filter), comes only from trades that had already closed.
  The null rotates the take-or-skip flags through time inside each symbol.
* **Exit.** A score known at one close exits at the next close, and can only shorten a trade. The threshold, which may
  be never, again comes only from closed trades. The null flips the sign of the gain, a calendar month at a time.

Research only. Nothing on the trading path imports it. Short trades are not simulated.
"""
from __future__ import annotations

import math
from bisect import bisect_right
from collections import defaultdict
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta

import numpy as np

ENTRY_GRID = (0.0, 0.25, 0.5, 0.75)
EXIT_GRIDS: Mapping[str, tuple[float, ...]] = {"greek": (0.6, 0.7, 0.8, 0.9), "composite": (0.1, 0.2, 0.3, 0.5)}
MIN_TRAIN = 30
MIN_SCORED = 30
MIN_HISTORY = 10


@dataclass(frozen=True)
class PathTrade:
    strategy: str
    symbol: str
    entry: datetime
    exit: datetime
    net_return: float
    entry_inputs: Mapping[str, float | None]
    closes: tuple[float, ...]                       # entry close through exit close
    exit_scores: Mapping[str, tuple[float, ...]]    # per close; higher means leave


@dataclass(frozen=True)
class Comparison:
    test: str
    strategy: str
    input: str
    n: int
    changed: float            # share of trades skipped, or exited early
    mean_base: float
    mean_rule: float
    total_base: float
    total_rule: float
    drawdown_base: float
    drawdown_rule: float
    later_half_gain: float    # rule minus base, mean per trade, later half
    symbols_up: int
    symbols: int
    p_value: float

    def passes(self, alpha: float) -> bool:
        return (self.p_value < alpha and self.mean_rule > self.mean_base and self.later_half_gain > 0
                and self.total_rule >= self.total_base)


def simulate_exit(closes: Sequence[float], scores: Sequence[float], threshold: float, cost: float) -> float:
    """Net return if a score at or above ``threshold`` at one close exits at the next. Never holds past the real exit."""
    last = len(closes) - 1
    leave = next((j + 1 for j in range(1, last) if scores[j] >= threshold), last)
    return closes[leave] / closes[0] - 1.0 - cost


def causal_percentiles(trades: Sequence[PathTrade], name: str) -> dict[int, float]:
    """A trade's input as a share of earlier values on its symbol. Too little history gets no percentile."""
    by_symbol: dict[str, list[PathTrade]] = defaultdict(list)
    for t in trades:
        if t.entry_inputs.get(name) is not None:
            by_symbol[t.symbol].append(t)
    out: dict[int, float] = {}
    for rows in by_symbol.values():
        rows.sort(key=lambda t: t.entry)
        entries = [t.entry for t in rows]
        for t in rows:
            earlier = sorted(float(p.entry_inputs[name]) for p in rows[:bisect_right(entries, t.entry)]  # type: ignore[arg-type]
                             if p.entry < t.entry)
            if len(earlier) >= MIN_HISTORY:
                out[id(t)] = bisect_right(earlier, float(t.entry_inputs[name])) / len(earlier)  # type: ignore[arg-type]
    return out


def _drawdown(returns: Sequence[float]) -> float:
    curve = np.cumsum(returns)
    return float(np.max(np.maximum.accumulate(np.r_[0.0, curve]) - np.r_[0.0, curve]))


def _symbols_up(rows: Sequence[PathTrade], rule: Sequence[float]) -> tuple[int, int]:
    gain: dict[str, float] = defaultdict(float)
    for t, r in zip(rows, rule, strict=True):
        gain[t.symbol] += r - t.net_return
    return sum(v > 0 for v in gain.values()), len(gain)


def _rotation_p(rows: Sequence[PathTrade], take: np.ndarray, observed: float, n: int, seed: int) -> float:
    returns = np.array([t.net_return for t in rows])
    groups: dict[str, list[int]] = defaultdict(list)
    for i, t in enumerate(rows):
        groups[t.symbol].append(i)
    rng = np.random.default_rng(seed)
    hits = 0
    for _ in range(n):
        flags = take.copy()
        for idx in groups.values():
            if len(idx) > 1:
                flags[idx] = np.roll(take[idx], int(rng.integers(1, len(idx))))
        if flags.any() and returns[flags].mean() - returns.mean() >= observed:
            hits += 1
    return (1 + hits) / (n + 1)


def _sign_flip_p(rows: Sequence[PathTrade], diffs: np.ndarray, n: int, seed: int) -> float:
    months: dict[tuple[int, int], float] = defaultdict(float)
    for t, d in zip(rows, diffs, strict=True):
        months[(t.entry.year, t.entry.month)] += float(d)
    totals = np.array(list(months.values()))
    signs = np.random.default_rng(seed).choice((-1.0, 1.0), size=(n, len(totals)))
    return float((1 + np.sum((signs * totals).sum(axis=1) >= totals.sum())) / (n + 1))


def evaluate_entry(trades: Sequence[PathTrade], name: str, strategy: str, *, purge_days: float = 5.0,
                   n_null: int = 1000, seed: int = 7) -> Comparison | None:
    """``trades`` is the whole venue, so percentiles can read every earlier trade on a symbol."""
    pct = causal_percentiles(trades, name)
    rows = sorted((t for t in trades if t.strategy == strategy and id(t) in pct), key=lambda t: t.entry)
    purge = timedelta(days=purge_days)
    scored: list[PathTrade] = []
    take: list[bool] = []
    for i, t in enumerate(rows):
        known = [p for p in rows[:i] if p.exit + purge <= t.entry]
        if len(known) < MIN_TRAIN:
            continue
        best, best_mean = 0.0, -math.inf
        for th in ENTRY_GRID:
            kept = [p.net_return for p in known if pct[id(p)] >= th]
            if len(kept) >= MIN_HISTORY and np.mean(kept) > best_mean:
                best, best_mean = th, float(np.mean(kept))
        scored.append(t)
        take.append(pct[id(t)] >= best)
    if len(scored) < MIN_SCORED:
        return None
    flags = np.array(take)
    base = np.array([t.net_return for t in scored])
    rule = base * flags
    observed = float(base[flags].mean() - base.mean()) if flags.any() else -math.inf
    half = len(scored) // 2
    later = base[half:]
    later_flags = flags[half:]
    later_gain = float(later[later_flags].mean() - later.mean()) if later_flags.any() else 0.0
    up, symbols = _symbols_up(scored, rule)
    p = 1.0 if flags.all() or not flags.any() else _rotation_p(scored, flags, observed, n_null, seed)
    return Comparison("entry", strategy, name, len(scored), float(1.0 - flags.mean()), float(base.mean()),
                      float(base[flags].mean()) if flags.any() else 0.0, float(base.sum()), float(rule.sum()),
                      _drawdown(base), _drawdown(rule), later_gain, up, symbols, p)


def evaluate_exit(trades: Sequence[PathTrade], name: str, strategy: str, *, cost: float, purge_days: float = 5.0,
                  n_null: int = 5000, seed: int = 7) -> Comparison | None:
    rows = sorted((t for t in trades if t.strategy == strategy and name in t.exit_scores and len(t.closes) >= 3),
                  key=lambda t: t.entry)
    grid = EXIT_GRIDS[name]
    outcomes = [{th: simulate_exit(t.closes, t.exit_scores[name], th, cost) for th in grid} for t in rows]
    purge = timedelta(days=purge_days)
    scored: list[PathTrade] = []
    rule: list[float] = []
    for i, t in enumerate(rows):
        known = [j for j in range(i) if rows[j].exit + purge <= t.entry]
        if len(known) < MIN_TRAIN:
            continue
        best, best_mean = None, float(np.mean([rows[j].net_return for j in known]))   # "never" must be beaten
        for th in grid:
            mean = float(np.mean([outcomes[j][th] for j in known]))
            if mean > best_mean:
                best, best_mean = th, mean
        scored.append(t)
        rule.append(t.net_return if best is None else outcomes[i][best])
    if len(scored) < MIN_SCORED:
        return None
    base = np.array([t.net_return for t in scored])
    after = np.array(rule)
    diffs = after - base
    half = len(scored) // 2
    up, symbols = _symbols_up(scored, rule)
    p = 1.0 if not np.any(diffs) else _sign_flip_p(scored, diffs, n_null, seed)
    return Comparison("exit", strategy, name, len(scored), float(np.mean(diffs != 0)), float(base.mean()),
                      float(after.mean()), float(base.sum()), float(after.sum()), _drawdown(base), _drawdown(after),
                      float(diffs[half:].mean()), up, symbols, p)


__all__ = ["Comparison", "PathTrade", "causal_percentiles", "evaluate_entry", "evaluate_exit", "simulate_exit"]
