"""Pattern-first selection: what the pattern stage picks, and that it only ever sees the training window."""
from __future__ import annotations

from typing import Any

import numpy as np
import pandas as pd
import pytest

from trading_live_claude.analysis import pattern_first as pf
from trading_live_claude.backtest import BacktestEngine
from trading_live_claude.strategies.base import Strategy, StrategyContext


def _frame(n: int = 600, seed: int = 4) -> pd.DataFrame:
    rng = np.random.default_rng(seed)
    close = 100.0 * np.exp(np.cumsum(rng.normal(0.0, 0.01, n)))
    return pd.DataFrame({"time": pd.date_range("2020-01-01", periods=n, freq="D", tz="UTC"),
                         "open": close, "high": close * 1.005, "low": close * 0.995, "close": close,
                         "volume": 1_000_000.0})


class _Column(Strategy):
    """Enters wherever a named column is set: lets a test plant a detector of known quality."""

    name = "column"

    def __init__(self, column: str = "plant") -> None:
        super().__init__(column=column)
        self.column = column

    def generate_signals(self, df: pd.DataFrame, ctx: StrategyContext) -> pd.DataFrame:
        out = df.copy()
        out["entry"] = df[self.column].astype(int)
        out["exit"] = 0
        return out


REGISTRY: dict[str, type[Strategy]] = {"good": _Column, "noise": _Column}


def _planted(n: int = 400) -> pd.DataFrame:
    df = _frame(n)
    up = (df["close"].shift(-10) / df["close"] - 1.0 > 0.03).fillna(False)
    rng = np.random.default_rng(9)
    df["plant"] = (up & (rng.random(n) < 0.6)).astype(int)        # fires only before real moves
    df["noise"] = (rng.random(n) < 0.15).astype(int)                # fires at random
    return df


def test_the_pattern_stage_keeps_the_informative_detector_and_drops_noise() -> None:
    grids = {"good": {"column": ["plant"]}, "noise": {"column": ["noise"]}}
    p = pf.find_pattern(_planted(), "X", grids=grids, registry=REGISTRY, min_fired=5)
    assert p is not None and [d.name for d in p.members] == ["good"]
    assert p.members[0].precision > p.base_rate


def test_no_detector_above_the_base_rate_means_no_pattern() -> None:
    p = pf.find_pattern(_planted(), "X", grids={"noise": {"column": ["noise"]}}, registry=REGISTRY, min_fired=5)
    assert p is None


def test_labels_stop_at_the_end_of_the_training_window() -> None:
    # Moves that begin inside the window but complete after it must not count: the last bars are unlabelled.
    df = _planted()
    df.loc[len(df) - 10:, "plant"] = 1
    df.loc[: len(df) - 11, "plant"] = 0
    p = pf.find_pattern(df, "X", grids={"good": {"column": ["plant"]}}, registry=REGISTRY, min_fired=5)
    assert p is None


def test_each_fold_finds_on_its_own_training_bars_only(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: list[pd.DataFrame] = []
    fixed = pf.Pattern((pf.Detector("bollinger", (("n_std", 2.0), ("window", 20)), 0.6, 0.3, 20),),
                       0.0, 0.6, 0.3, 0.3)

    def spy(train: pd.DataFrame, symbol: str, **_: Any) -> pf.Pattern:
        seen.append(train.copy())
        return fixed

    monkeypatch.setattr(pf, "find_pattern", spy)
    df = _frame(600)
    out = pf.pattern_walk_forward(df, "X", BacktestEngine(), train_bars=300, test_bars=100, step_bars=100)
    assert out is not None and out["folds"] == 3 and out["pattern_stability"] == 1.0
    for i, train in enumerate(seen):
        assert len(train) == 300
        assert np.allclose(train["close"].to_numpy(), df["close"].iloc[i * 100:i * 100 + 300].to_numpy())


def test_changing_a_test_block_cannot_change_the_pattern_chosen_before_it() -> None:
    grids = {"bollinger": {"window": [10, 20], "n_std": [1.5, 2.0]},
             "rsi_meanrevert": {"window": [7, 14], "oversold": [30, 35]}}
    df = _frame(500)
    shocked = df.copy()
    shocked.loc[300:, ["open", "high", "low", "close"]] *= 1.5
    a = pf.find_pattern(df.iloc[:300].reset_index(drop=True), "X", grids=grids, min_fired=3)
    b = pf.find_pattern(shocked.iloc[:300].reset_index(drop=True), "X", grids=grids, min_fired=3)
    assert a == b


def test_folds_without_a_pattern_are_counted(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(pf, "find_pattern", lambda *_a, **_k: None)
    assert pf.pattern_walk_forward(_frame(600), "X", BacktestEngine(), train_bars=300, test_bars=100,
                                   step_bars=100) is None


def test_the_threshold_gates_entries() -> None:
    df = _planted()
    det = pf.Detector("good", (("column", "plant"),), 0.9, 0.5, 10)
    open_ = pf.GatedComposite(pf.Pattern((det,), 0.0, 0.9, 0.5, 0.3), REGISTRY)
    shut = pf.GatedComposite(pf.Pattern((det,), 1.01, 0.9, 0.5, 0.3), REGISTRY)
    ctx = StrategyContext(symbol="X")
    assert open_.generate_signals(df, ctx)["entry"].sum() == df["plant"].sum()
    assert shut.generate_signals(df, ctx)["entry"].sum() == 0


def test_a_real_run_returns_the_sweeps_keys() -> None:
    grids = {"bollinger": {"window": [10, 20], "n_std": [1.0, 1.5]}}
    out = pf.pattern_walk_forward(_frame(700, seed=2), "X", BacktestEngine(), train_bars=400, test_bars=100,
                                  step_bars=100, grids=grids, min_fired=3)
    if out is not None:
        assert {"oos_score", "wfe", "oos_trades", "oos_win_rate", "folds", "modal_pattern"} <= set(out)
