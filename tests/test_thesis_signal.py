"""Phase 4's promise: thesis signals are recorded before the outcome and act on nothing."""
from __future__ import annotations

import ast
import json
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import httpx
import pytest
import respx

pytest.importorskip("networkx")
pytest.importorskip("scipy")

from trading_live_claude import intel
from trading_live_claude.audit.ledger import LEDGER_EVENTS, verify_chain
from trading_live_claude.intel import thesis_signal as ts
from trading_live_claude.intel.agents import FiredThesis
from trading_live_claude.intel.graph import Edge, append_edges, event_records_to_edges
from trading_live_claude.intel.graph_store import GraphStore
from trading_live_claude.intel.interpret import THEME_EXEMPLARS, Thesis
from trading_live_claude.intel.overlay import IntelSnapshot
from trading_live_claude.intel.thesis_signal import (
    DIRECTION_TABLE_VERSION,
    THEME_DIRECTIONS,
    ThesisBook,
    ThesisSignal,
    fired_to_signals,
    run_shadow_pass,
    thesis_to_signals,
)

_URL = "https://api.anthropic.com/v1/messages"
T0 = datetime(2026, 6, 1, 12, 0, tzinfo=UTC)


def _thesis(name: str, *themes: str, confidence: str = "high", direction: str = "adverse") -> Thesis:
    return Thesis(name=name, confidence=confidence, evidence=[], inference="i", action="a", themes=list(themes),
                  direction=direction)


def _fired(*evidence: str, direction: str = "adverse", domain: str = "energy", confidence: float = 0.8) -> FiredThesis:
    return FiredThesis(domain=domain, thesis="Supply stress", direction=direction, confidence=confidence,  # type: ignore[arg-type]
                       inference="i", evidence=list(evidence), adversary_verdict="UPHELD", adversary_reason="r")


def _event(event_id: str, category: str, hours: float, *symbols: str) -> list[Edge]:
    stamp = (T0 + timedelta(hours=hours)).isoformat()
    rec = {"id": event_id, "title": f"{category} story", "sources": ["ap"], "categories": [category]}
    return (event_records_to_edges([rec], domain=category, poll_id=stamp, as_of=stamp)
            + [Edge(("event", event_id), "mentions", ("symbol", s), as_of=stamp) for s in symbols])


def _snapshot_row(at: datetime, **fields: Any) -> str:
    snapshot = {"strategic_risk": 70.0, "market": {"equity_vol": 15.0}, **fields}
    return json.dumps({"as_of": at.isoformat(), "snapshot": snapshot})


@pytest.fixture()
def store(tmp_path: Path) -> Iterator[GraphStore]:
    with GraphStore(tmp_path / "g.sqlite") as s:
        s.ingest(_event("e1", "energy", 1, "XLE", "USO") + _event("e2", "conflict", 2, "LMT"))
        yield s


@pytest.fixture()
def book(tmp_path: Path) -> ThesisBook:
    return ThesisBook(tmp_path / "ledger")


# ---------------------------------------------------------------- the ledger event came first


def test_the_event_is_in_the_vocabulary() -> None:
    assert "THESIS_SIGNAL" in LEDGER_EVENTS


# ---------------------------------------------------------------- rule theses


def test_a_constructive_entry_yields_confirm_and_entry() -> None:
    got = thesis_to_signals(_thesis("Commodity carry-inversion proxy", "energy"), as_of=T0, universe={"XLE"})
    assert {(s.symbol, s.action, s.direction) for s in got} == {("XLE", "confirm", "constructive"), ("XLE", "entry", "constructive")}
    assert all(s.direction_table == DIRECTION_TABLE_VERSION and s.symbol_source == "theme" and s.origin == "rule" for s in got)


def test_an_adverse_entry_yields_veto_and_exit() -> None:
    got = thesis_to_signals(_thesis("Dollar strength divergence — risk-on into a strong USD", "materials"), as_of=T0, universe={"XLB"})
    assert {s.action for s in got} == {"veto", "exit"} and {s.direction for s in got} == {"adverse"}


def test_a_thesis_that_says_it_is_not_directional_only_watches() -> None:
    got = thesis_to_signals(_thesis("Conflict escalation watch", "defense_geopolitical"), as_of=T0, universe={"LMT"})
    assert [(s.symbol, s.action, s.direction) for s in got] == [("LMT", "watch", "unsigned")]


def test_a_theme_the_table_does_not_list_is_unsigned_not_guessed() -> None:
    got = thesis_to_signals(_thesis("A thesis nobody reviewed", "energy"), as_of=T0, universe={"XLE"})
    assert {s.action for s in got} == {"watch"}


def test_every_entry_in_the_table_is_a_real_theme_and_sign() -> None:
    assert all(theme in THEME_EXEMPLARS for _, theme in THEME_DIRECTIONS)
    assert set(THEME_DIRECTIONS.values()) <= {"adverse", "constructive", "unsigned"}


def test_the_table_covers_every_directional_rule_thesis_that_names_a_theme() -> None:
    """Parsed from interpret.py, so a new thesis cannot silently fall through to unsigned without a decision."""
    tree = ast.parse(Path(intel.__file__).with_name("interpret.py").read_text(encoding="utf-8"))
    fn = next(n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == "interpret")
    names = set()
    for call in (n for n in ast.walk(fn) if isinstance(n, ast.Call) and getattr(n.func, "id", "") == "Thesis"):
        kwargs = {k.arg: k.value for k in call.keywords}
        name = kwargs.get("name")
        if isinstance(name, ast.Constant) and isinstance(kwargs.get("themes"), ast.List) and kwargs["themes"].elts:
            names.add(name.value)
    assert names
    missing = {n for n in names if "isaster" not in n and not any(k[0] == n for k in THEME_DIRECTIONS)}
    assert not missing


def test_the_universe_limits_symbols_and_none_means_all_exemplars() -> None:
    t = _thesis("Commodity carry-inversion proxy", "energy")
    assert thesis_to_signals(t, as_of=T0, universe=set()) == []
    assert {s.symbol for s in thesis_to_signals(t, as_of=T0)} == set(THEME_EXEMPLARS["energy"])


def test_a_symbol_two_themes_share_is_one_signal() -> None:
    got = thesis_to_signals(_thesis("Complacency divergence", "volatility_convexity", "safe_haven", "safe_haven"), as_of=T0, universe={"GLD"})
    assert len(got) == len({s.signal_id for s in got}) == 2


def test_the_quiet_thesis_says_nothing() -> None:
    assert thesis_to_signals(_thesis("No notable configuration", "energy"), as_of=T0) == []


def test_strength_follows_the_band_and_records_its_inputs() -> None:
    def strength(band: str) -> float:
        return thesis_to_signals(_thesis("Commodity carry-inversion proxy", "energy", confidence=band), as_of=T0, universe={"XLE"})[0].strength
    assert strength("high") == 1.0 and strength("moderate") == 0.6 and strength("tentative") == 0.3
    snap = IntelSnapshot(strategic_risk=70.0, energy_stress=0.3, market={"equity_vol": 15.0})
    got = thesis_to_signals(_thesis("Conflict escalation watch", "defense_geopolitical"), as_of=T0, snapshot=snap, universe={"LMT"})
    assert got[0].inputs["asset_class"] == "equity" and got[0].inputs["confluence_stance"] in ("adverse", "neutral", "constructive")


def test_expiry_is_the_horizon_after_the_moment() -> None:
    sig = thesis_to_signals(_thesis("Commodity carry-inversion proxy", "energy"), as_of=T0, universe={"XLE"}, horizon_days=3)[0]
    assert sig.as_of == T0.isoformat() and sig.expires_at == (T0 + timedelta(days=3)).isoformat() and sig.horizon_days == 3


# ---------------------------------------------------------------- disaster posts nothing


@pytest.mark.parametrize("themes", [("insurance",), ("insurance", "materials")])
def test_the_disaster_thesis_yields_no_signal(themes: tuple[str, ...]) -> None:
    assert thesis_to_signals(_thesis("Disaster / insurance underpricing", *themes), as_of=T0) == []


def test_a_disaster_agent_thesis_yields_no_signal(store: GraphStore) -> None:
    view = store.as_of_view(T0 + timedelta(hours=5))
    assert fired_to_signals(_fired("e1", domain="disaster"), view) == []
    named = FiredThesis(domain="energy", thesis="Hurricane disaster rally", direction="constructive", confidence=0.9,
                        inference="i", evidence=["e1"], adversary_verdict="UPHELD", adversary_reason="r")
    assert fired_to_signals(named, view) == []


def test_no_table_entry_gives_a_direction_to_disaster() -> None:
    assert not [k for k in THEME_DIRECTIONS if "isaster" in k[0]]


# ---------------------------------------------------------------- agent theses


def test_an_agent_signal_lands_on_the_symbols_its_events_mention(store: GraphStore) -> None:
    view = store.as_of_view(T0 + timedelta(hours=5))
    got = fired_to_signals(_fired("e1"), view, evidence_hash="abc")
    assert {(s.symbol, s.action) for s in got} == {("XLE", "veto"), ("XLE", "exit"), ("USO", "veto"), ("USO", "exit")}
    assert all(s.origin == "agent" and s.symbol_source == "cited_event" and s.evidence_ids == ("e1",)
               and s.evidence_hash == "abc" and s.strength == 0.8 and s.as_of == view.cutoff.isoformat() for s in got)
    assert {s.action for s in fired_to_signals(_fired("e1", direction="constructive"), view)} == {"confirm", "entry"}


def test_an_agent_signal_cannot_use_a_mention_from_after_the_cutoff(store: GraphStore) -> None:
    view = store.as_of_view(T0 + timedelta(hours=5))
    store.ingest([Edge(("event", "e1"), "mentions", ("symbol", "LATE"), as_of=(T0 + timedelta(hours=9)).isoformat())])
    assert "LATE" not in {s.symbol for s in fired_to_signals(_fired("e1"), view)}


def test_an_agent_thesis_on_an_event_mentioning_nothing_yields_nothing(store: GraphStore) -> None:
    assert fired_to_signals(_fired("no-such-event"), store.as_of_view(T0 + timedelta(hours=5))) == []


# ---------------------------------------------------------------- shadow only


def test_a_signal_refuses_any_stage_but_shadow() -> None:
    good = thesis_to_signals(_thesis("Commodity carry-inversion proxy", "energy"), as_of=T0, universe={"XLE"})[0]
    with pytest.raises(ValueError):
        ThesisSignal(**{**good.__dict__, "stage": "live"})
    with pytest.raises(ValueError):
        ThesisSignal(**{**good.__dict__, "strength": 1.5})


def _imports(path: Path) -> set[str]:
    found: set[str] = set()
    for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
        if isinstance(node, ast.ImportFrom) and node.module:
            found.add(node.module)
        elif isinstance(node, ast.Import):
            found.update(a.name for a in node.names)
    return found


def test_the_module_and_script_import_nothing_from_the_order_path() -> None:
    root = Path(intel.__file__).resolve().parents[1]
    script = root.parents[1] / "scripts" / "thesis_shadow.py"
    for path in (Path(ts.__file__), script):
        banned = [m for m in _imports(path) if any(f".{x}" in m for x in ("execution", "brokers", "monitor", "risk", "strategies"))]
        assert not banned, path.name
    for name in ("place_order", "submit(", "Router", "OrderIntent"):
        assert name not in Path(ts.__file__).read_text(encoding="utf-8"), name


def test_nothing_on_the_order_path_reads_the_shadow_book() -> None:
    root = Path(intel.__file__).resolve().parents[1]
    for folder in ("execution", "monitor", "risk", "brokers", "strategies"):
        for path in (root / folder).rglob("*.py"):
            text = path.read_text(encoding="utf-8")
            assert "thesis_signal" not in text and "THESIS_SIGNAL" not in text, path


# ---------------------------------------------------------------- the book


def _signals(at: datetime = T0, **kw: Any) -> list[ThesisSignal]:
    return thesis_to_signals(_thesis("Commodity carry-inversion proxy", "energy"), as_of=at, universe={"XLE"}, **kw)


def test_the_book_records_each_signal_once_on_a_valid_chain(book: ThesisBook, tmp_path: Path) -> None:
    assert book.record(_signals()) == 2
    assert book.record(_signals()) == 0
    assert book.verify()[0] is True
    rows = [json.loads(line) for f in (tmp_path / "ledger").glob("*.thesis.jsonl") for line in f.read_text(encoding="utf-8").splitlines()]
    assert {r["event"] for r in rows} == {"THESIS_SIGNAL"} and all(r["stream"] == "thesis" for r in rows)
    assert verify_chain(rows)[0] is True
    assert all(r["payload"]["stage"] == "shadow" for r in rows)


def test_an_edit_to_a_recorded_signal_breaks_the_chain(book: ThesisBook, tmp_path: Path) -> None:
    book.record(_signals())
    path = next((tmp_path / "ledger").glob("*.thesis.jsonl"))
    text = path.read_text(encoding="utf-8")
    assert '"strength": 1.0' in text or '"strength":1.0' in text
    path.write_text(text.replace('"strength": 1.0', '"strength": 0.1').replace('"strength":1.0', '"strength":0.1'), encoding="utf-8")
    assert ThesisBook(tmp_path / "ledger").verify()[0] is False


def test_the_book_reads_point_in_time(book: ThesisBook) -> None:
    book.record(_signals(T0))
    book.record(_signals(T0 + timedelta(days=2)))
    assert len(book.signals()) == 4
    assert len(book.signals(before=T0)) == 0                # strictly before
    assert len(book.signals(before=T0 + timedelta(seconds=1))) == 2
    assert len(book.open_at(T0 + timedelta(days=6))) == 2   # the first has expired at five days
    assert book.signals()[0]["recorded_at"]


def test_replays_stay_out_of_the_forward_record(book: ThesisBook) -> None:
    book.record(_signals(T0, replay=True))
    assert book.signals() == [] and book.open_at(T0 + timedelta(days=1)) == []
    assert len(book.signals(include_replay=True)) == 2


# ---------------------------------------------------------------- the pass


@pytest.fixture()
def journals(tmp_path: Path) -> dict[str, Path]:
    return {"overlay_journal": tmp_path / "overlay.jsonl", "graph_journal": tmp_path / "graph.jsonl",
            "news_path": tmp_path / "news.jsonl", "agents_journal": tmp_path / "agents.jsonl"}


STRESS = {"event_acceleration": {"energy": 6.0, "conflict": 1.0, "military": 1.0}, "energy_stress": 0.4}


def test_a_pass_with_a_fresh_snapshot_records_rule_signals(store: GraphStore, book: ThesisBook, journals: dict[str, Path]) -> None:
    now = T0 + timedelta(hours=5)
    journals["overlay_journal"].write_text(_snapshot_row(now - timedelta(hours=1), **STRESS) + "\n", encoding="utf-8")
    result = run_shadow_pass(store, book, cutoff=now, **journals)
    assert result.snapshot_as_of == (now - timedelta(hours=1)).isoformat()
    assert result.signals and result.recorded == len(result.signals)
    assert result.replay is True and all(s.replay for s in result.signals)


def test_a_stale_feed_records_nothing_and_says_why(store: GraphStore, book: ThesisBook, journals: dict[str, Path]) -> None:
    now = T0 + timedelta(hours=30)
    journals["overlay_journal"].write_text(_snapshot_row(T0, **STRESS) + "\n", encoding="utf-8")
    result = run_shadow_pass(store, book, cutoff=now, **journals)
    assert result.signals == () and result.recorded == 0
    assert "over the 6h limit" in result.notes[0]


def test_no_feed_at_all_records_nothing_and_says_why(store: GraphStore, book: ThesisBook, journals: dict[str, Path]) -> None:
    result = run_shadow_pass(store, book, cutoff=T0, **journals)
    assert result.signals == () and "no real snapshot" in result.notes[0]


def test_a_snapshot_from_after_the_cutoff_cannot_be_used(store: GraphStore, book: ThesisBook, journals: dict[str, Path]) -> None:
    journals["overlay_journal"].write_text(_snapshot_row(T0 + timedelta(hours=9), **STRESS) + "\n", encoding="utf-8")
    result = run_shadow_pass(store, book, cutoff=T0 + timedelta(hours=8), **journals)
    assert result.signals == () and result.snapshot_as_of is None


def test_a_degraded_snapshot_is_not_used(store: GraphStore, book: ThesisBook, journals: dict[str, Path]) -> None:
    journals["overlay_journal"].write_text(_snapshot_row(T0, degraded=True, **STRESS) + "\n", encoding="utf-8")
    assert run_shadow_pass(store, book, cutoff=T0 + timedelta(hours=1), **journals).signals == ()


def test_a_pass_catches_up_with_the_graph_journal(store: GraphStore, book: ThesisBook, journals: dict[str, Path]) -> None:
    append_edges(_event("fresh", "energy", 3, "XOM"), path=journals["graph_journal"])
    run_shadow_pass(store, book, cutoff=T0 + timedelta(hours=5), **journals)
    assert store.as_of_view(T0 + timedelta(hours=5)).edges(object=("symbol", "XOM"))


def test_a_pass_makes_no_api_call_unless_asked(store: GraphStore, book: ThesisBook, journals: dict[str, Path]) -> None:
    with respx.mock(assert_all_called=False) as mock:
        route = mock.post(_URL)
        run_shadow_pass(store, book, cutoff=T0 + timedelta(hours=5), **journals)
    assert route.call_count == 0


@pytest.fixture()
def _fake_key(monkeypatch: pytest.MonkeyPatch) -> None:
    from trading_live_claude.config import get_settings
    get_settings.cache_clear()
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test-key")


def _reply(text: str) -> httpx.Response:
    return httpx.Response(200, json={"content": [{"type": "text", "text": text}], "stop_reason": "end_turn", "usage": {}})


@respx.mock
def test_a_debate_in_a_pass_reaches_the_book(store: GraphStore, book: ThesisBook, journals: dict[str, Path], _fake_key: None) -> None:
    claim = json.dumps({"thesis": "Supply", "direction": "adverse", "confidence": 0.8, "inference": "i", "evidence": ["e1"]})
    upheld = json.dumps({"verdict": "UPHELD", "reason": "r", "demote_by": 0})

    def reply(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        if "adversary" in body["system"]:
            return _reply(upheld)
        return _reply(claim if "Domain: energy" in body["messages"][0]["content"] else
                      json.dumps({"thesis": "x", "direction": "neutral", "confidence": 0.1}))
    respx.post(_URL).mock(side_effect=reply)
    result = run_shadow_pass(store, book, cutoff=T0 + timedelta(hours=5), debate_targets=[("symbol", "XLE")], **journals)
    assert {s.symbol for s in result.signals if s.origin == "agent"} == {"XLE", "USO"}
    assert result.debate_errors == {} and result.signals[0].evidence_hash
    assert len(book.signals(include_replay=True)) == result.recorded


@respx.mock
def test_a_failed_debate_is_reported_and_posts_nothing(store: GraphStore, book: ThesisBook, journals: dict[str, Path], _fake_key: None) -> None:
    respx.post(_URL).mock(return_value=httpx.Response(400, json={"error": {"type": "invalid_request_error"}}))
    result = run_shadow_pass(store, book, cutoff=T0 + timedelta(hours=5), debate_targets=[("symbol", "XLE")], **journals)
    assert result.signals == () and result.debate_errors["XLE"]["energy"] == "api_400:invalid_request_error"
    assert any("failed" in n for n in result.notes)
