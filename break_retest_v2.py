"""Break and Retest - Version 2 (ENTWURF). Eine feste Konfiguration, keine Auswahl.

Naeher an der Beschreibung im Video:
- Erkennung (Ausbruch, Retest, Bestaetigung) wie in break_retest.py, unveraendert.
- Stop: Tief bzw. Hoch der Bestaetigungskerze (laut Video "Candle Opening bzw. Low").
- Ziel: 50 % bei 3R, Rest bei 5R. Nach dem Teilgewinn Stop auf Break-even fuer den Rest.
- Einstieg nur in den Fenstern 9:00-10:00 und 15:30-16:30 (Berlin), an der Kerze-Oeffnung gemessen.
- Kein Trade am ersten Freitag im Monat (NFP-Tag).
Offen geblieben: Trendlinien statt horizontaler Pivot-Level (eigener Schritt).

Schreibt docs/break_retest_v2.json im selben Format wie docs/break_retest.json.
Der Zufalls-Vergleich fehlt bewusst (Anzeige "–").
"""
import json
import os
import sys
import time
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import break_retest as br  # noqa: E402

ROOT = os.path.dirname(os.path.abspath(__file__))
OUT = os.path.join(ROOT, "docs", "break_retest_v2.json")
TZ = ZoneInfo("Europe/Berlin")

MARKETS = [  # Maerkte der Orderblock-Strategie (signal_check.py)
    ("Gold", "Rohstoff", ["GOLD"]),
    ("Nasdaq 100", "Index", ["US100"]),
    ("EUR/USD", "Forex", ["EURUSD"]),
    ("GBP/USD", "Forex", ["GBPUSD"]),
    ("DAX", "Index", ["DE40", "GER40", "DAX40"]),
]

CFG = {"key": "V2_M5_H1", "label": "V2: Einstieg M5, Trend H1, Teil 3R + Rest 5R",
       "E": "M5", "T": "H1", "rr": "3/5", "zone": 0.4, "pmul": 1}
TP1, TP2, SHARE1 = 3.0, 5.0, 0.5
WINDOWS = [(9 * 60, 10 * 60), (15 * 60 + 30, 16 * 60 + 30)]


def in_window(t_open):
    dt = datetime.fromtimestamp(int(t_open), tz=TZ)
    m = dt.hour * 60 + dt.minute
    return any(a <= m < b for a, b in WINDOWS)


def is_nfp_day(t_open):
    dt = datetime.fromtimestamp(int(t_open), tz=TZ)
    return dt.weekday() == 4 and dt.day <= 7


def outcome_v2(h, l, c, j, d, entry, stop, risk, H):
    """Verlauf ab Kerze j. Gibt (R brutto, Ausstiegskerze, Grund)."""
    n = len(c)
    end = min(j + H, n - 1)
    if end <= j:
        return None
    s = stop
    part = False
    for k in range(j + 1, end + 1):
        stopped = (l[k] <= s) if d > 0 else (h[k] >= s)
        if stopped:
            return (TP1 * SHARE1 if part else -1.0), k, ("Teil+BE" if part else "Stop")
        if not part:
            hit1 = (h[k] >= entry + TP1 * risk) if d > 0 else (l[k] <= entry - TP1 * risk)
            if hit1:
                part = True
                s = entry          # Rest auf Break-even
        else:
            hit2 = (h[k] >= entry + TP2 * risk) if d > 0 else (l[k] <= entry - TP2 * risk)
            if hit2:
                return TP1 * SHARE1 + TP2 * (1 - SHARE1), k, "Ziel 5R"
    r_close = d * (c[end] - entry) / risk
    if part:
        return TP1 * SHARE1 + (1 - SHARE1) * r_close, end, "Zeit (Rest)"
    return r_close, end, "Zeit"


def simulate_v2(E, trend, P, W, H, spread, zone, live=False):
    """Gleiche Erkennung wie break_retest.simulate, aber Stop, Ziele und Filter nach V2.
    live=True: Bestaetigung auf der letzten Kerze zaehlt auch ohne Ausgang (Vorwaertstest)."""
    o, h, l, c, t = E["o"], E["h"], E["l"], E["c"], E["t"]
    n = len(c)
    a = br.atr(h, l, c)
    ph, pl = br.pivots(h, l, P)
    lh, ll = br.levels(ph, pl, h, l, P)
    trades = []
    used_h = used_l = np.nan
    setup = None
    busy = -1
    for j in range(1, n):
        aj = a[j]
        if np.isnan(aj) or j <= busy:
            continue
        tr = trend[j]

        if setup is not None:
            d, L, bf = setup["d"], setup["L"], setup["buf"]
            if tr != d or j - setup["b"] > W or (d > 0 and c[j] < L - bf) or (d < 0 and c[j] > L + bf):
                setup = None
                continue
            if d > 0:
                touch = l[j] <= L + setup["zone"]
                if touch or setup["touched"]:
                    setup["touched"] = True
                confirm = c[j] > o[j] and c[j] > L + bf
            else:
                touch = h[j] >= L - setup["zone"]
                if touch or setup["touched"]:
                    setup["touched"] = True
                confirm = c[j] < o[j] and c[j] < L - bf
            if setup["touched"] and confirm:
                entry = c[j]
                stop = l[j] if d > 0 else h[j]
                risk = abs(entry - stop)
                setup = None
                if risk <= 0 or risk > br.MAX_RISK * aj or risk < 2 * spread:
                    continue
                if not in_window(t[j]) or is_nfp_day(t[j]):
                    continue
                tgt1 = entry + d * TP1 * risk
                tgt2 = entry + d * TP2 * risk
                res = outcome_v2(h, l, c, j, d, entry, stop, risk, H)
                if res is None:
                    if not live:
                        continue
                    res = (0.0, j, "offen")
                r_g, x, why = res
                trades.append({"j": j, "t": int(t[j]), "x": int(t[x]), "d": int(d), "L": float(L),
                               "entry": float(entry), "stop": float(stop), "tgt": float(tgt1),
                               "tgt2": float(tgt2), "rk": float(risk / aj),
                               "R": float(r_g - spread / risk), "why": why})
                busy = x
            continue

        if tr > 0 and not np.isnan(lh[j]) and lh[j] != used_h:
            L = lh[j]
            bf = br.BUF * aj
            if c[j - 1] <= L + bf < c[j]:
                setup = {"d": 1, "L": L, "buf": bf, "zone": zone * aj, "b": j, "touched": False}
                used_h = L
        elif tr < 0 and not np.isnan(ll[j]) and ll[j] != used_l:
            L = ll[j]
            bf = br.BUF * aj
            if c[j] < L - bf <= c[j - 1]:
                setup = {"d": -1, "L": L, "buf": bf, "zone": zone * aj, "b": j, "touched": False}
                used_l = L
    return trades


def analyse_v2(data, spread):
    E, T = data["M5"], data["H1"]
    trend = br.trend_on(E, T)
    p = br.PARAMS["M5"]
    trades = simulate_v2(E, trend, p["P"], p["W"], p["H"], spread, CFG["zone"])
    key = CFG["key"]
    results = {key: br.split_stats(E, trades)}
    rs = np.array([x["R"] for x in trades], dtype=float)
    cum = np.cumsum(rs) if rs.size else np.array([])
    ks = [x["rk"] for x in trades]
    return {
        "results": results,
        "chosen": key,
        "verdict": br.verdict(results[key]["oos"]),
        "detail": {
            "baseline": None,
            "k": round(float(np.median(ks)), 2) if ks else None,
            "equity": [[x["t"], round(float(v), 3)] for x, v in zip(trades, cum)],
            "last": [{f: v for f, v in x.items() if f != "j"} for x in trades[-40:][::-1]],
        },
    }


def meta():
    return {
        "configs": [CFG],
        "params": {"trend": f"EMA{br.EMA_N}, Steigung ueber {br.SLOPE_LAG} Kerzen", "atr": br.ATR_N,
                   "buf": br.BUF, "max_risk": br.MAX_RISK, "oos_frac": round(br.OOS_FRAC, 3),
                   "min_is": br.MIN_IS, "min_oos": br.MIN_OOS, "tf": br.PARAMS,
                   "v2": {"teil_bei": TP1, "rest_bei": TP2, "anteil_teil": SHARE1,
                          "fenster_berlin": ["09:00-10:00", "15:30-16:30"],
                          "nfp": "erster Freitag im Monat kein Trade", "stop": "Tief/Hoch der Bestaetigungskerze"}},
    }


def run(out=OUT):
    h = br.login()
    res = {"updated": datetime.now(timezone.utc).isoformat(timespec="minutes"), **meta(), "markets": []}
    br.write(res, out)
    for name, klasse, cands in MARKETS:
        entry = {"name": name, "klasse": klasse}
        res["markets"].append(entry)
        try:
            epic, inst = br.resolve(h, klasse, cands)
            if epic is None:
                entry["error"] = "Markt nicht gefunden"
                br.write(res, out)
                continue
            data = {}
            for tf in ("M5", "H1"):
                for days in br.DAYS[tf]:
                    try:
                        data[tf] = br.fetch(h, epic, tf, days)
                        entry[f"{tf}_days"] = days
                        break
                    except Exception as e:
                        print(name, tf, days, "fehlgeschlagen:", e)
            missing = [tf for tf in ("M5", "H1") if tf not in data]
            if missing:
                entry["error"] = "Kerzen fehlen: " + ", ".join(missing)
                br.write(res, out)
                continue
            spread = br.measure_spread(h, epic)
            if not spread or spread <= 0:
                entry["error"] = "Spread nicht gemessen"
                br.write(res, out)
                continue
            entry.update({
                "epic": epic, "instrument": inst or epic, "spread": spread,
                "candles": {tf: int(len(data[tf]["c"])) for tf in data},
                "from": {tf: int(data[tf]["t"][0]) for tf in data},
                "to": {tf: int(data[tf]["t"][-1]) for tf in data},
            })
            entry.update(analyse_v2(data, spread))
        except Exception as e:
            entry["error"] = f"{type(e).__name__}: {e}"
        br.write(res, out)
        time.sleep(2)
    print("fertig:", out, "Maerkte:", len(res["markets"]))
    return res


if __name__ == "__main__":
    run()
