import json
import os
import sys

import requests

import backtest as bt


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
    try:
        res = bt.run(h, os.environ.get("GITHUB_RUN_ID", "lokal"))
    except Exception as e:
        res = {"error": str(e)}
        print("FEHLER:", e)
        os.makedirs("docs", exist_ok=True)
        with open("docs/backtest.json", "w") as f:
            json.dump(res, f)
        sys.exit(1)
    os.makedirs("docs", exist_ok=True)
    with open("docs/backtest.json", "w") as f:
        json.dump(res, f)
    print("Tage:", res["days"])
    for v in res["variants"]:
        print(v["name"], "| pro Tag:", v["per_day"], "| Training:", v["train"], "| Test:", v["test"])


main()
