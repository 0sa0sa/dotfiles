---
name: herdr-ultra-watch
description: Use when the user wants to see/monitor a Claude Code Workflow (ultracode) run inside herdr — "ワークフローの進捗を herdr で見せて", "ダッシュボード開いて", "ultracode を見える化", "どのエージェントが今何してる", or right after launching a long Workflow when the user is in herdr. Renders phases, running agents' current actions, results, and opens per-agent tail panes.
---

# herdr-ultra-watch — Workflow (ultracode) 実行の herdr ダッシュボード

Workflow ツールの run dir
(`~/.claude/projects/<proj>/<session>/subagents/workflows/<run_id>/` の journal.jsonl /
agent-*.jsonl / meta.json) とワークフロースクリプトの `meta` を読み、herdr ペインに描画する。

## コマンド

```bash
herdr-ultra-watch open [RUN|--latest] [--per-agent N] [--pane PANE]  # 横に split してダッシュボード起動
herdr-ultra-watch dashboard [RUN|--latest] [--once] [--width W] [--all]  # その場で描画 (--once は1フレーム)
herdr-ultra-watch tail <agent-id> [RUN]                                 # 1 agent の transcript を追従
herdr-ultra-watch daemon                                                # 新しい run を監視して自動 open (opt-in)
```

`RUN` は run dir のパスか run id (`wf_xxxxxxxx-xxx`)。省略時は最新。

## いつ何をするか

- **Workflow 起動直後**: PostToolUse(Workflow) hook が herdr 内なら自動で `open` する。
  hook が効いていない/閉じられた場合は `herdr-ultra-watch open <run_id>` を実行する。
- **ユーザーが状況を聞いたとき**: `dashboard --once <run_id> --width 100` の出力を要約して答える
  (自分の確認用。ANSI を除くなら `| sed 's/\x1b\[[0-9;]*m//g'`)。
- **特定 agent を追いたいとき**: `open --per-agent N` か、ダッシュボード上で `t`。

## ダッシュボードの見方 / キー

ヘッダ (名前・目的・全体バー・経過/ETA・tok) → フェーズ (未着手含む) → 今 (実行中 agent の
ツール呼び出しと直近の発言) → 結果 (完了 agent の1行要約、findings 件数) → FEED。

`j/k` 選択, `Enter` 詳細, `t` tail ペイン, `T` 全 tail, `r` findings 一覧, `e` 異常のみ,
`a` 全表示, `f` FEED, `p` strip/list, `?` ヘルプ, `q` 終了。

herdr 側のペイン一覧にも `custom_status` (`▶1 ✓5/6 Build` 形式) が出る。
