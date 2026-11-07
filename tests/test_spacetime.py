"""Greeks as one space-time jet, and Ito's lemma on it (BSM_Spectral/bsm_spacetime.py)."""
from __future__ import annotations

import math
import sys
from pathlib import Path

import numpy as np
import pytest
from scipy.stats import norm

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "BSM_Spectral"))
import bsm_event as be
import bsm_spacetime as st

DIGITAL = be.BSMDigital(strike=100.0, expiry=0.25, rate=0.04, dividend=0.015, skew=-0.3)
FLAT = be.BSMDigital(strike=100.0, expiry=0.25, rate=0.04, dividend=0.015, skew=0.0)
CALL = be.SmileOption(base=FLAT, strike=105.0, kind="call")
PUT = be.SmileOption(base=DIGITAL, strike=95.0, kind="put")
POINT = (101.0, 0.22, 0.25)


@pytest.mark.parametrize("model", [DIGITAL, FLAT, CALL, PUT, be.LatentEvent(strike=0.2, expiry=30.0)])
def test_prices_match_the_engine_at_plain_numbers(model) -> None:
    S, v, tau = (0.5, 0.3, 12.0) if isinstance(model, be.LatentEvent) else POINT
    assert st.price(model, S, v, tau) == pytest.approx(model.price(S, v, tau), rel=1e-13)
    J, _ = st.spacetime_jet(model, S, v, tau)
    assert J.value == pytest.approx(model.price(S, v, tau), rel=1e-13)


def test_black_scholes_greeks_have_their_closed_forms() -> None:
    """Flat smile: the jet's call greeks are the textbook ones, theta included."""
    S, v, tau = POINT
    K, r, q = CALL.strike, FLAT.rate, FLAT.dividend
    d1 = (math.log(S / K) + (r - q + 0.5 * v * v) * tau) / (v * math.sqrt(tau))
    d2 = d1 - v * math.sqrt(tau)
    g = st.greeks(CALL, S, v, tau)
    assert g["delta"] == pytest.approx(math.exp(-q * tau) * norm.cdf(d1), rel=1e-12)
    assert g["gamma"] == pytest.approx(math.exp(-q * tau) * norm.pdf(d1) / (S * v * math.sqrt(tau)), rel=1e-12)
    assert g["vega"] == pytest.approx(S * math.exp(-q * tau) * norm.pdf(d1) * math.sqrt(tau), rel=1e-12)
    theta = (-S * math.exp(-q * tau) * norm.pdf(d1) * v / (2 * math.sqrt(tau))
             - r * K * math.exp(-r * tau) * norm.cdf(d2) + q * S * math.exp(-q * tau) * norm.cdf(d1))
    assert g["theta"] == pytest.approx(theta, rel=1e-12)


@pytest.mark.parametrize("name,step", [("charm", (1, 0, 1)), ("veta", (0, 1, 1)), ("color", (2, 0, 1))])
def test_time_cross_greeks_match_finite_differences(name: str, step: tuple[int, int, int]) -> None:
    S, v, tau = POINT
    h = np.array([1e-2, 1e-4, 1e-4])

    def f(x):
        return DIGITAL.price(x[0], x[1], x[2])

    x0 = np.array(POINT)
    # central differences in each direction the greek takes, with the calendar sign on tau
    def d(func, i):
        e = np.zeros(3)
        e[i] = h[i]
        return lambda x: (func(x + e) - func(x - e)) / (2 * h[i])

    g = f
    for i, k in enumerate(step):
        for _ in range(k):
            g = d(g, i)
    expected = g(x0) * (-1) ** step[2]
    assert st.greeks(DIGITAL, S, v, tau)[name] == pytest.approx(expected, rel=2e-3)


@pytest.mark.parametrize("model", [FLAT, CALL, be.SmileOption(base=FLAT, strike=90.0, kind="put")])
def test_a_flat_smile_satisfies_the_black_scholes_pde(model) -> None:
    """Ito's lemma under the pricing measure: L P = r P, so the residual vanishes."""
    for S in (80.0, 100.0, 125.0):
        assert st.ito_residual(model, S, 0.25, 0.4) == pytest.approx(0.0, abs=1e-10)


def test_a_moneyness_smile_breaks_the_pde_and_the_residual_says_by_how_much() -> None:
    residual = st.ito_residual(DIGITAL, *POINT)
    assert abs(residual) > 1e-4
    assert st.ito_residual(FLAT, *POINT) == pytest.approx(0.0, abs=1e-10)


def test_partial_of_a_jet() -> None:
    x, y, z = be.JetN.variables((1.0, 2.0, 3.0), 4)
    J = x * x * y + z * z * z
    dx, dz = st.partial(J, 0), st.partial(J, 2)
    assert dx.value == pytest.approx(2 * 1 * 2)
    assert dz.value == pytest.approx(3 * 9)
    assert st.partial(dx, 1).value == pytest.approx(2 * 1)


def test_the_generator_of_a_known_diffusion() -> None:
    """For f = S^2 under dS = mu S dt + sigma S dW: L f = (2 mu + sigma^2) S^2."""
    dyn = st.StateDynamics(kind="random_walk", mu=0.05, scale=1.0, eta=0.0)
    X = be.JetN.variables((100.0, 0.2, 1.0), 4)
    f = X[0] * X[0]
    assert st.generator(f, X, dyn).value == pytest.approx((2 * 0.05 + 0.2 ** 2) * 100.0 ** 2, rel=1e-12)


def test_log_ou_drift_is_ito_consistent() -> None:
    """d ln v = kappa (theta - ln v) dt: the generator applied to ln v must give exactly that drift."""
    dyn = st.StateDynamics(kind="log_ou", kappa=4.0, theta=math.log(0.2), eta=0.9)
    X = be.JetN.variables((100.0, 0.3, 1.0), 4)
    got = st.generator(be._log(X[1]), X, dyn).value
    assert got == pytest.approx(4.0 * (math.log(0.2) - math.log(0.3)), rel=1e-12)


def test_horizon_moments_match_an_exact_diffusion() -> None:
    """Spot alone under GBM, a position in the underlying: mean and variance are known exactly."""
    mu, sig, S, h = 0.08, 0.25, 100.0, 5 / 252
    dyn = st.StateDynamics(kind="random_walk", mu=mu, scale=1.0, eta=0.0)
    got = st.horizon_moments(be.Underlying(), S, sig, 1.0, dyn, h)
    mean = S * (math.exp(mu * h) - 1)
    var = S * S * math.exp(2 * mu * h) * (math.exp(sig * sig * h) - 1)
    assert got["mean"] == pytest.approx(mean, rel=1e-5)
    assert got["var"] == pytest.approx(var, rel=1e-3)


def test_horizon_must_end_before_expiry() -> None:
    with pytest.raises(ValueError):
        st.horizon_moments(DIGITAL, 100.0, 0.2, 0.01, st.StateDynamics(), 0.02)


# ---------------------------------------------------------------- dynamics: fitting and simulation

DT = 1 / 252


def _path(dyn: st.StateDynamics, n: int, v0: float, seed: int = 0) -> tuple[np.ndarray, np.ndarray]:
    S, v = [100.0], [v0]
    for i in range(n):
        s1, v1 = st.simulate(dyn, S[-1], v[-1], DT, n_paths=1, n_steps=4, seed=seed * 100000 + i)
        S.append(float(s1[0]))
        v.append(float(v1[0]))
    return np.array(S), np.array(v)


def test_log_ou_fit_recovers_its_parameters() -> None:
    truth = st.StateDynamics(kind="log_ou", kappa=6.0, theta=math.log(0.2), eta=1.0, scale=1.0)
    S, v = _path(truth, 6000, 0.2, seed=1)
    dyn, _ = st.fit_dynamics(S, v, DT, "log_ou")
    assert dyn.kappa == pytest.approx(6.0, rel=0.35)
    assert math.exp(dyn.theta) == pytest.approx(0.2, rel=0.1)
    assert dyn.eta == pytest.approx(1.0, rel=0.05)
    assert dyn.scale == pytest.approx(1.0, rel=0.05)


def test_three_halves_fit_recovers_its_volatility() -> None:
    truth = st.StateDynamics(kind="three_halves", kappa=30.0, theta=0.2, eta=2.0, scale=1.0)
    S, v = _path(truth, 6000, 0.2, seed=2)
    dyn, _ = st.fit_dynamics(S, v, DT, "three_halves")
    assert dyn.eta == pytest.approx(2.0, rel=0.1)
    assert dyn.theta == pytest.approx(0.2, rel=0.2)


def test_correlation_between_spot_and_vol_is_recovered() -> None:
    truth = st.StateDynamics(kind="log_ou", kappa=6.0, theta=math.log(0.2), eta=1.0, rho=-0.7)
    S, v = _path(truth, 4000, 0.2, seed=3)
    assert st.fit_dynamics(S, v, DT, "log_ou")[0].rho == pytest.approx(-0.7, abs=0.06)


@pytest.mark.parametrize("kind,eta,expected", [("log_ou", 1.0, 1.0), ("three_halves", 2.0, 1.5)])
def test_each_model_shows_its_own_vol_of_vol_exponent(kind: str, eta: float, expected: float) -> None:
    truth = st.StateDynamics(kind=kind, kappa=3.0, theta=math.log(0.2) if kind == "log_ou" else 0.2, eta=eta)
    _, v = _path(truth, 5000, 0.2, seed=4)
    assert st.vol_of_vol_exponent(v) == pytest.approx(expected, abs=0.25)


def test_mean_reverting_vol_stays_positive_and_reverts() -> None:
    for kind, theta in (("log_ou", math.log(0.2)), ("three_halves", 0.2)):
        dyn = st.StateDynamics(kind=kind, kappa=8.0, theta=theta, eta=1.5)
        _, v = st.simulate(dyn, 100.0, 0.6, 1.0, n_paths=4000, n_steps=252, seed=5)
        assert (v > 0).all()
        assert np.median(v) < 0.35


def test_ito_expansion_agrees_with_monte_carlo_over_a_day() -> None:
    model = be.BSMDigital(strike=100.0, expiry=0.25, skew=-0.3)
    dyn = st.StateDynamics(kind="log_ou", kappa=6.0, theta=math.log(0.2), eta=1.2, rho=-0.7, scale=0.9)
    ito = st.horizon_moments(model, 100.0, 0.2, 0.25, dyn, DT)
    mc = st.monte_carlo_moments(model, 100.0, 0.2, 0.25, dyn, DT, n_paths=400000, n_steps=8, seed=6)
    assert abs(ito["mean"] - mc["mean"]) < 4 * mc["se_mean"]
    assert ito["sd"] == pytest.approx(mc["sd"], rel=0.02)
