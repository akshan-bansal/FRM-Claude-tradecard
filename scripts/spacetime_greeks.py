"""Greeks as a space-time jet under data-backed vol dynamics: checks and walk-forward evidence. Research only.

Four parts, each written to the output folder:
  1. pde.csv          how far the engine's moneyness smile is from the Black-Scholes PDE
  2. dynamics.csv     random walk, log-OU and 3/2 fits on each vol index, against the data's own statistics
  3. expansion.csv    the Ito-Taylor horizon moments against Monte Carlo with exact repricing
  4. walkforward.csv  forecasts of a digital's h-day P&L under each dynamics, scored on what happened

The P&L in part 4 is marked to model on the realised (spot, vol index) path, as in the engine's own backtest:
there is no digital price history. It measures which state dynamics forecast the model's P&L, not market P&L.

Run:  python scripts/spacetime_greeks.py --data BSM_Spectral/data --out reports/spacetime_greeks_2026-11-06
"""
from __future__ import annotations

import argparse
import math
import sys
import warnings
from pathlib import Path

import numpy as np
import pandas as pd
from scipy import stats

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "BSM_Spectral"))
import bsm_event as be
import bsm_spacetime as st

warnings.filterwarnings("ignore")
KEYS = ("SPX", "NDX", "DJIA", "WTI")
KINDS = ("random_walk", "log_ou", "three_halves")
DT = 1 / 252
TENOR = 63 / 252
H_DAYS = (1, 5, 10)
SEED = 20261106


def digital_for(key: str, strike: float, tau: float = TENOR) -> be.BSMDigital:
    u = be.UNDERLYINGS[key]
    return be.BSMDigital(strike=strike, expiry=tau, rate=u["rate"], dividend=u["dividend"], skew=u["skew"])


def pde_table() -> pd.DataFrame:
    rows = []
    for skew in (0.0, -0.3):
        m = be.BSMDigital(strike=100.0, expiry=TENOR, skew=skew)
        for S in (85.0, 95.0, 100.0, 105.0, 115.0):
            res = st.ito_residual(m, S, 0.2, TENOR)
            rows.append(dict(skew=skew, spot=S, price=m.price(S, 0.2), residual_per_year=res,
                             residual_per_day_pct_of_price=100 * res / 252 / m.price(S, 0.2)))
    return pd.DataFrame(rows)


def dynamics_table(data: Path) -> pd.DataFrame:
    rows = []
    for key in KEYS:
        _, S, V = be.load_underlying(str(data), key)
        v = V / 100.0
        base = dict(underlying=key, days=len(v), data_vol_of_vol_exponent=st.vol_of_vol_exponent(v),
                    data_vr21=st.variance_ratio(v, 21))
        for kind in KINDS:
            dyn, fit = st.fit_dynamics(S, v, DT, kind)
            b = fit["persistence"]
            rows.append(base | dict(kind=kind, aic=6 - 2 * fit["loglik"],
                                    half_life_days=math.log(2) / -math.log(b) if b < 1 else math.inf,
                                    model_vr21=(1 - b ** 21) / (21 * (1 - b)) if b < 1 else 1.0,
                                    kappa=dyn.kappa, theta=dyn.theta, eta=dyn.eta, rho=dyn.rho, scale=dyn.scale))
    return pd.DataFrame(rows)


def expansion_table(data: Path) -> pd.DataFrame:
    rows = []
    for key in KEYS:
        _, S, V = be.load_underlying(str(data), key)
        v = V / 100.0
        s0, v0 = float(S[-1]), float(v[-1])
        model = digital_for(key, strike=s0)
        for kind in ("random_walk", "log_ou"):
            dyn, _ = st.fit_dynamics(S, v, DT, kind)
            for hd in H_DAYS:
                h = hd * DT
                ito = st.horizon_moments(model, s0, v0, TENOR, dyn, h)
                mc = st.monte_carlo_moments(model, s0, v0, TENOR, dyn, h, n_paths=200000, n_steps=max(hd * 4, 8), seed=SEED)
                rows.append(dict(underlying=key, kind=kind, h_days=hd, ito_mean=ito["mean"], mc_mean=mc["mean"],
                                 mc_mean_se=mc["se_mean"], ito_sd=ito["sd"], mc_sd=mc["sd"],
                                 sd_error_pct=100 * (ito["sd"] / mc["sd"] - 1), mc_q01=mc["q01"]))
    return pd.DataFrame(rows)


def walkforward_table(data: Path, h_days: int, window: int, refit: int) -> tuple[pd.DataFrame, pd.DataFrame]:
    h = h_days * DT
    z99 = stats.norm.ppf(0.01)
    lines = []
    for key in KEYS:
        _, S, V = be.load_underlying(str(data), key)
        S, v = np.asarray(S, dtype=float), np.asarray(V, dtype=float) / 100.0
        fitted: dict[str, st.StateDynamics] = {}
        for t in range(window, len(S) - h_days, h_days):
            if (t - window) % refit < h_days or not fitted:
                fitted = {k: st.fit_dynamics(S[t - window:t + 1], v[t - window:t + 1], DT, k)[0] for k in KINDS}
            model = digital_for(key, strike=float(S[t]))
            realised = model.price(S[t + h_days], max(v[t + h_days], be.STATE_FLOOR), TENOR - h) - model.price(S[t], v[t], TENOR)
            for kind, dyn in fitted.items():
                m = st.horizon_moments(model, float(S[t]), float(v[t]), TENOR, dyn, h)
                if not m["sd"] > 0:
                    continue
                q = m["mean"] + z99 * m["sd"]
                lines.append(dict(underlying=key, kind=kind, t=t, realised=realised, mean=m["mean"], sd=m["sd"],
                                  z=(realised - m["mean"]) / m["sd"], breach=realised < q,
                                  pinball=float(be.pinball(np.array([-realised]), np.array([-q]), 0.99)[0])))
    L = pd.DataFrame(lines)
    g = L.groupby(["underlying", "kind"])
    summary = pd.DataFrame({"forecasts": g.size(), "mean_z2": g.z.apply(lambda z: float(np.mean(z * z))),
                            "breach_rate_1pct": g.breach.mean(), "pinball_x1e4": g.pinball.mean() * 1e4}).reset_index()
    return summary, L


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--data", type=Path, default=Path("BSM_Spectral/data"))
    ap.add_argument("--out", type=Path, required=True)
    ap.add_argument("--h-days", type=int, default=5)
    ap.add_argument("--window", type=int, default=504)
    ap.add_argument("--refit", type=int, default=63)
    args = ap.parse_args()
    args.out.mkdir(parents=True, exist_ok=True)
    pd.set_option("display.width", 220, "display.max_columns", 30)

    pde = pde_table()
    pde.to_csv(args.out / "pde.csv", index=False)
    print("=== PDE RESIDUAL\n" + pde.round(6).to_string(index=False), flush=True)
    dyn = dynamics_table(args.data)
    dyn.to_csv(args.out / "dynamics.csv", index=False)
    print("=== DYNAMICS\n" + dyn.round(3).to_string(index=False), flush=True)
    exp = expansion_table(args.data)
    exp.to_csv(args.out / "expansion.csv", index=False)
    print("=== EXPANSION VS MONTE CARLO\n" + exp.round(6).to_string(index=False), flush=True)
    summary, lines = walkforward_table(args.data, args.h_days, args.window, args.refit)
    summary.to_csv(args.out / "walkforward.csv", index=False)
    lines.to_csv(args.out / "walkforward_lines.csv", index=False)
    print("=== WALK-FORWARD\n" + summary.round(4).to_string(index=False), flush=True)


if __name__ == "__main__":
    main()
