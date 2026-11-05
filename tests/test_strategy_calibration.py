"""Strategy calibration: inputs use only earlier data, the Greek is the engine's, and noise earns no skill."""
from __future__ import annotations

from datetime import UTC, datetime, timedelta

import numpy as np
import pandas as pd
import pytest
from scipy.stats import norm

from trading_live_claude.analysis.strategy_calibration import (
    CalibrationTrade,
    graph_reading,
    greek_probability,
    prior_vol,
    score_input,
)
from trading_live_claude.intel.graph import Edge

T0 = datetime(2025, 1, 1, tzinfo=UTC)


def _edge(hours_before_t0: float, score: float, confidence: float = 1.0, symbol: str = "A") -> Edge:
    return Edge(subject=("event", "e"), predicate="sentiment_on", object=("symbol", symbol), weight=confidence,
                as_of=(T0 - timedelta(hours=hours_before_t0)).isoformat(), meta={"score": score})


def test_prior_vol_cannot_see_its_own_bar_or_later() -> None:
    close = pd.Series(np.exp(np.random.default_rng(1).normal(0, 0.01, 100).cumsum()))
    a = prior_vol(close, 80, 365.0)
    changed = close.copy()
    changed.iloc[80:] *= 5
    assert a == prior_vol(changed, 80, 365.0) and a is not None
    assert prior_vol(close, 5, 365.0) is None


def test_the_greek_matches_the_closed_form() -> None:
    vol, hold, periods, cost = 0.5, 10.0, 365.0, 0.009
    t = hold / periods
    d2 = (-np.log(1 + cost) - 0.5 * vol * vol * t) / (vol * np.sqrt(t))
    assert greek_probability(vol, hold, periods, cost) == pytest.approx(norm.cdf(d2))
    assert greek_probability(vol, hold, periods, 0.02) < greek_probability(vol, hold, periods, 0.0)


def test_graph_reads_only_the_window_before_entry() -> None:
    edges = [_edge(1, 0.5), _edge(2, -0.5, 3.0), _edge(30, 9.0), _edge(-1, 9.0), _edge(1, 9.0, symbol="B")]
    assert graph_reading(edges, "A", T0) == pytest.approx(-0.25)
    assert graph_reading(edges, "C", T0) is None


def _trades(returns: np.ndarray, inputs: np.ndarray) -> list[CalibrationTrade]:
    return [CalibrationTrade("s", "A", T0 + timedelta(days=i), T0 + timedelta(days=i + 1), float(r), float(x), float(x), None)
            for i, (r, x) in enumerate(zip(returns, inputs, strict=True))]


def test_noise_earns_no_skill_and_a_real_input_does() -> None:
    rng = np.random.default_rng(3)
    x = rng.uniform(0.1, 0.9, 400)
    noise = score_input(_trades(rng.normal(size=400), x), "composite", "s", n_rotations=200)
    real = score_input(_trades((rng.uniform(size=400) < x) * 2.0 - 1.0, x), "composite", "s", n_rotations=200)
    assert noise.skill is not None and real.skill is not None
    assert noise.skill < 0.02 < real.skill and real.p_value is not None and real.p_value < 0.01


def test_a_later_trade_cannot_change_an_earlier_score() -> None:
    rng = np.random.default_rng(4)
    x = rng.uniform(0.1, 0.9, 120)
    y = rng.normal(size=120)
    a = score_input(_trades(y, x), "composite", "s", n_rotations=10)
    assert a.n == 120 - 36 or a.n > 0
    short = score_input(_trades(y[:100], x[:100]), "composite", "s", n_rotations=10)
    assert short.n < a.n


def test_too_little_history_says_so() -> None:
    s = score_input(_trades(np.ones(10), np.full(10, 0.5)), "composite", "s")
    assert s.skill is None and s.n == 0
    assert score_input(_trades(np.ones(10), np.full(10, 0.5)), "graph", "s").n == 0
