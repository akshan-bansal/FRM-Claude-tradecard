"""The semantic layer: what each execution means for Canadian tax and conduct (AUDIT_SCOPE.md 5.3).

The ledger records what the code did. This module states what it *means*, as typed facts with a
versioned vocabulary (``config/audit_vocabulary.yaml``):

* ``tax_event``: acquisition or disposition (CRA: every disposition is reportable).
* ``property``: the *identical-property* key the adjusted cost base (ACB) is averaged over. It
  ignores the venue: SOL bought on Kraken and SOL bought elsewhere are one pool.
* ``lot_effect``: quantity and ACB before and after, in CAD.
* ``gain_cad``: proceeds minus ACB minus outlays, on a disposition.
* ``superficial``: the 61-day superficial-loss window (30 days before to 30 days after), the
  acquisitions inside it, the share of the loss denied, and where the denied amount went.
* ``holding_days_fifo``: first-in holding period of the quantity closed, for the business-income
  vs capital question only. ACB itself stays averaged, as CRA requires.

Every fact is derived by pure functions from ``EXECUTION_REPORTED`` rows plus the Bank of Canada
rates cached under ``state/fx``. Nothing is written back into the ledger, so re-deriving is the same
computation, and the export records the chain heads and rates it used. A fact that cannot be
determined (no FX rate for the day, a short sale the pool cannot represent) is ``unknown`` with the
reason, never a filled-in value.

This is a record-keeping aid, not tax advice: the superficial-loss test follows the rule's text
(identical property acquired in the window and still held at its end, by you or an affiliated
person), and anything it cannot see, such as a spouse's account not supplied, it cannot test.
"""
from __future__ import annotations

import json
import math
from collections import defaultdict
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any

from ..logging_setup import get_logger

log = get_logger(__name__)

VOCAB_VERSION = 1
WINDOW = timedelta(days=30)
CA_SUFFIXES = (".TO", ".V", ".CN", ".NE", ".TSX", ".TSXV")
BOC_SERIES = {"USD": "FXUSDCAD", "EUR": "FXEURCAD", "GBP": "FXGBPCAD", "JPY": "FXJPYCAD",
              "CHF": "FXCHFCAD", "AUD": "FXAUDCAD", "HKD": "FXHKDCAD", "NZD": "FXNZDCAD",
              "SGD": "FXSGDCAD", "SEK": "FXSEKCAD", "NOK": "FXNOKCAD", "KRW": "FXKRWCAD"}
# Stablecoins the venue quotes in are converted at the USD rate, and the row says so.
USD_PEGGED = {"USDT", "USDC"}


# --------------------------------------------------------------------------- property and currency

def instrument_currency(symbol: str, venue: str | None = None) -> str | None:
    """The currency an instrument trades in, from its listing: ``BASE/QUOTE`` -> QUOTE, a
    Canadian exchange suffix -> CAD, any other equity or future ticker -> USD."""
    s = symbol.strip().upper()
    if "/" in s:
        return s.split("/")[-1] or None
    if s.endswith(CA_SUFFIXES):
        return "CAD"
    return "USD" if s else None


def property_key(symbol: str, venue: str | None = None) -> str:
    """Identical-property key: one ACB pool per security, whatever venue it was bought on."""
    from ..intel.routing import classify_instrument
    s = symbol.strip().upper()
    cls = classify_instrument(s, (venue or "").split(":")[-1])
    if "/" in s or cls == "crypto":
        return f"crypto:{s.split('/')[0]}"
    if cls == "future":
        return f"future:{s.lstrip('/')}"
    for suf in CA_SUFFIXES:
        if s.endswith(suf):
            return f"ca-listed:{s[: -len(suf)]}"
    return f"us-listed:{s}"


# --------------------------------------------------------------------------- Bank of Canada FX

@dataclass(frozen=True)
class FxRate:
    currency: str
    rate: float | None          # CAD per unit; None = unknown
    rate_date: str | None
    source: str


class BankOfCanadaFX:
    """Daily Bank of Canada rates, cached under ``<state>/fx/<series>.json``.

    A trade date with no observation (a weekend or holiday) takes the most recent prior business
    day's rate, and the row records which date it used. Fetching is explicit (:meth:`fetch`), so a
    re-derivation from the cache never depends on the network.
    """

    VALET = "https://www.bankofcanada.ca/valet/observations/{series}/json"

    def __init__(self, cache_dir: Path) -> None:
        self.cache_dir = Path(cache_dir)
        self._series: dict[str, dict[str, float]] = {}

    def _path(self, series: str) -> Path:
        return self.cache_dir / f"{series}.json"

    def _load(self, series: str) -> dict[str, float]:
        if series not in self._series:
            try:
                raw = json.loads(self._path(series).read_text(encoding="utf-8"))
                self._series[series] = {str(k): float(v) for k, v in raw.items()}
            except (OSError, ValueError):
                self._series[series] = {}
        return self._series[series]

    def fetch(self, currency: str, start: date, end: date, *, client: Any = None) -> int:
        """Download and cache a currency's daily series. Returns the observations added."""
        import httpx
        series = BOC_SERIES.get(currency.upper())
        if series is None:
            raise ValueError(f"no Bank of Canada series for {currency}")
        http = client or httpx.Client(timeout=30.0, headers={"User-Agent": "FRM-Claude/1.0"})
        try:
            resp = http.get(self.VALET.format(series=series),
                            params={"start_date": start.isoformat(), "end_date": end.isoformat()})
            resp.raise_for_status()
            obs = resp.json().get("observations", [])
        finally:
            if client is None:
                http.close()
        have = self._load(series)
        before = len(have)
        for o in obs:
            v = (o.get(series) or {}).get("v")
            if v not in (None, ""):
                have[str(o["d"])] = float(v)
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        self._path(series).write_text(json.dumps(dict(sorted(have.items())), indent=0),
                                      encoding="utf-8")
        return len(have) - before

    def rate(self, currency: str | None, on: date) -> FxRate:
        if not currency:
            return FxRate("?", None, None, "trade currency unknown")
        ccy = currency.upper()
        if ccy == "CAD":
            return FxRate("CAD", 1.0, on.isoformat(), "CAD")
        lookup = "USD" if ccy in USD_PEGGED else ccy
        series = BOC_SERIES.get(lookup)
        if series is None:
            return FxRate(ccy, None, None, f"no Bank of Canada series for {ccy}")
        have = self._load(series)
        for back in range(0, 8):          # weekends and holidays: the latest prior business day
            d = (on - timedelta(days=back)).isoformat()
            if d in have:
                src = f"Bank of Canada {series}" + (f" (USD rate for {ccy})" if lookup != ccy else "")
                return FxRate(ccy, have[d], d, src)
        return FxRate(ccy, None, None, f"no cached {series} rate within 7 days before {on}")


# --------------------------------------------------------------------------- the ACB engine

@dataclass
class _Pool:
    qty: float = 0.0
    acb: float = 0.0
    fifo: list[list[Any]] = field(default_factory=list)      # [qty, acquired_at]
    pending_denied: list[dict[str, Any]] = field(default_factory=list)


def _executions(rows: Iterable[Mapping[str, Any]]) -> list[dict[str, Any]]:
    """EXECUTION_REPORTED payloads, deduplicated on exec_id, oldest first."""
    seen: dict[str, dict[str, Any]] = {}
    for r in rows:
        if r.get("event") != "EXECUTION_REPORTED":
            continue
        p = dict(r.get("payload") or {})
        if not p.get("exec_id") or p["exec_id"] in seen:
            continue
        p["_intent_id"] = r.get("intent_id")
        p["_stream"] = r.get("stream")
        p["_seq"] = r.get("seq")
        p["_time"] = datetime.fromisoformat(str(p["time"]).replace("Z", "+00:00"))
        seen[p["exec_id"]] = p
    return sorted(seen.values(), key=lambda p: (p["_time"], str(p["exec_id"])))


def classify(rows: Iterable[Mapping[str, Any]], fx: BankOfCanadaFX, *,
             as_of: datetime | None = None,
             affiliated: Iterable[Mapping[str, Any]] = ()) -> list[dict[str, Any]]:
    """One semantic fact block per execution, in time order. Pure over its inputs.

    ``affiliated`` are executions in accounts this repo does not trade (a spouse's, your RRSP or
    TFSA), as dicts with ``symbol``, ``side``, ``quantity``, ``time`` and optional ``venue``.
    They count for the superficial-loss test only; they never enter this pool's ACB.
    """
    as_of = as_of or datetime.now(UTC)
    execs = _executions(rows)
    aff = [dict(a, _time=datetime.fromisoformat(str(a["time"]).replace("Z", "+00:00")),
                _prop=property_key(str(a["symbol"]), a.get("venue")))
           for a in affiliated]

    # Position timeline per property (own accounts), for "still held at the window's end".
    by_prop: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for e in execs:
        e["_prop"] = property_key(str(e["symbol"]), e.get("venue"))
        by_prop[e["_prop"]].append(e)

    def held_at(prop: str, when: datetime, *, include_affiliated: bool) -> float:
        q = sum((float(e["quantity"]) if e["side"] == "Buy" else -float(e["quantity"]))
                for e in by_prop[prop] if e["_time"] <= when)
        if include_affiliated:
            q += sum((float(a["quantity"]) if a["side"] == "Buy" else -float(a["quantity"]))
                     for a in aff if a["_prop"] == prop and a["_time"] <= when)
        return q

    pools: dict[str, _Pool] = defaultdict(_Pool)
    out: list[dict[str, Any]] = []
    for e in execs:
        prop, t = e["_prop"], e["_time"]
        pool = pools[prop]
        q, px, fee = float(e["quantity"]), float(e["price"]), float(e.get("fee") or 0.0)
        r_trade = fx.rate(e.get("currency"), t.date())
        r_fee = fx.rate(e.get("fee_currency") or e.get("currency"), t.date())
        sem: dict[str, Any] = {
            "vocab": VOCAB_VERSION, "exec_id": e["exec_id"], "intent_id": e["_intent_id"],
            "stream": e["_stream"], "seq": e["_seq"], "time": t.isoformat(),
            "symbol": e["symbol"], "venue": e.get("venue"), "property": prop,
            "side": e["side"], "quantity": q, "price": px, "currency": e.get("currency"),
            "currency_source": e.get("currency_source"),
            "fx": {"rate": r_trade.rate, "date": r_trade.rate_date, "source": r_trade.source},
            "fee": fee, "fee_currency": e.get("fee_currency"),
            "tax_event": "acquisition" if e["side"] == "Buy" else "disposition",
            "rules_engaged": ["R1"],
        }
        unknown: list[str] = []
        if r_trade.rate is None:
            unknown.append(f"trade FX: {r_trade.source}")
        if fee and r_fee.rate is None:
            unknown.append(f"fee FX: {r_fee.source}")
        if unknown:
            sem.update(lot_effect="unknown", gain_cad="unknown", unknown_reasons=unknown)
            out.append(sem)
            log.warning("semantics.unknown", exec_id=e["exec_id"], reasons=unknown)
            continue
        fee_cad = fee * float(r_fee.rate or 0.0)
        gross_cad = q * px * float(r_trade.rate or 0.0)
        before = {"qty": pool.qty, "acb_cad": pool.acb}

        if e["side"] == "Buy":
            pool.qty += q
            pool.acb += gross_cad + fee_cad
            pool.fifo.append([q, t])
            # A loss denied earlier whose substitute is this acquisition lands in this ACB.
            for d in [d for d in pool.pending_denied if d["until"] >= t]:
                pool.acb += d["amount"]
                d["carried_to"] = e["exec_id"]
                pool.pending_denied.remove(d)
            sem["lot_effect"] = {"before": before, "after": {"qty": pool.qty, "acb_cad": pool.acb}}
        else:
            if q > pool.qty + 1e-9:
                sem.update(lot_effect="unknown", gain_cad="unknown",
                           unknown_reasons=[f"sells {q} with {pool.qty} held: a short sale the "
                                            "average-cost pool does not represent"])
                out.append(sem)
                continue
            acb_per = pool.acb / pool.qty if pool.qty else 0.0
            cost = acb_per * q
            gain = gross_cad - fee_cad - cost
            pool.qty -= q
            pool.acb -= cost
            # FIFO holding period of what was closed (reporting only).
            left, days = q, 0.0
            while left > 1e-12 and pool.fifo:
                lot = pool.fifo[0]
                take = min(lot[0], left)
                days += take * (t - lot[1]).total_seconds() / 86400.0
                lot[0] -= take
                left -= take
                if lot[0] <= 1e-12:
                    pool.fifo.pop(0)
            sem["holding_days_fifo"] = days / q if q else 0.0
            sem["gain_cad"] = gain
            sem["lot_effect"] = {"before": before, "after": {"qty": pool.qty, "acb_cad": pool.acb}}
            sem["rules_engaged"].append("R3")
            if gain < 0:
                sem["superficial"] = _superficial(e, gain, by_prop[prop], aff, prop, t, as_of,
                                                  held_at, pool)
                sem["rules_engaged"].append("R2")
        out.append(sem)
    return out


def _superficial(e: Mapping[str, Any], gain: float, own: list[dict[str, Any]],
                 aff: list[dict[str, Any]], prop: str, t: datetime, as_of: datetime,
                 held_at: Any, pool: _Pool) -> dict[str, Any]:
    q = float(e["quantity"])
    start, end = t - WINDOW, t + WINDOW
    own_buys = [x for x in own if x["side"] == "Buy" and start <= x["_time"] <= end
                and x["exec_id"] != e["exec_id"]]
    aff_buys = [a for a in aff if a["_prop"] == prop and a["side"] == "Buy"
                and start <= a["_time"] <= end]
    acquired = sum(float(x["quantity"]) for x in own_buys) + sum(float(a["quantity"]) for a in aff_buys)
    held_end = max(held_at(prop, min(end, as_of), include_affiliated=True), 0.0)
    window_open = end > as_of
    fraction = min(q, acquired, held_end) / q if q else 0.0
    denied = -gain * fraction
    out: dict[str, Any] = {
        "window_start": start.isoformat(), "window_end": end.isoformat(),
        "window_open": window_open,
        "acquired_in_window": [x["exec_id"] for x in own_buys] +
                              [f"affiliated:{a.get('account', '?')}:{a['_time'].isoformat()}"
                               for a in aff_buys],
        "acquired_qty": acquired, "held_at_window_end": held_end,
        "denied_fraction": fraction, "denied_loss_cad": denied,
        "allowed_loss_cad": gain + denied,
        "status": "provisional: window still open" if window_open else "final",
    }
    if denied <= 0:
        out["carried_to"] = None
        return out
    own_after = [x for x in own_buys if x["_time"] > t]
    if aff_buys and not own_buys:
        # The substitute sits in an account this pool does not hold (e.g. an RRSP/TFSA): the
        # denied loss does not enter this ACB, and for a registered plan it is lost.
        out["carried_to"] = "affiliated account (not added to this pool's ACB)"
    elif own_after:
        pool.pending_denied.append({"amount": denied, "until": end, "carried_to": None})
        out["carried_to"] = f"next acquisition in window ({own_after[0]['exec_id']})"
    else:
        pool.acb += denied                      # the substitute is already in the pool
        out["carried_to"] = "this pool's remaining ACB (substitute already held)"
    return out


# --------------------------------------------------------------------------- conduct flags (R4)

def conduct_flags(rows: Iterable[Mapping[str, Any]], *,
                  fast_cancel_s: float = 5.0) -> list[dict[str, Any]]:
    """Order-conduct facts from the order path: fast cancels and opposite-side orders resting.

    ``cancel_before_fill`` is any CANCELLED row, with how long the order was open, and flagged
    ``fast`` under ``fast_cancel_s``. ``self_match_risk`` is a dispatch while an opposite-side
    order on the same property, dispatched earlier, had no execution or cancel yet.
    """
    rows = sorted(rows, key=lambda r: (str(r.get("ts")), str(r.get("stream")), r.get("seq") or 0))
    open_orders: dict[str, dict[str, Any]] = {}
    submitted_at: dict[str, datetime] = {}
    flags: list[dict[str, Any]] = []
    for r in rows:
        ev, p = r.get("event"), r.get("payload") or {}
        ts = datetime.fromisoformat(str(r.get("ts")))
        iid = str(r.get("intent_id") or "")
        if ev == "BROKER_SUBMITTED" and iid:
            prop = property_key(str(p.get("symbol", "")), p.get("venue") or p.get("broker"))
            for other_id, o in open_orders.items():
                if o["property"] == prop and o["action"] != p.get("action"):
                    flags.append({"flag": "self_match_risk", "intent_id": iid,
                                  "resting_intent_id": other_id, "property": prop,
                                  "ts": ts.isoformat()})
            open_orders[iid] = {"property": prop, "action": p.get("action")}
            submitted_at[iid] = ts
        elif ev in ("EXECUTION_REPORTED", "BROKER_REJECTED", "BROKER_ERROR") and iid:
            open_orders.pop(iid, None)
        elif ev == "CANCELLED":
            open_orders.pop(iid, None)
            opened = submitted_at.get(iid)
            secs = (ts - opened).total_seconds() if opened else None
            flags.append({"flag": "cancel_before_fill", "intent_id": iid or None,
                          "order_id": p.get("order_id"), "reason": p.get("reason"),
                          "open_seconds": secs,
                          "fast": secs is not None and secs < fast_cancel_s,
                          "ts": ts.isoformat()})
    return flags


def totals(sems: Iterable[Mapping[str, Any]], year: int) -> dict[str, Any]:
    """Per-property totals for a tax year: proceeds, ACB of what was sold, gains, denied losses."""
    agg: dict[str, dict[str, float]] = defaultdict(lambda: defaultdict(float))
    unknown = 0
    for s in sems:
        if not str(s.get("time", "")).startswith(str(year)):
            continue
        if s.get("gain_cad") == "unknown":
            unknown += 1
            continue
        if s.get("tax_event") != "disposition":
            continue
        a = agg[str(s["property"])]
        a["dispositions"] += 1
        a["gain_cad"] += float(s["gain_cad"])
        sup = s.get("superficial") or {}
        a["denied_loss_cad"] += float(sup.get("denied_loss_cad") or 0.0)
    out = {k: dict(v) for k, v in sorted(agg.items())}
    for v in out.values():
        v["net_after_superficial_cad"] = v["gain_cad"] + v.get("denied_loss_cad", 0.0)
    return {"year": year, "by_property": out, "rows_with_unknown_facts": unknown,
            "finite": all(math.isfinite(x) for v in out.values() for x in v.values())}
