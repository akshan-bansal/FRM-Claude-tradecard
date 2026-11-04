"""What each engine layer has, and what is missing, without ever showing a secret.

Secrets are reported as set or missing, never by value. It reads settings only and calls no broker, so it
says what is configured, not whether a key works.
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any


@dataclass(frozen=True)
class Row:
    layer: str
    item: str
    state: str
    needed_for: str


def _secret(settings: Any, attr: str) -> str:
    return "set" if getattr(settings, attr, "") else "MISSING"


def _edges(state_dir: Path) -> str:
    try:
        return "present" if "elevated_in" in (state_dir / "intel_graph.jsonl").read_text(encoding="utf-8") else "none yet"
    except OSError:
        return "none yet"


def wiring_rows(settings: Any) -> list[Row]:
    """One row per thing a layer depends on."""
    state_dir = Path(settings.state_dir)
    rows = [
        Row("data", "QUESTRADE_REFRESH_TOKEN", _secret(settings, "questrade_refresh_token"),
            "Questrade paper and money (first login only, once tokens are stored)"),
        Row("data", "TOKEN_ENCRYPTION_KEY", _secret(settings, "token_encryption_key"), "Questrade paper and money"),
        Row("data", "stored Questrade tokens", "present" if (state_dir / "tokens.json.enc").exists() else "none yet",
            "Questrade after first login"),
        Row("data", "Kraken public data", "no key needed", "Kraken paper"),
        Row("data", "IB gateway", f"{settings.ib_web_host}:{settings.ib_web_port} (login in the browser)", "IB paper and money"),
        Row("execution", "KRAKEN_API_KEY", _secret(settings, "kraken_api_key"), "Kraken money"),
        Row("execution", "KRAKEN_API_SECRET", _secret(settings, "kraken_api_secret"), "Kraken money"),
        Row("execution", "EXECUTION_MODE", settings.execution_mode, "must be live for `trading live`"),
        Row("execution", "QUESTRADE_ENV", settings.questrade_env, "must match the token's environment for money"),
        Row("execution", "AUTONOMOUS_ENABLED", str(settings.autonomous_enabled), "the autonomous daemon"),
        Row("execution", "AUTONOMOUS_ACCOUNT", settings.autonomous_account, "paper rehearses; live is real money"),
        Row("execution", "IB_USE_PAPER", str(settings.ib_use_paper), "IB socket account"),
        Row("execution", "ib_auto_confirm_prompts", str(settings.ib_auto_confirm_prompts),
            "off: an IB warning prompt rejects the order"),
        Row("intel", "WORLDMONITOR_API_KEY", _secret(settings, "worldmonitor_api_key"), "overlay and thesis trims, every runner"),
        Row("intel", "elevated_in edges", _edges(state_dir), "persistence gate cannot fire without them"),
        Row("risk", "limits", f"heat {settings.portfolio_heat_cap:.1%}, drawdown {settings.max_drawdown_kill_switch:.1%}, "
            f"daily loss {settings.daily_loss_limit_pct:.1%}, leverage {settings.max_gross_leverage:.2f}x", "every runner"),
        Row("risk", "forced_exits", str(settings.forced_exits), "off unless set"),
        Row("risk", "require_card / require_basket", f"{settings.require_card} / {settings.require_basket}", "every runner but autonomous"),
        Row("risk", "spectral_var_cap", str(settings.spectral_var_cap), "off unless set"),
        Row("risk", "position_cap_mode", settings.position_cap_mode, "every runner"),
        Row("risk", "max_round_trip_cost_ratio", str(settings.max_round_trip_cost_ratio), "0 = off"),
        Row("data", "on_stale_quote", settings.on_stale_quote, "every runner: raise skips a frozen symbol"),
        Row("sizing", "parallel_sizing / sizing_v2", f"{settings.parallel_sizing} / {settings.sizing_v2}",
            "sizing_v2 is Kraken's lot rules"),
        Row("exits", "profit_lock / candle_exit / overbought_exit / oversold_entry",
            f"{settings.profit_lock} / {settings.candle_exit} / {settings.overbought_exit} / {settings.oversold_entry}",
            "off unless set; a flag turns one on for a run"),
        Row("rehearsal", "paper_starting_equity", f"{settings.paper_starting_equity:,.0f}",
            "set this to the money you plan to trade, or sizes will not translate"),
        Row("alerts", "TELEGRAM_BOT_TOKEN", _secret(settings, "telegram_bot_token"), "phone alerts"),
        Row("alerts", "TELEGRAM_CHAT_ID", _secret(settings, "telegram_chat_id"), "phone alerts"),
        Row("alerts", "SMTP_HOST", _secret(settings, "smtp_host"), "email alerts (optional)"),
        Row("audit", "ledger", "present" if (state_dir / "ledger").is_dir() else "none yet", "written by every session"),
    ]
    return rows


def missing_for_money(settings: Any) -> dict[str, list[str]]:
    """Per venue, the keys a money session still lacks."""
    need = {"questrade": ["questrade_refresh_token", "token_encryption_key"],
            "kraken": ["kraken_api_key", "kraken_api_secret"], "ib": []}
    return {venue: [k.upper() for k in keys if not getattr(settings, k, "")] for venue, keys in need.items()}
