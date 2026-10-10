"""Rueckblickender Test der Orderblock-Regel und mehrerer Varianten.

Alles wird auf M5-Kerzen gerechnet. Es zaehlt der Vergleich der Varianten
untereinander, nicht die exakte Uebereinstimmung mit den Live-Signalen.
Die ersten WARMUP_DAYS Tage dienen nur dem Einschwingen der Trendlinien.
"""
import time
from datetime import datetime, timedelta, timezone

import numpy as np
import pandas as pd
import requests

BASE = "https://demo-api-capital.backend-capital.com/api/v1"
EPIC = "GOLD"
DAYS = 40
WARMUP_DAYS = 10
TRAIN_SHARE = 0.75
STRONG = 1.5
SWING = 10
STOP_PUFFER = 0.5
SPREAD = 0.6
MIN_RISK = 1.0
MAX_BARS = 288
CHOCH_BARS = 12
LIMIT_BARS = 288
MIN_STOP = 6.0
SIDEWAYS = 0.5
MIN_TRAIN_TRADES = 20
HOUR_BLOCKS = [("00-06", 0, 6), ("06-10", 6, 10), ("10-14", 10, 14),
               ("14-18", 14, 18), ("18-24", 18, 24)]


def fetch_m5(h, start, end):
    rows = {}
    cur = start
    while cur < end:
        nxt = min(cur + timedelta(days=3), end)
        params = {
            "resolution": "MINUTE_5",
            "from": cur.strftime("%Y-%m-%dT%H:%M:%S"),
            "to": nxt.strftime("%Y-%m-%dT%H:%M:%S"),
            "max": 1000,
        }
        for attempt in range(4):
            r = requests.get(f"{BASE}/prices/{EPIC}", headers=h, params=params, timeout=30)
            if r.status_code == 429:
                time.sleep(3 * (attempt + 1))
                continue
            break
        r.raise_for_status()
        for p in r.json().get("prices", []):
            t = pd.to_datetime(p["snapshotTimeUTC"]).tz_localize("UTC")
            mid = lambda k: (p[k]["bid"] + p[k]["ask"]) / 2
            rows[t] = (mid("openPrice"), mid("highPrice"), mid("lowPrice"), mid("closePrice"))
        cur = nxt
        time.sleep(0.4)
    if len(rows) < 1500:
        raise RuntimeError(f"zu wenige Kerzen erhalten: {len(rows)}")
    df = pd.DataFrame(
        [(t,) + v for t, v in sorted(rows.items())],
        columns=["time", "open", "high", "low", "close"],
    )
    return df.iloc[:-1].reset_index(drop=True)


def trend_features(m5, rule, hours, with_strength):
    d = (
        m5.set_index("time")
        .resample(rule)
        .agg({"open": "first", "high": "max", "low": "min", "close": "last"})
        .dropna()
        .reset_index()
    )
    d["ema20"] = d.close.ewm(span=20, adjust=False).mean()
    d["ema50"] = d.close.ewm(span=50, adjust=False).mean()
    up = (d.ema20 > d.ema50) & (d.close > d.ema50)
    down = (d.ema20 < d.ema50) & (d.close < d.ema50)
    d["trend"] = np.where(up, "long", np.where(down, "short", "neutral"))
    d["avail"] = d.time + pd.Timedelta(hours=hours)
    cols = ["avail", "trend"]
    if with_strength:
        prev = d.close.shift(1)
        tr = np.maximum(d.high - d.low,
                        np.maximum((d.high - prev).abs(), (d.low - prev).abs()))
        d["atr"] = tr.rolling(14).mean()
        d["strength"] = (d.ema20 - d.ema50).abs() / d.atr
        cols.append("strength")
    return d[cols].dropna().reset_index(drop=True)


def find_blocks(df):
    body = (df.close - df.open).abs().values
    o = df.open.values
    c = df.close.values
    hi = df.high.values
    lo = df.low.values
    found = {}
    for i in range(SWING + 20, len(df) - 1):
        avg = body[i - 20:i].mean()
        if body[i] < STRONG * avg:
            continue
        prev_hi = hi[i - SWING:i].max()
        prev_lo = lo[i - SWING:i].min()
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
    result = []
    for k, (kind, i, fvg, sweep) in found.items():
        result.append({"k": k, "i": i, "kind": kind, "fvg": fvg, "sweep": sweep})
    return result


def simulate(kind, price, stop, risk, highs, lows, mode, first_stop_only=False):
    tr = {"1": 1.0, "1.5": 1.5, "2": 2.0, "be": 1.0}[mode]
    is_long = kind == "long"
    target = price + risk * tr if is_long else price - risk * tr
    be_level = price + 0.5 * risk if is_long else price - 0.5 * risk
    cur_stop = stop
    be_active = False
    for n, (hi, lo) in enumerate(zip(highs, lows)):
        stop_hit = (lo <= cur_stop) if is_long else (hi >= cur_stop)
        if stop_hit:
            return 0.0 if be_active else -1.0
        if n == 0 and first_stop_only:
            continue
        tgt_hit = (hi >= target) if is_long else (lo <= target)
        if tgt_hit:
            return tr
        if mode == "be" and not be_active:
            if (is_long and hi >= be_level) or (not is_long and lo <= be_level):
                be_active = True
                cur_stop = price
    return None


def gen_trades(m5, zone_type, entry_mode):
    o = m5.open.values
    c = m5.close.values
    hi = m5.high.values
    lo = m5.low.values
    times = m5.time.tolist()
    trades = []
    for b in find_blocks(m5):
        k, i, kind = b["k"], b["i"], b["kind"]
        is_long = kind == "long"
        if zone_type == "body":
            zlo, zhi = float(min(o[k], c[k])), float(max(o[k], c[k]))
        else:
            zlo, zhi = float(lo[k]), float(hi[k])
        if zhi - zlo < 0.05:
            continue

        if entry_mode == "choch":
            touch = None
            for j in range(i + 2, len(m5)):
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
            entry_idx = None
            for j in range(touch + 1, min(touch + 1 + CHOCH_BARS, len(m5))):
                broke = (c[j] < zlo) if is_long else (c[j] > zhi)
                if broke:
                    break
                choch = (c[j] > ref) if is_long else (c[j] < ref)
                if choch:
                    entry_idx = j
                    break
            if entry_idx is None:
                continue
            price = float(c[entry_idx])
            seg_start = entry_idx + 1
            first_only = False
            entry_time = times[entry_idx] + pd.Timedelta(minutes=5)
        else:
            mid = (zlo + zhi) / 2
            fill = None
            for j in range(i + 2, min(i + 2 + LIMIT_BARS, len(m5))):
                hit = (lo[j] <= mid) if is_long else (hi[j] >= mid)
                if hit:
                    fill = j
                    break
                inv = (c[j] < zlo) if is_long else (c[j] > zhi)
                if inv:
                    break
            if fill is None:
                continue
            price = float(mid)
            seg_start = fill
            first_only = True
            entry_time = times[fill]
            entry_idx = fill

        stop = zlo - STOP_PUFFER if is_long else zhi + STOP_PUFFER
        risk = abs(price - stop)
        if risk < MIN_RISK:
            continue
        seg_h = hi[seg_start:seg_start + MAX_BARS]
        seg_l = lo[seg_start:seg_start + MAX_BARS]
        rec = {
            "kind": kind, "entry_time": entry_time, "price": price,
            "stop": stop, "risk": risk, "fvg": b["fvg"], "sweep": b["sweep"],
        }
        for mode, col in (("1", "r1"), ("1.5", "r15"), ("2", "r2"), ("be", "rbe")):
            rec[col] = simulate(kind, price, stop, risk, seg_h, seg_l, mode, first_only)
        trades.append(rec)
    return pd.DataFrame(trades)


def prepare(trades, f4, f1):
    t = trades.sort_values("entry_time").reset_index(drop=True)
    t = pd.merge_asof(t, f4.rename(columns={"trend": "trend4"})[["avail", "trend4"]],
                      left_on="entry_time", right_on="avail", direction="backward")
    t = t.drop(columns=["avail"])
    t = pd.merge_asof(t, f1.rename(columns={"trend": "trend1"}),
                      left_on="entry_time", right_on="avail", direction="backward")
    t = t.drop(columns=["avail"])
    t["fit4"] = t.trend4 == t.kind
    t["fit1"] = (t.trend1 == "neutral") | (t.trend1 == t.kind)
    t["side"] = t.strength < SIDEWAYS
    t["big"] = t.risk >= MIN_STOP
    t["hour"] = t.entry_time.dt.tz_convert("Europe/Berlin").dt.hour
    return t


def summarize(df, col):
    done = df[df[col].notna()]
    r = done[col].astype(float)
    cost = (SPREAD / done.risk.astype(float)).sum() if len(done) else 0.0
    net_cost = float(r.sum() - cost)
    return {
        "n": int(len(done)),
        "w": int((r > 0).sum()),
        "l": int((r < 0).sum()),
        "be": int((r == 0).sum()),
        "open": int(len(df) - len(done)),
        "net": round(float(r.sum()), 2),
        "net_cost": round(net_cost, 2),
        "avg": round(net_cost / len(done), 3) if len(done) else 0.0,
    }


def load(h):
    end = datetime.now(timezone.utc).replace(second=0, microsecond=0)
    start = (end - timedelta(days=DAYS)).replace(hour=0, minute=0)
    return fetch_m5(h, start, end)


def run(h, req_id):
    return analyze(load(h), req_id)


def analyze(m5, req_id):
    first = m5.time.iloc[0]
    last = m5.time.iloc[-1]
    eval_start = first + pd.Timedelta(days=WARMUP_DAYS)
    split = eval_start + (last - eval_start) * TRAIN_SHARE
    eval_days = max(1.0, (last - eval_start).total_seconds() / 86400)

    f4 = trend_features(m5, "4h", 4, False)
    f1 = trend_features(m5, "1h", 1, True)

    sets = {}
    for zt in ("wick", "body"):
        for en in ("choch", "limit"):
            raw = gen_trades(m5, zt, en)
            if raw.empty:
                continue
            t = prepare(raw, f4, f1)
            t = t[t.entry_time >= eval_start].reset_index(drop=True)
            t = t.dropna(subset=["trend4", "trend1", "strength"]).reset_index(drop=True)
            sets[(zt, en)] = t
    if ("wick", "choch") not in sets:
        raise RuntimeError("keine Signale im Zeitraum gefunden")

    def v(name, zt, en, mask, col):
        return (name, zt, en, mask, col)

    variants = [
        v("Basis (Docht-Zone, Einstieg nach Strukturbruch, Ziel 1:1)", "wick", "choch", None, "r1"),
        v("Basis, Ziel 1:1,5", "wick", "choch", None, "r15"),
        v("Basis, Ziel 1:2", "wick", "choch", None, "r2"),
        v("Basis, Breakeven bei halbem Weg", "wick", "choch", None, "rbe"),
        v("Basis + Fair Value Gap + H1-Trend", "wick", "choch", lambda t: t.fvg & t.fit1, "r1"),
        v("Basis + kein Seitwärtsmarkt", "wick", "choch", lambda t: ~t.side, "r1"),
        v("Basis + Mindest-Stop 6 Punkte", "wick", "choch", lambda t: t.big, "r1"),
        v("Körper-Zone, Einstieg nach Strukturbruch, Ziel 1:1", "body", "choch", None, "r1"),
        v("Körper-Zone, Einstieg nach Strukturbruch, Ziel 1:2", "body", "choch", None, "r2"),
        v("Körper-Zone, Limit bei 50 %, Ziel 1:1", "body", "limit", None, "r1"),
        v("Körper-Zone, Limit bei 50 %, Ziel 1:2", "body", "limit", None, "r2"),
        v("Docht-Zone, Limit bei 50 %, Ziel 1:2", "wick", "limit", None, "r2"),
        v("Körper, Limit 50 %, 1:2 + Fair Value Gap", "body", "limit", lambda t: t.fvg, "r2"),
        v("Körper, Limit 50 %, 1:2 + 4H-Trend", "body", "limit", lambda t: t.fit4, "r2"),
        v("Körper, Limit 50 %, 1:2 + Liquiditäts-Sweep", "body", "limit", lambda t: t.sweep, "r2"),
        v("Körper, Limit 50 %, 1:2 + Fair Value Gap + Sweep", "body", "limit",
          lambda t: t.fvg & t.sweep, "r2"),
        v("Körper, Limit 50 %, 1:2 + Fair Value Gap + 4H-Trend", "body", "limit",
          lambda t: t.fvg & t.fit4, "r2"),
        v("Körper, Limit 50 %, 1:2 + Fair Value Gap + Sweep + 4H-Trend", "body", "limit",
          lambda t: t.fvg & t.sweep & t.fit4, "r2"),
        v("Körper, Limit 50 %, 1:1 + Fair Value Gap + Sweep + 4H-Trend", "body", "limit",
          lambda t: t.fvg & t.sweep & t.fit4, "r1"),
        v("Körper, Limit 50 %, Breakeven + Fair Value Gap + 4H-Trend", "body", "limit",
          lambda t: t.fvg & t.fit4, "rbe"),
        v("Körper, Strukturbruch, 1:2 + Fair Value Gap + Sweep + 4H-Trend", "body", "choch",
          lambda t: t.fvg & t.sweep & t.fit4, "r2"),
    ]

    out = []
    for name, zt, en, mask, col in variants:
        t = sets.get((zt, en))
        if t is None:
            continue
        m = mask(t) if mask is not None else pd.Series(True, index=t.index)
        sel = t[m]
        tr = sel[sel.entry_time < split]
        te = sel[sel.entry_time >= split]
        out.append({
            "name": name, "zt": zt, "en": en, "col": col,
            "per_day": round(len(sel) / eval_days, 1),
            "train": summarize(tr, col),
            "test": summarize(te, col),
            "all": summarize(sel, col),
        })
    ranked = [x for x in out if x["train"]["n"] >= MIN_TRAIN_TRADES]
    rest = [x for x in out if x["train"]["n"] < MIN_TRAIN_TRADES]
    ranked.sort(key=lambda x: x["train"]["avg"], reverse=True)
    out = ranked + rest

    def hours_for(item):
        t = sets[(item["zt"], item["en"])]
        mask = next(mk for nm, z, e, mk, cl in variants if nm == item["name"])
        sel = t[mask(t)] if mask is not None else t
        blocks = {}
        for label, a, b in HOUR_BLOCKS:
            part = sel[(sel.hour >= a) & (sel.hour < b)]
            blocks[label] = summarize(part, item["col"])
        return {"name": item["name"], "blocks": blocks}

    hours = []
    if out:
        base = next((x for x in out if x["name"].startswith("Basis (")), out[0])
        hours.append(hours_for(base))
        if out[0]["name"] != base["name"]:
            hours.append(hours_for(out[0]))

    return {
        "id": req_id,
        "created": datetime.now(timezone.utc).isoformat(),
        "from": eval_start.isoformat(),
        "to": last.isoformat(),
        "split": split.isoformat(),
        "days": round(eval_days, 1),
        "spread": SPREAD,
        "min_train": MIN_TRAIN_TRADES,
        "variants": out,
        "hours": hours,
    }
