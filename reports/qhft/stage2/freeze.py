"""Freeze the stage-2 pre-registration. Run this BEFORE fetching any stage-2 data.

Checks that the spec fingerprint in the draft matches ``spec.py``. Then it writes ``PREREGISTRATION.md``
(status FROZEN, UTC time) and ``FROZEN.sha256``. ``run_stage2.py`` refuses to run unless both exist
and still match. It refuses to re-freeze: a frozen protocol is changed only by a dated amendment
appended to PREREGISTRATION.md, whose new hash is then recorded by hand in FROZEN.sha256 with the reason.
"""
from __future__ import annotations

import hashlib
import sys
from datetime import UTC, datetime
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
from spec import SPEC  # noqa: E402

from trading_live_claude.quantum.fingerprint import of_model  # noqa: E402


def main() -> None:
    frozen, digest_file = HERE / "PREREGISTRATION.md", HERE / "FROZEN.sha256"
    if frozen.exists() or digest_file.exists():
        sys.exit("already frozen; change it only by a dated amendment")
    draft = (HERE / "PREREGISTRATION_DRAFT.md").read_text()
    fp = of_model(SPEC)
    if fp not in draft:
        sys.exit(f"spec.py fingerprint {fp} is not the one in the draft: update one of them first")
    stamp = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    text = draft.replace("**Status: DRAFT, not frozen.**", f"**Status: FROZEN {stamp}, before any stage-2 data was fetched.**")
    if text == draft:
        sys.exit("status line not found in the draft")
    frozen.write_text(text)
    digest = hashlib.sha256(frozen.read_bytes()).hexdigest()
    digest_file.write_text(f"{digest}  PREREGISTRATION.md  frozen {stamp}\n")
    print(f"frozen {stamp}\nsha256 {digest}\nspec   {fp}")


if __name__ == "__main__":
    main()
