#!/usr/bin/env python3
"""bsm_microstructure.py -- source plug-ins, append-only tick store and tick-level analytics.

Reconstruction of the microstructure layer described in "Spectral Value-at-Risk for Binary Event
Contracts" (draft paper, section 8.1). Every data source is normalised to two record types --
book snapshots and trades -- and stored as append-only, gzip-compressed JSON lines, one file per
source / market / UTC day (small files and ASCII-safe names for a FAT32 volume; appends add gzip
members and never rewrite existing bytes). New sources are plug-ins, not new research code.

Analytics run on whatever a source supports and skip with a stated reason otherwise:
  signature plot      variance and kurtosis of increments across sampling intervals (a diffusion
                      keeps kurtosis near 3; jumps make excess kurtosis grow like 1/dt)
  Kramers-Moyal       conditional moment coefficients D1..D4
  TSRV                two-scale realised variance (Zhang, Mykland and Ait-Sahalia, 2005)
  order flow          order-flow imbalance (Cont, Kukanov and Stoikov, 2014) and a Kyle-style
                      signed-trade price impact
  liquidity           spread, depth, imbalance, CUSUM on standardised depth drops (void alarm)
  execution           Almgren-Chriss (2000) schedule with a fourth-moment penalty, calibrated from
                      the estimates above, reporting each input's provenance (data or default)
Binary contracts are analysed in the latent coordinate w = sqrt(tau) Phi^-1(mid). A daily regime
signal can be written for bsm_event.py --signals.

Built-in sources: synthetic, kalshi_candles (hourly candles), kalshi_book (live order-book
polling), kalshi_markets (top of book for a whole series ladder in one listing request),
vendor_drop (LOBSTER and Databento-style CSV files). Kalshi's public market-data API needs no
account; prices arrive as '*_dollars' strings and sizes as '*_fp' strings (legacy integer-cent
payloads are still parsed). The self-test checks the parsers on recorded payloads offline.

Install: python -m pip install numpy scipy
Run:     python bsm_microstructure.py --selftest
         python bsm_microstructure.py collect --source synthetic --market SYN --root store
         python bsm_microstructure.py collect --source kalshi_book --market TICKER --root store --seconds 600
         python bsm_microstructure.py collect --source kalshi_markets --series KXINXU --root store
         python bsm_microstructure.py collect --source kalshi_candles --market TICKER --series SERIES \\
                --start 2026-01-01 --end 2026-02-01 --root store
         python bsm_microstructure.py collect --source vendor_drop --path drop_dir --market AMZN --root store
         python bsm_microstructure.py analyze --root store --source synthetic --market SYN
         python bsm_microstructure.py analyze --root store --source kalshi_book --market TICKER \\
                --binary --expiry 2026-11-05T21:00:00
         python bsm_microstructure.py signals --root store --source synthetic --market SYN --out daily_signals.csv
"""
from __future__ import annotations

import argparse
import csv
import glob
import gzip
import json
import math
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone

import numpy as np
from scipy import optimize, stats
from scipy.special import ndtr, ndtri

DAY = 86400.0


# ---------------------------------------------------------------------------
# 1. Record types
# ---------------------------------------------------------------------------

@dataclass
class BookSnapshot:
    ts: float                       # epoch seconds, UTC
    market: str
    bids: list                      # [[price, qty], ...] best first
    asks: list
    source: str = ''
    type: str = field(default='book', init=False)


@dataclass
class Trade:
    ts: float
    market: str
    price: float
    qty: float
    side: int = 0                   # +1 buyer-initiated, -1 seller-initiated, 0 unknown
    source: str = ''
    type: str = field(default='trade', init=False)


def to_json(rec):
    return json.dumps(asdict(rec), separators=(',', ':'))


def from_json(line):
    d = json.loads(line)
    kind = d.pop('type')
    return BookSnapshot(**d) if kind == 'book' else Trade(**d)


# ---------------------------------------------------------------------------
# 2. Append-only store
# ---------------------------------------------------------------------------

class Store:
    """root/source/market/YYYY-MM-DD.jsonl.gz, append-only."""

    def __init__(self, root):
        self.root = root

    @staticmethod
    def safe(name):
        return re.sub(r'[^A-Za-z0-9._-]', '_', str(name))[:100] or '_'

    def path(self, source, market, day):
        return os.path.join(self.root, self.safe(source), self.safe(market), f'{day}.jsonl.gz')

    @staticmethod
    def day_of(ts):
        return datetime.fromtimestamp(ts, timezone.utc).strftime('%Y-%m-%d')

    def append(self, records, folder=None):
        """Append records; `folder` files a whole ladder under one name (e.g. its series)."""
        groups = {}
        for r in records:
            groups.setdefault((r.source, folder or r.market, self.day_of(r.ts)), []).append(r)
        for (source, market, day), recs in groups.items():
            p = self.path(source, market, day)
            os.makedirs(os.path.dirname(p), exist_ok=True)
            with gzip.open(p, 'ab') as f:
                f.write(''.join(to_json(r) + '\n' for r in recs).encode('ascii'))
        return sum(len(v) for v in groups.values())

    def days(self, source, market):
        d = os.path.join(self.root, self.safe(source), self.safe(market))
        return sorted(os.path.basename(p)[:10] for p in glob.glob(os.path.join(d, '*.jsonl.gz')))

    def read(self, source, market, start=None, end=None):
        out = []
        for day in self.days(source, market):
            if (start and day < start) or (end and day > end):
                continue
            with gzip.open(self.path(source, market, day), 'rt', encoding='ascii') as f:
                out.extend(from_json(line) for line in f if line.strip())
        out.sort(key=lambda r: r.ts)
        return out


# ---------------------------------------------------------------------------
# 3. Source plug-ins
# ---------------------------------------------------------------------------

SOURCES = {}


def register_source(name):
    def deco(cls):
        cls.name = name
        SOURCES[name] = cls
        return cls
    return deco


class Source:
    """A plug-in yields BookSnapshot / Trade records and declares what it provides."""
    name = ''
    provides = frozenset()

    def fetch(self, **kw):
        raise NotImplementedError


def simulate(kind='diffusion', days=1, seconds=23400, sigma=0.01, jump_rate=1 / 600, noise=0.0,
             impact_share=0.4, queue_coupling=6.0, tick=0.0, binary=False, horizon_days=30.0,
             seed=0, start='2026-01-05', p0=100.0, book=True):
    """Per-second synthetic market: efficient price, observed mid, top of book, trades.

    Efficient increments mix order-flow impact (share `impact_share` of diffusion variance) with
    independent noise; `kind='jump'` adds compound-Poisson jumps carrying the same variance as the
    diffusion. Observed mid = efficient + iid noise. Best-queue sizes respond to the same order
    flow, so order-flow imbalance predicts mid moves. With `binary`, the efficient path is a latent
    score and quotes are N(x / sqrt(tau)) probabilities in cents.
    """
    rng = np.random.default_rng(seed)
    n = days * seconds
    t0 = datetime.strptime(start, '%Y-%m-%d').replace(hour=14, minute=30, tzinfo=timezone.utc).timestamp()
    ts = (t0 + np.arange(days)[:, None] * DAY + np.arange(seconds)[None, :]).ravel().astype(float)
    flow = rng.standard_normal(n)
    dx = sigma * (math.sqrt(impact_share) * flow + math.sqrt(1 - impact_share) * rng.standard_normal(n))
    if kind == 'jump':
        jump_sd = sigma / math.sqrt(jump_rate)  # jumps carry as much variance as the diffusion
        dx = dx + rng.poisson(jump_rate, n) * rng.standard_normal(n) * jump_sd
    dx[::seconds] = 0.0  # no overnight move in the synthetic sessions
    x = (0.0 if binary else p0) + np.cumsum(dx)
    obs = x + noise * rng.standard_normal(n)
    if binary:
        tau = horizon_days - (ts - ts[0]) / DAY
        mid = ndtr(obs / np.sqrt(tau))
        half = 0.005
        tick = tick or 0.01
    else:
        tau = None
        mid = obs
        half = 0.005 * p0 / 100
    bid = mid - half
    ask = mid + half
    if tick:
        bid = np.floor(bid / tick) * tick
        ask = np.maximum(np.ceil(ask / tick) * tick, bid + tick)
    bq = np.full(n, 50.0)
    aq = np.full(n, 50.0)
    shock = rng.standard_normal((2, n)) * 2.0
    for i in range(1, n if book else 1):
        if bid[i] == bid[i - 1]:
            bq[i] = max(1.0, bq[i - 1] + queue_coupling * flow[i] + shock[0, i])
        else:
            bq[i] = 50.0 + 10.0 * abs(shock[0, i])
        if ask[i] == ask[i - 1]:
            aq[i] = max(1.0, aq[i - 1] - queue_coupling * flow[i] + shock[1, i])
        else:
            aq[i] = 50.0 + 10.0 * abs(shock[1, i])
    trade_mask = rng.random(n) < 0.3
    side = np.where(flow + 0.5 * rng.standard_normal(n) > 0, 1, -1)
    qty = rng.integers(1, 20, n).astype(float)
    return {'ts': ts, 'efficient': x, 'mid': mid, 'bid': bid, 'ask': ask, 'bq': bq, 'aq': aq,
            'depth_bid': bq * 3, 'depth_ask': aq * 3, 'tau': tau, 'trade_mask': trade_mask,
            'side': side, 'qty': qty}


@register_source('synthetic')
class SyntheticSource(Source):
    provides = frozenset({'book', 'depth', 'trade'})

    def fetch(self, market='SYN', **kw):
        sim = simulate(**kw)
        for i in range(len(sim['ts'])):
            ts = float(sim['ts'][i])
            yield BookSnapshot(ts, market, [[float(sim['bid'][i]), float(sim['bq'][i])]],
                               [[float(sim['ask'][i]), float(sim['aq'][i])]], self.name)
            if sim['trade_mask'][i]:
                px = sim['ask'][i] if sim['side'][i] > 0 else sim['bid'][i]
                yield Trade(ts + 0.5, market, float(px), float(sim['qty'][i]), int(sim['side'][i]), self.name)


KALSHI_API = 'https://api.elections.kalshi.com/trade-api/v2'
KALSHI_PAUSE = 0.25  # seconds between paged / batched requests, well inside public rate limits


def _get_json(url, timeout=20, retries=3):
    for attempt in range(retries):
        req = urllib.request.Request(url, headers={'User-Agent': 'bsm-microstructure/1.0',
                                                   'Accept': 'application/json'})
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return json.load(resp)
        except urllib.error.HTTPError as exc:
            if exc.code in (429, 500, 502, 503) and attempt + 1 < retries:
                time.sleep(2.0 * (attempt + 1))
                continue
            raise
        except (urllib.error.URLError, TimeoutError):
            if attempt + 1 < retries:
                time.sleep(2.0 * (attempt + 1))
                continue
            raise


def kalshi_get(path, **params):
    q = urllib.parse.urlencode({k: v for k, v in params.items() if v is not None})
    return _get_json(f'{KALSHI_API}{path}' + (f'?{q}' if q else ''))


def kalshi_paged(path, key, **params):
    """Every item of a cursor-paginated Kalshi listing."""
    cursor = None
    while True:
        data = kalshi_get(path, cursor=cursor, **params)
        yield from data.get(key, [])
        cursor = data.get('cursor')
        if not cursor:
            return
        time.sleep(KALSHI_PAUSE)


def _price(obj, field='close'):
    """Price in probability units from '<field>_dollars' strings, or legacy integer cents."""
    if not obj:
        return None
    v = obj.get(f'{field}_dollars')
    if v not in (None, ''):
        return float(v)
    v = obj.get(field)
    return None if v is None else float(v) / 100.0


def _num(obj, key):
    """Size or volume from '<key>_fp' fixed-point strings, or legacy integers."""
    v = obj.get(f'{key}_fp', obj.get(key))
    return 0.0 if v in (None, '') else float(v)


def _epoch(x):
    if isinstance(x, (int, float)):
        return int(x)
    if len(x) <= 10:
        return int(datetime.strptime(x, '%Y-%m-%d').replace(tzinfo=timezone.utc).timestamp())
    return int(datetime.fromisoformat(x.replace('Z', '+00:00')).timestamp())


def parse_orderbook(data):
    """Order book -> (bids, asks) in probability units, best first; yes asks are 1 - no bids."""
    ob = data.get('orderbook_fp')
    if ob is not None:
        yes = [(float(p), float(q)) for p, q in ob.get('yes_dollars') or []]
        no = [(float(p), float(q)) for p, q in ob.get('no_dollars') or []]
    else:  # legacy payload: integer cents
        ob = data.get('orderbook') or {}
        yes = [(p / 100, float(q)) for p, q in ob.get('yes') or []]
        no = [(p / 100, float(q)) for p, q in ob.get('no') or []]
    bids = sorted(([p, q] for p, q in yes), reverse=True)
    asks = sorted([round(1.0 - p, 6), q] for p, q in no)
    return bids, asks


def parse_candle(c):
    """(end_ts, yes_bid_close, yes_ask_close, trade_close, volume) from one candlestick."""
    return (float(c['end_period_ts']), _price(c.get('yes_bid')), _price(c.get('yes_ask')),
            _price(c.get('price')), _num(c, 'volume'))


def market_snapshot(m, ts, source='kalshi_markets'):
    """Top of book of a market-listing row as a BookSnapshot (one level, sizes included)."""
    bid, ask = _price(m, 'yes_bid'), _price(m, 'yes_ask')
    return BookSnapshot(ts, m['ticker'], [[bid, _num(m, 'yes_bid_size')]] if bid is not None else [],
                        [[ask, _num(m, 'yes_ask_size')]] if ask is not None else [], source)


@register_source('kalshi_candles')
class KalshiCandles(Source):
    """Candlesticks (default hourly) -> closing yes bid/ask snapshots (no depth) and volume prints."""
    provides = frozenset({'book', 'trade'})

    def fetch(self, market, series, start, end, period=60):
        data = kalshi_get(f'/series/{series}/markets/{market}/candlesticks',
                          start_ts=_epoch(start), end_ts=_epoch(end), period_interval=period)
        for c in data.get('candlesticks', []):
            ts, yb, ya, px, vol = parse_candle(c)
            if yb is not None and ya is not None:
                yield BookSnapshot(ts, market, [[yb, 0.0]], [[ya, 0.0]], self.name)
            if px is not None and vol > 0:
                yield Trade(ts, market, px, vol, 0, self.name)


@register_source('kalshi_book')
class KalshiBook(Source):
    """Polls the public order book; a failed poll is reported and skipped, not fatal."""
    provides = frozenset({'book', 'depth'})

    def fetch(self, market, seconds=600, every=5.0, depth=10):
        stop = time.time() + seconds
        while time.time() < stop:
            try:
                bids, asks = parse_orderbook(kalshi_get(f'/markets/{market}/orderbook', depth=depth))
                yield BookSnapshot(time.time(), market, bids, asks, self.name)
            except (urllib.error.URLError, TimeoutError, ValueError) as exc:
                print(f'kalshi_book: poll failed ({exc}); continuing', file=sys.stderr)
            time.sleep(every)


@register_source('kalshi_markets')
class KalshiMarkets(Source):
    """Top of book for every open market in a series -- a whole strike ladder per request page."""
    provides = frozenset({'book', 'depth'})

    def fetch(self, series, status='open'):
        ts = time.time()
        for m in kalshi_paged('/markets', 'markets', series_ticker=series, status=status, limit=1000):
            yield market_snapshot(m, ts, self.name)


@register_source('vendor_drop')
class VendorDrop(Source):
    """Files dropped by a vendor: LOBSTER message/orderbook pairs or Databento-style CSV."""
    provides = frozenset({'book', 'depth', 'trade'})

    def fetch(self, path, market):
        for msg in sorted(glob.glob(os.path.join(path, '*_message_*.csv'))):
            yield from self._lobster(msg, msg.replace('_message_', '_orderbook_'), market)
        for f in sorted(glob.glob(os.path.join(path, '*.csv'))):
            if '_message_' in f or '_orderbook_' in f:
                continue
            yield from self._databento(f, market)

    def _lobster(self, msg_path, ob_path, market):
        m = re.search(r'_(\d{4}-\d{2}-\d{2})_', os.path.basename(msg_path))
        day0 = datetime.strptime(m.group(1), '%Y-%m-%d').replace(tzinfo=timezone.utc).timestamp()
        with open(msg_path, newline='') as fm, open(ob_path, newline='') as fo:
            for mrow, orow in zip(csv.reader(fm), csv.reader(fo)):
                ts = day0 + float(mrow[0])
                lv = [float(v) for v in orow]
                asks = [[lv[i] / 1e4, lv[i + 1]] for i in range(0, len(lv), 4) if lv[i] < 9e9]
                bids = [[lv[i + 2] / 1e4, lv[i + 3]] for i in range(0, len(lv), 4) if lv[i + 2] > -9e9]
                yield BookSnapshot(ts, market, bids, asks, self.name)
                if mrow[1] in ('4', '5'):  # executions; direction is the passive side
                    yield Trade(ts, market, float(mrow[4]) / 1e4, float(mrow[3]), -int(mrow[5]), self.name)

    @staticmethod
    def _ts(v):
        try:
            x = float(v)
            return x / 1e9 if x > 1e12 else x
        except ValueError:
            return datetime.fromisoformat(v.replace('Z', '+00:00')).timestamp()

    def _databento(self, path, market):
        with open(path, newline='') as f:
            for row in csv.DictReader(f):
                ts = self._ts(row['ts_event'])
                scale = lambda x: float(x) / 1e9 if abs(float(x)) > 1e7 else float(x)
                if row.get('bid_px_00') and row.get('ask_px_00'):
                    yield BookSnapshot(ts, market, [[scale(row['bid_px_00']), float(row.get('bid_sz_00') or 0)]],
                                       [[scale(row['ask_px_00']), float(row.get('ask_sz_00') or 0)]], self.name)
                if row.get('action') == 'T' and row.get('price'):
                    side = {'B': 1, 'A': -1}.get(row.get('side', 'N'), 0)
                    yield Trade(ts, market, scale(row['price']), float(row.get('size') or 0), side, self.name)


# ---------------------------------------------------------------------------
# 4. Frames
# ---------------------------------------------------------------------------

@dataclass
class BookFrame:
    ts: np.ndarray
    bid: np.ndarray
    ask: np.ndarray
    bq: np.ndarray
    aq: np.ndarray
    depth_bid: np.ndarray
    depth_ask: np.ndarray

    @property
    def mid(self):
        return 0.5 * (self.bid + self.ask)

    @property
    def has_depth(self):
        return bool(np.nanmax(self.bq + self.aq) > 0)


@dataclass
class TradeFrame:
    ts: np.ndarray
    price: np.ndarray
    qty: np.ndarray
    side: np.ndarray


def frames(records, levels=5):
    books = [r for r in records if r.type == 'book' and r.bids and r.asks]
    trades = [r for r in records if r.type == 'trade']
    bf = tf = None
    if books:
        bf = BookFrame(np.array([b.ts for b in books]),
                       np.array([b.bids[0][0] for b in books]), np.array([b.asks[0][0] for b in books]),
                       np.array([b.bids[0][1] for b in books]), np.array([b.asks[0][1] for b in books]),
                       np.array([sum(q for _, q in b.bids[:levels]) for b in books]),
                       np.array([sum(q for _, q in b.asks[:levels]) for b in books]))
    if trades:
        tf = TradeFrame(np.array([t.ts for t in trades]), np.array([t.price for t in trades]),
                        np.array([t.qty for t in trades]), np.array([t.side for t in trades]))
    return bf, tf


def frames_from_sim(sim):
    bf = BookFrame(sim['ts'], sim['bid'], sim['ask'], sim['bq'], sim['aq'], sim['depth_bid'], sim['depth_ask'])
    m = sim['trade_mask']
    px = np.where(sim['side'] > 0, sim['ask'], sim['bid'])
    tf = TradeFrame(sim['ts'][m] + 0.5, px[m], sim['qty'][m], sim['side'][m])
    return bf, tf


def latent_coordinate(ts, mid, expiry_ts, eps=1e-4):
    """w = sqrt(tau) Phi^-1(mid), tau in days to resolution: keeps increments comparable near expiry."""
    tau = np.maximum((expiry_ts - np.asarray(ts)) / DAY, 1e-6)
    return np.sqrt(tau) * ndtri(np.clip(mid, eps, 1 - eps))


# ---------------------------------------------------------------------------
# 5. Analytics
# ---------------------------------------------------------------------------

def sample_grid(ts, x, dt):
    """Previous-tick sampling on a grid of step dt seconds within each UTC day (no overnight)."""
    out = []
    day = np.floor(ts / DAY)
    for d in np.unique(day):
        m = day == d
        t, v = ts[m], x[m]
        grid = np.arange(t[0], t[-1] + 1e-9, dt)
        idx = np.searchsorted(t, grid, side='right') - 1
        out.append(v[idx])
    return out


def _increments(ts, x, dt):
    parts = [np.diff(s) for s in sample_grid(ts, x, dt) if len(s) > 1]
    return np.concatenate(parts) if parts else np.array([])  # a short sample may not span dt


def signature_plot(ts, x, dts=(10, 30, 60, 300, 900, 1800)):
    rows = []
    for dt in dts:
        dx = _increments(ts, x, dt)
        if len(dx) < 10:
            continue
        rows.append({'dt': dt, 'n': len(dx), 'var_per_sec': float(np.mean(dx ** 2) / dt),
                     'kurtosis': float(stats.kurtosis(dx, fisher=False))})
    return rows


def kurtosis_slope(rows):
    """Log-log slope of excess kurtosis on dt (about -1 when jumps dominate)."""
    pts = [(r['dt'], r['kurtosis'] - 3) for r in rows if r['kurtosis'] > 3.05]
    if len(pts) < 3:
        return float('nan')
    d, k = np.array(pts).T
    return float(np.polyfit(np.log(d), np.log(k), 1)[0])


def kramers_moyal(ts, x, dt=10, bins=10, min_count=50):
    """D_n(x) = E[(dx)^n | x] / (n! dt), n = 1..4, on quantile bins of the level."""
    lv, dx = [], []
    for s in sample_grid(ts, x, dt):
        lv.append(s[:-1])
        dx.append(np.diff(s))
    lv, dx = np.concatenate(lv), np.concatenate(dx)
    edges = np.unique(np.quantile(lv, np.linspace(0, 1, bins + 1)))
    idx = np.clip(np.searchsorted(edges, lv, side='right') - 1, 0, len(edges) - 2)
    rows = []
    for b in range(len(edges) - 1):
        m = idx == b
        if m.sum() >= min_count:
            rows.append([float(lv[m].mean())] + [float(np.mean(dx[m] ** k) / (math.factorial(k) * dt))
                                                  for k in range(1, 5)])
    return np.array(rows)


def tsrv(x, K=None):
    """Two-scale realised variance of one day's 1-second path, with small-sample adjustment."""
    x = np.asarray(x, float)
    n = len(x) - 1
    K = K or max(2, int(round(n ** (2 / 3))))
    rv_all = float(np.sum(np.diff(x) ** 2))
    rv_k = float(np.sum((x[K:] - x[:-K]) ** 2) / K)
    nbar = (n - K + 1) / K
    return (rv_k - nbar / n * rv_all) / (1 - nbar / n), rv_all


def ofi_events(bf):
    """Cont-Kukanov-Stoikov order-flow imbalance contribution of each book update."""
    pb, pa, qb, qa = bf.bid, bf.ask, bf.bq, bf.aq
    e = np.zeros(len(pb))
    e[1:] = ((pb[1:] >= pb[:-1]) * qb[1:] - (pb[1:] <= pb[:-1]) * qb[:-1]
             - (pa[1:] <= pa[:-1]) * qa[1:] + (pa[1:] >= pa[:-1]) * qa[:-1])
    return e


def _interval_regression(ts, mid, y_events, ev_ts, dt):
    """Regress mid changes over dt intervals on the summed events in the same intervals."""
    day = np.floor(ts / DAY)
    X, Y = [], []
    for d in np.unique(day):
        m = day == d
        t = ts[m]
        edges = np.arange(t[0], t[-1] + dt, dt)
        if len(edges) < 3:
            continue
        mi = mid[m][np.searchsorted(t, edges, side='right') - 1]
        me = (ev_ts >= t[0]) & (ev_ts <= t[-1] + dt)
        agg = np.histogram(ev_ts[me], bins=edges, weights=y_events[me])[0]
        X.append(agg)
        Y.append(np.diff(mi))
    X, Y = np.concatenate(X), np.concatenate(Y)
    res = stats.linregress(X, Y)
    return {'slope': float(res.slope), 'r2': float(res.rvalue ** 2), 't': float(res.slope / res.stderr)
            if res.stderr > 0 else float('nan'), 'n': len(X)}


def order_flow_imbalance(bf, dt=10):
    return _interval_regression(bf.ts, bf.mid, ofi_events(bf), bf.ts, dt)


def kyle_lambda(bf, tf, dt=60):
    return _interval_regression(bf.ts, bf.mid, tf.side * tf.qty, tf.ts, dt)


def cusum_alarms(x, k=0.5, h=5.0):
    s, alarms = 0.0, []
    for i, v in enumerate(x):
        s = max(0.0, s + v - k)
        if s > h:
            alarms.append(i)
            s = 0.0
    return alarms


def liquidity(bf, k=0.5, h=8.0):
    depth = bf.depth_bid + bf.depth_ask
    ld = np.log(np.maximum(depth, 1e-9))
    z = (ld - ld.mean()) / (ld.std() + 1e-12)
    alarms = cusum_alarms(-z, k, h)
    imb = (bf.bq - bf.aq) / np.maximum(bf.bq + bf.aq, 1e-12)
    spread = bf.ask - bf.bid
    return {'spread_mean': float(spread.mean()), 'spread_median': float(np.median(spread)),
            'depth_mean': float(depth.mean()), 'imbalance_mean': float(imb.mean()),
            'void_alarms': len(alarms), 'void_times': [float(bf.ts[i]) for i in alarms[:20]]}


def ac_closed_form(X, T, N, sigma, eta, lam):
    """Almgren-Chriss holdings x_j = X sinh(kappa (T - t_j)) / sinh(kappa T) (discrete kappa)."""
    tau = T / N
    kappa = math.acosh(1 + lam * sigma ** 2 * tau ** 2 / (2 * eta)) / tau
    t = np.arange(N + 1) * tau
    return X * np.sinh(kappa * (T - t)) / np.sinh(kappa * T)


def ac_numeric(X, T, N, sigma, eta, lam, excess_kurt=0.0, kurt_weight=0.05):
    """Minimise eta/tau sum n_k^2 + lam sigma^2 tau sum x_k^2 + c4 sum x_k^4 over holdings x_k.

    The fourth-moment penalty charges excess kurtosis of per-step price moves:
    c4 = kurt_weight * excess_kurt * lam sigma^2 tau / X^2, so at full position each unit of
    excess kurtosis costs kurt_weight times the variance penalty, fading as (x/X)^4. With
    excess_kurt = 0 this is plain Almgren-Chriss. The objective is convex with a tridiagonal
    Hessian, so Newton's method solves it exactly.
    """
    from scipy.linalg import solve_banded
    tau = T / N
    a, b = eta / tau, lam * sigma ** 2 * tau
    c4 = kurt_weight * excess_kurt * b / X ** 2
    y = X * (1 - np.arange(1, N) / N)
    for _ in range(100):
        x = np.concatenate([[X], y, [0.0]])
        g = 2 * a * (2 * x[1:-1] - x[:-2] - x[2:]) + 2 * b * x[1:-1] + 4 * c4 * x[1:-1] ** 3
        ab = np.zeros((3, N - 1))
        ab[0, 1:] = ab[2, :-1] = -2 * a
        ab[1] = 4 * a + 2 * b + 12 * c4 * x[1:-1] ** 2
        step = solve_banded((1, 1), ab, -g)
        y = y + step
        if np.max(np.abs(step)) < 1e-13 * X:
            break
    return np.concatenate([[X], y, [0.0]])


def calibrate_execution(report, X=None, T=3600.0, N=60, lam=None):
    """Execution parameters from the analysis, each tagged with its provenance."""
    prov = {}
    sig = report.get('tsrv_sigma_per_sec')
    if sig:
        prov['sigma'] = (sig, 'data: TSRV')
    elif report.get('signature'):
        prov['sigma'] = (math.sqrt(report['signature'][-1]['var_per_sec']), 'data: signature plot')
    else:
        prov['sigma'] = (0.01, 'default')
    ky = report.get('kyle')
    if ky and ky['slope'] > 0:
        prov['eta'] = (0.5 * ky['slope'] * ky.get('dt', 60), 'data: Kyle lambda x interval / 2')
    else:
        prov['eta'] = (1e-4, 'default')
    exc = None
    for r in report.get('signature', []):
        if r['dt'] >= T / N:
            exc = max(r['kurtosis'] - 3, 0.0)
            break
    prov['excess_kurtosis'] = (exc, 'data: signature plot') if exc is not None else (0.0, 'default')
    prov['X'] = (X, 'given') if X is not None else (1000.0, 'default')
    prov['lambda'] = (lam, 'given') if lam is not None else (1e-6, 'default')
    return prov


def analyze(bf, tf, provides, binary=False, expiry_ts=None, log=print):
    """Run every analytic the data supports; return a report with stated skip reasons."""
    report = {'skipped': {}}
    if bf is None or 'book' not in provides:
        report['skipped']['all book analytics'] = 'source provides no book snapshots'
        return report
    x = bf.mid
    if binary:
        if expiry_ts is None:
            report['skipped']['binary transform'] = 'no expiry given; analysing raw mid'
        else:
            x = latent_coordinate(bf.ts, bf.mid, expiry_ts)
    report['coordinate'] = 'latent w' if (binary and expiry_ts is not None) else 'mid'
    report['signature'] = signature_plot(bf.ts, x)
    report['kurtosis_slope'] = kurtosis_slope(report['signature'])
    try:
        report['kramers_moyal'] = kramers_moyal(bf.ts, x)
    except ValueError as exc:
        report['skipped']['Kramers-Moyal'] = str(exc)
    day = np.floor(bf.ts / DAY)
    tv = []
    for d in np.unique(day):
        g = sample_grid(bf.ts[day == d], x[day == d], 1.0)[0]
        if len(g) > 1000:
            tv.append((tsrv(g)[0], len(g)))
    if tv:
        report['tsrv_sigma_per_sec'] = math.sqrt(max(sum(v for v, _ in tv) / sum(n for _, n in tv), 0.0))
    else:
        report['skipped']['TSRV'] = 'needs at least 1000 one-second observations per day'
    if 'depth' in provides and bf.has_depth:
        report['ofi'] = order_flow_imbalance(bf)
        report['liquidity'] = liquidity(bf)
    else:
        report['skipped']['order-flow imbalance, liquidity'] = 'source provides no queue sizes'
    if tf is not None and 'trade' in provides and np.any(tf.side != 0):
        report['kyle'] = dict(kyle_lambda(bf, tf), dt=60)
    else:
        report['skipped']['Kyle impact'] = 'no signed trades from this source'
    prov = calibrate_execution(report)
    p = {k: v[0] for k, v in prov.items()}
    sched = ac_numeric(p['X'], 3600.0, 60, p['sigma'], p['eta'], p['lambda'], 0.0)
    sched4 = ac_numeric(p['X'], 3600.0, 60, p['sigma'], p['eta'], p['lambda'], p['excess_kurtosis'])
    report['execution'] = {'provenance': prov, 'halfway': float(sched[30] / p['X']),
                           'halfway_kurtosis': float(sched4[30] / p['X'])}
    return report


def print_report(rep, log=print):
    log(f'coordinate: {rep.get("coordinate", "-")}')
    if rep.get('signature'):
        log('signature plot: dt[s]  n  var/sec  kurtosis')
        for r in rep['signature']:
            log(f'  {r["dt"]:6d} {r["n"]:7d}  {r["var_per_sec"]:.3e}  {r["kurtosis"]:.2f}')
        log(f'  log-log slope of excess kurtosis on dt: {rep["kurtosis_slope"]:.2f}')
    if 'kramers_moyal' in rep and len(rep['kramers_moyal']):
        km = rep['kramers_moyal']
        log('Kramers-Moyal (median over level bins): ' + '  '.join(
            f'D{k} {np.median(km[:, k]):+.2e}' for k in range(1, 5)))
    if 'tsrv_sigma_per_sec' in rep:
        log(f'TSRV sigma per second {rep["tsrv_sigma_per_sec"]:.3e}')
    if 'ofi' in rep:
        o = rep['ofi']
        log(f'order-flow imbalance: slope {o["slope"]:.3e}, R2 {o["r2"]:.3f}, t {o["t"]:.1f} ({o["n"]} intervals)')
    if 'kyle' in rep:
        k = rep['kyle']
        log(f'Kyle lambda: {k["slope"]:.3e} per unit signed volume, R2 {k["r2"]:.3f}')
    if 'liquidity' in rep:
        q = rep['liquidity']
        log(f'liquidity: spread mean {q["spread_mean"]:.4g}, depth mean {q["depth_mean"]:.1f}, '
            f'imbalance {q["imbalance_mean"]:+.3f}, liquidity-void alarms {q["void_alarms"]}')
    if 'execution' in rep:
        e = rep['execution']
        log('execution (Almgren-Chriss, 1 hour, 60 steps): half-way holdings '
            f'{e["halfway"]:.3f} -> {e["halfway_kurtosis"]:.3f} with the fourth-moment penalty')
        for k, (v, src) in e['provenance'].items():
            log(f'  {k:16s} {v:.4g}  [{src}]')
    for what, why in rep['skipped'].items():
        log(f'skipped {what}: {why}')


def daily_signals(bf, x=None, dt=60):
    """Per-day regime signal: log(1 + excess kurtosis of dt-second increments) and void alarms."""
    x = bf.mid if x is None else x
    day = np.floor(bf.ts / DAY)
    rows = []
    for d in np.unique(day):
        m = day == d
        dx = _increments(bf.ts[m], x[m], dt)
        if len(dx) < 30:
            continue
        exc = max(float(stats.kurtosis(dx)), 0.0)
        voids = liquidity(BookFrame(*(getattr(bf, f)[m] for f in ('ts', 'bid', 'ask', 'bq', 'aq',
                                                                  'depth_bid', 'depth_ask'))))['void_alarms'] \
            if bf.has_depth else 0
        rows.append((datetime.fromtimestamp(d * DAY, timezone.utc).strftime('%Y-%m-%d'),
                     math.log1p(exc), exc, voids))
    return rows


# ---------------------------------------------------------------------------
# 6. Self-test and CLI
# ---------------------------------------------------------------------------

def selftest(tmpdir=None):
    import tempfile
    failures = []

    def check(name, ok, detail):
        print(f'[{"ok" if ok else "FAIL"}] {name}: {detail}')
        if not ok:
            failures.append(name)

    print('bsm_microstructure self-test (synthetic truth)')
    sig = 0.01
    diff = simulate('diffusion', days=20, sigma=sig, seed=1, book=False)
    rows = signature_plot(diff['ts'], diff['efficient'], (10, 60, 300, 1800))
    ks = [r['kurtosis'] for r in rows]
    check('signature plot, diffusion', all(2.3 < k < 3.9 for k in ks),
          'kurtosis ' + ', '.join(f'{r["kurtosis"]:.2f}@{r["dt"]}s' for r in rows))
    jump = simulate('jump', days=20, sigma=sig, seed=2, book=False)
    rows = signature_plot(jump['ts'], jump['efficient'], (10, 30, 60, 300, 900, 1800))
    slope = kurtosis_slope(rows)
    check('signature plot, jumps', rows[0]['kurtosis'] > 15 and -1.4 < slope < -0.7,
          'kurtosis ' + ', '.join(f'{r["kurtosis"]:.1f}@{r["dt"]}s' for r in rows) + f'; slope {slope:.2f}')

    km = kramers_moyal(diff['ts'], diff['efficient'], dt=10)
    d1, d2 = np.median(km[:, 1]), np.median(km[:, 2])
    check('Kramers-Moyal', abs(d2 / (sig ** 2 / 2) - 1) < 0.1 and abs(d1) < 0.1 * sig,
          f'D2 {d2:.3e} (truth {sig ** 2 / 2:.3e}), D1 {d1:+.1e}')

    noisy = simulate('diffusion', days=5, sigma=sig, noise=math.sqrt(32) * sig, seed=3, book=False)
    day = np.floor(noisy['ts'] / DAY)
    est, raw = zip(*(tsrv(noisy['mid'][day == d]) for d in np.unique(day)))
    iv = sig ** 2 * 23399
    check('TSRV under microstructure noise', abs(np.mean(est) / iv - 1) < 0.2 and np.mean(raw) / iv > 30,
          f'TSRV {np.mean(est):.3e} vs true {iv:.3e}; raw RV off by factor {np.mean(raw) / iv:.0f}')

    bf, tf = frames_from_sim(simulate('diffusion', days=3, sigma=sig, tick=0.01, seed=4))
    o = order_flow_imbalance(bf)
    k = kyle_lambda(bf, tf)
    check('order-flow imbalance and Kyle impact', o['slope'] > 0 and o['r2'] > 0.2 and k['slope'] > 0,
          f'OFI slope {o["slope"]:.2e}, R2 {o["r2"]:.2f}; Kyle slope {k["slope"]:.2e}, R2 {k["r2"]:.2f}')

    X, T, N, s, eta, lam = 1000.0, 3600.0, 60, 0.01, 1e-4, 1e-6
    cf = ac_closed_form(X, T, N, s, eta, lam)
    nm = ac_numeric(X, T, N, s, eta, lam)
    nk = ac_numeric(X, T, N, s, eta, lam, excess_kurt=20.0)
    check('Almgren-Chriss', np.max(np.abs(cf - nm)) / X < 1e-9 and nk[30] < nm[30],
          f'numerical vs closed form {np.max(np.abs(cf - nm)) / X:.1e} of position; half-way holdings '
          f'{nm[30] / X:.3f} -> {nk[30] / X:.3f} with kurtosis penalty')

    with tempfile.TemporaryDirectory(dir=tmpdir) as root:
        store = Store(root)
        src = SyntheticSource()
        recs = list(src.fetch(market='SYN:TEST', days=2, seconds=200, seed=5))
        store.append(recs[:300])
        store.append(recs[300:])
        back = store.read('synthetic', 'SYN:TEST')
        same = len(back) == len(recs) and all(to_json(a) == to_json(b) for a, b in
                                              zip(sorted(recs, key=lambda r: r.ts), back))
        check('append-only store round trip', same and store.days('synthetic', 'SYN:TEST') == ['2026-01-05', '2026-01-06'],
              f'{len(back)} records over {len(store.days("synthetic", "SYN:TEST"))} daily gzip files, FAT32-safe names')

        drop = os.path.join(root, 'drop')
        os.makedirs(drop)
        with open(os.path.join(drop, 'TEST_2026-01-05_34200000_57600000_message_1.csv'), 'w', newline='') as f:
            csv.writer(f).writerows([[34200.0, 1, 1, 100, 1000000, 1], [34201.0, 4, 1, 50, 1000000, 1],
                                     [34202.0, 1, 2, 80, 1000100, -1]])
        with open(os.path.join(drop, 'TEST_2026-01-05_34200000_57600000_orderbook_1.csv'), 'w', newline='') as f:
            csv.writer(f).writerows([[1000100, 80, 1000000, 100], [1000100, 80, 1000000, 50],
                                     [1000100, 160, 1000000, 50]])
        vrec = list(VendorDrop().fetch(drop, 'TEST'))
        nb = sum(r.type == 'book' for r in vrec)
        nt = [r for r in vrec if r.type == 'trade']
        check('vendor drop (LOBSTER)', nb == 3 and len(nt) == 1 and nt[0].side == -1 and abs(nt[0].price - 100.0) < 1e-9,
              f'{nb} book snapshots, {len(nt)} trade (seller-initiated at {nt[0].price if nt else float("nan")})')

    # Recorded Kalshi payload shapes (2026-10-30): dollar strings, and the legacy cent integers.
    new = {'orderbook_fp': {'no_dollars': [['0.4800', '100160.00'], ['0.5000', '150.00']],
                            'yes_dollars': [['0.4700', '100116.00'], ['0.4900', '106.00']]}}
    old = {'orderbook': {'no': [[48, 100160], [50, 150]], 'yes': [[47, 100116], [49, 106]]}}
    candle = {'end_period_ts': 1793354400, 'price': {}, 'volume_fp': '0.00',
              'yes_ask': {'close_dollars': '0.8600'}, 'yes_bid': {'close_dollars': '0.6900'}}
    row = {'ticker': 'KXINXU-26OCT30H1600-T7609.9999', 'yes_bid_dollars': '0.4900',
           'yes_ask_dollars': '0.5000', 'yes_bid_size_fp': '106.00', 'yes_ask_size_fp': '150.00'}
    b1, a1 = parse_orderbook(new)
    b2, a2 = parse_orderbook(old)
    snap = market_snapshot(row, 0.0)
    ok = (b1[0] == [0.49, 106.0] and a1[0] == [0.5, 150.0] and (b1, a1) == (b2, a2)
          and parse_candle(candle) == (1793354400.0, 0.69, 0.86, None, 0.0)
          and snap.bids == [[0.49, 106.0]] and snap.asks == [[0.5, 150.0]])
    check('Kalshi payload parsers', ok, f'best yes bid {b1[0]}, yes ask (1 - best no bid) {a1[0]}; '
          f'dollar and cent formats agree; candle {parse_candle(candle)[1:3]}')

    bf_c = BookFrame(bf.ts, bf.bid, bf.ask, np.zeros_like(bf.bq), np.zeros_like(bf.aq),
                     np.zeros_like(bf.bq), np.zeros_like(bf.aq))
    rep = analyze(bf_c, None, KalshiCandles.provides)
    check('plug-in capabilities and skip reasons',
          set(SOURCES) >= {'synthetic', 'kalshi_candles', 'kalshi_book', 'vendor_drop'}
          and 'Kyle impact' in rep['skipped'] and 'order-flow imbalance, liquidity' in rep['skipped'],
          f'sources {sorted(SOURCES)}; skipped: ' + '; '.join(f'{k} ({v})' for k, v in rep['skipped'].items()))

    b = simulate('diffusion', days=4, seconds=23400, sigma=0.02, binary=True, horizon_days=30.0, seed=6, book=False)
    expiry = b['ts'][0] + 30.0 * DAY
    w = latent_coordinate(b['ts'], b['mid'], expiry)
    rows = signature_plot(b['ts'], w, (60, 300, 1800))
    check('binary latent coordinate', all(2.0 < r['kurtosis'] < 4.5 for r in rows),
          'kurtosis in w ' + ', '.join(f'{r["kurtosis"]:.2f}@{r["dt"]}s' for r in rows) + ' (quotes in cents)')

    print(f'\n{"All checks passed." if not failures else "FAILED: " + ", ".join(failures)}')
    return not failures


def _parse_expiry(s):
    return datetime.fromisoformat(s).replace(tzinfo=timezone.utc).timestamp() if s else None


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split('\n\n')[0])
    ap.add_argument('--selftest', action='store_true')
    sub = ap.add_subparsers(dest='cmd')
    c = sub.add_parser('collect')
    c.add_argument('--source', required=True, choices=sorted(SOURCES))
    c.add_argument('--market', default=None, help='market ticker (all sources except kalshi_markets)')
    c.add_argument('--root', default='store')
    c.add_argument('--series', default=None)
    c.add_argument('--start', default=None)
    c.add_argument('--end', default=None)
    c.add_argument('--seconds', type=float, default=600)
    c.add_argument('--every', type=float, default=5.0)
    c.add_argument('--path', default=None)
    c.add_argument('--days', type=int, default=1)
    c.add_argument('--kind', default='diffusion', choices=['diffusion', 'jump'])
    for name in ('analyze', 'signals'):
        a = sub.add_parser(name)
        a.add_argument('--root', default='store')
        a.add_argument('--source', required=True)
        a.add_argument('--market', required=True)
        a.add_argument('--binary', action='store_true')
        a.add_argument('--expiry', default=None, help='ISO UTC resolution time for binaries')
        a.add_argument('--start', default=None)
        a.add_argument('--end', default=None)
        if name == 'signals':
            a.add_argument('--out', default='daily_signals.csv')
    args = ap.parse_args(argv)

    if args.selftest:
        return 0 if selftest() else 1
    if args.cmd == 'collect':
        src = SOURCES[args.source]()
        if args.source == 'synthetic':
            it = src.fetch(market=args.market, days=args.days, kind=args.kind)
        elif args.source == 'kalshi_candles':
            if not (args.series and args.start and args.end):
                ap.error('kalshi_candles needs --series, --start and --end (dates or ISO times)')
            it = src.fetch(args.market, args.series, args.start, args.end)
        elif args.source == 'kalshi_book':
            it = src.fetch(args.market, args.seconds, args.every)
        elif args.source == 'kalshi_markets':
            if not args.series:
                ap.error('kalshi_markets needs --series')
            it = src.fetch(args.series)
        else:
            if not args.path:
                ap.error('vendor_drop needs --path')
            it = src.fetch(args.path, args.market)
        if args.source != 'kalshi_markets' and not args.market:
            ap.error(f'{args.source} needs --market')
        folder = args.series if args.source == 'kalshi_markets' else None
        store, buf, total = Store(args.root), [], 0
        for rec in it:
            buf.append(rec)
            if len(buf) >= (50 if args.source == 'kalshi_book' else 5000):  # live polls flush often
                total += store.append(buf, folder)
                buf = []
        total += store.append(buf, folder)
        print(f'appended {total} records to {args.root}/{Store.safe(args.source)}/{Store.safe(folder or args.market)}')
        return 0
    if args.cmd in ('analyze', 'signals'):
        recs = Store(args.root).read(args.source, args.market, args.start, args.end)
        if not recs:
            print('no records found')
            return 1
        bf, tf = frames(recs)
        provides = SOURCES[args.source].provides if args.source in SOURCES else frozenset({'book', 'depth', 'trade'})
        expiry = _parse_expiry(args.expiry)
        if args.cmd == 'analyze':
            print_report(analyze(bf, tf, provides, args.binary, expiry))
        else:
            x = latent_coordinate(bf.ts, bf.mid, expiry) if (args.binary and expiry) else None
            rows = daily_signals(bf, x)
            with open(args.out, 'w', newline='') as f:
                wr = csv.writer(f)
                wr.writerow(['date', 'signal', 'excess_kurtosis_60s', 'void_alarms'])
                wr.writerows(rows)
            print(f'wrote {len(rows)} daily signals to {args.out}')
        return 0
    ap.print_help()
    return 0


if __name__ == '__main__':
    sys.exit(main())
