"""Vierte Testrunde: andere Strategien, die den ganzen Tag Signale geben.

A) Pullback im H1-Trend: Kerze beruehrt die EMA20, schliesst darueber/darunter in Trendrichtung
B) Ausbruch aus den letzten 20 Kerzen in Richtung H1-Trend
C) Rueckkehr zur Mitte: Kurs ausserhalb des Bollinger-Bands und schliesst wieder drin (nur Seitwaertsmarkt)
D) Asien-Spanne (1-8 Uhr deutsche Zeit) wird ab 8 Uhr durchbrochen
E) London-Spanne (8-13 Uhr deutsche Zeit) wird am Nachmittag durchbrochen
Stop bei A-C: 1,5 x ATR. Ziele 1:1 und 1:2. Gleiche Regeln fuer Training/Test wie zuvor.
"""
import numpy as np
import pandas as pd

import backtest as bt
import backtest2 as bt2

ATR_MULT = 1.5
COOLDOWN = 6
SIM = 288
RANGE_MIN_RISK = 3.0
RANGE_MAX_RISK = 40.0


def _frame(m5, minutes):
    d = bt2.resample(m5, minutes)
    prev = d.close.shift(1)
    tr = np.maximum(d.high - d.low, np.maximum((d.high - prev).abs(), (d.low - prev).abs()))
    d["atr"] = tr.rolling(14).mean()
    d["ema20"] = d.close.ewm(span=20, adjust=False).mean()
    d["hh"] = d.high.shift(1).rolling(20).max()
    d["ll"] = d.low.shift(1).rolling(20).min()
    d["mid"] = d.close.rolling(20).mean()
    sd = d.close.rolling(20).std()
    d["up"] = d["mid"] + 2 * sd
    d["dn"] = d["mid"] - 2 * sd
    d["close_time"] = d.time + pd.Timedelta(minutes=minutes)
    return d


def _trade(m5arrs, s, kind, price, stop):
    o, hi, lo, times = m5arrs
    risk = abs(price - stop)
    if risk < bt.MIN_RISK:
        return None
    seg_h = hi[s:s + SIM]
    seg_l = lo[s:s + SIM]
    rec = {"kind": kind, "entry_time": times[s], "price": price, "stop": stop, "risk": risk}
    for mode, col in (("1", "r1"), ("2", "r2")):
        rec[col] = bt.simulate(kind, price, stop, risk, seg_h, seg_l, mode, False)
    return rec


def gen_bar_strategy(m5, strat, minutes):
    d = _frame(m5, minutes)
    o5, hi5, lo5 = m5.open.values, m5.high.values, m5.low.values
    arrs = (o5, hi5, lo5, m5.time.tolist())
    ct = d.close_time
    idx = m5.time.searchsorted(ct)
    o, c, hi, lo = d.open.values, d.close.values, d.high.values, d.low.values
    out = []
    last = {"long": -999, "short": -999}
    for i in range(60, len(d) - 1):
        s = int(idx[i])
        if s >= len(m5) - 2:
            break
        atr = d.atr.iloc[i]
        if not atr or np.isnan(atr):
            continue
        kind = None
        if strat == "pullback":
            e = d.ema20.iloc[i]
            if lo[i] <= e and c[i] > e and c[i] > o[i]:
                kind = "long"
            elif hi[i] >= e and c[i] < e and c[i] < o[i]:
                kind = "short"
        elif strat == "breakout":
            if c[i] > d.hh.iloc[i]:
                kind = "long"
            elif c[i] < d.ll.iloc[i]:
                kind = "short"
        elif strat == "reversion":
            if lo[i] < d.dn.iloc[i] and c[i] > d.dn.iloc[i] and c[i] > o[i]:
                kind = "long"
            elif hi[i] > d.up.iloc[i] and c[i] < d.up.iloc[i] and c[i] < o[i]:
                kind = "short"
        if kind is None or i - last[kind] < COOLDOWN:
            continue
        last[kind] = i
        price = float(o5[s])
        stop = price - ATR_MULT * atr if kind == "long" else price + ATR_MULT * atr
        rec = _trade(arrs, s, kind, price, stop)
        if rec:
            out.append(rec)
    return pd.DataFrame(out)


def gen_range_strategy(m5, range_hours, scan_hours):
    o5, c5, hi5, lo5 = m5.open.values, m5.close.values, m5.high.values, m5.low.values
    arrs = (o5, hi5, lo5, m5.time.tolist())
    # Berlin-Zeit ungefaehr UTC+1 bis +2; hier feste Fenster in UTC (Sommer: 1-8 Uhr = 23-6 UTC)
    utc = m5.time.dt
    hour = utc.hour + utc.minute / 60
    day = utc.normalize()
    out = []
    for dt in day.unique():
        in_day = (day == dt).values
        rm = in_day & (hour >= range_hours[0]).values & (hour < range_hours[1]).values
        if rm.sum() < 0.8 * (range_hours[1] - range_hours[0]) * 12:
            continue
        rhi, rlo = hi5[rm].max(), lo5[rm].min()
        sm = np.where(in_day & (hour >= scan_hours[0]).values & (hour < scan_hours[1]).values)[0]
        for j in sm:
            kind = None
            if c5[j] > rhi:
                kind = "long"
            elif c5[j] < rlo:
                kind = "short"
            if kind is None:
                continue
            s = j + 1
            if s >= len(m5) - 2:
                break
            price = float(o5[s])
            stop = rlo - bt.STOP_PUFFER if kind == "long" else rhi + bt.STOP_PUFFER
            risk = abs(price - stop)
            if RANGE_MIN_RISK <= risk <= RANGE_MAX_RISK:
                rec = _trade(arrs, s, kind, price, stop)
                if rec:
                    out.append(rec)
            break
    return pd.DataFrame(out)


def analyze4(m5, req_id):
    first, last = m5.time.iloc[0], m5.time.iloc[-1]
    eval_start = first + pd.Timedelta(days=bt.WARMUP_DAYS)
    split = eval_start + (last - eval_start) * bt.TRAIN_SHARE
    eval_days = max(1.0, (last - eval_start).total_seconds() / 86400)
    f1 = bt.trend_features(m5, "1h", 1, True)

    sets = []
    for tf in (5, 15):
        for strat, label in (("pullback", "Pullback zur EMA20 im H1-Trend"),
                             ("breakout", "Ausbruch aus 20 Kerzen im H1-Trend"),
                             ("reversion", "Rückkehr zur Mitte im Seitwärtsmarkt")):
            sets.append((f"{label}, M{tf}", strat, lambda s=strat, t=tf: gen_bar_strategy(m5, s, t)))
    sets.append(("Asien-Spanne, Ausbruch ab 8 Uhr", "range",
                 lambda: gen_range_strategy(m5, (0, 7), (7, 12))))
    sets.append(("London-Spanne, Ausbruch am Nachmittag", "range",
                 lambda: gen_range_strategy(m5, (7, 12), (12.5, 17))))

    out = []
    for label, strat, fn in sets:
        raw = fn()
        if raw.empty:
            continue
        t = raw.sort_values("entry_time").reset_index(drop=True)
        t = pd.merge_asof(t, f1.rename(columns={"trend": "trend1"}),
                          left_on="entry_time", right_on="avail", direction="backward")
        t = t[t.entry_time >= eval_start].dropna(subset=["trend1", "strength"]).reset_index(drop=True)
        if strat in ("pullback", "breakout"):
            t = t[t.trend1 == t.kind]
        elif strat == "reversion":
            t = t[t.strength < bt.SIDEWAYS]
        for col, tg in (("r1", "1:1"), ("r2", "1:2")):
            out.append({
                "name": f"{label}, Ziel {tg}", "col": col,
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
