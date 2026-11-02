"""The live runner mirrors the paper runners, a halt lets exits out, and live books auto-halt.

Nothing here builds a live router or sets a live execution mode. The auto-halt runs against a stand-in
that reports mode "live" and can place nothing, and the gate is exercised on the paper router.
"""
from __future__ import annotations

import csv
import inspect
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest

from trading_live_claude import cli
from trading_live_claude.audit.ledger import Ledger, read_stream
from trading_live_claude.audit.replay import replay
from trading_live_claude.brokers.base import BrokerError
from trading_live_claude.brokers.models import OrderAction, Position, Quote
from trading_live_claude.brokers.paper import PaperBroker
from trading_live_claude.config.settings import Settings
from trading_live_claude.execution.daily_budget import DailyBudget
from trading_live_claude.execution.router import OrderIntent, Router
from trading_live_claude.monitor.live_loop import LiveMonitor
from trading_live_claude.risk.equity_watch import COLUMNS, EquityWatch
from trading_live_claude.risk.kill_switch import KillSwitch
from trading_live_claude.risk.sizing import PositionSizer
from trading_live_claude.strategies.examples.bollinger import BollingerMeanRevert

REPO = Path(__file__).resolve().parents[1]
RUNNERS = [REPO / "src" / "trading_live_claude" / "cli.py", REPO / "scripts" / "paper_ib.py",
           REPO / "scripts" / "paper_kraken.py"]


# ---------------------------------------------------------------- one policy, every runner

@pytest.mark.parametrize("path", RUNNERS, ids=lambda p: p.name)
def test_every_runner_builds_from_the_shared_session(path: Path) -> None:
    src = path.read_text(encoding="utf-8")
    assert "LiveMonitor(" not in src and "start_session(" in src
    assert "cap_pct=settings.portfolio_heat_cap" not in src     # no runner hand-lists its own limits


def test_limits_from_carries_every_risk_knob(tmp_path: Path) -> None:
    s = Settings(portfolio_heat_cap=0.04, max_drawdown_kill_switch=0.02, daily_loss_limit_pct=0.015,
                 max_open_positions=3, min_ticket_usd=250.0, max_gross_leverage=0.7,
                 max_position_notional_pct=0.2, force_exit_atr_mult=2.5, on_size_cap_breach="reject")
    r = Router.build_default(mode="paper", broker=_paper(tmp_path), state_dir=tmp_path, **Router.limits_from(s))
    assert r.heat.cap_pct == 0.04 and r.kill_switch.max_drawdown_pct == 0.02
    assert r.kill_switch.daily_loss_limit_pct == 0.015
    assert (r.max_open_positions, r.min_ticket_usd) == (3, 250.0)
    assert (r.max_gross_leverage, r.max_position_notional_pct) == (0.7, 0.2)
    assert (r.force_exit_atr_mult, r.on_size_cap_breach) == (2.5, "reject")


def test_live_takes_exactly_what_paper_takes() -> None:
    live = set(inspect.signature(cli.live).parameters)
    paper = set(inspect.signature(cli.paper).parameters)
    assert live - paper == {"confirm", "allow_screened"}
    assert paper - live == {"starting_equity"}


# ---------------------------------------------------------------- a halt lets exits out

class _Feed:
    name = venue = "kraken"

    def quote(self, symbol: str) -> Quote:
        return Quote(symbol=symbol, symbolId=1, bidPrice=100.0, askPrice=100.0, lastTradePrice=100.0,
                     time=datetime.now(UTC))

    def quotes(self, symbols: list[str]) -> list[Quote]:
        return [self.quote(s) for s in symbols]


def _paper(tmp: Path) -> PaperBroker:
    return PaperBroker(feed=_Feed(), journal_dir=tmp)  # type: ignore[arg-type]


def _intent(action: OrderAction, shares: int = 10, symbol: str = "AAA", **kw: object) -> OrderIntent:
    stop = 95.0 if action in (OrderAction.BUY, OrderAction.BUY_TO_COVER) else 105.0
    return OrderIntent(symbol=symbol, action=action, shares=shares, entry=100.0, stop=stop, target=None,
                       strategy="s", risk_dollars=0.0, account_number="PAPER-001", **kw)  # type: ignore[arg-type]


BOOK = {"equity": 100_000.0, "existing_risk": 0.0, "open_positions": 1}


def _halted(tmp: Path, held: float, ledger: Ledger | None = None) -> Router:
    pb = _paper(tmp)
    pb.positions = lambda _a: [Position(symbol="AAA", symbolId=1, openQuantity=held)] if held else []  # type: ignore[method-assign]
    r = Router.build_default(mode="paper", broker=pb, state_dir=tmp, ledger=ledger)
    r.kill_switch.trip(reason="test")
    return r


@pytest.mark.parametrize(("held", "action", "shares", "passes"), [
    (10.0, OrderAction.SELL, 10, True),            # closes it
    (10.0, OrderAction.SELL, 4, True),             # reduces it
    (10.0, OrderAction.SELL, 11, False),           # would flip it short
    (0.0, OrderAction.SELL, 10, False),            # would open a short
    (-5.0, OrderAction.SELL, 5, False),            # adds to a short
    (10.0, OrderAction.BUY, 5, False),             # adds to a long
    (0.0, OrderAction.BUY, 5, False),              # a new entry
    (-5.0, OrderAction.BUY_TO_COVER, 5, True),     # covers a short
    (-5.0, OrderAction.BUY_TO_COVER, 6, False),    # would flip it long
    (10.0, OrderAction.SELL_SHORT, 5, False),      # never a closing order
])
def test_only_an_order_that_reduces_a_held_position_passes(tmp_path: Path, held: float, action: OrderAction,
                                                           shares: int, passes: bool) -> None:
    d = _halted(tmp_path, held)._gate(_intent(action, shares), **BOOK)
    assert ("kill-switch tripped" not in d.rejected_reasons) is passes
    assert d.observed["held_quantity"] == held


def test_another_symbols_position_does_not_count(tmp_path: Path) -> None:
    d = _halted(tmp_path, 10.0)._gate(_intent(OrderAction.SELL, 5, symbol="BBB"), **BOOK)
    assert "kill-switch tripped" in d.rejected_reasons


def test_unreadable_holding_is_refused(tmp_path: Path) -> None:
    r = _halted(tmp_path, 10.0)

    def boom(_a: str) -> list[Position]:
        raise BrokerError("positions unavailable")

    r.broker.positions = boom  # type: ignore[method-assign]
    d = r._gate(_intent(OrderAction.SELL, 5), **BOOK)
    assert "kill-switch tripped" in d.rejected_reasons and d.observed["held_quantity"] is None


def test_gate_decisions_under_a_halt_replay(tmp_path: Path) -> None:
    ledger = Ledger(tmp_path / "ledger", stream="t", mode="paper", session_id="s")
    r = _halted(tmp_path, 10.0, ledger)
    r.submit(_intent(OrderAction.SELL, 4), **BOOK)    # passes
    r.submit(_intent(OrderAction.BUY, 4), **BOOK)     # refused
    rows = [row for p in (tmp_path / "ledger").glob("*t.jsonl") for row in read_stream(p)]
    results = replay(rows)
    assert [x["status"] for x in results] == ["match", "match"], results


# ---------------------------------------------------------------- the other gates that could trap an exit

OVER_HEAT = {"equity": 100_000.0, "existing_risk": 9_000.0, "open_positions": 1}


def _holding(tmp: Path, held: float, ledger: Ledger | None = None, **kw: object) -> Router:
    pb = _paper(tmp)
    pb.positions = lambda _a: [Position(symbol="AAA", symbolId=1, openQuantity=held)] if held else []  # type: ignore[method-assign]
    return Router.build_default(mode="paper", broker=pb, state_dir=tmp, ledger=ledger, **kw)  # type: ignore[arg-type]


def test_reducing_order_passes_a_heat_cap_the_book_is_already_over(tmp_path: Path) -> None:
    d = _holding(tmp_path, 10.0)._gate(_intent(OrderAction.SELL, 5), **OVER_HEAT)
    assert d.accepted and d.observed["reduces_position"] is True
    assert d.observed["heat_pct"] > 0.05                 # recorded, just not held against the exit


def test_reducing_order_passes_the_minimum_ticket(tmp_path: Path) -> None:
    d = _holding(tmp_path, 10.0, min_ticket_usd=5_000.0)._gate(_intent(OrderAction.SELL, 5), **BOOK)
    assert d.accepted


def test_reducing_order_passes_a_spent_daily_budget(tmp_path: Path) -> None:
    r = _holding(tmp_path, 10.0)
    r.daily_budget = DailyBudget(tmp_path, max_trades_per_day=0, max_notional_per_day_usd=0.0)
    d = r._gate(_intent(OrderAction.SELL, 5), **BOOK)
    assert d.accepted and "daily_budget" in d.observed


@pytest.mark.parametrize(("held", "action", "shares"), [
    (0.0, OrderAction.BUY, 5),                           # a new entry
    (10.0, OrderAction.BUY, 5),                          # adding to the position
    (10.0, OrderAction.SELL, 11),                        # would flip it short
    (0.0, OrderAction.SELL, 5),                          # would open a short
    (10.0, OrderAction.SELL_SHORT, 5),                   # never a closing order
])
def test_anything_else_still_meets_all_three_gates(tmp_path: Path, held: float, action: OrderAction,
                                                   shares: int) -> None:
    r = _holding(tmp_path, held, min_ticket_usd=5_000.0)
    r.daily_budget = DailyBudget(tmp_path, max_trades_per_day=0, max_notional_per_day_usd=0.0)
    d = r._gate(_intent(action, shares), **OVER_HEAT)
    text = " | ".join(d.rejected_reasons)
    assert "below min" in text and "portfolio heat" in text and "daily trade cap" in text
    assert d.observed["reduces_position"] is False


def test_unreadable_holding_gets_no_exemption(tmp_path: Path) -> None:
    r = _holding(tmp_path, 10.0, min_ticket_usd=5_000.0)

    def boom(_a: str) -> list[Position]:
        raise BrokerError("positions unavailable")

    r.broker.positions = boom  # type: ignore[method-assign]
    text = " | ".join(r._gate(_intent(OrderAction.SELL, 5), **OVER_HEAT).rejected_reasons)
    assert "below min" in text and "portfolio heat" in text


def test_a_failed_read_does_not_stop_a_new_entry(tmp_path: Path) -> None:
    """An entry was never exempt, so a positions outage must not change how it is gated."""
    r = _holding(tmp_path, 0.0)

    def boom(_a: str) -> list[Position]:
        raise BrokerError("positions unavailable")

    r.broker.positions = boom  # type: ignore[method-assign]
    assert r._gate(_intent(OrderAction.BUY, 5), **BOOK).accepted


def test_exempted_decisions_replay(tmp_path: Path) -> None:
    ledger = Ledger(tmp_path / "ledger", stream="t", mode="paper", session_id="s")
    r = _holding(tmp_path, 10.0, ledger, min_ticket_usd=5_000.0)
    r.daily_budget = DailyBudget(tmp_path, max_trades_per_day=0, max_notional_per_day_usd=0.0)
    r.submit(_intent(OrderAction.SELL, 5), **OVER_HEAT)  # exempt from all three
    r.submit(_intent(OrderAction.BUY, 5), **OVER_HEAT)   # refused by all three
    rows = [row for p in (tmp_path / "ledger").glob("*t.jsonl") for row in read_stream(p)]
    results = replay(rows)
    assert [x["status"] for x in results] == ["match", "match"], results


# ---------------------------------------------------------------- EquityWatch

T0 = datetime(2026, 11, 2, 15, 0, tzinfo=UTC)


def _watch(tmp: Path, account: str = "A1", mode: str = "live", max_dd: float = 0.10,
           daily: float = 0.03) -> EquityWatch:
    ks = KillSwitch(tmp, max_drawdown_pct=max_dd, daily_loss_limit_pct=daily)
    return EquityWatch(ks, account=account, mode=mode, currency="CAD", session_id="s")


def _rows(tmp: Path) -> list[dict[str, str]]:
    with (tmp / EquityWatch.FILENAME).open(encoding="utf-8", newline="") as f:
        return list(csv.DictReader(f))


def test_journals_the_paper_columns_plus_whose_row_it_is(tmp_path: Path) -> None:
    w = _watch(tmp_path)
    w.observe(100_000.0, positions_value=30_000.0, realized_pnl=5.0, unrealized_pnl=-7.0, now=T0)
    row = _rows(tmp_path)[0]
    paper = {"ts", "session_id", "equity", "cash", "positions_value", "realized_pnl", "unrealized_pnl",
             "peak_equity", "drawdown_pct"}
    assert paper < set(COLUMNS) and set(COLUMNS) - paper == {"account", "mode", "currency"}
    assert float(row["cash"]) == 70_000.0 and float(row["peak_equity"]) == 100_000.0


def test_drawdown_trips(tmp_path: Path) -> None:
    w = _watch(tmp_path, max_dd=0.03, daily=0.50)
    assert not w.observe(100_000.0, now=T0).halted
    assert not w.observe(98_000.0, now=T0 + timedelta(minutes=1)).halted
    state = w.observe(96_900.0, now=T0 + timedelta(minutes=2))
    assert state.halted and "max-drawdown" in state.reason


def test_daily_loss_trips_and_resets_with_the_utc_day(tmp_path: Path) -> None:
    w = _watch(tmp_path, max_dd=0.50, daily=0.03)
    w.observe(100_000.0, now=T0)
    assert not w.observe(97_500.0, now=T0 + timedelta(hours=1)).halted
    # The next UTC day opens at this mark, so the same level is no longer a daily loss.
    assert not w.observe(97_500.0, now=T0 + timedelta(days=1)).halted
    state = w.observe(94_000.0, now=T0 + timedelta(days=1, hours=1))
    assert state.halted and "daily-loss" in state.reason


def test_restart_does_not_forget_the_peak_or_the_days_open(tmp_path: Path) -> None:
    now = datetime.now(UTC)
    _watch(tmp_path, max_dd=0.03, daily=0.50).observe(100_000.0, now=now)
    state = _watch(tmp_path, max_dd=0.03, daily=0.50).observe(96_000.0, now=now)
    assert state.halted and "max-drawdown" in state.reason
    (tmp_path / "HALTED").unlink()
    state = _watch(tmp_path, max_dd=0.50, daily=0.03).observe(96_000.0, now=now)
    assert state.halted and "daily-loss" in state.reason


def test_another_accounts_rows_are_not_this_ones_peak(tmp_path: Path) -> None:
    now = datetime.now(UTC)
    _watch(tmp_path, account="PRACTICE", max_dd=0.03).observe(1_000_000.0, now=now)
    assert not _watch(tmp_path, account="A1", max_dd=0.03).observe(50_000.0, now=now).halted
    assert not _watch(tmp_path, account="PRACTICE", mode="autonomous", max_dd=0.03).observe(50_000.0, now=now).halted


def test_a_human_clear_restarts_the_measure_in_a_running_process(tmp_path: Path) -> None:
    w = _watch(tmp_path, max_dd=0.03, daily=0.50)
    w.observe(100_000.0, now=T0)
    assert w.observe(96_000.0, now=T0 + timedelta(minutes=1)).halted
    assert w.observe(96_000.0, now=T0 + timedelta(minutes=2)).halted
    w.kill_switch.clear("I HAVE INVESTIGATED")
    assert not w.observe(96_000.0, now=T0 + timedelta(minutes=3)).halted
    assert w.observe(93_000.0, now=T0 + timedelta(minutes=4)).halted


def test_a_human_clear_survives_a_restart(tmp_path: Path) -> None:
    now = datetime.now(UTC)
    w = _watch(tmp_path, max_dd=0.03, daily=0.50)
    w.observe(100_000.0, now=now - timedelta(minutes=5))
    assert w.observe(96_000.0, now=now - timedelta(minutes=4)).halted
    w.kill_switch.clear("I HAVE INVESTIGATED")
    assert not _watch(tmp_path, max_dd=0.03, daily=0.50).observe(96_000.0).halted


def test_a_manual_stop_keeps_its_reason(tmp_path: Path) -> None:
    w = _watch(tmp_path, max_dd=0.03, daily=0.50)
    w.observe(100_000.0, now=T0)
    w.kill_switch.trip(reason="operator said stop")
    state = w.observe(50_000.0, now=T0 + timedelta(minutes=1))
    assert state.reason.endswith("operator said stop")


def test_a_torn_journal_line_is_skipped(tmp_path: Path) -> None:
    now = datetime.now(UTC)
    _watch(tmp_path, max_dd=0.03).observe(100_000.0, now=now)
    with (tmp_path / EquityWatch.FILENAME).open("a", encoding="utf-8") as f:
        f.write("2026-11-01T00:00:00+00:00,s,A1,live")
    assert _watch(tmp_path, max_dd=0.03).observe(96_000.0, now=now).halted


def test_failed_journal_write_still_evaluates(tmp_path: Path) -> None:
    w = _watch(tmp_path, max_dd=0.03, daily=0.50)
    w.path = tmp_path / "no_such_dir" / "x.csv"
    w.observe(100_000.0, now=T0)
    assert w.observe(90_000.0, now=T0 + timedelta(minutes=1)).halted


# ---------------------------------------------------------------- wired into the monitor

class _LiveStandIn:
    """Reports the mode a real live router would, and has nothing behind it that could trade."""

    strategy_version_for = None

    def __init__(self, state_dir: Path, mode: str = "live") -> None:
        self.mode = mode
        self.kill_switch = KillSwitch(state_dir, max_drawdown_pct=0.03, daily_loss_limit_pct=0.50)

    def submit(self, *_a: object, **_k: object) -> None:
        return None


class _Account(_Feed):
    def __init__(self) -> None:
        self.value = 100_000.0

    def positions(self, _a: str) -> list[Position]:
        return []

    def equity(self, _a: str, currency: str = "CAD") -> float:
        return self.value


def _live_monitor(tmp: Path, mode: str) -> tuple[LiveMonitor, _Account, _LiveStandIn]:
    acct, router = _Account(), _LiveStandIn(tmp, mode)
    m = LiveMonitor(broker=acct, market=SimpleNamespace(), strategy=BollingerMeanRevert(),  # type: ignore[arg-type]
                    sizer=PositionSizer(risk_pct=0.01), router=router, account_number="A1",  # type: ignore[arg-type]
                    symbols=[], account_currency="CAD", risk_model="atr", heat_aggregation="sum")
    return m, acct, router


@pytest.mark.parametrize("mode", ["live", "autonomous"])
def test_live_modes_auto_halt_on_a_poll(tmp_path: Path, mode: str) -> None:
    m, acct, router = _live_monitor(tmp_path, mode)
    m.step()
    assert not router.kill_switch.state().halted
    acct.value = 96_000.0
    m.step()
    assert router.kill_switch.state().halted
    assert len(_rows(tmp_path)) == 2


@pytest.mark.parametrize("mode", ["paper", "dry-run"])
def test_other_modes_do_not_get_a_watch(tmp_path: Path, mode: str) -> None:
    """Paper journals through PaperBroker. A dry-run must not be able to halt a real account."""
    m, acct, router = _live_monitor(tmp_path, mode)
    m.step()
    acct.value = 50_000.0
    m.step()
    assert not router.kill_switch.state().halted
    assert not (tmp_path / EquityWatch.FILENAME).exists()
