import json
import os
import sys
import traceback

import requests

import backtest as bt
import backtest2 as bt2
import backtest3 as bt3


def write(res):
    os.makedirs("docs", exist_ok=True)
    with open("docs/backtest.json", "w") as f:
        json.dump(res, f)


def main():
    r = requests.post(
        f"{bt.BASE}/session",
        headers={"X-CAP-API-KEY": os.environ["CAPITAL_API_KEY"]},
        json={
            "identifier": os.environ["CAPITAL_IDENTIFIER"],
            "password": os.environ["CAPITAL_PASSWORD"],
        },
        timeout=20,
    )
    r.raise_for_status()
    h = {"CST": r.headers["CST"], "X-SECURITY-TOKEN": r.headers["X-SECURITY-TOKEN"]}

    m5 = None
    last_err = None
    for days in (180, 120, 70, 40):
        bt.DAYS = days
        try:
            m5 = bt.load(h)
            print("Kerzen geladen:", len(m5), "bei", days, "Tagen")
            break
        except Exception as e:
            last_err = e
            print("Laden mit", days, "Tagen fehlgeschlagen:", e)
    if m5 is None:
        write({"error": f"Kerzen konnten nicht geladen werden: {last_err}"})
        sys.exit(1)

    try:
        res = bt.analyze(m5, os.environ.get("GITHUB_RUN_ID", "lokal"))
    except Exception as e:
        write({"error": str(e)})
        print("FEHLER Runde 1:", e)
        sys.exit(1)

    try:
        res["round2"] = bt2.analyze2(m5, res["id"])
    except Exception as e:
        traceback.print_exc()
        res["round2"] = {"error": str(e)}

    try:
        res["round3"] = bt3.analyze3(m5, res["id"])
    except Exception as e:
        traceback.print_exc()
        res["round3"] = {"error": str(e)}

    write(res)
    print("Tage:", res["days"])
    for v in res["variants"]:
        print("R1", v["name"], "|", v["per_day"], "/Tag | Training:", v["train"], "| Test:", v["test"])
    for v in res.get("round2", {}).get("variants", []):
        print("R2", v["name"], "|", v["per_day"], "/Tag | t =", v["t"], "| Test:", v["test"])
    for v in res.get("round3", {}).get("variants", []):
        print("R3", v["name"], "|", v["per_day"], "/Tag | t =", v["t"], "| Test:", v["test"])


main()
