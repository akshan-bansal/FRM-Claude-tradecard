"""Spectral VaR gate: caps an entry's tail loss with the BSM_Spectral engine (``BSM_Spectral/``).

For a long position in the underlying, the BSM spectral paper's own underlying-risk filter applies:

1. daily log returns from the symbol's closes;
2. a GJR-GARCH(1,1) fit (``bsm_event.fit_gjr``) for the conditional variance, with its
   standardised residuals;
3. a semi-parametric law for those residuals: empirical body, generalised Pareto tails
   (``bsm_event.SemiParamGPD``), restandardised to mean 0 and variance 1;
4. the h-day variance forecast (``bsm_event.horizon_variance``) and the law's lower quantile give
   the h-day log-return quantile, so the loss fraction at ``level`` is ``1 - exp(mu*h + q*sd)``.

The gate then compares ``shares * entry * loss_fraction`` with ``cap_pct * equity``. When the
intended size exceeds the cap it returns the largest whole quantity that fits, and the Router trims
to it; the Router never lets this gate enlarge an order, and never runs it on an exit.

It fails closed. When the gate is on and the VaR cannot be computed (too little history, a fetch
error, a fit that does not converge to a finite variance), the entry is rejected with the reason.
A gate that quietly passed whatever it could not measure would be no gate.

Every figure it reports is computed from the closes it was given; nothing is assumed. The engine's
source hash is part of :meth:`thresholds`, so a change to ``bsm_event.py`` moves the Router's
``risk_check_version``.
"""
from __future__ import annotations

import importlib.util
import math
import sys
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from hashlib import sha256
from pathlib import Path
from types import ModuleType
from typing import Any

from ..logging_setup import get_logger

log = get_logger(__name__)

ENGINE_DIR = Path(__file__).resolve().parents[3] / "BSM_Spectral"
_ENGINE_MODULE = "bsm_event"


def load_engine(engine_dir: Path = ENGINE_DIR) -> ModuleType:
    """Import ``bsm_event.py`` from the BSM_Spectral subtree.

    BSM_Spectral is a folder of scripts that import each other by bare name, not a package, so
    it is loaded by path and registered under its own name (its sibling imports then resolve).
    """
    if _ENGINE_MODULE in sys.modules:
        return sys.modules[_ENGINE_MODULE]
    path = engine_dir / f"{_ENGINE_MODULE}.py"
    spec = importlib.util.spec_from_file_location(_ENGINE_MODULE, path)
    if spec is None or spec.loader is None:
        raise ImportError(f"BSM_Spectral engine not found at {path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[_ENGINE_MODULE] = module
    try:
        spec.loader.exec_module(module)
    except Exception:
        sys.modules.pop(_ENGINE_MODULE, None)
        raise
    return module


def engine_version(engine_dir: Path = ENGINE_DIR) -> str:
    try:
        return sha256((engine_dir / f"{_ENGINE_MODULE}.py").read_bytes()).hexdigest()[:16]
    except OSError:
        return "unknown"


@dataclass(frozen=True)
class SpectralCheck:
    """One gate evaluation. ``reason`` set = reject; else ``max_shares`` is the cap's fit."""

    reason: str | None
    max_shares: int | None
    summary: str
    observed: dict[str, Any]


@dataclass(frozen=True)
class TailEstimate:
    loss_fraction: float          # h-day loss at `level`, as a fraction of notional (positive)
    n_returns: int
    sigma_next: float             # one-step conditional sd of the daily log return
    persistence: float
    xi_lower: float               # GPD shape of the lower tail
    q_level: float                # standardised lower quantile used
    last_close: float


def estimate_tail(closes: Sequence[float], *, level: float, horizon_days: int,
                  tail_frac: float = 0.10, engine: ModuleType | None = None) -> TailEstimate:
    """The h-day loss fraction at ``level`` for a long unit position, from daily closes."""
    import numpy as np

    be = engine or load_engine()
    px = np.asarray([float(c) for c in closes], dtype=float)
    if px.size < 3 or not np.all(np.isfinite(px)) or np.any(px <= 0):
        raise ValueError("closes must be finite and positive")
    r = np.diff(np.log(px))
    params, s2, z = be.fit_gjr(r)
    hvar = float(be.horizon_variance(float(s2[-1]), params, horizon_days))
    if not math.isfinite(hvar) or hvar <= 0:
        raise ValueError(f"GJR horizon variance is not finite and positive ({hvar})")
    law = be.SemiParamGPD(z, tail_frac=tail_frac)
    q = float(np.asarray(law.ppf(np.array([1.0 - level])))[0])
    log_q = params.mu * horizon_days + q * math.sqrt(hvar)
    loss = 1.0 - math.exp(log_q)
    if not math.isfinite(loss):
        raise ValueError("loss fraction is not finite")
    return TailEstimate(loss_fraction=max(loss, 0.0), n_returns=int(r.size),
                        sigma_next=math.sqrt(float(s2[-1])), persistence=float(params.persistence),
                        xi_lower=float(law.xi_lo), q_level=q, last_close=float(px[-1]))


class SpectralVarGate:
    """Opt-in Router gate. ``closes_for(symbol)`` returns daily closes, oldest first."""

    def __init__(self, closes_for: Callable[[str], Sequence[float]], *, cap_pct: float,
                 level: float = 0.99, horizon_days: int = 1, min_returns: int = 500,
                 tail_frac: float = 0.10, engine_dir: Path = ENGINE_DIR) -> None:
        if not 0.0 < cap_pct < 1.0:
            raise ValueError("cap_pct is a fraction of equity in (0, 1)")
        if not 0.5 < level < 1.0:
            raise ValueError("level must be in (0.5, 1)")
        self.closes_for = closes_for
        self.cap_pct = cap_pct
        self.level = level
        self.horizon_days = int(horizon_days)
        self.min_returns = int(min_returns)
        self.tail_frac = tail_frac
        self.engine_dir = engine_dir
        self._engine = load_engine(engine_dir)
        self._engine_version = engine_version(engine_dir)
        # (symbol, n_closes, last_close) -> estimate. A new bar changes the key; within a bar the
        # fit is reused instead of re-running SLSQP on every poll.
        self._cache: dict[tuple[str, int, float], TailEstimate] = {}

    def thresholds(self) -> dict[str, Any]:
        return {"cap_pct": self.cap_pct, "level": self.level, "horizon_days": self.horizon_days,
                "min_returns": self.min_returns, "tail_frac": self.tail_frac,
                "engine": f"BSM_Spectral/bsm_event.py@{self._engine_version}"}

    def estimate(self, symbol: str) -> TailEstimate:
        closes = list(self.closes_for(symbol))
        if len(closes) - 1 < self.min_returns:
            raise _Short(len(closes) - 1)
        key = (symbol, len(closes), float(closes[-1]))
        if key not in self._cache:
            self._cache = {k: v for k, v in self._cache.items() if k[0] != symbol}
            self._cache[key] = estimate_tail(closes, level=self.level,
                                             horizon_days=self.horizon_days,
                                             tail_frac=self.tail_frac, engine=self._engine)
        return self._cache[key]

    def check(self, symbol: str, *, shares: int, entry: float, equity: float) -> SpectralCheck:
        try:
            est = self.estimate(symbol)
        except _Short as e:
            return SpectralCheck(reason=short_reason(e.n, symbol, self.min_returns),
                                 max_shares=None, summary="", observed={"n_returns": e.n})
        except Exception as e:                       # fetch or fit failure: fail closed
            log.warning("spectral_var.unavailable", symbol=symbol, error=str(e))
            return SpectralCheck(reason=unavailable_reason(symbol, str(e)),
                                 max_shares=None, summary="", observed={"error": str(e)})
        cap_usd = self.cap_pct * equity
        per_unit = entry * est.loss_fraction
        var_usd = shares * per_unit
        max_shares = int(cap_usd // per_unit) if per_unit > 0 else shares
        summary = summary_text(self.level, self.horizon_days, var_usd, est.loss_fraction,
                               cap_usd, self.cap_pct)
        observed = {"loss_fraction": est.loss_fraction, "var_usd": var_usd, "cap_usd": cap_usd,
                    "n_returns": est.n_returns, "sigma_next": est.sigma_next,
                    "persistence": est.persistence, "xi_lower": est.xi_lower,
                    "q_level": est.q_level, "last_close": est.last_close,
                    "max_shares": max_shares}
        return SpectralCheck(reason=None, max_shares=max_shares, summary=summary,
                             observed=observed)


def summary_text(level: float, horizon_days: int, var_usd: float, loss_fraction: float,
                 cap_usd: float, cap_pct: float) -> str:
    """The gate's one-line verdict, shared with ``trading audit replay`` so a replay reproduces it."""
    return (f"{level:.0%} {horizon_days}d VaR ${var_usd:,.0f} ({loss_fraction:.2%} of notional) "
            f"vs cap ${cap_usd:,.0f} ({cap_pct:.2%} of equity)")


def short_reason(n: int, symbol: str, min_returns: int) -> str:
    return f"spectral VaR: {n} daily returns for {symbol}, need {min_returns}"


def unavailable_reason(symbol: str, error: str) -> str:
    return f"spectral VaR unavailable for {symbol}: {error}"


class _Short(Exception):
    def __init__(self, n: int) -> None:
        super().__init__(n)
        self.n = n


def closes_from_broker(broker: Any, *, days: int = 1100) -> Callable[[str], list[float]]:
    """``closes_for`` over a broker's daily candles (Kraken returns at most ~720 of them)."""
    def closes_for(symbol: str) -> list[float]:
        end = datetime.now(UTC)
        bars = broker.candles(symbol, end - timedelta(days=days), end, "OneDay")
        return [float(b.close) for b in sorted(bars, key=lambda b: b.start)]
    return closes_for


def attach_spectral_gate(router: Any, broker: Any, *, cap_pct: float | None,
                         level: float = 0.99, horizon_days: int = 1,
                         min_returns: int = 500) -> SpectralVarGate | None:
    """Turn the gate on for ``router`` when ``cap_pct`` is given; None (the default) leaves it off."""
    if cap_pct is None:
        return None
    gate = SpectralVarGate(closes_from_broker(broker), cap_pct=cap_pct, level=level,
                           horizon_days=horizon_days, min_returns=min_returns)
    router.spectral_gate = gate
    return gate
