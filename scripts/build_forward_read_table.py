"""Write the forward-read trade table the phase 4 shadow reads (``forward_read_shadow`` in trading.yaml).

    python scripts/build_forward_read_table.py --venue kraken --bars-dir data/cache/kraken_ohlcvt \
        --cache reports/sizing_ab/kraken_trades.pkl --out data/forward_read/kraken.parquet

The rows are the walk-forward out-of-sample trades the phase 3 A/B used: parameters re-chosen on each training
fold, net of costs, with the annualized volatility before each entry. ``--cache`` reuses that run's trades.
Public data only; nothing read from or written to state/.
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "scripts"))

from sizing_ab import build  # noqa: E402
from strategy_calibration import VENUES, load_bars  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--venue", choices=sorted(VENUES), default="kraken")
    ap.add_argument("--bars-dir", type=Path, default=None)
    ap.add_argument("--cache", type=Path, default=None)
    ap.add_argument("--out", type=Path, required=True)
    a = ap.parse_args()
    periods, bps = VENUES[a.venue]
    trades = build(load_bars(a.venue, a.bars_dir), int(periods), bps, a.cache)
    cost = 2.0 * bps / 10_000.0
    df = pd.DataFrame([{"strategy": t.strategy, "symbol": t.symbol, "entry": t.entry, "exit": t.days[-1],
                        "net_return": float(pd.Series(t.returns).add(1).prod() - 1 - cost),
                        "bars": len(t.days), "vol": t.vol} for t in trades if t.days])
    a.out.parent.mkdir(parents=True, exist_ok=True)
    tmp = a.out.with_suffix(".tmp")
    df.sort_values(["entry", "strategy", "symbol"]).to_parquet(tmp, index=False)
    tmp.replace(a.out)
    print(f"[table] {len(df)} trades, {df['symbol'].nunique()} symbols, {df['strategy'].nunique()} strategies, "
          f"{df['entry'].min().date()} to {df['exit'].max().date()} -> {a.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
