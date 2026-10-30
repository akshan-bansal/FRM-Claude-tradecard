"""Backtest the risk lenses on the data available: train verdicts -> raw-p gate -> fused holdout trim/veto.

    python scripts/lens_backtest.py [--perm 500]

Reads state/lens_features.jsonl and state/lens_labels.jsonl; writes reports/lens_backtest_<date>.md.
No price or volatility input reaches any lens, and no multiple-testing correction is applied
(both by the user's decision; see analysis/risk_lens.py).
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import UTC, datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from trading_live_claude.analysis.feature_store import load_store
from trading_live_claude.analysis.risk_lens import (
    ALPHA,
    LENSES,
    VETO_QUANTILE,
    assert_no_price_inputs,
    backtest,
    build_panel,
    split,
    verdict,
)

ROOT = Path(__file__).resolve().parent.parent


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--perm", type=int, default=500)
    a = ap.parse_args()
    assert_no_price_inputs(LENSES)

    store = load_store(ROOT / "state" / "lens_features.jsonl")
    labels = {(r["date"], r["symbol"]): r for r in
              (json.loads(ln) for ln in (ROOT / "state" / "lens_labels.jsonl").read_text(encoding="utf-8").splitlines())}
    lines = [f"# Risk-lens backtest, {datetime.now(UTC):%Y-%m-%d %H:%M} UTC", "",
             "Target: 5-day drawdown / (20-day vol x sqrt 5). Lens inputs: news / overlay / graph only "
             "(no price or volatility). Null: date rotation. Gate: raw p < "
             f"{ALPHA} and rho > 0 on TRAIN dates, no multiple-testing correction. "
             "Holdout starts one label horizon after train ends. "
             f"Veto = top {1 - VETO_QUANTILE:.0%} of fused score.", ""]

    panels, train, hold, skipped = {}, {}, {}, []
    for lens in LENSES:
        p = build_panel(lens, store, labels)
        if p is None:
            skipped.append(lens)
            continue
        tr, ho, hold_start = split(p)
        panels[lens], train[lens], hold[lens] = p, tr, ho
        lines.append(f"- `{lens}`: {len(p.y)} events, {len(set(p.symbols))} symbols, {min(p.dates)}..{max(p.dates)}; "
                     f"train {int(tr.sum())}, holdout {int(ho.sum())} from {hold_start}")
    for lens in skipped:
        lines.append(f"- `{lens}`: too few events with known features and a matured label; not tested")

    tv = [v for lens in panels for rule in ("reliability", "future")
          if (v := verdict(panels[lens], rule, train[lens], n_perm=a.perm)) is not None]
    hv = [v for lens in panels for rule in ("reliability", "future")
          if (v := verdict(panels[lens], rule, hold[lens], n_perm=a.perm)) is not None]
    for title, vs in (("Train verdicts (these gate the fusion)", tv), ("Holdout verdicts (reported, not used)", hv)):
        lines += ["", f"## {title}", "",
                  f"{len(vs)} verdicts tested at p < {ALPHA} with no correction: about "
                  f"{len(vs) * ALPHA:.1f} would pass by chance alone if no lens had any signal.", "",
                  "| lens | rule | events | rho | p | passes |", "|---|---|---:|---:|---:|---|"]
        lines += [f"| {v.lens} | {v.rule} | {v.n} | {v.rho:+.3f} | {v.p:.3f} | {'yes' if v.passed else 'no'} |"
                  for v in vs] or ["| (none scoreable) | | | | | |"]

    voters = [(v.lens, v.rule) for v in tv if v.passed]
    lines += ["", "## Fused trim / veto on the holdout", ""]
    bt = backtest(panels, voters, hold)
    if bt is None:
        lines.append("No (lens, rule) passed the gate on the training dates (or the voters share too few "
                     "holdout events), so nothing is trimmed or vetoed. That is the verdict, not a failure.")
    else:
        lines += [f"Voters: {', '.join(bt.voters)}; {bt.n} holdout events; rho(fused, drawdown) {bt.rho:+.3f}.", "",
                  "| | mean 5-day drawdown |", "|---|---:|",
                  f"| all events (no lens) | {bt.dd_all:.2%} |", f"| vetoed ({bt.veto_share:.0%}) | {bt.dd_vetoed:.2%} |",
                  f"| kept | {bt.dd_kept:.2%} |", f"| kept, exposure-weighted after trim | {bt.dd_trimmed_exposure:.2%} |"]
    lines += ["", "## Diagnostic: each lens alone on the holdout (NOT gated; do not act on these)", "",
              "| lens/rule | events | rho | dd vetoed | dd kept |", "|---|---:|---:|---:|---:|"]
    for lens in panels:
        for rule in ("reliability", "future"):
            d = backtest(panels, [(lens, rule)], hold)
            if d is not None:
                lines.append(f"| {lens}/{rule} | {d.n} | {d.rho:+.3f} | {d.dd_vetoed:.2%} | {d.dd_kept:.2%} |")
    out = ROOT / "reports" / f"lens_backtest_{datetime.now(UTC):%Y-%m-%d}.md"
    out.parent.mkdir(exist_ok=True)
    out.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print("\n".join(lines))
    print(f"\n-> {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
