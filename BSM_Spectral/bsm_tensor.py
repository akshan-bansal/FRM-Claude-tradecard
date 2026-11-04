"""Rolling correlation and covariance as tensors: dated matrices stacked as (time, asset, asset).

Beside the engine, not in it: this file imports ``bsm_event`` and never changes it, so the spectral VaR gate's source
hash and the Router's ``risk_check_version`` do not move. Each slice equals what the engine's matrix code gives for
that window, and ``tests/test_bsm_tensor.py`` holds it to that.

Slice ``k`` uses returns ``R[ends[k] - window : ends[k]]``, which stop before row ``ends[k]``, so a slice dated at a
row knows nothing of that row's return.
"""
from __future__ import annotations

from dataclasses import dataclass

import numpy as np

import bsm_event as be


@dataclass(frozen=True)
class RollingCube:
    assets: tuple[str, ...]
    window: int
    ends: np.ndarray            # (T,) the row each slice stops before
    cov: np.ndarray             # (T, N, N)
    corr: np.ndarray            # (T, N, N)
    eigenvalues: np.ndarray     # (T, N) ascending
    eigenvectors: np.ndarray    # (T, N, N) columns are vectors

    def at(self, end: int) -> np.ndarray:
        """The correlation slice for the window stopping before row ``end``."""
        hit = np.flatnonzero(self.ends == end)
        if not len(hit):
            raise KeyError(f"no slice ends at row {end}")
        return self.corr[hit[0]]

    def mp_filtered(self) -> np.ndarray:
        """``bsm_event.mp_filter`` on every slice, (T, N, N)."""
        return np.stack([be.mp_filter(c, self.window)[0] for c in self.corr])

    def spectral(self, gamma: float = 1.0, lam_min: float = 0.05) -> np.ndarray:
        """``bsm_event.spectral_correlation`` on every slice, (T, N, N)."""
        return np.stack([be.spectral_correlation(c, gamma, lam_min) for c in self.corr])

    def top_share(self) -> np.ndarray:
        """Share of total variance on the largest eigenvalue, (T,)."""
        return self.eigenvalues[:, -1] / self.eigenvalues.sum(axis=1)


def build_cube(returns: np.ndarray, assets: tuple[str, ...] | list[str], *, window: int = 252,
               step: int = 1) -> RollingCube:
    """Refuses gaps and flat series: a filled or undefined correlation would look like a measurement."""
    R = np.asarray(returns, dtype=float)
    if R.ndim != 2 or R.shape[1] != len(assets):
        raise ValueError(f"returns {R.shape} do not match {len(assets)} assets")
    if window < 2 or step < 1 or len(R) < window:
        raise ValueError(f"need window >= 2, step >= 1 and at least {window} rows, got {len(R)}")
    if not np.isfinite(R).all():
        raise ValueError("returns hold NaN or inf; align the series first")
    # (T, N, window) -> (T, window, N)
    frames = np.lib.stride_tricks.sliding_window_view(R, window, axis=0)[::step].transpose(0, 2, 1)
    centred = frames - frames.mean(axis=1, keepdims=True)
    cov = np.einsum("tki,tkj->tij", centred, centred) / (window - 1)
    var = np.einsum("tii->ti", cov)
    if (var <= 0).any():
        raise ValueError("a series is flat over some window, so its correlation is undefined")
    sd = np.sqrt(var)
    corr = cov / np.einsum("ti,tj->tij", sd, sd)
    values, vectors = np.linalg.eigh((corr + corr.transpose(0, 2, 1)) / 2)
    ends = np.arange(window, len(R) + 1, step)
    return RollingCube(tuple(assets), window, ends, cov, corr, values, vectors)
