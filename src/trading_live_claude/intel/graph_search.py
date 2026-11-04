"""Search over the intel graph, as it stood at a moment: traversal, ranking, text, time and fusion.

Every search takes a :class:`~trading_live_claude.intel.graph_store.GraphView` and reads nothing else, so it
cannot see past the view's cutoff. That includes statistics: BM25's document frequencies come only from the
documents visible at the cutoff, because a corpus-wide index would let future headlines change today's scores.
:func:`reciprocal_rank_fusion` and :func:`mmr_diversify` only combine results already found.

**What counts as evidence** is a :class:`SearchScope`. By default a search leaves out the book's own activity
(``traded``, ``scaled``, anything touching a ``venue``), so a thesis cannot read our own trades back as
intelligence; simulated societies and agents, whose stances nobody has shown to be right; our own analysis
outputs; and ``poll`` nodes, which join everything seen in one read and would make every node a neighbour of
every other. :data:`SIGNAL_SCOPE` also removes the ``disaster`` domain and every event filed under it, by the
user's decision (2026-11-01): the desk does not seek to profit on disasters.

These are findings, not signals. Nothing on the trading path imports this module. Needs the ``graph`` extra.
"""
from __future__ import annotations

import re
import sqlite3
from collections import defaultdict
from collections.abc import Callable, Hashable, Iterable, Iterator, Mapping, Sequence
from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta
from itertools import groupby, pairwise
from typing import Any, Literal

import networkx as nx

from trading_live_claude.analysis.graph_adapter import ENDOGENOUS_NODES, ENDOGENOUS_PREDICATES
from trading_live_claude.intel.graph import Edge, NodeType, Predicate
from trading_live_claude.intel.graph_store import CachedGraphView, GraphView, stamp_to_us

Node = tuple[NodeType, str]
EdgeSpec = tuple[Predicate, Node]
Direction = Literal["out", "in", "both"]

SIMULATED_PREDICATES = frozenset({"holds_stance", "member_of", "follows", "engaged", "attends", "seeded"})
SIMULATED_NODES = frozenset({"society", "agent"})
ALL_EVENTS = 10**9


@dataclass(frozen=True)
class SearchScope:
    """Which edges a search may read. An edge is dropped if any rule below excludes it."""

    exclude_predicates: frozenset[str] = ENDOGENOUS_PREDICATES | SIMULATED_PREDICATES | frozenset({"ranked_by"})
    exclude_node_types: frozenset[str] = ENDOGENOUS_NODES | SIMULATED_NODES | frozenset({"poll", "analysis"})
    exclude_nodes: frozenset[Node] = frozenset()
    exclude_event_domains: frozenset[str] = frozenset()   # an event filed under one of these goes with its edges
    only_predicates: frozenset[str] | None = None         # None = every predicate not excluded


EVIDENCE_SCOPE = SearchScope()
SIGNAL_SCOPE = replace(EVIDENCE_SCOPE, exclude_nodes=frozenset({("domain", "disaster")}),
                       exclude_event_domains=frozenset({"disaster"}))


@dataclass(frozen=True)
class Hop:
    """One step of a path, with the newest edge that supports it, so a finding can cite its evidence."""

    source: Node
    target: Node
    predicate: str
    as_of: str


@dataclass(frozen=True)
class GraphPath:
    nodes: tuple[Node, ...]
    hops: tuple[Hop, ...]


@dataclass(frozen=True)
class TemporalHit:
    node: Node
    arrived_as_of: str
    via: Node
    predicate: str


@dataclass(frozen=True)
class EventHit:
    event_id: str
    score: float
    title: str = ""
    fields: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class Thickening:
    """Edge counts in the latest window and the one before. ``rate`` is ``None`` with no prior edges."""

    recent: int
    prior: int
    rate: float | None


@dataclass(frozen=True)
class Trending:
    node: Node
    recent: int
    baseline: int
    acceleration: float


# ---- reading a view ----------------------------------------------------------


def _dropped_events(edges: Iterable[Edge], scope: SearchScope) -> set[Node]:
    if not scope.exclude_event_domains:
        return set()
    return {e.subject for e in edges if e.predicate == "about_domain" and e.subject[0] == "event"
            and e.object[1] in scope.exclude_event_domains}


def _scoped(edges: Sequence[Edge], scope: SearchScope) -> list[Edge]:
    dropped = _dropped_events(edges, scope)
    kept: list[Edge] = []
    for e in edges:
        if e.predicate in scope.exclude_predicates:
            continue
        if scope.only_predicates is not None and e.predicate not in scope.only_predicates:
            continue
        if e.subject[0] in scope.exclude_node_types or e.object[0] in scope.exclude_node_types:
            continue
        if e.subject in scope.exclude_nodes or e.object in scope.exclude_nodes:
            continue
        if e.subject in dropped or e.object in dropped:
            continue
        kept.append(e)
    return kept


def build_graph(view: GraphView, scope: SearchScope = EVIDENCE_SCOPE, *, decay: bool = True) -> nx.MultiDiGraph:
    """The scoped edges as a directed multigraph. ``decay`` applies decay and TTL as of the cutoff.

    A cached view builds this once and returns the same object each time, so do not change what you get.
    """
    if isinstance(view, CachedGraphView):
        return view.remember(("graph", scope, decay), lambda: _built(view, scope, decay))
    return _built(view, scope, decay)


def _built(view: GraphView, scope: SearchScope, decay: bool) -> nx.MultiDiGraph:
    graph = nx.MultiDiGraph()
    for e in _scoped(view.washed() if decay else view.edges(), scope):
        graph.add_edge(e.subject, e.object, predicate=e.predicate, weight=abs(float(e.weight)), as_of=e.as_of)
    return graph


def _undirected(graph: nx.MultiDiGraph) -> nx.Graph:
    """Parallel edges summed, in sorted order so that results do not depend on journal order."""
    totals: dict[tuple[Node, Node], float] = defaultdict(float)
    for a, b, data in graph.edges(data=True):
        pair = (a, b) if a <= b else (b, a)
        totals[pair] += float(data["weight"])
    simple = nx.Graph()
    simple.add_nodes_from(sorted(graph.nodes))
    for (a, b), weight in sorted(totals.items()):
        simple.add_edge(a, b, weight=weight)
    return simple


def _simple(view: GraphView, scope: SearchScope) -> nx.Graph:
    if isinstance(view, CachedGraphView):
        return view.remember(("simple", scope), lambda: _undirected(build_graph(view, scope)))
    return _undirected(build_graph(view, scope))


def _hop(graph: nx.MultiDiGraph, a: Node, b: Node) -> Hop:
    found = [(a, b, d) for d in (graph.get_edge_data(a, b) or {}).values()]
    found += [(b, a, d) for d in (graph.get_edge_data(b, a) or {}).values()]
    src, dst, data = max(found, key=lambda f: (stamp_to_us(f[2]["as_of"]) or 0, f[2]["predicate"]))
    return Hop(source=src, target=dst, predicate=data["predicate"], as_of=data["as_of"])


def _to_path(graph: nx.MultiDiGraph, nodes: Sequence[Node]) -> GraphPath:
    return GraphPath(nodes=tuple(nodes), hops=tuple(_hop(graph, a, b) for a, b in pairwise(nodes)))


# ---- traversal ---------------------------------------------------------------


def neighbors(view: GraphView, node: Node, hops: int = 1, *, direction: Direction = "both",
              scope: SearchScope = EVIDENCE_SCOPE) -> dict[Node, int]:
    """Nodes within ``hops`` of ``node``, mapped to their distance, nearest first."""
    graph = build_graph(view, scope)
    if node not in graph:
        return {}
    walk: nx.Graph = {"out": graph, "in": graph.reverse(copy=False), "both": graph.to_undirected(as_view=True)}[direction]
    found = nx.single_source_shortest_path_length(walk, node, cutoff=hops)
    return {n: d for n, d in sorted(found.items(), key=lambda kv: (kv[1], kv[0])) if n != node}


def bfs_paths(view: GraphView, source: Node, target: Node, *, max_hops: int = 3, limit: int = 50,
              scope: SearchScope = EVIDENCE_SCOPE) -> list[GraphPath]:
    """Simple paths from ``source`` to ``target``, ignoring edge direction, shortest first."""
    graph = build_graph(view, scope)
    if source not in graph or target not in graph or source == target:
        return []
    simple = _simple(view, scope)
    if not nx.has_path(simple, source, target):
        return []
    out: list[GraphPath] = []
    for nodes in nx.shortest_simple_paths(simple, source, target):
        if len(nodes) - 1 > max_hops or len(out) >= limit:
            break
        out.append(_to_path(graph, nodes))
    return out


def shortest_path(view: GraphView, source: Node, target: Node, *,
                  scope: SearchScope = EVIDENCE_SCOPE) -> GraphPath | None:
    """The fewest-hop path, ignoring direction, or ``None`` when the two are not connected."""
    graph = build_graph(view, scope)
    if source not in graph or target not in graph:
        return None
    try:
        return _to_path(graph, nx.shortest_path(_simple(view, scope), source, target))
    except nx.NetworkXNoPath:
        return None


def temporal_walk(view: GraphView, start: Node, *, since: datetime | None = None,
                  scope: SearchScope = EVIDENCE_SCOPE) -> list[TemporalHit]:
    """What ``start`` could have reached by following edges forward in time, earliest arrival first.

    An ordinary path can run backwards through time; this one cannot, so it separates "this could have led to
    that" from "these are merely connected". Edges with the same stamp count as simultaneous. Reads undecayed
    edges: arrival is about when something was seen, not how much it still weighs.
    """
    edges = _scoped(view.edges(since=since), scope)
    reached: set[Node] = {start}
    hits: list[TemporalHit] = []
    timed = [(stamp_to_us(e.as_of), e) for e in edges]
    for _, group in groupby(timed, key=lambda t: t[0]):
        batch = [e for _, e in group]
        changed = True
        while changed:                             # simultaneous edges may chain in any journal order
            changed = False
            for e in batch:
                for a, b in ((e.subject, e.object), (e.object, e.subject)):
                    if a in reached and b not in reached:
                        reached.add(b)
                        hits.append(TemporalHit(node=b, arrived_as_of=e.as_of, via=a, predicate=e.predicate))
                        changed = True
    return hits


# ---- ranking -----------------------------------------------------------------


def _ranked(scores: Mapping[Node, float], top: int | None) -> list[tuple[Node, float]]:
    ordered = sorted(scores.items(), key=lambda kv: (-kv[1], kv[0]))
    return ordered if top is None else ordered[:top]


def personalized_pagerank(view: GraphView, seeds: Iterable[Node] | Mapping[Node, float], *, alpha: float = 0.85,
                          top: int | None = 50, include_seeds: bool = False,
                          scope: SearchScope = EVIDENCE_SCOPE) -> list[tuple[Node, float]]:
    """Nodes ranked by how much a random walk from ``seeds`` visits them. Seeds absent from the graph are ignored.

    Solved only on what the seeds can reach: a walk cannot get anywhere else, and leaving it in would hand
    unconnected nodes a small non-zero score from the solver's starting vector.
    """
    simple = _simple(view, scope)
    weights = dict(seeds) if isinstance(seeds, Mapping) else dict.fromkeys(seeds, 1.0)
    present = {n: float(w) for n, w in weights.items() if n in simple and w > 0}
    if not present:
        return []
    reachable = set(present).union(*(nx.node_connected_component(simple, n) for n in present))
    scores = nx.pagerank(simple.subgraph(sorted(reachable)), alpha=alpha, personalization=present, weight="weight")
    if not include_seeds:
        scores = {n: s for n, s in scores.items() if n not in present}
    return _ranked(scores, top)


def centrality(view: GraphView, kind: Literal["degree", "betweenness", "pagerank"] = "degree", *,
               node_type: NodeType | None = None, top: int | None = 50,
               scope: SearchScope = EVIDENCE_SCOPE) -> list[tuple[Node, float]]:
    """How central each node is. ``node_type`` filters the result, not the graph it is computed on."""
    simple = _simple(view, scope)
    if simple.number_of_nodes() == 0:
        return []
    if kind == "degree":
        scores = nx.degree_centrality(simple)
    elif kind == "betweenness":
        scores = nx.betweenness_centrality(simple, weight=None)
    elif kind == "pagerank":
        scores = nx.pagerank(simple, weight="weight")
    else:
        raise ValueError(f"unknown centrality {kind!r}")
    if node_type is not None:
        scores = {n: s for n, s in scores.items() if n[0] == node_type}
    return _ranked(scores, top)


def communities(view: GraphView, *, seed: int = 0, resolution: float = 1.0,
                scope: SearchScope = EVIDENCE_SCOPE) -> list[list[Node]]:
    """Louvain clusters, largest first. The seed is fixed, so the same view gives the same answer."""
    simple = _simple(view, scope)
    if simple.number_of_edges() == 0:
        return []
    found = nx.community.louvain_communities(simple, weight="weight", resolution=resolution, seed=seed)
    return sorted((sorted(c) for c in found), key=lambda c: (-len(c), c))


def neighbor_similarity(view: GraphView, scope: SearchScope = EVIDENCE_SCOPE) -> Callable[[Node, Node], float]:
    """A similarity for :func:`mmr_diversify`: the Jaccard overlap of two nodes' neighbours."""
    simple = _simple(view, scope)
    around = {n: set(simple[n]) for n in simple}

    def similarity(a: Node, b: Node) -> float:
        na, nb = around.get(a, set()), around.get(b, set())
        union = na | nb
        return len(na & nb) / len(union) if union else 0.0
    return similarity


# ---- text --------------------------------------------------------------------


def _joined(values: Any) -> str:
    """Sorted, so a document does not depend on the order its edges reached the journal."""
    return " ".join(sorted(map(str, values or [])))


def _documents(view: GraphView, scope: SearchScope,
               titles: Mapping[str, str] | None) -> Iterator[tuple[str, str, str, str, str, str]]:
    all_edges = view.edges()
    dropped = {n[1] for n in _dropped_events(all_edges, scope)}
    symbols: dict[str, set[str]] = defaultdict(set)
    for e in all_edges:
        if e.predicate == "mentions" and e.subject[0] == "event":
            symbols[e.subject[1]].add(e.object[1])
    for rec in view.recent_events(limit=ALL_EVENTS):
        event_id = str(rec["id"])
        if event_id in dropped:
            continue
        title = str(rec.get("title") or (titles or {}).get(event_id, ""))
        yield (event_id, title, _joined(rec.get("sources")), _joined(rec.get("categories")),
               _joined(rec.get("regions")), " ".join(sorted(symbols.get(event_id, ()))))


def bm25_search(view: GraphView, query: str, *, top: int = 20, scope: SearchScope = EVIDENCE_SCOPE,
                titles: Mapping[str, str] | None = None) -> list[EventHit]:
    """Events matching any query word, best first, with titles counting most.

    ``titles`` supplies headlines the graph does not hold, by event id. The caller answers for them being known by
    the cutoff: :func:`~trading_live_claude.intel.retrieval.news_titles` is the one that checks.

    The index lives only for this call and holds only what the view can see, so term statistics are point-in-time
    too. It costs a rebuild per search; the guarantee is worth more than the speed until the graph says otherwise.
    Words are quoted, so punctuation in a query cannot be read as FTS5 syntax.
    """
    terms = re.findall(r"\w+", query.lower())
    if not terms:
        return []
    conn = sqlite3.connect(":memory:")
    try:
        conn.execute("CREATE VIRTUAL TABLE docs USING fts5(event_id UNINDEXED, title, sources, categories, regions, symbols)")
        conn.executemany("INSERT INTO docs VALUES (?, ?, ?, ?, ?, ?)", _documents(view, scope, titles))
        rows = conn.execute(
            "SELECT event_id, title, sources, categories, regions, symbols, bm25(docs, 0, 5.0, 1.0, 2.0, 1.0, 3.0) AS rank "
            "FROM docs WHERE docs MATCH ? ORDER BY rank, event_id LIMIT ?",
            (" OR ".join(f'"{t}"' for t in terms), int(top))).fetchall()
    finally:
        conn.close()
    return [EventHit(event_id=r[0], score=-float(r[6]), title=r[1],
                     fields={"sources": r[2], "categories": r[3], "regions": r[4], "symbols": r[5]}) for r in rows]


# ---- time --------------------------------------------------------------------


def _poll_groups(edges: Iterable[Edge]) -> list[list[Edge]]:
    groups: dict[str, list[Edge]] = {}
    for e in edges:
        pid = e.subject[1] if e.subject[0] == "poll" else e.object[1] if e.object[0] == "poll" else e.as_of
        if pid:
            groups.setdefault(pid, []).append(e)
    return list(groups.values())


def co_persistence(view: GraphView, first: EdgeSpec, second: EdgeSpec, *, max_age_hours: float | None = None) -> int:
    """How many consecutive polls, ending at the latest, hold both edges. Reads polls, so it has no scope.

    A fill or headline is no poll and cannot break a run, the same rule as ``edge_persistence``. A group holding
    only one of the two edges, or a poll holding neither, ends it.
    """
    specs = {first, second}
    run = 0
    newest: Edge | None = None
    for group in reversed(_poll_groups(view.edges())):
        present = {(e.predicate, e.object) for e in group}
        if specs <= present:
            newest = newest or next(e for e in group if (e.predicate, e.object) in specs)
            run += 1
        elif (present & specs) or any(e.subject[0] == "poll" or e.object[0] == "poll" for e in group):
            break
    if run and max_age_hours is not None and newest is not None:
        at = stamp_to_us(newest.as_of)
        limit = stamp_to_us((view.cutoff - timedelta(hours=max_age_hours)).isoformat())
        if at is None or limit is None or at < limit:
            return 0
    return run


def first_seen(view: GraphView, *, subject: Node | None = None, predicate: Predicate | None = None,
               object: Node | None = None) -> str | None:
    """The ``as_of`` of the earliest matching edge, or ``None`` if the view holds none."""
    found = view.edges(subject=subject, predicate=predicate, object=object)
    return found[0].as_of if found else None


def thickening_rate(view: GraphView, predicate: Predicate, object: Node, *,
                    window: timedelta = timedelta(hours=24)) -> Thickening:
    """Is this edge appearing faster? Counts in the last ``window`` against the one before it."""
    start = view.cutoff - window
    found = view.edges(predicate=predicate, object=object, since=start - window)
    limit = stamp_to_us(start.isoformat())
    recent = sum(1 for e in found if (stamp_to_us(e.as_of) or 0) >= (limit or 0))
    prior = len(found) - recent
    return Thickening(recent=recent, prior=prior, rate=recent / prior if prior else None)


def trending_nodes(view: GraphView, *, node_type: NodeType | None = None, window: timedelta = timedelta(hours=24),
                   baseline: timedelta = timedelta(days=7), min_baseline: int = 5, max_acceleration: float = 10.0,
                   top: int = 20, scope: SearchScope = EVIDENCE_SCOPE) -> list[Trending]:
    """Nodes whose edges arrive faster than their own recent norm, fastest first.

    Same guards as ``intel/events.py``: below ``min_baseline`` edges a baseline is not one, so acceleration reads a
    neutral 1.0 rather than a fabricated spike, and the ratio is capped because it is heavy-tailed.
    """
    start = view.cutoff - window
    limit = stamp_to_us(start.isoformat()) or 0
    recent: dict[Node, int] = defaultdict(int)
    prior: dict[Node, int] = defaultdict(int)
    for e in _scoped(view.edges(since=start - baseline), scope):
        counts = recent if (stamp_to_us(e.as_of) or 0) >= limit else prior
        for node in (e.subject, e.object):
            if node_type is None or node[0] == node_type:
                counts[node] += 1
    periods = baseline / window
    out = []
    for node, count in recent.items():
        base = prior.get(node, 0)
        accel = 1.0 if base < max(min_baseline, 1) else min(count / (base / periods), max_acceleration)
        out.append(Trending(node=node, recent=count, baseline=base, acceleration=accel))
    return sorted(out, key=lambda t: (-t.acceleration, -t.recent, t.node))[:top]


# ---- fusion: these combine results and read nothing ------------------------------


def reciprocal_rank_fusion[T: Hashable](rankings: Sequence[Sequence[T]], *, k: int = 60,
                                        top: int | None = None) -> list[tuple[T, float]]:
    """Merge ranked lists by ``sum(1 / (k + rank))``. Needs no comparable scores, only an order."""
    scores: dict[T, float] = defaultdict(float)
    for ranking in rankings:
        for rank, item in enumerate(ranking, start=1):
            scores[item] += 1.0 / (k + rank)
    ordered = sorted(scores.items(), key=lambda kv: (-kv[1], str(kv[0])))
    return ordered if top is None else ordered[:top]


def mmr_diversify[T: Hashable](items: Sequence[T], relevance: Mapping[T, float], similarity: Callable[[T, T], float],
                               *, lambda_: float = 0.7, top: int = 10) -> list[T]:
    """Maximal marginal relevance: each pick balances its relevance against resembling what is already picked."""
    if not 0.0 <= lambda_ <= 1.0:
        raise ValueError("lambda_ must be in [0, 1]")
    remaining = sorted(items, key=str)
    picked: list[T] = []
    while remaining and len(picked) < top:
        def value(item: T) -> float:
            penalty = max((similarity(item, p) for p in picked), default=0.0)
            return lambda_ * relevance.get(item, 0.0) - (1.0 - lambda_) * penalty
        best = max(remaining, key=value)           # max keeps the first of equals, and the list is sorted
        picked.append(best)
        remaining.remove(best)
    return picked
