"""Point-in-time feature store for the risk lenses: one row per symbol per day, built at a cutoff.

The lens redefinition (NEXT_SESSION.md, 2026-10-12) needs features whose known-at time is provable.
The sources cannot give that on their own: the graph journal is washed (observed edges live 30 days,
stress edges 14), and the overlay journal is only written while a trading session polls. So every day
each symbol gets a snapshot here, built only from records known strictly before the cutoff, and the
snapshot is appended to ``state/lens_features.jsonl``, which nothing washes.

Cutoffs sit before the price the 5-day drawdown target is measured from: 00:00 UTC for crypto (Kraken's
daily candle close), 20:00 UTC for equities and futures (16:00 New York in summer, an hour before the
close in winter, so never after it).

Clocks are never mixed. Each feature family reads one clock and says so in its name:

* ``live_*``  - the live collector's arrival time (``state/news.jsonl``, ``arrived_utc``).
* ``gkg_*``   - GDELT's crawl clock (``state/gdelt_gkg/*.jsonl``, file stamp; backfill and follower).
* ``ov_*``    - the overlay snapshot's ``as_of`` (``state/intel_overlay.jsonl``), per asset class.
* ``graph_*`` - edge ``as_of`` in the graph journal (WorldMonitor events by domain).

Every row carries coverage per clock, so a lens can tell "nothing happened" from "nothing was
recorded". A row is ``mode: live`` when built within ``LIVE_GRACE`` of its cutoff (truly point in time)
and ``mode: rebuild`` otherwise (reconstructed from the journals). A rebuild never replaces a live row.

Contract, same as ``analysis.lens_audit``: this is a ruler. Nothing in execution, risk, brokers,
strategies, monitor or the daemon imports it, and it imports none of them (a test enforces both).
"""
from __future__ import annotations

import json
from bisect import bisect_left
from collections import defaultdict
from collections.abc import Iterable, Iterator, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any

from trading_live_claude.intel.routing import classify_symbol

VERSION = 1
LIVE_GRACE = timedelta(hours=2)
DAY = timedelta(days=1)
NOVELTY_LOOKBACK = timedelta(days=7)
ABNORMAL_BASELINE_DAYS = 28
MIN_BASELINE_COVERAGE = 0.8  # share of baseline days that must be covered before a ratio is reported
OVERLAY_MAX_AGE = timedelta(hours=6)
CUTOFF_HOUR_UTC = {"crypto": 0}  # every other class: DEFAULT_CUTOFF_HOUR
DEFAULT_CUTOFF_HOUR = 20


# --------------------------------------------------------------------------- universe


@dataclass(frozen=True)
class Instrument:
    symbol: str
    venue: str
    asset_class: str


def universe(seed_path: Path) -> list[Instrument]:
    """The basket universe. The venue fixes the overlay class where the symbol alone cannot: IB roots
    (GC, SI) are futures, Kraken pairs are crypto, QT names go through ``classify_symbol``."""
    seed: dict[str, Any] = json.loads(seed_path.read_text(encoding="utf-8"))
    out: list[Instrument] = []
    for venue in ("kraken", "qt", "ib"):
        for sym in seed.get(venue) or ():
            cls = {"kraken": "crypto", "ib": "future"}.get(venue) or classify_symbol(sym)
            out.append(Instrument(sym, venue, cls))
    return out


def cutoff_for(day: date, asset_class: str) -> datetime:
    """When ``day``'s features are frozen. Crypto's 00:00 cutoff belongs to the day that just ENDED."""
    hour = CUTOFF_HOUR_UTC.get(asset_class, DEFAULT_CUTOFF_HOUR)
    base = datetime(day.year, day.month, day.day, tzinfo=UTC)
    return base + DAY if hour == 0 else base + timedelta(hours=hour)


# --------------------------------------------------------------------------- sources


def _ts(raw: object) -> datetime | None:
    try:
        t = datetime.fromisoformat(str(raw).replace(" ", "T").replace("Z", "+00:00"))
    except ValueError:
        return None
    return t if t.tzinfo else t.replace(tzinfo=UTC)


def _jsonl(path: Path) -> Iterator[dict[str, Any]]:
    if not path.exists():
        return
    with path.open(encoding="utf-8") as fh:
        for line in fh:
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(row, dict):
                yield row


@dataclass(frozen=True)
class Article:
    at: datetime
    title_key: str
    source: str  # feed name (live) or publishing domain (gkg)
    title: bool  # the title names the symbol (vs. only tagged / org-listed)
    tone: float | None = None


class TimeIndex:
    """Sorted times with a payload, for half-open window queries ``[start, end)``.

    The end is open: a record stamped exactly at a cutoff was not known before it.
    """

    def __init__(self, items: Iterable[tuple[datetime, Any]]) -> None:
        pairs = sorted(items, key=lambda p: p[0])
        self.times = [p[0] for p in pairs]
        self.items = [p[1] for p in pairs]

    def window(self, start: datetime, end: datetime) -> list[Any]:
        return self.items[bisect_left(self.times, start):bisect_left(self.times, end)]

    def times_in(self, start: datetime, end: datetime) -> list[datetime]:
        return self.times[bisect_left(self.times, start):bisect_left(self.times, end)]

    def last_before(self, end: datetime) -> tuple[datetime, Any] | None:
        i = bisect_left(self.times, end)  # strictly before ``end``
        return (self.times[i - 1], self.items[i - 1]) if i else None


def _articles(rows: Iterable[dict[str, Any]], *, tag_field: str, source_field: str,
              symbols: set[str]) -> dict[str, TimeIndex]:
    per: dict[str, list[tuple[datetime, Article]]] = defaultdict(list)
    for r in rows:
        at = _ts(r.get("arrived_utc"))
        if at is None:
            continue
        named = set(r.get("symbols") or ())
        tagged = set(r.get(tag_field) or ()) - named
        tone = r.get("tone")
        for sym in (named | tagged) & symbols:
            per[sym].append((at, Article(at, str(r.get("title_key", "")), str(r.get(source_field, "")),
                                         sym in named, float(tone) if isinstance(tone, (int, float)) else None)))
    return {s: TimeIndex(v) for s, v in per.items()}


def _is_empty_overlay(snap: Mapping[str, Any]) -> bool:
    """The test-suite leak (2026-09-18) wrote snapshots with nothing in them; they are not observations."""
    return (not snap.get("country_alert_counts") and not snap.get("event_acceleration")
            and not snap.get("source_age_hours"))


@dataclass
class Sources:
    """Everything the builder reads, loaded once. Construct with :meth:`load`, or directly in tests."""

    live: dict[str, TimeIndex]
    gkg: dict[str, TimeIndex]
    live_passes: TimeIndex  # heartbeat times of the live collector
    gkg_files: TimeIndex  # stamps of GKG files fetched (status ok)
    overlay: TimeIndex  # overlay journal rows (non-empty)
    graph_events: TimeIndex  # (domain) per event->domain edge
    graph_since: datetime | None = None  # oldest surviving edge: earlier windows were washed away
    notes: list[str] = field(default_factory=list)

    @classmethod
    def load(cls, state: Path, symbols: Sequence[str]) -> Sources:
        syms = set(symbols)
        # Backfill rows (YYYY-MM.jsonl) and the forward follower's (live-YYYY-MM.jsonl): one clock, two writers.
        gkg_dir = state / "gdelt_gkg"
        gkg_rows: Iterator[dict[str, Any]] = (r for p in sorted(gkg_dir.glob("*.jsonl"))
                                              if not p.name.endswith("progress.jsonl") for r in _jsonl(p))
        overlay: list[tuple[datetime, dict[str, Any]]] = []
        dropped = 0
        for r in _jsonl(state / "intel_overlay.jsonl"):
            at = _ts(r.get("as_of"))
            if at is None or _is_empty_overlay(r.get("snapshot") or {}):
                dropped += 1
                continue
            overlay.append((at, r))
        events: list[tuple[datetime, str]] = []
        oldest: datetime | None = None
        for r in _jsonl(state / "intel_graph.jsonl"):
            at = _ts(r.get("as_of"))
            if at is None or r.get("predicate") == "mentions":
                continue
            oldest = at if oldest is None or at < oldest else oldest
            if r.get("predicate") == "about_domain":
                events.append((at, str((r.get("object") or ["", ""])[1])))
        gkg_ok = []
        for r in (r for p in sorted(gkg_dir.glob("*progress.jsonl")) for r in _jsonl(p)):
            if r.get("status") == "ok":
                stamp = str(r.get("stamp", ""))
                try:
                    gkg_ok.append((datetime.strptime(stamp, "%Y%m%d%H%M%S").replace(tzinfo=UTC), stamp))
                except ValueError:
                    continue
        passes = [(t, None) for t in (_ts(r.get("ts")) for r in _jsonl(state / "news_passes.jsonl")) if t]
        return cls(
            live=_articles(_jsonl(state / "news.jsonl"), tag_field="feed_symbols", source_field="source",
                           symbols=syms),
            gkg=_articles(gkg_rows, tag_field="org_symbols", source_field="domain", symbols=syms),
            live_passes=TimeIndex(passes), gkg_files=TimeIndex(gkg_ok), overlay=TimeIndex(overlay),
            graph_events=TimeIndex(events), graph_since=oldest,
            notes=[f"overlay rows dropped as empty or unstamped: {dropped}"] if dropped else [],
        )


# --------------------------------------------------------------------------- features


def _coverage_hours(index: TimeIndex, start: datetime, end: datetime) -> float:
    """Share of the window's hours with at least one heartbeat / file in them."""
    hours = max(1, round((end - start) / timedelta(hours=1)))
    seen = {int((t - start) / timedelta(hours=1)) for t in index.times_in(start, end)}
    return round(len(seen) / hours, 4)


def _news_family(prefix: str, idx: TimeIndex | None, cutoff: datetime, *, coverage: float,
                 day_coverage: Mapping[date, float]) -> dict[str, float | int | None]:
    """Counts, novelty, source spread, tone and an abnormal-volume ratio for one clock."""
    keys = ("title_24h", "tagged_24h", "sources_24h", "novelty_24h", "tone_24h", "title_abn")
    if coverage == 0.0:
        return {f"{prefix}_{k}": None for k in keys}  # not recorded is not the same as zero
    arts: list[Article] = idx.window(cutoff - DAY, cutoff) if idx else []
    titled = [a for a in arts if a.title]
    prior = {a.title_key for a in (idx.window(cutoff - NOVELTY_LOOKBACK, cutoff - DAY) if idx else []) if a.title}
    tones = [a.tone for a in titled if a.tone is not None]
    feats: dict[str, float | int | None] = {
        f"{prefix}_title_24h": len(titled),
        f"{prefix}_tagged_24h": len(arts) - len(titled),
        f"{prefix}_sources_24h": len({a.source for a in titled}),
        f"{prefix}_novelty_24h": round(sum(a.title_key not in prior for a in titled) / len(titled), 4) if titled else None,
        f"{prefix}_tone_24h": round(sum(tones) / len(tones), 4) if tones else None,
        f"{prefix}_title_abn": None,
    }
    # Abnormal volume: today's title count over the mean of covered prior days. Days the clock did not
    # cover are left out of the mean, and too few covered days means no ratio rather than a noisy one.
    base_days = [cutoff - DAY * k for k in range(1, ABNORMAL_BASELINE_DAYS + 1)]
    covered = [d for d in base_days if day_coverage.get(d.date(), 0.0) >= 0.5]
    if idx and len(covered) >= MIN_BASELINE_COVERAGE * ABNORMAL_BASELINE_DAYS:
        mean = sum(sum(a.title for a in idx.window(d - DAY, d)) for d in covered) / len(covered)
        feats[f"{prefix}_title_abn"] = round(len(titled) / mean, 4) if mean > 0 else None
    return feats


def _overlay_family(src: Sources, cutoff: datetime, asset_class: str) -> tuple[dict[str, Any], float | None]:
    keys = ("scalar", "halt", "degraded", "strategic_risk", "energy_stress", "fear_greed", "accel_max",
            "conflict_events", "global_alerts")
    hit = src.overlay.last_before(cutoff)
    if hit is None or cutoff - hit[0] > OVERLAY_MAX_AGE:
        return {f"ov_{k}": None for k in keys}, (None if hit is None else round((cutoff - hit[0]) / timedelta(hours=1), 2))
    at, row = hit
    snap, dec = row.get("snapshot") or {}, (row.get("decisions") or {}).get(asset_class) or {}
    accel = [v for v in (snap.get("event_acceleration") or {}).values() if isinstance(v, (int, float))]
    return {
        "ov_scalar": dec.get("scalar"), "ov_halt": dec.get("halt"), "ov_degraded": snap.get("degraded"),
        "ov_strategic_risk": snap.get("strategic_risk"), "ov_energy_stress": snap.get("energy_stress"),
        "ov_fear_greed": snap.get("fear_greed"), "ov_accel_max": max(accel) if accel else None,
        "ov_conflict_events": snap.get("conflict_events_active"), "ov_global_alerts": snap.get("global_alert_count"),
    }, round((cutoff - at) / timedelta(hours=1), 2)


def _graph_family(src: Sources, cutoff: datetime) -> tuple[dict[str, Any], bool]:
    """WorldMonitor events per domain in the last 24 h. Unknown (None) where the window predates the
    oldest surviving edge: the wash removed it, so a zero there would be invented."""
    if src.graph_since is None or src.graph_since > cutoff - DAY:
        return {"graph_events_24h": None, "graph_events_by_domain_24h": None}, False
    by: dict[str, int] = defaultdict(int)
    for dom in src.graph_events.window(cutoff - DAY, cutoff):
        by[dom] += 1
    return {"graph_events_24h": sum(by.values()), "graph_events_by_domain_24h": dict(sorted(by.items()))}, True


def build_row(inst: Instrument, day: date, src: Sources, *, now: datetime,
              live_days: Mapping[date, float], gkg_days: Mapping[date, float]) -> dict[str, Any] | None:
    """One snapshot, or None when the cutoff has not passed yet (a future row would be a look-ahead)."""
    cutoff = cutoff_for(day, inst.asset_class)
    if now < cutoff:
        return None
    live_cov = _coverage_hours(src.live_passes, cutoff - DAY, cutoff)
    gkg_cov = _coverage_hours(src.gkg_files, cutoff - DAY, cutoff)
    ov, ov_age = _overlay_family(src, cutoff, inst.asset_class)
    graph, graph_ok = _graph_family(src, cutoff)
    features: dict[str, Any] = {
        **_news_family("live", src.live.get(inst.symbol), cutoff, coverage=live_cov, day_coverage=live_days),
        **_news_family("gkg", src.gkg.get(inst.symbol), cutoff, coverage=gkg_cov, day_coverage=gkg_days),
        **ov, **graph,
    }
    return {
        "date": day.isoformat(), "symbol": inst.symbol, "venue": inst.venue, "asset_class": inst.asset_class,
        "cutoff_utc": cutoff.isoformat(), "built_utc": now.isoformat(),
        "mode": "live" if now - cutoff <= LIVE_GRACE else "rebuild", "version": VERSION,
        "features": features,
        "coverage": {"live": live_cov, "gkg": gkg_cov, "overlay_age_h": ov_age, "graph": graph_ok},
    }


def daily_coverage(index: TimeIndex, days: Iterable[date], asset_class: str) -> dict[date, float]:
    """Coverage of the 24 h window ending at each day's cutoff, keyed by the cutoff's date."""
    out: dict[date, float] = {}
    for d in days:
        c = cutoff_for(d, asset_class)
        out[c.date()] = _coverage_hours(index, c - DAY, c)
    return out


# --------------------------------------------------------------------------- the store


def existing_keys(store: Path) -> dict[tuple[str, str, int], str]:
    """(date, symbol, version) -> mode already stored."""
    out: dict[tuple[str, str, int], str] = {}
    for r in _jsonl(store):
        key = (str(r.get("date")), str(r.get("symbol")), int(r.get("version", 0)))
        if out.get(key) != "live":  # a live row is never superseded
            out[key] = str(r.get("mode"))
    return out


def build(store: Path, src: Sources, instruments: Sequence[Instrument], days: Sequence[date], *,
          now: datetime, force_rebuild: bool = False) -> list[dict[str, Any]]:
    """Append rows for every (instrument, day) whose cutoff has passed and that is not stored yet.

    ``force_rebuild`` re-appends rebuild rows (newest wins on read); live rows are never re-appended.
    """
    have = existing_keys(store)
    span = [min(days) - DAY * (ABNORMAL_BASELINE_DAYS + 2) + DAY * k
            for k in range((max(days) - min(days)).days + ABNORMAL_BASELINE_DAYS + 3)] if days else []
    cov_live: dict[str, dict[date, float]] = {}
    cov_gkg: dict[str, dict[date, float]] = {}
    rows: list[dict[str, Any]] = []
    for inst in instruments:
        if inst.asset_class not in cov_live:
            cov_live[inst.asset_class] = daily_coverage(src.live_passes, span, inst.asset_class)
            cov_gkg[inst.asset_class] = daily_coverage(src.gkg_files, span, inst.asset_class)
        for d in days:
            prior = have.get((d.isoformat(), inst.symbol, VERSION))
            if prior == "live" or (prior == "rebuild" and not force_rebuild):
                continue
            row = build_row(inst, d, src, now=now, live_days=cov_live[inst.asset_class],
                            gkg_days=cov_gkg[inst.asset_class])
            if row is not None:
                rows.append(row)
    if rows:
        store.parent.mkdir(parents=True, exist_ok=True)
        with store.open("a", encoding="utf-8") as fh:
            fh.writelines(json.dumps(r) + "\n" for r in rows)
    return rows


def load_store(store: Path, *, version: int = VERSION) -> dict[tuple[str, str], dict[str, Any]]:
    """(date, symbol) -> row: a live row if one exists, else the newest rebuild."""
    out: dict[tuple[str, str], dict[str, Any]] = {}
    for r in _jsonl(store):
        if int(r.get("version", 0)) != version:
            continue
        key = (str(r["date"]), str(r["symbol"]))
        if out.get(key, {}).get("mode") != "live":
            out[key] = r
    return out
