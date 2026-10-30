#!/usr/bin/env python3
"""BSM call/put Taylor surface through third order and synthetic stress tests.

Install: python -m pip install numpy scipy
Run:     python bsm_parallel_surface.py
         python bsm_parallel_surface.py --samples 100000 --seed 20261001

European options; continuous dividend yield; fixed strike, expiry, rates and
carry during shocks. Volatility is decimal (25% = .25). Values are per unit
of underlying, before contract multipliers. Greeks hold implied volatility
independent of spot: no smile dynamics, transaction costs, or historical data.
Taylor approximations may violate price bounds for large shocks; never clip
these violations away. Curvature energy is a proposed diagnostic, not a
validated trading signal. Exact BSM repricing is the synthetic benchmark.
"""
from dataclasses import dataclass, replace
from math import factorial
import argparse
import numpy as np
from scipy.special import ndtr


@dataclass(frozen=True)
class State:
    spot: float = 100.0
    strike: float = 100.0
    volatility: float = 0.25
    expiry: float = 0.25
    rate: float = 0.04
    dividend: float = 0.01

    def __post_init__(self):
        if not all(np.isfinite(v) for v in vars(self).values()):
            raise ValueError('All state parameters must be finite.')
        if min(self.spot, self.strike, self.volatility, self.expiry) <= 0:
            raise ValueError('BSM requires positive spot, strike, volatility and expiry.')


def prices(state, spot=None, volatility=None):
    """Return (call, put), accepting broadcastable scalar or array shocks."""
    s = np.asarray(state.spot if spot is None else spot, dtype=float)
    v = np.asarray(state.volatility if volatility is None else volatility, dtype=float)
    if np.any(~np.isfinite(s)) or np.any(~np.isfinite(v)) or np.any(s <= 0) or np.any(v <= 0):
        raise ValueError('Repricing requires finite positive spot and volatility.')
    root = np.sqrt(state.expiry)
    d1 = (np.log(s / state.strike) +
          (state.rate - state.dividend + .5 * v**2) * state.expiry) / (v * root)
    d2 = d1 - v * root
    asset = s * np.exp(-state.dividend * state.expiry)
    bond = state.strike * np.exp(-state.rate * state.expiry)
    return asset * ndtr(d1) - bond * ndtr(d2), bond * ndtr(-d2) - asset * ndtr(-d1)


def greeks(state):
    """Raw derivatives: volatility derivatives are per decimal unit, not 1%."""
    s, k, v, t, r, q = (state.spot, state.strike, state.volatility,
                        state.expiry, state.rate, state.dividend)
    root = np.sqrt(t)
    dq = np.exp(-q * t)
    d1 = (np.log(s / k) + (r - q + .5 * v*v) * t) / (v * root)
    d2 = d1 - v * root
    phi = np.exp(-.5 * d1*d1) / np.sqrt(2 * np.pi)
    vega = s * dq * phi * root
    gamma = dq * phi / (s * v * root)
    return {
        'delta_call': dq * ndtr(d1),
        'delta_put': dq * (ndtr(d1) - 1),
        'vega': vega,
        'gamma': gamma,
        'vanna': -dq * phi * d2 / v,
        'vomma': vega * d1 * d2 / v,
        'speed': -gamma / s * (1 + d1 / (v * root)),
        'zomma': gamma / v * (d1 * d2 - 1),
        # Corrected mixed third derivative, d^3 V / dS dvol^2.
        'spot_vol_vol': dq * phi / v**2 * (d1 + d2 - d1 * d2**2),
        'ultima': vega / v**2 *
                  (d1*d2*(d1*d2 - 1) - d1**2 - d2**2),
    }


def taylor_terms(state, dspot, dvol, option='call'):
    """Return first-, second-, third-degree increments (not cumulative)."""
    if option not in ('call', 'put'):
        raise ValueError("option must be 'call' or 'put'")
    a, b = np.broadcast_arrays(np.asarray(dspot, float), np.asarray(dvol, float))
    if np.any(~np.isfinite(a)) or np.any(~np.isfinite(b)):
        raise ValueError('Shocks must be finite.')
    g = greeks(state)
    first = g['delta_' + option] * a + g['vega'] * b
    second = .5*g['gamma']*a**2 + g['vanna']*a*b + .5*g['vomma']*b**2
    third = (g['speed']*a**3/6 + g['zomma']*a**2*b/2 +
             g['spot_vol_vol']*a*b**2/2 + g['ultima']*b**3/6)
    return first, second, third


def taylor_price(state, dspot, dvol, option='call', order=3):
    if order not in (1, 2, 3):
        raise ValueError('order must be 1, 2 or 3')
    terms = taylor_terms(state, dspot, dvol, option)
    base = prices(state)[0 if option == 'call' else 1]
    return base + sum(terms[:order])


def parity_residual(state, call, put, spot=None):
    s = state.spot if spot is None else np.asarray(spot)
    return (np.asarray(call) - np.asarray(put) -
            (s*np.exp(-state.dividend*state.expiry) -
             state.strike*np.exp(-state.rate*state.expiry)))


def curvature_energy(state, dspot, dvol, order=3, budget=1.0):
    """Mean squared nonlinear response of C+P / budget^2, not downside risk."""
    if order not in (2, 3) or not np.isfinite(budget) or budget <= 0:
        raise ValueError('Use order 2 or 3 and a finite positive budget.')
    _, quadratic, cubic = taylor_terms(state, dspot, dvol)
    nonlinear = 2 * (quadratic + (cubic if order == 3 else 0))
    return float(np.mean((nonlinear / budget)**2))


def numerical_derivative_check(state, scale=.5):
    """Independent degree-5 local polynomial fit for both option prices.

    Coordinates use spot scale 1% of S and volatility scale .01, both
    multiplied by scale. Returns maximum relative discrepancy across seven
    derivatives. This is a numerical diagnostic, not an exact proof.
    """
    powers = [(i, j) for i in range(6) for j in range(6-i)]
    targets = [(2,0), (1,1), (0,2), (3,0), (2,1), (1,2), (0,3)]
    names = ['gamma','vanna','vomma','speed','zomma','spot_vol_vol','ultima']
    x, y = np.meshgrid(np.linspace(-1, 1, 9), np.linspace(-1, 1, 9))
    x, y = x.ravel(), y.ravel()
    hs, hv = .01 * state.spot * scale, .01 * scale
    design = np.array([x**i * y**j for i, j in powers]).T
    values = prices(state, state.spot + hs*x, state.volatility + hv*y)
    analytic = np.array([greeks(state)[name] for name in names])
    result = {}
    for option, value in zip(('call', 'put'), values):
        co = np.linalg.lstsq(design, value, rcond=None)[0]
        numerical = np.array([co[powers.index((i,j))]*factorial(i)*factorial(j)
                              / (hs**i * hv**j) for i,j in targets])
        result[option] = float(np.max(np.abs(numerical - analytic) /
                                      np.maximum(np.abs(analytic), 1e-8)))
    return result


def run_tests(samples=100000, seed=20261001):
    if samples <= 0:
        raise ValueError('samples must be positive')
    state = State()
    print('SYNTHETIC BSM TEST; fixed expiry/carry; no historical observations')
    print('State:', state)
    print('Base call, put:', prices(state))
    print('Raw Greeks:', greeks(state))
    for step in (1., .5):
        check = numerical_derivative_check(state, step)
        print('Derivative fit step', step, 'max relative errors:', check)
        if step == .5:
            assert max(check.values()) < .001, 'Analytic derivative check failed'
    rng = np.random.default_rng(seed)
    z = rng.standard_normal((samples, 2))
    ds = 2 * z[:, 0]
    dv = .02 * (-.7*z[:, 0] + np.sqrt(1-.7**2)*z[:, 1])
    # Do not silently discard shocks outside the BSM domain.
    exact = prices(state, state.spot + ds, state.volatility + dv)
    print(f'\nMonte Carlo: n={samples}, seed={seed}; spot SD=2, vol SD=.02, corr=-.7')
    print('option order RMSE MAE p99_absolute_error negative_price_count')
    for side, option in enumerate(('call', 'put')):
        for order in (1, 2, 3):
            estimate = taylor_price(state, ds, dv, option, order)
            error = estimate - exact[side]
            print(option, order, f'{np.sqrt(np.mean(error**2)):.9f}',
                  f'{np.mean(abs(error)):.9f}', f'{np.quantile(abs(error), .99):.9f}',
                  int(np.count_nonzero(estimate < 0)))
    for order in (1, 2, 3):
        c = taylor_price(state, ds, dv, 'call', order)
        p = taylor_price(state, ds, dv, 'put', order)
        residual = np.max(abs(parity_residual(state, c, p, state.spot + ds)))
        assert residual < 1e-10, 'Taylor parity failed'
        print('Parity max residual, order', order, ':', residual)
    assert np.max(abs(parity_residual(state, *exact, state.spot+ds))) < 1e-10
    for order in (2, 3):
        print('Straddle curvature energy, order', order, '(budget=$1):',
              curvature_energy(state, ds, dv, order))
    print('\nStress: dspot dvol option exact first second third')
    for a, b in ((1,.01),(-5,.05),(-10,.10),(-20,.20),(-30,.30),(10,-.10)):
        stress = prices(state, state.spot+a, state.volatility+b)
        for side, option in enumerate(('call', 'put')):
            estimates = [taylor_price(state, a, b, option, o) for o in (1,2,3)]
            print(a, b, option, f'{stress[side]:.6f}',
                  *(f'{value:.6f}' for value in estimates))
    try:
        prices(state, spot=-5)
    except ValueError:
        print('\nNegative-price domain check: correctly rejected.')
    else:
        raise AssertionError('Negative spot must be rejected')
    print('Checks passed. Large-shock price-bound violations remain visible.')


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--samples', type=int, default=100000)
    parser.add_argument('--seed', type=int, default=20261001)
    args = parser.parse_args()
    run_tests(args.samples, args.seed)
