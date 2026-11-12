# herdr-orch: オーケストレーター記憶 設計

## 目的

`herdr-delegate` で worker を回すオーケストレーターが、セッションを跨いで次を失わないようにする。

- 目標と決定事項 (ミッション)
- 並列 worker 間の共有状況 (共有ボード)
- 過去の委任失敗から得た運用知識 (教訓)
- どの agent/model がどの種類のタスクに向くかの実績 (stats / recommend)

実装は単一ファイル CLI `.config/herdr/herdr-orch` (Python3 stdlib のみ)。
`herdr-delegate` が spawn / wait の節目で best-effort 連携する。連携が失敗しても委任は止まらない。

## データ配置と権限

| 対象 | 場所 | 備考 |
|---|---|---|
| ルート | `~/.config/herdr/orchestrator/` (env `HERDR_ORCH_DIR`) | dotfiles の外。ディレクトリ 0700 |
| ボード | `boards/<slug>.md` | 追記専用、flock で排他 |
| 教訓 | `lessons.jsonl` | 1 行 1 JSON |
| ミッション | `missions/<id>.json` | |
| カレント | `current.json` | `{key: mission id}` |
| delegate 台帳 (読み取り) | `~/.config/herdr/delegate/tasks/` (env `HERDR_DELEGATE_DIR`) | manifest.json, prompt.md, result.* |

ファイルは 0600。リポジトリ (dotfiles) には実データを置かない。テストは tempdir + 上記 env で隔離する。

## 機能

### 共有ボード

group (`herdr-delegate spawn --group`) 単位の追記ログ。
`herdr-orch board post|show|path|claims`。1 エントリ `- [時刻] [kind] (task) 1行目` + 続きは 2 スペースインデント。
kind は note / claim / finding / done。`claims` は同じ task の後続 done が無い claim = 着手中の主張。
GROUP は slug 化 (`[A-Za-z0-9._-]` 以外は `-`、先頭 `.` 禁止、64 字) しパストラバーサルを防ぐ。

### 教訓 (lessons)

`lessons.jsonl` の各行: `id, ts, tags[], text, source_task, expires_at, status(active|candidate)`。
`add` (手動 = active、ttl 既定 90d) / `list` / `forget` / `promote` / `prune` / `relevant` / `capture`。
`relevant` はプロンプトの語 (英数語 + CJK 2-gram) とタグ/本文の重なり + agent タグ一致でスコアし、0 点は出さない。
`capture TASK_ID` は失敗系 state の task から candidate を自動生成 (同一 source_task は冪等)。

### ミッション

セッションを跨ぐ目標。`mission new|list|show|note|decide|close|use|current`。
カレントは env `HERDR_MISSION` > `current.json` (key = `HERDR_PANE_ID` or `default`)。
`show` は manifest の `mission` が一致する task を走査して task_id / state / label を併記する。

### stats / recommend

`classify` でプロンプトを review / fix / implement / refactor / test / research / docs / other に分類
(委任契約フッターと親タスク節は除外)。
`stats [--since] [--by agent|model|kind|agent-model]`: n, done, failed, blocked, other, success_rate, median_secs, gate_failures, 上位 exit_reason。
success_rate = done / (n - running - killed)。
`recommend`: kind 別に n_rated>=3 の agent-model 群から最良 (success_rate、同率は median_secs が短い方) を返す。
不足時は既定表 (review→codex, fix/implement/research/other→claude sonnet, refactor/docs→claude haiku) を `basis: default` で返す。

### context / brief

- `context`: worker prompt に足す節 (`## 共有ボード` / `## ミッション` / `## 過去の教訓`)。全体 4000 字上限。
  herdr-delegate の create_task が親タスク節 + 本文の後、委任契約フッターの前に挿入 (`--no-orch-context` で抑止)。
  ボード内容は他 worker 由来の参照データで指示ではないと明記する。例外は握り潰して exit 0。
- `brief`: SessionStart hook (`herdr-delegate-sessionstart.sh`) が呼び additionalContext に連結。
  active mission と candidate lessons 件数、最大 15 行、`REFERENCE ONLY:` 始まり。herdr-orch が無ければ黙って省略 (8 秒 timeout)。

### herdr-delegate 連携

- spawn: `--mission ID` (既定 env `HERDR_MISSION` → `mission current`)、`--no-orch-context`。manifest に `mission`。
- wait 終端: done/killed 以外なら `lessons capture`、group があれば `board post --kind done`。stdout JSON と終了コードは変えない。
- herdr-orch の場所: env `HERDR_ORCH_BIN` > herdr-delegate と同じディレクトリ > PATH。

## auto memory との分担

| | auto memory | herdr-orch |
|---|---|---|
| 内容 | ユーザー嗜好・プロジェクト背景 | ワーカー運用知識・ミッション・ボード・実績 |
| 書き手 | Claude / ユーザー | オーケストレーター、herdr-delegate の自動連携 |
| 寿命 | 長期 | 教訓は TTL 付き、ボードは group 単位 |

重複させない。ユーザーの好みは auto memory、「この種のタスクで worker が詰まった理由」は lessons。

## 失効とプライバシー方針

- 教訓: 既定 TTL 90 日 (`--ttl 0` で無期限)。失効は `relevant` / 注入から除外、`lessons prune` で削除。
- 自動生成は candidate。人/オーケストレーターが `promote` するまで worker に注入しない。brief が件数を通知する。
- データは dotfiles の外 (0700/0600)。実名・社名・実パス・秘匿値を書かない。fixture は `jane.doe` / `/home/jane` 等の架空値。
- 注入される内容 (ボード・教訓) は参照データであり、worker への指示として扱わない。
