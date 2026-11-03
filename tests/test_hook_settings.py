"""Settings notice a change; a dead poller is visible; allocator weights follow the book; the hooks read the yaml."""
from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
import yaml

from tests.test_session import _Feed, _plan, _settings
from trading_live_claude.config.settings import get_settings
from trading_live_claude.intel import routing
from trading_live_claude.intel.graph import Edge, append_edges
from trading_live_claude.intel.routing import PersistenceGate
from trading_live_claude.monitor import session as sess
from trading_live_claude.monitor.live_loop import monitor_policy
from trading_live_claude.monitor.session import RefreshingBias, SessionOptions, StrategyBook, build_session
from trading_live_claude.strategies.examples.ema_crossover import EmaCrossover


# ---------------------------------------------------------------- 13.7: get_settings

def test_settings_stay_one_object_until_something_changes() -> None:
    assert get_settings() is get_settings()


def test_a_yaml_change_is_seen(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    path = tmp_path / "trading.yaml"
    monkeypatch.setenv("TRADING_YAML_PATH", str(path))
    path.write_text(yaml.safe_dump({"min_ticket_usd": 111.0}), encoding="utf-8")
    assert get_settings().min_ticket_usd == 111.0
    path.write_text(yaml.safe_dump({"min_ticket_usd": 222.0, "max_open_positions": 7}), encoding="utf-8")
    assert get_settings().min_ticket_usd == 222.0


def test_an_environment_change_is_seen(monkeypatch: pytest.MonkeyPatch) -> None:
    """The daemon writes to the environment after the first call."""
    before = get_settings()
    monkeypatch.setenv("QUESTRADE_ENV", "live")
    after = get_settings()
    assert after is not before and after.questrade_env == "live"


def test_a_clear_rebuilds() -> None:
    before = get_settings()
    get_settings.cache_clear()
    assert get_settings() is not before


# ---------------------------------------------------------------- 13.8: a dead poller is visible

def _poll_edge(age_hours: float, poll: str = "p1") -> Edge:
    stamp = (datetime.now(UTC) - timedelta(hours=age_hours)).isoformat()
    return Edge(subject=("poll", poll), predicate="elevated_in", object=("domain", "energy"), as_of=stamp)


def _gate(tmp: Path, edges: list[Edge], **kw: Any) -> PersistenceGate:
    path = tmp / "graph.jsonl"
    append_edges(edges, path)
    return PersistenceGate(graph_path=path, **kw)


def test_a_stale_run_is_logged_once(tmp_path: Path, record_log: Any) -> None:
    rec = record_log(routing)
    gate = _gate(tmp_path, [_poll_edge(30.0)], max_age_hours=6.0, refresh_seconds=0.0)
    gate.refresh(force=True)
    gate.refresh(force=True)
    stale = [e for e in rec.rows if e["event"] == "persistence_gate.stale_run"]
    assert len(stale) == 1 and stale[0]["age_hours"] == pytest.approx(30.0, abs=0.2) and stale[0]["max_age_hours"] == 6.0


def test_a_fresh_run_says_nothing(tmp_path: Path, record_log: Any) -> None:
    rec = record_log(routing)
    _gate(tmp_path, [_poll_edge(1.0)], max_age_hours=6.0).refresh(force=True)
    assert "persistence_gate.stale_run" not in rec.events


def test_a_newer_stale_poll_warns_again(tmp_path: Path, record_log: Any) -> None:
    rec = record_log(routing)
    gate = _gate(tmp_path, [_poll_edge(30.0)], max_age_hours=6.0)
    gate.refresh(force=True)
    append_edges([_poll_edge(20.0, "p2")], gate._graph_path)
    gate.refresh(force=True)
    assert rec.events.count("persistence_gate.stale_run") == 2


@pytest.mark.parametrize("edges", [[], [Edge(subject=("symbol", "A"), predicate="traded", object=("domain", "x"), as_of="2020-01-01T00:00:00+00:00")]])
def test_nothing_to_judge_says_nothing(tmp_path: Path, edges: list[Edge], record_log: Any) -> None:
    rec = record_log(routing)
    _gate(tmp_path, edges, max_age_hours=6.0).refresh(force=True)
    assert "persistence_gate.stale_run" not in rec.events


def test_no_limit_means_no_warning(tmp_path: Path, record_log: Any) -> None:
    rec = record_log(routing)
    _gate(tmp_path, [_poll_edge(30.0)], max_age_hours=None).refresh(force=True)
    assert "persistence_gate.stale_run" not in rec.events


# ---------------------------------------------------------------- 13.10: weights follow the book

class _Clock:
    t = 0.0

    def __call__(self) -> float:
        return self.t


def test_weights_refresh_on_the_cadence_and_not_before() -> None:
    clock, calls = _Clock(), []

    def compute() -> Any:
        calls.append(1)
        return lambda _s: 2.0

    bias = RefreshingBias(compute, lambda _s: 1.0, 100.0, symbols=["A"], clock=clock)
    assert bias("A") == 1.0
    bias.maybe_refresh()
    assert calls == []
    clock.t = 101.0
    bias.maybe_refresh()
    assert calls == [1] and bias("A") == 2.0
    bias.maybe_refresh()
    assert calls == [1]


@pytest.mark.parametrize("compute", [lambda: None, lambda: (_ for _ in ()).throw(RuntimeError("vendor down"))])
def test_a_failed_refresh_keeps_the_last_good_weights(compute: Any) -> None:
    clock = _Clock()
    bias = RefreshingBias(compute, lambda _s: 1.5, 10.0, symbols=["A"], clock=clock)
    clock.t = 11.0
    bias.maybe_refresh()
    assert bias("A") == 1.5


def test_no_weights_yet_reads_neutral() -> None:
    assert RefreshingBias(lambda: None, None, 10.0, symbols=["A"])("A") == 1.0


def test_a_refresh_goes_to_the_ledger() -> None:
    clock, rows = _Clock(), []
    ledger = SimpleNamespace(append=lambda e, p: rows.append((e, p)))
    bias = RefreshingBias(lambda: (lambda _s: 2.0), None, 10.0, symbols=["A", "B"], ledger=ledger, clock=clock)  # type: ignore[arg-type]
    clock.t = 11.0
    bias.maybe_refresh()
    assert rows == [("ALLOCATION_REFRESHED", {"weights": {"A": 2.0, "B": 2.0}})]


def test_it_says_whether_the_overlay_is_already_in_the_weights() -> None:
    overlaid = SimpleNamespace(applies_overlay=True)
    assert RefreshingBias(lambda: None, overlaid, 10.0, symbols=[]).applies_overlay is True
    assert RefreshingBias(lambda: None, lambda _s: 1.0, 10.0, symbols=[]).applies_overlay is False


def test_a_session_refreshes_between_polls(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[int] = []

    def weights(*_a: Any, **_k: Any) -> Any:
        calls.append(1)
        return lambda _s: 1.0

    monkeypatch.setattr(sess, "venue_weight_bias", weights)
    settings = _settings(tmp_path, allocator_refresh_seconds=0.001)
    session = build_session(settings, _plan(settings), feed=_Feed(), book=StrategyBook(default=EmaCrossover()))  # type: ignore[arg-type]
    assert isinstance(session.monitor.weight_bias_for, RefreshingBias) and calls == [1]
    import time
    time.sleep(0.01)
    session.monitor.interval_seconds = 0
    monkeypatch.setattr(session.monitor, "_sleep_seconds", lambda: 0.0)
    session.run(1)
    assert len(calls) == 2


def test_zero_keeps_the_startup_weights(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    fixed = lambda _s: 1.0  # noqa: E731
    monkeypatch.setattr(sess, "venue_weight_bias", lambda *_a, **_k: fixed)
    settings = _settings(tmp_path, allocator_refresh_seconds=0.0)
    session = build_session(settings, _plan(settings), feed=_Feed(), book=StrategyBook(default=EmaCrossover()))  # type: ignore[arg-type]
    assert session.monitor.weight_bias_for is fixed


# ---------------------------------------------------------------- 13.11: the hooks read the yaml

def test_the_hooks_read_their_knobs(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(sess, "venue_weight_bias", lambda *_a, **_k: None)
    settings = _settings(tmp_path, persistence_min_polls=9, persistence_max_age_hours=2.0,
                         allocator_bias_floor=0.5, allocator_bias_cap=1.5)
    m = build_session(settings, _plan(settings), feed=_Feed(), book=StrategyBook(default=EmaCrossover())).monitor  # type: ignore[arg-type]
    assert m.persistence_for.min_polls == 9 and m.persistence_for._max_age_hours == 2.0
    assert m.weight_bias_range == (0.5, 1.5)


def test_the_overlay_can_be_turned_off_in_the_yaml_for_any_mode() -> None:
    off = _settings(intel_overlay=False)
    for mode in ("paper", "live", "autonomous"):
        assert _plan(off, mode=mode).intel_overlay is False
    assert _plan(_settings(), options=SessionOptions(intel_overlay=False)).intel_overlay is False


def test_either_setting_turns_the_market_hours_guard_off() -> None:
    risk = {"entry_triggers": {}, "forced_exits": False, "risk_model": "atr", "heat_aggregation": "sum"}
    for hours, venues in ((True, False), (False, True)):
        policy = monitor_policy(SimpleNamespace(respect_market_hours=hours, skip_closed_venues=venues, **risk))
        assert policy["market_open_for"] is None
    assert monitor_policy(SimpleNamespace(respect_market_hours=True, skip_closed_venues=True, **risk))["market_open_for"]


def test_the_monitor_clamps_a_bias_to_the_configured_range(tmp_path: Path) -> None:
    from tests.test_monitor_guards import _monitor, _qty, _Script
    capped, pb_capped, _ = _monitor(tmp_path / "a", _Script(entry=1), weight_bias_for=lambda _s: 100.0,
                                    weight_bias_range=(0.1, 1.0))
    neutral, pb_neutral, _ = _monitor(tmp_path / "b", _Script(entry=1), weight_bias_for=lambda _s: 1.0)
    capped.step()
    neutral.step()
    assert _qty(pb_capped) == _qty(pb_neutral) > 0
