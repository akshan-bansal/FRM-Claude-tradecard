"""Pydantic models at the broker boundary: every adapter maps its own JSON onto these typed objects.

The camelCase field names come from the Questrade API this framework was first written against.
"""
from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from enum import StrEnum
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class OrderSide(StrEnum):
    BUY = "Buy"
    SELL = "Sell"


class OrderAction(StrEnum):
    BUY = "Buy"
    SELL = "Sell"
    BUY_TO_COVER = "BTC"
    SELL_SHORT = "SShort"


class OrderType(StrEnum):
    MARKET = "Market"
    LIMIT = "Limit"
    STOP = "Stop"
    STOP_LIMIT = "StopLimit"


class TimeInForce(StrEnum):
    DAY = "Day"
    GTC = "GoodTillCanceled"
    GTD = "GoodTillDate"


class Account(BaseModel):
    model_config = ConfigDict(extra="ignore")

    type: str
    number: str
    status: str
    isPrimary: bool = False
    isBilling: bool = False
    clientAccountType: str = ""


class Position(BaseModel):
    model_config = ConfigDict(extra="ignore")

    symbol: str
    symbolId: int
    openQuantity: float
    closedQuantity: float = 0.0
    currentMarketValue: float = 0.0
    currentPrice: float = 0.0
    averageEntryPrice: float = 0.0
    closedPnl: float = 0.0
    openPnl: float = 0.0
    totalCost: float = 0.0
    isRealTime: bool = False
    isUnderReorg: bool = False


class Quote(BaseModel):
    model_config = ConfigDict(extra="ignore")

    symbol: str
    symbolId: int
    bidPrice: float | None = None
    bidSize: int | None = None
    askPrice: float | None = None
    askSize: int | None = None
    lastTradePriceTrHrs: float | None = None
    lastTradePrice: float | None = None
    lastTradeSize: int | None = None
    lastTradeTime: datetime | None = None
    volume: int | None = None
    openPrice: float | None = None
    highPrice: float | None = None
    lowPrice: float | None = None
    delay: int | None = None
    isHalted: bool = False

    @property
    def mid(self) -> float | None:
        if self.bidPrice is not None and self.askPrice is not None and self.bidPrice > 0 and self.askPrice > 0:
            return (self.bidPrice + self.askPrice) / 2.0
        return self.lastTradePrice or self.lastTradePriceTrHrs


class Candle(BaseModel):
    model_config = ConfigDict(extra="ignore")

    start: datetime
    end: datetime
    low: float
    high: float
    open: float
    close: float
    volume: int = 0
    VWAP: float | None = None


def plain_decimal(x: float) -> str:
    """Exact decimal text with no exponent or rounding: 12.0 -> "12", 5e-08 -> "0.00000005".

    ``str(float)`` switches to scientific notation below 1e-4, which exchange APIs
    reject and which would mis-render satoshi-scale crypto quantities.
    """
    q = float(x)
    if q.is_integer():
        return str(int(q))
    return format(Decimal(repr(q)), "f")


class Order(BaseModel):
    """Order intent + lifecycle. Used both as request body and as response normalized form."""

    model_config = ConfigDict(extra="ignore")

    id: int | None = None
    symbol: str
    symbolId: int | None = None
    accountId: str | None = None
    action: OrderAction
    orderType: OrderType
    timeInForce: TimeInForce = TimeInForce.DAY
    totalQuantity: float
    limitPrice: float | None = None
    stopPrice: float | None = None
    primaryRoute: str = "AUTO"
    secondaryRoute: str = "AUTO"
    isAllOrNone: bool = False
    isAnonymous: bool = False

    # Risk metadata (never sent to a broker — used by router/journal)
    intended_stop: float | None = Field(default=None, exclude=True)
    intended_target: float | None = Field(default=None, exclude=True)
    risk_dollars: float | None = Field(default=None, exclude=True)
    strategy: str | None = Field(default=None, exclude=True)
    # The broker's own order reference when it is not an integer (a Kraken txid). ``id`` stays the
    # integer handle the journals use; this is what a cancel or an execution lookup needs.
    broker_ref: str | None = Field(default=None, exclude=True)


class Execution(BaseModel):
    """One execution as the broker reports it, pulled back after the order was placed.

    This is the record tax and audit rest on: price, quantity, fees and time come from the broker's
    execution report, never from the intent or from ``place_order``'s acknowledgement.
    ``currency_source`` says where ``currency`` came from (the broker's report, its symbol record,
    or the instrument's listing), since the CAD conversion depends on it.
    """

    model_config = ConfigDict(extra="ignore")

    exec_id: str
    order_id: str | None
    symbol: str
    side: Literal["Buy", "Sell"]
    quantity: float
    price: float
    fee: float = 0.0
    fee_currency: str | None = None
    currency: str | None = None
    currency_source: str = "unknown"
    time: datetime
    venue: str
    account: str | None = None
# Canonical venue tags, documentation for ``Fill.venue`` rather than a validation constraint.
# ``Fill.venue`` was Literal["paper","questrade-practice","questrade-live"] — a pre-multi-venue
# vocabulary that rejected a Kraken or IB fill built with its real venue, and disagreed with the
# journal, which already carried kraken / questrade / ib_web rows. Re-enumerating it as a Literal
# was tried and rejected: PaperBroker falls back to the feed's ``.name`` for any feed that doesn't
# declare ``.venue`` (test doubles, future adapters), so a closed set makes every new feed a schema
# change. The field is therefore an open ``str``; this tuple records the values real adapters emit,
# and ``tests/test_paper_broker_journal.py`` asserts each concrete broker's ``.venue`` is in it so
# a typo or drift still fails a test. The two legacy questrade spellings stay for historical rows.
CANONICAL_VENUES: tuple[str, ...] = (
    "paper", "questrade", "kraken", "ib", "ib_web", "global",
    "questrade-practice", "questrade-live",
)


class Fill(BaseModel):
    model_config = ConfigDict(extra="ignore")

    order_id: int | None
    symbol: str
    side: OrderSide
    quantity: float
    price: float
    commission: float = 0.0
    fill_time: datetime
    venue: str            # see CANONICAL_VENUES above for the values real adapters emit
