"""Evidence for a target: found by search, cited by path, and fixed at a moment.

:func:`retrieve_evidence` turns a target (a symbol, a domain or free text) into an :class:`EvidenceBundle`:
graph-ranked and text-ranked events, fused and diversified, each carrying the path that links it to the target.

Everything reads through one :class:`~trading_live_claude.intel.graph_store.GraphView`, so a bundle cannot see
past its cutoff. ``state/news.jsonl`` is the one source outside the store,
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
from typing import Any

from trading_live_claude.analysis.pit import parse_ts, read_jsonl
from trading_live_claude.config.settings import get_settings
from trading_live_claude.intel.graph_search import (
    SIGNAL_SCOPE,
    GraphPath,
    Node,
    SearchScope,
    bm25_search,
    co_persistence,
    mmr_diversify,
    neighbor_similarity,
    personalized_pagerank,
    reciprocal_rank_fusion,
    shortest_path,
    trending_nodes,
)
from trading_live_claude.intel.graph_store import GraphView, stamp_to_us
from trading_live_claude.intel.news_sentiment import event_sentiment
from trading_live_claude.logging_setup import get_logger

log = get_logger(__name__)

Target = Node | str
WATCHED_DOMAINS = ("conflict", "military", "energy", "economy")
MAX_RUN_AGE_HOURS = 6.0     # the persistence gate's own staleness limit
ALL_EVENTS = 10**9


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
    sentiment: dict[str, dict[str, float]] = field(default_factory=dict)  # symbol -> an LLM's score, a label only

    def record(self) -> dict[str, Any]:
        """The shape agents read: free text in ``title``, and how the event connects in ``path``."""
        rec: dict[str, Any] = {"id": self.event_id, "title": self.title, "sources": list(self.sources),
                               "categories": list(self.categories), "regions": list(self.regions),
                               "symbols": list(self.symbols), "ingestedAt": self.as_of}
        if self.sentiment:
            rec["sentiment"] = self.sentiment
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
                       "items": [[i.event_id, i.title, i.sources, i.categories, i.regions, i.symbols, i.as_of, i.sentiment]
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
    scores = event_sentiment(v)
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
            path=shortest_path(v, origin, node, scope=scope) if origin is not None and origin != node else None,
            sentiment=scores.get(event_id, {})))
    runs, together = _domain_state(v, scope)
    trending = tuple((t.node[1], round(t.acceleration, 3)) for t in trending_nodes(v, node_type="symbol", top=5, scope=scope)
                     if t.acceleration > 1.0)
    return EvidenceBundle(target=target, cutoff=view.cutoff, items=tuple(items), domain_runs=runs, co_runs=together,
                          trending=trending, content_hash=_hash(target, view, items, runs))


