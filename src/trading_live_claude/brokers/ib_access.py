"""One Interactive Brokers surface over the three ways in: TWS/IB Gateway, Client Portal, and the connector.

Each symbol belongs to a region by its listing (``venues.venue_for``): North America (US, TSX, TSX-V),
Europe (LSE), Asia-Pacific (ASX, Tokyo, Hong Kong) or futures. :class:`IBAccessPlan` says, per region,
which transports may serve market data (tried in order) and which one carries orders.

* **Reads fall back.** A quote, a bar or a balance is tried on each data transport in turn, and the last
  error is raised only when every one failed. A read is the same read whichever transport answers it.
* **Orders never fall back.** An order goes to its region's order transport or is rejected. Silently
  retrying an order on another path could fill it twice.
* **The connector never orders.** It is read-only, so it is refused as an order transport.

Transports are built on first use, so a region nobody trades never opens its connection. Symbols are
the desk's own spelling everywhere; each transport translates them itself.
"""
from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Literal

from ..logging_setup import get_logger
from ..venues import venue_for
from .base import Broker, BrokerError, OrderRejected
from .models import Account, Candle, Order, Position, Quote

log = get_logger(__name__)

Transport = Literal["socket", "web", "connector"]
Region = Literal["north_america", "europe", "asia_pacific", "futures"]
TRANSPORTS: tuple[Transport, ...] = ("socket", "web", "connector")
REGIONS: tuple[Region, ...] = ("north_america", "europe", "asia_pacific", "futures")
READ_ONLY: frozenset[str] = frozenset({"connector"})

_REGION_OF_VENUE: Mapping[str, Region] = {
    "US": "north_america", "TSX": "north_america", "TSXV": "north_america",
    "LSE": "europe",
    "ASX": "asia_pacific", "TSEJ": "asia_pacific", "SEHK": "asia_pacific",
}


def region_of(symbol: str) -> Region:
    """The IB region a symbol trades in. Crypto is Kraken's and has none."""
    if symbol.startswith("/"):
        return "futures"
    venue, _ = venue_for(symbol)
    if venue.code == "CRYPTO":
        raise BrokerError(f"{symbol} is crypto: it trades on Kraken, not Interactive Brokers.")
    return _REGION_OF_VENUE[venue.code]


@dataclass(frozen=True)
class IBAccessPlan:
    """Which transport serves each region. ``orders`` may name a region's transport or ``None`` (no orders)."""

    data: Mapping[Region, tuple[Transport, ...]] = field(default_factory=lambda: {
        r: ("socket", "web", "connector") for r in REGIONS})
    # TWS places stock orders; its live futures orders are not implemented, so futures go through Client Portal.
    orders: Mapping[Region, Transport | None] = field(default_factory=lambda: {
        "north_america": "socket", "europe": "socket", "asia_pacific": "socket", "futures": "web"})
    account: Transport = "socket"       # accounts, positions and equity: one book, read from one place

    def __post_init__(self) -> None:
        for region in REGIONS:
            chain = self.data.get(region, ())
            if not chain:
                raise ValueError(f"IB access plan: no data transport for {region}")
            unknown = [t for t in chain if t not in TRANSPORTS]
            if unknown:
                raise ValueError(f"IB access plan: unknown transport {unknown} for {region}")
            order = self.orders.get(region)
            if order is not None and order not in TRANSPORTS:
                raise ValueError(f"IB access plan: unknown order transport {order!r} for {region}")
            if order in READ_ONLY:
                raise ValueError(f"IB access plan: {order} is read-only and cannot carry {region} orders")
        if self.account not in TRANSPORTS:
            raise ValueError(f"IB access plan: unknown account transport {self.account!r}")

    @classmethod
    def from_mapping(cls, raw: Mapping[str, Any] | None) -> IBAccessPlan:
        """``ib_access`` from trading.yaml, merged over the defaults region by region."""
        base = cls()
        raw = raw or {}
        data: dict[Any, tuple[Any, ...]] = dict(base.data)
        for region, chain in (raw.get("data") or {}).items():
            data[region] = (chain,) if isinstance(chain, str) else tuple(chain)
        orders: dict[Any, Any] = dict(base.orders)
        orders.update(raw.get("orders") or {})
        return cls(data=data, orders=orders, account=raw.get("account", base.account))

    def describe(self) -> dict[str, Any]:
        return {"data": {r: list(self.data[r]) for r in REGIONS}, "orders": {r: self.orders.get(r) for r in REGIONS},
                "account": self.account}


class IBAccess:
    """A ``Broker`` for Interactive Brokers that picks the transport per region and per job."""

    name = "interactive-brokers"
    venue = "ib"

    def __init__(self, factories: Mapping[Transport, Callable[[], Broker]], plan: IBAccessPlan | None = None,
                 *, on_built: Callable[[Transport, Broker], None] | None = None) -> None:
        self.plan = plan or IBAccessPlan()
        self._factories = dict(factories)
        self._built: dict[Transport, Broker] = {}
        self._order_paths: dict[int, Transport] = {}
        self.on_built = on_built

    # ---- transports --------------------------------------------------------------------------

    def transport(self, name: Transport) -> Broker:
        if name not in self._built:
            if name not in self._factories:
                raise BrokerError(f"IB {name} transport is not configured")
            self._built[name] = self._factories[name]()
            log.info("ib_access.transport_built", transport=name)
            if self.on_built is not None:
                self.on_built(name, self._built[name])
        return self._built[name]

    def built(self, name: Transport) -> Broker | None:
        return self._built.get(name)

    def close(self) -> None:
        for broker in self._built.values():
            closer = getattr(broker, "close", None)
            if callable(closer):
                try:
                    closer()
                except Exception:                       # pragma: no cover - best-effort cleanup
                    log.warning("ib_access.close_failed", transport=type(broker).__name__)

    def _read[T](self, region: Region, what: str, call: Callable[[Broker], T]) -> T:
        last: Exception | None = None
        for name in self.plan.data[region]:
            if name not in self._factories:
                continue
            try:
                return call(self.transport(name))
            except BrokerError as e:
                last = e
                log.warning("ib_access.read_fell_back", region=region, what=what, transport=name, error=str(e))
        raise BrokerError(f"IB {what} for {region}: every data transport failed"
                          + (f"; last error: {last}" if last else " (none configured)")) from last

    # ---- Broker ------------------------------------------------------------------------------

    def quote(self, symbol: str) -> Quote:
        return self.quotes([symbol])[0]

    def quotes(self, symbols: list[str]) -> list[Quote]:
        by_region: dict[Region, list[str]] = {}
        for s in symbols:
            by_region.setdefault(region_of(s), []).append(s)
        got: dict[str, Quote] = {}
        for region, syms in by_region.items():
            def fetch(b: Broker, s: list[str] = syms) -> list[Quote]:
                return b.quotes(s)
            got.update({q.symbol: q for q in self._read(region, "quotes", fetch)})
        return [got[s] for s in symbols if s in got]

    def candles(self, symbol: str, start: datetime, end: datetime, interval: str = "OneDay") -> list[Candle]:
        return self._read(region_of(symbol), "candles", lambda b: b.candles(symbol, start, end, interval))

    def accounts(self) -> list[Account]:
        return self.transport(self.plan.account).accounts()

    def positions(self, account_number: str) -> list[Position]:
        return self.transport(self.plan.account).positions(account_number)

    def equity(self, account_number: str, currency: str = "CAD") -> float:
        return self.transport(self.plan.account).equity(account_number, currency)

    def order_transport(self, symbol: str) -> Transport:
        region = region_of(symbol)
        name = self.plan.orders.get(region)
        if name is None:
            raise OrderRejected(f"IB access plan carries no orders for {region} ({symbol}).")
        return name

    def place_order(self, order: Order) -> Order:
        name = self.order_transport(order.symbol)
        placed = self.transport(name).place_order(order)
        if placed.id is not None:
            self._order_paths[int(placed.id)] = name
        return placed

    def cancel_order(self, account_number: str, order_id: int) -> None:
        name = self._order_paths.get(int(order_id))
        if name is None:
            raise BrokerError(f"IB order {order_id} was not placed through this session; cancel it at IB.")
        self.transport(name).cancel_order(account_number, order_id)


class LazyTickle:
    """Keeps Client Portal alive once IBAccess opens it. Nothing runs while the web transport is unused."""

    def __init__(self, access: IBAccess, interval_s: float = 90.0) -> None:
        self.access = access
        self.interval_s = interval_s
        self.warn_fn: Callable[[str, str], object] | None = None
        self._thread: Any = None
        self._started = False
        self._previous = access.on_built
        access.on_built = self._built

    def _launch(self, web: Broker) -> None:
        from .ib_keepalive import TickleThread
        self._thread = TickleThread(web, interval_s=self.interval_s)  # type: ignore[arg-type]
        if self.warn_fn is not None:
            self._thread.warn_fn = self.warn_fn
        self._thread.start()

    def _built(self, name: Transport, broker: Broker) -> None:
        if self._previous is not None:
            self._previous(name, broker)
        if name == "web" and self._started and self._thread is None:
            self._launch(broker)

    def start(self) -> None:
        self._started = True
        web = self.access.built("web")
        if web is not None and self._thread is None:
            self._launch(web)

    def stop(self) -> None:
        self._started = False
        if self._thread is not None:
            self._thread.stop()


def ib_access_from_settings(settings: Any, *, money: bool, futures: Sequence[str] = (),
                            plan: IBAccessPlan | None = None) -> tuple[IBAccess, LazyTickle]:
    """Factories for every transport the settings can open. None connects until a region asks for it."""
    roots = [r.upper().lstrip("/") for r in futures]

    def socket() -> Broker:
        from .ib import IBBroker
        port = settings.ib_paper_port if settings.ib_use_paper else settings.ib_live_port
        return IBBroker(host=settings.ib_host, port=port, client_id=settings.ib_client_id,
                        account=settings.ib_account or "", enable_live_orders=money,
                        readonly_market_data=not money)

    def web() -> Broker:
        from .ib_web import CPGatewayAuth, IBWebBroker
        broker = IBWebBroker(auth=CPGatewayAuth(host=settings.ib_web_host, port=settings.ib_web_port,
                                                verify_ssl=settings.ib_web_verify_ssl),
                             enable_live_orders=money, auto_confirm_prompts=settings.ib_auto_confirm_prompts)
        for root in roots:
            broker.set_sec_type(root, "FUT")
        return broker

    def connector() -> Broker:
        from pathlib import Path

        from .ib_connector import IBConnectorBroker
        if not settings.token_encryption_key:
            raise BrokerError("The IB connector needs TOKEN_ENCRYPTION_KEY in .env.")
        broker = IBConnectorBroker.from_state(Path(settings.state_dir), settings.token_encryption_key)
        for root in roots:
            broker.set_sec_type(root, "FUT")
        return broker

    chosen = plan or IBAccessPlan.from_mapping(getattr(settings, "ib_access", None))
    access = IBAccess({"socket": socket, "web": web, "connector": connector}, chosen)
    return access, LazyTickle(access)


__all__ = ["READ_ONLY", "REGIONS", "TRANSPORTS", "IBAccess", "IBAccessPlan", "LazyTickle", "Region", "Transport",
           "ib_access_from_settings", "region_of"]
