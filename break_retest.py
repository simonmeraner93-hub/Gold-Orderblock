"""Break and Retest - Backtest mit Konfigurationssuche.

Getrennt vom Gold-Orderblock-Bot: importiert nichts aus signal_check.py oder den anderen
backtest-Dateien. Schreibt nur docs/break_retest.json (bzw. Teilergebnisse fuer den Merge).

REGELN
  Trend     Trend-Zeitrahmen T: Schlusskurs ueber (Long) bzw. unter (Short) der EMA50, und die
            EMA50 steigt bzw. faellt gegenueber vor 3 Kerzen. Nur abgeschlossene T-Kerzen.
            Kein Trend -> kein Trade.
  Level     Pivot-Hoch bzw. -Tief auf dem Einstiegs-Zeitrahmen E: hoechstes Hoch bzw. tiefstes Tief
            in P Kerzen links und rechts. Ein Pivot gilt ab seiner Bestaetigung (P Kerzen spaeter).
  Break     Schlusskurs kreuzt das letzte Pivot-Hoch (Long) bzw. -Tief (Short) um mehr als
            0,1 ATR, nur in Trendrichtung. Jedes Level wird hoechstens einmal gebrochen.
  Retest    Innerhalb von W Kerzen nach dem Break beruehrt der Tiefstkurs (Short: Hoechstkurs)
            die Zone Level +/- Z ATR.
  Einstieg  Bestaetigungskerze ab der Beruehrung: Long = bullisch (Schluss > Eroeffnung) und
            Schluss mehr als 0,1 ATR ueber dem Level, Short spiegelbildlich. Einstieg zum
            Schlusskurs dieser Kerze.
  Ungueltig Schluss mehr als 0,1 ATR zurueck durch das Level vor der Bestaetigung, Trend kippt,
            oder keine Bestaetigung innerhalb von W Kerzen.
  Stop      Extremkurs von der ersten Beruehrung bis zur Bestaetigung, plus 0,1 ATR.
            Risiko > 3 ATR oder < 2 Spreads -> kein Trade.
  Ziel      RR x Risiko. Haltezeit H Kerzen, danach Ausstieg zum Schlusskurs.
            Stop und Ziel in derselben Kerze -> zaehlt als Stop (konservativ).
  Kosten    Gemessener Spread (Median) wird pro Trade in R abgezogen. Die Daten sind Mittelkurse.
  Eins      Nur ein offener Trade je Markt und Konfiguration.

KONFIGURATIONEN (je Markt 36)
  Zeitrahmen-Paar (E, T) x RR (1, 1,5, 2) x Zone Z (0,2 oder 0,4 ATR) x Pivot-Breite (normal oder doppelt).

AUSWAHL UND TEST
  Je Markt wird die Konfiguration mit dem hoechsten Ø netto R im Vor-Test gewaehlt (erste 2/3 der
  Historie, mindestens 50 Trades). Bewertet wird nur die letzte 1/3 (Out-of-Sample), die bei der
  Auswahl nicht benutzt wurde. Zufalls-Vergleich nur fuer die gewaehlte Konfiguration: gleiche
  Stop- und Zielabstaende an zufaelligen Kerzen im Trend.

Aufruf:
  python break_retest.py                          # alles in einem Lauf nach docs/break_retest.json
  python break_retest.py --shard 0 --of 3 --out X # ein Drittel der Maerkte nach X
  python break_retest.py --merge DIR --out Y      # Teilergebnisse in DIR zusammenfuehren
"""
import argparse
import json
import os
import statistics
import time
from datetime import datetime, timedelta, timezone

import numpy as np
import pandas as pd
import requests

BASE = "https://demo-api-capital.backend-capital.com/api/v1"
OUT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "docs", "break_retest.json")

# (Name, Klasse, Kandidaten). Kandidaten sind Epics; bei Aktien steht dort der Suchbegriff.
UNIVERSE = [
    ("Gold", "Rohstoff", ["GOLD"]),
    ("Silber", "Rohstoff", ["SILVER"]),
    ("Öl (WTI)", "Rohstoff", ["OIL_CRUDE"]),
    ("Nasdaq 100", "Index", ["US100"]),
    ("S&P 500", "Index", ["US500"]),
    ("Dow Jones", "Index", ["US30"]),
    ("DAX", "Index", ["DE40", "GER40", "DAX40"]),
    ("FTSE 100", "Index", ["UK100"]),
    ("CAC 40", "Index", ["FR40", "FRA40"]),
    ("EUR/USD", "Forex", ["EURUSD"]),
    ("GBP/USD", "Forex", ["GBPUSD"]),
    ("USD/JPY", "Forex", ["USDJPY"]),
    ("AUD/USD", "Forex", ["AUDUSD"]),
    ("USD/CHF", "Forex", ["USDCHF"]),
    ("USD/CAD", "Forex", ["USDCAD"]),
    ("NZD/USD", "Forex", ["NZDUSD"]),
    ("EUR/JPY", "Forex", ["EURJPY"]),
    ("Apple", "Aktie", ["Apple"]),
    ("Microsoft", "Aktie", ["Microsoft"]),
    ("Nvidia", "Aktie", ["Nvidia"]),
    ("Tesla", "Aktie", ["Tesla"]),
    ("Amazon", "Aktie", ["Amazon"]),
    ("Alphabet", "Aktie", ["Alphabet"]),
    ("Meta", "Aktie", ["Meta Platforms"]),
    ("Netflix", "Aktie", ["Netflix"]),
    ("AMD", "Aktie", ["AMD"]),
    ("Coca-Cola", "Aktie", ["Coca-Cola"]),
    ("Disney", "Aktie", ["Disney"]),
    ("Intel", "Aktie", ["Intel"]),
    ("Nike", "Aktie", ["Nike"]),
]

TF_MIN = {"M1": 1, "M5": 5, "M15": 15, "H1": 60}
RES = {"M1": "MINUTE", "M5": "MINUTE_5", "M15": "MINUTE_15", "H1": "HOUR"}
# Tage zurueck, in dieser Reihenfolge versucht (kuerzer, falls die API weniger Historie liefert)
DAYS = {"M1": (120, 60, 30, 14, 7, 3), "M5": (180, 120, 60), "M15": (365, 180, 90), "H1": (540, 365, 180)}

# P = Pivot-Breite, W = Fenster fuer Retest und Bestaetigung, H = Haltezeit (Kerzen auf E)
PARAMS = {
    "M1": {"P": 5, "W": 60, "H": 240},
    "M5": {"P": 4, "W": 24, "H": 96},
    "M15": {"P": 3, "W": 16, "H": 96},
}
PAIRS = [("M1", "M15"), ("M5", "M15"), ("M15", "H1")]
RRS = [1.0, 1.5, 2.0]
ZONES = [0.2, 0.4]
PMULS = [1, 2]
CONFIGS = []
for _e, _t in PAIRS:
    for _rr in RRS:
        for _z in ZONES:
            for _pm in PMULS:
                CONFIGS.append({
                    "key": f"{_e}_{_t}_RR{_rr:g}_Z{_z:g}_P{_pm}x",
                    "E": _e, "T": _t, "rr": _rr, "zone": _z, "pmul": _pm,
                    "label": f"Einstieg {_e}, Trend {_t}, {_rr:g} R, Zone {_z:g} ATR, Pivot {'doppelt' if _pm == 2 else 'normal'}",
                })

ATR_N = 14
EMA_N = 50
SLOPE_LAG = 3
BUF = 0.1
MAX_RISK = 3.0
OOS_FRAC = 2 / 3
BASE_SEEDS = 10
MIN_IS = 50
MIN_OOS = 30


# ---------------------------------------------------------------- Daten

def login():
    r = requests.post(
        f"{BASE}/session",
        headers={"X-CAP-API-KEY": os.environ["CAPITAL_API_KEY"]},
        json={"identifier": os.environ["CAPITAL_IDENTIFIER"],
              "password": os.environ["CAPITAL_PASSWORD"]},
        timeout=20,
    )
    r.raise_for_status()
    return {"CST": r.headers["CST"], "X-SECURITY-TOKEN": r.headers["X-SECURITY-TOKEN"]}


def resolve(h, klasse, cands):
    """Epic und Instrumentname finden. Aktien ueber die Suche (nur Typ SHARES)."""
    if klasse != "Aktie":
        for ep in cands:
            r = requests.get(f"{BASE}/markets/{ep}", headers=h, timeout=20)
            if r.status_code == 200:
                return ep, ep
        return None, None
    term = cands[0]
    r = requests.get(f"{BASE}/markets", headers=h, params={"searchTerm": term}, timeout=20)
    r.raise_for_status()
    key = term.split()[0].lower()
    for m in r.json().get("markets", []):
        if m.get("instrumentType") == "SHARES" and key in m.get("instrumentName", "").lower():
            return m["epic"], m.get("instrumentName")
    return None, None


def fetch(h, epic, tf, days, per_call=1000):
    """Mittelkurse (Bid/Ask) fuer einen Zeitrahmen, in Abschnitten, damit jede Anfrage unter dem Limit bleibt."""
    tfm = TF_MIN[tf]
    chunk = 900 * tfm / 1440.0            # Tage pro Anfrage: ca. 900 Kerzen
    end = datetime.now(timezone.utc).replace(second=0, microsecond=0)
    cur = (end - timedelta(days=days)).replace(hour=0, minute=0)
    rows = {}
    while cur < end:
        nxt = min(cur + timedelta(days=chunk), end)
        params = {"resolution": RES[tf], "from": cur.strftime("%Y-%m-%dT%H:%M:%S"),
                  "to": nxt.strftime("%Y-%m-%dT%H:%M:%S"), "max": per_call}
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
        raise RuntimeError(f"zu wenige Kerzen {tf}: {len(rows)}")
    ts = sorted(rows)
    arr = np.array([rows[t] for t in ts], dtype=float)
    # letzte, evtl. noch laufende Kerze weglassen
    return {"t": np.array(ts[:-1], dtype=np.int64), "o": arr[:-1, 0], "h": arr[:-1, 1],
            "l": arr[:-1, 2], "c": arr[:-1, 3], "tf": tfm}


def measure_spread(h, epic):
    r = requests.get(f"{BASE}/prices/{epic}", headers=h,
                     params={"resolution": "MINUTE_5", "max": 500}, timeout=30)
    r.raise_for_status()
    sp = []
    for p in r.json().get("prices", []):
        c = p.get("closePrice", {})
        if c.get("ask") is not None and c.get("bid") is not None:
            sp.append(c["ask"] - c["bid"])
    return float(statistics.median(sp)) if sp else None


# ---------------------------------------------------------------- Indikatoren

def trend_on(E, T):
    """Trend je Kerze von E, aus der letzten abgeschlossenen Kerze von T (+1 auf, -1 ab, 0 kein)."""
    c = T["c"]
    e = pd.Series(c).ewm(span=EMA_N, adjust=False).mean().to_numpy()
    prev = np.full_like(e, np.nan)
    prev[SLOPE_LAG:] = e[:-SLOPE_LAG]
    tr = np.where((c > e) & (e > prev), 1, np.where((c < e) & (e < prev), -1, 0))
    t_close = T["t"] + T["tf"] * 60
    e_close = E["t"] + E["tf"] * 60
    idx = np.searchsorted(t_close, e_close, side="right") - 1
    return np.where(idx >= 0, tr[np.clip(idx, 0, None)], 0)


def atr(h, l, c):
    pc = np.concatenate([[c[0]], c[:-1]])
    tr = np.maximum(h - l, np.maximum(np.abs(h - pc), np.abs(l - pc)))
    return pd.Series(tr).rolling(ATR_N).mean().to_numpy()


def pivots(h, l, P):
    w = 2 * P + 1
    ph = h == pd.Series(h).rolling(w, center=True).max().to_numpy()
    pl = l == pd.Series(l).rolling(w, center=True).min().to_numpy()
    return ph, pl


def levels(ph, pl, h, l, P):
    """Letztes bestaetigtes Pivot-Hoch/-Tief je Kerze. Ein Pivot bei i wird erst bei i+P bekannt."""
    n = len(h)
    lh = np.full(n, np.nan)
    ll = np.full(n, np.nan)
    cur_h = cur_l = np.nan
    for j in range(n):
        i = j - P
        if i >= 0:
            if ph[i]:
                cur_h = h[i]
            if pl[i]:
                cur_l = l[i]
        lh[j] = cur_h
        ll[j] = cur_l
    return lh, ll


# ---------------------------------------------------------------- Simulation

def outcome(h, l, c, j, d, entry, stop, tgt, rr, H):
    """Verlauf eines Trades ab Kerze j (Einstieg zum Schluss von j). Gibt (R brutto, Ausstiegskerze, Grund)."""
    n = len(c)
    end = min(j + H, n - 1)
    if end <= j:
        return None
    risk = abs(entry - stop)
    if d > 0:
        s_hit = np.flatnonzero(l[j + 1:end + 1] <= stop)
        t_hit = np.flatnonzero(h[j + 1:end + 1] >= tgt)
    else:
        s_hit = np.flatnonzero(h[j + 1:end + 1] >= stop)
        t_hit = np.flatnonzero(l[j + 1:end + 1] <= tgt)
    s = s_hit[0] if s_hit.size else None
    t = t_hit[0] if t_hit.size else None
    if s is not None and (t is None or s <= t):
        return -1.0, j + 1 + s, "Stop"
    if t is not None:
        return rr, j + 1 + t, "Ziel"
    return d * (c[end] - entry) / risk, end, "Zeit"


def simulate(E, trend, rr, P, W, H, spread, zone, live=False):
    """Trades der Regel. live=True: eine Bestaetigung auf der letzten Kerze zaehlt auch ohne Ausgang
    (Ausgang 'offen'), damit die Live-Pruefung sie melden kann. Der Backtest nutzt live=False."""
    o, h, l, c, t = E["o"], E["h"], E["l"], E["c"], E["t"]
    n = len(c)
    a = atr(h, l, c)
    ph, pl = pivots(h, l, P)
    lh, ll = levels(ph, pl, h, l, P)
    trades = []
    used_h = used_l = np.nan
    setup = None
    busy = -1
    for j in range(1, n):
        aj = a[j]
        if np.isnan(aj) or j <= busy:
            continue
        tr = trend[j]

        # 1) Offenes Setup (Break in frueheren Kerzen) weiterverfolgen
        if setup is not None:
            d, L, bf = setup["d"], setup["L"], setup["buf"]
            if tr != d or j - setup["b"] > W or (d > 0 and c[j] < L - bf) or (d < 0 and c[j] > L + bf):
                setup = None
                continue
            if d > 0:
                touch = l[j] <= L + setup["zone"]
                if touch or setup["touched"]:
                    setup["touched"] = True
                    setup["ext"] = min(setup["ext"], l[j])
                confirm = c[j] > o[j] and c[j] > L + bf
            else:
                touch = h[j] >= L - setup["zone"]
                if touch or setup["touched"]:
                    setup["touched"] = True
                    setup["ext"] = max(setup["ext"], h[j])
                confirm = c[j] < o[j] and c[j] < L - bf
            if setup["touched"] and confirm:
                entry = c[j]
                stop = setup["ext"] - d * BUF * aj
                risk = abs(entry - stop)
                setup = None
                if risk <= 0 or risk > MAX_RISK * aj or risk < 2 * spread:
                    continue
                tgt = entry + d * rr * risk
                res = outcome(h, l, c, j, d, entry, stop, tgt, rr, H)
                if res is None:
                    if not live:
                        continue
                    res = (0.0, j, "offen")
                r_g, x, why = res
                trades.append({"j": j, "t": int(t[j]), "x": int(t[x]), "d": int(d), "L": float(L),
                               "entry": float(entry), "stop": float(stop), "tgt": float(tgt),
                               "rk": float(risk / aj), "R": float(r_g - spread / risk), "why": why})
                busy = x
            continue

        # 2) Neuer Break nur in Trendrichtung, jedes Level einmal
        if tr > 0 and not np.isnan(lh[j]) and lh[j] != used_h:
            L = lh[j]
            bf = BUF * aj
            if c[j - 1] <= L + bf < c[j]:
                setup = {"d": 1, "L": L, "buf": bf, "zone": zone * aj, "b": j,
                         "touched": False, "ext": np.inf}
                used_h = L
        elif tr < 0 and not np.isnan(ll[j]) and ll[j] != used_l:
            L = ll[j]
            bf = BUF * aj
            if c[j] < L - bf <= c[j - 1]:
                setup = {"d": -1, "L": L, "buf": bf, "zone": zone * aj, "b": j,
                         "touched": False, "ext": -np.inf}
                used_l = L
    return trades


def baseline(E, trend, rr, H, spread, k, n_pick):
    """Zufalls-Vergleich: gleiche Stop- und Zielabstaende an zufaelligen Kerzen im Trend. Netto R."""
    h, l, c = E["h"], E["l"], E["c"]
    a = atr(h, l, c)
    cand = np.flatnonzero((trend != 0) & ~np.isnan(a))
    cand = cand[cand > 0]
    if n_pick == 0 or cand.size == 0 or not k:
        return None
    rng = np.random.default_rng(7)
    means = []
    for _ in range(BASE_SEEDS):
        picks = rng.choice(cand, size=min(n_pick, cand.size), replace=False)
        rs = []
        for j in picks:
            d = int(trend[j])
            entry = c[j]
            risk = k * a[j]
            if risk < 2 * spread:
                continue
            stop = entry - d * risk
            tgt = entry + d * rr * risk
            res = outcome(h, l, c, j, d, entry, stop, tgt, rr, H)
            if res is not None:
                rs.append(res[0] - spread / risk)
        if rs:
            means.append(float(np.mean(rs)))
    return float(np.mean(means)) if means else None


def stats(rs):
    rs = np.asarray(rs, dtype=float)
    n = rs.size
    if n == 0:
        return {"n": 0}
    m = float(rs.mean())
    sd = float(rs.std(ddof=1)) if n > 1 else 0.0
    cum = np.cumsum(rs)
    peak = np.maximum.accumulate(np.maximum(cum, 0.0))
    return {"n": int(n),
            "win": round(100 * float((rs > 0).mean()), 1),
            "avg": round(m, 3),
            "sum": round(float(cum[-1]), 2),
            "t": round(m / (sd / np.sqrt(n)), 2) if sd > 0 else 0.0,
            "dd": round(float((peak - cum).max()), 2)}


def split_stats(E, trades):
    rs = np.array([x["R"] for x in trades], dtype=float)
    t_all = np.array([x["t"] for x in trades], dtype=float)
    cut = float(E["t"][0]) + OOS_FRAC * float(E["t"][-1] - E["t"][0])
    m = t_all < cut
    return {"all": stats(rs), "is": stats(rs[m]), "oos": stats(rs[~m])}


def choose(results):
    """Auswahl nur mit dem Vor-Test: hoechster Ø netto R, mindestens MIN_IS Trades."""
    best, best_avg = None, None
    for key, r in results.items():
        s = r["is"]
        if s["n"] >= MIN_IS and (best_avg is None or s["avg"] > best_avg):
            best, best_avg = key, s["avg"]
    return best


def verdict(s):
    if s["n"] < MIN_OOS:
        return "zu wenig Trades"
    return "bestätigt im Out-of-Sample" if s["avg"] > 0 else "nicht bestätigt"


def analyse_market(data, spread):
    trend_cache, trades_by, results = {}, {}, {}
    for cfg in CONFIGS:
        if cfg["E"] not in data or cfg["T"] not in data:
            # Zeitrahmen fehlt (z. B. M1 nicht verfuegbar): Konfiguration ohne Trades
            results[cfg["key"]] = {"all": stats([]), "is": stats([]), "oos": stats([])}
            continue
        E, T = data[cfg["E"]], data[cfg["T"]]
        tk = (cfg["E"], cfg["T"])
        if tk not in trend_cache:
            trend_cache[tk] = trend_on(E, T)
        p = PARAMS[cfg["E"]]
        trades = simulate(E, trend_cache[tk], cfg["rr"], p["P"] * cfg["pmul"], p["W"], p["H"],
                          spread, cfg["zone"])
        trades_by[cfg["key"]] = trades
        results[cfg["key"]] = split_stats(E, trades)

    chosen = choose(results)
    out = {"results": results, "chosen": chosen}
    if chosen is None:
        out["verdict"] = "keine Konfiguration mit genug Vor-Test-Trades"
        return out
    cfg = next(c for c in CONFIGS if c["key"] == chosen)
    trades = trades_by[chosen]
    E = data[cfg["E"]]
    trend = trend_cache[(cfg["E"], cfg["T"])]
    rs = np.array([x["R"] for x in trades], dtype=float)
    ks = [x["rk"] for x in trades]
    k = float(np.median(ks)) if ks else None
    p = PARAMS[cfg["E"]]
    base = baseline(E, trend, cfg["rr"], p["H"], spread, k, len(trades))
    cum = np.cumsum(rs)
    out["verdict"] = verdict(results[chosen]["oos"])
    out["detail"] = {
        "baseline": round(base, 3) if base is not None else None,
        "k": round(k, 2) if k is not None else None,
        "equity": [[x["t"], round(float(v), 3)] for x, v in zip(trades, cum)],
        "last": [{f: v for f, v in x.items() if f != "j"} for x in trades[-40:][::-1]],
    }
    return out


# ---------------------------------------------------------------- Lauf und Merge

def write(res, out):
    os.makedirs(os.path.dirname(os.path.abspath(out)), exist_ok=True)
    tmp = out + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(res, f, ensure_ascii=False)
    os.replace(tmp, out)


def meta():
    return {"configs": [{"key": c["key"], "label": c["label"], "E": c["E"], "T": c["T"],
                         "rr": c["rr"], "zone": c["zone"], "pmul": c["pmul"]} for c in CONFIGS],
            "params": {"trend": f"EMA{EMA_N}, Steigung ueber {SLOPE_LAG} Kerzen", "atr": ATR_N,
                       "buf": BUF, "max_risk": MAX_RISK, "oos_frac": round(OOS_FRAC, 3),
                       "min_is": MIN_IS, "min_oos": MIN_OOS, "tf": PARAMS}}


def run_shard(shard, of, out):
    h = login()
    items = [u for i, u in enumerate(UNIVERSE) if i % of == shard]
    res = {"updated": datetime.now(timezone.utc).isoformat(timespec="minutes"), **meta(), "markets": []}
    write(res, out)
    for name, klasse, cands in items:
        entry = {"name": name, "klasse": klasse}
        res["markets"].append(entry)
        try:
            epic, inst = resolve(h, klasse, cands)
            if epic is None:
                entry["error"] = "Markt nicht gefunden"
                write(res, out)
                continue
            data = {}
            for tf in ("M1", "M5", "M15", "H1"):
                for days in DAYS[tf]:
                    try:
                        data[tf] = fetch(h, epic, tf, days)
                        entry[f"{tf}_days"] = days
                        break
                    except Exception as e:
                        print(name, tf, days, "fehlgeschlagen:", e)
            # M5, M15, H1 sind Pflicht. Fehlt M1, laufen nur die Konfigurationen ohne M1 weiter.
            missing = [tf for tf in ("M5", "M15", "H1") if tf not in data]
            if missing:
                entry["error"] = "Kerzen fehlen: " + ", ".join(missing)
                write(res, out)
                continue
            if "M1" not in data:
                entry["hinweis"] = "M1 nicht verfuegbar: M1-Konfigurationen ohne Ergebnis"
            spread = measure_spread(h, epic)
            if not spread or spread <= 0:
                entry["error"] = "Spread nicht gemessen"
                write(res, out)
                continue
            entry.update({
                "epic": epic, "instrument": inst or epic, "spread": spread,
                "candles": {tf: int(len(data[tf]["c"])) for tf in data},
                "from": {tf: int(data[tf]["t"][0]) for tf in data},
                "to": {tf: int(data[tf]["t"][-1]) for tf in data},
            })
            entry.update(analyse_market(data, spread))
        except Exception as e:
            entry["error"] = f"{type(e).__name__}: {e}"
        write(res, out)
    print("fertig:", out, "Maerkte:", len(res["markets"]))


def merge(parts_dir, out):
    files = []
    for root, _, names in os.walk(parts_dir):
        files += [os.path.join(root, f) for f in names if f.endswith(".json")]
    if not files:
        raise SystemExit("keine Teilergebnisse gefunden")
    res, markets, updated = None, [], ""
    for f in sorted(files):
        with open(f, encoding="utf-8") as fh:
            p = json.load(fh)
        updated = max(updated, p.get("updated", ""))
        if res is None:
            res = {k: v for k, v in p.items() if k not in ("markets", "updated")}
        markets += p.get("markets", [])
    order = {u[0]: i for i, u in enumerate(UNIVERSE)}
    markets.sort(key=lambda m: order.get(m["name"], 999))
    res = {"updated": updated, **res, "markets": markets}
    write(res, out)
    print("zusammengefuehrt:", out, "Maerkte:", len(markets))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--shard", type=int, default=0)
    ap.add_argument("--of", type=int, default=1)
    ap.add_argument("--out", default=OUT)
    ap.add_argument("--merge", default=None)
    a = ap.parse_args()
    if a.merge:
        merge(a.merge, a.out)
    else:
        run_shard(a.shard, a.of, a.out)


if __name__ == "__main__":
    main()
