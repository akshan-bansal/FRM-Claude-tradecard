"""Point-in-time BSM_Spectral atoms per underlying, and the weekly panel built from them.

These are the ten atoms the stage-1 record validated on the S&P (``reports/qhft/stage1_2026-11-28``).
All passed a truncation test there and pass it here.

- ``gjr_vol``: annualised one-step GJR-GARCH forecast (``bsm_event.walk_forward_gjr``).
- ``vol_premium``: log of the vol index (or GJR stand-in) over ``gjr_vol``.
- ``tail_xi``: lower-tail GPD shape of the last 252 standardised residuals (``SemiParamGPD``).
- ``jet_e3_over_e2``, ``jet_curv_ratio``: third-to-second order P&L energy and the curvature
  eigenvalue ratio of a 21-day ATM digital's jet (``pnl_jet``, ``energy_by_order``).
- ``spot_vol_corr``: 63-day correlation of spot returns and vol-index changes.
- ``absorption``, ``shock_pctile``: top Marchenko-Pastur-filtered eigenvalue share of the cross-asset
  factors (``mp_filter``), and the chi-square percentile of yesterday's factor shock.
- ``curve_slope``: 10y minus 2y Treasury yield.
- ``breach_rate``: trailing 63-day 99% VaR breach rate minus 1%.

The residual-window atoms exist only once their whole window lies after the first GJR fit. Stage 1
found that a window reaching back earlier silently gave ``tail_xi = 0`` (its Amendment 1).
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from pathlib import Path
from types import ModuleType
from typing import Any

import numpy as np
import numpy.typing as npt
from scipy import stats

from ..risk.spectral_var import load_engine
from .panel import Panel

FloatArray = npt.NDArray[np.float64]

ATOM_NAMES: tuple[str, ...] = ("gjr_vol", "vol_premium", "tail_xi", "jet_e3_over_e2", "jet_curv_ratio",
                               "spot_vol_corr", "absorption", "shock_pctile", "curve_slope", "breach_rate")
MACRO: tuple[str, ...] = ("DGS2", "DGS10", "DEXUSEU", "DEXJPUS")
OWN_MACRO = {"EURUSD": "DEXUSEU", "USDJPY": "DEXJPUS"}   # never feed an asset its own series twice
TD = 252
HORIZON = 5
MIN_TRAIN = 504
WINDOW = 252


@dataclass(frozen=True)
class Aligned:
    key: str
    dates: npt.NDArray[Any]
    S: FloatArray
    V: FloatArray          # vol index in percentage points, or BSM's GJR stand-in
    macro: dict[str, FloatArray]
    rate: float
    dividend: float
    skew: float

    def truncate(self, n: int) -> Aligned:
        return Aligned(self.key, self.dates[:n], self.S[:n], self.V[:n],
                       {k: v[:n] for k, v in self.macro.items()}, self.rate, self.dividend, self.skew)


def load_aligned(data_dir: str | Path, key: str, engine: ModuleType | None = None) -> Aligned:
    """One underlying from FRED-style CSVs (``bsm_event.load_underlying``), aligned with the macro series."""
    be = engine or load_engine()
    u = be.UNDERLYINGS[key]
    dates, S, V = be.load_underlying(str(data_dir), key)
    macro = [be.load_series(be.find_file(str(data_dir), (f"{m}.csv",))) for m in MACRO]
    common, vals = be.align((dates, S), (dates, V), *macro)
    return Aligned(key, common, np.asarray(vals[0], float), np.asarray(vals[1], float),
                   {m: np.asarray(v, float) for m, v in zip(MACRO, vals[2:], strict=True)},
                   float(u["rate"]), float(u["dividend"]), float(u["skew"]))


def asset_atoms(al: Aligned, engine: ModuleType | None = None) -> dict[str, FloatArray]:
    """Every atom for one underlying. Value ``[t]`` uses closes ``0..t`` only."""
    be = engine or load_engine()
    n = len(al.S)
    s, v = al.S, al.V / 100.0
    out = {k: np.full(n, np.nan) for k in ATOM_NAMES}
    if n < MIN_TRAIN + 2:
        return out
    rS, rV = np.diff(np.log(s)), np.diff(v)
    wfS, wfV = be.walk_forward_gjr(rS, 63, MIN_TRAIN), be.walk_forward_gjr(rV, 63, MIN_TRAIN)
    own = OWN_MACRO.get(al.key)
    cols = [rS, rV, np.diff(al.macro["DGS2"]), np.diff(al.macro["DGS10"])]
    cols += [np.diff(np.log(al.macro[m])) for m in ("DEXUSEU", "DEXJPUS") if m != own]
    F = np.column_stack(cols)
    nf = F.shape[1]
    breaches = np.full(n, np.nan)
    q_prev = np.nan
    for t in range(MIN_TRAIN + 1, n):
        s2 = wfS.sigma2[t]
        if not np.isfinite(s2):
            continue
        p = wfS.params[int(wfS.block_of[t])]
        vol = math.sqrt(TD * s2)
        out["gjr_vol"][t] = vol
        out["vol_premium"][t] = math.log(v[t] / vol) if v[t] > 0 else np.nan
        win = wfS.sigma2[t - WINDOW:t]
        if t >= WINDOW and np.all(np.isfinite(win)):
            law = be.SemiParamGPD((rS[t - WINDOW:t] - p.mu) / np.sqrt(win), 0.10, None, 0.45)
            out["tail_xi"][t] = law.xi_lo
            q01 = p.mu + float(law.ppf(np.array([0.01]))[0]) * math.sqrt(s2)
        else:
            q01 = np.nan
        breaches[t - 1] = float(rS[t - 1] < q_prev) if np.isfinite(q_prev) else np.nan
        q_prev = q01
        w63 = breaches[t - 63:t]
        if np.isfinite(w63).sum() >= 40:
            out["breach_rate"][t] = float(np.nanmean(w63)) - 0.01
        rho = float(np.corrcoef(rS[t - WINDOW:t], rV[t - WINDOW:t])[0, 1])
        out["spot_vol_corr"][t] = float(np.corrcoef(rS[t - 63:t], rV[t - 63:t])[0, 1])
        sdS, sdv = s[t] * math.sqrt(HORIZON * s2), math.sqrt(HORIZON * wfV.sigma2[t])
        if np.isfinite(sdv) and np.isfinite(rho):
            Sig = np.array([[sdS**2, rho * sdS * sdv], [rho * sdS * sdv, sdv**2]])
            model = be.BSMDigital(strike=float(s[t]), expiry=21 / TD, skew=al.skew, rate=al.rate,
                                  dividend=al.dividend)
            J = be.pnl_jet(model, np.array([s[t], v[t]]), be.cov_factor(Sig), HORIZON / TD)
            e = be.energy_by_order(J)
            if e[1] > 0 and e[2] > 0:
                out["jet_e3_over_e2"][t] = math.log(e[2] / e[1])
            lam = np.linalg.eigvalsh(J.hessian())
            out["jet_curv_ratio"][t] = lam[0] / lam[1] if lam[1] != 0 else np.nan
        X = F[t - 126:t]
        Cf, _, _ = be.mp_filter(np.corrcoef(X, rowvar=False), len(X))
        out["absorption"][t] = float(np.linalg.eigvalsh(Cf)[-1] / nf)
        sd = X[:-1].std(0)
        if np.all(sd > 0):
            zt = (F[t - 1] - X[:-1].mean(0)) / sd
            m2 = float(zt @ np.linalg.solve(np.corrcoef(X[:-1], rowvar=False), zt))
            out["shock_pctile"][t] = float(stats.chi2.cdf(m2, nf))
        out["curve_slope"][t] = float(al.macro["DGS10"][t] - al.macro["DGS2"][t])
    return out


def load_panel(data_dir: str | Path, keys: tuple[str, ...], engine: ModuleType | None = None) -> Panel:
    """Weekly panel over the common calendar.

    - Decision days are every 5th common day, from the first day on which every asset has every atom.
    - Labels are each asset's own returns between consecutive decision days.
    """
    be = engine or load_engine()
    aligned = [load_aligned(data_dir, k, be) for k in keys]
    atoms = [asset_atoms(a, be) for a in aligned]
    common = aligned[0].dates
    for a in aligned[1:]:
        common = np.intersect1d(common, a.dates)
    pos = [np.searchsorted(a.dates, common) for a in aligned]
    ready = np.ones(len(common), dtype=bool)
    for A, p in zip(atoms, pos, strict=True):
        ready &= np.all(np.column_stack([np.isfinite(A[k][p]) for k in ATOM_NAMES]), axis=1)
    if not ready.any():
        raise ValueError("no common day on which every asset has every atom")
    grid = np.arange(int(np.argmax(ready)), len(common), HORIZON)
    W, N, K = len(grid), len(keys), len(ATOM_NAMES)
    X = np.full((W, N, K), np.nan)
    rv, ret = np.full((W, N), np.nan), np.full((W, N), np.nan)
    for i, (a, A, p) in enumerate(zip(aligned, atoms, pos, strict=True)):
        idx = p[grid]
        for k, name in enumerate(ATOM_NAMES):
            X[:, i, k] = A[name][idx]
        logS = np.log(a.S)
        for w in range(W - 1):
            r = np.diff(logS[idx[w]:idx[w + 1] + 1])
            rv[w, i] = math.sqrt(float(np.sum(r * r)) * TD / HORIZON)
            ret[w, i] = float(logS[idx[w + 1]] - logS[idx[w]])
    return Panel(tuple(keys), ATOM_NAMES, tuple(str(d) for d in common[grid]), X, rv, ret)
