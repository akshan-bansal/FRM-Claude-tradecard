"""Gap-risk gate: sizes an entry for the close-to-open jump a polled stop cannot catch."""
from __future__ import annotations

from pathlib import Path

import pytest

from tests.test_router import _intent, _StubBroker
from trading_live_claude.audit.ledger import Ledger
from trading_live_claude.audit.replay import replay
from trading_live_claude.brokers.models import OrderAction
from trading_live_claude.execution.router import OrderIntent, Router
from trading_live_claude.risk.gap_risk import GapRiskGate, adverse_gaps, gap_quantile


def _bars(n: int = 400, gap: float = 0.05, every: int = 20) -> tuple[list[float], list[float]]:
    """Flat closes at 100; every ``every``-th session opens ``gap`` below the close before it."""
    closes = [100.0] * n
    opens = [100.0 * (1 - gap) if i % every == 0 and i else 100.0 for i in range(n)]
    return opens, closes


def _router(tmp_path: Path, gate: GapRiskGate | None, **kw) -> Router:
    r = Router.build_default(mode="paper", broker=_StubBroker(), state_dir=tmp_path, **kw)
    r.gap_gate = gate
    return r


def test_gaps_are_measured_open_against_the_previous_close() -> None:
    assert adverse_gaps([100.0, 95.0, 103.0], [100.0, 100.0, 100.0]) == pytest.approx([0.05, -0.03])
    assert gap_quantile([-0.02, 0.0, 0.01, 0.04], 0.99) == pytest.approx(0.0391, abs=1e-4)
    assert gap_quantile([-0.02, -0.01], 0.99) == 0.0          # only gaps up: no loss to a long


def test_an_entry_is_trimmed_so_a_gap_through_its_stop_stays_inside_the_risk_budget(tmp_path: Path) -> None:
    gate = GapRiskGate(lambda _s: _bars(gap=0.10, every=5), risk_pct=0.01)
    r = _router(tmp_path, gate)
    intent = _intent(shares=200, entry=100.0, stop=98.0, risk=400.0)       # a 2% stop, 20% of equity
    d = r._gate(intent, equity=100_000.0, existing_risk=0.0, open_positions=0)
    loss = d.observed["gap_risk"]["loss_fraction"]
    assert d.accepted and loss == pytest.approx(0.10)
    assert intent.shares * 100.0 * loss <= 0.01 * 100_000.0 and intent.shares == 100
    assert d.trims[-1]["cap"] == "gap_risk"


def test_a_small_gap_leaves_the_size_alone(tmp_path: Path) -> None:
    r = _router(tmp_path, GapRiskGate(lambda _s: _bars(gap=0.001), risk_pct=0.01))
    intent = _intent(shares=100)
    d = r._gate(intent, equity=100_000.0, existing_risk=0.0, open_positions=0)
    assert d.accepted and intent.shares == 100 and not any(t["cap"] == "gap_risk" for t in d.trims)


def test_heat_counts_the_gap_where_it_exceeds_the_stop(tmp_path: Path) -> None:
    # The stop risks $400; a 5% gap on the $10,000 position risks $500, which tips heat over the cap.
    r = _router(tmp_path, GapRiskGate(lambda _s: _bars(gap=0.05, every=2), risk_pct=0.01))
    cap = r.heat.cap_pct * 100_000.0
    d = r._gate(_intent(shares=100), equity=100_000.0, existing_risk=cap - 450.0, open_positions=0)
    assert not d.accepted and any("with gap risk" in x for x in d.rejected_reasons)
    plain = _router(tmp_path, None)._gate(_intent(shares=100), equity=100_000.0, existing_risk=cap - 450.0,
                                          open_positions=0)
    assert plain.accepted                                                    # without the gate it fits


def test_too_little_history_fails_closed_and_crypto_is_exempt(tmp_path: Path) -> None:
    r = _router(tmp_path, GapRiskGate(lambda _s: _bars(n=50), risk_pct=0.01))
    d = r._gate(_intent(), equity=100_000.0, existing_risk=0.0, open_positions=0)
    assert not d.accepted and any("session gaps" in x for x in d.rejected_reasons)
    btc = OrderIntent(symbol="BTC/USD", action=OrderAction.BUY, shares=0.1, entry=60_000.0, stop=58_000.0,
                      target=None, strategy="t", risk_dollars=200.0, account_number="X")
    assert r._gate(btc, equity=100_000.0, existing_risk=0.0, open_positions=0).observed["gap_risk"] == {
        "exempt": "venue never closes"}


def test_an_unreadable_feed_fails_closed(tmp_path: Path) -> None:
    def broken(_s: str) -> tuple[list[float], list[float]]:
        raise RuntimeError("feed down")
    d = _router(tmp_path, GapRiskGate(broken, risk_pct=0.01))._gate(
        _intent(), equity=100_000.0, existing_risk=0.0, open_positions=0)
    assert not d.accepted and any("unavailable" in x for x in d.rejected_reasons)


def test_an_exit_is_never_gated(tmp_path: Path) -> None:
    r = _router(tmp_path, GapRiskGate(lambda _s: _bars(n=10), risk_pct=0.01))
    sell = OrderIntent(symbol="AAPL", action=OrderAction.SELL, shares=100, entry=100.0, stop=110.0,
                       target=None, strategy="t", risk_dollars=0.0, account_number="X")
    d = r._gate(sell, equity=100_000.0, existing_risk=0.0, open_positions=1)
    assert d.accepted and "gap_risk" not in d.observed


def test_a_trimmed_order_replays_from_the_ledger(tmp_path: Path) -> None:
    led = Ledger(tmp_path / "ledger", stream="t", session_id="s1")
    r = _router(tmp_path, GapRiskGate(lambda _s: _bars(gap=0.10, every=5), risk_pct=0.01), ledger=led)
    r.submit(_intent(shares=200, stop=98.0), equity=100_000.0, existing_risk=0.0, open_positions=0)
    trim = next(x for x in led.rows() if x["event"] == "RISK_TRIMMED")
    assert trim["payload"]["cap"] == "gap_risk"
    assert [x["status"] for x in replay(led.rows())] == ["match"]


def test_turning_the_gate_on_moves_the_risk_check_version(tmp_path: Path) -> None:
    r = _router(tmp_path, None)
    before = r.risk_check_version()
    r.gap_gate = GapRiskGate(lambda _s: _bars(), risk_pct=0.01)
    assert r.risk_check_version() != before


def test_the_gate_is_built_from_trading_yaml_for_paper_and_money_alike() -> None:
    from trading_live_claude.config.settings import Settings
    from trading_live_claude.monitor import session as sess
    assert Settings().gap_risk_level == 0.99
    assert "gap_risk_level" not in sess.NECESSARY_DIFFERENCES
