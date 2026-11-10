---
name: herdr-recall
description: Use when the user asks about past Claude Code / Codex conversations — "前に〜について話した/やったセッション", "あのとき何て結論した", "過去ログから探して", "以前の委任タスクの経緯", recalling decisions, commands, or errors from earlier sessions across projects. Full-text search over ~/.claude/projects and ~/.codex/sessions transcripts.
---

# herdr-recall — Claude / Codex の過去セッション横断検索

`herdr-recall` は `~/.claude/projects/**/*.jsonl` と `~/.codex/sessions/**/rollout-*.jsonl` を
SQLite FTS5 に差分索引し (`~/.config/herdr/recall.db`, 0600)、全文検索する。
検索前に自動で差分索引する (`--no-refresh` で省略)。

## コマンド

```bash
herdr-recall search "QUERY" [--project P] [--since 7d|12h|2w|2026-07-01] [--limit N] [--json]
herdr-recall show <SESSION_ID 先頭数文字可> [--around TS]   # 会話を時系列表示
herdr-recall recent [--limit N]                               # 直近セッション一覧
herdr-recall index [--full]                                   # 手動で索引 (--full は作り直し)
```

- 3文字未満や CJK の短い語は LIKE 検索にフォールバックする。複数語は AND。
- 結果行: `日時 source role sid先頭8 cwd [task=<herdr-delegate task_id>]`。
  delegate ワーカーのセッションは委任契約フッターの result パスで task_id が確定表示される。

## 使い方の型

1. `search` を `--json` で引き、候補の sid を絞る (`--project` / `--since` で狭める)。
2. `show <sid> --around <ts>` で前後の会話を読んで結論・コマンド・エラーを確認する。
3. 回答には日時・プロジェクト・sid を添える。過去の結論は現状と違う可能性があるので、
   ファイルやコードに関わる主張は現在の状態で再確認してから伝える。

## 注意

- 索引には会話本文 (ツール結果は除く・1メッセージ 4000 字上限) が入る。DB を外部へ渡さない。
- 検索結果をユーザー以外 (外部サービス・公開リポジトリ) へ転記しない。
