"""仙台三越「催物ご案内」PDF（月間の帯の表）から、本館7階ホールの催事と会期を読む。

画像として読むのではなく、PDFの中にある「文字の位置」と「区切り線の位置」を使う。
  - 表の上段に日付（1, 2, 3, …）が並ぶ → 各日の列の中心のx座標がわかる
  - 左の見出し列に縦書きで「ホ／ー／ル」 → ホールの行の上下がわかる
  - 行の中は縦線・横線で区切られていて、区切られた1区画が1つの催事
  - 区画の中の文字をつなげたものが催事名、区画の左右の端が会期の初日・最終日
（灰色の入れ替え日は、文字のない区画になるので自然に除かれる）

  python mitsukoshi_pdf.py sendai_event.pdf       # 単体で確認できる
"""
from __future__ import annotations

import io
import re
import sys
import unicodedata
from datetime import date


def _nf(s: str) -> str:
    return unicodedata.normalize("NFKC", s or "")


def parse(pdf: bytes | str, today: date | None = None) -> dict:
    import pdfplumber

    today = today or date.today()
    src = io.BytesIO(pdf) if isinstance(pdf, bytes) else pdf
    with pdfplumber.open(src) as doc:
        page = doc.pages[0]
        words = page.extract_words(keep_blank_chars=False)
        lines = page.lines

    # --- 日付の見出し（同じ高さに数字が20個以上並んでいる行） ---
    rows: dict[int, list] = {}
    for w in words:
        t = _nf(w["text"])
        if t.isdigit() and 1 <= int(t) <= 31:
            rows.setdefault(round(w["top"] / 15), []).append(w)
    header = max(rows.values(), key=len) if rows else []
    if len(header) < 20:
        raise ValueError("日付の見出し行が見つかりません")
    # 少し高さのずれた「1」も拾う（見出し行の上下20以内で、左端の列にある数字）
    htop = sorted(w["top"] for w in header)[len(header) // 2]
    hx_min = min(w["x0"] for w in header) - 60
    cols = {int(_nf(w["text"])): (w["x0"] + w["x1"]) / 2
            for w in words if _nf(w["text"]).isdigit() and abs(w["top"] - htop) < 20
            and 1 <= int(_nf(w["text"])) <= 31 and w["x0"] >= hx_min}
    days = sorted(cols)
    if days[0] != 1:
        raise ValueError(f"日付の見出しが1日から始まっていません（{days[:3]}）")
    colw = (cols[days[-1]] - cols[days[0]]) / (len(days) - 1)
    grid_left = cols[days[0]] - colw * 0.75
    grid_right = cols[days[-1]] + colw * 0.75

    # --- 対象の月（見出しの左にある「11月」など） ---
    month = None
    for i, w in enumerate(words):
        if _nf(w["text"]) == "月" and w["x0"] < grid_left:
            prev = [v for v in words if v["x1"] <= w["x0"] + 2 and abs(v["top"] - w["top"]) < 8
                    and _nf(v["text"]).isdigit()]
            if prev:
                month = int(_nf(max(prev, key=lambda v: v["x1"])["text"]))
                break
    if not month:
        raise ValueError("対象の月が見つかりません")
    year = today.year + (1 if month < today.month - 6 else 0) - (1 if month > today.month + 6 else 0)

    # --- ホールの行（左の見出し列に縦書きの「ホ」「ー」「ル」） ---
    label = [w for w in words if w["x1"] <= grid_left and _nf(w["text"]) in ("ホ", "ー", "ル", "-")]
    ho = [w for w in label if _nf(w["text"]) == "ホ"]
    if not ho:
        raise ValueError("「ホール」の見出しが見つかりません")
    ho = ho[0]
    lab = [w for w in label if abs(w["x0"] - ho["x0"]) < 6 and 0 <= w["top"] - ho["top"] < 120]
    lab_top, lab_bottom = min(w["top"] for w in lab), max(w["bottom"] for w in lab)
    long_h = [l for l in lines if l["x1"] - l["x0"] > (grid_right - grid_left) * 0.8 and abs(l["top"] - l["bottom"]) < 1]
    row_top = max((l["top"] for l in long_h if l["top"] <= lab_top), default=None)
    row_bottom = min((l["top"] for l in long_h if l["top"] >= lab_bottom), default=None)
    if row_top is None or row_bottom is None:
        raise ValueError("ホールの行の上下の線が見つかりません")

    vlines = [l for l in lines if abs(l["x0"] - l["x1"]) < 1 and l["bottom"] - l["top"] > 5]
    hlines = [l for l in lines if abs(l["top"] - l["bottom"]) < 1 and l["x1"] - l["x0"] > 5]

    def region(w):
        cx, cy = (w["x0"] + w["x1"]) / 2, (w["top"] + w["bottom"]) / 2
        left = max([grid_left] + [l["x0"] for l in vlines
                                  if l["x0"] <= w["x0"] + 1 and l["top"] - 1 <= cy <= l["bottom"] + 1])
        right = min([grid_right] + [l["x0"] for l in vlines
                                    if l["x0"] >= w["x1"] - 1 and l["top"] - 1 <= cy <= l["bottom"] + 1])
        top = max([row_top] + [l["top"] for l in hlines
                               if l["top"] <= w["top"] + 1 and l["x0"] - 1 <= cx <= l["x1"] + 1])
        bottom = min([row_bottom] + [l["top"] for l in hlines
                                     if l["top"] >= w["bottom"] - 1 and l["x0"] - 1 <= cx <= l["x1"] + 1])
        return (round(left), round(top), round(right), round(bottom))

    cells: dict[tuple, list] = {}
    for w in words:
        if w["x0"] >= grid_left - 1 and row_top < (w["top"] + w["bottom"]) / 2 < row_bottom:
            cells.setdefault(region(w), []).append(w)

    events = []
    for (left, top, right, bottom), ws in cells.items():
        inside = [d for d in days if left < cols[d] < right]
        if not inside:
            continue
        ws.sort(key=lambda w: (round(w["top"] / 6), w["x0"]))
        parts, last_top = [], None
        for w in ws:
            t = _nf(w["text"]).strip()
            if not t:
                continue
            if (last_top is not None and abs(w["top"] - last_top) > 6 and parts
                    and (parts[-1][-1].isascii() or t[0].isascii() or t[0] in "〈<（(")):
                parts.append(" ")
            parts.append(t)
            last_top = w["top"]
        title = re.sub(r"\s+", " ", "".join(parts)).strip()
        title = re.sub(r"^20\d\d\s*", "", title)
        if not title:
            continue
        ds, de = date(year, month, inside[0]), date(year, month, inside[-1])
        events.append({"title": title, "date_start": ds.isoformat(),
                       "date_end": de.isoformat() if de != ds else "",
                       "to_month_end": inside[-1] == days[-1],
                       "_order": (left, top)})
    events.sort(key=lambda e: (e["date_start"], e["_order"]))
    for e in events:
        e.pop("_order")
    return {"month": f"{year}-{month:02d}", "events": events}


if __name__ == "__main__":
    import json
    print(json.dumps(parse(sys.argv[1]), ensure_ascii=False, indent=1))
