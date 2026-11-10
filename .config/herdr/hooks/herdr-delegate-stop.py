#!/usr/bin/env python3
"""herdr-delegate worker 用 Claude Code Stop hook (終了契約の強制).

herdr-delegate spawn が claude worker に --settings <task>/worker-settings.json で注入する。
result.md / result.json に有効な status が無いまま worker が止まろうとしたら
{"decision":"block","reason":...} で最大 MAX_NUDGES 回まで書き込みを催促する。
催促を使い切ったら停止を許可し、<task>/protocol_violation を残す (wait が検知する)。

ループ防止は stop_nudges カウンタが担う。stop_hook_active が true でも、
カウンタが残っていれば催促する (さもないと 2 回目の催促が不可能になるため)。

環境変数: HERDR_DELEGATE_TASK_DIR (必須。無ければ委任 worker ではないので何もしない)
          HERDR_DELEGATE_RESULT (result.md のパス。無ければ TASK_DIR/result.md)
"""

import json
import os
import re
import sys
import time

MAX_NUDGES = 2
# herdr-delegate の RESULT_STATUSES と同期すること (テストで一致を検証)
RESULT_STATUSES = ("done", "failed", "blocked")


def has_valid_status(result_md, result_json):
    """result.md の status 行か result.json の status が有効か."""
    try:
        with open(result_md) as f:
            for line in f:
                m = re.match(r"\s*status:\s*(\w+)", line, re.IGNORECASE)
                if m and m.group(1).lower() in RESULT_STATUSES:
                    return True
    except OSError:
        pass
    try:
        with open(result_json) as f:
            data = json.load(f)
        return isinstance(data, dict) and data.get("status") in RESULT_STATUSES
    except (OSError, ValueError):
        return False


def read_nudges(path):
    try:
        with open(path) as f:
            return int(f.read().strip() or 0)
    except (OSError, ValueError):
        return 0


def nudge_reason(result_md):
    return (f"委任契約: {result_md} に status: done|failed|blocked と summary / artifacts を"
            "書いてから終了すること。質問があるなら status: blocked と kind / question を書く。")


def decide_stop(task_dir, result_md, nudges, max_nudges=MAX_NUDGES):
    """停止判定 (純関数): ("allow", None) / ("block", reason) / ("violation", None)."""
    if has_valid_status(result_md, os.path.join(task_dir, "result.json")):
        return "allow", None
    if nudges >= max_nudges:
        return "violation", None
    return "block", nudge_reason(result_md)


def main():
    task_dir = os.environ.get("HERDR_DELEGATE_TASK_DIR")
    if not task_dir:
        return 0
    try:
        json.load(sys.stdin)  # payload (stop_hook_active 等) は判定に使わない。壊れていても続行
    except ValueError:
        pass
    result_md = os.environ.get("HERDR_DELEGATE_RESULT") or os.path.join(task_dir, "result.md")
    counter = os.path.join(task_dir, "stop_nudges")
    nudges = read_nudges(counter)
    verdict, reason = decide_stop(task_dir, result_md, nudges)
    if verdict == "block":
        with open(counter, "w") as f:
            f.write(str(nudges + 1))
        print(json.dumps({"decision": "block", "reason": reason}, ensure_ascii=False))
    elif verdict == "violation":
        with open(os.path.join(task_dir, "protocol_violation"), "w") as f:
            json.dump({"at": time.strftime("%Y-%m-%dT%H:%M:%S%z"), "nudges": nudges}, f)
    return 0


if __name__ == "__main__":
    sys.exit(main())
