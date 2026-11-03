"""Graph search on a fixed sample graph, small enough that every answer can be checked by hand."""
from __future__ import annotations

import ast
import inspect
from collections.abc import Callable, Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

nx = pytest.importorskip("networkx")
pytest.importorskip("scipy")

from trading_live_claude.intel import graph_search as gs  # noqa: E402
from trading_live_claude.intel.graph import Edge, event_records_to_edges  # noqa: E402
from trading_live_claude.intel.graph_search import EVIDENCE_SCOPE, SIGNAL_SCOPE, SearchScope  # noqa: E402
from trading_live_claude.intel.graph_store import GraphStore, GraphView  # noqa: E402

T0 = datetime(2026, 6, 1, 12, 0, tzinfo=UTC)
AFTER = T0 + timedelta(hours=5)     # after the whole sample graph
XLE, USO, LMT, RE = ("symbol", "XLE"), ("symbol", "USO"), ("symbol", "LMT"), ("symbol", "RE")
E1, E2, E3, E4 = ("event", "e1"), ("event", "e2"), ("event", "e3"), ("event", "e4")
ENERGY, CONFLICT, DISASTER = ("domain", "energy"), ("domain", "conflict"), ("domain", "disaster")
AP, REUTERS, BBC = ("source", "ap"), ("source", "reuters"), ("source", "bbc")


def _at(hours: float) -> str:
    return (T0 + timedelta(hours=hours)).isoformat()


def _event(event_id: str, title: str, sources: list[str], category: str, hours: float, *symbols: str) -> list[Edge]:
    stamp = _at(hours)
    rec = {"id": event_id, "title": title, "sources": sources, "categories": [category]}
    edges = event_records_to_edges([rec], domain=category, poll_id=stamp, as_of=stamp)
    return edges + [Edge(("event", event_id), "mentions", ("symbol", s), as_of=stamp) for s in symbols]


def sample_edges() -> list[Edge]:
    """Two energy events joined by Reuters and XLE, one conflict event, one disaster event, and non-evidence."""
    return [
        *_event("e1", "Refinery strike halts pipeline flows", ["ap", "reuters"], "energy", 1, "XLE"),
        *_event("e2", "OPEC holds output quotas", ["reuters"], "energy", 2, "XLE", "USO"),
        *_event("e3", "Border clash escalates", ["bbc"], "conflict", 3, "LMT"),
        *_event("e4", "Hurricane batters the gulf coast", ["ap"], "disaster", 4, "RE"),
        Edge(("venue", "kraken"), "traded", XLE, weight=5000.0, as_of=_at(1)),
        Edge(("society", "run1"), "holds_stance", XLE, weight=0.9, as_of=_at(1)),
        Edge(("analysis", "lens"), "ranked_by", XLE, as_of=_at(1)),
    ]


@pytest.fixture()
def store(tmp_path: Path) -> Iterator[GraphStore]:
    with GraphStore(tmp_path / "g.sqlite") as s:
        s.ingest(sample_edges())
        yield s


@pytest.fixture()
def view(store: GraphStore) -> GraphView:
    return store.as_of_view(AFTER)


def _ranks(result: list[tuple[Any, float]]) -> list[Any]:
    return [n for n, _ in result]


# ---------------------------------------------------------------- what counts as evidence


def test_the_default_scope_leaves_out_our_trades_simulations_analysis_and_polls(view: GraphView) -> None:
    graph = gs.build_graph(view)
    assert {n[0] for n in graph.nodes} == {"event", "source", "domain", "symbol"}
    assert not {d["predicate"] for _, _, d in graph.edges(data=True)} & {"traded", "holds_stance", "ranked_by"}


def test_the_signal_scope_removes_disaster_events_and_all_they_touch_alone(view: GraphView) -> None:
    graph = gs.build_graph(view, SIGNAL_SCOPE)
    assert E4 not in graph and DISASTER not in graph and RE not in graph
    assert AP in graph                                    # a source that also reported other events stays
    assert E4 in gs.build_graph(view, EVIDENCE_SCOPE)     # the record itself is untouched


def test_the_signal_scope_reaches_every_search(view: GraphView) -> None:
    assert gs.bm25_search(view, "hurricane", scope=SIGNAL_SCOPE) == []
    assert [h.event_id for h in gs.bm25_search(view, "hurricane")] == ["e4"]
    assert RE not in gs.neighbors(view, AP, 2, scope=SIGNAL_SCOPE)
    assert gs.shortest_path(view, AP, RE, scope=SIGNAL_SCOPE) is None
    assert RE not in _ranks(gs.personalized_pagerank(view, [AP], scope=SIGNAL_SCOPE))
    assert all(h.node != RE for h in gs.temporal_walk(view, AP, scope=SIGNAL_SCOPE))
    assert not any(RE in c for c in gs.communities(view, scope=SIGNAL_SCOPE))
    assert all(t.node != RE for t in gs.trending_nodes(view, scope=SIGNAL_SCOPE, min_baseline=0))
    assert RE not in _ranks(gs.centrality(view, scope=SIGNAL_SCOPE))


def test_only_predicates_narrows_a_search(view: GraphView) -> None:
    scope = SearchScope(only_predicates=frozenset({"mentions"}))
    assert {d["predicate"] for _, _, d in gs.build_graph(view, scope).edges(data=True)} == {"mentions"}


# ---------------------------------------------------------------- traversal


def test_neighbors_counts_hops_and_respects_direction(view: GraphView) -> None:
    assert gs.neighbors(view, XLE) == {E1: 1, E2: 1}
    two = gs.neighbors(view, XLE, 2)
    assert two[E1] == 1 and two[REUTERS] == 2 and two[USO] == 2 and two[ENERGY] == 2
    assert LMT not in two
    assert gs.neighbors(view, E1, direction="out")[XLE] == 1
    assert E1 not in gs.neighbors(view, XLE, direction="out")   # XLE has no outgoing edges
    assert E1 in gs.neighbors(view, XLE, direction="in")
    assert gs.neighbors(view, ("symbol", "NOPE")) == {}


def test_bfs_paths_comes_back_shortest_first_with_evidence(view: GraphView) -> None:
    paths = gs.bfs_paths(view, XLE, USO, max_hops=4)
    assert paths[0].nodes == (XLE, E2, USO)
    assert [len(p.nodes) for p in paths] == sorted(len(p.nodes) for p in paths)
    assert all(len(p.hops) == len(p.nodes) - 1 for p in paths)
    hop = paths[0].hops[0]
    assert (hop.source, hop.target, hop.predicate, hop.as_of) == (E2, XLE, "mentions", _at(2))
    assert gs.bfs_paths(view, XLE, USO, max_hops=1) == []
    assert len(gs.bfs_paths(view, XLE, USO, max_hops=4, limit=1)) == 1
    assert gs.bfs_paths(view, XLE, LMT) == []             # not connected, conflict is its own island
    assert gs.bfs_paths(view, XLE, XLE) == []


def test_shortest_path_ignores_direction_and_reports_a_gap(view: GraphView) -> None:
    path = gs.shortest_path(view, USO, E1)
    assert path is not None and len(path.nodes) == 4      # USO, e2, then a shared node, then e1
    assert gs.shortest_path(view, XLE, LMT) is None
    assert gs.shortest_path(view, XLE, ("symbol", "NOPE")) is None


def test_temporal_walk_only_runs_forward_in_time(store: GraphStore, tmp_path: Path) -> None:
    with GraphStore(tmp_path / "chain.sqlite") as chain:
        # A -- B at hour 1, B -- C at hour 2, then D -- A at hour 3. From C only B lies ahead: A's link to B
        # was already over by the time C joined, and so D, which only A leads to, is out of reach too.
        chain.ingest([Edge(("source", "a"), "mentioned_by", ("source", "b"), as_of=_at(1)),
                      Edge(("source", "b"), "mentioned_by", ("source", "c"), as_of=_at(2)),
                      Edge(("source", "d"), "mentioned_by", ("source", "a"), as_of=_at(3))])
        v = chain.as_of_view(AFTER)
        hits = gs.temporal_walk(v, ("source", "a"))
        assert [(h.node[1], h.arrived_as_of) for h in hits] == [("b", _at(1)), ("c", _at(2)), ("d", _at(3))]
        assert [h.node[1] for h in gs.temporal_walk(v, ("source", "c"))] == ["b"]
        assert [h.node[1] for h in gs.temporal_walk(v, ("source", "b"))] == ["a", "c", "d"]


def test_temporal_walk_chains_through_edges_at_the_same_instant(view: GraphView) -> None:
    hits = {h.node: h for h in gs.temporal_walk(view, XLE)}
    assert hits[E1].arrived_as_of == _at(1)
    assert hits[REUTERS].arrived_as_of == _at(1)          # reached through e1 within that one read
    assert hits[USO].arrived_as_of == _at(2)


def test_temporal_walk_honours_since(view: GraphView) -> None:
    hits = gs.temporal_walk(view, XLE, since=T0 + timedelta(hours=1, minutes=30))
    assert E1 not in {h.node for h in hits} and E2 in {h.node for h in hits}


# ---------------------------------------------------------------- ranking


def test_pagerank_matches_a_hand_rolled_power_iteration(tmp_path: Path) -> None:
    # A path a - b - c seeded at a, with damping 0.5. Solved by power iteration below, not by networkx.
    with GraphStore(tmp_path / "p.sqlite") as s:
        s.ingest([Edge(("source", "a"), "mentioned_by", ("source", "b"), as_of=_at(0)),
                  Edge(("source", "b"), "mentioned_by", ("source", "c"), as_of=_at(0))])
        got = dict(gs.personalized_pagerank(s.as_of_view(AFTER), [("source", "a")], alpha=0.5, include_seeds=True))
    a, b, c = 1.0, 0.0, 0.0
    for _ in range(200):
        a, b, c = 0.5 + 0.5 * (b / 2), 0.5 * (a + c), 0.5 * (b / 2)
    assert got[("source", "a")] == pytest.approx(a, abs=1e-5)
    assert got[("source", "b")] == pytest.approx(b, abs=1e-5)
    assert got[("source", "c")] == pytest.approx(c, abs=1e-5)


def test_pagerank_favours_what_is_close_to_the_seed(view: GraphView) -> None:
    ranked = _ranks(gs.personalized_pagerank(view, [USO]))
    assert ranked[0] == E2
    assert ranked.index(XLE) < ranked.index(AP)
    assert USO not in ranked                              # seeds are left out unless asked for
    assert LMT not in ranked[:5]


def test_pagerank_gives_nothing_to_what_a_seed_cannot_reach(view: GraphView) -> None:
    assert not {LMT, BBC, CONFLICT, E3} & set(_ranks(gs.personalized_pagerank(view, [XLE], top=None)))
    both = _ranks(gs.personalized_pagerank(view, [XLE, LMT], top=None))
    assert E3 in both and E1 in both                      # each seed brings its own component


def test_pagerank_ignores_unknown_seeds_and_handles_an_empty_graph(view: GraphView, tmp_path: Path) -> None:
    assert gs.personalized_pagerank(view, [("symbol", "NOPE")]) == []
    assert gs.personalized_pagerank(view, {USO: 1.0, ("symbol", "NOPE"): 5.0})
    with GraphStore(tmp_path / "e.sqlite") as empty:
        assert gs.personalized_pagerank(empty.as_of_view(AFTER), [USO]) == []
        assert gs.centrality(empty.as_of_view(AFTER)) == []
        assert gs.communities(empty.as_of_view(AFTER)) == []


def test_centrality_picks_the_hub_and_filters_results_by_type(view: GraphView) -> None:
    assert gs.centrality(view, node_type="source")[0][0] in (AP, REUTERS)
    assert {n[0] for n, _ in gs.centrality(view, node_type="event")} == {"event"}
    between = dict(gs.centrality(view, "betweenness"))
    assert between[REUTERS] > between[USO] == 0.0         # Reuters joins two events; USO is a leaf
    assert gs.centrality(view, "pagerank")
    with pytest.raises(ValueError):
        gs.centrality(view, "nonsense")  # type: ignore[arg-type]


def test_communities_separate_the_unconnected_and_repeat_exactly(view: GraphView) -> None:
    found = gs.communities(view)
    home = {n: i for i, c in enumerate(found) for n in c}
    assert home[LMT] == home[BBC] == home[CONFLICT]
    assert home[LMT] != home[XLE]
    assert gs.communities(view) == found


def test_neighbor_similarity_is_jaccard(view: GraphView) -> None:
    sim = gs.neighbor_similarity(view)
    assert sim(XLE, XLE) == 1.0
    assert sim(XLE, USO) == pytest.approx(1 / 2)          # {e1, e2} against {e2}
    assert sim(XLE, LMT) == 0.0
    assert sim(XLE, ("symbol", "NOPE")) == 0.0


# ---------------------------------------------------------------- text


def test_bm25_finds_by_title_source_category_and_symbol(view: GraphView) -> None:
    assert [h.event_id for h in gs.bm25_search(view, "refinery strike")] == ["e1"]
    assert {h.event_id for h in gs.bm25_search(view, "reuters")} == {"e1", "e2"}
    assert {h.event_id for h in gs.bm25_search(view, "conflict")} == {"e3"}
    assert {h.event_id for h in gs.bm25_search(view, "XLE")} == {"e1", "e2"}
    assert gs.bm25_search(view, "nothing matches this") == []


def test_bm25_ranks_a_title_match_above_a_lesser_field(tmp_path: Path) -> None:
    with GraphStore(tmp_path / "t.sqlite") as s:
        s.ingest(_event("title", "Energy shock", ["ap"], "markets", 1) + _event("cat", "Quiet day", ["ap"], "energy", 1))
        hits = gs.bm25_search(s.as_of_view(AFTER), "energy")
    assert [h.event_id for h in hits] == ["title", "cat"] and hits[0].score > hits[1].score > 0


def test_bm25_takes_any_text_without_breaking(view: GraphView) -> None:
    for query in ('" OR 1=1 --', "NEAR(", "title:*", "AND", "BTC/USD", "'; DROP TABLE docs; --"):
        gs.bm25_search(view, query)
    assert gs.bm25_search(view, "") == [] and gs.bm25_search(view, "!!!") == []


def test_bm25_scores_are_point_in_time(tmp_path: Path) -> None:
    """A word's rarity is part of its score, so later documents must not change an earlier search."""
    with GraphStore(tmp_path / "idf.sqlite") as s:
        s.ingest(_event("old1", "Strike at the port", ["ap"], "energy", 1) + _event("old2", "Calm trading", ["ap"], "energy", 1))
        cutoff = T0 + timedelta(hours=2)
        before = gs.bm25_search(s.as_of_view(cutoff), "strike")
        for i in range(30):
            s.ingest(_event(f"later{i}", "Strike strike everywhere", ["ap"], "energy", 3))
        assert gs.bm25_search(s.as_of_view(cutoff), "strike") == before
        assert gs.bm25_search(s.as_of_view(AFTER), "strike")[0].score != before[0].score


# ---------------------------------------------------------------- time


def _poll(hours: float, *domains: str) -> list[Edge]:
    stamp = _at(hours)
    return [Edge(("poll", stamp), "observed", ("source", "news"), as_of=stamp),
            *(Edge(("poll", stamp), "elevated_in", ("domain", d), as_of=stamp) for d in domains)]


def test_co_persistence_needs_both_edges_in_consecutive_polls(tmp_path: Path) -> None:
    a, b = ("elevated_in", ("domain", "conflict")), ("elevated_in", ("domain", "energy"))
    with GraphStore(tmp_path / "c.sqlite") as s:
        s.ingest(_poll(0, "conflict", "energy") + _poll(1, "conflict") + _poll(2, "conflict", "energy")
                 + _poll(3, "conflict", "energy"))
        assert gs.co_persistence(s.as_of_view(AFTER), a, b) == 2  # hour 1 lacked energy
        s.ingest([Edge(("venue", "kraken"), "traded", XLE, as_of=_at(3.5))])
        assert gs.co_persistence(s.as_of_view(AFTER), a, b) == 2  # a fill is not a poll
        assert gs.co_persistence(s.as_of_view(T0 + timedelta(hours=2, minutes=1)), a, b) == 1
        assert gs.co_persistence(s.as_of_view(AFTER), a, ("elevated_in", ("domain", "economy"))) == 0
        late = s.as_of_view(T0 + timedelta(days=30))
        assert gs.co_persistence(late, a, b) == 2
        assert gs.co_persistence(late, a, b, max_age_hours=6.0) == 0


def test_first_seen(view: GraphView, store: GraphStore) -> None:
    assert gs.first_seen(view, object=XLE, predicate="mentions") == _at(1)
    assert gs.first_seen(view, object=USO) == _at(2)
    assert gs.first_seen(store.as_of_view(T0 + timedelta(hours=1, minutes=30)), object=USO) is None


def test_thickening_rate_compares_two_windows(tmp_path: Path) -> None:
    target = ("domain", "energy")
    edge = lambda h: Edge(("event", f"e{h}"), "about_domain", target, as_of=_at(h))  # noqa: E731
    with GraphStore(tmp_path / "th.sqlite") as s:
        s.ingest([edge(h) for h in (1, 2, 25, 26, 27, 28)])
        got = gs.thickening_rate(s.as_of_view(T0 + timedelta(hours=48)), "about_domain", target)
        assert (got.recent, got.prior, got.rate) == (4, 2, 2.0)
        fresh = gs.thickening_rate(s.as_of_view(T0 + timedelta(hours=3)), "about_domain", target)
        assert (fresh.recent, fresh.prior, fresh.rate) == (2, 0, None)   # no prior edges, so no made-up ratio


def _mentions(symbol: str, hours: list[float]) -> list[Edge]:
    return [Edge(("event", f"{symbol}{i}"), "mentions", ("symbol", symbol), as_of=_at(h)) for i, h in enumerate(hours)]


def test_trending_rewards_acceleration_against_a_nodes_own_norm(tmp_path: Path) -> None:
    now_h = 24 * 8
    steady = [now_h - 24 * d - 1 for d in range(1, 8)] * 2     # two a day for the week before
    with GraphStore(tmp_path / "tr.sqlite") as s:
        s.ingest(_mentions("HOT", [*steady, *[now_h - x / 2 for x in range(1, 9)]]))   # 8 today
        s.ingest(_mentions("FLAT", [*steady, now_h - 1, now_h - 2]))                  # 2 today, as usual
        s.ingest(_mentions("NEW", [now_h - 1, now_h - 2, now_h - 3]))                 # no history at all
        hits = gs.trending_nodes(s.as_of_view(T0 + timedelta(hours=now_h)), node_type="symbol")
    by = {t.node[1]: t for t in hits}
    assert by["HOT"].acceleration == pytest.approx(4.0)
    assert by["FLAT"].acceleration == pytest.approx(1.0)
    assert by["NEW"].acceleration == 1.0 and by["NEW"].baseline == 0   # thin baseline reads neutral, not a spike
    assert hits[0].node == ("symbol", "HOT")


def test_trending_with_no_baseline_floor_still_cannot_divide_by_zero(tmp_path: Path) -> None:
    with GraphStore(tmp_path / "z.sqlite") as s:
        s.ingest(_mentions("NEW", [47.0]))
        hit = gs.trending_nodes(s.as_of_view(T0 + timedelta(hours=48)), node_type="symbol", min_baseline=0)[0]
    assert hit.acceleration == 1.0


def test_trending_caps_the_ratio(tmp_path: Path) -> None:
    now_h = 24 * 8
    quiet = [now_h - 25 - i * 24 for i in range(5)]
    with GraphStore(tmp_path / "cap.sqlite") as s:
        s.ingest(_mentions("X", [*quiet, *[now_h - i / 10 - 0.1 for i in range(100)]]))
        hit = gs.trending_nodes(s.as_of_view(T0 + timedelta(hours=now_h)), node_type="symbol")[0]
    assert hit.acceleration == 10.0


# ---------------------------------------------------------------- fusion


def test_reciprocal_rank_fusion_by_hand() -> None:
    fused = dict(gs.reciprocal_rank_fusion([["a", "b"], ["b", "c"]], k=60))
    assert fused["b"] == pytest.approx(1 / 62 + 1 / 61)
    assert fused["a"] == pytest.approx(1 / 61)
    assert fused["c"] == pytest.approx(1 / 62)
    assert _ranks(gs.reciprocal_rank_fusion([["a", "b"], ["b", "c"]], top=1)) == ["b"]
    assert gs.reciprocal_rank_fusion([]) == []


def test_mmr_trades_relevance_for_variety() -> None:
    relevance = {"a": 1.0, "a2": 0.9, "b": 0.5}
    sim = lambda x, y: 1.0 if x[0] == y[0] else 0.0  # noqa: E731    # a and a2 say the same thing
    assert gs.mmr_diversify(["a", "a2", "b"], relevance, sim, lambda_=1.0, top=2) == ["a", "a2"]
    assert gs.mmr_diversify(["a", "a2", "b"], relevance, sim, lambda_=0.5, top=2) == ["a", "b"]
    assert gs.mmr_diversify([], relevance, sim) == []
    with pytest.raises(ValueError):
        gs.mmr_diversify(["a"], relevance, sim, lambda_=2.0)


# ---------------------------------------------------------------- nothing sees the future, phase 2's acceptance test


LATE = _at(100)
SEARCHES: dict[str, Callable[[GraphView], Any]] = {
    "build_graph": lambda v: sorted(map(str, gs.build_graph(v).edges(data=True))),
    "neighbors": lambda v: gs.neighbors(v, XLE, 3),
    "bfs_paths": lambda v: gs.bfs_paths(v, XLE, USO, max_hops=4),
    "shortest_path": lambda v: gs.shortest_path(v, XLE, LMT),
    "temporal_walk": lambda v: gs.temporal_walk(v, XLE),
    "personalized_pagerank": lambda v: gs.personalized_pagerank(v, [XLE]),
    "centrality": lambda v: gs.centrality(v, "betweenness"),
    "communities": lambda v: gs.communities(v),
    "neighbor_similarity": lambda v: gs.neighbor_similarity(v)(XLE, LMT),
    "bm25_search": lambda v: gs.bm25_search(v, "strike clash XLE leak"),
    "co_persistence": lambda v: gs.co_persistence(v, ("elevated_in", CONFLICT), ("elevated_in", ENERGY)),
    "first_seen": lambda v: gs.first_seen(v, object=LMT),
    "thickening_rate": lambda v: gs.thickening_rate(v, "mentions", XLE),
    "trending_nodes": lambda v: gs.trending_nodes(v, min_baseline=0),
}


def _future() -> list[Edge]:
    """Later edges built to change every search: a bridge joining XLE to LMT, and a matching headline."""
    return [*_event("leak", "Strike clash leak", ["ap", "bbc"], "energy", 100, "XLE", "LMT"),
            *_poll(100, "conflict", "energy")]


@pytest.mark.parametrize("name", sorted(SEARCHES))
def test_no_search_changes_when_future_edges_arrive(store: GraphStore, name: str) -> None:
    view = store.as_of_view(AFTER)
    before = SEARCHES[name](view)
    store.ingest(_future())
    assert SEARCHES[name](view) == before


def test_the_future_edges_do_change_a_view_taken_after_them(store: GraphStore) -> None:
    """Without this the test above could pass by the future edges simply having no effect."""
    before = {name: fn(store.as_of_view(AFTER)) for name, fn in SEARCHES.items()}
    store.ingest(_future())
    after = {name: fn(store.as_of_view(T0 + timedelta(hours=101))) for name, fn in SEARCHES.items()}
    unchanged = {name for name in SEARCHES if before[name] == after[name]}
    # first_seen(LMT) is the one whose answer is genuinely the same: the earliest mention did not move.
    assert unchanged == {"first_seen"}


def test_every_search_is_listed_in_the_future_test() -> None:
    public = {n for n, f in inspect.getmembers(gs, inspect.isfunction)
              if f.__module__ == gs.__name__ and not n.startswith("_")}
    assert public - {"reciprocal_rank_fusion", "mmr_diversify"} == set(SEARCHES)


def test_every_search_takes_a_view_first() -> None:
    for name in SEARCHES:
        first = next(iter(inspect.signature(getattr(gs, name)).parameters))
        assert first == "view", name


def test_the_module_has_no_other_route_to_the_data() -> None:
    tree = ast.parse(Path(gs.__file__).read_text(encoding="utf-8"))
    banned = {"load_edges", "open", "default_graph_journal", "GraphStore", "read_text", "sync_journal"}
    used = {n.id for n in ast.walk(tree) if isinstance(n, ast.Name)} | \
           {n.attr for n in ast.walk(tree) if isinstance(n, ast.Attribute)} | \
           {a.name for n in ast.walk(tree) if isinstance(n, ast.ImportFrom) for a in n.names}
    assert not used & banned
    assert not [n for n in ast.walk(tree) if isinstance(n, ast.Attribute) and n.attr == "_select"]
    connects = [n for n in ast.walk(tree) if isinstance(n, ast.Call) and getattr(n.func, "attr", "") == "connect"]
    assert len(connects) == 1 and connects[0].args[0].value == ":memory:"  # type: ignore[attr-defined]


def test_nothing_on_the_trading_path_imports_search() -> None:
    root = Path(gs.__file__).resolve().parents[1]
    for folder in ("execution", "monitor", "risk", "brokers", "strategies"):
        for path in (root / folder).rglob("*.py"):
            assert "graph_search" not in path.read_text(encoding="utf-8"), path


def test_the_same_view_gives_the_same_answers(view: GraphView) -> None:
    assert {n: fn(view) for n, fn in SEARCHES.items()} == {n: fn(view) for n, fn in SEARCHES.items()}


def test_results_do_not_depend_on_journal_order(tmp_path: Path) -> None:
    edges = sample_edges()
    with GraphStore(tmp_path / "f.sqlite") as forward, GraphStore(tmp_path / "r.sqlite") as backward:
        forward.ingest(edges)
        backward.ingest(reversed(edges))
        a, b = forward.as_of_view(AFTER), backward.as_of_view(AFTER)
        pa, pb = dict(gs.personalized_pagerank(a, [XLE])), dict(gs.personalized_pagerank(b, [XLE]))
        assert pa == pytest.approx(pb)
        assert gs.communities(a) == gs.communities(b)
        assert gs.bm25_search(a, "strike") == gs.bm25_search(b, "strike")
