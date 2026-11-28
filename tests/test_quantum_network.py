"""The two-level network: atoms -> each asset's local field -> learned asset couplings -> regimes.

Written before the implementation. Power and null are both tested: a planted cross-asset coupling
must be recovered and must improve held-out log loss; with no coupling, none may be learned.
"""
from __future__ import annotations

import numpy as np
import pytest
from scipy import optimize

from trading_live_claude.quantum import field
from trading_live_claude.quantum.network import (
    NetworkSpec,
    _objective,
    _pack,
    fit_network,
    log_loss,
    node_state,
    predict_network,
)


def planted_panel(seed: int, coupling: float, T: int = 600, N: int = 4, K: int = 2):
    """Each asset's stress belief is its own atom-0 evidence, pulled by asset 0 <-> 1 coupling."""
    r = np.random.default_rng(seed)
    X = r.uniform(-1, 1, (T, N, K))
    h = 1.2 * X[:, :, 0] - 0.1
    J = np.zeros((N, N))
    J[0, 1] = J[1, 0] = coupling
    m = field.solve_static(h, J).m
    a, b = -0.8 - 3.0 * m, 0.8 - 3.0 * m                  # ordered logit, beta = 3
    sa, sb = 1 / (1 + np.exp(-a)), 1 / (1 + np.exp(-b))
    u = r.random((T, N))
    C = np.where(u < sa, 0, np.where(u < sb, 1, 2))
    return X, C.astype(np.int64), J


@pytest.mark.parametrize("kind", ["field", "uncoupled"])
def test_analytic_gradient_matches_finite_differences(kind: str) -> None:
    X, C, _ = planted_panel(0, 0.6, T=40)
    spec = NetworkSpec(kind=kind)
    r = np.random.default_rng(1)
    theta = _pack(spec, X.shape[1], X.shape[2], r)
    theta[:] += r.normal(scale=0.05, size=theta.shape)
    f = lambda th: _objective(th, X, C, spec)[0]            # noqa: E731
    g = lambda th: _objective(th, X, C, spec)[1]            # noqa: E731
    assert optimize.check_grad(f, g, theta) < 1e-4 * max(1.0, float(np.linalg.norm(g(theta))))


def test_planted_coupling_is_recovered_and_helps_out_of_sample() -> None:
    X, C, _ = planted_panel(2, 0.3, T=900)
    tr, ev = slice(0, 600), slice(600, 900)
    fit_f = fit_network(X[tr], C[tr], NetworkSpec(kind="field"))
    fit_u = fit_network(X[tr], C[tr], NetworkSpec(kind="uncoupled"))
    assert fit_f.converged and fit_u.converged
    assert fit_f.J[0, 1] > 0.1
    off = [abs(fit_f.J[i, j]) for i in range(4) for j in range(i + 1, 4) if (i, j) != (0, 1)]
    assert max(off) < fit_f.J[0, 1] / 2
    assert log_loss(predict_network(fit_f, X[ev]), C[ev]) < log_loss(predict_network(fit_u, X[ev]), C[ev])


def test_no_coupling_is_learned_from_a_null_panel() -> None:
    """No systematic coupling (estimates centred on zero) and no out-of-sample gain from it.

    A fixed cut-off on max|J| is the wrong test: one 600-week panel gave 0.086 from noise alone.
    """
    entries, gains = [], []
    for seed in range(3, 9):
        X, C, _ = planted_panel(seed, 0.0, T=900)
        fit_f = fit_network(X[:600], C[:600], NetworkSpec(kind="field"))
        fit_u = fit_network(X[:600], C[:600], NetworkSpec(kind="uncoupled"))
        entries.extend(fit_f.J[np.triu_indices(4, 1)])
        gains.append(log_loss(predict_network(fit_u, X[600:]), C[600:])
                     - log_loss(predict_network(fit_f, X[600:]), C[600:]))
    e = np.array(entries)
    assert abs(e.mean()) < 3 * e.std(ddof=1) / np.sqrt(len(e))
    assert np.mean(gains) < 0.002


def test_learned_field_coupling_stays_contractive() -> None:
    X, C, _ = planted_panel(4, 0.9, T=400)      # stronger than the box allows
    fit_f = fit_network(X, C, NetworkSpec(kind="field", max_norm=0.95))
    assert field.spectral_norm(fit_f.J) <= 0.95 + 1e-9


def test_quantum_arm_reduces_to_the_uncoupled_family_without_coupling() -> None:
    X, _, _ = planted_panel(5, 0.0, T=30)
    spec_q = NetworkSpec(kind="quantum", gamma=0.4, t=1.0)
    spec_u = NetworkSpec(kind="uncoupled")
    r = np.random.default_rng(0)
    th = _pack(spec_q, 4, 2, r)
    from trading_live_claude.quantum.network import _unpack
    p = _unpack(th, spec_q, 4, 2)
    mq = node_state(spec_q, p, X)
    mu = node_state(spec_u, p, X)
    assert np.allclose(mq, np.cos(2 * 0.4 * 1.0) * mu, atol=1e-12)


def test_quantum_fit_runs_and_beats_its_starting_point() -> None:
    X, C, _ = planted_panel(6, 0.4, T=120, N=3, K=2)
    spec = NetworkSpec(kind="quantum", maxiter=60)
    fit_q = fit_network(X, C, spec)
    start = fit_network(X, C, spec.model_copy(update={"maxiter": 0}))
    assert fit_q.nll < start.nll
    P = predict_network(fit_q, X)
    assert np.allclose(P.sum(axis=2), 1.0) and np.all(P > 0)


def test_missing_asset_weeks_are_neutral_and_unscored() -> None:
    X, C, _ = planted_panel(7, 0.3, T=200)
    X[10:20, 2, :] = np.nan
    C[10:20, 2] = -1
    fit_f = fit_network(X, C, NetworkSpec(kind="field"))
    P = predict_network(fit_f, X)
    assert np.all(np.isfinite(P))
    with pytest.raises(ValueError):
        fit_network(X, np.full_like(C, -1), NetworkSpec(kind="field"))
