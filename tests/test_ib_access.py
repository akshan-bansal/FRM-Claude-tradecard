"""IBAccess: regions by listing, reads fall back, orders never do, and the connector never orders."""
from __future__ import annotations

from datetime import UTC, datetime

import pytest

from trading_live_claude.brokers.base import BrokerError, OrderRejected
from trading_live_claude.brokers.ib_access import IBAccess, IBAccessPlan, LazyTickle, region_of
from trading_live_claude.brokers.models import Order, OrderAction, OrderType, Quote


class _Fake:
    def __init__(self, name: str, *, fail: bool = False) -> None:
        self.name = name
        self.venue = "ib"
        self.fail = fail
        self.calls: list[str] = []
        self.next_id = 100

    def quotes(self, symbols: list[str]) -> list[Quote]:
        self.calls.append(f"quotes:{','.join(symbols)}")
        if self.fail:
            raise BrokerError(f"{self.name} down")
        return [Quote(symbol=s, symbolId=0, bidPrice=1.0, askPrice=1.1, lastTradePrice=1.05) for s in symbols]

    def candles(self, symbol: str, start: datetime, end: datetime, interval: str = "OneDay") -> list:
        self.calls.append(f"candles:{symbol}")
        if self.fail:
            raise BrokerError(f"{self.name} down")
        return []

    def place_order(self, order: Order) -> Order:
        self.calls.append(f"order:{order.symbol}")
        if self.fail:
            raise BrokerError(f"{self.name} down")
        order.id = self.next_id
        self.next_id += 1
        return order

    def cancel_order(self, account_number: str, order_id: int) -> None:
        self.calls.append(f"cancel:{order_id}")

    def accounts(self) -> list:
        self.calls.append("accounts")
        return []

    def positions(self, account_number: str) -> list:
        return []

    def equity(self, account_number: str, currency: str = "CAD") -> float:
        return 1.0


def _access(plan: IBAccessPlan | None = None, **fail: bool) -> tuple[IBAccess, dict[str, _Fake]]:
    fakes = {n: _Fake(n, fail=fail.get(n, False)) for n in ("socket", "web", "connector")}
    return IBAccess({n: (lambda f=f: f) for n, f in fakes.items()}, plan), fakes


def _order(symbol: str) -> Order:
    return Order(accountId="DU1", symbol=symbol, action=OrderAction.BUY, totalQuantity=1, orderType=OrderType.MARKET)


@pytest.mark.parametrize(("symbol", "region"), [
    ("AAPL", "north_america"), ("XIC.TO", "north_america"), ("ABC.V", "north_america"), ("VOD.L", "europe"),
    ("BHP.AX", "asia_pacific"), ("7203.T", "asia_pacific"), ("0700.HK", "asia_pacific"), ("/ES", "futures"),
])
def test_every_listing_has_a_region(symbol: str, region: str) -> None:
    assert region_of(symbol) == region


def test_crypto_is_not_ibs() -> None:
    with pytest.raises(BrokerError, match="Kraken"):
        region_of("BTC/USD")


def test_a_read_falls_back_and_says_so_only_when_all_fail() -> None:
    access, fakes = _access(socket=True)
    assert access.quote("AAPL").symbol == "AAPL"
    assert fakes["socket"].calls and fakes["web"].calls and not fakes["connector"].calls
    all_down, _ = _access(socket=True, web=True, connector=True)
    with pytest.raises(BrokerError, match="every data transport failed"):
        all_down.candles("BHP.AX", datetime(2026, 1, 1, tzinfo=UTC), datetime(2026, 2, 1, tzinfo=UTC))


def test_quotes_are_split_by_region_and_keep_their_order() -> None:
    plan = IBAccessPlan.from_mapping({"data": {"asia_pacific": ["connector"]}})
    access, fakes = _access(plan)
    got = access.quotes(["BHP.AX", "AAPL", "7203.T"])
    assert [q.symbol for q in got] == ["BHP.AX", "AAPL", "7203.T"]
    assert fakes["connector"].calls == ["quotes:BHP.AX,7203.T"] and fakes["socket"].calls == ["quotes:AAPL"]


def test_an_order_never_falls_back() -> None:
    access, fakes = _access(socket=True)
    with pytest.raises(BrokerError, match="socket down"):
        access.place_order(_order("AAPL"))
    assert not fakes["web"].calls and not fakes["connector"].calls


def test_futures_orders_go_through_client_portal_and_cancel_follows_the_order() -> None:
    access, fakes = _access()
    placed = access.place_order(_order("/ES"))
    assert fakes["web"].calls == ["order:/ES"] and not fakes["socket"].calls
    access.cancel_order("DU1", placed.id)
    assert fakes["web"].calls[-1] == f"cancel:{placed.id}"
    with pytest.raises(BrokerError, match="not placed through this session"):
        access.cancel_order("DU1", 999)


def test_the_connector_can_never_carry_orders() -> None:
    with pytest.raises(ValueError, match="read-only"):
        IBAccessPlan.from_mapping({"orders": {"asia_pacific": "connector"}})


def test_a_region_without_orders_rejects_them() -> None:
    access, _ = _access(IBAccessPlan.from_mapping({"orders": {"europe": None}}))
    with pytest.raises(OrderRejected, match="no orders for europe"):
        access.place_order(_order("VOD.L"))


def test_a_bad_plan_is_refused() -> None:
    with pytest.raises(ValueError, match="unknown transport"):
        IBAccessPlan.from_mapping({"data": {"europe": ["fax"]}})
    with pytest.raises(ValueError, match="no data transport"):
        IBAccessPlan.from_mapping({"data": {"europe": []}})


def test_transports_open_only_when_a_region_asks() -> None:
    access, _ = _access()
    assert access.built("socket") is None
    access.accounts()
    assert access.built("socket") is not None and access.built("web") is None


def test_the_keepalive_waits_for_client_portal() -> None:
    access, _ = _access()
    tickle = LazyTickle(access)
    started: list[object] = []
    tickle._launch = started.append                    # type: ignore[method-assign]
    tickle.start()
    assert started == []
    access.place_order(_order("/ES"))                  # opens the web transport
    assert started == [access.built("web")]
    tickle.stop()
