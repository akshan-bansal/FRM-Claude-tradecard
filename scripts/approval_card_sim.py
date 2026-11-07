"""Fake ESP32 approval card — long-polls the shim, signs prompts, replies.

Two modes:

  ``--auto``        auto-ACCEPT (or --auto=decline) every prompt. Great for
                    end-to-end smoke tests of the approval loop without any
                    hardware.
  ``--interactive`` (default) show each prompt and wait for a keypress
                    (a=ACCEPT, d=DECLINE, s=skip / let it expire). Mimics the
                    tap-to-approve UX the real card will have.
  ``--ui``          serve the card's screen and keys as a page on 127.0.0.1
                    (``card_sim_ui.html``), laid out for a 150 mm square OLED:
                    every signed field and the thesis in full, never cut as the
                    14x6 LCD must, and keycaps you click or press. The page only
                    says which key was pressed; the key and the signature stay here.

The keypair is persisted to ``state/card_sim/{card_id}.pem`` (private) and
registered with the shim on first run. Re-runs reuse it.
"""
from __future__ import annotations

import argparse
import base64
import hashlib
import http.server
import json
import queue
import secrets
import sys
import threading
import time
import urllib.error
import urllib.request
from datetime import UTC, datetime
from pathlib import Path

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

# --------------------------------------------------------------------------- #
# key management                                                              #
# --------------------------------------------------------------------------- #

def load_or_create_key(key_path: Path) -> Ed25519PrivateKey:
    if key_path.exists():
        return serialization.load_pem_private_key(  # type: ignore[return-value]
            key_path.read_bytes(), password=None
        )
    key_path.parent.mkdir(parents=True, exist_ok=True)
    key = Ed25519PrivateKey.generate()
    key_path.write_bytes(
        key.private_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PrivateFormat.PKCS8,
            encryption_algorithm=serialization.NoEncryption(),
        )
    )
    return key


def pubkey_pem(key: Ed25519PrivateKey) -> bytes:
    return key.public_key().public_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PublicFormat.SubjectPublicKeyInfo,
    )


# --------------------------------------------------------------------------- #
# HTTP helpers                                                                #
# --------------------------------------------------------------------------- #

_AUTH_HEADERS: dict[str, str] = {}


def _decode(raw: bytes) -> dict:
    """Body as a dict. A non-JSON body becomes {"error": "<text>"} instead of raising.

    2026-09-17: these helpers did ``json.loads(e.read())`` on the error path, so a shim 500 —
    which uvicorn returns as ``text/plain`` "Internal Server Error" — raised JSONDecodeError and
    killed the whole card process. That hid the real status code and made a server bug look like a
    client crash. Never let an error body's content type decide whether the card survives.
    """
    text = raw.decode("utf-8", errors="replace").strip()
    if not text:
        return {}
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError:
        return {"error": text[:500]}
    return parsed if isinstance(parsed, dict) else {"data": parsed}


def _transport_error(e: OSError) -> str:
    """Label for any transport failure, without letting it escape.

    ``OSError``, not ``URLError``: when the shim shuts down mid-response urllib surfaces a raw
    ``ConnectionResetError`` (WinError 10054) from the socket read, which is an OSError but NOT a
    URLError — it killed the card with a traceback at the end of the 2026-09-17 end-to-end run.
    A real card sitting on a desk must ride out the server restarting, so every transport fault
    becomes status 0 and the poll loop simply tries again.
    """
    reason = getattr(e, "reason", None) or e
    return f"{type(e).__name__}: {reason}"


def _post(url: str, body: dict) -> tuple[int, dict]:
    req = urllib.request.Request(
        url,
        data=json.dumps(body).encode("utf-8"),
        headers={"Content-Type": "application/json", **_AUTH_HEADERS},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            return r.status, _decode(r.read())
    except urllib.error.HTTPError as e:
        return e.code, _decode(e.read())
    except OSError as e:                    # shim down / refused / reset mid-read — keep polling
        return 0, {"error": _transport_error(e)}


def _get(url: str) -> tuple[int, dict]:
    req = urllib.request.Request(url, headers=_AUTH_HEADERS)
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            return r.status, _decode(r.read())
    except urllib.error.HTTPError as e:
        return e.code, _decode(e.read())
    except OSError as e:
        return 0, {"error": _transport_error(e)}


# --------------------------------------------------------------------------- #
# UI helpers                                                                  #
# --------------------------------------------------------------------------- #

REGISTER_TIMEOUT_SECONDS = 30.0   # a card may boot before the shim does

CANONICAL_FIELDS = (
    "broker", "action", "symbol", "shares", "entry",
    "notional_usd", "account", "intent_id", "nonce",
)


def _fingerprint(canonical: str, chars: int = 8) -> str:
    """``7F3A...91C2`` over the canonical bytes — computed HERE, not taken from the server.

    Kept as a local implementation (like ``parse_canonical``) rather than importing the package
    helper: a card verifies for itself, and real firmware has no access to our Python. Mirrors what
    ``firmware/tradecard/main/main.c`` must display.
    """
    digest = hashlib.sha256(canonical.encode("utf-8")).hexdigest().upper()
    half = chars // 2
    return f"{digest[:half]}...{digest[-half:]}"


def parse_canonical(p: dict) -> dict[str, str] | None:
    """Fields of the exact bytes the card will sign, or None if they can't be trusted.

    Order details are displayed from these, never from the prompt's separate JSON
    fields — otherwise a shim could show one trade and have the card sign another.
    Mirrors ``parse_canonical`` in ``firmware/tradecard/main/main.c``.
    """
    parts = str(p.get("canonical", "")).split("|")
    if len(parts) != len(CANONICAL_FIELDS) or not all(parts):
        return None
    signed = dict(zip(CANONICAL_FIELDS, parts, strict=True))
    if signed["intent_id"] != p.get("intent_id"):
        return None
    return signed


def render_prompt(p: dict, signed: dict[str, str]) -> str:
    """The compact line the real card will show. Order fields come from ``signed``."""
    seconds_left = max(
        0,
        int((datetime.fromisoformat(p["expires_at"]) - datetime.now(UTC)).total_seconds()),
    )
    header = (
        f"[{signed['broker'].upper()}] {signed['action']:>4} {signed['symbol']:<8} "
        f"{signed['shares']:>4}sh  ${float(signed['notional_usd']):>10,.2f}  "
        f"R=${p['risk_dollars']:>7,.2f}  [{p['strategy']}]  {seconds_left}s"
    )
    # Recomputed here over the exact bytes about to be signed, not read from the prompt's own
    # `fingerprint` field: the point is that the desk panel and the card agree independently.
    d = hashlib.sha256(str(p.get("canonical", "")).encode("utf-8")).hexdigest().upper()
    hash_line = f"\n  HASH {d[:4]}...{d[-4:]}   (recomputed here, over the exact bytes to be signed)"
    thesis = p.get("thesis") or ""
    if thesis:
        return header + f"\n  intel> {thesis}" + hash_line
    return header + hash_line


# --------------------------------------------------------------------------- #
# main loop                                                                   #
# --------------------------------------------------------------------------- #

LCD_COLS = 14


def fit(s: str) -> str:
    """The firmware's ``fit``: a value too wide for a row ends in ``~`` rather than reading as whole."""
    return s if len(s) <= LCD_COLS else s[: LCD_COLS - 1] + "~"


def screen_rows(signed: dict[str, str]) -> list[str]:
    """Rows 1-4 of the 14 x 6 LCD firmware's prompt screen, each from the signed bytes and nothing else."""
    return [fit(f"{signed['action']} {signed['symbol']}"), fit(f"{signed['shares']}sh"),
            fit(f"@{signed['entry']}"), fit(f"${signed['notional_usd']}")]


class CardUI:
    """The card's screen and keys as a local page. One prompt at a time; a press goes to a queue."""

    def __init__(self, port: int) -> None:
        self.token = secrets.token_urlsafe(18)
        self._lock = threading.Lock()
        self._state: dict = {"prompt": None, "last": ""}
        self.presses: queue.Queue[tuple[str, str]] = queue.Queue()
        page = (Path(__file__).with_name("card_sim_ui.html").read_text(encoding="utf-8")
                .replace("__CARD_TOKEN__", self.token))
        ui = self

        class Handler(http.server.BaseHTTPRequestHandler):
            def _send(self, code: int, body: bytes, kind: str) -> None:
                self.send_response(code)
                self.send_header("Content-Type", kind)
                self.send_header("Cache-Control", "no-store")
                self.end_headers()
                self.wfile.write(body)

            def _authorised(self) -> bool:
                # The token is only in the page this server hands out, which another origin cannot read.
                return secrets.compare_digest(self.headers.get("X-Card-Token", ""), ui.token)

            def do_GET(self) -> None:
                if self.path == "/":
                    self._send(200, page.encode("utf-8"), "text/html; charset=utf-8")
                elif self.path == "/state" and self._authorised():
                    with ui._lock:
                        self._send(200, json.dumps(ui._state).encode("utf-8"), "application/json")
                else:
                    self._send(404 if self.path != "/state" else 403, b"{}", "application/json")

            def do_POST(self) -> None:
                if self.path != "/press" or not self._authorised():
                    self._send(403, b"{}", "application/json")
                    return
                try:
                    body = json.loads(self.rfile.read(int(self.headers.get("Content-Length", "0") or 0)))
                except ValueError:
                    self._send(400, b"{}", "application/json")
                    return
                if body.get("key") in ("L", "R"):
                    ui.presses.put((str(body["key"]), str(body.get("intent_id", ""))))
                self._send(200, b"{}", "application/json")

            def log_message(self, *_: object) -> None:
                pass

        self.server = http.server.ThreadingHTTPServer(("127.0.0.1", port), Handler)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}/"

    def show(self, prompt: dict, signed: dict[str, str], fingerprint: str) -> None:
        with self._lock:
            self._state = {"last": "", "prompt": {
                "intent_id": prompt["intent_id"], "broker": signed["broker"], "expires_at": prompt["expires_at"],
                # Whole: the 150 mm panel has room, so nothing signed is cut. screen_rows is the LCD's view.
                "signed": dict(signed), "thesis": prompt.get("thesis") or "", "fingerprint": fingerprint}}

    def clear(self, last: str) -> None:
        with self._lock:
            self._state = {"prompt": None, "last": last}

    def decide(self, prompt: dict, signed: dict[str, str], fingerprint: str) -> str | None:
        """Wait for R or L on this prompt until it expires. A press for an earlier prompt is ignored."""
        self.show(prompt, signed, fingerprint)
        deadline = datetime.fromisoformat(prompt["expires_at"])
        while (left := (deadline - datetime.now(UTC)).total_seconds()) > 0:
            try:
                key_name, intent_id = self.presses.get(timeout=min(left, 0.5))
            except queue.Empty:
                continue
            if intent_id == prompt["intent_id"]:
                decision = "ACCEPT" if key_name == "R" else "DECLINE"
                self.clear(f"{decision[0]} {signed['symbol']}")
                return decision
        self.clear(f"X {signed['symbol']}")
        return None


def run(
    *,
    shim_url: str,
    card_id: str,
    key: Ed25519PrivateKey,
    auto: str | None,
    poll_interval: float,
    ui: CardUI | None = None,
) -> None:
    # Register (idempotent on the server side; re-registering is fine). Status 0 means the
    # transport failed, not that the server said yes — a card powered on before the shim must
    # wait rather than fall through to polling as if it were paired.
    body = {"card_id": card_id, "pubkey_pem": pubkey_pem(key).decode("utf-8")}
    deadline = time.monotonic() + REGISTER_TIMEOUT_SECONDS
    while True:
        status, resp = _post(f"{shim_url}/v1/card/register", body)
        if status == 0 and time.monotonic() < deadline:
            print(f"waiting for shim at {shim_url}: {resp.get('error')}", file=sys.stderr)
            time.sleep(min(poll_interval, 2.0))
            continue
        break
    if status == 0 or status >= 400:
        print(f"registration failed: {status} {resp}", file=sys.stderr)
        sys.exit(1)
    print(f"registered card_id={card_id} with {shim_url}")

    seen: set[str] = set()
    while True:
        try:
            status, resp = _get(f"{shim_url}/v1/intents/pending")
        except urllib.error.URLError as e:
            print(f"shim unreachable: {e}", file=sys.stderr)
            time.sleep(poll_interval)
            continue
        if status != 200:
            print(f"pending fetch failed: {status} {resp}", file=sys.stderr)
            time.sleep(poll_interval)
            continue

        for prompt in resp.get("prompts", []):
            if prompt["intent_id"] in seen:
                continue
            seen.add(prompt["intent_id"])
            signed = parse_canonical(prompt)
            if signed is None:
                print(f"\nREFUSED {prompt['intent_id']}: canonical malformed or bound "
                      "to another intent — not signing", file=sys.stderr)
                continue
            # Continuity check (phase 6). The card recomputes the fingerprint from the bytes it is
            # about to sign and compares it against the one the server sent for display. A mismatch
            # means the dashboard and the device would show different things for a single
            # signature — exactly the drift the fingerprint exists to catch — so refuse to sign.
            local_fp = _fingerprint(prompt["canonical"])
            served_fp = str(prompt.get("fingerprint", ""))
            if served_fp and served_fp != local_fp:
                print(f"\nREFUSED {prompt['intent_id']}: fingerprint mismatch — served "
                      f"{served_fp}, computed {local_fp} over the bytes to be signed",
                      file=sys.stderr)
                continue
            print("\n" + "=" * 78)
            print(render_prompt(prompt, signed))
            print(f"  HASH {local_fp}   (recomputed here, over the exact bytes to be signed)")
            print("=" * 78)

            decision = (ui.decide(prompt, signed, local_fp) if ui is not None
                        else _decide(prompt, auto=auto))
            if decision is None:
                print("(skipped — will expire)")
                continue

            sig = key.sign(prompt["canonical"].encode("utf-8"))
            body = {
                "decision": decision,
                "card_id": card_id,
                "signature": base64.b64encode(sig).decode("ascii"),
            }
            status, resp = _post(
                f"{shim_url}/v1/intents/{prompt['intent_id']}/response", body
            )
            print(f"-> {decision}: {status} {resp}")

        time.sleep(poll_interval)


def _decide(prompt: dict, *, auto: str | None) -> str | None:
    if auto == "accept":
        return "ACCEPT"
    if auto == "decline":
        return "DECLINE"
    # interactive
    while True:
        try:
            choice = input("[a]ccept / [d]ecline / [s]kip > ").strip().lower()
        except EOFError:
            return None
        if choice in {"a", "accept"}:
            return "ACCEPT"
        if choice in {"d", "decline"}:
            return "DECLINE"
        if choice in {"s", "skip", ""}:
            return None


def main() -> None:
    ap = argparse.ArgumentParser(description="Fake ESP32 approval card")
    ap.add_argument("--shim", default="http://127.0.0.1:8787")
    ap.add_argument("--card-id", default="card-sim-001")
    ap.add_argument("--key-file", type=Path, default=Path("state/card_sim/card-sim-001.pem"))
    ap.add_argument(
        "--auto",
        choices=["accept", "decline"],
        default=None,
        help="auto-respond to every prompt; omit for interactive tap-simulation",
    )
    ap.add_argument("--poll", type=float, default=1.0, help="poll interval seconds")
    ap.add_argument("--ui", action="store_true",
                    help="serve the card's screen and keycaps on 127.0.0.1 instead of the terminal prompt")
    ap.add_argument("--ui-port", type=int, default=8790)
    ap.add_argument("--auth-token", default=None,
                    help="Bearer token to send with every request; must match the "
                         "shim's --auth-token (paper scripts print this at startup).")
    args = ap.parse_args()
    if args.auth_token:
        _AUTH_HEADERS["Authorization"] = f"Bearer {args.auth_token}"

    key = load_or_create_key(args.key_file)
    print(f"card key: {args.key_file}")
    if args.ui and args.auto:
        ap.error("--ui and --auto are alternatives: one decides by hand, the other for you")
    ui = CardUI(args.ui_port) if args.ui else None
    if ui is not None:
        print(f"card screen: {ui.url}")
    run(
        shim_url=args.shim.rstrip("/"),
        card_id=args.card_id,
        key=key,
        auto=args.auto,
        poll_interval=args.poll,
        ui=ui,
    )


if __name__ == "__main__":
    main()
