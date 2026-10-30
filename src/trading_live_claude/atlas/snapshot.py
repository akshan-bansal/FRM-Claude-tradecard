"""Take an Evidence Atlas snapshot from the journals, and validate one before it is drawn.

Each extractor reads one journal and returns readings only for what that journal actually supports. A
missing journal, an empty window, or an undefined statistic yields no reading for that meter: never a zero
standing in for "unknown". Window counts are observed at the snapshot time; last-row values carry the
row's own timestamp, so their staleness is the journal's, not the snapshot's.
"""
from __future__ import annotations

import csv
import json
import math
import sqlite3
import statistics
from collections.abc import Callable, Mapping, Sequence
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from trading_live_claude.analysis.feature_store import _is_empty_overlay, load_store, universe
from trading_live_claude.analysis.graph_adapter import GraphSources, domain_z, venue_share
from trading_live_claude.analysis.pit import TimeIndex, coverage_hours, parse_ts, read_jsonl
from trading_live_claude.atlas.catalog import Meter
from trading_live_claude.news.gkg_themes import load_theme_map

DAY = timedelta(days=1)
FUTURE_TOLERANCE = timedelta(minutes=5)
EARLIEST = datetime(2015, 1, 1, tzinfo=UTC)

Reading = tuple[float, datetime, str]  # value, observed at, scope
Extractor = Callable[[Path, Path, datetime], dict[str, Reading]]


class SnapshotError(ValueError):
    pass


# --------------------------------------------------------------------------- extractors


def _portfolio(state: Path, root: Path, now: datetime) -> dict[str, Reading]:
    out: dict[str, Reading] = {}
    p = state / "paper_equity.csv"
    if p.exists():
        rows = list(csv.DictReader(p.read_text(encoding="utf-8").splitlines()))
        if rows:
            r = rows[-1]
            at = parse_ts(r.get("ts"))
            if at is not None:
                scope = f"paper session {str(r.get('session_id', ''))[:8]}, last equity row"
                eq = float(r["equity"])
                out |= {"PF.NAV": (eq, at, scope), "PF.CASH": (float(r["cash"]), at, scope),
                        "PF.RPNL": (float(r["realized_pnl"]), at, scope),
                        "PF.UPNL": (float(r["unrealized_pnl"]), at, scope),
                        "PF.DD": (float(r["drawdown_pct"]) * 100, at, scope)}
                if eq > 0:
                    out["PF.EXPO"] = (float(r["positions_value"]) / eq * 100, at, scope)
    fills = state / "paper_fills.jsonl"
    if fills.exists():
        n = sum(1 for r in read_jsonl(fills) if (t := parse_ts(r.get("fill_time"))) and now - DAY <= t < now)
        out["PF.FILLS24"] = (float(n), now, "fills in the 24 h before the snapshot, all sessions")
    return out


def _gates(state: Path, root: Path, now: datetime) -> dict[str, Reading]:
    out: dict[str, Reading] = {"RG.HALT": (1.0 if (state / "HALTED").exists() else 0.0, now, "state/HALTED")}
    rej = state / "rejected.jsonl"
    if rej.exists():
        rows = list(read_jsonl(rej))
        out["RG.REJTOT"] = (float(len(rows)), now, "whole rejection journal, all sessions")
        out["RG.REJ24"] = (float(sum(1 for r in rows if (t := parse_ts(r.get("ts"))) and now - DAY <= t < now)),
                           now, "rejections in the 24 h before the snapshot")
    return out


def _approvals(state: Path, root: Path, now: datetime) -> dict[str, Reading]:
    db = state / "approval.db"
    if not db.exists():
        return {}
    con = sqlite3.connect(f"file:{db.as_posix()}?mode=ro", uri=True)
    try:
        rows = con.execute("SELECT issued_at, expires_at, resolved_at, verdict FROM intents").fetchall()
    except sqlite3.Error:
        return {}
    finally:
        con.close()
    scope = "approval store, all sessions"
    pending = sum(1 for _, exp, _, v in rows if v is None and (t := parse_ts(exp)) and t > now)
    out: dict[str, Reading] = {"AP.PENDING": (float(pending), now, scope)}
    for mid, verdict in (("AP.ACCEPT", "ACCEPT"), ("AP.DECLINE", "DECLINE"), ("AP.EXPIRE", "EXPIRED")):
        out[mid] = (float(sum(1 for *_, v in rows if v == verdict)), now, scope)
    waits = [(r - i).total_seconds() for i_raw, _, r_raw, v in rows if v in ("ACCEPT", "DECLINE")
             if (i := parse_ts(i_raw)) and (r := parse_ts(r_raw))]
    if waits:
        out["AP.RESP"] = (statistics.median(waits), now, f"median over {len(waits)} decided prompts")
    return out


def _news(state: Path, root: Path, now: datetime) -> dict[str, Reading]:
    out: dict[str, Reading] = {}
    if (state / "news.jsonl").exists():
        day = [r for r in read_jsonl(state / "news.jsonl") if (t := parse_ts(r.get("arrived_utc"))) and now - DAY <= t < now]
        out["NW.HEAD24"] = (float(len(day)), now, "headlines in the 24 h before the snapshot")
        if day:
            out["NW.MAPPED"] = (100 * sum(1 for r in day if r.get("symbols")) / len(day), now, "same 24 h")
    health = state / "news_health.json"
    if health.exists():
        h = json.loads(health.read_text(encoding="utf-8"))
        at = parse_ts(h.get("as_of"))
        feeds = list((h.get("feeds") or {}).values())
        if at is not None and feeds:
            bad = sum(1 for f in feeds if f.get("last_error"))
            out["NW.FEEDSOK"] = (float(len(feeds) - bad), at, f"{len(feeds)} configured feeds")
            out["NW.FEEDSFAIL"] = (float(bad), at, f"{len(feeds)} configured feeds")
    beats = [t for r in read_jsonl(state / "news_passes.jsonl") if (t := parse_ts(r.get("ts")))]
    if beats:
        last = max(beats)
        out["NW.BEAT"] = ((now - last).total_seconds() / 60, now, f"last pass {last.isoformat()}")
    return out


def _gdelt(state: Path, root: Path, now: datetime) -> dict[str, Reading]:
    gkg = state / "gdelt_gkg"
    rows: list[tuple[datetime, dict[str, Any]]] = []
    for p in sorted(gkg.glob("*progress.jsonl")):
        for r in read_jsonl(p):
            if r.get("status") == "ok":
                try:
                    rows.append((datetime.strptime(str(r["stamp"]), "%Y%m%d%H%M%S").replace(tzinfo=UTC), r))
                except (KeyError, ValueError):
                    continue
    if not rows:
        return {}
    day = [r for t, r in rows if now - DAY <= t < now]
    scope = "GKG files stamped in the 24 h before the snapshot (backfill and follower)"
    times = [t for t, _ in rows]
    out: dict[str, Reading] = {
        "GK.FILES24": (float(len(day)), now, scope),
        "GK.ART24": (float(sum(int(r.get("rows", 0)) for r in day)), now, scope),
        "GK.KEPT24": (float(sum(int(r.get("kept", 0)) for r in day)), now, scope),
        "GK.COV": (100 * coverage_hours(TimeIndex((t, None) for t in times), now - DAY, now), now, scope),
        "GK.SPAN": ((max(times) - min(times)) / DAY, now, f"{min(times):%Y-%m-%d %H:%M} to {max(times):%Y-%m-%d %H:%M} UTC"),
    }
    backfill = [r for r in read_jsonl(gkg / "progress.jsonl")]
    if backfill:
        out["GK.GB"] = (sum(int(r.get("bytes", 0)) for r in backfill) / 1e9, now, "backfill progress, all runs")
    return out


def _graph_adapter(state: Path, root: Path, now: datetime) -> dict[str, Reading]:
    tm = load_theme_map(root / "config" / "gkg_theme_domains.yaml")
    syms = [i.symbol for i in universe(root / "config" / "basket_seed.json")]
    src = GraphSources.load(state, syms, tm)
    out: dict[str, Reading] = {}
    z = domain_z(src, now)
    if z is not None:
        for d, v in z.items():
            if v is not None:
                out[f"DZ.{d.upper()}"] = (v, now, f"theme map {tm.id}, 24 h vs prior 28 days")
    share = venue_share(src, now)
    if share is not None:
        out["GR.VENUE"] = (share * 100, now, "graph-journal edges in the 7 days before the snapshot")
    return out


def _overlay(state: Path, root: Path, now: datetime) -> dict[str, Reading]:
    last: tuple[datetime, dict[str, Any]] | None = None
    for r in read_jsonl(state / "intel_overlay.jsonl"):
        at = parse_ts(r.get("as_of"))
        if at is not None and at <= now and not _is_empty_overlay(r.get("snapshot") or {}):
            last = (at, r) if last is None or at > last[0] else last
    if last is None:
        return {}
    at, r = last
    snap, dec = r.get("snapshot") or {}, r.get("decisions") or {}
    scope = "last non-empty overlay snapshot"
    out: dict[str, Reading] = {}
    for mid, cls in (("OV.CRYPTO", "crypto"), ("OV.EQUITY", "equity"), ("OV.COMMODITY", "commodity")):
        v = (dec.get(cls) or {}).get("scalar")
        if isinstance(v, (int, float)):
            out[mid] = (float(v), at, scope)
    for mid, key in (("OV.GPR", "strategic_risk"), ("OV.FG", "fear_greed")):
        if isinstance(snap.get(key), (int, float)):
            out[mid] = (float(snap[key]), at, scope)
    if isinstance(snap.get("degraded"), bool):
        out["OV.DEGRADED"] = (1.0 if snap["degraded"] else 0.0, at, scope)
    return out


def _graph(state: Path, root: Path, now: datetime) -> dict[str, Reading]:
    p = state / "intel_graph.jsonl"
    if not p.exists():
        return {}
    rows = list(read_jsonl(p))
    recent = [r for r in rows if (t := parse_ts(r.get("as_of"))) and now - DAY <= t < now]
    scope = "graph journal; window = 24 h before the snapshot"
    return {"GR.EDGES": (float(len(rows)), now, "graph journal after washing"),
            "GR.EDGES24": (float(len(recent)), now, scope),
            "GR.MENT24": (float(sum(1 for r in recent if r.get("predicate") == "mentions")), now, scope)}


def _lens_store(state: Path, root: Path, now: datetime) -> dict[str, Reading]:
    out: dict[str, Reading] = {}
    rows = list(load_store(state / "lens_features.jsonl").values())
    if rows:
        newest = max(r["date"] for r in rows)
        scope = f"{len(rows)} rows, newest {newest}"
        out |= {"LS.ROWS": (float(len(rows)), now, scope),
                "LS.AGE": ((now.date() - datetime.fromisoformat(newest).date()).days, now, scope),
                "LS.LIVE": (100 * sum(r["mode"] == "live" for r in rows) / len(rows), now, scope),
                "LS.GX": (100 * sum(r["features"].get("gx_stress_exposure") is not None for r in rows) / len(rows),
                          now, scope)}
    labels = state / "lens_labels.jsonl"
    if labels.exists():
        out["LS.LABELS"] = (float(sum(1 for _ in read_jsonl(labels))), now, "lens_labels.jsonl")
    return out


def _lens_verdicts(state: Path, root: Path, now: datetime) -> dict[str, Reading]:
    reports = sorted((root / "reports").glob("lens_backtest_*.json"))
    if not reports:
        return {}
    d = json.loads(reports[-1].read_text(encoding="utf-8"))
    at = parse_ts(d.get("generated_utc"))
    if at is None:
        return {}
    scope = f"{reports[-1].name}"
    out: dict[str, Reading] = {"LV.TESTED": (float(d["tested"]), at, scope),
                               "LV.PASSED": (float(d["passed"]), at, scope),
                               "LV.CHANCE": (float(d["expected_by_chance"]), at, scope)}
    if d.get("fused_rho") is not None and math.isfinite(d["fused_rho"]):
        out["LV.RHO"] = (float(d["fused_rho"]), at, f"{scope}; voters {', '.join(d['voters'])}")
    if d.get("veto_gap") is not None:
        out["LV.VETOGAP"] = (float(d["veto_gap"]) * 100, at, scope)
    return out


def _oasis(state: Path, root: Path, now: datetime) -> dict[str, Reading]:
    from trading_live_claude.sim.budget import DEFAULT_TOTAL_USD, committed

    out: dict[str, Reading] = {}
    if (state / "oasis_ledger.jsonl").exists():
        c = committed(state)
        scope = f"ledger against the ${DEFAULT_TOTAL_USD:.0f} total"
        out |= {"OA.SPENT": (c["spent"], now, scope), "OA.RESERVED": (c["reserved"], now, scope),
                "OA.LEFT": (DEFAULT_TOTAL_USD - c["spent"] - c["reserved"], now, scope)}
    runs = [(t, r) for r in read_jsonl(state / "oasis_runs.jsonl") if (t := parse_ts(r.get("ingested_utc")))]
    if runs:
        at, last = max(runs, key=lambda x: x[0])
        scope = f"run {last.get('run_id')}"
        out |= {"OA.RUNS": (float(len(runs)), now, "oasis_runs.jsonl"),
                "OA.AGE": ((now - at).total_seconds() / 3600, now, scope),
                "OA.AGENTS": (float(last.get("n_agents", 0)), at, scope),
                "OA.SYMS": (float(len(last.get("by_symbol") or {})), at, scope)}
    return out


EXTRACTORS: tuple[Extractor, ...] = (_portfolio, _gates, _approvals, _news, _gdelt, _graph_adapter, _overlay,
                                     _graph, _lens_store, _lens_verdicts, _oasis)


# --------------------------------------------------------------------------- snapshot


def take_snapshot(state: Path, root: Path, meters: Sequence[Meter], catalog_id: str, *,
                  now: datetime) -> dict[str, Any]:
    """Run every extractor; an extractor that fails is recorded and contributes nothing, never a guess."""
    by_id = {m.id: m for m in meters}
    readings: list[dict[str, Any]] = []
    errors: list[str] = []
    for ex in EXTRACTORS:
        try:
            got = ex(state, root, now)
        except Exception as exc:  # one broken journal must not blank the whole atlas
            errors.append(f"{ex.__name__.lstrip('_')}: {type(exc).__name__}: {exc}"[:200])
            continue
        for mid, (value, at, scope) in sorted(got.items()):
            m = by_id.get(mid)
            if m is None or not math.isfinite(value):
                continue
            readings.append({"id": mid, "value": round(float(value), 6), "unit": m.unit, "source": m.source,
                             "scope": scope, "observed_utc": at.isoformat(), "stale_after_s": m.stale_after_s})
    return {"generated_utc": now.isoformat(), "state_dir": str(state), "catalog_id": catalog_id,
            "readings": readings, "extractor_errors": errors}


def validate_snapshot(snap: Mapping[str, Any], meters: Sequence[Meter], catalog_id: str, *,
                      now: datetime) -> dict[str, dict[str, Any]]:
    """Readings by meter id, or SnapshotError naming every problem: one bad reading rejects the file."""
    by_id = {m.id: m for m in meters}
    problems: list[str] = []
    if snap.get("catalog_id") != catalog_id:
        problems.append(f"taken against catalogue {snap.get('catalog_id')!r}, not the current {catalog_id!r}")
    out: dict[str, dict[str, Any]] = {}
    for r in snap.get("readings") or ():
        mid = str(r.get("id"))
        m = by_id.get(mid)
        at = parse_ts(r.get("observed_utc"))
        v = r.get("value")
        if m is None:
            problems.append(f"{mid}: not a catalogued meter")
            continue
        if not str(r.get("source", "")).strip() or not str(r.get("scope", "")).strip():
            problems.append(f"{mid}: reading has no source or no scope")
        if at is None or at < EARLIEST or at > now + FUTURE_TOLERANCE:
            problems.append(f"{mid}: implausible timestamp {r.get('observed_utc')!r}")
        if isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v):
            problems.append(f"{mid}: value {v!r} is not a finite number")
        if r.get("unit") != m.unit:
            problems.append(f"{mid}: unit {r.get('unit')!r} differs from the catalogue's {m.unit!r}")
        if mid in out:
            problems.append(f"{mid}: two readings")
        out[mid] = dict(r)
    if problems:
        raise SnapshotError("snapshot rejected: " + "; ".join(problems[:12]) +
                            (f" (+{len(problems) - 12} more)" if len(problems) > 12 else ""))
    return out
