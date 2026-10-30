"""An OASIS seed: what the simulated society is shown, which model runs it, and its hard budget.

The briefing is OSINT only: headlines that NAME a basket symbol in their title, from the live collector
(``state/news.jsonl``) and the GDELT backfill/follower (``state/gdelt_gkg``), known before the seed was
issued. No price or volatility series goes in (user rule for the fusion layer, 2026-10-12); a headline may
still quote a price, which is news text, not a series. Each item carries the id of its event node in the
graph, so "which intelligence did this society read" is a graph walk after ingest.

The seed is hashed over its canonical JSON. The runner echoes the hash in its result, and ingest refuses a
result whose hash does not match, so a result can never be attached to a briefing it was not run on.
"""
from __future__ import annotations

import hashlib
import json
from collections import defaultdict
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import asdict, dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from trading_live_claude.analysis.pit import parse_ts, read_jsonl
from trading_live_claude.sim.budget import DEFAULT_TOTAL_USD, RunBudget, reserve

PLATFORMS = ("twitter", "reddit")
MODEL_PLATFORMS = ("openai", "anthropic")
SEEDS_DIR = "oasis_seeds"


@dataclass(frozen=True)
class BriefItem:
    item_id: str  # the event node id in the graph: news:<source>:<content_hash> or gkg:<record id>
    title: str
    source: str
    arrived_utc: str
    symbols: tuple[str, ...]


@dataclass(frozen=True)
class SimSeed:
    run_id: str
    issued_utc: str
    platform: str
    model_platform: str
    model: str
    budget: RunBudget
    symbols: tuple[str, ...]
    aliases: Mapping[str, tuple[str, ...]]
    briefing: tuple[BriefItem, ...]
    seed_hash: str

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["budget"] = self.budget.to_dict()
        d["aliases"] = {k: list(v) for k, v in self.aliases.items()}
        return d


def _canonical(body: Mapping[str, Any]) -> str:
    return json.dumps(body, sort_keys=True, separators=(",", ":"))


def _candidates(state: Path, symbols: frozenset[str], start: datetime, end: datetime) -> list[BriefItem]:
    out: list[BriefItem] = []
    for r in read_jsonl(state / "news.jsonl"):
        at, named = parse_ts(r.get("arrived_utc")), tuple(sorted(set(r.get("symbols") or ()) & symbols))
        if at is None or not named or not start <= at < end or not r.get("content_hash"):
            continue
        out.append(BriefItem(f"news:{r.get('source')}:{r['content_hash']}", str(r.get("title", "")),
                             str(r.get("source", "")), at.isoformat(), named))
    for p in sorted((state / "gdelt_gkg").glob("*.jsonl")):
        if p.name.endswith("progress.jsonl"):
            continue
        for r in read_jsonl(p):
            at, named = parse_ts(r.get("arrived_utc")), tuple(sorted(set(r.get("symbols") or ()) & symbols))
            if at is None or not named or not start <= at < end or not r.get("title"):
                continue
            out.append(BriefItem(f"gkg:{r.get('guid')}", str(r["title"]), str(r.get("domain", "")),
                                 at.isoformat(), named))
    return out


def select_briefing(items: Iterable[BriefItem], *, max_items: int) -> tuple[BriefItem, ...]:
    """Newest first, one story per normalised title, round-robin across symbols so a heavily covered
    symbol (BTC) cannot crowd every other one out of a small briefing."""
    seen: set[str] = set()
    per: dict[str, list[BriefItem]] = defaultdict(list)
    for it in sorted(items, key=lambda i: i.arrived_utc, reverse=True):
        key = " ".join("".join(ch.lower() if ch.isalnum() else " " for ch in it.title).split())
        if not key or key in seen:
            continue
        seen.add(key)
        per[it.symbols[0]].append(it)
    picked: list[BriefItem] = []
    queues = [per[s] for s in sorted(per)]
    while queues and len(picked) < max_items:
        queues = [q for q in queues if q]
        for q in queues:
            if len(picked) >= max_items:
                break
            picked.append(q.pop(0))
    return tuple(sorted(picked, key=lambda i: i.arrived_utc))


def build_seed(state: Path, *, symbols: Sequence[str], aliases: Mapping[str, Sequence[str]], budget: RunBudget,
               model_platform: str, model: str, now: datetime, platform: str = "twitter",
               hours: float = 24.0, max_items: int = 60) -> SimSeed:
    if platform not in PLATFORMS:
        raise ValueError(f"platform must be one of {PLATFORMS}; got {platform!r}")
    if model_platform not in MODEL_PLATFORMS:
        raise ValueError(f"model_platform must be one of {MODEL_PLATFORMS}; got {model_platform!r}")
    if not model.strip():
        raise ValueError("model is required (e.g. the exact OpenAI or Claude model id)")
    syms = frozenset(symbols)
    briefing = select_briefing(_candidates(state, syms, now - timedelta(hours=hours), now), max_items=max_items)
    if not briefing:
        raise ValueError(f"no headline named a basket symbol in the {hours:g} h before {now.isoformat()}; "
                         "nothing to brief a society with")
    body = {"issued_utc": now.astimezone(UTC).isoformat(), "platform": platform,
            "model_platform": model_platform, "model": model, "budget": budget.to_dict(),
            "symbols": sorted(syms), "aliases": {s: list(aliases.get(s, ())) for s in sorted(syms)},
            "briefing": [asdict(b) for b in briefing]}
    h = hashlib.sha256(_canonical(body).encode()).hexdigest()
    return SimSeed(run_id=f"{now.astimezone(UTC):%Y%m%dT%H%M%SZ}-{h[:8]}", issued_utc=body["issued_utc"],
                   platform=platform, model_platform=model_platform, model=model, budget=budget,
                   symbols=tuple(sorted(syms)), aliases={s: tuple(aliases.get(s, ())) for s in sorted(syms)},
                   briefing=briefing, seed_hash=h)


def write_seed(seed: SimSeed, state: Path, *, total_usd: float = DEFAULT_TOTAL_USD) -> Path:
    """Reserve the run's cap in the ledger, then write the seed. Refused if the total would be exceeded."""
    reserve(state, seed.run_id, seed.budget.max_usd, total_usd=total_usd)
    path = state / SEEDS_DIR / f"{seed.run_id}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(seed.to_dict(), indent=1), encoding="utf-8")
    return path


def load_seed(path: Path) -> SimSeed:
    d = json.loads(path.read_text(encoding="utf-8"))
    seed = SimSeed(run_id=d["run_id"], issued_utc=d["issued_utc"], platform=d["platform"],
                   model_platform=d["model_platform"], model=d["model"], budget=RunBudget(**d["budget"]),
                   symbols=tuple(d["symbols"]), aliases={k: tuple(v) for k, v in d["aliases"].items()},
                   briefing=tuple(BriefItem(**{**b, "symbols": tuple(b["symbols"])}) for b in d["briefing"]),
                   seed_hash=d["seed_hash"])
    body = {k: v for k, v in d.items() if k not in ("run_id", "seed_hash")}
    if hashlib.sha256(_canonical(body).encode()).hexdigest() != seed.seed_hash:
        raise ValueError(f"{path.name}: seed content does not match its hash (edited after issue?)")
    return seed
