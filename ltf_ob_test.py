"""LTF-Ortstest: H1-Orderblock, darin ein M5-Orderblock, Einstieg auf der Bestaetigung (nur Statistik).

Kein Alarm, kein Telegram. signal_check.py (Live-Bot) bleibt unveraendert.
Je Markt:
1. H1-Orderblock wie im LTF-Test: Block direkt vor dem Impuls, mit FVG, im H1-Trend.
2. Danach ein M5-Orderblock gleicher Richtung, dessen Zone ganz innerhalb der H1-Zone liegt.
3. Auf M5: Antest des M5-Blocks und Bestaetigungskerze (ltf_confirm aus signal_check.py).
4. Nur in Richtung des H1-Trends zum Zeitpunkt der Bestaetigung.
5. Stop an der Kante des M5-Blocks, Ziel 1,5 R. R netto nach gemessenem Spread (Spread/Risiko je Trade).
Pro H1-Block hoechstens ein Trade (der erste passende M5-Block mit Bestaetigung).
"""
import os
import sys
import time
from datetime import datetime, timezone

import pandas as pd

# signal_check liest beim Import die Telegram-Variablen. Hier werden sie nicht benutzt.
os.environ.setdefault("TELEGRAM_BOT_TOKEN", "nicht-benutzt")
os.environ.setdefault("TELEGRAM_CHAT_ID", "nicht-benutzt")

ROOT = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, ROOT)
import signal_check as sc  # noqa: E402
import break_retest as br  # noqa: E402

OUT = os.path.join(ROOT, "docs", "ltf_ob_test.json")
M5_DAYS = 180
H1_DAYS = 540
RR = sc.LTF_RR   # 1,5, wie beim LTF-Test
HINWEIS = ("Test: H1-Orderblock, darin M5-Orderblock, Einstieg auf der Bestaetigung. Nur Statistik, "
           "kein Alarm, kein Telegram. R netto nach gemessenem Spread. Keine Handelsempfehlung.")


def to_df(E):
    """Kerzen aus break_retest.fetch (Arrays) als Tabelle wie im Bot."""
    return pd.DataFrame({
        "time": pd.to_datetime(E["t"], unit="s", utc=True),
        "open": E["o"], "high": E["h"], "low": E["l"], "close": E["c"],
    })


def outcome(m5, j, long_, stop, tgt):
    """Ergebnis ab der Einstiegskerze j. Liegen Stop und Ziel in einer Kerze, zaehlt der Stop (wie im LTF-Test)."""
    ec = m5.iloc[j]
    if (ec["low"] <= stop) if long_ else (ec["high"] >= stop):
        return "stop", -1.0
    for q in range(j + 1, len(m5)):
        c = m5.iloc[q]
        if (c["low"] <= stop) if long_ else (c["high"] >= stop):
            return "stop", -1.0
        if (c["high"] >= tgt) if long_ else (c["low"] <= tgt):
            return "ziel", RR
    return "offen", None


def run_market(h1, m5, spread):
    """Trades und Trichter. Der Trichter zaehlt H1-Bloecke, die bis zu der jeweiligen Stufe kamen.
    Kausal: Kein Blockstatus aus spaeteren Kerzen. Gueltigkeit prueft ltf_confirm nur bis zum Einstieg."""
    fun = {"H1-Block mit Regel": 0, "M5-Block in der H1-Zone": 0, "Bestaetigung auf M5": 0, "Trend passt": 0}
    trades = []
    if len(h1) < 61 or len(m5) < 2:
        return trades, fun
    tr = sc.trend_series(h1)
    h1_t = [int(x.timestamp()) for x in h1.time]
    m5_t = [int(x.timestamp()) for x in m5.time]
    blocks_h1 = sc.find_blocks(h1)
    blocks_m5 = sc.find_blocks(m5, loose=True)   # ohne Statusfilter: "ungueltig" wird aus spaeteren Kerzen bestimmt
    for b in blocks_h1:
        k, i = b["k"], b["i"]
        if k != i - 1 or not b["fvg"] or i + 1 >= len(h1) or i + 1 < 60:
            continue
        long_ = b["kind"] == "long"
        zlo, zhi = float(h1["low"].iloc[k]), float(h1["high"].iloc[k])
        if not zhi - zlo > 0:
            continue
        fun["H1-Block mit Regel"] += 1
        # FVG des H1-Blocks ist erst nach Schluss der Folgekerze i+1 bekannt, also startet die Suche danach
        s = next((q for q, t in enumerate(m5_t) if t >= h1_t[i + 1] + 3600), None)
        if s is None:
            continue
        stufe = 0
        for mb in blocks_m5:
            if mb["kind"] != b["kind"] or mb["k"] < s:
                continue
            mlo, mhi = float(mb["lo"]), float(mb["hi"])
            if not (zlo <= mlo and mhi <= zhi):
                continue
            if stufe < 1:
                fun["M5-Block in der H1-Zone"] += 1
                stufe = 1
            j = sc.ltf_confirm(m5, mb["i"] + 1, mlo, mhi, long_)
            if j is None:
                continue
            if stufe < 2:
                fun["Bestaetigung auf M5"] += 1
                stufe = 2
            if not sc.trend_ok(tr, h1_t, m5_t[j] + 300, long_):
                continue
            price = float(m5["close"].iloc[j])
            stop = mlo if long_ else mhi
            risk = abs(price - stop)
            if not risk > 0:
                continue
            tgt = price + RR * risk if long_ else price - RR * risk
            res, r = outcome(m5, j, long_, stop, tgt)
            kosten = spread / risk   # Spread in Risikoeinheiten, wie in break_retest
            fun["Trend passt"] += 1
            trades.append({"t": m5_t[j], "kind": b["kind"], "h1_block_t": int(b["t"]),
                           "m5_block_t": int(mb["t"]), "entry": price, "stop": stop, "tgt": tgt,
                           "kosten_R": round(kosten, 3), "res": res,
                           "r": None if r is None else round(r - kosten, 3)})
            break
    trades.sort(key=lambda x: x["t"], reverse=True)
    return trades, fun


def agg(trades):
    ziele = sum(1 for t in trades if t["res"] == "ziel")
    stops = sum(1 for t in trades if t["res"] == "stop")
    return {
        "trades": len(trades), "ziele": ziele, "stops": stops, "offen": len(trades) - ziele - stops,
        "gewinnquote": round(100 * ziele / (ziele + stops), 1) if ziele + stops else None,
        "R": round(sum(t["r"] for t in trades if t["r"] is not None), 2),
    }


def main():
    h = br.login()
    res = {"updated": datetime.fromtimestamp(time.time(), tz=timezone.utc).isoformat(timespec="minutes"),
           "hinweis": HINWEIS, "zeitraum": {"M5_tage": M5_DAYS, "H1_tage": H1_DAYS},
           "maerkte": {}, "errors": []}
    alle = []
    for key, name, cands, dec, step in sc.MARKETS:
        try:
            epic, _ = br.resolve(h, "CFD", cands)
            if epic is None:
                raise RuntimeError("Markt nicht gefunden")
            spread = br.measure_spread(h, epic)
            if not spread or spread <= 0:
                raise RuntimeError("Spread nicht gemessen")
            H1 = to_df(br.fetch(h, epic, "H1", H1_DAYS))
            M5 = to_df(br.fetch(h, epic, "M5", M5_DAYS))
            trades, fun = run_market(H1, M5, spread)
            for t in trades:
                t["markt"] = name
            alle += trades
            m = {"kerzen": {"H1": len(H1), "M5": len(M5)}, "trichter": fun, "statistik": agg(trades)}
        except Exception as e:
            m = {"fehler": f"{type(e).__name__}: {e}"}
            res["errors"].append({"markt": name, "fehler": m["fehler"]})
        res["maerkte"][name] = m
        time.sleep(2)
    seit = time.time() - 60 * 86400
    res["statistik"] = agg(alle)
    res["statistik_letzte_60_tage"] = agg([t for t in alle if t["t"] >= seit])
    res["trades"] = sorted(alle, key=lambda x: x["t"], reverse=True)
    br.write(res, OUT)
    print("Trades:", len(alle), "Fehler:", len(res["errors"]))
    return res


if __name__ == "__main__":
    main()
