#!/bin/sh
# Claude Code SessionStart hook (matcher: startup|resume|compact)。
# herdr-delegate の稼働中 task と未回収の完了 task を additionalContext として再注入する。
# 何も無い/herdr-delegate が無い/失敗時は無出力で exit 0。各コマンドは 8 秒で打ち切る。
bin="$(dirname "$0")/../herdr-delegate"
[ -x "$bin" ] || bin="$(command -v herdr-delegate)" || exit 0
command -v python3 >/dev/null 2>&1 || exit 0
exec python3 - "$bin" <<'PY' 2>/dev/null
import json, subprocess, sys, time
from datetime import datetime

BIN = sys.argv[1]
TIMEOUT = 8
SHOW_STATES = ("done", "failed", "blocked", "protocol_violation", "gone")
MAX_ITEMS = 10
MAX_AGE_SECS = 7 * 24 * 3600  # 古い未回収 task は再注入しない


def run(*args):
    try:
        p = subprocess.run([BIN, *args], capture_output=True, text=True,
                           timeout=TIMEOUT, stdin=subprocess.DEVNULL)
    except (OSError, subprocess.SubprocessError):
        return ""
    return p.stdout.strip() if p.returncode == 0 else ""


def is_recent(item):
    try:
        finished = datetime.strptime(item["finished_at"], "%Y-%m-%dT%H:%M:%S%z").timestamp()
    except (KeyError, TypeError, ValueError):
        return False
    return time.time() - finished <= MAX_AGE_SECS


def pending_lines():
    try:
        items = json.loads(run("reconcile", "--json") or "{}").get("undelivered", [])
    except ValueError:
        return []
    items = [i for i in items if i.get("state") in SHOW_STATES and is_recent(i)][:MAX_ITEMS]
    return [f"- {i.get('task_id')} state={i.get('state')} result={i.get('result')}"
            for i in items]


def main():
    parts = []
    active = run("status", "--active", "--format", "reinject")
    if active:
        parts.append(active)
    lines = pending_lines()
    if lines:
        parts.append("REFERENCE ONLY: herdr-delegate 未回収の完了 task "
                     "(herdr-delegate collect <id> で回収):\n" + "\n".join(lines))
    if parts:
        print(json.dumps({"hookSpecificOutput": {
            "hookEventName": "SessionStart",
            "additionalContext": "\n\n".join(parts)}}, ensure_ascii=False))


main()
PY
