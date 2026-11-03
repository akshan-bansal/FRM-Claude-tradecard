"""What the money path does when something goes wrong: a refused order, an unreadable file, a venue that says no.

Adapters here answer through fakes and ``respx``; nothing reaches a broker. Questrade's own HTTP layer stays out: tests do
not build a Questrade broker (CLAUDE.md), so what it sends is checked at ``to_questrade_payload`` in its own test file.
"""
from __future__ import annotations

import base64
import csv
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import httpx
import pandas as pd
import pytest
import respx
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives.asymmetric.rsa import generate_private_key

from tests.test_ib_web_broker import _CP_BASE, _mk_broker
from tests.test_monitor_guards import _Feed as _MonitorFeed
from tests.test_monitor_guards import _monitor, _qty, _Script
from tests.test_router import _buy, _intent, _StubBroker
from tests.test_session import _Feed, _plan, _settings
from trading_live_claude.brokers import kraken as kraken_module
from trading_live_claude.brokers.base import BrokerError, OrderRejected, StaleQuote
from trading_live_claude.brokers.fresh import FreshQuoteBroker
from trading_live_claude.brokers.fx import CurrencyNormalizingBroker, FxRates, FxUnavailable, ib_spot_rates
from trading_live_claude.brokers.kraken import KrakenAuthError, KrakenBroker, _pair_to_symbol
from trading_live_claude.brokers.models import Order, OrderAction, OrderType, Quote
from trading_live_claude.brokers.paper import PaperBroker
from trading_live_claude.execution.approval import verify_audit_record, wire_card_approval
from trading_live_claude.execution.daily_budget import DailyBudget
from trading_live_claude.execution.router import LiveModeNotConfirmed, OrderIntent, Router, route_key
from trading_live_claude.execution.venue_costs import VenueCostModel
from trading_live_claude.monitor import session as sess
from trading_live_claude.monitor.session import SessionOptions, StrategyBook, build_session
from trading_live_claude.risk.equity_watch import EquityWatch
from trading_live_claude.risk.kill_switch import KillSwitch
from trading_live_claude.strategies.examples.ema_crossover import EmaCrossover


def _order(order_type: OrderType = OrderType.MARKET, **kw: Any) -> Order:
    return Order(symbol="SOL/USD", symbolId=0, accountId="A", action=OrderAction.BUY, orderType=order_type,
                 totalQuantity=1.5, **kw)


# ---------------------------------------------------------------- Kraken

@pytest.fixture
def wire(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    state: dict[str, Any] = {"sent": [], "answer": {"txid": ["OZAA6H-FQI3S-DK6GHJ"]}}

    def fake(path: str, **k: Any) -> Any:
        state["sent"].append((path, k.get("data")))
        if isinstance(state["answer"], Exception):
            raise state["answer"]
        return state["answer"]

    monkeypatch.setattr(kraken_module, "private_post", fake)
    return state


def _kraken(live: bool = True, keys: bool = True) -> KrakenBroker:
    return KrakenBroker(api_key="k" if keys else "", api_secret="c2VjcmV0" if keys else "", enable_live_orders=live)


def test_kraken_sends_a_limits_price_and_a_stops_second_price(wire: dict[str, Any]) -> None:
    _kraken().place_order(_order(OrderType.LIMIT, limitPrice=100.0))
    assert wire["sent"][-1][1]["price"] == "100"
    _kraken().place_order(_order(OrderType.STOP_LIMIT, limitPrice=100.0, stopPrice=95.0))
    assert wire["sent"][-1][1]["price2"] == "95"


def test_kraken_keeps_the_real_txid_for_cancels(wire: dict[str, Any]) -> None:
    b = _kraken()
    placed = b.place_order(_order())
    assert placed.broker_ref == "OZAA6H-FQI3S-DK6GHJ"
    b.cancel_order("A", placed.id)
    assert wire["sent"][-1] == ("/0/private/CancelOrder", {"txid": "OZAA6H-FQI3S-DK6GHJ"})


def test_kraken_refusing_an_order_is_a_rejection(wire: dict[str, Any]) -> None:
    wire["answer"] = KrakenAuthError("EOrder:Insufficient funds")
    with pytest.raises(OrderRejected, match="Insufficient funds"):
        _kraken().place_order(_order())


def test_kraken_with_no_keys_refuses_before_sending(wire: dict[str, Any]) -> None:
    with pytest.raises(OrderRejected, match="no API credentials"):
        _kraken(keys=False).place_order(_order())
    with pytest.raises(BrokerError, match="no API credentials"):
        _kraken(keys=False).cancel_order("A", 1)
    with pytest.raises(BrokerError, match="no API credentials"):
        _kraken(keys=False).executions("A")
    assert wire["sent"] == []


def test_kraken_will_not_cancel_what_it_has_no_txid_for(wire: dict[str, Any]) -> None:
    with pytest.raises(BrokerError, match="no Kraken txid"):
        _kraken().cancel_order("A", 12345)
    assert wire["sent"] == []


def test_a_failed_kraken_cancel_is_an_error_not_silence(wire: dict[str, Any]) -> None:
    b = _kraken()
    placed = b.place_order(_order())
    wire["answer"] = KrakenAuthError("EOrder:Unknown order")
    with pytest.raises(BrokerError, match="CancelOrder failed"):
        b.cancel_order("A", placed.id)


def test_paper_kraken_cannot_cancel(wire: dict[str, Any]) -> None:
    with pytest.raises(BrokerError, match="live orders disabled"):
        _kraken(live=False).cancel_order("A", 1)


def test_kraken_asks_for_trades_since_a_time(wire: dict[str, Any]) -> None:
    wire["answer"] = {"trades": {}}
    assert _kraken().executions("A", since=datetime(2026, 11, 3, tzinfo=UTC)) == []
    assert wire["sent"][-1][1]["start"] == str(int(datetime(2026, 11, 3, tzinfo=UTC).timestamp()))


@respx.mock
def test_kraken_lot_rules_come_from_its_answer_and_a_miss_is_left_out() -> None:
    def respond(request: httpx.Request) -> httpx.Response:
        if request.url.params["pair"] == "SOLUSD":
            return httpx.Response(200, json={"error": [], "result": {"SOLUSD": {"lot_decimals": 2, "ordermin": "0.05", "costmin": "5"}}})
        return httpx.Response(200, json={"error": ["EQuery:Unknown asset pair"], "result": {}})

    respx.get("https://api.kraken.com/0/public/AssetPairs").mock(side_effect=respond)
    rules = _kraken(live=False, keys=False).lot_rules(["SOL/USD", "NOPE/USD"])
    assert set(rules) == {"SOL/USD"} and rules["SOL/USD"].step == 0.01 and rules["SOL/USD"].min_qty == 0.05


def test_kraken_with_no_symbols_makes_no_call() -> None:
    assert _kraken(live=False, keys=False).quotes([]) == []


@pytest.mark.parametrize(("wire_pair", "routed"), [("XXBTZUSD", "BTC/USD"), ("XETHZUSD", "ETH/USD"), ("SOLUSD", "SOL/USD"),
                                                   ("XDGUSD", "XDG/USD"), ("XXMRZUSD", "XMR/USD"), ("WEIRD", "WEIRD")])
def test_kraken_pairs_map_back_to_how_the_book_names_them(wire_pair: str, routed: str) -> None:
    assert _pair_to_symbol(wire_pair) == routed


# ---------------------------------------------------------------- IB

@respx.mock
def test_ib_cancels_only_when_live_orders_are_on() -> None:
    with pytest.raises(BrokerError, match="live orders disabled"):
        _mk_broker().cancel_order("DU111", 5)
    route = respx.delete(f"{_CP_BASE}/iserver/account/DU111/order/5").mock(return_value=httpx.Response(200, json={}))
    _mk_broker(enable_live=True).cancel_order("DU111", 5)
    assert route.called


@respx.mock
def test_ib_gives_up_on_chart_data_after_retrying(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("time.sleep", lambda _s: None)
    respx.post(f"{_CP_BASE}/iserver/secdef/search").mock(return_value=httpx.Response(200, json=[{"conid": 1, "symbol": "AAPL"}]))
    route = respx.get(f"{_CP_BASE}/iserver/marketdata/history").mock(return_value=httpx.Response(500, text="chart data unavailable"))
    start, end = datetime(2026, 1, 1, tzinfo=UTC), datetime(2026, 2, 1, tzinfo=UTC)
    assert _mk_broker().candles("AAPL", start, end, "OneDay") == [] and route.call_count > 1


@respx.mock
def test_ib_raises_on_any_other_chart_error() -> None:
    respx.post(f"{_CP_BASE}/iserver/secdef/search").mock(return_value=httpx.Response(200, json=[{"conid": 1, "symbol": "AAPL"}]))
    respx.get(f"{_CP_BASE}/iserver/marketdata/history").mock(return_value=httpx.Response(403, text="forbidden"))
    start, end = datetime(2026, 1, 1, tzinfo=UTC), datetime(2026, 2, 1, tzinfo=UTC)
    with pytest.raises(BrokerError, match="403"):
        _mk_broker().candles("AAPL", start, end, "OneDay")


@respx.mock
@pytest.mark.parametrize(("body", "equity"), [({"netliquidation": {"amount": "x", "currency": "USD"}}, 0.0),
                                              ({"netliquidation": {"amount": 5.0, "currency": "EUR"}}, 0.0),
                                              ({"netliquidation": {"amount": 5.0, "currency": "USD"}}, 5.0)])
def test_ib_equity_reads_only_a_clean_figure_in_the_currency_asked(body: dict[str, Any], equity: float) -> None:
    respx.get(f"{_CP_BASE}/portfolio/DU111/summary").mock(return_value=httpx.Response(200, json=body))
    assert _mk_broker().equity("DU111", "USD") == equity


# ---------------------------------------------------------------- the gate's refusals

def _router(tmp: Path, **kw: Any) -> Router:
    return Router.build_default(mode="paper", broker=_StubBroker(), state_dir=tmp, **kw)


BOOK = {"existing_risk": 0.0, "open_positions": 0, "current_open_notional": 0.0}


def test_no_equity_refuses_an_entry(tmp_path: Path) -> None:
    assert "equity 0" in _router(tmp_path)._gate(_intent(), equity=0.0, **BOOK).rejected_reasons[0]


def test_a_short_stop_below_the_entry_is_refused(tmp_path: Path) -> None:
    intent = OrderIntent(symbol="AAPL", action=OrderAction.SELL, shares=10, entry=100.0, stop=95.0, target=None,
                         strategy="t", risk_dollars=50.0, account_number="A")
    reasons = _router(tmp_path)._gate(intent, equity=100_000.0, **BOOK).rejected_reasons
    assert any("short stop" in r for r in reasons)


class _Spectral:
    def __init__(self, max_shares: float) -> None:
        self.max_shares = max_shares

    def thresholds(self) -> dict[str, Any]:
        return {}

    def check(self, _s: str, **_k: Any) -> SimpleNamespace:
        return SimpleNamespace(reason=None, max_shares=self.max_shares, summary="99% VaR", observed={"max_shares": self.max_shares})


def test_a_var_cap_that_admits_nothing_refuses_the_entry(tmp_path: Path) -> None:
    r = _router(tmp_path)
    r.spectral_gate = _Spectral(0.4)  # type: ignore[assignment]
    assert any("admits no tradeable size" in x for x in r._gate(_intent(shares=100), equity=100_000.0, **BOOK).rejected_reasons)


def test_a_var_trim_can_fall_under_the_minimum_ticket(tmp_path: Path) -> None:
    r = _router(tmp_path, min_ticket_usd=500.0)
    r.spectral_gate = _Spectral(1.0)  # type: ignore[assignment]
    assert any("after spectral VaR trim" in x for x in r._gate(_intent(shares=100), equity=100_000.0, **BOOK).rejected_reasons)


def test_a_router_is_only_live_through_its_confirmation(tmp_path: Path) -> None:
    kill = KillSwitch(tmp_path)
    with pytest.raises(LiveModeNotConfirmed):
        Router(mode="live", broker=_StubBroker(), journal=SimpleNamespace(), kill_switch=kill, heat=SimpleNamespace())  # type: ignore[arg-type]


def test_a_route_is_named_by_its_venue_then_its_name_then_default() -> None:
    assert route_key(SimpleNamespace(venue="kraken", name="x")) == "kraken"
    assert route_key(SimpleNamespace(venue=None, name="x")) == "x"
    assert route_key(SimpleNamespace(venue=None, name="")) == "default"


@pytest.mark.parametrize("row", [{"side": "long", "entry_price": 100.0, "quantity": 1.0},
                                 {"symbol": "A", "side": "short", "entry_price": 100.0, "quantity": 1.0},
                                 {"symbol": "A", "side": "long", "entry_price": 0.0, "quantity": 1.0},
                                 {"symbol": "A", "side": "long", "entry_price": 100.0, "quantity": 0.0}])
def test_forced_exits_skip_a_row_they_cannot_judge(tmp_path: Path, row: dict[str, Any]) -> None:
    assert _router(tmp_path).check_forced_exits([row], {"A": 1.0}, {"A": 5.0}) == []


def test_the_fee_counts_in_the_headroom_on_a_percentage_venue(tmp_path: Path) -> None:
    """Filling the book to the leverage cap must leave cash for the fee: a real spot order would be refused otherwise."""
    book = PaperBroker(feed=_MonitorFeed(), journal_dir=tmp_path, slippage_bps=0.0)  # type: ignore[arg-type]
    r = Router.build_default(mode="paper", broker=book, state_dir=tmp_path, max_position_notional_pct=1.0, cap_pct=1.0)
    assert r.submit(_buy("BTC/USD", 1000.0, 100.0), equity=100_000.0, existing_risk=0.0, open_positions=0) is not None
    assert book._cash >= 0.0


# ---------------------------------------------------------------- the kill-switch

def test_no_equity_trips_the_switch(tmp_path: Path) -> None:
    assert KillSwitch(tmp_path).evaluate(equity=0.0, peak_equity=100.0, day_open_equity=100.0).reason.endswith("equity <= 0")


def test_a_ledger_failure_never_stops_a_trip(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, record_log: Any) -> None:
    from trading_live_claude.audit import ledger as ledger_module
    from trading_live_claude.risk import kill_switch as kill_module

    def boom(*_a: Any, **_k: Any) -> Any:
        raise OSError("disk full")

    monkeypatch.setattr(ledger_module, "Ledger", boom)
    rec = record_log(kill_module)
    KillSwitch(tmp_path).trip("manual")
    assert KillSwitch(tmp_path).state().halted and "kill_switch.ledger_failed" in rec.events


def test_an_unreadable_sentinel_still_halts(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    KillSwitch(tmp_path).trip("manual")
    original = Path.read_text

    def refuse(self: Path, *a: Any, **k: Any) -> str:
        if self.name == "HALTED":
            raise OSError("locked")
        return original(self, *a, **k)

    monkeypatch.setattr(Path, "read_text", refuse)
    state = KillSwitch(tmp_path).state()
    assert state.halted and state.reason == "unknown"


def test_a_clear_needs_the_phrase(tmp_path: Path) -> None:
    KillSwitch(tmp_path).trip("manual")
    with pytest.raises(PermissionError):
        KillSwitch(tmp_path).clear("please")
    assert KillSwitch(tmp_path).state().halted


def test_a_clear_with_nothing_tripped_changes_nothing(tmp_path: Path) -> None:
    KillSwitch(tmp_path).clear("I HAVE INVESTIGATED")
    assert KillSwitch(tmp_path).last_cleared() is None


def _journal(tmp: Path, rows: list[dict[str, str]]) -> None:
    with (tmp / EquityWatch.FILENAME).open("w", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, fieldnames=["ts", "account", "mode", "currency", "equity"])
        w.writeheader()
        w.writerows(rows)


def test_a_torn_journal_line_does_not_lose_the_peak(tmp_path: Path) -> None:
    now = datetime.now(UTC).isoformat()
    _journal(tmp_path, [{"ts": now, "account": "A", "mode": "live", "currency": "CAD", "equity": "1000"},
                        {"ts": now, "account": "A", "mode": "live", "currency": "CAD", "equity": "not a number"}])
    assert EquityWatch(KillSwitch(tmp_path), account="A", mode="live", currency="CAD")._peak == 1000.0


def test_an_unreadable_journal_is_logged_and_starts_fresh(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, record_log: Any) -> None:
    from trading_live_claude.risk import equity_watch as watch_module
    rec = record_log(watch_module)

    def refuse(self: Path, *_a: Any, **_k: Any) -> Any:
        raise PermissionError("locked")

    monkeypatch.setattr(Path, "open", refuse)
    watch = EquityWatch(KillSwitch(tmp_path), account="A", mode="live", currency="CAD")
    assert watch._peak == 0.0 and "equity_watch.resume_failed" in rec.events


# ---------------------------------------------------------------- the daemon's budget

def test_the_budget_skips_a_row_it_cannot_date_and_other_days(tmp_path: Path) -> None:
    (tmp_path / "orders.jsonl").write_text(
        '{"ts": "not a time", "mode": "autonomous", "shares": 5, "entry": 10}\n'
        '{"ts": "2001-01-01T00:00:00+00:00", "mode": "autonomous", "shares": 5, "entry": 10}\n', encoding="utf-8")
    snap = DailyBudget(tmp_path, max_trades_per_day=5, max_notional_per_day_usd=1000.0, mode="autonomous").snapshot()
    assert snap.trades_today == 0 and snap.notional_today_usd == 0.0


# ---------------------------------------------------------------- costs

def test_nothing_traded_costs_nothing() -> None:
    m = VenueCostModel.for_venue("kraken")
    assert m.cost_for(shares=0.0, price=10.0) == 0.0 and m.round_trip_cost_ratio(shares=0.0, price=10.0) == 0.0


def test_an_override_keeps_the_rest_of_the_fee_shape() -> None:
    m = VenueCostModel.for_venue("kraken", slippage_bps=1.0)
    assert m.slippage_bps == 1.0 and m.commission_bps == 26.0


def test_ib_has_a_minimum_per_order_a_cap_and_a_ticket_floor() -> None:
    m = VenueCostModel.for_venue("ib")
    assert m.cost_for(shares=1.0, price=100.0) == 1.0                         # the $1 minimum
    assert m.cost_for(shares=1000.0, price=0.01) == pytest.approx(0.1)        # capped at 1% of value
    assert m.min_notional_for(max_cost_ratio=0.05, price=100.0, shares=100.0) > 0.0
    assert m.min_notional_for(max_cost_ratio=0.0, price=100.0, shares=100.0) == float("inf")
    assert m.min_notional_for(max_cost_ratio=0.0011, price=1.0, shares=100.0) == float("inf")


# ---------------------------------------------------------------- the feed wrappers pass through

class _Inner(_StubBroker):
    def __init__(self) -> None:
        super().__init__()
        self.cancelled: list[Any] = []

    def equity(self, _a: str, currency: str = "CAD") -> float:
        return 42.0

    def cancel_order(self, account: str, order_id: int) -> None:
        self.cancelled.append((account, order_id))


def test_the_guard_hides_nothing_from_the_broker_behind_it() -> None:
    inner = _Inner()
    g = FreshQuoteBroker(inner)  # type: ignore[arg-type]
    assert g.accounts() == [] and g.positions("A") == [] and g.equity("A") == 42.0
    g.place_order(_order())
    g.cancel_order("A", 7)
    assert inner.placed and inner.cancelled == [("A", 7)]
    with pytest.raises(AttributeError):
        g.no_such_thing  # noqa: B018


def test_the_guard_refuses_a_batch_with_a_stale_quote_in_it() -> None:
    class _Halted(_Inner):
        def quotes(self, symbols: list[str]) -> list[Quote]:
            return [Quote(symbol=s, symbolId=1, bidPrice=1.0, askPrice=1.0, lastTradePrice=1.0, isHalted=True) for s in symbols]

    with pytest.raises(StaleQuote):
        FreshQuoteBroker(_Halted(), on_stale="raise").quotes(["A"])  # type: ignore[arg-type]
    assert len(FreshQuoteBroker(_Halted(), on_stale="warn").quotes(["A"])) == 1  # type: ignore[arg-type]


def test_the_currency_wrapper_hides_nothing_from_the_broker_behind_it() -> None:
    inner = _Inner()
    w = CurrencyNormalizingBroker(inner, SimpleNamespace(rate=lambda _c: 1.25))  # type: ignore[arg-type]
    assert w.accounts() == [] and w.quotes(["AAPL"])[0].bidPrice == pytest.approx(99.5 * 1.25)
    w.cancel_order("A", 9)
    assert inner.cancelled == [("A", 9)]


def test_a_quote_in_the_home_currency_is_left_alone() -> None:
    w = CurrencyNormalizingBroker(_Inner(), SimpleNamespace(rate=lambda _c: 1.0))  # type: ignore[arg-type]
    assert w.quote("XIC.TO").bidPrice == 99.5


def test_ib_spot_rates_read_the_forex_contract() -> None:
    ib = SimpleNamespace(quote_contract=lambda contract: SimpleNamespace(mid={"USDCAD": 1.25}[contract.symbol]))
    assert ib_spot_rates(ib, "CAD").rate("USD") == pytest.approx(1.25)  # type: ignore[arg-type]


def test_spot_rates_hold_a_recent_rate_through_a_gap_then_say_so() -> None:
    clock = [datetime(2026, 11, 3, 15, 0, tzinfo=UTC)]
    answers: list[float | None] = [1.25, None]
    rates = FxRates(lambda _p: answers.pop(0) if answers else None, "CAD", ttl_s=1.0, max_age_s=100.0, clock=lambda: clock[0])
    assert rates.rate("USD") == pytest.approx(1.25)
    clock[0] = clock[0].replace(second=10)
    assert rates.rate("USD") == pytest.approx(1.25)
    clock[0] = clock[0].replace(minute=30)
    with pytest.raises(FxUnavailable):
        rates.rate("USD")


# ---------------------------------------------------------------- evidence that does not verify

def _pem(key: Any) -> str:
    return key.public_key().public_bytes(encoding=serialization.Encoding.PEM,
                                         format=serialization.PublicFormat.SubjectPublicKeyInfo).decode()


def _signed() -> dict[str, Any]:
    key = Ed25519PrivateKey.generate()
    return {"canonical": "abc", "signature": base64.b64encode(key.sign(b"abc")).decode(), "pubkey_pem": _pem(key)}


def test_good_evidence_verifies() -> None:
    assert verify_audit_record(_signed()) == (True, "ok")


@pytest.mark.parametrize(("change", "why"), [({"pubkey_pem": ""}, "public key"), ({"canonical": ""}, "canonical bytes"),
                                             ({"signature_alg": "rsa"}, "unsupported"), ({"canonical": "abd"}, "does not verify"),
                                             ({"pubkey_pem": "not a key"}, "does not parse"), ({"verdict": "EXPIRED"}, "nothing signed it")])
def test_missing_or_wrong_evidence_never_passes(change: dict[str, Any], why: str) -> None:
    ok, reason = verify_audit_record(_signed() | change)
    assert ok is False and why in reason


def test_another_kind_of_key_is_refused() -> None:
    rsa = generate_private_key(public_exponent=65537, key_size=2048)
    ok, reason = verify_audit_record(_signed() | {"pubkey_pem": _pem(rsa)})
    assert ok is False and "not Ed25519" in reason


def test_card_wiring_without_a_database_or_a_shim_uses_a_store_in_memory(tmp_path: Path) -> None:
    router = Router.build_default(mode="paper", broker=PaperBroker(feed=_Feed(), journal_dir=tmp_path, venue="kraken"),  # type: ignore[arg-type]
                                  state_dir=tmp_path)
    wiring = wire_card_approval(router, start_shim=False, auth_token="fixed", shim_port=9999)
    assert wiring.auth_token == "fixed" and wiring.desk_url.endswith(":9999/desk") and wiring.router.inner is router


# ---------------------------------------------------------------- the session's own words

def test_a_session_says_what_it_is_running_with(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(sess, "venue_weight_bias", lambda *_a, **_k: None)
    settings = _settings(tmp_path)
    opts = SessionOptions(profit_lock=True, candle_exit=True, overbought_exit=True, oversold_entry=True, mute_alerts="AAA",
                          max_cost_ratio=0.02, warmup_interval=60, parallel_sizing=False)
    lines: list[str] = []
    build_session(settings, _plan(settings, options=opts), feed=_Feed(), book=StrategyBook(default=EmaCrossover()), emit=lines.append)  # type: ignore[arg-type]
    said = "\n".join(lines)
    for phrase in ("profit lock ON", "candle exit ON", "overbought exit ON", "oversold entry ON", "alerts muted for: AAA",
                   "cost floor ON", "warm-up:", "sizing: sequential", "flattens"):
        assert phrase in said, phrase


def test_a_session_can_run_with_no_ledger_on_paper(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(sess, "venue_weight_bias", lambda *_a, **_k: None)
    settings = _settings(tmp_path)
    session = build_session(settings, _plan(settings, options=SessionOptions(audit_ledger=False)), feed=_Feed(),  # type: ignore[arg-type]
                            book=StrategyBook(default=EmaCrossover()))
    assert session.ledger is None


def test_a_session_says_when_the_spectral_gate_is_on(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(sess, "venue_weight_bias", lambda *_a, **_k: None)
    monkeypatch.setattr(sess, "attach_spectral_gate", lambda *_a, **_k: SimpleNamespace(thresholds=lambda: {"cap_pct": 0.005}))
    settings = _settings(tmp_path)
    lines: list[str] = []
    build_session(settings, _plan(settings), feed=_Feed(), book=StrategyBook(default=EmaCrossover()), emit=lines.append)  # type: ignore[arg-type]
    assert any("spectral VaR gate ON" in x for x in lines)


def test_the_real_rate_source_reads_the_audit_layers_cache(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.undo()                                 # the suite's 1:1 stand-in, for this one test
    rates = sess._home_rates(_settings(tmp_path), "CAD")
    assert rates.numeraire == "CAD" and rates.fx.cache_dir == Path(tmp_path) / "fx"


# ---------------------------------------------------------------- the monitor, when a read or an order fails

def test_flatten_reports_what_it_could_not_close(tmp_path: Path) -> None:
    m, pb, feed = _monitor(tmp_path, _Script(entry=1))
    m.step()
    assert _qty(pb) > 0

    def boom(*_a: Any, **_k: Any) -> Any:
        raise BrokerError("venue down")

    m.router.submit = boom  # type: ignore[method-assign]
    assert m.flatten() == ["BTC/USD"]
    m.router.submit = lambda *_a, **_k: None  # type: ignore[method-assign]
    assert m.flatten() == ["BTC/USD"]
    m._exit_price = lambda _s: 0.0  # type: ignore[method-assign]
    assert m.flatten() == ["BTC/USD"]


def test_flatten_closes_the_book_when_it_can(tmp_path: Path) -> None:
    m, pb, _ = _monitor(tmp_path, _Script(entry=1))
    m.step()
    assert m.flatten() == [] and _qty(pb) == 0.0
    assert m.flatten() == []                             # already flat


def test_close_symbols_reports_each_one(tmp_path: Path) -> None:
    m, pb, _ = _monitor(tmp_path, _Script(entry=1))
    m.step()
    held = _qty(pb)
    assert m.close_symbols([]) == []
    m._is_open = lambda _s: False  # type: ignore[method-assign]
    assert m.close_symbols(["btc/usd"])[0]["reason"] == "venue closed"
    m._is_open = lambda _s: True  # type: ignore[method-assign]
    real_price = m._exit_price
    m._exit_price = lambda _s: 0.0  # type: ignore[method-assign]
    assert m.close_symbols(["BTC/USD"])[0]["reason"] == "no price"
    m._exit_price = real_price  # type: ignore[method-assign]
    rows = m.close_symbols(["BTC/USD", "NOT-HELD"])
    assert rows[0]["accepted"] is True and rows[0]["qty"] == held and _qty(pb) == 0.0


def test_trim_to_slots_scales_the_book_and_skips_what_it_must(tmp_path: Path) -> None:
    m, pb, _ = _monitor(tmp_path, _Script(entry=1), ["AAA", "BBB"])
    m.router.max_position_notional_pct = 0.5
    m.router.heat.cap_pct = 1.0
    m.step()
    assert m.trim_to_slots() != [] and _qty(pb, "AAA") > 0
    m.router.max_open_positions = 0
    assert m.trim_to_slots() == []                       # no cap to trim to


def test_a_failed_positions_read_does_not_stop_a_joint_allocation(tmp_path: Path) -> None:
    m, pb, _ = _monitor(tmp_path, _Script(entry=1), parallel_sizing=True)
    real = pb.positions
    calls = {"n": 0}

    def flaky(account: str) -> list[Any]:
        calls["n"] += 1
        if calls["n"] == 2:
            raise BrokerError("positions unavailable")
        return real(account)

    pb.positions = flaky  # type: ignore[method-assign]
    m.step()
    assert calls["n"] >= 2


def test_too_little_history_skips_the_symbol(tmp_path: Path, record_log: Any) -> None:
    from trading_live_claude.monitor import live_loop
    rec = record_log(live_loop)
    m, pb, _ = _monitor(tmp_path, _Script(entry=1))
    m.market.recent = lambda *_a, **_k: pd.DataFrame({"close": [1.0]})  # type: ignore[method-assign]
    assert m.step() == []
    assert "monitor.insufficient_history" in rec.events and _qty(pb) == 0.0


def test_a_ledger_failure_never_breaks_a_poll(tmp_path: Path, record_log: Any) -> None:
    from trading_live_claude.monitor import live_loop
    rec = record_log(live_loop)

    class _Broken:
        def append(self, *_a: Any, **_k: Any) -> None:
            raise OSError("disk full")

    m, _, _ = _monitor(tmp_path, _Script(entry=1))
    m.ledger = _Broken()
    m._ledger_signal("BTC/USD", SimpleNamespace(name="x"), 1.0, None, sized=SimpleNamespace(shares=1.0), intent_id=None,
                     reason="", conviction=1.0)
    m._ledger_forced_exit("BTC/USD", SimpleNamespace(name="x"), 1.0, 1.0, "intent", "stop")
    assert {"monitor.ledger_signal_failed", "monitor.ledger_forced_exit_failed"} <= set(rec.events)


def test_a_thesis_trim_reaches_the_order_and_the_alert(tmp_path: Path) -> None:
    m, _, _ = _monitor(tmp_path, _Script(entry=1), emit_on_change_only=True)
    m._interpret_bias = lambda _s: (0.5, ["Oil shock"])  # type: ignore[method-assign]
    events = m.step()
    assert events[0].detail["interpret"]["bias"] == 0.5
