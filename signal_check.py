import os
from datetime import datetime, timedelta, timezone
import requests
import pandas as pd

BASE = "https://demo-api-capital.backend-capital.com/api/v1"
EPIC = "GOLD"
WINDOW_MIN = 7      # nur Ereignisse der letzten 7 Minuten melden
STRONG = 1.5        # Impuls-Kerze = Körper 1,5x größer als der Durchschnitt
SWING = 10          # Ausbruch über Hoch/Tief der letzten 10 Kerzen

TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
CHAT = os.environ["TELEGRAM_CHAT_ID"]


def tg(text):
    requests.post(
        f"https://api.telegram.org/bot{TOKEN}/sendMessage",
        data={"chat_id": CHAT, "text": text},
        timeout=15,
    )


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


def get_candles(h):
    r = requests.get(
        f"{BASE}/prices/{EPIC}",
        headers=h,
        params={"resolution": "MINUTE_5", "max": 300},
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
    return df.iloc[:-1].reset_index(drop=True)  # laufende Kerze weglassen


def find_blocks(df):
    blocks = []
    body = (df["close"] - df["open"]).abs()
    for i in range(SWING + 20, len(df)):
        avg = body.iloc[i - 20:i].mean()
        c = df.iloc[i]
        prev = df.iloc[i - SWING:i]
        if body.iloc[i] < STRONG * avg:
            continue
        if c.close > c.open and c.close > prev.high.max():
            kind = "long"
            for k in range(i - 1, max(i - 6, 0), -1):
                if df.iloc[k].close < df.iloc[k].open:
                    blocks.append((kind, k, i))
                    break
        elif c.close < c.open and c.close < prev.low.min():
            kind = "short"
            for k in range(i - 1, max(i - 6, 0), -1):
                if df.iloc[k].close > df.iloc[k].open:
                    blocks.append((kind, k, i))
                    break
    return blocks


def main():
            tg("✅ Test: Bot läuft")
    now = datetime.now(timezone.utc)
    if now.weekday() >= 5:
        return
    cutoff = now - timedelta(minutes=WINDOW_MIN)
    df = get_candles(login())
    close_time = lambda i: df.iloc[i].time + timedelta(minutes=5)

    for kind, k, i in find_blocks(df):
        lo, hi = df.iloc[k].low, df.iloc[k].high
        zone = f"{lo:.2f} - {hi:.2f}"
        name = "Long" if kind == "long" else "Short"
        icon = "🟢" if kind == "long" else "🔴"

        if close_time(i) >= cutoff:
            tg(f"{icon} Neuer {name}-Orderblock (M5)\nZone: {zone}")

        touched = False
        for j in range(i + 1, len(df)):
            c = df.iloc[j]
            if kind == "long":
                invalid = c.close < lo
                touch = c.low <= hi
            else:
                invalid = c.close > hi
                touch = c.high >= lo
            if invalid:
                if close_time(j) >= cutoff:
                    tg(f"❌ {name}-Orderblock ungültig\nZone: {zone}")
                break
            if touch and not touched:
                touched = True
                if close_time(j) >= cutoff:
                    tg(f"⚡ {name}-Orderblock zum ersten Mal angetestet\nZone: {zone}")


main()
