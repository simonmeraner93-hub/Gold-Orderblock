"""Zweite Testrunde: Zonen aus M5, M15 und H1, Einstieg immer auf M5.

Neu gegenueber Runde 1:
- Zonen koennen aus M15- und H1-Kerzen kommen (groessere Stops, Spanne kostet weniger)
- "stark" = Impuls mindestens 2,5-mal Durchschnitt und Ausbruch ueber 20 Kerzen
- t-Wert: wie weit weg vom Zufall liegt das Ergebnis (bei vielen Varianten ist t > 3 noetig)
"""
import numpy as np
import pandas as pd

import backtest as bt

TFS = {"M5": 5, "M15": 15, "H1": 60}
IMPULSE = {"normal": (1.5, 10), "stark": (2.5, 20)}
ZONE_LIFE = {"M5": 288, "M15": 576, "H1": 1440}
SIM_BARS = 576


def resample(m5, minutes):
    if minutes == 5:
        return m5.copy()
    d = (
        m5.set_index("time")
        .resample(f"{minutes}min")
        .agg({"open": "first", "high": "max", "low": "min", "close": "last"})
        .dropna()
        .reset_index()
    )
    return d.iloc[:-1].reset_index(drop=True)


def find_blocks2(df, strong, swing):
    body = (df.close - df.open).abs().values
    o = df.open.values
    c = df.close.values
    hi = df.high.values
    lo = df.low.values
    found = {}
    for i in range(swing + 20, len(df) - 1):
        avg = body[i - 20:i].mean()
        if body[i] < strong * avg:
            continue
        prev_hi = hi[i - swing:i].max()
        prev_lo = lo[i - swing:i].min()
        kind = None
        if c[i] > o[i] and c[i] > prev_hi:
            kind = "long"
        elif c[i] < o[i] and c[i] < prev_lo:
            kind = "short"
        if kind is None:
            continue
        for k in range(i - 1, max(i - 6, 0), -1):
            ok = (kind == "long" and c[k] < o[k]) or (kind == "short" and c[k] > o[k])
            if ok:
                if k not in found:
                    if kind == "long":
                        fvg = bool(lo[i + 1] > hi[i - 1])
                        sweep = bool(lo[k - 1:i].min() < lo[max(0, k - 21):k - 1].min()) if k > 21 else False
                    else:
                        fvg = bool(hi[i + 1] < lo[i - 1])
                        sweep = bool(hi[k - 1:i].max() > hi[max(0, k - 21):k - 1].max()) if k > 21 else False
                    found[k] = (kind, i, fvg, sweep)
                break
    return [{"k": k, "i": i, "kind": kd, "fvg": f, "sweep": sw}
            for k, (kd, i, f, sw) in found.items()]


def gen_htf_trades(m5, tf, impulse, zone_type, entry_mode):
    minutes = TFS[tf]
    strong, swing = IMPULSE[impulse]
    d = resample(m5, minutes)
    dtimes = d.time.tolist()
    o = m5.open.values
    c = m5.close.values
    hi = m5.high.values
    lo = m5.low.values
    times = m5.time.tolist()
    mtimes = m5.time
    life = ZONE_LIFE[tf]
    trades = []
    for b in find_blocks2(d, strong, swing):
        k, i, kind = b["k"], b["i"], b["kind"]
        is_long = kind == "long"
        ko, kc = float(d.open.iloc[k]), float(d.close.iloc[k])
        if zone_type == "body":
            zlo, zhi = min(ko, kc), max(ko, kc)
        else:
            zlo, zhi = float(d.low.iloc[k]), float(d.high.iloc[k])
        if zhi - zlo < 0.05:
            continue
        avail = dtimes[i + 1] + pd.Timedelta(minutes=minutes)
        s = int(mtimes.searchsorted(avail))
        if s >= len(m5) - 2:
            continue
        end_scan = min(len(m5), s + life)

        entry_idx = None
        price = None
        first_only = False
        if entry_mode == "choch":
            touch = None
            for j in range(s, end_scan):
                inv = (c[j] < zlo) if is_long else (c[j] > zhi)
                tch = (lo[j] <= zhi) if is_long else (hi[j] >= zlo)
                if inv:
                    break
                if tch:
                    touch = j
                    break
            if touch is None:
                continue
            ref = hi[touch] if is_long else lo[touch]
            for j in range(touch + 1, min(touch + 1 + bt.CHOCH_BARS, len(m5))):
                broke = (c[j] < zlo) if is_long else (c[j] > zhi)
                if broke:
                    break
                if (c[j] > ref) if is_long else (c[j] < ref):
                    entry_idx = j
                    break
            if entry_idx is None:
                continue
            price = float(c[entry_idx])
            seg_start = entry_idx + 1
            entry_time = times[entry_idx] + pd.Timedelta(minutes=5)
        else:
            mid = (zlo + zhi) / 2
            fill = None
            for j in range(s, end_scan):
                if (lo[j] <= mid) if is_long else (hi[j] >= mid):
                    fill = j
                    break
                if (c[j] < zlo) if is_long else (c[j] > zhi):
                    break
            if fill is None:
                continue
            price = float(mid)
            seg_start = fill
            first_only = True
            entry_time = times[fill]

        stop = zlo - bt.STOP_PUFFER if is_long else zhi + bt.STOP_PUFFER
        risk = abs(price - stop)
        if risk < bt.MIN_RISK:
            continue
        seg_h = hi[seg_start:seg_start + SIM_BARS]
        seg_l = lo[seg_start:seg_start + SIM_BARS]
        rec = {"kind": kind, "entry_time": entry_time, "price": price, "stop": stop,
               "risk": risk, "fvg": b["fvg"], "sweep": b["sweep"]}
        for mode, col in (("1", "r1"), ("1.5", "r15"), ("2", "r2"), ("be", "rbe")):
            rec[col] = bt.simulate(kind, price, stop, risk, seg_h, seg_l, mode, first_only)
        trades.append(rec)
    return pd.DataFrame(trades)


def tstat(df, col):
    done = df[df[col].notna()]
    if len(done) < 8:
        return 0.0
    x = done[col].astype(float) - bt.SPREAD / done.risk.astype(float)
    sd = x.std(ddof=1)
    if not sd or np.isnan(sd):
        return 0.0
    return round(float(x.mean() / (sd / np.sqrt(len(x)))), 2)


def analyze2(m5, req_id):
    first = m5.time.iloc[0]
    last = m5.time.iloc[-1]
    eval_start = first + pd.Timedelta(days=bt.WARMUP_DAYS)
    split = eval_start + (last - eval_start) * bt.TRAIN_SHARE
    eval_days = max(1.0, (last - eval_start).total_seconds() / 86400)
    f4 = bt.trend_features(m5, "4h", 4, False)
    f1 = bt.trend_features(m5, "1h", 1, True)

    cache = {}

    def data(tf, imp, zt, en):
        key = (tf, imp, zt, en)
        if key not in cache:
            raw = gen_htf_trades(m5, tf, imp, zt, en)
            if raw.empty:
                cache[key] = None
            else:
                t = bt.prepare(raw, f4, f1)
                t = t[t.entry_time >= eval_start]
                t = t.dropna(subset=["trend4", "trend1", "strength"]).reset_index(drop=True)
                cache[key] = t
        return cache[key]

    variants = []
    for tf in ("M5", "M15", "H1"):
        for imp in ("normal", "stark"):
            tag = f"{tf}-Zonen, {imp}er Impuls" if imp == "normal" else f"{tf}-Zonen, starker Impuls"
            variants.append((tag + ", Ziel 1:1", tf, imp, "wick", "choch", None, "r1"))
            variants.append((tag + ", Ziel 1:2", tf, imp, "wick", "choch", None, "r2"))
            variants.append((tag + ", Ziel 1:2 + Fair Value Gap + 4H-Trend", tf, imp, "wick", "choch",
                             lambda t: t.fvg & t.fit4, "r2"))
    for tf in ("M15", "H1"):
        variants.append((f"{tf}-Zonen, normaler Impuls, Limit 50 %, Ziel 1:2", tf, "normal", "wick", "limit", None, "r2"))
        variants.append((f"{tf}-Zonen, starker Impuls, Limit 50 %, Ziel 1:2", tf, "stark", "wick", "limit", None, "r2"))

    out = []
    for name, tf, imp, zt, en, mask, col in variants:
        t = data(tf, imp, zt, en)
        if t is None:
            continue
        m = mask(t) if mask is not None else pd.Series(True, index=t.index)
        sel = t[m]
        out.append({
            "name": name, "col": col, "tf": tf,
            "per_day": round(len(sel) / eval_days, 1),
            "t": tstat(sel, col),
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
    }
