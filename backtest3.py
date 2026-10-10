"""Dritte Testrunde: nur Top-Setups.

Jedes Signal bekommt eine Punktzahl aus bis zu 7 Merkmalen:
Fair Value Gap, Liquiditaets-Sweep, 4H-Trend passt, H1-Trend passt (nicht neutral),
kein Seitwaertsmarkt, Stop mindestens 6 Punkte, Handelszeit 9-21 Uhr (deutsche Zeit).
Zuerst eine Tabelle Punktzahl -> Ergebnis (steigt das Ergebnis mit den Punkten,
ist das ein echtes Zeichen), dann Varianten mit Mindestpunktzahl.
"""
import numpy as np
import pandas as pd

import backtest as bt
import backtest2 as bt2

SETS = [
    ("M5-Zonen", "M5", "normal", "wick", "choch"),
    ("M15-Zonen", "M15", "normal", "wick", "choch"),
    ("M15-Zonen, Limit 50 %", "M15", "normal", "wick", "limit"),
    ("M5-Zonen, starker Impuls", "M5", "stark", "wick", "choch"),
]
MIN_SCORES = (3, 4, 5)


def add_score(t):
    t = t.copy()
    t["fit1s"] = t.trend1 == t.kind
    t["active"] = (t.hour >= 9) & (t.hour < 21)
    t["score"] = (t.fvg.astype(int) + t.sweep.astype(int) + t.fit4.astype(int)
                  + t.fit1s.astype(int) + (~t.side).astype(int) + t.big.astype(int)
                  + t.active.astype(int))
    return t


def analyze3(m5, req_id):
    first = m5.time.iloc[0]
    last = m5.time.iloc[-1]
    eval_start = first + pd.Timedelta(days=bt.WARMUP_DAYS)
    split = eval_start + (last - eval_start) * bt.TRAIN_SHARE
    eval_days = max(1.0, (last - eval_start).total_seconds() / 86400)
    f4 = bt.trend_features(m5, "4h", 4, False)
    f1 = bt.trend_features(m5, "1h", 1, True)

    data = {}
    for label, tf, imp, zt, en in SETS:
        raw = bt2.gen_htf_trades(m5, tf, imp, zt, en)
        if raw.empty:
            continue
        t = bt.prepare(raw, f4, f1)
        t = t[t.entry_time >= eval_start]
        t = t.dropna(subset=["trend4", "trend1", "strength"]).reset_index(drop=True)
        data[label] = add_score(t)

    tables = []
    for label, t in data.items():
        for col, tg in (("r1", "1:1"), ("r2", "1:2")):
            rows = []
            for s in range(0, 8):
                sel = t[t.score == s]
                if len(sel) == 0:
                    continue
                sm = bt.summarize(sel, col)
                rows.append({"score": s, **sm})
            tables.append({"name": f"{label}, Ziel {tg}", "rows": rows})

    out = []
    for label, t in data.items():
        for ms in MIN_SCORES:
            for col, tg in (("r1", "1:1"), ("r2", "1:2")):
                sel = t[t.score >= ms]
                if len(sel) == 0:
                    continue
                out.append({
                    "name": f"{label}, mind. {ms} von 7 Merkmalen, Ziel {tg}",
                    "col": col,
                    "per_day": round(len(sel) / eval_days, 1),
                    "t": bt2.tstat(sel, col),
                    "train": bt.summarize(sel[sel.entry_time < split], col),
                    "test": bt.summarize(sel[sel.entry_time >= split], col),
                    "all": bt.summarize(sel, col),
                })
    ranked = [x for x in out if x["train"]["n"] >= bt.MIN_TRAIN_TRADES]
    rest = [x for x in out if x["train"]["n"] < bt.MIN_TRAIN_TRADES]
    ranked.sort(key=lambda x: x["train"]["avg"], reverse=True)
    return {
        "days": round(eval_days, 1),
        "from": eval_start.isoformat(),
        "to": last.isoformat(),
        "split": split.isoformat(),
        "variants": ranked + rest,
        "tables": tables,
    }
