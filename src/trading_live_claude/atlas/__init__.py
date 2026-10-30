"""Evidence Atlas: every meter the system defines, and readings taken only from the journals.

The catalogue (``config/meter_catalog.csv``) states what the code defines. A snapshot (``take_snapshot``)
reads ``state/`` and produces a reading only where a journal supports one; a meter with no data gets no
reading, never a substituted value. ``validate_snapshot`` rejects a whole file if any reading lacks a
source, a scope or a plausible timestamp. ``render`` draws the page: no reading means no needle, and a
reading older than its meter's ``stale_after_s`` turns amber, re-checked every minute in the page.

Read-only by construction: nothing here writes to ``state/`` or is imported by the trading path.
"""
from trading_live_claude.atlas.catalog import Meter, load_catalog
from trading_live_claude.atlas.render import render
from trading_live_claude.atlas.snapshot import SnapshotError, take_snapshot, validate_snapshot

__all__ = ["Meter", "SnapshotError", "load_catalog", "render", "take_snapshot", "validate_snapshot"]
