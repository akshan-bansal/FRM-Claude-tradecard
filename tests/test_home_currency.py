"""Every figure a session's gates read is in CAD, however the symbol is priced where it trades."""
from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import httpx
import pytest
import respx

from tests.test_session import _Feed, _plan, _settings
from trading_live_claude.audit.queries import fill_quality
from trading_live_claude.audit.semantics import BOC_SERIES
from trading_live_claude.brokers.base import StaleQuote
from trading_live_claude.brokers.fx import BankOfCanadaRates, CurrencyNormalizingBroker, FxUnavailable
from trading_live_claude.brokers.models import Order, OrderAction, OrderType, Position, Quote
from trading_live_claude.brokers.paper import PaperBroker
from trading_live_claude.execution.basket import ib_refusal
from trading_live_claude.execution.router import OrderIntent
from trading_live_claude.monitor import session as sess
from trading_live_claude.monitor.session import StrategyBook, build_session
from trading_live_claude.risk.quantity import QuantityRule
from trading_live_claude.strategies.examples.ema_crossover import EmaCrossover
from trading_live_claude.venues import currency_of

NOON = datetime(2026, 11, 3, 17, 0, tzinfo=UTC)         # a Tuesday
VALET = "https://www.bankofcanada.ca/valet/observations/"


def _cache(tmp: Path, **series: dict[str, float]) -> Path:
    d = tmp / "fx"
    d.mkdir(parents=True, exist_ok=True)
    for name, rows in series.items():
        (d / f"{name}.json").write_text(json.dumps(rows), encoding="utf-8")
    return d


def _rates(d: Path, numeraire: str = "CAD", now: datetime = NOON) -> BankOfCanadaRates:
    return BankOfCanadaRates(d, numeraire, clock=lambda: now)


# ---------------------------------------------------------------- the rate source

@respx.mock
def test_a_rate_comes_from_the_bank_and_the_home_currency_needs_none(tmp_path: Path) -> None:
    route = respx.get(f"{VALET}FXUSDCAD/json").mock(return_value=httpx.Response(200, json={
        "observations": [{"d": "2026-11-03", "FXUSDCAD": {"v": "1.25"}}]}))
    r = _rates(tmp_path / "fx")
    assert r.rate("CAD") == 1.0 and not route.called
    assert r.rate("USD") == 1.25 and route.called


@respx.mock
def test_it_asks_the_bank_at_most_once_per_ttl(tmp_path: Path) -> None:
    route = respx.get(f"{VALET}FXUSDCAD/json").mock(return_value=httpx.Response(200, json={
        "observations": [{"d": "2026-11-03", "FXUSDCAD": {"v": "1.25"}}]}))
    r = _rates(tmp_path / "fx")
    r.rate("USD")
    r.rate("USD")
    assert route.call_count == 1


@respx.mock
def test_an_outage_falls_back_to_the_cache(tmp_path: Path) -> None:
    respx.get(f"{VALET}FXUSDCAD/json").mock(return_value=httpx.Response(503))
    assert _rates(_cache(tmp_path, FXUSDCAD={"2026-11-02": 1.3})).rate("USD") == 1.3


@respx.mock
def test_no_rate_at_all_is_said_not_guessed(tmp_path: Path) -> None:
    respx.get(f"{VALET}FXUSDCAD/json").mock(return_value=httpx.Response(503))
    with pytest.raises(FxUnavailable, match="USD"):
        _rates(tmp_path / "fx").rate("USD")


@respx.mock
def test_a_currency_with_no_series_is_said_not_guessed(tmp_path: Path) -> None:
    with pytest.raises(FxUnavailable, match="XXX"):
        _rates(tmp_path / "fx").rate("XXX")


@respx.mock
def test_a_stablecoin_takes_the_usd_rate(tmp_path: Path) -> None:
    respx.get(f"{VALET}FXUSDCAD/json").mock(return_value=httpx.Response(200, json={"observations": []}))
    assert _rates(_cache(tmp_path, FXUSDCAD={"2026-11-03": 1.25})).rate("USDT") == 1.25


@respx.mock
def test_another_numeraire_triangulates_through_cad(tmp_path: Path) -> None:
    respx.get(url__startswith=VALET).mock(return_value=httpx.Response(200, json={"observations": []}))
    d = _cache(tmp_path, FXUSDCAD={"2026-11-03": 1.25}, FXJPYCAD={"2026-11-03": 0.01})
    assert _rates(d, "USD").rate("JPY") == pytest.approx(0.008)
    assert _rates(d, "USD").rate("CAD") == pytest.approx(0.8)


@pytest.mark.parametrize("symbol", ["AAPL", "VOD.L", "BHP.AX", "7203.T", "0700.HK", "/GC", "SOL/USD"])
def test_every_market_the_desk_can_trade_has_a_rate(symbol: str) -> None:
    assert currency_of(symbol) in BOC_SERIES


# ---------------------------------------------------------------- a real book through the wrapper

class _Book:
    name = venue = "kraken"

    def __init__(self) -> None:
        self.asked: list[str] = []
        self.placed: list[Order] = []

    def positions(self, _a: str) -> list[Position]:
        return [Position(symbol="SOL/USD", symbolId=0, openQuantity=10.0, currentPrice=100.0, currentMarketValue=1000.0,
                         averageEntryPrice=80.0, totalCost=800.0, openPnl=200.0)]

    def equity(self, _a: str, currency: str = "CAD") -> float:
        self.asked.append(currency)
        return 2_000.0

    def quote(self, symbol: str) -> Quote:
        return Quote(symbol=symbol, symbolId=1, bidPrice=99.0, askPrice=101.0, lastTradePrice=100.0)

    def lot_rules(self, _s: list[str]) -> dict[str, QuantityRule]:
        return {}

    def place_order(self, order: Order) -> Order:
        self.placed.append(order)
        return order


def _wrapped(rate: float = 1.25, **kw: Any) -> tuple[CurrencyNormalizingBroker, _Book]:
    inner = _Book()
    return CurrencyNormalizingBroker(inner, SimpleNamespace(rate=lambda c: 1.0 if c == "CAD" else rate), **kw), inner  # type: ignore[arg-type]


def test_a_position_reads_in_the_home_currency() -> None:
    w, _ = _wrapped()
    p = w.positions("X")[0]
    assert (p.currentPrice, p.currentMarketValue, p.averageEntryPrice, p.totalCost, p.openPnl) == (125.0, 1250.0, 100.0, 1000.0, 250.0)
    assert p.openQuantity == 10.0                     # units are units


def test_equity_is_asked_for_natively_and_converted() -> None:
    w, inner = _wrapped(native_equity="USD")
    assert w.equity("X", "CAD") == 2_500.0 and inner.asked == ["USD"]


def test_a_venue_that_can_answer_in_the_home_currency_is_just_asked() -> None:
    w, inner = _wrapped()
    assert w.equity("X", "CAD") == 2_000.0 and inner.asked == ["CAD"]


def test_a_limit_goes_to_the_venue_in_its_own_currency() -> None:
    w, inner = _wrapped()
    w.place_order(Order(symbol="SOL/USD", symbolId=1, accountId="A", action=OrderAction.BUY,
                        orderType=OrderType.LIMIT, totalQuantity=1.0, limitPrice=125.0))
    assert inner.placed[0].limitPrice == 100.0


def test_a_brokers_own_extras_still_reach_it() -> None:
    w, _ = _wrapped()
    assert w.lot_rules(["A"]) == {}
    with pytest.raises(AttributeError):
        w.no_such_thing  # noqa: B018


def test_no_rate_reads_as_a_stale_quote_so_the_symbol_is_skipped() -> None:
    def boom(_c: str) -> float:
        raise FxUnavailable("no rate")
    w = CurrencyNormalizingBroker(_Book(), SimpleNamespace(rate=boom))  # type: ignore[arg-type]
    with pytest.raises(StaleQuote):
        w.quote("SOL/USD")


# ---------------------------------------------------------------- a session

def _session(tmp: Path, monkeypatch: pytest.MonkeyPatch, rate: float = 1.25, **kw: Any) -> sess.Session:
    monkeypatch.setattr(sess, "_home_rates", lambda *_a, **_k: SimpleNamespace(rate=lambda c: 1.0 if c == "CAD" else rate))
    settings = _settings(tmp, **kw)
    return build_session(settings, _plan(settings), feed=_Feed(), book=StrategyBook(default=EmaCrossover()))  # type: ignore[arg-type]


def test_every_venue_plans_in_the_home_currency() -> None:
    for venue in sess.VENUES:
        assert _plan(_settings(), venue).account_currency == "CAD"
    assert _plan(_settings(account_currency="USD"), "kraken").account_currency == "USD"
    assert _plan(_settings(), "ib", options=sess.SessionOptions(account_currency="USD")).account_currency == "USD"


def test_a_paper_fill_and_its_execution_are_in_the_home_currency(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    book = _session(tmp_path, monkeypatch).exec_broker
    assert isinstance(book, PaperBroker)
    book.place_order(Order(symbol="AAA", symbolId=1, accountId="PAPER-001", action=OrderAction.BUY,
                           orderType=OrderType.MARKET, totalQuantity=10.0))
    assert book.fills[-1].price > 125.0                # the USD ask of 101, at 1.25, plus slippage
    e = book.executions("PAPER-001")[-1]
    assert e.currency == "CAD" and e.currency_source == "book (paper fill)"


def test_the_gate_reads_a_usd_name_in_cad_and_records_the_currency(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from trading_live_claude.audit.ledger import read_stream
    s = _session(tmp_path, monkeypatch)
    px = s.exec_broker.quote("AAA").mid
    assert px == pytest.approx(125.0)
    intent = OrderIntent(symbol="AAA", action=OrderAction.BUY, shares=10, entry=px, stop=px * 0.9, target=None,
                         strategy="ema_crossover", risk_dollars=125.0, account_number="PAPER-001")
    assert s.router.submit(intent, equity=100_000.0, existing_risk=0.0, open_positions=0) is not None
    rows = [r for p in (tmp_path / "ledger").glob("*.jsonl") for r in read_stream(p)]
    check = next(r for r in rows if r["event"] == "RISK_CHECK")
    assert check["payload"]["price_currency"] == "CAD"


def test_a_kraken_minimum_is_stated_in_the_home_currency(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    class _Kraken(_Feed):
        name = venue = "kraken"

        def lot_rules(self, _s: list[str]) -> dict[str, QuantityRule]:
            return {"AAA": QuantityRule(step=0.01, min_qty=0.01, min_notional=5.0)}

    monkeypatch.setattr(sess, "venue_weight_bias", lambda *_a, **_k: None)
    monkeypatch.setattr(sess, "_home_rates", lambda *_a, **_k: SimpleNamespace(rate=lambda _c: 1.25))
    settings = _settings(tmp_path)
    plan = sess.plan_session(settings, venue="kraken", mode="paper", symbols=["AAA"], book=StrategyBook(default=EmaCrossover()))
    s = build_session(settings, plan, feed=_Kraken(), book=StrategyBook(default=EmaCrossover()))  # type: ignore[arg-type]
    assert s.router.quantity_rule_for("AAA").min_notional == 6.25


def test_a_converted_series_has_its_own_cache(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    s = _session(tmp_path, monkeypatch)
    assert s.market.cache.root.name == "numeraire_CAD"


# ---------------------------------------------------------------- what a fill cost

def _row(event: str, payload: dict[str, Any], ts: str = "2026-11-03T15:00:00+00:00") -> dict[str, Any]:
    return {"event": event, "payload": payload, "intent_id": "i1", "stream": "kraken-live", "mode": "live", "ts": ts}


def _fx(rates: dict[str, float]) -> SimpleNamespace:
    return SimpleNamespace(rate=lambda ccy, _on: SimpleNamespace(rate=rates.get(ccy, 1.0 if ccy == "CAD" else None)))


def test_a_money_fill_in_usd_is_measured_against_a_decision_in_cad() -> None:
    rows = [_row("RISK_CHECK", {"entry": 125.0, "price_currency": "CAD"}),
            _row("EXECUTION_REPORTED", {"side": "Buy", "quantity": 1.0, "price": 100.1, "fee": 0.2, "currency": "USD"})]
    q = fill_quality(rows, fx=_fx({"USD": 1.25}))[0]
    assert q["slippage_bps"] == pytest.approx(10.0) and q["fee_bps"] == pytest.approx(20.0, abs=0.1)
    assert q["fills"] == 1 and q["unconverted"] == 0


def test_a_fill_that_cannot_be_converted_is_counted_not_read_as_slippage() -> None:
    rows = [_row("RISK_CHECK", {"entry": 125.0, "price_currency": "CAD"}),
            _row("EXECUTION_REPORTED", {"side": "Buy", "quantity": 1.0, "price": 100.0, "currency": "USD"})]
    q = fill_quality(rows)[0]
    assert (q["fills"], q["unconverted"], q["slippage_bps"]) == (0, 1, 0.0)
    assert fill_quality(rows, fx=_fx({}))[0]["unconverted"] == 1


def test_a_row_with_no_date_cannot_be_converted() -> None:
    rows = [_row("RISK_CHECK", {"entry": 125.0, "price_currency": "CAD"}),
            _row("EXECUTION_REPORTED", {"side": "Buy", "quantity": 1.0, "price": 100.0, "currency": "USD"}, ts="?")]
    assert fill_quality(rows, fx=_fx({"USD": 1.25}))[0]["unconverted"] == 1


def test_a_paper_fill_in_the_same_currency_needs_no_rate() -> None:
    rows = [_row("RISK_CHECK", {"entry": 125.0, "price_currency": "CAD"}),
            _row("EXECUTION_REPORTED", {"side": "Sell", "quantity": 1.0, "price": 124.875, "currency": "CAD"})]
    assert fill_quality(rows)[0]["slippage_bps"] == pytest.approx(10.0)


# ---------------------------------------------------------------- what IB may carry

@pytest.mark.parametrize("symbol", ["GC", "/GC", "/SIL", "7203.T", "VOD.L", "BHP.AX", "0700.HK"])
def test_ib_takes_derivatives_and_overseas_listings(symbol: str) -> None:
    assert ib_refusal(symbol) is None


@pytest.mark.parametrize(("symbol", "why"), [("AAPL", "trade on Questrade"), ("XIC.TO", "trade on Questrade"),
                                             ("BTC/USD", "trades on Kraken")])
def test_ib_refuses_what_belongs_elsewhere(symbol: str, why: str) -> None:
    assert why in (ib_refusal(symbol) or "")


def test_a_missing_universe_file_only_narrows_the_roots(tmp_path: Path) -> None:
    assert ib_refusal("GC", universe_path=tmp_path / "none.json") is None      # still has a roll calendar
