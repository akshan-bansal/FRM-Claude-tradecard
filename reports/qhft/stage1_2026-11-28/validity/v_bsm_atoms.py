"""Which BSM_Spectral outputs are valid atoms? Tested on real FRED data before any build.

An atom is valid if it is
  1. point-in-time: its value at close t is unchanged when every row after t is deleted;
  2. alive: it varies (not stuck, not near-constant, not ~always one value);
  3. not redundant: it is not a relabelling of another atom (|Spearman| < 0.85);
and we *describe*, without claiming, how much it says about the next 5 days.

Positive control: GJR volatility must rank next-week realised volatility (volatility clusters).
Negative control: a shuffled copy of every atom must show no information.

Usage: python -I v_bsm_atoms.py REPO_DIR FRED_DIR
"""
from __future__ import annotations

import math
import sys
import time

import numpy as np
from scipy import stats

REPO, FRED = sys.argv[1], sys.argv[2]
sys.path.insert(0, f"{REPO}/BSM_Spectral")
import bsm_event as be  # noqa: E402

TD = 252
H = 5


def load(name):
    return be.load_series(f"{FRED}/{name}.csv")


spx, vix = load("SP500"), load("VIXCLS")
d2, d10, eur, jpy = load("DGS2"), load("DGS10"), load("DEXUSEU"), load("DEXJPUS")
dates, (S, V, Y2, Y10, EU, JP) = be.align(spx, vix, d2, d10, eur, jpy)
print(f"aligned days: {len(dates)}  {dates[0]} .. {dates[-1]}")


def atoms(n: int) -> dict[str, np.ndarray]:
    """Every candidate atom on the first n days only. Value [t] uses closes 0..t."""
    s, v = S[:n], V[:n] / 100.0
    rS, rV = np.diff(np.log(s)), np.diff(v)
    wfS, wfV = be.walk_forward_gjr(rS, 63, 504), be.walk_forward_gjr(rV, 63, 504)
    out = {k: np.full(n, np.nan) for k in (
        "gjr_vol", "vol_premium", "tail_xi", "var99", "jet_e3_over_e2", "jet_curv_ratio",
        "spx_vix_corr", "absorption", "shock_pctile", "curve_slope", "breach_rate")}
    # cross-asset factor changes (log for prices, differences for yields and VIX)
    F = np.column_stack([rS, rV, np.diff(Y2[:n]), np.diff(Y10[:n]),
                         np.diff(np.log(EU[:n])), np.diff(np.log(JP[:n]))])
    breaches = np.full(n, np.nan)
    q_prev = np.nan
    for t in range(504 + 1, n):
        s2 = wfS.sigma2[t]            # forecast of r[t] (close t -> t+1) from r[:t]
        if not np.isfinite(s2):
            continue
        bi = int(wfS.block_of[t])
        p = wfS.params[bi]
        vol = math.sqrt(TD * s2)
        out["gjr_vol"][t] = vol
        out["vol_premium"][t] = math.log(v[t] / vol)
        # The residual window must lie wholly after the first GJR fit: a window reaching back
        # before it holds NaN variances, and the GPD law would be fitted to the NaN-free part only
        # (it silently returned xi = 0). Amendment 1 to PREREGISTRATION.md.
        win = wfS.sigma2[t - 252:t]
        if np.all(np.isfinite(win)):
            z = (rS[t - 252:t] - p.mu) / np.sqrt(win)
            law = be.SemiParamGPD(z, 0.10, None, 0.45)
            out["tail_xi"][t] = law.xi_lo
            q01 = p.mu + float(law.ppf(np.array([0.01]))[0]) * math.sqrt(s2)
            out["var99"][t] = 1.0 - math.exp(q01)
        else:
            q01 = np.nan
        # 99% breach history: was r[j] below its own VaR forecast, for j < t
        breaches[t - 1] = float(rS[t - 1] < q_prev) if np.isfinite(q_prev) else np.nan
        q_prev = q01
        w = breaches[t - 63:t]
        if np.isfinite(w).sum() >= 40:
            out["breach_rate"][t] = np.nanmean(w) - 0.01
        # 21-day ATM digital, 5-day shock covariance from the two GJR forecasts
        rho = float(np.corrcoef(rS[t - 252:t], rV[t - 252:t])[0, 1])
        out["spx_vix_corr"][t] = float(np.corrcoef(rS[t - 63:t], rV[t - 63:t])[0, 1])
        sdS = s[t] * math.sqrt(H * s2)
        sdv = math.sqrt(H * wfV.sigma2[t])
        Sig = np.array([[sdS**2, rho * sdS * sdv], [rho * sdS * sdv, sdv**2]])
        model = be.BSMDigital(strike=float(s[t]), expiry=21 / TD, skew=-0.3)
        J = be.pnl_jet(model, np.array([s[t], v[t]]), be.cov_factor(Sig), H / TD)
        e = be.energy_by_order(J)
        out["jet_e3_over_e2"][t] = math.log(e[2] / e[1])
        lam = np.linalg.eigvalsh(J.hessian())
        out["jet_curv_ratio"][t] = lam[0] / lam[1] if lam[1] != 0 else np.nan
        # correlation structure of the 6 factors, trailing 126 days, Marchenko-Pastur filtered
        X = F[t - 126:t]
        C = np.corrcoef(X, rowvar=False)
        Cf, _, _ = be.mp_filter(C, len(X))
        out["absorption"][t] = float(np.linalg.eigvalsh(Cf)[-1] / 6.0)
        zt = (F[t - 1] - X[:-1].mean(0)) / X[:-1].std(0)     # yesterday's shock vs the window before it
        Cp = np.corrcoef(X[:-1], rowvar=False)
        m2 = float(zt @ np.linalg.solve(Cp, zt))
        out["shock_pctile"][t] = stats.chi2.cdf(m2, 6)
        out["curve_slope"][t] = Y10[t] - Y2[t]
    return out


t0 = time.perf_counter()
A = atoms(len(S))
print(f"computed {len(A)} candidate atoms in {time.perf_counter() - t0:.1f}s\n")

# ---- 1. point-in-time: recompute on a truncated history, compare the overlap ----
k = len(S) - 300
At = atoms(k)
print("1. POINT-IN-TIME (recomputed with the last 300 days deleted)")
for name in A:
    a, b = A[name][:k], At[name]
    ok = np.isfinite(a) & np.isfinite(b)
    diff = float(np.max(np.abs(a[ok] - b[ok]))) if ok.any() else float("nan")
    same_nan = bool(np.array_equal(np.isfinite(a), np.isfinite(b)))
    print(f"   {name:<15} max|diff|={diff:.1e}  same coverage={same_nan}")

# ---- 2. alive ----
FIRST = int(np.argmax(np.all(np.column_stack([np.isfinite(A[k]) for k in A if k != "var99"] +
                                              [np.isfinite(A["var99"])]), axis=1)))
print(f"\nfirst day every atom is finite: index {FIRST} ({dates[FIRST]})")
print("\n2. ALIVE (coverage after warm-up, distinct values, share of the modal value)")
start = FIRST
for name, a in A.items():
    x = a[start:]
    f = x[np.isfinite(x)]
    vals, counts = np.unique(np.round(f, 10), return_counts=True)
    print(f"   {name:<15} coverage={len(f) / len(x):.2f}  distinct={len(vals):>5}  modal share={counts.max() / len(f):.2f}"
          f"  p5..p95=[{np.percentile(f, 5):+.3g}, {np.percentile(f, 95):+.3g}]")

# ---- 3. redundancy ----
names = list(A)
M = np.column_stack([A[n][start:] for n in names])
ok = np.all(np.isfinite(M), axis=1)
R = stats.spearmanr(M[ok]).statistic
print(f"\n3. REDUNDANCY (Spearman, {ok.sum()} common days). Pairs with |rho| >= 0.6:")
for i in range(len(names)):
    for j in range(i + 1, len(names)):
        if abs(R[i, j]) >= 0.6:
            print(f"   {names[i]:<15} ~ {names[j]:<15} {R[i, j]:+.2f}{'   <-- redundant' if abs(R[i, j]) >= 0.85 else ''}")
lam = np.linalg.eigvalsh(R)
print(f"   effective rank (participation ratio) = {lam.sum() ** 2 / (lam ** 2).sum():.1f} of {len(names)}")

# ---- 4. information about the next 5 days (descriptive; 22 tests, not a claim) ----
logS = np.log(S)
r = np.diff(logS)
fwd_ret = np.full(len(S), np.nan)
fwd_rv = np.full(len(S), np.nan)
for t in range(len(S) - H):
    fwd_ret[t] = logS[t + H] - logS[t]
    fwd_rv[t] = math.sqrt(np.sum(r[t:t + H] ** 2) * TD / H)
idx = np.arange(start, len(S) - H, H)   # non-overlapping weeks


def ic_ci(x, y, n_boot=2000, block=8, seed=0):
    ok = np.isfinite(x) & np.isfinite(y)
    x, y = x[ok], y[ok]
    ic = stats.spearmanr(x, y).statistic
    rng = np.random.default_rng(seed)
    n = len(x)
    boots = []
    for _ in range(n_boot):
        starts = rng.integers(0, n, n // block + 1)
        ii = (starts[:, None] + np.arange(block)).ravel()[:n] % n
        boots.append(stats.spearmanr(x[ii], y[ii]).statistic)
    lo, hi = np.percentile(boots, [2.5, 97.5])
    return ic, lo, hi, n


print(f"\n4. INFORMATION, Spearman IC with 95% block-bootstrap interval, {len(idx)} non-overlapping weeks")
print(f"   {'atom':<15} {'next-week return':>28} {'next-week realised vol':>30}")
rng = np.random.default_rng(1)
for name, a in A.items():
    x = a[idx]
    r1 = ic_ci(x, fwd_ret[idx])
    r2 = ic_ci(x, fwd_rv[idx])
    star = lambda c: "*" if c[1] > 0 or c[2] < 0 else " "
    print(f"   {name:<15} {r1[0]:+.3f} [{r1[1]:+.3f},{r1[2]:+.3f}]{star(r1)}   {r2[0]:+.3f} [{r2[1]:+.3f},{r2[2]:+.3f}]{star(r2)}")
print("   negative control (each atom shuffled):")
worst = 0.0
for name, a in A.items():
    x = rng.permutation(a[idx])
    worst = max(worst, abs(stats.spearmanr(x, fwd_rv[idx], nan_policy="omit").statistic),
                abs(stats.spearmanr(x, fwd_ret[idx], nan_policy="omit").statistic))
print(f"   largest |IC| among shuffled atoms = {worst:.3f}")

# ---- 5. the series-trust gates, on the evaluation weeks only (each call reprices 720 rays) ----
print("\n5. SERIES-TRUST GATES (risk_vector) on a sample of weeks")
s, v = S, V / 100.0
rS, rV = np.diff(np.log(s)), np.diff(v)
wfS, wfV = be.walk_forward_gjr(rS, 63, 504), be.walk_forward_gjr(rV, 63, 504)
sample = idx[::4]
gates, t0 = [], time.perf_counter()
for t in sample:
    rho = float(np.corrcoef(rS[t - 252:t], rV[t - 252:t])[0, 1])
    sdS, sdv = s[t] * math.sqrt(H * wfS.sigma2[t]), math.sqrt(H * wfV.sigma2[t])
    Sig = np.array([[sdS**2, rho * sdS * sdv], [rho * sdS * sdv, sdv**2]])
    rv = be.risk_vector(be.BSMDigital(strike=float(s[t]), expiry=21 / TD, skew=-0.3),
                        np.array([s[t], v[t]]), Sig, H / TD)
    gates.append((rv.ratio_gate, rv.repriced_gate, rv.crossing))
g = np.array(gates)
print(f"   {len(sample)} weeks, {(time.perf_counter() - t0) / len(sample) * 1e3:.0f} ms each")
print(f"   ratio gate passes {g[:, 0].mean():.0%}, repriced gate passes {g[:, 1].mean():.0%}, crossing flagged {g[:, 2].mean():.0%}")
