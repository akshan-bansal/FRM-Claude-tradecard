"""Phase 3 sizing A/B: the book marks correctly, a binding budget is shared as specified, tilts are causal."""
from __future__ import annotations

import math

import pandas as pd
import pytest

from trading_live_claude.analysis.sizing_ab import Rules, SizedTrade, held_returns, simulate, tilts

D = pd.date_range("2025-01-01", periods=10, freq="D", tz="UTC")


def _t(start: int, n: int, gross: float, *, symbol: str = "A", edge: float | None = None, vol: float = 0.5,
       cost: float = 0.0) -> SizedTrade:
    closes = [100.0 * (1 + gross) ** (k / n) for k in range(n + 1)]
    return SizedTrade("s", symbol, D[start], tuple(D[start + 1:start + 1 + n]), held_returns(closes, gross), cost, vol, edge)


def test_held_returns_compound_to_the_trades_gross() -> None:
    r = held_returns([100, 104, 99, 103], 0.05)
    assert math.prod(1 + x for x in r) == pytest.approx(1.05)


def test_one_trade_at_half_weight_moves_equity_by_half_its_return_less_costs() -> None:
    res = simulate([_t(0, 3, 0.10, cost=0.01)], [0.5], rank=None, rules=Rules(), name="x")
    assert res.equity.iloc[-1] == pytest.approx(1 - 0.5 * 0.005 + 0.5 * 1.10 * (1 - 0.005) - 0.5)
    assert res.exposure.iloc[-1] == 0.0


def test_a_binding_budget_is_pro_rata_in_a_and_ranked_in_b() -> None:
    trades = [_t(0, 2, 0.0, symbol="A"), _t(0, 2, 0.0, symbol="B")]
    a = simulate(trades, [0.8, 0.8], rank=None, rules=Rules(), name="a")
    b = simulate(trades, [0.8, 0.8], rank=[1.0, 2.0], rules=Rules(), name="b")
    assert a.weights == pytest.approx([0.5, 0.5])
    assert sorted(b.weights) == pytest.approx([0.2, 0.8]) and a.bound_days == b.bound_days == 1


def test_tilts_are_causal_and_follow_the_rules() -> None:
    trades = [_t(0, 1, 0.0, edge=0.01), _t(2, 1, 0.0, edge=0.02), _t(4, 1, 0.0, edge=-0.01), _t(5, 1, 0.0)]
    got = tilts(trades, Rules())
    assert got[0] == 1.0                      # no earlier reads: no reference, base size
    assert got[1] == pytest.approx(2.0)       # twice the earlier median
    assert got[2] == 0.5 and got[3] == 1.0    # a weak read, then no read
    later = tilts([*trades, _t(8, 1, 0.0, edge=9.0)], Rules())
    assert later[:4] == got
