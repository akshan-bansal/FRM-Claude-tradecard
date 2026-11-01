#!/usr/bin/env python3
"""bsm_detect.py -- multi-modal threat detection with calibrated false-alarm control.

The spectral-VaR machinery applied to screening: the hard statistical problem in explosive
detection is the same as in risk -- holding a tiny false-alarm rate in a heavy-tailed null while
detecting faint signatures. Each modality produces a detection score; scores are calibrated into
p-values with peaks-over-threshold GPD tails fitted on clean (benign) items, and fused across
modalities with the null correlation taken into account. Alarm-rate calibration is tested with
Kupiec / Christoffersen, thresholds are held under sensor drift by the PID loop, and drift
itself is flagged by CUSUM -- all from bsm_event.

Modalities (plug-ins; each maps a raw item to one score, larger = more threat-like)
  spectrum  1-D spectra (Raman / IR / ion-mobility). Adaptive matched filter against a library
            signature, with the background covariance denoised by the Marchenko-Pastur filter
            (the background is estimated from fewer clean items than channels allow).
  image     dual-energy X-ray (high / low attenuation channels). Reed-Xiaoli anomaly score of
            local material features against the clean-pixel distribution; max over the item.
  trace     vapour / trace-sensor time series. One-sided CUSUM of standardised readings.

Fusion
  fisher    combines p-values as if modalities were independent (miscalibrated when benign
            clutter -- a dense or cluttered bag -- raises every sensor's score together)
  stouffer  correlation-aware: z = 1' R^-1 z_m / sqrt(1' R^-1 1), R = null correlation of the
            normal scores, then GPD-calibrated like any other score

Everything here runs on synthetic data: signatures are generic placeholders read from a
library (load real reference signatures with --library), not real explosive spectra. This is a
statistical framework for evaluating and calibrating detectors, not a certified screening system;
operational use needs certified sensor data and evaluation to the applicable standards.

Install: python -m pip install numpy scipy
Run:     python bsm_detect.py --selftest
         python bsm_detect.py --demo
         python bsm_detect.py --demo --pfa 1e-4 --strength 0.5
"""
from __future__ import annotations

import argparse
import json
import math
import sys
from dataclasses import dataclass

import numpy as np
from scipy import stats

import bsm_event as be

# ---------------------------------------------------------------------------
# 1. Score calibration: peaks-over-threshold GPD tail on clean-item scores
# ---------------------------------------------------------------------------


class TailCalibrator:
    """p-value of a score under the benign null: empirical below the q-quantile, GPD above it.

    The GPD tail is what makes very small false-alarm rates (1e-3 .. 1e-5) estimable from a few
    thousand clean items -- the same peaks-over-threshold idea as the semi-parametric VaR tails.
    """

    def __init__(self, null_scores, q=0.90):
        s = np.sort(np.asarray(null_scores, float))
        self.null = s
        self.q = q
        self.u = float(np.quantile(s, q))
        exc = s[s > self.u] - self.u
        c, _, scale = stats.genpareto.fit(exc, floc=0.0)
        self.xi, self.beta = float(c), float(scale)

    def sf(self, x):
        """P(score >= x | benign)."""
        x = np.asarray(x, float)
        n = len(self.null)
        # continuity-corrected empirical tail keeps p strictly inside (0, 1) (finite normal scores)
        emp = (n - np.searchsorted(self.null, x, side='left') + 0.5) / (n + 1)
        tail = (1 - self.q) * stats.genpareto.sf(x - self.u, self.xi, 0.0, self.beta)
        return np.clip(np.where(x > self.u, tail, emp), 1e-300, 1.0)

    def threshold(self, pfa):
        """Score threshold with false-alarm probability pfa."""
        if pfa >= 1 - self.q:
            return float(np.quantile(self.null, 1 - pfa))
        return self.u + float(stats.genpareto.ppf(1 - pfa / (1 - self.q), self.xi, 0.0, self.beta))


# ---------------------------------------------------------------------------
# 2. Modality detectors
# ---------------------------------------------------------------------------


class SpectrumDetector:
    """Adaptive matched filter: (t' S^-1 (x - mu)) / sqrt(t' S^-1 t) with background covariance S.

    covariance='mp' denoises the background correlation with the Marchenko-Pastur filter before
    inverting it, which matters when clean training spectra are few relative to channels."""
    name = 'spectrum'

    def __init__(self, signature, covariance='mp'):
        t = np.asarray(signature, float)
        self.t = t / np.linalg.norm(t)
        self.covariance = covariance

    def fit(self, clean):
        X = np.asarray(clean, float)
        self.mu = X.mean(0)
        sd = X.std(0, ddof=1)
        C = np.corrcoef(X.T)
        if self.covariance == 'mp':
            C = be.mp_filter(C, len(X))[0]
        S = C * np.outer(sd, sd)
        self.w = np.linalg.solve(S + 1e-9 * np.trace(S) / len(S) * np.eye(len(S)), self.t)
        self.norm = math.sqrt(float(self.t @ self.w))
        return self

    def score(self, X):
        return (np.asarray(X, float) - self.mu) @ self.w / self.norm


class ImageDetector:
    """Reed-Xiaoli anomaly score of local dual-energy features, maximised over the item.

    Features per pixel (3x3 box-averaged): log high-energy attenuation and the low/high ratio,
    a crude effective-atomic-number proxy. The clean-pixel mean and covariance define the null."""
    name = 'image'

    @staticmethod
    def _features(imgs):
        imgs = np.asarray(imgs, float)  # (n, 2, H, W): channels high, low
        k = np.ones(3) / 3
        sm = np.apply_along_axis(lambda r: np.convolve(r, k, 'same'), -1, imgs)
        sm = np.apply_along_axis(lambda r: np.convolve(r, k, 'same'), -2, sm)
        hi, lo = np.maximum(sm[:, 0], 1e-6), np.maximum(sm[:, 1], 1e-6)
        return np.stack([np.log(hi), lo / hi], axis=-1)[:, 1:-1, 1:-1]  # drop edge pixels

    def fit(self, clean):
        F = self._features(clean).reshape(-1, 2)
        self.mu = F.mean(0)
        self.Pi = np.linalg.inv(np.cov(F.T))
        return self

    def score(self, imgs):
        D = self._features(imgs) - self.mu
        rx = np.einsum('nhwi,ij,nhwj->nhw', D, self.Pi, D)
        return rx.reshape(len(rx), -1).max(1)


class TraceDetector:
    """Maximum one-sided CUSUM of standardised readings (transient vapour / trace plume)."""
    name = 'trace'

    def __init__(self, k=0.5):
        self.k = k

    def fit(self, clean):
        X = np.asarray(clean, float)
        self.mu, self.sd = float(X.mean()), float(X.std())
        return self

    def score(self, X):
        Z = (np.asarray(X, float) - self.mu) / self.sd
        s = np.zeros(len(Z))
        best = np.zeros(len(Z))
        for t in range(Z.shape[1]):
            s = np.maximum(0.0, s + Z[:, t] - self.k)
            best = np.maximum(best, s)
        return best


DETECTORS = {'spectrum': SpectrumDetector, 'image': ImageDetector, 'trace': TraceDetector}

# ---------------------------------------------------------------------------
# 3. Fusion across modalities
# ---------------------------------------------------------------------------


def fisher(pvals):
    """Fisher's method: valid only if modalities are independent under the null."""
    P = np.clip(np.asarray(pvals, float), 1e-300, 1.0)
    return stats.chi2.sf(-2 * np.log(P).sum(1), 2 * P.shape[1])


class StoufferFusion:
    """Linear fusion of normal scores z_m of the modality p-values, then GPD-calibrated.

    Without threat simulants: correlation-aware Stouffer, w = R^-1 1 (equal evidence per modality).
    With simulants (labelled test articles, as used in certification): the optimal linear weights
    w = R^-1 delta, delta = mean z shift of simulants over clean items (Fisher discriminant), which
    lets the strongest modality dominate instead of being diluted. R is the clean-item correlation;
    w is scaled to unit variance under the null."""

    def fit(self, clean_pvals, threat_pvals=None):
        Z = stats.norm.isf(np.clip(clean_pvals, 1e-300, 1.0))
        self.R = np.corrcoef(Z.T)
        if threat_pvals is None:
            direction = np.ones(len(self.R))
        else:
            direction = stats.norm.isf(np.clip(threat_pvals, 1e-300, 1.0)).mean(0) - Z.mean(0)
        w = np.linalg.solve(self.R, direction)
        self.w = w / math.sqrt(float(w @ self.R @ w))
        self.cal = TailCalibrator(Z @ self.w)
        return self

    def score(self, pvals):
        return stats.norm.isf(np.clip(pvals, 1e-300, 1.0)) @ self.w

    def pvalue(self, pvals):
        return self.cal.sf(self.score(pvals))


# ---------------------------------------------------------------------------
# 4. Synthetic multi-modal items (generic placeholder signatures)
# ---------------------------------------------------------------------------

DEFAULT_LIBRARY = {
    'signature_A': {'peaks': [[31, 1.0, 1.5], [58, 0.7, 2.0], [97, 0.5, 1.2]]},  # channel, height, width
}


def library_signature(library, name, D=128):
    x = np.arange(D)
    return sum(h * np.exp(-0.5 * ((x - c) / w) ** 2) for c, h, w in library[name]['peaks'])


@dataclass
class World:
    D: int = 128            # spectral channels
    H: int = 16             # image size
    T: int = 200            # trace length
    clutter_sd: float = 0.5  # shared benign nuisance (log scale) raising every modality together


def simulate(n, world, rng, strength=0.0, signature=None, drift=0.0):
    """n items with a shared clutter factor; strength > 0 inserts the threat in every modality."""
    D, H, T = world.D, world.H, world.T
    clutter = np.exp(world.clutter_sd * rng.standard_normal(n))
    # spectra: smooth baseline + clutter-driven broad structure (low rank) + channel noise
    x = np.linspace(0, 1, D)
    basis = np.stack([np.exp(-0.5 * ((x - c) / 0.18) ** 2) for c in (0.15, 0.4, 0.65, 0.9)])
    coef = rng.standard_normal((n, 4)) * clutter[:, None]
    spec = 1.0 + coef @ basis + 0.25 * clutter[:, None] * rng.standard_normal((n, D)) + drift
    if strength and signature is not None:
        spec = spec + strength * signature
    # images: smooth benign attenuation field, low/high ratio near 1.3, clutter thickens objects
    base = 1.0 + 0.3 * clutter[:, None, None] * rng.standard_normal((n, H, H))
    k = np.ones(5) / 5
    base = np.apply_along_axis(lambda r: np.convolve(r, k, 'same'), -1, base)
    base = np.apply_along_axis(lambda r: np.convolve(r, k, 'same'), -2, base)
    hi = np.exp(base) * (1 + 0.02 * rng.standard_normal((n, H, H)))
    lo = hi * (1.3 + 0.05 * clutter[:, None, None] * rng.standard_normal((n, H, H)))
    if strength:
        r0, c0 = rng.integers(3, H - 6, n), rng.integers(3, H - 6, n)
        for i in range(n):
            lo[i, r0[i]:r0[i] + 3, c0[i]:c0[i] + 3] *= 1 + 0.25 * strength  # anomalous material ratio
    imgs = np.stack([hi, lo], axis=1)
    # traces: AR(1) sensor noise scaled by clutter, plus a transient plume
    e = rng.standard_normal((n, T)) * clutter[:, None]
    tr = np.zeros((n, T))
    for t in range(1, T):
        tr[:, t] = 0.8 * tr[:, t - 1] + 0.6 * e[:, t]
    tr += drift
    if strength:
        t0 = rng.integers(20, T - 20, n)
        tt = np.arange(T)
        tr += 6.0 * strength * np.exp(-0.5 * ((tt[None, :] - t0[:, None]) / 4.0) ** 2)
    return {'spectrum': spec, 'image': imgs, 'trace': tr}


# ---------------------------------------------------------------------------
# 5. Pipeline: fit on clean items, calibrate, evaluate
# ---------------------------------------------------------------------------


class MultiModalScreen:
    def __init__(self, signature, spectrum_covariance='mp'):
        self.det = {'spectrum': SpectrumDetector(signature, spectrum_covariance), 'image': ImageDetector(),
                    'trace': TraceDetector()}

    def fit(self, train, calib, simulants=None):
        """Detectors on `train` clean items; tail calibration and fusion on separate `calib` items;
        optimal fusion weights from `simulants` (threat test articles) when given."""
        for m, d in self.det.items():
            d.fit(train[m])
        self.cal = {m: TailCalibrator(d.score(calib[m])) for m, d in self.det.items()}
        Pc = self.pvalues(calib)
        self.fusion = StoufferFusion().fit(Pc)
        self.optimal = StoufferFusion().fit(Pc, self.pvalues(simulants)) if simulants is not None else None
        return self

    def pvalues(self, items):
        return np.column_stack([self.cal[m].sf(d.score(items[m])) for m, d in self.det.items()])

    def all_pvalues(self, items):
        P = self.pvalues(items)
        out = {m: P[:, j] for j, m in enumerate(self.det)}
        out['fisher'] = fisher(P)
        out['stouffer'] = self.fusion.pvalue(P)
        if self.optimal is not None:
            out['optimal'] = self.optimal.pvalue(P)
        return out


def evaluate(screen, clean, threat, pfa):
    """Observed false-alarm rate (with Kupiec p), detection rate at pfa, and AUC per method."""
    pc, pt = screen.all_pvalues(clean), screen.all_pvalues(threat)
    rows = {}
    for m in pc:
        fa = pc[m] <= pfa
        y = np.r_[np.zeros(len(pc[m])), np.ones(len(pt[m]))]
        s = -np.log(np.r_[pc[m], pt[m]])
        rows[m] = {'pfa_obs': float(fa.mean()), 'kupiec_p': be.kupiec(fa, pfa)[1],
                   'pd': float((pt[m] <= pfa).mean()), 'auc': be.auc(s, y)}
    return rows


def drift_control(screen, world, rng, pfa, n_batches=60, batch=500, ramp=0.6):
    """Benign drift (sensor baseline creeping up): fixed threshold vs the PID-controlled one.

    The PID loop scales the fused-score threshold from observed alarms on clean traffic (its log
    scale reacts to 1{alarm} - pfa per item); CUSUM on batch alarm counts flags the drift."""
    z_thr = screen.fusion.cal.threshold(pfa)
    # integral control on the batch alarm-rate error (rate - pfa); a per-item breach indicator
    # would saturate the clamp on every alarm and anti-windup would then freeze the integral
    pid = be.PIDVaRScale(alpha=1 - pfa, kp=0.0, ki=0.02 / pfa, lo=0.8, hi=1.5)  # gain in units of the target rate
    fixed, ctrl, excess = [], [], []
    for b in range(n_batches):
        items = simulate(batch, world, rng, drift=ramp * b / n_batches)
        z = screen.fusion.score(screen.pvalues(items))
        a_fixed = z > z_thr
        a_ctrl = z > z_thr * pid.factor
        fixed.append(a_fixed.mean())
        ctrl.append(a_ctrl.mean())
        pid.update(float(a_ctrl.mean()))  # error = batch alarm rate - pfa
        excess.append(a_fixed.sum() - batch * pfa)
    _, alarms = be.cusum(np.array(excess) / math.sqrt(batch * pfa), k=0.5, h=4.0)
    return np.array(fixed), np.array(ctrl), alarms


# ---------------------------------------------------------------------------
# 6. Self-test, demo, CLI
# ---------------------------------------------------------------------------


def selftest():
    failures = []

    def check(name, ok, detail):
        print(f'[{"ok" if ok else "FAIL"}] {name}: {detail}')
        if not ok:
            failures.append(name)

    print('bsm_detect self-test (synthetic data)')
    rng = np.random.default_rng(1)
    null = stats.t.rvs(4, size=20000, random_state=2)
    cal = TailCalibrator(null[:5000])
    fresh = stats.t.rvs(4, size=200000, random_state=3)
    rates = {a: float((fresh > cal.threshold(a)).mean()) for a in (1e-2, 1e-3, 1e-4)}
    ok = all(abs(r / a - 1) < 0.35 for a, r in rates.items())
    check('GPD tail calibration at small false-alarm rates', ok,
          ', '.join(f'target {a:.0e} observed {r:.2e}' for a, r in rates.items()) + ' (5,000 calibration items)')

    D, n = 128, 200
    sig = library_signature(DEFAULT_LIBRARY, 'signature_A')
    w = World()
    tr = simulate(n, w, rng)
    cl = simulate(4000, w, rng)
    th = simulate(4000, w, rng, strength=0.35, signature=sig)
    pds = {}
    for cov in ('sample', 'mp'):
        d = SpectrumDetector(sig, cov).fit(tr['spectrum'])
        c = TailCalibrator(d.score(cl['spectrum'][:2000]))
        thr = c.threshold(1e-2)
        pds[cov] = float((d.score(th['spectrum']) > thr).mean())
    check('Marchenko-Pastur denoised matched filter', pds['mp'] > pds['sample'],
          f'Pd at Pfa 1e-2 with {n} training spectra x {D} channels: sample covariance {pds["sample"]:.3f}, '
          f'MP-filtered {pds["mp"]:.3f}')

    screen = MultiModalScreen(sig).fit(simulate(400, w, rng), simulate(5000, w, rng),
                                       simulants=simulate(500, w, rng, 0.75, sig))
    res = evaluate(screen, simulate(20000, w, rng), simulate(2000, w, rng, 0.75, sig), 1e-3)
    best = max(res[m]['pd'] for m in ('spectrum', 'image', 'trace'))
    ok = (res['fisher']['pfa_obs'] > 2 * 1e-3 and res['stouffer']['kupiec_p'] > 0.01
          and res['optimal']['kupiec_p'] > 0.01 and res['optimal']['pd'] > best)
    check('correlation-aware fusion', ok,
          f'at Pfa 1e-3: false alarms Fisher {res["fisher"]["pfa_obs"]:.2e} (independence assumed), Stouffer '
          f'{res["stouffer"]["pfa_obs"]:.2e}, optimal {res["optimal"]["pfa_obs"]:.2e} (Kupiec p '
          f'{res["optimal"]["kupiec_p"]:.2f}); Pd optimal {res["optimal"]["pd"]:.3f} vs best single {best:.3f}')

    fixed, ctrl, alarms = drift_control(screen, w, np.random.default_rng(5), 1e-2)
    late = slice(len(fixed) // 2, None)
    check('drift: PID-held threshold and CUSUM alarm', ctrl[late].mean() < fixed[late].mean() and len(alarms) > 0,
          f'second-half false-alarm rate fixed {fixed[late].mean():.4f} vs PID {ctrl[late].mean():.4f} '
          f'(target 0.0100); CUSUM drift alarm at batch {alarms[0] if alarms else "-"}')

    print(f'\n{"All checks passed." if not failures else "FAILED: " + ", ".join(failures)}')
    return not failures


def demo(pfa=1e-3, strength=0.75, seed=7, library=None):
    lib = DEFAULT_LIBRARY if library is None else json.load(open(library, encoding='utf-8'))
    name = next(iter(lib))
    sig = library_signature(lib, name)
    rng = np.random.default_rng(seed)
    w = World()
    print(f'Synthetic screening lane: signature "{name}" (generic placeholder), strength {strength}, '
          f'target Pfa {pfa:.0e}; shared clutter sd {w.clutter_sd}')
    screen = MultiModalScreen(sig).fit(simulate(400, w, rng), simulate(5000, w, rng),
                                       simulants=simulate(500, w, rng, strength, sig))
    res = evaluate(screen, simulate(40000, w, rng), simulate(4000, w, rng, strength, sig), pfa)
    print('  method     false-alarm rate   Kupiec p   detection rate   AUC')
    for m, r in res.items():
        flag = '  <- over its false-alarm target; Pd not comparable' if r['kupiec_p'] < 0.01 and r['pfa_obs'] > pfa else ''
        print(f'  {m:9s}  {r["pfa_obs"]:.2e}           {r["kupiec_p"]:6.3f}     {r["pd"]:.3f}            {r["auc"]:.4f}{flag}')
    print(f'  null correlation of modality scores (clutter): \n    '
          + np.array2string(screen.fusion.R, precision=2).replace('\n', '\n    '))
    print('\nDetection rate vs threat strength at the same calibrated Pfa (simulants fixed at the demo strength):')
    print('  strength   spectrum   image   trace   stouffer   optimal')
    for s in (0.25, 0.5, 0.75, 1.0):
        r = evaluate(screen, simulate(2000, w, rng), simulate(2000, w, rng, s, sig), pfa)
        print(f'  {s:8.2f}   ' + '      '.join(f'{r[m]["pd"]:.3f}' for m in ('spectrum', 'image', 'trace', 'stouffer', 'optimal')))
    fixed, ctrl, alarms = drift_control(screen, w, rng, max(pfa, 1e-2))
    print(f'\nBaseline drift over 60 batches: false-alarm rate in the last 10 batches fixed threshold '
          f'{fixed[-10:].mean():.4f}, PID-held {ctrl[-10:].mean():.4f} (target {max(pfa, 1e-2):.4f}); '
          f'CUSUM drift alarms at batches {alarms[:5]}')
    print('\nSynthetic data only: placeholder signatures and simulated sensors; not a certified system.')


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split('\n\n')[0])
    ap.add_argument('--selftest', action='store_true')
    ap.add_argument('--demo', action='store_true')
    ap.add_argument('--pfa', type=float, default=1e-3)
    ap.add_argument('--strength', type=float, default=0.75)
    ap.add_argument('--library', default=None, help='JSON {name: {"peaks": [[channel, height, width], ...]}}')
    args = ap.parse_args(argv)
    if not (args.selftest or args.demo):
        ap.print_help()
        return 0
    if args.selftest and not selftest():
        return 1
    if args.demo:
        demo(args.pfa, args.strength, library=args.library)
    return 0


if __name__ == '__main__':
    sys.exit(main())
