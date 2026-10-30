"""News collector runner: scheduling, backoff, SEC contact scoping, tag merging, graph edges.

Fetches are faked and the clock is injected. No network is used.
"""
from __future__ import annotations

import ast
import json
from pathlib import Path

import pytest

from trading_live_claude.intel.graph import load_edges
from trading_live_claude.news import runner
from trading_live_claude.news.runner import Collector, FeedSpec, FetchResult, NewsConfig, load_config

SEC_UA = "tester contact@example.test"
GENERIC_UA = "generic-ua"


def _rss(*items: tuple[str, str]) -> bytes:
    body = "".join(f"<item><guid>{g}</guid><title>{t}</title><link>https://x.test/{g}</link></item>"
                   for g, t in items)
    return f'<rss version="2.0"><channel>{body}</channel></rss>'.encode()


class Clock:
    def __init__(self) -> None:
        self.t = 1_760_000_000.0

    def __call__(self) -> float:
        return self.t

    def sleep(self, s: float) -> None:
        self.t += s


def _cfg(tmp_path: Path, feeds: tuple[FeedSpec, ...], *, graph: bool = True) -> NewsConfig:
    return NewsConfig(journal=tmp_path / "news.jsonl",
                      graph_journal=(tmp_path / "graph.jsonl") if graph else None,
                      health=tmp_path / "health.json", user_agent=GENERIC_UA, sec_user_agent=SEC_UA,
                      feeds=feeds, aliases={"NVDA": ("Nvidia",), "BTC/USD": ("Bitcoin",)},
                      host_spacing_s={"slow.test": 5.0})


def test_a_pass_journals_and_writes_mention_edges_and_a_heartbeat(tmp_path: Path) -> None:
    clock = Clock()
    feeds = (FeedSpec("wire", "https://wire.test/rss", 15),)
    col = Collector(_cfg(tmp_path, feeds), clock=clock, sleep=clock.sleep,
                    fetch=lambda url, ua: FetchResult(200, _rss(("1", "Nvidia ships a chip"), ("2", "Rain"))))
    written = col.run_pass()
    assert [h.guid for h in written] == ["1", "2"]
    edges = load_edges(tmp_path / "graph.jsonl")
    assert [(e.predicate, e.object, e.meta["via"]) for e in edges] == [("mentions", ("symbol", "NVDA"), "title")]
    assert edges[0].as_of == written[0].arrived_utc  # stamped when seen, not when published
    beat = json.loads((tmp_path / "news_passes.jsonl").read_text(encoding="utf-8"))
    assert beat["written"] == 2 and beat["feeds_ok"] == 1


def test_a_feed_is_not_refetched_before_it_is_due(tmp_path: Path) -> None:
    clock, calls = Clock(), []
    feeds = (FeedSpec("wire", "https://wire.test/rss", 15),)
    col = Collector(_cfg(tmp_path, feeds), clock=clock, sleep=clock.sleep,
                    fetch=lambda url, ua: calls.append(url) or FetchResult(200, _rss()))
    col.run_pass()
    clock.t += 14 * 60
    assert col.due() == []
    clock.t += 61
    assert col.due() == list(feeds)
    assert len(calls) == 1


def test_a_429_backs_off_and_honours_retry_after_while_other_feeds_continue(tmp_path: Path) -> None:
    clock = Clock()
    feeds = (FeedSpec("gdelt", "https://gdelt.test/a", 30), FeedSpec("wire", "https://wire.test/rss", 15))

    def fetch(url: str, ua: str) -> FetchResult:
        if "gdelt" in url:
            return FetchResult(429, retry_after_s=7200, error="slow down")
        return FetchResult(200, _rss(("1", "Bitcoin climbs")))

    col = Collector(_cfg(tmp_path, feeds), clock=clock, sleep=clock.sleep, fetch=fetch)
    assert len(col.run_pass()) == 1
    st = col.state[feeds[0].key]
    assert st.fails == 1 and st.last_status == 429
    assert st.next_due - clock.t >= 7200  # Retry-After beats the 60-minute exponential step
    health = json.loads((tmp_path / "health.json").read_text(encoding="utf-8"))
    assert health["feeds"][feeds[0].key]["last_error"] == "slow down"


def test_malformed_xml_counts_as_a_failure_not_a_crash(tmp_path: Path) -> None:
    clock = Clock()
    feeds = (FeedSpec("bad", "https://bad.test/rss", 15),)
    col = Collector(_cfg(tmp_path, feeds), clock=clock, sleep=clock.sleep,
                    fetch=lambda url, ua: FetchResult(200, b"<html>not a feed"))
    assert col.run_pass() == []
    assert col.state[feeds[0].key].last_error.startswith("parse:")


def test_the_sec_contact_goes_to_sec_gov_only(tmp_path: Path) -> None:
    clock, seen = Clock(), {}
    feeds = (FeedSpec("edgar_8k", "https://www.sec.gov/cgi-bin/x", 10, sec=True),
             FeedSpec("wire", "https://wire.test/rss", 15))
    col = Collector(_cfg(tmp_path, feeds), clock=clock, sleep=clock.sleep,
                    fetch=lambda url, ua: seen.setdefault(url, ua) and FetchResult(200, _rss()))
    col.run_pass()
    assert seen == {"https://www.sec.gov/cgi-bin/x": SEC_UA, "https://wire.test/rss": GENERIC_UA}


def test_requests_to_one_host_are_spaced(tmp_path: Path) -> None:
    clock, times = Clock(), []
    feeds = (FeedSpec("g", "https://slow.test/a", 30), FeedSpec("g", "https://slow.test/b", 30))
    col = Collector(_cfg(tmp_path, feeds), clock=clock, sleep=clock.sleep,
                    fetch=lambda url, ua: times.append(clock.t) or FetchResult(200, _rss()))
    col.run_pass()
    assert times[1] - times[0] >= 5.0


def test_tags_from_several_per_ticker_feeds_merge_into_one_row(tmp_path: Path) -> None:
    clock = Clock()
    feeds = (FeedSpec("yahoo", "https://y.test/NVDA", 30, feed_symbols=("NVDA",)),
             FeedSpec("yahoo", "https://y.test/BTC", 30, feed_symbols=("BTC/USD",)))
    col = Collector(_cfg(tmp_path, feeds), clock=clock, sleep=clock.sleep,
                    fetch=lambda url, ua: FetchResult(200, _rss(("story-1", "Markets wobble"))))
    (h,) = col.run_pass()
    assert h.symbols == () and h.feed_symbols == ("BTC/USD", "NVDA")
    edges = load_edges(tmp_path / "graph.jsonl")
    assert sorted(e.object[1] for e in edges) == ["BTC/USD", "NVDA"]
    assert {e.meta["via"] for e in edges} == {"feed"}


def test_no_graph_journal_means_no_edges(tmp_path: Path) -> None:
    clock = Clock()
    feeds = (FeedSpec("wire", "https://wire.test/rss", 15),)
    col = Collector(_cfg(tmp_path, feeds, graph=False), clock=clock, sleep=clock.sleep,
                    fetch=lambda url, ua: FetchResult(200, _rss(("1", "Nvidia ships a chip"))))
    assert len(col.run_pass()) == 1
    assert not (tmp_path / "graph.jsonl").exists()


def test_the_stop_file_ends_the_loop_and_is_consumed(tmp_path: Path) -> None:
    clock = Clock()
    stop = tmp_path / "STOP_news"
    stop.touch()
    col = Collector(_cfg(tmp_path, (FeedSpec("wire", "https://wire.test/rss", 15),)),
                    clock=clock, sleep=clock.sleep, fetch=lambda url, ua: FetchResult(200, _rss()))
    col.run_forever(stop_file=stop)
    assert not stop.exists()


def test_config_expands_per_symbol_feeds_and_drops_sec_without_a_contact(tmp_path: Path) -> None:
    (tmp_path / "config").mkdir()
    cfg_path = tmp_path / "config" / "news.yaml"
    cfg_path.write_text("""
feeds:
  - {name: edgar_8k, url: "https://www.sec.gov/cgi-bin/browse-edgar?x=1", title_dedupe: false}
  - {name: wire, url: "https://wire.test/rss", every_min: 5}
  - {name: off, url: "https://off.test/rss", enabled: false}
per_symbol_feeds:
  - {name: yahoo, id_key: yahoo, url: "https://y.test/rss?s={ticker}"}
symbols:
  GC: {aliases: [gold futures], yahoo: GC=F}
  POL/USD: {aliases: [Polygon]}
""", encoding="utf-8")
    cfg = load_config(cfg_path)
    assert [f.name for f in cfg.feeds] == ["wire", "yahoo"]  # SEC dropped: no contact; "off" disabled
    assert cfg.feeds[1].url == "https://y.test/rss?s=GC%3DF" and cfg.feeds[1].feed_symbols == ("GC",)
    assert cfg.aliases["POL/USD"] == ("Polygon",)
    assert cfg.journal == tmp_path / "state" / "news.jsonl"

    local = tmp_path / "config" / "news.local.yaml"
    local.write_text('sec_user_agent: "x y@z.test"\n', encoding="utf-8")
    cfg = load_config(cfg_path, local)
    assert cfg.feeds[0].sec and cfg.title_dedupe_exempt == frozenset({"edgar_8k"})


def test_a_symbol_yaml_reads_as_a_boolean_is_refused(tmp_path: Path) -> None:
    (tmp_path / "config").mkdir()
    cfg_path = tmp_path / "config" / "news.yaml"
    cfg_path.write_text("symbols:\n  ON: {aliases: [onsemi], yahoo: ON}\n", encoding="utf-8")
    with pytest.raises(ValueError, match="quote them"):
        load_config(cfg_path)


def test_region_fills_from_the_symbol_else_the_template(tmp_path: Path) -> None:
    (tmp_path / "config").mkdir()
    cfg_path = tmp_path / "config" / "news.yaml"
    cfg_path.write_text("""
per_symbol_feeds:
  - {name: yahoo, id_key: yahoo, region: US, url: "https://y.test/rss?s={ticker}&region={region}"}
symbols:
  XIC.TO: {yahoo: XIC.TO, region: CA}
  NVDA: {yahoo: NVDA}
""", encoding="utf-8")
    urls = [f.url for f in load_config(cfg_path).feeds]
    assert urls == ["https://y.test/rss?s=XIC.TO&region=CA", "https://y.test/rss?s=NVDA&region=US"]


def test_the_shipped_config_loads() -> None:
    cfg = load_config(Path(__file__).resolve().parent.parent / "config" / "news_sources.yaml")
    assert any(f.name == "coindesk" for f in cfg.feeds)
    assert not any(f.name == "gdelt" for f in cfg.feeds)  # the throttled API is off


def test_the_runner_is_outside_the_trading_path() -> None:
    tree = ast.parse(Path(runner.__file__).read_text(encoding="utf-8"))
    mods: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module:
            mods.add(node.module)
        elif isinstance(node, ast.Import):
            mods |= {a.name for a in node.names}
    forbidden = ("execution", "risk", "brokers", "strategies", "monitor", "daemon", "tune", "cli")
    hits = {m for m in mods for f in forbidden if f".{f}" in f".{m}"}
    assert not hits, hits
