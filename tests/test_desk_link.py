"""The QuantPort.io desk panel: the shim serves it, the page reads the running session, and every figure
it bakes in comes from this repo's own journals.

In the order the wiring runs:

1. ``GET /desk`` is the unlock shell: public, carries no journal data and never the token.
2. ``GET /v1/desk/page`` is the built page, behind the same auth as every other ``/v1`` route.
3. The live strip (``/v1/stats``) measures card response time from ``issued_at`` and scopes its counts
   to the session; the atlas feed (``/v1/meters/snapshot``) emits only readings the page's validator
   accepts.
4. The builder works on a book with nothing fit to rank, leaves fills from sessions no journal knows
   out of the graph, and the template carries no figure measured on some other record.
"""
from __future__ import annotations

import csv
import importlib.util
import json
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from fastapi.testclient import TestClient

from trading_live_claude.brokers.models import OrderAction
from trading_live_claude.execution.approval import CardRegistry, InMemoryApprovalStore, fingerprint
from trading_live_claude.execution.approval_asgi import create_app
from trading_live_claude.execution.approval_sqlite import SqliteApprovalStore, SqliteCardRegistry
from trading_live_claude.execution.desk_feed import meters_snapshot, session_stats
from trading_live_claude.execution.router import OrderIntent

ROOT = Path(__file__).resolve().parent.parent
PAGE = "<title>QuantPort.io</title><script>const DATA = {};</script>"
SID = "a1b2c3d4e5f6a7b8c9d0e1f2a3b4c5d6"


def intent(symbol: str = "SOL/USD") -> OrderIntent:
    return OrderIntent(symbol=symbol, action=OrderAction.BUY, shares=3, entry=100.0, stop=95.0,
                       target=120.0, strategy="bollinger", risk_dollars=15.0, account_number="PAPER-001")


@pytest.fixture
def built_page(tmp_path: Path) -> Path:
    page = tmp_path / "desk.html"
    page.write_text(PAGE, encoding="utf-8")
    return page


def client(page: Path | None, token: str | None = None, **kw: object) -> TestClient:
    registry = CardRegistry()
    return TestClient(create_app(InMemoryApprovalStore(registry), registry, desk_page=page,
                                 auth_token=token, **kw))  # type: ignore[arg-type]


# --------------------------------------------------------------------------- the shell and the page


def test_shell_is_public_and_leaks_nothing(built_page: Path) -> None:
    body = client(built_page, token="s3cr3t").get("/desk").text
    assert "QUANTPORT" in body and "s3cr3t" not in body
    assert "DATA" not in body                       # no baked page content in the public shell
    assert '"auth_required": true' in body


def test_shell_script_is_scoped(built_page: Path) -> None:
    """document.write keeps this Window, so a top-level name here would collide with the page's own
    ``const $`` and kill the panel with a SyntaxError. The shell body stays inside an IIFE."""
    body = client(built_page).get("/desk").text
    assert "<script>(function(){" in body and "})();</script>" in body


def test_shell_is_not_rewritten_by_the_legacy_middleware(built_page: Path) -> None:
    r = client(built_page).get("/desk")
    assert r.status_code == 200 and "QuantPort.io" in r.text


def test_page_is_served_only_to_an_authorised_reader(built_page: Path) -> None:
    c = client(built_page, token="tok")
    assert c.get("/v1/desk/page").status_code == 401
    r = c.get("/v1/desk/page", headers={"Authorization": "Bearer tok"})
    assert r.status_code == 200 and r.text == PAGE and r.headers["cache-control"] == "no-store"
    assert client(built_page).get("/v1/desk/page").status_code == 200      # open shim, open page


def test_missing_page_says_how_to_build_it(tmp_path: Path) -> None:
    r = client(tmp_path / "never-built.html").get("/v1/desk/page")
    assert r.status_code == 404 and "build_desk_dash" in r.json()["build"]


def test_pending_prompts_carry_the_fingerprint_the_store_served(built_page: Path) -> None:
    registry = CardRegistry()
    store = InMemoryApprovalStore(registry)
    prompt = store.publish(intent(), broker="kraken", mode="paper", ttl_seconds=90.0)
    rows = TestClient(create_app(store, registry, desk_page=built_page)).get("/v1/intents/pending").json()["prompts"]
    assert [r["intent_id"] for r in rows] == [prompt.intent_id]
    assert rows[0]["fingerprint"] == prompt.fingerprint == fingerprint(prompt.canonical.encode("utf-8"))


def test_the_card_simulator_shows_the_same_fingerprint() -> None:
    spec = importlib.util.spec_from_file_location("card_sim", ROOT / "scripts" / "approval_card_sim.py")
    sim = importlib.util.module_from_spec(spec)  # type: ignore[arg-type]
    spec.loader.exec_module(sim)  # type: ignore[union-attr]
    p = InMemoryApprovalStore(CardRegistry()).publish(intent(), broker="kraken", mode="paper", ttl_seconds=90.0)
    assert f"HASH {p.fingerprint}" in sim.render_prompt(p.to_dict())


# --------------------------------------------------------------------------- the live strip


def _marks(state: Path, sid: str, n: int = 3, open_value: float = 0.0) -> None:
    with (state / "paper_equity.csv").open("w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh)
        w.writerow(["ts", "session_id", "equity", "cash", "positions_value", "realized_pnl",
                    "unrealized_pnl", "peak_equity", "drawdown_pct"])
        for i in range(n):
            ts = (datetime.now(UTC) - timedelta(minutes=n - i)).isoformat()
            eq = 100_000 - 10 * i
            w.writerow([ts, sid, eq, eq - open_value, open_value, 0, 0, 100_000, (100_000 - eq) / 100_000])


def _decided_store(state: Path) -> SqliteApprovalStore:
    key = Ed25519PrivateKey.generate()
    pem = key.public_key().public_bytes(serialization.Encoding.PEM,
                                        serialization.PublicFormat.SubjectPublicKeyInfo)
    reg = SqliteCardRegistry(state / "approval.db")
    reg.register("card-sim-001", pem)
    store = SqliteApprovalStore(reg, state / "approval.db")
    for decision in ("ACCEPT", "DECLINE"):
        p = store.publish(intent(), broker="kraken", mode="paper", ttl_seconds=90.0)
        assert store.respond(p.intent_id, decision=decision, card_id="card-sim-001",  # type: ignore[arg-type]
                             signature=key.sign(p.canonical.encode("utf-8")))
    store.publish(intent("ETH/USD"), broker="kraken", mode="paper", ttl_seconds=0.05)
    time.sleep(0.1)
    store.pending()                                  # sweeps the short-lived prompt to EXPIRED
    return store


def test_stats_measure_response_time_over_decisions_only(tmp_path: Path) -> None:
    _marks(tmp_path, SID)
    store = _decided_store(tmp_path)
    s = session_stats(store, tmp_path, SID, started_at=datetime.now(UTC) - timedelta(hours=1))
    assert (s["intents_approved"], s["intents_declined"], s["intents_expired"]) == (1, 1, 1)
    assert s["avg_ttl_response"] is not None and s["avg_ttl_response"] < 5.0   # EXPIRED's 90s sweep is out
    assert s["prompt_scope"] == "since this session started" and s["session_equity"] == 99_980.0


def test_stats_count_only_prompts_issued_since_the_session_started(tmp_path: Path) -> None:
    _marks(tmp_path, SID)
    store = _decided_store(tmp_path)
    later = session_stats(store, tmp_path, SID, started_at=datetime.now(UTC) + timedelta(hours=1))
    assert later["intents_total"] == 0 and later["avg_ttl_response"] is None


def test_stats_read_the_gate_reason_the_journal_actually_stores(tmp_path: Path) -> None:
    """``rejected.jsonl`` keeps a ``reasons`` list; reading a ``reason`` key showed "reason not
    recorded" under every denial on the old desk."""
    (tmp_path / "rejected.jsonl").write_text(json.dumps(
        {"symbol": "SOL/USD", "reasons": ["no card-signed basket for venue kraken"],
         "ts": datetime.now(UTC).isoformat()}) + "\n", encoding="utf-8")
    s = session_stats(InMemoryApprovalStore(CardRegistry()), tmp_path, None)
    assert s["last_gate_reason"] == "no card-signed basket for venue kraken"
    assert s["gate_rejections"] == 1 and s["gate_scope"] == "journal to date"


def test_stats_name_what_they_cannot_source(tmp_path: Path) -> None:
    s = client(None, state_dir=tmp_path, session_id=SID).get("/v1/stats").json()
    assert s["session_id"] is None and s["session_equity"] is None and s["avg_ttl_response"] is None
    assert {"session_equity", "max_drawdown_pct", "overlay_scalar"} <= set(s["placeholders"])


# --------------------------------------------------------------------------- the atlas feed


def test_meter_readings_pass_the_pages_validator(tmp_path: Path) -> None:
    _marks(tmp_path, SID)
    snap = meters_snapshot(tmp_path, ROOT)
    assert snap["schema_version"] == 1 and snap["readings"]
    for mid, r in snap["readings"].items():
        assert r["source"] and r["scope"] and r["status"] == "OBSERVED", mid
        assert r["stale_after_seconds"] > 0, mid
        assert datetime.fromisoformat(r["as_of"]) <= datetime.now(UTC), mid


# --------------------------------------------------------------------------- the builder and template


@pytest.fixture(scope="module")
def dash():
    spec = importlib.util.spec_from_file_location("build_desk_dash", ROOT / "scripts" / "build_desk_dash.py")
    mod = importlib.util.module_from_spec(spec)  # type: ignore[arg-type]
    spec.loader.exec_module(mod)  # type: ignore[union-attr]
    return mod


def test_a_book_with_nothing_fit_to_rank_still_builds(tmp_path: Path, dash) -> None:
    _marks(tmp_path, SID, n=30, open_value=3_000.0)          # running, position open
    data = dash.build(tmp_path)
    assert data["curve"] is None and data["sessions"] == [] and data["ret_hist"] is None
    assert data["scope"]["excluded"] == [("not closed flat", 1)]


def test_graph_fills_from_sessions_no_journal_knows_are_left_out(tmp_path: Path, dash) -> None:
    _marks(tmp_path, SID)
    edge = {"subject": ["venue", "kraken"], "predicate": "traded", "object": ["symbol", "SOL/USD"],
            "weight": 100.0, "as_of": datetime.now(UTC).isoformat()}
    (tmp_path / "intel_graph.jsonl").write_text("\n".join(json.dumps(edge | {"meta": {"session_id": s}})
                                                          for s in (SID, "test-session", "test-session")),
                                                encoding="utf-8")
    g = dash.graph_model(tmp_path / "intel_graph.jsonl", dash.known_sessions(tmp_path))
    assert g["edges_total"] == 1 and g["excluded"] == 2


def test_the_template_carries_no_figure_from_another_record() -> None:
    """The E: desk hard-coded a diagnostic ledger run (55/55 checks, a 49-row tape, card-diag-01) and a
    signature cut-over date. None of that was measured here, so none of it may appear."""
    page = (ROOT / "pwa" / "desk.template.html").read_text(encoding="utf-8")
    for planted in ("55/55", "49 rows", "card-diag-01", "332 /s", "2026-09-24", "XIC.TO 10 @31.05",
                    '<b>paper</b>', "6 / 6 caught"):
        assert planted not in page, planted
    assert "__DATA__" in page


def test_built_data_cannot_close_its_script_tag(tmp_path: Path, dash) -> None:
    _marks(tmp_path, SID)
    (tmp_path / "rejected.jsonl").write_text(json.dumps(
        {"symbol": "X", "reasons": ["</script><b>"], "ts": datetime.now(UTC).isoformat()}) + "\n",
        encoding="utf-8")
    out = tmp_path / "desk.html"
    import sys
    argv, sys.argv = sys.argv, ["build_desk_dash.py", "--state-dir", str(tmp_path), "--out", str(out)]
    try:
        assert dash.main() == 0
    finally:
        sys.argv = argv
    assert "</script><b>" not in out.read_text(encoding="utf-8")
