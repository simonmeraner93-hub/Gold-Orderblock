"""Liste der letzten 50 DAX-Orderbloecke (M5) nach deiner Strategie.

Nur Bloecke mit: Trend passt (H1, EMA20/50, neutral = beide), direkt vor dem Impuls,
Fair Value Gap vorhanden. Pro Block: Zeit, Zone, FVG-Bereich und - zum Nachpruefen -
was dein Einstieg (Ruecklauf ins FVG, Bestaetigungskerze, Stop unter/ueber dem Block)
ergeben haette.
"""
import json
import os
from datetime import datetime, timezone

import numpy as np
import requests

import backtest_strategie as bs
import backtest_bestaetigung as bz

CANDS = ["DE40", "GER40", "DAX40"]
COUNT = 50


def fetch_safe(h, epic, res, days, chunk_days, per_call):
    """Wie bs.fetch, aber leere Zeitraeume (Wochenende, 404) werden uebersprungen."""
    import time
    import pandas as pd
    from datetime import timedelta
    end = datetime.now(timezone.utc).replace(second=0, microsecond=0)
    cur = (end - timedelta(days=days)).replace(hour=0, minute=0)
    rows = {}
    while cur < end:
        nxt = min(cur + timedelta(days=chunk_days), end)
        params = {"resolution": res, "from": cur.strftime("%Y-%m-%dT%H:%M:%S"),
                  "to": nxt.strftime("%Y-%m-%dT%H:%M:%S"), "max": per_call}
        r = None
        for attempt in range(4):
            r = requests.get(f"{bs.BASE}/prices/{epic}", headers=h, params=params, timeout=30)
            if r.status_code == 429:
                time.sleep(3 * (attempt + 1))
                continue
            break
        if r.status_code == 404:
            cur = nxt
            continue
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
    return {"t": np.array(ts[:-1], dtype=np.int64), "o": arr[:-1, 0], "h": arr[:-1, 1],
            "l": arr[:-1, 2], "c": arr[:-1, 3]}


def outcome(d, entry_j, kind, price, stop, rr):
    risk = abs(price - stop)
    target = price + rr * risk if kind == 1 else price - rr * risk
    n = len(d["c"])
    end = min(entry_j + 1 + bs.MAX_HOLD["M5"], n)
    for j in range(entry_j + 1, end):
        if (kind == 1 and d["l"][j] <= stop) or (kind == -1 and d["h"][j] >= stop):
            return "Stop", target
        if (kind == 1 and d["h"][j] >= target) or (kind == -1 and d["l"][j] <= target):
            return "Ziel", target
    return "offen", target


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
    epic = None
    for ep in CANDS:
        if requests.get(f"{bs.BASE}/markets/{ep}", headers=h, timeout=20).status_code == 200:
            epic = ep
            break
    if epic is None:
        raise SystemExit("DAX nicht gefunden")
    m5 = h1 = None
    for days in (70, 40):
        try:
            m5 = fetch_safe(h, epic, "MINUTE_5", days, 3, 1000)
            break
        except Exception as e:
            print("M5", days, e)
    for days in (365, 180, 90):
        try:
            h1 = fetch_safe(h, epic, "HOUR", days, 30, 1000)
            break
        except Exception as e:
            print("H1", days, e)
    if m5 is None or h1 is None:
        raise SystemExit("Kerzen konnten nicht geladen werden")
    spread = bs.measure_spread(h, epic) or 0.0
    tr = bs.h1_trend(h1)
    avail = h1["t"] + 3600
    o, hi, lo, c, t = m5["o"], m5["h"], m5["l"], m5["c"], m5["t"]
    rows = []
    for b in bs.find_blocks(m5):
        if not (b["adj"] and b["fvg"]):
            continue
        k, i, kind = b["k"], b["i"], b["kind"]
        sig_t = int(t[i + 1]) + 300
        idx = np.searchsorted(avail, sig_t, side="right") - 1
        if idx < 0:
            continue
        trend = int(tr[idx])
        if not (trend == 0 or trend == kind):
            continue
        if kind == 1:
            fvg_lo, fvg_hi = float(hi[i - 1]), float(lo[i + 1])
        else:
            fvg_lo, fvg_hi = float(hi[i + 1]), float(lo[i - 1])
        row = {"t": int(t[k]), "kind": kind, "trend": trend,
               "lo": float(lo[k]), "hi": float(hi[k]),
               "fvg_lo": fvg_lo, "fvg_hi": fvg_hi, "trade": None}
        sim = bz.simulate_conf(m5, b, "M5", spread, "fvg")
        if sim is not None:
            ej = int(np.searchsorted(t, sim["t"]))
            price = float(c[ej])
            stop = row["lo"] - spread if kind == 1 else row["hi"] + spread
            tr_ = {"t": int(sim["t"]), "price": price, "stop": float(stop)}
            for rr in (1.0, 2.0):
                res, tgt = outcome(m5, ej, kind, price, stop, rr)
                tr_[f"r{int(rr)}"] = res
                tr_[f"z{int(rr)}"] = float(tgt)
            row["trade"] = tr_
        rows.append(row)
    rows = rows[-COUNT:]
    out = {"updated": datetime.now(timezone.utc).isoformat(timespec="minutes"),
           "epic": epic, "spread": spread, "blocks": rows}
    os.makedirs("docs", exist_ok=True)
    with open("docs/dax_blocks.json", "w") as f:
        json.dump(out, f)
    print("Bloecke:", len(rows))


if __name__ == "__main__":
    main()
