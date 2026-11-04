"""Evidence for a debate: found by search, cited by path, and fixed at a moment.

:func:`retrieve_evidence` turns a target (a symbol, a domain or free text) into an :class:`EvidenceBundle`:
graph-ranked and text-ranked events, fused and diversified, each carrying the path that links it to the target.
:func:`debate_on_view` runs the agent debate on that bundle, with search tools the agents may use, the desk's
earlier claims as context, and every attempt recorded in the debate journal (``state/intel_agents.jsonl``).

Everything reads through one :class:`~trading_live_claude.intel.graph_store.GraphView`, so a debate cannot see
past its cutoff, and the tools are bound to that view and scope before the model is involved: they take no time
or scope argument, so an agent cannot ask for more. ``state/news.jsonl`` is the one source outside the store,
and :func:`news_titles` applies the same strict cutoff to it. The default scope is
:data:`~trading_live_claude.intel.graph_search.SIGNAL_SCOPE`, which keeps ``disaster`` out.

These are hypotheses and nothing here posts a signal. Nothing on the trading path imports this module.
"""
from __future__ import annotations

import hashlib
import json
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import datetime
from itertools import combinations
from pathlib import Path
from typing import Any, get_args

import httpx

from trading_live_claude.analysis.pit import parse_ts, read_jsonl
from trading_live_claude.config.settings import get_settings
from trading_live_claude.intel.agents import DebateReport, Domain, run_debate
from trading_live_claude.intel.graph_search import (
    SIGNAL_SCOPE,
    GraphPath,
    Node,
    SearchScope,
    bm25_search,
    build_graph,
    co_persistence,
    mmr_diversify,
    neighbor_similarity,
    neighbors,
    personalized_pagerank,
    reciprocal_rank_fusion,
    shortest_path,
    trending_nodes,
)
from trading_live_claude.intel.graph_store import GraphView, stamp_to_us
from trading_live_claude.logging_setup import get_logger

log = get_logger(__name__)

Target = Node | str
WATCHED_DOMAINS = ("conflict", "military", "energy", "economy")
MAX_RUN_AGE_HOURS = 6.0     # the persistence gate's own staleness limit
ALL_EVENTS = 10**9
MAX_TOOL_RESULTS = 15
TOOL_NODE_TYPES = ("symbol", "domain", "source", "region", "event", "market")


def news_titles(view: GraphView, path: str | Path | None = None) -> dict[str, str]:
    """Headlines for ``mentions`` events, which the graph holds only as ``news:<source>:<hash>``.

    Only rows that arrived strictly before the view's cutoff, so this join cannot leak what the graph would not.
    A row with no readable arrival time is left out.
    """
    journal = Path(path) if path is not None else Path(get_settings().state_dir) / "news.jsonl"
    bound = stamp_to_us(view.cutoff.isoformat())
    titles: dict[str, str] = {}
    for row in read_jsonl(journal):
        arrived = parse_ts(str(row.get("arrived_utc") or ""))
        if arrived is None or bound is None or (stamp_to_us(arrived.isoformat()) or bound) >= bound:
            continue
        if row.get("source") and row.get("content_hash") and row.get("title"):
            titles[f"news:{row['source']}:{row['content_hash']}"] = str(row["title"])
    return titles


@dataclass(frozen=True)
class EvidenceItem:
    event_id: str
    title: str
    sources: tuple[str, ...]
    categories: tuple[str, ...]
    regions: tuple[str, ...]
    symbols: tuple[str, ...]
    as_of: str
    found_by: tuple[str, ...]        # which searches ranked it: graph, text
    path: GraphPath | None = None    # how it connects to the target, for citing

    def record(self) -> dict[str, Any]:
        """The shape agents read: free text in ``title``, and how the event connects in ``path``."""
        rec: dict[str, Any] = {"id": self.event_id, "title": self.title, "sources": list(self.sources),
                               "categories": list(self.categories), "regions": list(self.regions),
                               "symbols": list(self.symbols), "ingestedAt": self.as_of}
        if self.path is not None:
            rec["path"] = [f"{h.source[1]} -{h.predicate}-> {h.target[1]}" for h in self.path.hops]
        return rec


@dataclass(frozen=True)
class EvidenceBundle:
    """What was found for one target at one moment. ``content_hash`` identifies it in the debate journal."""

    target: Target
    cutoff: datetime
    items: tuple[EvidenceItem, ...]
    domain_runs: dict[str, int] = field(default_factory=dict)
    co_runs: dict[str, int] = field(default_factory=dict)
    trending: tuple[tuple[str, float], ...] = ()
    content_hash: str = ""

    def records(self) -> list[dict[str, Any]]:
        return [i.record() for i in self.items]

    def context(self) -> dict[str, Any]:
        out: dict[str, Any] = {}
        if self.domain_runs:
            out["consecutive_polls_elevated_by_domain"] = self.domain_runs
        if self.co_runs:
            out["consecutive_polls_elevated_together"] = self.co_runs
        if self.trending:
            out["trending_symbols_acceleration"] = dict(self.trending)
        return out


def _domain_state(view: GraphView, scope: SearchScope) -> tuple[dict[str, int], dict[str, int]]:
    """``elevated_in`` hangs off poll nodes, so it is not in the evidence graph. Read it as run lengths instead."""
    domains = [d for d in WATCHED_DOMAINS if ("domain", d) not in scope.exclude_nodes]
    runs = {d: view.persistence("elevated_in", ("domain", d), max_age_hours=MAX_RUN_AGE_HOURS) for d in domains}
    elevated = sorted(d for d, run in runs.items() if run > 0)
    together = {f"{a}+{b}": co_persistence(view, ("elevated_in", ("domain", a)), ("elevated_in", ("domain", b)),
                                           max_age_hours=MAX_RUN_AGE_HOURS)
                for a, b in combinations(elevated, 2)}
    return {d: r for d, r in runs.items() if r > 0}, {k: r for k, r in together.items() if r > 0}


def _hash(target: Target, view: GraphView, items: Sequence[EvidenceItem], runs: dict[str, int]) -> str:
    body = json.dumps({"target": target, "cutoff": view.cutoff.isoformat(), "runs": runs,
                       "items": [[i.event_id, i.title, i.sources, i.categories, i.regions, i.symbols, i.as_of]
                                 for i in items]}, sort_keys=True, default=str)
    return hashlib.sha256(body.encode("utf-8")).hexdigest()[:16]


def retrieve_evidence(view: GraphView, target: Target, *, budget: int = 12, scope: SearchScope = SIGNAL_SCOPE,
                      news_path: str | Path | None = None) -> EvidenceBundle:
    """Up to ``budget`` events for ``target``, as they stood at the view's cutoff.

    Graph rank comes from a personalized PageRank from the target, or for free text from its best text matches.
    Text rank comes from BM25 on the target's words. Both are fused by rank, then diversified so one story told
    five ways does not fill the bundle. A target nothing connects to gets an empty bundle, not general news.
    """
    v = view.cached()
    titles = news_titles(v, news_path)
    query = target if isinstance(target, str) else target[1]
    text_hits = [h.event_id for h in bm25_search(v, query, top=max(budget * 3, 20), scope=scope, titles=titles)]
    if isinstance(target, str):
        origin: Node | None = ("event", text_hits[0]) if text_hits else None
        seeds: list[Node] = [("event", e) for e in text_hits[:5]]
    else:
        origin = target
        seeds = [target]
    graph_ranked = personalized_pagerank(v, seeds, top=None, include_seeds=isinstance(target, str), scope=scope)
    graph_hits = [n[1] for n, _ in graph_ranked if n[0] == "event"]
    fused = reciprocal_rank_fusion([graph_hits, text_hits])
    records = {str(r["id"]): r for r in v.recent_events(limit=ALL_EVENTS)}
    candidates = [e for e, _ in fused if e in records]
    relevance = {("event", e): score for e, score in fused}
    similarity = neighbor_similarity(v, scope)
    chosen = mmr_diversify([("event", e) for e in candidates], relevance, similarity, top=budget)
    mentions: dict[str, set[str]] = {}
    for e in v.edges(predicate="mentions"):
        mentions.setdefault(e.subject[1], set()).add(e.object[1])
    items = []
    for node in chosen:
        event_id = node[1]
        rec = records[event_id]
        found_by = tuple(name for name, hits in (("graph", graph_hits), ("text", text_hits)) if event_id in hits)
        items.append(EvidenceItem(
            event_id=event_id, title=str(rec.get("title") or titles.get(event_id, "")),
            sources=tuple(sorted(map(str, rec.get("sources") or []))),  # type: ignore[call-overload]
            categories=tuple(sorted(map(str, rec.get("categories") or []))),  # type: ignore[call-overload]
            regions=tuple(sorted(map(str, rec.get("regions") or []))),  # type: ignore[call-overload]
            symbols=tuple(sorted(mentions.get(event_id, ()))),
            as_of=str(rec.get("ingestedAt") or ""), found_by=found_by,
            path=shortest_path(v, origin, node, scope=scope) if origin is not None and origin != node else None))
    runs, together = _domain_state(v, scope)
    trending = tuple((t.node[1], round(t.acceleration, 3)) for t in trending_nodes(v, node_type="symbol", top=5, scope=scope)
                     if t.acceleration > 1.0)
    return EvidenceBundle(target=target, cutoff=view.cutoff, items=tuple(items), domain_runs=runs, co_runs=together,
                          trending=trending, content_hash=_hash(target, view, items, runs))


class ViewTools:
    """Search for an agent, bound to one view, one scope and a call budget.

    No tool takes a time or a scope, and unexpected arguments are ignored, so nothing a model sends can widen what
    it sees. Results are untrusted text. :meth:`call` never raises: a bad call comes back as an error the model can
    read, and a spent budget tells it to answer from what it has.
    """

    def __init__(self, view: GraphView, scope: SearchScope = SIGNAL_SCOPE, *, titles: dict[str, str] | None = None,
                 max_calls: int = 6) -> None:
        self._view = view.cached()
        self._scope = scope
        self._titles = titles or {}
        self._left = int(max_calls)

    def definitions(self) -> list[dict[str, Any]]:
        node_type = {"type": "string", "enum": list(TOOL_NODE_TYPES)}
        node_id = {"type": "string", "description": "the node's id, for example XLE, energy or reuters"}

        def tool(name: str, description: str, properties: dict[str, Any], required: list[str]) -> dict[str, Any]:
            return {"name": name, "description": description,
                    "input_schema": {"type": "object", "properties": properties, "required": required,
                                     "additionalProperties": False}}
        return [
            tool("search_text", "Find events by words in their headline, sources, categories, regions or symbols. "
                                "Returns event ids you can cite.", {"query": {"type": "string"}}, ["query"]),
            tool("neighbors", "List what is connected to a node, up to two hops away.",
                 {"node_type": node_type, "node_id": node_id, "hops": {"type": "integer", "minimum": 1, "maximum": 2}},
                 ["node_type", "node_id"]),
            tool("find_path", "Show how two nodes are connected, with the edge behind each step.",
                 {"from_type": node_type, "from_id": node_id, "to_type": node_type, "to_id": node_id},
                 ["from_type", "from_id", "to_type", "to_id"]),
        ]

    def call(self, name: str, arguments: dict[str, Any]) -> tuple[str, bool]:
        if self._left <= 0:
            return "Search budget spent. Answer from what you have.", True
        self._left -= 1
        try:
            return json.dumps(self._run(name, arguments), default=str), False
        except (KeyError, TypeError, ValueError) as e:
            return f"Bad call to {name}: {e}", True

    def _run(self, name: str, a: dict[str, Any]) -> Any:
        if name == "search_text":
            hits = bm25_search(self._view, str(a["query"]), top=MAX_TOOL_RESULTS, scope=self._scope, titles=self._titles)
            return [{"id": h.event_id, "title": h.title, **h.fields} for h in hits]
        if name == "neighbors":
            hops = max(1, min(2, int(a.get("hops", 1))))
            found = neighbors(self._view, (a["node_type"], str(a["node_id"])), hops, scope=self._scope)
            return [{"type": n[0], "id": n[1], "hops": d} for n, d in list(found.items())[:MAX_TOOL_RESULTS]]
        if name == "find_path":
            path = shortest_path(self._view, (a["from_type"], str(a["from_id"])), (a["to_type"], str(a["to_id"])),
                                 scope=self._scope)
            if path is None:
                return {"connected": False}
            return {"connected": True, "steps": [{"from": h.source[1], "edge": h.predicate, "to": h.target[1],
                                                  "as_of": h.as_of} for h in path.hops]}
        raise ValueError(f"no tool called {name}")


class ThesisMemory:
    """The desk's earlier claims, from the debate journal, as known before a cutoff.

    A journal row counts by its ``as_of``, the moment its debate read the graph. Rows without one are skipped, since
    nothing can place them in time.
    """

    def __init__(self, path: str | Path | None = None) -> None:
        self.path = Path(path) if path is not None else Path(get_settings().state_dir) / "intel_agents.jsonl"

    def before(self, cutoff: datetime, *, limit: int = 8, domains: Sequence[str] | None = None) -> list[dict[str, Any]]:
        """Claims made before ``cutoff``, newest first."""
        bound = stamp_to_us(cutoff.isoformat())
        rows: list[tuple[int, dict[str, Any]]] = []
        for row in read_jsonl(self.path):
            at = stamp_to_us(str(row.get("as_of") or ""))
            if at is None or bound is None or at >= bound:
                continue
            for domain, outcome in (row.get("outcomes") or {}).items():
                claim = outcome.get("claim") if isinstance(outcome, dict) else None
                if not claim or (domains is not None and domain not in domains):
                    continue
                critique = outcome.get("critique") or {}
                rows.append((at, {"as_of": row["as_of"], "domain": domain, "thesis": claim.get("thesis"),
                                  "direction": claim.get("direction"), "confidence": claim.get("confidence"),
                                  "outcome": outcome.get("outcome"), "verdict": critique.get("verdict"),
                                  "reason": critique.get("reason")}))
        rows.sort(key=lambda r: r[0], reverse=True)
        return [r for _, r in rows[:limit]]


@dataclass(frozen=True)
class CitedEvidence:
    event_id: str
    path: GraphPath | None


@dataclass(frozen=True)
class ViewDebate:
    """A debate on one view: the report, the bundle it read, and each fired thesis's citations by domain."""

    report: DebateReport
    bundle: EvidenceBundle
    citations: dict[str, list[CitedEvidence]] = field(default_factory=dict)


def debate_on_view(view: GraphView, target: Target, *, domains: Sequence[Domain] | None = None, budget: int = 12,
                   scope: SearchScope = SIGNAL_SCOPE, client: httpx.Client | None = None, journal: bool = True,
                   journal_path: str | Path | None = None, news_path: str | Path | None = None,
                   max_tool_calls: int = 6) -> ViewDebate:
    """Retrieve evidence for ``target`` and debate it, all as of the view's cutoff.

    A specialist whose whole domain the scope excludes does not run, so the default scope skips ``disaster``. A
    claim keeps only citations that are events visible in this view and scope, and with none it is dropped.
    """
    v = view.cached()
    bundle = retrieve_evidence(v, target, budget=budget, scope=scope, news_path=news_path)
    excluded = {n[1] for n in scope.exclude_nodes if n[0] == "domain"}
    chosen = [d for d in (domains if domains is not None else get_args(Domain)) if d not in excluded]
    path = Path(journal_path) if journal_path is not None else None
    context = bundle.context()
    earlier = ThesisMemory(path).before(view.cutoff, domains=chosen)
    if earlier:
        context["earlier_claims"] = earlier
    titles = news_titles(v, news_path)
    graph = build_graph(v, scope)
    report = run_debate(
        bundle.records(), as_of=view.cutoff.isoformat(), domains=chosen, client=client, journal=journal,
        journal_path=path, tools=lambda: ViewTools(v, scope, titles=titles, max_calls=max_tool_calls),
        context=context or None, citation_check=lambda event_id: ("event", event_id) in graph,
        extra={"target": target, "evidence_hash": bundle.content_hash, "evidence_ids": [i.event_id for i in bundle.items]})
    origin = target if not isinstance(target, str) else None
    citations = {t.domain: [CitedEvidence(c, shortest_path(v, origin, ("event", c), scope=scope) if origin else None)
                            for c in t.evidence] for t in report.fired}
    return ViewDebate(report=report, bundle=bundle, citations=citations)
