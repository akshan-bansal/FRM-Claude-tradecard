"""How long a sharp move in one listing of a company takes to reach its listing on another continent.

Research only: nothing here reaches the Router. ``scripts/listing_delay_study.py`` is the way in.

A delay across time zones is mostly the other market being shut, so each event's delay is split on the
exchange-hopping clock:

    total = closure wait (event to the target's next bar) + reaction (from there to 50% capture)

An event is an hourly bar whose open-to-close move is large against the BSM_Spectral GJR-GARCH forecast
made before that bar. Open-to-close leaves out the overnight gap, because a gap is usually the *other*
listing's move arriving, and counting it would call the echo a new event. Same-bar moves count as zero delay
and say nothing about which listing led.

Prices are compared in each listing's own currency: an FX move while the target is shut reads as capture.
"""
from __future__ import annotations

import csv
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd
import statsmodels.api as sm
from scipy import stats

from ..brokers.models import Candle
from ..venues import venue_for

MEETINGS_PATH = Path(__file__).resolve().parents[3] / "config" / "triad_meetings.csv"
TRIAD = ("FED", "ECB", "BOJ")
MIN_EVENTS = 30
_MIN_WALK = 20


@dataclass(frozen=True)
class ListedPair:
    name: str
    home: str
    us: str


PAIRS = (ListedPair("toyota", "7203.T", "TM"), ListedPair("honda", "7267.T", "HMC"),
         ListedPair("sony", "6758.T", "SONY"), ListedPair("mufg", "8306.T", "MUFG"),
         ListedPair("bhp", "BHP.AX", "BHP"))


def bars_frame(candles: list[Candle]) -> pd.DataFrame:
    frame = pd.DataFrame({"open": [c.open for c in candles], "close": [c.close for c in candles],
                          "end": pd.to_datetime([c.end for c in candles], utc=True)},
                         index=pd.to_datetime([c.start for c in candles], utc=True))
    return frame.sort_index()


def load_meetings(path: Path = MEETINGS_PATH) -> dict[str, list[date]]:
    out: dict[str, list[date]] = {b: [] for b in TRIAD}
    with path.open(encoding="utf-8", newline="") as f:
        for row in csv.DictReader(f):
            out[row["bank"]].append(date.fromisoformat(row["decision_date"]))
    return {b: sorted(d) for b, d in out.items()}


def in_meeting_window(ts: datetime, days: list[date]) -> bool:
    """The decision day and the next, since a decision can land after the other market has shut."""
    d = ts.astimezone(UTC).date()
    return d in days or d - timedelta(days=1) in days


def gjr_sigma(returns: pd.Series, *, engine: Any = None, block: int = 252, min_train: int = 504) -> pd.Series:
    """Each bar's volatility forecast, from parameters fitted on earlier bars only. NaN before ``min_train``."""
    if engine is None:
        from ..risk.spectral_var import load_engine
        engine = load_engine()
    r = returns.to_numpy(dtype=float)
    sigma = np.full(len(r), np.nan)
    for start in range(min_train, len(r), block):
        p = engine.fit_gjr(r[:start])[0]
        stop = min(start + block, len(r))
        # The filter's default seed is the variance of all it is given, which would include this block.
        s2 = engine.gjr_filter(r[:stop] - p.mu, p, float(np.var(r[:start] - p.mu)))
        sigma[start:stop] = np.sqrt(s2[start:stop])
    return pd.Series(sigma, index=returns.index)


SigmaFn = Callable[[pd.Series], pd.Series]


def find_events(source: pd.DataFrame, *, sigma_fn: SigmaFn = gjr_sigma, threshold: float = 3.0,
                cooldown_h: float = 24.0) -> pd.DataFrame:
    """Bars past ``threshold`` forecast sigmas, keeping the first of a cluster."""
    move = np.log(source["close"] / source["open"])
    sigma = sigma_fn(move)
    z = (move / sigma).abs()
    keep: list[Any] = []
    last_end: pd.Timestamp | None = None
    for t in z.index[z >= threshold]:
        if last_end is None or source.at[t, "end"] - last_end >= pd.Timedelta(hours=cooldown_h):
            keep.append(t)
            last_end = source.at[t, "end"]
    return pd.DataFrame({"move": move[keep], "abs_z": z[keep], "sigma": sigma[keep], "end": source.loc[keep, "end"]})


def measure_event(start: pd.Timestamp, end: pd.Timestamp, move: float, target: pd.DataFrame, *,
                  capture: float = 0.5, horizon_bars: int = 14) -> dict[str, Any] | None:
    """The target's path after an event. None if it has no earlier close or no later bar to compare."""
    prior = target[target.index < start]
    after = target[target["end"] > start].head(horizon_bars)
    if prior.empty or after.empty:
        return None
    base, sign = float(prior["close"].iloc[-1]), np.sign(move)
    wait = max(0.0, (after.index[0] - end).total_seconds() / 3600.0)
    path = np.log(after["close"] / base) * sign / abs(move)
    hit = path >= capture
    gap = float(np.log(after["open"].iloc[0] / base) * sign / abs(move))
    if hit.any():
        total = max(0.0, (after.loc[hit.idxmax(), "end"] - end).total_seconds() / 3600.0)
        censored = False
    else:
        total = max(0.0, (after["end"].iloc[-1] - end).total_seconds() / 3600.0)   # a lower bound
        censored = True
    return {"closure_wait_h": wait, "reaction_h": max(0.0, total - wait), "total_h": total,
            "gap_capture": gap, "censored": censored}


def pair_events(pair: ListedPair, home: pd.DataFrame, us: pd.DataFrame, meetings: Mapping[str, list[date]],
                *, sigma_fn: SigmaFn = gjr_sigma, threshold: float = 3.0) -> pd.DataFrame:
    """Events both ways for one company, one row each, in time order."""
    rows: list[dict[str, Any]] = []
    legs = ((pair.home, pair.us, home, us, "asia_to_us"), (pair.us, pair.home, us, home, "us_to_asia"))
    for source_symbol, target_symbol, source, target, direction in legs:
        target_venue = venue_for(target_symbol)[0]
        for t, e in find_events(source, sigma_fn=sigma_fn, threshold=threshold).iterrows():
            measured = measure_event(t, e["end"], float(e["move"]), target)
            if measured is None:
                continue
            when = e["end"].to_pydatetime()
            rows.append({"pair": pair.name, "direction": direction, "source": source_symbol, "time": e["end"],
                         "move": e["move"], "abs_z": e["abs_z"], "sigma": e["sigma"],
                         "overlap": target_venue.tradeable(when), **measured,
                         **{b.lower(): in_meeting_window(when, meetings.get(b, [])) for b in TRIAD}})
    return pd.DataFrame(rows).sort_values("time").reset_index(drop=True) if rows else pd.DataFrame()


def covered_banks(events: pd.DataFrame, meetings: Mapping[str, list[date]]) -> list[str]:
    """Banks whose dates span the events. A date missing from the file is not the absence of a meeting."""
    first, last = events["time"].min().date(), events["time"].max().date()
    return [b for b in TRIAD if meetings.get(b) and meetings[b][0] <= first and meetings[b][-1] >= last]


def fit_delay_model(events: pd.DataFrame, meetings: Mapping[str, list[date]]) -> dict[str, Any]:
    """OLS on log reaction time with HAC errors, then a walk-forward test against the running mean."""
    if len(events) < MIN_EVENTS:
        return {"status": "insufficient", "events": len(events), "needed": MIN_EVENTS}
    banks = covered_banks(events, meetings)
    done = events[~events["censored"]].copy()
    done["y"] = np.log1p(done["reaction_h"])
    done["prior_y"] = done.groupby(["pair", "direction"])["y"].shift(1)
    done = done.dropna(subset=["prior_y"]).sort_values("time")
    if len(done) < MIN_EVENTS:
        return {"status": "insufficient", "events": len(done), "needed": MIN_EVENTS,
                "censored_share": float(events["censored"].mean())}
    columns = ["prior_y", "abs_z", "closure_wait_h", "overlap", *[b.lower() for b in banks]]
    # A column with no variation (a bank with no meeting among the events) cannot be estimated.
    columns = [c for c in columns if done[c].astype(float).nunique() > 1]
    X = sm.add_constant(done[columns].astype(float), has_constant="add")
    y = done["y"].to_numpy()
    fit = sm.OLS(y, X).fit(cov_type="HAC", cov_kwds={"maxlags": 5})
    model_err, mean_err = [], []
    for i in range(_MIN_WALK, len(done)):
        past = sm.OLS(y[:i], X.iloc[:i]).fit()
        model_err.append(abs(np.expm1(float(past.predict(X.iloc[[i]]).iloc[0])) - np.expm1(y[i])))
        mean_err.append(abs(np.expm1(y[:i].mean()) - np.expm1(y[i])))
    return {"status": "ok", "events": len(events), "fitted": len(done),
            "censored_share": float(events["censored"].mean()),
            "banks_used": banks, "banks_dropped": [b for b in TRIAD if b not in banks],
            "coefficients": {str(k): float(v) for k, v in fit.params.items()},
            "p_values": {str(k): float(v) for k, v in fit.pvalues.items()},
            "r_squared": float(fit.rsquared),
            "oos_mae_h": float(np.mean(model_err)), "oos_mae_mean_baseline_h": float(np.mean(mean_err))}


def delay_correlations(events: pd.DataFrame, meetings: Mapping[str, list[date]]) -> dict[str, Any]:
    """Spearman of reaction time on event size and meeting windows. A censored delay ranks at its lower bound."""
    if len(events) < MIN_EVENTS:
        return {"status": "insufficient", "events": len(events), "needed": MIN_EVENTS}
    out: dict[str, Any] = {"status": "ok"}
    for name in ["abs_z", *[b.lower() for b in covered_banks(events, meetings)]]:
        x = events[name].astype(float)
        if x.nunique() > 1 and events["reaction_h"].nunique() > 1:
            r = stats.spearmanr(x, events["reaction_h"])
            out[name] = {"rho": float(r.statistic), "p": float(r.pvalue), "hits": int(x.astype(bool).sum())}
    return out


def meeting_vol_association(bars: pd.DataFrame, meetings: Mapping[str, list[date]], *,
                            sigma_fn: SigmaFn = gjr_sigma, threshold: float = 3.0) -> dict[str, Any]:
    """Are high-volatility days likelier in a bank's meeting window? Fisher's exact test over trading days."""
    move = np.log(bars["close"] / bars["open"])
    z = (move / sigma_fn(move)).abs().dropna()
    if z.empty:
        return {}
    day = z.groupby(z.index.date).max() >= threshold
    first, last = day.index[0], day.index[-1]
    out: dict[str, Any] = {}
    for b in TRIAD:
        dates = meetings.get(b, [])
        if not dates or dates[0] > first or dates[-1] < last:
            continue
        window = np.array([d in dates or d - timedelta(days=1) in dates for d in day.index])
        table = [[int((day.values & window).sum()), int((~day.values & window).sum())],
                 [int((day.values & ~window).sum()), int((~day.values & ~window).sum())]]
        odds, p = stats.fisher_exact(table)
        out[b] = {"table": table, "odds_ratio": float(odds), "p": float(p)}
    return out
