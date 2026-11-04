"""Per fold: what walk_forward picked, how it scored cold (as the repo does) and warmed on the train bars."""
import sys
import warnings
from pathlib import Path

import numpy as np
import pandas as pd

REPO = Path(r"C:\Users\PC\OneDrive\Desktop\Components\FRM-Claude-tradecard")
sys.path.insert(0, str(REPO / "scripts"))
from sweep_universe import PARAM_GRIDS, combos, is_etf_like, score_run  # noqa: E402

from trading_live_claude.analysis.universe import min_oos_trades, wf_protocol  # noqa: E402
from trading_live_claude.backtest import BacktestEngine  # noqa: E402
from trading_live_claude.backtest.costs import CostModel  # noqa: E402
from trading_live_claude.intel.routing import classify_symbol  # noqa: E402
from trading_live_claude.strategies import STRATEGIES  # noqa: E402

warnings.filterwarnings("ignore")
BARS = Path(__file__).parent / "ibkr_bars"
OUT = Path(__file__).parent / "ibkr_out"
pd.set_option("display.width", 220, "display.max_columns", 30, "display.max_colwidth", 50)
SYMS = {"SPY": ("SPY", None), "GLD": ("GLD", None), "TLT": ("TLT", None), "STW.AX": ("STW.AX", None),
        "BHP.AX": ("BHP.AX", None), "ES": ("ES_cont", (252, 63, 63))}
print("grid size:", sum(len(combos(g)) for g in PARAM_GRIDS.values()), "configs over", len(PARAM_GRIDS), "strategies")

folds, summary = [], []
for sym, (file, window) in SYMS.items():
    df = pd.read_parquet(BARS / f"{file}.parquet")[["time", "open", "high", "low", "close", "volume"]].copy()
    df["high"] = df[["high", "open", "close"]].max(axis=1)
    df["low"] = df[["low", "open", "close"]].min(axis=1)
    df = df.reset_index(drop=True)
    cls = classify_symbol(sym)
    p = wf_protocol(cls)
    tr_n, te_n, st_n = window or (p.train_bars, p.test_bars, p.step_bars)
    eng = BacktestEngine(cost_model=CostModel.from_price(float(df.close.iloc[-1]), is_etf=is_etf_like(sym)))
    start, chain_warm, chain_bh = 0, [], []
    while start + tr_n + te_n <= len(df):
        tr = df.iloc[start:start + tr_n].reset_index(drop=True)
        te = df.iloc[start + tr_n:start + tr_n + te_n].reset_index(drop=True)
        both = df.iloc[start:start + tr_n + te_n].reset_index(drop=True)
        bt = None
        for sname, grid in PARAM_GRIDS.items():
            for params in combos(grid):
                got = score_run(eng, sname, params, tr, sym)
                if got and (bt is None or got[0] > bt[0]):
                    bt = (got[0], sname, params)
        cold = eng.run(STRATEGIES[bt[1]](**bt[2]), te, sym)
        warm = eng.run(STRATEGIES[bt[1]](**bt[2]), both, sym)
        curve = np.asarray(warm.equity_curve, dtype=float)
        warm_ret = float(curve[-1] / curve[tr_n - 1] - 1)
        pos = np.asarray(warm.positions, dtype=float)[tr_n:]
        bh = float(te.close.iloc[-1] / df.close.iloc[start + tr_n - 1] - 1)
        chain_warm.append(warm_ret)
        chain_bh.append(bh)
        folds.append(dict(sym=sym, test_start=te.time.iloc[0].date(), pick=bt[1], params=str(bt[2]),
                          cold_ret=cold.metrics.total_return, cold_trades=cold.metrics.num_trades,
                          warm_ret=warm_ret, time_in_mkt=float((pos != 0).mean()), buy_hold=bh))
        start += st_n
    w, b = np.array(chain_warm), np.array(chain_bh)
    f = pd.DataFrame([x for x in folds if x["sym"] == sym])
    summary.append(dict(sym=sym, folds=len(w), window=f"{tr_n}/{te_n}", min_oos_trades=min_oos_trades(cls),
                        cold_trades=int(f.cold_trades.sum()), cold_chain=float(np.prod(1 + f.cold_ret) - 1),
                        warm_chain=float(np.prod(1 + w) - 1), buy_hold_chain=float(np.prod(1 + b) - 1),
                        warm_beat_bh_folds=int((w > b).sum()), avg_time_in_mkt=float(f.time_in_mkt.mean()),
                        distinct_picks=f.pick.nunique()))
    print(sym, "done", flush=True)

F, S = pd.DataFrame(folds), pd.DataFrame(summary)
F.to_csv(OUT / "wf_folds.csv", index=False)
S.to_csv(OUT / "wf_summary.csv", index=False)
print(F.round(3).to_string())
print(S.round(3).to_string())
