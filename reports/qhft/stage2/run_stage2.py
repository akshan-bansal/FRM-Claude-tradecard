"""The one stage-2 run. Refuses unless the pre-registration is frozen and unchanged, and the spec matches it.

Usage (after freeze.py and fetch_stage2.sh): ``python run_stage2.py FRED_DIR [--workers N]``
"""
from __future__ import annotations

import argparse
import hashlib
import os
import sys
from pathlib import Path

import numpy as np

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
from spec import KEYS, PRIMARY_LEVEL, SCORED, SECONDARY_LEVEL, SPEC  # noqa: E402

from trading_live_claude.quantum.atoms import load_panel  # noqa: E402
from trading_live_claude.quantum.fingerprint import of_model  # noqa: E402
from trading_live_claude.quantum.panel import walk_forward, walk_forward_uncertainty  # noqa: E402


def frozen_or_exit() -> str:
    pre, rec = HERE / "PREREGISTRATION.md", HERE / "FROZEN.sha256"
    if not (pre.exists() and rec.exists()):
        sys.exit("not frozen: run freeze.py before fetching data")
    digest = hashlib.sha256(pre.read_bytes()).hexdigest()
    recorded = rec.read_text().split()[0]
    if digest != recorded:
        sys.exit(f"PREREGISTRATION.md changed ({digest[:12]} != {recorded[:12]}); refusing to run")
    if of_model(SPEC) not in pre.read_text():
        sys.exit("spec.py no longer matches the frozen pre-registration")
    return digest


def line(name: str, v: tuple[float, float, float, bool], level: float) -> str:
    return f"   {name:<44} {v[0]:+.4f}  [{v[1]:+.4f}, {v[2]:+.4f}] at {level:.1%}  -> {'PASS' if v[3] else 'FAIL'}"


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("fred_dir")
    ap.add_argument("--workers", type=int, default=os.cpu_count() or 1)
    a = ap.parse_args()
    digest = frozen_or_exit()
    panel = load_panel(a.fred_dir, KEYS)
    print(f"pre-registration {digest[:12]}  spec {of_model(SPEC)[:12]}  panel {panel.fingerprint()[:12]}")
    print(f"weeks {len(panel.dates)} ({panel.dates[0]} .. {panel.dates[-1]}), assets {', '.join(panel.assets)}; "
          f"scored {', '.join(KEYS[i] for i in SCORED)}")

    res = walk_forward(panel, SPEC, max_workers=a.workers)
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
        print(f"last composite weights {dict(zip([m for m in SPEC.models if m != 'composite'], np.round(res.pool_weights[-1], 3), strict=True))}")


if __name__ == "__main__":
    main()
