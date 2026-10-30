"""GDELT GKG backfill: parsing, point-in-time stamps, budget, resume, rolling window. Synthetic; no network."""
from __future__ import annotations

import io
import json
import zipfile
from datetime import UTC, datetime
from pathlib import Path

import pytest

from trading_live_claude.news.gkg import (
    Backfill,
    Fetched,
    parse_gkg,
    prune_to_span,
    stamps,
    window_start,
)

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


# --------------------------------------------------------------------------- theme bridge (2026-10-30)

THEME_MAP_PATH = Path(__file__).resolve().parent.parent / "config" / "gkg_theme_domains.yaml"


def _row_with_themes(rid: str, title: str, themes: str) -> str:
    c = _row(rid, title).split("\t")
    c[7] = themes
    return "\t".join(c)


def test_theme_codes_map_by_exact_code_and_prefix_and_exclusions_win() -> None:
    from trading_live_claude.news.gkg_themes import load_theme_map

    tm = load_theme_map(THEME_MAP_PATH)
    assert tm.domains_for(["ENV_OIL"]) == {"energy"}
    assert tm.domains_for(["ECON_OILPRICE"]) == {"energy", "economy"}
    assert tm.domains_for(["NATURAL_DISASTER_FLOOD", "UNREST_BELLIGERENT"]) == {"disaster", "conflict"}
    assert tm.domains_for(["MANMADE_DISASTER_IMPLIED", "EPU_ECONOMY_HISTORIC", "TAX_FNCACT"]) == frozenset()
    assert all(tm.exact[d] or tm.prefix[d] for d in tm.domains)


def test_the_map_id_changes_when_the_map_does(tmp_path: Path) -> None:
    from trading_live_claude.news.gkg_themes import load_theme_map

    a = load_theme_map(THEME_MAP_PATH)
    assert load_theme_map(THEME_MAP_PATH).id == a.id
    other = tmp_path / "m.yaml"
    other.write_text("domains:\n  energy: {exact: [ENV_OIL]}\n", encoding="utf-8")
    assert load_theme_map(other).id != a.id


def test_kept_rows_carry_themes_and_progress_counts_every_article(tmp_path: Path) -> None:
    from trading_live_claude.news.gkg_themes import load_theme_map

    tm = load_theme_map(THEME_MAP_PATH)
    body = _zip(_row_with_themes("a", "Nvidia ships", "ENV_OIL;ENV_OIL;TAX_FNCACT"),
                _row_with_themes("b", "Rain in Ohio", "NATURAL_DISASTER_FLOOD"),  # not kept, still counted
                _row_with_themes("c", "Markets", "ECON_INFLATION"))
    bf = Backfill(out_dir=tmp_path, aliases=ALIASES, budget_bytes=10**9, fetch=lambda s: Fetched(200, body),
                  theme_map=tm)
    row, kept = bf.process("20260601000000")
    assert [r["themes"] for r in kept] == [["ENV_OIL", "TAX_FNCACT"]]  # de-duplicated, order kept
    assert row["domain_counts"] == {"conflict": 0, "disaster": 1, "economy": 1, "energy": 1, "military": 0}
    assert row["theme_map"] == tm.id and row["rows"] == 3


def test_transient_fetch_failures_are_retried_and_a_404_is_not(monkeypatch: pytest.MonkeyPatch) -> None:
    import httpx

    from trading_live_claude.news import gkg

    calls: list[str] = []

    def flaky(url: str, **kw: object) -> httpx.Response:
        calls.append(url)
        if len(calls) < 3:
            raise httpx.ReadError("[SSL] record layer failure")
        return httpx.Response(200, content=b"zip")

    monkeypatch.setattr(httpx, "get", flaky)
    waits: list[float] = []
    assert gkg.fetch_file("20261030120000", sleep=waits.append) == Fetched(200, b"zip", "")
    assert len(calls) == 3 and waits == [2.0, 4.0]

    calls.clear()
    monkeypatch.setattr(httpx, "get", lambda url, **kw: calls.append(url) or httpx.Response(404))
    assert gkg.fetch_file("20261030120000", sleep=waits.append).status == 404 and len(calls) == 1

    monkeypatch.setattr(httpx, "get", lambda url, **kw: (_ for _ in ()).throw(httpx.ConnectError("down")))
    out = gkg.fetch_file("20261030120000", sleep=lambda s: None)
    assert out.status == 0 and "ConnectError" in out.error
