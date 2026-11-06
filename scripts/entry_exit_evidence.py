"""Test composite agreement and the Greek probability as an entry filter and as an exit.

    python scripts/entry_exit_evidence.py --venue kraken
    python scripts/entry_exit_evidence.py --venue ibkr --bars-dir <folder of SYMBOL.parquet>

Kraken uses public OHLC, IBKR uses saved bars. No orders, and nothing read from or written to state/. Parameters are the
pinned calibrated ones, so a pass here still needs the walk-forward gate. Writes reports/entry_exit_<venue>.md.
"""
from __future__ import annotations

import argparse
import statistics
import sys
from collections.abc import Callable, Sequence
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "scripts"))

from strategy_calibration import PANEL, STRATEGIES, VENUES, load_bars  # noqa: E402

from trading_live_claude.analysis.calibration import calibrate_for  # noqa: E402
from trading_live_claude.analysis.entry_exit import (  # noqa: E402
    Comparison,
    PathTrade,
    evaluate_entry,
    evaluate_exit,
)
from trading_live_claude.analysis.forward_read import net_return  # noqa: E402
from trading_live_claude.analysis.strategy_calibration import greek_probability, prior_vol  # noqa: E402
from trading_live_claude.backtest.engine import BacktestEngine, Trade  # noqa: E402
from trading_live_claude.risk.spectral_var import load_engine  # noqa: E402
from trading_live_claude.strategies.base import StrategyContext  # noqa: E402

INPUTS = ("composite", "greek")


def _greek_exit_path(close: pd.Series, start: int, end: int, hold: float, periods: float, cost: float) -> tuple[float, ...]:
    """1 - P(finish above breakeven), from what each close in the trade knew."""
    be = load_engine()
    model = be.BSMDigital(strike=1.0 + cost, expiry=1.0, rate=0.0, dividend=0.0, skew=0.0)
    entry = float(close.iloc[start])
    out: list[float] = []
    for j in range(end - start + 1):
        vol = prior_vol(close, start + j + 1, periods)         # returns through this close, and no later
        if vol is None:
            out.append(0.0)
            continue
        tau = max(hold - j, 1.0) / periods
        out.append(1.0 - float(model.probability(float(close.iloc[start + j]) / entry, vol, tau)))
    return tuple(out)


def pinned_trades(name: str, symbol: str, df: pd.DataFrame) -> list[Trade]:
    """The strategy's trades over the whole sample at its pinned calibrated parameters."""
    return BacktestEngine().run(calibrate_for(name, symbol), df, symbol).trades


def collect(bars: dict[str, pd.DataFrame], periods: float, per_side_bps: float, *,
            trades_for: Callable[[str, str, pd.DataFrame], list[Trade]] = pinned_trades,
            strategies: Sequence[str] = STRATEGIES) -> tuple[list[PathTrade], int]:
    cost = 2.0 * per_side_bps / 10_000.0
    rows: list[PathTrade] = []
    shorts = 0
    for symbol, raw in bars.items():
        df = raw.reset_index(drop=True)
        entries, exits = [], []
        for name in PANEL:
            try:
                sig = calibrate_for(name, symbol).generate_signals(df, StrategyContext(symbol=symbol, timeframe="1d"))
            except Exception as e:
                print(f"[entry-exit] panel {symbol} {name}: {type(e).__name__}", flush=True)
                continue
            entries.append(pd.Series(sig["entry"].to_numpy(), index=df.index).fillna(0).astype(bool))
            exits.append(pd.Series(sig["exit"].to_numpy(), index=df.index).fillna(0).astype(bool))
        if not entries:
            continue
        entry_agree = pd.concat(entries, axis=1).mean(axis=1)
        exit_agree = pd.concat(exits, axis=1).mean(axis=1)
        at_bar = {pd.Timestamp(t): i for i, t in enumerate(df["time"])}
        close = df["close"]
        for name in strategies:
            try:
                trades = trades_for(name, symbol, df)
            except Exception as e:
                print(f"[entry-exit] {symbol} {name}: {type(e).__name__}", flush=True)
                continue
            holds: list[int] = []
            for t in sorted(trades, key=lambda x: x.entry_time):
                start, end = at_bar.get(pd.Timestamp(t.entry_time)), at_bar.get(pd.Timestamp(t.exit_time))
                hold = statistics.median(holds) if holds else 10.0
                holds.append(t.bars_held)
                if start is None or end is None or start < 1 or end <= start:
                    continue
                if abs((t.exit_price / t.entry_price - 1.0) - t.pnl_pct) > 1e-9:
                    shorts += 1
                    continue
                vol = prior_vol(close, start, periods)
                rows.append(PathTrade(
                    name, symbol, t.entry_time, t.exit_time, net_return(t.pnl_pct, per_side_bps=per_side_bps),
                    {"composite": float(entry_agree.iloc[start - 1]),
                     "greek": greek_probability(vol, hold, periods, cost) if vol else None},
                    tuple(float(c) for c in close.iloc[start:end + 1]),
                    {"composite": tuple(float(x) for x in exit_agree.iloc[start:end + 1]),
                     "greek": _greek_exit_path(close, start, end, hold, periods, cost)}))
    return rows, shorts


def _row(c: Comparison, alpha: float) -> str:
    verdict = "PASS" if c.passes(alpha) else ("watch" if c.p_value < 0.05 and c.mean_rule > c.mean_base and c.later_half_gain > 0 else "")
    return (f"| {c.strategy} | {c.input} | {c.n} | {c.changed:.0%} | {c.mean_base:+.2%} | {c.mean_rule:+.2%} | "
            f"{c.total_base:+.1%} | {c.total_rule:+.1%} | {c.drawdown_base:.1%} | {c.drawdown_rule:.1%} | "
            f"{c.later_half_gain:+.2%} | {c.symbols_up}/{c.symbols} | {c.p_value:.3f} | {verdict} |")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--venue", choices=sorted(VENUES), default="kraken")
    ap.add_argument("--bars-dir", type=Path, default=None)
    ap.add_argument("--out", type=Path, default=ROOT / "reports")
    a = ap.parse_args()
    periods, bps = VENUES[a.venue]
    cost = 2.0 * bps / 10_000.0
    trades, shorts = collect(load_bars(a.venue, a.bars_dir), periods, bps)
    entry = [evaluate_entry(trades, i, s) for s in STRATEGIES for i in INPUTS]
    exits = [evaluate_exit(trades, i, s, cost=cost) for s in STRATEGIES for i in INPUTS]
    done = [c for c in [*entry, *exits] if c is not None]
    alpha = 0.05 / max(len(done), 1)
    head = ["| Strategy | Input | Trades | Changed | Mean base | Mean rule | Total base | Total rule | DD base | DD rule | "
            "Later-half gain | Symbols up | p | Verdict |", "|" + "---|" * 14]
    lines = [f"# Entry and exit evidence: {a.venue}", "",
             f"{len(trades)} long net trades at {bps:.0f} bps a side; {shorts} short trades not simulated. {len(done)} tests, so PASS "
             f"needs p < {alpha:.4f}, a positive later half and no lower total. `watch` is p < 0.05 and a positive later half.",
             "Totals and drawdowns add trade returns at equal notional, without compounding.", "", "## Entry filter", "", *head]
    lines += [_row(c, alpha) for c in entry if c is not None]
    lines += ["", "## Exit", "", *head]
    lines += [_row(c, alpha) for c in exits if c is not None]
    lines += ["", f"PASS: {sum(c.passes(alpha) for c in done)} of {len(done)}."]
    text = "\n".join(lines) + "\n"
    a.out.mkdir(parents=True, exist_ok=True)
    (a.out / f"entry_exit_{a.venue}.md").write_text(text, encoding="utf-8")
    print(text)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
