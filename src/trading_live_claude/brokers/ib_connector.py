"""IBConnectorBroker — market data over IBKR's official remote MCP connector. Read-only.

A third IB path beside ``brokers/ib.py`` and ``brokers/ib_web.py``, needing neither TWS nor the Client
Portal Gateway. The login is the process's own: dynamic client registration and PKCE against IBKR's
published OAuth server (``scripts/ib_connector_login.py``), not the one Claude's app holds.

**Read-only three ways.** The login asks for ``mcp.read`` alone and a token granted anything more is
refused; :data:`_READ_TOOLS` is the only set of tools the client will call; ``place_order`` always raises.
A paper session simulates fills on top, so its orders never reach this class.

The connector returns no account id, so nothing here can tell a paper login from a live one.
"""
from __future__ import annotations

import base64
import hashlib
import json
import secrets
import time
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from urllib.parse import urlencode

import httpx
import structlog

from .base import Broker, BrokerError, OrderRejected, TokenExpired
from .models import Account, Candle, Order, Position, Quote
from .token_store import TokenSet, TokenStore

log = structlog.get_logger(__name__)

MCP_URL = "https://api.ibkr.com/v1/api/mcp-public"
AUTHORIZE_URL = "https://api.ibkr.com/oauth2/authorize"
TOKEN_URL = "https://api.ibkr.com/oauth2/api/v1/token"
REGISTER_URL = "https://api.ibkr.com/oauth2/register"
SCOPE = "mcp.read"
_PROTOCOL = "2025-06-18"

_READ_TOOLS = frozenset({"search_contracts", "search_futures", "get_price_snapshot", "get_price_history",
                         "get_account_summary", "get_account_positions"})
# Listing suffix to IBKR's exchange code, as search_contracts names it. No suffix means a US listing.
_SUFFIX_EXCHANGE = {".TO": "TSE", ".L": "LSE", ".T": "TSEJ", ".AX": "ASX", ".HK": "SEHK"}
_INTERVALS: dict[str, tuple[str, int]] = {
    "OneMinute": ("ONE_MIN", 60), "FiveMinutes": ("FIVE_MINS", 300), "FifteenMinutes": ("FIFTEEN_MINS", 900),
    "ThirtyMinutes": ("THIRTY_MINS", 1800), "OneHour": ("ONE_HOUR", 3600), "OneDay": ("ONE_DAY", 86400),
    "OneWeek": ("ONE_WEEK", 604800), "OneMonth": ("ONE_MONTH", 2592000),
}
_PERIODS = ((1, "ONE_DAY"), (2, "TWO_DAYS"), (3, "THREE_DAYS"), (7, "ONE_WEEK"), (14, "TWO_WEEKS"),
            (31, "ONE_MONTH"), (92, "THREE_MONTHS"), (183, "SIX_MONTHS"), (366, "ONE_YEAR"),
            (731, "TWO_YEARS"), (1827, "FIVE_YEARS"))
_ASSUMED_DELAY_M = 15     # the connector states a status, not a delay; anything but REALTIME is not live


def _check_scope(body: dict[str, Any]) -> None:
    extra = set(str(body.get("scope") or SCOPE).split()) - {SCOPE}
    if extra:
        raise BrokerError(f"The connector granted more than {SCOPE}: {sorted(extra)}. Refusing the token.")


def token_set(body: dict[str, Any], previous_refresh: str = "") -> TokenSet:
    """A token response as what the store keeps. Refresh tokens may rotate, so a new one replaces the old."""
    _check_scope(body)
    return TokenSet(access_token=str(body["access_token"]),
                    refresh_token=str(body.get("refresh_token") or previous_refresh),
                    api_server=MCP_URL,
                    expires_at_epoch=time.time() + float(body.get("expires_in") or 0),
                    token_type=str(body.get("token_type") or "Bearer"))


def pkce_pair() -> tuple[str, str]:
    verifier = secrets.token_urlsafe(64)
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    return verifier, base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")


def register_client(client: httpx.Client, redirect_uri: str) -> str:
    """Dynamic client registration: a public client, so there is no secret to keep."""
    r = client.post(REGISTER_URL, json={"client_name": "trading-live-claude", "redirect_uris": [redirect_uri],
                                        "grant_types": ["authorization_code", "refresh_token"],
                                        "response_types": ["code"], "token_endpoint_auth_method": "none",
                                        "scope": SCOPE})
    r.raise_for_status()
    return str(r.json()["client_id"])


def authorize_url(client_id: str, redirect_uri: str, challenge: str, state: str) -> str:
    return AUTHORIZE_URL + "?" + urlencode({
        "response_type": "code", "client_id": client_id, "redirect_uri": redirect_uri, "scope": SCOPE,
        "state": state, "code_challenge": challenge, "code_challenge_method": "S256", "resource": MCP_URL})


def exchange_code(client: httpx.Client, client_id: str, redirect_uri: str, code: str, verifier: str) -> TokenSet:
    r = client.post(TOKEN_URL, data={"grant_type": "authorization_code", "client_id": client_id, "code": code,
                                     "redirect_uri": redirect_uri, "code_verifier": verifier,
                                     "resource": MCP_URL})
    r.raise_for_status()
    return token_set(r.json())


class ConnectorClient:
    """Tools over MCP's streamable HTTP transport: JSON-RPC, answered as JSON or as one SSE event."""

    def __init__(self, store: TokenStore, client_id: str, http: httpx.Client | None = None) -> None:
        self._store = store
        self._client_id = client_id
        self._http = http or httpx.Client(timeout=30.0)
        self._tokens = store.load()
        self._session: str | None = None
        self._next_id = 0
        if self._tokens is None:
            raise TokenExpired("No connector login. Run scripts/ib_connector_login.py.")

    def close(self) -> None:
        self._http.close()

    def _refresh(self) -> None:
        assert self._tokens is not None
        r = self._http.post(TOKEN_URL, data={"grant_type": "refresh_token", "client_id": self._client_id,
                                             "refresh_token": self._tokens.refresh_token, "resource": MCP_URL})
        if r.status_code != 200:
            raise TokenExpired(f"Connector refresh refused ({r.status_code}). Run scripts/ib_connector_login.py.")
        self._tokens = token_set(r.json(), self._tokens.refresh_token)
        self._store.save(self._tokens)

    def _post(self, payload: dict[str, Any]) -> httpx.Response:
        assert self._tokens is not None
        if self._tokens.expires_at_epoch <= time.time() + 30:
            self._refresh()
        headers = {"Authorization": f"Bearer {self._tokens.access_token}",
                   "Accept": "application/json, text/event-stream", "MCP-Protocol-Version": _PROTOCOL}
        if self._session:
            headers["Mcp-Session-Id"] = self._session
        try:
            return self._http.post(MCP_URL, json=payload, headers=headers)
        except httpx.HTTPError as e:
            raise BrokerError(f"Connector unreachable: {type(e).__name__}") from e

    def _rpc(self, method: str, params: dict[str, Any]) -> dict[str, Any]:
        self._next_id += 1
        payload = {"jsonrpc": "2.0", "id": self._next_id, "method": method, "params": params}
        r = self._post(payload)
        if r.status_code == 401:
            self._refresh()
            r = self._post(payload)
        if r.status_code >= 400:
            raise BrokerError(f"Connector {method} -> {r.status_code}")
        self._session = r.headers.get("Mcp-Session-Id", self._session)
        body = _rpc_body(r)
        if body.get("error"):
            raise BrokerError(f"Connector {method}: {body['error']}")
        return dict(body.get("result") or {})

    def _initialize(self) -> None:
        self._rpc("initialize", {"protocolVersion": _PROTOCOL, "capabilities": {},
                                 "clientInfo": {"name": "trading-live-claude", "version": "1"}})
        self._post({"jsonrpc": "2.0", "method": "notifications/initialized"})

    def call(self, tool: str, **arguments: Any) -> dict[str, Any]:
        if tool not in _READ_TOOLS:
            raise BrokerError(f"{tool} is not a read tool this client may call.")
        if self._session is None:
            self._initialize()
        result = self._rpc("tools/call", {"name": tool, "arguments": arguments})
        text = "".join(c.get("text", "") for c in result.get("content", []) if c.get("type") == "text")
        if result.get("isError"):
            raise BrokerError(f"Connector {tool}: {text[:200]}")
        return dict(result.get("structuredContent") or json.loads(text or "{}"))


def _rpc_body(r: httpx.Response) -> dict[str, Any]:
    if "text/event-stream" in r.headers.get("Content-Type", ""):
        lines = [ln[5:].strip() for ln in r.text.splitlines() if ln.startswith("data:")]
        if not lines:
            raise BrokerError("Connector sent an event stream with no data.")
        return dict(json.loads(lines[-1]))
    return dict(r.json())


@dataclass(frozen=True)
class _Contract:
    contract_id: int
    security_type: str
    exchange: str | None     # None takes SMART; futures need their own exchange or return nothing


class IBConnectorBroker(Broker):
    name = "ib_connector"
    venue = "ib_web"         # the same IBKR web surface and fee shape, so the cost tables need no second row

    def __init__(self, client: ConnectorClient) -> None:
        self._client = client
        self._futures: set[str] = set()
        self._contracts: dict[str, _Contract] = {}

    @classmethod
    def from_state(cls, state_dir: Path, encryption_key: str) -> IBConnectorBroker:
        client_file = state_dir / "ib_connector_client.json"
        if not client_file.exists():
            raise TokenExpired("No connector login. Run scripts/ib_connector_login.py.")
        client_id = json.loads(client_file.read_text(encoding="utf-8"))["client_id"]
        store = TokenStore(state_dir / "ib_connector_tokens.json.enc", encryption_key)
        return cls(ConnectorClient(store, client_id))

    def close(self) -> None:
        self._client.close()

    def set_sec_type(self, symbol: str, sec_type: str) -> None:
        if sec_type.upper() != "FUT":
            raise BrokerError(f"Only futures are marked by hand, not {sec_type}.")
        self._futures.add(symbol.upper())

    def resolve(self, symbol: str) -> _Contract:
        key = symbol.upper().lstrip("/")
        if key not in self._contracts:
            self._contracts[key] = self._resolve_future(key) if key in self._futures else self._resolve_stock(key)
        return self._contracts[key]

    def _resolve_stock(self, symbol: str) -> _Contract:
        suffix = next((s for s in _SUFFIX_EXCHANGE if symbol.endswith(s)), "")
        ticker = symbol[:-len(suffix)] if suffix else symbol
        rows = [r for r in self._client.call("search_contracts", query=ticker).get("results", [])
                if r.get("symbol") == ticker and _has(r, "STK")
                and (r.get("exchange") == _SUFFIX_EXCHANGE[suffix] if suffix else r.get("country_code") == "US")]
        if len(rows) != 1:
            # Never guess: the same ticker is a different company on another exchange.
            raise BrokerError(f"{symbol}: {len(rows)} matching listings, need exactly one.")
        return _Contract(int(rows[0]["underlying_contract_id"]), "STK", None)

    def _resolve_future(self, root: str) -> _Contract:
        rows = [r for r in self._client.call("search_contracts", query=root).get("results", [])
                if r.get("symbol") == root and _has(r, "FUT")]
        if len(rows) != 1:
            raise BrokerError(f"/{root}: {len(rows)} futures roots, need exactly one.")
        ladder = self._client.call("search_futures", underlying_contract_id=int(rows[0]["underlying_contract_id"]))
        today = datetime.now(UTC).strftime("%Y%m%d")
        live = [c for c in ladder.get("contracts", []) if str(c.get("last_trading_date")) >= today]
        if not live:
            raise BrokerError(f"/{root}: no contract left to trade.")
        front = min(live, key=lambda c: str(c["contract_month"]))
        return _Contract(int(front["contract_id"]), "FUT", str(front["exchange"]))

    def accounts(self) -> list[Account]:
        # The connector names no account. This stands for the one the login chose.
        return [Account(type="connector", number="connector", status="Active", isPrimary=True)]

    def positions(self, account_number: str) -> list[Position]:
        rows = self._client.call("get_account_positions").get("positions", [])
        if rows:
            # No row has been seen yet (the account was flat), so its field names are unverified.
            raise BrokerError("The connector returned positions; their row shape is not mapped yet.")
        return []

    def quote(self, symbol: str) -> Quote:
        c = self.resolve(symbol)
        args: dict[str, Any] = {"contract_id": c.contract_id,
                                "market_data_names": ["last", "bid_ask", "top_status", "volume"]}
        if c.exchange:
            args["exchange"] = c.exchange
        snap = self._client.call("get_price_snapshot", **args)
        last, book = snap.get("last") or {}, snap.get("bid-ask") or {}
        ts = last.get("ts")
        live = (snap.get("top-status") or {}).get("status") == "REALTIME"
        return Quote(symbol=symbol, symbolId=c.contract_id,
                     bidPrice=book.get("bid"), askPrice=book.get("ask"),
                     bidSize=_whole(book.get("bid_size")), askSize=_whole(book.get("ask_size")),
                     lastTradePrice=last.get("price"),
                     lastTradeTime=datetime.fromtimestamp(ts, tz=UTC) if ts else None,
                     volume=_whole((snap.get("volume") or {}).get("volume")),
                     delay=0 if live else _ASSUMED_DELAY_M, isHalted=bool(last.get("halted")))

    def quotes(self, symbols: list[str]) -> list[Quote]:
        return [self.quote(s) for s in symbols]

    def candles(self, symbol: str, start: datetime, end: datetime, interval: str = "OneDay") -> list[Candle]:
        if interval not in _INTERVALS:
            raise BrokerError(f"Unknown interval {interval!r}.")
        step, seconds = _INTERVALS[interval]
        c = self.resolve(symbol)
        # The tool counts back from now, so the period has to reach the start of the window.
        days = (datetime.now(UTC) - _aware(start)).days + 1
        period = next((name for limit, name in _PERIODS if days <= limit), _PERIODS[-1][1])
        args: dict[str, Any] = {"contract_id": c.contract_id, "security_type": c.security_type, "step": step,
                                "period": period, "outside_rth": False}
        if c.exchange:
            args["exchange"] = c.exchange
        b = self._client.call("get_price_history", **args)
        out = []
        for i, t in enumerate(b.get("time", [])):
            when = datetime.fromisoformat(str(t).replace("Z", "+00:00"))
            if _aware(start) <= when <= _aware(end):
                out.append(Candle(start=when, end=when + timedelta(seconds=seconds), open=b["open"][i],
                                  high=b["high"][i], low=b["low"][i], close=b["close"][i],
                                  volume=int(b["volume"][i] or 0)))
        return out

    def equity(self, account_number: str, currency: str = "CAD") -> float:
        s = self._client.call("get_account_summary")
        if str(s.get("currency", "")).upper() != currency.upper():
            raise BrokerError(f"The connector reports {s.get('currency')}, not {currency}.")
        return float(s["net_liquidation"])

    def place_order(self, order: Order) -> Order:
        raise OrderRejected("The connector feed is read-only; it places no orders.")

    def cancel_order(self, account_number: str, order_id: int) -> None:
        raise OrderRejected("The connector feed is read-only; it has no orders to cancel.")


def _has(row: dict[str, Any], security_type: str) -> bool:
    return any(s.get("security_type") == security_type for s in row.get("sections", []))


def _whole(x: Any) -> int | None:
    return None if x is None else int(x)


def _aware(d: datetime) -> datetime:
    return d if d.tzinfo else d.replace(tzinfo=UTC)
