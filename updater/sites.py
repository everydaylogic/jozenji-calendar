"""サイトごとの取得処理。

各関数は (前回のデータ, ctx) を受け取り、
  {"events": [...], "message": "...", "extra": {文書に足すフィールド}}
を返す。取得に失敗したら例外を出す（run.py が前回のデータを残す）。

events の形（本番のアーティファクトのデータと同じ）:
  {title, date_start, date_end（1日だけなら ""）, time, venue, url, note, ...目印}
"""
from __future__ import annotations

import io
import json
import logging
import re
import time
import unicodedata
import urllib.robotparser
from datetime import date, timedelta
from difflib import SequenceMatcher
from pathlib import Path
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup

import clinic_pdf
import mitsukoshi_pdf
from dates import parse_date
from llm import extract_events, html_to_text

log = logging.getLogger(__name__)
ROOT = Path(__file__).resolve().parent.parent
UA = ("Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) "
      "Chrome/130.0 Safari/537.36 jozenji-display-test")


# ---------------------------------------------------------------- 共通
class Ctx:
    def __init__(self, today: date, ollama: dict):
        self.today = today
        self.month_start = today.replace(day=1)
        self.ollama = ollama
        self.s = requests.Session()
        self.s.headers.update({"User-Agent": UA, "Accept-Language": "ja,en;q=0.8"})
        self._last = 0.0

    def get(self, url: str, **kw) -> requests.Response:
        wait = 2.0 - (time.time() - self._last)
        if wait > 0:
            time.sleep(wait)
        r = self.s.get(url, timeout=kw.pop("timeout", 40), **kw)
        self._last = time.time()
        r.raise_for_status()
        if not r.encoding or r.encoding.lower() == "iso-8859-1":
            r.encoding = r.apparent_encoding
        return r

    def months(self, n: int):
        y, m = self.today.year, self.today.month
        for _ in range(n):
            yield y, m
            y, m = (y + 1, 1) if m == 12 else (y, m + 1)


def nf(s: str) -> str:
    return unicodedata.normalize("NFKC", s or "").strip()


def key(s: str) -> str:
    return re.sub(r"[\s\W_]+", "", nf(s).lower())


def end_of(e: dict) -> str:
    return e.get("date_end") or e["date_start"]


def same_event(a: dict, b: dict) -> bool:
    """会期が重なり、名前が似ていれば同じ催し。"""
    if end_of(a) < b["date_start"] or end_of(b) < a["date_start"]:
        return False
    ka, kb = key(a["title"]), key(b["title"])
    if not ka or not kb:
        return False
    if ka[:6] in kb or kb[:6] in ka:
        return True
    return SequenceMatcher(None, ka, kb).ratio() >= 0.6


def from_llm(ev: dict, **extra) -> dict:
    out = {"title": ev["title"], "date_start": ev["date_start"],
           "date_end": ev.get("date_end") or "", "time": ev.get("time") or "",
           "venue": ev.get("venue") or "", "url": ev.get("url") or "",
           "note": ev.get("note") or ""}
    out.update(extra)
    return out


def llm_events(ctx: Ctx, text: str, *, page_url: str, sid: str, name: str, venue: str,
               hint: str, year=None, month=None) -> list[dict]:
    src = {"id": sid, "name": name, "venue": venue, "hint": hint}
    return extract_events(text, page_url=page_url, src=src, ocfg=ctx.ollama,
                          today=ctx.today, page_year=year, page_month=month)


def pdf_text(data: bytes) -> str:
    import pdfplumber
    with pdfplumber.open(io.BytesIO(data)) as doc:
        return "\n".join((p.extract_text() or "") for p in doc.pages)


# ---------------------------------------------------------------- Rensa
RENSA_HINT = ("仙台のライブハウスの月間スケジュール。日付は「01 Sun」のように日と曜日だけが書かれている。"
              "「Ｐｒｉｖａｔｅ」とだけ書かれた日はイベントではないので出力しない。"
              "出演者名とツアー名の両方があれば title は「出演者 / ツアー名」にする。"
              "time は OPEN/START の時刻（例 18:00 / 18:30）、note は「INFO 主催者名」。")


def rensa(prev: dict, ctx: Ctx) -> dict:
    rp = urllib.robotparser.RobotFileParser()
    try:
        r = ctx.s.get("http://www.rensa.jp/robots.txt", timeout=20)
        rp.parse(r.text.splitlines() if r.status_code == 200 else [])
    except requests.RequestException as e:
        raise RuntimeError(f"Rensa のサイトに接続できませんでした（{type(e).__name__}）")
    if not rp.can_fetch(UA, "http://www.rensa.jp/schedule/"):
        raise RuntimeError("サイトの robots.txt で自動取得が禁止されています")
    events = []
    for y, m in ctx.months(3):
        url = f"http://www.rensa.jp/schedule/?index={y}%2F{m:02d}"
        text = html_to_text(ctx.get(url).text, url)
        log.info("rensa %d-%02d: 本文 %d 文字", y, m, len(text))
        for ev in llm_events(ctx, text, page_url=url, sid="rensa", name="Rensa", venue="Rensa",
                             hint=RENSA_HINT, year=y, month=m):
            events.append(from_llm(ev, venue="Rensa"))
    return {"events": events}


# ---------------------------------------------------------------- 東京エレクトロンホール宮城
HALL = "東京エレクトロンホール宮城"
HALL_EVENT_HINT = ("県民会館の主催・共催公演一覧。日付は和暦（令和8年=2026年、令和9年=2027年）。"
                   "time は開場・開演の時刻。note は料金や「売り切れ」「発売中」などを40字以内で。"
                   "url は https://miyagi-hall.jp/events/番号/ の詳細リンク。")
HALL_SCHED_HINT = ("ホールの月間の催事予定表（PDFから取り出した文字）。表の1行が1件で、日付・曜日・催事名・開演時間・"
                   "主催または問い合わせ先が並ぶ。title は催事名（先頭の★は外す）。"
                   "time は開演時刻を「開演17:00」の形で（回が複数なら「①14:30 ②19:00 開演」、関係者のみの催しは"
                   "「13:00（関係者のみ）」）。note は主催または問い合わせ先の名前を短く（URL・メールアドレスは入れない）。"
                   "url は空文字。日付の書かれていない行は出力しない。")


def _hall_month(text: str, html: str) -> tuple[int, int] | None:
    for s in (nf(text), nf(html)):
        m = re.search(r"(20\d\d)\s*年\s*(\d{1,2})\s*月", s)
        if m:
            return int(m.group(1)), int(m.group(2))
        m = re.search(r"令和\s*(\d{1,2})\s*年\s*(\d{1,2})\s*月", s)
        if m:
            return 2018 + int(m.group(1)), int(m.group(2))
    return None


def miyagi_hall(prev: dict, ctx: Ctx) -> dict:
    notes, ok_a, ok_b = [], False, False
    # (b) 主催公演一覧
    ev_b = []
    try:
        url = "https://miyagi-hall.jp/event/"
        text = html_to_text(ctx.get(url).text, url)
        for ev in llm_events(ctx, text, page_url=url, sid="miyagi-hall", name=HALL, venue=HALL,
                             hint=HALL_EVENT_HINT):
            ev_b.append(from_llm(ev, venue=HALL))
        ok_b = True
    except Exception as e:  # noqa: BLE001
        notes.append(f"主催公演の一覧を読めませんでした（{type(e).__name__}）")
        log.warning("miyagi-hall (b): %s", e)

    # (a) 催事予定表
    ev_a, sched_month = [], prev.get("schedule_month", "")
    try:
        top = ctx.get("https://miyagi-hall.jp/").text
        first = re.search(r'href="(https://miyagi-hall\.jp/schedules/\d+/|/schedules/\d+/)"', top)
        if not first:
            raise RuntimeError("トップに催事予定表のリンクがありません")
        url0 = urljoin("https://miyagi-hall.jp/", first.group(1))
        html0 = ctx.get(url0).text
        ids = sorted({int(i) for i in re.findall(r"/schedules/(\d+)/", html0 + top)}, reverse=True)[:4]
        months_done = []
        for sid_ in ids:
            purl = f"https://miyagi-hall.jp/schedules/{sid_}/"
            html = html0 if purl == url0 else ctx.get(purl).text
            pdf = re.search(r'href="([^"]+/schedules/[^"]+\.pdf)"', html)
            if not pdf:
                continue
            data = ctx.get(urljoin(purl, pdf.group(1))).content
            text = pdf_text(data)
            ym = _hall_month(text, html)
            if not ym or date(ym[0], ym[1], 1) < ctx.month_start:
                continue
            y, m = ym
            log.info("miyagi-hall 予定表 %d-%02d: 本文 %d 文字", y, m, len(text))
            for ev in llm_events(ctx, text, page_url=purl, sid="miyagi-hall", name=HALL, venue=HALL,
                                 hint=HALL_SCHED_HINT, year=y, month=m):
                ev_a.append(from_llm(ev, venue=HALL, url=purl, from_schedule=True))
            months_done.append(f"{y}-{m:02d}")
        if not months_done:
            raise RuntimeError("今月以降の予定表を読めませんでした")
        sched_month = max(months_done)
        ok_a = True
    except Exception as e:  # noqa: BLE001
        notes.append(f"催事予定表を読めませんでした（{e}）")
        log.warning("miyagi-hall (a): %s", e)
        ev_a = [e for e in prev.get("events", []) if e.get("from_schedule")]

    if not ok_a and not ok_b:
        raise RuntimeError(" / ".join(notes))
    if not ok_b:
        ev_b = [e for e in prev.get("events", []) if not e.get("from_schedule")]
    events = ev_b + [a for a in ev_a if not any(same_event(a, b) for b in ev_b)]
    return {"events": events, "message": " / ".join(notes), "extra": {"schedule_month": sched_month}}


# ---------------------------------------------------------------- 仙台三越
MK_JSON = "https://www.mitsukoshi.mistore.jp/sendai/rss/site/events.json?ded=1"
MK_PAGE = "https://www.mitsukoshi.mistore.jp/sendai/event_calendar.html"
MK_PDF = ("https://www.mitsukoshi.mistore.jp/content/dam/isetan_mitsukoshi/site/sendai/"
          "event_calendar/event_calendar/sendai_event.pdf")
MK_VENUE = "仙台三越 本館7階 ホール"


def _mk_filter(items: list[dict]) -> list[dict]:
    picked = []
    for e in items:
        if e.get("datePattern") == "from":
            continue
        cats = {nf(t.get("title")) for t in e.get("categoryTag") or []}
        where = " ".join([nf(e.get("location"))] + [nf(t.get("title")) for t in e.get("floorTag") or []])
        if "催物場" in cats or any(p in where for p in ("ホール", "催物場", "催事場")):
            picked.append(e)
    paths = [e.get("path", "") for e in picked]
    return [e for e in picked if not any(p and e.get("path", "").startswith(p + "/") for p in paths)]


def mitsukoshi(prev: dict, ctx: Ctx) -> dict:
    notes = []
    ev_a, ok_a = [], False
    try:
        data = ctx.get(MK_JSON, headers={"Referer": MK_PAGE, "Accept": "application/json"}).json()
        for e in _mk_filter(data):
            ds, de = parse_date(e.get("dateFrom")), parse_date(e.get("dateTo"))
            if not ds:
                continue
            t = (e.get("dateTo") or "")[11:16]
            ev_a.append({"title": nf(e.get("navTitle")), "date_start": ds,
                         "date_end": de if de and de != ds else "",
                         "time": (f"最終日は{t}まで" if de != ds else f"{t}まで") if t else "",
                         "venue": nf(e.get("location")) or MK_VENUE,
                         "url": "https://www.mitsukoshi.mistore.jp" + e.get("path", "") + ".html",
                         "note": ""})
        ok_a = True
    except Exception as e:  # noqa: BLE001
        notes.append(f"イベント一覧を読めませんでした（{type(e).__name__}）")
        log.warning("mitsukoshi (a): %s", e)

    manual_prev = [e for e in prev.get("events", []) if e.get("manual")]
    manual_month = prev.get("manual_month", "")
    ev_b, ok_b = None, False
    try:
        res = mitsukoshi_pdf.parse(ctx.get(MK_PDF, headers={"Referer": MK_PAGE}).content, ctx.today)
        mon = res["month"]
        ev_b = []
        for p in res["events"]:
            note = f"催物ご案内（{int(mon[5:])}月）より"
            if p["to_month_end"]:
                note += "。翌月以降の会期は翌月の案内で確認"
            ev_b.append({"title": p["title"], "date_start": p["date_start"], "date_end": p["date_end"],
                         "time": "", "venue": MK_VENUE, "url": MK_PAGE, "note": note,
                         "manual": True, "pdf_month": mon})
        # 前月から続く催事は1件にまとめる
        first_day = f"{mon}-01"
        for e in ev_b:
            if e["date_start"] == first_day:
                for p in manual_prev:
                    if p.get("pdf_month", "") < mon and same_event(
                            {**p, "date_end": first_day}, {**e, "date_end": first_day}):
                        e["date_start"] = p["date_start"]
        keep = [p for p in manual_prev if p.get("pdf_month", "") != mon
                and not any(same_event(p, e) for e in ev_b)]
        ev_b = keep + ev_b
        manual_month = mon
        ok_b = True
        log.info("mitsukoshi PDF %s: %d件", mon, len(res["events"]))
    except Exception as e:  # noqa: BLE001
        notes.append(f"催物ご案内PDFを読めませんでした（{e}）")
        log.warning("mitsukoshi (b): %s", e)
        ev_b = manual_prev

    if not ok_a and not ok_b:
        raise RuntimeError(" / ".join(notes))
    if not ok_a:
        ev_a = [e for e in prev.get("events", []) if not e.get("manual")]
    events = ev_a + [b for b in ev_b if not any(same_event(b, a) for a in ev_a)]
    return {"events": events, "message": " / ".join(notes), "extra": {"manual_month": manual_month}}


# ---------------------------------------------------------------- 勾当台公園
PARK_URL = "https://www.city.sendai.jp/aoba-koen-somu/aobaku/koen/shokai.html"
HIKAPE_HINT = ("SENDAI光のページェント（定禅寺通のイルミネーション）の公式サイト。出力するのは次の2種類だけ："
               "(1) 光のページェント本体（title「SENDAI光のページェント」、会期、time に点灯時間、venue に点灯区間）、"
               "(2) 関連イベントのうち会場が勾当台公園（ビックテント・仮設広場など）のもの、または点灯式で、日時がはっきりしているもの。"
               "募金・クラウドファンディング・配布・SNS・写真コンクール・クリスマスマーケット（錦町公園）は出力しない。")
KOTODAIPARK_HINT = ("勾当台公園のイベントを紹介する記事の一覧。記事のタイトルに開催日と会場が書かれている。"
                    "会場が勾当台公園または定禅寺通のイベントだけを出力する（ほかの公園・商店街・店の中のものは出さない）。"
                    "毎週・毎月の定例の市（旬の香り市・朝市・夕市など）は出力しない。url は記事のリンク。")


def _park_table(ctx: Ctx) -> list[dict]:
    soup = BeautifulSoup(ctx.get(PARK_URL).text, "html.parser")
    out = []
    for table in soup.find_all("table"):
        rows = table.find_all("tr")
        if not rows:
            continue
        head = [nf(c.get_text(" ", strip=True)) for c in rows[0].find_all(["th", "td"])]
        idx = {k: next((i for i, h in enumerate(head) if k in h), None)
               for k in ("日", "時間", "イベント", "会場", "主催")}
        if idx["日"] is None or idx["イベント"] is None or idx["会場"] is None:
            continue
        for tr in rows[1:]:
            c = [nf(td.get_text(" ", strip=True)) for td in tr.find_all(["th", "td"])]
            if len(c) <= max(v for v in idx.values() if v is not None):
                continue
            venue = c[idx["会場"]]
            if "勾当台" not in venue:
                continue
            dtxt = c[idx["日"]]
            ds = parse_date(dtxt, ref=ctx.today)
            parts = re.split(r"[〜~～\-－]", dtxt)
            de = parse_date(parts[-1], ref=ctx.today) if len(parts) > 1 else None
            org = c[idx["主催"]].split("／")[0].split("/")[0].strip() if idx["主催"] is not None else ""
            if ds:
                out.append({"title": c[idx["イベント"]], "date_start": ds,
                            "date_end": de if de and de != ds else "",
                            "time": (c[idx["時間"]] if idx["時間"] is not None else "").replace("～", "〜"),
                            "venue": venue, "url": PARK_URL, "note": org})
    if not out:
        raise RuntimeError("公園イベント案内の表が見つかりません")
    return out


def kotodai(prev: dict, ctx: Ctx) -> dict:
    notes = []
    prev_ev = prev.get("events", [])
    table = _park_table(ctx)                       # (a) 失敗したら全体を失敗扱い
    tmin = min(e["date_start"] for e in table)
    regular = table + [e for e in prev_ev if not e.get("big") and e["date_start"] < tmin]

    big_new = []
    if ctx.today.month in (10, 11, 12):           # (b) 光のページェント
        try:
            text = ""
            for u in ("https://sendaihikape.jp/overview/", "https://sendaihikape.jp/event/"):
                text += html_to_text(ctx.get(u).text, u) + "\n\n"
            for ev in llm_events(ctx, text, page_url="https://sendaihikape.jp/overview/", sid="kotodai",
                                 name="SENDAI光のページェント", venue="", hint=HIKAPE_HINT):
                t = ev["title"]
                if re.search(r"募金|クラウド|写真|SNS|配布|カウントダウン|マーケット", t):
                    continue
                if "ページェント" not in t and "点灯式" not in t and "勾当台" not in (ev.get("venue") or ""):
                    continue
                big_new.append(from_llm(ev, big=True, note=ev.get("note") or "SENDAI光のページェント関連"))
        except Exception as e:  # noqa: BLE001
            notes.append(f"光のページェントの公式サイトを読めませんでした（{type(e).__name__}）")
            log.warning("kotodai (b): %s", e)
    try:                                           # (c) まとめサイト
        horizon = (ctx.today + timedelta(days=92)).isoformat()
        for u in ("https://www.kotodaipark.com/category/sendai-event/",
                  "https://www.kotodaipark.com/category/sendai-event/page/2/"):
            try:
                text = html_to_text(ctx.get(u).text, u)
            except requests.HTTPError:
                continue
            for ev in llm_events(ctx, text, page_url=u, sid="kotodai", name="勾当台公園イベント情報",
                                 venue="", hint=KOTODAIPARK_HINT):
                where = (ev.get("venue") or "") + ev["title"]
                if not re.search(r"勾当台|定禅寺", where) or re.search(r"旬の香り市|朝市|夕市", ev["title"]):
                    continue
                if ev["date_start"] > horizon:
                    continue
                big_new.append(from_llm(ev, big=True, note="まとめサイトの情報（公式で確認）"))
    except Exception as e:  # noqa: BLE001
        notes.append(f"まとめサイトを読めませんでした（{type(e).__name__}）")
        log.warning("kotodai (c): %s", e)

    big = []
    for e in big_new:
        if any(same_event(e, r) for r in regular) or any(same_event(e, b) for b in big):
            continue
        big.append(e)
    big += [p for p in prev_ev if p.get("big") and not any(same_event(p, b) for b in big)
            and not any(same_event(p, r) for r in regular)]
    return {"events": regular + big, "message": " / ".join(notes)}


# ---------------------------------------------------------------- 一番町健診クリニック
def morisanho(prev: dict, ctx: Ctx) -> dict:
    cal_path = ROOT / "data" / "clinic_calendar.json"
    cal = json.loads(cal_path.read_text(encoding="utf-8")) if cal_path.exists() else {}
    html = ctx.get("https://morisanho.or.jp/").text
    links = [urljoin("https://morisanho.or.jp/", h)
             for h in re.findall(r'href="([^"]*calendar[^"]*\.pdf)"', html, flags=re.I)]
    msg = ""
    if not links:
        msg = "サイト上で年間スケジュールPDFが見つかりませんでした"
    elif cal.get("pdf_url") not in links or not cal.get("floors"):
        try:
            new = clinic_pdf.parse(ctx.get(links[0]).content)
            cal = {"name": "一番町健診クリニック", "pdf_url": links[0],
                   "page_url": "https://morisanho.or.jp/#schedule", **new}
            cal_path.write_text(json.dumps(cal, ensure_ascii=False), encoding="utf-8")
            msg = f"{new['fiscal_year']}年度の年間スケジュールに更新しました"
        except Exception as e:  # noqa: BLE001
            msg = f"新しい年間スケジュールを読み取れませんでした（{e}）"
    period = cal.get("period") or []
    if period and ctx.today.isoformat() > period[1] and not msg:
        msg = "登録済みの年間スケジュールの期間が終わりました。新しいPDFが出たら自動で読み込みます"
    return {"events": [], "message": msg}


SITES = {
    "rensa": (rensa, True),
    "miyagi-hall": (miyagi_hall, True),
    "mitsukoshi": (mitsukoshi, False),
    "kotodai": (kotodai, True),
    "morisanho": (morisanho, False),
}
