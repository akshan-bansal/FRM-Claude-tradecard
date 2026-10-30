"""GDELT GKG 2.1 backfill and forward follower for the news lens.

Each GKG file is 15 minutes of GDELT's global crawl (~1,250 articles, ~5 MB zipped). Files are fetched
into memory, filtered to articles that refer to a basket symbol, and discarded; only matching rows are
kept, in monthly files under ``state/gdelt_gkg/``. Progress is checkpointed per file so a stopped run
resumes, and a byte budget caps the total transferred. Raw files are not rate-limited, unlike the DOC API.

Point-in-time: a row's ``arrived_utc`` is the file's own timestamp, the moment GDELT published the batch,
which is no earlier than the article was known. ``backfill: true`` marks every row, so a lens can tell
GDELT's crawl clock from the live collector's arrival clock and never mixes the two silently.

Two symbol fields, never merged, as in the live journal: ``symbols`` are aliases in the page title (the
article is about the instrument), ``org_symbols`` are aliases in GDELT's extracted organisation and name
lists (the article refers to it somewhere, often in passing). GDELT's document tone is kept as a raw
field; nothing here scores sentiment.

A rolling window (``prune_to_span``) keeps the newest N bytes' worth of source files (user 2026-10-12: 75 GB).

Themes (2026-10-30, for the graph adapter's bridge): each kept row carries its raw V1 theme codes, and with
a ``ThemeMap`` each progress row carries per-domain article counts over EVERY article in the file, not only
the kept ones, plus the map id: a global, point-in-time domain-intensity series on GDELT's own clock. Rows
and files fetched before this carry neither, and readers treat them as unknown.
"""
from __future__ import annotations

import io
import json
import re
import zipfile
from collections.abc import Callable, Iterator, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from html import unescape
from pathlib import Path

from trading_live_claude.news.collector import MAX_TITLE, _title_key, map_symbols
from trading_live_claude.news.gkg_themes import ThemeMap

BASE_URL = "https://data.gdeltproject.org/gdeltv2/{stamp}.gkg.csv.zip"
LAST_UPDATE_URL = "https://data.gdeltproject.org/gdeltv2/lastupdate.txt"
STEP = timedelta(minutes=15)
N_COLUMNS = 27
THEMES_COL = 7  # V1Themes: semicolon-separated theme codes
MAX_THEMES = 80
_TITLE = re.compile(r"<PAGE_TITLE>(.*?)</PAGE_TITLE>", re.S)
_OFFSET = re.compile(r",\d+")  # V2.1AllNames carries "name,char_offset;" pairs


def stamps(newest: datetime, oldest: datetime) -> Iterator[str]:
    """GKG file stamps, newest first, on the 15-minute grid. Both ends inclusive."""
    t = newest.astimezone(UTC).replace(second=0, microsecond=0)
    t -= timedelta(minutes=t.minute % 15)
    while t >= oldest:
        yield t.strftime("%Y%m%d%H%M%S")
        t -= STEP


def stamp_iso(stamp: str) -> str:
    return datetime.strptime(stamp, "%Y%m%d%H%M%S").replace(tzinfo=UTC).isoformat()


@dataclass(frozen=True)
class GkgFile:
    total: int  # articles in the file
    kept: list[dict[str, object]]  # articles naming a basket symbol
    domain_counts: dict[str, int] | None  # articles per stress domain over ALL articles; None without a map


def _themes(field: str) -> list[str]:
    """Distinct theme codes in order of first appearance, capped."""
    seen: dict[str, None] = {}
    for code in field.split(";"):
        if code:
            seen.setdefault(code, None)
    return list(seen)[:MAX_THEMES]


def parse_gkg(payload: bytes, *, stamp: str,
              aliases: Mapping[str, tuple[str, ...]]) -> tuple[int, list[dict[str, object]]]:
    """(rows in file, kept rows) from one zipped GKG file. Rows with no symbol in either field are dropped."""
    f = parse_gkg_file(payload, stamp=stamp, aliases=aliases)
    return f.total, f.kept


def parse_gkg_file(payload: bytes, *, stamp: str, aliases: Mapping[str, tuple[str, ...]],
                   theme_map: ThemeMap | None = None) -> GkgFile:
    """One zipped GKG file: kept rows (with their theme codes) and, given a map, per-domain counts."""
    with zipfile.ZipFile(io.BytesIO(payload)) as z:
        text = z.read(z.namelist()[0]).decode("utf-8", "replace")
    arrived = stamp_iso(stamp)
    total = 0
    kept: list[dict[str, object]] = []
    counts = dict.fromkeys(theme_map.domains, 0) if theme_map is not None else None
    for line in text.splitlines():
        c = line.split("\t")
        if len(c) < N_COLUMNS:
            continue
        total += 1
        themes = _themes(c[THEMES_COL])
        if counts is not None and theme_map is not None:
            for d in theme_map.domains_for(themes):
                counts[d] += 1
        m = _TITLE.search(c[26])
        title = re.sub(r"\s+", " ", unescape(m.group(1))).strip()[:MAX_TITLE] if m else ""
        symbols = map_symbols(title, aliases) if title else ()
        names = " ; ".join([c[13].replace(";", " ; "), _OFFSET.sub("", c[23]).replace(";", " ; ")])
        org_symbols = map_symbols(names, aliases)
        if not symbols and not org_symbols:
            continue
        try:
            tone = float(c[15].split(",")[0])
        except ValueError:
            tone = float("nan")
        kept.append({
            "source": "gdelt_gkg", "guid": c[0], "title": title, "link": c[4], "domain": c[3],
            "published_utc": "", "arrived_utc": arrived, "title_key": _title_key(title),
            "symbols": list(symbols), "org_symbols": list(org_symbols),
            "tone": tone if tone == tone else None, "themes": themes, "backfill": True,
        })
    return GkgFile(total, kept, counts)


def latest_stamp(*, timeout: float = 20.0) -> str | None:
    """The newest published GKG stamp, from GDELT's own index; None if it cannot be read."""
    import httpx

    try:
        r = httpx.get(LAST_UPDATE_URL, timeout=timeout, follow_redirects=True)
    except httpx.HTTPError:
        return None
    m = re.search(r"/(\d{14})\.gkg\.csv\.zip", r.text) if r.status_code == 200 else None
    return m.group(1) if m else None


@dataclass(frozen=True)
class Fetched:
    status: int
    body: bytes = b""
    error: str = ""


RETRIES = 3  # attempts per file for transient failures


def fetch_file(stamp: str, *, timeout: float = 60.0, sleep: Callable[[float], None] | None = None) -> Fetched:
    """One GKG file. Network errors and 5xx are retried with backoff (2 s, then 4 s): the 2026-10-30 e2e saw 7 of
    13 downloads fail with transient SSL "record layer" errors, and a file left failed stays a hole until the
    next run. A 404 is final (the file does not exist) and is returned at once."""
    import time

    import httpx

    wait = sleep or time.sleep
    last = Fetched(0, error="not attempted")
    for attempt in range(RETRIES):
        try:
            r = httpx.get(BASE_URL.format(stamp=stamp), timeout=timeout, follow_redirects=True)
        except httpx.HTTPError as exc:
            last = Fetched(0, error=f"{type(exc).__name__}: {exc}"[:200])
        else:
            if r.status_code < 500:
                break
            last = Fetched(r.status_code, error=f"HTTP {r.status_code}")
        if attempt < RETRIES - 1:
            wait(2.0 * 2 ** attempt)
    else:
        return last
    return Fetched(r.status_code, r.content if r.status_code == 200 else b"",
                   "" if r.status_code == 200 else f"HTTP {r.status_code}")


@dataclass
class Backfill:
    """Checkpointed, budgeted backfill. ``fetch`` is injectable for tests."""

    out_dir: Path
    aliases: Mapping[str, tuple[str, ...]]
    budget_bytes: int
    fetch: Callable[[str], Fetched] = fetch_file
    # The forward follower runs beside the backfill: its own progress and row files, so the two
    # processes never append to the same file and the backfill's byte budget is not spent going forward.
    prefix: str = ""
    # With a map, every progress row also records per-domain counts over all articles, and the map id.
    theme_map: ThemeMap | None = None

    @property
    def progress_path(self) -> Path:
        return self.out_dir / f"{self.prefix}progress.jsonl"

    def done(self) -> tuple[set[str], int]:
        """Stamps already settled (fetched or confirmed missing) and bytes transferred so far."""
        seen: set[str] = set()
        spent = 0
        if self.progress_path.exists():
            for line in self.progress_path.read_text(encoding="utf-8").splitlines():
                try:
                    row = json.loads(line)
                except json.JSONDecodeError:
                    continue
                spent += int(row.get("bytes", 0))
                if row.get("status") in ("ok", "missing"):
                    seen.add(str(row["stamp"]))
        return seen, spent

    def process(self, stamp: str) -> tuple[dict[str, object], list[dict[str, object]]]:
        """Fetch and filter one file; thread-safe, writes nothing. Returns (progress row, kept rows)."""
        res = self.fetch(stamp)
        if res.status == 404:
            return {"stamp": stamp, "status": "missing", "bytes": 0}, []
        if res.status != 200:
            return {"stamp": stamp, "status": "error", "bytes": len(res.body), "error": res.error}, []
        try:
            f = parse_gkg_file(res.body, stamp=stamp, aliases=self.aliases, theme_map=self.theme_map)
        except (zipfile.BadZipFile, KeyError, IndexError) as exc:
            return {"stamp": stamp, "status": "error", "bytes": len(res.body), "error": f"parse: {exc}"[:200]}, []
        row: dict[str, object] = {"stamp": stamp, "status": "ok", "bytes": len(res.body), "rows": f.total,
                                  "kept": len(f.kept), "title_kept": sum(1 for r in f.kept if r["symbols"])}
        if f.domain_counts is not None and self.theme_map is not None:
            row |= {"domain_counts": f.domain_counts, "theme_map": self.theme_map.id}
        return row, f.kept

    def settled_everywhere(self) -> set[str]:
        """Stamps settled by the backfill OR the follower, so neither re-fetches what the other has."""
        out: set[str] = set()
        for p in self.out_dir.glob("*progress.jsonl"):
            for line in p.read_text(encoding="utf-8").splitlines():
                try:
                    row = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if row.get("status") in ("ok", "missing"):
                    out.add(str(row["stamp"]))
        return out

    def commit(self, row: Mapping[str, object], kept: list[dict[str, object]]) -> None:
        """Write kept rows, then the progress row, from ONE thread. Rows first: a crash between the two
        re-fetches the file on resume (the rows are then written twice, de-duplicated later by guid),
        never marks a file done whose rows were lost."""
        self.out_dir.mkdir(parents=True, exist_ok=True)
        if kept:
            stamp = str(row["stamp"])
            with (self.out_dir / f"{self.prefix}{stamp[:4]}-{stamp[4:6]}.jsonl").open("a", encoding="utf-8") as fh:
                fh.writelines(json.dumps(r) + "\n" for r in kept)
        with self.progress_path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(dict(row)) + "\n")


# --------------------------------------------------------------------------- rolling window

WINDOW_FILE = "window.json"


def window_start(out_dir: Path) -> str | None:
    """Oldest stamp still inside the rolling window, or None before the first prune."""
    p = out_dir / WINDOW_FILE
    if not p.exists():
        return None
    try:
        return str(json.loads(p.read_text(encoding="utf-8"))["start"])
    except (json.JSONDecodeError, KeyError):
        return None


def prune_to_span(out_dir: Path, span_bytes: int) -> dict[str, object]:
    """Keep the newest ``span_bytes`` worth of fetched files; drop everything older.

    The window is measured in source bytes (what the files cost to fetch), newest first, across the
    backfill and the follower. Rows AND progress records older than the window go together: a progress
    record without its rows would read as a covered day with no news. ``window.json`` records the new
    start so a resumed backfill does not fetch the pruned files again. Run it only while no other
    process is appending (the follower checks the backfill is idle first).
    """
    rows: list[tuple[str, int]] = []
    for p in out_dir.glob("*progress.jsonl"):
        for line in p.read_text(encoding="utf-8").splitlines():
            try:
                r = json.loads(line)
            except json.JSONDecodeError:
                continue
            if r.get("status") == "ok":
                rows.append((str(r["stamp"]), int(r.get("bytes", 0))))
    rows.sort(reverse=True)
    spent, start = 0, None
    for stamp, size in rows:
        if spent + size > span_bytes:
            break
        spent += size
        start = stamp
    if start is None or start == (rows[-1][0] if rows else None):
        return {"pruned": False, "start": start, "span_bytes": spent}

    cut = stamp_iso(start)
    dropped_rows = 0
    for f in out_dir.glob("*.jsonl"):
        if f.name.endswith("progress.jsonl"):
            keep = [ln for ln in f.read_text(encoding="utf-8").splitlines()
                    if ln.strip() and str(json.loads(ln).get("stamp", "")) >= start]
        else:
            lines = f.read_text(encoding="utf-8").splitlines()
            keep = [ln for ln in lines if ln.strip() and str(json.loads(ln).get("arrived_utc", "")) >= cut]
            dropped_rows += len(lines) - len(keep)
        if not keep and not f.name.endswith("progress.jsonl"):
            f.unlink()
            continue
        tmp = f.with_suffix(".tmp")
        tmp.write_text("".join(ln + "\n" for ln in keep), encoding="utf-8")
        tmp.replace(f)
    (out_dir / WINDOW_FILE).write_text(json.dumps({"start": start, "span_bytes": spent}), encoding="utf-8")
    return {"pruned": True, "start": start, "span_bytes": spent, "rows_dropped": dropped_rows}
