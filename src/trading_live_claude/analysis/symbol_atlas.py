"""What one instrument is called on each broker (symbol-mapping gap 2).

Each adapter normalises symbols on its own (``to_kraken_pair``, ``_infer_stock_venue``), so nothing
says that ``XIC.TO`` on Questrade and ``XIC`` on TSE through IB are one instrument, or which brokers
can carry a name at all. The atlas is that shared view. Wire forms come from the adapters' own helpers
and the sleeve tables rather than a second copy of them, so a change in an adapter shows up here.

Listings say what a broker can technically carry. :func:`desk_route` is the separate policy question
of where the desk trades it (basket.ib_refusal: IB carries futures, Questrade carries equities).

Sleeve entries keep their ``symbol`` / ``pair`` fields; moving them onto ``canonical_id`` is the
migration NEXT_SESSION.md describes, and it can wait until a cross-venue trade is actually proposed.
"""
from __future__ import annotations

from dataclasses import dataclass

from ..brokers.base import Broker
from ..brokers.ib import _infer_stock_venue
from ..brokers.kraken import to_kraken_pair
from .asset_spec import _FUTURES_REGISTRY, CryptoSpec, FutureSpec, FXSpec, spec_for
from .symbol_validation import SymbolValidation, validate_sleeve
from .universe import CRYPTO_SLEEVE, WALK_FORWARD_VALIDATED
from .venue_calendar import venue_of

# Routing keys, as Router.brokers and BrokerSymbol.broker spell them (each adapter's ``venue``).
BROKERS = ("questrade", "kraken", "ib", "ib_web")
_DESK_ROUTE = {"crypto": "kraken", "future": "ib"}


@dataclass(frozen=True, kw_only=True)
class BrokerSymbol:
    canonical_id: str      # venue-agnostic identity: BTC-USD-SPOT, XIC-TSX, VOO-US, GC-FUT
    broker: str
    wire_form: str         # what the broker's API expects
    sec_type: str = "STK"  # STK / FUT / CRYPTO
    exchange: str = ""
    currency: str = "USD"


def _bare(symbol: str) -> str:
    return symbol.strip().upper().rsplit(".", 1)[0]


def canonical_id(symbol: str) -> str:
    """The venue-agnostic identity of a routed symbol."""
    spec = spec_for(symbol)
    if isinstance(spec, CryptoSpec):
        return f"{spec.base}-{spec.quote}-SPOT"
    if isinstance(spec, FXSpec):
        return f"{spec.base}{spec.quote}-FX"
    if isinstance(spec, FutureSpec):
        return f"{spec.root}-FUT"
    return f"{_bare(symbol)}-{venue_of(symbol).name.upper()}"


def listings(symbol: str) -> list[BrokerSymbol]:
    """Every broker that can carry ``symbol``, in that broker's own spelling.

    Empty for FX: spot FX is not tradeable in this framework (NEXT_SESSION.md, 2026-09-04).
    """
    spec = spec_for(symbol)
    cid = canonical_id(symbol)
    if isinstance(spec, CryptoSpec):
        entry = CRYPTO_SLEEVE.get(symbol.strip().upper())
        pair = entry.pair if entry is not None else to_kraken_pair(symbol)
        return [BrokerSymbol(canonical_id=cid, broker="kraken", wire_form=pair, sec_type="CRYPTO",
                             exchange="KRAKEN", currency=spec.currency)]
    if isinstance(spec, FXSpec):
        return []
    if isinstance(spec, FutureSpec):
        return [BrokerSymbol(canonical_id=cid, broker=b, wire_form=spec.root, sec_type="FUT",
                             exchange=spec.exchange_group, currency=spec.currency)
                for b in ("ib", "ib_web")]
    bare, exchange, currency = _infer_stock_venue(symbol)
    out = [BrokerSymbol(canonical_id=cid, broker=b, wire_form=bare, exchange=exchange, currency=currency)
           for b in ("ib", "ib_web")]
    if venue_of(symbol).name in ("us", "tsx"):  # Questrade carries North American listings only
        out.insert(0, BrokerSymbol(canonical_id=cid, broker="questrade", wire_form=symbol.strip().upper(),
                                   exchange=exchange, currency=currency))
    return out


def desk_route(symbol: str) -> str | None:
    """The routing key the desk trades ``symbol`` on, or None for what it does not trade (FX)."""
    if not listings(symbol):
        return None
    spec = spec_for(symbol)
    kind = ("crypto" if isinstance(spec, CryptoSpec) else
            "future" if isinstance(spec, FutureSpec) else "equity")
    return _DESK_ROUTE.get(kind, "questrade" if venue_of(symbol).name in ("us", "tsx") else "ib")


def known_symbols() -> list[str]:
    """The symbols the repo's own tables name: validated pool, crypto sleeve, futures registry."""
    return list(dict.fromkeys([*WALK_FORWARD_VALIDATED, *CRYPTO_SLEEVE, *_FUTURES_REGISTRY]))


def resolve(cid: str, broker: str) -> BrokerSymbol | None:
    """A canonical id's listing on ``broker``, among :func:`known_symbols`."""
    for sym in known_symbols():
        if canonical_id(sym) == cid:
            return next((b for b in listings(sym) if b.broker == broker), None)
    return None


def validate_atlas(broker: Broker, broker_key: str) -> list[SymbolValidation]:
    """Pre-flight every known symbol the desk trades on ``broker_key`` against that broker."""
    return validate_sleeve(broker, [s for s in known_symbols() if desk_route(s) == broker_key])


__all__ = ["BROKERS", "BrokerSymbol", "canonical_id", "desk_route", "known_symbols", "listings",
           "resolve", "validate_atlas"]
