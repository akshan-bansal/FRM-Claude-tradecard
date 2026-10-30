"""Point-in-time feature store: no look-ahead, clocks kept apart, unknown is not zero, live rows kept.

All inputs are synthetic and built in tmp dirs. No network.
"""
from __future__ import annotations

import ast
import json
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

from trading_live_claude.analysis import feature_store as fs
from trading_live_claude.analysis.feature_store import (
    Article,
    Instrument,
    Sources,
    TimeIndex,
    build,
    build_row,
    cutoff_for,
    load_store,
)

NVDA = Instrument("NVDA", "qt", "equity")
DAY = date(2026, 6, 10)
CUT = datetime(2026, 6, 10, 20, tzinfo=UTC)  # NVDA's cutoff on DAY


def _art(at: datetime, key: str, *, title: bool = True, source: str = "wire", tone: float | None = None) -> tuple:
    return at, Article(at, key, source, title, tone)


def _hourly(start: datetime, end: datetime) -> TimeIndex:
    hours = int((end - start) / timedelta(hours=1))
    return TimeIndex((start + timedelta(hours=h, minutes=5), None) for h in range(hours))


def _src(**kw: object) -> Sources:
    base = dict(live={}, gkg={}, live_passes=_hourly(CUT - timedelta(days=40), CUT + timedelta(days=2)),
                gkg_files=TimeIndex([]), overlay=TimeIndex([]), graph_events=TimeIndex([]), graph_since=None)
    base.update(kw)
    return Sources(**base)  # type: ignore[arg-type]


def _row(src: Sources, inst: Instrument = NVDA, day: date = DAY, now: datetime | None = None) -> dict:
    full = {CUT.date() - timedelta(days=k): 1.0 for k in range(60)}
    row = build_row(inst, day, src, now=now or CUT + timedelta(minutes=10), live_days=full, gkg_days={})
    assert row is not None
    return row


def test_records_at_or_after_the_cutoff_are_not_used() -> None:
    idx = TimeIndex([_art(CUT - timedelta(hours=1), "a"), _art(CUT, "b"), _art(CUT + timedelta(hours=1), "c")])
    assert _row(_src(live={"NVDA": idx}))["features"]["live_title_24h"] == 1


def test_a_cutoff_that_has_not_passed_builds_nothing() -> None:
    assert build_row(NVDA, DAY, _src(), now=CUT - timedelta(seconds=1), live_days={}, gkg_days={}) is None


def test_crypto_freezes_at_the_next_midnight_and_equities_at_20_utc() -> None:
    assert cutoff_for(DAY, "crypto") == datetime(2026, 6, 11, tzinfo=UTC)
    assert cutoff_for(DAY, "equity") == CUT and cutoff_for(DAY, "future") == CUT


def test_title_mentions_and_tags_are_counted_apart_with_spread_and_tone() -> None:
    idx = TimeIndex([_art(CUT - timedelta(hours=2), "a", source="x", tone=-2.0),
                     _art(CUT - timedelta(hours=3), "b", source="y", tone=1.0),
                     _art(CUT - timedelta(hours=4), "c", title=False, source="z")])
    f = _row(_src(live={"NVDA": idx}))["features"]
    assert (f["live_title_24h"], f["live_tagged_24h"], f["live_sources_24h"]) == (2, 1, 2)
    assert f["live_tone_24h"] == -0.5


def test_novelty_is_the_share_of_titles_not_seen_in_the_prior_week() -> None:
    idx = TimeIndex([_art(CUT - timedelta(days=3), "old story"),
                     _art(CUT - timedelta(hours=2), "old story"), _art(CUT - timedelta(hours=1), "new story")])
    assert _row(_src(live={"NVDA": idx}))["features"]["live_novelty_24h"] == 0.5


def test_an_uncovered_window_is_unknown_not_zero() -> None:
    f = _row(_src(live_passes=TimeIndex([])))
    assert f["features"]["live_title_24h"] is None and f["coverage"]["live"] == 0.0
    assert f["features"]["gkg_title_24h"] is None


def test_abnormal_volume_needs_a_covered_baseline() -> None:
    arts = [_art(CUT - timedelta(days=k, hours=2), f"s{k}") for k in range(1, 29)]
    arts += [_art(CUT - timedelta(hours=k), f"t{k}") for k in (1, 2, 3)]
    idx = TimeIndex(arts)
    full = {CUT.date() - timedelta(days=k): 1.0 for k in range(60)}
    row = build_row(NVDA, DAY, _src(live={"NVDA": idx}), now=CUT, live_days=full, gkg_days={})
    assert row is not None and row["features"]["live_title_abn"] == 3.0
    sparse = {CUT.date() - timedelta(days=k): 1.0 for k in range(10)}
    row = build_row(NVDA, DAY, _src(live={"NVDA": idx}), now=CUT, live_days=sparse, gkg_days={})
    assert row is not None and row["features"]["live_title_abn"] is None


def _overlay(at: datetime, scalar: float) -> tuple:
    return at, {"as_of": at.isoformat(),
                "snapshot": {"degraded": True, "strategic_risk": 70.0, "event_acceleration": {"energy": 1.4},
                             "country_alert_counts": {"US": 1}},
                "decisions": {"equity": {"scalar": scalar, "halt": False}, "crypto": {"scalar": 0.4, "halt": False}}}


def test_overlay_reads_the_class_decision_from_the_last_snapshot_before_the_cutoff() -> None:
    src = _src(overlay=TimeIndex([_overlay(CUT - timedelta(hours=3), 0.6), _overlay(CUT + timedelta(minutes=1), 0.1)]))
    row = _row(src)
    assert row["features"]["ov_scalar"] == 0.6 and row["features"]["ov_accel_max"] == 1.4
    assert row["coverage"]["overlay_age_h"] == 3.0


def test_a_stale_overlay_is_unknown() -> None:
    row = _row(_src(overlay=TimeIndex([_overlay(CUT - timedelta(hours=7), 0.6)])))
    assert row["features"]["ov_scalar"] is None and row["coverage"]["overlay_age_h"] == 7.0


def test_graph_windows_the_wash_removed_are_unknown() -> None:
    events = TimeIndex([(CUT - timedelta(hours=1), "energy"), (CUT - timedelta(hours=2), "energy")])
    washed = _row(_src(graph_events=events, graph_since=CUT - timedelta(hours=5)))
    assert washed["features"]["graph_events_24h"] is None and washed["coverage"]["graph"] is False
    kept = _row(_src(graph_events=events, graph_since=CUT - timedelta(days=3)))
    assert kept["features"]["graph_events_by_domain_24h"] == {"energy": 2}


def test_rows_built_late_are_rebuilds_and_never_replace_a_live_row(tmp_path: Path) -> None:
    store = tmp_path / "lens_features.jsonl"
    src = _src()
    live = build(store, src, [NVDA], [DAY], now=CUT + timedelta(minutes=5))
    assert [r["mode"] for r in live] == ["live"]
    assert build(store, src, [NVDA], [DAY], now=CUT + timedelta(days=5), force_rebuild=True) == []
    late = build(store, src, [NVDA], [DAY + timedelta(days=1)], now=CUT + timedelta(days=5))
    assert [r["mode"] for r in late] == ["rebuild"]
    assert load_store(store)[(DAY.isoformat(), "NVDA")]["mode"] == "live"


def test_the_empty_overlay_rows_from_the_test_leak_are_dropped(tmp_path: Path) -> None:
    (tmp_path / "intel_overlay.jsonl").write_text("\n".join([
        json.dumps({"as_of": "2026-06-10T10:00:00+00:00", "snapshot": {"global_alert_count": 12}, "decisions": {}}),
        json.dumps(_overlay(CUT - timedelta(hours=1), 0.5)[1]),
    ]) + "\n", encoding="utf-8")
    src = Sources.load(tmp_path, ["NVDA"])
    assert len(src.overlay.times) == 1 and "dropped" in src.notes[0]


def test_sources_load_keeps_live_and_gkg_clocks_apart(tmp_path: Path) -> None:
    (tmp_path / "news.jsonl").write_text(json.dumps({
        "arrived_utc": "2026-06-10T18:00:00+00:00", "title_key": "x", "source": "wire",
        "symbols": ["NVDA"], "feed_symbols": ["BTC/USD"]}) + "\n", encoding="utf-8")
    (tmp_path / "gdelt_gkg").mkdir()
    (tmp_path / "gdelt_gkg" / "live-2026-06.jsonl").write_text(json.dumps({
        "arrived_utc": "2026-06-10T17:00:00+00:00", "title_key": "y", "domain": "a.test",
        "symbols": [], "org_symbols": ["NVDA"], "tone": -3.0}) + "\n", encoding="utf-8")
    src = Sources.load(tmp_path, ["NVDA", "BTC/USD"])
    (live,) = src.live["NVDA"].items
    (tagged,) = src.live["BTC/USD"].items
    (gkg,) = src.gkg["NVDA"].items
    assert live.title and not tagged.title and not gkg.title and gkg.source == "a.test"


def test_the_store_is_outside_the_trading_path() -> None:
    tree = ast.parse(Path(fs.__file__).read_text(encoding="utf-8"))
    mods = {n.module for n in ast.walk(tree) if isinstance(n, ast.ImportFrom) and n.module}
    forbidden = ("execution", "risk", "brokers", "strategies", "monitor", "daemon", "tune", "cli")
    assert not {m for m in mods for f in forbidden if f".{f}" in f".{m}"}
