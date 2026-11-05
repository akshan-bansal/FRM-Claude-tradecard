"""Record or score the shadow VaR for a runner's book. Acts on nothing: see analysis/shadow_var.py.

Run by hand from the repo root, once a day after the runner has refreshed its cache:
    python scripts/var_shadow.py record --stream qt --stream kraken
    python scripts/var_shadow.py score
Add ``--ledger-dir <folder>`` to write somewhere other than the shared ledger, for a dry run.
"""
from __future__ import annotations

import argparse
from pathlib import Path

import pandas as pd

from trading_live_claude.analysis import shadow_var as sv
from trading_live_claude.config.settings import get_settings


def main() -> None:
    settings = get_settings()
    ap = argparse.ArgumentParser()
    ap.add_argument("command", choices=("record", "score"))
    ap.add_argument("--stream", action="append", default=[], help="A runner's ledger stream; repeat for more.")
    ap.add_argument("--state-dir", type=Path, default=Path(settings.state_dir))
    ap.add_argument("--cache-dir", type=Path, default=Path(settings.data_cache_dir))
    ap.add_argument("--ledger-dir", type=Path, default=None,
                    help=f"Where the {sv.STREAM} stream lives. Default: <state-dir>/ledger.")
    args = ap.parse_args()
    target = args.ledger_dir or args.state_dir / "ledger"
    pd.set_option("display.width", 200, "display.max_columns", 20)

    if args.command == "record":
        if not args.stream:
            ap.error("record needs at least one --stream")
        for stream in args.stream:
            payload = sv.record(stream, state_dir=args.state_dir, cache_dir=args.cache_dir, ledger_dir=target)
            if payload is None:
                print(f"{stream}: already recorded for its latest complete day")
                continue
            if not payload["written"]:
                print(f"{stream}: nothing recorded for {payload.get('as_of')}: {payload.get('reason')}")
                continue
            print(f"{stream}: recorded as_of {payload['as_of']}, window {payload['window']}, gross {payload['gross']:.2f}")
            for name, var in sorted(payload.get("methods", {}).items(), key=lambda kv: kv[1]):
                print(f"    {name:22s} {var:12.2f}")
            print(f"    {'gate_per_name_sum':22s} {payload.get('gate_per_name_sum')}")
        return

    scores = sv.score(target, args.cache_dir)
    if scores.empty:
        print("nothing to score yet")
        return
    print(f"{len(scores)} method-days, {int(scores.forward.sum())} written before their outcome")
    print(sv.summary(scores).round(4).to_string(index=False))


if __name__ == "__main__":
    main()
