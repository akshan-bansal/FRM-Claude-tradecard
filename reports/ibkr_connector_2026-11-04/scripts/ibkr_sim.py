"""Paper replay on IBKR bars through PaperBroker and the real Router, with the BSM_Spectral gate on and off.

Strategy and parameters are chosen on the opening training stretch only and replayed on what follows.
Every state file goes to a scratch directory: no ledger, no state/HALTED, no real broker.
"""
import json
import shutil
import sys
import warnings
from datetime import UTC, datetime
from pathlib import Path

import numpy as np
import pandas as pd

REPO = Path(r"C:\Users\PC\OneDrive\Desktop\Components\FRM-Claude-tradecard")
sys.path.insert(0, str(REPO / "scripts"))
from sweep_universe import best_config  # noqa: E402

from trading_live_claude.brokers.models import Candle, OrderAction, Quote  # noqa: E402
from trading_live_claude.brokers.paper import PaperBroker  # noqa: E402
from trading_live_claude.execution.journal import OrderJournal  # noqa: E402
from trading_live_claude.execution.router import OrderIntent, Router  # noqa: E402
from trading_live_claude.risk.heat import PortfolioHeat  # noqa: E402
from trading_live_claude.risk.kill_switch import KillSwitch  # noqa: E402
from trading_live_claude.risk.sizing import PositionSizer  # noqa: E402
from trading_live_claude.risk.spectral_var import attach_spectral_gate  # noqa: E402
from trading_live_claude.strategies import STRATEGIES  # noqa: E402
from trading_live_claude.strategies.base import StrategyContext  # noqa: E402

warnings.filterwarnings("ignore")
BARS = Path(__file__).parent / "ibkr_bars"
OUT = Path(__file__).parent / "ibkr_out"
SCRATCH = OUT / "sim_state"
pd.set_option("display.width", 220, "display.max_columns", 30, "display.max_colwidth", 70)
CAP = 0.005           # config/trading.example.yaml's example value for spectral_var_cap
EQUITY = 100_000.0
ACCOUNT = "PAPER-001"
# (label, bars file, multiplier, training bars)
BOOKS = [("SPY", "SPY", 1, 504), ("GLD", "GLD", 1, 504), ("TLT", "TLT", 1, 504),
         ("STW.AX", "STW.AX", 1, 504), ("BHP.AX", "BHP.AX", 1, 504),
         ("ES x50", "ES_cont", 50, 252), ("MES-size x5", "ES_cont", 5, 252)]


class ReplayFeed:
    """Answers only with bars up to the replay clock, scaled so one unit is one contract."""
    name = "replay"
    venue = "paper"

    def __init__(self, sym: str, df: pd.DataFrame, mult: int) -> None:
        self.sym, self.df, self.mult, self.i, self.use_open = sym, df, mult, 0, False

    def accounts(self):
        return []

    def positions(self, _):
        return []

    def equity(self, *_a, **_k):
        return EQUITY

    def place_order(self, order):
        raise AssertionError("a replay feed never places orders")

    def cancel_order(self, *_a, **_k):
        pass

    def quote(self, symbol):
        row = self.df.iloc[self.i]
        px = float(row.open if self.use_open else row.close) * self.mult
        half = px * 0.5e-4
        return Quote(symbol=symbol, symbolId=1, bidPrice=px - half, askPrice=px + half, lastTradePrice=px,
                     lastTradeTime=datetime.now(UTC), bidSize=100, askSize=100)

    def quotes(self, symbols):
        return [self.quote(s) for s in symbols]

    def candles(self, symbol, start, end, interval="OneDay"):
        # stops at the last COMPLETED bar: while filling at i's open, bar i has not closed
        last = self.i - 1 if self.use_open else self.i
        out = []
        for _, b in self.df.iloc[: last + 1].iterrows():
            t = b.time.to_pydatetime()
            out.append(Candle(start=t, end=t, low=float(b.low) * self.mult, high=float(b.high) * self.mult,
                              open=float(b.open) * self.mult, close=float(b.close) * self.mult,
                              volume=int(b.volume)))
        return out


def atr(df: pd.DataFrame, n: int = 14) -> pd.Series:
    pc = df.close.shift()
    tr = pd.concat([df.high - df.low, (df.high - pc).abs(), (df.low - pc).abs()], axis=1).max(axis=1)
    return tr.rolling(n).mean()


def replay(label: str, df: pd.DataFrame, mult: int, sname: str, params: dict, start: int, cap: float | None,
           min_returns: int = 500) -> dict:
    tag = f"{label.replace(' ', '_')}_{'on' if cap else 'off'}_{min_returns}"
    state = SCRATCH / tag
    shutil.rmtree(state, ignore_errors=True)
    state.mkdir(parents=True)
    feed = ReplayFeed(label, df, mult)
    broker = PaperBroker(feed=feed, starting_equity=EQUITY, slippage_bps=1.0, journal_dir=None, venue="paper")
    journal = OrderJournal(state)
    router = Router(mode="paper", broker=broker, journal=journal,
                    kill_switch=KillSwitch(state, paper=True), heat=PortfolioHeat(0.05), max_open_positions=5)
    gate = attach_spectral_gate(router, broker, cap_pct=cap, level=0.99, horizon_days=1, min_returns=min_returns)
    sig = STRATEGIES[sname](**params).generate_signals(df, StrategyContext(symbol=label.split()[0], timeframe="1d"))
    a = atr(df) * mult
    sizer = PositionSizer(risk_pct=0.01, atr_multiple=2.0, target_r=2.0)
    equity_curve, entries, accepted, trimmed = [], 0, 0, []
    stop = None
    held = 0.0
    for t in range(start, len(df) - 1):
        # decide on bar t's close, act at bar t+1's open
        want_entry, want_exit = bool(sig.entry.iloc[t]), bool(sig.exit.iloc[t])
        if held > 0 and stop is not None and float(df.close.iloc[t]) * mult < stop:
            want_exit = True
        feed.i, feed.use_open = t + 1, True
        px = float(df.open.iloc[t + 1]) * mult
        eq = broker.equity(ACCOUNT)
        if held > 0 and want_exit:
            intent = OrderIntent(symbol=label, action=OrderAction.SELL, shares=held, entry=px, stop=px * 1.10, target=None,
                                 strategy=sname, risk_dollars=0.0, account_number=ACCOUNT)
            if router.submit(intent, equity=eq, existing_risk=0.0, open_positions=1) is not None:
                held, stop = 0.0, None
        elif held == 0 and want_entry and np.isfinite(a.iloc[t]) and a.iloc[t] > 0:
            s = sizer.size(equity=eq, entry=px, atr_value=float(a.iloc[t]))
            if s.shares >= 1:
                entries += 1
                intent = OrderIntent(symbol=label, action=OrderAction.BUY, shares=float(s.shares), entry=px, stop=s.stop,
                                     target=s.target, strategy=sname, risk_dollars=s.dollar_risk, account_number=ACCOUNT)
                order = router.submit(intent, equity=eq, existing_risk=0.0, open_positions=0)
                if order is not None:
                    accepted += 1
                    filled = float(getattr(order, "totalQuantity", s.shares) or s.shares)
                    if filled < s.shares:
                        trimmed.append(s.shares - filled)
                    held, stop = filled, s.stop
        feed.i, feed.use_open = t + 1, False
        equity_curve.append(broker.mark_to_market() if hasattr(broker, "mark_to_market") else broker.equity(ACCOUNT))
    curve = np.array(equity_curve, dtype=float)
    peak = np.maximum.accumulate(curve)
    reasons: dict[str, int] = {}
    if journal.rejected_path.exists():
        for line in journal.rejected_path.read_text(encoding="utf-8").splitlines():
            rec = json.loads(line)
            for r in rec.get("reasons") or rec.get("reject_reasons") or [str(rec.get("reason", "?"))]:
                key = " ".join(str(r).split()[:6])
                reasons[key] = reasons.get(key, 0) + 1
    bh = float(df.close.iloc[-1] / df.close.iloc[start] - 1)
    return dict(book=label, strategy=sname, gate="on" if cap else "off", replay_bars=len(curve),
                entries=entries, accepted=accepted, trimmed=len(trimmed), avg_trim=float(np.mean(trimmed)) if trimmed else 0.0,
                still_held=held, ret=curve[-1] / EQUITY - 1, maxdd=float((curve / peak - 1).min()), buy_hold=bh,
                rejects=json.dumps(reasons), gate_loaded=gate is not None)


def main() -> None:
    SCRATCH.mkdir(parents=True, exist_ok=True)
    rows = []
    for label, file, mult, train in BOOKS:
        df = pd.read_parquet(BARS / f"{file}.parquet")[["time", "open", "high", "low", "close", "volume"]].copy()
        df["high"] = df[["high", "open", "close"]].max(axis=1)
        df["low"] = df[["low", "open", "close"]].min(axis=1)
        df = df.reset_index(drop=True)
        best = best_config(df.iloc[:train].reset_index(drop=True), label.split()[0])
        _, sname, params, _ = best
        for cap in (None, CAP):
            rows.append(replay(label, df, mult, sname, params, train, cap))
            print({k: (round(v, 4) if isinstance(v, float) else v) for k, v in rows[-1].items()}, flush=True)
    res = pd.DataFrame(rows)
    res.to_csv(OUT / "sim.csv", index=False)
    print("=== REJECTS")
    for _, x in res.iterrows():
        print(x.book, x.gate, x.rejects[:300])
    print("=== TABLE")
    print(res.drop(columns=["rejects", "gate_loaded"]).round(3).to_string())


if __name__ == "__main__":
    main()
