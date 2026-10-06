import os
import json
from datetime import datetime, timedelta, timezone
import requests
import pandas as pd

BASE = "https://demo-api-capital.backend-capital.com/api/v1"
EPIC = "GOLD"
WINDOW_MIN = 20
STRONG = 1.5
SWING = 10
STOP_PUFFER = 0.5
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
    df = pd.DataFrame(rows).sort_values("time").reset_index(drop=True)
    return df.iloc[:-1].reset_index(drop=True)


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
            for k in range(i - 1, max(i - 6, 0), -1):
                if df.iloc[k].close < df.iloc[k].open:
                    blocks.append(("long", k, i))
                    break
        elif c.close < c.open and c.close < prev.low.min():
            for k in range(i - 1, max(i - 6, 0), -1):
                if df.iloc[k].close > df.iloc[k].open:
                    blocks.append(("short", k, i))
                    break
    return blocks


def main():
    now = datetime.now(timezone.utc)
    state = load_state()
    if now.weekday() >= 5:
        save_state(state)
        return
    cutoff = now - timedelta(minutes=WINDOW_MIN)

    h = login()
    m5 = get_candles(h, "MINUTE_5")
    m1 = get_candles(h, "MINUTE")

    def notify(key, text):
        if key in state:
            return
        tg(text)
        state[key] = now.timestamp()

    def c5(i):
        return m5.iloc[i].time + timedelta(minutes=5)

    seen = set()
    for kind, k, i in find_blocks(m5):
        bt = int(m5.iloc[k].time.timestamp())
        if (kind, bt) in seen:
            continue
        seen.add((kind, bt))

        lo, hi = m5.iloc[k].low, m5.iloc[k].high
        zone = f"{lo:.2f} - {hi:.2f}"
        is_long = kind == "long"
        name = "Long" if is_long else "Short"
        icon = "🟢" if is_long else "🔴"

        if c5(i) >= cutoff:
            notify(f"{kind}-{bt}-new",
                   f"{icon} Neuer {name}-Orderblock (M5)\nZone: {zone}")

        touched = False
        invalid = False
        for j in range(i + 1, len(m5)):
            c = m5.iloc[j]
            inv = (c.close < lo) if is_long else (c.close > hi)
            tch = (c.low <= hi) if is_long else (c.high >= lo)
            if inv:
                invalid = True
                if c5(j) >= cutoff:
                    notify(f"{kind}-{bt}-invalid",
                           f"❌ {name}-Orderblock ungültig\nZone: {zone}")
                break
            if tch and not touched:
                touched = True
                if c5(j) >= cutoff:
                    notify(f"{kind}-{bt}-touch",
                           f"⚡ {name}-Orderblock zum ersten Mal angetestet\nZone: {zone}")

        if invalid or not touched:
            continue

        m = m1[m1.time >= c5(i)].reset_index(drop=True)
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
            if not choch:
                continue
            close_t = c.time + timedelta(minutes=1)
            if close_t >= cutoff:
                entry = c.close
                stop = lo - STOP_PUFFER if is_long else hi + STOP_PUFFER
                risk = abs(entry - stop)
                if risk > 0:
                    if is_long:
                        target = m5.high.iloc[-60:].max()
                        reward = target - entry
                    else:
                        target = m5.low.iloc[-60:].min()
                        reward = entry - target
                    if reward >= risk * 0.5:
                        tline = f"Ziel: {target:.2f} (CRV 1:{reward / risk:.1f})"
                    else:
                        tline = "Ziel: offen"
                    notify(
                        f"{kind}-{bt}-entry",
                        f"🚀 {name}-Einstieg bestätigt (M1-CHoCH)\n"
                        f"Zone: {zone}\n"
                        f"Einstieg ca.: {entry:.2f}\n"
                        f"Stop: {stop:.2f} (Risiko {risk:.1f} Punkte)\n"
                        f"{tline}\n"
                        f"Erst im Demokonto testen, kein Finanzrat.",
                    )
            break

    save_state(state)


main()
