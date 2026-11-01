"""Audit trail: a tamper-evident, event-sourced record of every trading state transition.

See ``docs/compliance/AUDIT_SCOPE.md``. The writer, envelope, hash chain, versioning and
projection are ported from the FRM-Claude ledger branch (2026-09-24); the semantic layer
(:mod:`.semantics`), execution reconciliation (:mod:`.recon`) and anchoring (:mod:`.anchor`)
were added for the Canadian regulatory scope.
"""
from .ledger import (
    GENESIS,
    LEDGER_EVENTS,
    ChainBreak,
    Ledger,
    LedgerEvent,
    canonical_json,
    read_stream,
    row_hash,
    verify_chain,
)
from .versioning import risk_check_version, strategy_version

__all__ = [
    "GENESIS",
    "LEDGER_EVENTS",
    "ChainBreak",
    "Ledger",
    "LedgerEvent",
    "canonical_json",
    "read_stream",
    "risk_check_version",
    "row_hash",
    "strategy_version",
    "verify_chain",
]
