"""Orderblock-Radar fuer mehrere Maerkte.

Telegram bei Orderblöcken (M5 und H1), die ein Fair Value Gap haben (ist es noch nicht
bekannt: Meldung mit Fragezeichen, ist es bekannt und nicht vorhanden: keine Meldung),
mit UND gegen den H1-Trend (die Meldung sagt, ob mit oder gegen den Trend):
 1. beim neuen Orderblock
 2. wenn der Kurs den Orderblock zum ersten Mal antestet
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
FVG_MIN = 0.25    # Fair Value Gap muss mindestens so gross sein wie dieser Anteil der durchschnittlichen Kerzenkoerper
GROUP_MAX = 2     # so viele kleine Fortsetzungskerzen duerfen zwischen Block und Impuls liegen
ADJACENT = True   # Orderblock muss die Kerze direkt vor dem Impuls sein (dann hängt das FVG am Block)
CONF_WAIT = {"M5": 12, "H1": 6}   # Kerzen nach dem ersten Antest, in denen die Bestaetigung kommen darf (nie auf der Antest-Kerze selbst)
SWING = 10
SHOW = 150
WINDOW_MIN = 30
LOG_KEEP_DAYS = 14   # so lange bleiben einzelne Testtrades in data.json, danach nur noch als Zaehler im Archiv
STATE_FILE = "sent.json"
SESSIONS = [("Asien", 0, 7), ("London", 7, 13), ("New York", 13, 21)]

# key, Name, moegliche Epics, Nachkommastellen, Abstand der runden Marken
MARKETS = [
    ("gold", "Gold", ["GOLD"], 2, 50),
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
        adj = k == i - 1
        # kleine Fortsetzungskerzen zwischen Block und Impuls (max. 2) gehoeren zur Zone
        if not adj and i - k - 1 <= GROUP_MAX:
            avg_g = float(body.iloc[max(0, i - 20):i].mean())
            same = True
            for j in range(k + 1, i):
                cj = df.iloc[j]
                dir_ok = (cj.close > cj.open) if kind == "long" else (cj.close < cj.open)
                if not dir_ok or abs(cj.close - cj.open) >= STRONG * avg_g:
                    same = False
                    break
            if same:
                adj = True
                lo = float(df.iloc[k:i].low.min())
                hi = float(df.iloc[k:i].high.max())
        status = "frisch"
        touch_j = None
        for j in range(i + 1, len(df)):
            c = df.iloc[j]
            if kind == "long":
                if c.close < lo:
                    status = "ungueltig"
                    break
                if c.low <= hi:
                    status = "angetestet"
                    if touch_j is None:
                        touch_j = j
            else:
                if c.close > hi:
                    status = "ungueltig"
                    break
                if c.high >= lo:
                    status = "angetestet"
                    if touch_j is None:
                        touch_j = j
        known = i + 1 < len(df)          # Folgekerze schon geschlossen?
        fvg = False
        if known:
            if kind == "long":
                fvg = bool(df.iloc[i + 1].low > df.iloc[i - 1].high)
            else:
                fvg = bool(df.iloc[i + 1].high < df.iloc[i - 1].low)
            if fvg:
                avg_b = float(body.iloc[max(0, i - 20):i].mean())
                size = (float(df.iloc[i + 1].low - df.iloc[i - 1].high) if kind == "long"
                        else float(df.iloc[i - 1].low - df.iloc[i + 1].high))
                if size < FVG_MIN * avg_b:
                    fvg = False
        fl = fh = ft = None
        filled = False
        if known and fvg:
            if kind == "long":
                fl, fh = float(df.iloc[i - 1].high), float(df.iloc[i + 1].low)
            else:
                fl, fh = float(df.iloc[i + 1].high), float(df.iloc[i - 1].low)
            ft = int(df.iloc[i - 1].time.timestamp())
            for j in range(i + 2, len(df)):
                if (kind == "long" and df.iloc[j].low <= fl) or (kind == "short" and df.iloc[j].high >= fh):
                    filled = True
                    break
        result.append({
            "kind": kind, "k": k, "i": i, "lo": lo, "hi": hi,
            "t": int(df.iloc[k].time.timestamp()), "status": status,
            "touch_t": int(df.iloc[touch_j].time.timestamp()) if touch_j is not None else None,
            "fvg": fvg, "fvg_known": known, "adj": adj,
            "fvg_lo": fl, "fvg_hi": fh, "fvg_t": ft, "fvg_filled": filled,
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


def rel_txt(trend, kind):
    if trend == "neutral":
        return "kein klarer Trend"
    return "mit dem Trend ✓" if kind == trend else "GEGEN den Trend ⚠"


def entry_check(df, times, b, tf):
    """Index der Bestaetigungskerze oder None. Bestaetigung fruehestens auf der Kerze NACH dem ersten Antest."""
    j0 = next((j for j, t in enumerate(times) if t >= b["touch_t"]), None)
    if j0 is None:
        return None
    long_ = b["kind"] == "long"
    body = (df.close - df.open).abs()
    armed = None  # letzte rote Antest-Kerze; jeder Antest zaehlt
    for j in range(j0, len(df)):
        c = df.iloc[j]
        if (long_ and c.close < b["lo"]) or (not long_ and c.close > b["hi"]):
            return None  # Zone durchbrochen
        touched = c.low <= b["hi"] if long_ else c.high >= b["lo"]
        red = c.close < c.open if long_ else c.close > c.open
        if touched and red:
            armed = j
            continue
        if armed is None or j - armed > CONF_WAIT[tf]:
            continue
        p = df.iloc[j - 1]
        avg_b = body.iloc[max(0, j - 20):j].mean()
        if abs(c.close - c.open) < STRONG * avg_b:
            continue  # Bestaetigungskerze muss gross sein (wie der Impuls)
        if long_ and c.close > c.open and c.close > p.high:
            return j
        if not long_ and c.close < c.open and c.close < p.low:
            return j
    return None


def paper_stats(dfs, blocks_all):
    """Testtrades 1:1 ueber alle Zonen der geladenen Kerzen - auch ueber Zonen, die danach
    durchbrochen wurden (sonst wuerden gerade die Stops fehlen). Regeln wie beim Einstiegsalarm."""
    stats, trades = {}, []
    for tf, (df, full, mins) in dfs.items():
        times = [int(x.timestamp()) for x in df.time]
        z = sl = o = 0
        open_r = 0.0
        for b in blocks_all[tf]:
            if b["touch_t"] is None:
                continue
            if ADJACENT and not b["adj"]:
                continue
            if b["fvg_known"] and not b["fvg"]:
                continue
            j = entry_check(df, times, b, tf)
            if j is None:
                continue
            long_ = b["kind"] == "long"
            price = float(df.iloc[j].close)
            stop = b["lo"] if long_ else b["hi"]
            risk = abs(price - stop)
            if not risk > 0:
                continue
            tgt = price + risk if long_ else price - risk
            ec = df.iloc[j]
            res = "offen"
            if (long_ and ec.low <= stop) or (not long_ and ec.high >= stop):
                res = "stop"
            else:
                for k in range(j + 1, len(full)):
                    c = full.iloc[k]
                    hit_stop = c.low <= stop if long_ else c.high >= stop
                    hit_tgt = c.high >= tgt if long_ else c.low <= tgt
                    if hit_stop:
                        res = "stop"
                        break
                    if hit_tgt:
                        res = "ziel"
                        break
            if res == "ziel":
                z += 1
                r = 1.0
            elif res == "stop":
                sl += 1
                r = -1.0
            else:
                o += 1
                last = float(full.iloc[-1].close)
                r = ((last - price) if long_ else (price - last)) / risk
                open_r += r
            trades.append({"id": f"{tf}-{b['kind']}-{b['t']}", "tf": tf, "kind": b["kind"],
                           "t": times[j], "res": res, "r": round(r, 2)})
        stats[tf] = {"z": z, "s": sl, "o": o, "open_r": round(open_r, 2)}
    trades.sort(key=lambda x: x["t"], reverse=True)
    return stats, trades


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
    blocks_all = {tf: list(v) for tf, v in blocks.items()}  # fuer die Statistik, bevor auf das sichtbare Fenster gekuerzt wird
    # nur Zonen aus dem sichtbaren Zeitfenster (gleich wie in der App) - Alarme und Chart bleiben so deckungsgleich
    for tf, (_d, full_tf0, _m) in dfs.items():
        t0 = int(full_tf0.iloc[-SHOW:].iloc[0].time.timestamp())
        blocks[tf] = [b for b in blocks[tf] if b["t"] >= t0]
    valid_h1 = [b for b in blocks["H1"] if b["status"] != "ungueltig"]

    # Antest durch die noch laufende Kerze sofort erkennen (nicht erst nach Kerzenschluss)
    for tf, (df, full, mins) in dfs.items():
        run = full.iloc[-1]
        run_t = int(run.time.timestamp())
        for b in blocks[tf]:
            if b["status"] != "frisch":
                continue
            hit = (float(run.low) <= b["hi"]) if b["kind"] == "long" else (float(run.high) >= b["lo"])
            if hit and run_t > b["t"]:
                b["status"] = "angetestet"
                b["touch_t"] = run_t

    # ---- Telegram ----
    if notify_ok:
        for tf, (df, _, mins) in dfs.items():
            for b in blocks[tf]:
                if b["status"] == "ungueltig":
                    continue
                if ADJACENT and not b["adj"]:
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
                    f"H1-Trend: {TRTXT[trend]} · {rel_txt(trend, b['kind'])} · Fair Value Gap: {fv}",
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

        # ---- Antest-Meldungen ----
        for tf, (df, full_tf, mins) in dfs.items():
            now_price = float(full_tf.iloc[-1].close)
            for b in blocks[tf]:
                if b["status"] != "angetestet" or b["touch_t"] is None:
                    continue
                if ADJACENT and not b["adj"]:
                    continue
                if b["fvg_known"] and not b["fvg"]:
                    continue
                if b["touch_t"] + mins * 60 < cutoff.timestamp():
                    continue
                skey = f"{epic}-{tf}-{b['kind']}-{b['t']}-test"
                if skey in state:
                    continue
                long_ = b["kind"] == "long"
                icon = "🟢" if long_ else "🔴"
                fv = "✓" if b["fvg"] else "?"
                tg("\n".join([
                    f"🎯 {name.upper() if key == 'gold' else name}: {'Long' if long_ else 'Short'}-Orderblock ANGETESTET ({tf}) {icon}",
                    f"Zone: {b['lo']:.{dec}f} - {b['hi']:.{dec}f} · Kurs jetzt: {now_price:.{dec}f}",
                    f"H1-Trend: {TRTXT[trend]} · {rel_txt(trend, b['kind'])} · Fair Value Gap: {fv}",
                ]))
                state[skey] = now.timestamp()

        # ---- Einstieg-Meldungen (Bestaetigungskerze nach dem Antest, nur Signal, keine Order) ----
        for tf, (df, full_tf, mins) in dfs.items():
            times = [int(x.timestamp()) for x in df.time]
            for b in blocks[tf]:
                if b["status"] != "angetestet" or b["touch_t"] is None:
                    continue
                if ADJACENT and not b["adj"]:
                    continue
                if b["fvg_known"] and not b["fvg"]:
                    continue
                j = entry_check(df, times, b, tf)
                if j is None or times[j] + mins * 60 < cutoff.timestamp():
                    continue
                skey = f"{epic}-{tf}-{b['kind']}-{b['t']}-entry"
                if skey in state:
                    continue
                long_ = b["kind"] == "long"
                price = float(df.iloc[j].close)
                stop = b["lo"] if long_ else b["hi"]
                risk = abs(price - stop)
                if not risk > 0:
                    continue
                tgt = price + risk if long_ else price - risk
                icon = "🟢" if long_ else "🔴"
                fv = "✓" if b["fvg"] else "?"
                tg("\n".join([
                    f"📍 {name.upper() if key == 'gold' else name}: EINSTIEG-Signal {'Long' if long_ else 'Short'} ({tf}) {icon}",
                    f"Bestätigungskerze geschlossen bei {price:.{dec}f}",
                    f"Stop (Zonenkante): {stop:.{dec}f} · 1:1 Ziel: {tgt:.{dec}f}",
                    f"Zone: {b['lo']:.{dec}f} - {b['hi']:.{dec}f}",
                    f"H1-Trend: {TRTXT[trend]} · {rel_txt(trend, b['kind'])} · Fair Value Gap: {fv}",
                    "Nur ein Signal, keine Order.",
                ]))
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
        vis = [b for b in blocks[tf]
               if b["t"] >= first_t and b["status"] != "ungueltig"
               and (b["adj"] or not ADJACENT)
               and (b["fvg"] or not b["fvg_known"])]
        zones = []
        for b in vis:
            z = {
                "kind": b["kind"], "lo": round(b["lo"], dec), "hi": round(b["hi"], dec),
                "t": b["t"], "status": b["status"], "touch_t": b["touch_t"],
                "fvg": "ja" if b["fvg"] else "offen",
                "with": trend == "neutral" or b["kind"] == trend,
            }
            if b["fvg"] and not b["fvg_filled"]:
                z["f_lo"] = round(b["fvg_lo"], dec)
                z["f_hi"] = round(b["fvg_hi"], dec)
                z["f_t"] = b["fvg_t"]
            zones.append(z)
        candles = [{
            "t": int(r.time.timestamp()),
            "o": round(float(r.open), dec), "h": round(float(r.high), dec),
            "l": round(float(r.low), dec), "c": round(float(r.close), dec),
        } for r in shown.itertuples()]
        frames[tf] = {"candles": candles, "blocks": zones}
    try:
        stats, trades = paper_stats(dfs, blocks_all)
    except Exception as e:
        print(name, "Statistik-Fehler:", e)
        stats, trades = {}, []
    return {
        "key": key, "name": name, "epic": epic, "dec": dec, "trend": trend,
        "price": round(last, dec), "levels": levels, "keyl": keyl, "frames": frames,
        "stats": stats, "trades": trades,
    }


def merge_log(prev, markets, now):
    """Sammelt die Testtrades dauerhaft: Ein abgeschlossener Trade (Ziel/Stop) wird nie mehr veraendert
    und faellt auch nicht heraus, wenn seine Zone aus dem Kerzenfenster rutscht. Aeltere Trades werden
    nach LOG_KEEP_DAYS nur noch als Zaehler im Archiv geführt."""
    log = {x["id"]: x for x in prev.get("log", []) if "id" in x and "key" in x}
    archive = prev.get("archive", {})
    since = prev.get("log_since") or now.isoformat()
    cutoff = now.timestamp() - LOG_KEEP_DAYS * 86400
    for m in markets:
        if m.get("stale") or not isinstance(m.get("trades"), list):
            continue
        seen = set()
        for t in m["trades"]:
            tid = m["key"] + "-" + t["id"]
            seen.add(tid)
            if t["t"] < cutoff:
                continue                      # schon im Archiv oder zu alt
            old = log.get(tid)
            if old is None or old["res"] == "offen":
                log[tid] = {"id": tid, "key": m["key"], "tf": t["tf"], "kind": t["kind"],
                            "t": t["t"], "res": t["res"], "r": t["r"]}
        for tid, t in log.items():            # offen, aber nicht mehr berechenbar -> nicht zaehlen
            if t["key"] == m["key"] and t["res"] == "offen" and tid not in seen:
                t["res"] = "unklar"
    for tid in list(log):                     # alte, abgeschlossene Trades ins Archiv
        t = log[tid]
        if t["t"] < cutoff and t["res"] != "offen":
            if t["res"] in ("ziel", "stop"):
                a = archive.setdefault(t["key"], {}).setdefault(t["tf"], {"z": 0, "s": 0})
                a["z" if t["res"] == "ziel" else "s"] += 1
            del log[tid]
    for m in markets:
        st = {}
        for tf in ("M5", "H1"):
            a = archive.get(m["key"], {}).get(tf, {"z": 0, "s": 0})
            z, sl, o, orr = a["z"], a["s"], 0, 0.0
            for t in log.values():
                if t["key"] != m["key"] or t["tf"] != tf:
                    continue
                if t["res"] == "ziel":
                    z += 1
                elif t["res"] == "stop":
                    sl += 1
                elif t["res"] == "offen":
                    o += 1
                    orr += t["r"]
            st[tf] = {"z": z, "s": sl, "o": o, "open_r": round(orr, 2)}
        m["stats"] = st
        m["trades"] = sorted((t for t in log.values() if t["key"] == m["key"] and t["res"] != "unklar"),
                             key=lambda x: x["t"], reverse=True)[:30]
    return list(log.values()), archive, since


def main():
    now = datetime.now(timezone.utc)
    cutoff = now - timedelta(minutes=WINDOW_MIN)
    notify_ok = now.weekday() < 5
    state = load_state()
    try:
        with open("docs/data.json") as f:
            prev = json.load(f)
        old = {m["key"]: m for m in prev.get("markets", [])}
    except Exception:
        prev, old = {}, {}
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
    try:
        log, archive, since = merge_log(prev, out, now)
    except Exception as e:
        print("Log-Fehler:", e)
        log, archive, since = prev.get("log", []), prev.get("archive", {}), prev.get("log_since")
    with open("docs/data.json", "w") as f:
        json.dump({"updated": now.isoformat(), "markets": out, "log": log,
                   "archive": archive, "log_since": since}, f, separators=(",", ":"))
    save_state(state)


main()
