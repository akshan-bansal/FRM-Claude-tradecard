"""Append-only, hash-chained event ledger (``docs/compliance/AUDIT_SCOPE.md`` section 5.1).

Ported from the FRM-Claude ledger branch (``feat/multi-scoring-attention-map-wq7f42``, 2026-09-24),
where it was ``AUDIT_LEDGER_SCOPE.md`` phase 3, then extended here for the gaps that scope lists:
broker errors, cancels, the kill-switch, broker-reported executions, reconciliation breaks,
anchors and corrections.

One row per state transition, written to ``state/ledger/<date>.<stream>.jsonl``. Each row carries
the hash of the row before it, so an edit, an insertion, a deletion or a reordering anywhere but
the tail is detectable by recomputing the chain.

**Chain granularity is per stream, deliberately.** This desk runs several paper books at once (a
Questrade equity book, a Kraken crypto book, an IB futures book), each its own OS process. A single
shared chain would need cross-process locking on every append, and a crashed writer holding the
lock would stall trading; worse, two processes appending to one chain interleave and produce a file
that fails verification for no security-relevant reason. So each writer owns a stream and its own
chain, identified in the filename and in every row. Verification is per stream, and streams are
independently meaningful: the question "was this book's record tampered with" is answerable, while
"did these two books interleave in this exact order" is not one the ledger promises to answer.

A writer that is not one long-lived process (the kill-switch, tripped from the CLI or from a
running book) opens its stream with ``shared=True``: each append then takes a lock file, re-reads
the chain tail and writes under the lock, so two processes cannot fork the chain.

**What the chain does and does not prove.** It makes edits to history detectable by anyone holding
a later row (or a copy of the file). It does *not* make the file immutable — an attacker who can
rewrite every row can recompute the whole chain. That is what the daily anchor
(:mod:`.anchor`, a signed git tag off this machine) is for. WORM storage is not implemented.

**Failure policy.** By default a ledger write failure is logged loudly and does not stop trading:
an audit writer that halts a live book is a new failure mode, and a paper desk would rather lose a
row than a session. Construct with ``strict=True`` to invert that — every append then raises, and
trading stops rather than proceeding unrecorded. A real regulated deployment would want strict.
"""
from __future__ import annotations

import json
import os
import socket
import threading
import time
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from hashlib import sha256
from pathlib import Path
from typing import Any, Literal

from ..logging_setup import get_logger

log = get_logger(__name__)

LEDGER_SCHEMA_VERSION = "1.0.0"

# The event vocabulary. A typo in an event name is caught at the call site rather than becoming a
# new event type silently. Names are never redefined: a change of meaning is a new name.
LedgerEvent = Literal[
    # session
    "SESSION_STARTED", "SESSION_STOPPED",
    "SESSION_PLAN",            # every setting the session runs on, and the fingerprint to compare by
    # signal -> gate
    "STRATEGY_SIGNAL", "SIGNAL_SUPPRESSED", "SIZED",
    "FORCED_EXIT",             # the monitor closing a position on Router.check_forced_exits
    "RISK_CHECK", "RISK_TRIMMED", "RISK_REJECTED",
    "KILL_SWITCH_TRIPPED", "KILL_SWITCH_CLEARED",
    # card approval
    "INTENT_CREATED", "INTENT_QUEUED", "INTENT_RELEASED", "INTENT_SENT", "INTENT_DISPLAYED",
    "APPROVED", "REJECTED", "EXPIRED", "SIGNED",
    # broker
    "BROKER_SUBMITTED", "FILLED", "PARTIAL", "BROKER_REJECTED",
    "BROKER_ERROR",            # dispatch raised something other than a rejection: state unknown
    "CANCEL_REQUESTED", "CANCELLED", "CANCEL_FAILED",
    "EXECUTION_REPORTED",      # pulled back from the broker, not taken from place_order's return
    "RECON_BREAK",             # an execution with no intent, or an intent with no execution
    "POSITION_FLATTENED", "FUTURES_ROLLED",
    # cards and evidence
    "CARD_REGISTERED", "CARD_REVOKED",
    "EXTERNAL_REFERENCE",      # a terms version, a registration check, an adviser note, by hash
    "ANCHORED",                # a day's Merkle root was written off this machine
    "CORRECTION",              # refers to an earlier (stream, seq); rows are never edited
]
LEDGER_EVENTS: frozenset[str] = frozenset(LedgerEvent.__args__)  # type: ignore[attr-defined]

# Envelope keys excluded from a row's own hash: `row_hash` cannot cover itself.
_HASH_EXCLUDED = ("row_hash",)

GENESIS = "0" * 64

_LOCK_WAIT_S = 5.0      # a shared-stream append waits this long for another writer
_STALE_LOCK_S = 30.0    # a lock older than this was left by a writer that died holding it


class ChainBreak(RuntimeError):
    """A ledger stream failed verification. Carries the offending row's sequence number."""

    def __init__(self, message: str, *, seq: int | None = None) -> None:
        super().__init__(message)
        self.seq = seq


def canonical_json(obj: Any) -> bytes:
    """Deterministic JSON bytes: sorted keys, no incidental whitespace, UTF-8.

    Two writers (and a later verifier) must produce identical bytes for identical content or every
    hash is meaningless — the same discipline ``execution.approval.canonical_bytes`` applies to the
    card's signed payload, for the same reason.
    """
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False,
                      default=str).encode("utf-8")


def _hash(data: bytes) -> str:
    return sha256(data).hexdigest()


def row_hash(row: dict[str, Any]) -> str:
    """Hash of an envelope, excluding the field that stores the hash itself."""
    return _hash(canonical_json({k: v for k, v in row.items() if k not in _HASH_EXCLUDED}))


def read_stream(path: Path) -> list[dict[str, Any]]:
    """Parse one ledger file. A torn final line (crash mid-write) is dropped, not fatal."""
    rows: list[dict[str, Any]] = []
    if not path.exists():
        return rows
    for i, line in enumerate(path.read_text(encoding="utf-8").splitlines()):
        if not line.strip():
            continue
        try:
            rows.append(json.loads(line))
        except json.JSONDecodeError:
            # Only the last line can legitimately be torn: fsync-per-row means anything earlier
            # was durable and complete, so a mid-file parse error is corruption worth reporting.
            log.error("ledger.unparseable_row", path=str(path), line_no=i + 1)
    return rows


def verify_chain(rows: list[dict[str, Any]], *, expect_prev: str = GENESIS) -> tuple[bool, str]:
    """Recompute the chain. Returns ``(ok, reason)``; ``reason`` names the first break.

    ``expect_prev`` is the hash the first row should chain to — ``GENESIS`` for the start of a
    stream, or the previous file's last ``row_hash`` when verifying a stream that rolled over.
    """
    prev = expect_prev
    expected_seq: int | None = None
    for row in rows:
        seq = row.get("seq")
        if expected_seq is not None and seq != expected_seq:
            return False, f"sequence gap: expected {expected_seq}, found {seq}"
        if row.get("prev_hash") != prev:
            return False, f"broken link at seq {seq}: prev_hash does not match the preceding row"
        payload = row.get("payload")
        if payload is not None and row.get("payload_hash") != _hash(canonical_json(payload)):
            return False, f"payload of row {seq} does not match its payload_hash"
        recomputed = row_hash(row)
        if row.get("row_hash") != recomputed:
            return False, f"row {seq} was modified after it was written"
        prev = recomputed
        expected_seq = (seq if isinstance(seq, int) else 0) + 1
    return True, "ok"


class Ledger:
    """Append-only writer for one stream. Thread-safe; one instance per process per stream."""

    def __init__(
        self,
        ledger_dir: Path | str,
        *,
        stream: str,
        user_id: str = "operator",
        device_id: str | None = None,
        mode: str = "paper",
        session_id: str | None = None,
        strict: bool = False,
        fsync: bool = True,
        shared: bool = False,
    ) -> None:
        self.dir = Path(ledger_dir)
        self.dir.mkdir(parents=True, exist_ok=True)
        self.stream = stream
        self.user_id = user_id
        self.device_id = device_id or f"host:{socket.gethostname()}"
        self.mode = mode
        self.session_id = session_id
        self.strict = strict
        self.shared = shared
        self._fsync = fsync
        self._lock = threading.Lock()
        self._seq, self._prev_hash = self._resume()

    # -- placement ---------------------------------------------------------- #

    def path_for(self, when: datetime) -> Path:
        return self.dir / f"{when:%Y-%m-%d}.{self.stream}.jsonl"

    def _existing_files(self) -> list[Path]:
        return sorted(self.dir.glob(f"*.{self.stream}.jsonl"))

    def _resume(self) -> tuple[int, str]:
        """Continue this stream's chain across restarts and across day rollover."""
        for path in reversed(self._existing_files()):
            rows = read_stream(path)
            if rows:
                last = rows[-1]
                return int(last.get("seq", 0)) + 1, str(last.get("row_hash", GENESIS))
        return 0, GENESIS

    # -- writing ------------------------------------------------------------ #

    def append(
        self,
        event: LedgerEvent | str,
        payload: dict[str, Any] | None = None,
        *,
        intent_id: str | None = None,
        broker_order_id: int | str | None = None,
        signature: str | None = None,
        signing_key_id: str | None = None,
        strategy_id: str | None = None,
        strategy_version: str | None = None,
        risk_check_version: str | None = None,
        execution: dict[str, Any] | None = None,
        device_id: str | None = None,
        at: datetime | None = None,
    ) -> dict[str, Any] | None:
        """Append one event. Returns the written row, or ``None`` if the write failed non-strict."""
        if event not in LEDGER_EVENTS:
            raise ValueError(f"unknown ledger event {event!r}; add it to LedgerEvent first")
        body = dict(payload or {})
        now = at or datetime.now(UTC)
        with self._lock, self._stream_lock():
            if self.shared:     # another process may have appended since this one last did
                self._seq, self._prev_hash = self._resume()
            row: dict[str, Any] = {
                "schema": LEDGER_SCHEMA_VERSION,
                "seq": self._seq,
                "ts": now.isoformat(),
                "stream": self.stream,
                "event": event,
                "mode": self.mode,
                "user_id": self.user_id,
                "device_id": device_id or self.device_id,
                "session_id": self.session_id,
                "intent_id": intent_id,
                "broker_order_id": broker_order_id,
                "strategy_id": strategy_id,
                "strategy_version": strategy_version,     # phase 4 fills these
                "risk_check_version": risk_check_version,
                "execution": execution,
                "signature": signature,                   # phase 5 wires the card's signature here
                "signing_key_id": signing_key_id,
                "payload": body,
                "payload_hash": _hash(canonical_json(body)),
                "prev_hash": self._prev_hash,
            }
            row["row_hash"] = row_hash(row)
            try:
                self._write(row, now)
            except OSError as e:
                # Do not advance seq/prev_hash: the row never landed, so the next append must
                # occupy this slot or verification would report a phantom gap.
                log.error("ledger.write_failed", stream=self.stream,
                          ledger_event=event, error=str(e))
                if self.strict:
                    raise
                return None
            self._seq += 1
            self._prev_hash = row["row_hash"]
        return row

    @contextmanager
    def _stream_lock(self) -> Iterator[None]:
        """Cross-process exclusion for a shared stream; a no-op for a single-writer one.

        A lock file created with O_EXCL is portable (Windows and POSIX). A lock older than
        ``_STALE_LOCK_S`` belonged to a writer that died holding it, and is broken rather than
        stalling every later writer.
        """
        if not self.shared:
            yield
            return
        lock = self.dir / f".{self.stream}.lock"
        deadline = time.monotonic() + _LOCK_WAIT_S
        while True:
            try:
                fd = os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
                break
            except FileExistsError:
                try:
                    if time.time() - lock.stat().st_mtime > _STALE_LOCK_S:
                        lock.unlink(missing_ok=True)
                        continue
                except FileNotFoundError:
                    continue
                if time.monotonic() > deadline:
                    raise OSError(f"ledger stream {self.stream!r} is locked by another writer") from None
                time.sleep(0.02)
        try:
            os.write(fd, str(os.getpid()).encode("ascii"))
            os.close(fd)
            yield
        finally:
            lock.unlink(missing_ok=True)

    def _write(self, row: dict[str, Any], when: datetime) -> None:
        path = self.path_for(when)
        with path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(row, ensure_ascii=False, default=str) + "\n")
            f.flush()
            if self._fsync:
                os.fsync(f.fileno())

    # -- reading ------------------------------------------------------------ #

    def rows(self) -> list[dict[str, Any]]:
        """Every row of this stream, oldest first, across day files."""
        out: list[dict[str, Any]] = []
        for path in self._existing_files():
            out.extend(read_stream(path))
        return out

    def verify(self) -> tuple[bool, str]:
        """Verify this stream end to end, across day rollovers."""
        return verify_chain(self.rows())
