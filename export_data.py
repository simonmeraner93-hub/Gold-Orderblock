import os
import json
from datetime import datetime, timezone
import requests
import pandas as pd

BASE = "https://demo-api-capital.backend-capital.com/api/v1"
EPIC = "GOLD"
FRAMES = {"M5": "MINUTE_5", "M15": "MINUTE_15", "H1": "HOUR"}
STRONG = 1.5
SWING = 10
SHOW = 120


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
    df = pd.DataFrame(rows).sort_values("time").reset_index(drop=True)
    return df


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
        lo = df.iloc[k].low
        hi = df.iloc[k].high
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
            "kind": kind,
            "lo": round(float(lo), 2),
            "hi": round(float(hi), 2),
            "t": int(df.iloc[k].time.timestamp()),
            "status": status,
        })
    return result


def main():
    h = login()
    out = {
        "updated": datetime.now(timezone.utc).isoformat(),
        "frames": {},
    }
    for name, res in FRAMES.items():
        df = get_candles(h, res)
        blocks = find_blocks(df)
        shown = df.iloc[-SHOW:]
        first_t = int(shown.iloc[0].time.timestamp())
        blocks = [b for b in blocks if b["t"] >= first_t]
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
        out["frames"][name] = {"candles": candles, "blocks": blocks}

    os.makedirs("docs", exist_ok=True)
    with open("docs/data.json", "w") as f:
        json.dump(out, f)


main()
