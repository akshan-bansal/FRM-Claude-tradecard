"""Where the tensor pipeline loses signal, on the saved IBKR bars. Read-only.

Run from the repo root:  python reports/eigen_vs_tensor_2026-11-04/signal_loss.py
The bars are not in the repo; they are expected in reports/ibkr_connector_2026-11-04/bars/.
"""
import sys
import warnings
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "BSM_Spectral"))
sys.path.insert(0, str(ROOT / "scripts"))
import bsm_eigentensor as et  # noqa: E402
import eigen_vs_tensor as ev  # noqa: E402

warnings.filterwarnings("ignore")
bars = ROOT / "reports" / "ibkr_connector_2026-11-04" / "bars"

# 1. rows lost when five calendars are joined
counts = {s: len(pd.read_parquet(bars / f"{s}.parquet")) for s in ev.SYMBOLS}
daily = ev.load_returns(bars)
print("bars per symbol", counts, "| joined returns", len(daily),
      f"| lost vs longest {1 - (len(daily) + 1) / max(counts.values()):.1%}")

# 2. same-date against aligned closes, per US-Sydney pair
aligned = ev.align_closes(daily)
for us in ("SPY", "GLD", "TLT"):
    for au in ("STW.AX", "BHP.AX"):
        print(f"{us}-{au} same-date {daily[us].corr(daily[au]):+.2f} aligned {aligned[us].corr(aligned[au]):+.2f}")

# 3. what rank truncation keeps of each tensor, real data against elliptical noise of the same tails
Z = et.devolatilize(daily.to_numpy())[0]
x = et.whiten(Z)
S, (K, kappa) = et.coskewness(x), et.excess_cokurtosis(x)


def kept(A, r):
    return float(np.linalg.norm(et.low_rank(A, r, **ev.SOLVER)) / np.linalg.norm(A))


rng = np.random.default_rng(7)
null = {"coskew": [], "cokurt": []}
for _ in range(40):
    n = et.elliptical_sample(rng, len(x), x.shape[1], kappa)
    null["coskew"].append([kept(et.coskewness(n), r) for r in (1, 2)])
    null["cokurt"].append([kept(et.excess_cokurtosis(n)[0], r) for r in (1, 2)])
for name, A in (("coskew", S), ("cokurt", K)):
    real = [kept(A, r) for r in (1, 2)]
    nm = np.mean(null[name], axis=0)
    print(f"{name}: rank-1 keeps {real[0]:.1%} (noise {nm[0]:.1%}), rank-2 keeps {real[1]:.1%} (noise {nm[1]:.1%})")
