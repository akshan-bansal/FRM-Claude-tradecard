"""News collector, step 1: parse, map to symbols, journal point-in-time without duplicates.

All payloads are SYNTHETIC fixtures. No network is used.
"""
from __future__ import annotations

import ast
import json
from pathlib import Path

import pytest

from trading_live_claude.news import collector
from trading_live_claude.news.collector import journal, load_seen, map_symbols, parse_feed

ARRIVED = "2026-10-10T14:00:00+00:00"
ALIASES = {"AAPL": ("Apple", "AAPL"), "BTC/USD": ("Bitcoin", "BTC"), "IT": ("IT",)}

RSS = b"""<?xml version="1.0"?>
<rss version="2.0"><channel><title>x</title>
<item><guid>g-1</guid><title>Apple beats earnings estimates</title>
 <link>https://example.test/a</link><pubDate>Fri, 09 Oct 2026 21:00:00 GMT</pubDate></item>
<item><guid>g-2</guid><title>Pineapple prices fall in Ohio</title>
 <link>https://example.test/b</link><pubDate>Fri, 09 Oct 2026 22:00:00 GMT</pubDate></item>
<item><title></title><link>https://example.test/empty</link></item>
</channel></rss>"""

ATOM = b"""<?xml version="1.0" encoding="utf-8"?>
<feed xmlns="http://www.w3.org/2005/Atom">
<entry><id>urn:e:1</id><title>Bitcoin rallies after ETF news</title>
 <link href="https://example.test/c"/><updated>2026-10-09T23:30:00Z</updated></entry>
</feed>"""


def test_rss_items_are_parsed_and_empty_titles_are_skipped() -> None:
    hs = parse_feed(RSS, source="wire", arrived_utc=ARRIVED, aliases=ALIASES)
    assert [h.guid for h in hs] == ["g-1", "g-2"]
    assert hs[0].symbols == ("AAPL",)


def test_atom_entries_are_parsed() -> None:
    (h,) = parse_feed(ATOM, source="edgar", arrived_utc=ARRIVED, aliases=ALIASES)
    assert h.guid == "urn:e:1" and h.link == "https://example.test/c"
    assert h.symbols == ("BTC/USD",)


def test_arrival_time_is_the_caller_supplied_time_never_the_published_time() -> None:
    (h,) = parse_feed(ATOM, source="edgar", arrived_utc=ARRIVED, aliases=ALIASES)
    assert h.arrived_utc == ARRIVED
    assert h.published_utc == "2026-10-09T23:30:00+00:00"  # kept for information only


def test_arrival_must_be_zoned() -> None:
    with pytest.raises(ValueError, match="timezone"):
        parse_feed(RSS, source="wire", arrived_utc="2026-10-10T14:00:00")


def test_whole_word_matching_avoids_substring_hits() -> None:
    assert map_symbols("Pineapple prices fall", {"AAPL": ("Apple",)}) == ()
    assert map_symbols("apple shares rise", {"AAPL": ("Apple",)}) == ("AAPL",)


def test_short_all_caps_tickers_are_case_sensitive() -> None:
    assert map_symbols("it is done", {"IT": ("IT",)}) == ()
    assert map_symbols("IT services grow", {"IT": ("IT",)}) == ("IT",)


def test_journal_writes_each_story_once_across_reruns(tmp_path: Path) -> None:
    path = tmp_path / "news.jsonl"
    hs = parse_feed(RSS, source="wire", arrived_utc=ARRIVED, aliases=ALIASES)
    assert len(journal(hs, path)) == 2
    assert journal(hs, path) == []
    assert len(path.read_text(encoding="utf-8").splitlines()) == 2


def test_a_syndicated_copy_under_another_id_is_not_counted_twice(tmp_path: Path) -> None:
    path = tmp_path / "news.jsonl"
    original = parse_feed(RSS, source="wire", arrived_utc=ARRIVED, aliases=ALIASES)
    copy = parse_feed(
        b"""<rss version="2.0"><channel><item><guid>other-9</guid>
        <title>Apple beats earnings estimates!</title><link>https://mirror.test/x</link></item></channel></rss>""",
        source="mirror", arrived_utc="2026-10-10T14:05:00+00:00", aliases=ALIASES)
    journal(original, path)
    assert journal(copy, path) == []  # same normalised title: one story, not two


def _one(title: str, guid: str) -> bytes:
    return (f'<rss version="2.0"><channel><item><guid>{guid}</guid><title>{title}</title>'
            f"<link>https://x.test/{guid}</link></item></channel></rss>").encode()


def test_a_reused_headline_outside_the_window_is_a_new_story(tmp_path: Path) -> None:
    path = tmp_path / "news.jsonl"
    first = parse_feed(_one("Fed holds rates", "a"), source="fed", arrived_utc=ARRIVED)
    later = parse_feed(_one("Fed holds rates", "b"), source="fed", arrived_utc="2026-10-20T14:00:00+00:00")
    soon = parse_feed(_one("Fed holds rates", "c"), source="wire", arrived_utc="2026-10-10T20:00:00+00:00")
    assert len(journal(first, path)) == 1
    assert journal(soon, path) == []
    assert len(journal(later, path)) == 1


def test_templated_titles_are_exempt_and_kept_by_id(tmp_path: Path) -> None:
    """EDGAR titles repeat for every filing by a company; two filings are two rows."""
    path = tmp_path / "news.jsonl"
    t = "8-K - NVIDIA CORP (0001045810) (Filer)"
    a = parse_feed(_one(t, "acc-1"), source="edgar_8k", arrived_utc=ARRIVED)
    b = parse_feed(_one(t, "acc-2"), source="edgar_8k", arrived_utc="2026-10-10T15:00:00+00:00")
    assert len(journal(a, path, title_dedupe_exempt={"edgar_8k"})) == 1
    assert len(journal(b, path, title_dedupe_exempt={"edgar_8k"})) == 1
    assert journal(b, path, title_dedupe_exempt={"edgar_8k"}) == []  # the id still de-duplicates


RDF = b"""<?xml version="1.0"?>
<rdf:RDF xmlns:rdf="http://www.w3.org/1999/02/22-rdf-syntax-ns#" xmlns="http://purl.org/rss/1.0/"
         xmlns:dc="http://purl.org/dc/elements/1.1/">
<channel rdf:about="https://bank.test/feed"><title>press</title></channel>
<item rdf:about="https://bank.test/pr/1"><title>Bank holds policy rate</title>
 <link>https://bank.test/pr/1</link><dc:date>2026-10-09T14:00:00-04:00</dc:date></item>
</rdf:RDF>"""


def test_rss_1_0_rdf_items_are_parsed() -> None:
    (h,) = parse_feed(RDF, source="boc", arrived_utc=ARRIVED)
    assert h.guid == "https://bank.test/pr/1" and h.title == "Bank holds policy rate"
    assert h.published_utc == "2026-10-09T18:00:00+00:00"


def test_feed_tags_are_kept_apart_from_title_mentions(tmp_path: Path) -> None:
    hs = parse_feed(RSS, source="yahoo", arrived_utc=ARRIVED, aliases=ALIASES, feed_symbols=["XIC.TO"])
    assert hs[0].symbols == ("AAPL",) and hs[0].feed_symbols == ("XIC.TO",)
    assert hs[1].symbols == () and hs[1].feed_symbols == ("XIC.TO",)  # tagged, not mentioned
    path = tmp_path / "news.jsonl"
    journal(hs, path)
    row = json.loads(path.read_text(encoding="utf-8").splitlines()[1])
    assert row["symbols"] == [] and row["feed_symbols"] == ["XIC.TO"]


def test_each_row_carries_its_arrival_and_a_content_hash(tmp_path: Path) -> None:
    path = tmp_path / "news.jsonl"
    journal(parse_feed(RSS, source="wire", arrived_utc=ARRIVED, aliases=ALIASES), path)
    row = json.loads(path.read_text(encoding="utf-8").splitlines()[0])
    assert row["arrived_utc"] == ARRIVED and len(row["content_hash"]) == 16
    assert row["symbols"] == ["AAPL"]


def test_a_corrupt_journal_line_is_ignored_not_fatal(tmp_path: Path) -> None:
    path = tmp_path / "news.jsonl"
    path.write_text("not json\n" + json.dumps({"source": "wire", "guid": "g-9", "title_key": "x"}) + "\n",
                    encoding="utf-8")
    assert load_seen(path) == {("wire", "g-9")}


def test_missing_journal_means_nothing_seen(tmp_path: Path) -> None:
    assert load_seen(tmp_path / "absent.jsonl") == set()


def test_the_collector_is_outside_the_trading_path() -> None:
    """Nothing that moves money may import the news collector, and it may not import the trading path."""
    tree = ast.parse(Path(collector.__file__).read_text(encoding="utf-8"))
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names |= {a.name.split(".")[0] for a in node.names}
        elif isinstance(node, ast.ImportFrom):
            names.add((node.module or "").split(".")[0] if node.level == 0 else (node.module or ""))
    forbidden = {"execution", "risk", "brokers", "strategies", "monitor", "daemon", "tune", "cli"}
    assert not (names & forbidden), names & forbidden
