"""The paths a real-money session takes, exercised without real money.

Rules these keep: no test sets a real live execution mode (settings on a money path are a stand-in object), no test
builds a live Router (``Router.build_default`` is replaced by a paper one that records what it was asked for), and no
test reaches a broker (each venue's adapter is replaced by a fake).
"""
from __future__ import annotations

import signal
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pandas as pd
import pytest
from typer.testing import CliRunner

from tests.test_session import PHRASE, _Feed, _IBLogin, _live_settings, _plan, _settings
from trading_live_claude import cli
from trading_live_claude.brokers.base import BrokerError, StaleQuote
from trading_live_claude.brokers.fresh import FreshQuoteBroker
from trading_live_claude.brokers.fx import CurrencyNormalizingBroker
from trading_live_claude.brokers.models import Quote
from trading_live_claude.brokers.paper import PaperBroker
from trading_live_claude.execution.router import AutonomousNotEnabled, Router
from trading_live_claude.monitor import session as sess
from trading_live_claude.monitor.live_loop import MonitorEvent
from trading_live_claude.monitor.session import (
    SessionError,
    SessionOptions,
    StrategyBook,
    alert_emitter,
    allocator_weight_bias,
    book_with_params,
    build_session,
    check_money_ready,
    intel_overlay_hooks,
    options_from_args,
    plan_session,
    start_session,
    venue_feed,
    venue_weight_bias,
)
from trading_live_claude.risk.kill_switch import KillSwitch
from trading_live_claude.strategies.examples.ema_crossover import EmaCrossover


def _book() -> StrategyBook:
    return StrategyBook(default=EmaCrossover())


# ---------------------------------------------------------------- the overlay, with a key

class _FakeClient:
    instances = 0

    def __init__(self, key: str) -> None:
        self.key = key
        _FakeClient.instances += 1

    async def __aenter__(self) -> _FakeClient:
        return self

    async def __aexit__(self, *_a: Any) -> None:
        return None

    async def snapshot(self) -> str:
        return f"snapshot for {self.key}"


def test_a_key_builds_the_overlay_and_interpret_reads_its_snapshot(monkeypatch: pytest.MonkeyPatch) -> None:
    from trading_live_claude.intel import interpret as interpret_module
    from trading_live_claude.intel import worldmonitor
    monkeypatch.setattr(worldmonitor, "WorldMonitorClient", _FakeClient)
    monkeypatch.setattr(interpret_module, "interpret", lambda snap: [f"thesis on {snap}"])
    lines: list[str] = []
    provider, interpret_for = intel_overlay_hooks(SimpleNamespace(worldmonitor_api_key="k"), 30.0, background=False, emit=lines.append)
    assert provider is not None and interpret_for() == []                      # nothing read yet: a no-op
    assert provider._snapshot_fn() == "snapshot for k"
    provider._last_snapshot = "a read"
    assert interpret_for() == ["thesis on a read"]
    assert any("intel overlay ON" in x for x in lines) and any("interpret" in x for x in lines)


def test_background_starts_the_provider_once(monkeypatch: pytest.MonkeyPatch) -> None:
    from trading_live_claude.intel import worldmonitor
    monkeypatch.setattr(worldmonitor, "WorldMonitorClient", _FakeClient)
    started: list[int] = []
    monkeypatch.setattr(sess.OverlayProvider, "start_background", lambda self: started.append(1))
    intel_overlay_hooks(SimpleNamespace(worldmonitor_api_key="k"))
    assert started == [1]


def test_no_key_means_no_overlay() -> None:
    assert intel_overlay_hooks(SimpleNamespace(worldmonitor_api_key="")) == (None, None)


def test_a_session_with_a_key_wires_the_overlay_into_the_monitor(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    provider, interpret = object(), lambda: []
    monkeypatch.setattr(sess, "intel_overlay_hooks", lambda *_a, **_k: (provider, interpret))
    monkeypatch.setattr(sess, "venue_weight_bias", lambda *_a, **_k: None)
    settings = _settings(tmp_path, worldmonitor_api_key="k")
    m = build_session(settings, _plan(settings), feed=_Feed(), book=_book()).monitor  # type: ignore[arg-type]
    assert m.overlay_for is provider and m.interpret_for is interpret


# ---------------------------------------------------------------- each venue's adapter

def test_questrade_is_no_longer_a_venue() -> None:
    with pytest.raises(SessionError, match="Unknown venue 'questrade'"):
        venue_feed(SimpleNamespace(), "questrade", money=True)


def test_ib_auto_is_the_regional_access_layer_and_opens_nothing_yet() -> None:
    from trading_live_claude.brokers.ib_access import IBAccess, LazyTickle
    access, keepalive = venue_feed(_ib_settings(ib_access={}), "ib", money=True, futures=["GC"])
    assert isinstance(access, IBAccess) and isinstance(keepalive, LazyTickle)
    assert access.built("socket") is None and access.built("web") is None
    assert access.order_transport("/GC") == "web" and access.order_transport("XIC.TO") == "socket"


def test_ib_auto_follows_the_yaml_plan_and_refuses_a_bad_one() -> None:
    access, _ = venue_feed(_ib_settings(ib_access={"orders": {"asia_pacific": "web"}}), "ib", money=False)
    assert access.order_transport("7203.T") == "web"  # type: ignore[attr-defined]
    with pytest.raises(SessionError, match="read-only"):
        venue_feed(_ib_settings(ib_access={"orders": {"europe": "connector"}}), "ib", money=False)


def test_kraken_places_orders_only_with_money_and_keys() -> None:
    with pytest.raises(SessionError, match="KRAKEN_API_KEY"):
        venue_feed(SimpleNamespace(kraken_api_key="", kraken_api_secret=""), "kraken", money=True)
    paper, _ = venue_feed(SimpleNamespace(kraken_api_key="", kraken_api_secret=""), "kraken", money=False)
    assert paper._enable_live_orders is False
    money, _ = venue_feed(SimpleNamespace(kraken_api_key="k", kraken_api_secret="c2VjcmV0"), "kraken", money=True)
    assert money._enable_live_orders is True


def _ib_settings(**kw: Any) -> SimpleNamespace:
    base = {"ib_web_host": "localhost", "ib_web_port": 5000, "ib_web_verify_ssl": False, "ib_auto_confirm_prompts": False,
            "ib_paper_port": 7497, "ib_live_port": 7496, "ib_use_paper": True, "ib_host": "127.0.0.1", "ib_client_id": 1,
            "ib_account": ""}
    return SimpleNamespace(**(base | kw))


def test_ib_web_resolves_roots_as_futures_and_comes_with_a_keepalive() -> None:
    web, keepalive = venue_feed(_ib_settings(), "ib", money=True, transport="web", futures=["gc", "MGC"])
    assert web._enable_live_orders is True and keepalive is not None
    assert web._sec_type_for("GC") == "FUT" and web._sec_type_for("MGC") == "FUT"
    assert web._auto_confirm_prompts is False


def test_ib_socket_takes_no_futures_flag_and_knows_its_port(monkeypatch: pytest.MonkeyPatch) -> None:
    with pytest.raises(SessionError, match="--futures"):
        venue_feed(_ib_settings(), "ib", money=False, transport="socket", futures=["GC"])
    from trading_live_claude.brokers import ib as ib_module
    made: list[dict[str, Any]] = []
    monkeypatch.setattr(ib_module, "IBBroker", lambda **kw: made.append(kw) or "socket")
    assert venue_feed(_ib_settings(ib_use_paper=False), "ib", money=True, transport="socket") == ("socket", None)
    assert made[0]["port"] == 7496 and made[0]["enable_live_orders"] is True and made[0]["readonly_market_data"] is False
    venue_feed(_ib_settings(), "ib", money=False, transport="socket")
    assert made[1]["port"] == 7497 and made[1]["enable_live_orders"] is False and made[1]["readonly_market_data"] is True


def test_an_unknown_transport_is_refused() -> None:
    with pytest.raises(SessionError, match="transport"):
        venue_feed(_ib_settings(), "ib", money=False, transport="pigeon")


# ---------------------------------------------------------------- what must be true before money moves

def test_the_phrase_passes_a_live_ib_session() -> None:
    check_money_ready(_live_settings(), _plan(_settings(), mode="live"), _IBLogin("U1"), live_confirmation=PHRASE)  # type: ignore[arg-type]


def test_a_screened_kraken_sleeve_passes_only_when_told() -> None:
    plan = plan_session(_settings(), venue="kraken", mode="live", symbols=["BTC/USD"], book=_book())
    with pytest.raises(SessionError, match="--allow-screened"):
        check_money_ready(_live_settings(), plan, _Feed(), live_confirmation=PHRASE)  # type: ignore[arg-type]
    check_money_ready(_live_settings(), plan, _Feed(), live_confirmation=PHRASE, allow_screened=True)  # type: ignore[arg-type]


def test_a_symbol_outside_the_sleeve_is_not_held_back() -> None:
    plan = plan_session(_settings(), venue="kraken", mode="live", symbols=["NOPE/USD"], book=_book())
    check_money_ready(_live_settings(), plan, _Feed(), live_confirmation=PHRASE)  # type: ignore[arg-type]


def test_ib_says_when_it_is_on_its_own_paper_account() -> None:
    plan = plan_session(_settings(), venue="ib", mode="live", symbols=["/GC"], book=_book())
    lines: list[str] = []
    settings = SimpleNamespace(execution_mode="live", ib_use_paper=True)
    check_money_ready(settings, plan, _Feed(), live_confirmation=PHRASE, emit=lines.append)  # type: ignore[arg-type]
    assert any("IB_USE_PAPER" in x for x in lines)


def test_the_daemon_needs_no_phrase_but_its_login_is_still_checked() -> None:
    plan = _plan(_settings(), mode="autonomous")
    check_money_ready(_live_settings(), plan, _IBLogin("DU1"), expected_env="practice")  # type: ignore[arg-type]
    with pytest.raises(SessionError, match="live account"):
        check_money_ready(_live_settings(), plan, _IBLogin("U1"), expected_env="practice")  # type: ignore[arg-type]


# ---------------------------------------------------------------- a money session's object graph

class _PaperStandIn:
    """Builds a paper router where a money router was asked for, and records what was asked."""

    def __init__(self) -> None:
        self.asked: dict[str, Any] = {}
        self.real = Router.build_default

    def __call__(self, **kw: Any) -> Router:
        self.asked = dict(kw)
        kw.update(mode="paper", live_confirmation=None)
        kw.pop("daily_max_trades", None)
        kw.pop("daily_max_notional_usd", None)
        return self.real(**kw)


def _money_session(tmp: Path, monkeypatch: pytest.MonkeyPatch, *, mode: str = "live", **options: Any) -> tuple[sess.Session, _PaperStandIn]:
    stand_in = _PaperStandIn()
    monkeypatch.setattr(sess.Router, "build_default", stand_in)
    monkeypatch.setattr(sess, "check_money_ready", lambda *_a, **_k: None)
    monkeypatch.setattr(sess, "venue_weight_bias", lambda *_a, **_k: None)
    settings = _settings(tmp)
    plan = _plan(settings, mode=mode, options=SessionOptions(**options))
    session = build_session(settings, plan, feed=_Feed(), book=_book(), live_confirmation=PHRASE)  # type: ignore[arg-type]
    return session, stand_in


def test_a_money_session_goes_to_the_real_broker_through_the_guards(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    session, stand_in = _money_session(tmp_path, monkeypatch)
    assert stand_in.asked["mode"] == "live" and stand_in.asked["live_confirmation"] == PHRASE
    assert isinstance(session.exec_broker, CurrencyNormalizingBroker)
    assert isinstance(session.exec_broker.inner, FreshQuoteBroker) and not isinstance(session.exec_broker, PaperBroker)
    assert stand_in.asked["broker"] is session.exec_broker
    assert stand_in.asked["cost_model"] is None             # the venue's real fee shape, not paper's override
    assert stand_in.asked["position_cap_pct_for"] is not None


def test_a_money_session_never_flattens_and_its_ledger_is_strict(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    session, _ = _money_session(tmp_path, monkeypatch, flatten_on_exit=True)
    assert session.monitor.flatten_on_exit is False
    assert session.ledger is not None and session.ledger.strict is True and session.ledger.mode == "live"
    assert session.monitor.stop_sentinel_dir == Path(_settings(tmp_path).state_dir)


def test_a_money_session_trades_the_named_account_or_the_logins_first(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    session, _ = _money_session(tmp_path, monkeypatch)
    assert session.monitor.account_number == "REAL-1"
    monkeypatch.setattr(sess.Router, "build_default", _PaperStandIn())
    settings = _settings(tmp_path)
    named = build_session(settings, _plan(settings, mode="live"), feed=_Feed(), book=_book(),  # type: ignore[arg-type]
                          account_number="CHOSEN", live_confirmation=PHRASE)
    assert named.monitor.account_number == "CHOSEN"


def test_a_login_with_no_accounts_stops_the_session(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    class _Empty(_Feed):
        def accounts(self) -> list[Any]:
            return []

    monkeypatch.setattr(sess.Router, "build_default", _PaperStandIn())
    monkeypatch.setattr(sess, "check_money_ready", lambda *_a, **_k: None)
    monkeypatch.setattr(sess, "venue_weight_bias", lambda *_a, **_k: None)
    settings = _settings(tmp_path)
    with pytest.raises(SessionError, match="No accounts"):
        build_session(settings, _plan(settings, mode="live"), feed=_Empty(), book=_book())  # type: ignore[arg-type]


def test_the_daemon_gets_its_budget_and_no_card(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _session, stand_in = _money_session(tmp_path, monkeypatch, mode="autonomous")
    assert stand_in.asked["mode"] == "autonomous" and "daily_max_trades" in stand_in.asked
    settings = _settings(tmp_path)
    with pytest.raises(SessionError, match="cannot be wrapped"):
        build_session(settings, _plan(settings, mode="autonomous", options=SessionOptions(require_card=True)),
                      feed=_Feed(), book=_book())  # type: ignore[arg-type]


def test_a_refused_router_is_a_session_error(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    def refuse(**_k: Any) -> Router:
        raise AutonomousNotEnabled("AUTONOMOUS_ENABLED is off")

    monkeypatch.setattr(sess.Router, "build_default", refuse)
    monkeypatch.setattr(sess, "check_money_ready", lambda *_a, **_k: None)
    monkeypatch.setattr(sess, "venue_weight_bias", lambda *_a, **_k: None)
    settings = _settings(tmp_path)
    with pytest.raises(SessionError, match="AUTONOMOUS_ENABLED"):
        build_session(settings, _plan(settings, mode="autonomous"), feed=_Feed(), book=_book())  # type: ignore[arg-type]


def test_money_readiness_runs_before_anything_opens(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    def refuse(*_a: Any, **_k: Any) -> None:
        raise SessionError("not ready")

    monkeypatch.setattr(sess, "check_money_ready", refuse)
    settings = _settings(tmp_path)
    with pytest.raises(SessionError, match="not ready"):
        build_session(settings, _plan(settings, mode="live"), feed=_Feed(), book=_book())  # type: ignore[arg-type]
    assert not (tmp_path / "ledger").exists()


def test_a_keepalive_warns_through_the_alerts(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(sess, "venue_weight_bias", lambda *_a, **_k: None)
    keepalive = SimpleNamespace(warn_fn=None, start=lambda: None, stop=lambda: None)
    settings = _settings(tmp_path)
    session = build_session(settings, _plan(settings), feed=_Feed(), book=_book(), keepalive=keepalive)  # type: ignore[arg-type]
    assert callable(keepalive.warn_fn) and session.keepalive is keepalive


# ---------------------------------------------------------------- running it

class _Monitor:
    flatten_on_exit = False

    def __init__(self) -> None:
        self.stopped = 0
        self.ran: list[int | None] = []
        self.closed: list[Any] = []
        self.trimmed = 0

    def request_stop(self) -> None:
        self.stopped += 1

    def run_forever(self, max_iterations: int | None = None) -> None:
        self.ran.append(max_iterations)

    def close_symbols(self, symbols: Any) -> list[dict[str, Any]]:
        self.closed.append(symbols)
        return [{"symbol": "AAA", "qty": 2.0, "price": 10.0, "accepted": True, "reason": ""},
                {"symbol": "BBB", "qty": 1.0, "accepted": False, "reason": "venue closed"}]

    def trim_to_slots(self) -> list[dict[str, Any]]:
        self.trimmed += 1
        return [{"symbol": "AAA", "sold": 1.0, "held": 2.0, "price": 10.0, "kept": 1.0, "scale": 0.5, "accepted": True},
                {"symbol": "BBB", "sold": 1.0, "held": 2.0, "price": 10.0, "kept": 1.0, "scale": 0.5, "accepted": False}]


def _runnable(**kw: Any) -> tuple[sess.Session, _Monitor, list[str]]:
    monitor, lines = _Monitor(), []
    session = sess.Session(plan=None, monitor=monitor, router=None, exec_broker=None, market=None, ledger=None,  # type: ignore[arg-type]
                           feed=SimpleNamespace(), emit=lines.append, **kw)
    return session, monitor, lines


def test_run_starts_and_stops_the_keepalive_and_closes_the_feed() -> None:
    events: list[str] = []
    keepalive = SimpleNamespace(start=lambda: events.append("start"), stop=lambda: events.append("stop"))
    session, monitor, _ = _runnable(keepalive=keepalive)
    session.feed = SimpleNamespace(close=lambda: events.append("close"))
    session.run(3)
    assert monitor.ran == [3] and events == ["start", "stop", "close"]


def test_run_stops_the_keepalive_even_when_the_loop_raises() -> None:
    events: list[str] = []
    session, monitor, _ = _runnable(keepalive=SimpleNamespace(start=lambda: None, stop=lambda: events.append("stop")))

    def boom(max_iterations: int | None = None) -> None:
        raise RuntimeError("loop died")

    monitor.run_forever = boom  # type: ignore[method-assign]
    with pytest.raises(RuntimeError):
        session.run()
    assert events == ["stop"]


def test_the_one_shots_run_first_and_report(monkeypatch: pytest.MonkeyPatch) -> None:
    session, monitor, lines = _runnable(close_symbols=("AAA", "BBB"), trim_to_slots=True)
    session.run(1)
    assert monitor.closed == [("AAA", "BBB")] and monitor.trimmed == 1
    assert any("close AAA" in x and "ACCEPTED" in x for x in lines) and any("NOT CLOSED: venue closed" in x for x in lines)
    assert any("slot trim AAA" in x and "ACCEPTED" in x for x in lines) and any("REJECTED by the Router" in x for x in lines)


def test_no_one_shots_means_none_run() -> None:
    session, monitor, _ = _runnable()
    session.run(1)
    assert monitor.closed == [] and monitor.trimmed == 0


def test_a_signal_finishes_the_poll_rather_than_killing_it(monkeypatch: pytest.MonkeyPatch) -> None:
    installed: dict[int, Any] = {}
    monkeypatch.setattr(signal, "signal", lambda s, h: installed.__setitem__(s, h))
    session, monitor, lines = _runnable()
    session.run(1)
    assert signal.SIGINT in installed and signal.SIGTERM in installed
    installed[signal.SIGTERM](signal.SIGTERM, None)
    assert monitor.stopped == 1 and any("finishing poll, then exiting" in x for x in lines)
    monitor.flatten_on_exit = True
    installed[signal.SIGINT](signal.SIGINT, None)
    assert any("then flattening" in x for x in lines)


def test_a_session_off_the_main_thread_still_runs(monkeypatch: pytest.MonkeyPatch) -> None:
    def refuse(*_a: Any) -> None:
        raise ValueError("signal only works in main thread")

    monkeypatch.setattr(signal, "signal", refuse)
    session, monitor, _ = _runnable()
    session.run(1)
    assert monitor.ran == [1]


# ---------------------------------------------------------------- resuming a simulated book

def test_a_session_resumes_its_book_and_a_broken_journal_stops_it(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(sess, "venue_weight_bias", lambda *_a, **_k: None)
    monkeypatch.setattr(PaperBroker, "resume", lambda self: {"fills_replayed": 3, "positions": {"AAA": 1.0}, "cash": 10.0, "realized_pnl": 1.0})
    settings = _settings(tmp_path)
    lines: list[str] = []
    plan = _plan(settings, options=SessionOptions(resume_session="abc"))
    session = build_session(settings, plan, feed=_Feed(), book=_book(), emit=lines.append)  # type: ignore[arg-type]
    assert session.exec_broker.session_id == "abc" and any("RESUMED session abc" in x for x in lines)  # type: ignore[attr-defined]

    def broken(self: PaperBroker) -> dict[str, Any]:
        raise PaperBroker.RehydrationMismatch("journals disagree")

    monkeypatch.setattr(PaperBroker, "resume", broken)
    with pytest.raises(SessionError, match="journals disagree"):
        build_session(settings, plan, feed=_Feed(), book=_book())  # type: ignore[arg-type]


# ---------------------------------------------------------------- the card path's thesis

def _thesis(overlay: Any, bias: Any, monitors: list[Any]) -> tuple[Any, dict[str, Any]]:
    seen: dict[str, Any] = {}

    def explain(_intent: Any, **kw: Any) -> tuple[str, str]:
        seen.update(kw)
        return "thesis", "ref"

    return sess._card_thesis(SimpleNamespace(explain=explain), overlay, bias, monitors), seen  # type: ignore[arg-type]


INTENT = SimpleNamespace(symbol="AAA", shares=2.0, entry=100.0, stop=90.0, target=130.0)


def test_the_card_sees_the_sizing_the_session_really_applied() -> None:
    dec = SimpleNamespace(scalar=0.5, asset_class="equity", reasons=["energy"], halt_new_entries=True)
    overlay = lambda _s: dec  # noqa: E731
    overlay.last_snapshot = "snap"  # type: ignore[attr-defined]
    monitor = SimpleNamespace(_interpret_bias=lambda _s: (0.8, ["Oil shock"]))
    fn, seen = _thesis(overlay, lambda _s: 1.5, [monitor])
    assert fn(INTENT, "kraken") == ("thesis", "ref")
    chain = " | ".join(seen["sizing_chain"])
    assert "Order size: 2 units" in chain and "x0.500" in chain and "(energy)" in chain and "HALT" in chain
    assert "Interpret bias: x0.800" in chain and "Oil shock" in chain and "Allocator weight: x1.50 (boost)" in chain
    assert seen["overlay_snapshot"] == "snap" and seen["overlay_decisions"] == [dec]
    assert seen["market"].r_multiple == 3.0 and seen["broker"] == "kraken"


def test_a_thesis_with_nothing_to_add_is_just_the_size() -> None:
    fn, seen = _thesis(None, None, [])
    fn(SimpleNamespace(symbol="AAA", shares=2.0, entry=100.0, stop=100.0, target=None), "ib")
    assert len(seen["sizing_chain"]) == 1 and seen["overlay_decisions"] is None and seen["market"].r_multiple is None


def test_a_broken_advisory_row_never_blocks_the_prompt() -> None:
    def boom(_s: str) -> float:
        raise RuntimeError("bias failed")

    fn, seen = _thesis(None, boom, [])
    assert fn(INTENT, "ib") == ("thesis", "ref") and len(seen["sizing_chain"]) == 1


def test_a_bad_intent_still_gets_a_thesis() -> None:
    fn, seen = _thesis(None, None, [])
    fn(SimpleNamespace(symbol="AAA", shares=2.0, entry=100.0, stop=None, target=None), "ib")
    assert seen["market"].r_multiple is None


def test_a_card_session_binds_a_shim_unless_attached(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(sess, "venue_weight_bias", lambda *_a, **_k: None)
    asked: list[dict[str, Any]] = []

    def wire(router: Any, **kw: Any) -> Any:
        asked.append(kw)
        return SimpleNamespace(router=router, shim_url="http://x", auth_token="t", desk_url="http://x/desk")

    monkeypatch.setattr(sess, "wire_card_approval", wire)
    settings = _settings(tmp_path)
    lines: list[str] = []
    book = _book()
    build_session(settings, _plan(settings, "kraken", options=SessionOptions(require_card=True, sizing_v2=False, card_shim_host="10.0.0.2")),
                  feed=_Feed(), book=book, emit=lines.append)  # type: ignore[arg-type]
    assert asked[0]["start_shim"] is True and asked[0]["shim_host"] == "10.0.0.2"
    ref = asked[0]["books"][0]
    assert (ref.venue, ref.currency, ref.asset_class) == ("kraken", "CAD", "crypto")
    assert any("approval shim" in x for x in lines) and any("desk panel" in x for x in lines)
    lines.clear()
    build_session(settings, _plan(settings, "kraken", options=SessionOptions(require_card=True, sizing_v2=False, card_attach=True)),
                  feed=_Feed(), book=book, emit=lines.append)  # type: ignore[arg-type]
    assert asked[1]["start_shim"] is False and asked[1]["books"] is None and any("ATTACHED" in x for x in lines)


# ---------------------------------------------------------------- allocators

class _Market:
    def __init__(self, fail: set[str] | None = None, frame: Any = None) -> None:
        self.fail, self.frame = fail or set(), frame

    def _df(self, symbol: str) -> pd.DataFrame:
        if symbol in self.fail:
            raise BrokerError("no bars")
        if self.frame is not None:
            return self.frame
        rng = pd.Series([100.0 + i * (1 + hash(symbol) % 3) for i in range(260)])
        return pd.DataFrame({"close": rng})

    def recent(self, symbol: str, **_k: Any) -> pd.DataFrame:
        return self._df(symbol)

    def history(self, symbol: str, **_k: Any) -> pd.DataFrame:
        return self._df(symbol)


def _validated_pair() -> list[str]:
    from trading_live_claude.analysis.universe import WALK_FORWARD_VALIDATED as wfv
    return list(wfv)[:2]


def test_the_equity_allocator_is_neutral_outside_the_validated_pool() -> None:
    assert allocator_weight_bias(_Market(), ["NOT-VALIDATED"]) is None


def test_the_equity_allocator_weights_the_validated_pool_and_prints_it() -> None:
    lines: list[str] = []
    syms = _validated_pair()
    fn = allocator_weight_bias(_Market(), syms, emit=lines.append)
    assert callable(fn) and fn("OTHER") == 1.0 and any("conviction bias" in x for x in lines)


def test_a_failed_fetch_leaves_that_symbol_out(monkeypatch: pytest.MonkeyPatch) -> None:
    syms = _validated_pair()
    assert callable(allocator_weight_bias(_Market(fail={syms[0]}), syms))


def test_the_allocator_failing_outright_turns_it_off_and_says_so(monkeypatch: pytest.MonkeyPatch) -> None:
    from trading_live_claude.portfolio import allocator
    monkeypatch.setattr(allocator.PortfolioAllocator, "allocate", lambda *_a, **_k: (_ for _ in ()).throw(ValueError("singular")))
    lines: list[str] = []
    assert allocator_weight_bias(_Market(), _validated_pair(), emit=lines.append) is None
    assert any("allocator bias OFF" in x for x in lines)


def test_the_overlay_goes_through_the_allocation_when_there_is_one() -> None:
    overlay = lambda _s: None  # noqa: E731
    lines: list[str] = []
    fn = allocator_weight_bias(_Market(), _validated_pair(), overlay, emit=lines.append)
    assert isinstance(fn, sess.OverlaidBias) and any("apply_overlay" in x for x in lines)


def test_kraken_uses_its_sleeve_and_applies_the_overlay_when_it_can(monkeypatch: pytest.MonkeyPatch) -> None:
    from trading_live_claude.portfolio import allocator, sleeve_bias
    result = allocator.PortfolioAllocator(max_weight=1.0, max_sleeve_weight=1.0, min_score=0.0).allocate(
        {"A": pd.Series([0.01, -0.01, 0.02] * 30)}, {"A": 1.0}, regime_scalar=1.0)
    monkeypatch.setattr(sleeve_bias, "sleeve_allocation", lambda _p: ({"A": 1.5}, result, 0.5))
    fn = venue_weight_bias("kraken", _Market(), ["A"], None)
    assert fn("A") == 1.5 and fn("B") == 1.0
    assert isinstance(venue_weight_bias("kraken", _Market(), ["A"], lambda _s: None), sess.OverlaidBias)
    monkeypatch.setattr(sleeve_bias, "sleeve_allocation", lambda _p: ({"A": 1.0}, None, 0.0))
    assert not isinstance(venue_weight_bias("kraken", _Market(), ["A"], lambda _s: None), sess.OverlaidBias)


def test_ib_equities_take_ibs_allocator(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: list[Any] = []
    monkeypatch.setattr(sess, "_ib_weight_bias", lambda *a, **_k: seen.append(a) or "ib")
    assert venue_weight_bias("ib", _Market(), ["XIC.TO"], None) == "ib" and seen


def test_ib_holds_bonds_back_until_the_overlay_says_risk_off(monkeypatch: pytest.MonkeyPatch) -> None:
    decision = SimpleNamespace(scalar=0.9)
    overlay = lambda _s: decision  # noqa: E731
    fn = venue_weight_bias("ib", _Market(fail={"TLT"}), ["SPY", "TLT"], overlay)
    assert fn("TLT") == 0.0 and fn("SPY") >= 0.0
    decision.scalar = 0.4
    assert fn("TLT") == sess._HEDGE_BIAS


def test_an_ib_hedge_with_no_overlay_stays_dormant_and_a_broken_one_too() -> None:
    assert venue_weight_bias("ib", _Market(), ["TLT"], None)("TLT") == 0.0

    def boom(_s: str) -> Any:
        raise RuntimeError("overlay broke")

    assert venue_weight_bias("ib", _Market(), ["TLT"], boom)("TLT") == 0.0


def test_ib_skips_a_symbol_with_too_little_history() -> None:
    short = pd.DataFrame({"close": [1.0, 2.0, 3.0]})
    lines: list[str] = []
    fn = venue_weight_bias("ib", _Market(frame=short), ["SPY"], None, emit=lines.append)
    assert fn("SPY") == 1.0


def test_ib_says_when_a_fetch_failed() -> None:
    lines: list[str] = []
    venue_weight_bias("ib", _Market(fail={"SPY"}), ["SPY"], None, emit=lines.append)
    assert any("SPY fetch failed" in x for x in lines)


# ---------------------------------------------------------------- alerts on a money path

def _sent(event: MonitorEvent, **kw: Any) -> list[tuple[str, str]]:
    sent: list[tuple[str, str]] = []
    on_event = alert_emitter(SimpleNamespace(send=lambda t, b: sent.append((t, b))), _book(), label="[qt-live]", currency="CAD", **kw)  # type: ignore[arg-type]
    on_event(event)
    return sent


def _event(kind: str, detail: Any) -> MonitorEvent:
    return MonitorEvent(datetime.now(UTC), "AAA", kind, 10.0, detail)


def test_every_alert_names_its_session_and_its_currency() -> None:
    (title, body), = _sent(_event("entry", {"sized": 2.5}))
    assert title.startswith("[qt-live]") and body.startswith("Prices in CAD.") and "2.5 shares" in body


def test_an_exit_alert_carries_a_fractional_size_and_its_reason() -> None:
    (title, body), = _sent(_event("exit", {"shares": 2.5, "reason": "profit_lock"}))
    assert "profit_lock" in title and "2.5" in body


def test_an_exit_with_no_detail_still_alerts() -> None:
    assert len(_sent(_event("exit", None))) == 1


def test_a_hedge_alerts_and_a_hold_does_not() -> None:
    assert len(_sent(_event("hedge", {"target_shares": 10, "delta": 2, "weight": 0.1, "equity": 1000.0}))) == 1
    lines: list[str] = []
    assert _sent(_event("hold", {}), emit=lines.append) == [] and any("HOLD" in x for x in lines)


# ---------------------------------------------------------------- parameters

def test_an_unknown_params_mode_is_refused() -> None:
    with pytest.raises(SessionError, match="params must be"):
        book_with_params(_book(), ["AAA"], "guess")


def test_default_params_leave_the_book_alone() -> None:
    book = _book()
    assert book_with_params(book, ["AAA"], "default") is book


def test_a_params_mode_builds_each_symbols_instance_and_prints_it(monkeypatch: pytest.MonkeyPatch) -> None:
    built = EmaCrossover()
    monkeypatch.setattr(sess, "build_strategy", lambda _n, _s, _m: SimpleNamespace(strategy=built, source="wf", note="thin"))
    lines: list[str] = []
    book = book_with_params(_book(), ["AAA"], "wf", emit=lines.append)
    assert book.per_symbol == {"AAA": built} and any("<- wf" in x and "[thin]" in x for x in lines)


# ---------------------------------------------------------------- the runners end to end, as far as money allows

def test_start_session_reaches_the_builder_with_its_policy_and_overrides(monkeypatch: pytest.MonkeyPatch) -> None:
    built: dict[str, Any] = {}
    monkeypatch.setattr(sess, "venue_feed", lambda *_a, **_k: ("feed", "keep"))
    monkeypatch.setattr(sess, "build_session", lambda _s, plan, **kw: built.update(plan=plan, **kw) or "session")
    assert start_session(_settings(), venue="ib", mode="paper", symbols="", futures=["GC"], strategy="bollinger",
                         options=SessionOptions(allow_screened=True, close_symbols="aaa,bbb", trim_to_slots=True)) == "session"
    assert built["feed"] == "feed" and built["keepalive"] == "keep" and built["allow_screened"] is True
    assert built["close_symbols"] == ("AAA", "BBB") and built["trim_to_slots"] is True and built["class_overrides"]
    assert built["plan"].symbols == ("GC",)         # a futures run names its whole book


def test_start_session_with_no_futures_has_no_overrides_and_names_its_label(monkeypatch: pytest.MonkeyPatch) -> None:
    built: dict[str, Any] = {}
    monkeypatch.setattr(sess, "venue_feed", lambda *_a, **_k: ("feed", None))
    monkeypatch.setattr(sess, "build_session", lambda _s, plan, **kw: built.update(kw) or "s")
    start_session(_settings(), venue="ib", mode="paper", symbols="AAA", strategy="ema_crossover")
    assert built["class_overrides"] is None and built["label"] == "ib-paper"


def test_each_venue_refuses_the_other_venues_class(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(sess, "venue_feed", lambda *_a, **_k: ("feed", None))
    monkeypatch.setattr(sess, "build_session", lambda *_a, **_k: "s")
    with pytest.raises(SessionError, match="Kraken"):
        start_session(_settings(), venue="ib", mode="paper", symbols="AAPL,BTC/USD", strategy="ema_crossover")
    with pytest.raises(SessionError, match="trade these on IB"):
        start_session(_settings(), venue="kraken", mode="paper", symbols="BTC/USD,AAPL", strategy="ema_crossover")


def test_the_script_arguments_all_reach_the_options() -> None:
    import argparse
    ap = argparse.ArgumentParser()
    sess.add_session_args(ap)
    args = ap.parse_args([])
    args.account_currency = "USD"                 # only paper_ib.py adds this flag
    assert options_from_args(args, level=True).account_currency == "USD"
    assert options_from_args(args, level=True).level is True


_LIVE = ["live", "--confirm", PHRASE, "--venue", "ib", "--symbols", "AAA", "--strategy", "ema_crossover",
         "--max-positions", "3", "--profit-lock", "--max-cost-ratio", "0.01", "--sequential-sizing"]


def test_trading_live_hands_the_session_its_phrase_and_flags(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    seen: dict[str, Any] = {}

    def record(settings: Any, **kw: Any) -> Any:
        seen.update(kw)
        return SimpleNamespace(run=lambda n: seen.update(ran=n))

    monkeypatch.setattr(cli, "get_settings", lambda: _settings(tmp_path))
    monkeypatch.setattr(cli, "start_session", record)
    res = CliRunner().invoke(cli.app, _LIVE)
    assert res.exit_code == 0, res.output
    assert seen["mode"] == "live" and seen["live_confirmation"] == PHRASE and seen["ran"] == 0
    o = seen["options"]
    assert (o.max_positions, o.profit_lock, o.max_cost_ratio, o.parallel_sizing) == (3, True, 0.01, False)


def test_trading_live_will_not_start_without_the_phrase_argument(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(cli, "get_settings", lambda: _settings(tmp_path))
    assert CliRunner().invoke(cli.app, ["live", "--venue", "ib"]).exit_code == 2


def test_trading_live_takes_no_paper_only_flags(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(cli, "get_settings", lambda: _settings(tmp_path))
    for flag in ("--flatten-on-exit", "--trim-to-slots", "--resume-session=abc", "--close-symbols=AAA"):
        assert CliRunner().invoke(cli.app, ["live", "--confirm", PHRASE, flag]).exit_code == 2, flag


def _daemon(tmp: Path, monkeypatch: pytest.MonkeyPatch, *, enabled: bool = True, account: str | None = "paper",
            venue: str | None = None) -> tuple[Any, dict[str, Any]]:
    seen: dict[str, Any] = {}
    if venue is None:
        monkeypatch.delenv("AUTONOMOUS_VENUE_RUNTIME", raising=False)
    else:
        monkeypatch.setenv("AUTONOMOUS_VENUE_RUNTIME", venue)
    monkeypatch.setattr(cli, "get_settings", lambda: _settings(tmp, autonomous_enabled=enabled))
    monkeypatch.setattr(cli, "_run_session", lambda _s, **kw: seen.update(kw))
    if account is None:
        monkeypatch.delenv("AUTONOMOUS_ACCOUNT_RUNTIME", raising=False)
    else:
        monkeypatch.setenv("AUTONOMOUS_ACCOUNT_RUNTIME", account)
    return CliRunner().invoke(cli.app, ["autonomous", "run"]), seen


def test_the_daemon_refuses_when_it_is_not_enabled(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    res, seen = _daemon(tmp_path, monkeypatch, enabled=False)
    assert res.exit_code == 2 and not seen


def test_the_daemon_refuses_while_halted(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    KillSwitch(tmp_path).trip("manual")
    res, seen = _daemon(tmp_path, monkeypatch, account="live")
    assert res.exit_code == 2 and not seen


def test_a_paper_halt_stops_the_rehearsal_and_not_the_money_daemon(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    KillSwitch(tmp_path, paper=True).trip("max-drawdown 12%")
    res, seen = _daemon(tmp_path, monkeypatch, account="paper")
    assert res.exit_code == 2 and not seen
    res, seen = _daemon(tmp_path, monkeypatch, account="live")
    assert res.exit_code == 0 and seen["expected_env"] == "live"


def test_the_daemon_refuses_an_unknown_account(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    res, seen = _daemon(tmp_path, monkeypatch, account="savings")
    assert res.exit_code == 2 and not seen


def test_the_daemon_rehearses_on_paper_and_checks_the_token_otherwise(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    res, seen = _daemon(tmp_path, monkeypatch, account="paper")
    assert res.exit_code == 0 and seen["rehearsal"] is True and seen["expected_env"] is None and seen["mode"] == "autonomous"
    res, seen = _daemon(tmp_path, monkeypatch, account="practice")
    assert res.exit_code == 0 and seen["rehearsal"] is False and seen["expected_env"] == "practice"


def test_the_daemon_trades_one_venue_ib_unless_told(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    res, seen = _daemon(tmp_path, monkeypatch, account="paper")
    assert res.exit_code == 0 and seen["venue"] == "ib"
    res, seen = _daemon(tmp_path, monkeypatch, account="paper", venue="kraken")
    assert res.exit_code == 0 and seen["venue"] == "kraken"
    res, seen = _daemon(tmp_path, monkeypatch, account="paper", venue="questrade")
    assert res.exit_code == 2 and not seen


# ---------------------------------------------------------------- reading through the guards

def test_a_frozen_quote_stops_a_money_session_reading_that_symbol(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    now = [datetime(2026, 11, 3, 15, 0, tzinfo=UTC)]
    guarded = FreshQuoteBroker(_Feed(), max_frozen_s=60.0, on_stale="raise", clock=lambda: now[0])  # type: ignore[arg-type]
    guarded.quote("AAA")
    now[0] += timedelta(seconds=120)
    with pytest.raises(StaleQuote):
        guarded.quote("AAA")


def test_a_moving_quote_is_not_stale(tmp_path: Path) -> None:
    class _Moving(_Feed):
        px = 100.0

        def quote(self, symbol: str) -> Quote:
            self.px += 1.0
            return Quote(symbol=symbol, symbolId=1, bidPrice=self.px - 1, askPrice=self.px + 1, lastTradePrice=self.px)

    now = [datetime(2026, 11, 3, 15, 0, tzinfo=UTC)]
    guarded = FreshQuoteBroker(_Moving(), max_frozen_s=60.0, on_stale="raise", clock=lambda: now[0])  # type: ignore[arg-type]
    for _ in range(3):
        guarded.quote("AAA")
        now[0] += timedelta(seconds=120)
