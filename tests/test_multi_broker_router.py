"""One Router, several brokers: dispatch, the unknown-route refusal, the card, replay and cancels."""
from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest

from trading_live_claude.audit.ledger import Ledger, read_stream
from trading_live_claude.audit.replay import replay
from trading_live_claude.brokers.base import BrokerError
from trading_live_claude.brokers.models import Candle, Order, OrderAction, Position, Quote
from trading_live_claude.brokers.paper import PaperBroker
from trading_live_claude.execution.approval import (
    ApprovalRouter,
    CardRegistry,
    InMemoryApprovalStore,
)
from trading_live_claude.execution.router import OrderIntent, Router, route_key


class _Feed:
    """A quote-only feed, so nothing here can reach a real broker."""

    def __init__(self, venue: str) -> None:
        self.name = self.venue = venue

    def accounts(self) -> list:
        return []

    def positions(self, _a: str) -> list[Position]:
        return []

    def quote(self, symbol: str) -> Quote:
        return Quote(symbol=symbol, symbolId=1, bidPrice=99.0, askPrice=101.0, lastTradePrice=100.0,
                     time=datetime.now(UTC))

    def quotes(self, symbols: list[str]) -> list[Quote]:
        return [self.quote(s) for s in symbols]

    def candles(self, *_a: object, **_k: object) -> list[Candle]:
        return []


def _paper(tmp: Path, venue: str) -> PaperBroker:
    return PaperBroker(feed=_Feed(venue), journal_dir=tmp / venue)  # type: ignore[arg-type]


def _router(tmp: Path, **kw: object) -> Router:
    return Router.build_default(mode="paper",
                                brokers={"kraken": _paper(tmp, "kraken"), "ib": _paper(tmp, "ib")},
                                default_broker="kraken", state_dir=tmp, **kw)  # type: ignore[arg-type]


def _intent(broker: str = "", symbol: str = "AAA", action: OrderAction = OrderAction.BUY) -> OrderIntent:
    return OrderIntent(symbol=symbol, action=action, shares=10, entry=100.0, stop=95.0, target=None,
                       strategy="s", risk_dollars=50.0, account_number="PAPER-001", broker=broker)


_BOOK = {"equity": 100_000.0, "existing_risk": 0.0, "open_positions": 0}


def _held(router: Router, route: str) -> list[str]:
    return [p.symbol for p in router.brokers[route].positions("PAPER-001") if p.openQuantity]


def test_intent_broker_selects_the_route(tmp_path: Path) -> None:
    r = _router(tmp_path)
    assert r.submit(_intent("ib"), **_BOOK) is not None
    assert _held(r, "ib") == ["AAA"] and _held(r, "kraken") == []


def test_empty_broker_takes_the_default_route(tmp_path: Path) -> None:
    r = _router(tmp_path)
    assert r.submit(_intent(), **_BOOK) is not None
    assert _held(r, "kraken") == ["AAA"] and _held(r, "ib") == []
    assert r.broker is r.brokers["kraken"]


def test_unknown_route_is_rejected_not_sent_to_the_default(tmp_path: Path) -> None:
    r = _router(tmp_path)
    assert r.submit(_intent("questrade"), **_BOOK) is None
    assert _held(r, "kraken") == [] and _held(r, "ib") == []
    d = r._gate(_intent("questrade"), **_BOOK)
    assert not d.accepted and "unknown broker 'questrade'" in d.rejected_reasons[0]


def test_every_gate_still_runs_on_a_non_default_route(tmp_path: Path) -> None:
    r = _router(tmp_path)
    r.kill_switch.trip(reason="test")
    assert r.submit(_intent("ib"), **_BOOK) is None
    assert _held(r, "ib") == []


def test_exactly_one_of_broker_and_brokers(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="exactly one"):
        Router.build_default(mode="paper", state_dir=tmp_path)
    with pytest.raises(ValueError, match="exactly one"):
        Router.build_default(mode="paper", broker=_paper(tmp_path, "ib"),
                             brokers={"ib": _paper(tmp_path, "ib")}, default_broker="ib",
                             state_dir=tmp_path)
    with pytest.raises(ValueError, match="not one of"):
        Router.build_default(mode="paper", brokers={"ib": _paper(tmp_path, "ib")},
                             default_broker="kraken", state_dir=tmp_path)


def test_autonomous_stays_on_one_broker(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AUTONOMOUS_ENABLED", "true")
    with pytest.raises(ValueError, match="one broker"):
        Router.build_default(mode="autonomous", brokers={"ib": _paper(tmp_path, "ib")},
                             default_broker="ib", state_dir=tmp_path)


def test_a_lone_broker_routes_under_its_venue(tmp_path: Path) -> None:
    p = _paper(tmp_path, "kraken")
    r = Router.build_default(mode="paper", broker=p, state_dir=tmp_path)
    assert route_key(p) == "kraken" and r.default_broker == "kraken" and r.broker is p
    assert r.submit(_intent("kraken"), **_BOOK) is not None


def test_basket_gate_only_covers_the_default_route(tmp_path: Path) -> None:
    class _Basket:
        venue = "kraken"

        def check_entry(self, _symbol: str) -> str | None:
            return None

    r = _router(tmp_path)
    r.basket_gate = _Basket()  # type: ignore[assignment]
    assert r._gate(_intent(), **_BOOK).accepted
    d = r._gate(_intent("ib"), **_BOOK)
    assert not d.accepted and "no card-signed basket gate on route ib" in d.rejected_reasons
    # Exits are never basket-gated, on any route.
    exit_ = _intent("ib", action=OrderAction.SELL)
    exit_.stop = 105.0
    assert r._gate(exit_, **_BOOK).accepted


def test_card_prompt_names_the_route_the_order_will_take(tmp_path: Path) -> None:
    r = _router(tmp_path)
    store = InMemoryApprovalStore(CardRegistry())
    published: list[str] = []
    real = store.publish

    def spy(intent: OrderIntent, **kw: object):
        published.append(str(kw["broker"]))
        return real(intent, **kw)  # type: ignore[arg-type]

    store.publish = spy  # type: ignore[method-assign]
    store.wait = lambda *_a, **_k: "DECLINE"  # type: ignore[method-assign]
    ar = ApprovalRouter(r, store=store, ttl_seconds=1.0)
    assert ar.card_venues == {"kraken": "kraken", "ib": "ib"}
    assert ar.submit(_intent("ib"), **_BOOK) is None
    assert ar.submit(_intent(), **_BOOK) is None
    assert published == ["ib", "kraken"]
    # An unknown route never reaches the card at all.
    assert ar.submit(_intent("questrade"), **_BOOK) is None
    assert published == ["ib", "kraken"]


def test_ledger_records_the_dispatched_broker_and_replays(tmp_path: Path) -> None:
    ledger = Ledger(tmp_path / "ledger", stream="t", mode="paper", session_id="s1")
    r = _router(tmp_path, ledger=ledger)
    assert r.submit(_intent("ib"), **_BOOK) is not None
    assert r.submit(_intent("questrade", symbol="BBB"), **_BOOK) is None
    rows = [row for p in sorted((tmp_path / "ledger").glob("*.jsonl")) for row in read_stream(p)]
    sub = next(x for x in rows if x["event"] == "BROKER_SUBMITTED")
    assert sub["payload"]["venue"] == "ib"
    results = replay(rows)
    assert [x["status"] for x in results] == ["match", "match"], results


def test_cancel_goes_to_the_named_route_and_never_raises(tmp_path: Path) -> None:
    class _Rec(_Feed):
        def __init__(self, venue: str) -> None:
            super().__init__(venue)
            self.cancelled: list[int] = []

        def place_order(self, order: Order) -> Order:
            return order

        def equity(self, *_a: object, **_k: object) -> float:
            return 1.0

        def cancel_order(self, _account: str, order_id: int) -> None:
            if order_id < 0:
                raise BrokerError("no such order")
            self.cancelled.append(order_id)

    a, b = _Rec("kraken"), _Rec("ib")
    r = Router.build_default(mode="paper", brokers={"kraken": a, "ib": b},  # type: ignore[dict-item]
                             default_broker="kraken", state_dir=tmp_path)
    assert r.cancel("A", 1, reason="t", broker="ib") is True
    assert r.cancel("A", 2, reason="t") is True
    assert b.cancelled == [1] and a.cancelled == [2]
    assert r.cancel("A", 3, reason="t", broker="questrade") is False
    assert r.cancel("A", -1, reason="t", broker="ib") is False
