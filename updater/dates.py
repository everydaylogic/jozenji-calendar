"""日付表記のゆれを YYYY-MM-DD に揃えるユーティリティ。

LLMが返した日付も、ページから直接拾った日付も、最後は必ずここを通す。
（小型モデルは和暦や年なし表記の変換をよく間違えるため、Python側で確定させる）
"""
from __future__ import annotations

import re
import unicodedata
from datetime import date

ERA_BASE = {"令和": 2018, "平成": 1988}

_ERA = re.compile(r"(令和|平成)\s*(元|\d{1,2})\s*年\s*(\d{1,2})\s*月\s*(\d{1,2})\s*日?")
_YMD = re.compile(r"(\d{4})\s*[-/.年]\s*(\d{1,2})\s*[-/.月]\s*(\d{1,2})")
_MD = re.compile(r"(?<!\d)(\d{1,2})\s*[/月]\s*(\d{1,2})(?!\d)")


def _norm(s: str) -> str:
    return unicodedata.normalize("NFKC", s or "").strip()


def _safe(y: int, m: int, d: int) -> str | None:
    try:
        return date(y, m, d).isoformat()
    except ValueError:
        return None


def guess_year(month: int, ref: date) -> int:
    """年が書かれていない日付の年を推定する。
    今日より3か月以上前の月なら来年の話とみなす（例：11月に見た「1月22日」は翌年）。"""
    if month < ref.month - 2:
        return ref.year + 1
    return ref.year


def parse_date(text: str | None, *, year: int | None = None,
               ref: date | None = None) -> str | None:
    """文字列から最初に見つかった日付を YYYY-MM-DD で返す。見つからなければ None。"""
    if not text:
        return None
    s = _norm(str(text))
    ref = ref or date.today()

    m = _ERA.search(s)
    if m:
        era, n, mo, d = m.groups()
        y = ERA_BASE[era] + (1 if n == "元" else int(n))
        return _safe(y, int(mo), int(d))

    m = _YMD.search(s)
    if m:
        return _safe(*map(int, m.groups()))

    m = _MD.search(s)
    if m:
        mo, d = int(m.group(1)), int(m.group(2))
        y = year if year else guess_year(mo, ref)
        return _safe(y, mo, d)

    return None
