"""The atlas: one identity per instrument, each broker's spelling, and the desk's venue split."""
from __future__ import annotations

from datetime import datetime

import pytest

from trading_live_claude.analysis.symbol_atlas import (
    BROKERS,
    canonical_id,
    desk_route,
    known_symbols,
    listings,
    resolve,
    validate_atlas,
)
from trading_live_claude.analysis.universe import CRYPTO_SLEEVE
from trading_live_claude.brokers.kraken import KrakenBroker, to_kraken_pair
from trading_live_claude.execution.basket import ib_refusal


@pytest.mark.parametrize(("symbol", "cid"), [
    ("BTC/USD", "BTC-USD-SPOT"), ("XIC.TO", "XIC-TSX"), ("VOO", "VOO-US"), ("GC", "GC-FUT"),
    ("/GC", "GC-FUT"), ("EURUSD", "EURUSD-FX"), ("VOD.L", "VOD-LSE"),
])
def test_canonical_id(symbol: str, cid: str) -> None:
    assert canonical_id(symbol) == cid


def _wire(symbol: str) -> dict[str, str]:
    return {b.broker: b.wire_form for b in listings(symbol)}


def test_listings_use_each_brokers_spelling() -> None:
    assert _wire("BTC/USD") == {"kraken": "XBTUSD"}
    assert _wire("XIC.TO") == {"ib": "XIC", "ib_web": "XIC"}
    assert _wire("VOO") == {"ib": "VOO", "ib_web": "VOO"}
    assert _wire("GC") == {"ib": "GC", "ib_web": "GC"}
    assert _wire("VOD.L") == {"ib": "VOD", "ib_web": "VOD"}
    assert _wire("EURUSD") == {}                                # spot FX is not traded here


def test_canadian_listing_carries_its_exchange_and_currency() -> None:
    ib = next(b for b in listings("XIC.TO") if b.broker == "ib")
    assert (ib.exchange, ib.currency, ib.sec_type) == ("TSE", "CAD", "STK")


def test_every_listing_names_a_known_broker() -> None:
    for sym in known_symbols():
        assert {b.broker for b in listings(sym)} <= set(BROKERS), sym


def test_sleeve_pairs_are_what_the_adapter_or_sleeve_says() -> None:
    """Two tables spell Kraken pairs today. The atlas takes the sleeve's, and this catches a split."""
    for sym, entry in CRYPTO_SLEEVE.items():
        assert _wire(sym) == {"kraken": entry.pair}
        adapter = to_kraken_pair(sym)
        assert adapter == sym or adapter == entry.pair, (sym, adapter, entry.pair)


def test_desk_route_matches_the_basket_split() -> None:
    assert desk_route("BTC/USD") == "kraken"
    assert desk_route("VOO") == "ib" and desk_route("XIC.TO") == "ib"
    assert desk_route("EURUSD") is None
    assert desk_route("VOD.L") == "ib"
    # What the basket lets IB carry is what the atlas routes to IB, and crypto is refused there.
    assert ib_refusal("GC") is None and desk_route("GC") == "ib"
    assert ib_refusal("VOO") is None and desk_route("VOO") == "ib"
    assert ib_refusal("BTC/USD") is not None and desk_route("BTC/USD") != "ib"


def test_resolve() -> None:
    assert resolve("BTC-USD-SPOT", "kraken").wire_form == "XBTUSD"      # type: ignore[union-attr]
    assert resolve("BTC-USD-SPOT", "ib") is None
    assert resolve("NOPE-US", "ib") is None


def test_validate_atlas_covers_what_the_desk_trades_on_that_broker() -> None:
    class _Feed:
        name = venue = "kraken"

        def candles(self, _symbol: str, _s: datetime, _e: datetime, _i: str) -> list[object]:
            return [object()]

    results = validate_atlas(_Feed(), "kraken")  # type: ignore[arg-type]
    assert {r.symbol for r in results} == set(CRYPTO_SLEEVE)
    assert all(r.status == "ok" for r in results)


def test_importing_the_atlas_does_not_need_a_live_broker() -> None:
    assert KrakenBroker.venue == "kraken"
