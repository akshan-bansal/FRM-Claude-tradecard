"""Futures on IBKR connector bars: backtest, walk-forward, forecasts, VaR, Router replay. Orders nothing real."""
import math
import warnings
from multiprocessing import Pool
from pathlib import Path

import numpy as np
import pandas as pd
from scipy import stats

import ibkr_sim
from sweep_universe import best_config, walk_forward
from trading_live_claude.models.timeseries import rolling_arima_forecast, rolling_garch_vol
from trading_live_claude.risk.spectral_var import estimate_tail, load_engine

warnings.filterwarnings("ignore")
BARS = Path(__file__).parent / "ibkr_bars"
OUT = Path(__file__).parent / "ibkr_out"
OUT.mkdir(exist_ok=True)
pd.set_option("display.width", 230, "display.max_columns", 30, "display.max_colwidth", 60)
ROOTS = ["ES", "ZN", "GC", "CL"]
TRAIN, TEST, VAR_MIN = 252, 63, 250
# label, root, multiplier: full contract, then the micro where one exists (config/futures_universe.json for GC, CL)
BOOKS = [("ES x50", "ES", 50), ("ES x5 micro-size", "ES", 5), ("ZN x1000", "ZN", 1000),
         ("GC x100", "GC", 100), ("GC x10 micro-size", "GC", 10), ("CL x1000", "CL", 1000), ("CL x100 micro-size", "CL", 100)]


def load(root: str) -> pd.DataFrame:
    df = pd.read_parquet(BARS / f"{root}_cont.parquet")[["time", "open", "high", "low", "close", "volume"]].copy()
    df["high"] = df[["high", "open", "close"]].max(axis=1)
    df["low"] = df[["low", "open", "close"]].min(axis=1)
    return df.reset_index(drop=True)


def var_check(root: str) -> dict:
    warnings.filterwarnings("ignore")
    px = load(root).close.to_numpy(dtype=float)
    engine = load_engine()
    x, T = 0, 0
    for j in range(VAR_MIN + 1, len(px) - 1):
        try:
            est = estimate_tail(px[:j + 1], level=0.99, horizon_days=1, engine=engine)
        except Exception:  # noqa: BLE001
            continue
        T += 1
        x += int(px[j + 1] / px[j] - 1 < -est.loss_fraction)
    lr = -2 * ((T - x) * math.log(0.99) + x * math.log(0.01) - (T - x) * math.log(1 - x / T) - x * math.log(x / T)) if 0 < x < T else float("nan")
    last = estimate_tail(px, level=0.99, horizon_days=1, engine=engine)
    return dict(root=root, returns=len(px) - 1, tests=T, breaches=x, rate=x / T if T else float("nan"),
                kupiec_p=float(stats.chi2.sf(lr, 1)) if lr == lr else float("nan"), latest_var=last.loss_fraction,
                gate_ready=len(px) - 1 >= 500)


def main() -> None:
    bt, wf, ts = [], [], []
    for root in ROOTS:
        df, sym = load(root), f"/{root}"
        s, name, params, m = best_config(df, sym)
        bt.append(dict(root=root, bars=len(df), years=round(len(df) / 252, 2), buy_hold=df.close.iloc[-1] / df.close.iloc[0] - 1,
                       strategy=name, params=str(params), ret=m.total_return, sharpe=m.sharpe, maxdd=m.max_drawdown, trades=m.num_trades))
        protocol = walk_forward(df, sym, asset_class="future")
        short = walk_forward(df, sym, train=TRAIN, test=TEST, step=TEST, asset_class="future")
        folds, start = [], 0
        while start + TRAIN + TEST <= len(df):
            folds.append(df.close.iloc[start + TRAIN + TEST - 1] / df.close.iloc[start + TRAIN - 1] - 1)
            start += TEST
        wf.append(dict(root=root, protocol_folds=(protocol or {}).get("folds", 0), **(short or dict(folds=0)),
                       buy_hold_per_fold=float(np.mean(folds)) if folds else float("nan")))
        r = np.diff(np.log(df.close.to_numpy(dtype=float)))
        fc, vol = rolling_arima_forecast(r), rolling_garch_vol(r)
        pairs = [(fc[i], r[i + 1]) for i in range(len(r) - 1) if np.isfinite(fc[i])]
        f, y = np.array([p[0] for p in pairs]), np.array([p[1] for p in pairs])
        hits = int((np.sign(f) == np.sign(y)).sum())
        base = max(float((y > 0).mean()), 1 - float((y > 0).mean()))
        idx = [i for i in range(len(r) - 1) if np.isfinite(vol[i])]
        v, yv, h = vol[idx], r[np.array(idx) + 1], pd.Series(r).rolling(20).std().to_numpy()[idx]
        ok = np.isfinite(h) & (h > 0) & (v > 0)
        q = lambda a: float(np.mean(np.log(a[ok] ** 2) + yv[ok] ** 2 / a[ok] ** 2))  # noqa: E731
        ts.append(dict(root=root, n=len(y), hit=hits / len(y), majority=base, p=float(stats.binomtest(hits, len(y), base).pvalue),
                       r2=1 - float(((y - f) ** 2).sum() / (y ** 2).sum()), qlike_garch=q(v), qlike_20d=q(h)))
        print(root, "done", flush=True)
    B, W, T = pd.DataFrame(bt), pd.DataFrame(wf), pd.DataFrame(ts)
    B.to_csv(OUT / "fut_backtest.csv", index=False)
    W.to_csv(OUT / "fut_walkforward.csv", index=False)
    T.to_csv(OUT / "fut_forecast.csv", index=False)
    print("=== BACKTEST\n" + B.drop(columns=["params"]).round(3).to_string())
    print("=== WALK\n" + W.drop(columns=["asset_class", "train_bars", "test_bars"], errors="ignore").round(3).to_string())
    print("=== FORECAST\n" + T.round(4).to_string())

    with Pool(4) as pool:
        V = pd.DataFrame(pool.map(var_check, ROOTS))
    V.to_csv(OUT / "fut_var.csv", index=False)
    print("=== VAR\n" + V.round(4).to_string())

    ibkr_sim.SCRATCH.mkdir(parents=True, exist_ok=True)
    rows = []
    for label, root, mult in BOOKS:
        df = load(root)
        _, sname, params, _ = best_config(df.iloc[:TRAIN].reset_index(drop=True), f"/{root}")
        for cap, floor in ((None, 500), (ibkr_sim.CAP, 500), (ibkr_sim.CAP, VAR_MIN)):
            row = ibkr_sim.replay(label, df, mult, sname, params, TRAIN, cap, min_returns=floor)
            row["gate"] = "off" if cap is None else f"on,{floor}"
            rows.append(row)
    R = pd.DataFrame(rows)
    R.to_csv(OUT / "fut_sim.csv", index=False)
    print("=== REJECTS")
    for _, x in R.iterrows():
        print(x.book, x.gate, x.rejects[:220])
    print("=== SIM\n" + R.drop(columns=["rejects", "gate_loaded"]).round(3).to_string())


if __name__ == "__main__":
    main()
