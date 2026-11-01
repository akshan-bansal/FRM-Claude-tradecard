#!/usr/bin/env python3
"""bsm_event.py -- spectral Value-at-Risk engine for binary (digital) event contracts.

Reconstruction of the core module described in the draft paper "Spectral Value-at-Risk for
Binary Event Contracts: An Analysis of the BSM Code Base". Rebuilt from the paper's text; the
paper's demo numbers came from the original code and stylised inputs, so they will not be
reproduced digit for digit.

A contract price is a smooth function of a two-vector state:
  bsm     "index above K at T": state (S, v), v = ATM implied vol (VIX proxy), linear smile
          sigma_K = v - beta*ln(F/K); digital = exp(-rT)[N(d2(sigma_K)) - phi(d2) sqrt(T) beta]
  latent  no tradable underlying: state (x, s), price = N((x - K)/(s sqrt(tau)))

Exact truncated-Taylor jets give derivative tensors to fourth order. Whitening by the shock
covariance gives principal curvatures, risk directions and Gaussian energy by order; a series
trust gate decides when to reprice. Around that core: GJR-GARCH state, semi-parametric GPD
tails, walk-forward VaR backtest with Kupiec / Christoffersen / Berkowitz tests, PID and CUSUM
control loops, hierarchical vs complete-sum hedging, compositional (Aitchison) powering, and
probability calibration / event-market tests.

Install: python -m pip install numpy scipy
Run:     python bsm_event.py --selftest
         python bsm_event.py --demo
         python bsm_event.py --backtest --synthetic
         python bsm_event.py --backtest --data data            (SP500.csv + VIXCLS.csv)
         python bsm_event.py --backtest --data data --underlying BTC   (any registered underlying)
         python bsm_event.py --backtest --data data --signals daily_signals.csv
         python bsm_event.py --events-synthetic
         python bsm_event.py --events contracts.csv --data data

Data files are date,value CSVs (FRED / Yahoo style; '.' or blank = missing). Paths are relative
to the working directory -- no drive letters. Synthetic data exercises every code path but says
nothing about real-market performance. Digital P&L in the backtest is marked to model (there is
no digital price history), so coverage tests evaluate the model against itself plus realised
index and VIX moves.
"""
from __future__ import annotations

import argparse
import csv
import math
import os
import sys
from dataclasses import dataclass, field, replace
from datetime import datetime
from decimal import Decimal, localcontext
from functools import lru_cache
from itertools import combinations_with_replacement, product
from math import factorial

import numpy as np
from numpy.polynomial.hermite_e import hermegauss
from scipy import optimize, stats
from scipy.signal import lfilter
from scipy.special import expit, gammaln, logit, ndtr, ndtri, xlogy

DEG = 4
TRADING_DAYS = 252
SQRT2PI = math.sqrt(2.0 * math.pi)
LOG2PI = math.log(2.0 * math.pi)
GAUSS_99_RADIUS = math.sqrt(-2.0 * math.log(0.01))  # 3.0349: 99% circle of a 2-D standard normal
STATE_FLOOR = 1e-4  # second state component (vol / intensity) is kept positive when repricing


# ---------------------------------------------------------------------------
# 1. Jets: exact truncated Taylor arithmetic
# ---------------------------------------------------------------------------

def _monomials(n, deg):
    """Exponent tuples in n variables with total degree <= deg, graded by degree."""
    out = []
    for d in range(deg + 1):
        for combo in combinations_with_replacement(range(n), d):
            e = [0] * n
            for v in combo:
                e[v] += 1
            out.append(tuple(e))
    return out


@lru_cache(maxsize=None)
def _tables(n, deg):
    monos = _monomials(n, deg)
    index = {m: i for i, m in enumerate(monos)}
    ia, ib, ic = [], [], []
    for i, a in enumerate(monos):
        da = sum(a)
        for j, b in enumerate(monos):
            if da + sum(b) <= deg:
                ia.append(i)
                ib.append(j)
                ic.append(index[tuple(x + y for x, y in zip(a, b))])
    E = np.array(monos, dtype=int).reshape(len(monos), n)
    fact = np.array([math.prod(factorial(e) for e in m) for m in monos], dtype=float)
    return monos, index, np.array(ia), np.array(ib), np.array(ic), E, fact, E.sum(axis=1)


def _jet_mul(a, b, n, deg):
    _, _, ia, ib, ic, *_ = _tables(n, deg)
    return np.bincount(ic, weights=a[ia] * b[ib], minlength=len(a))


def _hermite_e(x, kmax):
    """Probabilists' Hermite polynomials He_0..He_kmax at x."""
    h = [1.0, x]
    for k in range(1, kmax):
        h.append(x * h[k] - k * h[k - 1])
    return h[:kmax + 1]


def _npdf_derivs(x, deg):
    """phi^(k)(x) = (-1)^k He_k(x) phi(x), k = 0..deg."""
    phi = math.exp(-0.5 * x * x) / SQRT2PI
    he = _hermite_e(x, max(deg, 1))
    return [(-1) ** k * he[k] * phi for k in range(deg + 1)]


def _ncdf_derivs(x, deg):
    """Phi^(k) = phi^(k-1) for k >= 1."""
    return [float(ndtr(x))] + _npdf_derivs(x, max(deg - 1, 0))[:deg]


class JetN:
    """Truncated Taylor polynomial in n variables, total degree <= deg.

    c[k] is the coefficient of monomial monos[k]; the partial derivative with exponents e is
    c[k] * prod(e_i!). Arithmetic propagates derivatives exactly through a pricing formula.
    """
    __array_ufunc__ = None  # numpy scalars defer to our reflected operators

    def __init__(self, c, n, deg=DEG):
        self.n, self.deg = int(n), int(deg)
        self.c = np.asarray(c, dtype=float)

    @classmethod
    def _make(cls, c, n, deg):
        obj = cls.__new__(cls)
        obj.n, obj.deg, obj.c = n, deg, c
        return obj

    @classmethod
    def const(cls, value, n, deg=DEG):
        c = np.zeros(len(_tables(n, deg)[0]))
        c[0] = value
        return cls._make(c, n, deg)

    @classmethod
    def variables(cls, point, deg=DEG):
        """One jet per coordinate: x_i = point_i + dx_i."""
        n = len(point)
        index = _tables(n, deg)[1]
        out = []
        for i, x0 in enumerate(point):
            c = np.zeros(len(index))
            c[0] = x0
            if deg >= 1:
                e = [0] * n
                e[i] = 1
                c[index[tuple(e)]] = 1.0
            out.append(cls._make(c, n, deg))
        return out

    def _like(self, c):
        return type(self)._make(c, self.n, self.deg)

    def _other(self, o):
        if isinstance(o, JetN):
            if (o.n, o.deg) != (self.n, self.deg):
                raise ValueError('jets differ in dimension or degree')
            return o.c
        return None

    def __add__(self, o):
        oc = self._other(o)
        if oc is None:
            c = self.c.copy()
            c[0] += o
            return self._like(c)
        return self._like(self.c + oc)

    __radd__ = __add__

    def __neg__(self):
        return self._like(-self.c)

    def __sub__(self, o):
        return self + (-o)

    def __rsub__(self, o):
        return (-self) + o

    def __mul__(self, o):
        oc = self._other(o)
        if oc is None:
            return self._like(self.c * o)
        return self._like(_jet_mul(self.c, oc, self.n, self.deg))

    __rmul__ = __mul__

    def __truediv__(self, o):
        if isinstance(o, JetN):
            return self * o.reciprocal()
        return self._like(self.c / o)

    def __rtruediv__(self, o):
        return self.reciprocal() * o

    def __pow__(self, p):
        if isinstance(p, (int, np.integer)) and p >= 0:
            out = self.const(1.0, self.n, self.deg)
            for _ in range(int(p)):
                out = out * self
            return out
        x0, d, coef = self.c[0], [], 1.0
        d.append(x0 ** p)
        for k in range(1, self.deg + 1):
            coef *= p - k + 1
            d.append(coef * x0 ** (p - k))
        return self.compose(d)

    def compose(self, derivs):
        """f(self) given derivs[k] = f^(k)(value), k = 0..deg."""
        du = self.c.copy()
        du[0] = 0.0
        out = np.zeros_like(self.c)
        out[0] = derivs[0]
        pw = np.zeros_like(self.c)
        pw[0] = 1.0
        for k in range(1, self.deg + 1):
            pw = _jet_mul(pw, du, self.n, self.deg)
            out += derivs[k] / factorial(k) * pw
        return self._like(out)

    def reciprocal(self):
        x0 = self.c[0]
        return self.compose([(-1) ** k * factorial(k) / x0 ** (k + 1) for k in range(self.deg + 1)])

    def exp(self):
        return self.compose([math.exp(self.c[0])] * (self.deg + 1))

    def log(self):
        x0 = self.c[0]
        return self.compose([math.log(x0)] + [(-1) ** (k - 1) * factorial(k - 1) / x0 ** k
                                              for k in range(1, self.deg + 1)])

    def sqrt(self):
        return self ** 0.5

    def ncdf(self):
        return self.compose(_ncdf_derivs(self.c[0], self.deg))

    def npdf(self):
        return self.compose(_npdf_derivs(self.c[0], self.deg))

    # -- derivative information ------------------------------------------------
    @property
    def value(self):
        return float(self.c[0])

    def derivative(self, exps):
        _, index, *_, fact, _ = _tables(self.n, self.deg)
        k = index[tuple(int(e) for e in exps)]
        return float(self.c[k] * fact[k])

    def gradient(self):
        return np.array([self.derivative(np.eye(self.n, dtype=int)[i]) for i in range(self.n)])

    def hessian(self):
        eye = np.eye(self.n, dtype=int)
        return np.array([[self.derivative(eye[i] + eye[j]) for j in range(self.n)]
                         for i in range(self.n)])

    def tensor(self, k):
        """Symmetric k-th derivative tensor."""
        T = np.zeros((self.n,) * k)
        for idx in product(range(self.n), repeat=k):
            e = [0] * self.n
            for v in idx:
                e[v] += 1
            T[idx] = self.derivative(e)
        return T

    def truncate(self, order):
        c = self.c.copy()
        c[_tables(self.n, self.deg)[7] > order] = 0.0
        return self._like(c)

    def terms(self, z):
        """Per-order Taylor terms t_k(z), k = 0..deg; returns shape (deg+1, m)."""
        z = np.atleast_2d(np.asarray(z, dtype=float))
        monos, *_, degree = _tables(self.n, self.deg)
        powers = np.arange(self.deg + 1)
        pw = [z[:, v:v + 1] ** powers for v in range(self.n)]
        out = np.zeros((self.deg + 1, len(z)))
        for k, e in enumerate(monos):
            if self.c[k] == 0.0:
                continue
            val = np.full(len(z), self.c[k])
            for v, ev in enumerate(e):
                if ev:
                    val = val * pw[v][:, ev]
            out[degree[k]] += val
        return out

    def __call__(self, z):
        return self.terms(z).sum(axis=0)


class Jet(JetN):
    """Bivariate jet: c2[i, j] is the coefficient of a^i b^j for state shocks (a, b)."""

    def __init__(self, c, n=2, deg=DEG):
        super().__init__(c, 2, deg)

    @classmethod
    def const(cls, value, n=2, deg=DEG):
        return super().const(value, 2, deg)

    @property
    def c2(self):
        out = np.zeros((self.deg + 1, self.deg + 1))
        for k, (i, j) in enumerate(_tables(2, self.deg)[0]):
            out[i, j] = self.c[k]
        return out


def _exp(x):
    return x.exp() if isinstance(x, JetN) else np.exp(x)


def _log(x):
    return x.log() if isinstance(x, JetN) else np.log(x)


def _ncdf(x):
    return x.ncdf() if isinstance(x, JetN) else ndtr(x)


def _npdf(x):
    return x.npdf() if isinstance(x, JetN) else np.exp(-0.5 * x * x) / SQRT2PI


def _gauss_moment_1d(e):
    return 0.0 if e % 2 else float(math.prod(range(e - 1, 0, -2)))


@lru_cache(maxsize=None)
def _gaussian_gram(n, deg):
    monos = _tables(n, deg)[0]
    M = len(monos)
    G = np.empty((M, M))
    for a in range(M):
        for b in range(M):
            G[a, b] = math.prod(_gauss_moment_1d(x + y) for x, y in zip(monos[a], monos[b]))
    return G


def monomial_gram(n, deg, draws=None):
    """G[a, b] = E[z^a z^b] under N(0, I) (exact) or under the empirical law of `draws`."""
    if draws is None:
        return _gaussian_gram(n, deg)
    z = np.asarray(draws, dtype=float)
    E = _tables(n, deg)[5]
    pw = [z[:, v:v + 1] ** np.arange(2 * deg + 1) for v in range(n)]
    M = len(E)
    G = np.empty((M, M))
    for a in range(M):
        for b in range(a, M):
            e = E[a] + E[b]
            val = np.ones(len(z))
            for v in range(n):
                if e[v]:
                    val = val * pw[v][:, e[v]]
            G[a, b] = G[b, a] = val.mean()
    return G


def energy_by_order(J, draws=None):
    """E[t_k(z)^2] for k = 1..deg, z ~ N(0, I) unless draws are given."""
    G = monomial_gram(J.n, J.deg, draws)
    degree = _tables(J.n, J.deg)[7]
    out = []
    for k in range(1, J.deg + 1):
        ck = np.where(degree == k, J.c, 0.0)
        out.append(float(ck @ G @ ck))
    return np.array(out)


def gauss_hermite_2d(n=12):
    """Nodes and weights for E[f(z)], z ~ N(0, I_2); exact for polynomials of degree < 2n."""
    x, w = hermegauss(n)
    w = w / SQRT2PI
    X, Y = np.meshgrid(x, x, indexing='ij')
    return np.column_stack([X.ravel(), Y.ravel()]), np.outer(w, w).ravel()


# ---------------------------------------------------------------------------
# 2. Contract models
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class BSMDigital:
    """'Index above K at T' under BSM with a linear-in-log-moneyness smile; state (S, v).

    sigma_K = v - skew * ln(F/K), so d sigma / d ln K = skew (negative for equity skew).
    """
    strike: float
    expiry: float  # years
    rate: float = 0.04
    dividend: float = 0.015
    skew: float = -0.3
    names = ('S', 'v')

    def _parts(self, S, v, T, K):
        F = S * math.exp((self.rate - self.dividend) * T)
        lnFK = _log(F / K)
        sig = v - self.skew * lnFK
        return F, lnFK, sig, math.sqrt(T)

    def probability(self, S, v, tau=None):
        """Risk-neutral probability p_Q = N(d2) - phi(d2) sqrt(T) beta (undiscounted)."""
        T = self.expiry if tau is None else tau
        if T <= 0:
            raise ValueError('contract has expired')
        _, lnFK, sig, sq = self._parts(S, v, T, self.strike)
        d2 = (lnFK - 0.5 * sig * sig * T) / (sig * sq)
        return _ncdf(d2) - _npdf(d2) * sq * self.skew

    def price(self, S, v, tau=None):
        T = self.expiry if tau is None else tau
        return math.exp(-self.rate * T) * self.probability(S, v, T)

    def call(self, S, v, strike=None, tau=None):
        """Smile-consistent vanilla call at `strike` (the digital is minus its strike derivative)."""
        T = self.expiry if tau is None else tau
        K = self.strike if strike is None else strike
        F, lnFK, sig, sq = self._parts(S, v, T, K)
        d1 = (lnFK + 0.5 * sig * sig * T) / (sig * sq)
        d2 = d1 - sig * sq
        return math.exp(-self.rate * T) * (F * _ncdf(d1) - K * _ncdf(d2))


@dataclass(frozen=True)
class SmileOption:
    """Vanilla call or put priced off a digital's smile; used by the call-put hedge."""
    base: BSMDigital
    strike: float
    kind: str = 'call'
    names = ('S', 'v')

    @property
    def expiry(self):
        return self.base.expiry

    def price(self, S, v, tau=None):
        T = self.base.expiry if tau is None else tau
        c = self.base.call(S, v, self.strike, T)
        if self.kind == 'call':
            return c
        F = S * math.exp((self.base.rate - self.base.dividend) * T)
        return c - math.exp(-self.base.rate * T) * (F - self.strike)


@dataclass(frozen=True)
class LatentEvent:
    """Event with no tradable underlying: latent Gaussian score x, information intensity s.

    price = N((x - K) / (s sqrt(tau))); with dx = s dW the price is a martingale.
    """
    strike: float = 0.0
    expiry: float = 30.0  # time to resolution, same units as the intensity (days here)
    names = ('x', 's')

    def probability(self, x, s, tau=None):
        T = self.expiry if tau is None else tau
        if T <= 0:
            raise ValueError('event has resolved')
        return _ncdf((x - self.strike) / (s * math.sqrt(T)))

    price = probability

    def score(self, p, s, tau=None):
        """x = K + s sqrt(tau) Phi^-1(p): the market price pins down the latent score."""
        T = self.expiry if tau is None else tau
        return self.strike + s * math.sqrt(T) * ndtri(p)


def estimate_intensity(p, tau, dt=1.0, eps=1e-6):
    """Realised variance per unit time of w = sqrt(tau) Phi^-1(p).

    w is the latent score in units of the market's own intensity, so its increments have
    variance 1 per unit time when information arrives uniformly at the priced rate; sqrt(rv)
    is the intensity relative to that normalisation.
    """
    w = np.sqrt(np.asarray(tau, float)) * ndtri(np.clip(np.asarray(p, float), eps, 1 - eps))
    dw = np.diff(w)
    rv = float(np.mean(dw ** 2) / dt)
    return {'rv_per_time': rv, 's': math.sqrt(rv), 'n': len(dw)}


@dataclass(frozen=True)
class Underlying:
    """The underlying itself as a hedge instrument (bsm state)."""
    expiry: float = math.inf
    names = ('S', 'v')

    def price(self, S, v, tau=None):
        return S * 1.0


# ---------------------------------------------------------------------------
# 3. Spectral analysis of curvature risk
# ---------------------------------------------------------------------------

def cov_factor(Sigma):
    """Cholesky-type factor L with L L' = Sigma (eigen square root if not positive definite)."""
    Sigma = np.asarray(Sigma, dtype=float)
    try:
        return np.linalg.cholesky(Sigma)
    except np.linalg.LinAlgError:
        lam, V = np.linalg.eigh((Sigma + Sigma.T) / 2)
        return V @ np.diag(np.sqrt(np.maximum(lam, 0.0)))


def pnl_jet(model, state0, L, horizon=0.0, position=1.0, deg=DEG):
    """P&L over the horizon as a jet in whitened shocks z (state = state0 + L z)."""
    a, b = Jet.variables((0.0, 0.0), deg)
    X = state0[0] + L[0, 0] * a + L[0, 1] * b
    Y = state0[1] + L[1, 0] * a + L[1, 1] * b
    p0 = model.price(state0[0], state0[1])
    return (model.price(X, Y, model.expiry - horizon) - p0) * position


def exact_pnl(model, state0, L, z, horizon=0.0, position=1.0):
    """Exact repricing at state0 + L z for whitened shocks z of shape (m, 2)."""
    z = np.atleast_2d(np.asarray(z, dtype=float))
    x = np.asarray(state0, float)[None, :] + z @ np.asarray(L).T
    second = np.maximum(x[:, 1], STATE_FLOOR)
    p0 = model.price(state0[0], state0[1])
    return position * (model.price(x[:, 0], second, model.expiry - horizon) - p0)


def _profile_stats(theta, q):
    """Peak direction, anisotropy (peak |q| / rms) and count of stationary points (Z-eigenpairs)."""
    rms = float(np.sqrt(np.mean(q * q)))
    i = int(np.argmax(np.abs(q)))
    dq = np.roll(q, -1) - q
    stationary = int(np.sum(np.sign(dq) != np.sign(np.roll(dq, 1))))
    return {'theta_deg': math.degrees(theta[i]), 'value': float(q[i]),
            'anisotropy': float(abs(q[i]) / rms) if rms > 0 else float('nan'),
            'z_eigenpairs': stationary}


@dataclass
class RiskVector:
    price: float
    carry: float
    gradient: np.ndarray
    curvatures: np.ndarray
    directions: np.ndarray
    energy: np.ndarray
    energy2_closed: float
    tensor_peaks: dict
    t4_flat_spectrum: np.ndarray
    radius: float
    stress_theta: float
    worst_loss: float
    ray_terms: np.ndarray
    taylor4_loss: float
    ratio_gate: bool
    repriced_gate: bool
    action: str
    crossing: bool
    crossing_theta: float | None
    jet: JetN = field(repr=False)
    L: np.ndarray = field(repr=False)


def risk_vector(model, state0, Sigma, horizon=0.0, position=1.0, conf=0.99, radius=None,
                tol=0.05, crossing_frac=0.5, crossing_sep=30.0, n_theta=720, deg=DEG):
    """Spectral risk vector with series-trust gate.

    Whitened curvature M = L'HL gives principal curvatures (eigenvalues) and principal risk
    directions (eigenvectors). Orders 3..4 are summarised by their angular profiles on the unit
    circle. The confidence circle of `radius` is repriced exactly to find the stress ray; the
    ratio gate asks |t3| < |t2| and |t4| < |t3| on that ray, the repriced gate asks the Taylor-4
    error there to be within `tol` of the worst loss and sets the action. A second loss maximum
    at least `crossing_sep` degrees away reaching `crossing_frac` of the worst loss flags a
    crossing: a single stress ray misrepresents the risk.
    """
    state0 = np.asarray(state0, dtype=float)
    L = cov_factor(Sigma)
    J = pnl_jet(model, state0, L, horizon, position, deg)
    M = J.hessian()
    lam, Q = np.linalg.eigh(M)
    energy = energy_by_order(J)
    e2_closed = 0.25 * (np.trace(M) ** 2 + 2.0 * np.trace(M @ M))

    theta = np.linspace(0.0, 2.0 * math.pi, n_theta, endpoint=False)
    U = np.column_stack([np.cos(theta), np.sin(theta)])
    unit_terms = J.terms(U)
    peaks = {k: _profile_stats(theta, unit_terms[k]) for k in range(3, deg + 1)}
    flat = np.linalg.eigvalsh(J.tensor(4).reshape(4, 4)) if deg >= 4 else np.array([])

    r = GAUSS_99_RADIUS if (radius is None and conf == 0.99) else (
        radius if radius is not None else math.sqrt(-2.0 * math.log(1.0 - conf)))

    def loss_at(th):
        th = np.atleast_1d(th)
        return -exact_pnl(model, state0, L, r * np.column_stack([np.cos(th), np.sin(th)]),
                          horizon, position)

    losses = loss_at(theta)
    i = int(np.argmax(losses))
    step = 2.0 * math.pi / n_theta
    res = optimize.minimize_scalar(lambda t: -loss_at(t)[0], method='bounded',
                                   bounds=(theta[i] - step, theta[i] + step),
                                   options={'xatol': 1e-10})
    th_star, worst = (float(res.x), float(-res.fun)) if -res.fun >= losses[i] else (theta[i], losses[i])
    z_star = r * np.array([[math.cos(th_star), math.sin(th_star)]])
    t = J.terms(z_star)[:, 0]
    taylor_loss = float(-t.sum())
    ratio_gate = bool(deg >= 4 and abs(t[3]) < abs(t[2]) and abs(t[4]) < abs(t[3]))
    repriced_gate = bool(abs(taylor_loss - worst) <= tol * max(abs(worst), 1e-12))

    is_max = (losses >= np.roll(losses, 1)) & (losses >= np.roll(losses, -1))
    sep = np.abs((np.degrees(theta - th_star) + 180.0) % 360.0 - 180.0)
    cand = np.where(is_max & (sep >= crossing_sep))[0]
    crossing, crossing_theta = False, None
    if len(cand) and worst > 0:
        j = cand[np.argmax(losses[cand])]
        if losses[j] >= crossing_frac * worst:
            crossing, crossing_theta = True, math.degrees(theta[j])

    return RiskVector(
        price=float(model.price(state0[0], state0[1])), carry=float(J.c[0]),
        gradient=J.gradient(), curvatures=lam, directions=Q, energy=energy,
        energy2_closed=float(e2_closed), tensor_peaks=peaks, t4_flat_spectrum=flat, radius=r,
        stress_theta=math.degrees(th_star) % 360.0, worst_loss=worst, ray_terms=t,
        taylor4_loss=taylor_loss, ratio_gate=ratio_gate, repriced_gate=repriced_gate,
        action='taylor4' if repriced_gate else 'reprice', crossing=crossing,
        crossing_theta=crossing_theta, jet=J, L=L)


def format_risk_vector(rv, names=('a', 'b'), title=''):
    lines = [title] if title else []
    lines.append(f'  price {rv.price:.6f}   horizon carry {rv.carry:+.3e}')
    lines.append(f'  whitened gradient {np.array2string(rv.gradient, precision=4)}')
    lines.append(f'  principal curvatures {np.array2string(rv.curvatures, precision=4)}')
    for k in range(2):
        d = rv.directions[:, k]
        state_dir = rv.L @ d
        lines.append(f'    direction {k + 1}: whitened {np.array2string(d, precision=3)}'
                     f'  state d{names[0]}:d{names[1]} = {state_dir[0]:+.4g}:{state_dir[1]:+.4g}')
    lines.append('  energy by order (1..4) ' + '  '.join(f'{e:.2e}' for e in rv.energy)
                 + f'   [closed-form order 2: {rv.energy2_closed:.2e}]')
    for k, p in rv.tensor_peaks.items():
        lines.append(f'  order {k}: peak {p["value"]:+.3e} at {p["theta_deg"]:.1f} deg,'
                     f' anisotropy {p["anisotropy"]:.2f}, Z-eigenpairs {p["z_eigenpairs"]}')
    lines.append(f'  T4 flattening spectrum {np.array2string(rv.t4_flat_spectrum, precision=3)}')
    lines.append(f'  stress ray {rv.stress_theta:.1f} deg on radius {rv.radius:.3f}:'
                 f' worst loss {rv.worst_loss:.5f}, Taylor-4 loss {rv.taylor4_loss:.5f}')
    lines.append('  ray terms t1..t4 ' + '  '.join(f'{x:+.2e}' for x in rv.ray_terms[1:]))
    lines.append(f'  ratio gate {"pass" if rv.ratio_gate else "FAIL"}, repriced gate'
                 f' {"pass" if rv.repriced_gate else "FAIL"} -> action: {rv.action}')
    lines.append('  crossing: ' + (f'yes, second maximum at {rv.crossing_theta:.1f} deg'
                                   if rv.crossing else 'no'))
    return '\n'.join(lines)


def spectral_var(model, state0, Sigma, z, levels=(0.99,), horizon=0.0, position=1.0,
                 rv=None, **kw):
    """VaR from whitened draws z: Taylor-4 when the repriced gate trusts the series, else exact."""
    rv = rv or risk_vector(model, state0, Sigma, horizon, position, **kw)
    pnl = rv.jet(z) if rv.action == 'taylor4' else exact_pnl(model, state0, rv.L, z, horizon, position)
    return dict(zip(levels, np.quantile(-pnl, levels))), rv


# ---------------------------------------------------------------------------
# 4. GJR-GARCH state
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class GJRParams:
    omega: float
    alpha: float
    gamma: float
    beta: float
    mu: float = 0.0

    @property
    def persistence(self):
        return self.alpha + 0.5 * self.gamma + self.beta

    @property
    def longrun(self):
        return self.omega / max(1.0 - self.persistence, 1e-12)


def gjr_filter(e, p, s0=None):
    """Conditional variances; out[t] is the variance of e[t], out[n] the one-step forecast."""
    e = np.asarray(e, dtype=float)
    s0 = float(np.var(e)) if s0 is None else float(s0)
    if len(e) == 0:
        return np.array([s0])
    x = p.omega + (p.alpha + p.gamma * (e < 0)) * e * e
    y = lfilter([1.0], [1.0, -p.beta], x, zi=[p.beta * s0])[0]
    return np.concatenate([[s0], y])


def gjr_nll(e, p, s0=None):
    s2 = gjr_filter(e, p, s0)[:-1]
    if np.any(s2 <= 0) or not np.all(np.isfinite(s2)):
        return 1e300
    return 0.5 * float(np.sum(LOG2PI + np.log(s2) + e * e / s2))


def fit_gjr(r, max_persistence=0.999):
    """Gaussian QML on drift residuals; persistence alpha + gamma/2 + beta < max_persistence."""
    r = np.asarray(r, dtype=float)
    mu = float(np.mean(r))
    e = r - mu
    var = float(np.var(e))

    def unpack(th):
        return GJRParams(th[0] * var, th[1], th[2], th[3], mu)

    def obj(th):
        return gjr_nll(e, unpack(th), var) / len(e)

    cons = [{'type': 'ineq', 'fun': lambda th: max_persistence - 1e-6 - (th[1] + 0.5 * th[2] + th[3])}]
    bounds = [(1e-8, 10.0), (0.0, 0.5), (0.0, 0.8), (0.0, max_persistence)]
    best = None
    for x0 in ([0.05, 0.03, 0.08, 0.88], [0.25, 0.08, 0.05, 0.70]):
        res = optimize.minimize(obj, x0, method='SLSQP', bounds=bounds, constraints=cons,
                                options={'maxiter': 300, 'ftol': 1e-11})
        if best is None or (np.isfinite(res.fun) and res.fun < best.fun):
            best = res
    p = unpack(best.x)
    s2 = gjr_filter(e, p, var)
    return p, s2, e / np.sqrt(s2[:-1])


def horizon_variance(sigma2_next, p, h):
    """h sigma_bar^2 + (sigma^2_{t+1} - sigma_bar^2)(1 - rho^h)/(1 - rho)."""
    rho, lr = p.persistence, p.longrun
    if abs(1.0 - rho) < 1e-12:
        return h * sigma2_next
    return h * lr + (sigma2_next - lr) * (1.0 - rho ** h) / (1.0 - rho)


def gjr_paths(p, sigma2_next, z):
    """h-day log returns from running the GJR recursion through standardised shocks z, shape (n, h).

    The shortcut mu h + sqrt(horizon_variance) z keeps the 1-day law's shape at every horizon. The
    h-day return is instead a mixture over the variance path, and that path is pushed up by the
    path's own losses (the gamma term), so its lower tail is heavier than the rescaled 1-day law's.
    horizon_variance is its exact second moment only for shocks with E[z^2; z < 0] = 1/2.
    sigma2_next is the first day's variance; an (m,) array of origins gives (n, m) returns."""
    z = np.asarray(z, dtype=float)
    s2 = np.asarray(sigma2_next, dtype=float)
    if s2.ndim:
        z = z[..., None]
    s = s2 * np.ones(z.shape[:1] + s2.shape)
    R = np.zeros_like(s)
    for k in range(z.shape[1]):
        e = np.sqrt(s) * z[:, k]
        R += p.mu + e
        s = p.omega + (p.alpha + p.gamma * (e < 0)) * e * e + p.beta * s
    return R


def horizon_quantiles(p, sigma2_next, h, levels, law=None, residuals=None, n=50000, seed=0):
    """Quantiles at `levels` of the h-day log return under the fitted GJR dynamics.

    Shocks are `law.ppf` of uniforms, or bootstrapped from `residuals` (filtered historical
    simulation). h = 1 with a law is the law's own quantile, exactly. The seed is fixed, so repeated
    calls on the same inputs agree (common random numbers)."""
    levels = np.atleast_1d(np.asarray(levels, dtype=float))
    if h == 1 and law is not None and np.ndim(sigma2_next) == 0:
        return p.mu + math.sqrt(float(sigma2_next)) * np.asarray(law.ppf(levels), dtype=float)
    rng = np.random.default_rng(seed)
    if law is not None:
        z = np.asarray(law.ppf(np.clip(rng.random((n, h)), 1e-12, 1 - 1e-12)), dtype=float)
    else:
        res = np.asarray(residuals, dtype=float)
        z = res[rng.integers(0, len(res), size=(n, h))]
    return np.quantile(gjr_paths(p, sigma2_next, z), levels, axis=0)


@dataclass
class WalkForward:
    sigma2: np.ndarray      # sigma2[t]: variance forecast for r[t] using r[:t] only
    block_of: np.ndarray    # which fit produced sigma2[t] (-1 before the first fit)
    params: list
    starts: list
    z_insample: list        # standardised in-sample residuals of each fit


def walk_forward_gjr(r, block=63, min_train=504):
    """Expanding-window GJR fits refreshed every `block` days; each block uses data before its start."""
    r = np.asarray(r, dtype=float)
    n = len(r)
    sig = np.full(n + 1, np.nan)
    bo = np.full(n + 1, -1)
    params, starts, zs = [], [], []
    for b in range(min_train, n + 1, block):
        p, _, z = fit_gjr(r[:b])
        end = min(b + block, n)
        s2 = gjr_filter(r[:end] - p.mu, p, s0=float(np.var(r[:b] - p.mu)))
        sig[b:end + 1] = s2[b:end + 1]
        bo[b:end + 1] = len(params)
        params.append(p)
        starts.append(b)
        zs.append(z)
    return WalkForward(sig, bo, params, starts, zs)


# ---------------------------------------------------------------------------
# 5. Standardised-residual laws (all sampled by inverse transform -> common random numbers)
# ---------------------------------------------------------------------------

class NormalLaw:
    name = 'normal'

    def ppf(self, u):
        return ndtri(u)


class EmpiricalLaw:
    """Empirical quantile function of filtered residuals (filtered historical simulation)."""
    name = 'fhs'

    def __init__(self, z):
        self.z = np.sort(np.asarray(z, dtype=float))

    def ppf(self, u):
        idx = np.minimum((np.asarray(u) * len(self.z)).astype(int), len(self.z) - 1)
        return self.z[idx]


class SemiParamGPD:
    """Empirical body with generalised Pareto tails beyond the `tail_frac` quantiles.

    Shape xi = 1/k per side (k a tail index: given, or a Hill estimate when None), clipped to
    [0, xi_max]; scale from the mean excess, beta = (1 - xi) * mean(excess). The law is then
    restandardised to mean 0, variance 1.
    """
    name = 'gpd'

    def __init__(self, z, tail_frac=0.10, k=None, xi_max=0.45):
        z = np.sort(np.asarray(z, dtype=float))
        self.q = q = tail_frac
        self.body = z
        self.grid = (np.arange(len(z)) + 0.5) / len(z)
        self.lo, self.hi = float(np.quantile(z, q)), float(np.quantile(z, 1 - q))
        k_lo, k_hi = (k, k) if k is None or np.isscalar(k) else k
        ex_lo = -z[z < self.lo] + self.lo
        ex_hi = z[z > self.hi] - self.hi
        self.xi_lo = self._xi(-z[z < self.lo], -self.lo, k_lo, xi_max)
        self.xi_hi = self._xi(z[z > self.hi], self.hi, k_hi, xi_max)
        self.b_lo = (1 - self.xi_lo) * (ex_lo.mean() if len(ex_lo) else 0.1)
        self.b_hi = (1 - self.xi_hi) * (ex_hi.mean() if len(ex_hi) else 0.1)
        self.m, self.s = self._moments()

    @staticmethod
    def _xi(values, threshold, k, xi_max):
        if k is None:
            if threshold <= 0 or len(values) < 5:
                return 0.0
            k = 1.0 / max(np.mean(np.log(values / threshold)), 1e-12)
        return float(np.clip(1.0 / k, 0.0, xi_max))

    @staticmethod
    def _excess(p, xi, b):
        return -b * np.log(p) if xi < 1e-9 else b / xi * (p ** (-xi) - 1.0)

    def raw_ppf(self, u):
        u = np.asarray(u, dtype=float)
        out = np.interp(u, self.grid, self.body)
        lo, hi = u < self.q, u > 1 - self.q
        out[lo] = self.lo - self._excess(u[lo] / self.q, self.xi_lo, self.b_lo)
        out[hi] = self.hi + self._excess((1 - u[hi]) / self.q, self.xi_hi, self.b_hi)
        return out

    def _moments(self):
        q = self.q
        u = q + (1 - 2 * q) * (np.arange(20000) + 0.5) / 20000
        xb = np.interp(u, self.grid, self.body)
        m1 = (1 - 2 * q) * xb.mean()
        m2 = (1 - 2 * q) * (xb * xb).mean()
        for thr, xi, b, sgn in ((self.lo, self.xi_lo, self.b_lo, -1), (self.hi, self.xi_hi, self.b_hi, 1)):
            ey = b / (1 - xi)
            ey2 = 2 * b * b / ((1 - xi) * (1 - 2 * xi))
            m1 += q * (thr + sgn * ey)
            m2 += q * (thr * thr + 2 * sgn * thr * ey + ey2)
        return m1, math.sqrt(max(m2 - m1 * m1, 1e-12))

    def ppf(self, u):
        return (self.raw_ppf(u) - self.m) / self.s


class HansenSkewT:
    """Hansen (1994) skewed Student-t with zero mean and unit variance."""
    name = 'skewt'

    def __init__(self, z=None, eta=8.0, lam=0.0):
        if z is not None:
            eta, lam = self.fit(z)
        self.eta, self.lam = float(eta), float(lam)
        self.a, self.b, self.c = self._abc(self.eta, self.lam)

    @staticmethod
    def _abc(eta, lam):
        c = math.exp(gammaln((eta + 1) / 2) - gammaln(eta / 2)) / math.sqrt(math.pi * (eta - 2))
        a = 4 * lam * c * (eta - 2) / (eta - 1)
        return a, math.sqrt(1 + 3 * lam * lam - a * a), c

    @classmethod
    def logpdf(cls, z, eta, lam):
        a, b, c = cls._abc(eta, lam)
        s = np.where(z < -a / b, 1 - lam, 1 + lam)
        return math.log(b * c) - (eta + 1) / 2 * np.log1p(((b * z + a) / s) ** 2 / (eta - 2))

    @classmethod
    def fit(cls, z):
        z = np.asarray(z, dtype=float)
        res = optimize.minimize(lambda th: -np.sum(cls.logpdf(z, th[0], th[1])), [8.0, 0.0],
                                method='L-BFGS-B', bounds=[(2.2, 100.0), (-0.95, 0.95)])
        return res.x

    def ppf(self, u):
        u = np.asarray(u, dtype=float)
        eta, lam, a, b = self.eta, self.lam, self.a, self.b
        sc = math.sqrt((eta - 2) / eta)
        left = u < (1 - lam) / 2
        out = np.empty_like(u)
        out[left] = ((1 - lam) * sc * stats.t.ppf(u[left] / (1 - lam), eta) - a) / b
        out[~left] = ((1 + lam) * sc * stats.t.ppf(0.5 + (u[~left] - (1 - lam) / 2) / (1 + lam), eta) - a) / b
        return out

    def cdf(self, z):
        z = np.asarray(z, dtype=float)
        eta, lam, a, b = self.eta, self.lam, self.a, self.b
        sc = math.sqrt((eta - 2) / eta)
        y = b * z + a
        return np.where(z < -a / b,
                        (1 - lam) * stats.t.cdf(y / ((1 - lam) * sc), eta),
                        (1 - lam) / 2 + (1 + lam) * (stats.t.cdf(y / ((1 + lam) * sc), eta) - 0.5))


def mix_eigen(e1, e2, rho):
    """Place independent unit innovations on the +-45 degree eigen-factors of [[1, rho], [rho, 1]]."""
    a = np.sqrt((1 + rho) / 2) * e1
    b = np.sqrt((1 - rho) / 2) * e2
    return a + b, a - b


def unmix_eigen(x1, x2, rho):
    """Inverse of mix_eigen: rotate correlated residuals onto independent eigen-factors."""
    return (x1 + x2) / np.sqrt(2 * (1 + rho)), (x1 - x2) / np.sqrt(2 * (1 - rho))


# ---------------------------------------------------------------------------
# 6. Compositional (Aitchison) powering and correlation filters
# ---------------------------------------------------------------------------

def closure(x, axis=-1):
    x = np.asarray(x, dtype=float)
    return x / x.sum(axis=axis, keepdims=True)


def power_closure(x, gamma, axis=-1):
    """Aitchison power: raise a composition to gamma and re-close it."""
    return closure(np.power(x, gamma), axis)


def temper_rho(rho, gamma, floor=0.2):
    """Power the eigenvalue shares (1 +- rho)/2 of a 2x2 correlation; equals tanh(gamma atanh rho).

    The eigenvalue floor caps |rho| at 1 - floor (0.8 for the default)."""
    s = power_closure(np.array([(1 + rho) / 2, (1 - rho) / 2]), gamma)
    cap = 1.0 - floor
    return float(np.clip(s[0] - s[1], -cap, cap))


def _unit_diagonal(A):
    A = (A + A.T) / 2
    d = np.sqrt(np.maximum(np.diag(A), 1e-300))
    A = A / np.outer(d, d)
    np.fill_diagonal(A, 1.0)
    return A


def spectral_correlation(C, gamma=1.0, lam_min=0.05):
    """General-N powering: lambda~ = N C(max(N C(lambda^gamma), lambda_min)), unit diagonal."""
    lam, V = np.linalg.eigh((np.asarray(C) + np.asarray(C).T) / 2)
    N = len(lam)
    t = N * closure(np.maximum(lam, 0.0) ** gamma)
    t = N * closure(np.maximum(t, lam_min))
    return _unit_diagonal(V @ np.diag(t) @ V.T)


def mp_filter(C, n_obs):
    """Marchenko-Pastur filter: eigenvalues inside the noise band are replaced by their mean.

    Same rule as sp100_mp_portfolio_risk.mp_filter_correlation, inlined so this module has no
    path dependency on another repository."""
    C = np.asarray(C, dtype=float)
    N = len(C)
    lam_plus = (1 + math.sqrt(N / n_obs)) ** 2
    lam, V = np.linalg.eigh((C + C.T) / 2)
    noise = lam <= lam_plus
    if noise.any():
        lam = lam.copy()
        lam[noise] = lam[noise].mean()
    return _unit_diagonal(V @ np.diag(lam) @ V.T), lam_plus, int((~noise).sum())


# ---------------------------------------------------------------------------
# 7. Scoring and coverage tests
# ---------------------------------------------------------------------------

def pinball(loss, var, level):
    u = np.asarray(loss) - np.asarray(var)
    return np.maximum(level * u, (level - 1.0) * u)


def kupiec(hits, p):
    """Unconditional coverage LR test; p is the nominal breach probability."""
    hits = np.asarray(hits, dtype=bool)
    n, x = len(hits), int(hits.sum())
    pi = x / n if n else 0.0
    ll0 = xlogy(n - x, 1 - p) + xlogy(x, p)
    ll1 = xlogy(n - x, 1 - pi) + xlogy(x, pi)
    lr = float(-2 * (ll0 - ll1))
    return lr, float(stats.chi2.sf(lr, 1))


def christoffersen(hits, p):
    """Independence and conditional-coverage LR tests (first-order Markov alternative)."""
    h = np.asarray(hits, dtype=int)
    prev, cur = h[:-1], h[1:]
    n00 = int(np.sum((prev == 0) & (cur == 0)))
    n01 = int(np.sum((prev == 0) & (cur == 1)))
    n10 = int(np.sum((prev == 1) & (cur == 0)))
    n11 = int(np.sum((prev == 1) & (cur == 1)))
    p01 = n01 / (n00 + n01) if n00 + n01 else 0.0
    p11 = n11 / (n10 + n11) if n10 + n11 else 0.0
    tot = n00 + n01 + n10 + n11
    pi = (n01 + n11) / tot if tot else 0.0
    ll_alt = xlogy(n00, 1 - p01) + xlogy(n01, p01) + xlogy(n10, 1 - p11) + xlogy(n11, p11)
    ll_null = xlogy(n00 + n10, 1 - pi) + xlogy(n01 + n11, pi)
    lr_ind = float(max(-2 * (ll_null - ll_alt), 0.0))
    lr_cc = kupiec(hits, p)[0] + lr_ind
    return {'lr_ind': lr_ind, 'p_ind': float(stats.chi2.sf(lr_ind, 1)),
            'lr_cc': lr_cc, 'p_cc': float(stats.chi2.sf(lr_cc, 2))}


def berkowitz(pit, eps=1e-6):
    """LR test that Phi^-1(PIT) is iid N(0,1) against a Gaussian AR(1) alternative (3 df)."""
    y = ndtri(np.clip(np.asarray(pit, dtype=float), eps, 1 - eps))
    x, yy = y[:-1], y[1:]
    X = np.column_stack([np.ones_like(x), x])
    coef = np.linalg.lstsq(X, yy, rcond=None)[0]
    resid = yy - X @ coef
    s2 = max(float(np.mean(resid ** 2)), 1e-12)
    ll1 = -0.5 * np.sum(LOG2PI + math.log(s2) + resid ** 2 / s2)
    ll0 = -0.5 * np.sum(LOG2PI + yy ** 2)
    lr = float(-2 * (ll0 - ll1))
    return {'lr': lr, 'p': float(stats.chi2.sf(lr, 3)), 'mean': float(coef[0]),
            'ar1': float(coef[1]), 'var': s2}


def _block_boot_means(x, block, n_boot, seed):
    x = np.asarray(x, dtype=float)
    n = len(x)
    nb = math.ceil(n / block)
    rng = np.random.default_rng(seed)
    starts = rng.integers(0, n, size=(n_boot, nb))
    idx = ((starts[:, :, None] + np.arange(block)) % n).reshape(n_boot, -1)[:, :n]
    return x[idx].mean(axis=1)


def circular_block_bootstrap(x, block=10, n_boot=2000, seed=0):
    """Mean and 95% interval of mean(x) under the circular block bootstrap."""
    boot = _block_boot_means(x, block, n_boot, seed)
    return float(np.mean(x)), (float(np.quantile(boot, 0.025)), float(np.quantile(boot, 0.975)))


def paired_power(diff, block=4, alpha=0.05, target=0.8, n_boot=2000, seed=0):
    """Power of a paired comparison: is mean(diff) distinguishable from zero?

    The standard error comes from the circular block bootstrap. `power` is the probability that a
    two-sided level-alpha test detects an effect the size of the one observed with this many
    windows; `n_needed` is the number of windows that would give `target` power for that effect.
    Low power means the sample cannot tell the estimators apart -- not that they are equal.
    """
    d = np.asarray(diff, dtype=float)
    n = len(d)
    boot = _block_boot_means(d, block, n_boot, seed)
    se = float(np.std(boot, ddof=1))
    mean = float(d.mean())
    za, zb = stats.norm.ppf(1 - alpha / 2), stats.norm.ppf(target)
    if se <= 0 or n < 2:
        return {'mean': mean, 'se': se, 'z': float('nan'), 'power': float('nan'), 'n_needed': float('nan'),
                'ci': (mean, mean), 'n': n}
    z = abs(mean) / se
    power = float(stats.norm.cdf(z - za) + stats.norm.cdf(-z - za))
    sd_window = se * math.sqrt(n)  # long-run SD per window implied by the bootstrap
    n_needed = float(((za + zb) * sd_window / abs(mean)) ** 2) if mean != 0 else float('inf')
    return {'mean': mean, 'se': se, 'z': mean / se, 'power': power, 'n_needed': n_needed, 'n': n,
            'ci': (float(np.quantile(boot, 0.025)), float(np.quantile(boot, 0.975)))}


def cluster_bootstrap(values, clusters, n_boot=2000, seed=0):
    """Mean and 95% interval resampling whole clusters (e.g. all contracts sharing an expiry)."""
    values = np.asarray(values, dtype=float)
    clusters = np.asarray(clusters)
    uniq, inv = np.unique(clusters, return_inverse=True)
    sums = np.bincount(inv, weights=values, minlength=len(uniq))
    counts = np.bincount(inv, minlength=len(uniq)).astype(float)
    rng = np.random.default_rng(seed)
    idx = rng.integers(0, len(uniq), size=(n_boot, len(uniq)))
    boot = sums[idx].sum(axis=1) / np.maximum(counts[idx].sum(axis=1), 1)
    return float(values.mean()), (float(np.quantile(boot, 0.025)), float(np.quantile(boot, 0.975)))


# ---------------------------------------------------------------------------
# 8. Control loops
# ---------------------------------------------------------------------------

class PIDVaRScale:
    """PID on the log VaR scale driven by the breach error 1{L > VaR} - (1 - alpha).

    The factor is clamped to [lo, hi]; anti-windup freezes the integral while the clamp binds
    and the error pushes further into it."""

    def __init__(self, alpha=0.99, kp=0.2, ki=0.02, kd=0.0, lo=0.5, hi=2.0):
        self.target = 1.0 - alpha
        self.kp, self.ki, self.kd = kp, ki, kd
        self.lo, self.hi = math.log(lo), math.log(hi)
        self.integral = 0.0
        self.prev = 0.0
        self.log_scale = 0.0
        self.saturated = False

    @property
    def factor(self):
        return math.exp(self.log_scale)

    def update(self, breach):
        e = float(breach) - self.target
        trial = self.integral + e
        u = self.kp * e + self.ki * trial + self.kd * (e - self.prev)
        self.saturated = u > self.hi or u < self.lo
        if not ((u > self.hi and e > 0) or (u < self.lo and e < 0)):
            self.integral = trial
        else:
            u = self.kp * e + self.ki * self.integral + self.kd * (e - self.prev)
        self.prev = e
        self.log_scale = float(np.clip(u, self.lo, self.hi))
        return self.factor


def cusum(x, k=0.0, h=4.0):
    """One-sided CUSUM S_t = max(0, S_{t-1} + x_t - k); alarm and reset when S_t > h."""
    s, path, alarms = 0.0, [], []
    for i, v in enumerate(x):
        s = max(0.0, s + float(v) - k)
        if s > h:
            alarms.append(i)
            path.append(s)
            s = 0.0
        else:
            path.append(s)
    return np.array(path), alarms


# ---------------------------------------------------------------------------
# 9. Hedging: hierarchical (Greek matching) versus complete-sum
# ---------------------------------------------------------------------------

@dataclass
class HedgeResult:
    method: str
    weights: np.ndarray
    rms: float
    p99_loss: float
    rank: int | None = None
    cond: float | None = None


def solve_truncated(A, b, tol=1e-10):
    """Truncated-SVD solve of A x = b, dropping singular values below tol * s_max."""
    U, s, Vt = np.linalg.svd(A)
    keep = s > tol * s[0]
    x = Vt[keep].T @ ((U[:, keep].T @ b) / s[keep])
    return x, int(keep.sum()), float(s[0] / s[-1]) if s[-1] > 0 else float('inf')


def greeks(model, state0):
    """(delta, gamma, vega) = (d/dX, d2/dX2, d/dY) in state units, instantaneous."""
    J = pnl_jet(model, np.asarray(state0, float), np.eye(2), 0.0, 1.0)
    return np.array([J.derivative((1, 0)), J.derivative((2, 0)), J.derivative((0, 1))])


def complete_sum_weights(target, instruments, state0, L, horizon, order=4, gram=None, svd_tol=1e-10):
    """Minimise E[P&L^2] of the whole order-`order` Taylor polynomial using shock moments."""
    G = _gaussian_gram(2, DEG) if gram is None else gram
    ct = pnl_jet(target, state0, L, horizon).truncate(order).c
    Ci = np.array([pnl_jet(m, state0, L, horizon).truncate(order).c for m in instruments])
    A = Ci @ G @ Ci.T
    b = Ci @ G @ ct
    w, rank, cond = solve_truncated(A, -b, svd_tol)
    return w, rank, cond


def neighbour_strikes(model, offsets=(-1, 1, 2), step=0.01):
    """Neighbouring-strike contracts: relative steps for bsm, score units for latent."""
    if isinstance(model, BSMDigital):
        return [replace(model, strike=model.strike * (1 + step * o)) for o in offsets]
    return [replace(model, strike=model.strike + step * o) for o in offsets]


def hedge_comparison(target, instruments, state0, Sigma, horizon, n_oos=20000, seed=11,
                     svd_tol=1e-10, moment_draws=None, call_put=None, oos_draws=None):
    """Hierarchical vs complete-sum (orders 2 and 4) vs exact-fit bound, evaluated out of sample
    by exact repricing. `moment_draws` adds a moment-matched complete-sum fitted on draws from an
    adopted tail model; `call_put` adds a complete-sum hedge with calls and puts as separate
    instruments (rank deficient through put-call parity)."""
    state0 = np.asarray(state0, dtype=float)
    L = cov_factor(Sigma)
    z = np.random.default_rng(seed).standard_normal((n_oos, 2)) if oos_draws is None else oos_draws
    tgt = exact_pnl(target, state0, L, z, horizon)

    def book(instr):
        return np.column_stack([exact_pnl(m, state0, L, z, horizon) for m in instr])

    inst = book(instruments)
    out = []

    def evaluate(name, w, pnl_inst=inst, **info):
        pnl = tgt + pnl_inst @ w
        out.append(HedgeResult(name, w, float(np.sqrt(np.mean(pnl ** 2))),
                               float(np.quantile(-pnl, 0.99)), **info))

    evaluate('unhedged', np.zeros(len(instruments)))
    Gm = np.column_stack([greeks(m, state0) for m in instruments])
    w = np.linalg.lstsq(Gm, -greeks(target, state0), rcond=None)[0]
    evaluate('hierarchical', w, rank=int(np.linalg.matrix_rank(Gm)), cond=float(np.linalg.cond(Gm)))
    for order in (2, 4):
        w, rk, cn = complete_sum_weights(target, instruments, state0, L, horizon, order, None, svd_tol)
        evaluate(f'complete-sum o{order}', w, rank=rk, cond=cn)
    if moment_draws is not None:
        G = monomial_gram(2, DEG, moment_draws)
        w, rk, cn = complete_sum_weights(target, instruments, state0, L, horizon, 4, G, svd_tol)
        evaluate('complete-sum o4 moment-matched', w, rank=rk, cond=cn)
    if call_put:
        cp = book(call_put)
        w, rk, cn = complete_sum_weights(target, call_put, state0, L, horizon, 4, None, svd_tol)
        evaluate('call-put complete-sum o4', w, cp, rank=rk, cond=cn)
    w = np.linalg.lstsq(inst, -tgt, rcond=None)[0]
    evaluate('exact fit (bound)', w)
    return out


def format_hedges(results):
    lines = ['  method                              RMS P&L    99% loss   rank  cond']
    for r in results:
        rk = '' if r.rank is None else str(r.rank)
        cn = '' if r.cond is None else f'{r.cond:.1e}'
        lines.append(f'  {r.method:34s} {r.rms:9.5f}  {r.p99_loss:9.5f}   {rk:>4}  {cn}')
    return '\n'.join(lines)


# ---------------------------------------------------------------------------
# 10. Probabilities, calibration and event-market tests
# ---------------------------------------------------------------------------

def brier(p, y):
    return float(np.mean((np.asarray(p) - np.asarray(y)) ** 2))


def log_loss(p, y, eps=1e-12):
    p = np.clip(np.asarray(p, dtype=float), eps, 1 - eps)
    y = np.asarray(y, dtype=float)
    return float(-np.mean(y * np.log(p) + (1 - y) * np.log(1 - p)))


def auc(score, y):
    y = np.asarray(y).astype(bool)
    n1, n0 = int(y.sum()), int((~y).sum())
    if n1 == 0 or n0 == 0:
        return float('nan')
    r = stats.rankdata(score)
    return float((r[y].sum() - n1 * (n1 + 1) / 2) / (n1 * n0))


def within_strike_auc(score, y, groups):
    """AUC within each strike group (does the forecast rank days at a fixed strike?)."""
    score, y, groups = map(np.asarray, (score, y, groups))
    num = den = 0.0
    for g in np.unique(groups):
        m = groups == g
        n1 = y[m].sum()
        w = n1 * (m.sum() - n1)
        if w > 0:
            num += w * auc(score[m], y[m])
            den += w
    return float(num / den) if den else float('nan')


def reliability(p, y, bins=10):
    p, y = np.asarray(p, float), np.asarray(y, float)
    idx = np.minimum((p * bins).astype(int), bins - 1)
    rows = []
    for b in range(bins):
        m = idx == b
        if m.any():
            rows.append((b / bins, (b + 1) / bins, int(m.sum()), float(p[m].mean()), float(y[m].mean())))
    return rows


def fit_logit_power(p, y, eps=1e-6):
    """logit(p~) = gamma logit(p) + delta by maximum likelihood."""
    x = logit(np.clip(np.asarray(p, float), eps, 1 - eps))
    y = np.asarray(y, float)

    def nll(th):
        eta = th[0] * x + th[1]
        return float(np.sum(np.logaddexp(0, eta) - y * eta))

    def grad(th):
        r = expit(th[0] * x + th[1]) - y
        return np.array([np.sum(r * x), np.sum(r)])

    return optimize.minimize(nll, [1.0, 0.0], jac=grad, method='BFGS').x


def apply_logit_power(p, th, eps=1e-6):
    return expit(th[0] * logit(np.clip(np.asarray(p, float), eps, 1 - eps)) + th[1])


def fit_probit(p, y, eps=1e-6):
    """P(Y = 1) = Phi(a + b Phi^-1(p)) by maximum likelihood."""
    x = ndtri(np.clip(np.asarray(p, float), eps, 1 - eps))
    y = np.asarray(y, float)

    def nll(th):
        q = np.clip(ndtr(th[0] + th[1] * x), 1e-12, 1 - 1e-12)
        return float(-np.sum(y * np.log(q) + (1 - y) * np.log(1 - q)))

    return optimize.minimize(nll, [0.0, 1.0], method='Nelder-Mead').x


def apply_probit(p, th, eps=1e-6):
    return ndtr(th[0] + th[1] * ndtri(np.clip(np.asarray(p, float), eps, 1 - eps)))


def walk_forward_recalibrate(dates, expiries, p, y, refit_every=63, method='logit_power', min_obs=30):
    """Refit on contracts that had already expired by each refit date; apply until the next one."""
    dates, expiries = np.asarray(dates), np.asarray(expiries)
    p, y = np.asarray(p, float), np.asarray(y, float)
    fit, apply = (fit_logit_power, apply_logit_power) if method == 'logit_power' else (fit_probit, apply_probit)
    out = p.copy()
    udates = np.unique(dates)
    refits = udates[::refit_every]
    for i, d in enumerate(refits):
        nxt = refits[i + 1] if i + 1 < len(refits) else None
        train = expiries <= d
        if train.sum() < min_obs:
            continue
        th = fit(p[train], y[train])
        tgt = (dates >= d) if nxt is None else ((dates >= d) & (dates < nxt))
        out[tgt] = apply(p[tgt], th)
    return out


def power_devig(m, tol=1e-12):
    """Exponent k* with sum m_i^k* = 1 (bisection, no fitted parameter); returns k*, m^k*."""
    m = np.clip(np.asarray(m, dtype=float), 1e-9, 1 - 1e-9)
    f = lambda k: float(np.sum(m ** k) - 1.0)
    lo, hi = 1e-6, 1.0
    while f(hi) > 0:
        hi *= 2
    while f(lo) < 0:
        lo /= 2
    while hi - lo > tol:
        mid = 0.5 * (lo + hi)
        lo, hi = (mid, hi) if f(mid) > 0 else (lo, mid)
    k = 0.5 * (lo + hi)
    return k, m ** k


def ladder_scores(P, outcome):
    """Log score, multi-class Brier and ranked probability score for range ladders."""
    P = np.asarray(P, dtype=float)
    o = np.asarray(outcome, dtype=int)
    n, K = P.shape
    onehot = np.zeros_like(P)
    onehot[np.arange(n), o] = 1.0
    rps = np.sum((np.cumsum(P, 1) - np.cumsum(onehot, 1)) ** 2, axis=1) / (K - 1)
    return {'log_score': float(np.mean(np.log(np.maximum(P[np.arange(n), o], 1e-12)))),
            'brier': float(np.mean(np.sum((P - onehot) ** 2, axis=1))),
            'rps': float(np.mean(rps))}


def fit_ladder_power(M, outcome):
    """Power-and-closure exponent maximising the log score of past ladders."""
    M = np.asarray(M, float)
    o = np.asarray(outcome, int)

    def nll(k):
        P = power_closure(M, k, axis=1)
        return -np.mean(np.log(np.maximum(P[np.arange(len(o)), o], 1e-12)))

    return optimize.minimize_scalar(nll, bounds=(0.2, 5.0), method='bounded').x


def edge_trades(p, bid, ask, y, threshold=0.02, fee=0.01):
    """Buy at the ask / sell at the bid when the forecast clears the quote by threshold + fee."""
    p, bid, ask, y = (np.asarray(a, float) for a in (p, bid, ask, y))
    buy = p - ask > threshold + fee
    sell = bid - p > threshold + fee
    pnl = np.where(buy, y - ask - fee, np.where(sell, bid - y - fee, 0.0))
    return pnl, buy.astype(int) - sell.astype(int)


# ---------------------------------------------------------------------------
# 11. Data loading and synthetic data
# ---------------------------------------------------------------------------

def _parse_date(s):
    s = s.strip()
    for fmt in ('%Y-%m-%d', '%m/%d/%Y', '%d/%m/%Y', '%Y%m%d'):
        try:
            return np.datetime64(datetime.strptime(s[:10] if fmt == '%Y-%m-%d' else s, fmt).date())
        except ValueError:
            continue
    raise ValueError(f'unrecognised date {s!r}')


def load_series(path):
    """date,value CSV -> (dates, values); value column is close/adj close/value or the last one."""
    with open(path, newline='', encoding='utf-8-sig') as f:
        rows = list(csv.reader(f))
    header = [h.strip().lower() for h in rows[0]]
    vcol = len(header) - 1
    for name in ('adj close', 'adj_close', 'close', 'value', 'price'):
        if name in header:
            vcol = header.index(name)
            break
    d, v = [], []
    for row in rows[1:]:
        if len(row) <= vcol or not row[0].strip():
            continue
        try:
            val = float(row[vcol])
        except ValueError:
            continue
        if math.isfinite(val):
            d.append(_parse_date(row[0]))
            v.append(val)
    d, v = np.array(d), np.array(v)
    order = np.argsort(d, kind='stable')
    d, v = d[order], v[order]
    keep = np.concatenate([[True], d[1:] != d[:-1]])
    return d[keep], v[keep]


def find_file(data_dir, candidates):
    for name in candidates:
        path = os.path.join(data_dir, name)
        if os.path.exists(path):
            return path
    raise FileNotFoundError(f'none of {candidates} in {data_dir!r}')


def align(*series):
    """Intersect several (dates, values) series on common dates."""
    common = series[0][0]
    for d, _ in series[1:]:
        common = np.intersect1d(common, d)
    return common, [v[np.searchsorted(d, common)] for d, v in series]


# Underlyings. Any priced daily series works; `vol` names an implied-vol index in percentage points
# (VIX-style) when one exists, otherwise a point-in-time GJR-GARCH forecast stands in for it.
# `kalshi` lists the binary series written on the underlying and `events` (a regex on event tickers)
# picks one daily settlement. rate / dividend set the forward (for FX: domestic / foreign rate),
# skew is beta in sigma_K = v - beta ln(F/K). Files are FRED ids (<id>.csv) unless listed.
UNDERLYINGS = {
    'SPX': {'name': 'S&P 500', 'price': 'SP500', 'vol': 'VIXCLS', 'days_per_year': 252,
            'rate': 0.04, 'dividend': 0.015, 'skew': -0.3, 'kalshi': ['KXINXU', 'KXINX'], 'events': r'H1600$',
            'price_files': ('SPX.csv', 'GSPC.csv', '^GSPC.csv'), 'vol_files': ('VIX.csv', '^VIX.csv')},
    'NDX': {'name': 'Nasdaq-100', 'price': 'NASDAQ100', 'vol': 'VXNCLS', 'days_per_year': 252,
            'rate': 0.04, 'dividend': 0.007, 'skew': -0.3, 'kalshi': ['KXNASDAQ100U', 'KXNASDAQ100'],
            'events': r'H1600$'},
    'DJIA': {'name': 'Dow Jones Industrial Average', 'price': 'DJIA', 'vol': 'VXDCLS', 'days_per_year': 252,
             'rate': 0.04, 'dividend': 0.02, 'skew': -0.3, 'kalshi': [], 'events': ''},
    'WTI': {'name': 'WTI crude oil', 'price': 'DCOILWTICO', 'vol': 'OVXCLS', 'days_per_year': 252,
            'rate': 0.04, 'dividend': 0.04, 'skew': 0.0, 'kalshi': ['KXWTI'], 'events': r'\d{2}[A-Z]{3}\d{2}14$',
            'note': 'model state is FRED spot; Kalshi settles on the front futures contract (basis risk)'},
    'BTC': {'name': 'Bitcoin', 'price': 'CBBTCUSD', 'vol': None, 'days_per_year': 365,
            'rate': 0.0, 'dividend': 0.0, 'skew': 0.0, 'kalshi': ['KXBTCD', 'KXBTC'], 'events': r'\d{2}[A-Z]{3}\d{2}17$'},
    'ETH': {'name': 'Ether', 'price': 'CBETHUSD', 'vol': None, 'days_per_year': 365,
            'rate': 0.0, 'dividend': 0.0, 'skew': 0.0, 'kalshi': ['KXETHD'], 'events': r'\d{2}[A-Z]{3}\d{2}17$'},
    'EURUSD': {'name': 'EUR/USD', 'price': 'DEXUSEU', 'vol': None, 'days_per_year': 252,
               'rate': 0.04, 'dividend': 0.02, 'skew': 0.0, 'kalshi': ['KXEURUSD'], 'events': r'10$',
               'note': 'FRED H.10 rates arrive about a week late; stale states are not journaled'},
    'USDJPY': {'name': 'USD/JPY', 'price': 'DEXJPUS', 'vol': None, 'days_per_year': 252,
               'rate': 0.005, 'dividend': 0.04, 'skew': 0.0, 'kalshi': ['KXUSDJPY'], 'events': r'10$',
               'note': 'FRED H.10 rates arrive about a week late; stale states are not journaled'},
}


def underlying_of_series(series):
    return next((k for k, u in UNDERLYINGS.items() if series in u['kalshi']), None)


def gjr_vol_index(S, days_per_year=252, block=63, min_train=252):
    """Point-in-time GJR one-step vol, annualised, in percentage points like VIX.

    Element t uses only returns before t (walk-forward fits); NaN before the first fit."""
    wf = walk_forward_gjr(np.diff(np.log(S)), block, min_train)
    return 100.0 * np.sqrt(days_per_year * wf.sigma2)


def load_underlying(data_dir, key):
    """(dates, S, V) for a registered underlying; V is its vol index or the GJR stand-in.

    Non-positive prices (WTI spot on 2020-04-20 settled at -37.63) cannot enter a log-return
    model; they are dropped, so the return across the gap spans the missing days."""
    u = UNDERLYINGS[key]
    pd_, pv = load_series(find_file(data_dir, (f"{u['price']}.csv",) + tuple(u.get('price_files', ()))))
    price = (pd_[pv > 0], pv[pv > 0])
    if u['vol']:
        vol = load_series(find_file(data_dir, (f"{u['vol']}.csv",) + tuple(u.get('vol_files', ()))))
        dates, (S, V) = align(price, vol)
        return dates, S, V
    dates, S = price
    V = gjr_vol_index(S, u['days_per_year'])
    ok = np.isfinite(V)
    return dates[ok], S[ok], V[ok]


def load_spx_vix(data_dir):
    return load_underlying(data_dir, 'SPX')


def load_daily_signals(path):
    """Daily regime signal CSV written by bsm_microstructure.py (date,signal)."""
    return load_series(path)


def synthetic_market(n=3000, seed=7):
    """GJR index with skew-t shocks and a VIX proxy tied to forward variance (plus noise)."""
    rng = np.random.default_rng(seed)
    p = GJRParams(omega=2e-6, alpha=0.02, gamma=0.12, beta=0.89, mu=3e-4)
    z = HansenSkewT(eta=6.0, lam=-0.15).ppf(np.clip(rng.random(n), 1e-12, 1 - 1e-12))
    s2 = np.empty(n + 1)
    s2[0] = p.longrun
    r = np.empty(n)
    for t in range(n):
        e = math.sqrt(s2[t]) * z[t]
        r[t] = p.mu + e
        s2[t + 1] = p.omega + (p.alpha + p.gamma * (e < 0)) * e * e + p.beta * s2[t]
    S = 3000.0 * np.exp(np.concatenate([[0.0], np.cumsum(r)]))
    a = np.zeros(n + 1)
    eps = rng.standard_normal(n + 1)
    for t in range(1, n + 1):
        a[t] = 0.9 * a[t - 1] + math.sqrt(1 - 0.81) * eps[t]
    V = 100.0 * 1.12 * np.sqrt(TRADING_DAYS * s2) * np.exp(0.06 * a)
    dates = np.busday_offset(np.datetime64('2012-01-03'), np.arange(n + 1), roll='forward')
    return dates, S, V


# ---------------------------------------------------------------------------
# 12. Walk-forward VaR backtest of rolling digitals
# ---------------------------------------------------------------------------

@dataclass
class BacktestConfig:
    tenor_days: int = 21
    horizon: int = 5
    moneyness: tuple = (0.97, 1.00, 1.03)
    position: float = 1.0
    levels: tuple = (0.90, 0.95, 0.99)
    block: int = 63
    min_train: int = 504
    step: int = 0                       # 0 -> horizon (non-overlapping windows)
    n_sims: int = 4000
    tail_frac: float = 0.10
    tail_k: float | None = None         # fixed tail index; None -> tuned over tail_ks
    tail_ks: tuple = (3.0, 4.0, 6.0, 10.0, None)  # None in the grid = Hill estimate per side
    xi_max: float = 0.45
    gammas: tuple = (0.5, 0.75, 1.0, 1.25)
    rho_floor: float = 0.2
    curvature_penalty: float = 1.0
    tune_frac: float = 0.4
    skew: float = -0.3
    rate: float = 0.04
    dividend: float = 0.015
    vol_floor: float = 0.02
    paths: bool = False                 # H daily GJR steps per path instead of the closed form
    level_map: tuple = (0.0, 1.0)       # ATM vol v = a + b * VIX / 100
    days_per_year: int = 252            # 365 for assets that trade every day (crypto)
    seed: int = 20261001
    models: tuple = ('normal', 'gpd', 'skewt', 'fhs', 'spectral')


@dataclass
class _Block:
    rho: float
    laws: dict
    rows: np.ndarray
    factors: tuple
    pS: GJRParams
    pV: GJRParams


def _choose_tied(scores, grid, penalty):
    """Per-strike grid values minimising score + penalty * (second differences)^2."""
    M, G = scores.shape
    norm = scores / max(float(np.mean(np.abs(scores))), 1e-300)
    best, best_val = None, float('inf')
    for combo in product(range(G), repeat=M):
        vals = np.array([grid[g] for g in combo])
        obj = sum(norm[m, combo[m]] for m in range(M))
        if M >= 3:
            obj += penalty * float(np.sum(np.diff(vals, 2) ** 2))
        if obj < best_val:
            best, best_val = vals, obj
    return best


def run_var_backtest(dates, S, V, cfg=None, signals=None, out_csv=None, log=print):
    cfg = cfg or BacktestConfig()
    S, V = np.asarray(S, float), np.asarray(V, float)
    n = len(S)
    H = cfg.horizon
    step = cfg.step or H
    T, hy = cfg.tenor_days / cfg.days_per_year, H / cfg.days_per_year
    if n < cfg.min_train + 4 * H + 50:
        raise ValueError(f'need more than {cfg.min_train + 4 * H + 50} observations, have {n}')
    vmap = lambda x: cfg.level_map[0] + cfg.level_map[1] * x / 100.0
    rS, rV = np.diff(np.log(S)), np.diff(V / 100.0)
    log(f'Fitting walk-forward GJR-GARCH ({cfg.block}-day blocks, first fit at day {cfg.min_train}) ...')
    wfS = walk_forward_gjr(rS, cfg.block, cfg.min_train)
    wfV = walk_forward_gjr(rV, cfg.block, cfg.min_train)
    ts = list(range(cfg.min_train, n - H, step))
    n_tune = int(len(ts) * cfg.tune_frac)
    tune_ts, eval_ts = ts[:n_tune], ts[n_tune:]
    levels = np.array(cfg.levels)
    M = len(cfg.moneyness)
    cache = {}

    def block(t):
        bi = int(wfS.block_of[t])
        if bi not in cache:
            zS, zV = wfS.z_insample[bi], wfV.z_insample[bi]
            rho = float(np.corrcoef(zS, zV)[0, 1])
            f1, f2 = unmix_eigen(zS, zV, rho)
            laws = {'normal': (NormalLaw(), NormalLaw())}
            if 'skewt' in cfg.models:
                laws['skewt'] = (HansenSkewT(f1), HansenSkewT(f2))
            cache[bi] = _Block(rho, laws, np.column_stack([zS, zV]), (f1, f2), wfS.params[bi], wfV.params[bi])
        return cache[bi]

    def draw(blk, name, U, rho_t, k):
        if name == 'fhs':
            idx = np.minimum((U[..., 0] * len(blk.rows)).astype(int), len(blk.rows) - 1)
            return blk.rows[idx, 0], blk.rows[idx, 1]
        if name == 'gpd':
            if ('gpd', k) not in blk.laws:
                blk.laws[('gpd', k)] = tuple(SemiParamGPD(f, cfg.tail_frac, k, cfg.xi_max) for f in blk.factors)
            l1, l2 = blk.laws[('gpd', k)]
        else:
            l1, l2 = blk.laws[name]
        return mix_eigen(l1.ppf(U[..., 0]), l2.ppf(U[..., 1]), rho_t)

    def simulate(t, blk, name, rho_t, U, k):
        pS, pV = blk.pS, blk.pV
        x1, x2 = draw(blk, name, U, rho_t, k)
        if cfg.paths:
            return gjr_paths(pS, wfS.sigma2[t], x1), gjr_paths(pV, wfV.sigma2[t], x2)
        hS = math.sqrt(horizon_variance(wfS.sigma2[t], pS, H))
        hV = math.sqrt(horizon_variance(wfV.sigma2[t], pV, H))
        return pS.mu * H + hS * x1, pV.mu * H + hV * x2

    def run(window_ts, gam, models, k):
        W = len(window_ts)
        res = {m: {'var': np.zeros((W, M, len(levels))), 'pit': np.zeros((W, M))} for m in models}
        aux = []
        realized = np.zeros((W, M))
        for w, t in enumerate(window_ts):
            blk = block(t)
            rng = np.random.default_rng([cfg.seed, t])
            shape = (cfg.n_sims, H, 2) if cfg.paths else (cfg.n_sims, 2)
            U = np.clip(rng.random(shape), 1e-12, 1 - 1e-12)
            S0, v0 = S[t], max(vmap(V[t]), cfg.vol_floor)
            S1, v1 = S[t + H], max(vmap(V[t + H]), cfg.vol_floor)
            drawn = {}
            for mi, mny in enumerate(cfg.moneyness):
                c = BSMDigital(mny * S0, T, cfg.rate, cfg.dividend, cfg.skew)
                p0 = c.price(S0, v0)
                realized[w, mi] = -cfg.position * (c.price(S1, v1, T - hy) - p0)
                rho_t = temper_rho(blk.rho, gam[mi], cfg.rho_floor)
                for name in models:
                    base = 'gpd' if name == 'spectral' else name
                    key = (base, None if base == 'fhs' else gam[mi])
                    if key not in drawn:
                        dlogS, dv = simulate(t, blk, base, rho_t, U, k)
                        drawn[key] = (S0 * np.exp(dlogS), np.maximum(v0 + dv, cfg.vol_floor))
                    S_new, v_new = drawn[key]
                    if name == 'spectral':
                        xs = np.column_stack([S_new - S0, v_new - v0])
                        Sig = np.cov(xs.T)
                        L = cov_factor(Sig)
                        z = np.linalg.solve(L, xs.T).T
                        radius = float(np.quantile(np.linalg.norm(z, axis=1), levels.max()))
                        rv = risk_vector(c, (S0, v0), Sig, hy, cfg.position, radius=radius, n_theta=360)
                        pnl = rv.jet(z) if rv.action == 'taylor4' else cfg.position * (c.price(S_new, v_new, T - hy) - p0)
                        aux.append((w, mi, rv.action, rv.ratio_gate, rv.crossing, radius))
                    else:
                        pnl = cfg.position * (c.price(S_new, v_new, T - hy) - p0)
                    loss = -pnl
                    res[name]['var'][w, mi] = np.quantile(loss, levels)
                    res[name]['pit'][w, mi] = (np.sum(loss <= realized[w, mi]) + 0.5) / (len(loss) + 1)
        return res, realized, aux

    def tune_score(res, real):
        return np.array([np.mean([pinball(real[:, mi], res['gpd']['var'][:, mi, li], lv).mean()
                                  for li, lv in enumerate(levels)]) for mi in range(M)])

    # --- tuning on the first tune_frac of windows only: tail index, then tempering power per
    # strike tied across strikes by a curvature penalty
    k = cfg.tail_k
    if k is None and tune_ts:
        k_scores = [tune_score(*run(tune_ts, [1.0] * M, ('gpd',), kk)[:2]).mean() for kk in cfg.tail_ks]
        k = cfg.tail_ks[int(np.argmin(k_scores))]
    grid = list(cfg.gammas)
    scores = np.zeros((M, len(grid)))
    for gi, g in enumerate(grid):
        scores[:, gi] = tune_score(*run(tune_ts, [g] * M, ('gpd',), k)[:2])
    chosen = _choose_tied(scores, grid, cfg.curvature_penalty) if tune_ts else np.ones(M)
    log(f'Tuning windows {len(tune_ts)}, evaluation windows {len(eval_ts)}; tail index '
        f'{"Hill" if k is None else k}; tempering power per strike '
        + ', '.join(f'{m:.2f}:{g:.2f}' for m, g in zip(cfg.moneyness, chosen)))

    res, real, aux = run(eval_ts, list(chosen), cfg.models, k)
    eval_dates = np.asarray(dates)[eval_ts]
    mid = M // 2
    summary = {'chosen_gamma': chosen, 'tail_k': k, 'eval_dates': eval_dates, 'realized': real, 'results': res}

    log('\nModel     level  breach%  nominal  Kupiec p  Chr. cc p  Berk. p  pinball    dPinball vs normal [95% CI]')
    base_pb = {lv: sum(pinball(real[:, mi], res['normal']['var'][:, mi, li], lv) for mi in range(M))
               for li, lv in enumerate(levels)} if 'normal' in res else None
    for name in cfg.models:
        bk = berkowitz(res[name]['pit'][:, mid])
        for li, lv in enumerate(levels):
            hits = real > res[name]['var'][:, :, li]
            kp = kupiec(hits.ravel(), 1 - lv)[1]
            cc = christoffersen(hits[:, mid], 1 - lv)['p_cc']
            pb_w = sum(pinball(real[:, mi], res[name]['var'][:, mi, li], lv) for mi in range(M))
            diff = ''
            if base_pb is not None and name != 'normal':
                mean, (lo, hi) = circular_block_bootstrap(pb_w - base_pb[lv], block=4, seed=li)
                diff = f'{mean:+.2e} [{lo:+.2e}, {hi:+.2e}]'
            log(f'{name:9s} {lv:5.2f}  {100 * hits.mean():6.2f}  {100 * (1 - lv):6.2f}   {kp:7.3f}   '
                f'{cc:7.3f}   {bk["p"]:6.3f}  {pb_w.mean() / M:.3e}  {diff}')
    if aux:
        acts = np.array([a[2] == 'taylor4' for a in aux])
        log(f'\nSpectral gate: Taylor-4 trusted in {100 * acts.mean():.1f}% of windows, ratio gate passed in '
            f'{100 * np.mean([a[3] for a in aux]):.1f}%, crossings in {100 * np.mean([a[4] for a in aux]):.1f}%; '
            f'tail-consistent radius mean {np.mean([a[5] for a in aux]):.3f} vs Gaussian {GAUSS_99_RADIUS:.3f}')

    top = int(np.argmax(levels))
    lv = levels[top]
    if 'gpd' in res:
        pid = PIDVaRScale(alpha=lv)
        hits_pid = []
        for w in range(len(eval_ts)):
            hits_pid.append(real[w, mid] > res['gpd']['var'][w, mid, top] * pid.factor)
            pid.update(hits_pid[-1])
        raw = real[:, mid] > res['gpd']['var'][:, mid, top]
        log(f'\nPID-scaled gpd VaR {lv:.2f} (ATM): breach {100 * np.mean(hits_pid):.2f}% vs raw '
            f'{100 * raw.mean():.2f}% (nominal {100 * (1 - lv):.2f}%), final factor {pid.factor:.3f}')
        _, alarms = cusum(raw.astype(float) - (1 - lv), k=0.0, h=3.0)
        log('CUSUM breach alarms: ' + (', '.join(str(eval_dates[a]) for a in alarms) if alarms else 'none'))
        summary['pid_hits'] = np.array(hits_pid)
        if signals is not None:
            sd, sv = signals
            pos = np.searchsorted(sd, eval_dates, side='right') - 1
            ok = pos >= 0
            sig = np.where(ok, sv[np.clip(pos, 0, None)], np.nan)
            m = np.isfinite(sig)
            if m.sum() >= 20:
                high = sig >= np.nanmedian(sig)
                tbl = [[int(raw[m & high].sum()), int((~raw[m & high]).sum())],
                       [int(raw[m & ~high].sum()), int((~raw[m & ~high]).sum())]]
                ratio = real[:, mid] / np.maximum(res['gpd']['var'][:, mid, top], 1e-12)
                log(f'Daily signal: breach rate high {100 * raw[m & high].mean():.2f}% vs low '
                    f'{100 * raw[m & ~high].mean():.2f}% (Fisher p {stats.fisher_exact(tbl)[1]:.3f}); '
                    f'kurtosis of loss/VaR high {stats.kurtosis(ratio[m & high], fisher=False):.2f} vs low '
                    f'{stats.kurtosis(ratio[m & ~high], fisher=False):.2f}')
            else:
                log('Daily signal: fewer than 20 evaluation windows covered; skipped.')

    if out_csv:
        with open(out_csv, 'w', newline='') as f:
            wr = csv.writer(f)
            wr.writerow(['date', 'moneyness', 'model', 'realized_loss', 'pit'] + [f'var_{l:.2f}' for l in levels])
            for name in cfg.models:
                for w, d in enumerate(eval_dates):
                    for mi, mny in enumerate(cfg.moneyness):
                        wr.writerow([str(d), mny, name, f'{real[w, mi]:.8g}', f'{res[name]["pit"][w, mi]:.6f}']
                                    + [f'{x:.8g}' for x in res[name]['var'][w, mi]])
        log(f'Wrote {out_csv}')
    return summary


def run_horizon_backtest(dates, S, H=5, levels=(0.95, 0.975, 0.99), block=63, min_train=504,
                         tail_frac=0.10, tail_k=None, n_paths=20000, seed=0, log=print):
    """Walk-forward h-day VaR of a long unit position in the underlying: rescaled 1-day law vs paths.

    'scaled' is mu H + sqrt(horizon_variance) times the 1-day law's quantile, the shortcut the
    spectral VaR gate used for h > 1. 'path' runs H daily GJR steps through the same law
    (gjr_paths). Both use each block's walk-forward fit and SemiParamGPD on its in-sample residuals;
    windows are non-overlapping. Loss is the fraction of notional, 1 - exp(R)."""
    r = np.diff(np.log(np.asarray(S, dtype=float)))
    wf = walk_forward_gjr(r, block, min_train)
    ts = np.arange(min_train, len(r) - H + 1, H)
    lv = np.asarray(levels, dtype=float)
    real = np.array([1.0 - math.exp(r[t:t + H].sum()) for t in ts])
    var = {m: np.zeros((len(ts), len(lv))) for m in ('scaled', 'path')}
    blocks = wf.block_of[ts]
    for b in np.unique(blocks):
        w = np.where(blocks == b)[0]
        p, law = wf.params[b], SemiParamGPD(wf.z_insample[b], tail_frac, tail_k)
        s2 = wf.sigma2[ts[w]]
        hv = np.array([horizon_variance(x, p, H) for x in s2])
        q1 = np.asarray(law.ppf(1.0 - lv), dtype=float)
        var['scaled'][w] = 1.0 - np.exp(p.mu * H + np.sqrt(hv)[:, None] * q1[None, :])
        qp = horizon_quantiles(p, s2, H, 1.0 - lv, law=law, n=n_paths, seed=[seed, int(b)])
        var['path'][w] = 1.0 - np.exp(qp.T)
    log(f'{H}-day VaR, long unit position, {len(ts)} non-overlapping windows '
        f'{dates[ts[0] + 1]} .. {dates[ts[-1] + H]}')
    log('model    level  breach%  nominal  Kupiec p  Chr. cc p  pinball     d(path - scaled)  power  n for 80%')
    out = {'n': len(ts), 'real': real, 'var': var, 'diff': {}}
    for li, a in enumerate(lv):
        pb = {m: pinball(real, var[m][:, li], a) for m in var}
        d = pb['path'] - pb['scaled']
        pw = paired_power(d, block=4)
        out['diff'][float(a)] = d
        for m in var:
            hits = real > var[m][:, li]
            extra = (f'  {pw["mean"]:+.2e} +- {pw["se"]:.1e}  {pw["power"]:.2f}  {pw["n_needed"]:9.0f}'
                     if m == 'path' else '')
            log(f'{m:8s} {a:5.3f}  {100 * hits.mean():6.2f}  {100 * (1 - a):6.2f}   {kupiec(hits, 1 - a)[1]:7.3f}'
                f'   {christoffersen(hits, 1 - a)["p_cc"]:7.3f}   {pb[m].mean():.3e}{extra}')
        out.setdefault('hits', {})[float(a)] = {m: real > var[m][:, li] for m in var}
    return out


# ---------------------------------------------------------------------------
# 13. Event-market backtests
# ---------------------------------------------------------------------------

def read_contracts(path):
    """CSV with date, expiry, strike, outcome and optional bid, ask (probability units)."""
    with open(path, newline='', encoding='utf-8-sig') as f:
        rows = list(csv.DictReader(f))
    keys = {k.lower().strip(): k for k in rows[0]}
    get = lambda r, k, default=np.nan: r[keys[k]] if k in keys and r[keys[k]] != '' else default
    out = {'date': np.array([_parse_date(get(r, 'date')) for r in rows]),
           'expiry': np.array([_parse_date(get(r, 'expiry')) for r in rows]),
           'strike': np.array([float(get(r, 'strike')) for r in rows]),
           'outcome': np.array([float(get(r, 'outcome')) for r in rows]),
           'bid': np.array([float(get(r, 'bid')) for r in rows]),
           'ask': np.array([float(get(r, 'ask')) for r in rows])}
    return out


def synthetic_contracts(dates, S, V, every=5, tenor=21, offsets=(-0.04, -0.02, 0.0, 0.02, 0.04),
                        skew=-0.3, seed=3):
    """'Index above K' contracts with market quotes = distorted p_Q + noise, resolved on the path."""
    rng = np.random.default_rng(seed)
    rows = {k: [] for k in ('date', 'expiry', 'strike', 'outcome', 'bid', 'ask')}
    for t in range(0, len(S) - tenor, every):
        for o in offsets:
            K = S[t] * (1 + o)
            T = tenor / TRADING_DAYS
            pq = float(BSMDigital(K, T, skew=skew).probability(S[t], V[t] / 100))
            mid = float(expit(0.85 * logit(np.clip(pq, 1e-4, 1 - 1e-4)) + 0.10 + 0.15 * rng.standard_normal()))
            rows['date'].append(dates[t])
            rows['expiry'].append(dates[t + tenor])
            rows['strike'].append(K)
            rows['outcome'].append(float(S[t + tenor] > K))
            rows['bid'].append(max(mid - 0.01, 0.01))
            rows['ask'].append(min(mid + 0.01, 0.99))
    return {k: np.array(v) for k, v in rows.items()}


def run_event_backtest(con, dates, S, V, skew=-0.3, rate=0.04, dividend=0.015, refit_every=63,
                       threshold=0.02, fee=0.01, log=print):
    """Score p_Q (raw and walk-forward recalibrated) against outcomes and the market."""
    idx = np.searchsorted(dates, con['date'], side='right') - 1
    T = (con['expiry'] - con['date']).astype('timedelta64[D]').astype(float) / 365.0
    ok = (idx >= 0) & (T > 0) & np.isfinite(con['outcome'])
    pq = np.full(len(T), np.nan)
    for i in np.where(ok)[0]:
        pq[i] = float(BSMDigital(con['strike'][i], T[i], rate, dividend, skew)
                      .probability(S[idx[i]], V[idx[i]] / 100))
    ok &= np.isfinite(pq)
    d, e, y, p = con['date'][ok], con['expiry'][ok], con['outcome'][ok], pq[ok]
    groups = np.round(con['strike'][ok] / S[idx[ok]], 2)
    p_lp = walk_forward_recalibrate(d, e, p, y, refit_every, 'logit_power')
    p_pr = walk_forward_recalibrate(d, e, p, y, refit_every, 'probit')
    rows = [('p_Q raw', p), ('p_Q logit-power', p_lp), ('p_Q probit', p_pr)]
    mid = (con['bid'][ok] + con['ask'][ok]) / 2
    if np.isfinite(mid).all():
        rows.append(('market mid', mid))
    log(f'{ok.sum()} contracts, {len(np.unique(e))} expiries')
    log('forecast            Brier    log loss   AUC    within-strike AUC')
    for name, q in rows:
        log(f'{name:18s} {brier(q, y):.4f}   {log_loss(q, y):.4f}   {auc(q, y):.3f}   '
            f'{within_strike_auc(q, y, groups):.3f}')
    log('reliability (logit-power): ' + '  '.join(f'[{a:.1f},{b:.1f}) n={n} p={pm:.2f} f={fm:.2f}'
                                                  for a, b, n, pm, fm in reliability(p_lp, y, 5)))
    if np.isfinite(mid).all():
        pnl, side = edge_trades(p_lp, con['bid'][ok], con['ask'][ok], y, threshold, fee)
        traded = side != 0
        if traded.any():
            mean, (lo, hi) = cluster_bootstrap(pnl[traded], e[traded])
            log(f'edge trades: {traded.sum()} trades, mean P&L {mean:+.4f} per contract '
                f'[{lo:+.4f}, {hi:+.4f}] (cluster bootstrap by expiry)')
        else:
            log('edge trades: none cleared threshold + fee')
    return {'p': p, 'p_lp': p_lp, 'y': y}


def synthetic_ladders(n=400, K=6, overround=1.06, bias=0.8, seed=5):
    rng = np.random.default_rng(seed)
    true = rng.dirichlet(np.full(K, 1.5), size=n)
    mids = power_closure(true, bias, axis=1) * overround
    outcome = np.array([rng.choice(K, p=p) for p in true])
    return mids, outcome, true


def run_ladder_backtest(mids, outcome, log=print):
    n = len(mids)
    devig = np.array([power_devig(m)[1] for m in mids])
    half = n // 2
    k = fit_ladder_power(mids[:half], outcome[:half])
    fitted = power_closure(mids[half:], k, axis=1)
    log(f'{n} ladders; mean overround {np.mean(mids.sum(1)):.3f}; fitted power on first half {k:.3f}')
    log('forecast (second half)       log score   multi-Brier   RPS')
    for name, P in (('normalised mids', closure(mids[half:], axis=1)), ('power de-vig', devig[half:]),
                    ('fitted power-and-closure', fitted)):
        s = ladder_scores(P, outcome[half:])
        log(f'{name:28s} {s["log_score"]:9.4f}   {s["brier"]:9.4f}   {s["rps"]:.4f}')


# ---------------------------------------------------------------------------
# 14. Self-test and demonstration
# ---------------------------------------------------------------------------

def _demo_bsm(position=1.0):
    S0, v0 = 4500.0, 0.16
    model = BSMDigital(strike=S0, expiry=21 / TRADING_DAYS, skew=-0.3)
    h = 5
    sdS, sdv, rho = S0 * 0.011 * math.sqrt(h), 0.011 * math.sqrt(h), -0.75
    Sigma = np.array([[sdS ** 2, rho * sdS * sdv], [rho * sdS * sdv, sdv ** 2]])
    return model, np.array([S0, v0]), Sigma, h / TRADING_DAYS


def _demo_latent():
    model = LatentEvent(strike=0.0, expiry=30.0)
    h = 5.0
    Sigma = np.array([[1.0 * h, 0.0], [0.0, 0.04 * h]])
    return model, np.array([0.3, 1.0]), Sigma, h


# --- high-precision references for the self-test (decimal arithmetic, no extra dependencies).
# Double-precision checks bottom out at rounding (finite differences of 4th derivatives near 1e-8,
# quadrature sums near 1e-16); evaluating the reference side in 60-digit decimals removes that
# floor, so what remains measures the double-precision implementation itself.

_HP_PREC = 60
_HP_PI = Decimal('3.141592653589793238462643383279502884197169399375105820974944592307816')


def _hp(x):
    return x if isinstance(x, Decimal) else Decimal(float(x))  # exact value of the double


def _hp_npdf(x):
    return (-(x * x) / 2).exp() / (2 * _HP_PI).sqrt()


def _hp_ncdf(x):
    """Phi(x) = 1/2 + phi(x) sum x^(2n+1) / (1*3*...*(2n+1)): every term shares a sign, no cancellation."""
    term = total = x
    n = 0
    tiny = Decimal(10) ** -(_HP_PREC + 5)
    while abs(term) > tiny:
        n += 1
        term = term * x * x / (2 * n + 1)
        total += term
    return Decimal('0.5') + _hp_npdf(x) * total


def _hp_digital(model, S, v, T):
    r, q, b, K, T = (_hp(z) for z in (model.rate, model.dividend, model.skew, model.strike, T))
    F = S * ((r - q) * T).exp()
    lnFK = (F / K).ln()
    sig = v - b * lnFK
    sq = T.sqrt()
    d2 = (lnFK - sig * sig * T / 2) / (sig * sq)
    return (-(r * T)).exp() * (_hp_ncdf(d2) - _hp_npdf(d2) * sq * b)


def _hp_fd_weights(k, acc):
    """Exact rational central-difference weights for the k-th derivative, accuracy order acc."""
    from fractions import Fraction
    m = (k + 1) // 2 - 1 + (acc + 1) // 2
    offs = list(range(-m, m + 1))
    n = len(offs)
    A = [[Fraction(o) ** i for o in offs] + [Fraction(factorial(k) if i == k else 0)] for i in range(n)]
    for c in range(n):
        p = next(r for r in range(c, n) if A[r][c] != 0)
        A[c], A[p] = A[p], A[c]
        A[c] = [x / A[c][c] for x in A[c]]
        for r in range(n):
            if r != c and A[r][c] != 0:
                A[r] = [x - A[r][c] * y for x, y in zip(A[r], A[c])]
    return offs, [row[-1] for row in A]


def _hp_fd(f, mono, h, acc=4):
    """Mixed partial derivative by tensor-product stencils, all in decimal arithmetic."""
    o1, w1 = _hp_fd_weights(mono[0], acc) if mono[0] else ([0], [1])
    o2, w2 = _hp_fd_weights(mono[1], acc) if mono[1] else ([0], [1])
    total = Decimal(0)
    for a, wa in zip(o1, w1):
        for b, wb in zip(o2, w2):
            w = wa * wb
            if w:
                total += Decimal(w.numerator if hasattr(w, 'numerator') else w) / \
                    Decimal(w.denominator if hasattr(w, 'denominator') else 1) * f(a * h, b * h)
    return total / h ** sum(mono)


def _hp_hermegauss(n):
    """Gauss-Hermite nodes and weights for the standard normal, refined to decimal precision."""
    out = []
    for x0 in hermegauss(n)[0]:
        x = _hp(x0)
        for _ in range(5):
            h0, h1 = Decimal(1), x
            for k in range(1, n):
                h0, h1 = h1, x * h1 - k * h0
            x -= h1 / (n * h0)  # He_n' = n He_(n-1)
        h0, h1 = Decimal(1), x
        for k in range(1, n):
            h0, h1 = h1, x * h1 - k * h0
        out.append((x, Decimal(factorial(n)) / (n * n * h0 * h0)))
    return out


def selftest():
    failures = []

    def check(name, ok, detail):
        print(f'[{"ok" if ok else "FAIL"}] {name}: {detail}')
        if not ok:
            failures.append(name)

    print('bsm_event self-test (NumPy %s, SciPy %s)' % (np.__version__, __import__('scipy').__version__))

    a, b = Jet.variables((0.7, 1.3))
    err = max(np.abs(((a * b).exp().log() - a * b).c).max(), np.abs(((a / b) * b - a).c).max(),
              np.abs((a.sqrt() ** 2 - a).c).max())
    check('jet algebra', err < 1e-12, f'max coefficient error {err:.1e}')

    model, x0, Sigma, hz = _demo_bsm()
    L = cov_factor(Sigma)
    J = pnl_jet(model, x0, L, 0.0)
    f = lambda z: exact_pnl(model, x0, L, z, 0.0)
    with localcontext() as ctx:
        ctx.prec = _HP_PREC
        Lh = [[_hp(L[i, j]) for j in range(2)] for i in range(2)]
        S0, v0, T0 = _hp(x0[0]), _hp(x0[1]), _hp(model.expiry)
        p0 = _hp_digital(model, S0, v0, T0)
        fh = lambda a, b: _hp_digital(model, S0 + Lh[0][0] * a + Lh[0][1] * b,
                                      v0 + Lh[1][0] * a + Lh[1][1] * b, T0) - p0
        rel = {}
        for mono in _tables(2, DEG)[0][1:]:
            ref = _hp_fd(fh, mono, Decimal('1e-6'), acc=4)
            jv = J.derivative(mono)
            if abs(jv) > 1e-9:
                rel.setdefault(sum(mono), []).append(float(abs(_hp(jv) - ref) / abs(ref)))
    detail = ', '.join(f'order {k}: {max(v):.1e}' for k, v in sorted(rel.items()))
    check('jet vs finite difference (60-digit reference)', max(max(v) for v in rel.values()) < 1e-10,
          detail + ' (paper: 2e-6 to 2e-8)')

    # Residual of the degree-d expansion behaves like c e^(d+1) (1 + kappa e) before rounding sets in;
    # fitting the next-order factor removes the pre-asymptotic bias a straight log-log line has.
    u = np.array([0.6, 0.8])
    eps = np.geomspace(0.02, 0.5, 14)
    slopes = {}
    for deg in (4, 6):
        Jd = pnl_jet(model, x0, L, 0.0, deg=deg)
        res = np.array([abs(f(e * u[None])[0] - Jd(e * u[None])[0]) for e in eps])
        keep = res > 1e-13  # well above the ~1e-16 rounding floor of the repricing
        le, lr = np.log(eps[keep]), np.log(res[keep])
        fit = optimize.least_squares(lambda th: th[0] + th[1] * le + np.log(np.abs(1 + th[2] * np.exp(le))) - lr,
                                     [0.0, deg + 1.0, 0.0])
        slopes[deg] = fit.x[1]
    check('Taylor convergence order', abs(slopes[4] - 5) < 0.05 and abs(slopes[6] - 7) < 0.03,
          f'degree-4 residual exponent {slopes[4]:.3f} (5 expected), degree-6 {slopes[6]:.3f} (7 expected) '
          f'(paper: 5.05, 7.03)')

    K, hK = 4600.0, 0.5
    dig = BSMDigital(strike=K, expiry=21 / TRADING_DAYS)
    D = lambda h: -(dig.call(x0[0], x0[1], K + h) - dig.call(x0[0], x0[1], K - h)) / (2 * h)
    fd = (4 * D(hK / 2) - D(hK)) / 3
    err = abs(fd - dig.price(x0[0], x0[1]))
    check('digital = -dC/dK with smile', err < 1e-9, f'absolute error {err:.1e}')

    lat = LatentEvent(strike=0.0, expiry=30.0)
    xq, wq = hermegauss(60)
    wq = wq / SQRT2PI
    xs, s, dt = 0.4, 1.0, 1.0
    # double precision: differences from today's price, compensated sum (weights don't sum to 1 exactly)
    drift = math.fsum(wq * (lat.price(xs + s * math.sqrt(dt) * xq, s, 30.0 - dt) - float(lat.price(xs, s))))
    with localcontext() as ctx:  # 60 digits: trapezoid rule, exponentially convergent for this integrand
        ctx.prec = _HP_PREC
        h, X, sd, tau = Decimal('0.2'), _hp(xs), _hp(s), Decimal(30)
        mean = sum(h * _hp_npdf(h * i) * _hp_ncdf((X + sd * h * i) / (sd * (tau - 1).sqrt())) for i in range(-80, 81))
        hp_drift = abs(mean - _hp_ncdf(X / (sd * tau.sqrt())))
    check('latent price is a martingale', abs(drift) < 1e-16 and hp_drift < Decimal('1e-40'),
          f'mean price change {drift:.1e} in double precision, {float(hp_drift):.1e} at 60 digits (paper: ~1e-17)')

    rng = np.random.default_rng(1)
    for s_true, want in ((1.0, 1.0), (2.0, 4.0)):
        # stay away from resolution, where prices saturate at 0/1 and the transform is unstable
        tau = np.arange(4000, 2000, -1, dtype=float) * 0.25
        x = np.concatenate([[0.0], np.cumsum(s_true * math.sqrt(0.25) * rng.standard_normal(len(tau) - 1))])
        prices = ndtr(x / np.sqrt(tau))  # market prices with intensity normalised to 1
        est = estimate_intensity(prices, tau, 0.25)
        check(f'intensity estimate (s={s_true})', abs(est['rv_per_time'] / want - 1) < 0.12,
              f'rv per unit time {est["rv_per_time"]:.3f} (expected {want:.1f})')

    nodes, wts = gauss_hermite_2d(12)
    terms = J.terms(nodes)
    quad = np.array([np.sum(wts * terms[k] ** 2) for k in range(1, 5)])
    M = J.hessian()
    closed = 0.25 * (np.trace(M) ** 2 + 2 * np.trace(M @ M))
    e1 = abs(closed - quad[1]) / quad[1]
    e2 = np.max(np.abs(energy_by_order(J) - quad) / quad)
    with localcontext() as ctx:  # the same comparison with 60-digit Gauss-Hermite nodes and weights
        ctx.prec = _HP_PREC
        gh = _hp_hermegauss(12)
        c2 = [(e, _hp(J.c[k])) for k, e in enumerate(_tables(2, DEG)[0]) if sum(e) == 2]
        q2 = sum(wa * wb * sum(c * za ** e[0] * zb ** e[1] for e, c in c2) ** 2 for za, wa in gh for zb, wb in gh)
        Mh = [[_hp(M[i, j]) for j in range(2)] for i in range(2)]
        tr, tr2 = Mh[0][0] + Mh[1][1], sum(Mh[i][j] * Mh[j][i] for i in range(2) for j in range(2))
        e1_hp = abs((tr * tr + 2 * tr2) / 4 - q2) / q2
    check('second-order energy closed form', e1 < 1e-15 and e2 < 1e-12 and e1_hp < Decimal('1e-40'),
          f'relative error vs Gauss-Hermite {e1:.1e} in double precision, {float(e1_hp):.1e} at 60 digits '
          f'(paper: 3e-16); Gram energies all orders {e2:.1e}')

    errs = [abs(temper_rho(r, g, 0.0) - math.tanh(g * math.atanh(r)))
            for r in np.linspace(-0.9, 0.9, 7) for g in (0.5, 0.8, 1.3)]
    C = np.array([[1, 0.6, 0.2], [0.6, 1, 0.4], [0.2, 0.4, 1.0]])
    e_id = np.abs(spectral_correlation(C, 1.0, 0.0) - C).max()
    check('Aitchison powering = Fisher-z scaling', max(errs) < 1e-14 and temper_rho(0.95, 1.0) == 0.8 and e_id < 1e-12,
          f'max error {max(errs):.1e}; floor caps 0.95 -> {temper_rho(0.95, 1.0)}; identity powering {e_id:.1e}')

    p = GJRParams(omega=2e-6, alpha=0.03, gamma=0.10, beta=0.88)
    s2n, h = 1.5e-4, 10
    loop = sum(p.longrun + p.persistence ** (k - 1) * (s2n - p.longrun) for k in range(1, h + 1))
    err = abs(horizon_variance(s2n, p, h) - loop) / loop
    check('horizon variance closed form', err < 1e-12, f'relative error {err:.1e}')

    hv = horizon_variance(s2n, p, h)
    m2 = float(np.mean(gjr_paths(p, s2n, np.random.default_rng(9).standard_normal((200000, h))) ** 2))
    flat = GJRParams(omega=1e-4, alpha=0.0, gamma=0.0, beta=0.0)
    qf = horizon_quantiles(flat, 1e-4, h, [0.01], law=NormalLaw(), n=200000, seed=9)[0]
    qf_rel = qf / (0.01 * math.sqrt(h) * ndtri(0.01))
    q_std = horizon_quantiles(p, s2n, h, [0.01], law=NormalLaw(), n=200000, seed=9)[0] / math.sqrt(hv)
    q_one = horizon_quantiles(p, s2n, 1, [0.01], law=NormalLaw())[0]
    check('GJR paths vs the rescaled 1-day law',
          abs(m2 / hv - 1) < 0.02 and abs(qf_rel - 1) < 0.02 and q_std < ndtri(0.01) - 0.15
          and q_one == math.sqrt(s2n) * ndtri(0.01),
          f'E[R^2] / horizon variance {m2 / hv:.4f}; flat variance quantile {qf_rel:.4f} x sqrt(h) Gaussian; '
          f'standardised {h}-day 1% quantile {q_std:.3f} vs rescaled {ndtri(0.01):.3f}; h = 1 exact')

    rng = np.random.default_rng(42)
    n = 6000
    s2 = p.longrun
    r = np.empty(n)
    for t in range(n):
        e = math.sqrt(s2) * rng.standard_normal()
        r[t] = e
        s2 = p.omega + (p.alpha + p.gamma * (e < 0)) * e * e + p.beta * s2
    fit, _, z = fit_gjr(r)
    ok = abs(fit.alpha - p.alpha) < 0.04 and abs(fit.gamma - p.gamma) < 0.07 and abs(fit.beta - p.beta) < 0.06
    check('GJR-GARCH QML recovery', ok and fit.persistence < 0.999,
          f'alpha {fit.alpha:.3f} gamma {fit.gamma:.3f} beta {fit.beta:.3f} (true .030 .100 .880), '
          f'persistence {fit.persistence:.3f}')

    zt = stats.t.rvs(5, size=5000, random_state=3) / math.sqrt(5 / 3)
    law = SemiParamGPD(zt)
    t = np.linspace(-30, 30, 200001)
    uu = expit(t)
    dw = uu * (1 - uu) * (t[1] - t[0])
    xq = law.ppf(uu)
    m1, m2 = np.sum(dw * xq), np.sum(dw * xq * xq)
    st = HansenSkewT(eta=6.0, lam=-0.2)
    grid = np.linspace(0.001, 0.999, 999)
    rt = np.abs(st.cdf(st.ppf(grid)) - grid).max()
    xs = st.ppf(uu)
    sm1, sm2 = np.sum(dw * xs), np.sum(dw * xs * xs)
    ee1, ee2 = mix_eigen(*np.random.default_rng(4).standard_normal((2, 200000)), -0.6)
    rmix = np.corrcoef(ee1, ee2)[0, 1]
    check('tail laws', abs(m1) < 0.02 and abs(m2 - 1) < 0.05 and rt < 1e-8 and abs(sm1) < 0.01
          and abs(sm2 - 1) < 0.03 and abs(rmix + 0.6) < 0.01,
          f'GPD law mean {m1:+.3f} var {m2:.3f} (xi lo/hi {law.xi_lo:.2f}/{law.xi_hi:.2f}); skew-t roundtrip '
          f'{rt:.1e}, mean {sm1:+.3f} var {sm2:.3f}; mix_eigen corr {rmix:+.3f}')

    rng = np.random.default_rng(8)
    good = rng.random(5000) < 0.01
    clustered = np.zeros(5000, bool)
    for s0 in rng.choice(4990, 12, replace=False):
        clustered[s0:s0 + 4] = True
    pit_u = rng.random(3000)
    pit_bad = ndtr(1.4 * rng.standard_normal(3000))
    ok = (kupiec(good, 0.01)[1] > 0.01 and christoffersen(clustered, 0.01)['p_ind'] < 0.01
          and berkowitz(pit_u)['p'] > 0.01 and berkowitz(pit_bad)['p'] < 0.01)
    check('coverage tests', ok, f'Kupiec p {kupiec(good, 0.01)[1]:.2f} (calibrated), Christoffersen p_ind '
          f'{christoffersen(clustered, 0.01)["p_ind"]:.1e} (clustered), Berkowitz p {berkowitz(pit_u)["p"]:.2f} '
          f'(uniform) / {berkowitz(pit_bad)["p"]:.1e} (overdispersed)')

    pid = PIDVaRScale(alpha=0.99)
    for _ in range(200):
        pid.update(True)
    hi_int, hi_fac = pid.integral, pid.factor
    for _ in range(40):
        pid.update(False)
    recovered = pid.factor < hi_fac
    check('PID clamp and anti-windup', 1.95 < hi_fac <= 2.0 and hi_int < 30 and recovered,
          f'factor {hi_fac:.2f} at clamp, integral frozen at {hi_int:.2f}, factor {pid.factor:.2f} after 40 clean windows')

    inst = neighbour_strikes(model)
    Gm = np.column_stack([greeks(m, x0) for m in inst])
    gt = greeks(model, x0)
    w = np.linalg.lstsq(Gm, -gt, rcond=None)[0]
    resid = np.abs(Gm @ w + gt) / np.abs(gt)
    wc, _, _ = complete_sum_weights(model, inst, x0, L, hz, 4)
    poly = pnl_jet(model, x0, L, hz).c + sum(wi * pnl_jet(m, x0, L, hz).c for wi, m in zip(wc, inst))
    G = _gaussian_gram(2, DEG)
    pred = poly @ G @ poly
    Jp = Jet(poly)
    quad_e = np.sum(wts * Jp(nodes) ** 2)
    check('hedge algebra', resid.max() < 1e-8 and abs(pred - quad_e) / quad_e < 1e-10,
          f'hierarchical Greek residual {resid.max():.1e}; complete-sum E[P&L^2] Gram vs quadrature '
          f'{abs(pred - quad_e) / quad_e:.1e}')

    rng = np.random.default_rng(9)
    pp = rng.uniform(0.02, 0.98, 20000)
    yy = (rng.random(20000) < apply_logit_power(pp, (0.8, -0.2))).astype(float)
    th = fit_logit_power(pp, yy)
    k, devig = power_devig(np.array([0.30, 0.25, 0.22, 0.18, 0.11]))
    check('calibration maps', abs(th[0] - 0.8) < 0.05 and abs(th[1] + 0.2) < 0.05 and abs(devig.sum() - 1) < 1e-10,
          f'logit-power MLE gamma {th[0]:.3f} delta {th[1]:+.3f} (true 0.8, -0.2); power de-vig k* {k:.4f}, '
          f'sum {devig.sum():.12f}')

    print(f'\n{"All checks passed." if not failures else "FAILED: " + ", ".join(failures)}')
    return not failures


def demo():
    print('DEMONSTRATION on stylised inputs (not market evidence)\n')
    for label, pos in (('S&P digital, long', 1.0), ('S&P digital, short', -1.0)):
        model, x0, Sigma, hz = _demo_bsm()
        rv = risk_vector(model, x0, Sigma, hz, pos)
        print(format_risk_vector(rv, model.names, f'{label}: S={x0[0]:.0f}, v={x0[1]:.2f}, K={model.strike:.0f}, '
                                                  f'T=21d, 5-day horizon, rho(S,v)=-0.75'))
        print()
    model, x0, Sigma, hz = _demo_latent()
    rv = risk_vector(model, x0, Sigma, hz, 1.0)
    print(format_risk_vector(rv, model.names, f'Latent event, long: x={x0[0]}, s={x0[1]}, tau=30d, 5-day horizon'))

    print('\nHedging a long S&P digital with digitals at K(1-1%), K(1+1%), K(1+2%) (out-of-sample, exact repricing)')
    model, x0, Sigma, hz = _demo_bsm()
    inst = neighbour_strikes(model, (-1, 1, 2), 0.01)
    cp = [SmileOption(model, model.strike * (1 + 0.01 * o), k) for o in (-1, 1) for k in ('call', 'put')]
    print(format_hedges(hedge_comparison(model, inst, x0, Sigma, hz, call_put=cp)))
    res_tol = hedge_comparison(model, inst, x0, Sigma, hz, svd_tol=0.1)
    print(f'  complete-sum o4 with truncated-SVD threshold 0.1: RMS {res_tol[3].rms:.5f} '
          f'(rank kept {res_tol[3].rank})')

    law = HansenSkewT(eta=4.5, lam=-0.25)
    tail = lambda seed, n: np.column_stack([law.ppf(np.clip(np.random.default_rng(seed).random(n), 1e-12, 1 - 1e-12))
                                            for _ in range(1)] + [law.ppf(np.clip(np.random.default_rng(seed + 1).random(n), 1e-12, 1 - 1e-12))])
    print('\nSame hedge, shocks from a skewed-t law (eta 4.5, lambda -0.25); moment-matched fit on separate draws')
    print(format_hedges(hedge_comparison(model, inst, x0, Sigma, hz, moment_draws=tail(20, 50000),
                                         oos_draws=tail(30, 20000))))

    print('\nHedging a long latent-event contract with strikes K-0.25, K+0.25, K+0.5')
    model, x0, Sigma, hz = _demo_latent()
    inst = neighbour_strikes(model, (-1, 1, 2), 0.25)
    print(format_hedges(hedge_comparison(model, inst, x0, Sigma, hz)))
    print('\nSingle demonstrations on stylised inputs: they show the criteria can disagree '
          '(variance vs tail), not that one is better.')


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split('\n\n')[0],
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('--selftest', action='store_true')
    ap.add_argument('--demo', action='store_true')
    ap.add_argument('--backtest', action='store_true')
    ap.add_argument('--data', default=None, help='directory of FRED-style CSVs (<id>.csv)')
    ap.add_argument('--underlying', default='SPX', choices=sorted(UNDERLYINGS),
                    help='registered underlying for --data runs (see UNDERLYINGS)')
    ap.add_argument('--synthetic', action='store_true', help='use synthetic market data')
    ap.add_argument('--signals', default=None, help='daily signal CSV from bsm_microstructure.py')
    ap.add_argument('--paths', action='store_true', help='path simulation with H daily GJR steps')
    ap.add_argument('--sims', type=int, default=4000)
    ap.add_argument('--horizon', type=int, default=5)
    ap.add_argument('--out', default=None, help='write per-window backtest results to this CSV')
    ap.add_argument('--events', default=None, help='contracts CSV for the event-market backtest')
    ap.add_argument('--events-synthetic', action='store_true')
    ap.add_argument('--horizon-backtest', default=None, metavar='KEYS',
                    help="h-day VaR of the underlying, rescaled 1-day law vs GJR paths; comma-separated "
                         "underlyings or 'all' (needs --data; uses --horizon)")
    args = ap.parse_args(argv)

    if not any((args.selftest, args.demo, args.backtest, args.events, args.events_synthetic,
                args.horizon_backtest)):
        ap.print_help()
        return 0
    if args.selftest and not selftest():
        return 1
    if args.demo:
        demo()
    if args.horizon_backtest:
        if not args.data:
            ap.error('--horizon-backtest needs --data DIR')
        keys = list(UNDERLYINGS) if args.horizon_backtest == 'all' else args.horizon_backtest.split(',')
        runs = {}
        for key in keys:
            dates, S, _ = load_underlying(args.data, key)
            print(f'\n=== {key} ({UNDERLYINGS[key]["name"]})')
            runs[key] = run_horizon_backtest(dates, S, args.horizon)
        if len(runs) > 1:
            print(f'\nPooled over {", ".join(runs)} ({sum(o["n"] for o in runs.values())} windows; assets are '
                  'correlated, so pooled p-values are optimistic)')
            for a in next(iter(runs.values()))['diff']:
                cells = []
                for m in ('scaled', 'path'):
                    hits = np.concatenate([o['hits'][a][m] for o in runs.values()])
                    cells.append(f'{m} breach {100 * hits.mean():.2f}% (Kupiec p {kupiec(hits, 1 - a)[1]:.3f})')
                rel = [o['diff'][a].mean() / pinball(o['real'], o['var']['scaled'][:, list(o['diff']).index(a)],
                                                      a).mean() for o in runs.values()]
                better = sum(x < 0 for x in rel)
                print(f'  {a:.3f}: nominal {100 * (1 - a):.2f}%; ' + '; '.join(cells)
                      + f'; pinball path vs scaled {100 * np.mean(rel):+.1f}% on average, lower in {better}/{len(rel)}')
    if args.backtest:
        if args.data:
            u = UNDERLYINGS[args.underlying]
            dates, S, V = load_underlying(args.data, args.underlying)
            print(f'Loaded {len(S)} {u["name"]} observations {dates[0]} .. {dates[-1]}; vol: '
                  + (u['vol'] or 'point-in-time GJR forecast (no implied-vol index)'))
        elif args.synthetic:
            dates, S, V = synthetic_market()
            print(f'Synthetic market: {len(S)} days (exercises the code; not market evidence)')
        else:
            ap.error('--backtest needs --data DIR or --synthetic')
        cfg = BacktestConfig(n_sims=args.sims, horizon=args.horizon, paths=args.paths)
        if args.data:
            u = UNDERLYINGS[args.underlying]
            cfg = replace(cfg, days_per_year=u['days_per_year'], rate=u['rate'], dividend=u['dividend'],
                          skew=u['skew'])
        signals = load_daily_signals(args.signals) if args.signals else None
        run_var_backtest(dates, S, V, cfg, signals, args.out)
    if args.events or args.events_synthetic:
        if args.events:
            if not args.data:
                ap.error('--events needs --data DIR for the underlying state')
            dates, S, V = load_underlying(args.data, args.underlying)
            con = read_contracts(args.events)
            u = UNDERLYINGS[args.underlying]
            run_event_backtest(con, dates, S, V, skew=u['skew'], rate=u['rate'], dividend=u['dividend'])
            return 0
        else:
            dates, S, V = synthetic_market()
            con = synthetic_contracts(dates, S, V)
            print('Synthetic event contracts (exercise the code; not market evidence)')
        run_event_backtest(con, dates, S, V)
        if args.events_synthetic:
            print()
            run_ladder_backtest(*synthetic_ladders()[:2])
    return 0


if __name__ == '__main__':
    sys.exit(main())
