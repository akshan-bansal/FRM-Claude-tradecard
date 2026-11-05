"""Build the forward-read evidence on Kraken: phases 0 to 2 of POSITION_SIZING_SCOPE.md.

    python scripts/forward_read_evidence.py
    python scripts/forward_read_evidence.py --bars-dir <folder of SYMBOL.parquet> --per-side-bps 10 --title IBKR

Public OHLC only: no key, no orders, no state/ reads or writes, so a running paper session is not touched. Parameters
are the pinned calibrated ones, not re-optimized per fold. Those were chosen on overlapping history, so a positive
result here still needs the walk-forward gate. Writes reports/forward_read_trades.csv and forward_read_evidence.md.
"""
from __future__ import annotations

import argparse
import csv
import sys
import time
from dataclasses import asdict
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from trading_live_claude.analysis.calibration import calibrate_for  # noqa: E402
from trading_live_claude.analysis.forward_read import (  # noqa: E402
    Skill,
    TradeRow,
    causal_reads,
    net_return,
    skill,
)
from trading_live_claude.backtest.engine import BacktestEngine  # noqa: E402
from trading_live_claude.data.kraken_ohlc import kraken_ohlc  # noqa: E402

PAIRS = ("BTC/USD", "ETH/USD", "SOL/USD", "XRP/USD", "ADA/USD", "LINK/USD", "XMR/USD", "XLM/USD", "ZEC/USD", "AAVE/USD",
         "UNI/USD", "POL/USD")
STRATEGIES = ("ema_crossover", "macd", "bollinger", "rsi_meanrevert", "momentum_breakout", "rsi2_connors", "zscore_ou",
              "bb_rsi_combo", "ts_momentum", "dual_ma")
PER_SIDE_BPS = 45.0              # the paper default: 40 bps venue, 5 bps slippage


def collect(pairs: tuple[str, ...], strategies: tuple[str, ...]) -> tuple[list[TradeRow], list[str]]:
    rows: list[TradeRow] = []
    notes: list[str] = []
    engine = BacktestEngine()
    for pair in pairs:
        try:
            df = kraken_ohlc(pair.replace("/", ""), interval=1440)
        except Exception as e:
            notes.append(f"{pair}: no data, {type(e).__name__}: {e}")
            continue
        time.sleep(1.5)
        for name in strategies:
            try:
                result = engine.run(calibrate_for(name, pair), df, pair)
            except Exception as e:
                notes.append(f"{pair} {name}: skipped, {type(e).__name__}: {e}")
                continue
            rows += [TradeRow(name, pair, t.entry_time, t.exit_time, net_return(t.pnl_pct, per_side_bps=PER_SIDE_BPS),
                              t.bars_held) for t in result.trades]
    return rows, notes


def collect_files(folder: Path, strategies: tuple[str, ...], per_side_bps: float) -> tuple[list[TradeRow], list[str]]:
    """Saved daily bars: one parquet per symbol with time, open, high, low, close, volume. Read-only."""
    rows: list[TradeRow] = []
    notes: list[str] = []
    engine = BacktestEngine()
    for path in sorted(folder.glob("*.parquet")):
        df = pd.read_parquet(path)
        df["time"] = pd.to_datetime(df["time"], utc=True)
        for name in strategies:
            try:
                result = engine.run(calibrate_for(name, path.stem), df, path.stem)
            except Exception as e:
                notes.append(f"{path.stem} {name}: skipped, {type(e).__name__}: {e}")
                continue
            rows += [TradeRow(name, path.stem, t.entry_time, t.exit_time, net_return(t.pnl_pct, per_side_bps=per_side_bps),
                              t.bars_held) for t in result.trades]
    return rows, notes


def _line(label: str, s: Skill | None) -> str:
    if s is None:
        return f"| {label} | too few | | | |"
    buckets = " / ".join(f"{m:+.2%}" for m in s.bucket_means)
    return f"| {label} | {s.n} | {s.rank_ic:+.3f} | {s.p_value:.3f} | {buckets} |"


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--out", type=Path, default=ROOT / "reports")
    ap.add_argument("--bars-dir", type=Path, default=None, help="saved bars instead of Kraken's public OHLC")
    ap.add_argument("--per-side-bps", type=float, default=PER_SIDE_BPS)
    ap.add_argument("--title", default="Kraken")
    a = ap.parse_args()
    a.out.mkdir(parents=True, exist_ok=True)
    trades, notes = collect_files(a.bars_dir, STRATEGIES, a.per_side_bps) if a.bars_dir else collect(PAIRS, STRATEGIES)
    with (a.out / "forward_read_trades.csv").open("w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=list(TradeRow.__dataclass_fields__))
        w.writeheader()
        w.writerows(asdict(t) for t in trades)
    reads = causal_reads(trades)
    ordered = sorted(reads, key=lambda r: r.trade.entry)
    half = len(ordered) // 2
    lines = [f"# Forward-read evidence: {a.title}", "",
             f"{len(trades)} net trades, {len(reads)} with a read. Costs {a.per_side_bps:.0f} bps a side. "
             f"First entry {min(t.entry for t in trades):%Y-%m-%d}, last {max(t.entry for t in trades):%Y-%m-%d}.",
             f"Mean net return per trade {sum(t.net_return for t in trades) / len(trades):+.2%}.", "",
             "| Set | Reads | Rank IC | p (rotation) | Mean net return, lowest read to highest |", "|---|---|---|---|---|",
             _line("All", skill(reads)), _line("Earlier half", skill(ordered[:half])), _line("Later half", skill(ordered[half:]))]
    for name in STRATEGIES:
        lines.append(_line(name, skill([r for r in reads if r.trade.strategy == name])))
    lines += ["", f"{1 + 2 + len(STRATEGIES)} sets tested, so a lone small p-value among the strategies is expected by chance.",
              "Parameters are pinned, chosen on overlapping history, and trades overlap in time across strategies."]
    lines += [f"- {n}" for n in notes]
    text = "\n".join(lines) + "\n"
    (a.out / "forward_read_evidence.md").write_text(text, encoding="utf-8")
    print(text)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
