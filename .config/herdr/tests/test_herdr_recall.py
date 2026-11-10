import contextlib
import importlib.machinery
import importlib.util
import io
import json
import os
import stat
import tempfile
import unittest
from unittest import mock

SCRIPT = os.path.join(os.path.dirname(__file__), "..", "herdr-recall")
loader = importlib.machinery.SourceFileLoader("herdr_recall", SCRIPT)
spec = importlib.util.spec_from_loader("herdr_recall", loader)
hr = importlib.util.module_from_spec(spec)
loader.exec_module(hr)

T0 = "2026-07-13T01:00:00.000Z"  # JST 10:00


def claude_line(role, content, ts=T0, cwd="/work/app", **extra):
    msg = {"role": role, "content": content}
    return {"type": role, "timestamp": ts, "cwd": cwd, "message": msg, **extra}


def codex_msg(role, text, ts=T0, kind=None):
    kind = kind or ("input_text" if role == "user" else "output_text")
    return {
        "timestamp": ts,
        "type": "response_item",
        "payload": {"type": "message", "role": role, "content": [{"type": kind, "text": text}]},
    }


def jline(obj):
    """実 transcript と同じコンパクト JSON の1行(prefilter がこの形を前提にする)。"""
    return json.dumps(obj, ensure_ascii=False, separators=(",", ":")) + "\n"


def write_jsonl(path, objs):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        f.writelines(jline(o) for o in objs)


class TestParseClaude(unittest.TestCase):
    def test_string_content(self):
        cwd, msg = hr.parse_claude_obj(claude_line("user", "こんにちは"))
        self.assertEqual(cwd, "/work/app")
        self.assertEqual((msg.role, msg.text), ("user", "こんにちは"))

    def test_text_blocks_only(self):
        content = [
            {"type": "thinking", "thinking": "秘密"},
            {"type": "text", "text": "answer"},
            {"type": "tool_use", "name": "Bash", "input": {}},
        ]
        _, msg = hr.parse_claude_obj(claude_line("assistant", content))
        self.assertEqual(msg.text, "answer")

    def test_tool_result_dropped(self):
        content = [{"type": "tool_result", "content": "huge"}]
        self.assertIsNone(hr.parse_claude_obj(claude_line("user", content))[1])

    def test_meta_and_injected_dropped(self):
        self.assertIsNone(hr.parse_claude_obj(claude_line("user", "x", isMeta=True))[1])
        self.assertIsNone(hr.parse_claude_obj(claude_line("user", "<system-reminder>a"))[1])
        self.assertIsNone(hr.parse_claude_obj(claude_line("assistant", "x", isApiErrorMessage=True))[1])

    def test_non_message_type_keeps_cwd_only(self):
        cwd, msg = hr.parse_claude_obj({"type": "attachment", "cwd": "/w"})
        self.assertEqual((cwd, msg), ("/w", None))

    def test_text_capped(self):
        _, msg = hr.parse_claude_obj(claude_line("user", "a" * (hr.MAX_TEXT + 500)))
        self.assertEqual(len(msg.text), hr.MAX_TEXT)

    def test_bad_timestamp_dropped(self):
        self.assertIsNone(hr.parse_claude_obj(claude_line("user", "x", ts="nope"))[1])

    def test_prefilter(self):
        self.assertTrue(hr.claude_wanted(b'{"type":"user","x":1}\n'))
        self.assertFalse(hr.claude_wanted(b'{"type":"user","message":{"content":[{"type":"tool_result"}]}}\n'))
        self.assertFalse(hr.claude_wanted(b'{"type":"attachment"}\n'))


class TestParseMalformed(unittest.TestCase):
    def test_claude_odd_shapes_do_not_raise(self):
        bad = [
            {"type": "user", "timestamp": T0, "message": "plain string"},
            {"type": "user", "timestamp": T0, "message": {"content": [{"type": "text", "text": None}]}},
            {"type": "user", "timestamp": T0, "message": {"content": 5}},
            ["not", "a", "dict"],
            "str",
        ]
        for obj in bad:
            self.assertIsNone(hr.parse_claude_obj(obj)[1], obj)

    def test_codex_odd_shapes_do_not_raise(self):
        bad = [
            {"type": "response_item", "timestamp": T0, "payload": "str"},
            {"type": "response_item", "timestamp": T0,
             "payload": {"type": "message", "role": "user", "content": [{"type": "input_text", "text": 3}]}},
            {"type": "response_item", "timestamp": T0, "payload": {"type": "message", "role": "user", "content": "x"}},
            [1, 2],
        ]
        for obj in bad:
            self.assertIsNone(hr.parse_codex_obj(obj)[1], obj)


class TestParseCodex(unittest.TestCase):
    def test_session_meta_gives_cwd(self):
        obj = {"type": "session_meta", "payload": {"id": "x", "cwd": "/c"}}
        self.assertEqual(hr.parse_codex_obj(obj), ("/c", None))

    def test_user_and_assistant(self):
        self.assertEqual(hr.parse_codex_obj(codex_msg("user", "hi"))[1].role, "user")
        self.assertEqual(hr.parse_codex_obj(codex_msg("assistant", "yo"))[1].text, "yo")

    def test_developer_and_injected_dropped(self):
        self.assertIsNone(hr.parse_codex_obj(codex_msg("developer", "rules"))[1])
        self.assertIsNone(hr.parse_codex_obj(codex_msg("user", "<environment_context>..."))[1])
        self.assertIsNone(hr.parse_codex_obj(codex_msg("user", "# AGENTS.md instructions"))[1])

    def test_function_call_ignored(self):
        obj = {"timestamp": T0, "type": "response_item", "payload": {"type": "function_call", "name": "x"}}
        self.assertEqual(hr.parse_codex_obj(obj), (None, None))

    def test_prefilter(self):
        self.assertTrue(hr.codex_wanted(b'{"payload":{"type":"message"}}\n'))
        self.assertFalse(hr.codex_wanted(b'{"payload":{"type":"function_call_output"}}\n'))


class TestHelpers(unittest.TestCase):
    def test_parse_ts(self):
        self.assertEqual(hr.parse_ts("1970-01-01T00:00:01.500Z"), 1.5)
        self.assertIsNone(hr.parse_ts(None))
        self.assertIsNone(hr.parse_ts("x"))

    def test_session_id_for(self):
        self.assertEqual(hr.session_id_for("claude", "/p/-proj/abc-123.jsonl"), "abc-123")
        self.assertEqual(
            hr.session_id_for("claude", "/p/-proj/parent-uuid/subagents/agent-x.jsonl"), "parent-uuid/agent-x"
        )
        self.assertEqual(
            hr.session_id_for("claude", "/p/-proj/parent-uuid/subagents/workflow/agent-y.jsonl"),
            "parent-uuid/workflow/agent-y",
        )
        uid = "019e0613-096d-7583-b85c-cb3c37374d66"
        self.assertEqual(hr.session_id_for("codex", f"/s/2026/rollout-2026-05-08T14-32-51-{uid}.jsonl"), uid)

    def test_parse_since_relative(self):
        self.assertEqual(hr.parse_since("7d", now=1_000_000), 1_000_000 - 7 * 86400)
        self.assertEqual(hr.parse_since("12h", now=100_000), 100_000 - 12 * 3600)
        self.assertEqual(hr.parse_since("2w", now=2_000_000), 2_000_000 - 2 * 604800)

    def test_parse_since_absolute_and_invalid(self):
        self.assertAlmostEqual(hr.parse_since("2026-07-01"), hr.parse_user_time("2026-07-01"))
        with self.assertRaises(ValueError):
            hr.parse_since("yesterday")

    def test_parse_user_time(self):
        self.assertEqual(hr.parse_user_time("12345"), 12345.0)
        self.assertEqual(hr.parse_user_time("1970-01-01T00:00:10Z"), 10.0)

    def test_short_path(self):
        self.assertEqual(hr.short_path(os.path.join(hr.HOME, "x", "y")), "~/x/y")
        self.assertEqual(hr.short_path("/etc/z"), "/etc/z")
        self.assertEqual(hr.short_path(None), "")


class TestRefreshDue(unittest.TestCase):
    def test_refresh_due(self):
        self.assertTrue(hr.refresh_due(None, 100))
        self.assertTrue(hr.refresh_due("garbage", 100))
        self.assertFalse(hr.refresh_due("95", 100, interval=10))
        self.assertTrue(hr.refresh_due("90", 100, interval=10))


class TestQuery(unittest.TestCase):
    def test_split_terms_phrase(self):
        self.assertEqual(hr.split_terms('foo "bar baz"  qux'), ["foo", "bar baz", "qux"])

    def test_fts_capable_trigram(self):
        self.assertTrue(hr.fts_capable("abc", "trigram"))
        self.assertFalse(hr.fts_capable("ab", "trigram"))
        self.assertFalse(hr.fts_capable("削除", "trigram"))
        self.assertTrue(hr.fts_capable("削除する", "trigram"))

    def test_fts_capable_unicode61(self):
        self.assertTrue(hr.fts_capable("ab", "unicode61"))
        self.assertFalse(hr.fts_capable("削除", "unicode61"))
        self.assertFalse(hr.fts_capable("--", "unicode61"))

    def test_build_match_splits_terms(self):
        expr, like = hr.build_match(["herdr", "ab", 'q"x'], "trigram")
        self.assertEqual(expr, '"herdr" AND "q""x"')
        self.assertEqual(like, ["ab"])
        self.assertEqual(hr.build_match(["ab"], "trigram"), (None, ["ab"]))

    def test_like_pattern_escapes(self):
        self.assertEqual(hr.like_pattern("50%_a\\"), "%50\\%\\_a\\\\%")

    def test_snippet_centers_on_hit(self):
        text = "x" * 200 + "NEEDLE" + "y" * 200
        snip = hr.make_snippet(text, ["needle"], width=10)
        self.assertIn("NEEDLE", snip)
        self.assertTrue(snip.startswith("…") and snip.endswith("…"))

    def test_snippet_without_hit_uses_head(self):
        self.assertTrue(hr.make_snippet("abc def", ["zzz"]).startswith("abc"))


class TestTaskMatching(unittest.TestCase):
    TASK = {
        "task_id": "dlg-1", "agent": "claude", "cwd": "/repo",
        "worktree_path": "/repo-wt", "created_at": "2026-07-13T10:00:00+0900",
    }
    START = 1783904400.0  # 2026-07-13T10:00:00+0900

    def test_parse_manifest_time(self):
        self.assertEqual(hr.parse_manifest_time("2026-07-13T10:00:00+0900"), self.START)
        self.assertIsNone(hr.parse_manifest_time(None))
        self.assertIsNone(hr.parse_manifest_time("bad"))

    def test_match_by_cwd_and_time(self):
        m = hr.match_task([self.TASK], "claude", "s", "/repo", self.START + 20)
        self.assertEqual(m, "dlg-1")

    def test_match_worktree_cwd(self):
        self.assertEqual(hr.match_task([self.TASK], "claude", "s", "/repo-wt", self.START + 5), "dlg-1")

    def test_no_match_other_agent_cwd_or_time(self):
        t = [self.TASK]
        self.assertIsNone(hr.match_task(t, "codex", "s", "/repo", self.START + 5))
        self.assertIsNone(hr.match_task(t, "claude", "s", "/other", self.START + 5))
        self.assertIsNone(hr.match_task(t, "claude", "s", "/repo", self.START + 5000))
        self.assertIsNone(hr.match_task(t, "claude", "s", "/repo", self.START - 3600))
        self.assertIsNone(hr.match_task(t, "claude", "s", "/repo", None))

    def test_multiple_candidates_pick_closest_start(self):
        early = dict(self.TASK, task_id="dlg-early")
        late = dict(self.TASK, task_id="dlg-late", created_at="2026-07-13T10:05:00+0900")
        self.assertEqual(hr.match_task([early, late], "claude", "s", "/repo", self.START + 310),
                         "dlg-late")
        self.assertEqual(hr.match_task([late, early], "claude", "s", "/repo", self.START + 20),
                         "dlg-early")

    def test_prompt_footer_beats_time_heuristic(self):
        # 同 cwd・同時刻に2 task: 時刻だけでは区別できないが、契約フッターの result パスで確定する
        a = dict(self.TASK, task_id="dlg-163405-demo-impl")
        b = dict(self.TASK, task_id="dlg-163405-demo-readme")
        prompt = "fizz を実装\n--- 委任契約 ---\n完了したら /h/.config/herdr/delegate/tasks/dlg-163405-demo-impl/result.md に"
        self.assertEqual(hr.match_task([b, a], "claude", "s", "/repo", self.START + 1, prompt),
                         "dlg-163405-demo-impl")
        # 台帳に無い task_id は無視して従来の推定へ
        self.assertEqual(hr.task_from_prompt(prompt, {"dlg-other"}), None)

    def test_cap_text_keeps_tail(self):
        text = "a" * 9000 + "TAILMARK"
        out = hr.cap_text(text)
        self.assertEqual(len(out), hr.MAX_TEXT)
        self.assertTrue(out.endswith("TAILMARK"))

    def test_match_by_session_keys_when_present(self):
        by_id = dict(self.TASK, session_id="sid-9")
        by_path = dict(self.TASK, task_id="dlg-2", session_path="/x/sid-8.jsonl")
        self.assertEqual(hr.match_task([by_id], "claude", "sid-9", "/zzz", 0), "dlg-1")
        self.assertEqual(hr.match_task([by_path], "claude", "sid-8", "/zzz", 0), "dlg-2")

    def test_load_tasks_tolerates_bad_and_missing(self):
        with tempfile.TemporaryDirectory() as d:
            os.makedirs(os.path.join(d, "ok"))
            with open(os.path.join(d, "ok", "manifest.json"), "w") as f:
                json.dump({"task_id": "ok"}, f)  # 他キー欠損
            os.makedirs(os.path.join(d, "broken"))
            with open(os.path.join(d, "broken", "manifest.json"), "w") as f:
                f.write("{not json")
            os.makedirs(os.path.join(d, "empty"))
            self.assertEqual([t["task_id"] for t in hr.load_tasks(d)], ["ok"])
            self.assertEqual(hr.load_tasks(os.path.join(d, "nope")), [])


class IndexCase(unittest.TestCase):
    """一時ディレクトリに fixture transcript を置いて索引する土台。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.claude = os.path.join(self.tmp.name, "claude")
        self.codex = os.path.join(self.tmp.name, "codex")
        self.db = os.path.join(self.tmp.name, "out", "recall.db")
        os.makedirs(self.claude)
        os.makedirs(self.codex)
        self.conn = hr.connect(self.db)
        self.addCleanup(self.conn.close)

    def index(self, **kw):
        return hr.index_all(self.conn, self.claude, self.codex, **kw)

    def claude_file(self, name, objs, sub=None):
        path = os.path.join(self.claude, "-work-app", *( [sub] if sub else []), name)
        write_jsonl(path, objs)
        return path


class TestIndexing(IndexCase):
    def test_db_mode_0600(self):
        self.assertEqual(stat.S_IMODE(os.stat(self.db).st_mode), 0o600)

    def test_indexes_claude_and_codex(self):
        self.claude_file("sess-a.jsonl", [claude_line("user", "claude の話題"), claude_line("assistant", "返答")])
        uid = "019e0613-096d-7583-b85c-cb3c37374d66"
        write_jsonl(
            os.path.join(self.codex, "2026", f"rollout-2026-07-13T10-00-00-{uid}.jsonl"),
            [{"type": "session_meta", "timestamp": T0, "payload": {"id": uid, "cwd": "/work/cx"}},
             codex_msg("user", "codex question"), codex_msg("assistant", "codex answer")],
        )
        stats = self.index()
        self.assertEqual((stats["files"], stats["messages"]), (2, 4))
        rows = dict(self.conn.execute("SELECT sid, cwd FROM sessions").fetchall())
        self.assertEqual(rows, {"sess-a": "/work/app", uid: "/work/cx"})

    def test_incremental_skips_unchanged_and_appends(self):
        path = self.claude_file("s.jsonl", [claude_line("user", "first message")])
        self.assertEqual(self.index()["messages"], 1)
        self.assertEqual(self.index()["messages"], 0)
        with open(path, "a", encoding="utf-8") as f:
            f.write(jline(claude_line("user", "second message", ts="2026-07-13T02:00:00.000Z")))
        self.assertEqual(self.index()["messages"], 1)
        n, first, last = self.conn.execute("SELECT n, first_ts, last_ts FROM sessions").fetchone()
        self.assertEqual(n, 2)
        self.assertLess(first, last)

    def test_partial_last_line_waits(self):
        path = self.claude_file("s.jsonl", [claude_line("user", "complete one")])
        with open(path, "a", encoding="utf-8") as f:
            f.write('{"type":"user","timestamp":"2026-07-13T02:00:00.000Z","mess')  # 書き込み途中
        self.assertEqual(self.index()["messages"], 1)
        with open(path, "w", encoding="utf-8") as f:  # 完結させる(サイズも変わる)
            for o in (claude_line("user", "complete one"),
                      claude_line("user", "now complete", ts="2026-07-13T02:00:00.000Z")):
                f.write(jline(o))
        self.index()
        texts = [r[0] for r in self.conn.execute("SELECT text FROM msgs ORDER BY ts")]
        self.assertEqual(texts, ["complete one", "now complete"])

    def test_truncated_file_is_reindexed(self):
        path = self.claude_file(
            "s.jsonl", [claude_line("user", "long message " + "x" * 50), claude_line("user", "another one here")]
        )
        self.index()
        write_jsonl(path, [claude_line("user", "short")])
        self.index()
        self.assertEqual([r[0] for r in self.conn.execute("SELECT text FROM msgs")], ["short"])
        self.assertEqual(hr.search(self.conn, "another one"), [])  # FTS からも消えている

    def test_shrink_recomputes_session_counts(self):
        path = self.claude_file("s.jsonl", [
            claude_line("user", "long message " + "x" * 50),
            claude_line("user", "later one", ts="2026-07-13T05:00:00.000Z"),
        ])
        self.index()
        write_jsonl(path, [claude_line("user", "short")])
        self.index()
        n, first, last = self.conn.execute("SELECT n, first_ts, last_ts FROM sessions").fetchone()
        self.assertEqual(n, 1)
        self.assertEqual(first, last)

    def test_same_size_rewrite_detected_by_head(self):
        path = self.claude_file("s.jsonl", [claude_line("user", "aaaa first"), claude_line("user", "bbbb second")])
        self.index()
        write_jsonl(path, [claude_line("user", "cccc first"), claude_line("user", "dddd second")])  # 同サイズ
        self.index()
        texts = sorted(r[0] for r in self.conn.execute("SELECT text FROM msgs"))
        self.assertEqual(texts, ["cccc first", "dddd second"])
        self.assertEqual(hr.search(self.conn, "aaaa"), [])
        self.assertEqual(self.conn.execute("SELECT n FROM sessions").fetchone()[0], 2)

    def test_larger_rewrite_detected(self):
        path = self.claude_file("s.jsonl", [claude_line("user", "old content here")])
        self.index()
        write_jsonl(path, [claude_line("user", "new " + "y" * 300), claude_line("user", "new " + "z" * 300)])
        self.index()
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM msgs").fetchone()[0], 2)
        self.assertEqual(hr.search(self.conn, "old content"), [])

    def test_head_signature_helpers(self):
        path = os.path.join(self.tmp.name, "h.txt")
        with open(path, "wb") as f:
            f.write(b"abcdef")
        sig = hr.head_signature(path, 4)
        self.assertTrue(sig.startswith("4:"))
        self.assertTrue(hr.head_matches(path, sig))
        self.assertTrue(hr.head_matches(path, None))
        with open(path, "wb") as f:
            f.write(b"xbcdef")
        self.assertFalse(hr.head_matches(path, sig))

    def test_legacy_db_without_head_column_migrates(self):
        self.conn.execute("DROP TABLE files")
        self.conn.execute("CREATE TABLE files (path TEXT PRIMARY KEY, source TEXT, mtime REAL, size INTEGER, "
                          "offset INTEGER, sid TEXT)")
        self.conn.commit()
        hr.migrate(self.conn)
        cols = {r[1] for r in self.conn.execute("PRAGMA table_info(files)")}
        self.assertIn("head", cols)

    def test_malformed_lines_do_not_abort_index(self):
        path = self.claude_file("s.jsonl", [claude_line("user", "good one")])
        with open(path, "a", encoding="utf-8") as f:
            f.write('{"type":"user","timestamp":"%s","message":"plain"}\n' % T0)
            f.write('{"type":"assistant","timestamp":"%s","message":{"content":[{"type":"text","text":null}]}}\n' % T0)
            f.write('["type","user"]\n')
        stats = self.index()
        self.assertEqual((stats["messages"], stats["errors"]), (1, 0))

    def test_one_failing_file_counted_not_fatal(self):
        self.claude_file("a.jsonl", [claude_line("user", "fine one")])
        self.claude_file("b.jsonl", [claude_line("user", "fine two")])
        real = hr.read_new_messages

        def flaky(path, source, offset):
            if path.endswith("a.jsonl"):
                raise RuntimeError("boom")
            return real(path, source, offset)

        with mock.patch.object(hr, "read_new_messages", flaky):
            stats = self.index()
        self.assertEqual((stats["errors"], stats["messages"]), (1, 1))

    def test_budget_truncates_and_skips_last_refresh(self):
        self.claude_file("a.jsonl", [claude_line("user", "one")])
        stats = hr.index_all(self.conn, self.claude, self.codex, budget=-1)
        self.assertTrue(stats["truncated"])
        self.assertIsNone(self.conn.execute("SELECT v FROM meta WHERE k='last_refresh'").fetchone())
        self.index()
        self.assertIsNotNone(self.conn.execute("SELECT v FROM meta WHERE k='last_refresh'").fetchone())

    def test_concurrent_indexers_do_not_duplicate(self):
        path = self.claude_file("s.jsonl", [claude_line("user", "only once")])
        other = hr.connect(self.db)
        self.addCleanup(other.close)
        # 2つ目の接続が「未索引」と判断した後でも、ロック下の再読込で二重登録しない
        hr.index_file(self.conn, path, "claude")
        st = os.stat(path)
        other.execute("BEGIN IMMEDIATE")
        self.assertEqual(hr.index_file_locked(other, path, "claude", st), 0)
        other.rollback()
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM msgs").fetchone()[0], 1)

    def test_exclude_subagents(self):
        self.claude_file("main.jsonl", [claude_line("user", "main talk")])
        self.claude_file("agent-1.jsonl", [claude_line("user", "sub talk")], sub="uuid/subagents")
        self.assertEqual(self.index(exclude_subagents=True)["messages"], 1)
        self.assertEqual(self.index()["messages"], 1)  # 後から含めれば追加される
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM sessions WHERE subagent=1").fetchone()[0], 1)

    def test_bad_json_line_skipped(self):
        path = self.claude_file("s.jsonl", [claude_line("user", "good one")])
        with open(path, "a", encoding="utf-8") as f:
            f.write('{"type":"user" broken\n')
        self.assertEqual(self.index()["messages"], 1)

    def test_reset_db_removes_files(self):
        hr.reset_db(self.db)
        self.assertFalse(os.path.exists(self.db))

    def test_tokenizer_persisted_for_existing_db(self):
        self.conn.close()
        conn = hr.connect(self.db, tokenizer="unicode61")  # 既存 DB は保存値が優先
        self.addCleanup(conn.close)
        self.assertEqual(conn.tokenizer, "trigram")


class TestSearch(IndexCase):
    def setUp(self):
        super().setUp()
        self.claude_file("s1.jsonl", [
            claude_line("user", "herdr-delegate の弱点を直す", ts="2026-07-13T01:00:00.000Z"),
            claude_line("assistant", "削除ルールを追加した", ts="2026-07-13T01:05:00.000Z"),
            claude_line("user", "unrelated talk about kubernetes", ts="2026-07-10T01:00:00.000Z"),
        ])
        write_jsonl(os.path.join(self.claude, "-other", "s2.jsonl"),
                    [claude_line("user", "kubernetes upgrade plan", cwd="/work/infra", ts="2026-07-12T01:00:00.000Z")])
        self.index()

    def test_fts_match(self):
        rows = hr.search(self.conn, "kubernetes")
        self.assertEqual({r["sid"] for r in rows}, {"s1", "s2"})
        self.assertTrue(all("kubernetes" in r["snippet"] for r in rows))

    def test_and_semantics(self):
        rows = hr.search(self.conn, "kubernetes upgrade")
        self.assertEqual([r["sid"] for r in rows], ["s2"])

    def test_short_cjk_falls_back_to_like(self):
        rows = hr.search(self.conn, "削除")
        self.assertEqual(len(rows), 1)
        self.assertIn("削除", rows[0]["snippet"])

    def test_mixed_fts_and_like(self):
        self.assertEqual(len(hr.search(self.conn, "herdr-delegate 弱点")), 1)
        self.assertEqual(hr.search(self.conn, "herdr-delegate 無関係"), [])

    def test_project_filter(self):
        rows = hr.search(self.conn, "kubernetes", project="infra")
        self.assertEqual([r["sid"] for r in rows], ["s2"])

    def test_since_filter(self):
        since = hr.parse_user_time("2026-07-11T00:00:00Z")
        self.assertEqual([r["sid"] for r in hr.search(self.conn, "kubernetes", since=since)], ["s2"])

    def test_limit(self):
        self.assertEqual(len(hr.search(self.conn, "kubernetes", limit=1)), 1)

    def test_empty_query(self):
        self.assertEqual(hr.search(self.conn, "   "), [])

    def test_like_special_chars_literal(self):
        self.claude_file("s3.jsonl", [claude_line("user", "100% sure about a_b")])
        self.index()
        self.assertEqual(len(hr.search(self.conn, "a_b")), 1)
        self.assertEqual(len(hr.search(self.conn, "%")), 1)

    def test_unicode61_fallback_db(self):
        db = os.path.join(self.tmp.name, "u61.db")
        conn = hr.connect(db, tokenizer="unicode61")
        self.addCleanup(conn.close)
        hr.index_all(conn, self.claude, self.codex)
        self.assertEqual(conn.tokenizer, "unicode61")
        self.assertEqual(len(hr.search(conn, "kubernetes")), 2)
        self.assertEqual(len(hr.search(conn, "削除")), 1)  # CJK は LIKE


class TestSessionViews(IndexCase):
    def setUp(self):
        super().setUp()
        self.claude_file("abcdef12-0000.jsonl", [
            claude_line("user", f"msg {i}", ts=f"2026-07-13T01:{i:02d}:00.000Z") for i in range(20)
        ])
        self.claude_file("abcdef99-0000.jsonl", [claude_line("user", "other", ts="2026-07-14T01:00:00.000Z")])
        self.index()

    def test_resolve_unique_prefix(self):
        self.assertEqual(hr.resolve_session(self.conn, "abcdef12")[0], "abcdef12-0000")

    def test_resolve_ambiguous_and_missing(self):
        sid, cands = hr.resolve_session(self.conn, "abcdef")
        self.assertIsNone(sid)
        self.assertEqual(len(cands), 2)
        self.assertEqual(hr.resolve_session(self.conn, "zzz"), (None, []))

    def test_resolve_exact_wins_over_prefix(self):
        self.claude_file("abcdef12-0000-long.jsonl", [claude_line("user", "x")])
        self.index()
        self.assertEqual(hr.resolve_session(self.conn, "abcdef12-0000")[0], "abcdef12-0000")

    def test_messages_all(self):
        rows, center = hr.session_messages(self.conn, "abcdef12-0000")
        self.assertEqual((len(rows), center), (20, None))

    def test_messages_around(self):
        target = hr.parse_user_time("2026-07-13T01:10:00Z")
        rows, center = hr.session_messages(self.conn, "abcdef12-0000", around=target, context=2)
        self.assertEqual([r[2] for r in rows], ["msg 8", "msg 9", "msg 10", "msg 11", "msg 12"])
        self.assertEqual(rows[center][2], "msg 10")

    def test_messages_around_edge(self):
        rows, center = hr.session_messages(self.conn, "abcdef12-0000", around=0, context=3)
        self.assertEqual((rows[0][2], center), ("msg 0", 0))
        self.assertEqual(len(rows), 4)

    def test_recent_order_and_first_prompt(self):
        recent = hr.recent_sessions(self.conn, limit=1)
        self.assertEqual(recent[0]["sid"], "abcdef99-0000")
        self.assertEqual(recent[0]["first_prompt"], "other")


class TestCli(IndexCase):
    def run_cli(self, *argv):
        env = {
            "DB_PATH": self.db, "CLAUDE_DIR": self.claude, "CODEX_DIR": self.codex,
            "TASKS_DIR": os.path.join(self.tmp.name, "tasks"),
        }
        out, err = io.StringIO(), io.StringIO()
        with mock.patch.multiple(hr, **env), contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = hr.main(list(argv))
        return code, out.getvalue(), err.getvalue()

    def test_index_then_search_json_with_task(self):
        self.claude_file("s1.jsonl", [claude_line("user", "migrate the database", cwd="/work/app")])
        tdir = os.path.join(self.tmp.name, "tasks", "dlg-x")
        os.makedirs(tdir)
        with open(os.path.join(tdir, "manifest.json"), "w") as f:
            json.dump({"task_id": "dlg-x", "agent": "claude", "cwd": "/work/app",
                       "created_at": "2026-07-13T09:59:50+0900"}, f)
        self.assertEqual(self.run_cli("index")[0], 0)
        code, out, _ = self.run_cli("--no-refresh", "search", "database", "--json")
        data = json.loads(out)
        self.assertEqual((code, len(data)), (0, 1))
        self.assertEqual(data[0]["task_id"], "dlg-x")
        self.assertEqual(data[0]["sid"], "s1")

    def test_search_text_and_no_hit(self):
        self.claude_file("s1.jsonl", [claude_line("user", "migrate the database")])
        self.run_cli("index")
        code, out, _ = self.run_cli("search", "database")
        self.assertIn("s1", out)
        self.assertIn("migrate", out)
        code, out, _ = self.run_cli("search", "nonexistentword")
        self.assertEqual(code, 1)
        self.assertIn("該当なし", out)

    def test_search_no_hit_json_is_empty_list_exit_1(self):
        self.claude_file("s1.jsonl", [claude_line("user", "migrate the database")])
        self.run_cli("index")
        code, out, _ = self.run_cli("--no-refresh", "search", "nonexistentword", "--json")
        self.assertEqual((code, json.loads(out)), (1, []))

    def test_refresh_failure_falls_back_to_stale_db(self):
        self.claude_file("s1.jsonl", [claude_line("user", "migrate the database")])
        self.run_cli("index")
        self.conn.execute("DELETE FROM meta WHERE k='last_refresh'")
        self.conn.commit()
        with mock.patch.object(hr, "index_all", side_effect=hr.sqlite3.OperationalError("database is locked")):
            code, out, err = self.run_cli("search", "database")
        self.assertEqual(code, 0)
        self.assertIn("migrate", out)
        self.assertIn("警告", err)

    def test_refresh_skipped_within_interval(self):
        self.claude_file("s1.jsonl", [claude_line("user", "migrate the database")])
        self.run_cli("index")  # last_refresh 記録
        with mock.patch.object(hr, "index_all", side_effect=AssertionError("呼ばれないはず")):
            self.assertEqual(self.run_cli("search", "database")[0], 0)

    def test_search_without_db_exits_1(self):
        self.conn.close()
        hr.reset_db(self.db)
        code, _, err = self.run_cli("search", "x")
        self.assertEqual(code, 1)
        self.assertIn("index", err)

    def test_bad_since_exits_2(self):
        self.assertEqual(self.run_cli("search", "x", "--since", "soon")[0], 2)

    def test_show_ambiguous_and_missing(self):
        self.claude_file("abc1.jsonl", [claude_line("user", "one")])
        self.claude_file("abc2.jsonl", [claude_line("user", "two")])
        self.run_cli("index")
        self.assertEqual(self.run_cli("show", "abc")[0], 2)
        self.assertEqual(self.run_cli("show", "zzz")[0], 1)
        code, out, _ = self.run_cli("show", "abc1")
        self.assertEqual(code, 0)
        self.assertIn("one", out)

    def test_recent_lists_sessions(self):
        self.claude_file("abc1.jsonl", [claude_line("user", "hello recent")])
        self.run_cli("index")
        code, out, _ = self.run_cli("recent")
        self.assertEqual(code, 0)
        self.assertIn("hello recent", out)

    def test_full_index_rebuilds(self):
        self.claude_file("s1.jsonl", [claude_line("user", "only message")])
        self.run_cli("index")
        code, out, _ = self.run_cli("index", "--full")
        self.assertIn("messages=1", out)


if __name__ == "__main__":
    unittest.main()
