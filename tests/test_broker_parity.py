"""What the brokers report and charge, so a paper result means what a money result would.

Every network call is mocked. Paper: fills cross the spread and pay the venue's costs. Kraken: equity and
positions describe the whole book. Questrade: a token's environment is checked, not assumed.
"""
from __future__ import annotations

import time
from pathlib import Path
from typing import Any

import httpx
import pytest
import respx

from trading_live_claude.brokers import kraken as kraken_module
from trading_live_claude.brokers.base import BrokerError
from trading_live_claude.brokers.kraken import KrakenBroker, _asset_to_routed_symbol, _holdings
from trading_live_claude.brokers.models import Order, OrderAction, OrderType, Quote
from trading_live_claude.brokers.paper import PaperBroker
from trading_live_claude.execution.venue_costs import VenueCostModel
from trading_live_claude.brokers.questrade import QuestradeBroker, environment_of
from trading_live_claude.brokers.token_store import TokenSet

# ---------------------------------------------------------------- paper fills


class _Quotes:
    name = venue = "kraken"

    def __init__(self, bid: float | None = 99.0, ask: float | None = 101.0, last: float = 100.0) -> None:
        self.bid, self.ask, self.last = bid, ask, last

    def quote(self, symbol: str) -> Quote:
        return Quote(symbol=symbol, symbolId=1, bidPrice=self.bid, askPrice=self.ask, lastTradePrice=self.last)


def _order(action: OrderAction = OrderAction.BUY, qty: float = 10.0) -> Order:
    return Order(symbol="AAA", accountId="PAPER-001", action=action, orderType=OrderType.MARKET, totalQuantity=qty)


def _paper(tmp: Path, feed: Any = None, **kw: Any) -> PaperBroker:
    return PaperBroker(feed=feed or _Quotes(), journal_dir=tmp, **kw)


def test_a_rehearsal_fill_pays_the_far_side_of_the_book(tmp_path: Path) -> None:
    b = _paper(tmp_path, slippage_bps=0.0, commission_per_trade=0.0, fill_model="touch")
    b.place_order(_order())
    assert b.fills[-1].price == 101.0
    b.place_order(_order(OrderAction.SELL))
    assert b.fills[-1].price == 99.0


def test_slippage_comes_on_top_of_the_spread(tmp_path: Path) -> None:
    b = _paper(tmp_path, slippage_bps=10.0, commission_per_trade=0.0, fill_model="touch")
    b.place_order(_order())
    assert b.fills[-1].price == pytest.approx(101.0 * 1.001)


def test_a_round_trip_loses_the_spread_and_the_fees(tmp_path: Path) -> None:
    b = _paper(tmp_path, slippage_bps=0.0, fill_model="touch",
               cost_model=VenueCostModel(flat_per_fill=0.0, commission_bps=40.0))
    start = b.equity("PAPER-001")
    b.place_order(_order())
    b.place_order(_order(OrderAction.SELL))
    spread = 10 * (101.0 - 99.0)
    fees = 10 * 101.0 * 0.004 + 10 * 99.0 * 0.004
    assert b.equity("PAPER-001") == pytest.approx(start - spread - fees)


def test_a_one_sided_quote_falls_back_to_the_mid_price(tmp_path: Path) -> None:
    b = _paper(tmp_path, _Quotes(bid=None, ask=None), slippage_bps=0.0, commission_per_trade=0.0, fill_model="touch")
    b.place_order(_order())
    assert b.fills[-1].price == 100.0


def test_a_bare_paper_broker_keeps_its_old_fill(tmp_path: Path) -> None:
    b = _paper(tmp_path, slippage_bps=0.0, commission_per_trade=4.95)
    b.place_order(_order())
    assert b.fills[-1].price == 100.0 and b.fills[-1].commission == 4.95


def test_commission_bps_scales_with_the_order(tmp_path: Path) -> None:
    b = _paper(tmp_path, slippage_bps=0.0, cost_model=VenueCostModel(flat_per_fill=1.0, commission_bps=10.0))
    b.place_order(_order())
    assert b.fills[-1].commission == pytest.approx(1.0 + 10 * 100.0 * 0.001)


def test_a_paper_halt_keeps_a_manual_reason_and_restarts_after_a_clear(tmp_path: Path) -> None:
    from trading_live_claude.risk.kill_switch import KillSwitch
    feed = _Quotes(bid=100.0, ask=100.0)
    b = _paper(tmp_path, feed, slippage_bps=0.0, commission_per_trade=0.0, starting_equity=1_000.0)
    b.place_order(_order(qty=5.0))
    ks = KillSwitch(tmp_path)
    ks.trip("operator said stop")
    feed.bid = feed.ask = 50.0
    b.mark_to_market()
    assert ks.state().reason.endswith("operator said stop")   # not overwritten by the drawdown it sees
    ks.clear("I HAVE INVESTIGATED")
    b.mark_to_market()
    assert not ks.state().halted                              # the measure restarted at the clear
    feed.bid = feed.ask = 30.0
    b.mark_to_market()
    assert not ks.state().halted                              # a paper drawdown does not stop money
    assert KillSwitch(tmp_path, paper=True).state().halted    # it stops paper


# ---------------------------------------------------------------- Kraken's book

API = "https://api.kraken.com"


def _kraken() -> KrakenBroker:
    return KrakenBroker(api_key="k", api_secret="c2VjcmV0", enable_live_orders=False)


@pytest.fixture
def private(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    answers: dict[str, Any] = {"/0/private/Balance": {"ZUSD": "1000.0", "XXBT": "0.5", "SOL": "10", "SOL.S": "99",
                                                       "USDT": "5"},
                               "/0/private/TradesHistory": {"trades": {}}}

    def fake(path: str, **_k: Any) -> Any:
        answer = answers[path]
        if isinstance(answer, Exception):
            raise answer
        return answer

    monkeypatch.setattr(kraken_module, "private_post", fake)
    return answers


def _ticker(prices: dict[str, float]) -> None:
    def respond(request: httpx.Request) -> httpx.Response:
        pair = request.url.params["pair"]
        if pair not in prices:
            return httpx.Response(200, json={"error": ["EQuery:Unknown asset pair"], "result": {}})
        p = str(prices[pair])
        return httpx.Response(200, json={"error": [], "result": {pair: {"a": [p], "b": [p], "c": [p]}}})
    respx.get(f"{API}/0/public/Ticker").mock(side_effect=respond)


def test_unknown_assets_route_as_the_sleeve_trades_them() -> None:
    assert _asset_to_routed_symbol("XXBT") == "BTC/USD"
    assert _asset_to_routed_symbol("SOL") == "SOL/USD"


def test_staked_and_stable_balances_are_not_positions() -> None:
    assert _holdings({"ZUSD": "1", "USDT": "5", "SOL.S": "9", "ETH.F": "2", "SOL": "3", "XXBT": "0"}) == {"SOL/USD": 3.0}


@respx.mock
def test_equity_is_cash_plus_marked_holdings(private: dict[str, Any]) -> None:
    _ticker({"XBTUSD": 60_000.0, "SOLUSD": 100.0})
    with _kraken() as b:
        assert b.equity("KRAKEN", "USD") == pytest.approx(1000.0 + 0.5 * 60_000.0 + 10 * 100.0)


@respx.mock
def test_buying_does_not_read_as_a_loss(private: dict[str, Any]) -> None:
    """The bug that would have tripped the kill-switch on the first real fill."""
    _ticker({"SOLUSD": 100.0})
    private["/0/private/Balance"] = {"ZUSD": "2000.0"}
    with _kraken() as b:
        before = b.equity("KRAKEN", "USD")
        private["/0/private/Balance"] = {"ZUSD": "1000.0", "SOL": "10"}
        assert b.equity("KRAKEN", "USD") == pytest.approx(before)


@respx.mock
def test_a_quote_gap_keeps_the_last_mark(private: dict[str, Any]) -> None:
    private["/0/private/Balance"] = {"ZUSD": "0", "SOL": "10"}
    route = respx.get(f"{API}/0/public/Ticker")
    route.mock(return_value=httpx.Response(200, json={"error": [], "result": {"SOLUSD": {
        "a": ["100"], "b": ["100"], "c": ["100"]}}}))
    with _kraken() as b:
        assert b.equity("KRAKEN", "USD") == pytest.approx(1000.0)
        route.mock(return_value=httpx.Response(200, json={"error": ["EService:Unavailable"], "result": {}}))
        assert b.equity("KRAKEN", "USD") == pytest.approx(1000.0)


@respx.mock
def test_an_unpriceable_dust_asset_does_not_stop_the_book(private: dict[str, Any]) -> None:
    private["/0/private/Balance"] = {"ZUSD": "500", "SOL": "1", "DUST": "7"}
    _ticker({"SOLUSD": 100.0})
    with _kraken() as b:
        assert b.equity("KRAKEN", "USD") == pytest.approx(600.0)


def test_a_failed_balance_read_raises_rather_than_reporting_zero(private: dict[str, Any]) -> None:
    private["/0/private/Balance"] = kraken_module.KrakenAuthError("nope")
    with _kraken() as b, pytest.raises(BrokerError, match="Balance failed"):
        b.equity("KRAKEN", "USD")


def _trade(side: str, vol: float, price: float, at: float, pair: str = "SOLUSD") -> dict[str, Any]:
    return {"pair": pair, "type": side, "vol": str(vol), "price": str(price), "fee": "0", "time": at, "ordertxid": "O1"}


@respx.mock
def test_positions_carry_their_mark_and_average_entry(private: dict[str, Any], monkeypatch: pytest.MonkeyPatch) -> None:
    private["/0/private/Balance"] = {"SOL": "10"}
    private["/0/private/TradesHistory"] = {"trades": {
        "T2": _trade("buy", 5, 120.0, 2.0), "T1": _trade("buy", 10, 90.0, 1.0), "T3": _trade("sell", 5, 130.0, 3.0)}}
    monkeypatch.setattr(kraken_module, "_pair_to_symbol", lambda _p: "SOL/USD")
    _ticker({"SOLUSD": 110.0})
    with _kraken() as b:
        p = b.positions("KRAKEN")[0]
    assert p.symbol == "SOL/USD" and p.currentPrice == 110.0 and p.currentMarketValue == 1100.0
    assert p.averageEntryPrice == pytest.approx(100.0)        # (10x90 + 5x120)/15; a sell leaves the average
    assert p.openPnl == pytest.approx(100.0)


@respx.mock
def test_trade_history_is_read_only_when_the_holding_changes(private: dict[str, Any]) -> None:
    calls: list[str] = []
    private["/0/private/Balance"] = {"SOL": "10"}
    _ticker({"SOLUSD": 100.0})
    with _kraken() as b:
        real = b.executions
        b.executions = lambda *a, **k: calls.append("read") or real(*a, **k)  # type: ignore[method-assign,func-returns-value]
        b.positions("KRAKEN")
        b.positions("KRAKEN")
        assert calls == ["read"]
        private["/0/private/Balance"] = {"SOL": "12"}
        b.positions("KRAKEN")
        assert calls == ["read", "read"]


@respx.mock
def test_an_unreadable_trade_history_leaves_the_entry_unknown(private: dict[str, Any]) -> None:
    private["/0/private/Balance"] = {"SOL": "10"}
    private["/0/private/TradesHistory"] = kraken_module.KrakenAuthError("no permission")
    _ticker({"SOLUSD": 100.0})
    with _kraken() as b:
        assert b.positions("KRAKEN")[0].averageEntryPrice == 0.0


# ---------------------------------------------------------------- Questrade's environment

def _questrade(tmp: Path, api_server: str) -> QuestradeBroker:
    b = QuestradeBroker.from_settings(refresh_token="r", encryption_key=_key(), state_dir=tmp)
    b._tokens = TokenSet(access_token="a", refresh_token="r", api_server=api_server,
                         expires_at_epoch=time.time() + 600, token_type="Bearer")
    return b


def _key() -> str:
    from cryptography.fernet import Fernet
    return Fernet.generate_key().decode()


def test_environment_is_read_from_the_api_server() -> None:
    assert environment_of("https://api01.iq.questrade.com/") == "live"
    assert environment_of("https://api01.practice.questrade.com/") == "practice"


def test_a_live_token_cannot_pass_as_practice(tmp_path: Path) -> None:
    """AUTONOMOUS_ACCOUNT=practice with a real token used to mean real orders, under a practice label."""
    b = _questrade(tmp_path, "https://api01.iq.questrade.com/")
    with pytest.raises(BrokerError, match="live environment"):
        b.verify_environment("practice")
    b.verify_environment("live")


def test_a_practice_token_cannot_pass_as_live(tmp_path: Path) -> None:
    b = _questrade(tmp_path, "https://api01.practice.questrade.com/")
    with pytest.raises(BrokerError, match="practice environment"):
        b.verify_environment("live")
    b.verify_environment("practice")
