"""Issue OASIS seeds and ingest their results. Spends nothing itself: the simulation runs on the VM.

    python scripts/oasis_sim.py seed --model <model id> --max-usd 5 --max-agents 50 --max-steps 10 \\
        --usd-per-mtok-in <price> --usd-per-mtok-out <price> [--model-platform openai|anthropic]
    python scripts/oasis_sim.py ingest state/oasis_inbox/<run_id>.result.json
    python scripts/oasis_sim.py status
    python scripts/oasis_sim.py cancel <run_id>        # a seed that will never run: release its reservation

``seed`` writes state/oasis_seeds/<run_id>.json and reserves its cap against the cumulative total (default
$100, ``--total-usd``). Copy the seed to the VM, run scripts/oasis_vm_runner.py there, copy the result into
state/oasis_inbox/ and run ``ingest``. Prices are per million tokens; pass the model's current prices.
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import UTC, datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from trading_live_claude.analysis.feature_store import universe
from trading_live_claude.news.runner import load_config
from trading_live_claude.sim import (
    BudgetError,
    ResultError,
    RunBudget,
    build_seed,
    ingest,
    load_seed,
    parse_result,
    write_seed,
)
from trading_live_claude.sim.budget import DEFAULT_TOTAL_USD, cancel, committed
from trading_live_claude.sim.ingest import ingested_runs

ROOT = Path(__file__).resolve().parent.parent
STATE = ROOT / "state"


def _seed(a: argparse.Namespace) -> int:
    syms = [i.symbol for i in universe(ROOT / "config" / "basket_seed.json")]
    aliases = load_config(ROOT / "config" / "news_sources.yaml").aliases
    budget = RunBudget(max_usd=a.max_usd, max_agents=a.max_agents, max_steps=a.max_steps,
                       usd_per_mtok_in=a.usd_per_mtok_in, usd_per_mtok_out=a.usd_per_mtok_out)
    seed = build_seed(STATE, symbols=syms, aliases=aliases, budget=budget, model_platform=a.model_platform,
                      model=a.model, now=datetime.now(UTC), hours=a.hours, max_items=a.max_items)
    path = write_seed(seed, STATE, total_usd=a.total_usd)
    left = a.total_usd - sum(committed(STATE)[k] for k in ("spent", "reserved"))
    print(f"[oasis] seed {seed.run_id}: {len(seed.briefing)} headlines on "
          f"{len({s for b in seed.briefing for s in b.symbols})} symbols, {seed.model_platform}/{seed.model}, "
          f"cap ${budget.max_usd:.2f} reserved; ${left:.2f} of ${a.total_usd:.2f} left\n  -> {path}")
    return 0


def _ingest(a: argparse.Namespace) -> int:
    raw = json.loads(a.result.read_text(encoding="utf-8"))
    seed_path = a.seed or STATE / "oasis_seeds" / f"{raw.get('run_id')}.json"
    seed = load_seed(seed_path)
    res = parse_result(raw, seed)
    out = ingest(res, seed, STATE, now=datetime.now(UTC))
    flag = "  ** OVER BUDGET: the runner's meter did not hold the cap **" if out["over_budget"] else ""
    print(f"[oasis] ingested {out['run_id']}: {out['symbols']} symbols with a stance, {out['edges']} graph edges, "
          f"spent ${out['spent_usd']:.4f} ({res.stopped}){flag}")
    for note in raw.get("notes") or ():
        print(f"  runner note: {note}")
    return 0


def _status(a: argparse.Namespace) -> int:
    c = committed(STATE)
    print(f"spent ${c['spent']:.2f}, reserved ${c['reserved']:.2f}, "
          f"left ${a.total_usd - c['spent'] - c['reserved']:.2f} of ${a.total_usd:.2f}")
    print(f"open seeds (awaiting a result): {', '.join(c['open_runs']) or 'none'}")
    print(f"ingested runs: {len(ingested_runs(STATE))}")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--total-usd", type=float, default=DEFAULT_TOTAL_USD, help="cumulative cap across runs")
    sub = ap.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("seed")
    s.add_argument("--model", required=True, help="exact model id the VM should run")
    s.add_argument("--model-platform", choices=("openai", "anthropic"), default="openai")
    s.add_argument("--max-usd", type=float, required=True)
    s.add_argument("--max-agents", type=int, required=True)
    s.add_argument("--max-steps", type=int, required=True)
    s.add_argument("--usd-per-mtok-in", type=float, required=True)
    s.add_argument("--usd-per-mtok-out", type=float, required=True)
    s.add_argument("--hours", type=float, default=24.0, help="briefing look-back")
    s.add_argument("--max-items", type=int, default=60)
    i = sub.add_parser("ingest")
    i.add_argument("result", type=Path)
    i.add_argument("--seed", type=Path, default=None)
    sub.add_parser("status")
    c = sub.add_parser("cancel")
    c.add_argument("run_id")
    a = ap.parse_args()
    try:
        if a.cmd == "seed":
            return _seed(a)
        if a.cmd == "ingest":
            return _ingest(a)
        if a.cmd == "cancel":
            cancel(STATE, a.run_id)
            print(f"[oasis] {a.run_id}: reservation released")
            return 0
        return _status(a)
    except (BudgetError, ResultError, ValueError) as exc:
        print(f"[oasis] refused: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
