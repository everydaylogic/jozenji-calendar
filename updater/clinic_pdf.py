"""一番町健診クリニックの「年間スケジュール」PDFを読み取り、日ごとの健診実施状況にする。

PDFの作り（2026年度版で確認）:
  - 左半分が女性専用フロア（4F）、右半分が男性フロア（5F）。それぞれ4月〜翌3月の12か月
  - 日付マスの背景色: 灰色 = 休診、水色 = 午後休診、白 = 通常どおり
  - 午後の健診は平日のみ（土曜は午前のみ）

  python clinic_pdf.py calendar_r8_2026.pdf     # 単体で読み取り結果を確認できる
"""
from __future__ import annotations

import io
import json
import re
import sys
from collections import Counter
from datetime import date, datetime
from pathlib import Path

FLOORS = {"women": "女性（4F）", "men": "男性（5F）"}


def _fill_kind(c) -> str | None:
    if isinstance(c, (tuple, list)) and len(c) == 1:
        c = c[0]
    if isinstance(c, (int, float)):
        return "closed" if 0.8 <= c <= 0.9 else None          # 薄い灰色
    if isinstance(c, (tuple, list)) and len(c) == 3:
        r, g, b = c
        if b > r + 0.1 and g > 0.8 and b > 0.85:              # 水色
            return "pm_closed"
    return None


def parse(pdf: bytes | str | Path) -> dict:
    import pdfplumber

    src = io.BytesIO(pdf) if isinstance(pdf, bytes) else pdf
    with pdfplumber.open(src) as doc:
        page = doc.pages[0]
        text = page.extract_text() or ""
        words = page.extract_words()
        fills = [(r, _fill_kind(r.get("non_stroking_color"))) for r in page.rects
                 if r.get("fill") and r["x1"] - r["x0"] > 5 and r["bottom"] - r["top"] > 5]
        fills = [(r, k) for r, k in fills if k]
        width = page.width

    m = re.search(r"(\d{4})\s*年度", text)
    if not m:
        raise ValueError("「○○年度」の表記が見つかりません")
    fy = int(m.group(1))

    labels = [w for w in words if re.fullmatch(r"\d{1,2}月", w["text"])]
    if len(labels) != 24:
        raise ValueError(f"月の見出しが24個ではありません（{len(labels)}個）")
    # 1か月分の表の幅・高さ（見出し同士の間隔から求める）
    xs = sorted({round(l["x0"]) for l in labels})
    block_w = min(b - a for a, b in zip(xs, xs[1:]) if b - a > 20)
    ys = sorted({round(l["top"]) for l in labels})
    block_h = min(b - a for a, b in zip(ys, ys[1:]) if b - a > 20)

    days: dict[str, dict[str, str]] = {f: {} for f in FLOORS}
    for w in words:
        if not re.fullmatch(r"\d{1,2}", w["text"]):
            continue
        cx, cy = (w["x0"] + w["x1"]) / 2, (w["top"] + w["bottom"]) / 2
        owners = [l for l in labels if l["top"] < w["top"] < l["top"] + block_h
                  and l["x0"] - 5 <= cx < l["x0"] - 5 + block_w]
        if not owners:
            continue
        lab = max(owners, key=lambda l: l["top"])
        mon = int(lab["text"][:-1])
        d = date(fy if mon >= 4 else fy + 1, mon, int(w["text"])).isoformat()
        floor = "women" if lab["x0"] < width / 2 else "men"
        kind = "open"
        for r, k in fills:
            if r["x0"] <= cx <= r["x1"] and r["top"] <= cy <= r["bottom"]:
                kind = k
        days[floor][d] = kind

    expected = (date(fy + 1, 4, 1) - date(fy, 4, 1)).days
    for f, dd in days.items():
        if len(dd) != expected:
            raise ValueError(f"{FLOORS[f]} の日数が {len(dd)} 日（{expected} 日のはず）")

    return {
        "fiscal_year": fy,
        "period": [f"{fy}-04-01", f"{fy + 1}-03-31"],
        "hours": "一般健診：午前 7:45〜11:45（土曜は〜10:00）、午後 14:00〜14:30（平日のみ）／人間ドック：午前 7:45〜9:30",
        "floors": {f: {"label": FLOORS[f], "days": dict(sorted(days[f].items()))} for f in FLOORS},
        "parsed_at": datetime.now().isoformat(timespec="seconds"),
    }


def summary(cal: dict) -> str:
    return " / ".join(f"{v['label']} " + str(dict(Counter(v["days"].values())))
                      for v in cal["floors"].values())


if __name__ == "__main__":
    cal = parse(Path(sys.argv[1]))
    print(cal["period"], summary(cal))
    if len(sys.argv) > 2:
        Path(sys.argv[2]).write_text(json.dumps(cal, ensure_ascii=False, indent=1), encoding="utf-8")
        print("保存:", sys.argv[2])
