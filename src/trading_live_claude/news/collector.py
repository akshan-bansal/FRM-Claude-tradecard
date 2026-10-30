"""News collector: journal every headline on arrival, point-in-time, with no scoring.

Step 1 of the sentiment plan in NEXT_SESSION.md. It starts the history clock: each headline is
stamped with the moment it was SEEN (``arrived_utc``, supplied by the caller), never with its
published time, which feeds rewrite and backfill. Nothing here reads sentiment or influences a
trade. The module is outside the trading path; a test enforces that.

Sources are pluggable. This step parses RSS 2.0, RSS 1.0 (RDF) and Atom, which covers company,
exchange and central-bank feeds, EDGAR and GDELT's RSS output. Paid feeds are a later decision.

Two symbol fields, never merged: ``symbols`` are the aliases found in the title (the headline names
the instrument), ``feed_symbols`` are what a per-ticker feed was queried for (the vendor tagged it,
which often means only "related"). A scorer decides how much each is worth; the journal keeps both.
"""
from __future__ import annotations

import hashlib
import json
import re
import xml.etree.ElementTree as ET
from collections.abc import Collection, Iterable, Mapping
from dataclasses import asdict, dataclass
from datetime import UTC, datetime, timedelta
from email.utils import parsedate_to_datetime
from pathlib import Path

ATOM = "{http://www.w3.org/2005/Atom}"
RSS1 = "{http://purl.org/rss/1.0/}"
RDF = "{http://www.w3.org/1999/02/22-rdf-syntax-ns#}"
DC = "{http://purl.org/dc/elements/1.1/}"
MAX_TITLE = 300
# A syndicated reprint lands within hours of the original; a later story that happens to share a
# headline ("Fed holds rates") is a new story. Title de-duplication only looks back this far.
TITLE_WINDOW_H = 72.0


@dataclass(frozen=True)
class Headline:
    source: str
    guid: str  # the feed's own id when it has one, else the link, else the title
    title: str
    link: str
    published_utc: str  # as the feed states it; informational only, never used as a timestamp
    arrived_utc: str  # when this collector saw it; the only timestamp that means "known at"
    title_key: str  # normalised title, for catching syndicated copies under different ids
    symbols: tuple[str, ...]  # aliases named in the title
    feed_symbols: tuple[str, ...] = ()  # what a per-ticker feed was queried for; a vendor tag, not a mention


def _clean(text: str | None) -> str:
    return re.sub(r"\s+", " ", (text or "")).strip()[:MAX_TITLE]


def _title_key(title: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", title.lower()).strip()


def _when(raw: str) -> str:
    raw = raw.strip()
    if not raw:
        return ""
    try:
        dt = parsedate_to_datetime(raw)
    except (TypeError, ValueError):
        try:
            dt = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        except ValueError:
            return ""
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt.astimezone(UTC).isoformat()


def map_symbols(text: str, aliases: Mapping[str, Iterable[str]]) -> tuple[str, ...]:
    """Symbols whose alias appears as a whole word or phrase. Case-sensitive for short all-caps
    tickers so "IT" in "it" does not match; a headline with no alias match maps to nothing.

    ``aliases`` maps symbol -> names that refer to it. Callers own this table; it is config, not code.
    """
    hits: set[str] = set()
    for symbol, names in aliases.items():
        for name in names:
            flags = 0 if (name.isupper() and len(name) <= 5) else re.IGNORECASE
            if re.search(rf"(?<![A-Za-z0-9]){re.escape(name)}(?![A-Za-z0-9])", text, flags):
                hits.add(symbol)
                break
    return tuple(sorted(hits))


def parse_feed(payload: bytes, *, source: str, arrived_utc: str,
               aliases: Mapping[str, Iterable[str]] | None = None,
               feed_symbols: Iterable[str] = ()) -> list[Headline]:
    """Headlines in one RSS 2.0, RSS 1.0 or Atom document. Items without a title are skipped, not
    invented. ``feed_symbols`` is stamped on every item as-is: it records what was asked for."""
    if not arrived_utc or datetime.fromisoformat(arrived_utc).tzinfo is None:
        raise ValueError("arrived_utc must be a timezone-aware ISO timestamp supplied by the caller")
    root = ET.fromstring(payload)
    items: list[tuple[str, str, str, str]] = []
    if root.tag.endswith("rss") or root.find("channel") is not None:
        for it in root.iter("item"):
            items.append((_clean(it.findtext("guid")), _clean(it.findtext("link")),
                          _clean(it.findtext("title")), _when(it.findtext("pubDate") or "")))
    elif root.tag == f"{RDF}RDF":
        for it in root.iter(f"{RSS1}item"):
            items.append((_clean(it.get(f"{RDF}about")), _clean(it.findtext(f"{RSS1}link")),
                          _clean(it.findtext(f"{RSS1}title")), _when(it.findtext(f"{DC}date") or "")))
    else:
        for e in root.iter(f"{ATOM}entry"):
            link_el = e.find(f"{ATOM}link")
            link = _clean(link_el.get("href") if link_el is not None else "")
            items.append((_clean(e.findtext(f"{ATOM}id")), link,
                          _clean(e.findtext(f"{ATOM}title")),
                          _when(e.findtext(f"{ATOM}updated") or e.findtext(f"{ATOM}published") or "")))
    tagged = tuple(sorted(set(feed_symbols)))
    out: list[Headline] = []
    for guid, link, title, published in items:
        if not title:
            continue
        ident = guid or link or title
        out.append(Headline(source=source, guid=ident, title=title, link=link, published_utc=published,
                            arrived_utc=arrived_utc, title_key=_title_key(title),
                            symbols=map_symbols(title, aliases or {}), feed_symbols=tagged))
    return out


def load_seen(path: Path) -> set[tuple[str, str]]:
    """(source, guid) pairs already journaled. A missing or unreadable journal means nothing seen yet."""
    if not path.exists():
        return set()
    seen: set[tuple[str, str]] = set()
    for line in path.read_text(encoding="utf-8").splitlines():
        try:
            row = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(row, dict) and "source" in row and "guid" in row:
            seen.add((str(row["source"]), str(row["guid"])))
    return seen


def content_hash(h: Headline) -> str:
    return hashlib.sha256(f"{h.title}|{h.link}".encode()).hexdigest()[:16]


def _recent_titles(path: Path) -> dict[str, datetime]:
    """Normalised title -> latest arrival already journaled. Rows without a usable arrival are skipped."""
    latest: dict[str, datetime] = {}
    if not path.exists():
        return latest
    for line in path.read_text(encoding="utf-8").splitlines():
        try:
            row = json.loads(line)
            key = str(row.get("title_key", ""))
            at = datetime.fromisoformat(str(row["arrived_utc"]))
        except (json.JSONDecodeError, AttributeError, KeyError, TypeError, ValueError):
            continue
        if key and (key not in latest or at > latest[key]):
            latest[key] = at
    return latest


def journal(headlines: Iterable[Headline], path: Path, *,
            title_window_h: float = TITLE_WINDOW_H,
            title_dedupe_exempt: Collection[str] = ()) -> list[Headline]:
    """Append headlines not yet journaled. Returns the ones written.

    A headline is new when its (source, guid) is unseen AND no headline with the same normalised
    title arrived from ANY source within ``title_window_h`` before it, so a syndicated reprint under
    a different id is one story, not two, while a later story that reuses a headline is still kept.
    Sources in ``title_dedupe_exempt`` skip the title test: their titles are templates (EDGAR's
    "8-K - APPLE INC (0000320193) (Filer)" repeats for every filing) and only the id is meaningful.
    Each row also carries a content hash for later audit.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    seen = load_seen(path)
    titles = _recent_titles(path)
    window = timedelta(hours=title_window_h)
    written: list[Headline] = []
    with path.open("a", encoding="utf-8") as fh:
        for h in headlines:
            key = (h.source, h.guid)
            if key in seen:
                continue
            at = datetime.fromisoformat(h.arrived_utc)
            prior = titles.get(h.title_key)
            if (h.source not in title_dedupe_exempt and h.title_key and prior is not None
                    and timedelta(0) <= at - prior <= window):
                continue
            row = asdict(h) | {
                "symbols": list(h.symbols),
                "feed_symbols": list(h.feed_symbols),
                "content_hash": content_hash(h),
            }
            fh.write(json.dumps(row) + "\n")
            seen.add(key)
            if h.title_key:
                titles[h.title_key] = at
            written.append(h)
    return written


def fetch(url: str, *, user_agent: str, timeout: float = 15.0) -> bytes:
    """One polite GET. Not called by tests; the scheduler decides when it runs."""
    import httpx

    r = httpx.get(url, headers={"User-Agent": user_agent}, timeout=timeout, follow_redirects=True)
    r.raise_for_status()
    return r.content
