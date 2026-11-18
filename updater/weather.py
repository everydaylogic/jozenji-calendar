"""仙台の天気予報（気象庁）を docs/weather.json に保存する。GitHub Actions から数時間おきに動かす。

  python updater/weather.py

気象庁の宮城県の予報（府県天気予報）から、宮城県東部の天気・降水確率と、仙台の最高・最低気温を
日付ごとに取り出す。ページは自分の日付の分を選んで表示するので、更新が遅れても日付がずれない。
"""
from __future__ import annotations

import datetime as dt
import json
import sys
from pathlib import Path

import requests

URL = "https://www.jma.go.jp/bosai/forecast/data/forecast/040000.json"
AREA = "040010"   # 宮城県東部（天気・降水確率）
CITY = "34392"    # 仙台（気温）
ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "docs" / "weather.json"
JST = dt.timezone(dt.timedelta(hours=9))


def clean(s: str) -> str:
    return "".join(s.split())          # 気象庁の文は全角スペースで区切られている


def build(raw: list) -> dict:
    days: dict[str, dict] = {}

    def day(t: str) -> dict:
        return days.setdefault(t[:10], {})

    short = raw[0]
    for ts in short["timeSeries"]:
        td = ts["timeDefines"]
        for a in ts["areas"]:
            code = a["area"]["code"]
            if code == AREA and "weathers" in a:
                for t, w, c in zip(td, a["weathers"], a.get("weatherCodes", [])):
                    day(t).update(weather=clean(w), code=c)
            if code == AREA and "pops" in a:
                for t, p in zip(td, a["pops"]):
                    if p != "":
                        day(t).setdefault("pops", {})[t[11:13]] = int(p)
            if code == CITY and "temps" in a:
                # 同じ日に 2つ並ぶ。00時の方が朝の最低、09時の方が日中の最高
                for t, v in zip(td, a["temps"]):
                    if v == "":
                        continue
                    day(t)["min" if t[11:13] == "00" else "max"] = int(v)

    # 週間予報で、足りない気温・天気を補う（明日以降）
    if len(raw) > 1:
        week = raw[1]
        for ts in week["timeSeries"]:
            td = ts["timeDefines"]
            for a in ts["areas"]:
                code = a["area"]["code"]
                if code == CITY and "tempsMax" in a:
                    for t, mx, mn in zip(td, a["tempsMax"], a["tempsMin"]):
                        d = days.get(t[:10]) or day(t)
                        if mx != "" and "max" not in d:
                            d["max"] = int(mx)
                        if mn != "" and "min" not in d:
                            d["min"] = int(mn)
                if code.startswith("04") and "weatherCodes" in a and "pops" in a:
                    for t, c, p in zip(td, a["weatherCodes"], a["pops"]):
                        d = day(t)
                        d.setdefault("code", c)
                        if p != "" and "pop_day" not in d:
                            d["pop_day"] = int(p)

    return {
        "published_at": short["reportDatetime"],
        "office": short.get("publishingOffice", ""),
        "area": "宮城県東部（仙台）",
        "fetched_at": dt.datetime.now(JST).isoformat(timespec="minutes"),
        "days": dict(sorted(days.items())),
    }


def main() -> int:
    r = requests.get(URL, timeout=30, headers={"User-Agent": "jozenji-display (weather; weekly event board)"})
    r.raise_for_status()
    raw = r.json()
    if "--raw" in sys.argv:
        (ROOT / "dump").mkdir(exist_ok=True)
        (ROOT / "dump" / "jma_040000.json").write_text(json.dumps(raw, ensure_ascii=False, indent=1), encoding="utf-8")
    out = build(raw)
    old = json.loads(OUT.read_text(encoding="utf-8")) if OUT.exists() else {}
    # 同じ発表なら書き換えない（取得時刻だけ違う変更で、毎回公開し直さないため）
    if old.get("published_at") == out["published_at"] and old.get("days") == out["days"]:
        print("変更なし", out["published_at"])
        return 0
    OUT.write_text(json.dumps(out, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
    print(json.dumps(out, ensure_ascii=False, indent=1))
    return 0


if __name__ == "__main__":
    sys.exit(main())
