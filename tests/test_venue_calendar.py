"""Regular hours per venue, across time zones and daylight saving, and the symbol-to-venue map."""
from __future__ import annotations

from datetime import UTC, datetime
from zoneinfo import ZoneInfo

import pytest

from trading_live_claude.analysis.venue_calendar import VENUES, is_open, symbol_is_open, venue_of

NY = ZoneInfo("America/New_York")


def ny(*a: int) -> datetime:
    return datetime(*a, tzinfo=NY)  # type: ignore[misc]


@pytest.mark.parametrize(("symbol", "venue"), [
    ("BTC/USD", "crypto"), ("SOL/USD", "crypto"), ("EURUSD", "fx"), ("GC", "globex"), ("/CL", "globex"),
    ("VOO", "us"), ("XIC.TO", "tsx"), ("ABC.V", "tsx"), ("VOD.L", "lse"), ("BHP.AX", "asx"),
    ("7203.T", "tokyo"), ("0700.HK", "hkex"), ("SAP.DE", "xetra"),
])
def test_venue_of(symbol: str, venue: str) -> None:
    assert venue_of(symbol).name == venue


def test_crypto_is_always_open() -> None:
    assert is_open("crypto", datetime(2026, 11, 1, 3, 0, tzinfo=UTC))  # a Sunday night


def test_us_hours_edges_and_weekend() -> None:
    assert not is_open("us", ny(2026, 11, 2, 9, 29))
    assert is_open("us", ny(2026, 11, 2, 9, 30))
    assert is_open("us", ny(2026, 11, 2, 15, 59))
    assert not is_open("us", ny(2026, 11, 2, 16, 0))   # the close itself is closed
    assert not is_open("us", ny(2026, 11, 1, 12, 0))   # Sunday


def test_daylight_saving_moves_the_utc_open() -> None:
    # The US open is 13:30 UTC in summer time and 14:30 UTC in winter time; the zone handles it.
    assert is_open("us", datetime(2026, 7, 1, 13, 30, tzinfo=UTC))
    assert not is_open("us", datetime(2026, 12, 1, 13, 30, tzinfo=UTC))
    assert is_open("us", datetime(2026, 12, 1, 14, 30, tzinfo=UTC))


def test_naive_timestamps_read_as_utc() -> None:
    assert is_open("us", datetime(2026, 12, 1, 14, 30))


def test_tokyo_lunch_break() -> None:
    tk = ZoneInfo("Asia/Tokyo")
    assert is_open("tokyo", datetime(2026, 11, 2, 9, 0, tzinfo=tk))
    assert not is_open("tokyo", datetime(2026, 11, 2, 12, 0, tzinfo=tk))
    assert is_open("tokyo", datetime(2026, 11, 2, 12, 30, tzinfo=tk))


def test_globex_week_and_daily_break() -> None:
    assert not is_open("globex", ny(2026, 11, 1, 17, 59))  # Sunday before the open
    assert is_open("globex", ny(2026, 11, 1, 18, 0))
    assert is_open("globex", ny(2026, 11, 2, 3, 0))        # overnight
    assert not is_open("globex", ny(2026, 11, 2, 17, 30))  # the daily maintenance hour
    assert not is_open("globex", ny(2026, 11, 6, 17, 0))   # Friday close
    assert not is_open("globex", ny(2026, 11, 7, 12, 0))   # Saturday


def test_fx_week() -> None:
    assert not is_open("fx", ny(2026, 11, 1, 16, 59))
    assert is_open("fx", ny(2026, 11, 1, 17, 0))
    assert is_open("fx", ny(2026, 11, 4, 2, 0))
    assert not is_open("fx", ny(2026, 11, 6, 17, 0))


def test_symbol_is_open_uses_the_symbols_venue() -> None:
    ts = ny(2026, 11, 1, 12, 0)                            # Sunday noon
    assert symbol_is_open("BTC/USD", ts) and not symbol_is_open("VOO", ts)


def test_unknown_venue_raises() -> None:
    with pytest.raises(KeyError):
        is_open("nowhere")
    assert "us" in VENUES


def test_every_kraken_sleeve_symbol_trades_around_the_clock() -> None:
    """A sleeve symbol misread as an equity would silently stop trading on weekends."""
    from trading_live_claude.analysis.universe import CRYPTO_SLEEVE
    for sym in CRYPTO_SLEEVE:
        assert venue_of(sym).name == "crypto", sym


def test_validated_pool_lands_on_a_real_venue() -> None:
    from trading_live_claude.analysis.universe import WALK_FORWARD_VALIDATED
    for sym in WALK_FORWARD_VALIDATED:
        expected = "tsx" if sym.endswith(".TO") else "us"
        assert venue_of(sym).name == expected, sym
