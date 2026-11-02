"""KrakenBroker — implements the ``Broker`` protocol against Kraken's public + private REST APIs.

Purpose: give the ``Router`` an execution surface for the crypto sleeve, so orders that clear the
router's risk gates can actually fill (via ``PaperBroker`` first, and eventually — behind the
go-live gate — via Kraken's live private API).

Design constraints, in tension with each other:

* **Interface parity.** Every existing consumer (the router, the paper broker, the live monitor)
  addresses a broker through the same ``Broker`` protocol — same method names, same models. This
  file translates Kraken's shape into that surface without leaking any Kraken-specific type outward.
* **Fractional sizing.** Crypto is fractional; ``Order.totalQuantity`` is already ``float``, so the
  model can carry a fractional quantity end-to-end. The router will still round on the way in for
  equities, and pass through for crypto — the broker itself never quantizes.
* **Live placement is gated.** ``place_order`` REFUSES to send unless ``enable_live_orders=True``
  is passed at construction. This is a per-instance switch that no code path in the repo flips on
  its own; the go-live decision is a human one. The default is dry: orders raise a clear error
  rather than silently signing anything.
* **Testable without the network.** The HTTP surface is a single ``httpx.Client`` slot that a test
  can inject with a ``MockTransport``. The ``private_post`` helper is the existing signing core
  from ``kraken_auth.py`` — reused, not duplicated.

Pair conventions. The routed symbol in ``CRYPTO_SLEEVE`` is ``BTC/USD``; Kraken's Ticker/OHLC use
``XBTUSD`` on the wire and ``XXBTZUSD`` in the response key. The small ``_KRAKEN_PAIRS`` table
covers the sleeve today; anything outside it round-trips as the raw code the caller passes.
"""
from __future__ import annotations

import time
from datetime import UTC, datetime
from hashlib import sha256
from typing import Any, cast

import httpx

from ..logging_setup import get_logger
from .base import Broker, BrokerError, OrderRejected
from .kraken_auth import KRAKEN_REST, KrakenAuthError, private_post
from .models import (
    Account,
    Candle,
    Execution,
    Order,
    OrderAction,
    OrderType,
    Position,
    Quote,
)

log = get_logger(__name__)

# Routed symbol (as used everywhere else in this project) → Kraken wire pair.
# Response keys can differ ("XXBTZUSD" instead of "XBTUSD") — that mapping is done at read time
# from the payload keys themselves, so we do not have to enumerate it here.
_KRAKEN_PAIRS: dict[str, str] = {
    "BTC/USD": "XBTUSD",
    "ETH/USD": "ETHUSD",
    "XMR/USD": "XMRUSD",
    "XRP/USD": "XRPUSD",
    "XLM/USD": "XLMUSD",
    "LINK/USD": "LINKUSD",
    "PAXG/USD": "PAXGUSD",
}


def to_kraken_pair(symbol: str) -> str:
    """Routed symbol (``BTC/USD``) → Kraken wire pair (``XBTUSD``). Pass through for unknowns."""
    return _KRAKEN_PAIRS.get(symbol, symbol)


class KrakenBroker(Broker):
    """Broker adapter for Kraken. Public data works without creds; private data requires them.

    ``place_order`` requires ``enable_live_orders=True`` — the switch is explicit and NEVER flipped
    from application code. This is the go-live gate.
    """

    name = "kraken"
    venue = "kraken"

    def __init__(
        self,
        *,
        api_key: str = "",
        api_secret: str = "",
        client: httpx.Client | None = None,
        enable_live_orders: bool = False,
        timeout: float = 30.0,
    ) -> None:
        self._api_key = api_key
        self._api_secret = api_secret
        self._client = client or httpx.Client(
            timeout=timeout, headers={"User-Agent": "FRM-Claude/1.0"}
        )
        self._owns_client = client is None
        self._enable_live_orders = enable_live_orders
        self._txids: dict[int, str] = {}       # integer order handle -> Kraken order txid
        self._marks: dict[str, float] = {}     # last price seen per symbol
        self._entries: dict[str, tuple[float, float]] = {}  # symbol -> (quantity, average entry)

    def close(self) -> None:
        if self._owns_client:
            self._client.close()

    def __enter__(self) -> "KrakenBroker":
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    # ---- read-only surface ---------------------------------------------------

    def accounts(self) -> list[Account]:
        """Kraken has one account per user; synthesized so the router sees a stable id."""
        return [Account(type="Cash", number="KRAKEN", status="Active", isPrimary=True)]

    def positions(self, account_number: str) -> list[Position]:
        """Non-zero spot balances as ``Position`` entries, marked and with their average entry.

        Kraken's ``Balance`` endpoint returns every asset held on the account keyed by its Kraken
        asset code (``XXBT``, ``XETH``, ``ZUSD``). USD/fiat balances are cash and are surfaced
        through :meth:`equity` rather than as positions. Without credentials this returns [].
        """
        if not (self._api_key and self._api_secret):
            return []
        holdings = _holdings(self._balances())
        marks = self._marks_for(list(holdings))
        out: list[Position] = []
        for symbol, amount in holdings.items():
            price = marks[symbol]
            entry = self._average_entry(symbol, amount, account_number)
            out.append(Position(
                symbol=symbol, symbolId=0, openQuantity=amount, currentPrice=price,
                currentMarketValue=amount * price, averageEntryPrice=entry, totalCost=amount * entry,
                openPnl=(price - entry) * amount if entry else 0.0,
            ))
        return out

    def _balances(self) -> dict[str, Any]:
        try:
            return cast(dict[str, Any], private_post(
                "/0/private/Balance", key=self._api_key, secret=self._api_secret, client=self._client))
        except KrakenAuthError as e:
            raise BrokerError(f"Kraken Balance failed: {e}") from e

    def _marks_for(self, symbols: list[str]) -> dict[str, float]:
        """Mid price per symbol. Equity feeds the kill-switch, so a quote gap must not look like a loss:
        a symbol that cannot be priced now keeps its last mark. One never priced counts as zero, and
        then it is in neither the peak nor the mark."""
        for sym in symbols:
            try:
                mid = self.quote(sym).mid
            except BrokerError as e:
                log.warning("kraken.mark_failed", symbol=sym, error=str(e))
                continue
            if mid:
                self._marks[sym] = mid
            else:
                log.warning("kraken.mark_missing", symbol=sym)
        return {s: self._marks.get(s, 0.0) for s in symbols}

    def _average_entry(self, symbol: str, quantity: float, account_number: str) -> float:
        """Average cost of what is held, from trade history; 0.0 if it cannot be read. Re-read only when
        the holding changes, since ``TradesHistory`` counts against the private rate limit."""
        cached = self._entries.get(symbol)
        if cached is not None and cached[0] == quantity:
            return cached[1]
        try:
            rows = self.executions(account_number)
        except BrokerError as e:
            log.warning("kraken.entry_unknown", symbol=symbol, error=str(e))
            return 0.0
        qty = avg = 0.0
        for e in sorted((r for r in rows if r.symbol == symbol), key=lambda r: r.time):
            if e.side == "Buy":
                avg = (avg * qty + e.price * e.quantity) / (qty + e.quantity)
                qty += e.quantity
            else:
                qty = max(qty - e.quantity, 0.0)
        self._entries[symbol] = (quantity, avg)
        return avg

    def quote(self, symbol: str) -> Quote:
        return self.quotes([symbol])[0]

    def quotes(self, symbols: list[str]) -> list[Quote]:
        """Public Ticker for one or more pairs. One HTTP round-trip regardless of length."""
        if not symbols:
            return []
        wire = ",".join(to_kraken_pair(s) for s in symbols)
        payload = self._get_public("/0/public/Ticker", {"pair": wire})
        result = payload.get("result", {})
        # Kraken responds keyed by its canonical pair code (e.g. XXBTZUSD), which is not the wire
        # code we sent. Build a case-insensitive substring lookup: for each requested symbol, find
        # the response key that contains the wire pair or its trailing form.
        out: list[Quote] = []
        for sym in symbols:
            data = _find_ticker(result, to_kraken_pair(sym))
            if data is None:
                # No matching key; return an empty quote rather than raising, so the paper broker's
                # "no reference price" reject path is what handles the missing datum.
                out.append(Quote(symbol=sym, symbolId=0))
                continue
            bid = _first_price(data.get("b"))
            ask = _first_price(data.get("a"))
            last = _first_price(data.get("c"))
            out.append(Quote(
                symbol=sym, symbolId=0,
                bidPrice=bid, askPrice=ask, lastTradePrice=last,
            ))
        return out

    def candles(self, symbol: str, start: datetime, end: datetime,
                interval: str = "OneDay") -> list[Candle]:
        """Public OHLC bars for ``symbol`` between ``start`` and ``end`` (inclusive).

        ``interval`` accepts the project's canonical strings (``OneDay``, ``OneHour``, ``OneMinute``)
        and translates to Kraken minutes. Kraken caps at ~720 bars per request — the caller must
        page (see :mod:`...data.kraken_ohlc` for the daily flow); this method returns whatever the
        first request gives back, filtered to the window.
        """
        payload = self._get_public("/0/public/OHLC",
                                   {"pair": to_kraken_pair(symbol),
                                    "interval": _interval_minutes(interval)})
        result = payload.get("result", {})
        series = next((v for k, v in result.items() if k != "last"), None)
        if not series:
            return []
        out: list[Candle] = []
        start_ts = int(start.timestamp())
        end_ts = int(end.timestamp())
        for row in series:
            ts = int(row[0])
            if ts < start_ts or ts > end_ts:
                continue
            when = datetime.fromtimestamp(ts, tz=UTC)
            out.append(Candle(
                start=when, end=when,
                open=float(row[1]), high=float(row[2]),
                low=float(row[3]), close=float(row[4]),
                volume=int(float(row[6])),
            ))
        return out

    def equity(self, account_number: str, currency: str = "CAD") -> float:
        """Cash in ``currency`` plus every holding at its mark, USD by convention on Kraken.

        Holdings count: buying moves money from cash into a position, and an equity of cash alone would
        read that as a loss and trip the kill-switch on the first fill. A failed balance read raises
        rather than returning zero, for the same reason.
        """
        if not (self._api_key and self._api_secret):
            return 0.0
        balances = self._balances()
        target = f"Z{currency.upper()}"           # Kraken fiat prefix (ZUSD, ZCAD, ZEUR, ...)
        cash = float(balances.get(target, balances.get(currency.upper(), 0.0)))
        holdings = _holdings(balances)
        marks = self._marks_for(list(holdings))
        return cash + sum(amount * marks[sym] for sym, amount in holdings.items())

    # ---- order surface -------------------------------------------------------

    def place_order(self, order: Order) -> Order:
        """Submit an order to Kraken. REFUSES unless ``enable_live_orders=True`` at construction.

        The refusal is loud (an ``OrderRejected`` with an explicit reason) rather than a silent
        no-op, so the router's fill journal shows exactly why nothing hit the market. When enabled,
        maps the project's ``Order`` fields onto Kraken's ``AddOrder`` schema — ``symbol`` translated
        to the wire pair, ``totalQuantity`` passed as ``volume`` (fractional respected).
        """
        if not self._enable_live_orders:
            raise OrderRejected(
                "KrakenBroker: live orders disabled. Wrap this broker in PaperBroker(feed=...) "
                "for simulated fills, or construct with enable_live_orders=True after the human "
                "go-live confirmation."
            )
        if not (self._api_key and self._api_secret):
            raise OrderRejected("KrakenBroker: no API credentials configured.")

        body: dict[str, Any] = {
            "pair": to_kraken_pair(order.symbol),
            "type": "buy" if order.action == OrderAction.BUY else "sell",
            "ordertype": _order_type_wire(order.orderType),
            "volume": str(order.totalQuantity),
        }
        if order.orderType in (OrderType.LIMIT, OrderType.STOP_LIMIT) and order.limitPrice is not None:
            body["price"] = str(order.limitPrice)
        if order.orderType in (OrderType.STOP, OrderType.STOP_LIMIT) and order.stopPrice is not None:
            body["price2"] = str(order.stopPrice)

        try:
            result = private_post(
                "/0/private/AddOrder", key=self._api_key, secret=self._api_secret,
                data=body, client=self._client,
            )
        except KrakenAuthError as e:
            raise OrderRejected(f"Kraken AddOrder rejected: {e}") from e

        txids = result.get("txid") or []
        if txids:
            # Kraken order ids are strings; ``id`` is a stable integer fold for the journals and
            # ``broker_ref`` keeps the real txid, which cancels and execution lookups need.
            order.id = _txid_to_int(txids[0])
            order.broker_ref = str(txids[0])
            self._txids[order.id] = str(txids[0])
        log.info(
            "kraken.order.submitted",
            symbol=order.symbol,
            action=order.action.value,
            qty=order.totalQuantity,
            txid=txids[0] if txids else None,
        )
        return order

    def cancel_order(self, account_number: str, order_id: int) -> None:
        if not self._enable_live_orders:
            raise BrokerError("KrakenBroker: live orders disabled; nothing to cancel.")
        if not (self._api_key and self._api_secret):
            raise BrokerError("KrakenBroker: no API credentials configured.")
        txid = self._txids.get(int(order_id))
        if txid is None:
            # The integer handle is a one-way fold of the txid. Without the txid this process
            # recorded at placement there is nothing Kraken would recognise; sending the integer
            # (the pre-2026-10-31 behaviour) could never cancel anything.
            raise BrokerError(f"KrakenBroker: no Kraken txid on record for order {order_id}; "
                              "cancel it by txid on Kraken.")
        try:
            private_post(
                "/0/private/CancelOrder",
                key=self._api_key, secret=self._api_secret,
                data={"txid": txid}, client=self._client,
            )
        except KrakenAuthError as e:
            raise BrokerError(f"Kraken CancelOrder failed: {e}") from e

    def executions(self, account_number: str, *, since: datetime | None = None) -> list[Execution]:
        """Trades from the private ``TradesHistory`` endpoint (needs a Query Funds/Trades key).

        ``order_id`` is the integer fold of the trade's order txid, matching what ``place_order``
        returned, and the fee is in the pair's quote currency, as Kraken charges it by default.
        """
        if not (self._api_key and self._api_secret):
            raise BrokerError("KrakenBroker: no API credentials configured.")
        data: dict[str, object] = {}
        if since is not None:
            data["start"] = str(int(since.timestamp()))
        try:
            result = private_post("/0/private/TradesHistory", key=self._api_key,
                                  secret=self._api_secret, data=data, client=self._client)
        except KrakenAuthError as e:
            raise BrokerError(f"Kraken TradesHistory failed: {e}") from e
        trades = cast(dict[str, dict[str, Any]], result.get("trades") or {})
        out: list[Execution] = []
        for trade_id, t in trades.items():
            symbol = _pair_to_symbol(str(t.get("pair", "")))
            quote = symbol.split("/")[-1] if "/" in symbol else None
            out.append(Execution(
                exec_id=f"kraken:{trade_id}", order_id=str(_txid_to_int(str(t.get("ordertxid")))),
                symbol=symbol, side="Buy" if t.get("type") == "buy" else "Sell",
                quantity=float(t["vol"]), price=float(t["price"]), fee=float(t.get("fee") or 0.0),
                fee_currency=quote, currency=quote,
                currency_source="kraken pair quote" if quote else "unknown",
                time=datetime.fromtimestamp(float(t["time"]), tz=UTC), venue="kraken",
                account=account_number))
        return out

    # ---- helpers -------------------------------------------------------------

    def _get_public(self, path: str, params: dict[str, Any]) -> dict[str, Any]:
        # Small retry so a transient 5xx doesn't take down a monitor loop.
        last_err: Exception | None = None
        for attempt in range(2):
            try:
                r = self._client.get(KRAKEN_REST + path, params=params)
                data = r.json()
                if data.get("error"):
                    raise BrokerError(f"Kraken {path} error: {data['error']}")
                return data
            except (httpx.HTTPError, BrokerError) as e:
                last_err = e
                if attempt == 0:
                    time.sleep(0.5)
                    continue
                raise
        assert last_err is not None       # unreachable, satisfies mypy
        raise last_err


# ---- module-level helpers ----------------------------------------------------


_FIAT_ASSETS = frozenset(("ZUSD", "ZCAD", "ZEUR", "ZGBP", "ZJPY", "USDC", "USDT", "DAI"))


def _is_fiat_asset(asset: str) -> bool:
    return asset.upper() in _FIAT_ASSETS


# Kraken canonical asset code → the routed symbol most naturally paired against USD.
_KRAKEN_ASSET_TO_ROUTED: dict[str, str] = {
    "XXBT": "BTC/USD", "XBT": "BTC/USD",
    "XETH": "ETH/USD", "ETH": "ETH/USD",
    "XXMR": "XMR/USD", "XMR": "XMR/USD",
    "XXRP": "XRP/USD", "XRP": "XRP/USD",
    "XXLM": "XLM/USD", "XLM": "XLM/USD",
    "LINK": "LINK/USD",
    "PAXG": "PAXG/USD",
}


def _asset_to_routed_symbol(asset: str) -> str:
    """Kraken asset code (``XXBT``) → routed symbol (``BTC/USD``), paired against USD. An asset not in the
    table is its own code (``SOL``, ``AAVE``), so it routes as ``SOL/USD``, the form the sleeve trades."""
    return _KRAKEN_ASSET_TO_ROUTED.get(asset, f"{asset}/USD")


def _holdings(balances: dict[str, Any]) -> dict[str, float]:
    """Tradable non-fiat balances by routed symbol. A dotted code (``SOL.S``, ``ETH.F``) is staked or earning
    and cannot be sold from the spot book, so it is not a position."""
    out: dict[str, float] = {}
    for asset, amount in balances.items():
        qty = float(amount)
        if qty and not _is_fiat_asset(asset) and "." not in asset:
            out[_asset_to_routed_symbol(asset)] = qty
    return out


def _find_ticker(result: dict[str, Any], wire_pair: str) -> dict[str, Any] | None:
    """Kraken responds under its canonical key (e.g. ``XXBTZUSD``), not the wire code we sent.

    Match by suffix so ``XBTUSD`` (wire) finds ``XXBTZUSD`` (response) via the shared trailing form.
    """
    if wire_pair in result:
        return result[wire_pair]
    # Compare the trailing letters — the response key strips wrapper letters but keeps the pair.
    for key, val in result.items():
        if key.endswith(wire_pair) or wire_pair.endswith(key.replace("X", "").replace("Z", "")):
            return val
    return None


def _first_price(field: list[str] | None) -> float | None:
    if not field:
        return None
    try:
        return float(field[0])
    except (TypeError, ValueError):
        return None


_INTERVAL_MINUTES: dict[str, int] = {
    "OneMinute": 1, "FiveMinutes": 5, "FifteenMinutes": 15,
    "ThirtyMinutes": 30, "OneHour": 60, "FourHours": 240,
    "OneDay": 1440,
}


def _interval_minutes(interval: str) -> int:
    return _INTERVAL_MINUTES.get(interval, 1440)


def _order_type_wire(t: OrderType) -> str:
    return {
        OrderType.MARKET: "market",
        OrderType.LIMIT: "limit",
        OrderType.STOP: "stop-loss",
        OrderType.STOP_LIMIT: "stop-loss-limit",
    }.get(t, "market")


def _txid_to_int(txid: str) -> int:
    """Kraken txids are strings (``OZAA6H-FQI3S-DK6GHJ``); fold to an int for the journals.

    A SHA-256 fold, so the same txid gives the same integer in every process. Before 2026-10-31
    this used ``hash()``, which Python randomises per process: an id written by one session could
    never be matched by another, or against Kraken's own trade history. Not reversible; the txid
    itself is kept as ``Order.broker_ref``.
    """
    return int.from_bytes(sha256(txid.encode("utf-8")).digest()[:4], "big") % (2**31 - 1)


def _pair_to_symbol(pair: str) -> str:
    """Kraken's wire pair (``SOLUSD``, ``XXBTZUSD``) back to the routed ``BASE/QUOTE`` form."""
    p = pair.upper()
    for quote in ("ZUSD", "ZCAD", "ZEUR", "USDT", "USDC", "USD", "CAD", "EUR"):
        if p.endswith(quote) and len(p) > len(quote):
            base, q = p[: -len(quote)], quote
            break
    else:
        return pair
    base = {"XXBT": "BTC", "XBT": "BTC", "XETH": "ETH", "XXDG": "DOGE"}.get(base, base)
    if len(base) == 4 and base[0] == "X":
        base = base[1:]
    return f"{base}/{q[1:] if len(q) == 4 and q[0] == 'Z' else q}"
