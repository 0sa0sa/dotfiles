# herdr-delegate Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Claude Code をオーケストレーターとして、herdr ペインで動く claude / codex サブエージェントを spawn / wait / collect / send / kill できる CLI + スキルを実装する。

**Architecture:** 単体 Python スクリプト `herdr-delegate` が herdr socket API (JSON-RPC) と herdr CLI (JSON stdout) を叩き、タスク台帳 `~/.config/herdr/delegate/tasks/<task-id>/` を SSoT にする。プロンプトは temp wrapper スクリプト経由で argv 渡し（`herdr-handoff-codex` で実証済みのパターン）。完了検知は agent_status × result.md の二重シグナル。

**Tech Stack:** Python 3 標準ライブラリのみ（argparse / json / socket / subprocess / tempfile）。テストは unittest。

**Spec:** `docs/2026-07-10-herdr-delegate-design.md`（同リポジトリ）

## Global Constraints

- Python 3 stdlib のみ。外部依存禁止
- dotfiles は PUBLIC リポジトリ。会社名・プロジェクト名・メール・鍵類を一切書かない
- CLI 本体は `~/dotfiles/.config/herdr/herdr-delegate`（拡張子なし、実行可能、shebang `#!/usr/bin/env python3`）
- 実行環境へは symlink で配備: `~/.config/herdr/herdr-delegate` → dotfiles（既存ヘルパーと同パターン）
- socket ヘルパー `call()` は `herdr-fork-agent` の実装をそのまま流用（`HERDR_SOCKET_PATH` フォールバック含む）
- exit code 規約: 0=done / 1=failed(result.md が failed) / 2=blocked / 3=stalled / 4=pane 消失・herdr エラー
- コミットは各タスク末尾で dotfiles リポジトリに対して行う。**push はしない**

## 確認済みの herdr API 事実（実装者向け）

- ペイン内シェルには `HERDR_WORKSPACE_ID` / `HERDR_PANE_ID` / `HERDR_SOCKET_PATH` が設定済み
- `herdr <cmd> ...` は JSON を stdout に出す: `{"id":"cli:...","result":{...}}`
- socket JSON-RPC: `{"id":..,"method":"pane.get","params":{"pane_id":..}}\n` → `{"result":{"pane":{...}}}`。既知メソッド: `pane.get` / `pane.list` / `pane.split` / `pane.swap` / `pane.rename`
- `pane` オブジェクトのキー: `pane_id`, `tab_id`, `workspace_id`, `agent` (claude|codex|null), `agent_status` (idle|working|blocked|unknown), `cwd`, `foreground_cwd`, `label`, `agent_session.value`
- プロンプト投入: `herdr pane run <pane_id> "bash <wrapper.sh>"`、wrapper が prompt ファイルを読んで `exec claude ... "$prompt"` する（`herdr-handoff-codex` 参照）
- `herdr agent send` は literal text（Enter なし）。Enter は `herdr pane send-keys <pane> enter`
- `herdr tab create` / `herdr worktree create --json` の応答スキーマは未確認 → Task 2 / Task 5 冒頭に probe ステップあり

---

### Task 1: 台帳とピュアロジック核（TDD）

**Files:**
- Create: `~/dotfiles/.config/herdr/herdr-delegate`
- Test: `~/dotfiles/.config/herdr/tests/test_herdr_delegate.py`

**Interfaces:**
- Produces: `slugify(text) -> str`、`new_task_id(slug, base_dir) -> str`、`contract_footer(task_dir) -> str`、`decide(agent_status, result_status, nudged, idle_secs) -> str`（CONTINUE|NUDGE|DONE|FAILED_RESULT|BLOCKED|STALLED|PANE_GONE）、`read_result_status(task_dir) -> str|None`、`Manifest` dict の save/load（`save_manifest(task_dir, data)` / `load_manifest(task_dir)`）、`TASKS_DIR` 定数（`~/.config/herdr/delegate/tasks`、env `HERDR_DELEGATE_DIR` で上書き可＝テスト用）

- [ ] **Step 1: テストを書く**

`~/dotfiles/.config/herdr/tests/test_herdr_delegate.py`:

```python
import importlib.machinery
import importlib.util
import os
import tempfile
import unittest

SCRIPT = os.path.join(os.path.dirname(__file__), "..", "herdr-delegate")
loader = importlib.machinery.SourceFileLoader("herdr_delegate", SCRIPT)
spec = importlib.util.spec_from_loader("herdr_delegate", loader)
hd = importlib.util.module_from_spec(spec)
loader.exec_module(hd)


class TestSlug(unittest.TestCase):
    def test_slugify_basic(self):
        self.assertEqual(hd.slugify("Fix the Login Bug!"), "fix-the-login-bug")

    def test_slugify_truncates_to_20(self):
        self.assertLessEqual(len(hd.slugify("a" * 50)), 20)

    def test_slugify_japanese_falls_back(self):
        # 非ASCIIのみなら "task" にフォールバック
        self.assertEqual(hd.slugify("調査タスク"), "task")


class TestTaskId(unittest.TestCase):
    def test_unique_when_collision(self):
        with tempfile.TemporaryDirectory() as d:
            tid1 = hd.new_task_id("foo", d)
            os.makedirs(os.path.join(d, tid1))
            tid2 = hd.new_task_id("foo", d)
            self.assertNotEqual(tid1, tid2)
            self.assertTrue(tid1.startswith("dlg-"))


class TestContract(unittest.TestCase):
    def test_footer_mentions_result_path(self):
        footer = hd.contract_footer("/tmp/x/tasks/dlg-1")
        self.assertIn("/tmp/x/tasks/dlg-1/result.md", footer)
        self.assertIn("status:", footer)


class TestResultStatus(unittest.TestCase):
    def test_reads_status_line(self):
        with tempfile.TemporaryDirectory() as d:
            with open(os.path.join(d, "result.md"), "w") as f:
                f.write("status: done\nsummary: ok\n")
            self.assertEqual(hd.read_result_status(d), "done")

    def test_none_when_missing(self):
        with tempfile.TemporaryDirectory() as d:
            self.assertIsNone(hd.read_result_status(d))


class TestDecide(unittest.TestCase):
    def test_working_continues(self):
        self.assertEqual(hd.decide("working", None, False, 0), "CONTINUE")

    def test_idle_with_done_result(self):
        self.assertEqual(hd.decide("idle", "done", False, 0), "DONE")

    def test_idle_with_failed_result(self):
        self.assertEqual(hd.decide("idle", "failed", False, 0), "FAILED_RESULT")

    def test_blocked_result_wins_over_idle(self):
        self.assertEqual(hd.decide("idle", "blocked", False, 0), "BLOCKED")

    def test_agent_blocked(self):
        self.assertEqual(hd.decide("blocked", None, False, 0), "BLOCKED")

    def test_idle_no_result_nudges_once(self):
        self.assertEqual(hd.decide("idle", None, False, 0), "NUDGE")

    def test_idle_after_nudge_waits_grace(self):
        self.assertEqual(hd.decide("idle", None, True, 30), "CONTINUE")

    def test_idle_after_nudge_stalls_past_grace(self):
        self.assertEqual(hd.decide("idle", None, True, 61), "STALLED")

    def test_pane_gone(self):
        self.assertEqual(hd.decide(None, None, False, 0), "PANE_GONE")

    def test_working_with_done_result_still_waits(self):
        # result.md を書いた後まだ整理作業中: agent が落ち着くまで待つ
        self.assertEqual(hd.decide("working", "done", False, 0), "CONTINUE")


class TestManifest(unittest.TestCase):
    def test_roundtrip(self):
        with tempfile.TemporaryDirectory() as d:
            data = {"task_id": "dlg-x", "agent": "claude", "pane_id": "w1:p1"}
            hd.save_manifest(d, data)
            self.assertEqual(hd.load_manifest(d)["pane_id"], "w1:p1")


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: 失敗を確認**

Run: `cd ~/dotfiles/.config/herdr && python3 -m unittest tests.test_herdr_delegate -v`
Expected: FAIL（`herdr-delegate` が存在しない / 属性なし）

- [ ] **Step 3: 最小実装**

`~/dotfiles/.config/herdr/herdr-delegate`（新規、`chmod +x`）:

```python
#!/usr/bin/env python3
"""herdr-delegate — Claude Code をブローカーにした herdr ペイン委任 CLI.

サブコマンド: spawn / status / wait / collect / send / kill
設計: dotfiles docs/2026-07-10-herdr-delegate-design.md
"""

import json
import os
import random
import re
import socket
import string
import subprocess
import sys
import time

SOCK = os.environ.get("HERDR_SOCKET_PATH") or os.path.expanduser("~/.config/herdr/herdr.sock")
TASKS_DIR = os.environ.get("HERDR_DELEGATE_DIR") or os.path.expanduser(
    "~/.config/herdr/delegate/tasks"
)


# ---------- pure helpers ----------

def slugify(text):
    s = re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")
    return s[:20].rstrip("-") or "task"


def new_task_id(slug, base_dir):
    tid = f"dlg-{time.strftime('%H%M%S')}-{slug}"
    while os.path.exists(os.path.join(base_dir, tid)):
        suffix = "".join(random.choices(string.hexdigits.lower(), k=2))
        tid = f"dlg-{time.strftime('%H%M%S')}-{slug}-{suffix}"
    return tid


def contract_footer(task_dir):
    result = os.path.join(task_dir, "result.md")
    return f"""
--- 委任契約 ---
完了したら {result} に以下の形式で書き込むこと:
  status: done|failed
  summary: 結果 3-10 行
  artifacts: 変更ファイル/ブランチ/PR URL の列挙
result.md を書くまでタスク完了とみなされない。
質問・承認が必要なら result.md に status: blocked と理由を書いて停止。
"""


def read_result_status(task_dir):
    path = os.path.join(task_dir, "result.md")
    try:
        with open(path) as f:
            for line in f:
                m = re.match(r"\s*status:\s*(\w+)", line)
                if m:
                    return m.group(1)
        return "done"  # ファイルはあるが status 行なし → done 扱い
    except OSError:
        return None


STALL_GRACE_SECS = 60


def decide(agent_status, result_status, nudged, idle_secs):
    """wait ループの1判定. 戻り値: CONTINUE|NUDGE|DONE|FAILED_RESULT|BLOCKED|STALLED|PANE_GONE"""
    if agent_status is None:
        return "PANE_GONE"
    if result_status == "blocked" or agent_status == "blocked":
        return "BLOCKED"
    if agent_status == "working":
        return "CONTINUE"
    # idle / unknown
    if result_status == "done":
        return "DONE"
    if result_status == "failed":
        return "FAILED_RESULT"
    if not nudged:
        return "NUDGE"
    if idle_secs > STALL_GRACE_SECS:
        return "STALLED"
    return "CONTINUE"


def save_manifest(task_dir, data):
    with open(os.path.join(task_dir, "manifest.json"), "w") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)


def load_manifest(task_dir):
    with open(os.path.join(task_dir, "manifest.json")) as f:
        return json.load(f)


def main():
    print("not implemented", file=sys.stderr)
    sys.exit(1)


if __name__ == "__main__":
    main()
```

Run: `chmod +x ~/dotfiles/.config/herdr/herdr-delegate`

- [ ] **Step 4: テストが通ることを確認**

Run: `cd ~/dotfiles/.config/herdr && python3 -m unittest tests.test_herdr_delegate -v`
Expected: 全 PASS（15 tests）

- [ ] **Step 5: Commit**

```bash
cd ~/dotfiles && git add .config/herdr/herdr-delegate .config/herdr/tests/test_herdr_delegate.py
git commit -m "feat(herdr-delegate): task registry and pure decision logic"
```

---

### Task 2: spawn（新規タブモード）

**Files:**
- Modify: `~/dotfiles/.config/herdr/herdr-delegate`

**Interfaces:**
- Consumes: Task 1 の全ヘルパー
- Produces: `call(method, params)`（socket RPC）、`hcli(*args) -> dict`（herdr CLI 実行 + JSON parse で `result` を返す）、`cmd_spawn(args)`、argparse エントリポイント `main()`。manifest スキーマ: `{task_id, agent, model, pane_id, tab_id, workspace_id, cwd, worktree_path, worktree_branch, label, created_at, safe}`

- [ ] **Step 1: `herdr tab create` の応答スキーマを probe**

```bash
WS=$HERDR_WORKSPACE_ID
herdr tab create --workspace $WS --label dlg-probe --no-focus
herdr pane list --workspace $WS   # 新タブの pane_id を確認
```

Expected: JSON に新 tab の id が入る。**pane list で新タブ配下に shell ペインが 1 つあること、そのキー名を記録**。確認後 `herdr tab close <tab_id>` で掃除。応答に pane が直接含まれるなら pane list は不要 — 実装をその形に合わせる。

- [ ] **Step 2: spawn を実装**

`herdr-delegate` に追記（`main()` を置き換え）:

```python
# ---------- herdr I/O ----------

def call(method, params):
    s = socket.socket(socket.AF_UNIX)
    s.connect(SOCK)
    s.sendall((json.dumps({"id": f"delegate:{method}", "method": method, "params": params}) + "\n").encode())
    buf = b""
    while not buf.endswith(b"\n"):
        chunk = s.recv(65536)
        if not chunk:
            break
        buf += chunk
    s.close()
    resp = json.loads(buf)
    if "error" in resp:
        raise RuntimeError(f"{method}: {resp['error']}")
    return resp["result"]


def hcli(*args):
    proc = subprocess.run(["herdr", *args], capture_output=True, text=True)
    if proc.returncode != 0:
        raise RuntimeError(f"herdr {' '.join(args)}: {proc.stderr.strip()}")
    out = proc.stdout.strip()
    if not out:
        return {}
    return json.loads(out).get("result", {})


def get_pane(pane_id):
    try:
        return call("pane.get", {"pane_id": pane_id})["pane"]
    except (RuntimeError, OSError):
        return None


def current_workspace_id():
    ws = os.environ.get("HERDR_WORKSPACE_ID")
    if ws:
        return ws
    return hcli("pane", "current")["pane"]["workspace_id"]


# ---------- spawn ----------

AGENT_CMDS = {
    # {model} は "--model X" / "-m X" 済み文字列、{cwd} と {prompt} は wrapper 内で展開
    "claude": 'exec claude {model} {danger} "$prompt"',
    "codex": 'exec codex {model} {danger} --cd "$cwd" "$prompt"',
}
DANGER_FLAGS = {
    "claude": "--dangerously-skip-permissions",
    "codex": "--dangerously-bypass-approvals-and-sandbox",
}
MODEL_FLAGS = {"claude": "--model {m}", "codex": "-m {m}"}


def build_wrapper(task_dir, agent, model, safe, cwd):
    import shlex
    import tempfile

    prompt_path = os.path.join(task_dir, "prompt.md")
    model_part = MODEL_FLAGS[agent].format(m=model) if model else ""
    danger_part = "" if safe else DANGER_FLAGS[agent]
    agent_cmd = AGENT_CMDS[agent].format(model=model_part, danger=danger_part, cwd='"$cwd"')
    wrapper = tempfile.NamedTemporaryFile(
        "w", delete=False, prefix="herdr-delegate-", suffix=".sh"
    )
    wrapper.write("#!/usr/bin/env bash\nset -euo pipefail\n")
    wrapper.write(f"cwd={shlex.quote(cwd)}\n")
    wrapper.write(f'prompt="$(cat {shlex.quote(prompt_path)})"\n')
    wrapper.write('rm -f -- "$0"\n')
    wrapper.write(f'cd "$cwd"\n{agent_cmd}\n')
    wrapper.close()
    os.chmod(wrapper.name, 0o755)
    return wrapper.name


def cmd_spawn(args):
    prompt_body = args.prompt or open(args.prompt_file).read()
    cwd = os.path.abspath(args.cwd or os.getcwd())
    os.makedirs(TASKS_DIR, exist_ok=True)
    slug = slugify(args.label or prompt_body[:60])
    task_id = new_task_id(slug, TASKS_DIR)
    task_dir = os.path.join(TASKS_DIR, task_id)
    os.makedirs(task_dir)

    with open(os.path.join(task_dir, "prompt.md"), "w") as f:
        f.write(prompt_body.rstrip() + "\n" + contract_footer(task_dir))

    worktree_path = worktree_branch = None
    if args.worktree is not None:
        raise SystemExit("--worktree は Task 5 で実装")  # Task 5 で置換

    ws = current_workspace_id()
    tab = hcli("tab", "create", "--workspace", ws, "--cwd", cwd,
               "--label", task_id, "--no-focus")
    # probe 結果に応じて: tab 応答に pane が含まれるならそれを使う。
    # 含まれない場合は pane list から tab_id 一致の pane を拾う (以下は後者の想定)
    tab_id = tab["tab"]["tab_id"] if "tab" in tab else tab["tab_id"]
    panes = call("pane.list", {"workspace_id": ws})["panes"]
    pane = next(p for p in panes if p["tab_id"] == tab_id)
    pane_id = pane["pane_id"]

    wrapper = build_wrapper(task_dir, args.agent, args.model, args.safe, cwd)
    subprocess.run(["herdr", "pane", "run", pane_id, f"bash {wrapper}"],
                   capture_output=True, check=True)
    call("pane.rename", {"pane_id": pane_id, "label": task_id})

    manifest = {
        "task_id": task_id, "agent": args.agent, "model": args.model,
        "pane_id": pane_id, "tab_id": tab_id, "workspace_id": ws,
        "cwd": cwd, "worktree_path": worktree_path,
        "worktree_branch": worktree_branch, "label": args.label,
        "created_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"), "safe": args.safe,
    }
    save_manifest(task_dir, manifest)
    print(json.dumps({"task_id": task_id, "pane_id": pane_id,
                      "task_dir": task_dir}, ensure_ascii=False))


def main():
    import argparse

    p = argparse.ArgumentParser(prog="herdr-delegate")
    sub = p.add_subparsers(dest="cmd", required=True)

    sp = sub.add_parser("spawn")
    sp.add_argument("--agent", choices=["claude", "codex"], required=True)
    sp.add_argument("--model")
    sp.add_argument("--worktree", nargs="?", const="", default=None,
                    metavar="BRANCH")
    sp.add_argument("--cwd")
    sp.add_argument("--label")
    sp.add_argument("--safe", action="store_true")
    g = sp.add_mutually_exclusive_group(required=True)
    g.add_argument("--prompt")
    g.add_argument("--prompt-file")
    sp.set_defaults(func=cmd_spawn)

    args = p.parse_args()
    args.func(args)
```

Step 1 の probe 結果と食い違う箇所（`tab` 応答の形、pane の拾い方）は実測に合わせて修正すること。

- [ ] **Step 3: 単体テストの回帰確認**

Run: `cd ~/dotfiles/.config/herdr && python3 -m unittest tests.test_herdr_delegate -v`
Expected: 全 PASS

- [ ] **Step 4: スモーク — claude を実 spawn**

```bash
mkdir -p /tmp/hd-smoke && cd /tmp/hd-smoke
~/dotfiles/.config/herdr/herdr-delegate spawn --agent claude --model haiku \
  --label smoke1 --prompt "このディレクトリに hello.txt を作り中身を hello にせよ。"
```

Expected: JSON で task_id / pane_id が出力され、herdr に新タブ `dlg-*-smoke1` が現れ claude が起動する。1-2 分後に `/tmp/hd-smoke/hello.txt` と `<task_dir>/result.md` (status: done) ができる。

- [ ] **Step 5: Commit**

```bash
cd ~/dotfiles && git add .config/herdr/herdr-delegate
git commit -m "feat(herdr-delegate): spawn subcommand (new-tab mode, wrapper prompt injection)"
```

---

### Task 3: wait + collect

**Files:**
- Modify: `~/dotfiles/.config/herdr/herdr-delegate`

**Interfaces:**
- Consumes: `decide()` / `read_result_status()` / `get_pane()` / `load_manifest()`
- Produces: `cmd_wait(args)`（exit 0/1/2/3/4）、`cmd_collect(args)`、`task_dir_of(task_id) -> str`、`nudge(pane_id, task_dir)`、`send_line(pane_id, text)`（send + Enter + エコーバック検証。Task 4 の send も再利用）

- [ ] **Step 1: wait / collect を実装**

`herdr-delegate` に追記:

```python
POLL_SECS = 5
NUDGE_TEXT = "委任契約を確認: 完了しているなら {result} に status/summary/artifacts を書くこと。"


def task_dir_of(task_id):
    d = os.path.join(TASKS_DIR, task_id)
    if not os.path.isdir(d):
        raise SystemExit(f"unknown task: {task_id}")
    return d


def send_line(pane_id, text, retries=1):
    for _ in range(retries + 1):
        subprocess.run(["herdr", "agent", "send", pane_id, text],
                       capture_output=True, check=True)
        time.sleep(0.3)
        subprocess.run(["herdr", "pane", "send-keys", pane_id, "enter"],
                       capture_output=True, check=True)
        time.sleep(0.5)
        proc = subprocess.run(
            ["herdr", "agent", "read", pane_id, "--lines", "5"],
            capture_output=True, text=True)
        if text[:20] in proc.stdout:
            return True
    return False


def pane_tail(pane_id, lines=40):
    proc = subprocess.run(
        ["herdr", "agent", "read", pane_id, "--source", "recent-unwrapped",
         "--lines", str(lines)],
        capture_output=True, text=True)
    return proc.stdout


def cmd_wait(args):
    task_dir = task_dir_of(args.task_id)
    manifest = load_manifest(task_dir)
    pane_id = manifest["pane_id"]
    deadline = time.time() + args.timeout / 1000 if args.timeout else None
    nudged = False
    idle_since = None

    while True:
        pane = get_pane(pane_id)
        status = pane.get("agent_status") if pane else None
        if status == "idle":
            idle_since = idle_since or time.time()
        else:
            idle_since = None
        idle_secs = time.time() - idle_since if idle_since else 0
        action = decide(status, read_result_status(task_dir), nudged, idle_secs)

        if action == "DONE":
            print(json.dumps({"task_id": args.task_id, "status": "done",
                              "result": os.path.join(task_dir, "result.md")}))
            sys.exit(0)
        if action == "FAILED_RESULT":
            print(json.dumps({"task_id": args.task_id, "status": "failed",
                              "result": os.path.join(task_dir, "result.md")}))
            sys.exit(1)
        if action == "BLOCKED":
            subprocess.run(["herdr", "notification", "show",
                            f"delegate {args.task_id} blocked",
                            "--body", "サブエージェントが入力待ち"],
                           capture_output=True)
            print(json.dumps({"task_id": args.task_id, "status": "blocked"}))
            print(pane_tail(pane_id), file=sys.stderr)
            sys.exit(2)
        if action == "STALLED":
            print(json.dumps({"task_id": args.task_id, "status": "stalled"}))
            print(pane_tail(pane_id), file=sys.stderr)
            sys.exit(3)
        if action == "PANE_GONE":
            print(json.dumps({"task_id": args.task_id, "status": "failed",
                              "reason": "pane gone"}))
            sys.exit(4)
        if action == "NUDGE":
            send_line(pane_id, NUDGE_TEXT.format(
                result=os.path.join(task_dir, "result.md")))
            nudged = True
            idle_since = None

        if deadline and time.time() > deadline:
            print(json.dumps({"task_id": args.task_id, "status": "timeout",
                              "agent_status": status}))
            print(pane_tail(pane_id), file=sys.stderr)
            sys.exit(3)
        time.sleep(POLL_SECS)


def cmd_collect(args):
    task_dir = task_dir_of(args.task_id)
    result = os.path.join(task_dir, "result.md")
    if os.path.exists(result):
        with open(result) as f:
            print(f.read())
        return
    manifest = load_manifest(task_dir)
    print("# result.md なし — pane 末尾で代替:", file=sys.stderr)
    print(pane_tail(manifest["pane_id"], 60))
    sys.exit(3)
```

`main()` の argparse に追加:

```python
    wp = sub.add_parser("wait")
    wp.add_argument("task_id")
    wp.add_argument("--timeout", type=int, help="ms")
    wp.set_defaults(func=cmd_wait)

    cp = sub.add_parser("collect")
    cp.add_argument("task_id")
    cp.set_defaults(func=cmd_collect)
```

- [ ] **Step 2: 単体テスト回帰**

Run: `cd ~/dotfiles/.config/herdr && python3 -m unittest tests.test_herdr_delegate -v`
Expected: 全 PASS

- [ ] **Step 3: スモーク — Task 2 の spawn からの一気通貫**

```bash
cd /tmp/hd-smoke
TID=$(~/dotfiles/.config/herdr/herdr-delegate spawn --agent claude --model haiku \
  --label smoke2 --prompt "1+1 を計算して結果だけ report せよ。ファイル作成不要。" | python3 -c 'import sys,json;print(json.load(sys.stdin)["task_id"])')
~/dotfiles/.config/herdr/herdr-delegate wait $TID --timeout 300000; echo "exit=$?"
~/dotfiles/.config/herdr/herdr-delegate collect $TID
```

Expected: wait が exit=0、collect が result.md（status: done, summary に 2）を表示。

- [ ] **Step 4: Commit**

```bash
cd ~/dotfiles && git add .config/herdr/herdr-delegate
git commit -m "feat(herdr-delegate): wait/collect with dual-signal completion detection"
```

---

### Task 4: send + status + kill

**Files:**
- Modify: `~/dotfiles/.config/herdr/herdr-delegate`

**Interfaces:**
- Consumes: `send_line()` / `task_dir_of()` / `load_manifest()` / `get_pane()` / `read_result_status()`
- Produces: `cmd_send(args)`、`cmd_status(args)`（`--json` あり）、`cmd_kill(args)`（`--rm-worktree` は Task 5 で有効化）

- [ ] **Step 1: 実装**

```python
def cmd_send(args):
    manifest = load_manifest(task_dir_of(args.task_id))
    if not send_line(manifest["pane_id"], args.text):
        raise SystemExit("send: エコーバック検証に失敗 (2回試行)")


def cmd_status(args):
    rows = []
    if not os.path.isdir(TASKS_DIR):
        print("[]" if args.json else "(no tasks)")
        return
    ids = [args.task_id] if args.task_id else sorted(
        os.listdir(TASKS_DIR),
        key=lambda t: os.path.getmtime(os.path.join(TASKS_DIR, t)),
        reverse=True)
    for tid in ids:
        d = os.path.join(TASKS_DIR, tid)
        if not os.path.isfile(os.path.join(d, "manifest.json")):
            continue
        m = load_manifest(d)
        pane = get_pane(m["pane_id"])
        rows.append({
            "task_id": tid,
            "agent": m["agent"],
            "model": m.get("model"),
            "pane": m["pane_id"] if pane else "GONE",
            "agent_status": pane.get("agent_status") if pane else None,
            "result": read_result_status(d),
            "cwd": m["cwd"],
        })
    if args.json:
        print(json.dumps(rows, ensure_ascii=False, indent=2))
    else:
        for r in rows:
            print(f"{r['task_id']:34} {r['agent']:6} {str(r['agent_status']):8} "
                  f"result={r['result']} pane={r['pane']}")


def cmd_kill(args):
    task_dir = task_dir_of(args.task_id)
    m = load_manifest(task_dir)
    pane = get_pane(m["pane_id"])
    if pane:
        hcli("pane", "close", m["pane_id"])
    if args.rm_worktree and m.get("worktree_path"):
        pass  # Task 5 で実装
    m["killed_at"] = time.strftime("%Y-%m-%dT%H:%M:%S%z")
    save_manifest(task_dir, m)
```

argparse 追加:

```python
    sn = sub.add_parser("send")
    sn.add_argument("task_id")
    sn.add_argument("text")
    sn.set_defaults(func=cmd_send)

    st = sub.add_parser("status")
    st.add_argument("task_id", nargs="?")
    st.add_argument("--json", action="store_true")
    st.set_defaults(func=cmd_status)

    kl = sub.add_parser("kill")
    kl.add_argument("task_id")
    kl.add_argument("--rm-worktree", action="store_true")
    kl.set_defaults(func=cmd_kill)
```

- [ ] **Step 2: 単体テスト回帰**

Run: `cd ~/dotfiles/.config/herdr && python3 -m unittest tests.test_herdr_delegate -v`
Expected: 全 PASS

- [ ] **Step 3: スモーク**

```bash
~/dotfiles/.config/herdr/herdr-delegate status
# smoke2 タスクに追撃 → 応答確認 → kill
~/dotfiles/.config/herdr/herdr-delegate send <smoke2-task-id> "ありがとう。他に作業は不要。"
~/dotfiles/.config/herdr/herdr-delegate kill <smoke1-task-id>
~/dotfiles/.config/herdr/herdr-delegate status
```

Expected: status 一覧表示 / send がペインに入力される（herdr で目視）/ kill 後は pane=GONE。

- [ ] **Step 4: Commit**

```bash
cd ~/dotfiles && git add .config/herdr/herdr-delegate
git commit -m "feat(herdr-delegate): send/status/kill subcommands"
```

---

### Task 5: worktree モード

**Files:**
- Modify: `~/dotfiles/.config/herdr/herdr-delegate`

**Interfaces:**
- Consumes: `cmd_spawn` の `--worktree` プレースホルダ、`cmd_kill` の `--rm-worktree` プレースホルダ
- Produces: spawn 時 `--worktree [BRANCH]`（省略時ブランチ `delegate/<task-id>`）で herdr worktree workspace 内に spawn。manifest の `worktree_path` / `worktree_branch` / `workspace_id` が埋まる。kill `--rm-worktree` で workspace + worktree 削除

- [ ] **Step 1: `herdr worktree create --json` の応答スキーマを probe**

```bash
cd <任意の git リポジトリ>
herdr worktree create --cwd . --branch delegate/probe --json --no-focus
herdr worktree list --cwd . --json
```

Expected: 応答に worktree の path / workspace_id / 生成 pane の情報。キー名を記録し、掃除:
`herdr worktree remove --workspace <ID> --force --json`、残ブランチは `git branch -D delegate/probe`。

- [ ] **Step 2: spawn の worktree 分岐を実装**

`cmd_spawn` の `raise SystemExit("--worktree は Task 5 で実装")` を置換:

```python
    ws = tab_id = None
    if args.worktree is not None:
        worktree_branch = args.worktree or f"delegate/{task_id}"
        wt = hcli("worktree", "create", "--cwd", cwd,
                  "--branch", worktree_branch, "--json", "--no-focus")
        # probe 結果に合わせてキー名を修正すること
        worktree_path = wt["path"]
        ws = wt["workspace_id"]
        cwd = worktree_path
        panes = call("pane.list", {"workspace_id": ws})["panes"]
        pane = panes[0]
        pane_id, tab_id = pane["pane_id"], pane["tab_id"]
    else:
        ws = current_workspace_id()
        tab = hcli("tab", "create", "--workspace", ws, "--cwd", cwd,
                   "--label", task_id, "--no-focus")
        tab_id = tab["tab"]["tab_id"] if "tab" in tab else tab["tab_id"]
        panes = call("pane.list", {"workspace_id": ws})["panes"]
        pane = next(p for p in panes if p["tab_id"] == tab_id)
        pane_id = pane["pane_id"]
```

（既存の tab 分岐をこの if/else に統合する。以降の wrapper 起動・rename・manifest 保存は共通。）

`cmd_kill` の pass を置換:

```python
    if args.rm_worktree and m.get("worktree_path"):
        hcli("worktree", "remove", "--workspace", m["workspace_id"], "--force")
```

- [ ] **Step 3: 単体テスト回帰**

Run: `cd ~/dotfiles/.config/herdr && python3 -m unittest tests.test_herdr_delegate -v`
Expected: 全 PASS

- [ ] **Step 4: スモーク — worktree spawn 一気通貫**

```bash
cd <任意の git リポジトリ>
TID=$(~/dotfiles/.config/herdr/herdr-delegate spawn --agent claude --model haiku \
  --worktree --label wt-smoke \
  --prompt "SMOKE.md というファイルを作り 'worktree smoke' と書いてコミットせよ。" \
  | python3 -c 'import sys,json;print(json.load(sys.stdin)["task_id"])')
~/dotfiles/.config/herdr/herdr-delegate wait $TID --timeout 300000
~/dotfiles/.config/herdr/herdr-delegate collect $TID
git -C <リポジトリ> worktree list   # delegate/<task-id> worktree があること
~/dotfiles/.config/herdr/herdr-delegate kill $TID --rm-worktree
git -C <リポジトリ> worktree list   # 消えていること
git -C <リポジトリ> branch -D delegate/$TID  # ブランチ掃除
```

Expected: worktree 内でコミットが作られ、kill --rm-worktree で workspace / worktree が消える。

- [ ] **Step 5: Commit**

```bash
cd ~/dotfiles && git add .config/herdr/herdr-delegate
git commit -m "feat(herdr-delegate): worktree isolation mode"
```

---

### Task 6: codex スモーク + スキル + symlink 配備

**Files:**
- Create: `~/dotfiles/.claude/skills/herdr-delegate/SKILL.md`
- Symlink: `~/.config/herdr/herdr-delegate` → `~/dotfiles/.config/herdr/herdr-delegate`
- Symlink: `~/.claude/skills/herdr-delegate` → `~/dotfiles/.claude/skills/herdr-delegate`

**Interfaces:**
- Consumes: CLI 全サブコマンド
- Produces: ユーザーレベルスキル（どのプロジェクトのセッションからも呼べる）

- [ ] **Step 1: codex スモーク**

```bash
mkdir -p /tmp/hd-smoke-codex && cd /tmp/hd-smoke-codex
TID=$(~/dotfiles/.config/herdr/herdr-delegate spawn --agent codex \
  --label cdx-smoke --prompt "ping.txt を作り中身を pong にせよ。" \
  | python3 -c 'import sys,json;print(json.load(sys.stdin)["task_id"])')
~/dotfiles/.config/herdr/herdr-delegate wait $TID --timeout 300000; echo exit=$?
~/dotfiles/.config/herdr/herdr-delegate collect $TID
```

Expected: codex が契約どおり result.md を書く。書かない場合は NUDGE 経路で回収されるか確認し、必要なら契約文言を調整（codex が status 行を書きやすい表現に）。

- [ ] **Step 2: symlink 配備**

```bash
ln -sf ~/dotfiles/.config/herdr/herdr-delegate ~/.config/herdr/herdr-delegate
mkdir -p ~/dotfiles/.claude/skills
```

- [ ] **Step 3: SKILL.md を書く**

`~/dotfiles/.claude/skills/herdr-delegate/SKILL.md`:

````markdown
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
herdr-delegate wait <task-id> [--timeout MS]  # exit: 0=done 1=failed 2=blocked 3=stalled/timeout 4=pane消失
herdr-delegate collect <task-id>              # result.md を表示
herdr-delegate send <task-id> "追撃メッセージ"
herdr-delegate status [--json]
herdr-delegate kill <task-id> [--rm-worktree]
```

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
````

- [ ] **Step 4: symlink 作成 + スキル認識確認**

```bash
ln -sfn ~/dotfiles/.claude/skills/herdr-delegate ~/.claude/skills/herdr-delegate
ls -la ~/.claude/skills/herdr-delegate/SKILL.md
```

Expected: symlink 経由で SKILL.md が読める（スキルは次回セッションから認識）。

- [ ] **Step 5: 機微情報スキャン + Commit**

```bash
cd ~/dotfiles && grep -rinE '会社名やメール・鍵のパターン' .config/herdr/herdr-delegate .claude/skills/herdr-delegate/ docs/2026-07-10-herdr-delegate-*.md
# 実際は: grep -rinE '<company>|<product>|@[a-z0-9.-]+\.(jp|com)|AKIA|ghp_|xox[bap]|api[_-]?key'
git add .claude/skills/herdr-delegate .config/herdr/herdr-delegate docs/
git commit -m "feat(herdr-delegate): user-level skill and symlink deployment"
```

Expected: grep ヒット 0（`task-id` への偽陽性は除外して判断）。**push はしない**。

---

## Self-Review 結果

- Spec coverage: spawn/status/wait/collect/send/kill、契約、二重シグナル判定表、tab/worktree 隔離、yolo 既定 + `--safe`、blocked 通知、stalled 催促、台帳突合、cleanup 規律、テスト計画 5 項目 → Task 1-6 で全カバー。MCP 化は明示的に後続スコープ（設計どおり）
- Placeholder: Task 2 Step1 / Task 5 Step1 の probe は「応答スキーマが実測でしか分からない」ための意図的ステップで、実装コードは fallback 込みで提示済み
- Type consistency: `decide()` の戻り値・exit code・manifest キーは全タスクで同一
