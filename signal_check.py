"""Orderblock-Radar fuer mehrere Maerkte.

Telegram nur bei neuen Orderblöcken (M5 und H1), die
 - in Richtung des H1-Trends liegen (bei neutralem Trend beide Richtungen) und
 - ein Fair Value Gap haben (ist es noch nicht bekannt: Meldung mit Fragezeichen,
   ist es bekannt und nicht vorhanden: keine Meldung).
Schreibt ausserdem docs/data.json fuer die App.
"""
import os
import json
from datetime import datetime, timedelta, timezone
import requests
import pandas as pd

BASE = "https://demo-api-capital.backend-capital.com/api/v1"
REF_PRICE = 4160.0
GAP_BASE = {"M5": 1.0, "H1": 4.0}
STRONG = 1.5
SWING = 10
SHOW = 150
WINDOW_MIN = 30
STATE_FILE = "sent.json"
SESSIONS = [("Asien", 0, 7), ("London", 7, 13), ("New York", 13, 21)]

# key, Name, moegliche Epics, Nachkommastellen, Abstand der runden Marken
MARKETS = [
    ("gold", "Gold", ["GOLD"], 2, 50),
    ("us500", "S&P 500", ["US500"], 2, 100),
    ("us100", "Nasdaq 100", ["US100"], 2, 500),
    ("eurusd", "EUR/USD", ["EURUSD"], 5, 0.01),
    ("gbpusd", "GBP/USD", ["GBPUSD"], 5, 0.01),
    ("dax", "DAX", ["DE40", "GER40", "DAX40"], 2, 500),
]

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


def get_candles(h, epic, resolution):
    r = requests.get(
        f"{BASE}/prices/{epic}",
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


def calc_levels(h1, last, step, dec):
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
    base = (last // step) * step
    for k in range(-2, 4):
        p = base + step * k
        if abs(p - last) <= 2 * step:
            levels.append({"n": f"Marke {round(p, dec)}", "p": float(p), "k": "round"})
    for l in levels:
        l["p"] = round(l["p"], dec)
    return levels


def find_blocks(df):
    """Orderblöcke: starke Impulskerze + letzte Gegenkerze davor."""
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
        known = i + 1 < len(df)          # Folgekerze schon geschlossen?
        fvg = False
        if known:
            if kind == "long":
                fvg = bool(df.iloc[i + 1].low > df.iloc[i - 1].high)
            else:
                fvg = bool(df.iloc[i + 1].high < df.iloc[i - 1].low)
        result.append({
            "kind": kind, "k": k, "i": i, "lo": lo, "hi": hi,
            "t": int(df.iloc[k].time.timestamp()), "status": status,
            "fvg": fvg, "fvg_known": known,
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
                cur["fvg"] = cur["fvg"] or b["fvg"]
                if b["status"] == "angetestet":
                    cur["status"] = "angetestet"
            else:
                if cur is not None:
                    result.append(cur)
                cur = dict(b)
                cur["n"] = 1
        if cur is not None:
            result.append(cur)
    return result


def sweep_info(df, b, levels, unit2):
    """Liquiditaets-Sweep vor/waehrend der Zonenbildung."""
    k, i = b["k"], b["i"]
    if k < 2 or i >= len(df):
        return None
    is_long = b["kind"] == "long"
    win = df.iloc[max(k - 1, 0):i + 1]
    close_i = float(df.iloc[i].close)
    cands = []
    for n_bars, label in ((20, "letzten 20 Kerzen"), (48, unit2)):
        prior = df.iloc[max(0, k - 1 - n_bars):max(k - 1, 0)]
        if len(prior) < 10:
            continue
        if is_long:
            lvl = float(prior.low.min())
            if float(win.low.min()) < lvl < close_i:
                cands.append((lvl, "Tief der " + label))
        else:
            lvl = float(prior.high.max())
            if float(win.high.max()) > lvl > close_i:
                cands.append((lvl, "Hoch der " + label))
    for l in levels:
        if l["n"] == ("PDL" if is_long else "PDH"):
            if is_long and float(win.low.min()) < l["p"] < close_i:
                cands.append((l["p"], "Vortagstief"))
            if (not is_long) and float(win.high.max()) > l["p"] > close_i:
                cands.append((l["p"], "Vortageshoch"))
    if not cands:
        return None
    return (max(cands) if is_long else min(cands))[1]


TRTXT = {"long": "Long (aufwärts)", "short": "Short (abwärts)", "neutral": "Neutral"}


def process_market(h, state, now, cutoff, notify_ok, key, name, cands, dec, step, prev_epic):
    order = ([prev_epic] if prev_epic else []) + [c for c in cands if c != prev_epic]
    epic, h1_full, m5_full, err = None, None, None, None
    for ep in order:
        try:
            h1_full = get_candles(h, ep, "HOUR")
            m5_full = get_candles(h, ep, "MINUTE_5")
            epic = ep
            break
        except Exception as e:
            err = e
    if epic is None:
        raise RuntimeError(f"kein Epic gefunden ({cands}): {err}")

    h1c = h1_full.iloc[:-1].reset_index(drop=True)
    m5c = m5_full.iloc[:-1].reset_index(drop=True)
    if len(h1c) < 60 or len(m5c) < 60:
        raise RuntimeError("zu wenige Kerzen")
    trend = get_trend(h1c)
    last = float(m5c.close.iloc[-1])
    levels = calc_levels(h1_full, last, step, dec)
    scale = last / REF_PRICE

    dfs = {"M5": (m5c, m5_full, 5), "H1": (h1c, h1_full, 60)}
    blocks = {tf: find_blocks(dfs[tf][0]) for tf in dfs}
    valid_h1 = [b for b in blocks["H1"] if b["status"] != "ungueltig"]

    # ---- Telegram ----
    if notify_ok:
        for tf, (df, _, mins) in dfs.items():
            for b in blocks[tf]:
                if b["status"] == "ungueltig":
                    continue
                if trend != "neutral" and b["kind"] != trend:
                    continue
                if b["fvg_known"] and not b["fvg"]:
                    continue
                close_t = df.iloc[b["i"]].time + timedelta(minutes=mins)
                if close_t < cutoff:
                    continue
                skey = f"{epic}-{tf}-{b['kind']}-{b['t']}"
                if key == "gold" and tf == "M5":
                    skey = f"{b['kind']}-{b['t']}-new"
                if skey in state:
                    continue
                long_ = b["kind"] == "long"
                icon = "🟢" if long_ else "🔴"
                fv = "✓" if b["fvg"] else "? (Folgekerze fehlt noch)"
                sw = sweep_info(df, b, levels, "letzten 4 Std." if tf == "M5" else "letzten 2 Tage")
                swt = f"✓ ({sw})" if sw else "✗"
                lines = [
                    f"{icon} {name.upper() if key == 'gold' else name}: Neuer {'Long' if long_ else 'Short'}-Orderblock ({tf})",
                    f"Zone: {b['lo']:.{dec}f} - {b['hi']:.{dec}f}",
                    f"H1-Trend: {TRTXT[trend]} · Fair Value Gap: {fv}",
                ]
                if tf == "M5":
                    h1z = any(
                        x["kind"] == b["kind"] and x["lo"] <= b["hi"] and x["hi"] >= b["lo"]
                        for x in valid_h1
                    )
                    lines.append(f"in H1-Zone: {'✓' if h1z else '✗'} · Liquidity-Sweep: {swt}")
                else:
                    lines.append(f"Liquidity-Sweep: {swt}")
                tg("\n".join(lines))
                state[skey] = now.timestamp()

    # ---- App-Daten ----
    above = [l for l in levels if l["p"] > last]
    below = [l for l in levels if l["p"] < last]
    keyl = {
        "res": min(above, key=lambda l: l["p"]) if above else None,
        "sup": max(below, key=lambda l: l["p"]) if below else None,
    }
    frames = {}
    for tf, (df, full, mins) in dfs.items():
        shown = full.iloc[-SHOW:]
        first_t = int(shown.iloc[0].time.timestamp())
        vis = [b for b in blocks[tf] if b["t"] >= first_t]
        merged = merge(vis, GAP_BASE[tf] * scale)
        zones = [{
            "kind": b["kind"], "lo": round(b["lo"], dec), "hi": round(b["hi"], dec),
            "t": b["t"], "status": b["status"], "n": b["n"],
            "fvg": bool(b["fvg"]),
            "with": trend == "neutral" or b["kind"] == trend,
        } for b in merged]
        candles = [{
            "t": int(r.time.timestamp()),
            "o": round(float(r.open), dec), "h": round(float(r.high), dec),
            "l": round(float(r.low), dec), "c": round(float(r.close), dec),
        } for r in shown.itertuples()]
        frames[tf] = {"candles": candles, "blocks": zones}
    return {
        "key": key, "name": name, "epic": epic, "dec": dec, "trend": trend,
        "price": round(last, dec), "levels": levels, "keyl": keyl, "frames": frames,
    }


def main():
    now = datetime.now(timezone.utc)
    cutoff = now - timedelta(minutes=WINDOW_MIN)
    notify_ok = now.weekday() < 5
    state = load_state()
    try:
        with open("docs/data.json") as f:
            old = {m["key"]: m for m in json.load(f).get("markets", [])}
    except Exception:
        old = {}
    h = login()
    out = []
    for key, name, cands, dec, step in MARKETS:
        try:
            m = process_market(h, state, now, cutoff, notify_ok, key, name, cands, dec, step,
                               old.get(key, {}).get("epic"))
            print(name, m["epic"], "ok, Trend", m["trend"])
            out.append(m)
        except Exception as e:
            print(name, "Fehler:", e)
            if key in old:
                o = old[key]
                o["stale"] = True
                out.append(o)
    os.makedirs("docs", exist_ok=True)
    with open("docs/data.json", "w") as f:
        json.dump({"updated": now.isoformat(), "markets": out}, f)
    save_state(state)


main()
