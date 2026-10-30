"""Tests must never write the repo's real state/: journal defaults follow the configured state_dir.

Before 2026-10-30 the graph and overlay journals bound "state/..." at import time, so a full run
appended ~3.6 KB of fake fill edges and ~2.2 KB of empty overlay snapshots to the real journals.
"""
from __future__ import annotations

import os
from datetime import UTC, datetime
from pathlib import Path

from trading_live_claude.intel.graph import (
    append_edges,
    default_graph_journal,
    fill_edge,
    load_edges,
)
from trading_live_claude.intel.history import append_snapshot, default_overlay_journal
from trading_live_claude.intel.overlay import IntelSnapshot

REAL_STATE = (Path(__file__).resolve().parent.parent / "state").resolve()


def _sandbox() -> Path:
    return Path(os.environ["STATE_DIR"]).resolve()  # set per test by conftest._isolated_state


def test_journal_defaults_resolve_inside_the_test_sandbox() -> None:
    for p in (default_graph_journal(), default_overlay_journal()):
        assert p.resolve().parent == _sandbox()
        assert REAL_STATE not in p.resolve().parents


def test_default_writes_land_in_the_sandbox() -> None:
    edge = fill_edge(venue="fake", symbol="AAA", action="Buy", quantity=1.0, price=1.0,
                     session_id="s", order_id="1", as_of=datetime.now(UTC).isoformat())
    append_edges([edge])
    append_snapshot(IntelSnapshot(), {})
    assert [e.object for e in load_edges()] == [("symbol", "AAA")]
    assert (_sandbox() / "intel_graph.jsonl").exists() and (_sandbox() / "intel_overlay.jsonl").exists()

