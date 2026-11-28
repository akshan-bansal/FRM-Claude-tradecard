"""Stage-2 validity experiments: the two uncertainty principles and the field equations.

U1  complementarity qubit: direction and volatility read along two Bloch axes at angle phi.
    Feasible forecast pairs (b_d, b_v) = (n_d.r, n_v.r), |r| <= 1, and the Maassen-Uffink bound.
U2  on S&P weeks (exploratory; the stage-2 test uses other underlyings): do honest forecasts
    violate the constraint, and does its implied lean match the data's sign?
U3  Christoffersen-Diebold: how much direction information P(up) = Phi(mu/sigma) can carry.
F1  static field equations m = tanh(h + J m): unique when ||J||_2 < 1, can split when not.
F2  dynamic field equations (mean-field Bloch) vs the exact transverse-field Ising evolution.
"""
from __future__ import annotations

import math
import runpy
import sys

import numpy as np
from scipy import stats

# usage: v_uncertainty_field.py REPO FRED_DIR STAGE1_DIR  (STAGE1_DIR = reports/qhft/stage1_2026-11-28)
STAGE1 = sys.argv[3]
sys.path.insert(0, f"{STAGE1}/qhft")    # the stage-1 exact simulator
from sim import Propagator, encode, readout  # noqa: E402

rng = np.random.default_rng(0)


def h2(p: np.ndarray) -> np.ndarray:
    p = np.clip(p, 1e-15, 1 - 1e-15)
    return -(p * np.log2(p) + (1 - p) * np.log2(1 - p))


print("U1  complementarity: axes n_d = (0,0,1), n_v at angle phi in the x-z plane")
for phi_deg in (90.0, 60.0, 41.4, 20.0):
    phi = math.radians(phi_deg)
    nd, nv = np.array([0, 0, 1.0]), np.array([math.sin(phi), 0, math.cos(phi)])
    r = rng.normal(size=(200000, 3))
    r *= (rng.random(200000) ** (1 / 3) / np.linalg.norm(r, axis=1))[:, None]   # uniform in the ball
    bd, bv = r @ nd, r @ nv
    ellipse = (bd**2 + bv**2 - 2 * bd * bv * math.cos(phi)) / math.sin(phi) ** 2
    Hsum = h2((1 + bd) / 2) + h2((1 + bv) / 2)
    mu_bound = -math.log2((1 + abs(math.cos(phi))) / 2)
    print(f"    phi={phi_deg:5.1f}  max ellipse form {ellipse.max():.4f} (<=1 feasible)  "
          f"min H_d+H_v {Hsum.min():.4f} bits >= MU bound {mu_bound:.4f}  holds={Hsum.min() >= mu_bound - 1e-12}")
    # the corner case: b_d = 0 caps |b_v| at sin(phi); b_v = 1 forces b_d = cos(phi)
print("    => with no direction view (b_d = 0), vol confidence |b_v| is capped at sin(phi);")
print("       full vol confidence (b_v = 1) forces a direction lean b_d = cos(phi)")

# ---------------- exploratory S&P weeks ----------------
sys.argv = ["core", sys.argv[1], sys.argv[2]]
g = runpy.run_path(f"{STAGE1}/validity/v_bsm_atoms_core.py", run_name="core")
S, V, A = g["S"], g["V"] / 100.0, g["A"]
r = np.diff(np.log(S))
rV = np.diff(V)
idx = np.arange(796, len(S) - 5, 5)
fwd = np.array([np.log(S[t + 5] / S[t]) for t in idx])
up = (fwd > 0).astype(float)
sig_w = A["gjr_vol"][idx] / math.sqrt(252) * math.sqrt(5)           # weekly GJR sigma forecast
rv_next = np.array([math.sqrt(np.sum(r[t:t + 5] ** 2)) for t in idx])
volup = (rv_next > sig_w).astype(float)                              # next week hotter than forecast
rho = np.array([np.corrcoef(r[t - 252:t], rV[t - 252:t])[0, 1] for t in idx])
print(f"\nU2  S&P, {len(idx)} weeks (exploratory)")
print(f"    trailing spot-vol correlation rho: median {np.median(rho):+.2f}  -> phi = arccos(rho) ~ {math.degrees(math.acos(np.median(rho))):.0f} deg")
print(f"    outcome correlation corr(up, vol-up) = {np.corrcoef(up, volup)[0, 1]:+.2f}  (leverage, same week)")
# honest walk-forward marginals: logistic on (gjr_vol, vol_premium), refit every 13 weeks after 104
X = np.column_stack([A["gjr_vol"][idx], A["vol_premium"][idx]])
X = (X - X[:104].mean(0)) / X[:104].std(0)


def wf_logit(y: np.ndarray) -> np.ndarray:
    from scipy.optimize import minimize
    p = np.full(len(y), np.nan)
    for b in range(104, len(y), 13):
        Xt, yt = X[:b], y[:b]
        f = lambda w: np.sum(np.logaddexp(0, Xt @ w[1:] + w[0]) - yt * (Xt @ w[1:] + w[0])) + 0.5 * w[1:] @ w[1:]
        w = minimize(f, np.zeros(3)).x
        e = slice(b, min(b + 13, len(y)))
        p[e] = 1 / (1 + np.exp(-(X[e] @ w[1:] + w[0])))
    return p


pu, pv = wf_logit(up), wf_logit(volup)
ev = np.isfinite(pu)
bd, bv = 2 * pu[ev] - 1, 2 * pv[ev] - 1
phi = np.arccos(rho[ev])
form = (bd**2 + bv**2 - 2 * bd * bv * np.cos(phi)) / np.sin(phi) ** 2
print(f"    walk-forward forecasts, {ev.sum()} weeks: |b_d| p95 {np.percentile(abs(bd), 95):.2f}, |b_v| p95 {np.percentile(abs(bv), 95):.2f}")
print(f"    weeks violating the complementarity constraint: {np.mean(form > 1):.0%}")
lean = np.cos(phi) * bv      # the direction the constraint pushes toward as b_v grows
print(f"    implied lean vs realised direction: corr(cos(phi)*b_v, up) = {stats.spearmanr(lean, up[ev]).statistic:+.3f};"
      f"  actual forecast corr(b_d, up) = {stats.spearmanr(bd, up[ev]).statistic:+.3f}")
ll = lambda p, y: -np.mean(y * np.log(p) + (1 - y) * np.log(1 - p))
print(f"    log loss: direction {ll(pu[ev], up[ev]):.4f} vs base rate {ll(np.full(ev.sum(), up[:104].mean()), up[ev]):.4f};"
      f"  vol-up {ll(pv[ev], volup[ev]):.4f} vs base rate {ll(np.full(ev.sum(), volup[:104].mean()), volup[ev]):.4f}")

print("\nU3  Christoffersen-Diebold: P(up) = Phi(mu/sigma), mu = expanding mean weekly return")
mu = np.array([np.mean(fwd[:max(i - 1, 1)]) if i > 1 else 0.0 for i in range(len(idx))])
pcd = stats.norm.cdf(mu / sig_w)
print(f"    P(up) range over evaluation weeks: {pcd[ev].min():.3f} .. {pcd[ev].max():.3f}")
print(f"    log loss {ll(pcd[ev], up[ev]):.4f} vs base rate {ll(np.full(ev.sum(), up[:104].mean()), up[ev]):.4f}")

print("\nF1  static field equations m = tanh(h + J m)")
n = 8
for scale in (0.5, 0.95, 1.5, 3.0):
    J = rng.normal(size=(n, n))
    J = (J + J.T) / 2
    np.fill_diagonal(J, 0)
    J *= scale / np.linalg.norm(J, 2)
    h = rng.normal(scale=0.3, size=n)
    sols = set()
    for _ in range(50):
        m = rng.uniform(-1, 1, n)
        for _ in range(5000):
            m_new = np.tanh(h + J @ m)
            if np.max(abs(m_new - m)) < 1e-12:
                break
            m = 0.5 * m + 0.5 * m_new
        sols.add(tuple(np.round(m, 6)))
    print(f"    ||J||_2 = {scale:4.2f}: {len(sols)} distinct fixed point(s) from 50 starts")

print("\nF2  dynamic field equations (mean-field Bloch) vs exact evolution, 8 qubits, t = 1")


def bloch_mf(x: np.ndarray, Jm: np.ndarray, gamma: float, t: float, steps: int = 2000) -> np.ndarray:
    th = np.arccos(x)
    s = np.column_stack([np.sin(th), np.zeros_like(th), np.cos(th)])     # Bloch vectors of RY(arccos x)|0>
    dt = t / steps

    def f(s: np.ndarray) -> np.ndarray:
        B = np.column_stack([2 * gamma * np.ones(len(s)), np.zeros(len(s)), 2 * Jm @ s[:, 2]])
        return np.cross(B, s)

    for _ in range(steps):
        k1 = f(s); k2 = f(s + dt / 2 * k1); k3 = f(s + dt / 2 * k2); k4 = f(s + dt * k3)
        s = s + dt / 6 * (k1 + 2 * k2 + 2 * k3 + k4)
    return s[:, 2]


x = rng.uniform(-0.9, 0.9, n)
for Jscale in (0.0, 0.1, 0.3, 0.6):
    Jm = rng.normal(size=(n, n)); Jm = (Jm + Jm.T) / 2; np.fill_diagonal(Jm, 0); Jm *= Jscale / np.abs(Jm).max()
    Jd = {(i, j): float(Jm[i, j]) for i in range(n) for j in range(i + 1, n)}
    exact = readout(Propagator.build(n, Jd, 0.4).evolve(encode(x), 1.0), [])[0]
    mf = bloch_mf(x, Jm, 0.4, 1.0)
    print(f"    max|J| = {Jscale:.1f}: max |<Z> mean-field - exact| = {np.max(abs(mf - exact)):.2e}")
