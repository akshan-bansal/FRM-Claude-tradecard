"""The meter catalogue: what each meter measures, in which unit, from which journal, and when it goes stale."""
from __future__ import annotations

import csv
import hashlib
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class Meter:
    id: str
    section: str
    name: str
    unit: str
    lo: float | None  # a dial is drawn only when both bounds are set
    hi: float | None
    stale_after_s: int
    source: str
    definition: str


def _num(raw: str) -> float | None:
    return float(raw) if raw.strip() else None


def load_catalog(path: Path) -> tuple[str, list[Meter]]:
    """(catalogue id, meters). The id is a hash of the file, so a snapshot records which catalogue it was
    taken against. Duplicate ids and inverted dial bounds are refused."""
    text = path.read_text(encoding="utf-8")
    meters: list[Meter] = []
    for r in csv.DictReader(text.splitlines()):
        m = Meter(id=r["id"].strip(), section=r["section"].strip(), name=r["name"].strip(), unit=r["unit"].strip(),
                  lo=_num(r["lo"]), hi=_num(r["hi"]), stale_after_s=int(r["stale_after_s"]),
                  source=r["source"].strip(), definition=r["definition"].strip())
        if m.lo is not None and m.hi is not None and m.lo >= m.hi:
            raise ValueError(f"{m.id}: dial bounds lo={m.lo} hi={m.hi} are inverted")
        meters.append(m)
    ids = [m.id for m in meters]
    if len(ids) != len(set(ids)):
        raise ValueError(f"duplicate meter ids: {sorted({i for i in ids if ids.count(i) > 1})}")
    return hashlib.sha256(text.encode()).hexdigest()[:12], meters
