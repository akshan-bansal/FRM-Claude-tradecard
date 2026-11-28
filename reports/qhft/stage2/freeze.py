"""Freeze, or amend, the stage-2 pre-registration. Freeze BEFORE fetching any stage-2 data.

    python freeze.py                 first freeze: draft -> PREREGISTRATION.md (status FROZEN) + FROZEN.json
    python freeze.py --amend "why"   after a dated amendment was APPENDED to PREREGISTRATION.md

``FROZEN.json`` records, per freeze or amendment:
- the UTC time;
- the SHA-256 and byte length of PREREGISTRATION.md;
- the spec fingerprint;
- the SHA-256 of every code file the run depends on: the quantum package, BSM_Spectral's
  ``bsm_event.py``, and the stage-2 scripts.

``run_stage2.py`` refuses unless all of them match the latest record. Amendments are append-only:
every earlier frozen text must stay a byte-exact prefix of the current one. So a change to method or
code is always visible, dated and explained, never silent.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

HERE = Path(__file__).resolve().parent
REPO = HERE.parents[2]
sys.path.insert(0, str(HERE))
from spec import SPEC  # noqa: E402

from trading_live_claude.quantum.fingerprint import of_model  # noqa: E402

PREREG, RECORD, DRAFT = HERE / "PREREGISTRATION.md", HERE / "FROZEN.json", HERE / "PREREGISTRATION_DRAFT.md"
DRAFT_STATUS = "**Status: DRAFT, not frozen.**"


def code_files() -> list[Path]:
    pkg = sorted((REPO / "src" / "trading_live_claude" / "quantum").glob("*.py"))
    return [*pkg, REPO / "BSM_Spectral" / "bsm_event.py", HERE / "spec.py", HERE / "run_stage2.py",
            HERE / "fetch_stage2.sh"]


def code_hashes() -> dict[str, str]:
    return {str(p.relative_to(REPO)): hashlib.sha256(p.read_bytes()).hexdigest() for p in code_files()}


def sha(b: bytes) -> str:
    return hashlib.sha256(b).hexdigest()


def entry(kind: str, reason: str, text: bytes) -> dict[str, Any]:
    return {"kind": kind, "utc": datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"), "reason": reason,
            "prereg_sha256": sha(text), "prereg_bytes": len(text), "spec": of_model(SPEC),
            "code": code_hashes()}


def freeze() -> dict[str, Any]:
    if PREREG.exists() or RECORD.exists():
        sys.exit("already frozen; change it only by an appended, dated amendment and `freeze.py --amend`")
    draft = DRAFT.read_text()
    fp = of_model(SPEC)
    if fp not in draft:
        sys.exit(f"spec.py fingerprint {fp} is not the one in the draft: update one of them first")
    if DRAFT_STATUS not in draft:
        sys.exit("status line not found in the draft")
    stamp = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    text = draft.replace(DRAFT_STATUS, f"**Status: FROZEN {stamp}, before any stage-2 data was fetched.**")
    PREREG.write_text(text)
    rec = entry("freeze", "initial freeze, before any stage-2 data was fetched", PREREG.read_bytes())
    RECORD.write_text(json.dumps({"records": [rec]}, indent=2) + "\n")
    return rec


def amend(reason: str) -> dict[str, Any]:
    if not (PREREG.exists() and RECORD.exists()):
        sys.exit("nothing frozen to amend")
    records = json.loads(RECORD.read_text())["records"]
    text = PREREG.read_bytes()
    for r in records:
        if sha(text[:r["prereg_bytes"]]) != r["prereg_sha256"]:
            sys.exit(f"PREREGISTRATION.md was edited, not appended to, since the {r['kind']} of {r['utc']}")
    added = text[records[-1]["prereg_bytes"]:].decode()
    if "## Amendment" not in added:
        sys.exit("append a dated '## Amendment N' section explaining the change before recording it")
    if of_model(SPEC) not in text.decode():
        sys.exit("the amended text must state the new spec fingerprint")
    rec = entry("amendment", reason, text)
    records.append(rec)
    RECORD.write_text(json.dumps({"records": records}, indent=2) + "\n")
    return rec


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--amend", metavar="REASON")
    a = ap.parse_args()
    rec = amend(a.amend) if a.amend else freeze()
    print(f"{rec['kind']} {rec['utc']}\nprereg {rec['prereg_sha256']}\nspec   {rec['spec']}\n"
          f"code   {len(rec['code'])} files bound")


if __name__ == "__main__":
    main()
