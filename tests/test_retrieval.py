"""Phase 3's done criteria: a debate cites paths that exist, sees nothing from the future, and keeps disaster out."""
from __future__ import annotations

import inspect
import json
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import httpx
import pytest

pytest.importorskip("networkx")
pytest.importorskip("scipy")

from trading_live_claude import intel
from trading_live_claude.intel import graph_search as gs
from trading_live_claude.intel import retrieval as rt
from trading_live_claude.intel.graph import Edge, event_records_to_edges
from trading_live_claude.intel.graph_store import GraphStore, GraphView

T0 = datetime(2026, 6, 1, 12, 0, tzinfo=UTC)
AFTER = T0 + timedelta(hours=5)
XLE = ("symbol", "XLE")


def _at(hours: float) -> str:
    return (T0 + timedelta(hours=hours)).isoformat()


def _event(event_id: str, title: str, sources: list[str], category: str, hours: float, *symbols: str) -> list[Edge]:
    stamp = _at(hours)
    rec = {"id": event_id, "title": title, "sources": sources, "categories": [category]}
    return (event_records_to_edges([rec], domain=category, poll_id=stamp, as_of=stamp)
            + [Edge(("event", event_id), "mentions", ("symbol", s), as_of=stamp) for s in symbols])


def _sample() -> list[Edge]:
    return [*_event("e1", "Refinery strike halts pipeline flows", ["ap", "reuters"], "energy", 1, "XLE"),
            *_event("e2", "OPEC holds output quotas", ["reuters"], "energy", 2, "XLE", "USO"),
            *_event("e3", "Border clash escalates", ["bbc"], "conflict", 3, "LMT"),
            *_event("e4", "Hurricane batters the gulf refinery coast", ["ap"], "disaster", 4, "XLE", "RE"),
            Edge(("venue", "kraken"), "traded", XLE, weight=5000.0, as_of=_at(1)),
            Edge(("society", "run1"), "holds_stance", XLE, weight=0.9, as_of=_at(1))]


def _future() -> list[Edge]:
    return _event("leak", "Refinery leak strike XLE", ["ap"], "energy", 100, "XLE")


def _news_row(source: str, digest: str, title: str, hours: float) -> dict[str, str]:
    return {"source": source, "content_hash": digest, "title": title, "arrived_utc": _at(hours)}


@pytest.fixture()
def store(tmp_path: Path) -> Iterator[GraphStore]:
    with GraphStore(tmp_path / "g.sqlite") as s:
        s.ingest(_sample())
        yield s


@pytest.fixture()
def view(store: GraphStore) -> GraphView:
    return store.as_of_view(AFTER)


NO_NEWS = "no-news.jsonl"


# ---------------------------------------------------------------- cached views


def test_a_cached_view_reads_once_and_agrees_with_the_plain_view(view: GraphView, monkeypatch: pytest.MonkeyPatch) -> None:
    cached = view.cached()
    seen = []
    original = GraphView._select

    def spy(self: GraphView, where: str = "", params: Any = ()) -> Any:
        seen.append(where)
        return original(self, where, params)
    monkeypatch.setattr(GraphView, "_select", spy)
    first = cached.edges(predicate="mentions")
    assert cached.edges(predicate="mentions") == first
    assert len(seen) == 1
    assert first == view.edges(predicate="mentions")
    assert cached.cached() is cached


def test_searches_share_one_built_graph_on_a_cached_view(view: GraphView) -> None:
    cached = view.cached()
    assert gs.build_graph(cached) is gs.build_graph(cached)
    assert gs.build_graph(view) is not gs.build_graph(view)
    assert gs.personalized_pagerank(cached, [XLE]) == gs.personalized_pagerank(view, [XLE])


def test_a_cached_view_keeps_the_cutoff(store: GraphStore) -> None:
    cached = store.as_of_view(T0 + timedelta(hours=2, minutes=30)).cached()
    assert {e.as_of for e in cached.edges()} <= {_at(1), _at(2)}


# ---------------------------------------------------------------- the news join


def _write_news(path: Path, rows: list[dict[str, str]]) -> Path:
    path.write_text("".join(json.dumps(r) + "\n" for r in rows), encoding="utf-8")
    return path


def test_news_titles_stop_strictly_before_the_cutoff(tmp_path: Path, view: GraphView) -> None:
    journal = _write_news(tmp_path / "news.jsonl", [
        _news_row("ap", "old", "Known headline", 1), _news_row("ap", "edge", "Exactly at the cutoff", 5),
        _news_row("ap", "new", "Future headline", 6),
        {"source": "ap", "content_hash": "undated", "title": "No time", "arrived_utc": ""}])
    assert rt.news_titles(view, journal) == {"news:ap:old": "Known headline"}


def test_a_missing_news_journal_is_empty(tmp_path: Path, view: GraphView) -> None:
    assert rt.news_titles(view, tmp_path / NO_NEWS) == {}


def test_news_titles_fill_mentions_events_for_search_and_evidence(tmp_path: Path, store: GraphStore) -> None:
    store.ingest([Edge(("event", "news:ap:abc"), "mentions", XLE, as_of=_at(4.5))])
    journal = _write_news(tmp_path / "news.jsonl", [_news_row("ap", "abc", "Tanker fire closes the strait", 4.5)])
    v = store.as_of_view(AFTER)
    bundle = rt.retrieve_evidence(v, "tanker strait", news_path=journal)
    assert [i.event_id for i in bundle.items if i.title.startswith("Tanker")] == ["news:ap:abc"]
    assert rt.retrieve_evidence(v, "tanker strait", news_path=tmp_path / NO_NEWS).items == ()


def test_a_later_news_row_cannot_reach_an_earlier_search(tmp_path: Path, store: GraphStore) -> None:
    """The edge is old enough to see, but its headline arrived later: the words must stay unknown."""
    store.ingest([Edge(("event", "news:ap:abc"), "mentions", XLE, as_of=_at(1))])
    journal = _write_news(tmp_path / "news.jsonl", [_news_row("ap", "abc", "Tanker fire", 50)])
    assert rt.retrieve_evidence(store.as_of_view(AFTER), "tanker", news_path=journal).items == ()
    assert rt.retrieve_evidence(store.as_of_view(T0 + timedelta(hours=51)), "tanker", news_path=journal).items


# ---------------------------------------------------------------- retrieval


def test_a_symbol_gets_its_events_with_paths_that_exist(tmp_path: Path, view: GraphView) -> None:
    bundle = rt.retrieve_evidence(view, XLE, news_path=tmp_path / NO_NEWS)
    ids = [i.event_id for i in bundle.items]
    assert {"e1", "e2"} <= set(ids)
    graph = gs.build_graph(view, gs.SIGNAL_SCOPE)
    for item in bundle.items:
        assert item.path is not None and item.path.nodes[0] == XLE and item.path.nodes[-1] == ("event", item.event_id)
        for hop in item.path.hops:
            assert graph.has_edge(hop.source, hop.target)
    assert all(i.title for i in bundle.items if i.event_id in ("e1", "e2"))


def test_records_show_how_an_event_connects(tmp_path: Path, view: GraphView) -> None:
    rec = next(r for r in rt.retrieve_evidence(view, XLE, news_path=tmp_path / NO_NEWS).records() if r["id"] == "e2")
    assert rec["title"] == "OPEC holds output quotas" and rec["symbols"] == ["USO", "XLE"]
    assert rec["path"] == ["e2 -mentions-> XLE"]


def test_free_text_finds_by_words_and_widens_through_the_graph(tmp_path: Path, view: GraphView) -> None:
    bundle = rt.retrieve_evidence(view, "refinery strike", news_path=tmp_path / NO_NEWS)
    assert bundle.items[0].event_id == "e1"
    assert "text" in bundle.items[0].found_by
    assert any(i.event_id == "e2" and i.found_by == ("graph",) for i in bundle.items)


def test_a_target_nothing_connects_to_gets_nothing_not_general_news(tmp_path: Path, view: GraphView) -> None:
    assert rt.retrieve_evidence(view, ("symbol", "NOPE"), news_path=tmp_path / NO_NEWS).items == ()
    assert rt.retrieve_evidence(view, "words nothing uses", news_path=tmp_path / NO_NEWS).items == ()


def test_the_budget_caps_the_bundle_and_diversity_changes_the_pick(tmp_path: Path) -> None:
    with GraphStore(tmp_path / "d.sqlite") as s:
        s.ingest(_event("same1", "Oil strike", ["ap"], "energy", 1, "XLE") + _event("same2", "Oil strike again", ["ap"], "energy", 1, "XLE")
                 + _event("other", "Oil grid fault", ["bbc"], "economy", 1, "XLE"))
        bundle = rt.retrieve_evidence(s.as_of_view(AFTER), XLE, budget=2, news_path=tmp_path / NO_NEWS)
    assert len(bundle.items) == 2
    assert "other" in {i.event_id for i in bundle.items}   # not just the same story twice


def test_the_bundle_hash_is_stable_and_tracks_content(tmp_path: Path, store: GraphStore) -> None:
    news = tmp_path / NO_NEWS
    v = store.as_of_view(AFTER)
    first = rt.retrieve_evidence(v, XLE, news_path=news)
    assert first.content_hash == rt.retrieve_evidence(v, XLE, news_path=news).content_hash
    other = rt.retrieve_evidence(store.as_of_view(T0 + timedelta(hours=1, minutes=30)), XLE, news_path=news)
    assert other.content_hash != first.content_hash


def test_domain_runs_reach_the_context_from_poll_edges(tmp_path: Path) -> None:
    now = datetime.now(UTC)
    def poll(minutes: int, *domains: str) -> list[Edge]:
        stamp = (now - timedelta(minutes=minutes)).isoformat()
        return [Edge(("poll", stamp), "elevated_in", ("domain", d), weight=4.0, as_of=stamp) for d in domains]
    with GraphStore(tmp_path / "r.sqlite") as s:
        s.ingest(poll(90, "conflict") + poll(60, "conflict", "energy") + poll(30, "conflict", "energy"))
        bundle = rt.retrieve_evidence(s.as_of_view(now), "anything", news_path=tmp_path / NO_NEWS)
    assert bundle.domain_runs == {"conflict": 3, "energy": 2}
    assert bundle.co_runs == {"conflict+energy": 2}
    assert bundle.context()["consecutive_polls_elevated_by_domain"]["conflict"] == 3


# ---------------------------------------------------------------- disaster stays out of anything that can become a signal


def test_disaster_events_never_reach_a_bundle(tmp_path: Path, view: GraphView) -> None:
    news = tmp_path / NO_NEWS
    for target in (XLE, ("symbol", "RE"), "hurricane gulf", "refinery"):
        bundle = rt.retrieve_evidence(view, target, news_path=news)
        assert "e4" not in {i.event_id for i in bundle.items}, target
        assert "urricane" not in json.dumps(bundle.records())


# ---------------------------------------------------------------- nothing sees the future


@pytest.mark.parametrize("target", [XLE, "refinery strike"])
def test_future_edges_change_no_bundle(tmp_path: Path, store: GraphStore, target: Any) -> None:
    news = tmp_path / NO_NEWS
    v = store.as_of_view(AFTER)
    before = rt.retrieve_evidence(v, target, news_path=news)
    store.ingest(_future())
    assert rt.retrieve_evidence(v, target, news_path=news) == before
    after = rt.retrieve_evidence(store.as_of_view(T0 + timedelta(hours=101)), target, news_path=news)
    assert "leak" in {i.event_id for i in after.items}     # the same edges do arrive for a later view


def test_the_retrieval_module_reads_only_through_a_view_and_the_news_join() -> None:
    source = inspect.getsource(rt)
    for banned in ("load_edges", "default_graph_journal", "GraphStore", "sync_journal", "._select"):
        assert banned not in source, banned


def test_nothing_on_the_trading_path_imports_retrieval() -> None:
    root = Path(intel.__file__).resolve().parents[1]
    for folder in ("execution", "monitor", "risk", "brokers", "strategies"):
        for path in (root / folder).rglob("*.py"):
            assert "intel.retrieval" not in path.read_text(encoding="utf-8"), path


# ---------------------------------------------------------------- tools


# ---------------------------------------------------------------- the debate: citations, tools, memory, trace


def _reply(text: str, *, stop: str = "end_turn", extra: list[dict[str, Any]] | None = None) -> httpx.Response:
    return httpx.Response(200, json={"content": [*(extra or []), {"type": "text", "text": text}], "stop_reason": stop,
                                     "usage": {"input_tokens": 100, "output_tokens": 20}})


def _claim(*evidence: str, direction: str = "adverse", confidence: float = 0.8) -> str:
    return json.dumps({"thesis": "Supply stress", "direction": direction, "confidence": confidence,
                       "inference": "One line.", "evidence": list(evidence)})


UPHELD = json.dumps({"verdict": "UPHELD", "reason": "Corroborated.", "demote_by": 0})


def _script(*replies: httpx.Response) -> Any:
    it = iter(replies)
    return lambda _req: next(it)


# ---------------------------------------------------------------- failure is told apart from silence


