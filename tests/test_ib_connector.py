"""IBConnectorBroker over a mocked MCP endpoint. Response bodies are ones the real connector returned."""
from __future__ import annotations

import json
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import httpx
import pytest
import respx

from trading_live_claude.brokers import ib_connector as ibc
from trading_live_claude.brokers.base import BrokerError, OrderRejected, TokenExpired
from trading_live_claude.brokers.token_store import TokenSet, TokenStore
from trading_live_claude.monitor import session as sess

SPY = {"underlying_contract_id": 756733, "exchange": "ARCA", "symbol": "SPY", "country_code": "US",
       "sections": [{"security_type": "STK"}]}
BHP = [{"underlying_contract_id": 4036812, "exchange": "ASX", "symbol": "BHP", "country_code": "AU",
        "sections": [{"security_type": "STK"}]},
       {"underlying_contract_id": 4986, "exchange": "NYSE", "symbol": "BHP", "country_code": "US",
        "sections": [{"security_type": "STK"}]},
       {"underlying_contract_id": 542125739, "exchange": "LSE", "symbol": "BHP", "country_code": "GB",
        "sections": [{"security_type": "STK"}]}]
GC = {"underlying_contract_id": 17340718, "exchange": "COMEX", "symbol": "GC",
      "sections": [{"security_type": "IND"}, {"security_type": "FUT"}]}


def _store(tmp_path: Path, expires_in: float = 3600.0) -> TokenStore:
    store = TokenStore(tmp_path / "t.enc", "key")
    store.save(TokenSet("access", "refresh", ibc.MCP_URL, time.time() + expires_in))
    return store


class Endpoint:
    """Answers initialize and tools/call, remembering which tools were called with what."""

    def __init__(self, tools: dict[str, Any], *, sse: bool = False) -> None:
        self.tools, self.sse, self.calls = tools, sse, []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        msg = json.loads(request.content)
        if "id" not in msg:
            return httpx.Response(202)
        if msg["method"] == "initialize":
            result: dict[str, Any] = {"protocolVersion": "2025-06-18"}
        else:
            name, args = msg["params"]["name"], msg["params"]["arguments"]
            self.calls.append((name, args))
            result = {"content": [{"type": "text", "text": json.dumps(self.tools[name])}]}
        body = {"jsonrpc": "2.0", "id": msg["id"], "result": result}
        if self.sse:
            return httpx.Response(200, text=f"event: message\ndata: {json.dumps(body)}\n\n",
                                  headers={"Content-Type": "text/event-stream", "Mcp-Session-Id": "s1"})
        return httpx.Response(200, json=body, headers={"Mcp-Session-Id": "s1"})


def _broker(tmp_path: Path, tools: dict[str, Any], **kw: Any) -> tuple[ibc.IBConnectorBroker, Endpoint]:
    endpoint = Endpoint(tools, **kw)
    respx.post(ibc.MCP_URL).mock(side_effect=endpoint)
    return ibc.IBConnectorBroker(ibc.ConnectorClient(_store(tmp_path), "cid")), endpoint


@respx.mock
def test_quote_maps_a_realtime_snapshot(tmp_path: Path) -> None:
    b, _ = _broker(tmp_path, {
        "search_contracts": {"results": [SPY]},
        "get_price_snapshot": {"volume": {"volume": 484890.0}, "top-status": {"status": "REALTIME"},
                               "last": {"price": 755.54, "ts": 1793780983, "halted": False},
                               "bid-ask": {"bid": 755.54, "bid_size": 580.0, "ask": 755.64, "ask_size": 620.0}}})
    q = b.quote("SPY")
    assert (q.bidPrice, q.askPrice, q.lastTradePrice, q.delay) == (755.54, 755.64, 755.54, 0)
    assert q.symbolId == 756733 and q.bidSize == 580 and q.lastTradeTime is not None


@pytest.mark.parametrize("snapshot", [
    {"top-status": {"status": "FROZEN_DELAYED"}, "last": {"price": 1.0}},
    {"last": {"price": 1.0}},
])
@respx.mock
def test_a_quote_not_stated_realtime_counts_as_delayed(tmp_path: Path, snapshot: dict[str, Any]) -> None:
    b, _ = _broker(tmp_path, {"search_contracts": {"results": [SPY]}, "get_price_snapshot": snapshot})
    assert b.quote("SPY").delay


@respx.mock
def test_the_suffix_picks_the_listing_and_sse_replies_parse(tmp_path: Path) -> None:
    b, _ = _broker(tmp_path, {"search_contracts": {"results": BHP}}, sse=True)
    assert b.resolve("BHP.AX").contract_id == 4036812
    assert ibc.IBConnectorBroker(b._client).resolve("BHP").contract_id == 4986


@respx.mock
def test_an_ambiguous_ticker_is_refused(tmp_path: Path) -> None:
    b, _ = _broker(tmp_path, {"search_contracts": {"results": [SPY, SPY]}})
    with pytest.raises(BrokerError, match="need exactly one"):
        b.resolve("SPY")


@respx.mock
def test_a_future_takes_the_front_month_on_its_own_exchange(tmp_path: Path) -> None:
    b, ep = _broker(tmp_path, {
        "search_contracts": {"results": [GC]},
        "search_futures": {"contracts": [
            {"contract_id": 2, "exchange": "COMEX", "contract_month": "209912", "last_trading_date": "20991229"},
            {"contract_id": 1, "exchange": "COMEX", "contract_month": "209811", "last_trading_date": "20981125"},
            {"contract_id": 9, "exchange": "COMEX", "contract_month": "200001", "last_trading_date": "20000127"}]},
        "get_price_snapshot": {"top-status": {"status": "REALTIME"}, "last": {"price": 4045.8}, "bid-ask": {}}})
    b.set_sec_type("GC", "FUT")
    assert b.quote("/GC").symbolId == 1
    assert ep.calls[-1][1]["exchange"] == "COMEX"


@respx.mock
def test_candles_read_the_parallel_arrays_and_keep_to_the_window(tmp_path: Path) -> None:
    now = datetime.now(UTC).replace(microsecond=0)
    old, new = now - timedelta(days=30), now - timedelta(days=1)
    b, ep = _broker(tmp_path, {
        "search_contracts": {"results": [SPY]},
        "get_price_history": {"time": [old.isoformat().replace("+00:00", "Z"), new.isoformat().replace("+00:00", "Z")],
                              "volume": [1, 21671030], "open": [1.0, 752.72], "close": [1.0, 754.35],
                              "high": [1.0, 757.07], "low": [1.0, 752.39]}})
    bars = b.candles("SPY", now - timedelta(days=5), now)
    assert [c.close for c in bars] == [754.35] and bars[0].volume == 21671030
    assert ep.calls[-1][1]["period"] == "ONE_WEEK"


@respx.mock
def test_equity_refuses_another_currency(tmp_path: Path) -> None:
    b, _ = _broker(tmp_path, {"get_account_summary": {"currency": "CAD", "net_liquidation": 1002232.46}})
    assert b.equity("connector", "CAD") == 1002232.46
    with pytest.raises(BrokerError, match="reports CAD"):
        b.equity("connector", "USD")


@respx.mock
def test_positions_refuse_rows_they_cannot_map(tmp_path: Path) -> None:
    b, _ = _broker(tmp_path, {"get_account_positions": {"positions": []}})
    assert b.positions("connector") == []
    b, _ = _broker(tmp_path, {"get_account_positions": {"positions": [{"anything": 1}]}})
    with pytest.raises(BrokerError, match="not mapped"):
        b.positions("connector")


def test_no_order_can_leave(tmp_path: Path) -> None:
    b = ibc.IBConnectorBroker(ibc.ConnectorClient(_store(tmp_path), "cid"))
    with pytest.raises(OrderRejected):
        b.place_order(None)  # type: ignore[arg-type]
    with pytest.raises(OrderRejected):
        b.cancel_order("connector", 1)
    for tool in ("create_order_instruction", "submit_order", "cancel_order"):
        with pytest.raises(BrokerError, match="not a read tool"):
            b._client.call(tool)


def test_a_token_with_more_than_read_scope_is_refused() -> None:
    with pytest.raises(BrokerError, match="more than"):
        ibc.token_set({"access_token": "a", "scope": "mcp.read mcp.orders.submit"})
    assert "scope=mcp.read&" in ibc.authorize_url("c", "http://127.0.0.1/cb", "x", "s")


@respx.mock
def test_an_expired_token_refreshes_and_saves_the_rotated_one(tmp_path: Path) -> None:
    store = _store(tmp_path, expires_in=-1)
    respx.post(ibc.TOKEN_URL).mock(return_value=httpx.Response(200, json={
        "access_token": "a2", "refresh_token": "r2", "expires_in": 3600}))
    respx.post(ibc.MCP_URL).mock(side_effect=Endpoint({"get_account_positions": {"positions": []}}))
    ibc.ConnectorClient(store, "cid").call("get_account_positions")
    saved = TokenStore(tmp_path / "t.enc", "key").load()
    assert saved is not None and (saved.access_token, saved.refresh_token) == ("a2", "r2")


@respx.mock
def test_a_refused_refresh_says_to_log_in(tmp_path: Path) -> None:
    respx.post(ibc.TOKEN_URL).mock(return_value=httpx.Response(400))
    with pytest.raises(TokenExpired, match="ib_connector_login"):
        ibc.ConnectorClient(_store(tmp_path, expires_in=-1), "cid").call("get_account_positions")


def test_a_money_session_cannot_use_the_connector(tmp_path: Path) -> None:
    settings = SimpleNamespace(state_dir=tmp_path, token_encryption_key="key")
    with pytest.raises(sess.SessionError, match="read-only"):
        sess.venue_feed(settings, "ib", money=True, transport="connector")


def test_a_paper_session_needs_a_login_first(tmp_path: Path) -> None:
    settings = SimpleNamespace(state_dir=tmp_path, token_encryption_key="key")
    with pytest.raises(sess.SessionError, match="ib_connector_login"):
        sess.venue_feed(settings, "ib", money=False, transport="connector")
