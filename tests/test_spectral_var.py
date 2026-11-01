"""Spectral VaR gate: one-day closed form and multi-day GJR paths (risk/spectral_var.py)."""
from __future__ import annotations

import math

import numpy as np

from trading_live_claude.risk.spectral_var import estimate_tail, load_engine


def _leveraged_closes(n: int = 1500) -> list[float]:
    """Closes from the engine's synthetic GJR market (skew-t shocks, gamma = 0.12 leverage)."""
    _, S, _ = load_engine().synthetic_market(n=n, seed=7)
    return [float(x) for x in S]


def _rescaled_loss(closes: list[float], level: float, h: int) -> tuple[float, float]:
    """The old shortcut: mu h + sqrt(horizon_variance) times the 1-day law's quantile."""
    be = load_engine()
    r = np.diff(np.log(np.asarray(closes)))
    p, s2, z = be.fit_gjr(r)
    q = float(be.SemiParamGPD(z, tail_frac=0.10).ppf(np.array([1.0 - level]))[0])
    sd = math.sqrt(float(be.horizon_variance(float(s2[-1]), p, h)))
    return 1.0 - math.exp(p.mu * h + q * sd), q


def test_one_day_is_the_laws_quantile_on_the_next_days_sd() -> None:
    closes = _leveraged_closes()
    est = estimate_tail(closes, level=0.99, horizon_days=1)
    loss, q = _rescaled_loss(closes, 0.99, 1)
    assert est.q_level == q
    assert math.isclose(est.loss_fraction, loss, rel_tol=1e-12)


def test_multi_day_runs_the_gjr_paths_and_is_deterministic() -> None:
    closes = _leveraged_closes()
    a = estimate_tail(closes, level=0.99, horizon_days=10)
    b = estimate_tail(closes, level=0.99, horizon_days=10)
    assert a == b
    rescaled, q1 = _rescaled_loss(closes, 0.99, 10)
    # leverage pushes the variance path up after losses: the 10-day tail is heavier than the
    # rescaled 1-day law, so the gate now caps more tightly than the shortcut did
    assert a.loss_fraction > rescaled
    assert a.q_level < q1
