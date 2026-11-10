# herdr-delegate — Claude Code をブローカーにしたマルチエージェント委任機構

日付: 2026-07-10
ステータス: 設計確定（実装前）

## 目的

Claude Code（オーケストレーター）から、herdr のペインで動く別の AI コーディングエージェント
（別モデルの Claude Code、Codex CLI）をサブエージェントとして起動・監視・回収する機構。

内蔵 subagent でなく herdr 経由にする理由:

1. **モデル多様性** — Codex CLI (GPT 系) にタスクを振れる。クロスモデルレビュー等
2. **可視性・介入可能性** — サブエージェントがペインで見え、人間がいつでも覗く・引き継げる
3. **コンテキスト・quota 分離** — 重い作業を別プロセス別セッションへ。別 CLI の quota も使える
4. **長時間・非同期作業** — オーケストレーター終了後も生存し、後から結果回収できる

呼び出し形態は CLI + スキル先行。パターンが固まったら MCP サーバー化（サブコマンドは
将来の MCP ツールと 1:1 対応にしておく）。

## 全体構成

```
┌ Claude Code (オーケストレーター、任意のペイン) ─────────┐
│  skill: herdr-delegate (委任判断・モデルルーティング)      │
│    └─ Bash → herdr-delegate CLI                        │
└──────────────┬─────────────────────────────────────────┘
               │ spawn / wait / collect / send / kill
┌──────────────▼──────────────┐   ┌──────────────────────┐
│ herdr-delegate (Python)     │──▶│ herdr socket API     │
│ ~/.config/herdr/            │   │ agent start/send/wait │
│ (herdr-fork-agent と同系統)  │   │ worktree create 等    │
└──────────────┬──────────────┘   └──────────┬───────────┘
               │ 台帳                          │ ペイン起動
┌──────────────▼──────────────┐   ┌──────────▼───────────┐
│ ~/.config/herdr/delegate/    │   │ claude / codex (TUI)  │
│  tasks/<task-id>/            │◀──│ 契約: 完了時 result.md │
│   manifest.json / prompt.md  │   │ 状態は integration     │
│   result.md                  │   │ hook が herdr に報告   │
└─────────────────────────────┘   └──────────────────────┘
```

- **herdr-delegate CLI**: 唯一の新規実装。`herdr-fork-agent` と同じ Python 単体スクリプト、
  herdr socket 直叩き。dotfiles の `.config/herdr/` に置き symlink 運用
- **skill**: `~/.claude/skills/herdr-delegate/`（ユーザーレベル。どのプロジェクトからでも使う）
- **タスク台帳**: ファイルシステムが SSoT。オーケストレーターが死んでも別セッションから
  `status` / `collect` できる
- 前提: herdr の claude / codex integration がインストール済みで、agent_status
  (idle/working/blocked) がペインから herdr に報告されること

## サブコマンド

```
herdr-delegate spawn --agent claude|codex [--model X] [--worktree [branch]]
                     [--cwd PATH] [--label TEXT] [--safe] (--prompt "..."|--prompt-file P)
herdr-delegate status [<task-id>]         # 台帳 + herdr agent 状態の突合一覧
herdr-delegate wait <task-id> [--timeout MS]   # done/blocked/timeout まで block
herdr-delegate collect <task-id>          # result.md 出力 (無ければ pane read fallback)
herdr-delegate send <task-id> "追撃"       # 対話継続
herdr-delegate kill <task-id> [--rm-worktree]
```

### spawn の動作

1. task-id 採番 `dlg-<HHMMSS>-<slug>`、台帳ディレクトリ作成、manifest.json 記録
   （agent / model / pane_id / cwd / worktree / created_at）
2. 隔離: `--worktree` 指定時は `herdr worktree create --branch delegate/<task-id> --json`
   で専用 workspace。未指定は現 workspace の**新規タブ**（`herdr tab create` →
   `agent start --tab`）。オーケストレーターのタブを汚さず、ペインは常に人間が見える
3. 起動 argv:
   - `claude --model X --dangerously-skip-permissions "<契約付きプロンプト>"`
   - `codex -m X --dangerously-bypass-approvals-and-sandbox "<契約付きプロンプト>"`
   - **初回プロンプトは argv 渡し**（TUI への send+Enter タイミング問題を初手で回避）
   - `--safe` で yolo 無効化
4. yolo 既定の根拠: 無人で回すには承認プロンプトによる blocked 連発を避ける必要があり、
   書き込み系タスクは worktree 隔離が保険になる。既存 `herdr-fork-agent` と同方針

### 契約（プロンプトに埋め込むフッター）

```
--- 委任契約 ---
完了したら {task_dir}/result.md に以下の形式で書き込むこと:
  status: done|failed
  summary: 結果 3-10 行
  artifacts: 変更ファイル/ブランチ/PR URL の列挙
result.md を書くまでタスク完了とみなされない。
質問・承認が必要なら result.md に status: blocked と理由を書いて停止。
```

## 完了検知と状態遷移

二重シグナル（herdr の agent_status × result.md の有無）で判定する。

| herdr agent_status | result.md | 判定 |
|---|---|---|
| working | — | 継続 wait |
| idle | あり | **done** (exit 0) |
| idle | なし | `send` で「契約どおり result.md を書け」と 1 回催促 → 再 wait。それでも無ければ **stalled** (exit 3、pane 末尾 40 行を stderr に添付) |
| blocked | — | **blocked** (exit 2) + `herdr notification show`。オーケストレーターが `agent read` で状況を読み、`send` で回答するか人間へエスカレーション |
| pane 消失 | — | **failed** (exit 4) |
| `--timeout` 到達 | — | **timeout** (exit 6。旧 exit 3 から分離=互換破壊) |
| working で transcript/git に `--stall-secs` (600s) 進捗なし | — | 1 回 `herdr notification` (manifest `stall_notified_at` でラッチ)。さらに `--stall-grace` (300s) 変化なしで **quiet** (exit 5) |
| idle かつ Stop hook が催促を使い切った (`protocol_violation` マーカー) | なし | **protocol_violation** (exit 1) |
| herdr 到達不能 / preflight / 設定エラー / `--max-concurrent` 超過 | — | exit 8 (予約: 7 limited) |

進捗シグナル: claude は `~/.claude/projects/<cwd の英数字以外を - に置換>/*.jsonl` の最新、
codex は `~/.codex/sessions/YYYY/MM/DD/rollout-*.jsonl` のうち spawn 後・cwd 一致の最新の mtime+size。
取れなければ `git status --porcelain` の hash。観測失敗時はラッチと計時を保持する。

終了契約 (claude): `--settings worker-settings.json` の Stop hook が result の有効な status 行 (result.md / result.json)
を確認し、無ければ最大 2 回 block して書き込みを催促する (`stop_nudges`)。codex は wait 側の催促を 2 回に拡張。
結果ファイル: `result.json` (構造化) は `result.md` に優先。`spawn --schema` は required/type のみ検証し補正 1 回。

オーケストレーター側の待ち方: バックグラウンド実行で `herdr-delegate wait` を走らせ、
完了時に再起床する。ポーリング不要で複数タスクの fan-out も自然に書ける。

## スキル（オーケストレーション規約）

スキルは「いつ・何を・どのモデルに委任するか」の判断と CLI の使い方を担う。

### 委任判断

内蔵 subagent で足りるもの（短い調査、read-only 検索）は委任しない。herdr-delegate を
使うのは冒頭 4 目的のいずれかに該当するときだけ。

### モデルルーティング

| タスク難易度 | agent/model |
|---|---|
| 機械的（rename、定型追加） | claude --model haiku |
| 標準（実装・bugfix） | claude --model sonnet |
| 複雑（設計・複雑デバッグ） | claude --model opus |
| クロスモデルレビュー・セカンドオピニオン | codex |

### 隔離の自動選択

プロンプトが書き込みを伴う（実装・修正・リファクタ）→ `--worktree` 付与。
調査・レビュー・分析 → 現 CWD。迷ったら worktree。

### 定型レシピ

- **クロスモデルレビュー**: claude で実装 → 同じ diff を codex に
  `spawn --agent codex --prompt "レビューして result.md へ"` → 指摘を突合
- **fan-out**: 独立タスク N 件を spawn ×N → 各 `wait` をバックグラウンドで → 完了順に collect
- **長時間放流**: spawn だけしてターンを終える。後続セッション（または人間）が
  `status` / `collect`

## エラー処理・エッジケース

- **追撃 send の Enter 問題**: `agent send` は literal text。send → 300ms →
  `pane send-keys enter` → `agent read --lines 3` でエコーバック確認、不一致なら 1 回リトライ
- **task-id / ブランチ衝突**: id は時刻+乱数 suffix。worktree ブランチは
  `delegate/<task-id>` 固定で衝突しない
- **quota 切れ・ログイン失効**: agent_status が idle のまま result 無し → stalled 経路で
  pane 末尾が stderr に出るので原因が見える
- **オーケストレーター死亡**: 台帳とペインは残る。`status` が manifest と
  `herdr agent list` を突合し、孤児タスク（pane 消失）を failed 表示
- **cleanup 規律**: done を collect した後の `kill --rm-worktree` はスキルが明示的に行う
  （自動削除しない。人間が覗く猶予を残す）

## テスト計画

CLI は手動スモークで検証（個人ツール、対話 TUI 相手の自動化はコスパが悪い）:

1. claude spawn（同一 CWD・調査タスク）→ wait → collect が result.md を返す
2. codex spawn 同上（契約フォーマットを codex が守るか確認）
3. `--worktree` spawn → ブランチ・workspace 生成確認 → `kill --rm-worktree` で掃除
4. `--safe` で承認プロンプト発生 → blocked 検知 → send で回答 → done まで
5. fan-out 2 並列 → 双方 collect

## 却下した代替案

- **headless (`claude -p` / `codex exec`) をペインで走らせる**: 完了検知・結果契約は最も堅い
  （プロセス終了 + JSON 出力）が、途中介入・対話継続ができず可視性・介入可能性の目的と矛盾。
  将来 `spawn --headless` オプションとして統合可能な設計にしてある
- **新規実装なし（スキルのみで生 herdr コマンドを教える）**: 毎回オーケストレーション
  ロジックを LLM が再構築することになり、タスク台帳もなく、Enter 送信漏れ・blocked 放置
  などの失敗パターンを踏みやすい

## v2 (2026-11)

- **台帳 state machine**: manifest に `state/events/finished_at/exit_reason/delivered_at/base_commit`。終端は done/failed/gone/killed/protocol_violation。tmp+rename+flock で書込。旧 manifest は欠損キー許容。
- **exit code**: 0-4 維持、5 quiet、6 timeout (旧 3)、7 limited、8 preflight/設定/herdr 到達不能。
- **終了契約**: Stop hook (催促2回→protocol_violation)、`result.json` + `--schema`、blocked の `kind/question`。
- **安全装置**: worker 用 `permissions.deny` フロア、`kill --rm-worktree` の archive ref 退避、`doctor`/`reconcile`、`--idempotency-key`、`--max-concurrent`。
- **検証**: `--verify/--constraints/--stop-when` 契約、`--gate` 実行、`--parent` 連鎖、`wait ID.../--group`。
- **運用**: 枠切れ検出 + `health` + `--agent auto`、pane 表示 (`report-metadata`)、非 worktree の `diff`/`restore` snapshot。
- **周辺**: SessionStart hook で再注入、`herdr-ultra-watch` (Workflow 可視化, docs/herdr-ultra-watch.md)、`herdr-recall` (履歴 FTS 検索)。

