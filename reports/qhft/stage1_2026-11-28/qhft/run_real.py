"""The one real-data run of the pre-registered benchmark. Refuses to run if the pre-registration changed."""
from __future__ import annotations

import hashlib
import math
import runpy
import sys
from pathlib import Path

import numpy as np

import bench

HERE = Path(__file__).resolve().parent
FROZEN = "1cc0afbd9918f0c20f07c599481c556d48c0d994248509d6c7712a2184b47cd6"  # after Amendment 1
digest = hashlib.sha256((HERE / "PREREGISTRATION.md").read_bytes()).hexdigest()
if digest != FROZEN:
    sys.exit(f"PREREGISTRATION.md changed ({digest}); refusing to run")

repo, fred = sys.argv[1], sys.argv[2]
sys.argv = ["core", repo, fred]
g = runpy.run_path(str(HERE.parent / "validity" / "v_bsm_atoms_core.py"), run_name="core")
ATOMS = ["gjr_vol", "vol_premium", "tail_xi", "jet_e3_over_e2", "jet_curv_ratio",
         "spx_vix_corr", "absorption", "shock_pctile", "curve_slope", "breach_rate"]
S, A_d, dates = g["S"], g["A"], g["dates"]
r = np.diff(np.log(S))
idx = np.arange(796, len(S) - 5, 5)
A = np.column_stack([A_d[a][idx] for a in ATOMS])
y = np.array([math.sqrt(np.sum(r[t:t + 5] ** 2) * 252 / 5) for t in idx])
assert np.all(np.isfinite(A)) and np.all(np.isfinite(y)) and len(idx) == 337

res = bench.run(A, y, baseline_cols=(0, 1))
ev = res.eval_weeks
print(f"pre-registration {digest[:8]}…  weeks {len(idx)}, evaluated {len(ev)} "
      f"({dates[idx[ev[0]]]} .. {dates[idx[ev[-1]]]})")
print(f"class base rates in evaluation: " + ", ".join(
    f"{n} {np.mean(res.classes == k):.0%}" for k, n in enumerate(("Calm", "Elevated", "Stress"))))
print(f"edges per refit: min {min(map(len, res.edges_per_refit))}, max {max(map(len, res.edges_per_refit))}")

mean, lo, hi, ok = bench.verdict(res, "Q", "B")
print("\nPRIMARY  mean weekly log loss, Q minus B (nats), 95% circular block bootstrap")
print(f"   {mean:+.4f}  [{lo:+.4f}, {hi:+.4f}]   ->  {'PASS' if ok else 'FAIL'}")

print("\nDIAGNOSTICS (do not change the verdict)")
print(f"   {'model':<6} {'log loss':>9} {'Brier':>7} {'accuracy':>9}   vs B: mean [95% CI]")
print(f"   {'unif.':<6} {math.log(3):>9.4f} {2 / 3:>7.4f} {'':>9}")
for m in bench.MODELS:
    d = bench.block_bootstrap_ci(res.logloss(m) - res.logloss("B")) if m != "B" else None
    tail = f"   {d[0]:+.4f} [{d[1]:+.4f}, {d[2]:+.4f}]" if d else ""
    print(f"   {m:<6} {res.logloss(m).mean():>9.4f} {res.brier(m):>7.4f} {res.accuracy(m):>9.1%}{tail}")
q_vs_cx = bench.block_bootstrap_ci(res.logloss("Q") - res.logloss("C10x"))
q_vs_q0 = bench.block_bootstrap_ci(res.logloss("Q") - res.logloss("Q0"))
print(f"   Q - C10x (coupling vs classical products): {q_vs_cx[0]:+.4f} [{q_vs_cx[1]:+.4f}, {q_vs_cx[2]:+.4f}]")
print(f"   Q - Q0   (coupling on vs off):             {q_vs_q0[0]:+.4f} [{q_vs_q0[1]:+.4f}, {q_vs_q0[2]:+.4f}]")
print(f"\nLATENCY  Q features {res.q_eval_ms:.3f} ms/week; {len(res.edges_per_refit)} refits took {res.refit_s:.1f} s "
      f"({res.refit_s / len(res.edges_per_refit):.2f} s each, eigendecomposition of 1024x1024 H)")
np.savez(HERE / "real_run.npz", eval_weeks=ev, classes=res.classes, y=y,
         **{f"p_{m}": res.probs[m] for m in bench.MODELS})
