"""Daily bars for the crypto sleeve from Kraken's published OHLCVT archive, without the whole archive.

    python scripts/fetch_kraken_ohlcvt_daily.py --out data/cache/kraken_ohlcvt
    python scripts/fetch_kraken_ohlcvt_daily.py --list XBT        # names in the archive matching XBT

Kraken publishes every pair's candles as one ~9.5 GB ZIP split into five parts
(support article 360047124832). The server honours byte ranges, so this reads the ZIP's central
directory from the end of the last part and then fetches only each pair's ``_1440.csv`` (daily)
member: a few MB instead of 9.5 GB. The archive ends on a fixed date (``MANIFEST.json``), so the
days after it come from the public REST OHLC feed (its last 720 candles), spliced on the first day
the archive does not hold. Overlapping days are compared and the largest close difference reported.

Writes ``<PAIR>_daily.parquet`` (``time, open, high, low, close, volume``, UTC midnight, oldest first)
plus ``manifest.json`` with the source, archive end, splice day and overlap check for each pair.
Raw CSVs are kept under ``<out>/raw``. Public data only: no account, no orders, nothing in state/.
"""
from __future__ import annotations

import argparse
import io
import json
import sys
import time
import zipfile
from datetime import UTC, datetime
from pathlib import Path

import httpx
import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from trading_live_claude.analysis.universe import CRYPTO_SLEEVE  # noqa: E402
from trading_live_claude.data.kraken_ohlc import kraken_ohlc  # noqa: E402

BASE = "https://assets.kraken.com/marketing/institutions/"
PARTS = [f"Kraken_OHLCVT_Full.zip.part{i:02d}" for i in range(5)]
BLOCK = 1 << 20
COLS = ["time", "open", "high", "low", "close", "volume", "trades"]
# Archive member stems that differ from the wire pair the desk trades (renames keep their old file).
ALIASES: dict[str, tuple[str, ...]] = {"POLUSD": ("POLUSD", "MATICUSD"), "XBTUSD": ("XBTUSD", "XXBTZUSD"),
                                       "ETHUSD": ("ETHUSD", "XETHZUSD"), "XRPUSD": ("XRPUSD", "XXRPZUSD"),
                                       "XLMUSD": ("XLMUSD", "XXLMZUSD"), "XMRUSD": ("XMRUSD", "XXMRZUSD"),
                                       "ZECUSD": ("ZECUSD", "XZECZUSD")}


class RangedParts(io.RawIOBase):
    """The concatenated parts as one seekable, read-only file, fetched by HTTP range on demand."""

    def __init__(self, client: httpx.Client, urls: list[str]) -> None:
        self.client, self.urls = client, urls
        self.sizes = [int(client.head(u).headers["content-length"]) for u in urls]
        self.starts = [sum(self.sizes[:i]) for i in range(len(urls))]
        self.size, self.pos, self.fetched = sum(self.sizes), 0, 0
        self._cache: dict[int, bytes] = {}

    def readable(self) -> bool:
        return True

    def seekable(self) -> bool:
        return True

    def tell(self) -> int:
        return self.pos

    def seek(self, offset: int, whence: int = 0) -> int:
        self.pos = {0: offset, 1: self.pos + offset, 2: self.size + offset}[whence]
        return self.pos

    def _range(self, start: int, end: int) -> bytes:          # [start, end) across part boundaries
        out = bytearray()
        for i, (s, n) in enumerate(zip(self.starts, self.sizes, strict=True)):
            lo, hi = max(start, s), min(end, s + n)
            if lo >= hi:
                continue
            for attempt in range(5):
                try:
                    r = self.client.get(self.urls[i], headers={"Range": f"bytes={lo - s}-{hi - s - 1}"})
                    r.raise_for_status()
                    if r.status_code != 206 or len(r.content) != hi - lo:
                        raise ValueError(f"range not honoured: {r.status_code}, {len(r.content)} bytes")
                    break
                except (httpx.HTTPError, ValueError):
                    if attempt == 4:
                        raise
                    time.sleep(2 ** attempt)
            out += r.content
            self.fetched += hi - lo
        return bytes(out)

    def read(self, n: int = -1) -> bytes:
        end = self.size if n is None or n < 0 else min(self.size, self.pos + n)
        if end <= self.pos:
            return b""
        if end - self.pos > BLOCK:                               # big reads (members, directory) go straight
            data = self._range(self.pos, end)
        else:                                                    # small header reads share 1 MiB blocks
            data = bytearray()
            p = self.pos
            while p < end:
                b = p // BLOCK
                if b not in self._cache:
                    self._cache[b] = self._range(b * BLOCK, min(self.size, (b + 1) * BLOCK))
                chunk = self._cache[b]
                data += chunk[p - b * BLOCK:min(end, (b + 1) * BLOCK) - b * BLOCK]
                p = (b + 1) * BLOCK
            data = bytes(data)
        self.pos = end
        return data

    def readinto(self, buf: bytearray) -> int:   # type: ignore[override]
        data = self.read(len(buf))
        buf[:len(data)] = data
        return len(data)


def parse_daily(raw: bytes) -> pd.DataFrame:
    df = pd.read_csv(io.BytesIO(raw), header=None, names=COLS)
    df["time"] = pd.to_datetime(df["time"], unit="s", utc=True)
    for c in ("open", "high", "low", "close", "volume"):
        df[c] = df[c].astype(float)
    return df[["time", "open", "high", "low", "close", "volume"]].sort_values("time").reset_index(drop=True)


def splice(archive: pd.DataFrame, recent: pd.DataFrame) -> tuple[pd.DataFrame, dict]:
    """Archive up to its last day, then the REST feed after it. Overlap is checked, not mixed."""
    last = archive["time"].max()
    both = archive.merge(recent, on="time", suffixes=("_a", "_r"))
    diff = float(((both["close_a"] - both["close_r"]).abs() / both["close_a"]).max()) if len(both) else float("nan")
    tail = recent[recent["time"] > last]
    out = pd.concat([archive, tail], ignore_index=True).sort_values("time").reset_index(drop=True)
    gaps = int((out["time"].diff().dt.days > 1).sum())
    return out, {"archive_last": str(last.date()), "rest_days_added": len(tail), "overlap_days": len(both),
                 "overlap_max_close_diff": diff, "gaps_over_1_day": gaps}


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--out", type=Path, default=ROOT / "data" / "cache" / "kraken_ohlcvt")
    ap.add_argument("--list", default=None, help="Print archive member names containing this text, then stop.")
    ap.add_argument("--no-rest", action="store_true", help="Archive only; skip the REST splice.")
    a = ap.parse_args()
    client = httpx.Client(timeout=60, follow_redirects=True, headers={"User-Agent": "FRM-Claude/1.0"})
    src = RangedParts(client, [BASE + p for p in PARTS])
    z = zipfile.ZipFile(src)
    names = z.namelist()
    print(f"[ohlcvt] archive {src.size / 1e9:.2f} GB, {len(names):,} members, directory read with "
          f"{src.fetched / 1e6:.1f} MB", flush=True)
    if a.list is not None:
        for n in names:
            if a.list.upper() in n.upper() and ("1440" in n or "MANIFEST" in n.upper()):
                print(n)
        return 0
    by_stem = {Path(n).name.rsplit("_", 1)[0].upper(): n for n in names if n.endswith("_1440.csv")}
    a.out.mkdir(parents=True, exist_ok=True)
    raw_dir = a.out / "raw"
    raw_dir.mkdir(exist_ok=True)
    manifest: dict = {"source": BASE + "Kraken_OHLCVT_Full.zip.part00..04", "fetched": datetime.now(UTC).isoformat(),
                      "pairs": {}}
    man = next((n for n in names if Path(n).name.upper() == "MANIFEST.JSON"), None)
    if man:
        manifest["archive_manifest"] = json.loads(z.read(man).decode("utf-8"))
    for entry in CRYPTO_SLEEVE.values():
        pair = entry.pair
        member = next((by_stem[s] for s in ALIASES.get(pair, (pair,)) if s in by_stem), None)
        if member is None:
            print(f"[ohlcvt] {pair}: no daily member in the archive", flush=True)
            manifest["pairs"][pair] = {"error": "not in archive"}
            continue
        raw = z.read(member)
        (raw_dir / Path(member).name).write_bytes(raw)
        daily = parse_daily(raw)
        info: dict = {"member": member, "archive_bars": len(daily), "archive_first": str(daily["time"].min().date())}
        if not a.no_rest:
            try:
                recent = kraken_ohlc(pair, interval=1440, client=client)
                daily, extra = splice(daily, recent)
                info.update(extra)
            except Exception as e:                             # archive alone is still worth keeping
                info["rest_error"] = f"{type(e).__name__}: {e}"
            time.sleep(1.5)
        info["bars"] = len(daily)
        info["last"] = str(daily["time"].max().date())
        daily.to_parquet(a.out / f"{pair}_daily.parquet", index=False)
        manifest["pairs"][pair] = info
        print(f"[ohlcvt] {pair}: {info}", flush=True)
    manifest["bytes_downloaded"] = src.fetched
    (a.out / "manifest.json").write_text(json.dumps(manifest, indent=2, default=str), encoding="utf-8")
    print(f"[ohlcvt] done: {src.fetched / 1e6:.1f} MB downloaded in all", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
