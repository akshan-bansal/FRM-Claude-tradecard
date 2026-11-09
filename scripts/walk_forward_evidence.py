"""Validate the open findings on walk-forward trades: parameters re-chosen on each training fold, scored on the next.

    python scripts/walk_forward_evidence.py --venue kraken [--bars-dir data/cache/kraken_ohlcvt]
    python scripts/walk_forward_evidence.py --venue ibkr --bars-dir <folder of SYMBOL.parquet>

Fixed before the run:

* **Pairing claim.** The forward read ranks trades within a symbol (strategy against strategy), at p < 0.025 with the
  read rotated inside each symbol, and with a positive rank correlation in both halves.
* **Near misses.** The IBKR ``momentum_breakout`` Greek exit, at p < 0.025 under the usual pass rules. The IBKR
  ``zscore_ou`` composite filter cannot run: ``zscore_ou`` has no parameter grid.

Kraken uses public OHLC, IBKR uses saved bars. No orders, and nothing read from or written to state/. The composite
input still comes from the pinned panel. Writes reports/walk_forward_evidence_<venue>.md.
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "scripts"))

from entry_exit_evidence import collect  # noqa: E402
from strategy_calibration import VENUES, load_bars  # noqa: E402
from sweep_universe import combos, is_etf_like, score_run  # noqa: E402

from trading_live_claude.analysis.entry_exit import evaluate_exit  # noqa: E402
from trading_live_claude.analysis.forward_read import TradeRow, causal_reads, grouped_skill, skill  # noqa: E402
from trading_live_claude.analysis.universe import wf_protocol  # noqa: E402
from trading_live_claude.backtest.costs import CostModel  # noqa: E402
from trading_live_claude.backtest.engine import BacktestEngine, Trade  # noqa: E402
from trading_live_claude.intel.routing import classify_symbol  # noqa: E402
from trading_live_claude.optimize import PARAM_GRIDS  # noqa: E402
from trading_live_claude.strategies import STRATEGIES as REGISTRY  # noqa: E402

ALPHA = 0.025
NEAR_MISS = ("momentum_breakout", "greek")
FOLDS: dict[str, int] = {}


def oos_trades(name: str, symbol: str, df: pd.DataFrame) -> list[Trade]:
    """Every out-of-sample trade, each fold at the parameters its own training bars chose."""
    protocol = wf_protocol(classify_symbol(symbol))
    train, test, step = protocol.train_bars, protocol.test_bars, protocol.step_bars
    if len(df) < train + test:
        return []                  # too short for one fold: no trades, rather than an error
    engine = BacktestEngine(cost_model=CostModel.from_price(float(df["close"].iloc[train - 1]), is_etf=is_etf_like(symbol)))
    out: list[Trade] = []
    start = 0
    while start + train + test <= len(df):
        tr = df.iloc[start:start + train].reset_index(drop=True)
        best = None
        for p in combos(PARAM_GRIDS[name]):
            got = score_run(engine, name, p, tr, symbol)
            if got and (best is None or got[0] > best[0]):
                best = (got[0], p)
        if best is not None:
            fold = df.iloc[start:start + train + test].reset_index(drop=True)
            out += engine.run(REGISTRY[name](**best[1]), fold, symbol, warmup_bars=train).trades
            FOLDS[f"{symbol}|{name}"] = FOLDS.get(f"{symbol}|{name}", 0) + 1
        start += step
    return out


def _fmt(s: object) -> str:
    if s is None:
        return "too few |  |  |"
    return f"{s.n} | {s.rank_ic:+.3f} | {s.p_value:.3f} |"   # type: ignore[attr-defined]


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--venue", choices=sorted(VENUES), default="kraken")
    ap.add_argument("--bars-dir", type=Path, default=None)
    ap.add_argument("--out", type=Path, default=ROOT / "reports")
    a = ap.parse_args()
    periods, bps = VENUES[a.venue]
    bars = load_bars(a.venue, a.bars_dir)
    paths, shorts = collect(bars, periods, bps, trades_for=oos_trades, strategies=tuple(PARAM_GRIDS))
    rows = [TradeRow(t.strategy, t.symbol, t.entry, t.exit, t.net_return, len(t.closes) - 1) for t in paths]
    reads = causal_reads(rows)
    ordered = sorted(reads, key=lambda r: r.trade.entry)
    half = len(ordered) // 2
    by_symbol = lambda r: r.trade.symbol  # noqa: E731
    within = grouped_skill(reads, by_symbol)
    early, late = grouped_skill(ordered[:half], by_symbol), grouped_skill(ordered[half:], by_symbol)
    verdict = bool(within and early and late and within.p_value < ALPHA and within.rank_ic > 0
                   and early.rank_ic > 0 and late.rank_ic > 0)
    symbols = sorted({k.split("|")[0] for k in FOLDS})
    lines = [f"# Walk-forward validation: {a.venue}", "",
             f"{len(rows)} out-of-sample long trades at {bps:.0f} bps a side on {len(symbols)} symbols "
             f"({', '.join(symbols) or 'none'}); {shorts} short trades not simulated; {len(reads)} with a read. "
             f"Strategies are those with a parameter grid: {', '.join(PARAM_GRIDS)}.", "",
             "## Pairing claim", "", "| Test | Reads | Rank IC | p |", "|---|---|---|---|",
             f"| Pooled | {_fmt(skill(reads))}",
             f"| **Within symbol** (the claim) | {_fmt(within)}",
             f"| Within symbol, earlier half | {_fmt(early)}",
             f"| Within symbol, later half | {_fmt(late)}",
             f"| Within quarter | {_fmt(grouped_skill(reads, lambda r: (r.trade.entry.year, (r.trade.entry.month - 1) // 3)))}",
             f"| Within one strategy on one symbol | {_fmt(grouped_skill(reads, lambda r: (r.trade.strategy, r.trade.symbol)))}",
             "", f"**Pairing claim: {'VALIDATED' if verdict else 'NOT VALIDATED'}** (needs p < {ALPHA}, positive overall "
             "and in both halves)."]
    if a.venue == "ibkr":
        c = evaluate_exit(paths, NEAR_MISS[1], NEAR_MISS[0], cost=2.0 * bps / 10_000.0)
        lines += ["", "## Near misses", ""]
        if c is None:
            lines.append(f"- `{NEAR_MISS[0]}` Greek exit: too few out-of-sample trades to judge.")
        else:
            lines.append(f"- `{NEAR_MISS[0]}` Greek exit: {c.n} trades, {c.changed:.0%} exited early, mean "
                         f"{c.mean_base:+.2%} to {c.mean_rule:+.2%}, total {c.total_base:+.1%} to {c.total_rule:+.1%}, "
                         f"later half {c.later_half_gain:+.2%}, {c.symbols_up}/{c.symbols} symbols up, p = {c.p_value:.3f}. "
                         f"**{'VALIDATED' if c.passes(ALPHA) else 'NOT VALIDATED'}.**")
        lines.append("- `zscore_ou` composite filter: not run. `zscore_ou` has no parameter grid, so it cannot be walked forward.")
    text = "\n".join(lines) + "\n"
    a.out.mkdir(parents=True, exist_ok=True)
    (a.out / f"walk_forward_evidence_{a.venue}.md").write_text(text, encoding="utf-8")
    print(text)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
