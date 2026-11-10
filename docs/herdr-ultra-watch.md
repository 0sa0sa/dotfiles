# herdr-ultra-watch

ultracode (Claude Code Workflow) の実行を herdr ペインで可視化する CLI。
本体: `.config/herdr/herdr-ultra-watch`、hook: `.config/herdr/hooks/herdr-ultra-watch-hook.sh`。

## サブコマンド

- `dashboard [run] [--once]` phase/agent の進捗をライブ表示。完了、または running 0 のまま 10 分続くと終了
- `tail <agent> [run]` 1 agent の transcript 追従
- `open [run] [--per-agent N] [--pane ID]` herdr ペインで dashboard を開く
- `daemon [--interval S] [--print-only] [--include-existing]` 新 run を検出して自動で開く (opt-in)。pane 未解決 / open 失敗は 60 秒再試行
- `hook` PostToolUse(Workflow) の JSON を stdin から受けて open

## 自動で開く: PostToolUse hook の登録

`~/.claude/settings.json` に追記 (herdr 外では hook は何もしない)。

```json
{
  "hooks": {
    "PostToolUse": [
      {
        "matcher": "Workflow",
        "hooks": [
          { "type": "command", "command": "~/dotfiles/.config/herdr/hooks/herdr-ultra-watch-hook.sh" }
        ]
      }
    ]
  }
}
```

hook を使わず daemon で代替する場合: `herdr-ultra-watch daemon`。hook と daemon を併用しても排他マーカー
(`~/.config/herdr/ultra-watch/opened/<run_id>`) により二重には開かない。open 失敗時はマーカーを戻すため再試行できる。
