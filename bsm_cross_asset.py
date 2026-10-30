#!/usr/bin/env python3
"""bsm_cross_asset.py -- N-factor cross-asset extension of bsm_event.py.

Reconstruction of the module described in "Spectral Value-at-Risk for Binary Event Contracts"
(draft paper, section 8.2). Six daily factors -- S&P 500, VIX, 2- and 10-year Treasury yields,
EUR/USD and USD/JPY -- each get a walk-forward GJR-GARCH fit (63-day refresh). Standardised
residuals are correlated over a trailing 504-day window under three estimators:
  sample      the sample correlation
  spectral    general-N powering: lambda~ = N C(max(N C(lambda^gamma), lambda_min)), unit diagonal
  mp          Marchenko-Pastur: eigenvalues inside the noise band replaced by their average
From day 756, each non-overlapping five-day standardised shock vector is scored by Gaussian NLL,
by the realised variance of the minimum-variance portfolio, and by the 99% Mahalanobis exceedance
rate (target 1%), with a bootstrap interval on the NLL difference from the sample estimator.

A cross-asset book (long one-month ATM S&P digital, EUR/USD, USD/JPY and a 10-year par Treasury
priced by a semi-annual bond formula) is backtested at a five-day horizon under Gaussian,
per-factor GPD-tail (Gaussian copula) and joint filtered-historical shocks -- the joint resampling
draws one row index for all factors, keeping empirical (tail) dependence -- plus a walk-forward
choice among them by past pinball loss. Legs can be risk-balanced to equal standalone 99%
Gaussian VaR; expected shortfall is attributed by leg. The book's six-factor curvature spectrum
comes from JetN.

Install: python -m pip install numpy scipy
Run:     python bsm_cross_asset.py --selftest
         python bsm_cross_asset.py --demo                      (synthetic factors)
         python bsm_cross_asset.py --data data [--balance]     (FRED-style CSVs, see FILES)
"""
from __future__ import annotations

import argparse
import math
import sys
from dataclasses import dataclass, field

import numpy as np
from scipy import optimize, stats
from scipy.special import gammaln, ndtr

import bsm_event as be

FACTORS = ('SPX', 'VIX', 'UST2Y', 'UST10Y', 'EURUSD', 'USDJPY')
FILES = {'SPX': ('SP500.csv', 'SPX.csv', 'GSPC.csv'), 'VIX': ('VIX.csv', 'VIXCLS.csv'),
         'UST2Y': ('DGS2.csv', 'UST2Y.csv'), 'UST10Y': ('DGS10.csv', 'UST10Y.csv'),
         'EURUSD': ('DEXUSEU.csv', 'EURUSD.csv'), 'USDJPY': ('DEXJPUS.csv', 'USDJPY.csv')}
LOG = np.array([True, False, False, False, True, True])      # log returns vs absolute changes
SCALE = np.array([1.0, 0.01, 0.01, 0.01, 1.0, 1.0])          # VIX and yields quoted in percent


def to_state(levels):
    return np.asarray(levels, float) * SCALE


def factor_returns(states):
    s = np.asarray(states, float)
    out = s[1:] - s[:-1]
    out[:, LOG] = np.log(s[1:, LOG] / s[:-1, LOG])
    return out


def apply_returns(state0, r):
    """New states after factor returns r (rows); works for arrays and lists of jets."""
    return [state0[j] * be._exp(r[j]) if LOG[j] else state0[j] + r[j] for j in range(len(FACTORS))]


def load_factors(data_dir):
    series = [be.load_series(be.find_file(data_dir, FILES[f])) for f in FACTORS]
    dates, vals = be.align(*series)
    return dates, np.column_stack(vals)


def synthetic_factors(n=2500, seed=11):
    """Correlated GJR factors with skewed-t innovations (Gaussian copula); VIX mean-reverts."""
    rng = np.random.default_rng(seed)
    C = np.array([[1.00, -0.75, 0.25, 0.20, 0.10, 0.20],
                  [-0.75, 1.00, -0.20, -0.15, -0.05, -0.20],
                  [0.25, -0.20, 1.00, 0.80, 0.05, 0.30],
                  [0.20, -0.15, 0.80, 1.00, 0.05, 0.40],
                  [0.10, -0.05, 0.05, 0.05, 1.00, -0.30],
                  [0.20, -0.20, 0.30, 0.40, -0.30, 1.00]])
    sd = np.array([0.010, 0.012, 0.0006, 0.0007, 0.005, 0.006])
    law = be.HansenSkewT(eta=6.0, lam=-0.1)
    u = ndtr(rng.standard_normal((n, 6)) @ np.linalg.cholesky(C).T)
    z = law.ppf(np.clip(u, 1e-12, 1 - 1e-12))
    alpha, gamma, beta = 0.03, 0.08, 0.90
    s2 = sd ** 2
    omega = s2 * (1 - alpha - gamma / 2 - beta)
    state = np.array([4000.0, 0.18, 0.040, 0.042, 1.10, 140.0])
    out = [state.copy()]
    for t in range(n):
        e = np.sqrt(s2) * z[t]
        s2 = omega + (alpha + gamma * (e < 0)) * e * e + beta * s2
        r = e.copy()
        r[1] += 0.02 * (0.18 - state[1])
        state = np.array(apply_returns(state, r))
        state[1] = max(state[1], 0.08)
        out.append(state.copy())
    dates = np.busday_offset(np.datetime64('2015-01-02'), np.arange(n + 1), roll='forward')
    return dates, np.array(out) / SCALE


# ---------------------------------------------------------------------------
# Correlation estimators and the out-of-sample test
# ---------------------------------------------------------------------------

def estimators(Zh, gamma, lam_min=0.05):
    C = np.corrcoef(Zh.T)
    return {'sample': C, 'spectral': be.spectral_correlation(C, gamma, lam_min),
            'mp': be.mp_filter(C, len(Zh))[0]}


def mvt_logpdf_q(q, N, nu, logdet=0.0):
    """Log density of a multivariate Student-t with covariance C (scale C (nu-2)/nu) at Mahalanobis q."""
    return (gammaln((nu + N) / 2) - gammaln(nu / 2) - N / 2 * np.log((nu - 2) * np.pi) - logdet / 2
            - (nu + N) / 2 * np.log1p(q / (nu - 2)))


def fit_mvt_nu(qs, N):
    """Degrees of freedom by maximum likelihood from Mahalanobis distances."""
    qs = np.asarray(qs, float)
    res = optimize.minimize_scalar(lambda nu: -np.sum(mvt_logpdf_q(qs, N, nu)), bounds=(2.05, 200.0),
                                   method='bounded')
    return float(res.x)


def score_shock(C, s, conf=0.99, nu=None):
    """Scores of one realised shock vector under correlation C (unit-variance factors).

    nll is Gaussian; t_nll uses a multivariate Student-t with the same covariance and nu degrees
    of freedom (heavy-tailed shocks make the Gaussian NLL a ranking device, not a calibrated
    probability). exceed / exceed_t test the 99% ellipsoid under each law."""
    N = len(s)
    sign, logdet = np.linalg.slogdet(C)
    q = float(s @ np.linalg.solve(C, s))
    ones = np.ones(N)
    w = np.linalg.solve(C, ones)
    w = w / w.sum()
    out = {'nll': 0.5 * (logdet + q + N * be.LOG2PI), 'mv_var': float((w @ s) ** 2), 'q': q,
           'mv_pred': float(w @ C @ w), 'exceed': q > stats.chi2.ppf(conf, N)}
    if nu is not None:
        out['t_nll'] = float(-mvt_logpdf_q(q, N, nu, logdet))
        out['exceed_t'] = q * nu / ((nu - 2) * N) > stats.f.ppf(conf, N, nu)
    return out


@dataclass
class FactorModel:
    R: np.ndarray       # daily factor returns
    wfs: list           # walk-forward GJR fits per factor
    Z: np.ndarray       # standardised residuals using forecasts made before each day

    def horizon_shock(self, t, h):
        """Realised h-day factor return standardised by the closed-form horizon variance at t."""
        out = np.empty(len(self.wfs))
        hv = np.empty(len(self.wfs))
        for j, wf in enumerate(self.wfs):
            p = wf.params[wf.block_of[t]]
            hv[j] = be.horizon_variance(wf.sigma2[t], p, h)
            out[j] = (self.R[t:t + h, j].sum() - h * p.mu) / math.sqrt(hv[j])
        return out, hv


def fit_factors(states, block=63, min_train=252):
    R = factor_returns(states)
    wfs = [be.walk_forward_gjr(R[:, j], block, min_train) for j in range(R.shape[1])]
    Z = np.full(R.shape, np.nan)
    for j, wf in enumerate(wfs):
        for t in range(min_train, len(R)):
            p = wf.params[wf.block_of[t]]
            Z[t, j] = (R[t, j] - p.mu) / math.sqrt(wf.sigma2[t])
    return FactorModel(R, wfs, Z)


def correlation_test(fm, window=504, start=756, h=5, gammas=(0.5, 0.75, 1.0, 1.25), lam_min=0.05,
                     tune_frac=0.4, log=print):
    ts = list(range(start, len(fm.R) - h + 1, h))
    shocks = [fm.horizon_shock(t, h)[0] for t in ts]
    n_tune = int(len(ts) * tune_frac)

    def nll_spec(g, idx):
        return np.mean([score_shock(be.spectral_correlation(np.corrcoef(fm.Z[ts[i] - window:ts[i]].T), g, lam_min),
                                    shocks[i])['nll'] for i in idx])

    def risk_spec(g, idx):
        return np.mean([score_shock(be.spectral_correlation(np.corrcoef(fm.Z[ts[i] - window:ts[i]].T), g, lam_min),
                                    shocks[i])['mv_var'] for i in idx])

    tune = range(n_tune)
    g = gammas[int(np.argmin([nll_spec(g, tune) for g in gammas]))] if n_tune else 1.0
    g_risk = gammas[int(np.argmin([risk_spec(g, tune) for g in gammas]))] if n_tune else 1.0
    N = fm.R.shape[1]
    nu = fit_mvt_nu([score_shock(np.corrcoef(fm.Z[ts[i] - window:ts[i]].T), shocks[i])['q'] for i in tune], N) \
        if n_tune else 8.0
    rows = {k: [] for k in ('sample', 'spectral', 'spectral-risk', 'mp')}
    for i in range(n_tune, len(ts)):
        Zh = fm.Z[ts[i] - window:ts[i]]
        ests = estimators(Zh, g, lam_min)
        ests['spectral-risk'] = be.spectral_correlation(ests['sample'], g_risk, lam_min)
        for name, C in ests.items():
            rows[name].append(score_shock(C, shocks[i], nu=nu))
    log(f'Correlation test: {len(ts) - n_tune} evaluation windows of {h} days from day {ts[n_tune]} '
        f'(tuning windows {n_tune}); trailing window {window}, N={N}; spectral gamma tuned on '
        f'NLL {g}, on min-variance risk {g_risk}; Student-t dof {nu:.1f} fitted on tuning windows')
    base_nll = np.array([r['nll'] for r in rows['sample']])
    base_mv = np.array([r['mv_var'] for r in rows['sample']])

    def pw(d):
        if np.max(np.abs(d)) < 1e-10:  # the filter left the matrix unchanged in every window
            return {'mean': 0.0, 'ci': (0.0, 0.0)}, ' identical to sample'
        p = be.paired_power(d, block=4)
        n80 = '-' if not math.isfinite(p['n_needed']) else f'{min(p["n_needed"], 99999):.0f}'
        return p, f'{p["power"]:5.2f}  {n80:>6s}' if math.isfinite(p['power']) else '    -       -'

    log('Fit (Gaussian NLL of the realised shock vector; lower is better)')
    log('  estimator      mean NLL   dNLL vs sample [95% CI]       power  windows-for-80%   99% exceed')
    for name, rs in rows.items():
        nll = np.array([r['nll'] for r in rs])
        if name == 'sample':
            log(f'  {name:13s} {nll.mean():9.4f}   {"(reference)":30s}                       '
                f'{100 * np.mean([r["exceed"] for r in rs]):.2f}%')
            continue
        p, txt = pw(nll - base_nll)
        log(f'  {name:13s} {nll.mean():9.4f}   {p["mean"]:+.4f} [{p["ci"][0]:+.4f}, {p["ci"][1]:+.4f}]   {txt}'
            f'            {100 * np.mean([r["exceed"] for r in rs]):.2f}%')
    base_t = np.array([r['t_nll'] for r in rows['sample']])
    gauss_vs_t = np.mean([r['nll'] - r['t_nll'] for r in rows['sample']])
    log(f'Fit (multivariate Student-t NLL, dof {nu:.1f}; lower is better; beats the Gaussian law by '
        f'{gauss_vs_t:+.4f} per window under the sample estimator)')
    log('  estimator      mean NLL   dNLL vs sample [95% CI]       power  windows-for-80%   99% exceed (t law)')
    for name, rs in rows.items():
        tn = np.array([r['t_nll'] for r in rs])
        ext = 100 * np.mean([r['exceed_t'] for r in rs])
        if name == 'sample':
            log(f'  {name:13s} {tn.mean():9.4f}   {"(reference)":30s}                       {ext:.2f}%')
            continue
        p, txt = pw(tn - base_t)
        log(f'  {name:13s} {tn.mean():9.4f}   {p["mean"]:+.4f} [{p["ci"][0]:+.4f}, {p["ci"][1]:+.4f}]   {txt}'
            f'            {ext:.2f}%')
    log('Risk (realised variance of the out-of-sample minimum-variance portfolio built from each estimate)')
    log('  estimator      realised var  risk change vs sample [95% CI]   power  windows-for-80%   realised/predicted')
    for name, rs in rows.items():
        mv = np.array([r['mv_var'] for r in rs])
        ratio = mv.mean() / np.mean([r['mv_pred'] for r in rs])
        if name == 'sample':
            log(f'  {name:13s} {mv.mean():12.4f}   {"(reference)":32s}                       {ratio:.3f}')
            continue
        p, txt = pw(mv - base_mv)
        rel = 100 * p['mean'] / base_mv.mean()
        lo, hi = (100 * x / base_mv.mean() for x in p['ci'])
        log(f'  {name:13s} {mv.mean():12.4f}   {rel:+6.2f}% [{lo:+6.2f}%, {hi:+6.2f}%]          {txt}'
            f'            {ratio:.3f}')
    log('Tail (share of shock vectors outside the 99% Mahalanobis ellipsoid; nominal 1%)')
    log('  estimator      exceed   change vs sample [95% CI]        power  windows-for-80%')
    base_ex = np.array([r['exceed'] for r in rows['sample']], float)
    for name, rs in rows.items():
        ex = np.array([r['exceed'] for r in rs], float)
        if name == 'sample':
            log(f'  {name:13s} {100 * ex.mean():6.2f}%   (reference)')
            continue
        p, txt = pw(ex - base_ex)
        log(f'  {name:13s} {100 * ex.mean():6.2f}%   {100 * p["mean"]:+.2f}pp [{100 * p["ci"][0]:+.2f}, '
            f'{100 * p["ci"][1]:+.2f}]          {txt}')
    log('  power = chance a 5% two-sided test detects an effect of the observed size with this many windows;'
        ' low power means "not yet distinguishable", not "no effect"')
    return rows, g


# ---------------------------------------------------------------------------
# Cross-asset book
# ---------------------------------------------------------------------------

def bond_price(y, coupon, years=10, freq=2):
    """Semi-annual fixed-coupon bond per unit face; accepts arrays or jets for y."""
    d = 1.0 / (1.0 + y / freq) if not isinstance(y, be.JetN) else (1.0 + y / freq).reciprocal()
    total, dk = 0.0, d
    for _ in range(years * freq):
        total = total + coupon / freq * dk
        last = dk
        dk = dk * d
    return total + last


@dataclass
class CrossAssetBook:
    S0: float
    y0: float
    units: np.ndarray = field(default_factory=lambda: np.array([10000.0, 100000.0, 1.4e7, 100000.0]))
    tenor_days: int = 21
    legs = ('S&P digital', 'EUR/USD', 'USD/JPY', 'UST 10y')

    @property
    def digital(self):
        return be.BSMDigital(strike=self.S0, expiry=self.tenor_days / be.TRADING_DAYS)

    def leg_values(self, st, elapsed_days=0.0):
        S, v, _, y10, eu, uj = st
        if not isinstance(v, be.JetN):
            v = np.maximum(v, 0.01)  # simulated VIX can cross zero in the far tail
        tau = (self.tenor_days - elapsed_days) / be.TRADING_DAYS
        jpy = (1.0 / uj) if not isinstance(uj, be.JetN) else uj.reciprocal()
        return [self.units[0] * self.digital.price(S, v, tau), self.units[1] * eu,
                self.units[2] * jpy, self.units[3] * bond_price(y10, self.y0)]

    def value(self, st, elapsed_days=0.0):
        return sum(self.leg_values(st, elapsed_days))


def book_jet(book, state0, L, h):
    """Book P&L over h days as a JetN in six whitened factor shocks (factor returns = L z)."""
    z = be.JetN.variables([0.0] * len(FACTORS))
    r = [sum(L[j, k] * z[k] for k in range(len(FACTORS))) for j in range(len(FACTORS))]
    return book.value(apply_returns(state0, r), h) - book.value(state0, 0.0)


def risk_balance(book, state0, Sigma_h, h, target=1000.0):
    """Scale leg units so each leg's standalone 99% Gaussian VaR (delta) equals target."""
    units = book.units.copy()
    L = be.cov_factor(Sigma_h)
    z99 = stats.norm.ppf(0.99)
    for i in range(len(units)):
        one = CrossAssetBook(book.S0, book.y0, np.eye(len(units))[i] * units[i], book.tenor_days)
        g = book_jet(one, state0, L, h).gradient()
        sd = float(np.linalg.norm(g))
        units[i] *= target / (z99 * sd) if sd > 0 else 1.0
    return CrossAssetBook(book.S0, book.y0, units, book.tenor_days)


def book_backtest(states, fm, window=504, start=756, h=5, n_sims=5000, balance=False, tail_k=4.0,
                  seed=5, levels=(0.95, 0.99), calibrate=True, calib_min=20, calib_window=104, corr_fn=None,
                  log=print):
    """Five-day VaR backtest of the cross-asset book.

    calibrate: scale each factor's closed-form horizon variance by its realised-to-forecast ratio,
    the mean squared standardised horizon shock over completed earlier windows (trailing
    calib_window, at least calib_min). This corrects both GJR variance that runs low out of
    sample (FX, rates) and the overstatement of horizon variance for mean-reverting factors
    (S&P, VIX), using only shocks already realised at each forecast date.
    """
    ts = list(range(start, len(fm.R) - h + 1, h))
    # student-t: multivariate t with covariance C; gpd-t: per-factor GPD tails joined by a t copula.
    # Both get tail dependence a Gaussian copula lacks; nu is fitted on past shock vectors only.
    models = ('gaussian', 'gpd', 'fhs', 'student-t', 'gpd-t')
    var = {m: np.zeros((len(ts), len(levels))) for m in models}
    chosen = np.zeros((len(ts), len(levels)))
    realized = np.zeros(len(ts))
    es_share = []
    pick = []
    past_shocks = []
    scales = []
    nus = []
    for w, t in enumerate(ts):
        st0 = states[t]  # state at the start of return t
        Zh = fm.Z[t - window:t]
        C = np.corrcoef(Zh.T) if corr_fn is None else corr_fn(Zh)
        shock, hv = fm.horizon_shock(t, h)
        if calibrate and len(past_shocks) >= calib_min:
            k = np.mean(np.square(past_shocks[-calib_window:]), axis=0)
            hv = hv * k
            scales.append(k)
        if len(past_shocks) >= calib_min:
            Cinv = np.linalg.inv(C)
            qs = [float(s @ Cinv @ s) for s in past_shocks[-calib_window:]]
            nu = fit_mvt_nu(qs, len(FACTORS))
        else:
            nu = 8.0
        nus.append(nu)
        past_shocks.append(shock)  # realised by t + h, i.e. before the next window's forecast
        mu = np.array([wf.params[wf.block_of[t]].mu for wf in fm.wfs])
        book = CrossAssetBook(S0=st0[0], y0=st0[3])
        if balance:
            D = np.sqrt(hv)
            book = risk_balance(book, st0, D[:, None] * C * D[None, :], h)
        rng = np.random.default_rng([seed, t])
        g = rng.standard_normal((n_sims, len(FACTORS))) @ np.linalg.cholesky(C).T
        draws = {'gaussian': g}
        laws = [be.SemiParamGPD(Zh[:, j], 0.10, tail_k) for j in range(len(FACTORS))]
        u = np.clip(ndtr(g), 1e-12, 1 - 1e-12)
        draws['gpd'] = np.column_stack([laws[j].ppf(u[:, j]) for j in range(len(FACTORS))])
        idx = rng.integers(0, len(Zh), size=(n_sims, h))
        draws['fhs'] = Zh[idx].sum(axis=1) / math.sqrt(h)
        mix = np.sqrt(rng.chisquare(nu, size=(n_sims, 1)) / nu)
        tdraw = g / mix                                    # multivariate t, scale C
        draws['student-t'] = tdraw * math.sqrt((nu - 2) / nu)  # covariance C
        ut = np.clip(stats.t.cdf(tdraw, nu), 1e-12, 1 - 1e-12)
        draws['gpd-t'] = np.column_stack([laws[j].ppf(ut[:, j]) for j in range(len(FACTORS))])
        v0 = book.value(st0, 0.0)
        r_real = fm.R[t:t + h].sum(0)
        realized[w] = v0 - book.value(apply_returns(st0, r_real), h)
        for m in models:
            r = h * mu + draws[m] * np.sqrt(hv)
            sim_st = apply_returns(st0, r.T)
            legs = np.array(book.leg_values(sim_st, h)) - np.array(book.leg_values(st0, 0.0))[:, None]
            loss = -legs.sum(0)
            var[m][w] = np.quantile(loss, levels)
            if m == 'gpd':
                tail = loss >= var[m][w, -1]
                es_share.append(-legs[:, tail].mean(1) / loss[tail].mean())
        if w >= 10:
            past = {m: np.mean([be.pinball(realized[:w], var[m][:w, li], lv).mean()
                                for li, lv in enumerate(levels)]) for m in models}
            best = min(past, key=past.get)
        else:
            best = 'gaussian'
        pick.append(best)
        chosen[w] = var[best][w]
    log(f'\nCross-asset book, {h}-day VaR, {len(ts)} non-overlapping windows from day {start}'
        f'{" (risk-balanced legs)" if balance else ""}'
        + (f'; horizon variance calibrated walk-forward (mean scale '
           + ', '.join(f'{f} {s:.2f}' for f, s in zip(FACTORS, np.mean(scales, axis=0))) + ')' if scales else '')
        + f'; t dof median {np.median(nus):.1f}')
    def pid_scaled(V, lv):
        """The paper's PID loop on the log VaR scale (default gains, not tuned here): each window's
        VaR is scaled by a factor set from earlier breaches only, so clustered stress is caught."""
        pid = be.PIDVaRScale(alpha=lv)
        out = np.empty_like(V)
        for i in range(len(V)):
            out[i] = V[i] * pid.factor
            pid.update(realized[i] > out[i])
        return out

    log('model        level  breach%  nominal  Kupiec p  Chr. cc p   mean pinball   | with PID: breach%  Kupiec p  cc p  pinball')
    pid_var = {}
    for name, V in list(var.items()) + [('walk-forward', chosen)]:
        pid_var[name] = np.column_stack([pid_scaled(V[:, li], lv) for li, lv in enumerate(levels)])
        for li, lv in enumerate(levels):
            hits = realized > V[:, li]
            hp = realized > pid_var[name][:, li]
            log(f'{name:12s} {lv:5.2f}  {100 * hits.mean():6.2f}   {100 * (1 - lv):6.2f}   '
                f'{be.kupiec(hits, 1 - lv)[1]:7.3f}   {be.christoffersen(hits, 1 - lv)["p_cc"]:7.3f}   '
                f'{be.pinball(realized, V[:, li], lv).mean():9.4g}      |   {100 * hp.mean():6.2f}   '
                f'{be.kupiec(hp, 1 - lv)[1]:7.3f}  {be.christoffersen(hp, 1 - lv)["p_cc"]:5.3f}  '
                f'{be.pinball(realized, pid_var[name][:, li], lv).mean():.4g}')
    picks = {m: pick.count(m) for m in models}
    log('walk-forward picks: ' + ', '.join(f'{m} {c}' for m, c in picks.items()))
    share = np.mean(es_share, axis=0)
    log('99% expected-shortfall attribution (gpd): ' + ', '.join(
        f'{leg} {100 * s:.1f}%' for leg, s in zip(CrossAssetBook.legs, share)))
    return {'var': var, 'chosen': chosen, 'realized': realized, 'es_share': share, 'pid_var': pid_var}


def book_spectrum(states, fm, t, h=5, window=504, log=print):
    """Six-factor curvature spectrum of the book at day t (whitened by the horizon covariance)."""
    st0 = states[t]
    C = np.corrcoef(fm.Z[t - window:t].T)
    _, hv = fm.horizon_shock(t, h)
    D = np.sqrt(hv)
    L = be.cov_factor(D[:, None] * C * D[None, :])
    book = CrossAssetBook(S0=st0[0], y0=st0[3])
    J = book_jet(book, st0, L, h)
    lam, Q = np.linalg.eigh(J.hessian())
    e = be.energy_by_order(J)
    log(f'\nBook curvature spectrum at day {t} (JetN, 6 factors, degree {J.deg}):')
    log('  principal curvatures ' + np.array2string(lam, precision=2))
    k = int(np.argmax(np.abs(lam)))
    log('  dominant direction loadings ' + ', '.join(f'{f} {x:+.2f}' for f, x in zip(FACTORS, Q[:, k])))
    log('  energy by order (1..4) ' + '  '.join(f'{x:.3e}' for x in e))
    return J


# ---------------------------------------------------------------------------
# Self-test, demo, CLI
# ---------------------------------------------------------------------------

def selftest():
    failures = []

    def check(name, ok, detail):
        print(f'[{"ok" if ok else "FAIL"}] {name}: {detail}')
        if not ok:
            failures.append(name)

    print('bsm_cross_asset self-test')
    rng = np.random.default_rng(0)
    A = rng.standard_normal((800, 6)) @ rng.standard_normal((6, 6))
    C = np.corrcoef(A.T)
    S1 = be.spectral_correlation(C, 1.0, 0.0)
    S2 = be.spectral_correlation(C, 0.6, 0.05)
    lam2 = np.linalg.eigvalsh(S2)
    check('spectral estimator', np.abs(S1 - C).max() < 1e-12 and np.allclose(np.diag(S2), 1) and lam2.min() > 0,
          f'gamma=1 identity {np.abs(S1 - C).max():.1e}; gamma=0.6 unit diagonal, min eigenvalue {lam2.min():.3f}')

    W = rng.standard_normal((504, 6))
    M, lp, k = be.mp_filter(np.corrcoef(W.T), 504)
    off = np.abs(M - np.eye(6)).max()
    check('Marchenko-Pastur on pure noise', k == 0 and off < 1e-12,
          f'{k} signal eigenvalues above lambda+ {lp:.3f}; filtered matrix is identity to {off:.1e}')

    check('par bond', abs(bond_price(0.042, 0.042) - 1) < 1e-12, f'price at the coupon yield {bond_price(0.042, 0.042):.15f}')

    st0 = np.array([4000.0, 0.18, 0.040, 0.042, 1.10, 140.0])
    book = CrossAssetBook(S0=4000.0, y0=0.042)
    D = np.array([0.02, 0.03, 0.001, 0.0015, 0.01, 0.012])
    L = np.diag(D)
    J = book_jet(book, st0, L, 5)
    f = lambda z: book.value(apply_returns(st0, L @ z), 5) - book.value(st0, 0.0)
    h = 1e-4
    fd = np.array([(f(h * e) - f(-h * e)) / (2 * h) for e in np.eye(6)])
    rel = np.abs(fd - J.gradient()).max() / np.abs(J.gradient()).max()
    fd2 = (f(h * np.eye(6)[0]) - 2 * f(np.zeros(6)) + f(-h * np.eye(6)[0])) / h ** 2
    rel2 = abs(fd2 - J.hessian()[0, 0]) / abs(J.hessian()[0, 0])
    check('JetN book derivatives', rel < 1e-6 and rel2 < 1e-4, f'gradient vs FD {rel:.1e}, d2/dz_SPX^2 vs FD {rel2:.1e}')

    w = np.linalg.solve(C, np.ones(6))
    check('min-variance weights', abs((w / w.sum()).sum() - 1) < 1e-12, 'weights sum to one')

    print(f'\n{"All checks passed." if not failures else "FAILED: " + ", ".join(failures)}')
    return not failures


def run(dates, levels, balance=False, log=print):
    states = to_state(levels)
    log(f'{len(states)} aligned observations {dates[0]} .. {dates[-1]}; fitting six walk-forward GJR models ...')
    fm = fit_factors(states)
    correlation_test(fm, log=log)
    book_backtest(states, fm, balance=balance, log=log)
    book_spectrum(states, fm, len(fm.R) - 10, log=log)


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split('\n\n')[0])
    ap.add_argument('--selftest', action='store_true')
    ap.add_argument('--demo', action='store_true', help='run on synthetic factors')
    ap.add_argument('--data', default=None)
    ap.add_argument('--balance', action='store_true', help='risk-balance legs to equal standalone VaR')
    args = ap.parse_args(argv)
    if not (args.selftest or args.demo or args.data):
        ap.print_help()
        return 0
    if args.selftest and not selftest():
        return 1
    if args.demo:
        print('Synthetic six-factor market (exercises the code; not market evidence)')
        run(*synthetic_factors(), balance=args.balance)
    if args.data:
        run(*load_factors(args.data), balance=args.balance)
    return 0


if __name__ == '__main__':
    sys.exit(main())
