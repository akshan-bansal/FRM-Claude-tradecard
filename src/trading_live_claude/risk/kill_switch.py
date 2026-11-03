"""Kill-switch: a single sentinel file stops all new risk.

The sentinel is intentionally a file (not a DB row) so flipping it is
trivial — `touch state/HALTED` from any shell terminates all entries.
An order that only reduces a held position still passes (``Router._gate``).

A simulated book trips its own sentinel, ``HALTED_PAPER``: its drawdown stops paper, and money keeps
running until a human says otherwise (``promote_paper_halt``, `trading kill --from-paper`). Every
session stops for ``HALTED``.
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
    PAPER_SENTINEL_NAME = "HALTED_PAPER"
    # When a human last cleared it. Drawdown is measured from here (risk/equity_watch.py).
    CLEARED_NAME = "HALT_CLEARED"
    PAPER_CLEARED_NAME = "HALT_CLEARED_PAPER"
    PROMOTE_PHRASE = "HALT MONEY"

    def __init__(
        self,
        state_dir: Path,
        max_drawdown_pct: float = 0.10,
        daily_loss_limit_pct: float = 0.03,
        *,
        paper: bool = False,
    ) -> None:
        self.paper = paper
        self.state_dir = state_dir
        self.state_dir.mkdir(parents=True, exist_ok=True)
        self.max_drawdown_pct = max_drawdown_pct
        self.daily_loss_limit_pct = daily_loss_limit_pct

    @property
    def _path(self) -> Path:
        return self.state_dir / (self.PAPER_SENTINEL_NAME if self.paper else self.SENTINEL_NAME)

    @property
    def _cleared_path(self) -> Path:
        return self.state_dir / (self.PAPER_CLEARED_NAME if self.paper else self.CLEARED_NAME)

    @staticmethod
    def _read(path: Path) -> KillState:
        if not path.exists():
            return KillState(halted=False, reason="", activated_at=None)
        try:
            content = path.read_text(encoding="utf-8").strip()
        except OSError:
            content = ""
        return KillState(
            halted=True,
            reason=content or "unknown",
            activated_at=datetime.fromtimestamp(path.stat().st_mtime, tz=UTC),
        )

    def state(self) -> KillState:
        """Everyone stops for ``HALTED``. A simulated book also stops for its own sentinel."""
        main = self._read(self.state_dir / self.SENTINEL_NAME)
        return main if main.halted or not self.paper else self._read(self._path)

    def paper_only_halt(self) -> bool:
        """True when only the simulated books have stopped, and money is still running."""
        return not (self.state_dir / self.SENTINEL_NAME).exists() and (self.state_dir / self.PAPER_SENTINEL_NAME).exists()

    def promote_paper_halt(self, ack: str) -> str:
        """A human's express approval that a paper halt should stop money too. Returns the reason written."""
        if ack.strip().upper() != self.PROMOTE_PHRASE:
            raise PermissionError(f'Refusing to halt money on a paper halt without: "{self.PROMOTE_PHRASE}"')
        held = self._read(self.state_dir / self.PAPER_SENTINEL_NAME)
        if not held.halted:
            raise ValueError("No paper halt to promote. `trading kill` stops everything directly.")
        reason = f"paper halt approved by a human: {held.reason}"
        KillSwitch(self.state_dir).trip(reason)
        return reason

    def trip(self, reason: str) -> None:
        before = self.state()
        self._path.write_text(f"{datetime.now(UTC).isoformat()} :: {reason}\n", encoding="utf-8")
        scope = "paper" if self.paper else "all"
        log.error("kill_switch.tripped", reason=reason, scope=scope)
        # evaluate() re-trips on every poll while a limit is breached; only a change is an event.
        if not before.halted or not before.reason.endswith(f":: {reason}"):
            self._record("KILL_SWITCH_TRIPPED", {"reason": reason, "was_halted": before.halted, "scope": scope})

    def clear(self, ack: str) -> None:
        """Remove the sentinel. Requires explicit ack text to discourage accidental clears."""
        if ack.strip().upper() != "I HAVE INVESTIGATED":
            raise PermissionError("Refusing to clear kill switch without explicit ack.")
        if self._path.exists():
            reason = self._read(self._path).reason
            self._path.unlink()
            self._cleared_path.write_text(datetime.now(UTC).isoformat(), encoding="utf-8")
            log.warning("kill_switch.cleared", scope="paper" if self.paper else "all")
            self._record("KILL_SWITCH_CLEARED", {"cleared_reason": reason, "ack": ack.strip(),
                                                 "scope": "paper" if self.paper else "all"})

    def last_cleared(self) -> datetime | None:
        """When the switch was last cleared by a human, or None if it never has been."""
        try:
            return datetime.fromisoformat(self._cleared_path.read_text(encoding="utf-8").strip())
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
