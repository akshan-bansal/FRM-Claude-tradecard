#!/usr/bin/env python3
"""bsm_spectral.py -- two-factor spectral decomposition of vanilla option risk.

Reconstruction of the predecessor module described in "Spectral Value-at-Risk for Binary Event
Contracts" (draft paper, section 2): the spectral analysis that bsm_event.py says "carries over"
to binary contracts. Calls, puts and straddles are expanded to fourth order in (dS, dvol) with
exact jets, whitened by the shock covariance, and summarised by principal curvatures, risk
directions and Gaussian energy by order. It also compares shock-covariance filters, compares
hierarchical vs complete-sum hedges of a straddle with the underlying and straddles at 2T and
4T, and tracks principal directions over time (a diffusion-imaging style "fibre" diagnostic).

The original imported bsm_parallel_surface_4th, bsm_signal and bsm_direction, which were not
supplied. Here the fourth-order surface comes from bsm_event jets, the hand-derived third-order
Greeks in bsm_parallel_surface.py serve as an independent cross-check, and the signal and
direction pieces are re-derived below (curvature_signal, direction_track, backtest_dipy).

Assumptions, as in the original: no forecast of direction, flat vol (no smile), no costs, fixed
rate. Curvature energy is a proposed diagnostic, not a validated trading signal.

Install: python -m pip install numpy scipy
Run:     python bsm_spectral.py --selftest
         python bsm_spectral.py --demo
         python bsm_spectral.py --backtest --synthetic
         python bsm_spectral.py --backtest --data data          (SP500.csv + VIXCLS.csv)
         python bsm_spectral.py --backtest --data data --underlying NDX   (any registered underlying)
"""
from __future__ import annotations

import argparse
import math
import sys
from dataclasses import dataclass

import numpy as np

import bsm_event as be

TD = be.TRADING_DAYS


@dataclass(frozen=True)
class Vanilla:
    """Flat-vol BSM call, put or straddle; state (S, v) with v the decimal volatility."""
    strike: float
    expiry: float  # years
    rate: float = 0.04
    dividend: float = 0.01
    kind: str = 'straddle'
    names = ('S', 'v')

    def price(self, S, v, tau=None):
        T = self.expiry if tau is None else tau
        F = S * math.exp((self.rate - self.dividend) * T)
        sq = math.sqrt(T)
        d1 = (be._log(F / self.strike) + 0.5 * v * v * T) / (v * sq)
        d2 = d1 - v * sq
        disc = math.exp(-self.rate * T)
        call = disc * (F * be._ncdf(d1) - self.strike * be._ncdf(d2))
        if self.kind == 'call':
            return call
        put = call - disc * (F - self.strike)
        return put if self.kind == 'put' else call + put


# ---------------------------------------------------------------------------
# Spectral decomposition, signal and direction diagnostics
# ---------------------------------------------------------------------------

GREEK_MONOS = {'delta': (1, 0), 'vega': (0, 1), 'gamma': (2, 0), 'vanna': (1, 1), 'vomma': (0, 2),
               'speed': (3, 0), 'zomma': (2, 1), 'spot_vol_vol': (1, 2), 'ultima': (0, 3)}


def jet_greeks(model, S, v):
    """Raw state derivatives to third order from the jet (volatility per decimal unit)."""
    J = be.pnl_jet(model, np.array([S, v]), np.eye(2), 0.0)
    return {name: J.derivative(m) for name, m in GREEK_MONOS.items()}


def shock_cov(sd_spot, sd_vol, corr):
    return np.array([[sd_spot ** 2, corr * sd_spot * sd_vol], [corr * sd_spot * sd_vol, sd_vol ** 2]])


def curvature_signal(model, state0, Sigma, horizon=0.0, budget=1.0, order=4):
    """Gaussian energy of the nonlinear terms (orders 2..order) per budget^2.

    Generalises bsm_parallel_surface.curvature_energy (orders 2-3, Monte Carlo) with exact
    moments, including the 2x4 cross term. A diagnostic of convexity exposure, not downside risk.
    """
    J = be.pnl_jet(model, np.asarray(state0, float), be.cov_factor(Sigma), horizon)
    nonlinear = J.truncate(order).c.copy()
    nonlinear[be._tables(2, be.DEG)[7] < 2] = 0.0
    G = be.monomial_gram(2, be.DEG)
    return float(nonlinear @ G @ nonlinear) / budget ** 2


def fractional_anisotropy(lam):
    """2-D analogue of diffusion-tensor FA on curvature magnitudes: ||l1| - |l2|| / sqrt(l1^2 + l2^2).

    In [0, 1]. Curvature can be indefinite (a saddle, eigenvalues of opposite sign), unlike a
    diffusion tensor, so magnitudes are used and the sign pattern is reported separately."""
    l1, l2 = np.abs(lam)
    den = math.hypot(l1, l2)
    return abs(l1 - l2) / den if den > 0 else 0.0


def principal_direction(rv):
    """Dominant-|curvature| eigenvector mapped to standardised state units, angle in [0, 180)."""
    k = int(np.argmax(np.abs(rv.curvatures)))
    d = rv.L @ rv.directions[:, k]
    d = d / np.sqrt(np.diag(rv.L @ rv.L.T))
    return math.degrees(math.atan2(d[1], d[0])) % 180.0


def direction_track(angles):
    """Angular change between consecutive principal directions (axes, so modulo 180 degrees)."""
    a = np.asarray(angles, float)
    d = np.abs(np.diff(a)) % 180.0
    return np.minimum(d, 180.0 - d)


# ---------------------------------------------------------------------------
# Shock-covariance filters for the two-factor state
# ---------------------------------------------------------------------------

def cov_sample(X, window=252):
    return np.cov(X[-window:].T)


def cov_ewma(X, lam=0.94):
    w = lam ** np.arange(len(X) - 1, -1, -1)
    w = w / w.sum()
    Xc = X - (w[:, None] * X).sum(0)
    return (w[:, None, None] * Xc[:, :, None] * Xc[:, None, :]).sum(0)


def cov_shrink(X, window=252, weight=0.3):
    C = cov_sample(X, window)
    return (1 - weight) * C + weight * np.diag(np.diag(C))


def cov_tempered(X, window=252, gamma=0.75, floor=0.2):
    C = cov_sample(X, window)
    sd = np.sqrt(np.diag(C))
    rho = be.temper_rho(C[0, 1] / (sd[0] * sd[1]), gamma, floor)
    return shock_cov(sd[0], sd[1], rho)


FILTERS = {'sample': cov_sample, 'ewma': cov_ewma, 'shrink': cov_shrink, 'tempered': cov_tempered}


def compare_filters(X, horizon=5, start=504, window=252):
    """Walk-forward scores of non-overlapping horizon shocks under each daily-covariance filter.

    nll   Gaussian negative log-likelihood of the realised shock (fit)
    risk  realised variance of the first factor hedged with the second at the filter's
          minimum-variance ratio h = C01 / C11 (risk reduction actually delivered)
    """
    out = {k: {'nll': [], 'risk': []} for k in FILTERS}
    for t in range(start, len(X) - horizon, horizon):
        hist = X[t - window * 2:t]
        realised = X[t:t + horizon].sum(0)
        for name, f in FILTERS.items():
            C = horizon * f(hist)
            sign, logdet = np.linalg.slogdet(C)
            out[name]['nll'].append(0.5 * (logdet + realised @ np.linalg.solve(C, realised) + 2 * be.LOG2PI))
            out[name]['risk'].append((realised[0] - C[0, 1] / C[1, 1] * realised[1]) ** 2)
    return {k: {m: np.array(v) for m, v in d.items()} for k, d in out.items()}


def print_filter_comparison(res, log=print):
    """Fit and risk tables with paired power against the sample covariance."""
    base = res['sample']
    for metric, title in (('nll', 'fit: 5-day Gaussian NLL, lower is better'),
                          ('risk', 'risk: realised variance of the min-variance hedge, lower is better')):
        log(f'  {title}')
        log('    filter     mean        change vs sample [95% CI]          power  windows-for-80%')
        for name, d in res.items():
            x = d[metric]
            if name == 'sample':
                log(f'    {name:9s} {x.mean():10.4g}   (reference)')
                continue
            p = be.paired_power(x - base[metric], block=4)
            scale = 100 / base[metric].mean() if metric == 'risk' else 1.0
            unit = '%' if metric == 'risk' else ''
            n80 = f'{min(p["n_needed"], 99999):.0f}' if math.isfinite(p['n_needed']) else '-'
            log(f'    {name:9s} {x.mean():10.4g}   {scale * p["mean"]:+.4f}{unit} [{scale * p["ci"][0]:+.4f}{unit}, '
                f'{scale * p["ci"][1]:+.4f}{unit}]   {p["power"]:5.2f}  {n80:>6s}')
    log('  power: chance a 5% two-sided test detects an effect of the observed size with this many windows')


def anisotropy_by_horizon(S=100.0, v=0.25, T=63 / TD, kind='straddle', moneyness=1.0,
                          horizons=(1, 5, 10, 21, 42), sd_vol_daily=0.01, corr=-0.7, log=print):
    """How anisotropic the straddle's risk is as the horizon lengthens (shock covariance ~ horizon).

    lambda_min/max  ratio of whitened principal curvatures (0 = rank one, so FA = 1 regardless)
    FA              2-D fractional anisotropy of the whitened Hessian
    A3, A4          anisotropy (peak |q_k| / rms) of the order-3 and order-4 angular profiles
    circle          anisotropy of the exactly repriced loss on the 99% circle (peak / rms of the
                    positive part), which includes every order and the horizon carry
    E2..E4 share    share of nonlinear Gaussian energy by order
    """
    model = Vanilla(S * moneyness, T, kind=kind)
    sd_spot_daily = S * v / math.sqrt(TD)
    rows = []
    log(f'  {kind} K/S={moneyness:.2f}, tenor {T * TD:.0f}d, vol {v:.0%}, daily SD spot {sd_spot_daily:.2f} '
        f'vol {sd_vol_daily:.3f}, corr {corr}')
    log('  horizon  |lam| min/max  FA     A3     A4   circle   E2 share  E3 share  E4 share  ray deg  action')
    for h in horizons:
        if h / TD >= T:
            continue
        Sigma = h * shock_cov(sd_spot_daily, sd_vol_daily, corr)
        rv = be.risk_vector(model, (S, v), Sigma, h / TD, 1.0, n_theta=720)
        lam = rv.curvatures
        ratio = float(np.min(np.abs(lam)) / np.max(np.abs(lam))) if np.max(np.abs(lam)) > 0 else float('nan')
        th = np.linspace(0, 2 * math.pi, 720, endpoint=False)
        loss = -be.exact_pnl(model, (S, v), rv.L, rv.radius * np.column_stack([np.cos(th), np.sin(th)]), h / TD)
        pos = np.maximum(loss, 0.0)
        circ = float(pos.max() / np.sqrt(np.mean(pos ** 2))) if pos.any() else float('nan')
        e = rv.energy[1:]
        share = e / e.sum() if e.sum() > 0 else e
        row = {'horizon': h, 'ratio': ratio, 'fa': fractional_anisotropy(lam), 'a3': rv.tensor_peaks[3]['anisotropy'],
               'a4': rv.tensor_peaks[4]['anisotropy'], 'circle': circ, 'shares': share,
               'ray': rv.stress_theta, 'action': rv.action}
        rows.append(row)
        row['saddle'] = bool(lam[0] * lam[1] < 0)
        log(f'  {h:5d}d   {ratio:10.2e}{"s" if row["saddle"] else " "}  {row["fa"]:.3f}  {row["a3"]:.2f}   '
            f'{row["a4"]:.2f}   {circ:5.2f}    {share[0]:7.3f}   {share[1]:7.3f}   {share[2]:7.3f}   '
            f'{row["ray"]:6.1f}   {rv.action}')
    log('  (s = saddle: principal curvatures of opposite sign)')
    return rows


# ---------------------------------------------------------------------------
# Hedging a straddle with the underlying and longer-dated straddles
# ---------------------------------------------------------------------------

def straddle_hedge(S=100.0, v=0.25, T=0.25, Sigma=None, horizon=5 / TD, **kw):
    target = Vanilla(S, T, kind='straddle')
    instruments = [be.Underlying(), Vanilla(S, 2 * T, kind='straddle'), Vanilla(S, 4 * T, kind='straddle')]
    Sigma = shock_cov(2.0, 0.02, -0.7) if Sigma is None else Sigma
    return be.hedge_comparison(target, instruments, (S, v), Sigma, horizon, **kw)


# ---------------------------------------------------------------------------
# Direction-tracking backtest ("dipy" style)
# ---------------------------------------------------------------------------

def backtest_dipy(dates, S, V, tenor_days=63, horizon=5, start=504, lam=0.94, log=print):
    """Track the principal risk direction of a rolling ATM straddle through time.

    Each non-overlapping window: EWMA covariance of daily state shocks (dS, dv) from data before
    the window, scaled to the horizon; risk vector of a fresh ATM straddle; realised horizon loss
    compared with the worst loss on the 99% stress circle (a stress-VaR breach), and with the
    Taylor-4 estimate at the realised shock. Reports fibre continuity, FA and crossings.
    """
    S, V = np.asarray(S, float), np.asarray(V, float) / 100.0
    X = np.column_stack([np.diff(S), np.diff(V)])
    T, hy = tenor_days / TD, horizon / TD
    rows = []
    for t in range(start, len(S) - horizon, horizon):
        # spot moves rescaled to today's level (return x S_t); vol moves are already absolute
        scale = np.column_stack([S[t] / S[t - 504:t], np.ones(504)])
        C = horizon * cov_ewma(X[t - 504:t] * scale, lam)
        model = Vanilla(S[t], T)
        x0 = np.array([S[t], V[t]])
        rv = be.risk_vector(model, x0, C, hy, 1.0, n_theta=360)
        shock = np.array([S[t + horizon] - S[t], V[t + horizon] - V[t]])
        realised = -(model.price(S[t + horizon], max(V[t + horizon], 1e-3), T - hy) - model.price(*x0))
        z = np.linalg.solve(rv.L, shock)
        taylor = -rv.jet(z[None])[0]
        rows.append((principal_direction(rv), fractional_anisotropy(rv.curvatures), rv.crossing,
                     realised > rv.worst_loss, realised, taylor, rv.action == 'taylor4'))
    ang, fa, cross, breach, real, tay, trusted = map(np.array, zip(*rows))
    turn = direction_track(ang)
    err = np.abs(tay - real)
    log(f'{len(rows)} windows of {horizon} days; ATM straddle, {tenor_days}-day tenor, EWMA lambda {lam}')
    log(f'  principal direction: mean change {turn.mean():.1f} deg/window, fibre continuity '
        f'(change < 30 deg) {100 * np.mean(turn < 30):.1f}%')
    log(f'  fractional anisotropy mean {fa.mean():.3f}; crossings flagged in {100 * cross.mean():.1f}% of windows')
    log(f'  stress-circle breaches {100 * breach.mean():.2f}% (99% circle; Gaussian nominal ~1%)')
    log(f'  Taylor-4 at realised shock: median |error| {np.median(err):.4f}; trusted windows '
        f'{100 * trusted.mean():.1f}%, error trusted {np.median(err[trusted]) if trusted.any() else float("nan"):.4f} '
        f'vs untrusted {np.median(err[~trusted]) if (~trusted).any() else float("nan"):.4f}')
    if len(fa) > 10:
        rc = np.corrcoef(fa, err)[0, 1]
        log(f'  corr(FA, |Taylor-4 error|) {rc:+.3f}')
    return {'angle': ang, 'fa': fa, 'crossing': cross, 'breach': breach, 'realised': real, 'taylor': tay}


# ---------------------------------------------------------------------------
# Self-test and demo
# ---------------------------------------------------------------------------

def selftest():
    failures = []

    def check(name, ok, detail):
        print(f'[{"ok" if ok else "FAIL"}] {name}: {detail}')
        if not ok:
            failures.append(name)

    print('bsm_spectral self-test')
    S, K, v, T = 100.0, 100.0, 0.25, 0.25
    try:
        import bsm_parallel_surface as bps
    except ImportError as exc:  # pragma: no cover - the file ships alongside
        bps = None
        print(f'[skip] bsm_parallel_surface cross-check: {exc}')
    if bps is not None:
        st = bps.State(S, K, v, T, 0.04, 0.01)
        ref = bps.greeks(st)
        jg = jet_greeks(Vanilla(K, T, kind='call'), S, v)
        names = [('delta', 'delta_call'), ('vega', 'vega'), ('gamma', 'gamma'), ('vanna', 'vanna'),
                 ('vomma', 'vomma'), ('speed', 'speed'), ('zomma', 'zomma'),
                 ('spot_vol_vol', 'spot_vol_vol'), ('ultima', 'ultima')]
        rel = max(abs(jg[a] - ref[b]) / abs(ref[b]) for a, b in names)
        check('jets vs bsm_parallel_surface analytic Greeks', rel < 1e-9, f'max relative error {rel:.1e}')

        rng = np.random.default_rng(20261001)
        z = rng.standard_normal((400000, 2))
        ds, dv = 2 * z[:, 0], .02 * (-.7 * z[:, 0] + math.sqrt(1 - .49) * z[:, 1])
        mc = bps.curvature_energy(st, ds, dv, 3)
        exact = curvature_signal(Vanilla(K, T), (S, v), shock_cov(2.0, 0.02, -0.7), 0.0, order=3)
        check('curvature energy: exact moments vs Monte Carlo', abs(exact / mc - 1) < 0.02,
              f'exact {exact:.6e} vs bsm_parallel_surface MC {mc:.6e}')

    Jc = be.pnl_jet(Vanilla(K, T, kind='call'), np.array([S, v]), np.eye(2))
    Jp = be.pnl_jet(Vanilla(K, T, kind='put'), np.array([S, v]), np.eye(2))
    diff = (Jc - Jp).c
    deg = be._tables(2, be.DEG)[7]
    fwd = math.exp(-0.01 * T)
    err = max(np.abs(diff[deg >= 2]).max(), abs(diff[be._tables(2, be.DEG)[1][(1, 0)]] - fwd))
    check('put-call parity in the jets', err < 1e-12, f'call-put nonlinear residual / forward delta error {err:.1e}')

    res = straddle_hedge()
    G = np.column_stack([be.greeks(m, (S, v)) for m in
                         (be.Underlying(), Vanilla(S, 2 * T), Vanilla(S, 4 * T))])
    w = res[1].weights
    r = np.abs(G @ w + be.greeks(Vanilla(S, T), (S, v))).max()
    check('hierarchical straddle hedge', r < 1e-9, f'delta/gamma/vega residual {r:.1e}')

    X = np.random.default_rng(1).multivariate_normal([0, 0], [[1, -0.5], [-0.5, 2]], 3000)
    lam = 0.94
    w = (1 - lam) * lam ** np.arange(len(X) - 1, -1, -1)
    ok = all(np.all(np.linalg.eigvalsh(f(X)) > 0) for f in FILTERS.values())
    e = np.abs(cov_ewma(X, lam) - np.cov(X.T, aweights=w, bias=True)).max()
    check('covariance filters', ok and e < 1e-12, f'all positive definite; EWMA vs weighted covariance {e:.1e}')

    print(f'\n{"All checks passed." if not failures else "FAILED: " + ", ".join(failures)}')
    return not failures


def demo():
    print('DEMONSTRATION on stylised inputs (flat vol, no costs, fixed rate)\n')
    S, K, v, T = 100.0, 100.0, 0.25, 0.25
    Sigma = shock_cov(2.0, 0.02, -0.7)
    for kind in ('call', 'put', 'straddle'):
        model = Vanilla(K, T, kind=kind)
        rv = be.risk_vector(model, (S, v), Sigma, 0.0)
        print(be.format_risk_vector(rv, model.names, f'{kind}: S=100 K=100 vol=25% T=3m; shock SD spot 2, vol 0.02, corr -0.7'))
        print(f'  curvature signal (orders 2-4, budget $1) {curvature_signal(model, (S, v), Sigma):.4e}; '
              f'FA {fractional_anisotropy(rv.curvatures):.3f}; principal direction {principal_direction(rv):.1f} deg\n')
    print('Straddle(T) hedged with the underlying and straddles at 2T and 4T (5-day horizon, OOS exact repricing)')
    print(be.format_hedges(straddle_hedge()))
    print('\nStraddle anisotropy across horizons (ATM, 63-day tenor)')
    anisotropy_by_horizon()
    print('\nSame, 5% out-of-the-money call')
    anisotropy_by_horizon(kind='call', moneyness=1.05)
    dates, Sx, Vx = be.synthetic_market()
    X = np.column_stack([np.diff(np.log(Sx)), np.diff(Vx / 100)])
    print('\nShock-covariance filters, synthetic market (index log return, VIX change)')
    print_filter_comparison(compare_filters(X))


def run_data(dates, S, V):
    """Real-data diagnostics: filter comparison with power, anisotropy at today's state, fibre backtest."""
    X = np.column_stack([np.diff(np.log(S)), np.diff(np.asarray(V) / 100)])
    print(f'Shock-covariance filters, {dates[0]} .. {dates[-1]} (price log return, vol-index change)')
    print_filter_comparison(compare_filters(X))
    print(f'\nStraddle anisotropy across horizons at the last state (S {S[-1]:.2f}, vol {V[-1]:.2f})')
    sd_v = float(np.std(np.diff(np.asarray(V[-253:]) / 100)))
    rho = float(np.corrcoef(X[-252:].T)[0, 1])
    anisotropy_by_horizon(S=float(S[-1]), v=float(V[-1]) / 100, sd_vol_daily=sd_v, corr=rho)
    print()
    backtest_dipy(dates, S, V)


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split('\n\n')[0])
    ap.add_argument('--selftest', action='store_true')
    ap.add_argument('--demo', action='store_true')
    ap.add_argument('--backtest', action='store_true')
    ap.add_argument('--data', default=None)
    ap.add_argument('--synthetic', action='store_true')
    ap.add_argument('--underlying', default='SPX', choices=sorted(be.UNDERLYINGS),
                    help='registered underlying for --data runs')
    args = ap.parse_args(argv)
    if not (args.selftest or args.demo or args.backtest):
        ap.print_help()
        return 0
    if args.selftest and not selftest():
        return 1
    if args.demo:
        demo()
    if args.backtest:
        if args.data:
            run_data(*be.load_underlying(args.data, args.underlying))
            return 0
        if args.synthetic:
            dates, S, V = be.synthetic_market()
            print('Synthetic market (exercises the code; not market evidence)')
        else:
            ap.error('--backtest needs --data DIR or --synthetic')
        backtest_dipy(dates, S, V)
    return 0


if __name__ == '__main__':
    sys.exit(main())
