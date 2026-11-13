"""定禅寺通りイベントカレンダー（試験版）の更新。GitHub Actions から週1回（または手動で）動かす。

  python updater/run.py                  # 全サイト
  python updater/run.py --only mitsukoshi,morisanho   # 一部だけ（LLMを使わないサイトの確認など）

1. data/sources/<サイト>.json（前回のデータ）を読む
2. 各サイトを取得し、必要なところだけ Ollama のモデルで抜き出す
3. 取れたサイトは書き換え、取れなかったサイトは前回の内容を残す
4. tools/build_events_json.py で公開用の docs/events.json を作る
5. 結果の報告（サイトごとの件数・かかった時間・本番との違い）を data/last_run.md に書く
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import logging
import os
import subprocess
import sys
import time
from pathlib import Path

import requests

sys.path.insert(0, str(Path(__file__).resolve().parent))
import llm  # noqa: E402
import sites  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
SRC_DIR = ROOT / "data" / "sources"
JST = dt.timezone(dt.timedelta(hours=9))
PROD_JSON = "https://everydaylogic.github.io/jozenji-display/events.json"

log = logging.getLogger("run")


def ollama_cfg() -> dict:
    return {
        "host": os.environ.get("OLLAMA_HOST_URL", "http://127.0.0.1:11434"),
        "model": os.environ.get("OLLAMA_MODEL", "qwen3:8b"),
        "num_ctx": int(os.environ.get("OLLAMA_NUM_CTX", "8192")),
        "chunk_chars": int(os.environ.get("CHUNK_CHARS", "4000")),
        "timeout": 1800,
        "think": False,
    }


def finalize(events: list[dict], prev: list[dict], ctx) -> list[dict]:
    ms = ctx.month_start.isoformat()
    seen, out = set(), []
    prev_keys = {(e.get("title"), e.get("date_start")) for e in prev}
    for e in events:
        if not e.get("title") or not e.get("date_start") or sites.end_of(e) < ms:
            continue
        k = (sites.key(e["title"]), e["date_start"])
        if k in seen:
            continue
        seen.add(k)
        e["is_new"] = bool(prev) and (e["title"], e["date_start"]) not in prev_keys \
            and not any(sites.same_event(e, p) for p in prev if p.get("title") and p.get("date_start"))
        out.append(e)
    out.sort(key=lambda e: (e["date_start"], e["title"]))
    return out


def compare_with_prod(ctx) -> list[str]:
    """本番の events.json と、今月・来月分のイベントを見比べる。"""
    try:
        prod = requests.get(PROD_JSON, timeout=30).json()
    except Exception as e:  # noqa: BLE001
        return [f"本番の events.json を取得できませんでした（{e}）"]
    mine = json.loads((ROOT / "docs" / "events.json").read_text(encoding="utf-8"))
    y, m = ctx.today.year, ctx.today.month
    nxt = f"{y + (m == 12)}-{(m % 12) + 1:02d}"
    lim = f"{nxt}-31"
    def pick(d):
        return [e for e in d["events"] if e["start"] <= lim and e["end"] >= ctx.month_start.isoformat()]
    a, b = pick(prod), pick(mine)
    lines = ["| サイト | 本番 | 試験 | 本番にだけある | 試験にだけある |", "|---|---|---|---|---|"]
    for sid in sorted({e["s"] for e in a + b}):
        pa = [e for e in a if e["s"] == sid]
        pb = [e for e in b if e["s"] == sid]
        def match(x, ys):
            return any(sites.same_event(
                {"title": x["title"], "date_start": x["start"], "date_end": x["end"]},
                {"title": y_["title"], "date_start": y_["start"], "date_end": y_["end"]}) for y_ in ys)
        only_a = [f'{e["start"][5:]} {e["title"][:24]}' for e in pa if not match(e, pb)]
        only_b = [f'{e["start"][5:]} {e["title"][:24]}' for e in pb if not match(e, pa)]
        lines.append(f"| {sid} | {len(pa)} | {len(pb)} | {'<br>'.join(only_a) or '－'} | {'<br>'.join(only_b) or '－'} |")
    return [f"今月と来月（〜{nxt}）の分を、本番の公開データと比べた結果：", ""] + lines


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--only", help="カンマ区切りのサイトID")
    ap.add_argument("--today", help="基準日（テスト用）")
    a = ap.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

    today = dt.date.fromisoformat(a.today) if a.today else dt.datetime.now(JST).date()
    ctx = sites.Ctx(today, ollama_cfg())
    only = set(a.only.split(",")) if a.only else None
    report = [f"# 更新結果 {dt.datetime.now(JST):%Y-%m-%d %H:%M}（モデル: {ctx.ollama['model']}）", "",
              "| サイト | 結果 | 件数 | 新着 | 時間 | メモ |", "|---|---|---|---|---|---|"]
    t_all = time.time()
    cache_path = ROOT / "data" / "llm_cache.json"
    if cache_path.exists():
        llm.CACHE.update(json.loads(cache_path.read_text(encoding="utf-8")))
    for sid, (fn, uses_llm) in sites.SITES.items():
        if only and sid not in only:
            continue
        path = SRC_DIR / f"{sid}.json"
        doc = json.loads(path.read_text(encoding="utf-8"))
        now = dt.datetime.now(JST).isoformat(timespec="seconds")
        t0 = time.time()
        try:
            res = fn(doc, ctx)
            doc["events"] = finalize(res["events"], doc.get("events", []), ctx)
            doc.update(res.get("extra", {}))
            doc.update(ok=True, fetched_at=now, last_ok_at=now, message=res.get("message", ""))
            status = "成功"
        except Exception as e:  # noqa: BLE001
            log.exception("%s failed", sid)
            doc.update(ok=False, fetched_at=now, message=str(e)[:200])
            status = "失敗"
        sec = time.time() - t0
        n = len(doc.get("events", []))
        new = sum(1 for e in doc.get("events", []) if e.get("is_new"))
        report.append(f"| {sid} | {status} | {n} | {new} | {sec/60:.1f}分 | {doc.get('message') or ''} |")
        path.write_text(json.dumps(doc, ensure_ascii=False, indent=1), encoding="utf-8")
        log.info("%s: %s %d件 %.1f分", sid, status, n, sec / 60)

    if llm.USED:
        keep = {k: v for k, v in llm.CACHE.items() if k in llm.USED}
        cache_path.write_text(json.dumps(keep, ensure_ascii=False), encoding="utf-8")
    subprocess.run([sys.executable, str(ROOT / "tools" / "build_events_json.py"), str(SRC_DIR),
                    str(ROOT / "docs" / "events.json"), "--clinic", str(ROOT / "data" / "clinic_calendar.json")],
                   check=True)
    report += ["", f"合計 {(time.time() - t_all)/60:.1f}分", ""] + compare_with_prod(ctx)
    text = "\n".join(report) + "\n"
    (ROOT / "data" / "last_run.md").write_text(text, encoding="utf-8")
    if os.environ.get("GITHUB_STEP_SUMMARY"):
        with open(os.environ["GITHUB_STEP_SUMMARY"], "a", encoding="utf-8") as f:
            f.write(text)
    print(text)
    return 0


if __name__ == "__main__":
    sys.exit(main())
