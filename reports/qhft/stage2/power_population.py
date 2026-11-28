"""Does significance amplify over a larger concurrent population of assets? Tested, not assumed.

**Part A (the statistics alone).**
- Loss differences are generated as ``d[w, a] = delta + sqrt(rho) common[w] + sqrt(1 - rho) idio[w, a]``.
- They are scored with the week-clustered bootstrap the stage-2 benchmark uses.
- Pass means the 95% interval lies below 0.

**Part B (the real machinery).**
- A synthetic panel in which every asset's next-week volatility takes a spillover from asset 0.
- Walk-forward ``field`` vs ``uncoupled`` at 2, 4 and 8 assets.

Usage: ``python reports/qhft/stage2/power_population.py`` (synthetic only, no data).
"""
from __future__ import annotations

import math
import sys
from concurrent.futures import ProcessPoolExecutor
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[3] / "src"))
from trading_live_claude.quantum.panel import (
    Panel,
    WalkForwardSpec,
    design_effect,
    walk_forward,
    week_cluster_bootstrap,
)

WEEKS = 233          # the stage-1 evaluation length
SD = 0.6             # per asset-week sd of a log-loss difference (stage 1: 0.55-0.65)


def part_a(delta: float, rho: float, n: int, seed: int) -> bool:
    r = np.random.default_rng(seed)
    d = delta + SD * (math.sqrt(rho) * r.normal(size=(WEEKS, 1)) + math.sqrt(1 - rho) * r.normal(size=(WEEKS, n)))
    return week_cluster_bootstrap(d, block=8, reps=1000, seed=seed)[2] < 0


def panel(seed: int, coupling: float, N: int, W: int = 337) -> Panel:
    r = np.random.default_rng(seed)
    X = np.empty((W, N, 3))
    X[:, :, 0] = np.exp(r.normal(-1.8, 0.4, (W, N)))
    X[:, :, 1] = r.normal(0.1, 0.2, (W, N))
    X[:, :, 2] = r.uniform(-0.9, -0.5, (W, N))
    z = (np.log(X[:, :, 0]) + 1.8) / 0.4
    spill = np.zeros((W, N))
    spill[:, 1:] = coupling * z[:, [0]]
    common = r.normal(size=(W, 1))
    rv = np.exp(-1.8 + 0.4 * (0.7 * z + spill + 0.5 * (0.6 * common + 0.8 * r.normal(size=(W, N)))))
    ret = r.normal(0.001, rv / math.sqrt(252 / 5))
    rv[-1], ret[-1] = np.nan, np.nan
    return Panel(tuple(f"A{i}" for i in range(N)), ("gjr_vol", "vol_premium", "spot_vol_corr"),
                 tuple(str(w) for w in range(W)), X, rv, ret)


SPEC = WalkForwardSpec(models=("uncoupled", "field"), refit_every=13, network_refit_every=26, boot_reps=1000)


def part_b(args: tuple[int, float, int]) -> tuple[float, float, float, bool]:
    seed, coupling, N = args
    return walk_forward(panel(seed, coupling, N), SPEC).verdict("field", "uncoupled")


def main() -> None:
    print("A. pass rate of the 95% week-clustered interval, 233 weeks, 300 simulations per cell")
    print(f"   {'delta':>7} {'rho':>5} " + " ".join(f"{f'N={n}':>7}" for n in (1, 2, 4, 8, 16)) + "   design effect at N=16")
    with ProcessPoolExecutor() as ex:
        for delta in (-0.02, 0.0, +0.05):
            for rho in (0.0, 0.5, 0.9):
                rates = []
                for n in (1, 2, 4, 8, 16):
                    rates.append(np.mean(list(ex.map(part_a, [delta] * 300, [rho] * 300, [n] * 300, range(300)))))
                print(f"   {delta:+7.2f} {rho:5.1f} " + " ".join(f"{x:7.0%}" for x in rates)
                      + f"   {design_effect(rho, 16):5.1f}")
        print("\nB. walk-forward field vs uncoupled on a planted spillover panel (8 seeds per cell)")
        for coupling in (0.0, 0.6):
            for N in (2, 4, 8):
                out = list(ex.map(part_b, [(s, coupling, N) for s in range(8)]))
                widths = [hi - lo for _, lo, hi, _ in out]
                print(f"   spillover {coupling:.1f}, {N} assets: mean diff {np.mean([o[0] for o in out]):+.4f}, "
                      f"interval width {np.mean(widths):.4f}, pass {sum(o[3] for o in out)}/8")


if __name__ == "__main__":
    main()
