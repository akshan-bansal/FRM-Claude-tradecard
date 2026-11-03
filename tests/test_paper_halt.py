"""A paper drawdown stops paper. It stops money only when a human expressly says so."""
from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from typer.testing import CliRunner

from tests.test_router import _StubBroker
from tests.test_session import _Feed, _plan, _settings
from trading_live_claude import cli
from trading_live_claude.brokers.models import Quote
from trading_live_claude.brokers.paper import PaperBroker
from trading_live_claude.execution.router import Router
from trading_live_claude.monitor.live_loop import MonitorEvent
from trading_live_claude.monitor.session import StrategyBook, alert_emitter, build_session
from trading_live_claude.risk.kill_switch import KillSwitch
from trading_live_claude.strategies.examples.ema_crossover import EmaCrossover


def _switches(tmp: Path) -> tuple[KillSwitch, KillSwitch]:
    return KillSwitch(tmp), KillSwitch(tmp, paper=True)


def test_a_paper_trip_stops_paper_and_leaves_money_running(tmp_path: Path) -> None:
    money, paper = _switches(tmp_path)
    paper.trip("max-drawdown 12%")
    assert paper.state().halted and not money.state().halted
    assert (tmp_path / "HALTED_PAPER").exists() and not (tmp_path / "HALTED").exists()
    assert paper.paper_only_halt()


def test_the_main_switch_stops_everything(tmp_path: Path) -> None:
    money, paper = _switches(tmp_path)
    money.trip("manual")
    assert money.state().halted and paper.state().halted
    assert not paper.paper_only_halt()


def test_a_paper_books_drawdown_never_touches_the_main_sentinel(tmp_path: Path) -> None:
    class _Feed2:
        name = venue = "kraken"
        px = 100.0

        def quote(self, symbol: str) -> Quote:
            return Quote(symbol=symbol, symbolId=1, bidPrice=self.px, askPrice=self.px, lastTradePrice=self.px)

        def quotes(self, symbols: list[str]) -> list[Quote]:
            return [self.quote(s) for s in symbols]

    from trading_live_claude.brokers.models import Order, OrderAction, OrderType
    feed = _Feed2()
    book = PaperBroker(feed=feed, journal_dir=tmp_path, slippage_bps=0.0, commission_per_trade=0.0, starting_equity=1_000.0)  # type: ignore[arg-type]
    book.place_order(Order(symbol="SOL/USD", symbolId=1, accountId="PAPER-001", action=OrderAction.BUY,
                           orderType=OrderType.MARKET, totalQuantity=5.0))
    feed.px = 30.0
    book.mark_to_market()
    money, paper = _switches(tmp_path)
    assert paper.state().halted and "max-drawdown" in paper.state().reason
    assert not money.state().halted


@pytest.mark.parametrize("ack", ["", "halt", "I HAVE INVESTIGATED"])
def test_promotion_needs_the_phrase(tmp_path: Path, ack: str) -> None:
    money, paper = _switches(tmp_path)
    paper.trip("max-drawdown 12%")
    with pytest.raises(PermissionError, match="HALT MONEY"):
        money.promote_paper_halt(ack)
    assert not money.state().halted


def test_promotion_needs_a_paper_halt(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="No paper halt"):
        KillSwitch(tmp_path).promote_paper_halt("HALT MONEY")


def test_promotion_stops_money_and_says_why(tmp_path: Path) -> None:
    money, paper = _switches(tmp_path)
    paper.trip("max-drawdown 12%")
    money.promote_paper_halt("HALT MONEY")
    assert money.state().halted and "approved by a human" in money.state().reason
    assert "max-drawdown 12%" in money.state().reason


def test_clearing_paper_leaves_the_main_switch_and_its_measure_alone(tmp_path: Path) -> None:
    money, paper = _switches(tmp_path)
    money.trip("manual")
    paper.trip("max-drawdown 12%")
    paper.clear("I HAVE INVESTIGATED")
    assert not (tmp_path / "HALTED_PAPER").exists() and money.state().halted
    assert money.last_cleared() is None and paper.last_cleared() is not None
    assert paper.state().halted                        # the main switch still stops paper


def test_the_router_picks_its_scope_from_the_book(tmp_path: Path) -> None:
    assert Router.build_default(mode="paper", broker=_StubBroker(), state_dir=tmp_path).kill_switch.paper is True
    assert Router.build_default(mode="dry-run", broker=_StubBroker(), state_dir=tmp_path).kill_switch.paper is True


def test_a_rehearsal_is_paper_scoped_however_its_router_is_labelled(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AUTONOMOUS_ENABLED", "true")
    book = PaperBroker(feed=_Feed(), journal_dir=tmp_path)  # type: ignore[arg-type]
    router = Router.build_default(mode="autonomous", broker=book, state_dir=tmp_path)
    assert router.kill_switch.paper is True


def test_the_cli_promotes_only_on_the_phrase(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(cli, "get_settings", lambda: _settings(tmp_path))
    KillSwitch(tmp_path, paper=True).trip("max-drawdown 12%")
    refused = CliRunner().invoke(cli.app, ["kill", "--from-paper", "--confirm", "yes"])
    assert refused.exit_code == 2 and not KillSwitch(tmp_path).state().halted
    done = CliRunner().invoke(cli.app, ["kill", "--from-paper", "--confirm", "HALT MONEY"])
    assert done.exit_code == 0 and KillSwitch(tmp_path).state().halted


def test_the_cli_clears_only_what_it_is_told_to(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(cli, "get_settings", lambda: _settings(tmp_path))
    KillSwitch(tmp_path).trip("manual")
    KillSwitch(tmp_path, paper=True).trip("max-drawdown 12%")
    assert CliRunner().invoke(cli.app, ["clear-kill", "--ack", "I HAVE INVESTIGATED", "--paper"]).exit_code == 0
    assert (tmp_path / "HALTED").exists() and not (tmp_path / "HALTED_PAPER").exists()


def _alerts(detail: dict[str, Any]) -> list[tuple[str, str]]:
    sent: list[tuple[str, str]] = []
    alerter = SimpleNamespace(send=lambda t, b: sent.append((t, b)))
    from datetime import UTC, datetime
    on_event = alert_emitter(alerter, StrategyBook(default=EmaCrossover()), label="[paper]", currency="CAD")  # type: ignore[arg-type]
    on_event(MonitorEvent(datetime.now(UTC), "*", "halt", 0.0, detail))
    return sent


def test_a_paper_halt_asks_for_approval_and_a_real_one_does_not() -> None:
    (title, body), = _alerts({"reason": "max-drawdown 12%", "paper_only": True})
    assert "KILL-SWITCH" in title and "--from-paper" in body and "Money sessions keep running" in body
    (_title, body), = _alerts({"reason": "manual", "paper_only": False})
    assert "--from-paper" not in body and "clears it" in body


def test_the_session_alerts_once_per_trip(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    session = build_session(settings, _plan(settings), feed=_Feed(), book=StrategyBook(default=EmaCrossover()))  # type: ignore[arg-type]
    seen: list[MonitorEvent] = []
    session.monitor.on_event = seen.append
    session.monitor._note_halt()
    assert seen == []
    KillSwitch(tmp_path, paper=True).trip("max-drawdown 12%")
    session.monitor._note_halt()
    session.monitor._note_halt()
    assert [e.kind for e in seen] == ["halt"] and seen[0].detail["paper_only"] is True
    KillSwitch(tmp_path, paper=True).clear("I HAVE INVESTIGATED")
    session.monitor._note_halt()
    KillSwitch(tmp_path).trip("manual")
    session.monitor._note_halt()
    assert [e.kind for e in seen] == ["halt", "halt"] and seen[1].detail["paper_only"] is False
