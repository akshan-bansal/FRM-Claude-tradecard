"""Settings: secrets from .env, trading knobs from config/trading.yaml.

Why split?
  .env is gitignored and Claude's settings.json explicitly denies `Read(.env)`.
  Trading knobs (strategy, symbols, risk caps, daily budgets) are not secret;
  putting them in a YAML file Claude *can* read and rewrite lets the
  `trading tune` command (and Claude during a session) pick configuration
  automatically without ever touching credentials.

Precedence:
  1. config/trading.yaml      (Claude-managed; trading knobs)
  2. environment variables    (escape hatch; useful in CI and one-off shells)
  3. .env                     (secrets only)
  4. dataclass defaults
"""
from __future__ import annotations

import os
from functools import lru_cache
from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

ExecutionMode = Literal["paper", "dry-run", "live", "autonomous"]
QuestradeEnv = Literal["practice", "live"]
# practice is checked against the token and refused if the token is live; paper is a rehearsal: the same
# autonomous gates and budget on a simulated broker.
AutonomousAccount = Literal["paper", "practice", "live"]

DEFAULT_TRADING_YAML = Path("config") / "trading.yaml"
TRADING_YAML_ENV_VAR = "TRADING_YAML_PATH"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    # === Secrets (env only) ===
    questrade_refresh_token: str = ""
    token_encryption_key: str = ""
    telegram_bot_token: str = ""
    telegram_chat_id: str = ""
    smtp_host: str = ""
    smtp_user: str = ""
    smtp_pass: str = ""
    alert_email_to: str = ""
    quantconnect_user_id: str = ""
    quantconnect_api_token: str = ""
    # Quantpedia Pro API (research data only: strategy metadata, performance curves, source, papers).
    quantpedia_username: str = ""
    quantpedia_api_key: str = ""
    # Kraken private REST API (crypto sleeve execution). Public market data needs none of this;
    # only the future KrakenBroker's private endpoints (balances, orders) do.
    kraken_api_key: str = ""
    kraken_api_secret: str = ""
    # Interactive Brokers — TWS/IB Gateway connection. Credentials live in TWS/Gateway itself
    # (login is done in the desktop app), so we only need connection knobs here. Never inline
    # IB_USERNAME / IB_PASSWORD_AUTH in a committed file — those go in .env only, and only get
    # read by the MCP-launched interactive-brokers subprocess (see .mcp.json).
    ib_host: str = "127.0.0.1"
    ib_paper_port: int = 7497                # paper TWS = 7497; paper IB Gateway = 4002
    ib_live_port: int = 7496                 # live TWS = 7496; live IB Gateway = 4001
    ib_client_id: int = 42
    ib_account: str = ""                     # empty = pick first managedAccounts result
    ib_use_paper: bool = True                # default the paper connection
    # IBKR Web API — Client Portal Gateway defaults. When the Gateway is running (java program
    # from IBKR Campus), it serves the REST surface on https://localhost:5000/v1/api by default.
    # verify_ssl=False because the Gateway ships a self-signed certificate.
    ib_web_host: str = "localhost"
    ib_web_port: int = 5000
    ib_web_verify_ssl: bool = False
    # WorldMonitor OSINT/news MCP (Pro). Sent as the X-WorldMonitor-Key header to the data tools;
    # only get_sources works without it. Not required for any trading path.
    worldmonitor_api_key: str = ""
    # Anthropic API — used by intel/agents.py (specialist readers + adversarial thesis debate).
    # Only exercised when the agent layer is called; never on the deterministic hot path.
    anthropic_api_key: str = ""
    anthropic_model: str = "claude-sonnet-5"

    # === Trading knobs (yaml-managed) ===
    execution_mode: ExecutionMode = "paper"
    questrade_env: QuestradeEnv = "practice"
    questrade_account_number: str = ""

    risk_pct_per_trade: float = Field(default=0.01, ge=0.0001, le=0.05)
    portfolio_heat_cap: float = Field(default=0.05, ge=0.001, le=1.0)
    # Risk estimate the heat gate budgets on. risk_model: "cvar" (Expected Shortfall tail,
    # default) | "var" | "atr" (ATR-stop loss). heat_aggregation: "corr" (covariance-aware,
    # credits diversification, default) | "sum" (correlation-blind total).
    risk_model: str = Field(default="cvar", pattern="^(atr|var|cvar)$")
    heat_aggregation: str = Field(default="corr", pattern="^(sum|corr)$")
    daily_loss_limit_pct: float = Field(default=0.03, ge=0.001, le=0.2)
    # Kill-switch tightened 2026-09-08 from 0.10 → 0.03. Empirical rationale:
    # 8% halted only after $8k paper loss on $100k; 3% catches runaway concentration
    # early (typical single-position adverse move ~1-2% is within noise, 3% signals
    # thesis breakdown or leverage-cap breach). Override in trading.yaml if wanted.
    max_drawdown_kill_switch: float = Field(default=0.03, ge=0.01, le=0.5)
    max_open_positions: int = Field(default=5, ge=1, le=50)
    min_ticket_usd: float = Field(default=100.0, ge=0.0)

    # Portfolio-level gross leverage cap (2026-09-08). Prevents the sleeve as a whole
    # from exceeding available cash — orthogonal to the per-position vol-target
    # leverage cap in PositionSizer, which is per-name only. Surfaced when a single QT
    # paper session took 100% of equity on one VDY position while cash went to -$10k.
    # 1.0 = no gross leverage (paper default); raise to 1.5-2.0 for margin accounts.
    max_gross_leverage: float = Field(default=1.0, ge=0.1, le=5.0)

    # Per-symbol notional cap as fraction of equity (2026-09-08). Prevents any single
    # name from dominating the book even when the allocator's boost × vol-target ×
    # leverage-cap chain would allow it. 0.50 = no single position may exceed half of
    # equity. Weighted variant (by inverse relative vol) deferred to a follow-up.
    max_position_notional_pct: float = Field(default=0.50, ge=0.01, le=1.0)

    # Dynamic per-name cap (risk/position_cap.py), on every runner. 'vol_scaled': cap =
    # max_position_notional_pct x position_cap_ref_vol / realised vol (higher of 20d/60d),
    # clipped to [floor, ceiling]; unknown vol gets the floor. 'static': the flat cap above.
    # The cap also counts notional already held in the name.
    position_cap_mode: Literal["static", "vol_scaled"] = "vol_scaled"
    position_cap_ref_vol: float = Field(default=0.20, gt=0.0, le=2.0)
    position_cap_floor_pct: float = Field(default=0.05, gt=0.0, le=1.0)
    position_cap_ceiling_pct: float = Field(default=0.75, gt=0.0, le=1.0)

    # Router-level intra-day forced-exit trigger (2026-09-08). Independent of the
    # strategy's own exit bar. When a position's unrealized loss exceeds N × ATR_at_entry,
    # Router.check_forced_exits emits a close intent on the next monitor poll. Runs on
    # 5-min poll cadence — closes the gap where daily-bar strategies can't see mid-day
    # drawdowns. Set to 0.0 to disable; typical range 2-4 (higher = more room for noise).
    force_exit_atr_mult: float = Field(default=3.0, ge=0.0, le=10.0)
    # Whether the monitor acts on that trigger. Off by default: it sends SELL orders with no strategy
    # signal behind them, so turning it on is the operator's decision.
    forced_exits: bool = False

    # Skip a symbol while its venue is outside regular hours (analysis/venue_calendar.py).
    respect_market_hours: bool = True
    # Per-symbol entry channels, for strategies that emit entry_level: event | level | both. A symbol
    # not named takes its strategy family's default (Strategy.level_trigger_default).
    entry_triggers: dict[str, Literal["event", "level", "both"]] = Field(default_factory=dict)

    # Everything else a session runs on, so that what was practised is what runs (monitor/session.py).
    # A runner's flag overrides one of these for that run.
    poll_interval_seconds: int = Field(default=300, ge=5, le=3600)
    strategy_map: str = ""                   # SYM=strategy,SYM=strategy
    intel_overlay: bool = True               # on whenever WORLDMONITOR_API_KEY is set
    intel_refresh_seconds: float = Field(default=900.0, ge=30.0)
    require_card: bool = False
    card_shim_port: int = 8787
    card_shim_host: str = "127.0.0.1"        # loopback; a LAN address reaches the card or desk from a device
    card_attach: bool = False                # wrap for the card but bind no port: a shim is already running
    card_ttl_seconds: float = Field(default=90.0, gt=0.0)
    require_basket: bool | None = None       # None: on whenever require_card is on
    # Paper only: close the book when the session stops. A money session never closes itself.
    flatten_on_exit: bool = True
    warmup_interval_seconds: int = Field(default=0, ge=0)   # poll this fast at first; 0 = off
    warmup_minutes: float = Field(default=60.0, ge=0.0)
    # Exits and adds on a held position (signals/*). Each is off unless turned on.
    profit_lock: bool = False
    profit_lock_exempt: str = "ts_momentum"  # comma-separated strategy names
    candle_exit: bool = False
    candle_exit_exempt: str = ""
    overbought_exit: bool = False
    overbought_exit_exempt: str = ""
    oversold_entry: bool = False
    oversold_entry_only: str = "ts_momentum"
    parallel_sizing: bool = True             # each poll's entries sized jointly
    mute_alerts: str = ""                    # comma-separated symbols: still traded and journaled
    max_round_trip_cost_ratio: float = Field(default=0.0, ge=0.0)  # 0 = off
    params: Literal["default", "wf", "calibrated"] = "default"     # where strategy parameters come from
    sizing_v2: bool = True                   # Kraken's own lot rules; refuses to start without them
    ib_futures_only: bool = True             # desk_policy: IB carries futures; equities trade on Questrade
    spectral_var_cap: float | None = Field(default=None, gt=0.0, le=1.0)  # None = gate off
    spectral_var_level: float = Field(default=0.99, gt=0.5, lt=1.0)
    spectral_var_horizon: int = Field(default=1, ge=1, le=60)
    hedge_symbol: str = ""                   # UUP to run the dollar-hedge overlay
    # Set this to the money you plan to trade. Whole-unit rounding, the minimum ticket and fees all scale
    # with it, so a rehearsal at another size does not translate.
    paper_starting_equity: float = Field(default=100_000.0, gt=0.0)
    # Per venue, over the venue's own fee shape (execution/venue_costs.py): any VenueCostModel field.
    paper_costs: dict[str, dict[str, float]] = Field(default_factory=dict)
    # Off: an IB warning prompt on an order rejects it, and its text reaches the journal.
    ib_auto_confirm_prompts: bool = False

    # Behavior when the per-symbol notional cap OR the portfolio gross-leverage cap
    # would be breached (2026-09-09). 'trim' (default) resizes the intent to fit and
    # accepts; 'reject' returns the pre-2026-09-09 behavior of dropping the whole intent.
    # Trim exists because live paper sessions showed a boosted name (VDY.TO ts_momentum
    # × 2.33× allocator) firing entry every poll and getting rejected every poll for 39
    # polls in a row — while a 50%-cap position would have been perfectly tradeable.
    on_size_cap_breach: Literal["trim", "reject"] = "trim"

    # Stale-quote guard on every feed (brokers/fresh.py). A quote is stale when halted,
    # delayed, priceless, crossed, unchanged for stale_quote_frozen_s, or (when set) its last
    # trade is older than stale_quote_max_trade_age_s. 'raise' skips that symbol's entries/
    # exits and rejects a paper fill; 'warn' only logs. 0 disables a check. 900s = three
    # unchanged polls at the 300s cadence; equities read stale outside market hours by design.
    stale_quote_frozen_s: float = Field(default=900.0, ge=0.0)
    stale_quote_max_trade_age_s: float = Field(default=0.0, ge=0.0)
    on_stale_quote: Literal["warn", "raise"] = "raise"

    # Exchange hopping (venues.py, brokers/fx.py). skip_closed_venues: the monitor
    # neither polls nor trades a symbol outside its venue's regular hours. IB paper converts
    # every price into account_currency using IB spot FX (routed through USD), refreshed every
    # fx_rate_ttl_s; past fx_max_rate_age_s without a rate, that symbol's quotes read as stale.
    skip_closed_venues: bool = True
    fx_rate_ttl_s: float = Field(default=60.0, ge=1.0)
    fx_max_rate_age_s: float = Field(default=900.0, ge=1.0)

    # 24-hour global book (execution/scheduler.py, scripts/paper_global.py). Intents for a closed
    # venue queue until its next tradeable window: open + scheduler_open_buffer_min through close
    # - scheduler_close_buffer_min (keeps orders out of auctions), expiring scheduler_intent_ttl_min
    # after release. Entries need a spread under the per-class ceiling; Tokyo rounds to 100-share
    # lots and Hong Kong names need an explicit board_lots entry. corr_lead_lag: Dimson lags for
    # the heat gate's cross-venue correlation (0 = same-day correlation).
    scheduler_open_buffer_min: float = Field(default=5.0, ge=0.0)
    scheduler_close_buffer_min: float = Field(default=10.0, ge=0.0)
    scheduler_intent_ttl_min: float = Field(default=30.0, ge=1.0)
    max_spread_bps_equity: float = Field(default=50.0, gt=0.0)
    max_spread_bps_crypto: float = Field(default=30.0, gt=0.0)
    board_lots: dict[str, int] = Field(default_factory=dict)
    corr_lead_lag: int = Field(default=1, ge=0, le=5)
    # Futures roll: business days before the earlier of last trade and contract-month start.
    futures_roll_bdays: int = Field(default=5, ge=1, le=30)

    default_strategy: str = "ema_crossover"
    default_symbols: str = "AAPL,MSFT,SHOP.TO,XIC.TO"
    timezone: str = "America/Toronto"
    account_currency: Literal["CAD", "USD"] = "CAD"

    autonomous_enabled: bool = False
    autonomous_account: AutonomousAccount = "practice"
    autonomous_interval_seconds: int = Field(default=1200, ge=60, le=3600)
    autonomous_strategy: str = "ema_crossover"
    autonomous_symbols: str = "AAPL,MSFT,XIC.TO,VFV.TO"
    autonomous_daily_max_trades: int = Field(default=10, ge=1, le=200)
    autonomous_daily_max_notional_usd: float = Field(default=10_000.0, ge=100.0)
    autonomous_auto_start_on_session: bool = False

    log_level: str = "INFO"
    state_dir: Path = Path("state")
    log_dir: Path = Path("logs")
    data_cache_dir: Path = Path("data/cache")

    @field_validator("default_symbols", "autonomous_symbols")
    @classmethod
    def _strip_symbols(cls, v: str) -> str:
        return ",".join(s.strip().upper() for s in v.split(",") if s.strip())

    @property
    def symbols_list(self) -> list[str]:
        return [s for s in self.default_symbols.split(",") if s]

    @property
    def autonomous_symbols_list(self) -> list[str]:
        return [s.strip().upper() for s in self.autonomous_symbols.split(",") if s.strip()]

    @property
    def is_live_capable(self) -> bool:
        return bool(
            self.questrade_refresh_token
            and self.token_encryption_key
            and self.execution_mode == "live"
            and self.questrade_env == "live"
        )

    def ensure_dirs(self) -> None:
        for d in (self.state_dir, self.log_dir, self.data_cache_dir, Path("reports"), Path("config")):
            d.mkdir(parents=True, exist_ok=True)


_TRADING_KNOB_FIELDS: tuple[str, ...] = (
    "execution_mode",
    "questrade_env",
    "questrade_account_number",
    "risk_pct_per_trade",
    "portfolio_heat_cap",
    "risk_model",
    "heat_aggregation",
    "daily_loss_limit_pct",
    "max_drawdown_kill_switch",
    "max_open_positions",
    "min_ticket_usd",
    "max_gross_leverage",
    "max_position_notional_pct",
    "position_cap_mode",
    "position_cap_ref_vol",
    "position_cap_floor_pct",
    "position_cap_ceiling_pct",
    "force_exit_atr_mult",
    "forced_exits",
    "respect_market_hours",
    "entry_triggers",
    "poll_interval_seconds",
    "strategy_map",
    "intel_overlay",
    "intel_refresh_seconds",
    "require_card",
    "card_shim_port",
    "card_shim_host",
    "card_attach",
    "card_ttl_seconds",
    "flatten_on_exit",
    "warmup_interval_seconds",
    "warmup_minutes",
    "profit_lock",
    "profit_lock_exempt",
    "candle_exit",
    "candle_exit_exempt",
    "overbought_exit",
    "overbought_exit_exempt",
    "oversold_entry",
    "oversold_entry_only",
    "parallel_sizing",
    "mute_alerts",
    "max_round_trip_cost_ratio",
    "params",
    "sizing_v2",
    "ib_futures_only",
    "require_basket",
    "spectral_var_cap",
    "spectral_var_level",
    "spectral_var_horizon",
    "hedge_symbol",
    "paper_starting_equity",
    "paper_costs",
    "ib_auto_confirm_prompts",
    "on_size_cap_breach",
    "stale_quote_frozen_s",
    "stale_quote_max_trade_age_s",
    "on_stale_quote",
    "skip_closed_venues",
    "fx_rate_ttl_s",
    "fx_max_rate_age_s",
    "scheduler_open_buffer_min",
    "scheduler_close_buffer_min",
    "scheduler_intent_ttl_min",
    "max_spread_bps_equity",
    "max_spread_bps_crypto",
    "board_lots",
    "corr_lead_lag",
    "futures_roll_bdays",
    "default_strategy",
    "default_symbols",
    "timezone",
    "account_currency",
    "autonomous_enabled",
    "autonomous_account",
    "autonomous_interval_seconds",
    "autonomous_strategy",
    "autonomous_symbols",
    "autonomous_daily_max_trades",
    "autonomous_daily_max_notional_usd",
    "autonomous_auto_start_on_session",
    "log_level",
    "state_dir",
    "log_dir",
    "data_cache_dir",
)


def _resolve_yaml_path(path: Path | None = None) -> Path:
    if path is not None:
        return path
    override = os.environ.get(TRADING_YAML_ENV_VAR, "").strip()
    return Path(override) if override else DEFAULT_TRADING_YAML


def _load_trading_yaml(path: Path | None = None) -> dict[str, Any]:
    resolved = _resolve_yaml_path(path)
    if not resolved.exists():
        return {}
    try:
        data = yaml.safe_load(resolved.read_text(encoding="utf-8")) or {}
    except yaml.YAMLError:
        return {}
    return {k: v for k, v in data.items() if k in _TRADING_KNOB_FIELDS}


def write_trading_yaml(updates: dict[str, Any], path: Path | None = None) -> None:
    """Merge `updates` into the existing trading.yaml and write atomically."""
    resolved = _resolve_yaml_path(path)
    resolved.parent.mkdir(parents=True, exist_ok=True)
    current: dict[str, Any] = {}
    if resolved.exists():
        try:
            current = yaml.safe_load(resolved.read_text(encoding="utf-8")) or {}
        except yaml.YAMLError:
            current = {}
    merged = {**current, **updates}
    tmp = resolved.with_suffix(resolved.suffix + ".tmp")
    tmp.write_text(yaml.safe_dump(merged, sort_keys=False, default_flow_style=False), encoding="utf-8")
    tmp.replace(resolved)


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    yaml_data = _load_trading_yaml()
    s = Settings(**yaml_data)
    s.ensure_dirs()
    return s
