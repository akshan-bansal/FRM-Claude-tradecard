"""Evidence Atlas: the catalogue, readings only from journals, whole-file rejection, and the page."""
from __future__ import annotations

import csv
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from trading_live_claude.atlas import (
    SnapshotError,
    load_catalog,
    render,
    take_snapshot,
    validate_snapshot,
)
from trading_live_claude.atlas import snapshot as snapmod

ROOT = Path(__file__).resolve().parent.parent
NOW = datetime(2026, 10, 30, 12, tzinfo=UTC)
CAT_ID, METERS = load_catalog(ROOT / "config" / "meter_catalog.csv")


def test_the_catalogue_is_well_formed() -> None:
    assert len(METERS) == len({m.id for m in METERS}) >= 50
    assert all(m.stale_after_s > 0 and m.source and m.definition for m in METERS)
    sections = {m.section for m in METERS}
    assert {"04 News", "05 GDELT", "06 Stress domains", "10 Lens verdicts", "11 OASIS"} <= sections


def test_an_empty_state_folder_yields_no_invented_readings(tmp_path: Path) -> None:
    snap = take_snapshot(tmp_path, ROOT, METERS, CAT_ID, now=NOW)
    ids = {r["id"] for r in snap["readings"]}
    assert ids == {"RG.HALT"}  # the only fact an empty folder supports: no HALTED file
    assert validate_snapshot(snap, METERS, CAT_ID, now=NOW)["RG.HALT"]["value"] == 0.0


def _state(tmp_path: Path) -> Path:
    st = tmp_path / "state"  # conftest's sandbox already made it
    st.mkdir(exist_ok=True)
    with (st / "paper_equity.csv").open("w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=["ts", "session_id", "equity", "cash", "positions_value", "realized_pnl",
                                           "unrealized_pnl", "peak_equity", "drawdown_pct"])
        w.writeheader()
        w.writerow({"ts": (NOW - timedelta(minutes=10)).isoformat(), "session_id": "abcdef1234", "equity": 99000,
                    "cash": 49500, "positions_value": 49500, "realized_pnl": -400, "unrealized_pnl": -600,
                    "peak_equity": 100000, "drawdown_pct": 0.01})
    (st / "news.jsonl").write_text("".join(json.dumps(r) + "\n" for r in [
        {"arrived_utc": (NOW - timedelta(hours=1)).isoformat(), "symbols": ["NVDA"]},
        {"arrived_utc": (NOW - timedelta(hours=2)).isoformat(), "symbols": []},
        {"arrived_utc": (NOW - timedelta(days=3)).isoformat(), "symbols": ["NVDA"]}]), encoding="utf-8")
    (st / "news_passes.jsonl").write_text(json.dumps({"ts": (NOW - timedelta(minutes=30)).isoformat()}) + "\n",
                                          encoding="utf-8")
    (st / "oasis_ledger.jsonl").write_text(json.dumps({"kind": "reserve", "run_id": "r", "usd": 5}) + "\n",
                                           encoding="utf-8")
    return st


def test_readings_come_from_the_journals_with_their_own_timestamps(tmp_path: Path) -> None:
    snap = take_snapshot(_state(tmp_path), ROOT, METERS, CAT_ID, now=NOW)
    r = validate_snapshot(snap, METERS, CAT_ID, now=NOW)
    assert r["PF.NAV"]["value"] == 99000 and r["PF.DD"]["value"] == 1.0 and r["PF.EXPO"]["value"] == 50.0
    assert r["PF.NAV"]["observed_utc"] == (NOW - timedelta(minutes=10)).isoformat()  # the row's time, not now
    assert r["NW.HEAD24"]["value"] == 2 and r["NW.MAPPED"]["value"] == 50.0 and r["NW.BEAT"]["value"] == 30.0
    assert r["OA.RESERVED"]["value"] == 5 and r["OA.LEFT"]["value"] == 95
    assert "PF.FILLS24" not in r and "AP.PENDING" not in r  # no fills journal, no approval db: no reading


def test_one_bad_reading_rejects_the_whole_snapshot(tmp_path: Path) -> None:
    good = take_snapshot(_state(tmp_path), ROOT, METERS, CAT_ID, now=NOW)
    for mutate, match in ((lambda s: s["readings"][0].update(scope=""), "no source or no scope"),
                          (lambda s: s["readings"][0].update(observed_utc="2099-01-01T00:00:00+00:00"), "implausible"),
                          (lambda s: s["readings"][0].update(unit="EUR"), "unit"),
                          (lambda s: s["readings"].append({**s["readings"][0], "id": "XX.FAKE"}), "not a catalogued"),
                          (lambda s: s.update(catalog_id="old"), "catalogue")):
        bad = json.loads(json.dumps(good))
        mutate(bad)
        with pytest.raises(SnapshotError, match=match):
            validate_snapshot(bad, METERS, CAT_ID, now=NOW)


def test_a_failing_extractor_is_recorded_and_contributes_nothing(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    def boom(state: Path, root: Path, now: datetime) -> dict:
        raise RuntimeError("journal unreadable")

    monkeypatch.setattr(snapmod, "EXTRACTORS", (boom, snapmod._gates))
    snap = take_snapshot(tmp_path, ROOT, METERS, CAT_ID, now=NOW)
    assert [r["id"] for r in snap["readings"]] == ["RG.HALT"] and "journal unreadable" in snap["extractor_errors"][0]


def test_the_page_embeds_catalogue_and_snapshot_safely(tmp_path: Path) -> None:
    snap = take_snapshot(_state(tmp_path), ROOT, METERS, CAT_ID, now=NOW)
    snap["readings"][0]["scope"] = "</script><b>x</b>"  # hostile text must not close the data block
    page = render(METERS, CAT_ID, snap)
    assert page.count("</script>") == 3 and "<\\/script>" in page
    assert f'data-id="{CAT_ID}"' in page and "NO DATA" in page and "LOAD SNAPSHOT" in page
    assert "https://" not in page  # self-contained: nothing fetched


def test_the_atlas_is_read_only_and_outside_the_trading_path() -> None:
    import ast

    forbidden = ("execution", "brokers", "strategies", "monitor", "daemon", "tune", "cli")
    for f in (ROOT / "src" / "trading_live_claude" / "atlas").glob("*.py"):
        tree = ast.parse(f.read_text(encoding="utf-8"))
        mods = {n.module for n in ast.walk(tree) if isinstance(n, ast.ImportFrom) and n.module}
        assert not {m for m in mods for x in forbidden if f".{x}" in f".{m}"}, f.name
        writes = [n for n in ast.walk(tree) if isinstance(n, ast.Attribute) and n.attr in ("write_text", "unlink")]
        assert not writes, f"{f.name} writes files"
