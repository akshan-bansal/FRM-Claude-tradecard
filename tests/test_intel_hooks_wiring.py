"""The intel hooks reach every runner, and each hook does what its wiring promises.

Nothing here builds a live router or sets a live execution mode: the runners are read as source, and
the hooks run on a stub broker with a recording router.
"""
from __future__ import annotations

import ast
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import pandas as pd
import pytest

from tests.test_monitor import _Broker, _EntryStrategy, _Market, _Router
from trading_live_claude.intel.graph import (
    append_edges,
    edge_persistence,
    event_records_to_edges,
    fill_edge,
    load_edges,
    recent_events_from_graph,
    snapshot_to_edges,
)
from trading_live_claude.intel.overlay import IntelSnapshot
from trading_live_claude.intel.routing import PersistenceGate
from trading_live_claude.monitor import session
from trading_live_claude.monitor.live_loop import LiveMonitor, MonitorEvent
from trading_live_claude.risk.sizing import PositionSizer

REPO = Path(__file__).resolve().parents[1]
ALL_HOOKS = {"overlay_for", "interpret_for", "weight_bias_for", "persistence_for"}


def _monitor_kwargs(path: Path, function: str | None = None) -> set[str]:
    """Keyword names on the LiveMonitor call in ``function`` (or the only one in the file)."""
    tree = ast.parse(path.read_text(encoding="utf-8"))
    scope: ast.AST = tree
    if function is not None:
        scope = next(n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef) and n.name == function)
    calls = [n for n in ast.walk(scope) if isinstance(n, ast.Call) and getattr(n.func, "id", "") == "LiveMonitor"]
    assert len(calls) == 1, f"{path.name}:{function}"
    return {k.arg for k in calls[0].keywords if k.arg}


def test_the_one_monitor_every_runner_gets_has_every_intel_hook() -> None:
    assert _monitor_kwargs(REPO / "src" / "trading_live_claude" / "monitor" / "session.py", "build_session") >= ALL_HOOKS


def test_no_key_leaves_the_overlay_off_and_the_runner_running() -> None:
    assert session.intel_overlay_hooks(SimpleNamespace(worldmonitor_api_key="")) == (None, None)


# ---------------------------------------------------------------- allocator bias


class _CloseMarket:
    def recent(self, symbol: str, bars: int, interval: str = "1d") -> pd.DataFrame:
        # Dated like MarketData's bars: the allocator pairs names by trading day, not by row.
        drift = {"AAA": 0.001, "BBB": 0.002}[symbol]
        return pd.DataFrame({"time": pd.bdate_range(end="2026-11-02", periods=bars, tz="UTC"),
                             "close": [100.0 * (1 + drift * i + 0.01 * ((i * 7) % 5 - 2)) for i in range(bars)]})


def test_a_symbol_outside_the_validated_list_stays_neutral(monkeypatch: pytest.MonkeyPatch) -> None:
    validated = {"AAA": SimpleNamespace(oos_score=1.0), "BBB": SimpleNamespace(oos_score=1.0)}
    monkeypatch.setattr("trading_live_claude.analysis.universe.WALK_FORWARD_VALIDATED", validated)
    bias = session.allocator_weight_bias(_CloseMarket(), ["AAA", "BBB", "UNVALIDATED"])
    assert bias is not None
    assert bias("UNVALIDATED") == 1.0
    assert bias("AAA") > 0.0 and bias("BBB") > 0.0


def test_no_validated_symbol_means_no_bias(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("trading_live_claude.analysis.universe.WALK_FORWARD_VALIDATED", {})
    assert session.allocator_weight_bias(_CloseMarket(), ["UNVALIDATED"]) is None


# ---------------------------------------------------------------- hooks on the monitor


def _monitor(router: _Router, events: list[MonitorEvent], **hooks: object) -> LiveMonitor:
    return LiveMonitor(broker=_Broker(), market=_Market(), strategy=_EntryStrategy(),  # type: ignore[arg-type]
                       sizer=PositionSizer(risk_pct=0.01), router=router,  # type: ignore[arg-type]
                       account_number="X", symbols=["CCC"], on_event=events.append, **hooks)  # type: ignore[arg-type]


def _shares(**hooks: object) -> list[int]:
    router = _Router()
    _monitor(router, [], **hooks).step()
    return [i.shares for i in router.intents]  # type: ignore[attr-defined]


def test_a_persistence_halt_blocks_the_order_but_not_the_alert() -> None:
    router, events = _Router(), []
    _monitor(router, events, persistence_for=lambda _s: (True, "equity: 'conflict' elevated 6 polls")).step()
    assert router.intents == []
    assert [e.kind for e in events] == ["entry"]


def test_a_broken_persistence_gate_fails_open() -> None:
    def broken(_s: str) -> tuple[bool, str]:
        raise OSError("graph unreadable")
    assert _shares(persistence_for=broken) == _shares()


@pytest.mark.parametrize("bias, factor", [(2.0, 2.0), (0.5, 0.5), (9.0, 3.0), (0.0, 0.1)])
def test_the_allocator_bias_scales_an_order_within_its_bounds(bias: float, factor: float) -> None:
    assert _shares(weight_bias_for=lambda _s: bias) == [int(_shares()[0] * factor)]


def test_a_broken_allocator_bias_is_neutral() -> None:
    def broken(_s: str) -> float:
        raise ValueError("no covariance")
    assert _shares(weight_bias_for=broken) == _shares()


# ---------------------------------------------------------------- the graph journal behind the gate


def _polls(path: Path, n: int, start: datetime, domain: str = "conflict") -> datetime:
    for i in range(n):
        snap = IntelSnapshot(as_of=start + i * timedelta(minutes=30), event_acceleration={domain: 6.0})
        append_edges(snapshot_to_edges(snap), path=path)
    return start + n * timedelta(minutes=30)


def _fill(path: Path, at: datetime) -> None:
    append_edges([fill_edge(venue="kraken", symbol="SOL/USD", action="Buy", quantity=1.0, price=100.0,
                            session_id="s", order_id=1, as_of=at.isoformat())], path=path)


def _run(path: Path, **kw: object) -> int:
    return edge_persistence(load_edges(path), predicate="elevated_in", object=("domain", "conflict"), **kw)  # type: ignore[arg-type]


def test_a_fill_neither_resets_nor_splits_a_persistence_run(tmp_path: Path) -> None:
    path = tmp_path / "graph.jsonl"
    end = _polls(path, 6, datetime.now(UTC) - timedelta(hours=4))
    _fill(path, end)
    assert _run(path) == 6
    _polls(path, 2, end + timedelta(minutes=1))
    assert _run(path) == 8
    assert PersistenceGate(min_polls=5, refresh_seconds=0.0, graph_path=path)("SPY")[0] is True


def test_a_calm_poll_still_breaks_the_run(tmp_path: Path) -> None:
    path = tmp_path / "graph.jsonl"
    end = _polls(path, 6, datetime.now(UTC) - timedelta(hours=4))
    append_edges(snapshot_to_edges(IntelSnapshot(as_of=end, event_acceleration={"conflict": 1.0},
                                                 category_alert_counts={"conflict": 1})), path=path)
    assert _run(path) == 0


def test_a_market_bridge_still_counts_its_own_run(tmp_path: Path) -> None:
    path = tmp_path / "graph.jsonl"
    start = datetime.now(UTC) - timedelta(hours=2)
    for i in range(3):
        append_edges(snapshot_to_edges(IntelSnapshot(as_of=start + timedelta(minutes=i), energy_stress=0.7)), path=path)
    assert edge_persistence(load_edges(path), predicate="stressed_by", object=("domain", "energy")) == 3


def test_a_stale_run_does_not_halt_entries_today(tmp_path: Path) -> None:
    path = tmp_path / "graph.jsonl"
    _polls(path, 6, datetime.now(UTC) - timedelta(days=60))
    assert _run(path) == 6                                         # the record is still there
    assert _run(path, max_age_hours=6.0) == 0                      # it just says nothing about today
    assert PersistenceGate(min_polls=5, refresh_seconds=0.0, graph_path=path)("SPY") == (False, "")
    held = PersistenceGate(min_polls=5, refresh_seconds=0.0, max_age_hours=None, graph_path=path)
    assert held("SPY")[0] is True


# ---------------------------------------------------------------- the held agent layer, end to end


def _evidence(tmp_path: Path) -> list[dict[str, object]]:
    records = [{"id": "ev-1", "title": "Refinery strike halts pipeline flows", "sources": ["reuters", "ap"],
                "categories": ["energy"], "country": "SA"}]
    now = datetime.now(UTC).isoformat()
    path = tmp_path / "events.jsonl"
    append_edges(event_records_to_edges(records, domain="news_signal", poll_id=now, as_of=now), path=path)
    return recent_events_from_graph(load_edges(path))


def test_the_headline_reaches_the_agents_evidence(tmp_path: Path) -> None:
    assert _evidence(tmp_path)[0]["title"] == "Refinery strike halts pipeline flows"


