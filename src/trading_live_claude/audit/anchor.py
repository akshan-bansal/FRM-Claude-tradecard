"""Daily anchor: a day's Merkle root over every ledger row, in a signed git tag off this machine.

The hash chain makes an edit detectable to anyone holding a later row, but not to someone who
rewrites the whole file and recomputes every hash. An anchor closes that: once a day's root is in
a signed tag pushed to the remote, a rewritten day no longer matches the root GitHub has held since
the push, and the tag's signature and push time are outside this laptop's control.

* Leaves are each row's ``row_hash``, ordered by (stream, seq), for one UTC day's files.
* The tree is SHA-256 over concatenated child digests; an odd node is paired with itself.
* The tag is ``ledger-anchor/<day>``, annotated (``-s`` when signing), on the repo's HEAD, with the
  anchor as JSON in its message. ``verify_tag`` recomputes the root and compares.

A day is anchored only after its chains verify; anchoring a broken record would certify it.
"""
from __future__ import annotations

import json
import subprocess
from datetime import UTC, date, datetime
from hashlib import sha256
from pathlib import Path
from typing import Any

from ..logging_setup import get_logger
from .ledger import GENESIS, read_stream, verify_chain
from .projection import streams

log = get_logger(__name__)

TAG_PREFIX = "ledger-anchor/"
ANCHORS_FILE = "ledger_anchors.jsonl"     # beside state/ledger, never inside it


def merkle_root(leaves: list[str]) -> str:
    """Root of the SHA-256 Merkle tree over hex leaves; the empty tree's root is GENESIS."""
    level = [bytes.fromhex(h) for h in leaves]
    if not level:
        return GENESIS
    while len(level) > 1:
        if len(level) % 2:
            level.append(level[-1])
        level = [sha256(level[i] + level[i + 1]).digest() for i in range(0, len(level), 2)]
    return level[0].hex()


def day_anchor(ledger_dir: Path, day: date) -> dict[str, Any]:
    """The anchor for one UTC day. Raises ``ValueError`` if any stream fails verification."""
    ledger_dir = Path(ledger_dir)
    leaves: list[str] = []
    out_streams: dict[str, Any] = {}
    for name, paths in sorted(streams(ledger_dir).items()):
        prev = GENESIS
        for path in paths:
            rows = read_stream(path)
            ok, reason = verify_chain(rows, expect_prev=prev)
            if not ok:
                raise ValueError(f"stream {name} ({path.name}) does not verify: {reason}")
            if path.name.startswith(f"{day.isoformat()}.") and rows:
                leaves += [str(r["row_hash"]) for r in rows]
                out_streams[name] = {
                    "rows": len(rows), "first_seq": rows[0]["seq"], "last_seq": rows[-1]["seq"],
                    "last_row_hash": rows[-1]["row_hash"],
                    "file_sha256": sha256(path.read_bytes()).hexdigest(),
                }
            if rows:
                prev = str(rows[-1]["row_hash"])
    return {"day": day.isoformat(), "root": merkle_root(leaves), "leaves": len(leaves),
            "streams": out_streams}


def tag_name(day: date) -> str:
    return f"{TAG_PREFIX}{day.isoformat()}"


def _git(repo: Path, *args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    return subprocess.run(["git", "-C", str(repo), *args], capture_output=True, text=True,
                          timeout=60, check=check)


def write_tag(repo: Path, anchor: dict[str, Any], *, sign: bool = True,
              signing_key: str | None = None, ssh: bool = False) -> dict[str, Any]:
    """Create the day's tag on HEAD. Signs with git's configured key, or ``signing_key``.

    ``ssh=True`` signs with an SSH key (``signing_key`` = the public key file), passed per call
    with ``-c`` so no git configuration is changed. With ``sign=False`` the tag is annotated but
    unsigned, and the result says so.
    """
    day = date.fromisoformat(anchor["day"])
    tag = tag_name(day)
    message = f"ledger anchor {anchor['day']}\n\n{json.dumps(anchor, sort_keys=True, indent=1)}\n"
    cfg: list[str] = []
    if ssh:
        cfg += ["-c", "gpg.format=ssh"]
    if signing_key:
        cfg += ["-c", f"user.signingkey={signing_key}"]
    args = [*cfg, "tag", "-s" if sign else "-a", tag, "-m", message, "HEAD"]
    res = _git(repo, *args, check=False)
    if res.returncode != 0:
        raise RuntimeError(f"git tag {tag} failed: {res.stderr.strip() or res.stdout.strip()}")
    commit = _git(repo, "rev-list", "-n", "1", tag).stdout.strip()
    return {"tag": tag, "signed": sign, "commit": commit}


def push_tag(repo: Path, tag: str, *, remote: str = "origin") -> None:
    res = _git(repo, "push", remote, f"refs/tags/{tag}", check=False)
    if res.returncode != 0:
        raise RuntimeError(f"git push {remote} {tag} failed: {res.stderr.strip()}")


def tag_anchor(repo: Path, day: date) -> dict[str, Any] | None:
    """The anchor JSON a tag carries, or None if the tag does not exist locally."""
    res = _git(repo, "cat-file", "-p", f"refs/tags/{tag_name(day)}", check=False)
    if res.returncode != 0:
        return None
    body = res.stdout.split("\n\n", 1)[-1]
    start, end = body.find("{"), body.rfind("}")
    return json.loads(body[start:end + 1]) if start >= 0 else None


def verify_day(ledger_dir: Path, repo: Path, day: date, *,
               check_signature: bool = True) -> tuple[bool, str]:
    """Recompute a day's root and compare it with the tag's. Optionally ``git tag -v`` it."""
    tagged = tag_anchor(repo, day)
    if tagged is None:
        return False, f"no tag {tag_name(day)} (fetch tags from the remote first)"
    try:
        now = day_anchor(ledger_dir, day)
    except ValueError as e:
        return False, str(e)
    if now["root"] != tagged.get("root"):
        changed = sorted(s for s in set(now["streams"]) | set(tagged.get("streams", {}))
                         if now["streams"].get(s) != tagged.get("streams", {}).get(s))
        return False, f"root differs from the tag; streams changed since anchoring: {changed}"
    if check_signature:
        res = _git(repo, "tag", "-v", tag_name(day), check=False)
        if res.returncode != 0:
            return False, f"root matches, but the tag signature does not verify: {res.stderr.strip()[:200]}"
    return True, "ok"


def record_anchor(state_dir: Path, anchor: dict[str, Any], tag: dict[str, Any]) -> None:
    """Note the anchor beside the ledger and in the control stream."""
    from .ledger import Ledger
    row = {"at": datetime.now(UTC).isoformat(), **anchor, **tag}
    with (Path(state_dir) / ANCHORS_FILE).open("a", encoding="utf-8") as f:
        f.write(json.dumps(row, sort_keys=True) + "\n")
    Ledger(Path(state_dir) / "ledger", stream="control", shared=True).append(
        "ANCHORED", {"day": anchor["day"], "root": anchor["root"], "leaves": anchor["leaves"],
                     "tag": tag["tag"], "signed": tag["signed"], "commit": tag["commit"]})


def anchored_days(state_dir: Path) -> set[str]:
    path = Path(state_dir) / ANCHORS_FILE
    if not path.exists():
        return set()
    return {json.loads(line)["day"] for line in path.read_text(encoding="utf-8").splitlines()
            if line.strip()}


def ledger_days(ledger_dir: Path) -> list[date]:
    days = {p.name.split(".", 1)[0] for p in Path(ledger_dir).glob("*.jsonl")}
    return sorted(date.fromisoformat(d) for d in days if len(d) == 10)
