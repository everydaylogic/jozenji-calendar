"""ページ本文 → 軽量LLM（Ollama）→ イベントのリスト。

小型モデルの弱点（形式崩れ・でっち上げ・日付の誤変換）を、次の3つで抑えている。
  1. JSONスキーマを指定して出力形式を固定する（Ollama の structured output）
  2. 返ってきたタイトルとURLが本当にページ内にあるか照合し、無いものは捨てる
  3. 日付は dates.py で最終的に Python 側が決める
"""
from __future__ import annotations

import json
import logging
import re
import unicodedata
from datetime import date
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup
from markdownify import markdownify

from dates import parse_date

log = logging.getLogger(__name__)

EVENT_SCHEMA = {
    "type": "object",
    "properties": {
        "events": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "title": {"type": "string"},
                    "date_start": {"type": "string"},
                    "date_end": {"type": "string"},
                    "time": {"type": "string"},
                    "venue": {"type": "string"},
                    "url": {"type": "string"},
                    "note": {"type": "string"},
                },
                "required": ["title", "date_start"],
            },
        }
    },
    "required": ["events"],
}

SYSTEM_PROMPT = (
    "あなたはWebページの本文からイベント情報を正確に抜き出すアシスタントです。"
    "本文に書かれていることだけを使い、書かれていない情報を推測で補ってはいけません。"
    "出力は指定されたJSONだけにしてください。"
)

USER_TEMPLATE = """# 対象ページ
- サイト: {name}
- URL: {url}
- 今日の日付: {today}
{page_month_line}
# このサイトについての注意
{hint}

# 抜き出すもの
本文に載っている公演・催し・イベントを、1件ずつ events 配列に入れてください。
- title: イベント名（本文の表記のまま）
- date_start: 開始日を YYYY-MM-DD で。和暦は西暦に直す（令和8年=2026年）
- date_end: 複数日にわたる場合の最終日を YYYY-MM-DD で。1日だけなら空文字
- time: 開場・開演などの時刻（本文の表記のまま。無ければ空文字）
- venue: 会場名。書かれていなければ空文字
- url: そのイベントの詳細ページへのリンク（本文中の (https://...) をそのまま）。無ければ空文字
- note: 料金・ジャンル・主催など、補足を40字以内で。無ければ空文字

イベントが1件も無ければ {{"events": []}} を返してください。

# 本文
{text}
"""


# ---------------------------------------------------------------- 前処理
def html_to_text(html: str, base_url: str, selector: str | None = None) -> str:
    """HTMLから本文らしき部分をMarkdownに変換する（リンクは絶対URLで残す）。"""
    soup = BeautifulSoup(html, "html.parser")
    for tag in soup(["script", "style", "noscript", "iframe", "svg", "form", "nav"]):
        tag.decompose()
    # ページ全体のヘッダー・フッターだけ消す（記事内の <header> は残す）
    for tag in soup.find_all(["header", "footer"]):
        if not tag.find_parent(["article", "main", "section", "li"]):
            tag.decompose()
    root = soup.select_one(selector) if selector else None
    root = root or soup.body or soup
    for a in root.find_all("a", href=True):
        a["href"] = urljoin(base_url, a["href"])
    md = markdownify(str(root), strip=["img"], heading_style="ATX")
    md = re.sub(r"\[\s*\]\([^)]*\)", "", md)          # 文字のないリンク（画像だけのリンク）
    md = re.sub(r"[ \t　]+", " ", md)
    md = re.sub(r"\n\s*\n+", "\n\n", md)
    return md.strip()


def chunk_text(text: str, size: int, overlap: int = 400) -> list[str]:
    """行の切れ目で size 文字ごとに分割。境目のイベントが欠けないよう少し重ねる。"""
    if len(text) <= size:
        return [text]
    chunks, buf = [], ""
    for line in text.splitlines(keepends=True):
        if len(buf) + len(line) > size and buf:
            chunks.append(buf)
            buf = buf[-overlap:]
        buf += line
    if buf.strip():
        chunks.append(buf)
    return chunks


# ---------------------------------------------------------------- LLM呼び出し
# 同じ文章・同じモデルへの問い合わせは、前回の答えを使い回す（変わっていないページはLLMを動かさない）
CACHE: dict = {}
USED: set = set()


def call_ollama(prompt: str, ocfg: dict) -> dict:
    import hashlib
    k = hashlib.sha1((ocfg["model"] + "\n" + prompt).encode("utf-8")).hexdigest()
    USED.add(k)
    if k in CACHE:
        log.info("LLM: 前回と同じ内容なので前回の結果を使います")
        return CACHE[k]
    out = _call_ollama(prompt, ocfg)
    CACHE[k] = out
    return out


def _call_ollama(prompt: str, ocfg: dict) -> dict:
    body = {
        "model": ocfg["model"],
        "messages": [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": prompt},
        ],
        "format": EVENT_SCHEMA,
        "stream": False,
        "options": {"temperature": 0, "num_ctx": ocfg.get("num_ctx", 8192)},
    }
    if "think" in ocfg:
        body["think"] = ocfg["think"]
    r = requests.post(f"{ocfg['host'].rstrip('/')}/api/chat", json=body,
                      timeout=ocfg.get("timeout", 300))
    r.raise_for_status()
    content = r.json()["message"]["content"]
    return json.loads(content)


# ---------------------------------------------------------------- 検証
def _key(s: str) -> str:
    s = unicodedata.normalize("NFKC", s or "").lower()
    return re.sub(r"[\s\W_]+", "", s)


def grounded(title: str, chunk: str) -> bool:
    """タイトル（またはその一部）が本文に実在するか。でっち上げ対策。"""
    body = _key(chunk)
    parts = [p for p in re.split(r"\s*[/／|｜]\s*", title) if p.strip()] or [title]
    for p in parts:
        k = _key(p)
        if len(k) >= 2 and k[:12] in body:
            return True
    return False


def clean_event(raw: dict, chunk: str, *, page_url: str, src: dict,
                page_year: int | None, page_month: int | None,
                today: date) -> dict | None:
    title = (raw.get("title") or "").strip()
    if not title or not grounded(title, chunk):
        log.debug("discard (not in page): %r", title)
        return None

    ds_raw = (raw.get("date_start") or "").strip()
    # 月別ページで「01」のように日だけ返ってきた場合はページの年月で補う
    if page_month and re.fullmatch(r"\d{1,2}", ds_raw):
        ds_raw = f"{page_year}-{page_month:02d}-{int(ds_raw):02d}"
    ds = parse_date(ds_raw, year=page_year, ref=today)
    if not ds:
        log.debug("discard (no date): %r %r", title, ds_raw)
        return None
    de = parse_date(raw.get("date_end"), year=page_year, ref=today)
    if de and de < ds:
        de = None

    url = (raw.get("url") or "").strip()
    if not url or url not in chunk:          # 本文に無いURLは信用しない
        url = page_url

    return {
        "source": src["id"],
        "section": src.get("section", "events"),
        "title": title,
        "date_start": ds,
        "date_end": de if de and de != ds else None,
        "expires": None,
        "time": (raw.get("time") or "").strip(),
        "venue": (raw.get("venue") or "").strip() or src.get("venue", ""),
        "category": "",
        "url": url,
        "note": (raw.get("note") or "").strip()[:80],
    }


# ---------------------------------------------------------------- 本体
def extract_events(text: str, *, page_url: str, src: dict, ocfg: dict,
                   today: date, page_year: int | None = None,
                   page_month: int | None = None) -> list[dict]:
    chunks = chunk_text(text, ocfg.get("chunk_chars", 4500))
    page_month_line = (f"- このページは {page_year}年{page_month}月 のスケジュールです\n"
                       if page_month else "")
    out = []
    for i, chunk in enumerate(chunks, 1):
        prompt = USER_TEMPLATE.format(
            name=src["name"], url=page_url, today=today.isoformat(),
            page_month_line=page_month_line, hint=src.get("hint", "特になし"),
            text=chunk)
        try:
            data = call_ollama(prompt, ocfg)
        except (requests.RequestException, json.JSONDecodeError, KeyError) as e:
            log.warning("%s chunk %d/%d: LLM error: %s", src["id"], i, len(chunks), e)
            continue
        raws = data.get("events", []) if isinstance(data, dict) else []
        kept = [ev for r in raws if isinstance(r, dict)
                if (ev := clean_event(r, chunk, page_url=page_url, src=src,
                                      page_year=page_year, page_month=page_month,
                                      today=today))]
        log.info("%s chunk %d/%d: LLM %d件 → 採用 %d件", src["id"], i, len(chunks),
                 len(raws), len(kept))
        out.extend(kept)
    return out
