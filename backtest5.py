"""Fuenfte Testrunde: Ausbruch, Retest, dann Einstieg.

Ablauf: Kurs schliesst ueber das Hoch der letzten N Kerzen (Short: unter das Tief),
in Richtung H1-Trend. Danach wartet man bis zu 12 Kerzen auf den Ruecklauf an das
durchbrochene Niveau (Retest).
- "Bestaetigung": Kerze beruehrt das Niveau und schliesst wieder darueber -> Einstieg danach,
  Stop unter dem Retest-Tief (0,3 ATR Puffer)
- "Limit am Niveau": Kauf direkt am Niveau, Stop 1 ATR dahinter
Faellt der Kurs vorher 0,5 ATR durch das Niveau zurueck, gilt der Ausbruch als gescheitert.
"""
import numpy as np
import pandas as pd

import backtest as bt
import backtest2 as bt2
import backtest4 as bt4

RETEST_BARS = 12
FAIL_ATR = 0.5
SIM = 288


def gen_retest(m5, minutes, look, entry_type):
    d = bt2.resample(m5, minutes)
    prev = d.close.shift(1)
    tr = np.maximum(d.high - d.low, np.maximum((d.high - prev).abs(), (d.low - prev).abs()))
    atr_s = tr.rolling(14).mean()
    hh = d.high.shift(1).rolling(look).max()
    ll = d.low.shift(1).rolling(look).min()
    close_time = d.time + pd.Timedelta(minutes=minutes)
    idx = m5.time.searchsorted(close_time)
    o5, c5, hi5, lo5 = m5.open.values, m5.close.values, m5.high.values, m5.low.values
    times = m5.time.tolist()
    arrs = (o5, hi5, lo5, times)
    dc = d.close.values
    window = int(RETEST_BARS * minutes / 5)
    out = []
    last = {"long": -999, "short": -999}
    for i in range(look + 20, len(d) - 1):
        atr = atr_s.iloc[i]
        if not atr or np.isnan(atr):
            continue
        kind = None
        if dc[i] > hh.iloc[i]:
            kind, level = "long", float(hh.iloc[i])
        elif dc[i] < ll.iloc[i]:
            kind, level = "short", float(ll.iloc[i])
        if kind is None or i - last[kind] < 6:
            continue
        last[kind] = i
        is_long = kind == "long"
        s0 = int(idx[i])
        if s0 >= len(m5) - 3:
            break
        rec = None
        for j in range(s0, min(s0 + window, len(m5) - 2)):
            failed = (c5[j] < level - FAIL_ATR * atr) if is_long else (c5[j] > level + FAIL_ATR * atr)
            touch = (lo5[j] <= level) if is_long else (hi5[j] >= level)
            if entry_type == "limit":
                if touch:
                    price = level
                    stop = level - atr if is_long else level + atr
                    risk = abs(price - stop)
                    if risk >= bt.MIN_RISK:
                        rec = {"kind": kind, "entry_time": times[j], "price": price,
                               "stop": stop, "risk": risk}
                        for mode, col in (("1", "r1"), ("2", "r2")):
                            rec[col] = bt.simulate(kind, price, stop, risk,
                                                   hi5[j:j + SIM], lo5[j:j + SIM], mode, True)
                    break
                if failed:
                    break
            else:
                if failed:
                    break
                holds = (c5[j] > level) if is_long else (c5[j] < level)
                if touch and holds:
                    s = j + 1
                    price = float(o5[s])
                    if is_long:
                        stop = min(lo5[s0:j + 1].min(), level) - 0.3 * atr
                    else:
                        stop = max(hi5[s0:j + 1].max(), level) + 0.3 * atr
                    risk = abs(price - stop)
                    if risk >= bt.MIN_RISK:
                        rec = {"kind": kind, "entry_time": times[s], "price": price,
                               "stop": stop, "risk": risk}
                        for mode, col in (("1", "r1"), ("2", "r2")):
                            rec[col] = bt.simulate(kind, price, stop, risk,
                                                   hi5[s:s + SIM], lo5[s:s + SIM], mode, False)
                    break
        if rec:
            out.append(rec)
    return pd.DataFrame(out)


def analyze5(m5, req_id):
    first, last = m5.time.iloc[0], m5.time.iloc[-1]
    eval_start = first + pd.Timedelta(days=bt.WARMUP_DAYS)
    split = eval_start + (last - eval_start) * bt.TRAIN_SHARE
    eval_days = max(1.0, (last - eval_start).total_seconds() / 86400)
    f1 = bt.trend_features(m5, "1h", 1, True)
    out = []
    for minutes in (15, 5):
        for look in (20, 40):
            for et, etl in (("confirm", "Retest mit Bestätigung"), ("limit", "Limit am Niveau")):
                raw = gen_retest(m5, minutes, look, et)
                if raw.empty:
                    continue
                t = raw.sort_values("entry_time").reset_index(drop=True)
                t = pd.merge_asof(t, f1.rename(columns={"trend": "trend1"}),
                                  left_on="entry_time", right_on="avail", direction="backward")
                t = t[t.entry_time >= eval_start].dropna(subset=["trend1", "strength"])
                t = t[t.trend1 == t.kind].reset_index(drop=True)
                for col, tg in (("r1", "1:1"), ("r2", "1:2")):
                    out.append({
                        "name": f"Ausbruch {look} Kerzen, {etl}, M{minutes}, Ziel {tg}", "col": col,
                        "per_day": round(len(t) / eval_days, 1),
                        "t": bt2.tstat(t, col),
                        "train": bt.summarize(t[t.entry_time < split], col),
                        "test": bt.summarize(t[t.entry_time >= split], col),
                        "all": bt.summarize(t, col),
                    })
    ranked = [x for x in out if x["train"]["n"] >= bt.MIN_TRAIN_TRADES]
    rest = [x for x in out if x["train"]["n"] < bt.MIN_TRAIN_TRADES]
    ranked.sort(key=lambda x: x["train"]["avg"], reverse=True)
    return {"days": round(eval_days, 1), "from": eval_start.isoformat(), "to": last.isoformat(),
            "split": split.isoformat(), "variants": ranked + rest}
