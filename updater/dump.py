"""各サイトのページを、そのまま dump/ に保存するだけ（読み取り方を作るための材料集め。LLMは使わない）。"""
from __future__ import annotations

import datetime as dt
import re
import sys
from pathlib import Path
from urllib.parse import urljoin

sys.path.insert(0, str(Path(__file__).resolve().parent))
import sites  # noqa: E402

OUT = Path(__file__).resolve().parent.parent / "dump"


def save(name: str, data: bytes | str):
    OUT.mkdir(exist_ok=True)
    p = OUT / name
    p.write_bytes(data if isinstance(data, bytes) else data.encode("utf-8"))
    print("saved", name, p.stat().st_size)


def main():
    ctx = sites.Ctx(dt.datetime.now(sites.__dict__.get("JST", dt.timezone(dt.timedelta(hours=9)))).date(), {})
    jobs = []
    for y, m in ctx.months(3):
        jobs.append((f"rensa_{y}{m:02d}.html", f"http://www.rensa.jp/schedule/?index={y}%2F{m:02d}"))
    jobs += [("hall_event.html", "https://miyagi-hall.jp/event/"),
             ("hall_top.html", "https://miyagi-hall.jp/"),
             ("park.html", sites.PARK_URL),
             ("hikape_overview.html", "https://sendaihikape.jp/overview/"),
             ("hikape_event.html", "https://sendaihikape.jp/event/")]
    for name, url in jobs:
        try:
            r = ctx.get(url)
            save(name, r.content)
        except Exception as e:  # noqa: BLE001
            print("FAILED", name, url, e)
    try:
        top = (OUT / "hall_top.html").read_text(errors="ignore")
        first = re.search(r'href="(https://miyagi-hall\.jp/schedules/\d+/|/schedules/\d+/)"', top)
        url0 = urljoin("https://miyagi-hall.jp/", first.group(1))
        html0 = ctx.get(url0).text
        ids = sorted({int(i) for i in re.findall(r"/schedules/(\d+)/", html0 + top)}, reverse=True)[:4]
        for i in ids:
            purl = f"https://miyagi-hall.jp/schedules/{i}/"
            html = ctx.get(purl).text
            save(f"hall_sched_{i}.html", html)
            pdf = re.search(r'href="([^"]+/schedules/[^"]+\.pdf)"', html)
            if pdf:
                save(f"hall_sched_{i}.pdf", ctx.get(urljoin(purl, pdf.group(1))).content)
    except Exception as e:  # noqa: BLE001
        print("FAILED hall schedules", e)


if __name__ == "__main__":
    main()
