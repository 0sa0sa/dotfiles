---
name: herdr-delegate
description: Use when delegating tasks to other AI coding agents (Codex CLI, or Claude Code with a different model) running in visible herdr panes — cross-model review, second opinions, offloading heavy/long-running work to a separate session/quota, or fan-out of independent tasks. Trigger phrases: "codex に投げて", "別モデルでレビュー", "herdr で並列に", "サブエージェントに委任", "cross-model review".
---

# herdr-delegate — herdr ペインへのタスク委任

`herdr-delegate` CLI で claude / codex サブエージェントを herdr ペインに spawn し、
完了を待って結果を回収する。台帳: `~/.config/herdr/delegate/tasks/<task-id>/`。

## いつ使うか

内蔵 Agent tool で足りる短い調査・read-only 検索には使わない。使うのは:
1. Codex (GPT 系) の視点が欲しい（クロスモデルレビュー等）
2. 人間が途中で覗く・引き継ぐ可能性がある並列作業
3. メイン context を消費したくない重量級タスク
4. セッションを跨ぐ長時間タスク（spawn してターンを終え、後で collect）

## コマンド

```bash
herdr-delegate spawn --agent claude|codex [--model X] [--worktree [BRANCH]] \
    [--cwd PATH] [--label TEXT] [--safe] [--idempotency-key K] \
    [--schema FILE] [--parent ID[,ID]] [--group NAME] [--max-concurrent N] [--no-deny-floor] \
    (--prompt "..."|--prompt-file P)
# → {"task_id": ..., "pane_id": ..., "task_dir": ...}
herdr-delegate wait <task-id> [--timeout MS] [--stall-secs 600] [--stall-grace 300]
herdr-delegate wait <id>... | --group NAME [--all|--any]   # JSON サマリを1回出力
herdr-delegate collect <task-id>              # result を表示 + artifacts を絶対パス化
herdr-delegate send <task-id> "追撃メッセージ"
herdr-delegate status [--json] [--active] [--format reinject]
herdr-delegate reconcile [--mark-delivered]   # 未配信の完了 task を列挙 (pending)
herdr-delegate kill <task-id> [--rm-worktree [--force-discard]]
herdr-delegate doctor [--json] [--reap]       # crashed/overtime/stranded/孤立 worktree。所見ありで exit 1
```

### exit code (`wait`)

| code | 意味 |
|---|---|
| 0 | done |
| 1 | failed |
| 2 | blocked（result.md に status: blocked、または agent 自身が blocked） |
| 3 | stalled（アイドル継続。ペイン末尾に原因） |
| 4 | pane 消失 |
| 5 | quiet（working だが進捗なし: transcript/git が `--stall-secs`+`--stall-grace` 変化なし。`--stall-secs` 超で通知1回） |
| 6 | timeout（`--timeout` 到達）。**旧: 3 (stalled と同コード)。互換破壊** |
| 7 | limited（idle/blocked で result なしのとき pane 末尾の枠切れ表示を検出。`health` に記録、state は再開可能） |
| 8 | preflight 失敗 / 設定エラー / herdr 到達不能 / `--max-concurrent` 超過（旧: 到達不能は 4 相当） |

Stop hook の催促 (最大2回) を使い切っても result が無い worker は state=`protocol_violation`、exit 1 (status は failed)。
複数 wait の終了コードは全 done なら 0、他は最初の非 0。blocked は即時に stderr へ1行 (kind/question 付き)。

`status` も herdr に到達できないと 8 で終わる (GONE 一覧にはならない)。

## モデルルーティング

| タスク | 指定 |
|---|---|
| 機械的 (rename・定型) | `--agent claude --model haiku` |
| 標準実装・bugfix | `--agent claude --model sonnet` |
| 設計・複雑デバッグ | `--agent claude --model opus` |
| クロスモデルレビュー・セカンドオピニオン | `--agent codex` |

## 隔離の自動選択

- プロンプトが書き込みを伴う（実装・修正・リファクタ）→ `--worktree` を付ける
- 調査・レビュー・分析のみ → 現 cwd でよい
- 迷ったら `--worktree`

## 待ち方

`wait` は Bash の run_in_background で走らせる。完了時に自動で起こされる。
複数タスクは spawn を先に全部済ませ、wait を並べて background 実行（fan-out）。

exit 2 (blocked) が返ったら: stderr のペイン末尾を読み、`send` で回答できるなら回答、
判断が必要なら人間にエスカレーション。exit 3 (stalled) はペイン末尾に原因
（quota 切れ・ログイン失効等）が出ているので確認する。exit 6 (timeout) は worker が生きている場合があるので、再 `wait` か `status` で判断。
exit 8 は herdr 起動・PATH・cwd・idempotency-key 衝突を確認。

## 初回フォルダ確認ダイアログ（trust prompt）

エージェントが初めて触るディレクトリでは、CLI 側の「このフォルダを信頼するか」的な
確認ダイアログが出ることがある。`--dangerously-skip-permissions` /
`--dangerously-bypass-approvals-and-sandbox` はこれを抑止しない。

- **claude**: `wait` に一回限りの自動 Return ワークアラウンドが組み込み済み
  （ダイアログの文言を検知して自動で Enter を送る）。`status` 確認中にこの
  画面が一瞬見えても、これは想定内でありユーザーの操作は不要。
- **codex**: 実測 (2026-07-10) では文言が異なる
  （`Do you trust the contents of this directory? ... 1. Yes, continue`）が、
  この文言も `wait` の自動検知パターンに追加済みで、claude と同様に
  一回限りの自動 Return ワークアラウンドで自動処理される。`status` 確認中に
  この画面が一瞬見えても、これは想定内でありユーザーの操作は不要。

## レシピ

**クロスモデルレビュー:**
```bash
herdr-delegate spawn --agent codex --label review-x \
  --prompt "$(git diff main...HEAD | head -c 50000)

上記 diff をレビューし、指摘を result.md の summary に列挙せよ。"
```

**長時間放流:** spawn だけしてターンを終える。次のセッションで
`herdr-delegate status` → `collect`。

## 掃除の規律

collect し終えたタスクは `kill`（worktree 使用時は `--rm-worktree`）。
自動削除はされない — 人間がペインを覗く猶予を残すため。

`kill --rm-worktree` には既知の穴がある: pane/workspace がこのツール経由でなく
外側で既に閉じられていた場合、`kill --rm-worktree` は無音で no-op し、
worktree とブランチがディスク上に孤立する。`git worktree list` で `kill` 後に
残骸が見えたら、手動で `git worktree remove --force <path>` +
`git branch -D <branch>` で片付ける。

## 終了契約・安全装置 (A2)

- claude worker は `--settings <task>/worker-settings.json` で Stop hook
  (`~/.config/herdr/hooks/herdr-delegate-stop.py`) と `permissions.deny` フロア
  (`git push --force*` / `rm -rf /` 系 / `herdr pane close*` / `herdr worktree remove*` / `herdr server stop*`) を受ける。
  `--no-deny-floor` で deny のみ外せる。適用 deny は manifest の `deny_floor`。
  **codex は hook も deny も不可** (催促2回のみ)。隔離は `--worktree` / `--safe` で担保すること。
- 構造化結果: `spawn --schema FILE` (`required` と `properties.<k>.type` のみ検証) で worker は
  `result.json` (`status/summary/artifacts` + schema のキー) を書く。違反は補正を1回送り、
  それでも違反なら警告つきで done (manifest `schema_valid=false`)。`result.json` は `result.md` より優先。
- blocked は `kind: needs_input|capability|transient|dependency` と `question:` を併記 (未記載は needs_input)。
- 連鎖: `spawn --parent <id>` で親の summary / branch / `git diff --stat` が prompt 先頭に入る。
- 並列: 全 spawn 後に `wait --group NAME` を1本だけ background で走らせる。`--max-concurrent` は助言的ガード。
- `kill --rm-worktree`: commit/未コミットがあれば `refs/herdr/archive/<id>[-dirty]` に退避してから削除。
  未コミットありは `--force-discard` 必須。検査失敗は削除せず `inspection_failed`。pane/workspace が既に無くても git で外す。
- `doctor --reap` は完了済みで pane が残る task の pane を閉じる (worktree は残す)。

## 契約・gate・枠切れ・snapshot (A3)

- 契約: `spawn --verify T --constraints T --stop-when T` は prompt 末尾「## Contract」に入る。
  worker は result に「## Verification evidence」節 (result.json は `verification_evidence`) を書くこと。無い done は blocked(needs_input)。
- gate: `--gate CMD` (複数可, `--gate-timeout 300`, `--gate-retries 3`)。done 報告時に worker の cwd で実行し、失敗は出力末尾 3KB を `send` 相当で差し戻し
  (result は退避) wait 継続。N 回失敗で failed (exit 1, exit_reason=`gate_failed`, notification)。`collect` 単独では gate は走らない (wait 経由のみ)。
- 枠切れ: exit 7 + `~/.config/herdr/delegate/health.json`。`herdr-delegate health [CLI] [--reset]` で確認/解除 (期限切れは自動復帰)。
  `spawn --agent auto` は枠切れでない CLI を選ぶ (claude 既定 → codex。manifest `fallback_from`)。検出文言は想定ベースで実機未採取。
- pane 表示: task state 遷移ごとに `herdr pane report-metadata --source herdr-delegate --custom-status <state>` を反映 (失敗は無視)。
- snapshot: 非 worktree かつ git repo の spawn 前後を `refs/herdr-delegate/<id>/{pre,post}` に保存。
  `diff <id> [--stat]`、`restore <id> [--file F]` (復元前に `pre-restore` を取り、worker が触れたファイルのみ戻す。running 中は拒否)。
  `kill` で ref を GC (`--keep-snapshot` で残す)。

## 規約: 契約と Stop hook

- worker は終了時に必ず `$HERDR_DELEGATE_RESULT` (result.md) か result.json に `status: done|failed|blocked` を書く。
- Stop hook は status が無いと最大2回 block して催促 → 使い切ると `protocol_violation` (exit 1)。
- 指示は ledger の `result.md` 先頭に `status:` / `summary:` / `artifacts:`。`## Verification evidence` は契約ありの時のみ必須。

## 再入・回復

- SessionStart hook (`~/.config/herdr/hooks/herdr-delegate-sessionstart.sh`, startup|resume|compact) が
  稼働中 task と未回収の完了 task を additionalContext に再注入する (REFERENCE ONLY)。
- 手動: `herdr-delegate reconcile` (pane 消失を gone に確定し未回収を列挙) → `collect` → `kill`。
- 冪等: `spawn --idempotency-key K` は同 prompt なら既存 task を `existing: true` で返す。別 prompt は exit 8。
- 診断: `doctor` (所見あり exit 1)。枠切れは `health`。作業前後の差分は `diff` / 戻しは `restore`。

## Swarm レシピ

1. **decide-before-fan-out**: 並列 worker が共有する判断 (API 形・命名・スキーマ) は先に自分で決め、
   全 prompt に同一の「## Decisions」ブロックとして貼る。worker に再決定させない。
2. **fan-out**: `--group G --max-concurrent N` で spawn し、`wait --group G --all` を background で1本。
3. **verifier**: 実装 worker と別モデルの verifier を `--parent <impl-id> --verify "..." --gate "<test cmd>"` で spawn
   (claude 実装 → `--agent codex` で検証、など)。
4. **reconcile**: 結果が食い違う/統合が要る場合は、親 id を全部 `--parent` に並べた第三の worker に統合させる。
   自分では中継しない。

## 関連 CLI (herdr-ultra-watch / herdr-recall)

- `herdr-ultra-watch dashboard [RUN|--latest] [--once]` / `tail <agent>` / `open` / `daemon`:
  ultracode (Workflow) の phase・agent 進捗を herdr ペインに表示。PostToolUse(Workflow) hook で自動オープン。
- `herdr-recall search QUERY [--project P] [--since 7d] [--json]` / `show SESSION [--around TS]` / `recent` / `index`:
  Claude/Codex 過去セッションの全文検索 (`~/.config/herdr/recall.db`)。delegate task と突き合わせ表示。

