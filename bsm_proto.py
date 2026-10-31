#!/usr/bin/env python3
"""bsm_proto.py -- bsm.v1 protobuf codec for the market store and the maturation journal.

Schemas live in proto/bsm/v1/ (market.proto, journal.proto); generated Python is in bsm_pb/.
Journal field names equal the JSON keys, so every journal line parses into its message as-is.
Parsing is strict (unknown keys are errors), which makes `validate` a drift check: if
bsm_maturation.py starts writing a field the schema lacks, validation fails.

Install: python -m pip install protobuf==5.29.3      (matches protoc 29.3's generated code)
Run:     python bsm_proto.py compile                  regenerate bsm_pb/ with protoc
         python bsm_proto.py validate                 strict parse + exact round trip of all records
         python bsm_proto.py export [--out exports]   length-delimited binary per stream, re-read
         python bsm_proto.py --selftest               offline checks
Binary files are varint-length-delimited messages (the writeDelimitedTo convention), so any
protobuf runtime can stream them.
"""
from __future__ import annotations

import argparse
import csv
import glob
import gzip
import json
import math
import os
import shutil
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
PROTO_DIR = os.path.join(HERE, 'proto')
PB_DIR = os.path.join(HERE, 'bsm_pb')
PROTOS = ('bsm/v1/market.proto', 'bsm/v1/journal.proto')


def compile_protos(protoc=None):
    """Regenerate bsm_pb/ from proto/ with protoc (Python modules and .pyi stubs)."""
    exe = protoc or shutil.which('protoc')
    if not exe:
        raise SystemExit('protoc not found on PATH; install protoc 29.x or pass --protoc')
    os.makedirs(PB_DIR, exist_ok=True)
    subprocess.run([exe, f'-I{PROTO_DIR}', f'--python_out={PB_DIR}', f'--pyi_out={PB_DIR}',
                    *(os.path.join(PROTO_DIR, p) for p in PROTOS)], check=True)
    print(f'compiled {", ".join(PROTOS)} -> {PB_DIR}')


if __name__ == '__main__' and sys.argv[1:2] == ['compile']:  # works even before bsm_pb/ exists
    compile_protos(sys.argv[3] if sys.argv[2:3] == ['--protoc'] else None)
    sys.exit(0)

sys.path.insert(0, PB_DIR)
from google.protobuf import json_format  # noqa: E402
from bsm.v1 import journal_pb2, market_pb2  # noqa: E402

JOURNAL_STREAMS = {
    'forecasts_var': journal_pb2.VarForecast, 'matured_var': journal_pb2.VarMatured,
    'forecasts_kalshi': journal_pb2.KalshiForecast, 'matured_kalshi': journal_pb2.KalshiMatured,
    'forecasts_cross': journal_pb2.CrossForecast, 'matured_cross': journal_pb2.CrossMatured,
    'fetch_log': journal_pb2.FetchLog,
}
ENVELOPE_FIELD = {journal_pb2.VarForecast: 'var_forecast', journal_pb2.VarMatured: 'var_matured',
                  journal_pb2.KalshiForecast: 'kalshi_forecast', journal_pb2.KalshiMatured: 'kalshi_matured',
                  journal_pb2.CrossForecast: 'cross_forecast', journal_pb2.CrossMatured: 'cross_matured',
                  journal_pb2.FetchLog: 'fetch_log', journal_pb2.KalshiSettled: 'kalshi_settled'}


# ---------------------------------------------------------------------------
# Dict <-> message
# ---------------------------------------------------------------------------

def from_dict(d, cls):
    """Strict parse: an unknown key raises json_format.ParseError."""
    return json_format.ParseDict(d, cls(), ignore_unknown_fields=False)


def to_dict(msg):
    return json_format.MessageToDict(msg, preserving_proto_field_name=True,
                                     always_print_fields_with_no_presence=True)


def same(a, b):
    """Equality up to representation: None, empty strings, lists and maps == absent (proto3 has no
    presence for them), 7525 == 7525.0, int64s written as strings compare by value. Floats must
    match exactly."""
    def present(v):
        return v is not None and v != [] and v != {} and v != ''

    if isinstance(a, dict) and isinstance(b, dict):
        keys = {k for k in a if present(a[k])} | {k for k in b if present(b[k])}
        return all(same(a.get(k), b.get(k)) for k in keys)
    if isinstance(a, list) and isinstance(b, list):
        return len(a) == len(b) and all(same(x, y) for x, y in zip(a, b))
    if isinstance(a, bool) or isinstance(b, bool):
        return a == b
    try:
        fa, fb = float(a), float(b)
        return fa == fb or (math.isnan(fa) and math.isnan(fb))
    except (TypeError, ValueError):
        return a == b


def market_to_proto(r):
    """bsm_microstructure record (or its JSON dict) -> MarketRecord."""
    d = r if isinstance(r, dict) else {**r.__dict__}
    if d['type'] == 'book':
        return market_pb2.MarketRecord(book=market_pb2.BookSnapshot(
            ts=d['ts'], market=d['market'], source=d.get('source', ''),
            bids=[market_pb2.Level(price=p, qty=q) for p, q in d['bids']],
            asks=[market_pb2.Level(price=p, qty=q) for p, q in d['asks']]))
    return market_pb2.MarketRecord(trade=market_pb2.Trade(
        ts=d['ts'], market=d['market'], price=d['price'], qty=d['qty'], side=d.get('side', 0),
        source=d.get('source', '')))


def market_to_dict(m):
    """MarketRecord -> the store's JSON shape ([[price, qty], ...] levels, 'type' tag)."""
    if m.WhichOneof('record') == 'book':
        b = m.book
        return {'ts': b.ts, 'market': b.market, 'bids': [[x.price, x.qty] for x in b.bids],
                'asks': [[x.price, x.qty] for x in b.asks], 'source': b.source, 'type': 'book'}
    t = m.trade
    return {'ts': t.ts, 'market': t.market, 'price': t.price, 'qty': t.qty, 'side': t.side,
            'source': t.source, 'type': 'trade'}


def settled_row_to_proto(row):
    """data/kalshi/<series>_settled.csv row -> KalshiSettled (empty / None cells left unset)."""
    num = lambda v: None if v in ('', 'None', None) else float(v)
    msg = journal_pb2.KalshiSettled(
        series=row['series'], event=row['event'], ticker=row['ticker'], strike_type=row['strike_type'],
        close_time=row['close_time'], expiry=row['expiry'], result=int(row['result']),
        state_date=row['state_date'], S0=float(row['S0']), v0=float(row['v0']))
    for f in ('floor', 'cap', 'quote_ts', 'bid', 'ask'):
        v = num(row[f])
        if v is not None:
            setattr(msg, f, v)
    if row['settlement'] not in ('', 'None'):
        msg.settlement = row['settlement']
    return msg


def envelope(msg):
    return journal_pb2.JournalEntry(**{ENVELOPE_FIELD[type(msg)]: msg})


# ---------------------------------------------------------------------------
# Length-delimited binary streams
# ---------------------------------------------------------------------------

def _varint(n):
    out = bytearray()
    while True:
        b = n & 0x7F
        n >>= 7
        out.append(b | (0x80 if n else 0))
        if not n:
            return bytes(out)


def write_delimited(f, msgs):
    n = 0
    for m in msgs:
        b = m.SerializeToString()
        f.write(_varint(len(b)))
        f.write(b)
        n += 1
    return n


def read_delimited(f, cls):
    while True:
        shift = size = 0
        while True:
            c = f.read(1)
            if not c:
                if shift:
                    raise EOFError('truncated length prefix')
                return
            size |= (c[0] & 0x7F) << shift
            shift += 7
            if not c[0] & 0x80:
                break
        buf = f.read(size)
        if len(buf) != size:
            raise EOFError('truncated message')
        m = cls()
        m.ParseFromString(buf)
        yield m


# ---------------------------------------------------------------------------
# Sources of records on disk
# ---------------------------------------------------------------------------

def journal_records(journal_dir):
    for stream, cls in JOURNAL_STREAMS.items():
        path = os.path.join(journal_dir, f'{stream}.jsonl')
        if os.path.exists(path):
            with open(path, encoding='utf-8') as f:
                yield stream, cls, [json.loads(line) for line in f if line.strip()]


def store_files(store_root):
    return sorted(glob.glob(os.path.join(store_root, '*', '*', '*.jsonl.gz')))


def settled_files(data_dir):
    return sorted(glob.glob(os.path.join(data_dir, 'kalshi', '*_settled.csv')))


# ---------------------------------------------------------------------------
# Commands
# ---------------------------------------------------------------------------

def validate(journal_dir='journal', data_dir='data', store_root='store', log=print):
    """Strict parse and exact round trip of every journal line, the config, settled CSVs and the store."""
    bad = 0
    for stream, cls, rows in journal_records(journal_dir):
        errs = []
        for i, d in enumerate(rows):
            try:
                if not same(to_dict(from_dict(d, cls)), d):
                    errs.append(f'line {i + 1}: round trip changed the record')
            except json_format.ParseError as exc:
                errs.append(f'line {i + 1}: {exc}')
        bad += len(errs)
        log(f'  journal/{stream}.jsonl: {len(rows) - len(errs)}/{len(rows)} records valid as bsm.v1.{cls.__name__}'
            + (f'; first error: {errs[0]}' if errs else ''))
    cfg_path = os.path.join(journal_dir, 'config.json')
    if os.path.exists(cfg_path):
        with open(cfg_path, encoding='utf-8') as f:
            cfg = json.load(f)
        try:
            ok = same(to_dict(from_dict(cfg, journal_pb2.MaturationConfig)), cfg)
            log(f'  journal/config.json: {"valid" if ok else "round trip changed it"} as bsm.v1.MaturationConfig')
            bad += not ok
        except json_format.ParseError as exc:
            log(f'  journal/config.json: INVALID ({exc})')
            bad += 1
    for path in settled_files(data_dir):
        with open(path, newline='', encoding='utf-8') as f:
            rows = list(csv.DictReader(f))
        errs = 0
        for row in rows:
            m = settled_row_to_proto(row)
            back = journal_pb2.KalshiSettled()
            back.ParseFromString(m.SerializeToString())
            errs += back != m
        bad += errs
        log(f'  {os.path.relpath(path)}: {len(rows) - errs}/{len(rows)} rows valid as bsm.v1.KalshiSettled')
    n_store = errs = 0
    for path in store_files(store_root):
        with gzip.open(path, 'rt', encoding='ascii') as f:
            for line in f:
                if not line.strip():
                    continue
                d = json.loads(line)
                n_store += 1
                m = market_to_proto(d)
                back = market_pb2.MarketRecord()
                back.ParseFromString(m.SerializeToString())
                errs += not same(market_to_dict(back), d)
    bad += errs
    log(f'  store/: {n_store - errs}/{n_store} records valid as bsm.v1.MarketRecord '
        f'({len(store_files(store_root))} daily files)')
    log('All records valid.' if not bad else f'{bad} invalid records.')
    return bad == 0


def export(out_dir='exports', journal_dir='journal', data_dir='data', store_root='store', log=print):
    """Write one length-delimited .pb per stream, read each back, and compare sizes with JSON."""
    os.makedirs(out_dir, exist_ok=True)
    jobs = []
    for stream, cls, rows in journal_records(journal_dir):
        jobs.append((stream, cls, [from_dict(d, cls) for d in rows],
                     os.path.getsize(os.path.join(journal_dir, f'{stream}.jsonl'))))
    settled = []
    js = 0
    for path in settled_files(data_dir):
        with open(path, newline='', encoding='utf-8') as f:
            settled += [settled_row_to_proto(r) for r in csv.DictReader(f)]
        js += os.path.getsize(path)
    if settled:
        jobs.append(('kalshi_settled', journal_pb2.KalshiSettled, settled, js))
    market, raw = [], 0
    for path in store_files(store_root):
        with gzip.open(path, 'rt', encoding='ascii') as f:
            text = f.read()
        raw += len(text.encode())
        market += [market_to_proto(json.loads(line)) for line in text.splitlines() if line.strip()]
    if market:
        jobs.append(('market', market_pb2.MarketRecord, market, raw))
    log(f'  {"stream":18s} {"records":>8s} {"JSON bytes":>11s} {"proto bytes":>12s}  ratio')
    for name, cls, msgs, json_bytes in jobs:
        path = os.path.join(out_dir, f'{name}.pb')
        with open(path, 'wb') as f:
            write_delimited(f, msgs)
        with open(path, 'rb') as f:
            back = list(read_delimited(f, cls))
        if back != msgs:
            raise SystemExit(f'{path}: read-back mismatch')
        size = os.path.getsize(path)
        log(f'  {name:18s} {len(msgs):8d} {json_bytes:11d} {size:12d}  {size / max(json_bytes, 1):5.2f}')
    log(f'Wrote and re-read {len(jobs)} streams in {out_dir}/ (varint length-delimited bsm.v1 messages).')


def selftest():
    import io
    failures = []

    def check(name, ok, detail):
        print(f'[{"ok" if ok else "FAIL"}] {name}: {detail}')
        if not ok:
            failures.append(name)

    print('bsm_proto self-test (offline)')
    var = {'id': 'var|2026-10-29|h1|m1.0|spectral', 'stream': 'var', 'made_at': '2026-10-30T15:46:03+00:00',
           'asof': '2026-10-29', 'horizon': 1, 'moneyness': 1.0, 'model': 'spectral', 'strike': 7720.39,
           'S0': 7720.39, 'v0': 0.172, 'tenor': 1 / 12, 'p0': 0.53, 'position': 1.0,
           'contract': {'rate': 0.04, 'dividend': 0.015, 'skew': -0.3, 'vol_floor': 0.02, 'level_map': [0.0, 1.0]},
           'levels': [0.9, 0.95, 0.99], 'var': [0.05, 0.08, 0.17], 'quantiles': [0.1 * i for i in range(103)],
           'action': 'taylor4', 'ratio_gate': False, 'crossing': False, 'radius': 4.18}
    kal = {'id': 'kalshi|T|1', 'stream': 'kalshi', 'made_at': 'x', 'ts': 1793375165.92, 'series': 'KXINX',
           'event': 'E', 'ticker': 'T', 'strike_type': 'less', 'floor': None, 'cap': 7300, 'close_time': 'c',
           'expiry': '2026-10-30', 'bid': None, 'ask': 0.01, 'bid_size': 0.0, 'ask_size': 5.0,
           'state_date': '2026-10-29', 'S0': 7720.39, 'v0': 0.172, 'T': 1 / 252, 'p_model': 0.0001}
    rt = [same(to_dict(from_dict(d, c)), d) for d, c in ((var, journal_pb2.VarForecast), (kal, journal_pb2.KalshiForecast))]
    m = from_dict(kal, journal_pb2.KalshiForecast)
    check('dict round trip with optional fields', all(rt) and not m.HasField('floor') and m.HasField('cap')
          and not m.HasField('bid'), 'var and kalshi forecasts; null floor/bid stay unset, cap 7300 set')
    try:
        from_dict({**var, 'new_field': 1}, journal_pb2.VarForecast)
        drift = False
    except json_format.ParseError:
        drift = True
    check('strict parsing catches schema drift', drift, 'an unknown key is rejected')
    book = {'ts': 1.5, 'market': 'M', 'bids': [[0.49, 106.0]], 'asks': [[0.5, 150.0], [0.51, 3.0]],
            'source': 'kalshi_book', 'type': 'book'}
    trade = {'ts': 2.0, 'market': 'M', 'price': 0.5, 'qty': 3.0, 'side': -1, 'source': 'x', 'type': 'trade'}
    buf = io.BytesIO()
    write_delimited(buf, [market_to_proto(book), market_to_proto(trade), market_to_proto(book)])
    buf.seek(0)
    back = [market_to_dict(x) for x in read_delimited(buf, market_pb2.MarketRecord)]
    check('length-delimited stream', len(back) == 3 and same(back[0], book) and same(back[1], trade),
          f'{len(back)} records read back; negative side survives sint32 encoding')
    big = journal_pb2.FetchLog(rows=2 ** 40)
    b2 = io.BytesIO()
    write_delimited(b2, [big] * 2)
    b2.seek(0)
    check('varint framing', [x.rows for x in read_delimited(b2, journal_pb2.FetchLog)] == [2 ** 40] * 2,
          'multi-byte length prefixes and int64 values')
    env = envelope(from_dict(var, journal_pb2.VarForecast))
    check('journal envelope', env.WhichOneof('entry') == 'var_forecast', 'JournalEntry oneof dispatch')

    # Matured records as bsm_maturation actually writes them (synthetic data, temp journal).
    import tempfile
    import bsm_event as be
    import bsm_maturation as bm
    with tempfile.TemporaryDirectory() as tmp:
        data, jdir = os.path.join(tmp, 'data'), os.path.join(tmp, 'journal')
        os.makedirs(data)
        dates, S, V = be.synthetic_market(n=700, seed=4)

        def write(n):
            for name, vals in (('SP500.csv', S), ('VIXCLS.csv', V)):
                with open(os.path.join(data, name), 'w', newline='') as f:
                    f.write('observation_date,value\n' + ''.join(f'{d},{v:.4f}\n' for d, v in zip(dates[:n], vals[:n])))

        cfg = json.loads(json.dumps(bm.DEFAULT_CFG))
        cfg['var'].update(n_sims=500, models=['normal', 'spectral'])
        write(690)
        fc = bm.var_snapshot(data, jdir, cfg, log=lambda *a: None)
        write(696)
        mat = bm.var_mature(data, jdir, log=lambda *a: None)
        ok_v = all(same(to_dict(from_dict(json.loads(json.dumps(r, default=bm._default)), journal_pb2.VarForecast)),
                        json.loads(json.dumps(r, default=bm._default))) for r in fc)
        ok_m = all(same(to_dict(from_dict(r, journal_pb2.VarMatured)), r) for r in mat)
    km = {'id': 'kalshi|T|1', 'matured_date': '2026-10-30', 'matured_at': 'x', 'ticker': 'T', 'outcome': 1,
          'settlement': '7610.12'}
    cm = {'id': 'cross|d|h5|gpd', 'matured_date': 'd', 'matured_at': 'x', 'asof': 'd', 'horizon': 5, 'model': 'gpd',
          'realized_loss': -12.5, 'breach': [False, False], 'pit': 0.4}
    ok_o = same(to_dict(from_dict(km, journal_pb2.KalshiMatured)), km) and same(
        to_dict(from_dict(cm, journal_pb2.CrossMatured)), cm)
    check('matured-record schemas', ok_v and ok_m and ok_o and len(mat) == len(fc),
          f'{len(fc)} forecasts and {len(mat)} matured VaR records written by bsm_maturation parse strictly '
          f'(spectral gate fields included); Kalshi and cross matured shapes round-trip')
    print(f'\n{"All checks passed." if not failures else "FAILED: " + ", ".join(failures)}')
    return not failures


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split('\n\n')[0])
    ap.add_argument('--selftest', action='store_true')
    ap.add_argument('--journal', default='journal')
    ap.add_argument('--data', default='data')
    ap.add_argument('--store', default='store')
    sub = ap.add_subparsers(dest='cmd')
    c = sub.add_parser('compile')
    c.add_argument('--protoc', default=None)
    sub.add_parser('validate')
    e = sub.add_parser('export')
    e.add_argument('--out', default='exports')
    args = ap.parse_args(argv)
    if args.selftest:
        return 0 if selftest() else 1
    if args.cmd == 'compile':
        compile_protos(args.protoc)
    elif args.cmd == 'validate':
        return 0 if validate(args.journal, args.data, args.store) else 1
    elif args.cmd == 'export':
        export(args.out, args.journal, args.data, args.store)
    else:
        ap.print_help()
    return 0


if __name__ == '__main__':
    sys.exit(main())
