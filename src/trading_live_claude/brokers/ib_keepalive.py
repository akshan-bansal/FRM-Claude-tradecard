"""Keeps a Client Portal Gateway session alive, and warns before it dies.

Moved from ``scripts/paper_ib.py`` so a money session on IB gets the same keep-alive as a paper one.
"""
from __future__ import annotations

import threading
import time
from collections.abc import Callable

from ..logging_setup import get_logger
from .ib_web import IBWebBroker

log = get_logger(__name__)


class TickleThread(threading.Thread):
    """Daemon thread that pings CP Gateway's ``/tickle`` on a cadence and monitors session age.

    The Gateway drops the session after a few minutes of idle, so a 90s tickle keeps it warm
    between our 300s polls. Independent of that idle cap, CP Gateway itself hard-caps a session
    at ~24h even when kept tickled — after that the session dies and only a fresh browser login
    at ``https://localhost:5000`` restores it. Neither QT nor Kraken has this: their brokers
    auto-refresh their own tokens. This thread therefore also:

    * Escalates a WARN at ``warn_after_hours`` (default 20h) — enough runway to schedule a
      re-auth manually before it hard-fails mid-poll.
    * Escalates a CRITICAL at ``critical_after_hours`` (default 23h) — session is minutes away
      from expiring.
    * Escalates a CRITICAL on any tickle failure whose message looks like an auth expiry.

    ``warn_fn`` is a plain callable ``(level, message) → None``; a session points it at its Alerter.
    """

    def __init__(self, broker: IBWebBroker, interval_s: float = 90.0, *,
                 warn_after_hours: float = 20.0, critical_after_hours: float = 23.0,
                 warn_fn: Callable[[str, str], object] | None = None) -> None:
        super().__init__(daemon=True, name="ibweb-tickle")
        self.broker = broker
        self.interval_s = interval_s
        self._stop_event = threading.Event()
        self._started_at = time.monotonic()
        self._warn_after_s = warn_after_hours * 3600.0
        self._critical_after_s = critical_after_hours * 3600.0
        self.warn_fn = warn_fn or (lambda level, msg: log.warning("ibweb.session", level=level, message=msg))
        # Ensure each escalation lands exactly once per instance (not once per tickle after the
        # threshold is crossed) so the phone doesn't get spammed every 90s for the last four hours.
        self._warn_fired = False
        self._critical_fired = False

    def stop(self) -> None:
        self._stop_event.set()

    def _age_hours(self) -> float:
        return (time.monotonic() - self._started_at) / 3600.0

    def _check_session_age(self) -> None:
        elapsed = time.monotonic() - self._started_at
        if not self._critical_fired and elapsed >= self._critical_after_s:
            self._critical_fired = True
            self.warn_fn("CRITICAL",
                         f"IB Web session is {self._age_hours():.1f}h old — expires imminently. "
                         f"Re-auth at https://localhost:5000 NOW to avoid mid-poll failure.")
        elif not self._warn_fired and elapsed >= self._warn_after_s:
            self._warn_fired = True
            self.warn_fn("WARN",
                         f"IB Web session is {self._age_hours():.1f}h old — CP Gateway caps at "
                         f"~24h. Plan a re-auth at https://localhost:5000 before the window closes.")

    def run(self) -> None:
        # Wait BEFORE the first tickle so we don't stampede against the first quote call.
        while not self._stop_event.wait(self.interval_s):
            try:
                self.broker.tickle()
            except Exception as e:                        # pragma: no cover
                # A tickle failure this late in the loop almost always means the session died —
                # fire a CRITICAL once so the human sees it in the alert stream. Suppress repeats
                # of the same failure so the log doesn't fill with the same line every 90s.
                msg = str(e).lower()
                if not self._critical_fired and (
                    "unauthorized" in msg or "not authenticated" in msg or "401" in msg or "403" in msg
                ):
                    self._critical_fired = True
                    self.warn_fn("CRITICAL", f"IB Web tickle failed — session likely expired: {e}. "
                                             f"Re-auth at https://localhost:5000.")
                else:
                    log.warning("ibweb.tickle_failed", error=str(e))
            self._check_session_age()
