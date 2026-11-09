"""Calibrate each strategy's win probability across composite agreement, Greek probabilities and graph readings.

    python scripts/strategy_calibration.py --venue kraken
    python scripts/strategy_calibration.py --venue ibkr --bars-dir <folder of SYMBOL.parquet>

Kraken uses public OHLC, IBKR uses saved bars. The graph journal is only read. No orders, and no writes to state/.
Writes reports/strategy_calibration_<venue>.md.
"""
from __future__ import annotations

import argparse
import statistics
import sys
import time
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from trading_live_claude.analysis.calibration import calibrate_for  # noqa: E402
from trading_live_claude.analysis.forward_read import net_return  # noqa: E402
from trading_live_claude.analysis.strategy_calibration import (  # noqa: E402
    CalibrationTrade,
    graph_reading,
    greek_probability,
    prior_vol,
    score_input,
)
from trading_live_claude.analysis.universe import CRYPTO_SLEEVE  # noqa: E402
from trading_live_claude.backtest.engine import BacktestEngine  # noqa: E402
from trading_live_claude.data.kraken_ohlc import kraken_ohlc  # noqa: E402
from trading_live_claude.intel.graph import load_edges  # noqa: E402
from trading_live_claude.strategies.base import StrategyContext  # noqa: E402

PAIRS = ("BTC/USD", "ETH/USD", "SOL/USD", "XRP/USD", "ADA/USD", "LINK/USD", "XMR/USD", "XLM/USD", "ZEC/USD", "AAVE/USD",
         "UNI/USD", "POL/USD")
PANEL = ("ema_crossover", "macd", "bollinger", "rsi_meanrevert", "momentum_breakout", "rsi2_connors", "zscore_ou",
         "bb_rsi_combo", "ts_momentum", "dual_ma")
STRATEGIES = (*PANEL, "composite")
INPUTS = ("composite", "greek", "graph")
VENUES = {"kraken": (365.0, 45.0), "ibkr": (252.0, 10.0)}


def load_bars(venue: str, bars_dir: Path | None) -> dict[str, pd.DataFrame]:
    if venue == "ibkr":
        if bars_dir is None:
            raise SystemExit("--venue ibkr needs --bars-dir")
        out = {p.stem: pd.read_parquet(p) for p in sorted(bars_dir.glob("*.parquet"))}
        for df in out.values():
            df["time"] = pd.to_datetime(df["time"], utc=True)
        return out
    bars: dict[str, pd.DataFrame] = {}
    if bars_dir is not None:       # deep daily bars from scripts/fetch_kraken_ohlcvt_daily.py
        for pair in PAIRS:
            path = bars_dir / f"{CRYPTO_SLEEVE[pair].pair}_daily.parquet"
            if not path.exists():
                print(f"[bars] {pair}: no {path.name}", flush=True)
                continue
            df = pd.read_parquet(path)
            df["time"] = pd.to_datetime(df["time"], utc=True)
            gap = df["time"].diff().dt.days > 3     # a thin early market: start after its last long gap
            start = df.loc[gap, "time"].max() if gap.any() else df["time"].min()
            bars[pair] = df[df["time"] >= start].reset_index(drop=True)
        return bars
    for pair in PAIRS:
        bars[pair] = kraken_ohlc(pair.replace("/", ""), interval=1440)
        time.sleep(1.5)
    return bars


def collect(bars: dict[str, pd.DataFrame], periods: float, per_side_bps: float, edges: list) -> list[CalibrationTrade]:
    engine = BacktestEngine()
    cost = 2.0 * per_side_bps / 10_000.0
    rows: list[CalibrationTrade] = []
    for symbol, df in bars.items():
        df = df.reset_index(drop=True)
        fired = []
        for name in PANEL:
            try:
                sig = calibrate_for(name, symbol).generate_signals(df, StrategyContext(symbol=symbol, timeframe="1d"))
                fired.append(pd.Series(sig["entry"].to_numpy(), index=df.index).fillna(0).astype(bool))
            except Exception as e:
                print(f"[calibration] panel {symbol} {name}: {type(e).__name__}", flush=True)
        agree = pd.concat(fired, axis=1).mean(axis=1) if fired else pd.Series(dtype=float)
        at_bar = {pd.Timestamp(t): i for i, t in enumerate(df["time"])}
        for name in STRATEGIES:
            try:
                trades = engine.run(calibrate_for(name, symbol), df, symbol).trades
            except Exception as e:
                print(f"[calibration] {symbol} {name}: {type(e).__name__}", flush=True)
                continue
            holds: list[int] = []
            for t in sorted(trades, key=lambda x: x.entry_time):
                i = at_bar.get(pd.Timestamp(t.entry_time))
                vol = prior_vol(df["close"], i, periods) if i else None
                hold = statistics.median(holds) if holds else 10.0   # closed trades only, and earlier ones
                rows.append(CalibrationTrade(
                    name, symbol, t.entry_time, t.exit_time, net_return(t.pnl_pct, per_side_bps=per_side_bps),
                    composite=float(agree.iloc[i - 1]) if i and len(agree) else None,
                    greek=greek_probability(vol, hold, periods, cost) if vol else None,
                    graph=graph_reading(edges, symbol, t.entry_time)))
                holds.append(t.bars_held)
    return rows


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--venue", choices=sorted(VENUES), default="kraken")
    ap.add_argument("--bars-dir", type=Path, default=None)
    ap.add_argument("--graph-journal", type=Path, default=ROOT / "state" / "intel_graph.jsonl")
    ap.add_argument("--out", type=Path, default=ROOT / "reports")
    a = ap.parse_args()
    periods, bps = VENUES[a.venue]
    edges = load_edges(a.graph_journal) if a.graph_journal.exists() else []
    trades = collect(load_bars(a.venue, a.bars_dir), periods, bps, edges)
    lines = [f"# Strategy calibration: {a.venue}", "",
             f"{len(trades)} net trades at {bps:.0f} bps a side. Skill is walk-forward Brier against the prior win rate; "
             "above zero beats it. IC is against net return, p from rotating the input in time.", "",
             "| Strategy | Input | Scored | Brier skill | Rank IC | p |", "|---|---|---|---|---|---|"]
    best: list[tuple[float, str]] = []
    for name in STRATEGIES:
        rows = [t for t in trades if t.strategy == name]
        for inp in INPUTS:
            s = score_input(rows, inp, name)
            if s.skill is None:
                lines.append(f"| {name} | {inp} | {s.n} | too few | | |")
                continue
            ic = "" if s.rank_ic is None else f"{s.rank_ic:+.3f}"
            p = "" if s.p_value is None else f"{s.p_value:.3f}"
            lines.append(f"| {name} | {inp} | {s.n} | {s.skill:+.4f} | {ic} | {p} |")
            best.append((s.skill, f"{name}/{inp}"))
    positive = sum(1 for v, _ in best if v > 0)
    lines += ["", f"{len(best)} strategy-input pairs scored, {positive} with positive skill. Across this many tests, a few small "
              "p-values are expected by chance. Parameters are pinned, on overlapping history."]
    text = "\n".join(lines) + "\n"
    a.out.mkdir(parents=True, exist_ok=True)
    (a.out / f"strategy_calibration_{a.venue}.md").write_text(text, encoding="utf-8")
    print(text)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
