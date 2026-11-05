"""Shadow VaR: what each representation would forecast for a runner's book, recorded before the outcome.

Acts on nothing. Nothing on the order path imports this module, and tests check that by import and by
source. It is the shadow stage for the eigen-against-tensor test (``reports/eigen_vs_tensor_2026-11-04``):
that backtest found no tensor method better than the best matrix one, so the methods earn a place on
the trade path only by a forward record, not by a backtest.

Once a day, by hand, for one runner's ledger stream:

1. the book is folded from that stream's ``EXECUTION_REPORTED`` rows dated on or before ``as_of``, in the
   account currency the executions were recorded in;
2. closes come from the runners' own candle cache, never fetched, in the numeraire copy when there is one;
3. every method in ``bsm_eigentensor.fit_methods`` forecasts the book's one-day 99% loss for the bar after
   ``as_of``, beside the sum of the live gate's per-name tail figure (undiversified, and only where a name
   has the 500 returns that gate needs);
4. one ``SHADOW_VAR`` row goes to the shared ``var-shadow`` stream, and only when there is a forecast: ledger
   rows are permanent, so a day the history cannot carry is reported, not recorded. A second run for the same
   stream and ``as_of`` writes nothing.

A row counts as a forecast only if it was recorded on a calendar day (UTC) before its outcome bar. A stale
cache can make ``as_of`` days old, so the outcome may already be known when the row is written: scoring
keeps those rows apart instead of counting them.

The methods' source files are hashed into every row, so a change to any of them splits the record.
"""
from __future__ import annotations

import hashlib
import importlib.util
import sys
from collections.abc import Mapping
from datetime import UTC, date, datetime, time
from pathlib import Path
from types import ModuleType
from typing import Any
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd

from ..audit.ledger import Ledger
from ..logging_setup import get_logger
from ..risk.spectral_var import ENGINE_DIR, estimate_tail, load_engine

log = get_logger(__name__)

STREAM = "var-shadow"
EVENT = "SHADOW_VAR"
SHADOW = "shadow"
ALPHA = 0.01
MAX_WINDOW, MIN_WINDOW = 504, 252        # longest history up to the backtest's window, never under a year
WARM = 60                                # bars the EWMA volatility needs before its first forecast
LAM = 0.94
GATE_MIN_RETURNS = 500                   # what risk/spectral_var.py's gate needs for a name
_TENSOR_MODULE = "bsm_eigentensor"


def load_methods(engine_dir: Path = ENGINE_DIR) -> ModuleType:
    """Import ``bsm_eigentensor.py`` beside the engine it builds on, as ``spectral_var`` loads the engine."""
    load_engine(engine_dir)
    if _TENSOR_MODULE in sys.modules:
        return sys.modules[_TENSOR_MODULE]
    spec = importlib.util.spec_from_file_location(_TENSOR_MODULE, engine_dir / f"{_TENSOR_MODULE}.py")
    if spec is None or spec.loader is None:
        raise ImportError(f"{_TENSOR_MODULE} not found in {engine_dir}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[_TENSOR_MODULE] = module
    try:
        spec.loader.exec_module(module)
    except Exception:
        sys.modules.pop(_TENSOR_MODULE, None)
        raise
    return module


def method_version(engine_dir: Path = ENGINE_DIR) -> str:
    h = hashlib.sha256()
    for path in (engine_dir / "bsm_event.py", engine_dir / f"{_TENSOR_MODULE}.py", Path(__file__)):
        try:
            h.update(path.read_bytes())
        except OSError:
            h.update(b"missing:" + path.name.encode())
    return h.hexdigest()[:16]


# ---------------------------------------------------------------------------- book and bars


def _execution_day(row: Mapping[str, Any]) -> date:
    stamp = pd.Timestamp((row.get("payload") or {}).get("time") or row.get("ts"))
    return (stamp.tz_convert("UTC") if stamp.tzinfo else stamp).date()


def book_from_ledger(rows: list[Mapping[str, Any]], as_of: date) -> tuple[dict[str, float], set[str]]:
    """Net quantity per symbol from recorded executions dated on or before ``as_of``, and their currencies."""
    book: dict[str, float] = {}
    currencies: set[str] = set()
    for row in rows:
        if row.get("event") != "EXECUTION_REPORTED" or _execution_day(row) > as_of:
            continue
        p = row.get("payload") or {}
        sign = 1.0 if str(p.get("side", "")).lower() in ("buy", "b") else -1.0
        book[p["symbol"]] = book.get(p["symbol"], 0.0) + sign * float(p["quantity"])
        if p.get("currency"):
            currencies.add(str(p["currency"]).upper())
    return {s: q for s, q in book.items() if abs(q) > 1e-12}, currencies


def cached_closes(cache_dir: Path, symbol: str, numeraire: str = "CAD") -> pd.Series:
    """Daily closes by UTC date from the candle cache's shards, the numeraire copy first."""
    safe = symbol.replace(".", "_").replace("/", "_")
    for folder in (cache_dir / f"numeraire_{numeraire}", cache_dir):
        shards = sorted(folder.glob(f"{safe}_1d_*.parquet")) if folder.is_dir() else []
        if shards:
            df = pd.concat([pd.read_parquet(p) for p in shards]).drop_duplicates(subset="time")
            days = pd.to_datetime(df["time"], utc=True).dt.normalize().dt.date
            series = pd.Series(df["close"].astype(float).to_numpy(), index=days)
            return series[~series.index.duplicated(keep="last")].sort_index()
    return pd.Series(dtype=float)


def _clock_offset(symbol: str, on: date) -> float | None:
    """The venue's UTC offset in hours, so books on one clock can be told from books on several."""
    try:
        from ..venues import venue_for
        tz = ZoneInfo(venue_for(symbol)[0].tz)
    except Exception:
        return None
    offset = datetime.combine(on, time(12), tzinfo=tz).utcoffset()
    return offset.total_seconds() / 3600 if offset is not None else None


# ---------------------------------------------------------------------------- forecast


def forecast(book: Mapping[str, float], closes: Mapping[str, pd.Series], as_of: date, *,
             alpha: float = ALPHA, **solver: Any) -> dict[str, Any]:
    """Every method's next-bar loss quantile for ``book``, in the currency the closes are in.

    Uses only closes dated on or before ``as_of``. Returns the payload's figures, or a ``reason`` and no
    figures when the history cannot carry a forecast."""
    et = load_methods()
    names = sorted(book)
    out: dict[str, Any] = {"as_of": as_of.isoformat(), "book": {s: book[s] for s in names}, "alpha": alpha,
                           "method_version": method_version(), "methods": {}, "reason": None}
    missing = [s for s in names if closes.get(s) is None or closes[s].empty]
    if not names:
        out["reason"] = "empty book"
        return out
    if missing:
        out["reason"] = f"no cached closes for {', '.join(missing)}"
        return out
    frame = pd.DataFrame({s: closes[s][closes[s].index <= as_of] for s in names}).dropna()
    if frame.empty or frame.index[-1] != as_of:
        out["reason"] = f"not every name has a close on {as_of.isoformat()}"
        return out
    R = np.log(frame).diff().dropna().to_numpy()
    window = min(MAX_WINDOW, len(R) - WARM)
    out["bars"], out["window"] = len(R), int(window)
    if window < MIN_WINDOW:
        out["reason"] = f"{len(R)} shared returns, need {MIN_WINDOW + WARM}"
        return out
    offsets = {_clock_offset(s, as_of) for s in names}
    out["same_date_across_clocks"] = len(offsets - {None}) > 1
    Z, sigma = et.devolatilize(R, lam=LAM, warm=WARM)
    # next bar's volatility: the EWMA step on the last return, which ``devolatilize`` leaves out
    sigma_next = np.sqrt(LAM * sigma[-1] ** 2 + (1.0 - LAM) * R[-1] ** 2)
    last = frame.iloc[-1].to_numpy()
    values = np.array([book[s] for s in names]) * last
    out["values"] = {s: float(v) for s, v in zip(names, values, strict=True)}
    out["gross"] = float(np.abs(values).sum())
    fitted, root = et.fit_methods(Z[-window:], **solver)
    exposure = values * sigma_next
    out["methods"] = {name: float(et.portfolio_var(m, root, exposure, alpha)) for name, m in fitted.items()}
    gate: float | None = 0.0
    for s, v in zip(names, values, strict=True):
        series = closes[s][closes[s].index <= as_of]
        if len(series) - 1 < GATE_MIN_RETURNS:
            gate = None
            break
        gate += abs(float(v)) * estimate_tail(series.to_numpy(), level=1 - alpha, horizon_days=1).loss_fraction
    out["gate_per_name_sum"] = gate
    return out


# ---------------------------------------------------------------------------- record and score


def _ledger(ledger_dir: Path) -> Ledger:
    return Ledger(ledger_dir, stream=STREAM, mode=SHADOW, shared=True)


def recorded(ledger_dir: Path) -> list[dict[str, Any]]:
    return [dict(r["payload"], recorded_at=r.get("ts")) for r in _ledger(ledger_dir).rows() if r.get("event") == EVENT]


def latest_complete_day(closes: Mapping[str, pd.Series], today: date) -> date | None:
    """The last day every name has a close, strictly before ``today`` so a bar still forming is never used."""
    days = None
    for series in closes.values():
        have = {d for d in series.index if d < today}
        days = have if days is None else days & have
    return max(days) if days else None


def record(stream: str, *, state_dir: Path, cache_dir: Path, today: date | None = None,
           ledger_dir: Path | None = None, **solver: Any) -> dict[str, Any] | None:
    """Write one ``SHADOW_VAR`` row for ``stream`` at its latest complete day, unless that day is recorded.

    Reads the runner's stream from ``state_dir/ledger`` and writes to ``ledger_dir`` (the same folder by
    default). Returns the payload with ``written`` saying whether it went to the ledger, or None when the row
    already exists. A payload with a ``reason`` is never written."""
    today = today or datetime.now(UTC).date()
    source = Ledger(state_dir / "ledger", stream=stream, mode=SHADOW)
    rows = source.rows()
    book_now, _ = book_from_ledger(rows, today)
    closes = {s: cached_closes(cache_dir, s) for s in book_now}
    as_of = latest_complete_day(closes, today) if closes else None
    if as_of is None:
        payload: dict[str, Any] = {"stream": stream, "as_of": None, "reason": "no book or no shared closes",
                                   "methods": {}, "method_version": method_version()}
    else:
        book, currencies = book_from_ledger(rows, as_of)
        # a name held at ``as_of`` but sold since still needs its closes
        closes.update({s: cached_closes(cache_dir, s) for s in book if s not in closes})
        payload = {"stream": stream, **forecast(book, closes, as_of, **solver), "currencies": sorted(currencies)}
    target = ledger_dir or (state_dir / "ledger")
    done = {(r.get("stream"), r.get("as_of"), r.get("method_version")) for r in recorded(target)}
    if (payload["stream"], payload["as_of"], payload["method_version"]) in done:
        return None
    if payload.get("reason") or not payload.get("methods"):
        return {**payload, "written": False}
    _ledger(target).append(EVENT, payload)
    return {**payload, "written": True}


def score(ledger_dir: Path, cache_dir: Path) -> pd.DataFrame:
    """One line per recorded forecast and method: the outcome bar's loss, the breach, the pinball loss.

    ``forward`` is True only when the row was written on a UTC day before its outcome bar's day."""
    be = load_engine()
    lines = []
    for row in recorded(ledger_dir):
        if not row.get("methods") or not row.get("as_of"):
            continue
        as_of = date.fromisoformat(row["as_of"])
        closes = {s: cached_closes(cache_dir, s) for s in row["book"]}
        frame = pd.DataFrame(closes).dropna()
        later = frame.index[frame.index > as_of]
        if as_of not in frame.index or not len(later):
            continue
        outcome = later[0]
        loss = -float(sum(q * (frame.at[outcome, s] - frame.at[as_of, s]) for s, q in row["book"].items()))
        written = pd.Timestamp(row["recorded_at"]).tz_convert("UTC").date()
        figures = dict(row["methods"])
        if row.get("gate_per_name_sum") is not None:
            figures["gate_per_name_sum"] = row["gate_per_name_sum"]
        for method, var in figures.items():
            lines.append({"stream": row["stream"], "as_of": as_of, "outcome": outcome, "method": method, "var": var,
                          "loss": loss, "breach": loss > var, "forward": written < outcome,
                          "pinball": float(be.pinball(np.array([loss]), np.array([var]), 1 - row["alpha"])[0]),
                          "method_version": row["method_version"]})
    return pd.DataFrame(lines)


def summary(scores: pd.DataFrame) -> pd.DataFrame:
    """Per method, forward rows only: how many, the breach rate against the nominal, mean pinball loss."""
    if scores.empty:
        return scores
    fwd = scores[scores.forward]
    if fwd.empty:
        return pd.DataFrame(columns=["method", "forecasts", "breaches", "breach_rate", "pinball"])
    g = fwd.groupby("method")
    return pd.DataFrame({"forecasts": g.size(), "breaches": g.breach.sum(), "breach_rate": g.breach.mean(),
                         "pinball": g.pinball.mean()}).reset_index().sort_values("pinball")

