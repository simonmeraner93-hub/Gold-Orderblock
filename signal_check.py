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
WINDOW_MIN = 30
APP_ENTRY_HOURS = 6
STATE_FILE = "sent.json"

TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
CHAT = os.environ["TELEGRAM_CHAT_ID"]


def tg(text):
    requests.post(
        f"https://api.telegram.org/bot{TOKEN}/sendMessage",
        data={"chat_id": CHAT, "text": text},
        timeout=15,
    )


def load_state():
    try:
        with open(STATE_FILE) as f:
            return json.load(f)
    except Exception:
        return {}


def save_state(s):
    cut = datetime.now(timezone.utc).timestamp() - 3 * 86400
    s = {k: v for k, v in s.items() if v >= cut}
    with open(STATE_FILE, "w") as f:
        json.dump(s, f)


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
