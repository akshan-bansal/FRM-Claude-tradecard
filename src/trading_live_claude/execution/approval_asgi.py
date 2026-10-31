"""FastAPI app for the TradeCard approval shim.

Same wire surface as the previous stdlib handler, same OpenAPI 3.1 shape,
plus native async plumbing for the SSE/WebSocket push path that a later
commit will add. Pydantic models carry the sharp-edge descriptions (byte
format of ``canonical``, base64-of-raw-64 encoding of ``signature``, etc.)
so the auto-generated ``/openapi.json`` is the single source of truth.

Route surface (same as before this port):
  Public bootstrap (unversioned):
    GET  /healthz
    GET  /openapi.json
  Versioned (auth-required when ``auth_token`` is set):
    POST   /v1/card/register
    DELETE /v1/card/{card_id}
    POST   /v1/intents
    GET    /v1/intents/pending
    GET    /v1/intents/{intent_id}
    POST   /v1/intents/{intent_id}/response
    GET    /v1/intel/{ref}
    GET    /v1/passbook
    GET    /v1/stats            (desk: live session strip)
    GET    /v1/basket           (desk: card-signed baskets and pending proposals)
    POST   /v1/basket/analyze   (desk: run the engines over typed symbols)
    POST   /v1/basket/propose   (desk: queue a basket for the card; approves nothing)
    GET    /v1/ib/access        (desk: which IB ports answer right now)
    GET    /v1/meters/snapshot  (desk: atlas readings from the journals)
    GET    /v1/desk/page        (desk: the built QuantPort.io page)
  Public shell (unversioned, carries no data):
    GET    /desk

Legacy (unversioned) callers of the /v1 routes still resolve during the
deprecation window; a middleware rewrites the scope path and logs a
warning per hit. Delete the middleware when the window closes.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import secrets
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Annotated, Literal

from fastapi import Depends, FastAPI, Header, HTTPException, Request, status
from fastapi.responses import JSONResponse, Response
from pydantic import BaseModel, ConfigDict, Field

from ..brokers.models import OrderAction
from ..intel.vs_engine import DEFAULT_WRITEUP_DIR
from .approval import CardRegistry, InMemoryApprovalStore
from .router import OrderIntent

SPEC_VERSION = "1.0.0"
DEFAULT_DESK_PAGE = Path(__file__).resolve().parents[3] / "pwa" / "desk.html"

Broker = Literal["ib", "kraken", "questrade"]
Verdict = Literal["ACCEPT", "DECLINE", "EXPIRED"]
Mode = Literal["paper", "dry-run", "live", "autonomous"]
Decision = Literal["ACCEPT", "DECLINE"]

# --------------------------------------------------------------------------- #
# request / response models — carry the sharp-edge descriptions               #
# --------------------------------------------------------------------------- #

class ErrorBody(BaseModel):
    error: str


class HealthzBody(BaseModel):
    ok: bool
    pending: int = Field(ge=0, description="Count of unresolved prompts.")


class CardRegisterBody(BaseModel):
    card_id: str = Field(description="Stable identifier the card presents on every "
                                     "request. Registering the same id again "
                                     "replaces the prior pubkey.")
    pubkey_pem: str = Field(description="Ed25519 SubjectPublicKeyInfo PEM. Anything "
                                        "else (RSA, raw, DER) is rejected.")


class CardRegisterResult(BaseModel):
    card_id: str


class CardRevokeResult(BaseModel):
    card_id: str
    revoked: bool = Field(description="True if the card was registered before this call.")


class OrderIntentBody(BaseModel):
    """Body of POST /v1/intents — the trading engine publishes an intent."""
    model_config = ConfigDict(extra="ignore")

    symbol: str
    action: Literal["Buy", "Sell", "BTC", "SShort"]
    shares: int = Field(ge=1)
    entry: float
    stop: float
    target: float | None = None
    strategy: str
    risk_dollars: float
    account_number: str
    symbolId: int | None = None
    broker: Broker
    mode: Mode = "paper"
    ttl_seconds: float = Field(
        default=90.0,
        description="Prompt lifetime; the server auto-EXPIREs it after this "
                    "window without a signed response.",
    )
    thesis: str = Field(default="", max_length=140,
                        description="Optional VS-engine narration; rendered on the card.")
    intel_ref: str = Field(default="", description="Optional writeup key; "
                                                    "GET /v1/intel/{ref} returns the full text.")


class PromptOut(BaseModel):
    """Mirrors :class:`~.approval.Prompt.to_dict()`."""
    intent_id: str = Field(description="Opaque; do not parse. The mint format "
                                        "is an implementation detail.")
    issued_at: datetime
    expires_at: datetime
    broker: Broker
    symbol: str
    action: str
    shares: int
    entry: float
    stop: float
    target: float | None = None
    notional_usd: float
    risk_dollars: float
    strategy: str
    account: str
    mode: Mode
    thesis: str
    intel_ref: str
    nonce: str = Field(description="Server-minted per prompt. Included in the "
                                    "canonical bytes so signatures are single-use.")
    canonical: str = Field(
        description=(
            "Exact string the card must sign. Wire format is "
            "`broker|action|symbol|shares|entry|notional|account|intent_id|nonce`. "
            "Sign these bytes as UTF-8; do NOT re-serialize the Prompt JSON, "
            "and do NOT alter any field before signing. This byte-level "
            "contract is not enforceable by OpenAPI — a codegen client MUST "
            "treat `canonical` as opaque and pass it straight into the signer."
        ),
    )
    fingerprint: str = Field(
        default="",
        description="`7F3A...91C2`: head and tail of the SHA-256 of `canonical`, for comparing "
                    "the desk panel against the card screen by eye. Never used to verify.")


class PromptsList(BaseModel):
    prompts: list[PromptOut]


class ResponseBody(BaseModel):
    decision: Decision
    card_id: str
    signature: str = Field(description=(
        "base64 of the RAW 64-byte Ed25519 signature — no PEM wrapper, no DER, "
        "no hex. The bytes signed are the Prompt's `canonical` field encoded "
        "as UTF-8."
    ))


class RespondResult(BaseModel):
    accepted: bool = Field(description="True iff the signature verified AND "
                                        "the intent was still live AND not "
                                        "previously consumed.")
    intent_id: str


class PassbookEntryOut(BaseModel):
    intent_id: str
    resolved_at: datetime
    verdict: Verdict
    broker: Broker
    symbol: str
    action: str
    shares: int
    notional_usd: float
    strategy: str
    thesis: str
    intel_ref: str
    card_id: str | None = Field(default=None,
                                 description="Signer for ACCEPT / DECLINE; "
                                             "null for EXPIRED.")


class PassbookPage(BaseModel):
    entries: list[PassbookEntryOut]
    limit: int
    offset: int


class StatsBody(BaseModel):
    """The desk's live session strip. A field the shim cannot source is null and named in
    ``placeholders``: a client must render null as unknown, never as a flat book or a fast card."""
    session_id: str | None = Field(default=None, description="Null when the session has no mark yet.")
    prompt_scope: str = Field(description="What the prompt counts cover: this session, or the whole store.")
    starting_equity: float | None = None
    session_equity: float | None = Field(default=None, description="Latest journalled mark, never recomputed.")
    peak_equity: float | None = None
    max_drawdown_pct: float | None = Field(default=None, description="Percent, worst journalled mark.")
    acceptance_rate: float = Field(ge=0, le=1)
    intents_total: int = Field(ge=0)
    intents_approved: int = Field(ge=0)
    intents_declined: int = Field(ge=0)
    intents_expired: int = Field(ge=0)
    intents_pending: int = Field(ge=0)
    avg_ttl_response: float | None = Field(
        default=None, ge=0,
        description="Median seconds from issue to the card's answer over ACCEPT/DECLINE only "
                    "(an EXPIRED row's resolved_at is the sweep, not a decision).")
    gate_rejections: int = Field(ge=0, description="Rows in state/rejected.jsonl within gate_scope.")
    gate_scope: str = ""
    last_gate_reason: str = ""
    overlay_scalar: float | None = Field(default=None, ge=0, le=1)
    overlay_risk_zone: str | None = None
    placeholders: list[str] = Field(default_factory=list)


class BasketAsk(BaseModel):
    """A venue and the symbols a human typed for it."""
    model_config = ConfigDict(extra="forbid")
    venue: Literal["kraken", "qt", "ib"]
    symbols: list[str] = Field(min_length=1, max_length=60)


# --------------------------------------------------------------------------- #
# app factory                                                                 #
# --------------------------------------------------------------------------- #

def create_app(
    store,                       # InMemoryApprovalStore | SqliteApprovalStore
    registry,                    # CardRegistry | SqliteCardRegistry
    *,
    writeup_dir: Path = DEFAULT_WRITEUP_DIR,
    auth_token: str | None = None,
    desk_page: Path | None = None,    # built desk panel; None -> DEFAULT_DESK_PAGE
    state_dir: Path | None = None,    # journals the desk routes read
    session_id: str | None = None,    # the book this shim is attached to
    session_started_at: datetime | None = None,   # scopes the live strip's prompt counts
) -> FastAPI:
    """Build the FastAPI app. Store + registry are injected so tests (and
    the ``wire_card_approval`` helper) can swap in the SQLite variants."""

    app = FastAPI(
        title="TradeCard Approval Shim",
        version=SPEC_VERSION,
        description=(
            "REST wire between the trading engine's ApprovalRouter and a "
            "physical (or simulated) Ed25519 approval card. The card signs "
            "the Prompt.canonical bytes; the shim verifies against a pubkey "
            "registered under card_id. See NEXT_SESSION.md §11 for sequencing."
        ),
        openapi_url=None,        # served by our custom handler for ETag caching
        docs_url=None,
        redoc_url=None,
    )

    # ------------------------------------------------------------------ #
    # auth                                                               #
    # ------------------------------------------------------------------ #

    _PUBLIC_PATHS = {"/healthz", "/openapi.json", "/desk"}
    _VERSIONED_PREFIX = "/v1"

    def require_auth(
        authorization: Annotated[str | None, Header()] = None,
        request: Request = None,       # type: ignore[assignment]
    ) -> None:
        if auth_token is None:
            return
        if request is not None and request.url.path in _PUBLIC_PATHS:
            return
        if not authorization or not authorization.startswith("Bearer "):
            raise HTTPException(status.HTTP_401_UNAUTHORIZED, "auth required")
        presented = authorization[len("Bearer "):].strip()
        if not hmac.compare_digest(presented, auth_token):
            raise HTTPException(status.HTTP_401_UNAUTHORIZED, "auth required")

    Auth = Depends(require_auth)

    # ------------------------------------------------------------------ #
    # legacy-path rewrite (deprecation window)                           #
    # ------------------------------------------------------------------ #

    @app.middleware("http")
    async def normalize_legacy_paths(request: Request, call_next):
        path = request.url.path
        if path in _PUBLIC_PATHS:
            return await call_next(request)
        if path.startswith(_VERSIONED_PREFIX + "/") or path == _VERSIONED_PREFIX:
            return await call_next(request)
        # Legacy — rewrite scope path, log deprecation.
        sys.stderr.write(
            f"[approval-shim] DEPRECATED path {path} — clients should use "
            f"{_VERSIONED_PREFIX}{path} (SPEC_VERSION {SPEC_VERSION}); "
            "legacy paths will be removed in a future release.\n"
        )
        request.scope["path"] = _VERSIONED_PREFIX + path
        request.scope["raw_path"] = (_VERSIONED_PREFIX + path).encode("ascii")
        return await call_next(request)

    # ------------------------------------------------------------------ #
    # bootstrap surfaces (unversioned)                                   #
    # ------------------------------------------------------------------ #

    @app.get("/healthz", response_model=HealthzBody, tags=["health"])
    def healthz():
        return {"ok": True, "pending": len(store.pending())}

    # ETag-cached spec. Auto-generated by FastAPI from the pydantic models.
    _spec_cache: dict[str, object] = {}

    @app.get("/openapi.json", include_in_schema=False)
    def openapi_spec(request: Request):
        if not _spec_cache:
            spec = app.openapi()
            body = json.dumps(spec, separators=(",", ":")).encode("utf-8")
            etag = '"' + hashlib.sha256(body).hexdigest()[:16] + '"'
            _spec_cache["body"] = body
            _spec_cache["etag"] = etag
        etag = _spec_cache["etag"]
        if request.headers.get("if-none-match") == etag:
            return Response(status_code=304, headers={"ETag": etag})   # type: ignore[arg-type]
        accept = request.headers.get("accept", "")
        media = ("application/vnd.oai.openapi+json;version=3.1"
                 if "vnd.oai.openapi" in accept else "application/json")
        return Response(
            content=_spec_cache["body"],     # type: ignore[arg-type]
            media_type=media,
            headers={"ETag": etag,           # type: ignore[dict-item]
                     "Cache-Control": "public, max-age=300"},
        )

    # ------------------------------------------------------------------ #
    # cards                                                              #
    # ------------------------------------------------------------------ #

    @app.post("/v1/card/register", status_code=201,
              response_model=CardRegisterResult,
              responses={400: {"model": ErrorBody}, 401: {"model": ErrorBody}},
              dependencies=[Auth], tags=["cards"])
    def register_card(body: CardRegisterBody):
        try:
            registry.register(body.card_id, body.pubkey_pem.encode("utf-8"))
        except ValueError as e:
            raise HTTPException(400, str(e)) from e
        return CardRegisterResult(card_id=body.card_id)

    @app.delete("/v1/card/{card_id}", response_model=CardRevokeResult,
                responses={404: {"model": CardRevokeResult},
                           401: {"model": ErrorBody}},
                dependencies=[Auth], tags=["cards"])
    def revoke_card(card_id: str):
        if not card_id:
            raise HTTPException(400, "missing card_id")
        revoked = registry.revoke(card_id)
        return JSONResponse(
            status_code=200 if revoked else 404,
            content={"card_id": card_id, "revoked": revoked},
        )

    # ------------------------------------------------------------------ #
    # intents                                                            #
    # ------------------------------------------------------------------ #

    @app.post("/v1/intents", status_code=201, response_model=PromptOut,
              responses={400: {"model": ErrorBody}, 401: {"model": ErrorBody}},
              dependencies=[Auth], tags=["intents"])
    def publish_intent(body: OrderIntentBody):
        try:
            intent = OrderIntent(
                symbol=body.symbol,
                action=OrderAction(body.action),
                shares=body.shares,
                entry=body.entry,
                stop=body.stop,
                target=body.target,
                strategy=body.strategy,
                risk_dollars=body.risk_dollars,
                account_number=body.account_number,
                symbolId=body.symbolId,
            )
            prompt = store.publish(
                intent, mode=body.mode, broker=body.broker,
                ttl_seconds=body.ttl_seconds,
                thesis=body.thesis, intel_ref=body.intel_ref,
            )
            return prompt.to_dict()
        except (ValueError, KeyError) as e:
            raise HTTPException(400, str(e)) from e

    @app.get("/v1/intents/pending", response_model=PromptsList,
             responses={401: {"model": ErrorBody}}, dependencies=[Auth],
             tags=["intents"])
    def list_pending():
        return {"prompts": [p.to_dict() for p in store.pending()]}

    @app.get("/v1/intents/{intent_id}", response_model=PromptOut,
             responses={404: {"model": ErrorBody}, 401: {"model": ErrorBody}},
             dependencies=[Auth], tags=["intents"])
    def get_intent(intent_id: str):
        for p in store.pending():
            if p.intent_id == intent_id:
                return p.to_dict()
        raise HTTPException(404, "unknown or resolved intent")

    @app.post("/v1/intents/{intent_id}/response",
              response_model=RespondResult,
              responses={400: {"model": ErrorBody},
                         401: {"model": RespondResult}},
              dependencies=[Auth], tags=["intents"])
    def respond_intent(intent_id: str, body: ResponseBody):
        try:
            signature = base64.b64decode(body.signature)
        except (ValueError, base64.binascii.Error) as e:   # type: ignore[attr-defined]
            raise HTTPException(400, f"bad signature encoding: {e}") from e
        ok = store.respond(intent_id, decision=body.decision,
                           card_id=body.card_id, signature=signature)
        return JSONResponse(
            status_code=200 if ok else 401,
            content={"accepted": ok, "intent_id": intent_id},
        )

    # ------------------------------------------------------------------ #
    # intel                                                              #
    # ------------------------------------------------------------------ #

    @app.get("/v1/intel/{ref}", dependencies=[Auth], tags=["intel"],
             responses={400: {"model": ErrorBody}, 404: {"model": ErrorBody},
                        401: {"model": ErrorBody}})
    def get_intel(ref: str):
        if not ref or "/" in ref or "\\" in ref or ".." in ref:
            raise HTTPException(400, "bad intel_ref")
        path = writeup_dir / f"{ref}.json"
        try:
            resolved = path.resolve()
            resolved.relative_to(writeup_dir.resolve())
        except (OSError, ValueError) as e:
            raise HTTPException(400, "bad intel_ref") from e
        if not resolved.exists():
            raise HTTPException(404, "no writeup")
        try:
            return json.loads(resolved.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as e:
            raise HTTPException(500, f"unreadable writeup: {e}") from e

    # ------------------------------------------------------------------ #
    # passbook                                                           #
    # ------------------------------------------------------------------ #

    @app.get("/v1/passbook", response_model=PassbookPage,
             responses={400: {"model": ErrorBody}, 401: {"model": ErrorBody}},
             dependencies=[Auth], tags=["passbook"])
    def get_passbook(limit: int = 50, offset: int = 0):
        entries = [e.to_dict() for e in store.passbook(limit=limit, offset=offset)]
        return {"entries": entries,
                "limit": min(max(limit, 0), 500),
                "offset": max(offset, 0)}

    # ------------------------------------------------------------------ #
    # QuantPort.io desk panel: live strip, baskets, IB probe, atlas      #
    # ------------------------------------------------------------------ #

    @app.get("/v1/stats", response_model=StatsBody, responses={401: {"model": ErrorBody}},
             dependencies=[Auth], tags=["desk"])
    def get_stats():
        from .desk_feed import session_stats
        return session_stats(store, state_dir, session_id, session_started_at)

    def _basket_files() -> tuple[Path, Path]:
        if state_dir is None:
            raise HTTPException(503, "this shim has no state dir, so it cannot read or write baskets")
        return Path(state_dir) / "baskets.jsonl", Path(state_dir) / "basket_proposals.jsonl"

    @app.get("/v1/basket", dependencies=[Auth], tags=["desk"])
    def get_baskets():
        from .basket import BasketBook, pending_proposals, registry_verifier
        bpath, ppath = _basket_files()
        book = BasketBook(bpath, registry_verifier(Path(state_dir) / "approval.db"))  # type: ignore[arg-type]
        venues = {}
        for v in ("kraken", "qt", "ib"):
            ap = book.approved(v)
            venues[v] = None if ap is None else {
                "symbols": sorted(ap.symbols), "issued_at": ap.issued_at, "signed_at": ap.signed_at,
                "card_id": ap.card_id, "analysis_hash": ap.analysis_hash}
        return {"venues": venues, "rejected_rows": book.rejected_rows,
                "proposals": [{k: p[k] for k in ("id", "venue", "symbols", "issued_at", "fingerprint",
                                                  "analysis_hash")} for p in pending_proposals(ppath)]}

    @app.post("/v1/basket/analyze", dependencies=[Auth], tags=["desk"])
    def post_basket_analyze(ask: BasketAsk):
        from .desk_feed import analyse_basket
        _basket_files()
        return analyse_basket(ask.venue, ask.symbols, Path(state_dir))  # type: ignore[arg-type]

    @app.post("/v1/basket/propose", dependencies=[Auth], tags=["desk"])
    def post_basket_propose(ask: BasketAsk):
        """Analyse, then queue the basket for the card. Nothing is approved by this call: the card
        must sign the fingerprint returned here (scripts/basket.py on the simulated card)."""
        from .basket import append_proposal, make_proposal
        from .desk_feed import analyse_basket
        _, ppath = _basket_files()
        rep = analyse_basket(ask.venue, ask.symbols, Path(state_dir))  # type: ignore[arg-type]
        refused = [r["symbol"] for r in rep["rows"] if r["policy_refusal"]]
        if refused:
            raise HTTPException(422, f"venue policy refuses: {', '.join(refused)}")
        prop = make_proposal(venue=ask.venue, symbols=ask.symbols, analysis=rep)
        append_proposal(prop, ppath)
        return {k: prop[k] for k in ("id", "venue", "symbols", "issued_at", "fingerprint", "analysis_hash")}

    @app.get("/v1/ib/access", dependencies=[Auth], tags=["desk"])
    def get_ib_access():
        from .desk_feed import ib_access
        return ib_access()

    @app.get("/v1/meters/snapshot", include_in_schema=False, dependencies=[Auth])
    def meters_snapshot():
        """Atlas readings from the journals, in the same schema an uploaded snapshot uses."""
        if state_dir is None:
            return JSONResponse({"schema_version": 1, "mode": "NO STATE DIR", "readings": {},
                                 "notes": ["This shim was started without a state directory, so it "
                                           "has no journals to read readings from."]})
        from .desk_feed import meters_snapshot as _snapshot
        try:
            return JSONResponse(_snapshot(Path(state_dir)))
        except Exception as exc:       # a contradictory journal is reported, not rounded off
            return JSONResponse({"schema_version": 1, "mode": "UNREADABLE", "readings": {},
                                 "notes": [f"{type(exc).__name__}: {exc}"]}, status_code=409)

    # Serving the built panel from the shim is what makes it live: its fetches become same-origin,
    # so no CORS hole is opened and the token never travels in a URL. The split is deliberate:
    #
    #   GET /desk            public, carries no data: a shell that asks for the token if needed
    #   GET /v1/desk/page    the built page, behind the same auth as every other /v1 route
    #
    # The page is journal-derived (equity, fills, P&L), so reaching the port must not be enough to
    # read it. The shell keeps the token in sessionStorage and writes the page into the document.

    @app.get("/desk", include_in_schema=False)
    def desk_shell() -> Response:
        cfg = json.dumps({"spec": SPEC_VERSION, "auth_required": auth_token is not None,
                          "poll_seconds": 2})
        shell = (
            "<!doctype html><meta charset=utf-8>"
            "<meta name=viewport content='width=device-width,initial-scale=1'>"
            "<title>QuantPort.io</title>"
            "<style>body{margin:0;height:100vh;display:grid;place-items:center;background:#0b1417;"
            "color:#e6eef0;font:13px ui-monospace,Menlo,monospace}"
            "form{display:none;gap:8px;margin-top:14px}form.on{display:flex}"
            "input,button{font:inherit;color:#e6eef0;background:#172c35;border:1px solid #213d48;"
            "border-radius:2px;padding:8px 10px}button{cursor:pointer}"
            "p{color:#7d99a3;letter-spacing:.1em}</style>"
            "<main><div style='letter-spacing:.14em'>QUANTPORT<span style='color:#3fb8c4'>.IO</span>"
            "</div><p id=msg>CONNECTING TO THE SHIM…</p>"
            "<form id=f><input id=t type=password placeholder='shim token' autocomplete='off'>"
            "<button>UNLOCK</button></form></main>"
            # Scoped: document.write hands the document to the built page but keeps this Window, so
            # a name declared here would still be declared when the page's own script runs, and two
            # `const $` in one scope is a SyntaxError that kills the whole page.
            "<script>(function(){\n"
            f"const CFG={cfg};const KEY='tc.shim.token';\n"
            "const $=(i)=>document.getElementById(i);\n"
            "async function load(){\n"
            "  const tok=sessionStorage.getItem(KEY);\n"
            "  let r;\n"
            "  try{ r=await fetch('/v1/desk/page',{headers:tok?{Authorization:'Bearer '+tok}:{}});}\n"
            "  catch(e){ $('msg').textContent='SHIM UNREACHABLE — '+e.message; return; }\n"
            "  if(r.status===401){ sessionStorage.removeItem(KEY);\n"
            "    $('msg').textContent='THIS SHIM NEEDS ITS TOKEN. The session printed it at launch.';\n"
            "    $('f').className='on'; $('t').focus(); return; }\n"
            "  if(!r.ok){ $('msg').textContent='SHIM RETURNED '+r.status+' — '+(await r.text()).slice(0,160); return; }\n"
            "  const html=await r.text();\n"
            "  const pre='<scr'+'ipt>window.__SHIM__='+JSON.stringify(CFG)+';</scr'+'ipt>';\n"
            "  document.open(); document.write(pre+html); document.close();\n"
            "}\n"
            "$('f').addEventListener('submit',(e)=>{e.preventDefault();\n"
            "  sessionStorage.setItem(KEY,$('t').value.trim()); $('f').className='';\n"
            "  $('msg').textContent='UNLOCKING…'; load();});\n"
            "load();\n"
            "})();</script>"
        )
        return Response(shell, media_type="text/html; charset=utf-8",
                        headers={"Cache-Control": "no-store"})

    @app.get("/v1/desk/page", include_in_schema=False, dependencies=[Auth])
    def desk_built_page() -> Response:
        page = desk_page if desk_page is not None else DEFAULT_DESK_PAGE
        if not page.exists():
            return JSONResponse({"error": "the desk panel has not been built", "expected": str(page),
                                 "build": "python scripts/build_desk_dash.py"}, status_code=404)
        return Response(page.read_text(encoding="utf-8"), media_type="text/html; charset=utf-8",
                        headers={"Cache-Control": "no-store"})

    # ------------------------------------------------------------------ #
    # security scheme in the generated spec                              #
    # ------------------------------------------------------------------ #

    def _customize_openapi():
        if app.openapi_schema:
            return app.openapi_schema
        from fastapi.openapi.utils import get_openapi
        schema = get_openapi(
            title=app.title, version=app.version,
            description=app.description, routes=app.routes,
        )
        schema.setdefault("components", {}).setdefault("securitySchemes", {})
        schema["components"]["securitySchemes"]["BearerAuth"] = {
            "type": "http", "scheme": "bearer",
            "description": ("Shared secret minted by the paper script when "
                            "--require-card is on, or supplied to "
                            "scripts/approval_shim.py via --auth-token."),
        }
        schema["security"] = [{"BearerAuth": []}]
        app.openapi_schema = schema
        return schema

    app.openapi = _customize_openapi
    return app


def mint_auth_token() -> str:
    """256 bits of URL-safe entropy — shared secret between the shim thread
    and the card / simulator paired with it."""
    return secrets.token_urlsafe(32)
