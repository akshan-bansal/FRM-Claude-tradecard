"""A test fold is scored with its training bars as warm-up, so a long lookback can act inside it."""
from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from trading_live_claude.backtest import BacktestEngine
from trading_live_claude.strategies import STRATEGIES

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import sweep_universe


def _frame(close: np.ndarray) -> pd.DataFrame:
    t = pd.date_range("2022-01-03", periods=len(close), freq="B", tz="UTC")
    return pd.DataFrame({"time": t, "open": close, "high": close * 1.001, "low": close * 0.999,
                         "close": close, "volume": 1e6})


def _v_shape(n: int, turn: int = 150) -> pd.DataFrame:
    """Falls, then rises at 0.2% a bar: the crossover has to happen before it can be held."""
    down = 100.0 * 0.998 ** np.arange(turn)
    return _frame(np.concatenate([down, down[-1] * 1.002 ** np.arange(1, n - turn + 1)]))


def _strategy():
    return STRATEGIES["ema_crossover"](fast=20, slow=200)


def test_no_warmup_is_the_old_run(random_walk_df: pd.DataFrame) -> None:
    eng = BacktestEngine()
    a = eng.run(_strategy(), random_walk_df, "TEST")
    b = eng.run(_strategy(), random_walk_df, "TEST", warmup_bars=0)
    pd.testing.assert_series_equal(a.equity_curve, b.equity_curve)
    assert a.metrics == b.metrics


def test_a_warmed_fold_reports_only_its_own_bars(random_walk_df: pd.DataFrame) -> None:
    res = BacktestEngine().run(_strategy(), random_walk_df, "TEST", warmup_bars=400)
    assert len(res.equity_curve) == len(res.returns) == len(res.positions) == 100
    assert res.positions.index[0] == 400


def test_a_long_lookback_acts_in_a_short_fold_only_when_warmed() -> None:
    df = _v_shape(500)
    eng = BacktestEngine()
    cold = eng.run(_strategy(), df.iloc[400:].reset_index(drop=True), "TEST")
    warm = eng.run(_strategy(), df, "TEST", warmup_bars=400)
    assert cold.metrics.total_return == 0.0
    assert warm.metrics.total_return > 0.05


def test_a_carried_position_pays_to_arrive() -> None:
    df = _v_shape(500)
    eng = BacktestEngine()
    res = eng.run(_strategy(), df, "TEST", warmup_bars=400)
    assert (res.positions == 1).all()
    per_side = eng.cost_model.per_side_frac()
    r = 0.002
    expected = eng.starting_equity * (1.0 + r - per_side) * (1.0 + r) ** 99
    assert res.ending_equity == pytest.approx(expected, rel=1e-9)


def test_a_trade_carried_in_is_priced_from_the_fold_start() -> None:
    rise = _v_shape(400).close.to_numpy()
    close = np.concatenate([rise, rise[-1] * 0.998 ** np.arange(1, 201)])
    df = _frame(close)
    res = BacktestEngine().run(_strategy(), df, "TEST", warmup_bars=350)
    assert res.trades, "the falling leg should close the position"
    assert res.trades[0].entry_price == pytest.approx(close[349])
    assert res.trades[0].exit_price < close[399]


def test_warmup_must_leave_a_bar_to_score(random_walk_df: pd.DataFrame) -> None:
    eng = BacktestEngine()
    for bad in (-1, len(random_walk_df)):
        with pytest.raises(ValueError, match="warmup_bars"):
            eng.run(_strategy(), random_walk_df, "TEST", warmup_bars=bad)


def test_warmup_does_not_look_ahead(random_walk_df: pd.DataFrame) -> None:
    """Later bars must not change what an earlier scored bar shows."""
    eng = BacktestEngine()
    short = eng.run(_strategy(), random_walk_df.iloc[:450], "TEST", warmup_bars=350)
    long = eng.run(_strategy(), random_walk_df, "TEST", warmup_bars=350)
    np.testing.assert_allclose(short.returns.to_numpy(), long.returns.to_numpy()[:100])
    np.testing.assert_array_equal(short.positions.to_numpy(), long.positions.to_numpy()[:100])


def test_walk_forward_scores_a_lookback_longer_than_the_fold(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(sweep_universe, "PARAM_GRIDS", {"ema_crossover": {"fast": [20], "slow": [200]}})
    wf = sweep_universe.walk_forward(_v_shape(800), "SPY", train=300, test=100, step=100)
    assert wf is not None and wf["folds"] == 5
    # Scored alone, every fold reads exactly 0. Warmed, the folds whose training bars hold the
    # crossover earn; later ones cannot see it, as warm-up reaches back only to the training start.
    assert wf["oos_return"] > 0.0
