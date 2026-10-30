"""The OASIS result contract: what the VM runner hands back, validated before anything is ingested.

The runner extracts everything from its OASIS database itself and writes one JSON file, so the repo depends
on this contract and never on OASIS's internal schema. A result is refused when it was not run on the seed
it names (run id or seed hash differ), when it exceeds the seed's agent or step limits, or when a stance is
outside [-1, +1] or names a symbol the seed did not brief. Spend over the cap is not refused (the money is
already spent and the ledger must record it) but is flagged ``over_budget``.
"""
from __future__ import annotations

import math
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from trading_live_claude.analysis.pit import parse_ts
from trading_live_claude.sim.seed import SimSeed

STOP_REASONS = ("steps", "budget", "error")
OVER_BUDGET_TOLERANCE = 1.01  # one step can overshoot slightly before the meter stops the run


class ResultError(ValueError):
    pass


@dataclass(frozen=True)
class SimResult:
    run_id: str
    seed_hash: str
    finished_utc: str  # the VM's clock: informational; ingest stamps edges with the ingest time
    spent_usd: float
    tokens_in: int
    tokens_out: int
    n_agents: int
    n_steps: int
    stopped: str
    over_budget: bool
    stances: Mapping[str, Mapping[str, float]]  # agent id -> symbol -> stance in [-1, +1] (-1 adverse)
    follows: tuple[tuple[str, str], ...]  # (follower, followee)
    engaged: tuple[tuple[str, str, int], ...]  # (agent, author, interactions)
    attends: tuple[tuple[str, str, int], ...]  # (agent, symbol, items that mentioned it)


def _int(raw: Mapping[str, Any], key: str) -> int:
    v = raw.get(key)
    if not isinstance(v, int) or isinstance(v, bool) or v < 0:
        raise ResultError(f"{key} must be a non-negative integer; got {v!r}")
    return v


def parse_result(raw: Mapping[str, Any], seed: SimSeed) -> SimResult:
    if raw.get("run_id") != seed.run_id:
        raise ResultError(f"result is for run {raw.get('run_id')!r}, not {seed.run_id!r}")
    if raw.get("seed_hash") != seed.seed_hash:
        raise ResultError("result seed_hash does not match the seed: it was run on a different briefing")
    if parse_ts(raw.get("finished_utc")) is None:
        raise ResultError(f"finished_utc is not a timestamp: {raw.get('finished_utc')!r}")
    spent = raw.get("spent_usd")
    if not isinstance(spent, (int, float)) or not math.isfinite(spent) or spent < 0:
        raise ResultError(f"spent_usd must be a non-negative number; got {spent!r}")
    n_agents, n_steps = _int(raw, "n_agents"), _int(raw, "n_steps")
    if n_agents > seed.budget.max_agents or n_steps > seed.budget.max_steps:
        raise ResultError(f"{n_agents} agents / {n_steps} steps exceed the seed's "
                          f"{seed.budget.max_agents} / {seed.budget.max_steps}")
    if raw.get("stopped") not in STOP_REASONS:
        raise ResultError(f"stopped must be one of {STOP_REASONS}; got {raw.get('stopped')!r}")
    briefed = set(seed.symbols)
    stances: dict[str, dict[str, float]] = {}
    for agent, views in (raw.get("stances") or {}).items():
        row: dict[str, float] = {}
        for sym, v in (views or {}).items():
            if sym not in briefed:
                raise ResultError(f"agent {agent} holds a stance on {sym!r}, which the seed did not brief")
            if not isinstance(v, (int, float)) or not math.isfinite(v) or not -1.0 <= v <= 1.0:
                raise ResultError(f"agent {agent} stance on {sym} must be in [-1, 1]; got {v!r}")
            row[sym] = float(v)
        if row:
            stances[str(agent)] = row
    try:
        follows = tuple((str(a), str(b)) for a, b in raw.get("follows") or ())
        engaged = tuple((str(a), str(b), int(n)) for a, b, n in raw.get("engaged") or ())
        attends = tuple((str(a), str(s), int(n)) for a, s, n in raw.get("attends") or ())
    except (TypeError, ValueError) as exc:
        raise ResultError(f"follows / engaged / attends are malformed: {exc}") from exc
    if any(s not in briefed for _, s, _ in attends):
        raise ResultError("attends names a symbol the seed did not brief")
    return SimResult(
        run_id=seed.run_id, seed_hash=seed.seed_hash, finished_utc=str(raw["finished_utc"]),
        spent_usd=float(spent), tokens_in=_int(raw, "tokens_in"), tokens_out=_int(raw, "tokens_out"),
        n_agents=n_agents, n_steps=n_steps, stopped=str(raw["stopped"]),
        over_budget=float(spent) > seed.budget.max_usd * OVER_BUDGET_TOLERANCE,
        stances=stances, follows=follows, engaged=engaged, attends=attends)
