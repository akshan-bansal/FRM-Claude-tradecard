#!/usr/bin/env python3
"""bsm_spacetime.py -- greeks as one space-time jet, and Ito's lemma applied to it.

Research only: nothing on the order path imports this. It leaves ``bsm_event.py`` untouched, since
the live spectral gate versions itself by that file's hash.

``bsm_event`` takes a price's derivatives in the state (S, v) by truncated Taylor jets and handles
time separately, by repricing at a shorter expiry. Here time-to-expiry tau joins the state, so one
jet in (S, v, tau) carries every greek to fourth total order: delta, gamma, speed, vega, vanna,
volga, zomma, ultima, and the time ones, theta, charm, veta and color, out of the same tensors.

Ito's lemma then acts on that jet. For a state that moves as

    dS = b_S dt + ...,  dv = b_v dt + ...,  dtau = -dt,  with instantaneous covariance a(S, v),

the price changes by dP = (L P) dt + grad P . dX_martingale, with the generator

    L = -d/dtau + b_S d/dS + b_v d/dv + 1/2 a_SS d2/dS2 + a_Sv d2/dS dv + 1/2 a_vv d2/dv2.

Every coefficient is itself a jet, so L P is a jet again, two orders shorter. That gives:

* a check on the price formulas: under the risk-neutral constant-vol dynamics, L P = r P is the
  Black-Scholes PDE, so ``ito_residual`` is zero for a consistent formula and measures how far a
  smile parametrisation is from one;
* the horizon expansion E[P(t + h)] = P + h L P + h^2/2 L^2 P (Dynkin), and the variance from the
  same series applied to P^2, under any of the state dynamics below.

Calendar convention: tau is in years and t = T - tau, so a greek in calendar time is the tau
derivative with its sign flipped. Theta here is per year.
"""
from __future__ import annotations

import math
from dataclasses import dataclass

import bsm_event as be
import numpy as np

NAMES = ('S', 'v', 'tau')
S_, V_, T_ = 0, 1, 2

# name -> exponents of (S, v, tau). Time greeks are reported per unit of calendar time.
GREEKS = {
    'delta': (1, 0, 0), 'gamma': (2, 0, 0), 'speed': (3, 0, 0),
    'vega': (0, 1, 0), 'vanna': (1, 1, 0), 'volga': (0, 2, 0), 'zomma': (2, 1, 0), 'ultima': (0, 3, 0),
    'theta': (0, 0, 1), 'charm': (1, 0, 1), 'veta': (0, 1, 1), 'color': (2, 0, 1),
}


def _sqrt(x):
    return x.sqrt() if isinstance(x, be.JetN) else np.sqrt(x)


# ---------------------------------------------------------------------------
# 1. Prices with tau free to be a jet
# ---------------------------------------------------------------------------

def _digital(m, S, v, tau):
    """``BSMDigital.price`` with every operation jet-safe, tau included."""
    F = S * be._exp((m.rate - m.dividend) * tau)
    lnFK = be._log(F / m.strike)
    sig = v - m.skew * lnFK
    sq = _sqrt(tau)
    d2 = (lnFK - 0.5 * sig * sig * tau) / (sig * sq)
    return be._exp(-m.rate * tau) * (be._ncdf(d2) - be._npdf(d2) * sq * m.skew)


def _call(m, S, v, tau, strike):
    """``BSMDigital.call``: a smile-consistent vanilla call."""
    F = S * be._exp((m.rate - m.dividend) * tau)
    lnFK = be._log(F / strike)
    sig = v - m.skew * lnFK
    sq = _sqrt(tau)
    d1 = (lnFK + 0.5 * sig * sig * tau) / (sig * sq)
    d2 = d1 - sig * sq
    return be._exp(-m.rate * tau) * (F * be._ncdf(d1) - strike * be._ncdf(d2))


def price(model, S, v, tau):
    """The model's price with tau as an argument that may be a jet."""
    if isinstance(model, be.BSMDigital):
        return _digital(model, S, v, tau)
    if isinstance(model, be.SmileOption):
        base = model.base
        c = _call(base, S, v, tau, model.strike)
        if model.kind == 'call':
            return c
        F = S * be._exp((base.rate - base.dividend) * tau)
        return c - be._exp(-base.rate * tau) * (F - model.strike)
    if isinstance(model, be.LatentEvent):
        return be._ncdf((S - model.strike) / (v * _sqrt(tau)))
    if isinstance(model, be.Underlying):
        return S * 1.0 + 0.0 * tau
    raise TypeError(f'no space-time price for {type(model).__name__}')


def spacetime_jet(model, S, v, tau, deg=be.DEG):
    """The price as a jet in (S, v, tau) about the given point."""
    if not tau > 0:
        raise ValueError('tau must be positive: the contract has expired')
    X = be.JetN.variables((float(S), float(v), float(tau)), deg)
    return price(model, *X), X


def greeks(model, S, v, tau, deg=be.DEG):
    """Named greeks from one jet. Time greeks are per year of calendar time (d/dt = -d/dtau)."""
    J, _ = spacetime_jet(model, S, v, tau, deg)
    return {name: J.derivative(e) * (-1) ** e[T_] for name, e in GREEKS.items() if sum(e) <= deg}


def tensors(model, S, v, tau, deg=be.DEG):
    """Every derivative tensor of order 1..deg in (S, v, tau): the whole space-time picture."""
    J, _ = spacetime_jet(model, S, v, tau, deg)
    return {k: J.tensor(k) for k in range(1, deg + 1)}


# ---------------------------------------------------------------------------
# 2. Jet calculus: the partial derivative of a jet is a jet
# ---------------------------------------------------------------------------

def partial(J, i):
    """d J / d x_i as a jet of the same shape. It is exact to one degree less than J."""
    monos, index = be._tables(J.n, J.deg)[:2]
    out = np.zeros_like(J.c)
    for k, m in enumerate(monos):
        if m[i] and J.c[k]:
            lower = list(m)
            lower[i] -= 1
            out[index[tuple(lower)]] += J.c[k] * m[i]
    return J._like(out)


# ---------------------------------------------------------------------------
# 3. State dynamics
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class StateDynamics:
    """How (S, v) move, as drift and instantaneous covariance per year. Works on floats or jets.

    kind
      'pricing'       risk-neutral, constant vol: dS = (r - q) S dt + v S dW, v fixed. With
                      ``discount = r`` the generator gives the Black-Scholes PDE.
      'random_walk'   what ``var_snapshot`` assumes: v moves arithmetically, sd ``eta`` per sqrt year,
                      no drift.
      'log_ou'        d ln v = kappa (theta - ln v) dt + eta dW_v: mean reversion, vol-of-vol
                      proportional to the level (exponent 1).
      'three_halves'  dv = kappa v (theta - v) dt + eta v^1.5 dW_v: vol-of-vol exponent 1.5.
    Spot: dS = mu S dt + scale v S dW_S, corr(dW_S, dW_v) = rho. ``scale`` maps the implied-vol
    state to the instantaneous spot vol.
    """
    kind: str = 'random_walk'
    mu: float = 0.0
    scale: float = 1.0
    kappa: float = 0.0
    theta: float = 0.0
    eta: float = 0.0
    rho: float = 0.0
    rate: float = 0.0
    dividend: float = 0.0
    discount: float = 0.0

    def coefficients(self, S, v):
        """(b_S, b_v, a_SS, a_Sv, a_vv) at the state; jets in, jets out."""
        if self.kind == 'pricing':
            return ((self.rate - self.dividend) * S, 0.0 * v, v * v * S * S, 0.0 * v, 0.0 * v)
        spot_sd = self.scale * v * S
        if self.kind == 'random_walk':
            vol_drift, vol_sd = 0.0 * v, self.eta + 0.0 * v
        elif self.kind == 'log_ou':
            vol_drift = v * (self.kappa * (self.theta - be._log(v)) + 0.5 * self.eta * self.eta)
            vol_sd = self.eta * v
        elif self.kind == 'three_halves':
            vol_drift = self.kappa * v * (self.theta - v)
            vol_sd = self.eta * v * _sqrt(v)
        else:
            raise ValueError(f'unknown dynamics {self.kind!r}')
        return (self.mu * S, vol_drift, spot_sd * spot_sd, self.rho * spot_sd * vol_sd, vol_sd * vol_sd)


def generator(J, X, dyn):
    """L J: Ito's lemma's drift term for the jet J under ``dyn``, as a jet exact to two degrees less."""
    S, v, _ = X
    bS, bv, aSS, aSv, avv = dyn.coefficients(S, v)
    dS, dv, dtau = partial(J, S_), partial(J, V_), partial(J, T_)
    out = (-1.0) * dtau + bS * dS + bv * dv + 0.5 * aSS * partial(dS, S_) + aSv * partial(dS, V_) \
        + 0.5 * avv * partial(dv, V_)
    return out - dyn.discount * J if dyn.discount else out


def carre_du_champ(J, X, dyn):
    """grad J' a grad J: the instantaneous variance rate of J, as a jet exact to one degree less."""
    S, v, _ = X
    _, _, aSS, aSv, avv = dyn.coefficients(S, v)
    dS, dv = partial(J, S_), partial(J, V_)
    return aSS * dS * dS + 2.0 * aSv * dS * dv + avv * dv * dv


def pricing_dynamics(model):
    """Risk-neutral constant-vol dynamics with the model's own rate and dividend."""
    base = model.base if isinstance(model, be.SmileOption) else model
    return StateDynamics(kind='pricing', rate=getattr(base, 'rate', 0.0),
                         dividend=getattr(base, 'dividend', 0.0), discount=getattr(base, 'rate', 0.0))


def ito_residual(model, S, v, tau):
    """L P - r P under the risk-neutral constant-vol dynamics: zero for a Black-Scholes-consistent price.

    A smile parametrisation that moves the vol with log-moneyness is not one in general, and this
    measures by how much, in price units per year."""
    J, X = spacetime_jet(model, S, v, tau, deg=2)
    return float(generator(J, X, pricing_dynamics(model)).value)


# ---------------------------------------------------------------------------
# 4. Horizon expansion
# ---------------------------------------------------------------------------

def horizon_moments(model, S, v, tau, dyn, h):
    """Mean and variance of the price change over ``h`` years, to second order in h (Dynkin).

    E[P_h] - P = h L P + h^2/2 L^2 P, and Var = h G + h^2/2 (L^2 (P^2) - 2 (L P)^2 - 2 P L^2 P)
    with G the carre du champ. Needs the fourth-order jet, since L^2 takes four derivatives."""
    if not h < tau:
        raise ValueError('the horizon must end before expiry')
    J, X = spacetime_jet(model, S, v, tau, deg=4)
    LP = generator(J, X, dyn)
    LLP = generator(LP, X, dyn)
    P2 = J * J
    LLP2 = generator(generator(P2, X, dyn), X, dyn)
    p, lp, llp = float(J.value), float(LP.value), float(LLP.value)
    mean = h * lp + 0.5 * h * h * llp
    var = h * float(carre_du_champ(J, X, dyn).value) + 0.5 * h * h * (float(LLP2.value) - 2 * lp * lp - 2 * p * llp)
    return {'mean': mean, 'var': var, 'sd': math.sqrt(max(var, 0.0)), 'drift_rate': lp,
            'variance_rate': float(carre_du_champ(J, X, dyn).value)}


# ---------------------------------------------------------------------------
# 5. Fitting the vol dynamics to an index, and simulating them
# ---------------------------------------------------------------------------

def _normal_loglik(resid, sd):
    sd = np.asarray(sd, dtype=float)
    return float(np.sum(-0.5 * np.log(2 * np.pi * sd * sd) - 0.5 * (resid / sd) ** 2))


def fit_log_ou(v, dt):
    """Exact AR(1) in ln v. Log-likelihood is for v itself, so it compares with ``fit_three_halves``."""
    x = np.log(np.asarray(v, dtype=float))
    X = np.column_stack([np.ones(len(x) - 1), x[:-1]])
    (a, b), *_ = np.linalg.lstsq(X, x[1:], rcond=None)
    b = float(np.clip(b, 1e-6, 1 - 1e-9))
    resid = x[1:] - (a + b * x[:-1])
    s = float(resid.std(ddof=2))
    kappa = -math.log(b) / dt
    return {'kind': 'log_ou', 'kappa': kappa, 'theta': float(a / (1 - b)), 'eta': s * math.sqrt(2 * kappa / (1 - b * b)),
            'persistence': b, 'loglik': _normal_loglik(resid, s) - float(np.sum(x[1:])), 'shocks': resid / s}


def fit_three_halves(v, dt):
    """Gaussian quasi-likelihood of dv = kappa v (theta - v) dt + eta v^1.5 dW over one step."""
    v = np.asarray(v, dtype=float)
    w, dv = v[:-1], np.diff(v)
    scale = w ** 1.5
    X = np.column_stack([dt / np.sqrt(w), dt * np.sqrt(w)])            # dv / w^1.5 = kappa theta w^-0.5 dt - kappa w^0.5 dt
    (c1, c2), *_ = np.linalg.lstsq(X, dv / scale, rcond=None)
    kappa = max(-float(c2), 1e-9)
    resid = dv / scale - X @ np.array([c1, c2])
    eta = float(resid.std(ddof=2)) / math.sqrt(dt)
    sd = eta * scale * math.sqrt(dt)
    return {'kind': 'three_halves', 'kappa': kappa, 'theta': float(c1) / kappa, 'eta': eta,
            # the linearised pull towards theta, kappa theta per year, gives a comparable persistence
            'persistence': math.exp(-kappa * float(c1) / kappa * dt), 'loglik': _normal_loglik(dv - (X @ np.array([c1, c2])) * scale, sd),
            'shocks': resid / (eta * math.sqrt(dt))}


def fit_random_walk(v, dt):
    """What ``var_snapshot`` assumes: dv arithmetic, no drift."""
    dv = np.diff(np.asarray(v, dtype=float))
    s = float(dv.std(ddof=1))
    return {'kind': 'random_walk', 'kappa': 0.0, 'theta': 0.0, 'eta': s / math.sqrt(dt), 'persistence': 1.0,
            'loglik': _normal_loglik(dv - dv.mean(), s), 'shocks': (dv - dv.mean()) / s}


FITTERS = {'random_walk': fit_random_walk, 'log_ou': fit_log_ou, 'three_halves': fit_three_halves}


def fit_dynamics(S, v, dt, kind):
    """A ``StateDynamics`` for the spot and vol index together. Spot drift is set to zero."""
    fit = FITTERS[kind](v, dt)
    r = np.diff(np.log(np.asarray(S, dtype=float)))
    v = np.asarray(v, dtype=float)
    scale = math.sqrt(float(np.mean(r * r)) / float(np.mean(v[:-1] ** 2 * dt)))
    rho = float(np.corrcoef(r / v[:-1], fit['shocks'])[0, 1])
    dyn = StateDynamics(kind=kind, mu=0.0, scale=scale, kappa=fit['kappa'], theta=fit['theta'], eta=fit['eta'], rho=rho)
    return dyn, fit


def vol_of_vol_exponent(v):
    """Slope of ln|dv| on ln v: 0 for a random walk, 1 for log-OU, 1.5 for the 3/2 model."""
    v = np.asarray(v, dtype=float)
    dv = np.abs(np.diff(v))
    ok = dv > 0
    return float(np.polyfit(np.log(v[:-1][ok]), np.log(dv[ok]), 1)[0])


def variance_ratio(x, k):
    x = np.asarray(x, dtype=float)
    return float(np.var(x[k:] - x[:-k]) / (k * np.var(np.diff(x))))


def simulate(dyn, S0, v0, h, n_paths=20000, n_steps=20, seed=0):
    """Paths of (S, v) to ``h`` years: log-Euler for S, and positivity kept for v by stepping in ln v."""
    rng = np.random.default_rng(seed)
    dt = h / n_steps
    S = np.full(n_paths, float(S0))
    v = np.full(n_paths, float(v0))
    for _ in range(n_steps):
        z1 = rng.standard_normal(n_paths)
        z2 = dyn.rho * z1 + math.sqrt(max(1 - dyn.rho ** 2, 0.0)) * rng.standard_normal(n_paths)
        spot_vol = dyn.scale * v
        S = S * np.exp((dyn.mu - 0.5 * spot_vol ** 2) * dt + spot_vol * math.sqrt(dt) * z1)
        if dyn.kind == 'random_walk':
            v = np.maximum(v + dyn.eta * math.sqrt(dt) * z2, be.STATE_FLOOR)
        elif dyn.kind == 'log_ou':
            lv = np.log(v)
            v = np.exp(lv + dyn.kappa * (dyn.theta - lv) * dt + dyn.eta * math.sqrt(dt) * z2)
        elif dyn.kind == 'three_halves':
            # d ln v = (kappa (theta - v) - eta^2 v / 2) dt + eta sqrt(v) dW
            v = v * np.exp((dyn.kappa * (dyn.theta - v) - 0.5 * dyn.eta ** 2 * v) * dt + dyn.eta * np.sqrt(v * dt) * z2)
        else:
            raise ValueError(f'cannot simulate {dyn.kind!r}')
    return S, v


def monte_carlo_moments(model, S, v, tau, dyn, h, **kw):
    """Mean, sd and lower 1% quantile of the price change over h, by simulation and exact repricing."""
    ST, vT = simulate(dyn, S, v, h, **kw)
    dP = model.price(ST, np.maximum(vT, be.STATE_FLOOR), tau - h) - model.price(S, v, tau)
    return {'mean': float(dP.mean()), 'sd': float(dP.std(ddof=1)), 'q01': float(np.quantile(dP, 0.01)),
            'se_mean': float(dP.std(ddof=1) / math.sqrt(len(dP)))}
