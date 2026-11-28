"""Two rival uncertainty relations between direction and volatility forecasts.

**Qubit complementarity.**
- Direction and volatility are read from one qubit along two Bloch axes at angle ``phi``. The angle
  is set by the BSM whitening of (spot, vol) shocks: in whitened coordinates the two shock axes meet
  at ``arccos(rho)``, ``rho`` being the spot-vol correlation.
- With biases ``b_d = 2 P(up) - 1`` and ``b_v = 2 P(vol up) - 1``, the achievable pairs are the
  projection of the Bloch ball: ``(b_d**2 + b_v**2 - 2 b_d b_v cos(phi)) / sin(phi)**2 <= 1``.
- Two consequences. With no direction view, volatility confidence is capped at ``sin(phi)``; full
  volatility confidence forces a direction lean of ``cos(phi)``.
- The Maassen-Uffink relation bounds the summed entropies:
  ``H(dir) + H(vol) >= -log2((1 + |cos(phi)|) / 2)`` bits.
- A single qubit holds the two marginals, never their joint law. Markets show both outcomes every
  week, and they correlate (-0.30 for the S&P, the leverage effect). So this relation can only act
  as a constraint on the two marginal forecasts, never as a model of the pair.

**Christoffersen-Diebold.**
- ``P(r > 0) = 1 - F(-mu / sigma)``. A volatility forecast moves the direction forecast only through
  the drift-to-volatility ratio.
- On S&P weeks this was slightly worse than the base rate (stage-2 validity U3).
"""
from __future__ import annotations

from collections.abc import Callable

import numpy as np
import numpy.typing as npt
from scipy import stats

FloatArray = npt.NDArray[np.float64]
_EDGE = 1e-9


def phi_from_correlation(rho: float) -> float:
    """Angle between the direction and volatility readout axes, from the spot-vol correlation."""
    return float(np.arccos(np.clip(rho, -1.0 + _EDGE, 1.0 - _EDGE)))


def complementarity_form(bd: npt.ArrayLike, bv: npt.ArrayLike, phi: float | FloatArray) -> FloatArray:
    """``<= 1`` exactly when ``(b_d, b_v)`` is reachable by one qubit state."""
    d, v = np.asarray(bd, dtype=np.float64), np.asarray(bv, dtype=np.float64)
    ph = np.asarray(phi, dtype=np.float64)
    if np.any(np.sin(ph) < _EDGE):
        raise ValueError("readout axes must not be parallel")
    out: FloatArray = (d * d + v * v - 2.0 * d * v * np.cos(ph)) / np.sin(ph) ** 2
    return out


def is_feasible(bd: npt.ArrayLike, bv: npt.ArrayLike, phi: float | FloatArray,
                tol: float = 1e-12) -> npt.NDArray[np.bool_]:
    out: npt.NDArray[np.bool_] = complementarity_form(bd, bv, phi) <= 1.0 + tol
    return out


def project_feasible(bd: npt.ArrayLike, bv: npt.ArrayLike,
                     phi: float | FloatArray) -> tuple[FloatArray, FloatArray]:
    """Shrink infeasible pairs toward ``(0, 0)`` onto the boundary; feasible pairs are left alone."""
    d, v = np.asarray(bd, dtype=np.float64), np.asarray(bv, dtype=np.float64)
    form = complementarity_form(d, v, phi)
    scale = np.where(form > 1.0, 1.0 / np.sqrt(np.maximum(form, 1.0)), 1.0)
    return d * scale, v * scale


def constrain_marginals(p_up: npt.ArrayLike, p_volup: npt.ArrayLike,
                        phi: float | FloatArray) -> tuple[FloatArray, FloatArray]:
    """Probabilities in, the nearest jointly reachable probabilities out (same radial shrink)."""
    d, v = project_feasible(2.0 * np.asarray(p_up) - 1.0, 2.0 * np.asarray(p_volup) - 1.0, phi)
    return (1.0 + d) / 2.0, (1.0 + v) / 2.0


def binary_entropy_bits(p: npt.ArrayLike) -> FloatArray:
    q = np.clip(np.asarray(p, dtype=np.float64), 1e-15, 1 - 1e-15)
    out: FloatArray = -(q * np.log2(q) + (1 - q) * np.log2(1 - q))
    return out


def entropic_bound(phi: float) -> float:
    """Maassen-Uffink lower bound, in bits, on ``H(direction) + H(volatility)``."""
    return float(-np.log2((1.0 + abs(np.cos(phi))) / 2.0))


def christoffersen_diebold_up(mu: npt.ArrayLike, sigma: npt.ArrayLike,
                              cdf: Callable[[FloatArray], FloatArray] | None = None) -> FloatArray:
    """``P(r > 0) = 1 - F(-mu / sigma)``; ``F`` is the standardised-return law (normal by default)."""
    m, s = np.asarray(mu, dtype=np.float64), np.asarray(sigma, dtype=np.float64)
    if np.any(s <= 0) or not np.all(np.isfinite(s)):
        raise ValueError("sigma must be positive and finite")
    F = cdf if cdf is not None else stats.norm.cdf
    out: FloatArray = 1.0 - np.asarray(F(-m / s), dtype=np.float64)
    return out
