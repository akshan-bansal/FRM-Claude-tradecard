"""The commands the desk runs. They must parse, and every flag must reach the session's plan.

Nothing here reaches a broker: ``start_session`` is replaced by a recorder and the plan is built from
what it was handed. The sessions that are built run on the paper broker over a stub feed.
"""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from typer.testing import CliRunner

from tests.test_session import _Feed, _plan, _settings
from trading_live_claude import cli
from trading_live_claude.execution.approval import ApprovalRouter
from trading_live_claude.monitor import session as sess
from trading_live_claude.monitor.session import (
    NECESSARY_DIFFERENCES,
    SessionError,
    SessionOptions,
    StrategyBook,
    build_session,
    plan_differences,
    plan_session,
    resolve_book,
    resolve_symbols,
    start_session,
)
from trading_live_claude.risk.quantity import QuantityRule
from trading_live_claude.strategies.examples.ema_crossover import EmaCrossover

REPO = Path(__file__).resolve().parents[1]

QT_SYMBOLS = "EQB.TO,QQQ,XIC.TO,ZEB.TO,CGL.TO,VALE,DBC,SRU.UN.TO,CRT.UN.TO,ENB.TO,XIU.TO,VDY.TO,SLF.TO,RSI.TO,ITA,LMT,RTX,NOC,GD"
QT_MAP = ("EQB.TO=ts_momentum,QQQ=ts_momentum,XIC.TO=rsi_meanrevert,ZEB.TO=atr_channel,CGL.TO=atr_channel,VALE=bollinger,"
          "DBC=bollinger,SRU.UN.TO=confirm_rsi_meanrevert,CRT.UN.TO=rsi_meanrevert,ENB.TO=confirm_bollinger,XIU.TO=composite,"
          "VDY.TO=ts_momentum,SLF.TO=bollinger,RSI.TO=bollinger,ITA=ts_momentum,LMT=ts_momentum,RTX=ts_momentum,"
          "NOC=ts_momentum,GD=ts_momentum")
QT_RUNBOOK = ["signal", "--strategy", "bollinger", "--symbols", QT_SYMBOLS, "--strategy-map", QT_MAP, "--interval", "300",
              "--paper", "--paper-equity", "100000", "--level", "--intel-overlay", "--profit-lock",
              "--profit-lock-exempt", "ts_momentum", "--candle-exit", "--candle-exit-exempt", "ts_momentum",
              "--overbought-exit", "--overbought-exit-exempt", "ts_momentum", "--oversold-entry",
              "--oversold-entry-only", "ts_momentum", "--parallel-sizing", "--mute-alerts", "RSI.TO",
              "--warmup-interval", "60", "--warmup-minutes", "60", "--no-flatten-on-exit", "--max-positions", "10"]
KRAKEN_RUNBOOK = ["--interval", "300", "--paper-equity", "100000", "--parallel-sizing", "--warmup-interval", "60",
                  "--warmup-minutes", "60", "--no-flatten-on-exit"]
CARD = ["--require-card", "--card-shim-port", "8787", "--card-ttl", "120"]


def _recorded(monkeypatch: pytest.MonkeyPatch, args: list[str]) -> dict[str, Any]:
    seen: dict[str, Any] = {}

    def record(settings: Any, **kw: Any) -> Any:
        seen.update(kw)
        raise SessionError("recorded")

    monkeypatch.setattr(cli, "get_settings", _settings)
    monkeypatch.setattr(cli, "start_session", record)
    res = CliRunner().invoke(cli.app, args)
    assert seen, res.output
    assert res.exit_code == 2                     # the recorder's refusal, not a parse error
    return seen


def _kraken_script(monkeypatch: pytest.MonkeyPatch, args: list[str]) -> dict[str, Any]:
    spec = importlib.util.spec_from_file_location("paper_kraken_script", REPO / "scripts" / "paper_kraken.py")
    mod = importlib.util.module_from_spec(spec)  # type: ignore[arg-type]
    spec.loader.exec_module(mod)  # type: ignore[union-attr]
    seen: dict[str, Any] = {}

    def record(settings: Any, **kw: Any) -> Any:
        seen.update(kw)
        return SimpleNamespace(run=lambda _n: None)

    monkeypatch.setattr(mod, "start_session", record)
    monkeypatch.setattr(mod, "get_settings", _settings)
    monkeypatch.setattr(sys, "argv", ["paper_kraken.py", *args])
    mod.main()
    return seen


def _plan_from(seen: dict[str, Any], mode: str = "paper") -> sess.SessionPlan:
    settings = _settings()
    book = resolve_book(settings, seen["venue"], seen.get("strategy", ""), seen.get("strategy_map", ""))
    symbols = resolve_symbols(settings, seen["venue"], seen.get("symbols"), book)
    return plan_session(settings, venue=seen["venue"], mode=mode, symbols=symbols, book=book,  # type: ignore[arg-type]
                        options=seen["options"])


# ---------------------------------------------------------------- the commands parse and arrive

def test_the_questrade_command_reaches_the_plan(monkeypatch: pytest.MonkeyPatch) -> None:
    p = _plan_from(_recorded(monkeypatch, QT_RUNBOOK))
    assert (p.venue, p.mode, p.stream, p.interval_seconds, p.paper_equity) == ("questrade", "paper", "qt", 300, 100_000.0)
    assert len(p.symbols) == 19 and p.strategy_map["XIU.TO"] == "composite" and p.strategy == "bollinger"
    assert p.emit_on_change_only is False and p.intel_overlay is True
    assert p.profit_lock and p.profit_lock_exempt == ("ts_momentum",)
    assert p.candle_exit and p.candle_exit_exempt == ("ts_momentum",)
    assert p.overbought_exit and p.overbought_exit_exempt == ("ts_momentum",)
    assert p.oversold_entry and p.oversold_entry_only == ("ts_momentum",)
    assert p.parallel_sizing and p.mute_alerts == ("RSI.TO",)
    assert (p.warmup_interval_seconds, p.warmup_minutes) == (60, 60.0)
    assert p.flatten_on_exit is False and p.limits["max_open_positions"] == 10


def test_the_kraken_command_reaches_the_plan(monkeypatch: pytest.MonkeyPatch) -> None:
    p = _plan_from(_kraken_script(monkeypatch, KRAKEN_RUNBOOK))
    assert (p.venue, p.interval_seconds, p.paper_equity) == ("kraken", 300, 100_000.0)
    assert p.parallel_sizing and (p.warmup_interval_seconds, p.warmup_minutes) == (60, 60.0)
    assert p.flatten_on_exit is False and p.sizing_v2 is True and p.require_card is False


def test_the_card_flags_reach_the_plan_and_bring_the_basket_gate(monkeypatch: pytest.MonkeyPatch) -> None:
    p = _plan_from(_kraken_script(monkeypatch, [*KRAKEN_RUNBOOK, *CARD]))
    assert p.require_card and (p.card_shim_port, p.card_ttl_seconds) == (8787, 120.0) and p.require_basket
    q = _plan_from(_recorded(monkeypatch, [*QT_RUNBOOK, "--require-card", "--card-shim-host", "192.168.1.20"]))
    assert q.require_card and q.card_shim_host == "192.168.1.20" and q.require_basket
    off = _plan_from(_recorded(monkeypatch, [*QT_RUNBOOK, "--require-card", "--no-require-basket"]))
    assert off.require_card and not off.require_basket


def test_what_the_runbook_practises_is_what_money_would_run(monkeypatch: pytest.MonkeyPatch) -> None:
    seen = _recorded(monkeypatch, QT_RUNBOOK)
    paper, money = _plan_from(seen), _plan_from(seen, mode="live")
    assert set(plan_differences(paper, money)) <= NECESSARY_DIFFERENCES
    assert paper.fingerprint() != _plan(_settings(), mode="paper").fingerprint()
    assert money.profit_lock and money.oversold_entry and money.limits["max_open_positions"] == 10


# ---------------------------------------------------------------- paper's own, and only paper's

def test_only_paper_closes_its_book_on_stop() -> None:
    assert _plan(_settings(), mode="paper").flatten_on_exit is True
    on_anyway = SessionOptions(flatten_on_exit=True)
    assert _plan(_settings(), mode="live", options=on_anyway).flatten_on_exit is False
    assert _plan(_settings(), mode="paper", options=SessionOptions(flatten_on_exit=False)).flatten_on_exit is False
    assert _plan(_settings(), mode="dry-run").flatten_on_exit is False


@pytest.mark.parametrize("opts", [SessionOptions(resume_session="abc"), SessionOptions(close_symbols="AAA"),
                                  SessionOptions(trim_to_slots=True)])
def test_the_one_shots_refuse_a_real_book(opts: SessionOptions) -> None:
    with pytest.raises(SessionError, match="simulated book"):
        start_session(_settings(), venue="questrade", mode="live", symbols="AAA", options=opts,
                      strategy="ema_crossover")


# ---------------------------------------------------------------- the desk's venue policy

def test_ib_takes_derivatives_and_overseas_listings_but_not_north_american_equities() -> None:
    with pytest.raises(SessionError, match="trade on Questrade"):
        start_session(_settings(), venue="ib", mode="paper", symbols="AAPL", strategy="bollinger")
    sess._check_ib_policy(["GC", "MGC"], ["GC", "MGC"])              # roots are futures by definition
    sess._check_ib_policy(["7203.T", "VOD.L", "0700.HK"], [])        # overseas listings are IB's too
    with pytest.raises(SessionError, match="not IB"):
        sess._check_ib_policy(["7203.T"], [], futures_only=True)     # the knob narrows it back to futures


# ---------------------------------------------------------------- built, on the paper broker

class _KrakenFeed(_Feed):
    name = venue = "kraken"
    rules = {"AAA": QuantityRule(step=0.01, min_qty=0.01, min_notional=5.0)}

    def lot_rules(self, _symbols: list[str]) -> dict[str, QuantityRule]:
        return self.rules


def _kraken_plan(settings: Any, **options: Any) -> sess.SessionPlan:
    return plan_session(settings, venue="kraken", mode="paper", symbols=["AAA"], book=StrategyBook(default=EmaCrossover()),
                        options=SessionOptions(**options))


def test_kraken_sizes_in_its_own_lots(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(sess, "venue_weight_bias", lambda *_a, **_k: None)
    settings = _settings(tmp_path)
    session = build_session(settings, _kraken_plan(settings), feed=_KrakenFeed(), book=StrategyBook(default=EmaCrossover()))
    assert session.monitor.sizing_policy is not None
    assert session.router.quantity_rule_for("AAA") == _KrakenFeed.rules["AAA"]
    assert session.exec_broker._cost_model.commission_bps == 26.0  # type: ignore[attr-defined]


def test_kraken_refuses_to_guess_lot_rules(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(sess, "venue_weight_bias", lambda *_a, **_k: None)
    monkeypatch.setattr(_KrakenFeed, "rules", {})
    settings = _settings(tmp_path)
    with pytest.raises(SessionError, match="lot rules"):
        build_session(settings, _kraken_plan(settings), feed=_KrakenFeed(), book=StrategyBook(default=EmaCrossover()))


def test_a_card_session_on_questrade_gates_on_the_qt_basket(tmp_path: Path) -> None:
    """The basket book calls Questrade ``qt``; a session passing its own name would read no basket at all."""
    settings = _settings(tmp_path)
    plan = _plan(settings, options=SessionOptions(require_card=True, card_attach=True))
    session = build_session(settings, plan, feed=_Feed(), book=StrategyBook(default=EmaCrossover()))
    assert isinstance(session.router, ApprovalRouter) and session.router.inner.basket_gate.venue == "qt"


def test_every_session_watches_for_a_stop_and_reads_guarded_quotes(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    session = build_session(settings, _plan(settings), feed=_Feed(), book=StrategyBook(default=EmaCrossover()))
    assert session.monitor.stop_sentinel_dir == Path(settings.state_dir)
    priced = session.exec_broker._feed  # type: ignore[attr-defined]
    assert priced.__class__.__name__ == "CurrencyNormalizingBroker" and priced.inner.__class__.__name__ == "FreshQuoteBroker"
    assert session.router.position_cap_pct_for is not None


def test_the_runner_stops_on_the_sentinel_without_flattening_when_told(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    plan = _plan(settings, options=SessionOptions(flatten_on_exit=False))
    session = build_session(settings, plan, feed=_Feed(), book=StrategyBook(default=EmaCrossover()))
    (Path(settings.state_dir) / "STOP").write_text("", encoding="utf-8")
    session.run(5)
    assert session.monitor.flatten_on_exit is False and session.monitor._stop_requested is True
