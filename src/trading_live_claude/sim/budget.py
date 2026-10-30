"""OASIS spend: a hard cap per run, and a cumulative ledger across runs.

User 2026-10-30: a $100 credit for simulations. Every seed RESERVES its per-run cap in
``state/oasis_ledger.jsonl`` when it is issued; ingesting the run's result SETTLES the reservation at what
the run actually spent; a seed that will never run can be CANCELLED to release it. A new seed is refused
when the total committed (settled spend + open reservations) plus its cap would exceed the total.

Prices are per million tokens and supplied by the caller: model prices change, and a cap computed from a
stale built-in price would not be a cap.
"""
from __future__ import annotations

import json
import math
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

LEDGER = "oasis_ledger.jsonl"
DEFAULT_TOTAL_USD = 100.0
MAX_AGENTS = 1000
MAX_STEPS = 100


class BudgetError(ValueError):
    pass


@dataclass(frozen=True)
class RunBudget:
    max_usd: float
    max_agents: int
    max_steps: int
    usd_per_mtok_in: float
    usd_per_mtok_out: float

    def __post_init__(self) -> None:
        for name in ("max_usd", "usd_per_mtok_in", "usd_per_mtok_out"):
            v = getattr(self, name)
            if not (isinstance(v, (int, float)) and math.isfinite(v) and v > 0):
                raise BudgetError(f"{name} must be a positive number; got {v!r}")
        if not 1 <= self.max_agents <= MAX_AGENTS:
            raise BudgetError(f"max_agents must be 1..{MAX_AGENTS}; got {self.max_agents}")
        if not 1 <= self.max_steps <= MAX_STEPS:
            raise BudgetError(f"max_steps must be 1..{MAX_STEPS}; got {self.max_steps}")

    def cost(self, tokens_in: int, tokens_out: int) -> float:
        return tokens_in / 1e6 * self.usd_per_mtok_in + tokens_out / 1e6 * self.usd_per_mtok_out

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _rows(state: Path) -> list[dict[str, Any]]:
    p = state / LEDGER
    if not p.exists():
        return []
    out = []
    for line in p.read_text(encoding="utf-8").splitlines():
        try:
            out.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return out


def committed(state: Path) -> dict[str, Any]:
    """Settled spend, open reservations, and the runs in each."""
    reserved: dict[str, float] = {}
    settled: dict[str, float] = {}
    cancelled: set[str] = set()
    for r in _rows(state):
        rid = str(r.get("run_id"))
        if r.get("kind") == "reserve":
            reserved[rid] = float(r["usd"])
        elif r.get("kind") == "settle":
            settled[rid] = float(r["spent_usd"])
        elif r.get("kind") == "cancel":
            cancelled.add(rid)
    open_ = {k: v for k, v in reserved.items() if k not in settled and k not in cancelled}
    return {"spent": sum(settled.values()), "reserved": sum(open_.values()),
            "open_runs": sorted(open_), "settled_runs": sorted(settled)}


def remaining(state: Path, total_usd: float = DEFAULT_TOTAL_USD) -> float:
    c = committed(state)
    return total_usd - c["spent"] - c["reserved"]


def _append(state: Path, row: dict[str, Any]) -> None:
    state.mkdir(parents=True, exist_ok=True)
    with (state / LEDGER).open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(row | {"ts": datetime.now(UTC).isoformat()}) + "\n")


def reserve(state: Path, run_id: str, usd: float, *, total_usd: float = DEFAULT_TOTAL_USD) -> None:
    left = remaining(state, total_usd)
    if usd > left + 1e-9:
        raise BudgetError(f"run cap ${usd:.2f} exceeds what is left of the ${total_usd:.2f} total "
                          f"(${left:.2f}); settle or cancel open runs, or lower --max-usd")
    _append(state, {"kind": "reserve", "run_id": run_id, "usd": round(usd, 6)})


def settle(state: Path, run_id: str, spent_usd: float) -> None:
    _append(state, {"kind": "settle", "run_id": run_id, "spent_usd": round(spent_usd, 6)})


def cancel(state: Path, run_id: str) -> None:
    c = committed(state)
    if run_id not in c["open_runs"]:
        raise BudgetError(f"{run_id} has no open reservation to cancel")
    _append(state, {"kind": "cancel", "run_id": run_id})
