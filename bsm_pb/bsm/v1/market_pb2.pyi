from google.protobuf.internal import containers as _containers
from google.protobuf import descriptor as _descriptor
from google.protobuf import message as _message
from typing import ClassVar as _ClassVar, Iterable as _Iterable, Mapping as _Mapping, Optional as _Optional, Union as _Union

DESCRIPTOR: _descriptor.FileDescriptor

class Level(_message.Message):
    __slots__ = ("price", "qty")
    PRICE_FIELD_NUMBER: _ClassVar[int]
    QTY_FIELD_NUMBER: _ClassVar[int]
    price: float
    qty: float
    def __init__(self, price: _Optional[float] = ..., qty: _Optional[float] = ...) -> None: ...

class BookSnapshot(_message.Message):
    __slots__ = ("ts", "market", "bids", "asks", "source")
    TS_FIELD_NUMBER: _ClassVar[int]
    MARKET_FIELD_NUMBER: _ClassVar[int]
    BIDS_FIELD_NUMBER: _ClassVar[int]
    ASKS_FIELD_NUMBER: _ClassVar[int]
    SOURCE_FIELD_NUMBER: _ClassVar[int]
    ts: float
    market: str
    bids: _containers.RepeatedCompositeFieldContainer[Level]
    asks: _containers.RepeatedCompositeFieldContainer[Level]
    source: str
    def __init__(self, ts: _Optional[float] = ..., market: _Optional[str] = ..., bids: _Optional[_Iterable[_Union[Level, _Mapping]]] = ..., asks: _Optional[_Iterable[_Union[Level, _Mapping]]] = ..., source: _Optional[str] = ...) -> None: ...

class Trade(_message.Message):
    __slots__ = ("ts", "market", "price", "qty", "side", "source")
    TS_FIELD_NUMBER: _ClassVar[int]
    MARKET_FIELD_NUMBER: _ClassVar[int]
    PRICE_FIELD_NUMBER: _ClassVar[int]
    QTY_FIELD_NUMBER: _ClassVar[int]
    SIDE_FIELD_NUMBER: _ClassVar[int]
    SOURCE_FIELD_NUMBER: _ClassVar[int]
    ts: float
    market: str
    price: float
    qty: float
    side: int
    source: str
    def __init__(self, ts: _Optional[float] = ..., market: _Optional[str] = ..., price: _Optional[float] = ..., qty: _Optional[float] = ..., side: _Optional[int] = ..., source: _Optional[str] = ...) -> None: ...

class MarketRecord(_message.Message):
    __slots__ = ("book", "trade")
    BOOK_FIELD_NUMBER: _ClassVar[int]
    TRADE_FIELD_NUMBER: _ClassVar[int]
    book: BookSnapshot
    trade: Trade
    def __init__(self, book: _Optional[_Union[BookSnapshot, _Mapping]] = ..., trade: _Optional[_Union[Trade, _Mapping]] = ...) -> None: ...
