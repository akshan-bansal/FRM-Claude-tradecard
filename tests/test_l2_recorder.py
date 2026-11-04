"""The L2 recorder samples without losing order flow, and reads back only what was known by a cutoff."""
from __future__ import annotations

import ast
import gzip
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from trading_live_claude.microstructure import l2_recorder
from trading_live_claude.microstructure.kraken_l2 import BookUpdate
from trading_live_claude.microstructure.l2_recorder import L2Recorder, read_samples
from trading_live_claude.microstructure.orderbook import LimitOrderBook, OrderBookLevel

REPO = Path(__file__).resolve().parents[1]
T0 = datetime(2026, 6, 1, 23, 59, 50, tzinfo=UTC)


def _update(symbol: str = "BTC/USD", *, ofi: float = 1.0, ok: bool = True, levels: int = 3) -> BookUpdate:
    book = LimitOrderBook([OrderBookLevel(100.0 - i, 2.0) for i in range(levels)],
                          [OrderBookLevel(101.0 + i, 3.0) for i in range(levels)])
    return BookUpdate(symbol=symbol, book=book, microprice=book.microprice, imbalance=book.imbalance(), ofi=ofi,
                      checksum_ok=ok)


class _Clock:
    def __init__(self, start: datetime = T0) -> None:
        self.now = start

    def __call__(self) -> datetime:
        return self.now


def _recorder(root: Path, clock: _Clock, **kw: object) -> L2Recorder:
    return L2Recorder("kraken", root=root, clock=clock, sample_seconds=5.0, **kw)  # type: ignore[arg-type]


def _all(root: Path, symbol: str = "BTC/USD") -> list[dict[str, object]]:
    return list(read_samples("kraken", symbol, before=T0 + timedelta(days=30), root=root))


def test_it_samples_on_the_interval_and_sums_order_flow_between(tmp_path: Path) -> None:
    clock = _Clock()
    rec = _recorder(tmp_path, clock)
    rec.on_update(_update(ofi=1.0))                       # the first update is always sampled
    for _ in range(4):
        clock.now += timedelta(seconds=1)
        rec.on_update(_update(ofi=2.0))
    clock.now += timedelta(seconds=1)
    rec.on_update(_update(ofi=3.0))                       # 5s on: the next sample covers all five updates
    rec.close()
    rows = _all(tmp_path)
    assert [r["ofi"] for r in rows] == [1.0, 11.0]
    assert [r["updates"] for r in rows] == [1, 5]


def test_a_failed_checksum_in_the_interval_marks_the_sample(tmp_path: Path) -> None:
    clock = _Clock()
    rec = _recorder(tmp_path, clock)
    rec.on_update(_update())
    clock.now += timedelta(seconds=1)
    rec.on_update(_update(ok=False))
    clock.now += timedelta(seconds=5)
    rec.on_update(_update())
    clock.now += timedelta(seconds=5)
    rec.on_update(_update())
    rec.close()
    assert [r["checksum_ok"] for r in _all(tmp_path)] == [True, False, True]


def test_each_symbol_is_kept_apart_and_a_slash_makes_no_folder(tmp_path: Path) -> None:
    rec = _recorder(tmp_path, _Clock())
    rec.on_update(_update("BTC/USD"))
    rec.on_update(_update("ETH/USD"))
    rec.close()
    assert sorted(p.name for p in (tmp_path / "kraken").iterdir()) == ["BTC_USD", "ETH_USD"]
    assert _all(tmp_path, "ETH/USD")[0]["symbol"] == "ETH/USD"


def test_samples_split_across_days(tmp_path: Path) -> None:
    clock = _Clock()
    rec = _recorder(tmp_path, clock)
    rec.on_update(_update())
    clock.now += timedelta(seconds=20)
    rec.on_update(_update())
    rec.close()
    assert sorted(p.name for p in (tmp_path / "kraken" / "BTC_USD").iterdir()) == ["2026-06-01.jsonl.gz",
                                                                                 "2026-06-02.jsonl.gz"]
    assert len(_all(tmp_path)) == 2


def test_depth_is_capped(tmp_path: Path) -> None:
    rec = L2Recorder("kraken", root=tmp_path, depth=2, clock=_Clock())
    rec.on_update(_update(levels=5))
    rec.close()
    row = _all(tmp_path)[0]
    assert len(row["bids"]) == 2 and len(row["asks"]) == 2    # type: ignore[arg-type]


def test_it_flushes_without_waiting_for_close(tmp_path: Path) -> None:
    clock = _Clock()
    rec = _recorder(tmp_path, clock, flush_rows=2)
    for _ in range(2):
        rec.on_update(_update())
        clock.now += timedelta(seconds=5)
    assert len(_all(tmp_path)) == 2


def test_a_read_stops_strictly_before_the_cutoff(tmp_path: Path) -> None:
    clock = _Clock()
    rec = _recorder(tmp_path, clock)
    stamps = []
    for _ in range(4):
        stamps.append(clock.now)
        rec.on_update(_update())
        clock.now += timedelta(seconds=5)
    rec.close()
    got = list(read_samples("kraken", "BTC/USD", before=stamps[2], root=tmp_path))
    assert [datetime.fromisoformat(str(r["recv_utc"])) for r in got] == stamps[:2]
    with pytest.raises(ValueError):
        list(read_samples("kraken", "BTC/USD", before=datetime(2026, 6, 1), root=tmp_path))  # naive


def test_a_truncated_file_gives_what_it_holds_and_no_error(tmp_path: Path) -> None:
    clock = _Clock(datetime(2026, 6, 1, 12, 0, tzinfo=UTC))
    rec = _recorder(tmp_path, clock)
    rec.on_update(_update())
    rec.close()
    path = tmp_path / "kraken" / "BTC_USD" / "2026-06-01.jsonl.gz"
    clean = path.read_bytes()
    path.write_bytes(clean + gzip.compress(b'{"recv_utc": "2026')[:12])
    assert len(_all(tmp_path)) == 1


def test_an_unknown_symbol_reads_as_empty(tmp_path: Path) -> None:
    assert _all(tmp_path, "NOPE/USD") == []


def test_the_recorder_stays_off_the_order_path() -> None:
    for path in (Path(l2_recorder.__file__), REPO / "scripts" / "record_l2.py"):
        imported: set[str] = set()
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if isinstance(node, ast.ImportFrom) and node.module:
                imported.add(node.module)
            elif isinstance(node, ast.Import):
                imported.update(a.name for a in node.names)
        forbidden = ("execution", "brokers", "monitor", "risk")
        assert not [m for m in imported if any(f".{f}" in m for f in forbidden)], path.name
