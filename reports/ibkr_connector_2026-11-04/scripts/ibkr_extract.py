"""Pull IBKR get_price_history results out of the session record, unretyped, and write canonical bars."""
import json
import re
from pathlib import Path

import pandas as pd

SESSION = Path(r"C:\Users\PC\.claude\projects\C--Users-PC-OneDrive-Desktop-Components-FRM-Claude-tradecard"
               r"\56237ccf-c9aa-4faf-85df-cb3e14075f59.jsonl")
OUT = Path(__file__).parent / "ibkr_bars"
OUT.mkdir(exist_ok=True)

STOCKS = {756733: "SPY", 51529211: "GLD", 15547841: "TLT", 14065804: "STW.AX", 4036812: "BHP.AX"}
# front-month walk order per root
FUTURES = {
    "ES": {495512557: "ESZ4", 603558932: "ESH5", 620731015: "ESM5", 637533641: "ESU5", 495512563: "ESZ5",
           649180695: "ESH6", 649180678: "ESM6", 649180671: "ESU6", 515416632: "ESZ6"},
    "SPI": {711236452: "APZ5", 730232834: "APH6", 749811513: "APM6", 770350159: "APU6", 793387092: "APZ6"},
    "ZN": {710205969: "ZNH5", 729594955: "ZNM5", 749139098: "ZNU5", 770327397: "ZNZ5",
           792996527: "ZNH6", 815824229: "ZNM6", 840227361: "ZNU6", 866514750: "ZNZ6"},
    "GC": {623469623: "GCG5", 372852975: "GCM5", 656780474: "GCQ5", 397594951: "GCZ5",
           693609539: "GCG6", 430360630: "GCM6", 732156872: "GCQ6", 462941472: "GCZ6"},
    "CL": {304037421: "CLH5", 256019308: "CLM5", 304037481: "CLU5", 256019341: "CLZ5",
           304037427: "CLH6", 296574762: "CLM6", 304037484: "CLU6", 296574787: "CLZ6"},
}


def load_results() -> dict[int, list[dict]]:
    uses: dict[str, dict] = {}
    found: dict[int, list[dict]] = {}
    for line in SESSION.read_text(encoding="utf-8").splitlines():
        try:
            rec = json.loads(line)
        except json.JSONDecodeError:
            continue
        content = (rec.get("message") or {}).get("content")
        if not isinstance(content, list):
            continue
        for block in content:
            if not isinstance(block, dict):
                continue
            if block.get("type") == "tool_use" and "get_price_history" in block.get("name", ""):
                uses[block["id"]] = block["input"]
            elif block.get("type") == "tool_result" and block.get("tool_use_id") in uses:
                body = block.get("content")
                text = body if isinstance(body, str) else "".join(c.get("text", "") for c in body or [])
                spilled = re.search(r"saved to (.+?\.txt)", text)
                if spilled:
                    text = Path(spilled.group(1)).read_text(encoding="utf-8")
                try:
                    data = json.loads(text)
                except json.JSONDecodeError:
                    continue
                if isinstance(data, list):
                    data = json.loads(data[0]["text"])
                if data.get("time"):
                    cid = int(uses[block["tool_use_id"]]["contract_id"])
                    found.setdefault(cid, []).append(data)
    return found


def frame(results: list[dict]) -> pd.DataFrame:
    """Longest fetch wins; the sample and short re-fetches are subsets of it."""
    d = max(results, key=lambda r: len(r["time"]))
    df = pd.DataFrame({k: d[k] for k in ("time", "open", "high", "low", "close", "volume")})
    df["time"] = pd.to_datetime(df["time"], utc=True)
    return df.sort_values("time").reset_index(drop=True)


def check(name: str, df: pd.DataFrame) -> None:
    bad = int(((df.high < df[["open", "close"]].max(axis=1) - 1e-9)
               | (df.low > df[["open", "close"]].min(axis=1) + 1e-9)).sum())
    big = df.close.pct_change().abs().max()
    print(f"{name:8} bars={len(df):5} {df.time.iloc[0].date()} -> {df.time.iloc[-1].date()} "
          f"last={df.close.iloc[-1]:>9} dupes={int(df.time.dt.date.duplicated().sum())} "
          f"ohlc_bad={bad} zero_vol={int((df.volume == 0).sum())} max|ret|={big:.3f}")


# CL is held on its quarterly months only, where volume does not cross cleanly: leave 8 business
# days before last trade (data/futures_calendar.py's roll_lead_days for CL).
ROLL_DATES = {"CL": {name: (pd.Timestamp(d) - pd.offsets.BDay(8)).date() for name, d in {
    "CLH5": "2025-02-20", "CLM5": "2025-05-20", "CLU5": "2025-08-20", "CLZ5": "2025-11-20",
    "CLH6": "2026-02-20", "CLM6": "2026-05-19", "CLU6": "2026-08-20"}.items()}}
ACTIVE_MIN_VOLUME = {"ES": 10_000, "SPI": 1_000, "ZN": 10_000, "GC": 1_000, "CL": 1_000}


def stitch(frames: dict[str, pd.DataFrame], root: str) -> pd.DataFrame:
    """Front month by volume; ratio-adjust at each switch using both contracts' closes on the SAME day.

    Rows with zero volume are settlement prints or post-expiry repeats, not trades, and are dropped.
    """
    traded = {k: v[v.volume > 0].assign(day=lambda x: x.time.dt.date) for k, v in frames.items()}
    days = sorted(set().union(*(set(v.day) for v in traded.values())))
    order = list(frames)
    pos, rows, ratios = 0, [], []
    roll_on = ROLL_DATES.get(root, {})
    for day in days:
        cur = traded[order[pos]]
        cur_row = cur[cur.day == day]
        if pos + 1 < len(order):
            nxt = traded[order[pos + 1]]
            nxt_row = nxt[nxt.day == day]
            cur_vol = float(cur_row.volume.iloc[0]) if len(cur_row) else 0.0
            due = roll_on.get(order[pos])
            switch = len(nxt_row) and (day >= due if due else float(nxt_row.volume.iloc[0]) > cur_vol)
            if switch or (cur_row.empty and len(nxt_row) and day > cur.day.iloc[-1]):
                # same-day closes; a contract already gone is measured on its last shared day
                both = cur.merge(nxt, on="day", suffixes=("_c", "_n"))
                both = both[both.day <= day]
                if both.empty:
                    raise ValueError(f"no shared day to adjust {order[pos]} -> {order[pos + 1]}")
                ratios.append((len(rows), float(both.close_n.iloc[-1]) / float(both.close_c.iloc[-1])))
                pos += 1
                cur_row = nxt_row
        if cur_row.empty:
            continue
        r = cur_row.iloc[0]
        rows.append({"time": r.time, "open": r.open, "high": r.high, "low": r.low, "close": r.close,
                     "volume": r.volume, "active_contract": order[pos]})
    df = pd.DataFrame(rows)
    df.attrs_root = root
    df[["open", "high", "low", "close"]] = df[["open", "high", "low", "close"]].astype(float)
    for at, ratio in ratios:      # back-adjust everything before the switch
        df.loc[df.index < at, ["open", "high", "low", "close"]] *= ratio
    active = df.index[df.volume >= ACTIVE_MIN_VOLUME[df.attrs_root]]
    print(f"  rolls: {[(df.active_contract.iloc[a], df.time.iloc[a].date().isoformat(), round(q, 5)) for a, q in ratios]}")
    return df.iloc[active[0]:].reset_index(drop=True)   # thin deferred prints before the first active day are not a market


def main() -> None:
    res = load_results()
    today = pd.Timestamp("2026-11-04", tz="UTC").date()
    for cid, sym in STOCKS.items():
        df = frame(res[cid])
        if sym in ("SPY", "GLD", "TLT") and df.time.iloc[-1].date() == today:
            df = df.iloc[:-1]     # US session still open when fetched
        check(sym, df)
        df.to_parquet(OUT / f"{sym}.parquet")
    for root, ladder in FUTURES.items():
        have = {name: frame(res[cid]) for cid, name in ladder.items() if cid in res}
        missing = [name for cid, name in ladder.items() if cid not in res]
        print(f"{root}: contracts {list(have)} missing {missing}")
        for name, f in have.items():
            n = int((f.volume > 0).sum())
            print(f"  {name}: rows={len(f)} traded={n}")
        cont = stitch(have, root)
        if cont.time.iloc[-1].date() == today:
            cont = cont.iloc[:-1]
        check(root, cont)
        cont.to_parquet(OUT / f"{root}_cont.parquet")


if __name__ == "__main__":
    main()
