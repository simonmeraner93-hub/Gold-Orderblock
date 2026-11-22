"""Break and Retest - Vorwaertstest der festen Version 2 (ENTWURF, ohne Telegram).

Jeder Lauf:
- erkennt Signale der festen V2-Regel auf den letzten DET_DAYS Tagen M5 (fuenf Maerkte),
  nur Signale, deren Einstiegskerze nach dem Start des Tests begann;
- rechnet alle noch offenen Trades mit den aktuellen Kerzen weiter (gleiche Ausstiegslogik wie V2);
- schreibt docs/break_retest_forward.json (Markierungen, Journal, Statistik) und
  docs/break_retest_forward_state.json (Signale und Zustand, gegen Doppelerfassung).

Keine Auswahl, keine Meldung. Ergebnisse netto nach gemessenem Spread, ohne Slippage und Kommission.
"""
import json
import os
import sys
import time
from datetime import datetime, timezone

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import break_retest as br  # noqa: E402
import break_retest_v2 as v2  # noqa: E402

ROOT = os.path.dirname(os.path.abspath(__file__))
OUT = os.path.join(ROOT, "docs", "break_retest_forward.json")
STATE = os.path.join(ROOT, "docs", "break_retest_forward_state.json")
DET_DAYS = 10       # Erkennung und Fortschreibung
CHART_DAYS = 5      # Kerzen und Signale fuer die Seite
H = br.PARAMS["M5"]["H"]
TZ = v2.TZ
HINWEIS = ("Vorwaertstest der festen Regel V2. Keine Auswahl, keine Meldung, keine Handelsempfehlung. "
           "Ergebnisse netto nach gemessenem Spread, ohne Slippage und Kommission.")


def _iso(ts):
    return datetime.fromtimestamp(int(ts), tz=timezone.utc).isoformat(timespec="minutes")


def load(path, default):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return default


def window_label(t):
    dt = datetime.fromtimestamp(int(t), tz=TZ)
    m = dt.hour * 60 + dt.minute
    if 9 * 60 <= m < 10 * 60:
        return "09:00"
    if 15 * 60 + 30 <= m < 16 * 60 + 30:
        return "15:30"
    return "-"


def evaluate(sig, E):
    """Stand eines Signals anhand der aktuellen Kerzen. Gibt Felder fuer status/R/Ausstieg zurueck."""
    idx = np.flatnonzero(E["t"] == sig["t"])
    if idx.size == 0:
        return {"status": "Daten fehlen"}
    j = int(idx[0])
    res = v2.outcome_v2(E["h"], E["l"], E["c"], j, sig["d"], sig["entry"], sig["stop"], sig["risk"], H)
    if res is None:
        return {"status": "offen", "R": None, "why": None, "exit_t": None, "exit_p": None}
    r_g, x, why = res
    closed = why in ("Stop", "Teil+BE", "Ziel 5R") or (why.startswith("Zeit") and x == j + H)
    exit_p = {"Stop": sig["stop"], "Teil+BE": sig["entry"], "Ziel 5R": sig["tgt2"]}.get(
        why, float(E["c"][x]))
    R_net = r_g - sig["spread"] / sig["risk"]
    return {"status": "geschlossen" if closed else "offen", "R": round(float(R_net), 3), "why": why,
            "exit_t": int(E["t"][x]), "exit_p": round(float(exit_p), 6)}


def run(out=OUT, state_path=STATE, now=None):
    now = time.time() if now is None else now
    state = load(state_path, {"start": None, "sig": {}})
    if state.get("start") is None:
        state["start"] = int(now)
    h = br.login()
    res = {"updated": _iso(now), "start": _iso(state["start"]), "hinweis": HINWEIS,
           "markets": [], "errors": [], "journal": [], "stats": {}}
    chart_from = now - CHART_DAYS * 86400

    for name, klasse, cands in v2.MARKETS:
        m = {"name": name, "klasse": klasse}
        try:
            epic, inst = br.resolve(h, klasse, cands)
            if epic is None:
                raise RuntimeError("Markt nicht gefunden")
            E = br.fetch(h, epic, "M5", DET_DAYS)
            T = br.fetch(h, epic, "H1", 30)
            spread = br.measure_spread(h, epic)
            if not spread or spread <= 0:
                raise RuntimeError("Spread nicht gemessen")
            trend = br.trend_on(E, T)
            p = br.PARAMS["M5"]
            found = v2.simulate_v2(E, trend, p["P"], p["W"], p["H"], spread, v2.CFG["zone"], live=True)
            for x in found:
                if x["t"] < state["start"]:
                    continue
                key = f"{name}|{x['t']}"
                if key in state["sig"]:
                    continue
                state["sig"][key] = {
                    "key": key, "market": name, "t": int(x["t"]), "window": window_label(x["t"]),
                    "d": int(x["d"]), "L": float(x["L"]), "entry": float(x["entry"]),
                    "stop": float(x["stop"]), "tgt": float(x["tgt"]), "tgt2": float(x["tgt2"]),
                    "risk": float(abs(x["entry"] - x["stop"])), "rk": float(x["rk"]),
                    "spread": float(spread), "status": "offen", "R": None, "why": None,
                    "exit_t": None, "exit_p": None,
                }
            # bestehende Signale dieses Marktes fortschreiben
            for key, sig in state["sig"].items():
                if sig["market"] != name or sig["status"] == "geschlossen":
                    continue
                sig.update(evaluate(sig, E))
            # Kerzen und Metadaten fuer die Seite
            mask = E["t"] >= chart_from
            m.update({
                "epic": epic, "instrument": inst or epic, "spread": spread,
                "bars": [[int(t), round(float(o), 6), round(float(hh), 6), round(float(ll), 6), round(float(c), 6)]
                         for t, o, hh, ll, c in zip(E["t"][mask], E["o"][mask], E["h"][mask],
                                                   E["l"][mask], E["c"][mask])],
            })
        except Exception as e:
            m["error"] = f"{type(e).__name__}: {e}"
            res["errors"].append({"name": name, "error": m["error"]})
        res["markets"].append(m)
        time.sleep(2)

    # Signale fuer die Seite (letzte CHART_DAYS Tage), Journal (geschlossen) und Statistik
    allsig = list(state["sig"].values())
    for m in res["markets"]:
        m["signals"] = [s for s in allsig if s["market"] == m["name"] and s["t"] >= chart_from]
    closed = sorted([s for s in allsig if s["status"] == "geschlossen"], key=lambda s: s["t"])
    res["journal"] = closed[-500:]

    def agg(rows):
        rs = np.array([s["R"] for s in rows], dtype=float)
        if rs.size == 0:
            return {"n": 0}
        return {"n": int(rs.size), "win": round(100 * float((rs > 0).mean()), 1),
                "avg": round(float(rs.mean()), 3), "sum": round(float(rs.sum()), 2)}

    res["stats"] = {
        "offen": sum(1 for s in allsig if s["status"] != "geschlossen"),
        "gesamt": agg(closed),
        "je_markt": {mk: agg([s for s in closed if s["market"] == mk]) for mk, _, _ in v2.MARKETS},
        "je_fenster": {w: agg([s for s in closed if s["window"] == w]) for w in ("09:00", "15:30")},
    }
    br.write(res, out)
    br.write(state, state_path)
    print("Signale:", len(allsig), "geschlossen:", len(closed), "Fehler:", len(res["errors"]))
    return res


if __name__ == "__main__":
    run()
