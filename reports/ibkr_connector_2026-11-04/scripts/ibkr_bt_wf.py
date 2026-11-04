"""Backtest + walk-forward on IBKR connector bars. Writes CSVs next to the bars; orders nothing."""
import sys
import warnings
from pathlib import Path

import numpy as np
import pandas as pd

REPO = Path(r"C:\Users\PC\OneDrive\Desktop\Components\FRM-Claude-tradecard")
sys.path.insert(0, str(REPO / "scripts"))
from sweep_universe import best_config, walk_forward  # noqa: E402

from trading_live_claude.intel.routing import classify_symbol  # noqa: E402

warnings.filterwarnings("ignore")
BARS = Path(__file__).parent / "ibkr_bars"
OUT = Path(__file__).parent / "ibkr_out"
OUT.mkdir(exist_ok=True)
SYMS = {"SPY": "SPY", "GLD": "GLD", "TLT": "TLT", "STW.AX": "STW.AX", "BHP.AX": "BHP.AX", "ES": "ES_cont"}


def load(name: str) -> pd.DataFrame:
    df = pd.read_parquet(BARS / f"{name}.parquet")[["time", "open", "high", "low", "close", "volume"]]
    # IBKR's official close can sit a hair outside the RTH range; widen the range, never move a price.
    df["high"] = df[["high", "open", "close"]].max(axis=1)
    df["low"] = df[["low", "open", "close"]].min(axis=1)
    return df.reset_index(drop=True)


rows, wf_rows = [], []
for sym, file in SYMS.items():
    df = load(file)
    yrs = len(df) / 252
    bh = df.close.iloc[-1] / df.close.iloc[0] - 1
    best = best_config(df, sym)
    if best:
        score, sname, params, m = best
        rows.append(dict(sym=sym, bars=len(df), years=round(yrs, 2), buy_hold=bh, strategy=sname, params=str(params),
                         score=score, total_return=m.total_return, sharpe=m.sharpe, maxdd=m.max_drawdown,
                         trades=m.num_trades, win_rate=m.win_rate))
    # the class protocol first; ES is too short for it, so also a labelled shorter window
    wf = walk_forward(df, sym)
    wf_rows.append(dict(sym=sym, window="protocol", cls=classify_symbol(sym), **(wf or dict(folds=0))))
    if sym == "ES":
        wf = walk_forward(df, sym, train=252, test=63, step=63, asset_class="equity")
        wf_rows.append(dict(sym=sym, window="252/63 non-protocol", cls="equity", **(wf or dict(folds=0))))
    print(sym, "done", flush=True)

bt, wfd = pd.DataFrame(rows), pd.DataFrame(wf_rows)
bt.to_csv(OUT / "backtest.csv", index=False)
wfd.to_csv(OUT / "walkforward.csv", index=False)
pd.set_option("display.width", 220, "display.max_columns", 30, "display.max_colwidth", 60)
print(bt.drop(columns=["params"]).round(3).to_string())
print(bt[["sym", "params"]].to_string())
print(wfd.drop(columns=["asset_class"], errors="ignore").round(3).to_string())
_ = np
