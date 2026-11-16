"""Backtest: Einstiegs-Varianten (aendert NICHTS am Live-Bot).

Population wie in backtest_strategie.py: Orderblock mit starkem Impuls (1.5x), direkt angrenzend,
FVG vorhanden, nur in Richtung des H1-Trends. Antest = Beruehrung des Blocks, wie im Live-Bot.

Varianten:
  basis   = heutiger Einstieg: Bestaetigungskerze auf demselben Zeitrahmen nach dem Antest
  abstand = wie basis, aber nur wenn der Einstiegskurs hoechstens 0,5 Zonenhoehen vom Zonenrand liegt
  ltf     = nur H1-Zonen: Antest und Bestaetigung auf M5-Kerzen (gleiche Bestaetigungsregel)
  sitzung = wie basis, aber nur Einstieg zwischen 08:00 und 22:00 (Rom)

Stop knapp hinter der Zone, Ziele 1R, 1.5R, 2R, Spread wird pro Trade abgezogen (wie in den anderen Backtests).
Ergebnis: docs/backtest_entry.json, Format wie bei den anderen Backtests.
"""
import json
import os
import traceback
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

import numpy as np
import requests

import backtest_strategie as bs
import backtest_bestaetigung as bc

ROME = ZoneInfo("Europe/Rome")
MAX_DIST = 0.5                       # Einstieg hoechstens 0,5 Zonenhoehen vom Zonenrand
SESSION = (8, 22)                    # Einstieg nur zwischen 08:00 und 22:00 Rom
LTF_WAIT = bs.WAIT["H1"] * 12        # Wartezeit in M5-Kerzen fuer H1-Zonen
RRS = bc.RRS
VARIANTS = ["basis", "abstand", "ltf", "sitzung"]
NAMES = {
    "basis": "Heutiger Einstieg (Bestaetigung auf demselben Zeitrahmen)",
    "abstand": "Nur wenn Einstieg max. 0,5 Zonenhoehen vom Rand entfernt",
    "ltf": "H1-Zone, Antest und Bestaetigung auf M5",
    "sitzung": "Nur Einstieg 08:00 bis 22:00 Rom",
}


def confirm(d, start, zlo, zhi, kind, wait, conf_wait):
    """Antest plus Bestaetigung, genau wie im Bot. Gibt den Index der Bestaetigungskerze oder None."""
    o, hi, lo, c = d["o"], d["h"], d["l"], d["c"]
    body = np.abs(c - o)
    n = len(c)
    armed = None
    for j in range(max(start, 1), min(start + wait, n)):
        if (kind == 1 and c[j] < zlo) or (kind == -1 and c[j] > zhi):
            return None                                   # Zone durchbrochen
        hit = (lo[j] <= zhi) if kind == 1 else (hi[j] >= zlo)
        red = (c[j] < o[j]) if kind == 1 else (c[j] > o[j])
        if hit and red:
            armed = j                                     # jeder Antest mit Gegenkerze zaehlt
            continue
        if armed is None or j - armed > conf_wait:
            continue
        if body[j] < bc.CONF_BODY * body[max(0, j - 20):j].mean():
            continue                                      # Bestaetigungskerze muss gross sein
        if kind == 1 and c[j] > o[j] and c[j] > hi[j - 1]:
            return j
        if kind == -1 and c[j] < o[j] and c[j] < lo[j - 1]:
            return j
    return None


def outcome(d, j, zlo, zhi, kind, spread, tf):
    """Ergebnis in R je Ziel, nach Spread. None, wenn der Stop zu eng ist."""
    o, hi, lo, c = d["o"], d["h"], d["l"], d["c"]
    n = len(c)
    price = float(c[j])
    stop = zlo - spread if kind == 1 else zhi + spread
    risk = abs(price - stop)
    if risk < bs.MIN_RISK_SPREADS * spread:
        return None
    cost = spread / risk
    out = {}
    for rr in RRS:
        target = price + rr * risk if kind == 1 else price - rr * risk
        val = None
        end = min(j + 1 + bs.MAX_HOLD[tf], n)
        for q in range(j + 1, end):
            if (lo[q] <= stop) if kind == 1 else (hi[q] >= stop):
                val = -1.0
                break
            if (hi[q] >= target) if kind == 1 else (lo[q] <= target):
                val = rr
                break
        if val is None:
            last = c[end - 1]
            val = ((last - price) if kind == 1 else (price - last)) / risk
        out[rr] = float(val - cost)
    return {"price": price, "t": int(d["t"][j]), "r": out}


def zdist(price, zlo, zhi, kind):
    """Abstand des Einstiegs zum Zonenrand in Zonenhoehen (0 = am Rand oder in der Zone)."""
    dist = max(0.0, price - zhi) if kind == 1 else max(0.0, zlo - price)
    return dist / (zhi - zlo)


def run_market(m5, h1, spread):
    tr = bs.h1_trend(h1)
    avail = h1["t"] + 3600
    start_ok = avail[min(bs.WARMUP_H1, len(avail) - 1)]
    trades = []
    m5_t0 = int(m5["t"][0])   # LTF braucht M5-Daten: H1-Zonen werden nur ab hier gewertet, fuer alle Varianten gleich

    def add(variant, res, tf):
        for rr in RRS:
            trades.append({"tf": tf, "variant": variant, "entry": "bestaetigung",
                           "rr": rr, "r": res["r"][rr], "t": res["t"], "zd": res["zd"]})

    for tf, d in (("M5", m5), ("H1", h1)):
        step = 300 if tf == "M5" else 3600
        for b in bs.find_blocks(d):
            if not (b["adj"] and b["fvg"]):
                continue
            if tf == "H1" and int(d["t"][b["i"]]) < m5_t0 + 3600:
                continue
            sig_t = int(d["t"][b["i"] + 1]) + step
            if sig_t < start_ok:
                continue
            idx = np.searchsorted(avail, sig_t, side="right") - 1
            if idx < 0:
                continue
            trend = tr[idx]
            if not (trend == 0 or trend == b["kind"]):
                continue
            k, i, kind = b["k"], b["i"], b["kind"]
            zlo, zhi = float(d["l"][k]), float(d["h"][k])
            if zhi - zlo <= 0:
                continue

            j = confirm(d, i + 1, zlo, zhi, kind, bs.WAIT[tf] + 1, bc.CONF_WAIT[tf])
            if j is None:
                continue
            base = outcome(d, j, zlo, zhi, kind, spread, tf)
            if base is None:
                continue
            base["zd"] = zdist(base["price"], zlo, zhi, kind)
            add("basis", base, tf)

            if base["zd"] <= MAX_DIST:
                add("abstand", base, tf)

            hour = datetime.fromtimestamp(base["t"], ROME).hour
            if SESSION[0] <= hour < SESSION[1]:
                add("sitzung", base, tf)

            if tf == "H1":
                s = int(np.searchsorted(m5["t"], int(d["t"][i]) + 3600))
                j5 = confirm(m5, s, zlo, zhi, kind, LTF_WAIT + 1, bc.CONF_WAIT["M5"])
                if j5 is not None:
                    ltf = outcome(m5, j5, zlo, zhi, kind, spread, "M5")
                    if ltf is not None:
                        ltf["zd"] = zdist(ltf["price"], zlo, zhi, kind)
                        add("ltf", ltf, "H1")
    return trades


def abstand_mittel(trades):
    """Mittlerer Abstand des Einstiegs zur Zone (in Zonenhoehen) je Zeitrahmen und Variante."""
    acc = {}
    for t in trades:
        if t["rr"] != RRS[0]:
            continue
        acc.setdefault(t["tf"], {}).setdefault(t["variant"], []).append(t["zd"])
    return {tf: {v: round(float(np.mean(x)), 2) for v, x in vs.items()} for tf, vs in acc.items()}


def write(res):
    os.makedirs("docs", exist_ok=True)
    with open("docs/backtest_entry.json", "w") as f:
        json.dump(res, f)


def print_summary(pooled):
    f = lambda x, fmt: "–" if x is None else format(x, fmt)
    for tf in ("M5", "H1"):
        for v in VARIANTS:
            for rr in RRS:
                s = pooled.get(tf, {}).get(v, {}).get("bestaetigung", {}).get(str(rr))
                if not s or s.get("n", 0) == 0:
                    continue
                print(f"{tf:3} {v:9} {rr}R  n={s['n']:5}  win={f(s['win'], '5.1f')}%  avg={f(s['avg'], '+.3f')}R  "
                      f"t={f(s['t'], '+.2f')}  Hälfte1={f(s['h1'], '+.3f')}  Hälfte2={f(s['h2'], '+.3f')}")


def main():
    r = requests.post(
        f"{bs.BASE}/session",
        headers={"X-CAP-API-KEY": os.environ["CAPITAL_API_KEY"]},
        json={"identifier": os.environ["CAPITAL_IDENTIFIER"],
              "password": os.environ["CAPITAL_PASSWORD"]},
        timeout=20,
    )
    r.raise_for_status()
    h = {"CST": r.headers["CST"], "X-SECURITY-TOKEN": r.headers["X-SECURITY-TOKEN"]}
    res = {"updated": datetime.now(timezone.utc).isoformat(timespec="minutes"),
           "varianten": NAMES, "max_abstand": MAX_DIST, "sitzung": list(SESSION),
           "hinweis": "H1-Zonen nur im Zeitraum der M5-Daten, damit alle Varianten vergleichbar sind",
           "markets": []}
    write(res)
    pooled = []
    for name, cands in bs.MARKETS:
        entry = {"name": name}
        res["markets"].append(entry)
        try:
            epic = None
            for ep in cands:
                if requests.get(f"{bs.BASE}/markets/{ep}", headers=h, timeout=20).status_code == 200:
                    epic = ep
                    break
            if epic is None:
                entry["error"] = "Markt nicht gefunden"
                write(res)
                continue
            m5 = None
            for days in (365, 180, 120, 70, 40):
                try:
                    m5 = bs.fetch(h, epic, "MINUTE_5", days, 3, 1000)
                    entry["m5_days"] = days
                    break
                except Exception as e:
                    print(name, "M5", days, "fehlgeschlagen:", e)
            h1 = None
            for days in (540, 365, 180, 90):
                try:
                    h1 = bs.fetch(h, epic, "HOUR", days, 30, 1000)
                    entry["h1_days"] = days
                    break
                except Exception as e:
                    print(name, "H1", days, "fehlgeschlagen:", e)
            if m5 is None or h1 is None:
                entry["error"] = "Kerzen konnten nicht geladen werden"
                write(res)
                continue
            spread = bs.measure_spread(h, epic)
            price = float(m5["c"][-1])
            if not spread or spread <= 0:
                spread = 0.000144 * price
            entry.update({"epic": epic, "spread": spread,
                          "m5_candles": len(m5["c"]), "h1_candles": len(h1["c"]),
                          "m5_ab": datetime.fromtimestamp(int(m5["t"][0]), timezone.utc).strftime("%Y-%m-%d")})
            trades = run_market(m5, h1, spread)
            entry["stats"] = bs.summarize(trades)
            pooled.extend(trades)
            write(res)
        except Exception as e:
            traceback.print_exc()
            entry["error"] = str(e)
            write(res)
    res["pooled"] = bs.summarize(pooled)
    res["abstand_mittel"] = abstand_mittel(pooled)
    write(res)
    print_summary(res["pooled"])
    print("Mittlerer Abstand zur Zone (Zonenhoehen):", res["abstand_mittel"])
    print("fertig")


if __name__ == "__main__":
    main()
