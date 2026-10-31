"""Retention copy: the audit record, copied off ``state/`` with a manifest, and verified.

CRA expects records kept six years from the end of the tax year they relate to. ``state/`` lives on
one OneDrive-synced laptop, where a sync conflict can silently fork a file, so the record needs a
second copy somewhere the operator chooses. This writes one snapshot directory per run:

* every *closed* ledger day file (today's is still being written and is left for the next run);
* the anchors file, the journals, the FX cache;
* ``approval.db`` through SQLite's backup API, so a copy taken while a shim runs is consistent;
* ``manifest.json`` with each file's SHA-256, re-read from the copy, not from the source.

Nothing in ``state/`` is moved or deleted. The destination is never chosen here.
"""
from __future__ import annotations

import json
import shutil
import sqlite3
from datetime import UTC, datetime
from hashlib import sha256
from pathlib import Path
from typing import Any

JOURNALS = ("orders.jsonl", "fills.jsonl", "rejected.jsonl", "cancels.jsonl",
            "paper_orders.jsonl", "paper_fills.jsonl", "paper_equity.csv", "baskets.jsonl",
            "ledger_anchors.jsonl")


def _digest(path: Path) -> str:
    h = sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def archive(state_dir: Path, dest_root: Path, *, now: datetime | None = None) -> dict[str, Any]:
    now = now or datetime.now(UTC)
    state_dir, dest_root = Path(state_dir), Path(dest_root)
    if dest_root.resolve().is_relative_to(state_dir.resolve()):
        raise ValueError("the retention copy must live outside state/")
    snap = dest_root / f"audit-{now:%Y%m%dT%H%M%SZ}"
    snap.mkdir(parents=True, exist_ok=False)
    today = now.date().isoformat()
    copied: list[tuple[Path, Path]] = []

    for src in sorted((state_dir / "ledger").glob("*.jsonl")):
        if src.name.startswith(today):
            continue
        dst = snap / "ledger" / src.name
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, dst)
        copied.append((src, dst))
    for name in JOURNALS:
        src = state_dir / name
        if src.exists():
            shutil.copy2(src, snap / name)
            copied.append((src, snap / name))
    for src in sorted((state_dir / "fx").glob("*.json")):
        dst = snap / "fx" / src.name
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, dst)
        copied.append((src, dst))
    db = state_dir / "approval.db"
    if db.exists():
        src_con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
        dst_con = sqlite3.connect(snap / "approval.db")
        try:
            src_con.backup(dst_con)
        finally:
            dst_con.close()
            src_con.close()
        copied.append((db, snap / "approval.db"))

    files = {str(dst.relative_to(snap)).replace("\\", "/"): _digest(dst) for _src, dst in copied}
    mismatched = [str(dst.relative_to(snap)) for src, dst in copied
                  if src.name != "approval.db" and _digest(src) != files[
                      str(dst.relative_to(snap)).replace("\\", "/")]]
    manifest = {"created_at": now.isoformat(), "source": str(state_dir.resolve()),
                "files": files, "skipped_open_day": today,
                "copy_mismatches": mismatched}
    (snap / "manifest.json").write_text(json.dumps(manifest, indent=1, sort_keys=True),
                                        encoding="utf-8")
    return {"snapshot": str(snap), "files": len(files), "copy_mismatches": mismatched}
