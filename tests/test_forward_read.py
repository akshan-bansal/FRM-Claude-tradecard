"""Forward-read evidence: reads use only closed earlier trades, thin cells shrink, and noise shows no skill."""
from __future__ import annotations

from datetime import UTC, datetime, timedelta

import numpy as np
import pytest

from trading_live_claude.analysis.forward_read import TradeRow, causal_reads, net_return, skill

T0 = datetime(2025, 1, 1, tzinfo=UTC)


def _trade(day: int, ret: float, *, strategy: str = "s", symbol: str = "A", held: int = 2) -> TradeRow:
    return TradeRow(strategy, symbol, T0 + timedelta(days=day), T0 + timedelta(days=day + held), ret, held)


def test_costs_are_charged_on_both_sides() -> None:
    assert net_return(0.01, per_side_bps=45) == pytest.approx(0.001)


def test_a_later_or_unclosed_trade_cannot_change_a_read() -> None:
    base = [_trade(i * 10, 0.01) for i in range(8)]
    target = _trade(100, 0.0)
    before = {r.trade.entry: r.edge for r in causal_reads([*base, target])}[target.entry]
    still_open = _trade(99, 9.0, held=30)
    later = _trade(200, -9.0)
    after = {r.trade.entry: r.edge for r in causal_reads([*base, target, still_open, later])}[target.entry]
    assert before == after


def test_the_purge_holds_back_a_trade_that_closed_just_before() -> None:
    base = [_trade(i * 10, 0.01) for i in range(8)]
    target = _trade(100, 0.0)
    near = _trade(95, 9.0, held=2)                       # closed 3 days before, inside the 5-day purge
    a = next(r for r in causal_reads([*base, target]) if r.trade == target)
    b = next(r for r in causal_reads([*base, near, target]) if r.trade == target)
    assert a.edge == b.edge


def test_a_thin_cell_reads_near_its_pool_not_its_one_trade() -> None:
    pool = [_trade(i * 10, 0.0, symbol="B") for i in range(20)]
    lucky = _trade(1, 0.5, symbol="A", held=1)
    reads = causal_reads([*pool, lucky, _trade(300, 0.0, symbol="A")])
    read = [r for r in reads if r.trade.symbol == "A"][-1]
    assert read.n_cell == 1 and 0 < read.edge < 0.1


def test_too_little_evidence_gets_no_read() -> None:
    assert causal_reads([_trade(i * 10, 0.01) for i in range(5)]) == []


def _reads(returns: np.ndarray, edges: np.ndarray) -> list:
    from trading_live_claude.analysis.forward_read import Read
    return [Read(_trade(i, float(r)), float(e), 5, 50) for i, (r, e) in enumerate(zip(returns, edges, strict=True))]


def test_noise_shows_no_skill_and_real_signal_does() -> None:
    rng = np.random.default_rng(1)
    edge = rng.normal(size=300)
    noise = skill(_reads(rng.normal(size=300), edge), n_rotations=300)
    real = skill(_reads(edge + rng.normal(size=300), edge), n_rotations=300)
    assert noise is not None and real is not None
    assert noise.p_value > 0.05 and real.p_value < 0.01 and real.bucket_means[-1] > real.bucket_means[0]
    assert skill(_reads(edge[:10], edge[:10])) is None
