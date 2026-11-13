# 定禅寺通りイベントカレンダー（試験版）

本番 [jozenji-display](https://github.com/everydaylogic/jozenji-display) のデータ更新を、
Claude ではなく **GitHub Actions ＋ ローカルLLM（Ollama）** で回せるかを確かめるためのリポジトリです。
画面（`docs/`）と公開用データの作り方（`tools/build_events_json.py`）は本番と同じです。

## 動かし方

1. 「Actions」タブ →「イベントデータの更新（試験）」→「Run workflow」
2. 終わったら、その実行のページの一番下に結果（サイトごとの件数・時間・本番との違い）が出ます
3. 変更があれば `docs/events.json` が更新され、Pages に反映されます

最初は `only` に `mitsukoshi,morisanho` を入れると、LLMを使わない部分だけを数分で確認できます。

## 何をどう取っているか

| サイト | 取り方 | LLM |
|---|---|---|
| Rensa | 月別スケジュール3か月分のHTMLを、ページの作り（日付・出演者・タイトルの欄）から読む | 使わない |
| 東京エレクトロンホール宮城 | 催事予定表PDFの表＋主催公演一覧のHTMLを、欄ごとに読む | 使わない |
| 仙台三越 催物場 | イベントJSON＋催物ご案内PDF（帯の表を線と文字の位置から読む） | 使わない |
| 勾当台公園 | 市の公園イベント案内の表（使わない）／光のページェント公式・まとめサイト（使う） | 一部使う |
| 一番町健診クリニック | 年間スケジュールPDFの色分けを読む（新しいPDFのときだけ） | 使わない |

最初の試験（全部LLMで読む方式）は1時間48分かかり、Rensa とホールで日付の取り違えが多かったため、
作りの決まっているページはLLMを使わずに読む方式に切り替えた。
LLMへの問い合わせは結果を `data/llm_cache.json` に残し、ページが変わっていなければ使い回す。

LLMの出力は、タイトルがページの本文に実在するかを照合し、無いもの（作り話）は捨てています。
取れなかったサイトは前回のデータを残します。

## ファイル

```
updater/run.py            更新の本体（サイトごとに実行 → events.json 作成 → 結果の報告）
updater/sites.py          サイトごとの取り方
updater/llm.py            本文の整形・Ollama 呼び出し・作り話の除去
updater/mitsukoshi_pdf.py 三越の催物ご案内PDFの読み取り
updater/clinic_pdf.py     クリニックの年間スケジュールPDFの読み取り
updater/dates.py          和暦などの日付の統一
data/sources/*.json       サイトごとのデータ（前回分。毎回書き換え）
data/clinic_calendar.json 健診の年間スケジュール
data/last_run.md          直近の更新結果
docs/                     公開ページ（本番と同じ）
tools/                    公開用 events.json の作成（本番と同じ）
```
