"""The allocator bias compares names on the same trading day, never by row number.

Both session callers fed the allocator returns keyed by row, so two names were paired by position: days
apart once their calendars' holidays differed, and a short history was paired with the oldest rows of
everyone else. These tests fail on that pairing. They also pin the trap a raw-timestamp key would fall
into (venues stamp a daily bar at different hours, Sydney on the prior UTC evening), and what happens
when names share too few days to compare.
"""
from __future__ import annotations

from collections.abc import Sequence
from types import SimpleNamespace
from typing import Any

import numpy as np
import pandas as pd
import pytest

from trading_live_claude.analysis.venue_calendar import venue_of
from trading_live_claude.monitor import session

END = "2026-11-02"
DAYS = pd.bdate_range(end=END, periods=520)
# Holidays one calendar keeps and the other does not, 2025-26.
US_ONLY = pd.to_datetime(["2025-11-27", "2026-01-19", "2026-05-25", "2026-06-19", "2026-07-03"])
CA_ONLY = pd.to_datetime(["2025-10-13", "2025-12-26", "2026-05-18", "2026-07-01", "2026-08-03", "2026-10-12"])
_CLOCK = {"America/New_York": "09:30", "America/Toronto": "00:00", "Australia/Sydney": "09:59"}


def _stamps(days: pd.DatetimeIndex, tz: str, clock: str) -> pd.DatetimeIndex:
    """Each day at ``clock`` on the venue's own wall clock, as the UTC instant a feed would record."""
    return pd.DatetimeIndex([pd.Timestamp(f"{d:%Y-%m-%d} {clock}", tz=tz) for d in days]).tz_convert("UTC")


def _bars(symbol: str, rets: pd.Series, *, closed: Sequence[pd.Timestamp] = (), bare: bool = False) -> pd.DataFrame:
    """Bars as MarketData returns them: a RangeIndex, and ``time`` stamped the way a feed stamps them."""
    closes = 100.0 * (1.0 + rets).cumprod()
    closes = closes[~closes.index.isin(pd.DatetimeIndex(closed))]
    tz = venue_of(symbol).tz
    when = (pd.DatetimeIndex(closes.index).tz_localize("UTC") if bare
            else _stamps(pd.DatetimeIndex(closes.index), tz, _CLOCK.get(tz, "09:30")))
    return pd.DataFrame({"time": when, "close": closes.to_numpy()})


class _Market:
    """``recent`` and ``history`` shaped like :class:`~trading_live_claude.data.market.MarketData`."""

    def __init__(self, frames: dict[str, pd.DataFrame]) -> None:
        self.frames = frames

    def recent(self, symbol: str, bars: int = 200, interval: str = "1d") -> pd.DataFrame:
        return self.frames[symbol].tail(bars).reset_index(drop=True)

    def history(self, symbol: str, years: float = 3.0, interval: str = "1d") -> pd.DataFrame:
        return self.frames[symbol].reset_index(drop=True)


def _noise(seed: int, days: pd.DatetimeIndex = DAYS, sd: float = 0.01, drift: float = 0.0) -> pd.Series:
    return pd.Series(np.random.default_rng(seed).normal(drift, sd, len(days)), index=days)


def _validated(monkeypatch: pytest.MonkeyPatch, names: Sequence[str]) -> None:
    table = {s: SimpleNamespace(oos_score=1.0) for s in names}
    monkeypatch.setattr("trading_live_claude.analysis.universe.WALK_FORWARD_VALIDATED", table)


# ---------------------------------------------------------------- the date key


@pytest.mark.parametrize("symbol, bare", [("SPY", False), ("XIC.TO", False), ("BHP.AX", False), ("SPY", True)])
def test_a_bar_is_dated_by_the_day_its_venue_traded(symbol: str, bare: bool) -> None:
    # Across both DST changes of March and April 2026: New York's opens move 14:30Z -> 13:30Z, and
    # Sydney's 09:59 falls on the prior UTC evening throughout. A bare date (midnight UTC) is its own day.
    days = pd.bdate_range("2026-03-02", "2026-04-17")
    rets = session._dated_returns(_bars(symbol, _noise(1, days), bare=bare), symbol)
    assert list(rets.index) == list(days[1:])


def test_venues_stamped_hours_apart_still_share_every_day() -> None:
    days = pd.bdate_range("2026-03-02", "2026-04-17")
    us, sydney = _bars("SPY", _noise(1, days)), _bars("BHP.AX", _noise(2, days))
    assert not set(us["time"]) & set(sydney["time"])        # a raw timestamp would share nothing
    shared = session._dated_returns(us, "SPY").index.intersection(session._dated_returns(sydney, "BHP.AX").index)
    assert len(shared) == len(days) - 1                      # Mondays included


# ---------------------------------------------------------------- what the bias compares

def _pairs_book(closed_for: dict[str, Sequence[pd.Timestamp]]) -> tuple[dict[str, pd.DataFrame], list[str], list[str]]:
    """Two pairs that move together on the same day, each a Toronto and a US name, and four loners."""
    f, g = _noise(10), _noise(11)
    paired = {"AAA.TO": f, "CCC": f, "BBB.TO": g, "DDD": g}
    loners = {s: _noise(20 + i) for i, s in enumerate(["EEE", "FFF", "GGG.TO", "HHH.TO"])}
    rets = {s: r + _noise(30 + i, sd=0.002) for i, (s, r) in enumerate(paired.items())} | loners
    frames = {s: _bars(s, r, closed=closed_for.get("ca" if s.endswith(".TO") else "us", ())) for s, r in rets.items()}
    return frames, list(paired), list(loners)


def test_names_on_different_holidays_are_compared_on_the_same_day(monkeypatch: pytest.MonkeyPatch) -> None:
    frames, paired, loners = _pairs_book({"us": US_ONLY, "ca": CA_ONLY})
    _validated(monkeypatch, frames)
    bias = session.allocator_weight_bias(_Market(frames), list(frames))
    assert bias is not None
    # Each pair is one bet, so each of its names gets about half a loner's bias. Paired by row, the
    # holidays put most rows a day or two apart and the pairs looked independent (ratio near 0.8).
    ratio = max(bias(s) for s in paired) / min(bias(s) for s in loners)
    assert ratio < 0.65


def test_a_short_history_is_compared_with_the_same_days_of_the_rest(monkeypatch: pytest.MonkeyPatch) -> None:
    # SSS has 80 bars, all recent, and moves with AAA over them; earlier, AAA moved on its own. Paired by
    # row, SSS's 80 bars met the oldest rows of every other name, where AAA had nothing to do with it.
    recent = DAYS[-80:]
    g = _noise(40)
    aaa = _noise(41).where(~DAYS.isin(recent), g) + _noise(42, sd=0.002)
    sss = g.loc[recent] + _noise(43, recent, sd=0.002)
    rets = {"AAA": aaa, "SSS": sss} | {s: _noise(50 + i) for i, s in enumerate(["BBB", "CCC", "DDD", "EEE", "FFF"])}
    frames = {s: _bars(s, r) for s, r in rets.items()}
    _validated(monkeypatch, frames)
    bias = session.allocator_weight_bias(_Market(frames), list(frames))
    assert bias is not None
    others = min(bias(s) for s in ["BBB", "CCC", "DDD", "EEE", "FFF"])
    assert bias("SSS") / others < 0.65 and bias("AAA") / others < 0.65


def test_stamps_hours_apart_still_pair_on_ib(monkeypatch: pytest.MonkeyPatch) -> None:
    # The trap a raw-timestamp key falls into: New York at 13:30Z/14:30Z, Sydney the UTC evening before.
    f, g = _noise(60, drift=0.001), _noise(61, drift=0.001)
    rets = {"SPY": f, "STW.AX": f, "QQQ": g, "BHP.AX": g}
    rets = {s: r + _noise(70 + i, sd=0.002) for i, (s, r) in enumerate(rets.items())}
    loners = ["GLD", "IAU", "CBA.AX", "CSL.AX"]           # no government bond: IB holds those dormant at 0
    rets |= {s: _noise(80 + i, drift=0.001) for i, s in enumerate(loners)}
    frames = {s: _bars(s, r) for s, r in rets.items()}
    bias = session._ib_weight_bias(_Market(frames), list(frames), None)
    assert max(bias(s) for s in ["SPY", "STW.AX", "QQQ", "BHP.AX"]) < min(bias(s) for s in loners)


# ---------------------------------------------------------------- too little in common


def _apart() -> dict[str, pd.DataFrame]:
    """Two names a year apart: no trading day in common."""
    return {"AAA": _bars("AAA", _noise(90, DAYS[:120], drift=0.002)),
            "BBB": _bars("BBB", _noise(91, DAYS[-120:], drift=0.002))}


def test_no_shared_day_turns_the_bias_off_rather_than_flooring_it(monkeypatch: pytest.MonkeyPatch) -> None:
    frames = _apart()
    _validated(monkeypatch, frames)
    said: list[str] = []
    assert session.allocator_weight_bias(_Market(frames), list(frames), emit=said.append) is None
    assert any("bias OFF" in s for s in said)
    ib = session._ib_weight_bias(_Market(frames), list(frames), None)
    assert ib("AAA") == 1.0 and ib("BBB") == 1.0


def _with_a_stranger() -> dict[str, pd.DataFrame]:
    """Six names over the last 300 days, and SSS, whose 60 days share only the first 30 of theirs."""
    late, stranger = DAYS[-300:], DAYS[-330:-270]
    frames = {s: _bars(s, _noise(100 + i, late, drift=0.001)) for i, s in enumerate(["AAA", "BBB", "CCC", "DDD", "EEE", "FFF"])}
    frames["SSS"] = _bars("SSS", _noise(110, stranger, drift=0.001))
    return frames


def test_a_name_sharing_too_few_days_is_set_aside_at_neutral(monkeypatch: pytest.MonkeyPatch) -> None:
    frames = _with_a_stranger()
    _validated(monkeypatch, frames)
    said: list[str] = []
    bias = session.allocator_weight_bias(_Market(frames), list(frames), emit=said.append)
    assert bias is not None
    assert bias("SSS") == 1.0
    assert any("SSS" in s and "neutral" in s for s in said)
    # The rest are allocated among themselves: six names, so the 0.20 cap is reachable.
    assert any(bias(s) != 1.0 for s in ["AAA", "BBB", "CCC", "DDD", "EEE", "FFF"])


def test_with_the_overlay_a_set_aside_name_takes_only_its_class_scalar(monkeypatch: pytest.MonkeyPatch) -> None:
    frames = _with_a_stranger()
    _validated(monkeypatch, frames)
    decision = SimpleNamespace(asset_class="equity", scalar=0.8, halt=False)

    def overlay_for(_symbol: str) -> Any:
        return decision

    bias = session.allocator_weight_bias(_Market(frames), list(frames), overlay_for)
    assert bias is not None
    assert bias("SSS") == pytest.approx(0.8)


def test_on_ib_a_short_or_set_aside_name_keeps_one(monkeypatch: pytest.MonkeyPatch) -> None:
    frames = _with_a_stranger()
    frames["TINY"] = _bars("TINY", _noise(120, DAYS[-40:], drift=0.001))   # under the 60 bars IB needs
    said: list[str] = []
    bias = session._ib_weight_bias(_Market(frames), list(frames), None, emit=said.append)
    assert bias("SSS") == 1.0 and bias("TINY") == 1.0
    assert any(bias(s) != 1.0 for s in ["AAA", "BBB", "CCC", "DDD", "EEE", "FFF"])
