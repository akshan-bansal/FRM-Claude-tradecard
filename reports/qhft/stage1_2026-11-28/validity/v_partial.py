"""Incremental information: each atom's IC with next-week realised vol after removing GJR vol."""
import math, runpy, sys
import numpy as np
from scipy import stats
sys.argv = [sys.argv[0], sys.argv[1], sys.argv[2], sys.argv[3]]
g = runpy.run_path(sys.argv[3], run_name="core")
A, S = g["A"], g["S"]
H, TD = 5, 252
r = np.diff(np.log(S))
fwd_rv = np.full(len(S), np.nan)
for t in range(len(S) - H):
    fwd_rv[t] = math.sqrt(np.sum(r[t:t + H] ** 2) * TD / H)
idx = np.arange(796, len(S) - H, H)  # Amendment 1: first day all atoms are finite
def resid(y, c):
    ry, rc = stats.rankdata(y), stats.rankdata(c)
    b = np.polyfit(rc, ry, 1)
    return ry - np.polyval(b, rc)
c = A["gjr_vol"][idx]
y = fwd_rv[idx]
print(f"partial Spearman IC with next-week realised vol, controlling for gjr_vol ({len(idx)} weeks)")
rng = np.random.default_rng(0)
for name, a in A.items():
    if name == "gjr_vol":
        continue
    x = a[idx]
    ok = np.isfinite(x) & np.isfinite(y) & np.isfinite(c)
    ex, ey = resid(x[ok], c[ok]), resid(y[ok], c[ok])
    ic = np.corrcoef(ex, ey)[0, 1]
    n, B = ok.sum(), 8
    boots = []
    for _ in range(2000):
        ii = (rng.integers(0, n, n // B + 1)[:, None] + np.arange(B)).ravel()[:n] % n
        boots.append(np.corrcoef(ex[ii], ey[ii])[0, 1])
    lo, hi = np.percentile(boots, [2.5, 97.5])
    flag = "*" if lo > 0 or hi < 0 else " "
    print(f"   {name:<15} {ic:+.3f} [{lo:+.3f},{hi:+.3f}]{flag}")
