"""News collector runner: poll the configured feeds on their own schedules and journal what arrives.

Step 1 of the sentiment plan (NEXT_SESSION.md): it starts the point-in-time history and reads no
sentiment. Scoring is a later layer that reads ``state/news.jsonl``; nothing here feeds a trade.

* Each feed has its own interval. A failure (HTTP error, 429, timeout, unparseable body) backs that
  feed off exponentially, honouring ``Retry-After``; the others carry on.
* Requests to one host are spaced (SEC caps fair access).
* The SEC contact User-Agent is sent to sec.gov only. Every other host gets the generic one.
* Per-ticker feeds tag a headline with the symbol queried. The same story often comes back under
  several tickers in one pass; those tags are merged before journaling so none is dropped.
* Journaled headlines also become ``event --mentions--> symbol`` edges in the graph journal.
* Every pass writes a heartbeat row, so a reader can tell a quiet hour from an hour the collector was down.
"""
from __future__ import annotations

import json
import time
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.parse import quote, urlsplit

import yaml

from trading_live_claude.intel.graph import Edge, append_edges
from trading_live_claude.logging_setup import get_logger
from trading_live_claude.news.collector import Headline, content_hash, journal, parse_feed

log = get_logger(__name__)

SEC_HOSTS = frozenset({"www.sec.gov", "sec.gov", "data.sec.gov", "efts.sec.gov"})
DEFAULT_SPACING_S = 1.0
MAX_BACKOFF_S = 6 * 3600.0


@dataclass(frozen=True)
class FeedSpec:
    name: str  # the journal's ``source``
    url: str
    every_min: float
    title_dedupe: bool = True
    sec: bool = False  # needs the SEC contact User-Agent; skipped when none is configured
    feed_symbols: tuple[str, ...] = ()

    @property
    def key(self) -> str:
        """Unique per feed: per-ticker feeds share a source name but not a URL."""
        return f"{self.name}|{self.url}"


@dataclass(frozen=True)
class NewsConfig:
    journal: Path
    graph_journal: Path | None
    health: Path
    user_agent: str
    sec_user_agent: str
    feeds: tuple[FeedSpec, ...]
    aliases: Mapping[str, tuple[str, ...]]
    host_spacing_s: Mapping[str, float] = field(default_factory=dict)

    @property
    def title_dedupe_exempt(self) -> frozenset[str]:
        return frozenset(f.name for f in self.feeds if not f.title_dedupe)


def load_config(path: Path, local_path: Path | None = None) -> NewsConfig:
    """Read the committed source list and, if present, the machine-local overrides beside it.

    The local file is gitignored and holds what should not be committed (the SEC contact). Any key
    in it replaces the committed value. SEC feeds without a contact are dropped with a warning,
    because SEC refuses anonymous clients and a refused feed would only back off forever.
    """
    raw: dict[str, Any] = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    if local_path is not None and local_path.exists():
        raw |= yaml.safe_load(local_path.read_text(encoding="utf-8")) or {}

    symbols: dict[str, dict[str, Any]] = raw.get("symbols") or {}
    # YAML 1.1 reads bare ON / OFF / YES / NO as booleans, so an unquoted `ON:` (onsemi) arrives as
    # True. Coercing it to "True" would journal a symbol that does not exist; refuse it instead.
    for sym, spec in symbols.items():
        bad = [repr(v) for v in (sym, *((spec or {}).get("aliases") or ()), *(
            v for k, v in (spec or {}).items() if k != "aliases")) if not isinstance(v, str)]
        if bad:
            raise ValueError(f"news config: symbol {sym!r} has non-text values {', '.join(bad)}; "
                             "quote them (YAML reads bare ON/OFF/YES/NO as booleans)")
    aliases = {sym: tuple((spec or {}).get("aliases") or ()) for sym, spec in symbols.items()}
    sec_ua = str(raw.get("sec_user_agent") or "").strip()

    feeds: list[FeedSpec] = []
    for f in raw.get("feeds") or ():
        if not f.get("enabled", True):
            continue
        spec = FeedSpec(name=str(f["name"]), url=str(f["url"]), every_min=float(f.get("every_min", 15)),
                        title_dedupe=bool(f.get("title_dedupe", True)),
                        sec=urlsplit(str(f["url"])).hostname in SEC_HOSTS)
        if spec.sec and not sec_ua:
            log.warning("news.feed_skipped", feed=spec.name, reason="sec_user_agent not set")
            continue
        feeds.append(spec)
    for tpl in raw.get("per_symbol_feeds") or ():
        if not tpl.get("enabled", True):
            continue
        id_key = str(tpl["id_key"])  # which per-symbol field fills {ticker}, e.g. "yahoo"
        for sym, spec in symbols.items():
            ticker = (spec or {}).get(id_key)
            if not ticker:
                continue
            # {region}: the symbol's own `region`, else the template's default (Yahoo returns nothing
            # for a TSX ticker unless asked with region=CA).
            region = (spec or {}).get("region") or tpl.get("region", "")
            url = str(tpl["url"]).format(ticker=quote(ticker), region=quote(str(region)))
            feeds.append(FeedSpec(name=str(tpl["name"]), url=url,
                                  every_min=float(tpl.get("every_min", 30)),
                                  title_dedupe=bool(tpl.get("title_dedupe", True)), feed_symbols=(sym,)))

    root = path.resolve().parent.parent  # config/ sits at the repo root

    def _p(key: str, default: str) -> Path:
        p = Path(str(raw.get(key) or default))
        return p if p.is_absolute() else root / p

    graph = raw.get("graph_journal", "state/intel_graph.jsonl")
    return NewsConfig(
        journal=_p("journal", "state/news.jsonl"),
        graph_journal=None if graph in (None, "", False) else _p("graph_journal", str(graph)),
        health=_p("health", "state/news_health.json"),
        user_agent=str(raw.get("user_agent") or "FRM-Claude-news/0.1"),
        sec_user_agent=sec_ua,
        feeds=tuple(feeds),
        aliases=aliases,
        host_spacing_s={str(k): float(v) for k, v in (raw.get("host_spacing_s") or {}).items()},
    )


# --------------------------------------------------------------------------- fetching


@dataclass(frozen=True)
class FetchResult:
    status: int
    body: bytes = b""
    retry_after_s: float | None = None
    error: str = ""


Fetcher = Callable[[str, str], FetchResult]  # (url, user_agent) -> result


def http_fetch(url: str, user_agent: str, *, timeout: float = 20.0) -> FetchResult:
    """One GET. Network faults come back as status 0 with the error, never as an exception."""
    import httpx

    try:
        r = httpx.get(url, headers={"User-Agent": user_agent, "Accept-Encoding": "gzip, deflate"},
                      timeout=timeout, follow_redirects=True)
    except httpx.HTTPError as exc:
        return FetchResult(status=0, error=f"{type(exc).__name__}: {exc}"[:200])
    retry: float | None = None
    ra = r.headers.get("Retry-After", "")
    if ra.strip().isdigit():
        retry = float(ra)
    return FetchResult(status=r.status_code, body=r.content if r.status_code == 200 else b"",
                       retry_after_s=retry, error="" if r.status_code == 200 else r.text[:200])


@dataclass
class FeedState:
    next_due: float = 0.0  # epoch seconds
    fails: int = 0
    last_ok_utc: str = ""
    last_error: str = ""
    last_status: int = 0
    items_last: int = 0
    written_total: int = 0


@dataclass
class Collector:
    """Holds per-feed schedule state across passes. Clock, fetcher and sleeper are injectable."""

    cfg: NewsConfig
    fetch: Fetcher = http_fetch
    clock: Callable[[], float] = time.time
    sleep: Callable[[float], None] = time.sleep
    state: dict[str, FeedState] = field(default_factory=dict)
    _last_hit: dict[str, float] = field(default_factory=dict)

    def _ua_for(self, feed: FeedSpec) -> str:
        return self.cfg.sec_user_agent if feed.sec else self.cfg.user_agent

    def _space(self, url: str) -> None:
        host = urlsplit(url).hostname or ""
        gap = self.cfg.host_spacing_s.get(host, DEFAULT_SPACING_S)
        wait = self._last_hit.get(host, -1e18) + gap - self.clock()
        if wait > 0:
            self.sleep(wait)
        self._last_hit[host] = self.clock()

    def due(self) -> list[FeedSpec]:
        now = self.clock()
        return [f for f in self.cfg.feeds if self.state.setdefault(f.key, FeedState()).next_due <= now]

    def seconds_to_next(self) -> float:
        now = self.clock()
        dues = [self.state.setdefault(f.key, FeedState()).next_due for f in self.cfg.feeds]
        return max(0.0, min(dues, default=now + 60) - now)

    def run_pass(self) -> list[Headline]:
        """Fetch every due feed once, journal the new headlines, write their graph edges."""
        got: dict[tuple[str, str], Headline] = {}
        for feed in self.due():
            st = self.state[feed.key]
            self._space(feed.url)
            arrived = datetime.fromtimestamp(self.clock(), UTC).isoformat()
            res = self.fetch(feed.url, self._ua_for(feed))
            st.last_status = res.status
            headlines: list[Headline] | None = None
            if res.status == 200:
                try:
                    headlines = parse_feed(res.body, source=feed.name, arrived_utc=arrived,
                                           aliases=self.cfg.aliases, feed_symbols=feed.feed_symbols)
                except Exception as exc:  # malformed XML is the feed's fault, not a reason to stop
                    res = replace(res, error=f"parse: {type(exc).__name__}: {exc}"[:200])
            if headlines is None:
                st.fails += 1
                st.last_error = res.error or f"HTTP {res.status}"
                backoff = min(feed.every_min * 60 * 2 ** st.fails, MAX_BACKOFF_S)
                st.next_due = self.clock() + max(backoff, res.retry_after_s or 0.0)
                log.warning("news.feed_failed", feed=feed.name, url=feed.url, status=res.status,
                            error=st.last_error, retry_in_s=round(st.next_due - self.clock()))
                continue
            st.fails, st.last_error, st.items_last, st.last_ok_utc = 0, "", len(headlines), arrived
            st.next_due = self.clock() + feed.every_min * 60
            for h in headlines:
                key = (h.source, h.guid)
                prev = got.get(key)
                got[key] = h if prev is None else replace(
                    prev, feed_symbols=tuple(sorted(set(prev.feed_symbols) | set(h.feed_symbols))))

        ordered = sorted(got.values(), key=lambda h: h.arrived_utc)
        written = journal(ordered, self.cfg.journal, title_dedupe_exempt=self.cfg.title_dedupe_exempt)
        for h in written:
            for feed in self.cfg.feeds:
                if feed.name == h.source and (not feed.feed_symbols or set(feed.feed_symbols) & set(h.feed_symbols)):
                    self.state[feed.key].written_total += 1
                    break
        if self.cfg.graph_journal is not None:
            edges = list(mention_edges(written))
            if edges:
                append_edges(edges, self.cfg.graph_journal)
        self.write_health()
        self.record_pass(len(got), len(written))
        log.info("news.pass", fetched=len(got), written=len(written),
                 mapped=sum(1 for h in written if h.symbols or h.feed_symbols))
        return written

    @property
    def passes_path(self) -> Path:
        return self.cfg.journal.with_name(self.cfg.journal.stem + "_passes.jsonl")

    def record_pass(self, fetched: int, written: int) -> None:
        """Heartbeat, one row per pass: lets a reader tell a quiet hour from an hour the collector was down."""
        ok = sum(1 for s in self.state.values() if s.fails == 0 and s.last_ok_utc)
        with self.passes_path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps({"ts": datetime.fromtimestamp(self.clock(), UTC).isoformat(),
                                 "fetched": fetched, "written": written,
                                 "feeds_ok": ok, "feeds_failing": len(self.state) - ok}) + "\n")

    def write_health(self) -> None:
        rows = {f.key: {"source": f.name, "url": f.url, **self.state.setdefault(f.key, FeedState()).__dict__}
                for f in self.cfg.feeds}
        self.cfg.health.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.cfg.health.with_suffix(".tmp")
        tmp.write_text(json.dumps({"as_of": datetime.now(UTC).isoformat(), "feeds": rows}, indent=1),
                       encoding="utf-8")
        tmp.replace(self.cfg.health)

    def run_forever(self, *, stop_file: Path, max_passes: int | None = None) -> None:
        """Poll until ``stop_file`` appears (it is consumed) or ``max_passes`` is reached."""
        passes = 0
        while max_passes is None or passes < max_passes:
            if stop_file.exists():
                stop_file.unlink(missing_ok=True)
                log.info("news.stopped", reason="stop file")
                return
            if self.due():
                self.run_pass()
                passes += 1
            else:
                self.sleep(min(self.seconds_to_next(), 30.0))


def mention_edges(headlines: Iterable[Headline]) -> Iterable[Edge]:
    """One ``event --mentions--> symbol`` edge per symbol. A title mention outranks a feed tag."""
    for h in headlines:
        event = ("event", f"news:{h.source}:{content_hash(h)}")
        for sym in h.symbols:
            yield Edge(subject=event, predicate="mentions", object=("symbol", sym), weight=1.0,
                       as_of=h.arrived_utc, meta={"source": h.source, "via": "title"})
        for sym in h.feed_symbols:
            if sym not in h.symbols:
                yield Edge(subject=event, predicate="mentions", object=("symbol", sym), weight=1.0,
                           as_of=h.arrived_utc, meta={"source": h.source, "via": "feed"})


__all__ = ["Collector", "FeedSpec", "FetchResult", "NewsConfig", "http_fetch", "load_config",
           "mention_edges"]
