"""Content-addressed versions for strategies and the risk gate (``docs/compliance/AUDIT_SCOPE.md`` 5.1).

Ported from the FRM-Claude ledger branch, where it was ``AUDIT_LEDGER_SCOPE.md`` phase 4.

A ledger row says *what* happened; these say *which code and which settings* made it happen. Without
them, a row from last month is uninterpretable: the strategy may have been retuned and a gate
threshold moved, and nothing in the record would show it.

Both versions are content hashes, not hand-maintained numbers, because a hand-maintained version is
one someone forgets to bump. A strategy's version changes when its source or its parameters change;
the gate's version changes when the gate code or any threshold changes.

**Known limit, stated rather than hidden:** ``strategy_version`` hashes the strategy module's own
source plus the instance's parameters. It does NOT follow imports, so an edit to a shared helper
(``signals/indicators.py``, say) changes behaviour without changing the version. Closing that would
mean hashing the transitive import graph — deferred deliberately; if it matters later, hash the
installed package tree instead of guessing which imports are relevant.
"""
from __future__ import annotations

import inspect
import os
import platform
import subprocess
from dataclasses import asdict, is_dataclass
from hashlib import sha256
from pathlib import Path
from typing import Any

from ..logging_setup import get_logger
from .ledger import canonical_json

log = get_logger(__name__)

# 16 hex chars = 64 bits. Enough that an accidental collision between two versions of one
# strategy is not a practical concern, short enough to read in a log line.
_DIGEST_CHARS = 16

UNKNOWN = "unknown"


def content_hash(payload: Any) -> str:
    """Stable short digest of any JSON-serialisable content."""
    return sha256(canonical_json(payload)).hexdigest()[:_DIGEST_CHARS]


def module_source(obj: object) -> str:
    """Source of the module defining ``obj``'s type (or ``obj`` itself for a class/function).

    Returns ``""`` when the source is unavailable — a C extension, an interactive session, or a
    frozen build. Callers must treat that as "unversioned", never as "unchanged".
    """
    target = obj if inspect.isclass(obj) or inspect.isfunction(obj) else type(obj)
    try:
        module = inspect.getmodule(target)
        return inspect.getsource(module) if module is not None else ""
    except (OSError, TypeError):
        return ""


def strategy_params(strategy: object) -> dict[str, Any]:
    """The parameters that define this instance's behaviour.

    ``Strategy.__init__`` stores its kwargs on ``self.params``; the per-trade exit knobs are class
    attributes an instance can override, and they change behaviour, so they belong in the version.
    """
    params = dict(getattr(strategy, "params", {}) or {})
    for knob in ("stop_atr_mult", "trail_atr_mult", "time_stop_bars", "supports_level_trigger",
                 "level_trigger_default"):
        if hasattr(strategy, knob):
            params[knob] = getattr(strategy, knob)
    return params


def strategy_version(strategy: object) -> str:
    """Version of one strategy *instance*: its module source plus its parameters.

    Two instances of the same class with different parameters version differently — that is the
    point, since ``bollinger(n_std=2.0)`` and ``bollinger(n_std=2.5)`` are different strategies as
    far as a later reader is concerned.
    """
    source = module_source(strategy)
    if not source:
        log.warning("versioning.source_unavailable", strategy=getattr(strategy, "name", strategy))
        return UNKNOWN
    return content_hash({
        "class": type(strategy).__qualname__,
        "source": source,
        "params": strategy_params(strategy),
    })


def gate_thresholds(router: object) -> dict[str, Any]:
    """Every number the risk gate compares against, flattened for hashing.

    Read off the live router rather than from settings, so the version reflects what this process
    actually enforces — not what a config file said at some point.
    """
    heat = getattr(router, "heat", None)
    ks = getattr(router, "kill_switch", None)
    budget = getattr(router, "daily_budget", None)
    cost_model = getattr(router, "cost_model", None)
    out: dict[str, Any] = {
        "mode": getattr(router, "mode", None),
        "brokers": sorted(getattr(router, "brokers", None) or ()),
        "default_broker": getattr(router, "default_broker", None),
        "max_open_positions": getattr(router, "max_open_positions", None),
        "min_ticket_usd": getattr(router, "min_ticket_usd", None),
        "max_round_trip_cost_ratio": getattr(router, "max_round_trip_cost_ratio", None),
        "cost_model": asdict(cost_model) if is_dataclass(cost_model) else None,
        "max_gross_leverage": getattr(router, "max_gross_leverage", None),
        "max_position_notional_pct": getattr(router, "max_position_notional_pct", None),
        "force_exit_atr_mult": getattr(router, "force_exit_atr_mult", None),
        "on_size_cap_breach": getattr(router, "on_size_cap_breach", None),
        "heat_cap_pct": getattr(heat, "cap_pct", None),
        "max_drawdown_pct": getattr(ks, "max_drawdown_pct", None),
        "daily_loss_limit_pct": getattr(ks, "daily_loss_limit_pct", None),
    }
    if budget is not None:
        out["daily_max_trades"] = getattr(budget, "max_trades", None)
        out["daily_max_notional_usd"] = getattr(budget, "max_notional", None)
    # The opt-in gates: whether they are on, and the numbers they compare against. Turning one on
    # changes what "RISK_CHECK accepted" meant, so it must move the version.
    basket = getattr(router, "basket_gate", None)
    out["basket_gate"] = getattr(basket, "venue", None) if basket is not None else None
    spectral = getattr(router, "spectral_gate", None)
    out["spectral_var"] = spectral.thresholds() if spectral is not None else None
    return out


def risk_check_version(router: object) -> str:
    """Version of the risk check: the gate's own source plus every threshold it enforces.

    Hashing the source matters as much as the thresholds: adding, removing or reordering a gate
    changes what "RISK_CHECK accepted" meant, with every threshold untouched.
    """
    gate = getattr(type(router), "_gate", None)
    try:
        source = inspect.getsource(gate) if gate is not None else ""
    except (OSError, TypeError):
        source = ""
    if not source:
        log.warning("versioning.gate_source_unavailable", router=type(router).__qualname__)
        return UNKNOWN
    return content_hash({"gate_source": source, "thresholds": gate_thresholds(router)})


# --------------------------------------------------------------------------- session provenance

_REPO_ROOT = Path(__file__).resolve().parents[3]
_SECRET_FLAG = ("token", "key", "secret", "password", "passwd", "auth")


def code_version(root: Path = _REPO_ROOT) -> str:
    """The git commit this process runs, suffixed ``+dirty`` when tracked files differ from it.

    ``unknown`` when there is no git checkout (an installed wheel) or git is unavailable: never a
    guess, since a wrong commit on the record is worse than an admitted gap.
    """
    try:
        head = subprocess.run(["git", "-C", str(root), "rev-parse", "HEAD"], capture_output=True,
                              text=True, timeout=10, check=True).stdout.strip()
        dirty = subprocess.run(["git", "-C", str(root), "status", "--porcelain",
                                "--untracked-files=no"], capture_output=True, text=True,
                               timeout=10, check=True).stdout.strip()
    except (OSError, subprocess.SubprocessError):
        return UNKNOWN
    return f"{head}+dirty" if dirty else head


def file_hash(path: Path) -> str | None:
    """SHA-256 of a config file's bytes, or None when it does not exist."""
    try:
        return sha256(Path(path).read_bytes()).hexdigest()
    except OSError:
        return None


def redact_argv(argv: list[str]) -> list[str]:
    """The command line with the value of any token/key/secret-like option replaced."""
    out: list[str] = []
    hide_next = False
    for arg in argv:
        if hide_next:
            out.append("<redacted>")
            hide_next = False
            continue
        name = arg.split("=", 1)[0].lower()
        secretish = arg.startswith("-") and any(w in name for w in _SECRET_FLAG)
        if secretish and "=" in arg:
            out.append(f"{arg.split('=', 1)[0]}=<redacted>")
        else:
            out.append(arg)
            hide_next = secretish
    return out


def session_provenance(*, argv: list[str], root: Path = _REPO_ROOT) -> dict[str, Any]:
    """What a SESSION_STARTED row records: which code, which config, which command, where."""
    return {
        "argv": redact_argv(list(argv)),
        "code": code_version(root),
        "config_trading_yaml_sha256": file_hash(root / "config" / "trading.yaml"),
        "python": platform.python_version(),
        "platform": platform.platform(),
        "pid": os.getpid(),
    }
