"""Pattern-first selection: find a composite pattern, then confirm it statistically, inside each fold.

The universe sweep's default order is statistics first: grid-search returns in-sample, then walk-forward
the winner. This order reverses it. On each walk-forward fold:

1. **Pattern stage, training window only.** Every parameter combination in ``PARAM_GRIDS`` is a
   detector, scored against forward-move labels (``label_events``) built from the training bars alone,
   so the last ``horizon`` bars of a window are unlabelled rather than peeking into the test block. A
   detector qualifies when a one-sided binomial test puts its precision above the window's base rate,
   Bonferroni-corrected for every combination tried there. The most precise qualifiers are joined in
   :class:`CompositeStrategy`, and the :class:`Scorer`'s agreement threshold is calibrated on the
   same window.
2. **Confirmation stage.** That one pattern, unchanged, is backtested on the following test block, with
   its training bars as warm-up and unscored. Its win rate, trade count and score are what the sweep's
   tier rules judge.

Nothing found in a window is carried to another, so the out-of-sample figures never see data the
pattern was chosen on. A fold with no detector above its base rate is counted, not skipped silently.

The binomial test treats bars as independent. Forward-move labels overlap across ``horizon`` bars, so it
overstates significance; the out-of-sample confirmation, not this test, is the evidence.
"""
from __future__ import annotations

import itertools
from collections import Counter
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

import numpy as np
import pandas as pd
from scipy import stats

from ..analysis.classification import confusion
from ..analysis.labeling import label_events
from ..backtest import BacktestEngine
from ..optimize import PARAM_GRIDS, _valid
from ..scoring.objective import ObjectiveAdapter, ObjectiveInput
from ..scoring.scorer import Scorer, calibrate_threshold
from ..strategies import STRATEGIES
from ..strategies.base import Strategy, StrategyContext
from ..strategies.composite import CompositeStrategy

OBJECTIVE = ObjectiveAdapter.from_name("sortino_over_dd")


@dataclass(frozen=True)
class Detector:
    name: str
    params: tuple[tuple[str, Any], ...]
    precision: float
    recall: float
    fired: int

    def label(self) -> str:
        return f"{self.name}({', '.join(f'{k}={v}' for k, v in self.params)})"


@dataclass(frozen=True)
class Pattern:
    members: tuple[Detector, ...]
    tau: float
    precision: float
    recall: float
    base_rate: float

    def key(self) -> str:
        return f"{' + '.join(d.label() for d in self.members)} @ tau {self.tau:.2f}"


class GatedComposite(Strategy):
    """A pattern as a tradeable strategy: the members' union, entered only at ``score >= tau``."""

    name = "pattern"

    def __init__(self, pattern: Pattern, registry: Mapping[str, type[Strategy]] = STRATEGIES) -> None:
        super().__init__()
        self.pattern = pattern
        self._composite = CompositeStrategy([registry[d.name](**dict(d.params)) for d in pattern.members])
        self._scorer = Scorer()

    def required_history_bars(self) -> int:
        return self._composite.required_history_bars()

    def generate_signals(self, df: pd.DataFrame, ctx: StrategyContext) -> pd.DataFrame:
        sig = self._composite.generate_signals(df, ctx)
        sig["entry"] = self._scorer.gate(sig, self.pattern.tau).astype(int)
        return sig


def param_combos(grid: Mapping[str, list[Any]]) -> list[dict[str, Any]]:
    keys = list(grid)
    return [p for p in (dict(zip(keys, c, strict=True)) for c in itertools.product(*grid.values())) if _valid(p)]


def find_pattern(train: pd.DataFrame, symbol: str, *, horizon: int = 10, up_threshold: float = 0.03,
                 max_members: int = 3, min_fired: int = 10, min_recall: float = 0.1, alpha: float = 0.05,
                 grids: Mapping[str, Mapping[str, list[Any]]] = PARAM_GRIDS,
                 registry: Mapping[str, type[Strategy]] = STRATEGIES) -> Pattern | None:
    """The best composite pattern in ``train``, or None when no detector beats the base rate."""
    labels = label_events(train, horizon=horizon, up_threshold=up_threshold)
    known = labels.dropna()
    if known.empty:
        return None
    base = float(known.astype(float).mean())
    ctx = StrategyContext(symbol=symbol)
    found: list[tuple[float, Detector]] = []
    tried = 0
    for name, grid in grids.items():
        for p in param_combos(grid):
            try:
                sig = registry[name](**p).generate_signals(train, ctx)
            except Exception:
                continue
            tried += 1
            rep = confusion(sig["entry"], labels)
            fired = rep.tp + rep.fp
            if fired < min_fired or rep.precision <= base:
                continue
            p_value = float(stats.binomtest(rep.tp, fired, base, alternative="greater").pvalue)
            found.append((p_value, Detector(name, tuple(sorted(p.items())), rep.precision, rep.recall, fired)))
    best: dict[str, Detector] = {}
    for p_value, d in found:
        if p_value <= alpha / max(tried, 1) and (d.name not in best or d.precision > best[d.name].precision):
            best[d.name] = d
    if not best:
        return None
    members = tuple(sorted(best.values(), key=lambda d: d.precision, reverse=True)[:max_members])
    staged = Pattern(members, 0.0, 0.0, 0.0, base)
    sig = GatedComposite(staged, registry)._composite.generate_signals(train, ctx)
    choice = calibrate_threshold(Scorer(), sig, labels, min_recall=min_recall)
    return Pattern(members, choice.tau, choice.precision, choice.recall, base)


def _score(engine: BacktestEngine, strategy: Strategy, df: pd.DataFrame, symbol: str,
           warmup_bars: int = 0) -> tuple[float, Any] | None:
    try:
        res = engine.run(strategy, df, symbol, warmup_bars=warmup_bars)
    except Exception:
        return None
    return OBJECTIVE.score(ObjectiveInput.from_metrics(res.metrics)), res.metrics


def pattern_walk_forward(df: pd.DataFrame, symbol: str, engine: BacktestEngine, *, train_bars: int,
                         test_bars: int, step_bars: int, **pattern_kw: Any) -> dict[str, Any] | None:
    """Find on each training window, confirm on the block after it. Same output keys as the sweep's walk-forward."""
    oos_scores: list[float] = []
    is_scores: list[float] = []
    rets: list[float] = []
    dds: list[float] = []
    keys: list[str] = []
    wins, trades, empty, start = 0.0, 0, 0, 0
    while start + train_bars + test_bars <= len(df):
        train = df.iloc[start:start + train_bars].reset_index(drop=True)
        fold = df.iloc[start:start + train_bars + test_bars].reset_index(drop=True)
        start += step_bars
        pattern = find_pattern(train, symbol, **pattern_kw)
        if pattern is None:
            empty += 1
            continue
        strategy = GatedComposite(pattern, pattern_kw.get("registry", STRATEGIES))
        ins = _score(engine, strategy, train, symbol)
        oos = _score(engine, strategy, fold, symbol, warmup_bars=train_bars)
        if ins is None or oos is None:
            empty += 1
            continue
        keys.append(pattern.key())
        is_scores.append(ins[0])
        oos_scores.append(oos[0])
        rets.append(oos[1].total_return)
        dds.append(oos[1].max_drawdown)
        trades += oos[1].num_trades
        wins += float(oos[1].win_rate) * oos[1].num_trades
    if not oos_scores:
        return None
    modal, count = Counter(keys).most_common(1)[0]
    oos_mean, is_mean = float(np.mean(oos_scores)), float(np.mean(is_scores))
    return {"oos_score": oos_mean, "wfe": oos_mean / is_mean if is_mean > 0 else 0.0,
            "oos_return": float(np.mean(rets)), "oos_maxdd": float(np.min(dds)),
            "oos_trades": trades, "oos_win_rate": wins / trades if trades else 0.0,
            "folds": len(oos_scores), "folds_without_pattern": empty,
            "modal_pattern": modal, "pattern_stability": count / len(keys),
            "train_bars": train_bars, "test_bars": test_bars}
