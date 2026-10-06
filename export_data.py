import os
import json
from datetime import datetime, timedelta, timezone
import requests
import pandas as pd

BASE = "https://demo-api-capital.backend-capital.com/api/v1"
EPIC = "GOLD"
FRAMES = {"M5": "MINUTE_5", "M15": "MINUTE_15", "H1": "HOUR"}
GAP = {"M5": 1.0, "M15": 2.0, "H1": 4.0}
STRONG = 1.5
SWING = 10
SHOW = 120
STOP_PUFFER = 0.5


def login():
    r = requests.post(
        f"{BASE}/session",
        headers={"X-CAP-API-KEY": os.environ["CAPITAL_API_KEY"]},
        json={
            "identifier": os.environ["CAPITAL_IDENTIFIER"],
            "password": os.environ["CAPITAL_PASSWORD"],
        },
        timeout=15,
    )
    r.raise_for_status()
    return {
        "CST": r.headers["CST"],
        "X-SECURITY-TOKEN": r.headers["X-SECURITY-TOKEN"],
    }


def get_candles(h, resolution):
    r = requests.get(
        f"{BASE}/prices/{EPIC}",
        headers=h,
        params={"resolution": resolution, "max": 300},
        timeout=15,
    )
    r.raise_for_status()
    rows = []
    for p in r.json()["prices"]:
        mid = lambda k: (p[k]["bid"] + p[k]["ask"]) / 2
        rows.append({
            "time": pd.to_datetime(p["snapshotTimeUTC"]).tz_localize("UTC"),
            "open": mid("openPrice"),
            "high": mid("highPrice"),
            "low": mid("lowPrice"),
            "close": mid("closePrice"),
        })
    return pd.DataFrame(rows).sort_values("time").reset_index(drop=True)


def get_trend(h1):
    c = h1["close"]
    e20 = c.ewm(span=20, adjust=False).mean().iloc[-1]
    e50 = c.ewm(span=50, adjust=False).mean().iloc[-1]
    last = c.iloc[-1]
    if e20 > e50 and last > e50:
        return "long"
    if e20 < e50 and last < e50:
        return "short"
    return "neutral"


def find_blocks(df):
    body = (df["close"] - df["open"]).abs()
    found = {}
    for i in range(SWING + 20, len(df)):
        avg = body.iloc[i - 20:i].mean()
        c = df.iloc[i]
        prev = df.iloc[i - SWING:i]
        if body.iloc[i] < STRONG * avg:
            continue
        kind = None
        if c.close > c.open and c.close > prev.high.max():
            kind = "long"
        elif c.close < c.open and c.close < prev.low.min():
            kind = "short"
        if kind is None:
            continue
        for k in range(i - 1, max(i - 6, 0), -1):
            o = df.iloc[k]
            ok = (kind == "long" and o.close < o.open) or (
                kind == "short" and o.close > o.open
            )
            if ok:
                if k not in found:
                    found[k] = (kind, i)
                break

    result = []
    for k, (kind, i) in found.items():
        lo = float(df.iloc[k].low)
        hi = float(df.iloc[k].high)
        status = "frisch"
        for j in range(i + 1, len(df)):
            c = df.iloc[j]
            if kind == "long":
                if c.close < lo:
                    status = "ungueltig"
                    break
                if c.low <= hi:
                    status = "angetestet"
            else:
                if c.close > hi:
                    status = "ungueltig"
                    break
                if c.high >= lo:
                    status = "angetestet"
        result.append({
            "kind": kind, "k": k, "i": i, "lo": lo, "hi": hi,
            "t": int(df.iloc[k].time.timestamp()), "status": status,
        })
    return result


def merge(blocks, gap):
    result = []
    for kind in ("long", "short"):
        grp = sorted(
            [b for b in blocks if b["kind"] == kind and b["status"] != "ungueltig"],
            key=lambda b: b["lo"],
        )
        cur = None
        for b in grp:
            if cur is not None and b["lo"] <= cur["hi"] + gap:
                cur["hi"] = max(cur["hi"], b["hi"])
                cur["lo"] = min(cur["lo"], b["lo"])
                cur["t"] = min(cur["t"], b["t"])
                cur["n"] += 1
                if b["status"] == "angetestet":
                    cur["status"] = "angetestet"
            else:
                if cur is not None:
                    result.append(cur)
                cur = dict(b)
                cur["n"] = 1
        if cur is not None:
            result.append(cur)
    for b in blocks:
        if b["status"] == "ungueltig":
            x = dict(b)
            x["n"] = 1
            result.append(x)
    return result


def find_entries(m5, m1, blocks):
    entries = []
    for b in blocks:
        i = b["i"]
        lo, hi = b["lo"], b["hi"]
        is_long = b["kind"] == "long"
        touched = False
        invalid = False
        for j in range(i + 1, len(m5)):
            c = m5.iloc[j]
            inv = (c.close < lo) if is_long else (c.close > hi)
            tch = (c.low <= hi) if is_long else (c.high >= lo)
            if inv:
                invalid = True
                break
            if tch:
                touched = True
                break
        if invalid or not touched:
            continue

        start = m5.iloc[i].time + timedelta(minutes=5)
        m = m1[m1.time >= start].reset_index(drop=True)
        if len(m) < 10:
            continue
        t = None
        for idx in range(len(m)):
            c = m.iloc[idx]
            hit = (c.low <= hi) if is_long else (c.high >= lo)
            if hit:
                t = idx
                break
        if t is None:
            continue

        w = m.iloc[max(t - 5, 0):t + 1]
        ref = w.high.max() if is_long else w.low.min()
        for idx in range(t + 1, min(t + 61, len(m))):
            c = m.iloc[idx]
            broke = (c.close < lo) if is_long else (c.close > hi)
            if broke:
                break
            choch = (c.close > ref) if is_long else (c.close < ref)
            if choch:
                stop = lo - STOP_PUFFER if is_long else hi + STOP_PUFFER
                entries.append({
                    "kind": b["kind"],
                    "t": int(c.time.timestamp()),
                    "price": round(float(c.close), 2),
                    "stop": round(float(stop), 2),
                    "lo": round(lo, 2),
                    "hi": round(hi, 2),
                })
                break
    return entries


def main():
    h = login()
    m1 = get_candles(h, "MINUTE").iloc[:-1].reset_index(drop=True)
    dfs = {n: get_candles(h, r) for n, r in FRAMES.items()}
    trend = get_trend(dfs["H1"].iloc[:-1].reset_index(drop=True))
    out = {
        "updated": datetime.now(timezone.utc).isoformat(),
        "trend": trend,
        "frames": {},
        "entries": [],
    }
    for name, df in dfs.items():
        blocks = find_blocks(df)
        shown = df.iloc[-SHOW:]
        first_t = int(shown.iloc[0].time.timestamp())
        if name == "M5":
            closed = df.iloc[:-1].reset_index(drop=True)
            ents = find_entries(closed, m1, blocks)
            out["entries"] = [e for e in ents if e["t"] >= first_t]
        visible = [b for b in blocks if b["t"] >= first_t]
        merged = merge(visible, GAP[name])
        clean = [
            {
                "kind": b["kind"],
                "lo": round(b["lo"], 2),
                "hi": round(b["hi"], 2),
                "t": b["t"],
                "status": b["status"],
                "n": b["n"],
                "with": trend == "neutral" or b["kind"] == trend,
            }
            for b in merged
        ]
        candles = [
            {
                "t": int(r.time.timestamp()),
                "o": round(float(r.open), 2),
                "h": round(float(r.high), 2),
                "l": round(float(r.low), 2),
                "c": round(float(r.close), 2),
            }
            for r in shown.itertuples()
        ]
        out["frames"][name] = {"candles": candles, "blocks": clean}

    os.makedirs("docs", exist_ok=True)
    with open("docs/data.json", "w") as f:
        json.dump(out, f)


main()
