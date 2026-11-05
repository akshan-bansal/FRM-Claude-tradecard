"""Entry and exit tests: exits act a close late and only shorten, thresholds use closed trades, noise does not pass."""
from __future__ import annotations

from datetime import UTC, datetime, timedelta

import numpy as np
import pytest

from trading_live_claude.analysis.entry_exit import (
    PathTrade,
    causal_percentiles,
    evaluate_entry,
    evaluate_exit,
    simulate_exit,
)

T0 = datetime(2024, 1, 1, tzinfo=UTC)
COST = 0.01


def _trade(day: int, net: float, *, value: float | None = 0.5, closes: tuple[float, ...] = (100.0, 100.0, 100.0),
           scores: tuple[float, ...] = (0.0, 0.0, 0.0), symbol: str = "A", strategy: str = "s") -> PathTrade:
    return PathTrade(strategy, symbol, T0 + timedelta(days=day), T0 + timedelta(days=day + 2), net,
                     {"composite": value}, closes, {"composite": scores})


def test_an_exit_acts_on_the_next_close_and_never_holds_longer() -> None:
    closes = (100.0, 110.0, 120.0, 90.0, 130.0)
    assert simulate_exit(closes, (0, 1, 0, 0, 0), 1.0, COST) == pytest.approx(0.20 - COST)   # score at 110, out at 120
    assert simulate_exit(closes, (0, 0, 0, 0, 0), 1.0, COST) == pytest.approx(0.30 - COST)   # the real exit
    assert simulate_exit(closes, (1, 0, 0, 0, 0), 1.0, COST) == pytest.approx(0.30 - COST)   # the entry close is not a signal


def test_a_percentile_reads_only_earlier_trades_on_its_symbol() -> None:
    rows = [_trade(i, 0.0, value=float(i)) for i in range(12)]
    later = _trade(100, 0.0, value=1000.0)
    other = _trade(5, 0.0, value=1000.0, symbol="B")
    before = causal_percentiles(rows, "composite")
    after = causal_percentiles([*rows, later, other], "composite")
    assert all(before[id(t)] == after[id(t)] for t in rows if id(t) in before)
    assert id(rows[9]) not in after and after[id(rows[10])] == 1.0


def test_a_later_trade_cannot_change_an_earlier_decision() -> None:
    rng = np.random.default_rng(2)
    rows = [_trade(i * 3, float(r), value=float(v)) for i, (r, v) in enumerate(zip(rng.normal(0, 0.02, 130), rng.uniform(size=130), strict=True))]
    short = evaluate_entry(rows[:120], "composite", "s", n_null=20)
    extra = [*rows[:120], _trade(1000, 9.0, value=0.99)]
    same = evaluate_entry(extra, "composite", "s", n_null=20)
    assert short is not None and same is not None and same.n == short.n + 1
    assert same.total_base == pytest.approx(short.total_base + 9.0)


def test_noise_does_not_pass_the_entry_filter_and_a_real_input_does() -> None:
    rng = np.random.default_rng(5)
    value = rng.uniform(size=400)
    noise = [_trade(i * 2, float(r), value=float(v)) for i, (r, v) in enumerate(zip(rng.normal(0, 0.03, 400), value, strict=True))]
    real = [_trade(i * 2, float((v - 0.5) * 0.1 + e), value=float(v)) for i, (v, e) in enumerate(zip(value, rng.normal(0, 0.01, 400), strict=True))]
    a = evaluate_entry(noise, "composite", "s", n_null=300)
    b = evaluate_entry(real, "composite", "s", n_null=300)
    assert a is not None and b is not None
    assert not a.passes(0.01)
    assert b.p_value < 0.01 and b.mean_rule > b.mean_base and b.changed > 0.1


def _exit_rows(fall: bool, seed: int = 8) -> list[PathTrade]:
    rng = np.random.default_rng(seed)
    rows = []
    for i in range(300):
        warned = bool(rng.uniform() < 0.5)
        drift = -0.05 if (warned and fall) else 0.0
        noise = float(rng.normal(0, 0.01))
        closes = (100.0, 100.0, 100.0, 100.0 * (1 + drift + noise))
        scores = (0.0, 0.9 if warned else 0.0, 0.0, 0.0)
        rows.append(_trade(i * 5, closes[-1] / 100.0 - 1.0 - COST, closes=closes, scores=scores))
    return rows


def test_an_exit_that_avoids_real_falls_shows_up_and_a_useless_one_changes_little() -> None:
    real = evaluate_exit(_exit_rows(True), "composite", "s", cost=COST, n_null=500)
    useless = evaluate_exit(_exit_rows(False), "composite", "s", cost=COST, n_null=500)
    assert real is not None and useless is not None
    assert real.mean_rule > real.mean_base and real.p_value < 0.01 and real.drawdown_rule < real.drawdown_base
    assert not useless.passes(0.01)


def test_too_few_trades_give_no_verdict() -> None:
    rows = [_trade(i, 0.0) for i in range(20)]
    assert evaluate_entry(rows, "composite", "s") is None
    assert evaluate_exit(rows, "composite", "s", cost=COST) is None
