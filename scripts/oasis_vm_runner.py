"""OASIS VM runner: run one seed as a CAMEL-AI OASIS simulation under a hard spending cap.

Runs on the VM, NOT in the trading environment. ``camel-oasis`` needs Python < 3.12 and pins packages the
repo cannot share. Copy this one file and the seed to the VM:

    python3.11 -m venv oasis-env && oasis-env/bin/pip install camel-oasis==0.2.5
    export OPENAI_API_KEY=...        # or ANTHROPIC_API_KEY, matching the seed's model_platform
    oasis-env/bin/python oasis_vm_runner.py <run_id>.json --out-dir out/ --dry-run   # plan and cost only
    oasis-env/bin/python oasis_vm_runner.py <run_id>.json --out-dir out/

It writes ``out/<run_id>.result.json`` (copy it back and run ``scripts/oasis_sim.py ingest`` in the repo) and
``out/oasis_<run_id>.db``, OASIS's own record, kept on the VM for audit.

What a run does:
1. Verifies the seed's hash, so an edited seed is refused here as well as at ingest.
2. Plans the run to fit the cap: an interview reserve is set aside first, then steps (and agents if needed)
   are cut until the estimated cost fits.
3. Builds the society: agent 0 is a newswire that posts the briefing; agents 1..n are investor personas
   drawn deterministically from the seed hash.
4. Runs LLM steps (post, comment, like, repost, follow) on the seed's platform, stopping early when the
   metered spend reaches the cap less the interview reserve. Every model call is metered from its own
   token usage; a call that reports none is charged a conservative estimate.
5. Interviews every persona for a stance on each briefed symbol, in [-1, +1], -1 adverse.
6. Extracts follows, engagements, symbol attention and stances from the OASIS database into the result.

Written against the documented OASIS 0.2.x API; the OASIS calls are exercised only on a real VM run. Every
step that does not touch OASIS is unit-tested in the repo (tests/test_oasis_sim.py).
"""
from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import math
import os
import random
import re
import sqlite3
import sys
from collections import Counter
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

# Conservative per-call token estimates, used for planning and for any call that reports no usage.
EST_IN, EST_OUT = 2500, 250
EST_INTERVIEW_OUT = 200
PLAN_SAFETY = 0.85  # plan to 85% of the cap: estimates can be low

ARCHETYPES: tuple[tuple[str, str], ...] = (
    ("retail_momentum", "Retail investor who follows trending tickers and reacts quickly to headlines."),
    ("value_skeptic", "Long-horizon value investor, skeptical of hype, cares about fundamentals."),
    ("macro_analyst", "Macro analyst reading central-bank and geopolitical news for cross-asset risk."),
    ("crypto_native", "Crypto-native trader following exchange news, regulation and on-chain stories."),
    ("risk_manager", "Institutional risk manager focused on drawdowns and tail risk."),
    ("commodities_specialist", "Energy, metals and commodities specialist."),
    ("tech_analyst", "Semiconductor and technology-sector analyst."),
    ("contrarian", "Contrarian who fades crowded narratives."),
)

STANCE_PROMPT = (
    "Based only on what you have read on this platform, give your view of each instrument below over the "
    "next five trading days. Reply with ONE JSON object and nothing else, mapping each symbol exactly as "
    "written to a number from -1 (clearly adverse: you expect it to fall) to +1 (clearly favourable), or "
    "null if what you read gives you no view. Symbols: {symbols}"
)


class SeedError(ValueError):
    pass


# --------------------------------------------------------------------------- seed


def load_seed(path: Path) -> dict[str, Any]:
    """The seed, after checking its content against its hash (the same canonical form the repo uses)."""
    d = json.loads(path.read_text(encoding="utf-8"))
    body = {k: v for k, v in d.items() if k not in ("run_id", "seed_hash")}
    canon = json.dumps(body, sort_keys=True, separators=(",", ":"))
    if hashlib.sha256(canon.encode()).hexdigest() != d.get("seed_hash"):
        raise SeedError(f"{path.name}: content does not match seed_hash; refusing to run an edited seed")
    if d.get("platform") != "twitter":
        raise SeedError(f"platform {d.get('platform')!r} is not supported by this runner yet (twitter only)")
    if d.get("model_platform") not in ("openai", "anthropic"):
        raise SeedError(f"unknown model_platform {d.get('model_platform')!r}")
    return d


# --------------------------------------------------------------------------- planning and metering


def call_cost(budget: Mapping[str, float], tokens_in: int, tokens_out: int) -> float:
    return tokens_in / 1e6 * budget["usd_per_mtok_in"] + tokens_out / 1e6 * budget["usd_per_mtok_out"]


@dataclass(frozen=True)
class Plan:
    personas: int
    steps: int
    interview_reserve_usd: float
    est_usd: float


def plan_run(budget: Mapping[str, Any]) -> Plan:
    """Fit personas and steps under the cap. The interview reserve comes first: a run that cannot afford to
    ask its agents for a stance has produced nothing ingestible."""
    cap = float(budget["max_usd"]) * PLAN_SAFETY
    personas = max(1, int(budget["max_agents"]) - 1)  # agent 0 is the newswire
    steps = int(budget["max_steps"])
    step_call = call_cost(budget, EST_IN, EST_OUT)
    interview_call = call_cost(budget, EST_IN, EST_INTERVIEW_OUT)
    while personas >= 1:
        reserve = personas * interview_call
        affordable = math.floor((cap - reserve) / (personas * step_call)) if cap > reserve else 0
        if affordable >= 1:
            steps = min(steps, affordable)
            return Plan(personas, steps, round(reserve, 6), round(reserve + personas * steps * step_call, 6))
        personas -= 1
    raise SeedError(f"max_usd ${budget['max_usd']} cannot cover even one persona for one step plus its "
                    "interview at these prices")


class BudgetExhausted(RuntimeError):
    pass


@dataclass
class Meter:
    """Token and spend accounting for every model call. ``limit_usd`` can be lowered as the run proceeds."""

    budget: Mapping[str, float]
    limit_usd: float
    tokens_in: int = 0
    tokens_out: int = 0
    calls: int = 0
    unmetered_calls: int = 0

    @property
    def spent_usd(self) -> float:
        return call_cost(self.budget, self.tokens_in, self.tokens_out)

    def check(self) -> None:
        if self.spent_usd >= self.limit_usd:
            raise BudgetExhausted(f"spent ${self.spent_usd:.4f} of ${self.limit_usd:.4f}")

    def record(self, response: Any) -> None:
        self.calls += 1
        usage = getattr(response, "usage", None)
        pin = getattr(usage, "prompt_tokens", None) if usage is not None else None
        pout = getattr(usage, "completion_tokens", None) if usage is not None else None
        if isinstance(pin, int) and isinstance(pout, int):
            self.tokens_in += pin
            self.tokens_out += pout
        else:  # no usage reported (e.g. a stream): charge the conservative estimate, never zero
            self.unmetered_calls += 1
            self.tokens_in += EST_IN
            self.tokens_out += EST_OUT


def attach_meter(model: Any, meter: Meter) -> None:
    """Wrap the model backend's ``run`` / ``arun`` so every call is checked against the cap and metered."""
    for name in ("run", "arun"):
        fn = getattr(model, name, None)
        if fn is None:
            continue
        if asyncio.iscoroutinefunction(fn):
            async def awrapped(*a: Any, _fn: Callable[..., Any] = fn, **k: Any) -> Any:
                meter.check()
                r = await _fn(*a, **k)
                meter.record(r)
                return r
            setattr(model, name, awrapped)
        else:
            def wrapped(*a: Any, _fn: Callable[..., Any] = fn, **k: Any) -> Any:
                meter.check()
                r = _fn(*a, **k)
                meter.record(r)
                return r
            setattr(model, name, wrapped)


# --------------------------------------------------------------------------- society


@dataclass(frozen=True)
class Persona:
    agent_id: int
    user_name: str
    name: str
    bio: str


def personas(seed_hash: str, n: int) -> list[Persona]:
    """Deterministic investor personas: the same seed always builds the same society."""
    rng = random.Random(seed_hash)
    out = []
    for i in range(1, n + 1):
        key, bio = ARCHETYPES[(i - 1) % len(ARCHETYPES)]
        horizon = rng.choice(("days", "weeks", "months"))
        tolerance = rng.choice(("low", "moderate", "high"))
        out.append(Persona(i, f"{key}_{i:03d}", f"{key.replace('_', ' ').title()} {i}",
                           f"{bio} Holding horizon: {horizon}. Risk tolerance: {tolerance}."))
    return out


def briefing_posts(seed: Mapping[str, Any]) -> list[str]:
    return [f"{b['title']} (source: {b['source']}, {b['arrived_utc'][:16]} UTC)" for b in seed["briefing"]]


# --------------------------------------------------------------------------- extraction


def parse_stance(text: str, symbols: Sequence[str]) -> dict[str, float]:
    """The first JSON object in an interview answer, kept only for briefed symbols with values in [-1, 1].
    Out-of-range numbers are dropped, not clamped: a malformed answer is not a strong view."""
    m = re.search(r"\{.*?\}", text or "", re.S)
    if not m:
        return {}
    try:
        raw = json.loads(m.group(0))
    except json.JSONDecodeError:
        return {}
    if not isinstance(raw, dict):
        return {}
    wanted = {s.upper(): s for s in symbols}
    out: dict[str, float] = {}
    for k, v in raw.items():
        sym = wanted.get(str(k).strip().upper())
        if sym is None or isinstance(v, bool) or not isinstance(v, (int, float)):
            continue
        if math.isfinite(v) and -1.0 <= v <= 1.0:
            out[sym] = float(v)
    return out


def mentions(text: str, aliases: Mapping[str, Iterable[str]]) -> set[str]:
    """Symbols named in a text: whole-word aliases, short all-caps ones case-sensitively (as in the repo)."""
    hit = set()
    for sym, names in aliases.items():
        for name in [sym, *names]:
            flags = 0 if (name.isupper() and len(name) <= 5) else re.IGNORECASE
            if re.search(rf"(?<![A-Za-z0-9]){re.escape(name)}(?![A-Za-z0-9])", text or "", flags):
                hit.add(sym)
                break
    return hit


def _columns(con: sqlite3.Connection, table: str) -> set[str]:
    return {r[1] for r in con.execute(f"PRAGMA table_info('{table}')")}


@dataclass
class Extracted:
    stances: dict[str, dict[str, float]] = field(default_factory=dict)
    follows: list[tuple[str, str]] = field(default_factory=list)
    engaged: list[tuple[str, str, int]] = field(default_factory=list)
    attends: list[tuple[str, str, int]] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)


def extract(db: Path, *, symbols: Sequence[str], aliases: Mapping[str, Iterable[str]],
            newswire: int = 0) -> Extracted:
    """Read the OASIS database defensively: a missing table or column is noted, never guessed at."""
    out = Extracted()
    con = sqlite3.connect(db)
    try:
        ucols = _columns(con, "user")
        if {"user_id", "agent_id"} <= ucols:
            agent_of = {int(u): int(a) for u, a in con.execute("SELECT user_id, agent_id FROM user")}
        else:
            agent_of = {}
            out.notes.append("user table lacks user_id/agent_id; user ids used as agent ids")

        def ag(uid: Any) -> int:
            return agent_of.get(int(uid), int(uid))

        if {"follower_id", "followee_id"} <= _columns(con, "follow"):
            out.follows = [(str(ag(a)), str(ag(b))) for a, b in
                           con.execute("SELECT follower_id, followee_id FROM follow")]
        else:
            out.notes.append("no follow table")
        pcols = _columns(con, "post")
        author: dict[int, int] = {}
        texts: list[tuple[int, str]] = []
        eng: Counter[tuple[str, str]] = Counter()
        if {"post_id", "user_id"} <= pcols:
            orig = "original_post_id" if "original_post_id" in pcols else "NULL"
            content = "content" if "content" in pcols else "''"
            rows = list(con.execute(f"SELECT post_id, user_id, {orig}, {content} FROM post"))
            author = {int(p): ag(u) for p, u, _, _ in rows}
            for _, u, o, c in rows:
                texts.append((ag(u), c or ""))
                if o is not None and int(o) in author and author[int(o)] != ag(u):
                    eng[(str(ag(u)), str(author[int(o)]))] += 1  # a repost or quote of someone's post
        else:
            out.notes.append("no post table")
        for table in ("like", "comment"):
            cols = _columns(con, table)
            if {"user_id", "post_id"} <= cols:
                for u, p in con.execute(f"SELECT user_id, post_id FROM '{table}'"):
                    a = author.get(int(p))
                    if a is not None and a != ag(u):
                        eng[(str(ag(u)), str(a))] += 1
                if table == "comment" and "content" in cols:
                    texts += [(ag(u), c or "") for u, c in con.execute("SELECT user_id, content FROM comment")]
        out.engaged = [(a, b, n) for (a, b), n in sorted(eng.items())]
        att: Counter[tuple[str, str]] = Counter()
        for a, text in texts:
            if a == newswire:
                continue  # the briefing itself is not anyone's attention
            for s in mentions(text, aliases):
                att[(str(a), s)] += 1
        out.attends = [(a, s, n) for (a, s), n in sorted(att.items())]
        if {"user_id", "action", "info"} <= _columns(con, "trace"):
            for u, info in con.execute("SELECT user_id, info FROM trace WHERE action = 'interview'"):
                try:
                    payload = json.loads(info) if isinstance(info, str) else {}
                except json.JSONDecodeError:
                    payload = {"response": info}
                view = parse_stance(str(payload.get("response", "")), symbols)
                if view and ag(u) != newswire:
                    out.stances[str(ag(u))] = view
        else:
            out.notes.append("no trace table: no interview answers, so no stances")
    finally:
        con.close()
    return out


# --------------------------------------------------------------------------- the run (OASIS)


async def run_simulation(seed: Mapping[str, Any], out_dir: Path) -> dict[str, Any]:
    import oasis
    from camel.models import ModelFactory
    from camel.types import ModelPlatformType
    from oasis import ActionType, AgentGraph, LLMAction, ManualAction, SocialAgent, UserInfo

    budget = seed["budget"]
    plan = plan_run(budget)
    meter = Meter(budget, limit_usd=float(budget["max_usd"]) - plan.interview_reserve_usd)
    platform = {"openai": ModelPlatformType.OPENAI, "anthropic": ModelPlatformType.ANTHROPIC}[seed["model_platform"]]
    model = ModelFactory.create(model_platform=platform, model_type=seed["model"])
    attach_meter(model, meter)

    actions = [ActionType.CREATE_POST, ActionType.CREATE_COMMENT, ActionType.LIKE_POST, ActionType.REPOST,
               ActionType.QUOTE_POST, ActionType.FOLLOW, ActionType.DO_NOTHING]
    graph = AgentGraph()
    people = [Persona(0, "newswire", "Newswire", "Posts verified headlines. Does not comment.")]
    people += personas(seed["seed_hash"], plan.personas)
    for p in people:
        graph.add_agent(SocialAgent(agent_id=p.agent_id, user_info=UserInfo(
            user_name=p.user_name, name=p.name, description=p.bio, profile=None, recsys_type="twitter"),
            agent_graph=graph, model=model, available_actions=actions))
    db = out_dir / f"oasis_{seed['run_id']}.db"
    env = oasis.make(agent_graph=graph, platform=oasis.DefaultPlatformType.TWITTER, database_path=str(db))
    stopped, steps_done = "steps", 0
    try:
        await env.reset()
        await env.step({graph.get_agent(0): [ManualAction(action_type=ActionType.CREATE_POST,
                                                          action_args={"content": t}) for t in briefing_posts(seed)]})
        for _ in range(plan.steps):
            if meter.spent_usd >= meter.limit_usd:
                stopped = "budget"
                break
            try:
                await env.step({a: LLMAction() for i, a in graph.get_agents() if i != 0})
            except BudgetExhausted:
                stopped = "budget"
                break
            steps_done += 1
        meter.limit_usd = float(budget["max_usd"])  # the interview reserve is now released
        prompt = STANCE_PROMPT.format(symbols=", ".join(seed["symbols"]))
        try:
            await env.step({a: ManualAction(action_type=ActionType.INTERVIEW, action_args={"prompt": prompt})
                            for i, a in graph.get_agents() if i != 0})
        except BudgetExhausted:
            stopped = "budget"
    except Exception as exc:  # report what happened; the spend so far is real and must reach the ledger
        stopped = "error"
        print(f"[oasis-runner] run failed: {type(exc).__name__}: {exc}", file=sys.stderr)
    finally:
        await env.close()
    ex = extract(db, symbols=seed["symbols"], aliases=seed["aliases"]) if db.exists() else Extracted(
        notes=["no database was written"])
    return {"run_id": seed["run_id"], "seed_hash": seed["seed_hash"], "finished_utc": datetime.now(UTC).isoformat(),
            "spent_usd": round(meter.spent_usd, 6), "tokens_in": meter.tokens_in, "tokens_out": meter.tokens_out,
            "n_agents": len(people), "n_steps": steps_done, "stopped": stopped, "stances": ex.stances,
            "follows": ex.follows, "engaged": ex.engaged, "attends": ex.attends,
            "notes": [*ex.notes, f"model calls {meter.calls}, unmetered {meter.unmetered_calls}",
                      f"plan: {plan.personas} personas x {plan.steps} steps, est ${plan.est_usd}"]}


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("seed", type=Path)
    ap.add_argument("--out-dir", type=Path, default=Path("."))
    ap.add_argument("--dry-run", action="store_true", help="verify the seed and print the plan; no LLM calls")
    a = ap.parse_args()
    seed = load_seed(a.seed)
    plan = plan_run(seed["budget"])
    print(f"[oasis-runner] {seed['run_id']}: {seed['model_platform']}/{seed['model']}, {len(seed['briefing'])} "
          f"briefing posts, {plan.personas} personas x {plan.steps} steps (+ newswire), est ${plan.est_usd:.2f} "
          f"of the ${seed['budget']['max_usd']} cap (interview reserve ${plan.interview_reserve_usd:.2f})")
    if a.dry_run:
        return 0
    key = {"openai": "OPENAI_API_KEY", "anthropic": "ANTHROPIC_API_KEY"}[seed["model_platform"]]
    if not os.environ.get(key):
        print(f"[oasis-runner] {key} is not set on this machine", file=sys.stderr)
        return 2
    a.out_dir.mkdir(parents=True, exist_ok=True)
    result = asyncio.run(run_simulation(seed, a.out_dir))
    out = a.out_dir / f"{seed['run_id']}.result.json"
    out.write_text(json.dumps(result, indent=1), encoding="utf-8")
    print(f"[oasis-runner] {result['stopped']}: spent ${result['spent_usd']:.4f}, {result['n_steps']} steps, "
          f"{len(result['stances'])} stances -> {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
