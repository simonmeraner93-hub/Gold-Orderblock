"""
Gold Orderblock Bot (capital.com Demo + Telegram)
- Findet Orderblöcke im M5-Chart
- Meldet: neue Zone, erste Berührung, CHoCH-Bestätigung (M1), Zone ungültig
- Speichert den Zustand in state.json (verhindert Doppelmeldungen)
"""
import os
import json
from datetime import datetime, timedelta, timezone

import requests

# ---------------- EINSTELLUNGEN ----------------
EPIC = "GOLD"
BASE_URL = "https://demo-api-capital.backend-capital.com/api/v1"
STATE_FILE = "state.json"

# Schalter für Alarmarten (True = an, False = aus)
ALERT_NEW_ZONE = True
ALERT_TOUCH = True
ALERT_CONFIRM = True
ALERT_INVALID = True

TREND_FILTER = True        # nur Zonen in Richtung des M5-Trends (Kurs über/unter EMA50)
EMA_PERIOD = 50
ATR_PERIOD = 14
IMPULSE_ATR = 1.2          # wie stark muss die Bewegung nach dem Orderblock sein
LOOKBACK_CANDLES = 150     # so weit zurück sucht der Bot Zonen (M5-Kerzen)
RECENT_MINUTES = 20        # "neue Zone" nur melden, wenn sie so frisch ist
EVENT_MAX_AGE_MIN = 30     # ältere Ereignisse werden nicht mehr gemeldet
CONFIRM_WINDOW_MIN = 60    # so lange nach Berührung wird auf CHoCH gewartet
STOP_BUFFER = 0.5          # Stop-Vorschlag: so viele Dollar hinter der Zone
# -----------------------------------------------

UTC = timezone.utc


def log(msg):
    print(msg, flush=True)


# ---------- capital.com ----------
def login():
    r = requests.post(
        f"{BASE_URL}/session",
        headers={"X-CAP-API-KEY": os.environ["CAPITAL_API_KEY"]},
        json={
            "identifier": os.environ["CAPITAL_IDENTIFIER"],
            "password": os.environ["CAPITAL_PASSWORD"],
            "encryptedPassword": False,
        },
        timeout=20,
    )
    r.raise_for_status()
    return {
        "X-SECURITY-TOKEN": r.headers["X-SECURITY-TOKEN"],
        "CST": r.headers["CST"],
    }


def get_candles(headers, resolution, count):
    r = requests.get(
        f"{BASE_URL}/prices/{EPIC}",
        headers=headers,
        params={"resolution": resolution, "max": count},
        timeout=20,
    )
    r.raise_for_status()
    out = []
    for p in r.json()["prices"]:
        def mid(key):
            return (p[key]["bid"] + p[key]["ask"]) / 2
        t = datetime.fromisoformat(p["snapshotTimeUTC"]).replace(tzinfo=UTC)
        out.append({
            "t": t,
            "o": mid("openPrice"),
            "h": mid("highPrice"),
            "l": mid("lowPrice"),
            "c": mid("closePrice"),
        })
    out.sort(key=lambda x: x["t"])
    return out[:-1]  # letzte (noch laufende) Kerze weglassen


# ---------- Indikatoren ----------
def ema(values, period):
    k = 2 / (period + 1)
    e = values[0]
    for v in values[1:]:
        e = v * k + e * (1 - k)
    return e


def atr_at(candles, i, period=ATR_PERIOD):
    start = max(1, i - period + 1)
    trs = []
    for j in range(start, i + 1):
        c, prev = candles[j], candles[j - 1]
        trs.append(max(c["h"] - c["l"], abs(c["h"] - prev["c"]), abs(c["l"] - prev["c"])))
    return sum(trs) / len(trs) if trs else 0


# ---------- Orderblock-Erkennung (M5) ----------
def detect_zones(m5):
    n = len(m5)
    first = max(ATR_PERIOD + 1, n - LOOKBACK_CANDLES)
    found = []
    for i in range(first, n - 1):
        c = m5[i]
        atr = atr_at(m5, i)
        if atr <= 0:
            continue
        # Long-Orderblock: letzte rote Kerze vor starkem Aufwärtsimpuls
        if c["c"] < c["o"]:
            for j in range(i + 1, min(i + 4, n)):
                if m5[j]["c"] <= m5[j]["o"]:
                    break
                if m5[j]["c"] > c["h"]:
                    if m5[j]["c"] - c["l"] >= IMPULSE_ATR * atr:
                        found.append({"type": "long", "lo": c["l"], "hi": c["h"],
                                      "formed": m5[j]["t"]})
                    break
        # Short-Orderblock: letzte grüne Kerze vor starkem Abwärtsimpuls
        if c["c"] > c["o"]:
            for j in range(i + 1, min(i + 4, n)):
                if m5[j]["c"] >= m5[j]["o"]:
                    break
                if m5[j]["c"] < c["l"]:
                    if c["h"] - m5[j]["c"] >= IMPULSE_ATR * atr:
                        found.append({"type": "short", "lo": c["l"], "hi": c["h"],
                                      "formed": m5[j]["t"]})
                    break
    # Überlappende Zonen gleicher Richtung zusammenfassen (weniger Rauschen)
    accepted = []
    for z in found:
        dup = any(a["type"] == z["type"] and z["lo"] <= a["hi"] and z["hi"] >= a["lo"]
                  for a in accepted)
        if not dup:
            accepted.append(z)
    return accepted


# ---------- CHoCH-Bestätigung (M1) ----------
def find_choch(is_long, z, touch_time, m1):
    pre = [c for c in m1 if touch_time - timedelta(minutes=10) <= c["t"] < touch_time]
    after = [c for c in m1 if c["t"] >= touch_time]
    if not after:
        return None
    window = list(pre)
    extreme = None
    ref = None
    for c in after:
        window.append(c)
        if is_long:
            if extreme is None or c["l"] < extreme:
                extreme = c["l"]
                ref = max(x["h"] for x in window)  # letztes kleines Hoch
            elif extreme <= z["hi"] and c["c"] > ref and c["c"] >= z["lo"]:
                return c["t"]
        else:
            if extreme is None or c["h"] > extreme:
                extreme = c["h"]
                ref = min(x["l"] for x in window)  # letztes kleines Tief
            elif extreme >= z["lo"] and c["c"] < ref and c["c"] <= z["hi"]:
                return c["t"]
    return None


# ---------- Telegram ----------
def send_telegram(text):
    r = requests.post(
        f"https://api.telegram.org/bot{os.environ['TELEGRAM_BOT_TOKEN']}/sendMessage",
        json={"chat_id": os.environ["TELEGRAM_CHAT_ID"], "text": text},
        timeout=20,
    )
    if not r.ok:
        log(f"Telegram-Fehler: {r.status_code} {r.text}")


# ---------- Zustand ----------
def load_state():
    try:
        with open(STATE_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {"zones": {}}


def save_state(state):
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(state, f, indent=1)


def fmt(x):
    return f"{x:.2f}"


def zone_text(z):
    return f"Zone: {fmt(z['lo'])} - {fmt(z['hi'])}"


def stop_text(z):
    if z["type"] == "long":
        return f"Stop-Vorschlag: unter {fmt(z['lo'] - STOP_BUFFER)}"
    return f"Stop-Vorschlag: über {fmt(z['hi'] + STOP_BUFFER)}"


# ---------- Hauptlogik ----------
def main():
    now = datetime.now(UTC)
    headers = login()
    m5 = get_candles(headers, "MINUTE_5", 300)
    m1 = get_candles(headers, "MINUTE", 200)
    if len(m5) < EMA_PERIOD + 5 or not m1:
        log("Zu wenige Kursdaten.")
        return

    price = m1[-1]["c"]
    trend_ema = ema([c["c"] for c in m5], EMA_PERIOD)
    trend = "long" if m5[-1]["c"] > trend_ema else "short"
    log(f"Kurs {fmt(price)} | EMA{EMA_PERIOD} {fmt(trend_ema)} | Trend: {trend}")

    def trend_ok(z):
        return (not TREND_FILTER) or z["type"] == trend

    def is_recent(event_time, minutes=EVENT_MAX_AGE_MIN):
        return now - event_time <= timedelta(minutes=minutes)

    state = load_state()
    zones = state["zones"]

    # Neue Zonen in den Zustand aufnehmen
    for d in detect_zones(m5):
        zid = f"{d['type']}_{d['formed'].strftime('%Y%m%dT%H%M')}"
        overlap = any(z["type"] == d["type"] and d["lo"] <= z["hi"] and d["hi"] >= z["lo"]
                      and z.get("status") != "invalid" for z in zones.values())
        if zid in zones or overlap:
            continue
        zones[zid] = {
            "type": d["type"], "lo": d["lo"], "hi": d["hi"],
            "formed": d["formed"].isoformat(),
            "status": "active", "alerted": False,
            "f_new": False, "f_touch": False, "f_confirm": False, "f_invalid": False,
        }

    messages = []

    for zid, z in zones.items():
        if z["status"] == "invalid":
            continue
        is_long = z["type"] == "long"
        formed = datetime.fromisoformat(z["formed"])
        arrow = "🟢" if is_long else "🔴"
        side = "Long" if is_long else "Short"

        # Meldung: neue Zone
        if not z["f_new"]:
            z["f_new"] = True
            if (ALERT_NEW_ZONE and trend_ok(z)
                    and is_recent(formed, RECENT_MINUTES)):
                messages.append(f"{arrow} Neuer {side}-Orderblock (M5)\n{zone_text(z)}")
                z["alerted"] = True

        # M5-Kerzen seit der Entstehung durchgehen
        touch_time = None
        invalid_time = None
        for c in m5:
            if c["t"] <= formed:
                continue
            if is_long:
                if c["c"] < z["lo"]:
                    invalid_time = c["t"]
                    break
                if touch_time is None and c["l"] <= z["hi"]:
                    touch_time = c["t"]
            else:
                if c["c"] > z["hi"]:
                    invalid_time = c["t"]
                    break
                if touch_time is None and c["h"] >= z["lo"]:
                    touch_time = c["t"]

        # Meldung: Zone ungültig
        if invalid_time is not None:
            z["status"] = "invalid"
            if (ALERT_INVALID and z["alerted"] and not z["f_invalid"]
                    and is_recent(invalid_time + timedelta(minutes=5))):
                messages.append(f"❌ {side}-Orderblock ungültig\n{zone_text(z)}")
            z["f_invalid"] = True
            continue

        # Meldung: erste Berührung
        if touch_time is not None and not z["f_touch"]:
            z["f_touch"] = True
            if (ALERT_TOUCH and trend_ok(z)
                    and is_recent(touch_time + timedelta(minutes=5))):
                messages.append(
                    f"⚡ {side}-Orderblock zum ersten Mal angetestet\n{zone_text(z)}\n"
                    f"Kurs: {fmt(price)}\nNoch keine Bestätigung - warte auf CHoCH."
                )
                z["alerted"] = True
            z["touch_time"] = touch_time.isoformat()

        # Meldung: CHoCH-Bestätigung
        if z["f_touch"] and not z["f_confirm"] and "touch_time" in z:
            tt = datetime.fromisoformat(z["touch_time"])
            if now - tt > timedelta(minutes=CONFIRM_WINDOW_MIN):
                z["f_confirm"] = True  # abgelaufen, nicht mehr warten
            else:
                choch = find_choch(is_long, z, tt, m1)
                if choch is not None:
                    z["f_confirm"] = True
                    if ALERT_CONFIRM and trend_ok(z) and is_recent(choch):
                        messages.append(
                            f"✅ {side}-Bestätigung (CHoCH im M1)\n{zone_text(z)}\n"
                            f"Kurs: {fmt(price)}\n{stop_text(z)}"
                        )
                        z["alerted"] = True

    # Alte Zonen (älter als 24h) aus dem Zustand entfernen
    for zid in list(zones.keys()):
        if now - datetime.fromisoformat(zones[zid]["formed"]) > timedelta(hours=24):
            del zones[zid]

    for m in messages:
        log(m)
        send_telegram(m)

    save_state(state)
    log(f"Fertig. {len(messages)} Meldung(en) gesendet.")


if __name__ == "__main__":
    main()
