"""Forecast checks on IBKR bars: ARIMA direction, GARCH vol, BSM_Spectral VaR breaches, cross-sectional rank."""
import math
import warnings
from pathlib import Path

import numpy as np
import pandas as pd
from scipy import stats

from trading_live_claude.models.cross_sectional import CrossSectionalRanker, build_panel
from trading_live_claude.models.timeseries import rolling_arima_forecast, rolling_garch_vol
from trading_live_claude.risk.spectral_var import estimate_tail, load_engine

warnings.filterwarnings("ignore")
BARS = Path(__file__).parent / "ibkr_bars"
OUT = Path(__file__).parent / "ibkr_out"
OUT.mkdir(exist_ok=True)
SYMS = {"SPY": "SPY", "GLD": "GLD", "TLT": "TLT", "STW.AX": "STW.AX", "BHP.AX": "BHP.AX", "ES": "ES_cont", "SPI": "SPI_cont"}
pd.set_option("display.width", 220, "display.max_columns", 30)


def load(file: str) -> pd.DataFrame:
    df = pd.read_parquet(BARS / f"{file}.parquet")
    df["high"] = df[["high", "open", "close"]].max(axis=1)
    df["low"] = df[["low", "open", "close"]].min(axis=1)
    return df


frames = {s: load(f) for s, f in SYMS.items()}

# --- one-step ARIMA direction and GARCH volatility, out of sample
ts_rows = []
for sym, df in frames.items():
    r = np.diff(np.log(df.close.to_numpy(dtype=float)))
    if len(r) < 300:
        ts_rows.append(dict(sym=sym, n=0, note=f"{len(r)} returns, below the 250-bar fit window plus a test"))
        continue
    fc = rolling_arima_forecast(r)
    vol = rolling_garch_vol(r)
    # each model's value at i is only compared with a return AFTER i, whichever way it is indexed
    pairs = [(fc[i], r[i + 1]) for i in range(len(r) - 1) if np.isfinite(fc[i])]
    f, y = np.array([p[0] for p in pairs]), np.array([p[1] for p in pairs])
    hits = int((np.sign(f) == np.sign(y)).sum())
    up_share = float((y > 0).mean())
    base = max(up_share, 1 - up_share)
    p = float(stats.binomtest(hits, len(y), base).pvalue)
    r2 = 1 - float(((y - f) ** 2).sum() / (y ** 2).sum())
    v = np.array([vol[i] for i in range(len(r) - 1) if np.isfinite(vol[i])])
    yv = np.array([r[i + 1] for i in range(len(r) - 1) if np.isfinite(vol[i])])
    hist = pd.Series(r).rolling(20).std().to_numpy()
    h = np.array([hist[i] for i in range(len(r) - 1) if np.isfinite(vol[i])])
    ok = np.isfinite(h) & (h > 0) & (v > 0)
    qlike = lambda s: float(np.mean(np.log(s[ok] ** 2) + yv[ok] ** 2 / s[ok] ** 2))  # noqa: E731
    ts_rows.append(dict(sym=sym, n=len(y), hit=hits / len(y), always_majority=base, p_vs_majority=p, r2_oos=r2,
                        qlike_garch=qlike(v), qlike_20d=qlike(h), corr_vol_absret=float(np.corrcoef(v[ok], np.abs(yv[ok]))[0, 1])))
ts = pd.DataFrame(ts_rows)
ts.to_csv(OUT / "forecast_ts.csv", index=False)
print(ts.round(4).to_string())

# --- BSM_Spectral 1-day 99% VaR: refit every 21 bars with no future data, count next-day breaches
engine = load_engine()
LEVEL, MIN_RET, REFIT = 0.99, 500, 21
var_rows = []
for sym, df in frames.items():
    px = df.close.to_numpy(dtype=float)
    if len(px) - 1 < MIN_RET:
        try:
            t = estimate_tail(px, level=LEVEL, horizon_days=1, engine=engine)
            fit = f"fit ok on {t.n_returns}, but the gate would refuse"
        except Exception as e:  # noqa: BLE001
            fit = f"fit failed: {e}"
        var_rows.append(dict(sym=sym, n_returns=len(px) - 1, gate="fails closed (<500 returns)", note=fit))
        continue
    breaches, tests, last = 0, 0, None
    for start in range(MIN_RET + 1, len(px) - 1, REFIT):
        try:
            est = estimate_tail(px[:start], level=LEVEL, horizon_days=1, engine=engine)
        except Exception:  # noqa: BLE001
            continue
        for j in range(start, min(start + REFIT, len(px) - 1)):
            # the conditional sd moves daily between refits only inside the model; hold the refit's loss figure
            tests += 1
            breaches += int(px[j + 1] / px[j] - 1 < -est.loss_fraction)
    last = estimate_tail(px, level=LEVEL, horizon_days=1, engine=engine)
    rate = breaches / tests if tests else float("nan")
    # Kupiec proportion-of-failures
    pi, x, T = 1 - LEVEL, breaches, tests
    lr = float("nan")
    if tests and 0 < x < T:
        lr = -2 * ((T - x) * math.log(1 - pi) + x * math.log(pi) - (T - x) * math.log(1 - x / T) - x * math.log(x / T))
    var_rows.append(dict(sym=sym, n_returns=len(px) - 1, tests=tests, breaches=x, rate=rate, nominal=pi,
                         kupiec_p=float(stats.chi2.sf(lr, 1)) if lr == lr else float("nan"),
                         latest_var=last.loss_fraction, sigma_next=last.sigma_next, xi_lower=last.xi_lower,
                         persistence=last.persistence))
var = pd.DataFrame(var_rows)
var.to_csv(OUT / "spectral_var.csv", index=False)
print(var.round(4).to_string())

# --- cross-sectional ranker: the five 5-year names on a shared trading date
prices = {}
for sym in ("SPY", "GLD", "TLT", "STW.AX", "BHP.AX"):
    df = frames[sym][["time", "open", "high", "low", "close", "volume"]].copy()
    if sym.endswith(".AX"):
        df["time"] = df["time"] + pd.Timedelta(days=1)   # IBKR stamps a Sydney session on the prior UTC evening
    df["time"] = df["time"].dt.normalize()
    prices[sym] = df.reset_index(drop=True)
panel = build_panel(prices, horizon=21)
print("panel rows", len(panel), "dates", panel["date"].nunique(), "full cross-sections",
      int((panel.groupby("date").size() == 5).sum()))
rk = CrossSectionalRanker()
res = rk.walk_forward(panel, horizon=21, train_min=252)
ic = res.ic_series
t = float(ic.mean() / (ic.std(ddof=1) / math.sqrt(len(ic)))) if len(ic) > 2 else float("nan")
print(f"ranker: rebalances={res.n_rebalances} mean_rank_IC={res.rank_ic:.4f} t={t:.2f} "
      f"top1_curve={res.long_curve[-1]:.3f} equal_weight_curve={res.bench_curve[-1]:.3f}")
latest = rk.fit_latest(panel, horizon=21)
print("ranker latest predicted 21d relative return:", {k: round(v, 4) for k, v in latest.items()})
pd.DataFrame([dict(rebalances=res.n_rebalances, rank_ic=res.rank_ic, t=t, top1=res.long_curve[-1],
                   equal_weight=res.bench_curve[-1], **{f"latest_{k}": v for k, v in latest.items()})]).to_csv(OUT / "ranker.csv", index=False)
