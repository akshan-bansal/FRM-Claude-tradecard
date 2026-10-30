"""GDELT GKG backfill: parsing, point-in-time stamps, budget, resume, rolling window. Synthetic; no network."""
from __future__ import annotations

import io
import json
import zipfile
from datetime import UTC, datetime
from pathlib import Path

from trading_live_claude.news.gkg import Backfill, Fetched, parse_gkg, prune_to_span, stamps, window_start

ALIASES = {"NVDA": ("Nvidia",), "SONY": ("Sony",)}


def _row(rid: str, title: str, orgs: str = "", names: str = "", tone: str = "-1.5,2,3") -> str:
    c = [""] * 27
    c[0], c[3], c[4], c[13], c[15], c[23] = rid, "news.test", f"https://news.test/{rid}", orgs, tone, names
    c[26] = f"<PAGE_TITLE>{title}</PAGE_TITLE>" if title else ""
    return "\t".join(c)


def _zip(*rows: str) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("x.gkg.csv", "\n".join(rows))
    return buf.getvalue()


def test_title_and_org_mentions_are_kept_apart_and_unmatched_rows_dropped() -> None:
    payload = _zip(_row("a", "Nvidia &amp; partners ship"), _row("b", "Markets wobble", orgs="sony corp"),
                   _row("c", "Rain in Ohio"), "short\tline")
    total, kept = parse_gkg(payload, stamp="20261012163000", aliases=ALIASES)
    assert total == 3  # the malformed line is not an article
    assert [(r["guid"], r["symbols"], r["org_symbols"]) for r in kept] == [
        ("a", ["NVDA"], []), ("b", [], ["SONY"])]
    assert kept[0]["title"] == "Nvidia & partners ship"  # entities unescaped
    assert kept[0]["tone"] == -1.5 and kept[0]["backfill"] is True


def test_arrival_is_the_file_stamp() -> None:
    _, kept = parse_gkg(_zip(_row("a", "Nvidia ships")), stamp="20260601001500", aliases=ALIASES)
    assert kept[0]["arrived_utc"] == "2026-06-01T00:15:00+00:00" and kept[0]["published_utc"] == ""


def test_names_lose_their_character_offsets() -> None:
    _, kept = parse_gkg(_zip(_row("a", "", names="Sony Group,120;Tokyo,300")), stamp="20260601000000",
                        aliases=ALIASES)
    assert kept[0]["org_symbols"] == ["SONY"]


def test_stamps_walk_back_on_the_quarter_hour_grid() -> None:
    got = list(stamps(datetime(2026, 10, 12, 16, 37, tzinfo=UTC), datetime(2026, 10, 12, 16, 0, tzinfo=UTC)))
    assert got == ["20261012163000", "20261012161500", "20261012160000"]


def test_missing_files_settle_and_errors_retry_on_resume(tmp_path: Path) -> None:
    ok, gone, bad = "20260601003000", "20260601001500", "20260601000000"
    replies = {ok: Fetched(200, _zip(_row("a", "Nvidia ships"))), gone: Fetched(404), bad: Fetched(503, error="x")}
    bf = Backfill(out_dir=tmp_path, aliases=ALIASES, budget_bytes=10**9, fetch=lambda s: replies[s])
    for s in replies:
        bf.commit(*bf.process(s))
    done, spent = bf.done()
    assert done == {ok, gone}  # the 503 is not settled, so a resumed run fetches it again
    assert spent == len(replies[ok].body)
    rows = [json.loads(ln) for ln in (tmp_path / "2026-06.jsonl").read_text(encoding="utf-8").splitlines()]
    assert [r["guid"] for r in rows] == ["a"]


def test_prune_keeps_the_newest_span_and_drops_rows_with_their_progress(tmp_path: Path) -> None:
    body = _zip(_row("a", "Nvidia ships"))
    old = Backfill(out_dir=tmp_path, aliases=ALIASES, budget_bytes=10**9, fetch=lambda s: Fetched(200, body))
    new = Backfill(out_dir=tmp_path, aliases=ALIASES, budget_bytes=0, fetch=lambda s: Fetched(200, body),
                   prefix="live-")
    old.commit(*old.process("20260501000000"))
    old.commit(*old.process("20260601000000"))
    new.commit(*new.process("20260701000000"))
    res = prune_to_span(tmp_path, 2 * len(body))  # room for the two newest files
    assert res["pruned"] and res["start"] == "20260601000000" and res["rows_dropped"] == 1
    assert not (tmp_path / "2026-05.jsonl").exists()
    assert "20260501000000" not in old.settled_everywhere()  # progress dropped with its rows
    assert new.settled_everywhere() == {"20260601000000", "20260701000000"}
    assert window_start(tmp_path) == "20260601000000"
    assert prune_to_span(tmp_path, 10**9)["pruned"] is False  # everything fits: nothing to do


def test_spent_bytes_accumulate_across_runs(tmp_path: Path) -> None:
    body = _zip(_row("a", "Rain"))
    bf = Backfill(out_dir=tmp_path, aliases=ALIASES, budget_bytes=10**9, fetch=lambda s: Fetched(200, body))
    bf.commit(*bf.process("20260601000000"))
    again = Backfill(out_dir=tmp_path, aliases=ALIASES, budget_bytes=10**9, fetch=lambda s: Fetched(200, body))
    again.commit(*again.process("20260601001500"))
    assert again.done()[1] == 2 * len(body)
    assert not list(tmp_path.glob("2026-06.jsonl"))  # nothing matched, so no rows file
