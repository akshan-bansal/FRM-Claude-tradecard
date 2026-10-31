from google.protobuf.internal import containers as _containers
from google.protobuf import descriptor as _descriptor
from google.protobuf import message as _message
from typing import ClassVar as _ClassVar, Iterable as _Iterable, Mapping as _Mapping, Optional as _Optional, Union as _Union

DESCRIPTOR: _descriptor.FileDescriptor

class ContractParams(_message.Message):
    __slots__ = ("rate", "dividend", "skew", "vol_floor", "level_map", "days_per_year")
    RATE_FIELD_NUMBER: _ClassVar[int]
    DIVIDEND_FIELD_NUMBER: _ClassVar[int]
    SKEW_FIELD_NUMBER: _ClassVar[int]
    VOL_FLOOR_FIELD_NUMBER: _ClassVar[int]
    LEVEL_MAP_FIELD_NUMBER: _ClassVar[int]
    DAYS_PER_YEAR_FIELD_NUMBER: _ClassVar[int]
    rate: float
    dividend: float
    skew: float
    vol_floor: float
    level_map: _containers.RepeatedScalarFieldContainer[float]
    days_per_year: int
    def __init__(self, rate: _Optional[float] = ..., dividend: _Optional[float] = ..., skew: _Optional[float] = ..., vol_floor: _Optional[float] = ..., level_map: _Optional[_Iterable[float]] = ..., days_per_year: _Optional[int] = ...) -> None: ...

class VarForecast(_message.Message):
    __slots__ = ("id", "stream", "made_at", "asof", "horizon", "moneyness", "model", "strike", "S0", "v0", "tenor", "p0", "position", "contract", "levels", "var", "quantiles", "action", "ratio_gate", "crossing", "radius", "underlying")
    ID_FIELD_NUMBER: _ClassVar[int]
    STREAM_FIELD_NUMBER: _ClassVar[int]
    MADE_AT_FIELD_NUMBER: _ClassVar[int]
    ASOF_FIELD_NUMBER: _ClassVar[int]
    HORIZON_FIELD_NUMBER: _ClassVar[int]
    MONEYNESS_FIELD_NUMBER: _ClassVar[int]
    MODEL_FIELD_NUMBER: _ClassVar[int]
    STRIKE_FIELD_NUMBER: _ClassVar[int]
    S0_FIELD_NUMBER: _ClassVar[int]
    V0_FIELD_NUMBER: _ClassVar[int]
    TENOR_FIELD_NUMBER: _ClassVar[int]
    P0_FIELD_NUMBER: _ClassVar[int]
    POSITION_FIELD_NUMBER: _ClassVar[int]
    CONTRACT_FIELD_NUMBER: _ClassVar[int]
    LEVELS_FIELD_NUMBER: _ClassVar[int]
    VAR_FIELD_NUMBER: _ClassVar[int]
    QUANTILES_FIELD_NUMBER: _ClassVar[int]
    ACTION_FIELD_NUMBER: _ClassVar[int]
    RATIO_GATE_FIELD_NUMBER: _ClassVar[int]
    CROSSING_FIELD_NUMBER: _ClassVar[int]
    RADIUS_FIELD_NUMBER: _ClassVar[int]
    UNDERLYING_FIELD_NUMBER: _ClassVar[int]
    id: str
    stream: str
    made_at: str
    asof: str
    horizon: int
    moneyness: float
    model: str
    strike: float
    S0: float
    v0: float
    tenor: float
    p0: float
    position: float
    contract: ContractParams
    levels: _containers.RepeatedScalarFieldContainer[float]
    var: _containers.RepeatedScalarFieldContainer[float]
    quantiles: _containers.RepeatedScalarFieldContainer[float]
    action: str
    ratio_gate: bool
    crossing: bool
    radius: float
    underlying: str
    def __init__(self, id: _Optional[str] = ..., stream: _Optional[str] = ..., made_at: _Optional[str] = ..., asof: _Optional[str] = ..., horizon: _Optional[int] = ..., moneyness: _Optional[float] = ..., model: _Optional[str] = ..., strike: _Optional[float] = ..., S0: _Optional[float] = ..., v0: _Optional[float] = ..., tenor: _Optional[float] = ..., p0: _Optional[float] = ..., position: _Optional[float] = ..., contract: _Optional[_Union[ContractParams, _Mapping]] = ..., levels: _Optional[_Iterable[float]] = ..., var: _Optional[_Iterable[float]] = ..., quantiles: _Optional[_Iterable[float]] = ..., action: _Optional[str] = ..., ratio_gate: bool = ..., crossing: bool = ..., radius: _Optional[float] = ..., underlying: _Optional[str] = ...) -> None: ...

class VarMatured(_message.Message):
    __slots__ = ("id", "matured_date", "matured_at", "asof", "horizon", "moneyness", "model", "realized_loss", "breach", "pinball", "pit", "underlying")
    ID_FIELD_NUMBER: _ClassVar[int]
    MATURED_DATE_FIELD_NUMBER: _ClassVar[int]
    MATURED_AT_FIELD_NUMBER: _ClassVar[int]
    ASOF_FIELD_NUMBER: _ClassVar[int]
    HORIZON_FIELD_NUMBER: _ClassVar[int]
    MONEYNESS_FIELD_NUMBER: _ClassVar[int]
    MODEL_FIELD_NUMBER: _ClassVar[int]
    REALIZED_LOSS_FIELD_NUMBER: _ClassVar[int]
    BREACH_FIELD_NUMBER: _ClassVar[int]
    PINBALL_FIELD_NUMBER: _ClassVar[int]
    PIT_FIELD_NUMBER: _ClassVar[int]
    UNDERLYING_FIELD_NUMBER: _ClassVar[int]
    id: str
    matured_date: str
    matured_at: str
    asof: str
    horizon: int
    moneyness: float
    model: str
    realized_loss: float
    breach: _containers.RepeatedScalarFieldContainer[bool]
    pinball: _containers.RepeatedScalarFieldContainer[float]
    pit: float
    underlying: str
    def __init__(self, id: _Optional[str] = ..., matured_date: _Optional[str] = ..., matured_at: _Optional[str] = ..., asof: _Optional[str] = ..., horizon: _Optional[int] = ..., moneyness: _Optional[float] = ..., model: _Optional[str] = ..., realized_loss: _Optional[float] = ..., breach: _Optional[_Iterable[bool]] = ..., pinball: _Optional[_Iterable[float]] = ..., pit: _Optional[float] = ..., underlying: _Optional[str] = ...) -> None: ...

class KalshiForecast(_message.Message):
    __slots__ = ("id", "stream", "made_at", "ts", "series", "event", "ticker", "strike_type", "floor", "cap", "close_time", "expiry", "bid", "ask", "bid_size", "ask_size", "state_date", "S0", "v0", "T", "p_model", "underlying")
    ID_FIELD_NUMBER: _ClassVar[int]
    STREAM_FIELD_NUMBER: _ClassVar[int]
    MADE_AT_FIELD_NUMBER: _ClassVar[int]
    TS_FIELD_NUMBER: _ClassVar[int]
    SERIES_FIELD_NUMBER: _ClassVar[int]
    EVENT_FIELD_NUMBER: _ClassVar[int]
    TICKER_FIELD_NUMBER: _ClassVar[int]
    STRIKE_TYPE_FIELD_NUMBER: _ClassVar[int]
    FLOOR_FIELD_NUMBER: _ClassVar[int]
    CAP_FIELD_NUMBER: _ClassVar[int]
    CLOSE_TIME_FIELD_NUMBER: _ClassVar[int]
    EXPIRY_FIELD_NUMBER: _ClassVar[int]
    BID_FIELD_NUMBER: _ClassVar[int]
    ASK_FIELD_NUMBER: _ClassVar[int]
    BID_SIZE_FIELD_NUMBER: _ClassVar[int]
    ASK_SIZE_FIELD_NUMBER: _ClassVar[int]
    STATE_DATE_FIELD_NUMBER: _ClassVar[int]
    S0_FIELD_NUMBER: _ClassVar[int]
    V0_FIELD_NUMBER: _ClassVar[int]
    T_FIELD_NUMBER: _ClassVar[int]
    P_MODEL_FIELD_NUMBER: _ClassVar[int]
    UNDERLYING_FIELD_NUMBER: _ClassVar[int]
    id: str
    stream: str
    made_at: str
    ts: float
    series: str
    event: str
    ticker: str
    strike_type: str
    floor: float
    cap: float
    close_time: str
    expiry: str
    bid: float
    ask: float
    bid_size: float
    ask_size: float
    state_date: str
    S0: float
    v0: float
    T: float
    p_model: float
    underlying: str
    def __init__(self, id: _Optional[str] = ..., stream: _Optional[str] = ..., made_at: _Optional[str] = ..., ts: _Optional[float] = ..., series: _Optional[str] = ..., event: _Optional[str] = ..., ticker: _Optional[str] = ..., strike_type: _Optional[str] = ..., floor: _Optional[float] = ..., cap: _Optional[float] = ..., close_time: _Optional[str] = ..., expiry: _Optional[str] = ..., bid: _Optional[float] = ..., ask: _Optional[float] = ..., bid_size: _Optional[float] = ..., ask_size: _Optional[float] = ..., state_date: _Optional[str] = ..., S0: _Optional[float] = ..., v0: _Optional[float] = ..., T: _Optional[float] = ..., p_model: _Optional[float] = ..., underlying: _Optional[str] = ...) -> None: ...

class KalshiMatured(_message.Message):
    __slots__ = ("id", "matured_date", "matured_at", "ticker", "outcome", "settlement")
    ID_FIELD_NUMBER: _ClassVar[int]
    MATURED_DATE_FIELD_NUMBER: _ClassVar[int]
    MATURED_AT_FIELD_NUMBER: _ClassVar[int]
    TICKER_FIELD_NUMBER: _ClassVar[int]
    OUTCOME_FIELD_NUMBER: _ClassVar[int]
    SETTLEMENT_FIELD_NUMBER: _ClassVar[int]
    id: str
    matured_date: str
    matured_at: str
    ticker: str
    outcome: int
    settlement: str
    def __init__(self, id: _Optional[str] = ..., matured_date: _Optional[str] = ..., matured_at: _Optional[str] = ..., ticker: _Optional[str] = ..., outcome: _Optional[int] = ..., settlement: _Optional[str] = ...) -> None: ...

class KalshiSettled(_message.Message):
    __slots__ = ("series", "event", "ticker", "strike_type", "floor", "cap", "close_time", "expiry", "quote_ts", "bid", "ask", "result", "settlement", "state_date", "S0", "v0")
    SERIES_FIELD_NUMBER: _ClassVar[int]
    EVENT_FIELD_NUMBER: _ClassVar[int]
    TICKER_FIELD_NUMBER: _ClassVar[int]
    STRIKE_TYPE_FIELD_NUMBER: _ClassVar[int]
    FLOOR_FIELD_NUMBER: _ClassVar[int]
    CAP_FIELD_NUMBER: _ClassVar[int]
    CLOSE_TIME_FIELD_NUMBER: _ClassVar[int]
    EXPIRY_FIELD_NUMBER: _ClassVar[int]
    QUOTE_TS_FIELD_NUMBER: _ClassVar[int]
    BID_FIELD_NUMBER: _ClassVar[int]
    ASK_FIELD_NUMBER: _ClassVar[int]
    RESULT_FIELD_NUMBER: _ClassVar[int]
    SETTLEMENT_FIELD_NUMBER: _ClassVar[int]
    STATE_DATE_FIELD_NUMBER: _ClassVar[int]
    S0_FIELD_NUMBER: _ClassVar[int]
    V0_FIELD_NUMBER: _ClassVar[int]
    series: str
    event: str
    ticker: str
    strike_type: str
    floor: float
    cap: float
    close_time: str
    expiry: str
    quote_ts: float
    bid: float
    ask: float
    result: int
    settlement: str
    state_date: str
    S0: float
    v0: float
    def __init__(self, series: _Optional[str] = ..., event: _Optional[str] = ..., ticker: _Optional[str] = ..., strike_type: _Optional[str] = ..., floor: _Optional[float] = ..., cap: _Optional[float] = ..., close_time: _Optional[str] = ..., expiry: _Optional[str] = ..., quote_ts: _Optional[float] = ..., bid: _Optional[float] = ..., ask: _Optional[float] = ..., result: _Optional[int] = ..., settlement: _Optional[str] = ..., state_date: _Optional[str] = ..., S0: _Optional[float] = ..., v0: _Optional[float] = ...) -> None: ...

class CrossBook(_message.Message):
    __slots__ = ("S0", "y0", "units")
    S0_FIELD_NUMBER: _ClassVar[int]
    Y0_FIELD_NUMBER: _ClassVar[int]
    UNITS_FIELD_NUMBER: _ClassVar[int]
    S0: float
    y0: float
    units: _containers.RepeatedScalarFieldContainer[float]
    def __init__(self, S0: _Optional[float] = ..., y0: _Optional[float] = ..., units: _Optional[_Iterable[float]] = ...) -> None: ...

class CrossForecast(_message.Message):
    __slots__ = ("id", "stream", "made_at", "asof", "horizon", "model", "state", "book", "levels", "var", "quantiles")
    ID_FIELD_NUMBER: _ClassVar[int]
    STREAM_FIELD_NUMBER: _ClassVar[int]
    MADE_AT_FIELD_NUMBER: _ClassVar[int]
    ASOF_FIELD_NUMBER: _ClassVar[int]
    HORIZON_FIELD_NUMBER: _ClassVar[int]
    MODEL_FIELD_NUMBER: _ClassVar[int]
    STATE_FIELD_NUMBER: _ClassVar[int]
    BOOK_FIELD_NUMBER: _ClassVar[int]
    LEVELS_FIELD_NUMBER: _ClassVar[int]
    VAR_FIELD_NUMBER: _ClassVar[int]
    QUANTILES_FIELD_NUMBER: _ClassVar[int]
    id: str
    stream: str
    made_at: str
    asof: str
    horizon: int
    model: str
    state: _containers.RepeatedScalarFieldContainer[float]
    book: CrossBook
    levels: _containers.RepeatedScalarFieldContainer[float]
    var: _containers.RepeatedScalarFieldContainer[float]
    quantiles: _containers.RepeatedScalarFieldContainer[float]
    def __init__(self, id: _Optional[str] = ..., stream: _Optional[str] = ..., made_at: _Optional[str] = ..., asof: _Optional[str] = ..., horizon: _Optional[int] = ..., model: _Optional[str] = ..., state: _Optional[_Iterable[float]] = ..., book: _Optional[_Union[CrossBook, _Mapping]] = ..., levels: _Optional[_Iterable[float]] = ..., var: _Optional[_Iterable[float]] = ..., quantiles: _Optional[_Iterable[float]] = ...) -> None: ...

class CrossMatured(_message.Message):
    __slots__ = ("id", "matured_date", "matured_at", "asof", "horizon", "model", "realized_loss", "breach", "pit")
    ID_FIELD_NUMBER: _ClassVar[int]
    MATURED_DATE_FIELD_NUMBER: _ClassVar[int]
    MATURED_AT_FIELD_NUMBER: _ClassVar[int]
    ASOF_FIELD_NUMBER: _ClassVar[int]
    HORIZON_FIELD_NUMBER: _ClassVar[int]
    MODEL_FIELD_NUMBER: _ClassVar[int]
    REALIZED_LOSS_FIELD_NUMBER: _ClassVar[int]
    BREACH_FIELD_NUMBER: _ClassVar[int]
    PIT_FIELD_NUMBER: _ClassVar[int]
    id: str
    matured_date: str
    matured_at: str
    asof: str
    horizon: int
    model: str
    realized_loss: float
    breach: _containers.RepeatedScalarFieldContainer[bool]
    pit: float
    def __init__(self, id: _Optional[str] = ..., matured_date: _Optional[str] = ..., matured_at: _Optional[str] = ..., asof: _Optional[str] = ..., horizon: _Optional[int] = ..., model: _Optional[str] = ..., realized_loss: _Optional[float] = ..., breach: _Optional[_Iterable[bool]] = ..., pit: _Optional[float] = ...) -> None: ...

class FetchLog(_message.Message):
    __slots__ = ("fetched_at", "series", "rows", "first", "last", "sha256", "revised", "dropped")
    FETCHED_AT_FIELD_NUMBER: _ClassVar[int]
    SERIES_FIELD_NUMBER: _ClassVar[int]
    ROWS_FIELD_NUMBER: _ClassVar[int]
    FIRST_FIELD_NUMBER: _ClassVar[int]
    LAST_FIELD_NUMBER: _ClassVar[int]
    SHA256_FIELD_NUMBER: _ClassVar[int]
    REVISED_FIELD_NUMBER: _ClassVar[int]
    DROPPED_FIELD_NUMBER: _ClassVar[int]
    fetched_at: str
    series: str
    rows: int
    first: str
    last: str
    sha256: str
    revised: int
    dropped: int
    def __init__(self, fetched_at: _Optional[str] = ..., series: _Optional[str] = ..., rows: _Optional[int] = ..., first: _Optional[str] = ..., last: _Optional[str] = ..., sha256: _Optional[str] = ..., revised: _Optional[int] = ..., dropped: _Optional[int] = ...) -> None: ...

class MaturationConfig(_message.Message):
    __slots__ = ("var", "kalshi", "cross", "frozen", "underlyings", "enabled")
    class Var(_message.Message):
        __slots__ = ("horizons", "tenor_days", "moneyness", "position", "levels", "n_sims", "tail_frac", "tail_k", "xi_max", "gammas", "rho_floor", "skew", "rate", "dividend", "vol_floor", "level_map", "models", "days_per_year")
        HORIZONS_FIELD_NUMBER: _ClassVar[int]
        TENOR_DAYS_FIELD_NUMBER: _ClassVar[int]
        MONEYNESS_FIELD_NUMBER: _ClassVar[int]
        POSITION_FIELD_NUMBER: _ClassVar[int]
        LEVELS_FIELD_NUMBER: _ClassVar[int]
        N_SIMS_FIELD_NUMBER: _ClassVar[int]
        TAIL_FRAC_FIELD_NUMBER: _ClassVar[int]
        TAIL_K_FIELD_NUMBER: _ClassVar[int]
        XI_MAX_FIELD_NUMBER: _ClassVar[int]
        GAMMAS_FIELD_NUMBER: _ClassVar[int]
        RHO_FLOOR_FIELD_NUMBER: _ClassVar[int]
        SKEW_FIELD_NUMBER: _ClassVar[int]
        RATE_FIELD_NUMBER: _ClassVar[int]
        DIVIDEND_FIELD_NUMBER: _ClassVar[int]
        VOL_FLOOR_FIELD_NUMBER: _ClassVar[int]
        LEVEL_MAP_FIELD_NUMBER: _ClassVar[int]
        MODELS_FIELD_NUMBER: _ClassVar[int]
        DAYS_PER_YEAR_FIELD_NUMBER: _ClassVar[int]
        horizons: _containers.RepeatedScalarFieldContainer[int]
        tenor_days: int
        moneyness: _containers.RepeatedScalarFieldContainer[float]
        position: float
        levels: _containers.RepeatedScalarFieldContainer[float]
        n_sims: int
        tail_frac: float
        tail_k: float
        xi_max: float
        gammas: _containers.RepeatedScalarFieldContainer[float]
        rho_floor: float
        skew: float
        rate: float
        dividend: float
        vol_floor: float
        level_map: _containers.RepeatedScalarFieldContainer[float]
        models: _containers.RepeatedScalarFieldContainer[str]
        days_per_year: int
        def __init__(self, horizons: _Optional[_Iterable[int]] = ..., tenor_days: _Optional[int] = ..., moneyness: _Optional[_Iterable[float]] = ..., position: _Optional[float] = ..., levels: _Optional[_Iterable[float]] = ..., n_sims: _Optional[int] = ..., tail_frac: _Optional[float] = ..., tail_k: _Optional[float] = ..., xi_max: _Optional[float] = ..., gammas: _Optional[_Iterable[float]] = ..., rho_floor: _Optional[float] = ..., skew: _Optional[float] = ..., rate: _Optional[float] = ..., dividend: _Optional[float] = ..., vol_floor: _Optional[float] = ..., level_map: _Optional[_Iterable[float]] = ..., models: _Optional[_Iterable[str]] = ..., days_per_year: _Optional[int] = ...) -> None: ...
    class Kalshi(_message.Message):
        __slots__ = ("series", "event_suffix", "skew", "rate", "dividend", "snapshot_hour_ny", "events", "max_state_lag", "days_per_year")
        SERIES_FIELD_NUMBER: _ClassVar[int]
        EVENT_SUFFIX_FIELD_NUMBER: _ClassVar[int]
        SKEW_FIELD_NUMBER: _ClassVar[int]
        RATE_FIELD_NUMBER: _ClassVar[int]
        DIVIDEND_FIELD_NUMBER: _ClassVar[int]
        SNAPSHOT_HOUR_NY_FIELD_NUMBER: _ClassVar[int]
        EVENTS_FIELD_NUMBER: _ClassVar[int]
        MAX_STATE_LAG_FIELD_NUMBER: _ClassVar[int]
        DAYS_PER_YEAR_FIELD_NUMBER: _ClassVar[int]
        series: _containers.RepeatedScalarFieldContainer[str]
        event_suffix: str
        skew: float
        rate: float
        dividend: float
        snapshot_hour_ny: int
        events: str
        max_state_lag: int
        days_per_year: int
        def __init__(self, series: _Optional[_Iterable[str]] = ..., event_suffix: _Optional[str] = ..., skew: _Optional[float] = ..., rate: _Optional[float] = ..., dividend: _Optional[float] = ..., snapshot_hour_ny: _Optional[int] = ..., events: _Optional[str] = ..., max_state_lag: _Optional[int] = ..., days_per_year: _Optional[int] = ...) -> None: ...
    class Cross(_message.Message):
        __slots__ = ("horizons", "window", "tail_k", "n_sims")
        HORIZONS_FIELD_NUMBER: _ClassVar[int]
        WINDOW_FIELD_NUMBER: _ClassVar[int]
        TAIL_K_FIELD_NUMBER: _ClassVar[int]
        N_SIMS_FIELD_NUMBER: _ClassVar[int]
        horizons: _containers.RepeatedScalarFieldContainer[int]
        window: int
        tail_k: float
        n_sims: int
        def __init__(self, horizons: _Optional[_Iterable[int]] = ..., window: _Optional[int] = ..., tail_k: _Optional[float] = ..., n_sims: _Optional[int] = ...) -> None: ...
    class Frozen(_message.Message):
        __slots__ = ("at", "history", "source", "code_sha256_16", "amendments")
        AT_FIELD_NUMBER: _ClassVar[int]
        HISTORY_FIELD_NUMBER: _ClassVar[int]
        SOURCE_FIELD_NUMBER: _ClassVar[int]
        CODE_SHA256_16_FIELD_NUMBER: _ClassVar[int]
        AMENDMENTS_FIELD_NUMBER: _ClassVar[int]
        at: str
        history: _containers.RepeatedScalarFieldContainer[str]
        source: str
        code_sha256_16: str
        amendments: _containers.RepeatedScalarFieldContainer[str]
        def __init__(self, at: _Optional[str] = ..., history: _Optional[_Iterable[str]] = ..., source: _Optional[str] = ..., code_sha256_16: _Optional[str] = ..., amendments: _Optional[_Iterable[str]] = ...) -> None: ...
    class Underlying(_message.Message):
        __slots__ = ("var", "kalshi", "frozen")
        VAR_FIELD_NUMBER: _ClassVar[int]
        KALSHI_FIELD_NUMBER: _ClassVar[int]
        FROZEN_FIELD_NUMBER: _ClassVar[int]
        var: MaturationConfig.Var
        kalshi: MaturationConfig.Kalshi
        frozen: MaturationConfig.Frozen
        def __init__(self, var: _Optional[_Union[MaturationConfig.Var, _Mapping]] = ..., kalshi: _Optional[_Union[MaturationConfig.Kalshi, _Mapping]] = ..., frozen: _Optional[_Union[MaturationConfig.Frozen, _Mapping]] = ...) -> None: ...
    class UnderlyingsEntry(_message.Message):
        __slots__ = ("key", "value")
        KEY_FIELD_NUMBER: _ClassVar[int]
        VALUE_FIELD_NUMBER: _ClassVar[int]
        key: str
        value: MaturationConfig.Underlying
        def __init__(self, key: _Optional[str] = ..., value: _Optional[_Union[MaturationConfig.Underlying, _Mapping]] = ...) -> None: ...
    VAR_FIELD_NUMBER: _ClassVar[int]
    KALSHI_FIELD_NUMBER: _ClassVar[int]
    CROSS_FIELD_NUMBER: _ClassVar[int]
    FROZEN_FIELD_NUMBER: _ClassVar[int]
    UNDERLYINGS_FIELD_NUMBER: _ClassVar[int]
    ENABLED_FIELD_NUMBER: _ClassVar[int]
    var: MaturationConfig.Var
    kalshi: MaturationConfig.Kalshi
    cross: MaturationConfig.Cross
    frozen: MaturationConfig.Frozen
    underlyings: _containers.MessageMap[str, MaturationConfig.Underlying]
    enabled: _containers.RepeatedScalarFieldContainer[str]
    def __init__(self, var: _Optional[_Union[MaturationConfig.Var, _Mapping]] = ..., kalshi: _Optional[_Union[MaturationConfig.Kalshi, _Mapping]] = ..., cross: _Optional[_Union[MaturationConfig.Cross, _Mapping]] = ..., frozen: _Optional[_Union[MaturationConfig.Frozen, _Mapping]] = ..., underlyings: _Optional[_Mapping[str, MaturationConfig.Underlying]] = ..., enabled: _Optional[_Iterable[str]] = ...) -> None: ...

class JournalEntry(_message.Message):
    __slots__ = ("var_forecast", "var_matured", "kalshi_forecast", "kalshi_matured", "cross_forecast", "cross_matured", "fetch_log", "kalshi_settled")
    VAR_FORECAST_FIELD_NUMBER: _ClassVar[int]
    VAR_MATURED_FIELD_NUMBER: _ClassVar[int]
    KALSHI_FORECAST_FIELD_NUMBER: _ClassVar[int]
    KALSHI_MATURED_FIELD_NUMBER: _ClassVar[int]
    CROSS_FORECAST_FIELD_NUMBER: _ClassVar[int]
    CROSS_MATURED_FIELD_NUMBER: _ClassVar[int]
    FETCH_LOG_FIELD_NUMBER: _ClassVar[int]
    KALSHI_SETTLED_FIELD_NUMBER: _ClassVar[int]
    var_forecast: VarForecast
    var_matured: VarMatured
    kalshi_forecast: KalshiForecast
    kalshi_matured: KalshiMatured
    cross_forecast: CrossForecast
    cross_matured: CrossMatured
    fetch_log: FetchLog
    kalshi_settled: KalshiSettled
    def __init__(self, var_forecast: _Optional[_Union[VarForecast, _Mapping]] = ..., var_matured: _Optional[_Union[VarMatured, _Mapping]] = ..., kalshi_forecast: _Optional[_Union[KalshiForecast, _Mapping]] = ..., kalshi_matured: _Optional[_Union[KalshiMatured, _Mapping]] = ..., cross_forecast: _Optional[_Union[CrossForecast, _Mapping]] = ..., cross_matured: _Optional[_Union[CrossMatured, _Mapping]] = ..., fetch_log: _Optional[_Union[FetchLog, _Mapping]] = ..., kalshi_settled: _Optional[_Union[KalshiSettled, _Mapping]] = ...) -> None: ...
