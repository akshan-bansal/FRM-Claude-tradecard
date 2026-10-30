"""Ingest an OASIS run: a durable run record, graph edges, and the ledger settled at what it spent.

* ``state/oasis_runs.jsonl`` (durable, never washed): one row per run with the per-symbol aggregate the
  society reported: mean stance, dispersion (sample sd across agents) and how many agents held a view.
  ``ingested_utc`` is when the record became known here; the feature store reads stance as of that time,
  never as of the VM's ``finished_utc``.
* Graph edges (7-day TTL), all stamped with the ingest time:
  ``society --holds_stance--> symbol`` (weight = mean stance), ``agent --member_of--> society``,
  ``agent --follows--> agent``, ``agent --engaged--> agent`` (weight = interactions),
  ``agent --attends--> symbol`` (weight = items that mentioned it; a count, not a view), and
  ``event --seeded--> society`` for every briefing item, so the intelligence a society read is a walk.
  Agent ids are ``<run_id>:<agent>`` so two runs never share a node.

A stance is what the simulation REPORTED; nothing here says it was right. Ingesting a run twice is refused.
"""
from __future__ import annotations

import json
import math
from collections import defaultdict
from datetime import datetime
from pathlib import Path
from typing import Any

from trading_live_claude.analysis.pit import read_jsonl
from trading_live_claude.intel.graph import Edge, append_edges
from trading_live_claude.sim.budget import settle
from trading_live_claude.sim.result import SimResult
from trading_live_claude.sim.seed import SimSeed

RUNS = "oasis_runs.jsonl"


def aggregate(result: SimResult) -> dict[str, dict[str, float | int | None]]:
    """Per symbol: mean stance, sample dispersion (None with one agent), and the number of agents."""
    views: dict[str, list[float]] = defaultdict(list)
    for row in result.stances.values():
        for sym, v in row.items():
            views[sym].append(v)
    out: dict[str, dict[str, float | int | None]] = {}
    for sym, xs in sorted(views.items()):
        mu = sum(xs) / len(xs)
        sd = math.sqrt(sum((x - mu) ** 2 for x in xs) / (len(xs) - 1)) if len(xs) > 1 else None
        out[sym] = {"stance": round(mu, 6), "dispersion": None if sd is None else round(sd, 6), "n": len(xs)}
    return out


def edges_for(result: SimResult, seed: SimSeed, by_symbol: dict[str, dict[str, Any]], as_of: str) -> list[Edge]:
    soc = ("society", result.run_id)
    agents = {a for a in result.stances} | {x for p in result.follows for x in p} | \
             {a for a, _, _ in result.engaged} | {b for _, b, _ in result.engaged} | {a for a, _, _ in result.attends}

    def ag(a: str) -> tuple[str, str]:
        return ("agent", f"{result.run_id}:{a}")

    out = [Edge(subject=soc, predicate="holds_stance", object=("symbol", s), weight=float(v["stance"]),
                as_of=as_of, meta={"dispersion": "" if v["dispersion"] is None else v["dispersion"],
                                   "n": v["n"], "run_id": result.run_id})
           for s, v in by_symbol.items()]
    out += [Edge(ag(a), "member_of", soc, 1.0, as_of) for a in sorted(agents)]  # type: ignore[arg-type]
    out += [Edge(ag(a), "follows", ag(b), 1.0, as_of) for a, b in result.follows]  # type: ignore[arg-type]
    out += [Edge(ag(a), "engaged", ag(b), float(n), as_of) for a, b, n in result.engaged]  # type: ignore[arg-type]
    out += [Edge(ag(a), "attends", ("symbol", s), float(n), as_of) for a, s, n in result.attends]  # type: ignore[arg-type]
    out += [Edge(("event", b.item_id), "seeded", soc, 1.0, as_of) for b in seed.briefing]
    return out


def ingested_runs(state: Path) -> set[str]:
    return {str(r.get("run_id")) for r in read_jsonl(state / RUNS)}


def ingest(result: SimResult, seed: SimSeed, state: Path, *, now: datetime,
           graph_journal: Path | None = None) -> dict[str, Any]:
    if result.run_id in ingested_runs(state):
        raise ValueError(f"run {result.run_id} is already ingested")
    as_of = now.isoformat()
    by_symbol = aggregate(result)
    row = {"run_id": result.run_id, "seed_hash": result.seed_hash, "issued_utc": seed.issued_utc,
           "finished_utc": result.finished_utc, "ingested_utc": as_of, "platform": seed.platform,
           "model_platform": seed.model_platform, "model": seed.model, "spent_usd": result.spent_usd,
           "max_usd": seed.budget.max_usd, "over_budget": result.over_budget, "tokens_in": result.tokens_in,
           "tokens_out": result.tokens_out, "n_agents": result.n_agents, "n_steps": result.n_steps,
           "stopped": result.stopped, "briefing_items": len(seed.briefing), "by_symbol": by_symbol}
    state.mkdir(parents=True, exist_ok=True)
    with (state / RUNS).open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(row) + "\n")
    edges = edges_for(result, seed, by_symbol, as_of)
    append_edges(edges, graph_journal)
    settle(state, result.run_id, result.spent_usd)
    return {"run_id": result.run_id, "symbols": len(by_symbol), "edges": len(edges),
            "spent_usd": result.spent_usd, "over_budget": result.over_budget}
