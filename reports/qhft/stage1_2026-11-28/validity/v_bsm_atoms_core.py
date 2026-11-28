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

