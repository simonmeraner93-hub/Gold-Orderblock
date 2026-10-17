"""Backtest mit Bestaetigungs-Einstieg (so wie du handelst).

Ablauf pro Orderblock (M5 oder H1):
 1. starker Ausbruch (Impuls >= 1.5x oder 2x Durchschnittskoerper)
 2. Fair Value Gap vorhanden, Block direkt vor dem Impuls, H1-Trend passt
 3. Kurs laeuft in den Block zurueck (Rücklauf), schliesst aber nicht dahinter
 4. Bestaetigung: eine Kerze schliesst in Trendrichtung ueber dem Hoch (Long)
    bzw. unter dem Tief (Short) der Vorkerze. Einstieg zum Schlusskurs dieser Kerze.
 5. Stop knapp hinter dem Block, Ziel 1R, 1.5R oder 2R, Spread wird abgezogen.
Hat der Kurs den Block nicht bis zum Ende der Wartezeit zurueckgeholt oder keine
Bestaetigung geliefert, gibt es keinen Trade.
"""
import json
import os
import traceback
from datetime import datetime, timezone

import numpy as np
import requests

import backtest_strategie as bs

RRS = [1.0, 1.5, 2.0]
CONF_WAIT = {"M5": 12, "H1": 6}      # Kerzen nach der Beruehrung fuer die Bestaetigung
VARIANTS = ["trend", "strategie", "stark"]
NAMES = {
    "trend": "Trend (ohne FVG)",
    "strategie": "Trend + FVG",
    "stark": "Trend + FVG + starker Ausbruch (2x)",
}


def simulate_conf(d, b, tf, spread):
    o, hi, lo, c = d["o"], d["h"], d["l"], d["c"]
    n = len(c)
    k, i, kind = b["k"], b["i"], b["kind"]
    zlo, zhi = lo[k], hi[k]
    if zhi - zlo <= 0:
        return None
    touch = None
    for j in range(i + 2, min(i + 2 + bs.WAIT[tf], n)):
        if (kind == 1 and c[j] < zlo) or (kind == -1 and c[j] > zhi):
            return None                       # Block durchbrochen
        if (kind == 1 and lo[j] <= zhi) or (kind == -1 and hi[j] >= zlo):
            touch = j
            break
    if touch is None:
        return None
    entry_j = None
    for j in range(touch, min(touch + CONF_WAIT[tf], n)):
        if (kind == 1 and c[j] < zlo) or (kind == -1 and c[j] > zhi):
            return None
        if kind == 1 and c[j] > o[j] and c[j] > hi[j - 1]:
            entry_j = j
            break
        if kind == -1 and c[j] < o[j] and c[j] < lo[j - 1]:
            entry_j = j
            break
    if entry_j is None:
        return None
    price = c[entry_j]
    stop = zlo - spread if kind == 1 else zhi + spread
    risk = abs(price - stop)
    if risk < bs.MIN_RISK_SPREADS * spread:
        return None
    res = {}
    for rr in RRS:
        target = price + rr * risk if kind == 1 else price - rr * risk
        val = None
        end = min(entry_j + 1 + bs.MAX_HOLD[tf], n)
        for j in range(entry_j + 1, end):
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
    return {"r": res, "risk": risk, "t": int(d["t"][entry_j])}


def run_market(m5, h1, spread):
    tr = bs.h1_trend(h1)
    avail = h1["t"] + 3600
    start_ok = avail[min(bs.WARMUP_H1, len(avail) - 1)]
    trades = []
    for tf, d in (("M5", m5), ("H1", h1)):
        step = 300 if tf == "M5" else 3600
        for strong, tag in ((1.5, "n"), (2.0, "s")):
            bs.STRONG = strong
            blocks = bs.find_blocks(d)
            bs.STRONG = 1.5
            for b in blocks:
                sig_t = int(d["t"][b["i"] + 1]) + step
                if sig_t < start_ok:
                    continue
                idx = np.searchsorted(avail, sig_t, side="right") - 1
                if idx < 0:
                    continue
                trend = tr[idx]
                fit = (trend == 0) or (trend == b["kind"])
                if not fit:
                    continue
                variants = []
                if tag == "n":
                    variants.append("trend")
                    if b["adj"] and b["fvg"]:
                        variants.append("strategie")
                elif b["adj"] and b["fvg"]:
                    variants.append("stark")
                if not variants:
                    continue
                sim = simulate_conf(d, b, tf, spread)
                if sim is None:
                    continue
                cost = spread / sim["risk"]
                for rr in RRS:
                    rn = sim["r"][rr] - cost
                    for v in variants:
                        trades.append({"tf": tf, "variant": v, "entry": "bestaetigung",
                                       "rr": rr, "r": float(rn), "t": sim["t"]})
    return trades


def write(res):
    os.makedirs("docs", exist_ok=True)
    with open("docs/backtest_bestaetigung.json", "w") as f:
        json.dump(res, f)


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
    res = {"updated": datetime.now(timezone.utc).isoformat(timespec="minutes"), "markets": []}
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
            for days in (180, 120, 70, 40):
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
                          "m5_candles": len(m5["c"]), "h1_candles": len(h1["c"])})
            trades = run_market(m5, h1, spread)
            entry["stats"] = bs.summarize(trades)
            pooled.extend(trades)
            write(res)
        except Exception as e:
            traceback.print_exc()
            entry["error"] = str(e)
            write(res)
    res["pooled"] = bs.summarize(pooled)
    write(res)
    print("fertig")


if __name__ == "__main__":
    main()
