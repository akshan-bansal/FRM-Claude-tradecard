"""The one stage-2 run. It refuses unless the pre-registration, spec and code all match the latest freeze record.

Before any verdict it runs the protocol's on-data validity checks: every atom finite on every decision
day, and a truncated run reproducing the full run. If either fails it stops, and no verdict is printed.

Usage (after freeze.py and fetch_stage2.sh): ``python run_stage2.py FRED_DIR [--workers N]``
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
from pathlib import Path

import numpy as np

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
from spec import KEYS, PRIMARY_LEVEL, SCORED, SECONDARY_LEVEL, SPEC  # noqa: E402

from trading_live_claude.quantum.atoms import load_panel  # noqa: E402
from trading_live_claude.quantum.fingerprint import of_model  # noqa: E402
from trading_live_claude.quantum.panel import (  # noqa: E402
    Panel,
    PanelResult,
    walk_forward,
    walk_forward_uncertainty,
)

TRUNCATE_AT = 200


def frozen_or_exit() -> dict[str, object]:
    from freeze import PREREG, RECORD, code_hashes

    if not (PREREG.exists() and RECORD.exists()):
        sys.exit("not frozen: run freeze.py before fetching data")
    last = json.loads(RECORD.read_text())["records"][-1]
    problems = []
    if hashlib.sha256(PREREG.read_bytes()).hexdigest() != last["prereg_sha256"]:
        problems.append("PREREGISTRATION.md")
    if of_model(SPEC) != last["spec"]:
        problems.append("spec.py constants")
    now = code_hashes()
    problems += [f for f in sorted(set(now) | set(last["code"])) if now.get(f) != last["code"].get(f)]
    if problems:
        sys.exit("changed since the last freeze record, refusing to run: " + ", ".join(problems))
    return last


def validity_or_exit(panel: Panel, full: PanelResult, workers: int) -> None:
    bad = int(np.sum(~np.isfinite(panel.X)))
    if bad:
        sys.exit(f"VALIDITY FAILED: {bad} missing atom values on decision days; no verdict is reported")
    if len(panel.dates) <= TRUNCATE_AT + 1:
        sys.exit(f"VALIDITY FAILED: {len(panel.dates)} weeks, too few to check truncation at week {TRUNCATE_AT}")
    part = walk_forward(panel.truncate(TRUNCATE_AT), SPEC, max_workers=workers)
    k = len(part.eval_weeks)
    if not np.array_equal(part.eval_weeks, full.eval_weeks[:k]):
        sys.exit("VALIDITY FAILED: truncated run evaluated different weeks; no verdict is reported")
    worst = {m: float(np.max(np.abs(part.probs[m] - full.probs[m][:k]))) for m in SPEC.models}
    if max(worst.values()) > 1e-9:
        sys.exit(f"VALIDITY FAILED: lookahead, truncated run differs {worst}; no verdict is reported")
    print(f"validity: all atoms finite; truncation at week {TRUNCATE_AT} reproduces {k} weeks for every model")


def line(name: str, v: tuple[float, float, float, bool], level: float) -> str:
    return f"   {name:<44} {v[0]:+.4f}  [{v[1]:+.4f}, {v[2]:+.4f}] at {level:.1%}  -> {'PASS' if v[3] else 'FAIL'}"


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("fred_dir")
    ap.add_argument("--workers", type=int, default=os.cpu_count() or 1)
    a = ap.parse_args()
    rec = frozen_or_exit()
    panel = load_panel(a.fred_dir, KEYS)
    print(f"frozen {rec['utc']}  prereg {str(rec['prereg_sha256'])[:12]}  spec {of_model(SPEC)[:12]}  "
          f"panel {panel.fingerprint()[:12]}")
    print(f"weeks {len(panel.dates)} ({panel.dates[0]} .. {panel.dates[-1]}), assets {', '.join(panel.assets)}; "
          f"scored {', '.join(KEYS[i] for i in SCORED)}")

    res = walk_forward(panel, SPEC, max_workers=a.workers)
    validity_or_exit(panel, res, a.workers)
    unc = walk_forward_uncertainty(panel, SPEC)
    print(f"evaluated {len(res.eval_weeks)} weeks; uncertainty constraint moved {unc.binding_share:.1%} of asset-weeks")
    not_conv = {k: sum(not f.converged for f in v) for k, v in res.network_fits.items()}
    print(f"network refits not converged: {not_conv}")

    print("\nPRIMARY (family of two, each at 97.5%)")
    print(line("P1 field (learned couplings) - B", res.verdict("field", "B", level=PRIMARY_LEVEL, assets=SCORED), PRIMARY_LEVEL))
    print(line("P2 quantum (learned couplings) - B", res.verdict("quantum", "B", level=PRIMARY_LEVEL, assets=SCORED), PRIMARY_LEVEL))
    print("\nSECONDARY (uncertainty relations, each at 95%)")
    print(line("S1 joint complementarity - joint free", unc.verdict("joint_complementarity", "joint_free", assets=SCORED), SECONDARY_LEVEL))
    print(line("S2 direction Christoffersen-Diebold - base rate", unc.verdict("dir_cd", "dir_base", assets=SCORED), SECONDARY_LEVEL))
    print("\nDIAGNOSTICS (95%, do not change any verdict)")
    for m, vs in (("field", "uncoupled"), ("quantum", "field"), ("quantum", "uncoupled"), ("composite", "B"),
                  ("uncoupled", "B")):
        print(line(f"{m} - {vs}", res.verdict(m, vs, assets=SCORED), 0.95))
    print(line("direction logit - base rate", unc.verdict("dir_logit", "dir_base", assets=SCORED), 0.95))
    print("\nper asset, mean log loss (regimes)")
    print("   " + f"{'':<8}" + "".join(f"{m:>11}" for m in SPEC.models))
    for i, k in enumerate(KEYS):
        print("   " + f"{k:<8}" + "".join(f"{np.nanmean(res.logloss(m)[:, i]):>11.4f}" for m in SPEC.models))
    d = res.logloss("field") - res.logloss("B")
    C = np.ma.corrcoef(np.ma.masked_invalid(d[:, list(SCORED)]).T)
    off = C[~np.eye(len(SCORED), dtype=bool)]
    print(f"\nmean cross-asset correlation of loss differences (field - B): {float(off.mean()):+.2f}")
    if res.pool_weights:
        names = [m for m in SPEC.models if m != "composite"]
        weights = {m: round(float(w), 3) for m, w in zip(names, res.pool_weights[-1], strict=True)}
        print(f"last composite weights {weights}")


if __name__ == "__main__":
    main()
