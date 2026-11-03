"""Regular trading hours per venue, and whether a symbol's venue is open.

Exchange hopping, level 1: the monitor skips a symbol while its venue is closed, so it neither spends
broker calls nor acts on a stale quote off-hours. It enables no new trading.

Hours only, in each venue's own time zone, so daylight saving follows from the zone. There is no
holiday or half-day calendar: on a holiday a venue reads as open and the symbol is polled as it always
was. That errs towards a wasted poll, never towards hiding a live session. Futures all use the Globex
week, which is approximate for roots with a midday break (CBOT grains).
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, time
from zoneinfo import ZoneInfo

from .asset_spec import FutureSpec, spec_for

Session = tuple[time, time]

_MON_FRI = (0, 1, 2, 3, 4)
_ALL_DAY: Session = (time.min, time.max)


def _weekdays(*sessions: Session) -> dict[int, tuple[Session, ...]]:
    return {d: sessions for d in _MON_FRI}


@dataclass(frozen=True)
class Venue:
    name: str
    tz: str
    # Weekday (Monday = 0) -> that day's sessions in local time, as [open, close). A day that is
    # missing is a closed day.
    hours: dict[int, tuple[Session, ...]]

    def is_open(self, ts: datetime | None = None) -> bool:
        ts = ts or datetime.now(UTC)
        local = (ts if ts.tzinfo else ts.replace(tzinfo=UTC)).astimezone(ZoneInfo(self.tz))
        return any(o <= local.time() < c for o, c in self.hours.get(local.weekday(), ()))


VENUES: dict[str, Venue] = {v.name: v for v in (
    Venue("crypto", "UTC", {d: (_ALL_DAY,) for d in range(7)}),
    # FX: Sunday 17:00 to Friday 17:00, New York.
    Venue("fx", "America/New_York", {6: ((time(17), time.max),), 0: (_ALL_DAY,), 1: (_ALL_DAY,),
                                     2: (_ALL_DAY,), 3: (_ALL_DAY,), 4: ((time.min, time(17)),)}),
    # CME Globex: Sunday 18:00 to Friday 17:00, New York, with the daily 17:00-18:00 break.
    Venue("globex", "America/New_York", {
        6: ((time(18), time.max),),
        **{d: ((time.min, time(17)), (time(18), time.max)) for d in (0, 1, 2, 3)},
        4: ((time.min, time(17)),)}),
    Venue("us", "America/New_York", _weekdays((time(9, 30), time(16)))),
    Venue("tsx", "America/Toronto", _weekdays((time(9, 30), time(16)))),
    Venue("lse", "Europe/London", _weekdays((time(8), time(16, 30)))),
    Venue("xetra", "Europe/Berlin", _weekdays((time(9), time(17, 30)))),
    Venue("asx", "Australia/Sydney", _weekdays((time(10), time(16)))),
    Venue("tokyo", "Asia/Tokyo", _weekdays((time(9), time(11, 30)), (time(12, 30), time(15, 30)))),
    Venue("hkex", "Asia/Hong_Kong", _weekdays((time(9, 30), time(12)), (time(13), time(16)))),
)}

# Listing suffix -> venue. No suffix means a US listing, as everywhere else in this repo.
LISTING_SUFFIXES: dict[str, str] = {
    ".TO": "tsx", ".V": "tsx", ".NE": "tsx", ".CN": "tsx",
    ".L": "lse", ".DE": "xetra", ".AX": "asx", ".T": "tokyo", ".HK": "hkex",
}


def venue_of(symbol: str) -> Venue:
    """The venue whose hours govern ``symbol``: its asset class first, then its listing suffix."""
    spec = spec_for(symbol)
    if spec.asset_class == "crypto":
        return VENUES["crypto"]
    if spec.asset_class == "fx":
        return VENUES["fx"]
    if isinstance(spec, FutureSpec):
        return VENUES["globex"]
    up = symbol.strip().upper()
    for suffix, venue in LISTING_SUFFIXES.items():
        if up.endswith(suffix):
            return VENUES[venue]
    return VENUES["us"]


def is_open(venue: str, ts: datetime | None = None) -> bool:
    """Whether a named venue is in regular hours at ``ts`` (now, if omitted; naive = UTC)."""
    return VENUES[venue].is_open(ts)


def symbol_is_open(symbol: str, ts: datetime | None = None) -> bool:
    """Whether ``symbol``'s venue is in regular hours. This is what the monitor's guard calls."""
    return venue_of(symbol).is_open(ts)


__all__ = ["LISTING_SUFFIXES", "VENUES", "Venue", "is_open", "symbol_is_open", "venue_of"]
