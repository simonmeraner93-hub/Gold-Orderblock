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
HISTORY_DAYS = 14
STATE_FILE = "sent.json"
SESSIONS = [("Asien", 0, 7), ("London", 7, 13), ("New York", 13, 21)]

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


def calc_levels(h1, last):
    df = h1.copy()
    df["date"] = df.time.dt.date
    df["hour"] = df.time.dt.hour
    today = df.date.iloc[-1]
    levels = []

    prev = [d for d in df.date.unique() if d < today]
    if prev:
        p = df[df.date == prev[-1]]
        levels.append({"n": "PDH", "p": float(p.high.max()), "k": "pd"})
        levels.append({"n": "PDL", "p": float(p.low.min()), "k": "pd"})

    t = df[df.date == today]
    levels.append({"n": "Tageshoch", "p": float(t.high.max()), "k": "day"})
    levels.append({"n": "Tagestief", "p": float(t.low.min()), "k": "day"})

    for name, s, e in SESSIONS:
        sel = df[(df.hour >= s) & (df.hour < e)]
        if sel.empty:
            continue
        d = sel.date.iloc[-1]
        w = sel[sel.date == d]
        levels.append({"n": f"{name} Hoch", "p": float(w.high.max()), "k": "sess"})
        levels.append({"n": f"{name} Tief", "p": float(w.low.min()), "k": "sess"})

    base = int(last // 50) * 50
    for k in range(-2, 4):
        p = base + 50 * k
        if abs(p - last) <= 100:
            levels.append({"n": f"Marke {p}", "p": float(p), "k": "round"})

    for l in levels:
        l["p"] = round(l["p"], 2)
    return levels


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
                    "bt": b["t"],
                    "t": int(c.time.timestamp()),
                    "price": round(float(c.close), 2),
                    "stop": round(float(stop), 2),
                    "lo": round(lo, 2),
                    "hi": round(hi, 2),
                })
                break
    return entries


def load_history():
    try:
        with open("docs/data.json") as f:
            return json.load(f).get("history", [])
    except Exception:
        return []


def evaluate_entry(h, m1, m5):
    t_close = pd.Timestamp(h["t"] + 60, unit="s", tz="UTC")
    if len(m1) and m1.time.iloc[0] <= t_close:
        series = m1[m1.time >= t_close]
    else:
        series = m5[m5.time >= t_close.ceil("5min")]
    is_long = h["kind"] == "long"
    for r in series.itertuples():
        if is_long:
            stop_hit = r.low <= h["stop"]
            tgt_hit = r.high >= h["target"]
        else:
            stop_hit = r.high >= h["stop"]
            tgt_hit = r.low <= h["target"]
        if stop_hit:
            return "Stop", int(r.time.timestamp())
        if tgt_hit:
            return "Ziel", int(r.time.timestamp())
    return "offen", None


def main():
    now = datetime.now(timezone.utc)
    state = load_state()
    cutoff = now - timedelta(minutes=WINDOW_MIN)

    h = login()
    full = {n: get_candles(h, r) for n, r in FRAMES.items()}
    m1 = get_candles(h, "MINUTE").iloc[:-1].reset_index(drop=True)
    closed = {n: d.iloc[:-1].reset_index(drop=True) for n, d in full.items()}
    trend = get_trend(closed["H1"])

    m5 = closed["M5"]
    last = float(m5.close.iloc[-1])
    levels = calc_levels(full["H1"], last)
    above = [l for l in levels if l["p"] > last]
    below = [l for l in levels if l["p"] < last]
    key = {
        "res": min(above, key=lambda l: l["p"]) if above else None,
        "sup": max(below, key=lambda l: l["p"]) if below else None,
    }
    blocks5 = find_blocks(m5)
    ents5 = find_entries(m5, m1, blocks5)

    def notify(key, text):
        if key in state:
            return
        tg(text)
        state[key] = now.timestamp()

    def close5(i):
        return m5.iloc[i].time + timedelta(minutes=5)

    if now.weekday() < 5:
        for b in blocks5:
            kind, i, bt = b["kind"], b["i"], b["t"]
            lo, hi = b["lo"], b["hi"]
            is_long = kind == "long"
            zone = f"{lo:.2f} - {hi:.2f}"
            name = "Long" if is_long else "Short"
            icon = "🟢" if is_long else "🔴"

            if close5(i) >= cutoff:
                notify(f"{kind}-{bt}-new",
                       f"{icon} Neuer {name}-Orderblock (M5)\nZone: {zone}")

            touched = False
            for j in range(i + 1, len(m5)):
                c = m5.iloc[j]
                inv = (c.close < lo) if is_long else (c.close > hi)
                tch = (c.low <= hi) if is_long else (c.high >= lo)
                if inv:
                    if close5(j) >= cutoff:
                        notify(f"{kind}-{bt}-invalid",
                               f"❌ {name}-Orderblock ungültig\nZone: {zone}")
                    break
                if tch and not touched:
                    touched = True
                    if close5(j) >= cutoff:
                        notify(f"{kind}-{bt}-touch",
                               f"⚡ {name}-Orderblock zum ersten Mal angetestet\nZone: {zone}")

        trend_name = {"long": "Long", "short": "Short", "neutral": "Neutral"}[trend]
        for e in ents5:
            if e["t"] + 60 < cutoff.timestamp():
                continue
            is_long = e["kind"] == "long"
            name = "Long" if is_long else "Short"
            risk = abs(e["price"] - e["stop"])
            if risk <= 0:
                continue
            cands = sorted(
                [l for l in levels
                 if (l["p"] > e["price"] if is_long else l["p"] < e["price"])],
                key=lambda l: abs(l["p"] - e["price"]),
            )
            tline = "Ziel: offen (kein Level mit CRV 1:1)"
            for l in cands:
                rew = abs(l["p"] - e["price"])
                if rew >= risk:
                    tline = f"Ziel: {l['p']:.2f} ({l['n']}, CRV 1:{rew / risk:.1f})"
                    break
            fits = trend == "neutral" or e["kind"] == trend
            tr = "passt" if fits else "GEGEN den Trend"
            notify(
                f"{e['kind']}-{e['bt']}-entry",
                f"🚀 {name}-Einstieg bestätigt (M1-CHoCH)\n"
                f"Zone: {e['lo']:.2f} - {e['hi']:.2f}\n"
                f"Einstieg ca.: {e['price']:.2f}\n"
                f"Stop: {e['stop']:.2f} (Risiko {risk:.1f} Punkte)\n"
                f"{tline}\n"
                f"H1-Trend: {trend_name} ({tr})\n"
                f"Erst im Demokonto testen, kein Finanzrat.",
            )

    history = load_history()
    known = {x["id"] for x in history}
    first_run = len(history) == 0
    for e in ents5:
        eid = f"{e['kind']}-{e['bt']}"
        if eid in known:
            continue
        risk = abs(e["price"] - e["stop"])
        if risk <= 0:
            continue
        if e["kind"] == "long":
            target = e["price"] + risk
        else:
            target = e["price"] - risk
        history.append({
            "id": eid,
            "kind": e["kind"],
            "t": e["t"],
            "price": e["price"],
            "stop": e["stop"],
            "target": round(target, 2),
            "lo": e["lo"],
            "hi": e["hi"],
            "fit": None if first_run else (trend == "neutral" or e["kind"] == trend),
            "res": "offen",
            "rt": None,
        })
    keep_t = (now - timedelta(days=HISTORY_DAYS)).timestamp()
    history = [x for x in history if x["t"] >= keep_t]
    for x in history:
        if x["res"] == "offen":
            res, rt = evaluate_entry(x, m1, m5)
            x["res"] = res
            x["rt"] = rt
            if res == "offen" and x["t"] < (now - timedelta(hours=23)).timestamp():
                x["res"] = "unklar"
    history.sort(key=lambda x: x["t"], reverse=True)

    out = {
        "updated": now.isoformat(),
        "trend": trend,
        "levels": levels,
        "key": key,
        "history": history,
        "frames": {},
        "entries": [],
    }
    for name, df_full in full.items():
        blocks = blocks5 if name == "M5" else find_blocks(closed[name])
        shown = df_full.iloc[-SHOW:]
        first_t = int(shown.iloc[0].time.timestamp())
        if name == "M5":
            limit = max(first_t, int((now - timedelta(hours=APP_ENTRY_HOURS)).timestamp()))
            out["entries"] = [
                {k: v for k, v in e.items() if k != "bt"}
                for e in ents5 if e["t"] >= limit
            ]
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
    save_state(state)


main()
