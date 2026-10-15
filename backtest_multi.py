"""Backtest der Orderblock-Strategien auf mehreren Maerkten.

Gleiche Regeln wie bei Gold. Punktwerte (Stop-Puffer, Mindest-Risiko usw.)
werden mit dem Kurs skaliert. Die Spanne wird aus den echten Bid/Ask-Daten
gemessen (Median der letzten ca. 500 Kerzen).
"""
import json
import os
import statistics
import sys
import traceback

import requests

import backtest as bt
import backtest2 as bt2
import backtest4 as bt4
import backtest5 as bt5

MARKETS = [
    ("Gold (zum Vergleich)", ["GOLD"]),
    ("S&P 500", ["US500"]),
    ("Nasdaq 100", ["US100"]),
    ("EUR/USD", ["EURUSD"]),
    ("GBP/USD", ["GBPUSD"]),
    ("DAX", ["DE40", "GER40", "DAX40"]),
]
REF_PRICE = 4160.0
BASE_CONST = {
    "STOP_PUFFER": 0.5, "MIN_RISK": 1.0, "MIN_STOP": 6.0, "MIN_ZONE": 0.05,
}
BASE4 = {"RANGE_MIN_RISK": 3.0, "RANGE_MAX_RISK": 40.0}


def write(res):
    os.makedirs("docs", exist_ok=True)
    with open("docs/backtest_multi.json", "w") as f:
        json.dump(res, f)


def pick_epic(h, cands):
    for ep in cands:
        r = requests.get(f"{bt.BASE}/markets/{ep}", headers=h, timeout=20)
        if r.status_code == 200:
            return ep
    return None


def measure_spread(h, ep):
    r = requests.get(f"{bt.BASE}/prices/{ep}", headers=h,
                     params={"resolution": "MINUTE_5", "max": 500}, timeout=30)
    r.raise_for_status()
    sp = []
    for p in r.json().get("prices", []):
        c = p["closePrice"]
        if c.get("ask") is not None and c.get("bid") is not None:
            sp.append(c["ask"] - c["bid"])
    return statistics.median(sp) if sp else None


def main():
    r = requests.post(
        f"{bt.BASE}/session",
        headers={"X-CAP-API-KEY": os.environ["CAPITAL_API_KEY"]},
        json={"identifier": os.environ["CAPITAL_IDENTIFIER"],
              "password": os.environ["CAPITAL_PASSWORD"]},
        timeout=20,
    )
    r.raise_for_status()
    h = {"CST": r.headers["CST"], "X-SECURITY-TOKEN": r.headers["X-SECURITY-TOKEN"]}

    res = {"markets": []}
    write(res)
    for name, cands in MARKETS:
        entry = {"name": name}
        res["markets"].append(entry)
        try:
            ep = pick_epic(h, cands)
            if ep is None:
                entry["error"] = "Markt nicht gefunden: " + ", ".join(cands)
                print(name, entry["error"])
                write(res)
                continue
            entry["epic"] = ep
            bt.EPIC = ep
            m5 = None
            for days in (180, 120, 70, 40):
                bt.DAYS = days
                try:
                    m5 = bt.load(h)
                    break
                except Exception as e:
                    print(name, "Laden mit", days, "Tagen fehlgeschlagen:", e)
            if m5 is None:
                entry["error"] = "Kerzen konnten nicht geladen werden"
                write(res)
                continue
            price = float(m5.close.iloc[-1])
            f = price / REF_PRICE
            spread = measure_spread(h, ep)
            if spread is None or spread <= 0:
                spread = 0.0144 / 100 * price
                entry["spread_note"] = "geschätzt wie bei Gold"
            bt.SPREAD = spread
            for k, v in BASE_CONST.items():
                setattr(bt, k, v * f)
            for k, v in BASE4.items():
                setattr(bt4, k, v * f)
            entry.update({"price": round(price, 5), "spread": round(spread, 5),
                          "spread_pct": round(100 * spread / price, 4),
                          "candles": len(m5)})
            print(name, ep, "Kerzen", len(m5), "Spanne", spread, f"({entry['spread_pct']} %)")
            rid = f"multi-{ep}"
            for key, fn in (("round2", bt2.analyze2), ("round5", bt5.analyze5), ("round4", bt4.analyze4)):
                try:
                    entry[key] = fn(m5, rid)
                except Exception as e:
                    traceback.print_exc()
                    entry[key] = {"error": str(e)}
                write(res)
        except Exception as e:
            traceback.print_exc()
            entry["error"] = str(e)
        write(res)
    print("fertig")


main()
