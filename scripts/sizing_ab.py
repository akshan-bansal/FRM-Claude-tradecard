"""Phase 3 A/B: volatility sizing against forward-read sizing, on the same walk-forward trades.

    python scripts/sizing_ab.py --venue kraken --bars-dir data/cache/kraken_ohlcvt

Fixed before the first run (2026-11-08), and not changed after it:

* **Trades.** Every out-of-sample long trade from the walk-forward validation (parameters re-chosen on each
  training fold), all grid strategies on every pair, at 45 bps a side. Both arms take the same trades on the
  same days. A trade with no volatility estimate is unsized in both.
* **Book.** One shared book, marked at each close, starting at 1. Gross exposure is capped at 1.0 of equity.
* **Arm A (today).** Weight = min(0.15 / annual_vol, 1.0). When a day's entries exceed the headroom, all of them
  are scaled by the same factor.
* **Arm B (forward read).** Weight = A's weight x tilt. Tilt = clip((edge / annual_vol) / reference, 0, 2), where
  the reference is the median positive edge-to-vol ratio of reads made before the trade. A read with edge <= 0
  takes 0.5; a trade with no read takes 1.0. When the budget binds, it fills from the highest tilt down.
  ``edge`` is the shrunk forward read from ``analysis.forward_read.causal_reads``, which the walk-forward test
  validated; the scope's ``edge_lcb`` is not used.
* **Pass bar.** B beats A on ``sortino_over_dd`` over the whole period and in each half; a month-block bootstrap
  (same months for both arms, 2,000 resamples) gives p < 0.05; and B's mean gross exposure and turnover are each
  no more than 1.1 times A's, so B cannot win by taking more leverage or trading more.

No orders, nothing read from or written to state/. Writes reports/sizing_ab_<venue>.md.
"""
from __future__ import annotations

import argparse
import pickle
import sys
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "scripts"))

from entry_exit_evidence import collect  # noqa: E402
from strategy_calibration import VENUES, load_bars  # noqa: E402
from walk_forward_evidence import oos_trades  # noqa: E402

from trading_live_claude.analysis.forward_read import TradeRow, causal_reads  # noqa: E402
from trading_live_claude.analysis.sizing_ab import (  # noqa: E402
    Rules,
    SizedTrade,
    base_weight,
    held_returns,
    month_block_p,
    objective,
    simulate,
    summary,
    tilts,
)
from trading_live_claude.analysis.strategy_calibration import prior_vol  # noqa: E402
from trading_live_claude.optimize import PARAM_GRIDS  # noqa: E402

ALPHA, LEVER = 0.05, 1.1


def build(bars: dict[str, pd.DataFrame], periods: int, bps: float, cache: Path | None) -> list[SizedTrade]:
    if cache and cache.exists():
        paths = pickle.loads(cache.read_bytes())
    else:
        paths, _ = collect(bars, periods, bps, trades_for=oos_trades, strategies=tuple(PARAM_GRIDS))
        if cache:
            cache.parent.mkdir(parents=True, exist_ok=True)
            cache.write_bytes(pickle.dumps(paths))
    rows = [TradeRow(t.strategy, t.symbol, t.entry, t.exit, t.net_return, len(t.closes) - 1) for t in paths]
    edge = {(r.trade.strategy, r.trade.symbol, r.trade.entry): r.edge for r in causal_reads(rows)}
    cost = 2.0 * bps / 10_000.0
    out = []
    for t in paths:
        df = bars[t.symbol].reset_index(drop=True)
        at = {pd.Timestamp(x): i for i, x in enumerate(df["time"])}
        start, end = at[pd.Timestamp(t.entry)], at[pd.Timestamp(t.exit)]
        out.append(SizedTrade(t.strategy, t.symbol, pd.Timestamp(t.entry),
                              tuple(pd.Timestamp(x) for x in df["time"].iloc[start + 1:end + 1]),
                              held_returns(t.closes, t.net_return + cost), cost,
                              prior_vol(df["close"], start, periods), edge.get((t.strategy, t.symbol, t.entry))))
    return out


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--venue", choices=sorted(VENUES), default="kraken")
    ap.add_argument("--bars-dir", type=Path, default=None)
    ap.add_argument("--out", type=Path, default=ROOT / "reports")
    ap.add_argument("--cache", type=Path, default=None, help="Pickle of the walk-forward trades, reused if present.")
    a = ap.parse_args()
    periods, bps = VENUES[a.venue]
    trades = build(load_bars(a.venue, a.bars_dir), int(periods), bps, a.cache)
    rules = Rules()
    base = [base_weight(t.vol, rules) for t in trades]
    tilt = tilts(trades, rules)
    arm_a = simulate(trades, base, rank=None, rules=rules, name="A: volatility")
    arm_b = simulate(trades, [w * k for w, k in zip(base, tilt, strict=True)], rank=tilt, rules=rules,
                     name="B: forward read")
    n = sum(1 for w in base if w > 0)
    sa, sb = summary(arm_a, int(periods), n), summary(arm_b, int(periods), n)
    mid = arm_a.returns.index[len(arm_a.returns) // 2]
    halves = [(arm_a.returns[arm_a.returns.index < mid], arm_b.returns[arm_b.returns.index < mid]),
              (arm_a.returns[arm_a.returns.index >= mid], arm_b.returns[arm_b.returns.index >= mid])]
    half_scores = [(objective(x, int(periods), n), objective(y, int(periods), n)) for x, y in halves]
    p, median_gain = month_block_p(arm_a.returns, arm_b.returns, int(periods), n)
    checks = {
        "whole period": sb["sortino_over_dd"] > sa["sortino_over_dd"],
        "earlier half": half_scores[0][1] > half_scores[0][0],
        "later half": half_scores[1][1] > half_scores[1][0],
        f"bootstrap p < {ALPHA}": p < ALPHA,
        f"gross exposure <= {LEVER}x A": sb["mean_gross"] <= LEVER * sa["mean_gross"],
        f"turnover <= {LEVER}x A": sb["turnover_per_year"] <= LEVER * sa["turnover_per_year"],
    }
    with_read = sum(1 for t in trades if t.edge is not None)
    weak = sum(1 for t in trades if t.edge is not None and t.edge <= 0)
    fmt = {"sortino_over_dd": "{:.2f}", "sortino": "{:.2f}", "total_return": "{:+.1%}", "cagr": "{:+.1%}",
           "max_drawdown": "{:.1%}", "time_in_market": "{:.0%}", "mean_gross": "{:.2f}",
           "turnover_per_year": "{:.1f}x", "cost_drag_per_year": "{:.2%}", "budget_bound_share": "{:.0%}"}
    lines = [f"# Phase 3 sizing A/B: {a.venue}", "",
             f"{len(trades)} walk-forward trades ({n} sized, {len(trades) - n} with no volatility estimate), "
             f"{with_read} with a read ({weak} with edge <= 0), {bps:.0f} bps a side, "
             f"{arm_a.returns.index[0].date()} to {arm_a.returns.index[-1].date()}.", "",
             "| Metric | A: volatility | B: forward read |", "|---|---|---|"]
    lines += [f"| {k} | {fmt[k].format(sa[k])} | {fmt[k].format(sb[k])} |" for k in fmt]
    lines += [f"| sortino_over_dd, earlier half | {half_scores[0][0]:.2f} | {half_scores[0][1]:.2f} |",
              f"| sortino_over_dd, later half | {half_scores[1][0]:.2f} | {half_scores[1][1]:.2f} |", "",
              f"Month-block bootstrap: p = {p:.3f}, median gain in sortino_over_dd {median_gain:+.2f}.", "",
              "| Pass check | Result |", "|---|---|"]
    lines += [f"| {k} | {'yes' if v else 'NO'} |" for k, v in checks.items()]
    lines += ["", f"**Phase 3: {'PASSED' if all(checks.values()) else 'NOT PASSED'}.** One variant run."]
    text = "\n".join(lines) + "\n"
    a.out.mkdir(parents=True, exist_ok=True)
    (a.out / f"sizing_ab_{a.venue}.md").write_text(text, encoding="utf-8")
    print(text)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
