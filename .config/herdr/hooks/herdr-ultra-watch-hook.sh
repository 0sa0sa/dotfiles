#!/bin/sh
# Claude Code PostToolUse hook (matcher: "Workflow").
# herdr 内なら herdr-ultra-watch で dashboard ペインを開く。常に即 exit 0 (open は background)。
[ "${HERDR_ENV:-}" = "1" ] || [ -n "${HERDR_PANE_ID:-}" ] || exit 0
payload="$(cat)"
bin="$(dirname "$0")/../herdr-ultra-watch"
[ -x "$bin" ] || bin="$(command -v herdr-ultra-watch)" || exit 0
( printf '%s' "$payload" | "$bin" hook >/dev/null 2>&1 & ) >/dev/null 2>&1
exit 0
