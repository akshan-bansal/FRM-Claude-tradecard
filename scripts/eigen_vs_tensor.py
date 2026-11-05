"""Test eigenrepresentations against eigentensor representations of joint returns. Research only.

Three questions, in order, on daily bars saved as ``{SYMBOL}.parquet``:
  1. Structure: is there anything beyond correlation, against elliptical simulations?
  2. Eigenfidelity and eigenpersistence: do the directions hold up between disjoint windows,
     against shuffled dates (time removed) and independently shuffled columns (structure removed)?
  3. Value-at-risk: do tensors improve 99% VaR out of sample over the best matrix method?

The rule is fixed before the run: a tensor method counts as better only if it beats the best
matrix method on pinball loss with a Holm-adjusted Diebold-Mariano p below 0.05. The best matrix
method is chosen after the fact, which is the harder comparison for the tensor.

Run:  python scripts/eigen_vs_tensor.py --bars reports/ibkr_connector_2026-11-04/bars \
          --out reports/eigen_vs_tensor_2026-11-04
"""
from __future__ import annotations

import argparse
import sys
import warnings
from concurrent.futures import ProcessPoolExecutor
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "BSM_Spectral"))
import bsm_eigentensor as et
import bsm_event as be

warnings.filterwarnings("ignore")
SYMBOLS = ("SPY", "GLD", "TLT", "STW.AX", "BHP.AX")
SYDNEY = (".AX",)                  # IBKR stamps a Sydney session on the prior UTC evening
KINDS = ("matrix", "horizon", "coskew", "cokurt")
ALPHA = 0.01
PORTFOLIOS = 10                    # random long-only books beside equal weight
SEED = 20261104
SOLVER = dict(n_starts=8, max_iter=1000)


def load_returns(bars: Path, weekly: bool = False) -> pd.DataFrame:
    closes = {}
    for symbol in SYMBOLS:
        df = pd.read_parquet(bars / f"{symbol}.parquet")
        days = pd.to_datetime(df["time"], utc=True)
        if symbol.endswith(SYDNEY):
            days = days + pd.Timedelta(days=1)
        closes[symbol] = pd.Series(df["close"].to_numpy(dtype=float), index=days.dt.normalize())
    prices = pd.DataFrame(closes).dropna()
    if weekly:
        prices = prices.iloc[::5]
    return np.log(prices).diff().dropna()


def _one_null(args: tuple[int, int, int, float]) -> tuple[float, float]:
    seed, T, n, kappa = args
    x = et.elliptical_sample(np.random.default_rng(seed), T, n, kappa)
    s, k, _ = et.structure_stats(x, **SOLVER)
    return s, k


def structure_test(Z: np.ndarray, sims: int, pool: ProcessPoolExecutor, label: str) -> dict:
    x = et.whiten(Z)
    skew, kurt, kappa = et.structure_stats(x, **SOLVER)
    residual = float(np.max(et.z_eigenpairs(et.coskewness(x), **SOLVER)[2][:1]))
    T, n = Z.shape
    jobs = [(SEED + i, T, n, kappa) for i in range(sims)]
    null = np.array(list(pool.map(_one_null, jobs, chunksize=8)))
    return dict(sample=label, bars=T, kappa=kappa, nu=4.0 + 6.0 / kappa if kappa > 0 else float("inf"),
                coskew=skew, coskew_null=float(null[:, 0].mean()), coskew_p=et.mc_pvalue(skew, null[:, 0]),
                cokurt=kurt, cokurt_null=float(null[:, 1].mean()), cokurt_p=et.mc_pvalue(kurt, null[:, 1]),
                leading_residual=residual)


def _one_shuffle(args: tuple[str, str, int, np.ndarray, dict]) -> dict:
    how, kind, seed, X, kw = args
    rng = np.random.default_rng(seed)
    if how == "dates":
        X = X[rng.permutation(len(X))]
    else:
        X = np.column_stack([rng.permutation(X[:, j]) for j in range(X.shape[1])])
    profile = et.eigen_profile(kind, X, **kw)
    return {lag: row["fidelity"] for lag, row in profile.items()} | {"persist": et.eigenpersistence(profile)}


def eigen_tests(Z: np.ndarray, R: np.ndarray, shuffles: int, pool: ProcessPoolExecutor,
                window: int, step: int, lags: tuple[int, ...]) -> tuple[pd.DataFrame, pd.DataFrame]:
    rows, curve = [], []
    for kind in KINDS:
        # the horizon stack needs returns as they came, since devolatilizing removes what it measures
        X = R if kind == "horizon" else Z
        kw = dict(window=window, step=step, lags=lags) | ({} if kind in ("matrix", "horizon") else SOLVER)
        profile = et.eigen_profile(kind, X, **kw)
        persist = et.eigenpersistence(profile)
        nulls = {}
        for how in ("dates", "columns"):
            jobs = [(how, kind, SEED + 1000 * i, X, kw) for i in range(shuffles)]
            nulls[how] = list(pool.map(_one_shuffle, jobs, chunksize=2))
        by_date = np.array([r["persist"] for r in nulls["dates"]])
        by_col = np.array([r["persist"] for r in nulls["columns"]])
        rows.append(dict(kind=kind, eigenpersistence=persist, stable_population=float(np.nanmean(by_date)),
                         no_structure=float(np.nanmean(by_col)),
                         # low: it moved more than sampling noise alone would make it move
                         p_moved=float((1 + np.sum(by_date <= persist)) / (len(by_date) + 1)),
                         # high: it held more than directions with no structure at all
                         p_structure=float((1 + np.sum(by_col >= persist)) / (len(by_col) + 1))))
        for lag, row in profile.items():
            curve.append(dict(kind=kind, lag_windows=lag, bars_apart=lag * window, pairs=row["pairs"],
                              eigenfidelity=row["fidelity"], excess=row["excess"], capture=row["capture"],
                              stable_population=float(np.nanmean([r.get(lag, np.nan) for r in nulls["dates"]])),
                              no_structure=float(np.nanmean([r.get(lag, np.nan) for r in nulls["columns"]]))))
    return pd.DataFrame(rows), pd.DataFrame(curve)


def align_closes(daily: pd.DataFrame) -> pd.DataFrame:
    """Pair each US session with the next Sydney one, which is the first to react to it.

    A Sydney session closes before that date's US session opens, so same-date returns understate
    how the two markets move together. Not tradable as a forecast, only a way to measure that."""
    sydney = [c for c in daily.columns if c.endswith(SYDNEY)]
    others = [c for c in daily.columns if c not in sydney]
    aligned = pd.concat([daily[others].iloc[:-1].reset_index(drop=True),
                         daily[sydney].iloc[1:].reset_index(drop=True)], axis=1)
    return aligned[list(daily.columns)]


def asynchrony_checks(daily: pd.DataFrame, sims: int, pool: ProcessPoolExecutor) -> tuple[pd.DataFrame, pd.DataFrame]:
    """Is the horizon tensor's structure the markets, or only their closing at different times?"""
    sydney = [c for c in daily.columns if c.endswith(SYDNEY)]
    others = [c for c in daily.columns if c not in sydney]
    aligned = align_closes(daily)
    rows = []
    for label, frame in (("all five, same date", daily), ("all five, Sydney a day later", aligned),
                         ("US listings only", daily[others]), ("Sydney listings only", daily[sydney])):
        R = frame.to_numpy()
        share = et.horizon_share(R)
        rng = np.random.default_rng(SEED)
        null = [et.horizon_share(R[rng.permutation(len(R))]) for _ in range(sims)]
        row = dict(sample=label, assets=R.shape[1], horizon_share=share, shuffled=float(np.mean(null)),
                   p=et.mc_pvalue(share, null))
        if len(frame.columns) == len(daily.columns):
            stack = et.horizon_stack(R)
            cross = stack[np.ix_([frame.columns.get_loc(c) for c in others],
                                 [frame.columns.get_loc(c) for c in sydney])]
            row |= {f"us_sydney_corr_{h}d": float(cross[:, :, i].mean()) for i, h in enumerate((1, 5, 21))}
        rows.append(row)
    structure = pd.DataFrame([structure_test(et.devolatilize(aligned.to_numpy())[0], sims, pool, "daily, Sydney a day later")])
    return pd.DataFrame(rows), structure


def var_test(R: np.ndarray, window: int, refit: int) -> tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame]:
    n = R.shape[1]
    rng = np.random.default_rng(SEED)
    weights = np.vstack([np.full(n, 1.0 / n), rng.dirichlet(np.ones(n), PORTFOLIOS)])
    names, var, losses = et.var_backtest(R, weights, window=window, refit=refit, alpha=ALPHA, **SOLVER)
    daily = {}
    rows = []
    for j, name in enumerate(names):
        pinball = be.pinball(losses, var[:, :, j], 1.0 - ALPHA)
        daily[name] = pinball.mean(axis=1)      # one series per method, averaged over the books each day
        hits = losses > var[:, :, j]
        rows.append(dict(method=name, pinball=float(pinball.mean()) * 1e4, breach_rate=float(hits.mean()),
                         breach_rate_equal_weight=float(hits[:, 0].mean()),
                         kupiec_p_equal_weight=be.kupiec(hits[:, 0], ALPHA)[1],
                         mean_var=float(var[:, :, j].mean())))
    scores = pd.DataFrame(rows)
    matrix = scores[~scores.method.str.startswith("tensor")]
    best = matrix.sort_values("pinball").method.iloc[0]
    tensors = [m for m in names if m.startswith("tensor")]
    tests = [et.diebold_mariano(daily[m], daily[best]) for m in tensors]
    adjusted = et.holm([p for _, p in tests])
    compare = pd.DataFrame([dict(tensor=m, best_matrix=best, days=len(losses), dm_stat=t[0], p=t[1], holm_p=h,
                                 pinball_change_pct=100 * (daily[m].mean() / daily[best].mean() - 1))
                            for m, t, h in zip(tensors, tests, adjusted, strict=True)])
    tails = []
    for base in ("sample", "spectral", "mp"):
        stat, p = et.diebold_mariano(daily[f"elliptical_{base}"], daily[f"gauss_{base}"])
        tails.append(dict(matrix=base, dm_stat=stat, p=p, pinball_change_pct=100 * (
            daily[f"elliptical_{base}"].mean() / daily[f"gauss_{base}"].mean() - 1)))
    return scores, compare, pd.DataFrame(tails)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--bars", type=Path, required=True)
    ap.add_argument("--out", type=Path, required=True)
    ap.add_argument("--sims", type=int, default=500)
    ap.add_argument("--shuffles", type=int, default=100)
    ap.add_argument("--window", type=int, default=252)
    ap.add_argument("--step", type=int, default=63)
    ap.add_argument("--var-window", type=int, default=504)
    ap.add_argument("--refit", type=int, default=21)
    ap.add_argument("--workers", type=int, default=6)
    ap.add_argument("--checks-only", action="store_true", help="Skip the structure and eigen runs.")
    args = ap.parse_args()
    args.out.mkdir(parents=True, exist_ok=True)
    pd.set_option("display.width", 220, "display.max_columns", 30)

    daily, weekly = load_returns(args.bars), load_returns(args.bars, weekly=True)
    print(f"daily {daily.shape} {daily.index[0].date()} to {daily.index[-1].date()}; weekly {weekly.shape}", flush=True)
    R = daily.to_numpy()
    Z = et.devolatilize(R)[0]
    Zw = et.devolatilize(weekly.to_numpy(), lam=0.9, warm=26)[0]
    with ProcessPoolExecutor(max_workers=args.workers) as pool:
        horizon_checks, aligned = asynchrony_checks(daily, args.sims, pool)
        horizon_checks.to_csv(args.out / "horizon_checks.csv", index=False)
        aligned.to_csv(args.out / "structure_aligned.csv", index=False)
        print("=== HORIZON CHECKS\n" + horizon_checks.round(4).to_string(), flush=True)
        print("=== STRUCTURE, CLOSES ALIGNED\n" + aligned.round(4).to_string(), flush=True)
        if not args.checks_only:
            _main_runs(args, R, Z, Zw, pool)
    scores, compare, tails = var_test(R, args.var_window, args.refit)
    scores.to_csv(args.out / "var_scores.csv", index=False)
    compare.to_csv(args.out / "var_compare.csv", index=False)
    tails.to_csv(args.out / "var_tails.csv", index=False)
    print("=== VAR\n" + scores.round(4).to_string(), flush=True)
    print("=== TENSOR VS BEST MATRIX\n" + compare.round(4).to_string(), flush=True)
    print("=== ONE TAIL NUMBER VS GAUSSIAN\n" + tails.round(4).to_string(), flush=True)


def _main_runs(args: argparse.Namespace, R: np.ndarray, Z: np.ndarray, Zw: np.ndarray,
               pool: ProcessPoolExecutor) -> None:
    structure = pd.DataFrame([structure_test(Z, args.sims, pool, "daily"),
                              structure_test(Zw, args.sims, pool, "weekly"),
                              structure_test(et.whiten(R), args.sims, pool, "daily, no devol")])
    structure.to_csv(args.out / "structure.csv", index=False)
    print("=== STRUCTURE\n" + structure.round(4).to_string(), flush=True)
    lags = tuple(range(1, len(R) // args.window))
    summary, curve = eigen_tests(Z, R[60:], args.shuffles, pool, args.window, args.step, lags)
    summary.to_csv(args.out / "eigenpersistence.csv", index=False)
    curve.to_csv(args.out / "eigenfidelity.csv", index=False)
    print("=== EIGENPERSISTENCE\n" + summary.round(4).to_string(), flush=True)
    print("=== EIGENFIDELITY\n" + curve.round(4).to_string(), flush=True)


if __name__ == "__main__":
    main()
