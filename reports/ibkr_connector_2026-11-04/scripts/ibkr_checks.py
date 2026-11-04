"""Two corrections to the first forecast pass: VaR refit daily as the gate does, ranker on full cross-sections."""
import math
import warnings
from multiprocessing import Pool
from pathlib import Path

import numpy as np
import pandas as pd
from scipy import stats

from trading_live_claude.models.cross_sectional import CrossSectionalRanker, build_panel
from trading_live_claude.risk.spectral_var import estimate_tail, load_engine

warnings.filterwarnings("ignore")
BARS = Path(__file__).parent / "ibkr_bars"
OUT = Path(__file__).parent / "ibkr_out"
SYMS = {"SPY": "SPY", "GLD": "GLD", "TLT": "TLT", "STW.AX": "STW.AX", "BHP.AX": "BHP.AX", "ES": "ES_cont"}
LEVEL, MIN_RET = 0.99, 500


def var_daily(item: tuple[str, str]) -> dict:
    sym, file = item
    warnings.filterwarnings("ignore")
    px = pd.read_parquet(BARS / f"{file}.parquet").close.to_numpy(dtype=float)
    engine = load_engine()
    breaches, tests, failed = 0, 0, 0
    for j in range(MIN_RET + 1, len(px) - 1):
        try:
            est = estimate_tail(px[:j + 1], level=LEVEL, horizon_days=1, engine=engine)   # closes through day j
        except Exception:  # noqa: BLE001
            failed += 1
            continue
        tests += 1
        breaches += int(px[j + 1] / px[j] - 1 < -est.loss_fraction)     # judged on day j+1
    pi, x, T = 1 - LEVEL, breaches, tests
    lr = -2 * ((T - x) * math.log(1 - pi) + x * math.log(pi) - (T - x) * math.log(1 - x / T) - x * math.log(x / T)) if 0 < x < T else float("nan")
    return dict(sym=sym, tests=T, failed_fits=failed, breaches=x, rate=x / T if T else float("nan"),
                kupiec_p=float(stats.chi2.sf(lr, 1)) if lr == lr else float("nan"))


def ranker() -> None:
    prices = {}
    for sym in ("SPY", "GLD", "TLT", "STW.AX", "BHP.AX"):
        df = pd.read_parquet(BARS / f"{sym}.parquet")[["time", "open", "high", "low", "close", "volume"]].copy()
        if sym.endswith(".AX"):
            df["time"] = df["time"] + pd.Timedelta(days=1)
        df["time"] = df["time"].dt.normalize()
        prices[sym] = df.reset_index(drop=True)
    panel = build_panel(prices, horizon=21)
    counts = panel.groupby("date")["symbol"].transform("size") if "symbol" in panel else panel.groupby("date")["date"].transform("size")
    full = panel[counts == 5]
    rk = CrossSectionalRanker()
    res = rk.walk_forward(full, horizon=21, train_min=252)
    ic = res.ic_series
    print(f"ranker full-section: dates={full['date'].nunique()} last={full['date'].max()} rebalances={res.n_rebalances} "
          f"IC={res.rank_ic:.4f} t={ic.mean() / (ic.std(ddof=1) / math.sqrt(len(ic))):.2f} "
          f"IC>0 share={float((ic > 0).mean()):.2f} top1={res.long_curve[-1]:.3f} equal={res.bench_curve[-1]:.3f}")
    print("latest predicted 21d relative:", {k: round(v, 4) for k, v in rk.fit_latest(full, horizon=21).items()})


if __name__ == "__main__":
    ranker()
    with Pool(6) as pool:
        rows = pool.map(var_daily, list(SYMS.items()))
    v = pd.DataFrame(rows)
    v.to_csv(OUT / "spectral_var_daily.csv", index=False)
    print(v.round(4).to_string())
    print("pooled:", int(v.breaches.sum()), "/", int(v.tests.sum()), round(v.breaches.sum() / v.tests.sum(), 4))
    _ = np
