"""Eigen against eigentensor representations (BSM_Spectral/bsm_eigentensor.py)."""
from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "BSM_Spectral"))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import bsm_eigentensor as et
from eigen_vs_tensor import align_closes


def _unit(*values: float) -> np.ndarray:
    v = np.asarray(values, dtype=float)
    return v / np.linalg.norm(v)


def _returns(T: int = 900, n: int = 5, seed: int = 1) -> np.ndarray:
    rng = np.random.default_rng(seed)
    common = rng.standard_normal((T, 1))
    return 0.01 * (0.6 * common + rng.standard_normal((T, n)))


def test_a_matrix_is_the_second_order_case() -> None:
    """Order 2 must give the ordinary eigen-decomposition, or the two sides are not comparable."""
    A = np.cov(np.random.default_rng(0).standard_normal((200, 4)), rowvar=False)
    lam, V, residual = et.z_eigenpairs(A, n_starts=12, max_iter=5000, tol=1e-12)
    top = np.linalg.eigh(A)
    assert lam[0] == pytest.approx(top[0][-1], rel=1e-6)
    assert abs(V[0] @ top[1][:, -1]) == pytest.approx(1.0, abs=1e-6)
    assert residual[0] < 1e-6


@pytest.mark.parametrize("m", [3, 4])
def test_known_eigenpairs_are_recovered(m: int) -> None:
    e1, e2 = np.eye(4)[0], np.eye(4)[1]
    A = 3.0 * et.outer_power(e1, m) + 1.0 * et.outer_power(e2, m)
    lam, V, residual = et.z_eigenpairs(A)
    assert lam[0] == pytest.approx(3.0, rel=1e-6)
    assert abs(V[0] @ e1) == pytest.approx(1.0, abs=1e-6)
    assert np.any(np.isclose(lam, 1.0, rtol=1e-6))
    assert residual.max() < 1e-5


def test_a_start_that_never_settled_is_not_reported() -> None:
    e1, e2 = np.eye(4)[0], np.eye(4)[1]
    A = 3.0 * et.outer_power(e1, 3) + 1.0 * et.outer_power(e2, 3)
    lam, _, residual = et.z_eigenpairs(A, max_iter=20)
    assert residual.max() <= et.SETTLED * np.linalg.norm(A) or len(lam) == 1


@pytest.mark.parametrize("m", [3, 4])
def test_every_pair_solves_the_eigen_equation(m: int) -> None:
    x = et.whiten(np.random.default_rng(3).standard_t(5, size=(800, 4)))
    A = et.coskewness(x) if m == 3 else et.excess_cokurtosis(x)[0]
    lam, V, _ = et.z_eigenpairs(A, max_iter=20000, tol=1e-11)
    for value, v in zip(lam[:3], V[:3], strict=True):
        Av = A
        for _ in range(m - 1):
            Av = Av @ v
        np.testing.assert_allclose(Av, value * v, atol=1e-5)
        assert np.linalg.norm(v) == pytest.approx(1.0)


def test_a_negative_even_order_eigenvalue_is_found() -> None:
    """Fat tails in one direction and thin in another: a search from one sign would miss the thin."""
    e1 = np.eye(3)[0]
    assert et.z_eigenpairs(-2.0 * et.outer_power(e1, 4))[0][0] == pytest.approx(-2.0, rel=1e-6)


def test_eigenpairs_repeat_for_a_seed() -> None:
    x = et.whiten(_returns())
    a = et.z_eigenpairs(et.coskewness(x), seed=4)
    b = et.z_eigenpairs(et.coskewness(x), seed=4)
    np.testing.assert_array_equal(a[0], b[0])
    np.testing.assert_array_equal(a[1], b[1])


def test_whitening_leaves_identity_covariance() -> None:
    np.testing.assert_allclose(np.cov(et.whiten(_returns()), rowvar=False), np.eye(5), atol=1e-10)


def test_elliptical_cokurtosis_is_only_isotropic() -> None:
    """kappa carries an elliptical law's tails; the remainder is near zero and falls with sample."""
    rng = np.random.default_rng(5)
    small = et.elliptical_sample(rng, 2000, 4, 1.0)
    large = et.elliptical_sample(rng, 200000, 4, 1.0)
    aniso_small, _ = et.excess_cokurtosis(small)
    aniso_large, kappa = et.excess_cokurtosis(large)
    assert kappa == pytest.approx(1.0, abs=0.25)
    assert np.linalg.norm(aniso_large) < np.linalg.norm(aniso_small) / 3


def test_removing_the_isotropic_part_is_exact() -> None:
    n = 3
    excess_of = et.isotropic(n) * 0.5
    kappa = 3.0 * np.einsum("iijj->", excess_of) / (n * (n + 2))
    assert kappa == pytest.approx(1.5)
    np.testing.assert_allclose(excess_of - kappa / 3.0 * et.isotropic(n), 0.0, atol=1e-12)


def test_gaussian_has_no_coskew_structure_but_a_skewed_factor_does() -> None:
    rng = np.random.default_rng(6)
    gaussian = et.whiten(rng.standard_normal((4000, 4)))
    factor = rng.chisquare(2, size=(4000, 1)) - 2.0
    skewed = et.whiten(rng.standard_normal((4000, 4)) + factor @ _unit(1, 1, 0, 0)[None, :])
    assert et.structure_stats(skewed)[0] > 5 * et.structure_stats(gaussian)[0]


def test_low_rank_recovers_a_rank_one_tensor_and_full_rank_tends_to_the_tensor() -> None:
    v = _unit(1, 2, 0)
    A = 2.0 * et.outer_power(v, 3)
    np.testing.assert_allclose(et.low_rank(A, 1), A, atol=1e-6)


def test_hosvd_reconstructs() -> None:
    A = et.horizon_stack(_returns())
    factors, core, values = et.hosvd(A)
    rebuilt = core
    for mode, U in enumerate(factors):
        rebuilt = np.moveaxis(np.tensordot(U, np.moveaxis(rebuilt, mode, 0), axes=1), 0, mode)
    np.testing.assert_allclose(rebuilt, A, atol=1e-10)
    assert values[2][0] >= values[2][-1]


def test_identical_horizons_leave_no_horizon_structure() -> None:
    assert et.horizon_share(_returns(), horizons=(1, 1, 1)) == pytest.approx(0.0, abs=1e-12)


def test_fidelity_runs_from_chance_to_one() -> None:
    v = _unit(1, 2, 3, 4, 5)
    assert et.fidelity(v, v) == pytest.approx(1.0)
    assert et.fidelity(v, -v) == pytest.approx(1.0)
    assert et.excess_fidelity(1.0, 5) == pytest.approx(1.0)
    assert et.excess_fidelity(0.2, 5) == pytest.approx(0.0)
    rng = np.random.default_rng(0)
    draws = rng.standard_normal((5000, 5))
    draws /= np.linalg.norm(draws, axis=1, keepdims=True)
    assert np.mean((draws @ v) ** 2) == pytest.approx(0.2, abs=0.01)


def test_a_constant_factor_persists_and_noise_does_not() -> None:
    """Eigenpersistence must read high for a direction that stays and near zero for pure noise."""
    rng = np.random.default_rng(8)
    T, n = 1260, 5
    loading = _unit(1, 1, 1, 1, 0)
    stays = 0.01 * (rng.standard_normal((T, 1)) @ loading[None, :] * 3.0 + rng.standard_normal((T, n)))
    noise = 0.01 * rng.standard_normal((T, n))
    held = et.eigenpersistence(et.eigen_profile("matrix", stays, window=252, step=63))
    drift = et.eigenpersistence(et.eigen_profile("matrix", noise, window=252, step=63))
    assert held > 0.9
    assert abs(drift) < 0.25


def test_a_rotating_factor_loses_persistence_with_lag() -> None:
    rng = np.random.default_rng(9)
    T, n = 1260, 5
    angle = np.linspace(0.0, np.pi / 2, T)
    loading = np.zeros((T, n))
    loading[:, 0], loading[:, 1] = np.cos(angle), np.sin(angle)
    X = 0.01 * (3.0 * rng.standard_normal((T, 1)) * loading + rng.standard_normal((T, n)))
    profile = et.eigen_profile("matrix", X, window=252, step=63, lags=(1, 3))
    assert profile[1]["fidelity"] > profile[3]["fidelity"]


def test_eigen_windows_in_a_pair_share_no_dates() -> None:
    """A lag below one window would compare an estimate with itself and flatter the fidelity."""
    profile = et.eigen_profile("matrix", _returns(T=600), window=252, step=21, lags=(1, 2))
    assert profile[1]["pairs"] == len(range(0, 600 - 2 * 252 + 1, 21))
    assert 2 not in profile or profile[2]["pairs"] == len(range(0, 600 - 3 * 252 + 1, 21))


def test_a_tensor_kind_gives_a_profile() -> None:
    for kind in ("coskew", "cokurt", "horizon"):
        profile = et.eigen_profile(kind, _returns(T=800), window=252, step=126, lags=(1,))
        assert 0.0 <= profile[1]["fidelity"] <= 1.0 + 1e-9
        assert 0.0 <= profile[1]["capture"] <= 1.0 + 1e-9


def test_cornish_fisher_reduces_to_the_normal_and_widens_with_tails() -> None:
    assert et.cornish_fisher(0.01) == pytest.approx(-2.3263, abs=1e-3)
    assert et.cornish_fisher(0.01, k=2.0) < et.cornish_fisher(0.01)
    assert et.cornish_fisher(0.01, s=-0.5) < et.cornish_fisher(0.01)
    assert et.cornish_fisher(0.01, k=1e6) == et.cornish_fisher(0.01, k=et.KURT_CLIP)


def test_tensor_var_matches_the_books_own_moments() -> None:
    """Raw tensors contracted with a book must equal that book's sample skew and excess kurtosis."""
    Z = np.random.default_rng(11).standard_t(6, size=(5000, 4))
    w = np.array([0.4, 0.3, 0.2, 0.1])
    x = et.whiten(Z)
    root = et.whitener(Z)[1]
    u = root @ w
    u /= np.linalg.norm(u)
    aniso, kappa = et.excess_cokurtosis(x)
    y = (x @ u - (x @ u).mean()) / (x @ u).std()
    assert et.contract(et.coskewness(x), u) == pytest.approx(np.mean(y ** 3), abs=2e-3)
    assert kappa + et.contract(aniso, u) == pytest.approx(np.mean(y ** 4) - 3.0, abs=5e-3)


def test_var_backtest_does_not_look_ahead() -> None:
    R = _returns(T=800)
    w = np.array([[0.2] * 5, [0.5, 0.2, 0.1, 0.1, 0.1]])
    names, a, losses = et.var_backtest(R, w, window=252, refit=63, n_starts=2, max_iter=30)
    changed = R.copy()
    changed[700:] *= 25.0
    _, b, _ = et.var_backtest(changed, w, window=252, refit=63, n_starts=2, max_iter=30)
    days_before = 700 - 60 - 252
    np.testing.assert_allclose(a[:days_before + 1], b[:days_before + 1])
    assert "tensor_rank1" in names and "elliptical_sample" in names
    assert a.shape == (len(losses), 2, len(names))


def test_diebold_mariano_and_holm() -> None:
    rng = np.random.default_rng(2)
    base = rng.normal(1.0, 0.2, 500)
    assert et.diebold_mariano(base - 0.2, base + rng.normal(0, 0.01, 500))[0] < -3
    assert et.diebold_mariano(base, base)[1] == 1.0
    np.testing.assert_allclose(et.holm([0.01, 0.04, 0.03]), [0.03, 0.06, 0.06])


def test_monte_carlo_p_value_cannot_be_zero() -> None:
    assert et.mc_pvalue(9.0, [1.0, 2.0, 3.0]) == pytest.approx(0.25)
    assert et.mc_pvalue(0.0, [1.0, 2.0, 3.0]) == 1.0


def test_aligning_closes_recovers_a_link_same_date_returns_hide() -> None:
    """A Sydney name that reacts a day after a US name: same-date correlation misses it, and a
    horizon stack reads the difference as structure until the closes are lined up."""
    rng = np.random.default_rng(12)
    us = rng.standard_normal(1500) * 0.01
    sydney = np.roll(us, 1) + rng.standard_normal(1500) * 0.005
    daily = pd.DataFrame({"SPY": us, "XXX": rng.standard_normal(1500) * 0.01, "STW.AX": sydney})
    aligned = align_closes(daily)
    assert list(aligned.columns) == list(daily.columns) and len(aligned) == len(daily) - 1
    assert abs(daily["SPY"].corr(daily["STW.AX"])) < 0.1
    assert aligned["SPY"].corr(aligned["STW.AX"]) > 0.8
    assert et.horizon_share(aligned.to_numpy()) < et.horizon_share(daily.to_numpy()) / 3
