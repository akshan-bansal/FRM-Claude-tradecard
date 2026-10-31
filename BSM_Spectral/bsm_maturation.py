#!/usr/bin/env python3
"""bsm_maturation.py -- data maturation for the BSM spectral engine.

A forecast is journaled the moment it is made, together with every input it used. Its label is
unknown until the outcome exists -- the forward horizon has elapsed, or Kalshi has settled the
contract. `mature` fills in each label once it is knowable and stamps `matured_date`, the day it
became known, so evaluations can embargo on it. Hyperparameters are frozen in journal/config.json
before the first forward forecast (pre-registration): the forward record is never tuned on itself.

Every underlying registered in bsm_event.UNDERLYINGS is handled the same way (S&P 500, Nasdaq-100,
Dow, WTI, Bitcoin, Ether, EUR/USD, USD/JPY by default; `enabled` in config.json narrows the list).
Each gets its own frozen config on its first cycle; underlyings without an implied-vol index use a
point-in-time GJR forecast in its place.

Streams (per underlying)
  var     1- and 5-day VaR of rolling 21-day digitals (3 strikes x 5 models), marked to model
  kalshi  Kalshi binaries on the underlying (above/below and range ladders, one daily settlement):
          market quote and model p_Q at snapshot time; label = Kalshi's settlement. These are
          observed digital prices, which the paper's marked-to-model backtest lacks. A forecast is
          skipped when the latest close is more than `max_state_lag` business days old (FRED FX).
  cross   5-day VaR of the cross-asset book (Gaussian, per-factor GPD tails, joint FHS)
Already-matured history is backfilled where it exists: FRED daily series, and settled Kalshi
contracts with their pre-open (09:00 New York) quotes from hourly candles.

Sources: FRED fredgraph.csv (no key; SP500 covers only the last ten years) and Kalshi's public
market-data API (no account). Model state for a Kalshi contract is the last FRED close strictly
before its settlement day, so the model never sees the day it forecasts. Market quotes are compared
with the model only when taken before the 09:30 New York open of the settlement day (backfill uses
09:00); the quote can still know overnight futures moves. Schedule `run` for about 09:00 New York
on weekdays so every forward snapshot is comparable.

Run:  python bsm_maturation.py backfill [--days 40]   FRED history + settled Kalshi contracts
      python bsm_maturation.py freeze                  tune on history, freeze config (once)
      python bsm_maturation.py snapshot                journal forecasts as of the latest data
      python bsm_maturation.py mature                  resolve every label that is now known
      python bsm_maturation.py status                  maturity, pending counts, scorecard
      python bsm_maturation.py run                     refresh + mature + snapshot + status
      python bsm_maturation.py --selftest              offline checks
Layout: data/ (CSVs, data/kalshi/, data/vintages/), journal/ (append-only JSONL), store/ (ticks).
"""
from __future__ import annotations

import argparse
import csv
import gzip
import hashlib
import http.client
import json
import math
import os
import re
import sys
import time
import urllib.error
import urllib.request
from datetime import date, datetime, timedelta, timezone

import numpy as np
from scipy import optimize

import bsm_cross_asset as bx
import bsm_event as be
import bsm_microstructure as ms

FRED_URL = 'https://fred.stlouisfed.org/graph/fredgraph.csv?id={}'
FRED = {'SP500': 'SP500.csv', 'VIXCLS': 'VIXCLS.csv', 'DGS2': 'DGS2.csv', 'DGS10': 'DGS10.csv',
        'DEXUSEU': 'DEXUSEU.csv', 'DEXJPUS': 'DEXJPUS.csv'}
QGRID = np.concatenate([[0.001, 0.005], np.round(np.arange(0.01, 1.0, 0.01), 2), [0.995, 0.999]])
TARGET_WINDOWS = 250  # independent matured windows before coverage tests have reasonable power

DEFAULT_CFG = {
    'var': {'horizons': [1, 5], 'tenor_days': 21, 'moneyness': [0.97, 1.0, 1.03], 'position': 1.0,
            'levels': [0.90, 0.95, 0.99], 'n_sims': 4000, 'tail_frac': 0.10, 'tail_k': 4.0,
            'xi_max': 0.45, 'gammas': [1.0, 1.0, 1.0], 'rho_floor': 0.2, 'skew': -0.3, 'rate': 0.04,
            'dividend': 0.015, 'vol_floor': 0.02, 'level_map': [0.0, 1.0],
            'models': ['normal', 'gpd', 'skewt', 'fhs', 'spectral']},
    'kalshi': {'series': ['KXINXU', 'KXINX'], 'event_suffix': 'H1600', 'skew': -0.3, 'rate': 0.04,
               'dividend': 0.015, 'snapshot_hour_ny': 9},
    'cross': {'horizons': [5], 'window': 504, 'tail_k': 4.0, 'n_sims': 5000},
}
CROSS_FRED = ['DGS2', 'DGS10', 'DEXUSEU', 'DEXJPUS']
MAX_STATE_LAG = 3  # business days from the model's last close to settlement before a state is stale


def enabled_underlyings(cfg):
    return [k for k in (cfg or {}).get('enabled', list(be.UNDERLYINGS)) if k in be.UNDERLYINGS]


def fred_ids(cfg=None):
    ids = ['SP500', 'VIXCLS'] + CROSS_FRED
    for key in enabled_underlyings(cfg):
        u = be.UNDERLYINGS[key]
        ids += [u['price']] + ([u['vol']] if u['vol'] else [])
    return list(dict.fromkeys(ids))


def default_underlying_cfg(key):
    u = be.UNDERLYINGS[key]
    var = {**json.loads(json.dumps(DEFAULT_CFG['var'])), 'rate': u['rate'], 'dividend': u['dividend'],
           'skew': u['skew'], 'days_per_year': u['days_per_year']}
    kal = {'series': list(u['kalshi']), 'events': u['events'], 'skew': u['skew'], 'rate': u['rate'],
           'dividend': u['dividend'], 'snapshot_hour_ny': 9, 'max_state_lag': MAX_STATE_LAG,
           'days_per_year': u['days_per_year']}
    return var, kal


def ucfg(cfg, key, fallback=False):
    """(var config, kalshi config) of one underlying. The S&P keeps its original frozen sections;
    others live under cfg['underlyings'][key] once frozen (fallback=True returns registry defaults)."""
    if key == 'SPX':
        k = cfg['kalshi']
        return ({**cfg['var'], 'days_per_year': cfg['var'].get('days_per_year', 252)},
                {**k, 'events': k.get('events', k.get('event_suffix', 'H1600') + '$'),
                 'max_state_lag': k.get('max_state_lag', MAX_STATE_LAG), 'days_per_year': 252})
    sec = (cfg or {}).get('underlyings', {}).get(key)
    if sec:
        return sec['var'], sec['kalshi']
    return default_underlying_cfg(key) if fallback else (None, None)


def year_frac(state_date, expiry, days_per_year):
    """Model time from the state close to the settlement day: trading days for exchange-traded
    underlyings, calendar days for ones that trade every day."""
    if days_per_year >= 365:
        return max(int((np.datetime64(expiry, 'D') - np.datetime64(state_date, 'D')).astype(int)), 1) / 365.0
    return trading_days(state_date, expiry) / days_per_year


def load_cached(data_dir, key, cache):
    if key not in cache:
        try:
            cache[key] = be.load_underlying(data_dir, key)
        except FileNotFoundError:
            cache[key] = None
    return cache[key]


# ---------------------------------------------------------------------------
# Journal and time helpers
# ---------------------------------------------------------------------------

def _default(o):
    if isinstance(o, (np.floating, np.integer)):
        return o.item()
    if isinstance(o, np.ndarray):
        return o.tolist()
    if isinstance(o, (np.datetime64, date)):
        return str(o)
    raise TypeError(type(o))


def append_jsonl(path, rows):
    if not rows:
        return 0
    os.makedirs(os.path.dirname(path) or '.', exist_ok=True)
    with open(path, 'a', encoding='utf-8') as f:
        for r in rows:
            f.write(json.dumps(r, separators=(',', ':'), default=_default) + '\n')
    return len(rows)


def read_jsonl(path):
    if not os.path.exists(path):
        return []
    with open(path, encoding='utf-8') as f:
        return [json.loads(line) for line in f if line.strip()]


def jfile(jdir, name):
    return os.path.join(jdir, f'{name}.jsonl')


def now_iso():
    return datetime.now(timezone.utc).isoformat(timespec='seconds')


def ny_offset_hours(ts):
    """US Eastern offset from UTC (EDT -4 from the second Sunday of March, EST -5 from the first
    Sunday of November), computed directly so no tz database is needed."""
    d = datetime.fromtimestamp(ts, timezone.utc)
    start = datetime(d.year, 3, 8, 7, tzinfo=timezone.utc)
    start += timedelta(days=(6 - start.weekday()) % 7)
    end = datetime(d.year, 11, 1, 6, tzinfo=timezone.utc)
    end += timedelta(days=(6 - end.weekday()) % 7)
    return -4 if start <= d < end else -5


def ny_date(ts):
    return datetime.fromtimestamp(ts + 3600 * ny_offset_hours(ts), timezone.utc).date()


def ny_ts(day, hour):
    """UTC epoch of `hour`:00 New York time on `day`."""
    base = datetime(day.year, day.month, day.day, hour, tzinfo=timezone.utc).timestamp()
    return base - 3600 * ny_offset_hours(base + 6 * 3600)


def trading_days(a, b):
    """Business days from date a to date b (holidays ignored), at least one."""
    return max(int(np.busday_count(np.datetime64(a, 'D'), np.datetime64(b, 'D'))), 1)


def load_config(jdir, required=True):
    path = os.path.join(jdir, 'config.json')
    if not os.path.exists(path):
        if required:
            raise SystemExit('journal/config.json missing: run `python bsm_maturation.py freeze` first '
                             '(or `run`, which freezes defaults on first use)')
        return None
    with open(path, encoding='utf-8') as f:
        return json.load(f)


# ---------------------------------------------------------------------------
# Backfill: FRED daily series (with revision vintages) and settled Kalshi contracts
# ---------------------------------------------------------------------------

def http_text(url, timeout=60, retries=3):
    """GET with retries on transient transport failures (TLS errors, resets, truncated reads)."""
    for attempt in range(retries):
        req = urllib.request.Request(url, headers={'User-Agent': 'bsm-maturation/1.0'})
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return r.read().decode('utf-8')
        except (OSError, http.client.HTTPException) as exc:
            retryable = not isinstance(exc, urllib.error.HTTPError) or exc.code in (429, 500, 502, 503)
            if retryable and attempt + 1 < retries:
                time.sleep(2.0 * (attempt + 1))
                continue
            raise


def replace_with_retry(tmp, path, tries=6, pause=0.5):
    """os.replace that waits out transient locks (OneDrive sync or antivirus holding the file)."""
    for attempt in range(tries):
        try:
            os.replace(tmp, path)
            return
        except PermissionError:
            if attempt + 1 == tries:
                raise
            time.sleep(pause * (attempt + 1))


def _refresh_one(sid, data_dir, fetch, stamp, log):
    text = fetch(FRED_URL.format(sid))
    path = os.path.join(data_dir, f'{sid}.csv')
    tmp = path + '.tmp'
    with open(tmp, 'w', encoding='utf-8', newline='') as f:
        f.write(text)
    nd, nv = be.load_series(tmp)
    revised = dropped = 0
    if os.path.exists(path):
        od, ov = be.load_series(path)
        common, io, inn = np.intersect1d(od, nd, return_indices=True)
        revised = int(np.sum(np.abs(ov[io] - nv[inn]) > 1e-9))
        dropped = len(od) - len(common)
        if revised or dropped:
            vdir = os.path.join(data_dir, 'vintages')
            os.makedirs(vdir, exist_ok=True)
            with open(path, 'rb') as src, gzip.open(os.path.join(vdir, f'{sid}_{stamp}.csv.gz'), 'wb') as dst:
                dst.write(src.read())
    replace_with_retry(tmp, path)
    log(f'  FRED {sid:8s} {len(nd):6d} obs {nd[0]} .. {nd[-1]}'
        + (f'  ({revised} revised, {dropped} dropped; old file kept in vintages/)' if revised or dropped else ''))
    return {'fetched_at': now_iso(), 'series': sid, 'rows': len(nd), 'first': str(nd[0]), 'last': str(nd[-1]),
            'sha256': hashlib.sha256(text.encode()).hexdigest(), 'revised': revised, 'dropped': dropped}


def refresh_fred(data_dir, jdir, fetch=http_text, log=print, ids=None):
    """Fetch FRED series (default: the S&P and cross-asset set); if history changed, keep the old
    file in data/vintages/. Each series is independent: one that fails (network, or a file locked
    by OneDrive sync) is reported after all the others have been refreshed."""
    os.makedirs(data_dir, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')
    rows, failed = [], []
    for sid in (ids or list(FRED)):
        try:
            rows.append(_refresh_one(sid, data_dir, fetch, stamp, log))
        except Exception as exc:
            failed.append(f'{sid} ({type(exc).__name__}: {exc})')
            log(f'  FRED {sid:8s} FAILED: {type(exc).__name__}: {exc}')
    append_jsonl(jfile(jdir, 'fetch_log'), rows)
    if failed:
        raise RuntimeError('FRED refresh failed for ' + '; '.join(failed))
    return rows


KALSHI_COLUMNS = ['series', 'event', 'ticker', 'strike_type', 'floor', 'cap', 'close_time', 'expiry',
                  'quote_ts', 'bid', 'ask', 'result', 'settlement', 'state_date', 'S0', 'v0']


def _read_csv(path):
    if not os.path.exists(path):
        return []
    with open(path, newline='', encoding='utf-8') as f:
        return list(csv.DictReader(f))


def _state_before(dates, S, V, day):
    """Last close strictly before `day` (point in time for a contract settling on `day`)."""
    i = int(np.searchsorted(dates, np.datetime64(day, 'D'))) - 1
    if i < 0:
        return None
    return str(dates[i]), float(S[i]), float(V[i]) / 100.0


def select_contracts(markets, S0, v0, T, max_strikes=12):
    """Contracts to record for one settlement day, chosen from the prior close (never the outcome).

    A range ladder (any 'between' bucket) is kept whole. For above/below strikes, keep those within
    max(1%, 2.5 sigma sqrt(T)) of the prior close and thin them to max_strikes evenly spaced ones.
    """
    if any(m.get('strike_type') == 'between' for m in markets):
        return markets
    width = max(0.01, 2.5 * v0 * math.sqrt(T))
    near = sorted((m for m in markets if m.get('floor_strike') is not None
                   and abs(m['floor_strike'] / S0 - 1) <= width), key=lambda m: m['floor_strike'])
    if len(near) <= max_strikes:
        return near
    keep = sorted(set(np.round(np.linspace(0, len(near) - 1, max_strikes)).astype(int)))
    return [near[i] for i in keep]


def backfill_kalshi(data_dir, series, days=40, key=None, kcfg=None, max_strikes=12, log=print):
    """Settled contracts of the last `days` daily settlements with their 09:00 New York quotes.

    Incremental: events already in the CSV are skipped. Settlement days whose model state would be
    stale (more than max_state_lag business days old) are skipped and counted.
    """
    key = key or be.underlying_of_series(series) or 'SPX'
    kcfg = kcfg or default_underlying_cfg(key)[1]
    pattern = re.compile(kcfg['events'])
    dpy = kcfg.get('days_per_year', 252)
    out = os.path.join(data_dir, 'kalshi', f'{series}_settled.csv')
    done = {r['event'] for r in _read_csv(out)}
    dates, S, V = be.load_underlying(data_dir, key)
    events = []
    for ev in ms.kalshi_paged('/events', 'events', series_ticker=series, status='settled', limit=200):
        if not pattern.search(ev['event_ticker']):
            continue
        events.append(ev['event_ticker'])
        if len(events) >= days:
            break
    todo = [e for e in reversed(events) if e not in done]
    log(f'  Kalshi {series} ({key}): {len(events)} recent settled daily events, {len(todo)} new to backfill')
    rows, requests, stale = [], 0, 0
    for ev in todo:
        markets = list(ms.kalshi_paged('/markets', 'markets', event_ticker=ev, limit=1000))
        requests += 1
        if not markets:
            continue
        close_ts = ms._epoch(markets[0]['close_time'])
        expiry = ny_date(close_ts)
        state = _state_before(dates, S, V, expiry)
        if state is None:
            continue
        sd, S0, v0 = state
        if trading_days(sd, expiry) > kcfg.get('max_state_lag', MAX_STATE_LAG):
            stale += 1
            continue
        snap = ny_ts(expiry, kcfg.get('snapshot_hour_ny', 9))
        settled = [m for m in markets if m.get('result') in ('yes', 'no')]
        for m in select_contracts(settled, S0, v0, year_frac(sd, expiry, dpy), max_strikes):
            bid = ask = qts = None
            open_ts = ms._epoch(m['open_time'])
            if open_ts < snap:
                cs = ms.kalshi_get(f'/series/{series}/markets/{m["ticker"]}/candlesticks',
                                   start_ts=open_ts, end_ts=int(snap), period_interval=60)
                requests += 1
                for c in cs.get('candlesticks', []):
                    tt, b, a, _, _ = ms.parse_candle(c)
                    if tt <= snap and b is not None and a is not None:
                        bid, ask, qts = b, a, tt
                time.sleep(ms.KALSHI_PAUSE)
            rows.append({'series': series, 'event': ev, 'ticker': m['ticker'], 'strike_type': m.get('strike_type'),
                         'floor': m.get('floor_strike'), 'cap': m.get('cap_strike'), 'close_time': m['close_time'],
                         'expiry': str(expiry), 'quote_ts': qts, 'bid': bid, 'ask': ask,
                         'result': 1 if m['result'] == 'yes' else 0, 'settlement': m.get('expiration_value'),
                         'state_date': sd, 'S0': S0, 'v0': v0})
        time.sleep(ms.KALSHI_PAUSE)
    if rows:
        os.makedirs(os.path.dirname(out), exist_ok=True)
        new = not os.path.exists(out)
        with open(out, 'a', newline='', encoding='utf-8') as f:
            w = csv.DictWriter(f, fieldnames=KALSHI_COLUMNS)
            if new:
                w.writeheader()
            w.writerows(rows)
    log(f'  Kalshi {series} ({key}): {len(rows)} settled contracts added ({requests} requests'
        + (f'; {stale} days skipped with a stale state' if stale else '') + f') -> {out}')
    return rows


# ---------------------------------------------------------------------------
# Model probability of a Kalshi contract
# ---------------------------------------------------------------------------

def kalshi_prob(strike_type, floor, cap, S0, v0, T, skew=-0.3, rate=0.04, dividend=0.015):
    """p_Q of the contract's event under the smile-consistent BSM digital."""
    def dig(K):
        return float(be.BSMDigital(float(K), T, rate, dividend, skew).probability(S0, v0))
    if strike_type in ('greater', 'greater_or_equal'):
        return dig(floor)
    if strike_type in ('less', 'less_or_equal'):
        return 1.0 - dig(cap)
    if strike_type == 'between':
        return max(dig(floor) - dig(cap), 0.0)
    return float('nan')


def _f(x):
    return float('nan') if x in (None, '', 'None') else float(x)


# ---------------------------------------------------------------------------
# Snapshots: journal forecasts as of the latest data
# ---------------------------------------------------------------------------

def _seed(*parts):
    return int(hashlib.sha256('|'.join(map(str, parts)).encode()).hexdigest()[:12], 16)


def var_snapshot(data_dir, jdir, cfg, log=print, key='SPX'):
    c, _ = ucfg(cfg, key)
    if c is None:
        log(f'  var {key}: no frozen config yet; skipped')
        return []
    dpy = c.get('days_per_year', 252)
    dates, S, V = be.load_underlying(data_dir, key)
    asof = str(dates[-1])
    if any(r['asof'] == asof and r.get('underlying', 'SPX') == key for r in read_jsonl(jfile(jdir, 'forecasts_var'))):
        log(f'  var {key}: forecasts as of {asof} already journaled')
        return []
    rS, rV = np.diff(np.log(S)), np.diff(V / 100.0)
    pS, s2S, zS = be.fit_gjr(rS)
    pV, s2V, zV = be.fit_gjr(rV)
    rho = float(np.corrcoef(zS, zV)[0, 1])
    f1, f2 = be.unmix_eigen(zS, zV, rho)
    laws = {'normal': (be.NormalLaw(), be.NormalLaw()),
            'gpd': tuple(be.SemiParamGPD(f, c['tail_frac'], c['tail_k'], c['xi_max']) for f in (f1, f2)),
            'skewt': (be.HansenSkewT(f1), be.HansenSkewT(f2))}
    rows_fhs = np.column_stack([zS, zV])
    vmap = lambda x: c['level_map'][0] + c['level_map'][1] * x / 100.0
    S0, v0 = float(S[-1]), max(vmap(V[-1]), c['vol_floor'])
    U = np.clip(np.random.default_rng(_seed('var', asof) if key == 'SPX' else _seed('var', key, asof))
                .random((c['n_sims'], 2)), 1e-12, 1 - 1e-12)
    T = c['tenor_days'] / dpy
    levels = np.array(c['levels'])
    out = []
    for H in c['horizons']:
        hy = H / dpy
        hS = math.sqrt(be.horizon_variance(s2S[-1], pS, H))
        hV = math.sqrt(be.horizon_variance(s2V[-1], pV, H))
        for mi, mny in enumerate(c['moneyness']):
            con = be.BSMDigital(mny * S0, T, c['rate'], c['dividend'], c['skew'])
            p0 = float(con.price(S0, v0))
            rho_t = be.temper_rho(rho, c['gammas'][mi], c['rho_floor'])
            for name in c['models']:
                base = 'gpd' if name == 'spectral' else name
                if base == 'fhs':
                    idx = np.minimum((U[:, 0] * len(rows_fhs)).astype(int), len(rows_fhs) - 1)
                    x1, x2 = rows_fhs[idx, 0], rows_fhs[idx, 1]
                else:
                    l1, l2 = laws[base]
                    x1, x2 = be.mix_eigen(l1.ppf(U[:, 0]), l2.ppf(U[:, 1]), rho_t)
                S_new = S0 * np.exp(pS.mu * H + hS * x1)
                v_new = np.maximum(v0 + pV.mu * H + hV * x2, c['vol_floor'])
                extra = {}
                if name == 'spectral':
                    xs = np.column_stack([S_new - S0, v_new - v0])
                    Sig = np.cov(xs.T)
                    L = be.cov_factor(Sig)
                    z = np.linalg.solve(L, xs.T).T
                    radius = float(np.quantile(np.linalg.norm(z, axis=1), levels.max()))
                    rv = be.risk_vector(con, (S0, v0), Sig, hy, c['position'], radius=radius, n_theta=360)
                    pnl = rv.jet(z) if rv.action == 'taylor4' else c['position'] * (con.price(S_new, v_new, T - hy) - p0)
                    extra = {'action': rv.action, 'ratio_gate': rv.ratio_gate, 'crossing': rv.crossing, 'radius': radius}
                else:
                    pnl = c['position'] * (con.price(S_new, v_new, T - hy) - p0)
                loss = -pnl
                fid = f'var|{asof}|h{H}|m{mny}|{name}' if key == 'SPX' else f'var|{key}|{asof}|h{H}|m{mny}|{name}'
                out.append({'id': fid, 'stream': 'var', 'made_at': now_iso(), 'underlying': key,
                            'asof': asof, 'horizon': H, 'moneyness': mny, 'model': name, 'strike': mny * S0,
                            'S0': S0, 'v0': v0, 'tenor': T, 'p0': p0, 'position': c['position'],
                            'contract': {'rate': c['rate'], 'dividend': c['dividend'], 'skew': c['skew'],
                                         'vol_floor': c['vol_floor'], 'level_map': c['level_map'],
                                         'days_per_year': dpy},
                            'levels': c['levels'], 'var': np.quantile(loss, levels).round(10),
                            'quantiles': np.quantile(loss, QGRID).round(10), **extra})
    append_jsonl(jfile(jdir, 'forecasts_var'), out)
    vol_name = be.UNDERLYINGS[key]['vol'] or 'GJR vol'
    log(f'  var {key}: journaled {len(out)} forecasts as of {asof} (price {S0:.6g}, {vol_name} {V[-1]:.2f}; '
        f'horizons {c["horizons"]})')
    return out


def cross_snapshot(data_dir, jdir, cfg, log=print):
    c = cfg['cross']
    dates, levels = bx.load_factors(data_dir)
    asof = str(dates[-1])
    if any(r['asof'] == asof for r in read_jsonl(jfile(jdir, 'forecasts_cross'))):
        log(f'  cross: forecasts as of {asof} already journaled')
        return []
    states = bx.to_state(levels)
    R = bx.factor_returns(states)
    fits = [be.fit_gjr(R[:, j]) for j in range(R.shape[1])]
    Z = np.column_stack([f[2] for f in fits])[-c['window']:]
    C = np.corrcoef(Z.T)
    st0 = states[-1]
    book = bx.CrossAssetBook(S0=float(st0[0]), y0=float(st0[3]))
    rng = np.random.default_rng(_seed('cross', asof))
    out = []
    for H in c['horizons']:
        hv = np.array([be.horizon_variance(f[1][-1], f[0], H) for f in fits])
        mu = np.array([f[0].mu for f in fits])
        g = rng.standard_normal((c['n_sims'], len(bx.FACTORS))) @ np.linalg.cholesky(C).T
        laws = [be.SemiParamGPD(Z[:, j], 0.10, c['tail_k']) for j in range(len(bx.FACTORS))]
        u = np.clip(be.ndtr(g), 1e-12, 1 - 1e-12)
        draws = {'gaussian': g,
                 'gpd': np.column_stack([laws[j].ppf(u[:, j]) for j in range(len(bx.FACTORS))]),
                 'fhs': Z[rng.integers(0, len(Z), size=(c['n_sims'], H))].sum(axis=1) / math.sqrt(H)}
        v0 = book.value(st0, 0.0)
        for name, d in draws.items():
            loss = v0 - book.value(bx.apply_returns(st0, (H * mu + d * np.sqrt(hv)).T), H)
            out.append({'id': f'cross|{asof}|h{H}|{name}', 'stream': 'cross', 'made_at': now_iso(), 'asof': asof,
                        'horizon': H, 'model': name, 'state': st0, 'book': {'S0': book.S0, 'y0': book.y0,
                                                                           'units': book.units},
                        'levels': [0.95, 0.99], 'var': np.quantile(loss, [0.95, 0.99]).round(4),
                        'quantiles': np.quantile(loss, QGRID).round(4)})
    append_jsonl(jfile(jdir, 'forecasts_cross'), out)
    log(f'  cross: journaled {len(out)} book forecasts as of {asof}')
    return out


def kalshi_snapshot(data_dir, jdir, store_root, cfg, log=print):
    """Journal every open daily contract of every enabled underlying with the model's p_Q."""
    ts = time.time()
    store = ms.Store(store_root)
    out, cache, counts = [], {}, []
    for key in enabled_underlyings(cfg):
        _, c = ucfg(cfg, key, fallback=True)
        if not c['series']:
            continue
        data = load_cached(data_dir, key, cache)
        if data is None:
            log(f'  kalshi {key}: no price data; skipped')
            continue
        dates, S, V = data
        pattern = re.compile(c['events'])
        dpy = c.get('days_per_year', 252)
        n0, stale = len(out), 0
        for series in c['series']:
            markets = [m for m in ms.kalshi_paged('/markets', 'markets', series_ticker=series, status='open',
                                                  limit=1000) if pattern.search(m['event_ticker'])]
            store.append([ms.market_snapshot(m, ts) for m in markets], folder=series)
            for m in markets:
                expiry = ny_date(ms._epoch(m['close_time']))
                state = _state_before(dates, S, V, expiry)
                if state is None:
                    continue
                sd, S0, v0 = state
                # stale = the latest close is old relative to *now* (e.g. FRED's lagged FX rates);
                # a contract settling days ahead is not stale merely because settlement is far off
                if trading_days(sd, ny_date(ts)) > c.get('max_state_lag', MAX_STATE_LAG):
                    stale += 1
                    continue
                T = year_frac(sd, expiry, dpy)
                p = kalshi_prob(m.get('strike_type'), m.get('floor_strike'), m.get('cap_strike'), S0, v0, T,
                                c['skew'], c['rate'], c['dividend'])
                out.append({'id': f'kalshi|{m["ticker"]}|{int(ts)}', 'stream': 'kalshi', 'made_at': now_iso(),
                            'underlying': key, 'ts': ts, 'series': series, 'event': m['event_ticker'],
                            'ticker': m['ticker'], 'strike_type': m.get('strike_type'), 'floor': m.get('floor_strike'),
                            'cap': m.get('cap_strike'), 'close_time': m['close_time'], 'expiry': str(expiry),
                            'bid': ms._price(m, 'yes_bid'), 'ask': ms._price(m, 'yes_ask'),
                            'bid_size': ms._num(m, 'yes_bid_size'), 'ask_size': ms._num(m, 'yes_ask_size'),
                            'state_date': sd, 'S0': S0, 'v0': v0, 'T': T, 'p_model': p})
            time.sleep(ms.KALSHI_PAUSE)
        counts.append(f'{key} {len(out) - n0}' + (f' ({stale} stale skipped)' if stale else ''))
    append_jsonl(jfile(jdir, 'forecasts_kalshi'), out)
    log(f'  kalshi: journaled {len(out)} open contracts with model p_Q -- ' + ', '.join(counts)
        + f'; ladders also appended to {store_root}/kalshi_markets/')
    return out


# ---------------------------------------------------------------------------
# Maturation: resolve labels that are now knowable
# ---------------------------------------------------------------------------

def _pit(quantiles, x):
    return float(np.clip(np.interp(x, quantiles, QGRID, left=0.0, right=1.0), 0.0005, 0.9995))


def var_mature(data_dir, jdir, log=print):
    done = {r['id'] for r in read_jsonl(jfile(jdir, 'matured_var'))}
    out, waiting, cache = [], 0, {}
    for r in read_jsonl(jfile(jdir, 'forecasts_var')):
        if r['id'] in done:
            continue
        key = r.get('underlying', 'SPX')
        data = load_cached(data_dir, key, cache)
        if data is None:
            waiting += 1
            continue
        dates, S, V = data
        i = int(np.searchsorted(dates, np.datetime64(r['asof'])))
        j = i + r['horizon']
        if i >= len(dates) or str(dates[i]) != r['asof'] or j >= len(dates):
            waiting += 1
            continue
        k = r['contract']
        con = be.BSMDigital(r['strike'], r['tenor'], k['rate'], k['dividend'], k['skew'])
        v1 = max(k['level_map'][0] + k['level_map'][1] * V[j] / 100.0, k['vol_floor'])
        dpy = k.get('days_per_year', 252)
        loss = -r['position'] * (float(con.price(S[j], v1, r['tenor'] - r['horizon'] / dpy)) - r['p0'])
        out.append({'id': r['id'], 'matured_date': str(dates[j]), 'matured_at': now_iso(), 'asof': r['asof'],
                    'underlying': key,
                    'horizon': r['horizon'], 'moneyness': r['moneyness'], 'model': r['model'],
                    'realized_loss': loss, 'breach': [bool(loss > v) for v in r['var']],
                    'pinball': [float(be.pinball(loss, v, lv)) for v, lv in zip(r['var'], r['levels'])],
                    'pit': _pit(r['quantiles'], loss)})
    append_jsonl(jfile(jdir, 'matured_var'), out)
    log(f'  var: {len(out)} forecasts matured, {waiting} still maturing')
    return out


def cross_mature(data_dir, jdir, log=print):
    dates, levels = bx.load_factors(data_dir)
    states = bx.to_state(levels)
    R = bx.factor_returns(states)
    done = {r['id'] for r in read_jsonl(jfile(jdir, 'matured_cross'))}
    out, waiting = [], 0
    for r in read_jsonl(jfile(jdir, 'forecasts_cross')):
        if r['id'] in done:
            continue
        i = int(np.searchsorted(dates, np.datetime64(r['asof'])))
        H = r['horizon']
        if i >= len(dates) or str(dates[i]) != r['asof'] or i + H >= len(dates):
            waiting += 1
            continue
        b = r['book']
        book = bx.CrossAssetBook(S0=b['S0'], y0=b['y0'], units=np.array(b['units']))
        st0 = np.array(r['state'])
        loss = float(book.value(st0, 0.0) - book.value(bx.apply_returns(st0, R[i:i + H].sum(0)), H))
        out.append({'id': r['id'], 'matured_date': str(dates[i + H]), 'matured_at': now_iso(), 'asof': r['asof'],
                    'horizon': H, 'model': r['model'], 'realized_loss': loss,
                    'breach': [bool(loss > v) for v in r['var']], 'pit': _pit(r['quantiles'], loss)})
    append_jsonl(jfile(jdir, 'matured_cross'), out)
    log(f'  cross: {len(out)} forecasts matured, {waiting} still maturing')
    return out


def kalshi_mature(jdir, log=print):
    done = {r['id'] for r in read_jsonl(jfile(jdir, 'matured_kalshi'))}
    pending = [r for r in read_jsonl(jfile(jdir, 'forecasts_kalshi')) if r['id'] not in done]
    now = time.time()
    events = sorted({r['event'] for r in pending if ms._epoch(r['close_time']) < now})
    results = {}
    for ev in events:
        for m in ms.kalshi_paged('/markets', 'markets', event_ticker=ev, limit=1000):
            if m.get('result') in ('yes', 'no'):
                results[m['ticker']] = (1 if m['result'] == 'yes' else 0, m.get('expiration_value'))
        time.sleep(ms.KALSHI_PAUSE)
    out = []
    for r in pending:
        if r['ticker'] in results:
            y, val = results[r['ticker']]
            out.append({'id': r['id'], 'matured_date': r['expiry'], 'matured_at': now_iso(),
                        'ticker': r['ticker'], 'outcome': y, 'settlement': val})
    append_jsonl(jfile(jdir, 'matured_kalshi'), out)
    log(f'  kalshi: {len(out)} contracts settled and matured, {len(pending) - len(out)} still open or unsettled')
    return out


# ---------------------------------------------------------------------------
# Status and scorecard
# ---------------------------------------------------------------------------

def _independent(asofs, h):
    """Indices of a non-overlapping subsequence (consecutive picks >= h business days apart)."""
    keep, last = [], None
    for i, a in sorted(enumerate(asofs), key=lambda x: x[1]):
        d = np.datetime64(a, 'D')
        if last is None or np.busday_count(last, d) >= h:
            keep.append(i)
            last = d
    return keep


def var_scorecard(jdir, log=print):
    fc = {r['id']: r for r in read_jsonl(jfile(jdir, 'forecasts_var'))}
    mat = read_jsonl(jfile(jdir, 'matured_var'))
    if not fc:
        log('  var: nothing journaled yet')
        return
    by = {}
    for r in mat:
        by.setdefault((r.get('underlying', 'SPX'), r['horizon'], r['model']), []).append(r)
    asofs = sorted({r['asof'] for r in fc.values()})
    unds = sorted({r.get('underlying', 'SPX') for r in fc.values()})
    log(f'  var: {len(fc)} forecasts over {len(asofs)} snapshot days ({asofs[0]} .. {asofs[-1]}) for '
        f'{", ".join(unds)}; {len(mat)} matured')
    if not mat:
        return
    log('  underlying horizon model     matured indep  level breach%  Kupiec p  Chr. cc p  Berk. p   ETA to 250 indep')
    for (und, h, model), rows in sorted(by.items()):
        rows = sorted(rows, key=lambda r: r['asof'])
        mid = [r for r in rows if r['moneyness'] == 1.0]
        ind = [mid[i] for i in _independent([r['asof'] for r in mid], h)]
        last = np.datetime64(max(r['asof'] for r in rows), 'D')
        eta = np.busday_offset(last, max(TARGET_WINDOWS - len(ind), 0) * h, roll='forward')
        for li, lv in enumerate(fc[rows[0]['id']]['levels']):
            hits = np.array([r['breach'][li] for r in ind])
            allhits = np.array([r['breach'][li] for r in rows])
            kp = be.kupiec(hits, 1 - lv)[1] if len(hits) else float('nan')
            cc = be.christoffersen(hits, 1 - lv)['p_cc'] if len(hits) > 2 else float('nan')
            bk = be.berkowitz([r['pit'] for r in ind])['p'] if len(ind) >= 10 else float('nan')
            log(f'  {und:10s} {h:4d}d  {model:9s} {len(rows):6d} {len(ind):5d}   {lv:4.2f}  {100 * allhits.mean():6.2f}   '
                f'{kp:7.3f}   {cc:7.3f}   {bk:6.3f}   {eta}')


def gjr_vol_asof(dates, S, state_date, cache, days_per_year=252):
    """Annualised one-step-ahead GJR-GARCH vol using closes through state_date only."""
    if state_date not in cache:
        i = int(np.searchsorted(dates, np.datetime64(state_date), side='right'))
        _, s2, _ = be.fit_gjr(np.diff(np.log(S[:i])))
        cache[state_date] = math.sqrt(days_per_year * s2[-1])
    return cache[state_date]


def fit_ladder(legs, skew=-0.3, rate=0.04, dividend=0.015):
    """Fit level S and vol v of the BSM digital curve to one quote-time ladder (weights 1/spread).

    Uses only the quotes themselves, so it is point in time; returns (S, v, fitted probabilities).
    The fitted curve is a coherent distribution across strikes, free of one-sided and stale mids."""
    S0, v0, T = legs[0]['S0'], legs[0]['v0'], legs[0]['T']
    mid = np.array([(r['bid'] + r['ask']) / 2 for r in legs])
    spr = np.array([max(r['ask'] - r['bid'], 0.01) for r in legs])

    def probs(S, v):
        return np.array([kalshi_prob(r['strike_type'], r['floor'], r['cap'], S, v, T, skew, rate, dividend)
                         for r in legs])

    fit = optimize.least_squares(lambda th: (probs(S0 * np.exp(th[0]), v0 * np.exp(th[1])) - mid) / spr, [0.0, 0.0])
    S, v = S0 * math.exp(fit.x[0]), v0 * math.exp(fit.x[1])
    return S, v, probs(S, v)


def _kalshi_rows(data_dir, jdir, cfg):
    """Matured Kalshi contracts of every enabled underlying: backfilled settled history plus matured
    forward snapshots.

    Besides the journaled p (vol index or GJR stand-in, frozen config), each row carries p_gjr: the
    same prior-close model with a one-step GJR-GARCH vol fitted on closes through the state date."""
    mat = {r['id']: r for r in read_jsonl(jfile(jdir, 'matured_kalshi'))}
    forward = [r for r in read_jsonl(jfile(jdir, 'forecasts_kalshi')) if r['id'] in mat]
    rows, cache = [], {}
    for key in enabled_underlyings(cfg):
        _, k = ucfg(cfg, key, fallback=True)
        data = load_cached(data_dir, key, cache)
        if data is None or not k['series']:
            continue
        dates, S, _ = data
        dpy = k.get('days_per_year', 252)
        vcache = {}
        args = (k['skew'], k['rate'], k['dividend'])
        for series in k['series']:
            for r in _read_csv(os.path.join(data_dir, 'kalshi', f'{series}_settled.csv')):
                T = year_frac(r['state_date'], r['expiry'], dpy)
                S0, v0 = float(r['S0']), float(r['v0'])
                vg = gjr_vol_asof(dates, S, r['state_date'], vcache, dpy)
                rows.append({'source': 'backfill', 'underlying': key, 'series': series, 'event': r['event'],
                             'ticker': r['ticker'], 'expiry': r['expiry'], 'quote_date': r['expiry'],
                             'bid': _f(r['bid']), 'ask': _f(r['ask']), 'y': int(r['result']),
                             'strike_type': r['strike_type'], 'floor': _f(r['floor']), 'cap': _f(r['cap']),
                             'S0': S0, 'v0': v0, 'T': T,
                             'p': kalshi_prob(r['strike_type'], _f(r['floor']), _f(r['cap']), S0, v0, T, *args),
                             'p_gjr': kalshi_prob(r['strike_type'], _f(r['floor']), _f(r['cap']), S0, vg, T, *args)})
        first_daily = {}
        for r in forward:
            if r.get('underlying', be.underlying_of_series(r['series'])) != key:
                continue
            fk = (r['ticker'], ny_date(r['ts']))
            if fk not in first_daily or r['ts'] < first_daily[fk]['ts']:
                first_daily[fk] = r
        for r in first_daily.values():
            # The model state is the prior close, so market quotes are comparable only before the open;
            # later quotes still count for model calibration but not for market-vs-model scoring.
            local = datetime.fromtimestamp(r['ts'] + 3600 * ny_offset_hours(r['ts']), timezone.utc)
            pre_open = local.hour + local.minute / 60 < 9.5 and str(local.date()) == r['expiry']
            vg = gjr_vol_asof(dates, S, r['state_date'], vcache, dpy)
            rows.append({'source': 'forward', 'underlying': key, 'series': r['series'], 'event': r['event'],
                         'ticker': r['ticker'], 'expiry': r['expiry'], 'quote_date': str(ny_date(r['ts'])),
                         'bid': _f(r['bid']) if pre_open else float('nan'),
                         'ask': _f(r['ask']) if pre_open else float('nan'), 'y': mat[r['id']]['outcome'],
                         'strike_type': r['strike_type'], 'floor': _f(r['floor']), 'cap': _f(r['cap']),
                         'p': r['p_model'], 'S0': r['S0'], 'v0': r['v0'], 'T': r['T'],
                         'p_gjr': kalshi_prob(r['strike_type'], _f(r['floor']), _f(r['cap']), r['S0'], vg, r['T'],
                                              *args)})
    return rows


def kalshi_scorecard(data_dir, jdir, cfg, log=print):
    rows = _kalshi_rows(data_dir, jdir, cfg)
    pend = len(read_jsonl(jfile(jdir, 'forecasts_kalshi'))) - len(read_jsonl(jfile(jdir, 'matured_kalshi')))
    if not rows:
        log(f'  kalshi: no matured contracts yet ({pend} journaled quotes pending settlement)')
        return
    for key, series in [(k, s) for k in enabled_underlyings(cfg) for s in ucfg(cfg, k, fallback=True)[1]['series']]:
        kc = ucfg(cfg, key, fallback=True)[1]
        rs = [r for r in rows if r['series'] == series]
        if not rs:
            continue
        nb = sum(r['source'] == 'backfill' for r in rs)
        evs = sorted({r['event'] for r in rs})
        vol_name = be.UNDERLYINGS[key]['vol'] or 'GJR vol'
        log(f'  kalshi {series} ({key}): {len(rs)} matured contracts ({nb} backfilled, {len(rs) - nb} forward) '
            f'over {len(evs)} settlement days; {pend} forward quotes pending overall')
        y = np.array([r['y'] for r in rs], float)
        p = np.clip(np.array([r['p'] for r in rs], float), 1e-6, 1 - 1e-6)
        d = np.array([np.datetime64(r['quote_date']) for r in rs])
        e = np.array([np.datetime64(r['expiry']) for r in rs])
        # recalibration uses only contracts matured strictly before each quote day (embargo)
        p_lp = be.walk_forward_recalibrate(d, e - np.timedelta64(1, 'D'), p, y, refit_every=5, min_obs=60)
        bid = np.array([r['bid'] for r in rs], float)
        ask = np.array([r['ask'] for r in rs], float)
        two = np.isfinite(bid) & np.isfinite(ask) & (ask > bid) & (bid > 0) & (ask < 1)
        mid = np.clip((bid + ask) / 2, 1e-6, 1 - 1e-6)
        p_gjr = np.clip(np.array([r['p_gjr'] for r in rs], float), 1e-6, 1 - 1e-6)
        # ladder-fitted curve: one (S, v) per settlement day and quote day, from that day's quotes only
        quoted = np.isfinite(bid) & np.isfinite(ask)
        p_fit = np.full(len(rs), np.nan)
        vol_ratio = []
        groups = {}
        for i in np.where(quoted)[0]:
            groups.setdefault((rs[i]['event'], rs[i]['quote_date']), []).append(i)
        for idx in groups.values():
            if len(idx) >= 4:
                _, v_fit, pf = fit_ladder([rs[i] for i in idx], kc['skew'], kc['rate'], kc['dividend'])
                p_fit[idx] = pf
                vol_ratio.append(v_fit / rs[idx[0]]['v0'])
        fitted = np.isfinite(p_fit)
        p_fit = np.clip(np.where(fitted, p_fit, 0.5), 1e-4, 1 - 1e-4)
        # Term-structure correction learned walk-forward: scale VIX by the median ladder-implied
        # 1-day vol / VIX ratio of settlement days strictly before this one (needs 5 earlier days).
        day_ratio = {}
        for key, idx in groups.items():
            if len(idx) >= 4 and np.isfinite(p_fit[idx[0]]):
                day_ratio.setdefault(rs[idx[0]]['expiry'], []).append(
                    fit_ladder([rs[i] for i in idx], kc['skew'], kc['rate'], kc['dividend'])[1] / rs[idx[0]]['v0'])
        p_ts = np.full(len(rs), np.nan)
        for i, r in enumerate(rs):
            past = [x for dday, xs in day_ratio.items() if dday < r['expiry'] for x in xs]
            if len(past) >= 5:
                p_ts[i] = kalshi_prob(r['strike_type'], r['floor'], r['cap'], r['S0'], r['v0'] * float(np.median(past)),
                                      r['T'], kc['skew'], kc['rate'], kc['dividend'])
        has_ts = np.isfinite(p_ts)
        p_ts = np.clip(np.where(has_ts, p_ts, 0.5), 1e-6, 1 - 1e-6)
        mid_q = np.clip((bid + ask) / 2, 1e-4, 1 - 1e-4)
        allm = np.ones(len(rs), bool)
        log('    forecast                       n     Brier    log loss   AUC')
        for name, q, m in ((f'model p_Q ({vol_name}, frozen)', p, allm), ('model p_Q (GJR 1-day vol)', p_gjr, allm),
                           (f'model p_Q ({vol_name}, same days)', p, has_ts),
                           ('model p_Q (learned vol scale)', p_ts, has_ts),
                           ('model p_Q recalibrated', p_lp, allm), ('model p_Q (two-sided)', p, two),
                           ('market mid (two-sided)', mid, two), ('market mid (quoted ladders)', mid_q, fitted),
                           ('ladder-fit curve (quoted)', p_fit, fitted)):
            if m.sum() >= 5:
                log(f'    {name:28s} {int(m.sum()):5d}   {be.brier(q[m], y[m]):.4f}   {be.log_loss(q[m], y[m]):.4f}   '
                    f'{be.auc(q[m], y[m]):.3f}')
        evarr = np.array([r['event'] for r in rs])
        if fitted.sum() >= 20:
            for label, a, b in (('Brier', (p_fit - y) ** 2, (mid_q - y) ** 2),
                                ('log loss', -(y * np.log(p_fit) + (1 - y) * np.log(1 - p_fit)),
                                 -(y * np.log(mid_q) + (1 - y) * np.log(1 - mid_q)))):
                mean, (lo, hi) = be.cluster_bootstrap((a - b)[fitted], evarr[fitted])
                log(f'    ladder-fit minus market mid, {label}: {mean:+.4f} [{lo:+.4f}, {hi:+.4f}] '
                    f'(cluster bootstrap by settlement day; negative = curve better)')
            log(f'    ladder-implied 1-day vol / {vol_name}: median {np.median(vol_ratio):.2f} '
                f'(range {np.min(vol_ratio):.2f} to {np.max(vol_ratio):.2f}) over {len(vol_ratio)} ladders')
        if two.sum() >= 20:
            pnl, side = be.edge_trades(p_lp[two], bid[two], ask[two], y[two], 0.02, 0.01)
            traded = side != 0
            if traded.sum() >= 5:
                mean, (lo, hi) = be.cluster_bootstrap(pnl[traded], np.array([r['event'] for r in rs])[two][traded])
                log(f'    edge trades vs quotes (recalibrated p, threshold 0.02 + fee 0.01): {traded.sum()} trades, '
                    f'mean {mean:+.4f} [{lo:+.4f}, {hi:+.4f}] per contract (cluster bootstrap by settlement day)')
        if any(r['strike_type'] == 'between' for r in rs):
            _ladder_scorecard(rs, log)


def _ladder_scorecard(rs, log):
    """Range ladders: one probability vector per settlement day, scored against the winning bucket."""
    by = {}
    for r in rs:
        by.setdefault((r['event'], r['quote_date']), []).append(r)
    Pm, Pk, Pd, outc = [], [], [], []
    for _, legs in sorted(by.items()):
        legs = sorted(legs, key=lambda r: (r['floor'] if math.isfinite(r['floor']) else -1e18))
        y = [r['y'] for r in legs]
        mids = np.array([(r['bid'] + r['ask']) / 2 for r in legs], float)
        if sum(y) != 1 or len(legs) < 5 or not np.all(np.isfinite(mids)):
            continue
        model = np.array([r['p'] for r in legs], float)
        if abs(model.sum() - 1) > 0.02:
            continue
        mids = np.clip(mids, 1e-4, None)
        Pm.append(be.closure(mids))
        Pd.append(be.power_devig(mids)[1] if mids.sum() > 1 else be.closure(mids))
        Pk.append(be.closure(np.clip(model, 1e-6, None)))
        outc.append(int(np.argmax(y)))
    if len(outc) < 3:
        log(f'    ladders: {len(outc)} complete quoted ladders so far (need 3 to score)')
        return
    K = min(len(p) for p in Pm)
    if any(len(p) != K for p in Pm):  # ladders differ in bucket count; score each on its own
        sc = {name: np.mean([be.ladder_scores(np.array([P[i]]), [outc[i]])['log_score'] for i in range(len(outc))])
              for name, P in (('market', Pm), ('de-vig', Pd), ('model', Pk))}
        log('    ladders: ' + ', '.join(f'{k} log score {v:.3f}' for k, v in sc.items()) + f' over {len(outc)} days')
        return
    for name, P in (('market (normalised mids)', Pm), ('market (power de-vig)', Pd), ('model p_Q', Pk)):
        s = be.ladder_scores(np.array(P), outc)
        log(f'    ladder {name:26s} log {s["log_score"]:.3f}  multi-Brier {s["brier"]:.3f}  RPS {s["rps"]:.4f}  ({len(outc)} days)')


def cross_scorecard(jdir, log=print):
    fc = read_jsonl(jfile(jdir, 'forecasts_cross'))
    mat = read_jsonl(jfile(jdir, 'matured_cross'))
    log(f'  cross: {len(fc)} book forecasts journaled, {len(mat)} matured')
    by = {}
    for r in mat:
        by.setdefault(r['model'], []).append(r)
    for model, rows in sorted(by.items()):
        h = rows[0]['horizon']
        ind = [rows[i] for i in _independent([r['asof'] for r in rows], h)]
        rate = 100 * np.mean([r['breach'][1] for r in rows])
        log(f'    {model:9s} matured {len(rows)} (independent {len(ind)}), 99% breach {rate:.2f}%')


def status(data_dir, jdir, cfg, log=print):
    log('MATURITY STATUS')
    latest = {}
    for row in read_jsonl(jfile(jdir, 'fetch_log')):
        latest[row['series']] = row
    for sid, row in sorted(latest.items()):
        log(f'  data {sid:10s} last {row["last"]} (fetched {row["fetched_at"]})')
    var_scorecard(jdir, log)
    kalshi_scorecard(data_dir, jdir, cfg, log)
    cross_scorecard(jdir, log)


# ---------------------------------------------------------------------------
# Freeze (pre-registration) and the daily cycle
# ---------------------------------------------------------------------------

def _code_hash():
    h = hashlib.sha256()
    for name in ('bsm_event.py', 'bsm_cross_asset.py', 'bsm_maturation.py'):
        with open(os.path.join(os.path.dirname(os.path.abspath(__file__)), name), 'rb') as f:
            h.update(f.read())
    return h.hexdigest()[:16]


def freeze(data_dir, jdir, tune=True, force=False, log=print):
    path = os.path.join(jdir, 'config.json')
    if os.path.exists(path) and not force:
        log(f'  config already frozen at {path}; not re-tuning (pre-registered). Use --force to replace.')
        return load_config(jdir)
    cfg = json.loads(json.dumps(DEFAULT_CFG))
    dates, S, V = be.load_spx_vix(data_dir)
    note = 'defaults (not tuned)'
    if tune:
        log(f'Tuning on history {dates[0]} .. {dates[-1]} with the walk-forward backtest (tuning split only) ...')
        summary = be.run_var_backtest(dates, S, V, be.BacktestConfig(), log=log)
        cfg['var']['tail_k'] = summary['tail_k']
        cfg['var']['gammas'] = [float(g) for g in summary['chosen_gamma']]
        note = 'tail index and tempering powers from the backtest tuning split'
    cfg['frozen'] = {'at': now_iso(), 'history': [str(dates[0]), str(dates[-1])], 'source': note,
                     'code_sha256_16': _code_hash()}
    os.makedirs(jdir, exist_ok=True)
    with open(path, 'w', encoding='utf-8') as f:
        json.dump(cfg, f, indent=2, default=_default)
    log(f'  froze {path}: tail_k {cfg["var"]["tail_k"]}, gammas {cfg["var"]["gammas"]} ({note})')
    return cfg


def _write_config(jdir, cfg):
    path = os.path.join(jdir, 'config.json')
    tmp = path + '.tmp'
    with open(tmp, 'w', encoding='utf-8') as f:
        json.dump(cfg, f, indent=2, default=_default)
    os.replace(tmp, path)


def freeze_underlying(data_dir, jdir, key, min_obs=1100, log=print):
    """Pre-register one more underlying: tune on its history with the walk-forward backtest
    (tuning split only) and add its section to config.json. Frozen sections are never rewritten."""
    cfg = load_config(jdir)
    if key == 'SPX' or key in cfg.get('underlyings', {}):
        return cfg
    dates, S, V = be.load_underlying(data_dir, key)
    if len(S) < min_obs:
        log(f'  freeze {key}: only {len(S)} observations (< {min_obs}); not frozen, no forecasts journaled')
        return cfg
    var, kal = default_underlying_cfg(key)
    log(f'Tuning {key} on {dates[0]} .. {dates[-1]} (tuning split only) ...')
    summary = be.run_var_backtest(dates, S, V, be.BacktestConfig(days_per_year=var['days_per_year'], rate=var['rate'],
                                                                 dividend=var['dividend'], skew=var['skew']), log=log)
    var['tail_k'] = summary['tail_k']
    var['gammas'] = [float(g) for g in summary['chosen_gamma']]
    cfg.setdefault('underlyings', {})[key] = {
        'var': var, 'kalshi': kal,
        'frozen': {'at': now_iso(), 'history': [str(dates[0]), str(dates[-1])],
                   'source': 'tail index and tempering powers from the backtest tuning split',
                   'code_sha256_16': _code_hash()}}
    _write_config(jdir, cfg)
    log(f'  froze {key}: tail_k {var["tail_k"]}, gammas {var["gammas"]}')
    return cfg


def run_cycle(data_dir, jdir, store_root, days=40, log=print):
    """One daily cycle. Stages are independent: a failing source (say, Kalshi unreachable) is
    logged and skipped so the others still run; returns the number of failed stages."""
    import traceback
    log(f'[{now_iso()}] data maturation cycle')
    failed = []

    def stage(name, fn, *args):
        try:
            return fn(*args)
        except Exception as exc:  # keep the cycle alive; the failure is reported and counted
            failed.append(name)
            log(f'  !! {name} failed: {type(exc).__name__}: {exc}')
            log('     ' + traceback.format_exc().strip().splitlines()[-1])
            return None

    stage('FRED refresh', refresh_fred, data_dir, jdir, http_text, log, fred_ids(load_config(jdir, required=False)))
    cfg = load_config(jdir, required=False) or freeze(data_dir, jdir, tune=True, log=log)
    for key in enabled_underlyings(cfg):
        if key != 'SPX':
            stage(f'freeze {key}', freeze_underlying, data_dir, jdir, key, 1100, log)
    cfg = load_config(jdir)
    for key in enabled_underlyings(cfg):
        _, kc = ucfg(cfg, key)
        for series in (kc or {}).get('series', []):
            stage(f'Kalshi backfill {series}', backfill_kalshi, data_dir, series, days, key, kc, 12, log)
    stage('var mature', var_mature, data_dir, jdir, log)
    stage('cross mature', cross_mature, data_dir, jdir, log)
    stage('kalshi mature', kalshi_mature, jdir, log)
    for key in enabled_underlyings(cfg):
        stage(f'var snapshot {key}', var_snapshot, data_dir, jdir, cfg, log, key)
    stage('cross snapshot', cross_snapshot, data_dir, jdir, cfg, log)
    stage('kalshi snapshot', kalshi_snapshot, data_dir, jdir, store_root, cfg, log)
    stage('status', status, data_dir, jdir, cfg, log)
    try:  # schema drift check against proto/bsm/v1 (needs the protobuf runtime)
        import bsm_proto
    except ImportError as exc:
        log(f'  proto validation skipped: {exc}')
    else:
        log('PROTO VALIDATION (bsm.v1)')
        if stage('proto validation', bsm_proto.validate, jdir, data_dir, store_root, log) is False:
            failed.append('proto validation (invalid records)')
    log(f'cycle finished: {"all stages ok" if not failed else "FAILED stages: " + ", ".join(failed)}')
    return len(failed)


# ---------------------------------------------------------------------------
# Self-test (offline: synthetic data, recorded payload shapes, no network)
# ---------------------------------------------------------------------------

def selftest():
    import tempfile
    failures = []

    def check(name, ok, detail):
        print(f'[{"ok" if ok else "FAIL"}] {name}: {detail}')
        if not ok:
            failures.append(name)

    print('bsm_maturation self-test (offline)')
    cases = [(datetime(2026, 7, 1, 13, tzinfo=timezone.utc), 9), (datetime(2026, 12, 1, 14, tzinfo=timezone.utc), 9),
             (datetime(2026, 3, 8, 6, 59, tzinfo=timezone.utc), 1), (datetime(2026, 3, 8, 7, 0, tzinfo=timezone.utc), 3),
             (datetime(2026, 11, 1, 5, 59, tzinfo=timezone.utc), 1), (datetime(2026, 11, 1, 6, 0, tzinfo=timezone.utc), 1)]
    got = [(d + timedelta(hours=ny_offset_hours(d.timestamp()))).hour for d, _ in cases]
    rt = datetime.fromtimestamp(ny_ts(date(2026, 1, 15), 9), timezone.utc).hour
    check('New York time without a tz database', got == [h for _, h in cases] and rt == 14,
          f'local hours {got} (expected {[h for _, h in cases]}); 09:00 ET on 2026-01-15 = {rt}:00 UTC')

    S0, v0, T = 7600.0, 0.17, 1 / 252
    edges = np.arange(7400, 7801, 25)
    ladder = ([('less', None, edges[0])] + [('between', a, b - 1e-4) for a, b in zip(edges[:-1], edges[1:])]
              + [('greater', edges[-1] - 1e-4, None)])
    tot = sum(kalshi_prob(t, f, c, S0, v0, T) for t, f, c in ladder)
    above = kalshi_prob('greater_or_equal', 7600, None, S0, v0, T)
    # Kalshi buckets leave 1e-4-point gaps between cap and next floor, so the sum falls a hair short of 1
    check('Kalshi contract probabilities', abs(tot - 1) < 1e-4 and 0.4 < above < 0.6,
          f'full range ladder sums to {tot:.8f}; P(S >= 7600 | S0 7600, 1 day) = {above:.4f}')

    with tempfile.TemporaryDirectory() as tmp:
        data, jdir = os.path.join(tmp, 'data'), os.path.join(tmp, 'journal')
        os.makedirs(data)
        dates, S, V = be.synthetic_market(n=900, seed=3)
        cut = 880

        def write(n):
            for name, vals in (('SP500.csv', S), ('VIXCLS.csv', V)):
                with open(os.path.join(data, name), 'w', newline='') as f:
                    f.write('observation_date,value\n' + ''.join(f'{d},{v:.4f}\n' for d, v in zip(dates[:n], vals[:n])))

        write(cut)
        cfg = json.loads(json.dumps(DEFAULT_CFG))
        cfg['var']['n_sims'] = 800
        cfg['var']['models'] = ['normal', 'gpd', 'fhs']
        first = var_snapshot(data, jdir, cfg, log=lambda *a: None)
        again = var_snapshot(data, jdir, cfg, log=lambda *a: None)
        early = var_mature(data, jdir, log=lambda *a: None)
        write(cut + 5)
        mat = var_mature(data, jdir, log=lambda *a: None)
        r = next(x for x in first if x['horizon'] == 5 and x['moneyness'] == 1.0 and x['model'] == 'gpd')
        m = next(x for x in mat if x['id'] == r['id'])
        con = be.BSMDigital(r['strike'], r['tenor'], 0.04, 0.015, -0.3)
        _, Sf, Vf = be.load_spx_vix(data)  # the values as written to CSV
        direct = -(float(con.price(Sf[cut + 4], max(Vf[cut + 4] / 100, 0.02), r['tenor'] - 5 / 252)) - r['p0'])
        ok = (len(first) == 18 and not again and not early and len(mat) == 18
              and m['matured_date'] == str(dates[cut + 4]) and abs(m['realized_loss'] - direct) < 1e-12
              and 0 < m['pit'] < 1 and m['breach'] == [direct > v for v in r['var']])
        check('journal -> maturation round trip', ok,
              f'{len(first)} forecasts, re-snapshot adds {len(again)}, {len(early)} mature early; after 5 more days '
              f'{len(mat)} matured (1-day and 5-day), matured_date {m["matured_date"]}, PIT {m["pit"]:.3f}')

        old = 'observation_date,SP500\n2026-10-27,7700.00\n2026-10-28,7710.00\n'
        new = 'observation_date,SP500\n2026-10-27,7700.00\n2026-10-28,7711.00\n2026-10-29,7720.39\n'
        fdir = os.path.join(tmp, 'fred')
        os.makedirs(fdir)
        with open(os.path.join(fdir, 'SP500.csv'), 'w') as f:
            f.write(old)
        texts = {sid: (new if sid == 'SP500' else 'observation_date,X\n2026-10-29,1.0\n') for sid in FRED}
        rows = refresh_fred(fdir, jdir, fetch=lambda url: texts[url.rsplit('=', 1)[1]], log=lambda *a: None)
        sp = next(r for r in rows if r['series'] == 'SP500')
        kept = os.listdir(os.path.join(fdir, 'vintages'))
        check('FRED revision vintages', sp['revised'] == 1 and sp['rows'] == 3 and len(kept) == 1,
              f'1 revised value detected, old file kept as {kept[0] if kept else "-"}')

    print(f'\n{"All checks passed." if not failures else "FAILED: " + ", ".join(failures)}')
    return not failures


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split('\n\n')[0],
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('--selftest', action='store_true')
    ap.add_argument('--data', default='data')
    ap.add_argument('--journal', default='journal')
    ap.add_argument('--store', default='store')
    sub = ap.add_subparsers(dest='cmd')
    b = sub.add_parser('backfill')
    b.add_argument('--days', type=int, default=40, help='settled Kalshi events per series')
    b.add_argument('--no-kalshi', action='store_true')
    f = sub.add_parser('freeze')
    f.add_argument('--no-tune', action='store_true')
    f.add_argument('--force', action='store_true')
    sub.add_parser('snapshot')
    sub.add_parser('mature')
    sub.add_parser('status')
    r = sub.add_parser('run')
    r.add_argument('--days', type=int, default=40)
    args = ap.parse_args(argv)

    if args.selftest:
        return 0 if selftest() else 1
    if args.cmd == 'backfill':
        cfg = load_config(args.journal, required=False)
        refresh_fred(args.data, args.journal, ids=fred_ids(cfg))
        if not args.no_kalshi:
            for key in enabled_underlyings(cfg):
                _, kc = ucfg(cfg, key, fallback=True) if cfg else default_underlying_cfg(key)
                for series in kc['series']:
                    backfill_kalshi(args.data, series, args.days, key, kc)
    elif args.cmd == 'freeze':
        freeze(args.data, args.journal, tune=not args.no_tune, force=args.force)
        for key in enabled_underlyings(load_config(args.journal)):
            freeze_underlying(args.data, args.journal, key)
    elif args.cmd == 'snapshot':
        cfg = load_config(args.journal)
        for key in enabled_underlyings(cfg):
            var_snapshot(args.data, args.journal, cfg, key=key)
        cross_snapshot(args.data, args.journal, cfg)
        kalshi_snapshot(args.data, args.journal, args.store, cfg)
    elif args.cmd == 'mature':
        var_mature(args.data, args.journal)
        cross_mature(args.data, args.journal)
        kalshi_mature(args.journal)
    elif args.cmd == 'status':
        status(args.data, args.journal, load_config(args.journal))
    elif args.cmd == 'run':
        return 1 if run_cycle(args.data, args.journal, args.store, args.days) else 0
    else:
        ap.print_help()
    return 0


if __name__ == '__main__':
    sys.exit(main())
