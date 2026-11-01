"""Risk-guard validation: is each guard firing as designed, and is there enough record to judge it?

Reads the journals and the audit ledger under ``state/`` and fetches nothing. Writes
``reports/risk_guard_analysis_YYYY-MM-DD.md``. Calibration needs at least a week of paper sessions on
more than one venue (NEXT_SESSION.md); below that the report says so and recommends nothing, however
tidy the numbers look.

Counts come from two places. Since trim mode became the default, the per-symbol and gross-leverage caps
mostly resize rather than reject, so their rejection strings undercount them: a resize only leaves a
trace as a ledger ``RISK_TRIMMED`` row. Sessions without a ledger cannot show their trims.

    python scripts/risk_guard_analysis.py [--state-dir state] [--out reports/x.md]
"""
from __future__ import annotations

import argparse
import csv
import json
import sys
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any

from trading_live_claude.audit.queries import all_rows
from trading_live_claude.brokers.models import OrderAction, Position
from trading_live_claude.execution.router import OrderIntent, Router

MIN_DAYS = 7
MIN_VENUES = 2
BINDING_BAND = (0.01, 0.05)     # frequent enough to be doing work, rare enough not to strangle
KILL_THRESHOLDS = (0.03, 0.08)  # the tightened threshold against the one it replaced
SWEEP = (2.0, 2.5, 3.0, 3.5, 4.0)
REPO = Path(__file__).resolve().parents[1]


def _jsonl(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def _equity(path: Path) -> list[dict[str, str]]:
    if not path.exists():
        return []
    with path.open(encoding="utf-8", newline="") as f:
        return list(csv.DictReader(f))


@dataclass
class Report:
    state_dir: str
    days: list[str] = field(default_factory=list)
    venues: list[str] = field(default_factory=list)
    sessions: list[str] = field(default_factory=list)
    intents: int = 0
    accepted_entries: int = 0
    fills: int = 0
    ledger_rows: int = 0
    rejections: Counter[str] = field(default_factory=Counter)
    trims: Counter[str] = field(default_factory=Counter)
    forced_exits: int = 0
    kill_trips: int = 0
    halted_now: bool = False
    drawdown_by_session: dict[str, float] = field(default_factory=dict)
    leverage_by_session: dict[str, float] = field(default_factory=dict)
    negative_cash_rows: int = 0
    both_caps: int = 0
    sweep: list[dict[str, Any]] = field(default_factory=list)
    sweep_skipped: int = 0
    wiring: dict[str, bool] = field(default_factory=dict)

    @property
    def enough_record(self) -> bool:
        return len(self.days) >= MIN_DAYS and len(self.venues) >= MIN_VENUES


def _reason_key(reason: str) -> str:
    """Fold a rejection string onto the guard that wrote it."""
    for needle, key in (("kill-switch", "kill_switch"), ("single-name notional", "symbol_notional"),
                        ("gross leverage", "gross_leverage"), ("no room after size caps", "gross_leverage"),
                        ("portfolio heat", "heat"), ("open positions", "max_open_positions"),
                        ("below min", "min_ticket"), ("spectral VaR", "spectral_var"),
                        ("basket", "basket"), ("daily", "daily_budget"), ("card:", "card_declined"),
                        ("unknown broker", "route"), ("broker", "broker")):
        if needle in reason:
            return key
    return "other"


def exits_pass_a_tripped_kill_switch() -> bool:
    """Ask the real gate: can a position still be closed once the kill-switch has tripped?"""
    class _Null:
        name = venue = "probe"

        def positions(self, _account: str) -> list[Position]:
            return [Position(symbol="PROBE", symbolId=0, openQuantity=10.0)]

    with TemporaryDirectory() as d:
        router = Router.build_default(mode="dry-run", broker=_Null(), state_dir=Path(d))  # type: ignore[arg-type]
        router.kill_switch.trip(reason="risk_guard_analysis probe")
        intent = OrderIntent(symbol="PROBE", action=OrderAction.SELL, shares=10, entry=100.0, stop=110.0,
                             target=None, strategy="probe", risk_dollars=0.0, account_number="probe")
        return router._gate(intent, equity=100_000.0, existing_risk=0.0, open_positions=1).accepted


def wiring_checks(src: Path) -> dict[str, bool]:
    """Whether the code that feeds each guard exists. A guard nothing feeds can never fire, and no
    amount of journal data would show that; every count below would just read zero."""
    monitor = (src / "monitor" / "live_loop.py").read_text(encoding="utf-8")
    approval = (src / "execution" / "approval.py").read_text(encoding="utf-8")
    cli = (src / "cli.py").read_text(encoding="utf-8")
    paper = (src / "brokers" / "paper.py").read_text(encoding="utf-8")
    return {
        "monitor can call Router.check_forced_exits (needs `forced_exits: true`)": "check_forced_exits(" in monitor,
        "monitor passes open notional to the gross-leverage gate": "current_open_notional=open_notional" in monitor,
        "card path passes open notional through": "current_open_notional=current_open_notional" in approval,
        "place-order passes open notional": "current_open_notional=open_notional" in cli,
        "paper broker auto-trips the kill-switch (KillSwitch.evaluate)": "_kill_switch.evaluate(" in paper,
        "live books auto-trip the kill-switch (EquityWatch)": "EquityWatch(" in monitor,
        "a tripped kill-switch still lets exits through": exits_pass_a_tripped_kill_switch(),
    }


def _force_exit_sweep(fills: list[dict[str, Any]], equity: list[dict[str, str]],
                      atr_by_intent: dict[str, float]) -> tuple[list[dict[str, Any]], int]:
    """How often each multiple would have fired, on positions whose path the journal can show.

    The equity journal only holds a total, so a position's price is readable only while it is the
    session's one open position. ATR at entry comes from ledger STRATEGY_SIGNAL rows, keyed to the
    fill by session and symbol. Anything else is counted as skipped rather than estimated.
    """
    by_session: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for f in fills:
        by_session[str(f.get("session_id"))].append(f)
    excursions: list[float] = []
    skipped = 0
    for session, rows in by_session.items():
        buys = [f for f in rows if str(f.get("side", "")).lower() == "buy"]
        symbols = {f.get("symbol") for f in rows}
        atr = atr_by_intent.get(f"{session}:{next(iter(symbols))}") if len(symbols) == 1 else None
        if len(symbols) != 1 or atr is None or atr <= 0 or not buys:
            skipped += len(buys)
            continue
        qty, entry = float(buys[0]["quantity"]), float(buys[0]["price"])
        marks = [float(r["positions_value"]) / qty for r in equity
                 if r.get("session_id") == session and float(r.get("positions_value") or 0) > 0]
        if not marks:
            skipped += 1
            continue
        excursions.append(max(0.0, entry - min(marks)) / atr)
    table = [{"mult": m, "would_fire": sum(x > m for x in excursions), "positions": len(excursions)}
             for m in SWEEP]
    return table, skipped


def analyse(state_dir: Path, *, src: Path | None = None) -> Report:
    orders = _jsonl(state_dir / "orders.jsonl")
    rejected = _jsonl(state_dir / "rejected.jsonl")
    fills = _jsonl(state_dir / "paper_fills.jsonl")
    paper_orders = _jsonl(state_dir / "paper_orders.jsonl")
    equity = _equity(state_dir / "paper_equity.csv")
    rows = all_rows(state_dir / "ledger") if (state_dir / "ledger").is_dir() else []

    rep = Report(state_dir=str(state_dir), intents=len(orders), fills=len(fills), ledger_rows=len(rows))
    stamps = [str(r.get("ts", ""))[:10] for r in [*orders, *rejected, *paper_orders, *equity, *rows]]
    stamps += [str(f.get("fill_time", ""))[:10] for f in fills]
    rep.days = sorted({d for d in stamps if d})
    rep.venues = sorted({str(r["venue"]) for r in [*fills, *paper_orders] if r.get("venue")})
    rep.sessions = sorted({str(r["session_id"]) for r in [*fills, *paper_orders, *equity] if r.get("session_id")})
    rep.accepted_entries = sum(1 for o in orders if o.get("accepted") and o.get("action") == "Buy")
    rep.halted_now = (state_dir / "HALTED").exists()

    seen: set[tuple[Any, str]] = set()
    for r in rejected:
        for reason in r.get("reasons") or []:
            seen.add((r.get("intent_id") or id(r), reason))
    for o in orders:
        for reason in o.get("rejected_reasons") or []:
            seen.add((o.get("intent_id") or id(o), reason))
    per_intent: dict[Any, set[str]] = defaultdict(set)
    for intent, reason in seen:
        rep.rejections[_reason_key(reason)] += 1
        per_intent[intent].add(_reason_key(reason))
    rep.both_caps = sum(1 for keys in per_intent.values() if {"symbol_notional", "gross_leverage"} <= keys)

    atr_by_key: dict[str, float] = {}
    for row in rows:
        event, payload = row.get("event"), row.get("payload") or {}
        if event == "RISK_TRIMMED":
            rep.trims[str(payload.get("cap"))] += 1
        elif event == "FORCED_EXIT":
            rep.forced_exits += 1
        elif event == "KILL_SWITCH_TRIPPED":
            rep.kill_trips += 1
        elif event == "STRATEGY_SIGNAL" and payload.get("atr"):
            atr_by_key.setdefault(f"{row.get('session_id')}:{payload.get('symbol')}", float(payload["atr"]))

    for r in equity:
        session = str(r.get("session_id"))
        eq = float(r.get("equity") or 0)
        rep.drawdown_by_session[session] = max(rep.drawdown_by_session.get(session, 0.0),
                                               float(r.get("drawdown_pct") or 0))
        if eq > 0:
            rep.leverage_by_session[session] = max(rep.leverage_by_session.get(session, 0.0),
                                                   float(r.get("positions_value") or 0) / eq)
        rep.negative_cash_rows += float(r.get("cash") or 0) < 0

    rep.sweep, rep.sweep_skipped = _force_exit_sweep(fills, equity, atr_by_key)
    rep.wiring = wiring_checks(src or REPO / "src" / "trading_live_claude")
    return rep


def _binding(rep: Report, reject_key: str, trim_key: str) -> str:
    hits = rep.rejections[reject_key] + rep.trims[trim_key]
    if not rep.accepted_entries:
        return f"{hits} hits, no accepted entries to rate them against"
    rate = hits / rep.accepted_entries
    lo, hi = BINDING_BAND
    return f"{hits} hits over {rep.accepted_entries} accepted entries = {rate:.1%} (design band {lo:.0%}-{hi:.0%})"


def render(rep: Report, *, today: str) -> str:
    lines = [f"# Risk-guard validation, {today}", ""]
    lines += [f"Read from `{rep.state_dir}`. Router intents: {rep.intents}. Paper fills: {rep.fills}. "
              f"Sessions: {len(rep.sessions)}. Ledger rows: {rep.ledger_rows}. "
              f"Days: {len(rep.days)} ({', '.join(rep.days) or 'none'}). "
              f"Venues: {', '.join(rep.venues) or 'none'}.", ""]
    if rep.enough_record:
        lines += ["**Record:** meets the minimum window. Read the rates below as evidence.", ""]
    else:
        lines += [f"**Record: not enough to calibrate anything.** The analysis needs at least {MIN_DAYS} days "
                  f"of paper sessions on at least {MIN_VENUES} venues. The counts below describe what "
                  "happened; they support no threshold change.", ""]

    lines += ["## Can each guard fire at all?", "", "| Check | State |", "|---|---|"]
    lines += [f"| {k} | {'yes' if v else '**NO**'} |" for k, v in rep.wiring.items()]
    lines += [""]

    lines += ["## Per guard", ""]
    worst_dd = max(rep.drawdown_by_session.values(), default=0.0)
    over = {t: sum(d >= t for d in rep.drawdown_by_session.values()) for t in KILL_THRESHOLDS}
    lines += [f"- **Kill-switch.** {rep.kill_trips} trips in the ledger, {rep.rejections['kill_switch']} rejections, "
              f"`HALTED` {'present' if rep.halted_now else 'absent'}. Worst session drawdown {worst_dd:.2%}. "
              + "; ".join(f"sessions reaching {t:.0%}: {n}" for t, n in over.items()) + "."]
    lines += [f"- **Per-symbol notional cap.** {_binding(rep, 'symbol_notional', 'symbol_notional')}."]
    lines += [f"- **Gross-leverage cap.** {_binding(rep, 'gross_leverage', 'gross_leverage')}. Highest gross "
              f"leverage seen in a session: {max(rep.leverage_by_session.values(), default=0.0):.2f}x. "
              f"Equity rows with negative cash: {rep.negative_cash_rows}."]
    lines += [f"- **Forced exit.** {rep.forced_exits} `FORCED_EXIT` rows."]
    if rep.rejections:
        lines += ["", "Every rejection by guard: "
                  + ", ".join(f"{k} {v}" for k, v in sorted(rep.rejections.items())) + "."]
    lines += [""]

    lines += ["## Forced-exit multiple sweep", ""]
    usable = rep.sweep[0]["positions"] if rep.sweep else 0
    if usable:
        lines += ["| Multiple of ATR at entry | Positions it would have closed | Of |", "|---|---|---|"]
        lines += [f"| {r['mult']:.1f} | {r['would_fire']} | {r['positions']} |" for r in rep.sweep]
    else:
        lines += ["No position has both a readable price path and a recorded ATR at entry, so no multiple "
                  "can be scored. ATR at entry is on ledger `STRATEGY_SIGNAL` rows from now on."]
    if rep.sweep_skipped:
        lines += ["", f"{rep.sweep_skipped} position(s) skipped: more than one symbol in the session, or no "
                  "ATR recorded."]
    lines += [""]

    lines += ["## Interactions", "",
              f"- Intents rejected by both size caps: {rep.both_caps}. Repeated hits would mean the two caps are "
              "collinear at their defaults."]
    return "\n".join(lines) + "\n"


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--state-dir", type=Path, default=Path("state"))
    ap.add_argument("--out", type=Path, default=None)
    args = ap.parse_args(argv)
    today = datetime.now(UTC).date().isoformat()
    out = args.out or Path("reports") / f"risk_guard_analysis_{today}.md"
    rep = analyse(args.state_dir)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(render(rep, today=today), encoding="utf-8")
    print(f"wrote {out}", file=sys.stdout)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
