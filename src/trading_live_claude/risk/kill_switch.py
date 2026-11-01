"""Kill-switch: a single sentinel file stops all new risk.

The sentinel is intentionally a file (not a DB row) so flipping it is
trivial — `touch state/HALTED` from any shell terminates all entries.
An order that only reduces a held position still passes (``Router._gate``).
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from ..logging_setup import get_logger

log = get_logger(__name__)


@dataclass
class KillState:
    halted: bool
    reason: str
    activated_at: datetime | None


class KillSwitch:
    SENTINEL_NAME = "HALTED"
    # When a human last cleared it. Drawdown is measured from here (risk/equity_watch.py).
    CLEARED_NAME = "HALT_CLEARED"

    def __init__(
        self,
        state_dir: Path,
        max_drawdown_pct: float = 0.10,
        daily_loss_limit_pct: float = 0.03,
    ) -> None:
        self.state_dir = state_dir
        self.state_dir.mkdir(parents=True, exist_ok=True)
        self.max_drawdown_pct = max_drawdown_pct
        self.daily_loss_limit_pct = daily_loss_limit_pct

    @property
    def _path(self) -> Path:
        return self.state_dir / self.SENTINEL_NAME

    def state(self) -> KillState:
        if not self._path.exists():
            return KillState(halted=False, reason="", activated_at=None)
        try:
            content = self._path.read_text(encoding="utf-8").strip()
        except OSError:
            content = ""
        return KillState(
            halted=True,
            reason=content or "unknown",
            activated_at=datetime.fromtimestamp(self._path.stat().st_mtime, tz=UTC),
        )

    def trip(self, reason: str) -> None:
        before = self.state()
        self._path.write_text(f"{datetime.now(UTC).isoformat()} :: {reason}\n", encoding="utf-8")
        log.error("kill_switch.tripped", reason=reason)
        # evaluate() re-trips on every poll while a limit is breached; only a change is an event.
        if not before.halted or not before.reason.endswith(f":: {reason}"):
            self._record("KILL_SWITCH_TRIPPED", {"reason": reason, "was_halted": before.halted})

    def clear(self, ack: str) -> None:
        """Remove the sentinel. Requires explicit ack text to discourage accidental clears."""
        if ack.strip().upper() != "I HAVE INVESTIGATED":
            raise PermissionError("Refusing to clear kill switch without explicit ack.")
        if self._path.exists():
            reason = self.state().reason
            self._path.unlink()
            (self.state_dir / self.CLEARED_NAME).write_text(datetime.now(UTC).isoformat(), encoding="utf-8")
            log.warning("kill_switch.cleared")
            self._record("KILL_SWITCH_CLEARED", {"cleared_reason": reason, "ack": ack.strip()})

    def last_cleared(self) -> datetime | None:
        """When the switch was last cleared by a human, or None if it never has been."""
        try:
            return datetime.fromisoformat((self.state_dir / self.CLEARED_NAME).read_text(encoding="utf-8").strip())
        except (OSError, ValueError):
            return None

    def _record(self, event: str, payload: dict[str, object]) -> None:
        """Append to the shared ``control`` ledger stream. Never stops a trip or a clear.

        The stream is shared because a trip comes from whichever process sees the breach: a
        running book, the daemon, or ``trading kill`` in a shell.
        """
        try:
            from ..audit.ledger import Ledger
            Ledger(self.state_dir / "ledger", stream="control", shared=True).append(event, payload)
        except Exception as e:                          # the sentinel file is the control
            log.error("kill_switch.ledger_failed", ledger_event=event, error=str(e))

    def evaluate(self, *, equity: float, peak_equity: float, day_open_equity: float) -> KillState:
        """Decide whether to trip based on current account state."""
        if equity <= 0:
            self.trip("equity <= 0")
            return self.state()
        dd = 1.0 - (equity / peak_equity) if peak_equity > 0 else 0.0
        if dd >= self.max_drawdown_pct:
            self.trip(f"max-drawdown {dd:.2%} >= {self.max_drawdown_pct:.2%}")
            return self.state()
        daily = 1.0 - (equity / day_open_equity) if day_open_equity > 0 else 0.0
        if daily >= self.daily_loss_limit_pct:
            self.trip(f"daily-loss {daily:.2%} >= {self.daily_loss_limit_pct:.2%}")
            return self.state()
        return self.state()
