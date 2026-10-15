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
BRK_LOOK = 20
BRK_ATR = 1.5
BRK_COOLDOWN = 6
BRK_BACKFILL_H = 20
H1_CHOCH_BARS = 12
RT_BARS = 36
RT_FAIL = 0.5

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
        fvg = False
        if i >= 1 and i + 1 < len(df):
            if kind == "long":
                fvg = bool(df.iloc[i + 1].low > df.iloc[i - 1].high)
            else:
                fvg = bool(df.iloc[i + 1].high < df.iloc[i - 1].low)
        result.append({
            "kind": kind, "k": k, "i": i, "lo": lo, "hi": hi,
            "t": int(df.iloc[k].time.timestamp()), "status": status, "fvg": fvg,
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
                    "k": int(b["k"]),
                    "i": int(b["i"]),
                    "t": int(c.time.timestamp()),
                    "price": round(float(c.close), 2),
                    "stop": round(float(stop), 2),
                    "lo": round(lo, 2),
                    "hi": round(hi, 2),
                    "fvg": bool(b.get("fvg", False)),
                })
                break
    return entries


def sweep_info(m5, e, levels):
    """Liquiditaets-Sweep: vor/waehrend der Zonenbildung wurde ein frueheres Tief (Long) bzw. Hoch (Short)
    unterboten und der Kurs ist danach wieder zurueckgekehrt. Es zaehlen nur Niveaus, die es VOR dem Sweep gab."""
    k, i = e.get("k"), e.get("i")
    if k is None or i is None or k < 2 or i >= len(m5):
        return None
    is_long = e["kind"] == "long"
    win = m5.iloc[max(k - 1, 0):i + 1]
    close_i = float(m5.iloc[i].close)
    cands = []
    for n_bars, label in ((20, "Tief/Hoch der letzten 20 Kerzen"), (48, "Tief/Hoch der letzten 4 Std.")):
        prior = m5.iloc[max(0, k - 1 - n_bars):max(k - 1, 0)]
        if len(prior) < 10:
            continue
        if is_long:
            lvl = float(prior.low.min())
            if float(win.low.min()) < lvl < close_i:
                cands.append((lvl, label.replace("Tief/Hoch", "Tief")))
        else:
            lvl = float(prior.high.max())
            if float(win.high.max()) > lvl > close_i:
                cands.append((lvl, label.replace("Tief/Hoch", "Hoch")))
    for l in levels:
        if l["n"] == ("PDL" if is_long else "PDH"):
            if is_long and float(win.low.min()) < l["p"] < close_i:
                cands.append((l["p"], "Vortagstief"))
            if (not is_long) and float(win.high.max()) > l["p"] > close_i:
                cands.append((l["p"], "Vortageshoch"))
    if not cands:
        return None
    best = max(cands) if is_long else min(cands)
    return best[1]


def quality(e, trend, h1_blocks, m5=None, levels=None):
    """Vier Pruefpunkte wie bei TraderJe: Fair Value Gap, H1-Trend, Zone in H1-Zone, Liquiditaets-Sweep."""
    fvg = bool(e.get("fvg"))
    fit = e["kind"] == trend
    h1z = any(
        b["kind"] == e["kind"] and b["status"] != "ungueltig"
        and b["lo"] <= e["hi"] and b["hi"] >= e["lo"]
        for b in h1_blocks
    )
    sw = sweep_info(m5, e, levels or []) if m5 is not None else None
    return {"f": fvg, "t": fit, "z": h1z, "s": sw, "v": 2,
            "n": int(fvg) + int(fit) + int(h1z) + int(sw is not None)}


def find_h1_entries(m5, h1, h1_blocks):
    """Test: Kurs tippt erstmals eine H1-Zone an, Einstieg nach Bestaetigung auf M5 (Schluss ueber/unter das Hoch/Tief der Beruehrungskerze)."""
    out = []
    if len(m5) < 30 or len(h1) < 5:
        return out
    m5_start = m5.time.iloc[0]
    for b in h1_blocks:
        i = b["i"]
        if b["status"] == "ungueltig" or i + 1 >= len(h1):
            continue
        is_long = b["kind"] == "long"
        lo, hi = b["lo"], b["hi"]
        avail = h1.iloc[i + 1].time + pd.Timedelta(hours=1)
        # schon vor dem M5-Fenster angetestet -> kein Ersttest mehr
        pre = h1[(h1.time > h1.iloc[i + 1].time) & (h1.time + pd.Timedelta(hours=1) <= m5_start)]
        if ((pre.low <= hi) if is_long else (pre.high >= lo)).any():
            continue
        seg = m5[m5.time >= max(avail, m5_start)].reset_index(drop=True)
        touch = None
        for j in range(len(seg)):
            c = seg.iloc[j]
            inv = (c.close < lo) if is_long else (c.close > hi)
            tch = (c.low <= hi) if is_long else (c.high >= lo)
            if inv:
                break
            if tch:
                touch = j
                break
        if touch is None:
            continue
        ref = seg.iloc[touch].high if is_long else seg.iloc[touch].low
        for k in range(touch + 1, min(touch + 1 + H1_CHOCH_BARS, len(seg))):
            c = seg.iloc[k]
            broke = (c.close < lo) if is_long else (c.close > hi)
            if broke:
                break
            if (c.close > ref) if is_long else (c.close < ref):
                price = float(c.close)
                stop = lo - STOP_PUFFER if is_long else hi + STOP_PUFFER
                risk = abs(price - stop)
                if risk < 1.0:
                    break
                ct = int((c.time + pd.Timedelta(minutes=5)).timestamp())
                out.append({
                    "id": f"h1-{b['kind']}-{b['t']}", "kind": b["kind"], "ct": ct,
                    "price": round(price, 2), "stop": round(stop, 2), "risk": round(risk, 2),
                    "t1": round(price + risk if is_long else price - risk, 2),
                    "t2": round(price + 2 * risk if is_long else price - 2 * risk, 2),
                    "lo": round(lo, 2), "hi": round(hi, 2),
                })
                break
    return out


def load_history(key="history"):
    try:
        with open("docs/data.json") as f:
            return json.load(f).get(key, [])
    except Exception:
        return []


def breakout_signals(m15, h1):
    """Test-Strategie: Ausbruch aus 20 M15-Kerzen in Richtung H1-Trend, Stop 1,5 ATR, Ziel 1:1."""
    d = m15.copy()
    prev = d.close.shift(1)
    tr = pd.concat([d.high - d.low, (d.high - prev).abs(), (d.low - prev).abs()], axis=1).max(axis=1)
    d["atr"] = tr.rolling(14).mean()
    d["hh"] = d.high.shift(1).rolling(BRK_LOOK).max()
    d["ll"] = d.low.shift(1).rolling(BRK_LOOK).min()
    d["ct"] = d.time + pd.Timedelta(minutes=15)

    t = h1.copy()
    e20 = t.close.ewm(span=20, adjust=False).mean()
    e50 = t.close.ewm(span=50, adjust=False).mean()
    t["trend"] = "neutral"
    t.loc[(e20 > e50) & (t.close > e50), "trend"] = "long"
    t.loc[(e20 < e50) & (t.close < e50), "trend"] = "short"
    t["avail"] = t.time + pd.Timedelta(hours=1)
    d = pd.merge_asof(d.sort_values("ct"), t[["avail", "trend"]],
                      left_on="ct", right_on="avail", direction="backward")

    out = []
    lastrow = d.iloc[-1]
    cut24 = int(lastrow.ct.timestamp()) - 86400
    stat = {"close": round(float(lastrow.close), 2),
            "hh": None if pd.isna(lastrow.hh) else round(float(lastrow.hh), 2),
            "ll": None if pd.isna(lastrow.ll) else round(float(lastrow.ll), 2),
            "trend": None if pd.isna(lastrow.trend) else lastrow.trend,
            "raw24": 0, "ok24": 0}
    last = {"long": -999, "short": -999}
    for i in range(BRK_LOOK + 15, len(d)):
        r = d.iloc[i]
        if pd.isna(r.atr) or pd.isna(r.hh) or pd.isna(r.trend):
            continue
        kind = None
        if r.close > r.hh:
            kind = "long"
        elif r.close < r.ll:
            kind = "short"
        if kind is None or i - last[kind] < BRK_COOLDOWN:
            continue
        last[kind] = i
        if int(r.ct.timestamp()) >= cut24:
            stat["raw24"] += 1
        if r.trend != kind:
            continue
        if int(r.ct.timestamp()) >= cut24:
            stat["ok24"] += 1
        price = float(r.close)
        risk = BRK_ATR * float(r.atr)
        if risk < 1.0:
            continue
        stop = price - risk if kind == "long" else price + risk
        target = price + risk if kind == "long" else price - risk
        out.append({
            "kind": kind, "ct": int(r.ct.timestamp()),
            "price": round(price, 2), "stop": round(stop, 2),
            "target": round(target, 2), "risk": round(risk, 2),
            "level": round(float(r.hh if kind == "long" else r.ll), 2),
            "atr": round(float(r.atr), 3),
        })
    return out, stat


def eval_retest(sig, m5):
    """Ausbruch + Retest: Limit am durchbrochenen Niveau, Stop 1 ATR dahinter, Ziel 1:2."""
    is_long = sig["kind"] == "long"
    level, atr = sig["level"], sig["atr"]
    stop = level - atr if is_long else level + atr
    target = level + 2 * atr if is_long else level - 2 * atr
    t0 = pd.Timestamp(sig["ct"], unit="s", tz="UTC")
    seg = m5[m5.time >= t0].reset_index(drop=True)
    if seg.empty:
        return {"st": "wartet", "stop": round(stop, 2), "target": round(target, 2)}
    fill = None
    for j in range(min(RT_BARS, len(seg))):
        r = seg.iloc[j]
        touch = (r.low <= level) if is_long else (r.high >= level)
        failed = (r.close < level - RT_FAIL * atr) if is_long else (r.close > level + RT_FAIL * atr)
        if touch:
            fill = j
            break
        if failed:
            return {"st": "verfallen", "stop": round(stop, 2), "target": round(target, 2)}
    if fill is None:
        st = "verfallen" if len(seg) >= RT_BARS else "wartet"
        return {"st": st, "stop": round(stop, 2), "target": round(target, 2)}
    ft = int(seg.iloc[fill].time.timestamp())
    res, rt = "offen", None
    for k in range(fill, len(seg)):
        r = seg.iloc[k]
        stop_hit = (r.low <= stop) if is_long else (r.high >= stop)
        if stop_hit:
            res, rt = "Stop", int(r.time.timestamp())
            break
        if k == fill:
            continue
        tgt_hit = (r.high >= target) if is_long else (r.low <= target)
        if tgt_hit:
            res, rt = "Ziel", int(r.time.timestamp())
            break
    return {"st": "ausgelöst", "ft": ft, "res": res, "rt": rt,
            "stop": round(stop, 2), "target": round(target, 2)}


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


# Name, mögliche Epics bei capital.com (das erste, das existiert, wird genommen)
MARKETS = [
    ("Silber", ["SILVER"]),
    ("S&P 500", ["US500"]),
    ("Nasdaq 100", ["US100"]),
    ("EUR/USD", ["EURUSD"]),
    ("Öl (WTI)", ["OIL_CRUDE", "CRUDEOIL", "USOIL"]),
]
TFS = [("M5", "MINUTE_5", 5), ("H1", "HOUR", 60)]
STATE = "sent_multi.json"


def pick_epic(h, cands):
    for ep in cands:
        try:
            r = requests.get(f"{BASE}/markets/{ep}", headers=h, timeout=15)
            if r.status_code == 200:
                return ep
        except Exception:
            pass
    return None


def main():
    global STATE_FILE, EPIC
    STATE_FILE = STATE
    state = load_state()
    now = datetime.now(timezone.utc)
    cutoff = now - timedelta(minutes=WINDOW_MIN)
    if now.weekday() >= 5:
        return
    h = login()
    for name, cands in MARKETS:
        try:
            ep = pick_epic(h, cands)
            if ep is None:
                print(name, "kein Epic gefunden", cands)
                continue
            EPIC = ep
            for tf, res, mins in TFS:
                df = get_candles(h, res).iloc[:-1].reset_index(drop=True)
                if len(df) < 60:
                    continue
                raw = find_blocks(df)
                for b in raw:
                    if b["status"] == "ungueltig":
                        continue
                    close_t = df.iloc[b["i"]].time + timedelta(minutes=mins)
                    if close_t < cutoff:
                        continue
                    key = f"{ep}-{tf}-{b['kind']}-{b['t']}"
                    if key in state:
                        continue
                    dec = 5 if b["hi"] < 10 else 2
                    long_ = b["kind"] == "long"
                    icon = "🟢" if long_ else "🔴"
                    tg(f"{icon} {name}: Neuer {'Long' if long_ else 'Short'}-Orderblock ({tf})\n"
                       f"Zone: {b['lo']:.{dec}f} - {b['hi']:.{dec}f}")
                    state[key] = now.timestamp()
                print(name, ep, tf, "ok", len(raw), "Blöcke")
        except Exception as e:
            print(name, "Fehler:", e)
    save_state(state)


if __name__ == "__main__":
    main()
