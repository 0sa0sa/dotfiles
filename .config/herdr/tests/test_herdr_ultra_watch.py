import importlib.machinery
import importlib.util
import json
import os
import shutil
import tempfile
import unittest

SCRIPT = os.path.join(os.path.dirname(__file__), "..", "herdr-ultra-watch")
loader = importlib.machinery.SourceFileLoader("herdr_ultra_watch", SCRIPT)
spec = importlib.util.spec_from_loader("herdr_ultra_watch", loader)
uw = importlib.util.module_from_spec(spec)
loader.exec_module(uw)

SID = "00000000-0000-4000-8000-000000000000"
JOURNAL = [
    '{"type":"launched"}',
    '{"type":"started","key":"k1","agentId":"aaa111","label":"analyze:one","phase":"Analyze"}',
    '{"type":"started","key":"k2","agentId":"bbb222","label":"analyze:two","phase":"Analyze"}',
    '{"type":"started","key":"k3","agentId":"ccc333","label":"verify:x","phase":"Verify"}',
    '{"type":"result","key":"k1","agentId":"aaa111","result":"ok","toolsUsed":["Bash"]}',
    '{"type":"result","key":"k3","agentId":"ccc333","result":{"error":"boom"}}',
    "not json",
]


def assistant(ts, blocks, mid="m1", out=10):
    return json.dumps({"type": "assistant", "timestamp": ts, "message": {
        "id": mid, "content": blocks, "usage": {"output_tokens": out}}})


class TestFormat(unittest.TestCase):
    def test_elapsed(self):
        self.assertEqual(uw.fmt_elapsed(None), "-")
        self.assertEqual(uw.fmt_elapsed(65), "1m05s")
        self.assertEqual(uw.fmt_elapsed(3725), "1h02m")

    def test_tokens(self):
        self.assertEqual(uw.fmt_tokens(0), "-")
        self.assertEqual(uw.fmt_tokens(999), "999")
        self.assertEqual(uw.fmt_tokens(12345), "12.3k")

    def test_clip_wide_chars(self):
        self.assertEqual(uw.clip("abcdef", 4), "abc…")
        out = uw.clip("日本語テキスト", 6)
        self.assertLessEqual(uw.disp_width(out), 6)
        self.assertTrue(out.endswith("…"))
        self.assertEqual(uw.clip("ab", 0), "")

    def test_pad_width(self):
        self.assertEqual(uw.disp_width(uw.pad("日本", 8)), 8)

    def test_parse_ts(self):
        self.assertEqual(uw.parse_ts("1970-01-01T00:00:10.000Z"), 10.0)
        self.assertIsNone(uw.parse_ts("garbage"))
        self.assertIsNone(uw.parse_ts(None))


class TestJournal(unittest.TestCase):
    def test_states(self):
        a = uw.parse_journal(JOURNAL)
        self.assertEqual(list(a), ["aaa111", "bbb222", "ccc333"])
        self.assertEqual(a["aaa111"]["status"], "done")
        self.assertEqual(a["bbb222"]["status"], "running")
        self.assertEqual(a["ccc333"]["status"], "error")

    def test_error_result_shapes(self):
        self.assertTrue(uw.is_error_result({"error": "x"}))
        self.assertTrue(uw.is_error_result("Error: boom"))
        self.assertTrue(uw.is_error_result("TypeError: x is undefined"))
        self.assertFalse(uw.is_error_result("fine"))
        self.assertFalse(uw.is_error_result("Error handling added to X"))
        self.assertFalse(uw.is_error_result("errors were fixed in 3 files"))
        self.assertFalse(uw.is_error_result({"findings": []}))

    def test_result_without_started(self):
        a = uw.parse_journal(['{"type":"result","agentId":"zzz","result":"ok"}'])
        self.assertEqual(a["zzz"]["status"], "done")


class TestStats(unittest.TestCase):
    def lines(self):
        return [
            '{"type":"user","timestamp":"2026-11-10T05:00:00.000Z","message":{"role":"user","content":"hi"}}',
            assistant("2026-11-10T05:00:10.000Z", [{"type": "tool_use", "name": "Bash", "input": {"command": "ls"}}], "m1", 5),
            assistant("2026-11-10T05:00:10.500Z", [{"type": "text", "text": "x"}], "m1", 5),
            assistant("2026-11-10T05:01:00.000Z", [{"type": "tool_use", "name": "Read", "input": {}}], "m2", 7),
            "broken",
        ]

    def test_update(self):
        s = uw.update_stats(uw.new_stats(), self.lines())
        self.assertEqual(s["last_tool"], "Read")
        self.assertEqual(s["tool_calls"], 2)
        self.assertEqual(uw.out_tokens(s), 12)  # m1 は重複排除
        self.assertEqual(s["last_ts"] - s["first_ts"], 60.0)

    def test_incremental_equals_batch(self):
        s = uw.new_stats()
        uw.update_stats(s, self.lines()[:2])
        uw.update_stats(s, self.lines()[2:])
        self.assertEqual(uw.out_tokens(s), 12)
        self.assertEqual(s["tool_calls"], 2)

    def test_stalled_and_elapsed(self):
        s = uw.update_stats(uw.new_stats(), self.lines())
        running = {"status": "running"}
        self.assertEqual(uw.agent_state(running, s, s["last_ts"] + 10), "running")
        self.assertEqual(uw.agent_state(running, s, s["last_ts"] + uw.STALL_SEC + 1), "stalled")
        self.assertEqual(uw.agent_state({"status": "done"}, s, 1e12), "done")
        self.assertEqual(uw.agent_elapsed("done", s, 1e12), 60.0)
        self.assertEqual(uw.agent_elapsed("running", s, s["first_ts"] + 5), 5.0)
        self.assertIsNone(uw.agent_elapsed("running", None, 0))

    def test_no_transcript_is_running(self):
        self.assertEqual(uw.agent_state({"status": "running"}, None, 1e12), "running")


class TestAggregate(unittest.TestCase):
    def test_counts_and_phases(self):
        agents = uw.parse_journal(JOURNAL)
        phases, counts = uw.aggregate(agents, {}, 0)
        self.assertEqual([p for p, _ in phases], ["Analyze", "Verify"])
        self.assertEqual(counts, {"running": 1, "done": 1, "error": 1, "stalled": 0})
        self.assertFalse(uw.all_finished(counts))

    def test_all_finished(self):
        self.assertTrue(uw.all_finished({"running": 0, "done": 2, "error": 1, "stalled": 0}))
        self.assertFalse(uw.all_finished({"running": 0, "done": 0, "error": 0, "stalled": 0}))
        self.assertFalse(uw.all_finished({"running": 0, "done": 1, "error": 0, "stalled": 1}))

    def test_idle_tracking(self):
        stalled = {"running": 0, "done": 1, "error": 0, "stalled": 1}
        self.assertEqual(uw.next_idle_since(stalled, None, 100), 100)
        self.assertEqual(uw.next_idle_since(stalled, 100, 200), 100)
        self.assertEqual(uw.next_idle_since({"running": 0, "done": 0, "error": 0, "stalled": 0}, None, 5), 5)
        self.assertIsNone(uw.next_idle_since({"running": 1, "done": 0, "error": 0, "stalled": 1}, 100, 200))
        self.assertIsNone(uw.next_idle_since({"running": 0, "done": 2, "error": 0, "stalled": 0}, 100, 200))

    def test_expiry(self):
        self.assertFalse(uw.idle_expired(None, 10 ** 9))
        self.assertFalse(uw.idle_expired(100, 100 + uw.IDLE_GIVE_UP_SEC - 1))
        self.assertTrue(uw.idle_expired(100, 100 + uw.IDLE_GIVE_UP_SEC))
        self.assertFalse(uw.retry_expired(100, 100 + uw.RETRY_SEC))
        self.assertTrue(uw.retry_expired(100, 101 + uw.RETRY_SEC))


class TestTail(unittest.TestCase):
    def test_text_and_tool(self):
        ev = json.loads(assistant("2026-11-10T05:00:10Z", [
            {"type": "text", "text": "first line\nsecond"},
            {"type": "tool_use", "name": "Read", "input": {"file_path": "/a/b/c.py"}},
            {"type": "thinking", "thinking": "x"}]))
        out = uw.format_event(ev, 80)
        self.assertEqual(len(out), 2)
        self.assertIn("first line", out[0])
        self.assertNotIn("second", out[0])
        self.assertIn("> Read /a/b/c.py", out[1])

    def test_ignores_non_assistant(self):
        self.assertEqual(uw.format_event({"type": "user", "message": {"content": "x"}}, 80), [])

    def test_tool_hint(self):
        self.assertEqual(uw.tool_hint({"command": "ls\n-la"}), "ls")  # Bash は先頭 1 行
        self.assertEqual(uw.tool_hint({"file_path": os.path.expanduser("~/x/y.py")}), "~/x/y.py")
        self.assertEqual(uw.tool_hint({"pattern": "foo", "path": "/p"}), "foo")
        self.assertEqual(uw.tool_hint({"description": "do\nit"}), "do it")
        self.assertEqual(uw.tool_hint({"x": 1}), "")
        self.assertEqual(uw.tool_hint(None), "")


class TestDiscovery(unittest.TestCase):
    def setUp(self):
        self.base = tempfile.mkdtemp()
        self.run = os.path.join(self.base, "-proj", SID, "subagents", "workflows", "wf_aaaaaaaa-111")
        self.old = os.path.join(self.base, "-proj", "subagents", "workflows", "wf_bbbbbbbb-222")
        for d in (self.run, self.old):
            os.makedirs(d)
            open(os.path.join(d, "journal.jsonl"), "w").write('{"type":"launched"}\n')
        os.utime(os.path.join(self.old, "journal.jsonl"), (1, 1))

    def tearDown(self):
        shutil.rmtree(self.base)

    def test_find_orders_newest_first_both_layouts(self):
        self.assertEqual(uw.find_run_dirs(self.base), [self.run, self.old])

    def test_resolve(self):
        self.assertEqual(uw.resolve_run_dir(None, self.base), self.run)
        self.assertEqual(uw.resolve_run_dir("wf_bbbbbbbb-222", self.base), self.old)
        self.assertEqual(uw.resolve_run_dir(self.old, self.base), self.old)
        self.assertIsNone(uw.resolve_run_dir("wf_nope", self.base))

    def test_session_id(self):
        self.assertEqual(uw.session_id_of(self.run), SID)
        self.assertIsNone(uw.session_id_of(self.old))

    def test_locate_from_hook_by_run_id(self):
        payload = {"tool_response": {"transcriptDir": ".../workflows/wf_bbbbbbbb-222"}}
        self.assertEqual(uw.locate_from_hook(payload, self.base), self.old)

    def test_locate_from_hook_fallback_session(self):
        self.assertEqual(uw.locate_from_hook({"session_id": SID, "tool_response": "x"}, self.base), self.run)
        self.assertIsNone(uw.locate_from_hook({"session_id": "other"}, self.base))

    def test_new_runs(self):
        self.assertEqual(uw.new_runs({"a"}, ["a", "b"]), ["b"])


class TestHelpers(unittest.TestCase):
    def test_resolve_agent(self):
        a = uw.parse_journal(JOURNAL)
        self.assertEqual(uw.resolve_agent(a, "analyze:two"), "bbb222")
        self.assertEqual(uw.resolve_agent(a, "aaa"), "aaa111")
        self.assertIsNone(uw.resolve_agent(a, "nope"))
        a["aaa222"] = {"label": "z", "phase": "P", "status": "done"}
        self.assertIsNone(uw.resolve_agent(a, "aaa"))  # 曖昧

    def test_owner_pane(self):
        agents = [{"pane_id": "w1:p1", "agent_session": {"value": "s1"}}, {"pane_id": "w1:p2"}]
        self.assertEqual(uw.owner_pane("s1", agents), "w1:p1")
        self.assertIsNone(uw.owner_pane("s9", agents))

    def test_find_key(self):
        self.assertEqual(uw.find_key({"tab": {"id": 1}, "root_pane": {"pane_id": "w:p1"}}, "pane_id"), "w:p1")
        self.assertIsNone(uw.find_key({"a": [1, {"b": 2}]}, "pane_id"))


class TestRunReader(unittest.TestCase):
    def test_incremental_and_partial_line(self):
        d = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, d)
        jp = os.path.join(d, "journal.jsonl")
        open(jp, "w").write(JOURNAL[1] + "\n" + JOURNAL[2][:20])  # 末尾は書きかけ
        r = uw.RunReader(d)
        self.assertEqual(list(r.journal()), ["aaa111"])
        open(jp, "a").write(JOURNAL[2][20:] + "\n")
        self.assertEqual(list(r.journal()), ["aaa111", "bbb222"])

    def test_claim_run_once(self):
        d = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, d)
        os.environ["HERDR_ULTRA_WATCH_DIR"] = d
        self.addCleanup(os.environ.pop, "HERDR_ULTRA_WATCH_DIR")
        self.assertTrue(uw.claim_run("wf_x"))
        self.assertFalse(uw.claim_run("wf_x"))


class FakeHcli:
    """hcli 差し替え。fail_on の先頭語を含む呼び出しで RuntimeError。"""

    def __init__(self, fail_on=()):
        self.calls, self.fail_on, self.n = [], fail_on, 0

    def __call__(self, *args):
        self.calls.append(args)
        if args[:2] in self.fail_on:
            raise RuntimeError("boom")
        self.n += 1
        return {"pane_id": f"p{self.n}"}


class TestOpen(unittest.TestCase):
    def setUp(self):
        d = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, d)
        os.environ["HERDR_ULTRA_WATCH_DIR"] = d
        self.addCleanup(os.environ.pop, "HERDR_ULTRA_WATCH_DIR")
        self.orig = uw.hcli
        self.addCleanup(setattr, uw, "hcli", self.orig)

    def test_run_failure_closes_split_pane_and_releases_claim(self):
        fake = FakeHcli(fail_on=[("pane", "run")])
        uw.hcli = fake
        self.assertFalse(uw.open_claimed("/x/wf_a", "p0"))
        self.assertIn(("pane", "close", "p1"), fake.calls)
        self.assertTrue(uw.claim_run("wf_a"))  # marker 解放済みなので再 claim できる

    def test_success_keeps_claim(self):
        uw.hcli = FakeHcli()
        self.assertTrue(uw.open_claimed("/x/wf_b", "p0"))
        self.assertTrue(uw.open_claimed("/x/wf_b", "p0"))  # 他が claim 済みは再試行不要
        self.assertFalse(uw.claim_run("wf_b"))

    def test_handle_new_run_retries_when_pane_unresolved(self):
        uw.hcli = lambda *a: {"agents": []}
        self.assertFalse(uw.handle_new_run(f"/p/{SID}/subagents/workflows/wf_c", False))
        self.assertTrue(uw.handle_new_run(f"/p/{SID}/subagents/workflows/wf_c", True))

    def test_hcli_converts_missing_binary_to_runtime_error(self):
        uw.hcli = self.orig
        old = os.environ.get("PATH")
        os.environ["PATH"] = "/nonexistent"
        self.addCleanup(os.environ.__setitem__, "PATH", old)
        with self.assertRaises(RuntimeError):
            uw.hcli("agent", "list")



# ---------- dashboard v2 ----------

SCRIPT_JS = """
// header
export const meta = {
  name: 'demo-flow',
  description: "Fix it, 'port' it",
  phases: [
    { title: 'Core', detail: 'ledger/state' },
    { title: "Review", detail: `adversarial {x}` },
    { title: 'Ship', detail: 'docs', },
  ],
}

const X = 1
"""


class TestMetaJs(unittest.TestCase):
    def test_parse_mixed_quotes_and_trailing_commas(self):
        m = uw.parse_meta_js(SCRIPT_JS)
        self.assertEqual(m["name"], "demo-flow")
        self.assertEqual(m["description"], "Fix it, 'port' it")
        self.assertEqual([p["title"] for p in m["phases"]], ["Core", "Review", "Ship"])
        self.assertEqual(m["phases"][1]["detail"], "adversarial {x}")  # 文字列内の波括弧で深さを崩さない

    def test_broken_input_returns_none(self):
        for bad in ("", "const a = 1", "export const meta = { name: 'x', phases: [", "export const meta = { name: foo() }"):
            self.assertIsNone(uw.parse_meta_js(bad), bad)

    def test_find_script_variants(self):
        d = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, d)
        run = os.path.join(d, "proj", SID, "subagents", "workflows", "wf_aaaa1111-222")
        os.makedirs(run)
        self.assertIsNone(uw.find_script(run, base=d))
        scripts = os.path.join(d, "proj", SID, "workflows", "scripts")
        os.makedirs(scripts)
        path = os.path.join(scripts, "demo-flow-wf_aaaa1111-222.js")
        open(path, "w").write(SCRIPT_JS)
        self.assertEqual(os.path.realpath(uw.find_script(run, base=d)), os.path.realpath(path))
        # session 階層なし: proj/workflows/scripts
        run2 = os.path.join(d, "p2", "subagents", "workflows", "wf_bbbb1111-333")
        os.makedirs(run2)
        os.makedirs(os.path.join(d, "p2", "workflows", "scripts"))
        p2 = os.path.join(d, "p2", "workflows", "scripts", "x-wf_bbbb1111-333.js")
        open(p2, "w").write(SCRIPT_JS)
        self.assertEqual(os.path.realpath(uw.find_script(run2, base=d)), os.path.realpath(p2))


def arow(label, phase, state, first=0.0, last=10.0, **kw):
    r = {"id": label, "label": label, "phase": phase, "state": state, "elapsed": last - first, "tokens": 100,
         "tools": 2, "tool": "Bash", "hint": "ls -la", "text": "考え中のテキスト", "idle": 3.0, "tool_error": False,
         "first_ts": first, "last_ts": last, "recent": [], "result": "done ok", "model": "opus-5-5", "desc": ""}
    r.update(kw)
    return r


PLANNED = [{"title": "Core", "detail": "d1"}, {"title": "Review", "detail": "d2"}, {"title": "Ship", "detail": "d3"}]


class TestPhaseStates(unittest.TestCase):
    def st(self, rows, run_done=False, planned=PLANNED):
        return {p["name"]: p for p in uw.phase_states(planned, rows, run_done)}

    def test_last_planned_phase_done_when_earlier_phase_restarts_later(self):
        # Ship (meta 最後) 完了後、時系列で後に Review の agent が始まった -> Ship は done
        rows = [arow("c", "Core", "done", 0, 10), arow("s", "Ship", "done", 20, 30),
                arow("r", "Review", "running", 40, 50)]
        st = self.st(rows)
        self.assertEqual(st["Ship"]["state"], "done")
        self.assertFalse(st["Ship"]["waiting"])

    def test_pending_and_planned_order(self):
        out = uw.phase_states(PLANNED, [arow("a", "Review", "running")], False)
        self.assertEqual([p["name"] for p in out], ["Core", "Review", "Ship"])
        self.assertEqual([p["state"] for p in out], ["pending", "running", "pending"])
        self.assertEqual(out[0]["detail"], "d1")

    def test_parallel_started_uses_meta_order_not_start_order(self):
        rows = [arow("r", "Review", "running", first=1), arow("c", "Core", "running", first=5)]
        self.assertEqual([p["name"] for p in uw.phase_states(PLANNED, rows, False)], ["Core", "Review", "Ship"])

    def test_phase_outside_meta_appended(self):
        out = uw.phase_states(PLANNED, [arow("x", "Weird", "done")], True)
        self.assertEqual(out[-1]["name"], "Weird")
        self.assertFalse(out[-1]["planned"])
        self.assertEqual(out[-1]["state"], "done")

    def test_all_done_waits_for_next_phase(self):
        rows = [arow("c", "Core", "done"), arow("r", "Review", "running")]
        s = self.st(rows)
        self.assertEqual(s["Core"]["state"], "done")  # 後続 phase が started
        s = self.st([arow("c", "Core", "done")])
        self.assertEqual(s["Core"]["state"], "running")  # 次の起動待ち
        self.assertTrue(s["Core"]["waiting"])
        s = self.st([arow("c", "Core", "done")], run_done=True)
        self.assertEqual(s["Core"]["state"], "done")

    def test_error_dominates_and_wall_time(self):
        rows = [arow("a", "Core", "done", 0, 50), arow("b", "Core", "error", 10, 90)]
        c = self.st(rows)["Core"]
        self.assertEqual(c["state"], "error")
        self.assertEqual(c["wall"], 90)
        self.assertEqual((c["done"], c["total"]), (1, 2))

    def test_grow_marker_only_without_later_started(self):
        s = self.st([arow("a", "Core", "running")])
        self.assertTrue(s["Core"]["grow"])
        s = self.st([arow("a", "Core", "running"), arow("b", "Review", "running")])
        self.assertFalse(s["Core"]["grow"])


class TestEta(unittest.TestCase):
    def phases(self, done_walls, running_first=None, pending=1):
        ph = [{"state": "done", "wall": w, "first_ts": 0} for w in done_walls]
        if running_first is not None:
            ph.append({"state": "running", "wall": None, "first_ts": running_first})
        ph += [{"state": "pending", "wall": None, "first_ts": None}] * pending
        return ph

    def test_none_without_evidence(self):
        self.assertIsNone(uw.eta(self.phases([]), [1, 2], 100))

    def test_median_formula(self):
        ph = self.phases([60, 120, 600], running_first=900)  # median 120, remaining 2, 1 running
        self.assertEqual(uw.eta(ph, [], 1000), 120 * 2 / 1 - 100)

    def test_floor_zero_and_hidden_cases(self):
        ph = self.phases([60], running_first=0)
        self.assertEqual(uw.eta(ph, [], 10 ** 6), 0.0)
        self.assertIsNone(uw.eta(ph, [], 10, any_stalled=True))
        self.assertIsNone(uw.eta(ph, [], 10, finished=True))

    def test_five_done_agents_fallback(self):
        ph = self.phases([], running_first=0, pending=1)
        self.assertIsNotNone(uw.eta(ph, [10] * 5, 5))


class TestGist(unittest.TestCase):
    def test_findings(self):
        res = {"findings": [{"severity": "high", "problem": "壊れる"}, {"severity": "med"}, {"severity": "medium"},
                            {"severity": "low"}]}
        g, kind = uw.result_gist(res)
        self.assertEqual((g, kind), ("findings 4 (high 1, medium 2, low 1)", "findings"))
        g, _ = uw.result_gist(res, 80)
        self.assertIn("壊れる", g)
        self.assertEqual(uw.result_gist({"findings": []})[0], "findings 0")

    def test_dict_keys_priority(self):
        self.assertEqual(uw.result_gist({"verdict": "ship", "summary": "ok!"})[0], "ok!")
        self.assertEqual(uw.result_gist({"a": 1, "b": "x"})[0], "a=1 b=x")

    def test_error(self):
        self.assertEqual(uw.result_gist({"error": "boom\nstack"}), ("boom stack", "error"))
        self.assertEqual(uw.result_gist("TypeError: bad")[1], "error")
        self.assertEqual(uw.result_gist("Error: " + "x" * 50, 20)[0].__len__(), 20)

    def test_markdown_and_harness_note(self):
        s = "[harness: subagent output matched x.] (Matched in the string values.)\n\n## **変更** したよ。次に\n- a"
        self.assertEqual(uw.result_gist(s)[0], "変更 したよ。")
        self.assertEqual(uw.result_gist("- `code` is **bold**. more")[0], "code is bold.")

    def test_generic_first_line_prefers_keyword(self):
        s = "Done\n\nいろいろやった\nテスト 12 件 passed。\n"
        self.assertEqual(uw.result_gist(s)[0], "テスト 12 件 passed。")
        self.assertEqual(uw.result_gist("Done")[0], "Done")

    def test_japanese_and_clip(self):
        g, _ = uw.result_gist("日本語の長い結果テキストです" * 5, 20)
        self.assertLessEqual(uw.disp_width(g), 20)


def mk_model(rows, planned=PLANNED, now=1000.0, **kw):
    agents = {r["id"]: {"label": r["label"], "phase": r["phase"],
                        "status": "running" if r["state"] in ("running", "stalled") else r["state"],
                        "result": r["result"]} for r in rows}
    stats = {}
    for r in rows:
        st = uw.new_stats()
        st.update(first_ts=r["first_ts"], last_ts=r["last_ts"], last_tool=r["tool"], last_hint=r["hint"],
                  last_text=r["text"], last_activity_ts=now - (r["idle"] or 0), tool_calls=r["tools"])
        st["_msgs"] = {"m": r["tokens"]}
        st["tool_error"] = r["tool_error"]
        stats[r["id"]] = st
    events = [("started", r["id"]) for r in rows] + [(r["state"], r["id"]) for r in rows if r["state"] in ("done", "error")]
    return uw.build_model(agents, stats, {}, {"name": "demo-flow", "description": "Fix it all " * 8, "phases": planned},
                          "wf_demo", events, now, **kw)


def busy_rows(now=1000.0, n_done=12):
    rows = [arow(f"done:{i}", "Core" if i < 3 else "Review", "done", now - 500 + i, now - 400 + i * 2) for i in range(n_done)]
    rows += [arow("日本語ラベル:実行中", "Review", "running", now - 100, now - 3),
             arow("run:2", "Review", "running", now - 90, now - 3, tool_error=True),
             arow("stall:1", "Review", "running", now - 900, now - 600, idle=600)]
    return rows


class TestBuildModel(unittest.TestCase):
    def test_counts_progress_tokens(self):
        m = mk_model(busy_rows())
        self.assertEqual(m.counts, {"running": 2, "done": 12, "error": 0, "stalled": 1})
        self.assertEqual((m.finished_n, m.seen), (12, 15))
        self.assertEqual(m.tokens, 1500)
        self.assertEqual(m.planned_n, 3)
        self.assertTrue(m.has_alert)
        self.assertIsNone(m.eta)  # 停滞あり
        self.assertEqual(len(m.feed), 27)  # started 15 + done 12

    def test_fallback_without_meta(self):
        agents = {"a": {"label": "l", "phase": "P1", "status": "done", "result": "x"}}
        m = uw.build_model(agents, {}, {}, None, "wf_z", [], 0)
        self.assertEqual((m.name, m.planned_n), ("wf_z", 1))
        self.assertEqual(m.description, "")

    def test_elapsed_frozen_when_finished(self):
        rows = [arow("a", "Core", "done", 100, 400), arow("b", "Ship", "done", 150, 700)]
        m = mk_model(rows, now=10 ** 6)
        self.assertTrue(m.finished)
        self.assertEqual(m.elapsed, 600)

    def test_stall_event_in_feed(self):
        m = mk_model(busy_rows(), stall_events=[(999.0, "stall:1")])
        self.assertEqual(m.feed[0]["kind"], "stalled")


class TestRender(unittest.TestCase):
    def frame(self, m, w, h, **vskw):
        vs = uw.ViewState(**vskw)
        return uw.render_frame(m, vs, w, h, 3, color=False)

    def test_layout_modes(self):
        self.assertEqual([uw.layout(w) for w in (59, 99, 100, 119, 120, 200)],
                         ["narrow", "narrow", "wide1", "wide1", "wide2", "wide2"])

    def test_no_line_overflows_any_width_height(self):
        m = mk_model(busy_rows())
        for w in (30, 60, 70, 100, 119, 140):
            for h in (8, 15, 30, 60):
                for color in (False, True):
                    vs = uw.ViewState()
                    lines = uw.render_frame(m, vs, w, h, 1, color)
                    self.assertLessEqual(len(lines), h, (w, h))
                    for ln in lines:
                        self.assertLessEqual(uw.disp_width(uw.strip_ansi(ln)), w, (w, h, ln))
                        if color:
                            self.assertTrue(ln.endswith("\x1b[0m"))

    def test_japanese_labels_do_not_overflow(self):
        rows = busy_rows() + [arow("ひらがなカタカナ漢字" * 4, "Ship", "running")]
        m = mk_model(rows)
        for w in (45, 60, 70, 100, 140):
            for ln in self.frame(m, w, 40):
                self.assertLessEqual(uw.disp_width(ln), w)

    def test_tiny_height_summary_only(self):
        m = mk_model(busy_rows())
        out = self.frame(m, 70, 5)
        self.assertEqual(len(out), 1)
        self.assertIn("▶2", out[0])
        self.assertIn("12/15", out[0])
        self.assertIn("Review", out[0])

    def test_never_drop_items_at_min_height(self):
        m = mk_model(busy_rows())
        out = "\n".join(self.frame(m, 70, 8))
        for need in ("demo-flow", "全体", "実行中", "q終了", "日本語ラベル:実行中"):
            self.assertIn(need, out)

    def test_height_drop_order(self):
        m = mk_model(busy_rows())
        tall = "\n".join(self.frame(m, 70, 60))
        self.assertIn("FEED", "\n".join(self.frame(m, 70, 60, feed_n=5)))
        self.assertNotIn("FEED", tall)  # 狭幅は feed 既定 off
        self.assertIn("「考え中のテキスト」", tall)
        mid = self.frame(m, 70, 22)
        self.assertEqual(len(mid) <= 22, True)
        self.assertIn("日本語ラベル:実行中", "\n".join(mid))
        # 低くなるほど: text 行 -> tool 行 の順に消える
        counts = []
        for h in (40, 20, 14, 11, 9):
            txt = "\n".join(self.frame(m, 70, h))
            counts.append(("「考え中のテキスト」" in txt, "Bash" in txt))
        for a, b in zip(counts, counts[1:]):
            self.assertTrue(a[0] >= b[0])
        self.assertTrue(counts[0][0])

    def test_results_min_three_then_removed(self):
        m = mk_model(busy_rows())
        n = lambda h: "\n".join(self.frame(m, 70, h)).count(" ✓ done:")
        self.assertGreaterEqual(n(24), 3)
        self.assertEqual(n(8), 0)

    def test_results_fold_done_phases_unless_all(self):
        m = mk_model(busy_rows())
        txt = "\n".join(self.frame(m, 70, 60))
        self.assertNotIn("done:0 ", txt)  # Core は完了 phase なので畳む
        self.assertIn("畳み済 phase: Core", txt)
        self.assertIn("done:0", "\n".join(self.frame(m, 70, 60, show_all=True)))

    def test_wide_strip_and_two_columns(self):
        m = mk_model(busy_rows())
        w1 = "\n".join(self.frame(m, 110, 40))
        self.assertIn("┌ Core", w1)
        w2 = self.frame(m, 140, 40)
        self.assertTrue(any("┃" in ln for ln in w2))
        self.assertTrue(any("FEED" in ln for ln in w2))
        old = os.environ.get("DASH_ASCII")
        os.environ["DASH_ASCII"] = "1"
        try:
            a = "\n".join(self.frame(m, 140, 40))
            self.assertNotIn("┃", a)
            self.assertNotIn("─", a)
        finally:
            os.environ.pop("DASH_ASCII") if old is None else os.environ.__setitem__("DASH_ASCII", old)

    def test_strip_falls_back_to_list_when_too_narrow(self):
        many = [{"title": f"Phase-number-{i}", "detail": ""} for i in range(8)]
        m = mk_model([arow("a", "Phase-number-0", "running")], planned=many)
        out = "\n".join(self.frame(m, 100, 40))
        self.assertNotIn("┌", out)
        self.assertIn("Phase-number-7", out)

    def test_header_color_states(self):
        ok = mk_model([arow("a", "Core", "done"), arow("b", "Ship", "done")])
        txt = "\n".join(uw.render_frame(ok, uw.ViewState(), 70, 30, 0, True))
        self.assertIn("\x1b[1;32m◆", txt)
        self.assertIn("完了", txt)
        bad = mk_model(busy_rows())
        self.assertIn("\x1b[1;31m◆", "\n".join(uw.render_frame(bad, uw.ViewState(), 70, 30, 0, True)))

    def test_no_color_has_no_escapes(self):
        m = mk_model(busy_rows())
        self.assertNotIn("\x1b", "".join(self.frame(m, 100, 40)))

    def test_spinner_advances_with_frame(self):
        m = mk_model(busy_rows())
        a = uw.render_frame(m, uw.ViewState(), 70, 40, 0, False)
        b = uw.render_frame(m, uw.ViewState(), 70, 40, 1, False)
        self.assertNotEqual(a, b)
        self.assertEqual(a, uw.render_frame(m, uw.ViewState(), 70, 40, 0, False))

    def test_stalled_and_tool_error_shown(self):
        txt = "\n".join(self.frame(mk_model(busy_rows()), 100, 40))
        self.assertIn("停滞", txt)
        self.assertIn("! tool error", txt)

    def test_thinking_hint(self):
        r = arow("t", "Core", "running", idle=30, text="")
        txt = "\n".join(self.frame(mk_model([r]), 70, 30))
        self.assertIn("考え中…", txt)
        r = arow("t", "Core", "running", idle=90)
        self.assertIn("考え中… 90s", "\n".join(self.frame(mk_model([r]), 70, 30)))

    def test_only_errors_filter(self):
        rows = busy_rows() + [arow("bad", "Review", "error", result={"error": "kaboom"})]
        txt = "\n".join(self.frame(mk_model(rows), 70, 40, only_err=True))
        self.assertIn("kaboom", txt)
        self.assertNotIn("run:2", txt)
        self.assertIn("stall:1", txt)

    def test_detail_and_results_views(self):
        res = {"findings": [{"severity": "low", "file": "a.py", "line": 3, "problem": "p1", "fix": "f1"},
                            {"severity": "high", "file": "b.py", "line": 9, "problem": "p2", "fix": "f2"}]}
        rows = busy_rows() + [arow("rv", "Review", "done", result=res)]
        m = mk_model(rows)
        vs = uw.ViewState(view="detail", detail_id="rv")
        txt = "\n".join(uw.render_frame(m, vs, 80, 40, 0, False))
        self.assertIn("b.py:9 p2", txt)
        self.assertLess(txt.index("p2"), txt.index("p1"))  # severity 降順
        vs = uw.ViewState(view="results")
        txt = "\n".join(uw.render_frame(m, vs, 80, 40, 0, False))
        self.assertIn("findings 計 2: high 1 · medium 0 · low 1", txt)
        for ln in uw.render_frame(m, uw.ViewState(view="detail", detail_id="rv"), 40, 12, 0, False):
            self.assertLessEqual(uw.disp_width(ln), 40)

    def test_detail_scroll_clamped(self):
        m = mk_model(busy_rows())
        vs = uw.ViewState(view="detail", detail_id="日本語ラベル:実行中", scroll=999)
        uw.render_frame(m, vs, 80, 10, 0, False)
        self.assertEqual(vs.scroll, vs.max_scroll)


class TestKeys(unittest.TestCase):
    def test_parse_keys(self):
        self.assertEqual(uw.parse_keys("j\x1b[Bk\x1b\r\t\x7f\x1b[5~q"), ["j", "down", "k", "esc", "enter", "tab", "backspace", "q"])

    def test_navigation_and_detail(self):
        m = mk_model(busy_rows())
        vs = uw.ViewState(interactive=True)
        ids = uw.nav_ids(m, vs)
        self.assertEqual(uw.sel_id(vs, "now", ids), ids[0])
        uw.handle_key(vs, m, "j")
        self.assertEqual(vs.sel["now"], ids[1])
        uw.handle_key(vs, m, "G")
        self.assertEqual(vs.sel["now"], ids[-1])
        uw.handle_key(vs, m, "enter")
        self.assertEqual((vs.view, vs.detail_id), ("detail", ids[-1]))
        uw.handle_key(vs, m, "esc")
        self.assertEqual(vs.view, "main")
        self.assertEqual(uw.handle_key(vs, m, "t"), "tail")
        self.assertEqual(uw.handle_key(vs, m, "T"), "tails")
        self.assertEqual(uw.handle_key(vs, m, "R"), "reload")
        self.assertEqual(uw.handle_key(vs, m, "q"), "quit")

    def test_toggles_and_feed_cycle(self):
        m = mk_model(busy_rows())
        vs = uw.ViewState()
        seen = []
        for _ in range(5):
            uw.handle_key(vs, m, "f")
            seen.append(vs.feed_n)
        self.assertEqual(seen, [5, 10, 20, 0, 5])
        uw.handle_key(vs, m, "a")
        uw.handle_key(vs, m, "e")
        uw.handle_key(vs, m, "p")
        self.assertEqual((vs.show_all, vs.only_err, vs.strip), (True, True, False))
        uw.handle_key(vs, m, "tab")
        self.assertEqual(vs.focus, "results")
        uw.handle_key(vs, m, "?")
        self.assertTrue(vs.help)
        uw.handle_key(vs, m, "x")
        self.assertFalse(vs.help)


class TestDiffAndNotify(unittest.TestCase):
    def test_diff_output(self):
        a = ["x", "y", "z"]
        self.assertEqual(uw.diff_output(a, list(a)), "")
        out = uw.diff_output(a, ["x", "Y", "z"])
        self.assertIn("\x1b[2;1HY", out)
        self.assertNotIn("x", out)
        self.assertTrue(uw.diff_output(None, a).startswith("\x1b[H"))
        self.assertTrue(uw.diff_output(a, ["x"]).startswith("\x1b[H"))

    def test_notifications_once(self):
        rows = [arow("a", "Core", "error", result={"error": "x"}), arow("b", "Core", "running", idle=700),
                arow("s", "Ship", "stalled")]
        m = mk_model(rows)
        seen = set()
        first = uw.new_notifications(m, seen)
        self.assertEqual(sorted(t for _, t, _ in first), ["ultracode エラー", "ultracode 停滞", "ultracode 停滞"])
        self.assertEqual(uw.new_notifications(m, seen), [])
        fin = mk_model([arow("a", "Core", "done"), arow("b", "Ship", "done")])
        n = uw.new_notifications(fin, seen)
        self.assertEqual(len(n), 1)
        self.assertIn("完了 ✓2 ✗0", n[0][1])


class TestIncrementalStats(unittest.TestCase):
    def setUp(self):
        self.d = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.d)
        self.jp = os.path.join(self.d, "journal.jsonl")
        self.ap = os.path.join(self.d, "agent-aaa111.jsonl")
        open(self.jp, "w").write(JOURNAL[1] + "\n")
        open(os.path.join(self.d, "agent-aaa111.meta.json"), "w").write(
            '{"description":"d","workflowPhase":"Analyze","pickedEffortModel":"claude-opus-5-5"}')

    def test_reads_only_new_bytes_and_text_fields(self):
        open(self.ap, "w").write(assistant("2026-11-10T05:00:10Z", [
            {"type": "thinking", "thinking": "hidden"}, {"type": "text", "text": "\n\n  最初の行\n2行目"},
            {"type": "tool_use", "name": "Grep", "input": {"pattern": "foo", "path": "/x"}}]) + "\n")
        r = uw.RunReader(self.d)
        r.snapshot()
        st = r.stats["aaa111"]
        self.assertEqual(st["last_text"], "最初の行")
        self.assertEqual((st["last_tool"], st["last_hint"]), ("Grep", "foo"))
        self.assertEqual(r.metas["aaa111"]["pickedEffortModel"], "claude-opus-5-5")
        off = r._offsets[self.ap]
        with open(self.ap, "a") as f:
            f.write(json.dumps({"type": "user", "timestamp": "2026-11-10T05:00:11Z", "message": {"content": [
                {"type": "tool_result", "is_error": True, "content": "No such file"}]}}) + "\n")
        r.snapshot()
        self.assertGreater(r._offsets[self.ap], off)
        self.assertEqual(st["tool_calls"], 1)  # 既存分は再集計されない
        self.assertTrue(st["tool_error"])
        self.assertEqual([k for _, k, _ in st["recent"]], ["text", "tool", "error"])

    def test_recent_is_bounded(self):
        st = uw.new_stats()
        uw.update_stats(st, [assistant("2026-11-10T05:00:00Z", [{"type": "text", "text": f"t{i}"}], f"m{i}") for i in range(60)])
        self.assertEqual(len(st["recent"]), uw.RECENT_MAX)

    def test_finished_agent_dropped_and_not_reread(self):
        open(self.ap, "w").write(assistant("2026-11-10T05:00:10Z", [{"type": "text", "text": "hi"}]) + "\n")
        r = uw.RunReader(self.d)
        r.snapshot()
        self.assertTrue(r.stats["aaa111"]["recent"])
        with open(self.jp, "a") as f:
            f.write(JOURNAL[4] + "\n")  # aaa111 result
        with open(self.ap, "a") as f:
            f.write(assistant("2026-11-10T05:00:20Z", [{"type": "text", "text": "last"}], "m2") + "\n")
        r.snapshot()
        st = r.stats["aaa111"]
        self.assertEqual(len(st["recent"]), 0)
        self.assertEqual(st["last_text"], "")
        self.assertEqual(st["last_ts"] - st["first_ts"], 10.0)  # 最終 1 回は読んでいる
        off = r._offsets[self.ap]
        with open(self.ap, "a") as f:
            f.write(assistant("2026-11-10T05:00:30Z", [{"type": "text", "text": "late"}], "m3") + "\n")
        r.snapshot()
        self.assertEqual(r._offsets[self.ap], off)  # 以後は読まない
        self.assertEqual(r.agents["aaa111"]["result"], "ok")

    def test_workflow_meta_cached_by_mtime(self):
        sd = os.path.join(self.d, "..", "..", "..", "workflows", "scripts")
        run = os.path.join(self.d, "a", "b", "c", "wf_zzzz1111-1")
        os.makedirs(run)
        os.makedirs(os.path.join(run, "..", "..", "..", "workflows", "scripts"))
        sp = os.path.join(run, "..", "..", "..", "workflows", "scripts", "n-wf_zzzz1111-1.js")
        open(sp, "w").write(SCRIPT_JS)
        r = uw.RunReader(run)
        self.assertEqual(r.workflow_meta()["name"], "demo-flow")
        first = r.wfmeta
        self.assertIs(r.workflow_meta(), first)  # mtime 不変なら再パースしない
        open(sp, "w").write(SCRIPT_JS.replace("demo-flow", "other"))
        os.utime(sp, (time_now() + 5, time_now() + 5))
        self.assertEqual(r.workflow_meta()["name"], "other")


def time_now():
    import time
    return time.time()


class TestDashboardOnce(unittest.TestCase):
    def test_once_snapshot_end_to_end(self):
        import contextlib
        import io
        d = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, d)
        run = os.path.join(d, "proj", SID, "subagents", "workflows", "wf_deadbeef-001")
        os.makedirs(run)
        open(os.path.join(run, "journal.jsonl"), "w").write("\n".join(JOURNAL) + "\n")
        buf = io.StringIO()
        args = uw.build_parser().parse_args(["dashboard", "--once", run, "--width", "60"])
        with contextlib.redirect_stdout(buf):
            self.assertEqual(uw.cmd_dashboard(args), 0)
        out = buf.getvalue().splitlines()
        self.assertTrue(out)
        self.assertTrue(all(uw.disp_width(ln) <= 60 for ln in out))
        self.assertIn("wf_deadbeef-001", out[0])
        self.assertIn("エラー", "\n".join(out))


class TestReviewFixes(unittest.TestCase):
    def test_u2028_line_not_split(self):
        d = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, d)
        ev = json.dumps({"type": "result", "agentId": "a", "result": "x\u2028y"}, ensure_ascii=False)
        with open(os.path.join(d, "t.jsonl"), "w", encoding="utf-8") as f:
            f.write(ev + "\n")
        lines = uw.RunReader(d).new_lines(os.path.join(d, "t.jsonl"))
        self.assertEqual(len(lines), 1)
        self.assertEqual(json.loads(lines[0])["result"], "x\u2028y")

    def test_not_finished_between_phases(self):
        rows = [arow("c", "Core", "done"), arow("r", "Review", "done", 11, 20)]
        m = mk_model(rows, now=30.0)
        self.assertFalse(m.finished)  # Ship 未着手
        self.assertTrue(mk_model(rows, now=1000.0).finished)  # 10 分動かなければ phase スキップ扱い
        self.assertEqual(m.pending_n, 1)
        self.assertTrue(m.eta_unknown)
        self.assertTrue([p for p in m.phases if p["name"] == "Review"][0]["waiting"])
        out = "\n".join(uw.render_frame(m, uw.ViewState(), 100, 30))
        self.assertIn("~残 ?", out)
        self.assertNotIn("100%", out)

    def test_bar_floor(self):
        full = lambda b: "".join(t for t, _ in b).count("█")
        self.assertEqual(full(uw.bar(18, 19, 8)), 7)
        self.assertEqual(full(uw.bar(19, 19, 8, open_end=True)), 7)
        self.assertEqual(full(uw.bar(19, 19, 8)), 8)

    def test_clip_mid_keeps_tail(self):
        s = uw.clip_mid("verify:misc-ops:duration-stall", 16)
        self.assertEqual(uw.disp_width(s), 16)
        self.assertTrue(s.endswith("stall"))

    def test_eta_uses_agent_median(self):
        ph = [{"state": "running", "wall": None, "first_ts": 0.0}]
        self.assertEqual(uw.eta(ph, [100, 100, 100], 50, running_elapsed=[30]), 70)
        self.assertIsNone(uw.eta(ph, [100], 50, running_elapsed=[30]))


if __name__ == "__main__":
    unittest.main()
