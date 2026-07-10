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
    [--cwd PATH] [--label TEXT] [--safe] (--prompt "..."|--prompt-file P)
# → {"task_id": ..., "pane_id": ..., "task_dir": ...}
herdr-delegate wait <task-id> [--timeout MS]
herdr-delegate collect <task-id>              # result.md を表示
herdr-delegate send <task-id> "追撃メッセージ"
herdr-delegate status [--json]
herdr-delegate kill <task-id> [--rm-worktree]
```

### exit code (`wait`)

| code | 意味 |
|---|---|
| 0 | done |
| 1 | failed |
| 2 | blocked（result.md に status: blocked、または agent 自身が blocked） |
| 3 | stalled / timeout（アイドル継続 or `--timeout` 到達） |
| 4 | pane 消失 |

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
（quota 切れ・ログイン失効等）が出ているので確認する。

## 初回フォルダ確認ダイアログ（trust prompt）

エージェントが初めて触るディレクトリでは、CLI 側の「このフォルダを信頼するか」的な
確認ダイアログが出ることがある。`--dangerously-skip-permissions` /
`--dangerously-bypass-approvals-and-sandbox` はこれを抑止しない。

- **claude**: `wait` に一回限りの自動 Return ワークアラウンドが組み込み済み
  （ダイアログの文言を検知して自動で Enter を送る）。`status` 確認中にこの
  画面が一瞬見えても、これは想定内でありユーザーの操作は不要。
- **codex**: 実測 (2026-07-10) では文言が異なる
  （`Do you trust the contents of this directory? ... 1. Yes, continue`）ため、
  claude 向けの自動検知パターンにはマッチせず自動 Return は働かない。この
  ダイアログで固まると `wait` は agent が働き出さないまま stalled (exit 3)
  を返す。codex を新規ディレクトリに spawn した直後に stalled になったら、
  まず pane を読んで trust ダイアログでないか確認し、そうなら
  `herdr pane send-keys <pane-id> enter` で1回 Enter を送ってから
  `wait` を再実行する（手動介入。ここが直っていない既知のギャップ）。

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
