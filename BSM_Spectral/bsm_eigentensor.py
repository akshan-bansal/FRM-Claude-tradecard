#!/usr/bin/env python3
"""bsm_eigentensor.py -- eigenrepresentations against eigentensor representations of joint returns.

Research only: nothing on the order path imports this.

An eigenrepresentation decomposes the correlation matrix. An eigentensor representation decomposes
a higher-order array:
  coskew      third moments of whitened returns, S_ijk = E[x_i x_j x_k]
  cokurt      fourth moments of whitened returns less their elliptical part, see excess_cokurtosis
  horizon     correlation stacked over holding periods, asset x asset x horizon, by HOSVD

Whitening removes what correlation already explains. For an elliptical law (Gaussian, Student-t)
coskew and cokurt are then zero up to sampling noise, so anything left is structure a matrix cannot
carry. Eigenvalues of coskew and cokurt are Z-eigenpairs, A x^(m-1) = lambda x with |x| = 1, found by
the shifted symmetric higher-order power method (Kolda and Mayo, 2011). Unlike a matrix's, they are
not orthogonal, not unique to a start, and a tensor can hold many.

Two measures compare any of these, with a direction taken from each of two disjoint windows:
  eigenfidelity     the squared overlap of the later direction with the earlier one, 1 is the same
                    direction and k/N is what chance gives; moment capture is the later window's
                    moment along the earlier direction as a share of the most it could have been
  eigenpersistence  eigenfidelity above chance as the windows move apart, averaged over the lags

Reading them against shuffled dates separates what moved from what was only noise: a joint shuffle
keeps the population and removes time, so falling short of it means the structure really changed.

Install: python -m pip install numpy scipy
"""
from __future__ import annotations

import math

import bsm_event as be
import numpy as np
from scipy import stats

_SUBSCRIPTS = {2: 'ij,sj->si', 3: 'ijk,sj,sk->si', 4: 'ijkl,sj,sk,sl->si'}
NEAR_DUPLICATE = 1e-5
SETTLED = 1e-6       # largest |A x^(m-1) - lambda x|, relative to |A|, that counts as an eigenpair


# ---------------------------------------------------------------------------
# 1. Moment tensors of whitened returns
# ---------------------------------------------------------------------------

def devolatilize(R, lam=0.94, warm=60):
    """Per-asset EWMA: returns (Z, sigma) from bar ``warm`` on. sigma[t] uses returns before t only."""
    R = np.asarray(R, dtype=float)
    var = np.empty_like(R)
    var[0] = np.mean(R[:warm] ** 2, axis=0)
    for t in range(1, len(R)):
        var[t] = lam * var[t - 1] + (1.0 - lam) * R[t - 1] ** 2
    sigma = np.sqrt(var)
    return (R / sigma)[warm:], sigma[warm:]


def whitener(X):
    """Symmetric inverse square root of the sample covariance, and the square root itself.

    Symmetric rather than Cholesky so that whitened coordinates stay close to the assets, which is
    what lets a direction be compared across windows, and no ordering of the assets matters."""
    # one asset gives a 0-d covariance, which is still a 1 x 1 matrix
    lam, V = np.linalg.eigh(np.atleast_2d(np.cov(np.asarray(X, dtype=float), rowvar=False)))
    lam = np.maximum(lam, 1e-12)
    return (V / np.sqrt(lam)) @ V.T, (V * np.sqrt(lam)) @ V.T


def whiten(X):
    X = np.asarray(X, dtype=float)
    return (X - X.mean(axis=0)) @ whitener(X)[0]


def coskewness(x):
    return np.einsum('ti,tj,tk->ijk', x, x, x, optimize=True) / len(x)


def isotropic(n):
    """The symmetrised delta product, delta_ij delta_kl + delta_ik delta_jl + delta_il delta_jk."""
    d = np.eye(n)
    return (np.einsum('ij,kl->ijkl', d, d) + np.einsum('ik,jl->ijkl', d, d)
            + np.einsum('il,jk->ijkl', d, d))


def excess_cokurtosis(x):
    """Fourth moments less the Gaussian's, split into an isotropic part and the rest.

    An elliptical law's excess cokurtosis is kappa/3 times isotropic(n), so it says only how heavy
    the tails are overall. Returns (aniso, kappa): the remainder is what no elliptical law has."""
    n = x.shape[1]
    iso = isotropic(n)
    excess = np.einsum('ti,tj,tk,tl->ijkl', x, x, x, x, optimize=True) / len(x) - iso
    kappa = 3.0 * np.einsum('iijj->', excess) / (n * (n + 2))
    return excess - kappa / 3.0 * iso, float(kappa)


def contract(A, v):
    """A(v, ..., v) for a symmetric tensor and one direction."""
    out = np.asarray(A)
    for _ in range(out.ndim):
        out = out @ v
    return float(out)


def outer_power(v, m):
    out = np.asarray(v, dtype=float)
    for _ in range(m - 1):
        out = np.multiply.outer(out, v)
    return out


# ---------------------------------------------------------------------------
# 2. Z-eigenpairs
# ---------------------------------------------------------------------------

def _hopm(A, starts, max_iter, tol):
    """Shifted power method over a batch of starts. Each one climbs to a local maximum of A x^m."""
    m = A.ndim
    subscripts = _SUBSCRIPTS[m]
    shift = (m - 1) * float(np.linalg.norm(A))
    x = starts
    for _ in range(max_iter):
        step = (np.einsum(subscripts, A, *([x] * (m - 1)), optimize=True) if m > 2 else x @ A) + shift * x
        step /= np.linalg.norm(step, axis=1, keepdims=True)
        moved = np.max(np.linalg.norm(step - x, axis=1))
        x = step
        if moved < tol:
            break
    Ax = np.einsum(subscripts, A, *([x] * (m - 1)), optimize=True) if m > 2 else x @ A
    lam = np.sum(Ax * x, axis=1)
    residual = np.linalg.norm(Ax - lam[:, None] * x, axis=1)
    return lam, x, residual


def z_eigenpairs(A, n_starts=8, max_iter=1000, tol=1e-9, seed=0):
    """Distinct Z-eigenpairs of a symmetric tensor, largest |lambda| first, as (lam, V, residual).

    Starts are the coordinate axes plus random directions from a fixed seed, so the answer repeats.
    An even order is searched from both signs, or only its positive eigenvalues would be found; for
    an odd order (lambda, v) and (-lambda, -v) are one pair, reported with lambda >= 0."""
    A = np.asarray(A, dtype=float)
    n, m = A.shape[0], A.ndim
    if not np.linalg.norm(A) > 0:
        return np.zeros(1), np.eye(n)[:1], np.zeros(1)
    rng = np.random.default_rng(seed)
    random = rng.standard_normal((n_starts, n))
    starts = np.vstack([np.eye(n), random / np.linalg.norm(random, axis=1, keepdims=True)])
    lam, V, res = _hopm(A, starts, max_iter, tol)
    if m % 2 == 0:
        lam2, V2, res2 = _hopm(-A, starts, max_iter, tol)
        lam, V, res = np.concatenate([lam, -lam2]), np.vstack([V, V2]), np.concatenate([res, res2])
    # a start still moving when the iterations ran out is not an eigenpair, so it is not reported;
    # if none settled, the closest one is kept and its residual says so
    settled = np.flatnonzero(res <= SETTLED * np.linalg.norm(A))
    if settled.size == 0:
        settled = np.array([int(np.argmin(res))])
    order = settled[np.argsort(-np.abs(lam[settled]))]
    kept: list[int] = []
    for i in order:
        if all(abs(lam[i] - lam[j]) > NEAR_DUPLICATE or abs(V[i] @ V[j]) < 1.0 - NEAR_DUPLICATE for j in kept):
            kept.append(int(i))
    lam, V, res = lam[kept], V[kept], res[kept]
    # one sign per direction, so that a repeat of the same pair reads as the same vector
    if m % 2 == 0:
        V = V * np.where(V[np.arange(len(V)), np.argmax(np.abs(V), axis=1)] < 0, -1.0, 1.0)[:, None]
    else:
        V, lam = V * np.where(lam < 0, -1.0, 1.0)[:, None], np.abs(lam)
    return lam, V, res


def low_rank(A, rank, **kw):
    """Greedy deflation: subtract the leading lambda v^m and repeat, keeping what was taken out.

    The tensor analogue of keeping only the eigenvalues above a noise band. Deflation is a heuristic
    for tensors, since their eigenvectors are not orthogonal, so the terms need not be the tensor's
    own first eigenpairs after the first."""
    A = np.asarray(A, dtype=float)
    kept = np.zeros_like(A)
    remainder = A.copy()
    for _ in range(rank):
        lam, V, _ = z_eigenpairs(remainder, **kw)
        term = lam[0] * outer_power(V[0], A.ndim)
        kept += term
        remainder -= term
    return kept


def hosvd(A):
    """Factor matrices and core of a Tucker decomposition by per-mode SVD, with mode singular values."""
    A = np.asarray(A, dtype=float)
    factors, values = [], []
    for mode in range(A.ndim):
        unfolded = np.moveaxis(A, mode, 0).reshape(A.shape[mode], -1)
        U, s, _ = np.linalg.svd(unfolded, full_matrices=False)
        factors.append(U)
        values.append(s)
    core = A
    for mode, U in enumerate(factors):
        core = np.moveaxis(np.tensordot(U.T, np.moveaxis(core, mode, 0), axes=1), 0, mode)
    return factors, core, values


def horizon_stack(R, horizons=(1, 5, 21)):
    """Correlation of overlapping h-bar sums, asset x asset x horizon."""
    R = np.asarray(R, dtype=float)
    layers = []
    for h in horizons:
        summed = np.cumsum(R, axis=0)
        summed = summed[h - 1:] - np.vstack([np.zeros((1, R.shape[1])), summed[:-h]])
        layers.append(np.corrcoef(summed, rowvar=False))
    return np.stack(layers, axis=2)


def horizon_share(R, horizons=(1, 5, 21)):
    """Share of the stack's energy the horizon mode's leading component leaves out. 0 is one matrix."""
    s = hosvd(horizon_stack(R, horizons))[2][2]
    return float(1.0 - s[0] ** 2 / np.sum(s ** 2))


# ---------------------------------------------------------------------------
# 3. Eigenfidelity and eigenpersistence
# ---------------------------------------------------------------------------

def leading_direction(kind, X, horizons=(1, 5, 21), **kw):
    """One window's leading direction and the object it came from, in whitened or asset coordinates."""
    if kind == 'matrix':
        C = np.corrcoef(X, rowvar=False)
        return np.linalg.eigh((C + C.T) / 2)[1][:, -1], C
    if kind == 'horizon':
        stack = horizon_stack(X, horizons)
        return hosvd(stack)[0][0][:, 0], stack
    x = whiten(X)
    A = coskewness(x) if kind == 'coskew' else excess_cokurtosis(x)[0]
    _, V, _ = z_eigenpairs(A, **kw)
    return V[0], A


def fidelity(u, v):
    """Squared overlap of two unit directions; orthonormal k-column subspaces average over k."""
    u, v = np.atleast_2d(np.asarray(u, dtype=float).T).T, np.atleast_2d(np.asarray(v, dtype=float).T).T
    return float(np.linalg.norm(u.T @ v) ** 2 / u.shape[1])


def excess_fidelity(f, n, k=1):
    """0 at what chance gives two random k-subspaces of n dimensions, 1 at the same subspace."""
    chance = k / n
    return (f - chance) / (1.0 - chance)


def largest_moment(kind, later, **kw):
    """The most any direction could find in this window's object."""
    if kind == 'matrix':
        return float(np.linalg.eigvalsh(later)[-1])
    if kind == 'horizon':
        return float(hosvd(later)[2][0][0])
    return float(np.max(np.abs(z_eigenpairs(later, **kw)[0])))


def moment_capture(kind, v, later, top):
    """How much of ``top``, the later window's largest moment, the earlier direction still finds."""
    if kind == 'matrix':
        return float(v @ later @ v / top)
    if kind == 'horizon':
        return float(np.linalg.norm(v @ later.reshape(later.shape[0], -1)) / top)
    return float(abs(contract(later, v)) / top) if top > 0 else 0.0


def eigen_profile(kind, X, window=252, step=21, lags=(1, 2, 3), **kw):
    """Eigenfidelity and capture of ``kind`` between disjoint windows ``lag`` window-lengths apart.

    Windows start every ``step`` bars, so pairs at one lag share dates with other pairs but no pair
    shares a date within itself: the overlap between two estimates carries no common sample."""
    X = np.asarray(X, dtype=float)
    starts = list(range(0, len(X) - window + 1, step))
    fitted = {s: leading_direction(kind, X[s:s + window], **kw) for s in starts}
    tops = {s: largest_moment(kind, fitted[s][1], **kw) for s in starts}
    n = X.shape[1]
    rows = {}
    for lag in lags:
        pairs = [(s, s + lag * window) for s in starts if s + lag * window in fitted]
        if not pairs:
            continue
        f = [fidelity(fitted[a][0], fitted[b][0]) for a, b in pairs]
        c = [moment_capture(kind, fitted[a][0], fitted[b][1], tops[b]) for a, b in pairs]
        rows[lag] = dict(pairs=len(pairs), fidelity=float(np.mean(f)), capture=float(np.mean(c)),
                         excess=excess_fidelity(float(np.mean(f)), n))
    return rows


def eigenpersistence(profile):
    """Eigenfidelity above chance, averaged over the lags: 1 never moves, 0 is no better than chance."""
    return float(np.mean([row['excess'] for row in profile.values()])) if profile else float('nan')


# ---------------------------------------------------------------------------
# 4. Structure beyond correlation
# ---------------------------------------------------------------------------

def structure_stats(x, **kw):
    """Largest |Z-eigenvalue| of coskew and of anisotropic cokurt for whitened returns, and kappa."""
    aniso, kappa = excess_cokurtosis(x)
    return (float(np.max(np.abs(z_eigenpairs(coskewness(x), **kw)[0]))),
            float(np.max(np.abs(z_eigenpairs(aniso, **kw)[0]))), kappa)


def elliptical_sample(rng, T, n, kappa):
    """Whitened draws from the Student-t with this marginal excess kurtosis (Gaussian if kappa <= 0)."""
    g = rng.standard_normal((T, n))
    if kappa <= 0:
        return whiten(g)
    nu = 4.0 + 6.0 / kappa
    return whiten(g * np.sqrt((nu - 2.0) / rng.chisquare(nu, size=(T, 1))))


def mc_pvalue(observed, simulated):
    return float((1 + np.sum(np.asarray(simulated) >= observed)) / (len(simulated) + 1))


# ---------------------------------------------------------------------------
# 5. Value-at-risk out of sample
# ---------------------------------------------------------------------------

SKEW_CLIP, KURT_CLIP = 1.5, 7.0


def cornish_fisher(alpha, s=0.0, k=0.0):
    """Lower-tail standardised quantile, with skew and excess kurtosis clipped to keep it monotone."""
    z = float(stats.norm.ppf(alpha))
    s = float(np.clip(s, -SKEW_CLIP, SKEW_CLIP))
    k = float(np.clip(k, -KURT_CLIP, KURT_CLIP))
    return z + (z ** 2 - 1) * s / 6 + (z ** 3 - 3 * z) * k / 24 - (2 * z ** 3 - 5 * z) * s ** 2 / 36


def correlation_to_cov(C, stdev):
    return C * np.outer(stdev, stdev)


def fit_methods(Z, gamma=0.75, **kw):
    """Every estimator on one training window, as name -> (covariance, skew tensor, aniso tensor, kappa).

    The matrix family sets the shape from a correlation alone. The elliptical variants add a single
    number for how heavy the tails are, which is what the tensor must beat: a tensor that only beat
    Gaussian would be showing fat tails, not cross-asset structure."""
    Z = np.asarray(Z, dtype=float)
    stdev = Z.std(axis=0, ddof=1)
    C = np.atleast_2d(np.corrcoef(Z, rowvar=False))
    covs = {'sample': np.atleast_2d(np.cov(Z, rowvar=False)),
            'spectral': correlation_to_cov(be.spectral_correlation(C, gamma), stdev),
            'mp': correlation_to_cov(be.mp_filter(C, len(Z))[0], stdev)}
    x = whiten(Z)
    S = coskewness(x)
    aniso, kappa = excess_cokurtosis(x)
    out = {}
    for name, cov in covs.items():
        out[f'gauss_{name}'] = (cov, None, None, 0.0)
        out[f'elliptical_{name}'] = (cov, None, None, kappa)
    out['tensor_full'] = (covs['sample'], S, aniso, kappa)
    for rank in (1, 2):
        out[f'tensor_rank{rank}'] = (covs['sample'], low_rank(S, rank, **kw), low_rank(aniso, rank, **kw), kappa)
    return out, whitener(Z)[1]


def portfolio_var(method, root, exposure, alpha):
    """Loss quantile for one exposure vector (weights x each asset's forecast volatility)."""
    cov, S, aniso, kappa = method
    sd = math.sqrt(float(exposure @ cov @ exposure))
    s, k = 0.0, kappa
    if S is not None:
        u = root @ exposure
        u = u / np.linalg.norm(u)
        s = contract(S, u)
        k = kappa + contract(aniso, u)
    return -sd * cornish_fisher(alpha, s, k)


def var_backtest(R, weights, window=504, refit=21, alpha=0.01, lam=0.94, warm=60, gamma=0.75, **kw):
    """Walk-forward VaR for several long-only books under every method. Fit on bars before t only.

    Returns (names, var, losses): var has shape (days, portfolios, methods), losses (days, portfolios)."""
    R = np.asarray(R, dtype=float)
    weights = np.asarray(weights, dtype=float)
    Z, sigma = devolatilize(R, lam=lam, warm=warm)
    returns = R[warm:]
    names, table, losses = None, [], []
    fitted, root = None, None
    for t in range(window, len(Z)):
        if (t - window) % refit == 0:
            fitted, root = fit_methods(Z[t - window:t], gamma=gamma, **kw)
            names = names or list(fitted)
        table.append([[portfolio_var(fitted[n], root, w * sigma[t], alpha) for n in names] for w in weights])
        losses.append(-(weights @ returns[t]))
    return names, np.asarray(table), np.asarray(losses)


def newey_west_se(d, lags=5):
    d = np.asarray(d, dtype=float)
    e = d - d.mean()
    v = float(e @ e) / len(e)
    for lag in range(1, lags + 1):
        v += 2 * (1 - lag / (lags + 1)) * float(e[lag:] @ e[:-lag]) / len(e)
    return math.sqrt(max(v, 0.0) / len(e))


def diebold_mariano(loss_a, loss_b, lags=5):
    """Two-sided test of equal mean loss. Negative statistic: a has the lower loss."""
    d = np.asarray(loss_a, dtype=float) - np.asarray(loss_b, dtype=float)
    se = newey_west_se(d, lags)
    if se <= 0:
        return 0.0, 1.0
    stat = float(d.mean() / se)
    return stat, float(2 * stats.norm.sf(abs(stat)))


def holm(pvalues):
    """Holm step-down adjusted p-values, in the given order."""
    p = np.asarray(pvalues, dtype=float)
    adjusted = np.empty_like(p)
    running = 0.0
    for rank, i in enumerate(np.argsort(p)):
        running = max(running, (len(p) - rank) * p[i])
        adjusted[i] = min(running, 1.0)
    return adjusted
