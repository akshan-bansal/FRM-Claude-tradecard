"""Evidence Atlas: take a snapshot of the journals and render the page.

    python scripts/atlas.py                                   # snapshot state/ and render reports/atlas.html
    python scripts/atlas.py --state-dir <dir> --output <html> # another state folder (e.g. an e2e run)
    python scripts/atlas.py --snapshot-only --snapshot-out snapshot.json

The snapshot is validated before it is drawn; a reading without a source, a scope or a plausible timestamp
rejects the whole file. Nothing is written to the state folder.
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import UTC, datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from trading_live_claude.atlas import (
    SnapshotError,
    load_catalog,
    render,
    take_snapshot,
    validate_snapshot,
)

ROOT = Path(__file__).resolve().parent.parent


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--state-dir", type=Path, default=ROOT / "state")
    ap.add_argument("--output", type=Path, default=ROOT / "reports" / "atlas.html")
    ap.add_argument("--snapshot-out", type=Path, default=None, help="also write the snapshot JSON here")
    ap.add_argument("--snapshot-only", action="store_true")
    a = ap.parse_args()

    cat_id, meters = load_catalog(ROOT / "config" / "meter_catalog.csv")
    now = datetime.now(UTC)
    snap = take_snapshot(a.state_dir, ROOT, meters, cat_id, now=now)
    try:
        validate_snapshot(snap, meters, cat_id, now=now)
    except SnapshotError as exc:
        print(f"[atlas] {exc}", file=sys.stderr)
        return 1
    if a.snapshot_out:
        a.snapshot_out.parent.mkdir(parents=True, exist_ok=True)
        a.snapshot_out.write_text(json.dumps(snap, indent=1), encoding="utf-8")
    print(f"[atlas] {len(snap['readings'])} readings for {len(meters)} meters from {a.state_dir}"
          + (f"; extractor errors: {snap['extractor_errors']}" if snap["extractor_errors"] else ""))
    if not a.snapshot_only:
        a.output.parent.mkdir(parents=True, exist_ok=True)
        a.output.write_text(render(meters, cat_id, snap), encoding="utf-8")
        print(f"[atlas] -> {a.output}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
