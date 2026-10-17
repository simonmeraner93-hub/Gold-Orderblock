"""Backtest deiner Hauptstrategie.

Regeln wie im Bot:
 - Orderblock (M5 oder H1): Impulskerze >= 1.5 x Durchschnittskoerper, Block = letzte Gegenkerze davor
 - Block muss direkt vor dem Impuls liegen (angrenzend)
 - Fair Value Gap zwingend (mindestens 0.25 x Durchschnittskoerper gross)
 - nur in Richtung des H1-Trends (EMA20/EMA50), bei neutralem Trend beide Richtungen

Zum Vergleich laufen vier Varianten:
 alle    = jeder Orderblock
 trend   = nur Orderblock im Trend
 fvg     = nur Orderblock mit FVG (angrenzend)
 strategie = Trend + FVG (deine Strategie)

Einstieg: Limit-Order an der Kante oder in der Mitte der Zone, erst nachdem das FVG
bekannt ist. Stop knapp hinter der Zone. Ziel 1R, 2R oder 3R. Spread wird abgezogen.
Wenn Einstieg und Stop in derselben Kerze liegen, zaehlt es als Verlust (vorsichtig).
"""
import json
import math
import os
import statistics
import time
import traceback
from datetime import datetime, timedelta, timezone

import numpy as np
import pandas as pd
import requests

BASE = "https://demo-api-capital.backend-capital.com/api/v1"
STRONG = 1.5
SWING = 10
FVG_MIN = 0.25
RRS = [1.0, 2.0, 3.0]
ENTRIES = ["kante", "mitte"]
VARIANTS = ["alle", "trend", "fvg", "strategie"]
MAX_HOLD = {"M5": 288, "H1": 72}
WAIT = {"M5": 288, "H1": 48}
MIN_RISK_SPREADS = 3.0
WARMUP_H1 = 60

MARKETS = [
    ("Gold", ["GOLD"]),
    ("Nasdaq 100", ["US100"]),
    ("EUR/USD", ["EURUSD"]),
    ("GBP/USD", ["GBPUSD"]),
    ("DAX", ["DE40", "GER40", "DAX40"]),
]


# ---------------------------------------------------------------- Daten

def fetch(h, epic, res, days, chunk_days, per_call):
    end = datetime.now(timezone.utc).replace(second=0, microsecond=0)
    cur = (end - timedelta(days=days)).replace(hour=0, minute=0)
    rows = {}
    while cur < end:
        nxt = min(cur + timedelta(days=chunk_days), end)
        params = {
            "resolution": res,
            "from": cur.strftime("%Y-%m-%dT%H:%M:%S"),
            "to": nxt.strftime("%Y-%m-%dT%H:%M:%S"),
            "max": per_call,
        }
        for attempt in range(4):
            r = requests.get(f"{BASE}/prices/{epic}", headers=h, params=params, timeout=30)
            if r.status_code == 429:
                time.sleep(3 * (attempt + 1))
                continue
            break
        r.raise_for_status()
        for p in r.json().get("prices", []):
            t = int(pd.Timestamp(p["snapshotTimeUTC"], tz="UTC").timestamp())
            mid = lambda k: (p[k]["bid"] + p[k]["ask"]) / 2
            rows[t] = (mid("openPrice"), mid("highPrice"), mid("lowPrice"), mid("closePrice"))
        cur = nxt
        time.sleep(0.4)
    if len(rows) < 300:
        raise RuntimeError(f"zu wenige Kerzen erhalten: {len(rows)}")
    ts = sorted(rows)
    arr = np.array([rows[t] for t in ts], dtype=float)
    # letzte (evtl. noch laufende) Kerze weglassen
    return {"t": np.array(ts[:-1], dtype=np.int64), "o": arr[:-1, 0], "h": arr[:-1, 1],
            "l": arr[:-1, 2], "c": arr[:-1, 3]}


def measure_spread(h, epic):
    r = requests.get(f"{BASE}/prices/{epic}", headers=h,
                     params={"resolution": "MINUTE_5", "max": 500}, timeout=30)
    r.raise_for_status()
    sp = []
    for p in r.json().get("prices", []):
        c = p["closePrice"]
        if c.get("ask") is not None and c.get("bid") is not None:
            sp.append(c["ask"] - c["bid"])
    return statistics.median(sp) if sp else None


# ---------------------------------------------------------------- Logik

def h1_trend(h1):
    c = pd.Series(h1["c"])
    e20 = c.ewm(span=20, adjust=False).mean().values
    e50 = c.ewm(span=50, adjust=False).mean().values
    last = h1["c"]
    tr = np.zeros(len(c), dtype=int)           # 1 long, -1 short, 0 neutral
    tr[(e20 > e50) & (last > e50)] = 1
    tr[(e20 < e50) & (last < e50)] = -1
    return tr


def find_blocks(d):
    """Alle Orderbloecke mit FVG-Infos. d = Dict mit t,o,h,l,c."""
    o, hi, lo, c = d["o"], d["h"], d["l"], d["c"]
    n = len(c)
    body = np.abs(c - o)
    found = {}
    for i in range(SWING + 20, n - 1):
        avg = body[i - 20:i].mean()
        if body[i] < STRONG * avg:
            continue
        kind = None
        if c[i] > o[i] and c[i] > hi[i - SWING:i].max():
            kind = 1
        elif c[i] < o[i] and c[i] < lo[i - SWING:i].min():
            kind = -1
        if kind is None:
            continue
        for k in range(i - 1, max(i - 6, 0), -1):
            if (kind == 1 and c[k] < o[k]) or (kind == -1 and c[k] > o[k]):
                if k not in found:
                    found[k] = (kind, i)
                break
    out = []
    for k, (kind, i) in found.items():
        avg = body[max(0, i - 20):i].mean()
        if kind == 1:
            size = lo[i + 1] - hi[i - 1]
        else:
            size = lo[i - 1] - hi[i + 1]
        fvg = bool(size > 0 and size >= FVG_MIN * avg)
        out.append({"k": k, "i": i, "kind": kind, "adj": k == i - 1, "fvg": fvg})
    return out


def simulate(d, b, entry_mode, tf, spread):
    """Gibt (R_vor_Kosten, Risiko, Einstiegszeit) oder None zurueck."""
    o, hi, lo, c = d["o"], d["h"], d["l"], d["c"]
    n = len(c)
    k, i, kind = b["k"], b["i"], b["kind"]
    zlo, zhi = lo[k], hi[k]
    if zhi - zlo <= 0:
        return None
    if entry_mode == "kante":
        price = zhi if kind == 1 else zlo
    else:
        price = (zlo + zhi) / 2
    stop = zlo - spread if kind == 1 else zhi + spread
    risk = abs(price - stop)
    if risk < MIN_RISK_SPREADS * spread:
        return None
    fill = None
    for j in range(i + 2, min(i + 2 + WAIT[tf], n)):
        touched = (lo[j] <= price) if kind == 1 else (hi[j] >= price)
        if touched:
            fill = j
            break
        inv = (c[j] < zlo) if kind == 1 else (c[j] > zhi)
        if inv:
            return None
    if fill is None:
        return None
    # Einstiegskerze: Stop in derselben Kerze zaehlt als Verlust
    if (kind == 1 and lo[fill] <= stop) or (kind == -1 and hi[fill] >= stop):
        return {"r": {rr: -1.0 for rr in RRS}, "risk": risk, "t": int(d["t"][fill])}
    res = {}
    for rr in RRS:
        target = price + rr * risk if kind == 1 else price - rr * risk
        val = None
        end = min(fill + 1 + MAX_HOLD[tf], n)
        for j in range(fill + 1, end):
            stop_hit = (lo[j] <= stop) if kind == 1 else (hi[j] >= stop)
            if stop_hit:
                val = -1.0
                break
            tgt_hit = (hi[j] >= target) if kind == 1 else (lo[j] <= target)
            if tgt_hit:
                val = rr
                break
        if val is None:
            last = c[end - 1]
            val = ((last - price) if kind == 1 else (price - last)) / risk
        res[rr] = val
    return {"r": res, "risk": risk, "t": int(d["t"][fill])}


def run_market(m5, h1, spread):
    """Liefert Liste von Trades: dict(tf, variant, entry, rr, r_net, t)."""
    tr = h1_trend(h1)
    avail = h1["t"] + 3600
    start_ok = avail[min(WARMUP_H1, len(avail) - 1)]
    trades = []
    for tf, d in (("M5", m5), ("H1", h1)):
        step = 300 if tf == "M5" else 3600
        for b in find_blocks(d):
            sig_t = int(d["t"][b["i"] + 1]) + step
            if sig_t < start_ok:
                continue
            idx = np.searchsorted(avail, sig_t, side="right") - 1
            if idx < 0:
                continue
            trend = tr[idx]
            fit = (trend == 0) or (trend == b["kind"])
            ok_fvg = b["adj"] and b["fvg"]
            variants = ["alle"]
            if fit:
                variants.append("trend")
            if ok_fvg:
                variants.append("fvg")
            if fit and ok_fvg:
                variants.append("strategie")
            for em in ENTRIES:
                sim = simulate(d, b, em, tf, spread)
                if sim is None:
                    continue
                cost = spread / sim["risk"]
                for rr in RRS:
                    rn = sim["r"][rr] - cost
                    for v in variants:
                        trades.append({"tf": tf, "variant": v, "entry": em, "rr": rr,
                                       "r": float(rn), "t": sim["t"]})
    return trades


def stats(rows):
    """rows = Liste (t, r)."""
    if not rows:
        return {"n": 0}
    rows = sorted(rows)
    r = np.array([x[1] for x in rows])
    n = len(r)
    avg = float(r.mean())
    sd = float(r.std(ddof=1)) if n > 1 else 0.0
    t = avg / (sd / math.sqrt(n)) if sd > 0 else 0.0
    half = n // 2
    return {
        "n": n,
        "win": round(float((r > 0).mean()) * 100, 1),
        "avg": round(avg, 3),
        "t": round(t, 2),
        "h1": round(float(r[:half].mean()), 3) if half else None,
        "h2": round(float(r[half:].mean()), 3) if n - half else None,
    }


def summarize(trades):
    res = {}
    groups = {}
    for tr in trades:
        key = (tr["tf"], tr["variant"], tr["entry"], tr["rr"])
        groups.setdefault(key, []).append((tr["t"], tr["r"]))
    for (tf, v, em, rr), rows in groups.items():
        res.setdefault(tf, {}).setdefault(v, {}).setdefault(em, {})[str(rr)] = stats(rows)
    return res


# ---------------------------------------------------------------- Lauf

def write(res):
    os.makedirs("docs", exist_ok=True)
    with open("docs/backtest_strategie.json", "w") as f:
        json.dump(res, f)


def main():
    r = requests.post(
        f"{BASE}/session",
        headers={"X-CAP-API-KEY": os.environ["CAPITAL_API_KEY"]},
        json={"identifier": os.environ["CAPITAL_IDENTIFIER"],
              "password": os.environ["CAPITAL_PASSWORD"]},
        timeout=20,
    )
    r.raise_for_status()
    h = {"CST": r.headers["CST"], "X-SECURITY-TOKEN": r.headers["X-SECURITY-TOKEN"]}

    res = {"updated": datetime.now(timezone.utc).isoformat(timespec="minutes"), "markets": []}
    write(res)
    pooled = []
    for name, cands in MARKETS:
        entry = {"name": name}
        res["markets"].append(entry)
        try:
            epic = None
            for ep in cands:
                rr_ = requests.get(f"{BASE}/markets/{ep}", headers=h, timeout=20)
                if rr_.status_code == 200:
                    epic = ep
                    break
            if epic is None:
                entry["error"] = "Markt nicht gefunden"
                write(res)
                continue
            m5 = None
            for days in (180, 120, 70, 40):
                try:
                    m5 = fetch(h, epic, "MINUTE_5", days, 3, 1000)
                    entry["m5_days"] = days
                    break
                except Exception as e:
                    print(name, "M5", days, "Tage fehlgeschlagen:", e)
            h1 = None
            for days in (540, 365, 180, 90):
                try:
                    h1 = fetch(h, epic, "HOUR", days, 30, 1000)
                    entry["h1_days"] = days
                    break
                except Exception as e:
                    print(name, "H1", days, "Tage fehlgeschlagen:", e)
            if m5 is None or h1 is None:
                entry["error"] = "Kerzen konnten nicht geladen werden"
                write(res)
                continue
            spread = measure_spread(h, epic)
            price = float(m5["c"][-1])
            if not spread or spread <= 0:
                spread = 0.000144 * price
                entry["spread_note"] = "geschaetzt"
            entry.update({"epic": epic, "spread": spread, "spread_pct": round(100 * spread / price, 4),
                          "m5_candles": len(m5["c"]), "h1_candles": len(h1["c"])})
            print(name, epic, "M5", len(m5["c"]), "H1", len(h1["c"]), "Spanne", spread)
            trades = run_market(m5, h1, spread)
            entry["stats"] = summarize(trades)
            pooled.extend(trades)
            write(res)
        except Exception as e:
            traceback.print_exc()
            entry["error"] = str(e)
            write(res)
    res["pooled"] = summarize(pooled)
    write(res)
    # kurze Zusammenfassung ins Log
    for tf in ("M5", "H1"):
        for em in ENTRIES:
            for rr in RRS:
                s = res["pooled"].get(tf, {}).get("strategie", {}).get(em, {}).get(str(rr))
                print(tf, em, f"{rr}R", "Strategie:", s)
    print("fertig")


if __name__ == "__main__":
    main()
