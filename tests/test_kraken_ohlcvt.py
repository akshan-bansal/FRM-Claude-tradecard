"""Kraken OHLCVT archive reader: a member read by byte range across split parts, and the REST splice."""
from __future__ import annotations

import importlib.util
import io
import random
import re
import sys
import zipfile
from pathlib import Path

import httpx
import pandas as pd
import respx

ROOT = Path(__file__).resolve().parent.parent
_spec = importlib.util.spec_from_file_location("fetch_kraken_ohlcvt_daily", ROOT / "scripts" / "fetch_kraken_ohlcvt_daily.py")
assert _spec and _spec.loader
mod = importlib.util.module_from_spec(_spec)
sys.modules[_spec.name] = mod
_spec.loader.exec_module(mod)

CSV = b"1700000000,1.0,2.0,0.5,1.5,10,3\n1700086400,1.5,2.5,1.0,2.0,20,4\n"


def _split_zip() -> list[bytes]:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("XBTUSD_1440.csv", CSV)
        z.writestr("XBTUSD_1.csv", random.Random(0).randbytes(4_000_000))   # large, incompressible
    data = buf.getvalue()
    third = len(data) // 3
    return [data[:third], data[third:2 * third], data[2 * third:]]


@respx.mock
def test_a_member_reads_by_range_across_part_boundaries() -> None:
    parts = _split_zip()
    urls = [f"https://example.test/p{i}" for i in range(len(parts))]
    for url, body in zip(urls, parts, strict=True):
        def handler(request: httpx.Request, body: bytes = body) -> httpx.Response:
            m = re.fullmatch(r"bytes=(\d+)-(\d+)", request.headers.get("range", ""))
            if request.method == "HEAD":
                return httpx.Response(200, headers={"content-length": str(len(body))})
            assert m, "every GET must be a range"
            lo, hi = int(m[1]), int(m[2])
            return httpx.Response(206, content=body[lo:hi + 1])
        respx.route(url=url).mock(side_effect=handler)
    with httpx.Client() as client:
        src = mod.RangedParts(client, urls)
        z = zipfile.ZipFile(src)
        assert z.read("XBTUSD_1440.csv") == CSV
        assert src.fetched < sum(map(len, parts))                # the large member was never fetched
    daily = mod.parse_daily(CSV)
    assert list(daily.columns) == ["time", "open", "high", "low", "close", "volume"] and daily["close"].iloc[-1] == 2.0


def test_the_splice_keeps_the_archive_and_adds_only_later_days() -> None:
    days = pd.date_range("2026-09-28", periods=5, freq="D", tz="UTC")
    archive = pd.DataFrame({"time": days[:3], "open": 1.0, "high": 1.0, "low": 1.0, "close": [1.0, 2.0, 3.0], "volume": 1.0})
    recent = pd.DataFrame({"time": days[1:], "open": 9.0, "high": 9.0, "low": 9.0, "close": [2.0, 3.3, 4.0, 5.0], "volume": 9.0})
    out, info = mod.splice(archive, recent)
    assert out["close"].tolist() == [1.0, 2.0, 3.0, 4.0, 5.0]
    assert info["rest_days_added"] == 2 and info["overlap_days"] == 2 and abs(info["overlap_max_close_diff"] - 0.1) < 1e-9
