"""Equity journal and drawdown auto-halt for a live book.

``PaperBroker._journal_equity`` journals equity and calls ``KillSwitch.evaluate`` on every mark for a
paper book. A live broker does neither, so without this the drawdown and daily-loss limits never trip
on ``trading live`` or ``autonomous_run``. This gives a live book the same two things: the same
columns as ``paper_equity.csv`` (plus which account and mode the row belongs to) and the same call.

The difference is where the peak lives. A paper session starts fresh; a real account does not, so the
peak and the day's open are read back from the journal, and restarting a process cannot forget a
drawdown. They are measured from the last human clear, because a breach measured from the old peak
would re-trip on the next poll and no one could ever resume.

A withdrawal looks like a loss here, and trips the same way. That errs towards stopping.
"""
from __future__ import annotations

import csv
from datetime import UTC, datetime
from pathlib import Path

from ..logging_setup import get_logger
from .kill_switch import KillState, KillSwitch

log = get_logger(__name__)

COLUMNS = ("ts", "session_id", "account", "mode", "currency", "equity", "cash", "positions_value",
           "realized_pnl", "unrealized_pnl", "peak_equity", "drawdown_pct")


class EquityWatch:
    FILENAME = "live_equity.csv"

    def __init__(self, kill_switch: KillSwitch, *, account: str, mode: str, currency: str,
                 session_id: str | None = None) -> None:
        self.kill_switch = kill_switch
        self.path: Path = kill_switch.state_dir / self.FILENAME
        self.account, self.mode, self.currency = str(account), mode, currency
        self.session_id = session_id or ""
        self._peak, self._day, self._day_open = self._resume(datetime.now(UTC))
        self._halted = kill_switch.state().halted

    def _resume(self, now: datetime) -> tuple[float, str, float]:
        """Peak and today's open for this account, mode and currency, since the last clear."""
        peak, day_open = 0.0, 0.0
        today = now.date().isoformat()
        cleared = self.kill_switch.last_cleared()
        try:
            with self.path.open(encoding="utf-8", newline="") as f:
                for row in csv.DictReader(f):
                    try:
                        if (row["account"], row["mode"], row["currency"]) != (self.account, self.mode, self.currency):
                            continue
                        if cleared is not None and datetime.fromisoformat(row["ts"]) <= cleared:
                            continue
                        equity = float(row["equity"])
                    except (KeyError, TypeError, ValueError):   # a torn last line from a crash
                        continue
                    peak = max(peak, equity)
                    if not day_open and row["ts"][:10] == today:
                        day_open = equity
        except FileNotFoundError:
            pass
        except OSError as e:
            log.error("equity_watch.resume_failed", path=str(self.path), error=str(e))
        return peak, today, day_open

    def observe(self, equity: float, *, positions_value: float = 0.0, realized_pnl: float = 0.0,
                unrealized_pnl: float = 0.0, now: datetime | None = None) -> KillState:
        """Journal this mark, then trip the kill-switch if it breaches a limit.

        While the switch is already tripped nothing is evaluated, so a manual stop keeps its reason.
        """
        now = now or datetime.now(UTC)
        halted = self.kill_switch.state().halted
        if self._halted and not halted:
            self._peak, self._day_open = 0.0, 0.0
        self._halted = halted

        day = now.date().isoformat()
        if day != self._day or self._day_open <= 0:
            self._day, self._day_open = day, equity
        self._peak = max(self._peak, equity)
        self._write(now, equity, positions_value, realized_pnl, unrealized_pnl)

        if halted:
            return self.kill_switch.state()
        state = self.kill_switch.evaluate(equity=equity, peak_equity=self._peak,
                                          day_open_equity=self._day_open)
        self._halted = state.halted
        return state

    def _write(self, now: datetime, equity: float, positions_value: float, realized_pnl: float,
               unrealized_pnl: float) -> None:
        """Append one row. A failed write is logged and never stops the evaluation after it."""
        drawdown = 1.0 - equity / self._peak if self._peak > 0 else 0.0
        row = {"ts": now.isoformat(), "session_id": self.session_id, "account": self.account,
               "mode": self.mode, "currency": self.currency, "equity": round(equity, 4),
               # Derived: a live broker's equity call does not report cash on its own.
               "cash": round(equity - positions_value, 4), "positions_value": round(positions_value, 4),
               "realized_pnl": round(realized_pnl, 4), "unrealized_pnl": round(unrealized_pnl, 4),
               "peak_equity": round(self._peak, 4), "drawdown_pct": round(drawdown, 6)}
        try:
            is_new = not self.path.exists()
            with self.path.open("a", encoding="utf-8", newline="") as f:
                w = csv.DictWriter(f, fieldnames=COLUMNS)
                if is_new:
                    w.writeheader()
                w.writerow(row)
        except OSError as e:
            log.error("equity_watch.journal_failed", path=str(self.path), error=str(e))
