"""A paper session and its money counterpart are the same session, apart from what has to differ.

The money side is checked by comparing plans and by what refuses to start. Nothing here builds a live router,
sets a live execution mode in the environment or talks to a broker: the only session built runs on the paper
broker over a stub feed.
"""
from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from trading_live_claude import cli
from trading_live_claude.audit.ledger import read_stream
from trading_live_claude.audit.queries import all_rows, fill_quality, session_plans
from trading_live_claude.brokers.base import BrokerError
from trading_live_claude.brokers.models import Account, Candle, Quote
from trading_live_claude.brokers.paper import PaperBroker
from trading_live_claude.config.settings import Settings
from trading_live_claude.execution.daily_budget import DailyBudget
from trading_live_claude.intel.routing import OverlayProvider
from trading_live_claude.monitor import session as sess
from trading_live_claude.monitor.doctor import missing_for_money, wiring_rows
from trading_live_claude.monitor.session import (
    NECESSARY_DIFFERENCES,
    VENUES,
    SessionError,
    SessionOptions,
    StrategyBook,
    build_session,
    check_money_ready,
    plan_differences,
    plan_session,
    resolve_book,
    resolve_symbols,
)
from trading_live_claude.strategies.examples.ema_crossover import EmaCrossover

REPO = Path(__file__).resolve().parents[1]


def _settings(tmp: Path | None = None, **kw: Any) -> Settings:
    base: dict[str, Any] = {"worldmonitor_api_key": "", "telegram_bot_token": "", "smtp_host": ""}
    if tmp is not None:
        base |= {"state_dir": tmp, "data_cache_dir": tmp / "cache"}
    return Settings(_env_file=None, **(base | kw))  # type: ignore[call-arg]


def _book() -> StrategyBook:
    return StrategyBook(default=EmaCrossover())


def _plan(settings: Settings, venue: str = "questrade", mode: str = "paper", **kw: Any) -> sess.SessionPlan:
    return plan_session(settings, venue=venue, mode=mode, symbols=["AAA"], book=_book(), **kw)  # type: ignore[arg-type]


# ---------------------------------------------------------------- paper and money are one plan

@pytest.mark.parametrize("venue", VENUES)
def test_a_money_plan_differs_from_paper_only_where_it_must(venue: str) -> None:
    diff = plan_differences(_plan(_settings(), venue, "paper"), _plan(_settings(), venue, "live"))
    assert set(diff) <= NECESSARY_DIFFERENCES, diff
    assert diff["money"] == (False, True) and diff["strict_ledger"] == (False, True)
    assert diff["paper_equity"][1] is None


@pytest.mark.parametrize("knob", [
    {"portfolio_heat_cap": 0.02}, {"max_drawdown_kill_switch": 0.05}, {"daily_loss_limit_pct": 0.01},
    {"max_open_positions": 2}, {"min_ticket_usd": 500.0}, {"max_gross_leverage": 0.5},
    {"max_position_notional_pct": 0.1}, {"force_exit_atr_mult": 2.0}, {"on_size_cap_breach": "reject"},
    {"risk_pct_per_trade": 0.005}, {"risk_model": "atr"}, {"heat_aggregation": "sum"},
    {"respect_market_hours": False}, {"forced_exits": True}, {"entry_triggers": {"AAA": "both"}},
    {"poll_interval_seconds": 60}, {"intel_overlay": False}, {"intel_refresh_seconds": 120.0},
    {"require_card": True}, {"card_ttl_seconds": 30.0}, {"require_basket": True}, {"spectral_var_cap": 0.005},
    {"spectral_var_level": 0.95}, {"spectral_var_horizon": 5}, {"hedge_symbol": "UUP"},
])
def test_every_knob_reaches_paper_and_money_alike(knob: dict[str, Any]) -> None:
    """A practised setting has to be the setting that runs. Moving one must move both plans, and equally."""
    settings = _settings(**knob)
    base = _plan(_settings(), mode="paper")
    paper, money = _plan(settings, mode="paper"), _plan(settings, mode="live")
    assert plan_differences(base, paper), f"{knob} changed nothing"
    assert paper.fingerprint() == money.fingerprint()
    assert base.fingerprint() != paper.fingerprint()


def test_the_fingerprint_ignores_only_what_has_to_differ() -> None:
    paper = _plan(_settings(paper_starting_equity=5_000.0), mode="paper")
    assert paper.fingerprint() == _plan(_settings(), mode="live").fingerprint()
    other = plan_session(_settings(), venue="questrade", mode="live", symbols=["BBB"], book=_book())
    assert paper.fingerprint() != other.fingerprint()


def test_flags_override_the_yaml_for_one_run_in_either_mode() -> None:
    opts = SessionOptions(interval=30, spectral_var_cap=0.01, require_card=True, intel_overlay=False)
    for mode in ("paper", "live"):
        p = _plan(_settings(), mode=mode, options=opts)
        assert (p.interval_seconds, p.spectral_var_cap, p.require_card, p.intel_overlay) == (30, 0.01, True, False)


def test_overlay_defaults_on_in_every_mode() -> None:
    for mode in ("paper", "dry-run", "live", "autonomous"):
        assert _plan(_settings(), mode=mode).intel_overlay is True


def test_rehearsal_is_the_daemon_on_a_simulated_broker() -> None:
    real = _plan(_settings(), mode="autonomous")
    rehearsal = _plan(_settings(), mode="autonomous", rehearsal=True)
    assert set(plan_differences(real, rehearsal)) <= NECESSARY_DIFFERENCES
    assert rehearsal.money is False and rehearsal.paper_equity == 100_000.0
    assert real.daily_budget == rehearsal.daily_budget and real.daily_budget is not None


def test_streams_stay_separate() -> None:
    streams = [_plan(_settings(), v, m).stream for v in VENUES for m in ("paper", "live")]
    assert len(set(streams)) == len(streams)
    assert _plan(_settings(), mode="paper", options=SessionOptions(stream="qt")).stream == "qt"


def test_paper_costs_are_per_venue_and_can_be_overridden() -> None:
    assert _plan(_settings(), "kraken").paper_costs["commission_bps"] == 40.0
    assert _plan(_settings(), "questrade").paper_costs["commission_per_trade"] == 4.95
    p = _plan(_settings(paper_costs={"kraken": {"commission_bps": 26.0}}), "kraken")
    assert p.paper_costs["commission_bps"] == 26.0 and p.paper_costs["slippage_bps"] == 5.0
    assert _plan(_settings(), "kraken", "live").paper_costs == {}


def test_unknown_venue_is_refused() -> None:
    with pytest.raises(SessionError, match="Unknown venue"):
        _plan(_settings(), "binance")


# ---------------------------------------------------------------- strategies and symbols

def test_kraken_trades_its_sleeve_with_the_sleeves_parameters() -> None:
    from trading_live_claude.analysis.universe import CRYPTO_SLEEVE
    book = resolve_book(_settings(), "kraken")
    assert set(book.per_symbol) == set(CRYPTO_SLEEVE)
    btc = CRYPTO_SLEEVE["BTC/USD"]
    strat = book.per_symbol["BTC/USD"]
    assert strat.name == btc.strategy and dict(btc.params).items() <= strat.params.items()
    assert resolve_symbols(_settings(), "kraken", "", book) == list(CRYPTO_SLEEVE)


def test_the_yaml_strategy_map_reaches_any_venue() -> None:
    book = resolve_book(_settings(strategy_map="XIC.TO=bollinger"), "questrade", "macd")
    assert book.default.name == "macd" and book.per_symbol["XIC.TO"].name == "bollinger"
    assert resolve_symbols(_settings(), "questrade", "AAA", book) == ["AAA", "XIC.TO"]


def test_bad_strategy_inputs_say_what_is_wrong() -> None:
    with pytest.raises(SessionError, match="Unknown strategy"):
        resolve_book(_settings(), "questrade", "nope")
    with pytest.raises(SessionError, match="SYMBOL=strategy"):
        resolve_book(_settings(strategy_map="XIC.TO"), "questrade")


def test_ib_defaults_and_futures_join_the_symbols() -> None:
    book = resolve_book(_settings(), "ib")
    assert book.default.name == "bollinger"
    assert resolve_symbols(_settings(), "ib", "", book, ["es"]) == [*sess.DEFAULT_IB_SYMBOLS, "ES"]


# ---------------------------------------------------------------- what must be true before real money

class _Questrade:
    venue = name = "questrade"

    def __init__(self, error: str | None = None) -> None:
        self.error, self.checked = error, None

    def verify_environment(self, expected: str) -> None:
        self.checked = expected
        if self.error:
            raise BrokerError(self.error)


PHRASE = "I UNDERSTAND THE RISK"


def _live_settings(**kw: Any) -> SimpleNamespace:
    """Stands in for settings on a live run, so no test sets a real live execution mode."""
    return SimpleNamespace(execution_mode="live", questrade_env="live", ib_use_paper=False, **kw)


def test_money_refuses_without_the_execution_mode() -> None:
    with pytest.raises(SessionError, match="EXECUTION_MODE"):
        check_money_ready(_settings(), _plan(_settings(), mode="live"), _Questrade(), live_confirmation=PHRASE)  # type: ignore[arg-type]


def test_money_refuses_without_the_phrase() -> None:
    plan = _plan(_settings(), mode="live")
    with pytest.raises(SessionError, match="confirmation phrase"):
        check_money_ready(_live_settings(), plan, _Questrade(), live_confirmation="yes")  # type: ignore[arg-type]


def test_a_token_for_the_wrong_environment_refuses_to_start() -> None:
    feed = _Questrade("This Questrade token is for the live environment")
    with pytest.raises(SessionError, match="live environment"):
        check_money_ready(_live_settings(), _plan(_settings(), mode="live"), feed, live_confirmation=PHRASE)  # type: ignore[arg-type]
    assert feed.checked == "live"


def test_the_daemon_checks_the_account_it_was_told_to_use() -> None:
    feed = _Questrade()
    plan = _plan(_settings(), mode="autonomous")
    check_money_ready(_live_settings(), plan, feed, expected_env="practice")  # type: ignore[arg-type]
    assert feed.checked == "practice"


def test_kraken_money_refuses_a_screened_sleeve_unless_told() -> None:
    plan = plan_session(_settings(), venue="kraken", mode="live", symbols=["BTC/USD"], book=_book())
    with pytest.raises(SessionError, match="tier=screened"):
        check_money_ready(_live_settings(), plan, object(), live_confirmation=PHRASE)  # type: ignore[arg-type]
    check_money_ready(_live_settings(), plan, object(), live_confirmation=PHRASE, allow_screened=True)  # type: ignore[arg-type]


def test_kraken_money_needs_its_keys() -> None:
    with pytest.raises(SessionError, match="KRAKEN_API_KEY"):
        sess.venue_feed(_settings(), "kraken", money=True)


def test_paper_kraken_needs_no_keys_and_cannot_place_orders() -> None:
    feed, _ = sess.venue_feed(_settings(), "kraken", money=False)
    assert feed._enable_live_orders is False  # type: ignore[attr-defined]
    feed.close()  # type: ignore[attr-defined]


def test_autonomous_cannot_be_wrapped_by_the_card(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AUTONOMOUS_ENABLED", "true")
    settings = _settings(tmp_path, require_card=True)
    plan = _plan(settings, mode="autonomous", rehearsal=True)
    with pytest.raises(SessionError, match="cannot be wrapped"):
        build_session(settings, plan, feed=_Feed(), book=_book())  # type: ignore[arg-type]


# ---------------------------------------------------------------- building and running

class _Feed:
    """Quotes and candles only. Nothing behind it can place an order."""

    name = venue = "questrade"

    def accounts(self) -> list[Account]:
        return [Account(type="Margin", number="REAL-1", status="Active", isPrimary=True)]

    def positions(self, _a: str) -> list[Any]:
        return []

    def equity(self, *_a: Any, **_k: Any) -> float:
        return 50_000.0

    def quote(self, symbol: str) -> Quote:
        return Quote(symbol=symbol, symbolId=1, bidPrice=99.0, askPrice=101.0, lastTradePrice=100.0)

    def quotes(self, symbols: list[str]) -> list[Quote]:
        return [self.quote(s) for s in symbols]

    def candles(self, symbol: str, start: datetime, end: datetime, interval: str = "OneDay") -> list[Candle]:
        now = datetime.now(UTC)
        return [Candle(start=now - timedelta(days=i), end=now - timedelta(days=i), open=100.0, high=101.0,
                       low=99.0, close=100.0 + (i % 5), volume=1000) for i in range(400, 0, -1)]


def _ledger_rows(tmp: Path) -> list[dict[str, Any]]:
    return [r for p in sorted((tmp / "ledger").glob("*.jsonl")) for r in read_stream(p)]


def test_a_paper_session_is_built_and_runs_with_every_layer_wired(tmp_path: Path, capsys: Any) -> None:
    settings = _settings(tmp_path)
    plan = _plan(settings, options=SessionOptions(interval=5))
    session = build_session(settings, plan, feed=_Feed(), book=_book(), label="[t]", emit=print)  # type: ignore[arg-type]
    m = session.monitor
    assert isinstance(session.exec_broker, PaperBroker) and session.exec_broker._cross_spread is True
    assert m.persistence_for is not None and m.session_open_for is not None
    assert m.risk_model == "cvar" and m.heat_aggregation == "corr"
    assert session.router.mode == "paper" and session.router.max_gross_leverage == 1.0
    session.run(1)
    rows = _ledger_rows(tmp_path)
    assert [r["event"] for r in rows][:2] == ["SESSION_STARTED", "SESSION_PLAN"]
    assert rows[1]["payload"]["fingerprint"] == plan.fingerprint()
    assert session_plans(all_rows(tmp_path / "ledger"))[0]["fingerprint"] == plan.fingerprint()
    assert plan.fingerprint() in capsys.readouterr().out


def test_a_session_warns_when_a_symbol_is_not_in_the_accounts_currency(tmp_path: Path) -> None:
    settings = _settings(tmp_path, account_currency="CAD")
    said: list[str] = []
    plan = plan_session(settings, venue="questrade", mode="paper", symbols=["VOO", "XIC.TO"], book=_book())
    build_session(settings, plan, feed=_Feed(), book=_book(), emit=said.append)  # type: ignore[arg-type]
    warning = next(s for s in said if s.startswith("WARNING"))
    assert "VOO" in warning and "XIC.TO" not in warning


def test_a_refused_symbol_stops_the_session_before_anything_opens(tmp_path: Path) -> None:
    class _Broken(_Feed):
        def candles(self, *_a: Any, **_k: Any) -> list[Candle]:
            raise BrokerError("404 on candles")

    settings = _settings(tmp_path)
    with pytest.raises(SystemExit, match="REFUSING"):
        build_session(settings, _plan(settings), feed=_Broken(), book=_book())  # type: ignore[arg-type]
    assert not (tmp_path / "ledger").exists()


def test_a_session_can_opt_out_of_the_ledger_only_on_paper() -> None:
    off = SessionOptions(audit_ledger=False)
    assert _plan(_settings(), options=off).audit_ledger is False
    assert _plan(_settings(), mode="live", options=off).audit_ledger is True


def test_the_rehearsal_runs_the_daemons_gates_on_paper(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AUTONOMOUS_ENABLED", "true")
    settings = _settings(tmp_path)
    plan = _plan(settings, mode="autonomous", rehearsal=True)
    session = build_session(settings, plan, feed=_Feed(), book=_book())  # type: ignore[arg-type]
    assert session.router.mode == "autonomous" and isinstance(session.router.daily_budget, DailyBudget)
    assert isinstance(session.exec_broker, PaperBroker)
    assert session.monitor._equity_watch is None        # a paper book journals itself


def test_runners_share_one_builder() -> None:
    for path in (REPO / "src" / "trading_live_claude" / "cli.py", REPO / "scripts" / "paper_ib.py",
                 REPO / "scripts" / "paper_kraken.py"):
        src = path.read_text(encoding="utf-8")
        assert "LiveMonitor(" not in src, path.name
        assert "start_session(" in src, path.name


def test_cli_commands_exist_for_the_same_venues() -> None:
    assert callable(cli.paper) and callable(cli.live) and callable(cli.doctor)


# ---------------------------------------------------------------- the slow feed

def test_a_background_overlay_never_refreshes_on_the_poll_path() -> None:
    calls: list[int] = []

    def snap() -> Any:
        calls.append(1)
        return SimpleNamespace(degraded=False)

    provider = OverlayProvider(snap, refresh_seconds=3600.0, journal=False,
                               overlay=SimpleNamespace(evaluate=lambda _s: {}))  # type: ignore[arg-type]
    provider.start_background()
    assert len(calls) == 1                               # the first read happens up front
    provider._ts = 0.0                                   # stale: an inline refresh would fire now
    provider("AAA")
    assert len(calls) == 1


def test_without_background_it_still_refreshes_inline() -> None:
    calls: list[int] = []

    def snap() -> Any:
        calls.append(1)
        return SimpleNamespace(degraded=False)

    provider = OverlayProvider(snap, refresh_seconds=0.0, journal=False,
                               overlay=SimpleNamespace(evaluate=lambda _s: {}))  # type: ignore[arg-type]
    provider("AAA")
    assert len(calls) == 1


# ---------------------------------------------------------------- the doctor

def test_the_doctor_never_shows_a_secret() -> None:
    secret = "SECRET-VALUE-123"
    settings = _settings(questrade_refresh_token=secret, token_encryption_key=secret, kraken_api_key=secret,
                         kraken_api_secret=secret, worldmonitor_api_key=secret, anthropic_api_key=secret,
                         telegram_bot_token=secret, telegram_chat_id=secret, smtp_host=secret)
    rows = wiring_rows(settings)
    assert all(secret not in " ".join((r.layer, r.item, r.state, r.needed_for)) for r in rows)
    assert {r.state for r in rows if r.item == "KRAKEN_API_KEY"} == {"set"}
    assert missing_for_money(settings) == {"questrade": [], "kraken": [], "ib": []}


def test_the_doctor_names_what_is_missing() -> None:
    missing = missing_for_money(_settings(questrade_refresh_token="", token_encryption_key="", kraken_api_key="",
                                          kraken_api_secret=""))
    assert missing["kraken"] == ["KRAKEN_API_KEY", "KRAKEN_API_SECRET"]
    assert missing["questrade"] == ["QUESTRADE_REFRESH_TOKEN", "TOKEN_ENCRYPTION_KEY"]


def test_every_secret_is_named_in_the_env_template() -> None:
    template = (REPO / ".env.example").read_text(encoding="utf-8")
    for key in ("QUESTRADE_REFRESH_TOKEN", "TOKEN_ENCRYPTION_KEY", "KRAKEN_API_KEY", "KRAKEN_API_SECRET",
                "WORLDMONITOR_API_KEY", "ANTHROPIC_API_KEY", "TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID"):
        assert f"{key}=" in template, key


# ---------------------------------------------------------------- what fills cost

def _row(event: str, payload: dict[str, Any], intent: str, stream: str = "kraken", mode: str = "paper") -> dict[str, Any]:
    return {"event": event, "payload": payload, "intent_id": intent, "stream": stream, "mode": mode}


def test_fill_quality_measures_slippage_and_fees_against_the_decision_price() -> None:
    rows = [
        _row("RISK_CHECK", {"entry": 100.0}, "a"),
        _row("EXECUTION_REPORTED", {"side": "Buy", "quantity": 10, "price": 101.0, "fee": 4.04}, "a"),
        _row("RISK_CHECK", {"entry": 100.0}, "b"),
        _row("EXECUTION_REPORTED", {"side": "Sell", "quantity": 10, "price": 99.0, "fee": 3.96}, "b"),
    ]
    q = fill_quality(rows)[0]
    assert q["fills"] == 2 and q["slippage_bps"] == 100.0 and q["fee_bps"] == 40.0 and q["all_in_bps"] == 140.0


def test_fill_quality_keeps_paper_and_money_apart() -> None:
    rows = [
        _row("RISK_CHECK", {"entry": 100.0}, "a"),
        _row("EXECUTION_REPORTED", {"side": "Buy", "quantity": 1, "price": 100.5, "fee": 0.0}, "a"),
        _row("RISK_CHECK", {"entry": 100.0}, "b", "kraken-live", "live"),
        _row("EXECUTION_REPORTED", {"side": "Buy", "quantity": 1, "price": 100.9, "fee": 0.0}, "b", "kraken-live", "live"),
    ]
    assert {(q["stream"], q["slippage_bps"]) for q in fill_quality(rows)} == {("kraken", 50.0), ("kraken-live", 90.0)}


def test_fill_quality_skips_a_fill_with_no_decision_behind_it() -> None:
    assert fill_quality([_row("EXECUTION_REPORTED", {"side": "Buy", "quantity": 1, "price": 1.0}, "x")]) == []


# ---------------------------------------------------------------- the budget counts its own mode

def test_paper_orders_do_not_spend_the_autonomous_budget(tmp_path: Path) -> None:
    now = datetime.now(UTC).isoformat()
    rows = [{"ts": now, "accepted": True, "mode": "paper", "shares": 10, "entry": 100.0},
            {"ts": now, "accepted": True, "mode": "autonomous", "shares": 1, "entry": 100.0},
            {"ts": now, "accepted": True, "shares": 1, "entry": 100.0}]
    (tmp_path / "orders.jsonl").write_text("".join(json.dumps(r) + "\n" for r in rows), encoding="utf-8")
    snap = DailyBudget(tmp_path, mode="autonomous").snapshot()
    assert snap.trades_today == 2 and snap.notional_today_usd == 200.0     # an unlabelled row still counts
    assert DailyBudget(tmp_path).snapshot().trades_today == 3
