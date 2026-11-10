import importlib.machinery
import io
import importlib.util
import json
import os
import re
import shutil
import signal
import subprocess
import tempfile
import time
import types
import unittest
from unittest import mock

SCRIPT = os.path.join(os.path.dirname(__file__), "..", "herdr-delegate")
loader = importlib.machinery.SourceFileLoader("herdr_delegate", SCRIPT)
spec = importlib.util.spec_from_loader("herdr_delegate", loader)
hd = importlib.util.module_from_spec(spec)
loader.exec_module(hd)


def wait_args(*task_ids, **kw):
    """cmd_wait 用の引数 namespace (stall 監視は既定で無効)."""
    base = dict(task_ids=list(task_ids), group=None, all=False, any=False, timeout=None,
                stall_secs=0, stall_grace=hd.DEFAULT_STALL_GRACE)
    base.update(kw)
    return types.SimpleNamespace(**base)


def stdout_calls(m_print):
    """mock 化した print の呼び出しのうち stdout 宛て (file= なし) のもの."""
    return [c for c in m_print.call_args_list if not c.kwargs]


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

    def _status_of(self, text):
        with tempfile.TemporaryDirectory() as d:
            with open(os.path.join(d, "result.md"), "w") as f:
                f.write(text)
            return hd.read_result_status(d)

    def test_no_status_line_is_not_done(self):
        self.assertIsNone(self._status_of("summary: 書きかけ\n"))

    def test_empty_file_is_not_done(self):
        self.assertIsNone(self._status_of(""))

    def test_unknown_status_value_is_none(self):
        self.assertIsNone(self._status_of("status: in_progress\n"))

    def test_status_is_case_insensitive(self):
        self.assertEqual(self._status_of("Status: BLOCKED\n"), "blocked")


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

    def test_idle_after_first_nudge_waits_interval(self):
        self.assertEqual(hd.decide("idle", None, 1, hd.NUDGE_INTERVAL_SECS), "CONTINUE")

    def test_idle_after_first_nudge_nudges_again_past_interval(self):
        self.assertEqual(hd.decide("idle", None, 1, hd.NUDGE_INTERVAL_SECS + 1), "NUDGE")

    def test_idle_after_all_nudges_waits_grace(self):
        self.assertEqual(hd.decide("idle", None, hd.MAX_NUDGES, 30), "CONTINUE")

    def test_idle_after_all_nudges_stalls_past_grace(self):
        self.assertEqual(hd.decide("idle", None, hd.MAX_NUDGES, 61), "STALLED")

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


class TestTrustCursorOnNo(unittest.TestCase):
    def test_cursor_on_yes(self):
        self.assertFalse(hd.trust_cursor_on_no("❯ 1. Yes, I trust this folder\n  2. No, exit"))

    def test_cursor_on_no(self):
        # 実測 (2026-11-10, 新しい claude): 初期カーソルが "No, exit"
        self.assertTrue(hd.trust_cursor_on_no(" ❯ No, exit\n   Yes, I trust this folder"))

    def test_cursor_on_yes_new_layout(self):
        self.assertFalse(hd.trust_cursor_on_no("   No, exit\n ❯ Yes, I trust this folder"))


class TestMaybeTrustDialog(unittest.TestCase):
    def run_dialog(self, screens):
        sent = []
        with mock.patch.object(hd, "pane_visible", side_effect=screens), \
             mock.patch.object(hd.time, "sleep"), \
             mock.patch.object(hd.subprocess, "run", side_effect=lambda cmd, **k: sent.append(cmd[-1])):
            result = hd.maybe_trust_dialog("p1")
        return result, sent

    def test_no_dialog_sends_nothing(self):
        self.assertEqual(self.run_dialog(["normal output"]), (False, []))

    def test_cursor_on_yes_sends_return_only(self):
        screens = ["Is this a project you created or one you trust?\n ❯ Yes, I trust this folder"]
        self.assertEqual(self.run_dialog(screens), (True, ["Return"]))

    def test_retries_down_until_cursor_moves(self):
        no = "Is this a project you created or one you trust?\n ❯ No, exit\n   Yes, I trust this folder"
        yes = "Is this a project you created or one you trust?\n   No, exit\n ❯ Yes, I trust this folder"
        self.assertEqual(self.run_dialog([no, no, yes]), (True, ["Down", "Down", "Return"]))


class TestExtractReadText(unittest.TestCase):
    def test_unwraps_json_envelope(self):
        out = '{"id":"x","result":{"read":{"text":"a\\nb"}}}'
        self.assertEqual(hd.extract_read_text(out), "a\nb")

    def test_plain_text_passthrough(self):
        self.assertEqual(hd.extract_read_text("plain\ntext"), "plain\ntext")

    def test_other_json_passthrough(self):
        self.assertEqual(hd.extract_read_text('{"a":1}'), '{"a":1}')
        self.assertEqual(hd.extract_read_text(""), "")


class TestIsTrustPrompt(unittest.TestCase):
    def test_matches_observed_claude_dialog_text(self):
        # 実測 (2026-07-10, claude CLI, Task 3 スモークテスト) の実際の文言
        text = (
            "Quick safety check: Is this a project you created or one you "
            "trust?\n\n❯ 1. Yes, I trust this folder\n  2. No, exit"
        )
        self.assertTrue(hd.is_trust_prompt(text))

    def test_matches_alternate_wording(self):
        text = "Do you trust the files in this folder?\n1. Yes  2. No"
        self.assertTrue(hd.is_trust_prompt(text))

    def test_case_insensitive(self):
        text = "DO YOU TRUST THE FILES IN THIS FOLDER?"
        self.assertTrue(hd.is_trust_prompt(text))

    def test_matches_observed_codex_dialog_text(self):
        # 実測 (2026-07-10, codex CLI, Task 6 スモークテスト) の実際の文言
        text = (
            "> You are in /private/tmp/hd-smoke-codex\n\n"
            "  Do you trust the contents of this directory? Working with "
            "untrusted contents comes\n  with higher risk of prompt "
            "injection. Trusting the directory allows project-local\n"
            "  config, hooks, and exec policies to load.\n\n"
            "› 1. Yes, continue\n  2. No, quit\n\n"
            "  Press enter to continue"
        )
        self.assertTrue(hd.is_trust_prompt(text))

    def test_no_match_on_normal_output(self):
        text = "some normal claude output about the task\nworking...\n"
        self.assertFalse(hd.is_trust_prompt(text))

    def test_empty_or_none_is_false(self):
        self.assertFalse(hd.is_trust_prompt(""))
        self.assertFalse(hd.is_trust_prompt(None))


class TestBuildWrapper(unittest.TestCase):
    def test_model_shell_injection_protection(self):
        """Guard against shell injection via --model parameter.

        This test verifies that a malicious model string containing shell
        metacharacters is properly quoted so it cannot execute as shell commands.
        """
        import shlex

        # Malicious model string with shell metacharacters
        malicious_model = "haiku'; touch /tmp/should-not-exist-via-test; echo '"

        with tempfile.TemporaryDirectory() as task_dir:
            with tempfile.TemporaryDirectory() as cwd_dir:
                # Create required prompt.md file
                with open(os.path.join(task_dir, "prompt.md"), "w") as f:
                    f.write("test prompt\n")

                wrapper_path = hd.build_wrapper(
                    task_dir, "claude", malicious_model, False, cwd_dir
                )
                self.addCleanup(lambda: os.path.exists(wrapper_path) and os.unlink(wrapper_path))

                # Read the generated wrapper script
                with open(wrapper_path) as f:
                    wrapper_content = f.read()

                # Verify the model value was quoted using shlex.quote() before insertion
                # This ensures the malicious string appears as a single literal token
                expected_model_part = "--model " + shlex.quote(malicious_model)
                self.assertIn(
                    expected_model_part,
                    wrapper_content,
                    "Model parameter must be properly quoted to prevent shell injection"
                )

                # Find the exec line and verify shell parsing would not execute injection
                exec_line = None
                for line in wrapper_content.split('\n'):
                    if 'exec claude' in line:
                        exec_line = line
                        break

                self.assertIsNotNone(exec_line, "No 'exec claude' line found in wrapper")

                # Parse with shlex to verify malicious components are not separate tokens
                try:
                    tokens = shlex.split(exec_line)
                    # If injection were possible, 'touch' and the path would be separate tokens
                    self.assertNotIn('touch', tokens,
                                   "Shell injection detected: 'touch' is a separate token")
                    self.assertNotIn('/tmp/should-not-exist-via-test', tokens,
                                   "Shell injection detected: path is a separate token")
                except ValueError:
                    # Parsing might fail if script contains unexpanded shell variables like $prompt
                    # In that case, just verify the expected quoting is present
                    pass

        # Belt-and-suspenders: verify the file was never created by injection
        self.assertFalse(
            os.path.exists('/tmp/should-not-exist-via-test'),
            "Malicious file should not have been created"
        )
        # Cleanup in case it somehow was created
        self.addCleanup(
            lambda: os.path.exists('/tmp/should-not-exist-via-test') and os.unlink('/tmp/should-not-exist-via-test')
        )

    def test_normal_model_parameter_works(self):
        """Verify that normal model names still work correctly after the fix."""
        with tempfile.TemporaryDirectory() as task_dir:
            with tempfile.TemporaryDirectory() as cwd_dir:
                # Create required prompt.md file
                with open(os.path.join(task_dir, "prompt.md"), "w") as f:
                    f.write("test prompt\n")

                wrapper_path = hd.build_wrapper(
                    task_dir, "claude", "sonnet", False, cwd_dir
                )
                self.addCleanup(lambda: os.path.exists(wrapper_path) and os.unlink(wrapper_path))

                # Read the generated wrapper script
                with open(wrapper_path) as f:
                    wrapper_content = f.read()

                # Verify the normal model name is present and properly quoted
                self.assertIn('--model sonnet', wrapper_content,
                            "Normal model parameter should be in wrapper")
                self.assertIn('exec claude', wrapper_content,
                            "exec claude command should be in wrapper")


class TestWaitTrustWorkaroundOneShot(unittest.TestCase):
    """Regression coverage for cmd_wait's one-shot trust-dialog workaround.

    is_trust_prompt() itself is a pure function already covered by
    TestIsTrustPrompt above. What is NOT covered anywhere else is the
    control-flow property inside cmd_wait: once the trust dialog is
    detected and a Return keystroke is sent, `trust_workaround_tried`
    must prevent the block from ever running again for the rest of the
    task's polling loop (no re-check of pane_visible, no repeated
    Return), and decide()'s normal idle-tracking bookkeeping must resume
    untouched on subsequent iterations. All herdr I/O (subprocess calls,
    time.sleep) is mocked so this exercises only the control flow.
    """

    TRUST_DIALOG_TEXT = (
        "Quick safety check: Is this a project you created or one you "
        "trust?\n\n❯ 1. Yes, I trust this folder\n  2. No, exit"
    )
    NORMAL_TEXT = "some normal claude output about the task\nworking...\n"

    def setUp(self):
        self.task_dir = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.task_dir, ignore_errors=True)
        hd.save_manifest(self.task_dir, {"task_id": "t", "agent": "claude",
                                         "pane_id": "w1:p1", "cwd": self.task_dir})

    @staticmethod
    def _args(task_id):
        return wait_args(task_id)

    @staticmethod
    def _return_sendkeys_calls(run_mock):
        return [
            c for c in run_mock.call_args_list
            if c.args and isinstance(c.args[0], list)
            and c.args[0][:3] == ["herdr", "pane", "send-keys"]
            and c.args[0][-1] == "Return"
        ]

    def test_trust_dialog_sends_return_exactly_once_across_polls(self):
        # iter1: dialog visible -> one-shot workaround fires and `continue`s
        #        (decide()/idle bookkeeping must NOT run this iteration).
        # iter2-3: workaround already tried -> normal polling resumes,
        #        agent still "working".
        # iter4: agent goes idle and result.md says "done" -> DONE exit.
        pane_sequence = [
            {"agent_status": "working"},
            {"agent_status": "working"},
            {"agent_status": "working"},
            {"agent_status": "idle"},
            {"agent_status": "idle"},  # unused safety buffer
        ]
        with mock.patch.object(hd, "task_dir_of", return_value=self.task_dir), \
             mock.patch.object(hd, "save_recovery"), \
             mock.patch.object(hd, "get_pane", side_effect=pane_sequence), \
             mock.patch.object(hd, "pane_visible",
                                return_value=self.TRUST_DIALOG_TEXT) as m_pane_visible, \
             mock.patch.object(hd, "read_result_status", return_value="done"), \
             mock.patch.object(hd, "nudge", return_value=True) as m_nudge, \
             mock.patch.object(hd.time, "sleep"), \
             mock.patch.object(hd.subprocess, "run") as m_run:
            with self.assertRaises(SystemExit) as cm:
                hd.cmd_wait(self._args("dlg-test-trust-oneshot"))

        self.assertEqual(cm.exception.code, 0)
        # One-shot guard: even though the mocked pane text keeps matching
        # the trust-dialog markers forever, the `not trust_workaround_tried`
        # guard must stop the block from re-checking after the first hit.
        # If a future refactor moves/drops that guard or the `continue`,
        # pane_visible would be re-invoked every loop and this fails.
        self.assertEqual(m_pane_visible.call_count, 1)
        return_calls = self._return_sendkeys_calls(m_run)
        self.assertEqual(len(return_calls), 1, return_calls)
        self.assertEqual(
            return_calls[0].args[0],
            ["herdr", "pane", "send-keys", "w1:p1", "Return"],
        )
        # The trust-dialog iteration must short-circuit before decide()'s
        # nudge/idle-tracking logic runs, so nudge() is never reached here.
        m_nudge.assert_not_called()

    def test_no_trust_dialog_never_sends_return(self):
        pane_sequence = [
            {"agent_status": "working"},
            {"agent_status": "working"},
            {"agent_status": "idle"},
        ]
        with mock.patch.object(hd, "task_dir_of", return_value=self.task_dir), \
             mock.patch.object(hd, "save_recovery"), \
             mock.patch.object(hd, "get_pane", side_effect=pane_sequence), \
             mock.patch.object(hd, "pane_visible",
                                return_value=self.NORMAL_TEXT) as m_pane_visible, \
             mock.patch.object(hd, "read_result_status", return_value="done"), \
             mock.patch.object(hd, "nudge", return_value=True) as m_nudge, \
             mock.patch.object(hd.time, "sleep"), \
             mock.patch.object(hd.subprocess, "run") as m_run:
            with self.assertRaises(SystemExit) as cm:
                hd.cmd_wait(self._args("dlg-test-no-trust-dialog"))

        self.assertEqual(cm.exception.code, 0)
        self.assertGreaterEqual(m_pane_visible.call_count, 1)
        return_calls = self._return_sendkeys_calls(m_run)
        self.assertEqual(len(return_calls), 0, return_calls)
        m_nudge.assert_not_called()


class TestSendLine(unittest.TestCase):
    """Regression coverage for send_line's echo-verification window.

    Bug (Task 4 live smoke test, 2026-07-10): against a fast-replying agent
    (e.g. haiku), the echoed input text can be pushed out of a narrow
    5-line tail by the time the agent's reply arrives, before send_line's
    verification read runs. That false negative triggered a retry, which
    sent the same text to the pane a second time (confirmed live: the
    message appeared twice with two separate agent replies).

    The fix widens the verification read to pane_tail(pane_id, lines=40)
    (source=recent-unwrapped), matching the pattern already used
    elsewhere in the file, instead of a bespoke `--lines 5` read.

    Reviewer finding (2026-07-10, post-merge review of 3404f58): the
    original version of this test's mock returned the SAME full
    `wide_stdout` no matter what `--lines`/`--source` the code under test
    actually passed, and separately re-derived a "narrow window" via
    Python string slicing as an unrelated sanity check. That meant the
    mock could not distinguish the fixed code from the old buggy code
    (`["herdr", "agent", "read", pane_id, "--lines", "5"]`, no --source):
    both would have been handed the full wide_stdout and both would have
    "passed". The strengthened mock below performs a REAL truncation
    driven by the REAL `--lines` value in the command it receives, so the
    test's pass/fail outcome is actually coupled to send_line's behavior.
    """

    PANE_ID = "w1:p1"
    TEXT = "ありがとう。他に作業は不要。長めのメッセージでテストする。"

    # Fixture with the echo line near the top, followed by enough
    # simulated "agent reply" lines to push it out of a naive last-5-lines
    # view while still being well within a last-40-lines view.
    REPLY_LINES = [f"agent reply line {i}" for i in range(10)]
    FIXTURE_LINES = [TEXT] + REPLY_LINES

    @staticmethod
    def _completed(cmd, stdout=""):
        import subprocess as _sp
        return _sp.CompletedProcess(cmd, 0, stdout=stdout, stderr="")

    @classmethod
    def _tail(cls, n):
        """The last n lines of FIXTURE_LINES, joined like real pane output."""
        return "\n".join(cls.FIXTURE_LINES[-n:]) + "\n"

    def _fake_run_factory(self):
        """Builds a subprocess.run side_effect that branches on the herdr
        subcommand, so `send`/`send-keys` calls always succeed. Crucially,
        `agent read` calls parse the REAL `--lines` value out of the
        command argv and return only that many trailing lines of
        FIXTURE_LINES -- a real truncation driven by the real call args,
        not a pre-computed constant. This makes the mock behave like an
        actual capped pane read: request `--lines 40` and you get the
        echo back; request the old narrow `--lines 5` and you don't."""
        calls = {"read": 0}

        def fake_run(cmd, **kwargs):
            if cmd[:3] == ["herdr", "agent", "send"]:
                return self._completed(cmd)
            if cmd[:3] == ["herdr", "pane", "send-keys"]:
                return self._completed(cmd)
            if cmd[:3] == ["herdr", "agent", "read"]:
                calls["read"] += 1
                if "--lines" not in cmd:
                    raise AssertionError(f"agent read without --lines: {cmd}")
                n = int(cmd[cmd.index("--lines") + 1])
                return self._completed(cmd, self._tail(n))
            raise AssertionError(f"unexpected command: {cmd}")

        return fake_run

    def _constant_fake_run_factory(self, read_stdout):
        """Simpler mock for tests that don't care about window sizing:
        every `agent read` call returns the same fixed stdout regardless
        of args. Used by the plain success/exhausted-retries tests below,
        which are not about the echo-window regression."""

        def fake_run(cmd, **kwargs):
            if cmd[:3] == ["herdr", "agent", "send"]:
                return self._completed(cmd)
            if cmd[:3] == ["herdr", "pane", "send-keys"]:
                return self._completed(cmd)
            if cmd[:3] == ["herdr", "agent", "read"]:
                return self._completed(cmd, read_stdout)
            raise AssertionError(f"unexpected command: {cmd}")

        return fake_run

    def test_fast_reply_pushes_echo_out_of_narrow_window_but_wide_window_catches_it(self):
        # Sanity-check the fixture is representative: the last-5-lines
        # view must NOT contain the echo, while the last-40-lines view
        # must contain it. (Documents intent; the real proof is below,
        # where fake_run truncates based on send_line's actual argv.)
        self.assertNotIn(self.TEXT[:20], self._tail(5))
        self.assertIn(self.TEXT[:20], self._tail(40))

        fake_run = self._fake_run_factory()
        with mock.patch.object(hd.subprocess, "run", side_effect=fake_run) as m_run, \
             mock.patch.object(hd.time, "sleep"):
            result = hd.send_line(self.PANE_ID, self.TEXT)

        # This assertion is the behavioral proof: it only passes if
        # send_line's real verification read requested enough lines
        # (--lines 40, via pane_tail) for the mock's real truncation to
        # still contain the echo. If send_line regresses to the old
        # narrow `--lines 5` read, the mock truncates to _tail(5), the
        # echo is absent, and this goes False (see mutation-test note in
        # the class docstring / task report for a live before/after run
        # against a temporarily-reverted herdr-delegate).
        self.assertTrue(result)
        send_calls = [c for c in m_run.call_args_list
                      if c.args[0][:3] == ["herdr", "agent", "send"]]
        self.assertEqual(len(send_calls), 1, "must not retry / double-send")

        # Secondary, literal check: the verification read must use the
        # same wide pattern as pane_tail(): recent-unwrapped source,
        # lines=40 (not the old narrow "--lines 5" with default source).
        # Kept for readability/debuggability, but the assertTrue(result)
        # above (backed by real mock truncation) is the primary proof.
        read_calls = [c for c in m_run.call_args_list
                      if c.args[0][:3] == ["herdr", "agent", "read"]]
        self.assertEqual(len(read_calls), 1)
        read_cmd = read_calls[0].args[0]
        self.assertIn("--source", read_cmd)
        self.assertEqual(read_cmd[read_cmd.index("--source") + 1], "recent-unwrapped")
        self.assertIn("--lines", read_cmd)
        self.assertEqual(read_cmd[read_cmd.index("--lines") + 1], "40")

    def test_old_narrow_read_would_have_failed_verification(self):
        """Direct proof that the mock itself is faithful: replaying the
        OLD buggy read command (`--lines 5`, no --source) against the
        same fixture/truncation logic used above yields stdout that does
        NOT contain the echo. This is what send_line would have seen pre-
        fix, and it is what causes
        test_fast_reply_pushes_echo_out_of_narrow_window_but_wide_window_catches_it
        to fail if send_line's real implementation ever regresses back to
        that narrow read (verified live via mutation testing; see task
        report)."""
        fake_run = self._fake_run_factory()
        old_buggy_read_cmd = ["herdr", "agent", "read", self.PANE_ID, "--lines", "5"]
        old_result = fake_run(old_buggy_read_cmd)
        self.assertNotIn(self.TEXT[:20], old_result.stdout)

        new_fixed_read_cmd = ["herdr", "agent", "read", self.PANE_ID,
                               "--source", "recent-unwrapped", "--lines", "40"]
        new_result = fake_run(new_fixed_read_cmd)
        self.assertIn(self.TEXT[:20], new_result.stdout)

    def test_send_succeeds_cleanly_on_first_attempt(self):
        fake_run = self._constant_fake_run_factory(f"{self.TEXT}\n")
        with mock.patch.object(hd.subprocess, "run", side_effect=fake_run) as m_run, \
             mock.patch.object(hd.time, "sleep"):
            result = hd.send_line(self.PANE_ID, self.TEXT)

        self.assertTrue(result)
        send_calls = [c for c in m_run.call_args_list
                      if c.args[0][:3] == ["herdr", "agent", "send"]]
        self.assertEqual(len(send_calls), 1)

    def test_send_fails_after_retries_exhausted(self):
        # Echo never shows up in any read, no matter how many attempts.
        fake_run = self._constant_fake_run_factory("unrelated pane output\n")
        with mock.patch.object(hd.subprocess, "run", side_effect=fake_run) as m_run, \
             mock.patch.object(hd.time, "sleep"):
            result = hd.send_line(self.PANE_ID, self.TEXT, retries=1)

        self.assertFalse(result)
        send_calls = [c for c in m_run.call_args_list
                      if c.args[0][:3] == ["herdr", "agent", "send"]]
        # retries=1 means the initial attempt plus one retry = 2 sends.
        self.assertEqual(len(send_calls), 2)


class TestCmdKillWorktreeOrdering(unittest.TestCase):
    """Regression coverage for cmd_kill's pane-close/worktree-remove ordering.

    Bug (Task 5, live smoke test, 2026-07-10): the original cmd_kill closed
    a task's pane unconditionally before calling `herdr worktree remove`.
    For worktree-mode tasks, `worktree remove` tears down the entire
    single-pane workspace itself -- closing the pane first made the
    workspace vanish, so the subsequent `worktree remove` failed with
    workspace_not_found (reproduced live once; required manual
    `git worktree remove --force` / `git branch -D` cleanup of an
    orphaned worktree).

    The fix: skip the redundant `pane close` when doing --rm-worktree
    removal of a worktree-mode task (worktree remove handles pane teardown
    itself); for non-worktree tasks, or when --rm-worktree is not passed,
    still just close the pane normally and leave any worktree/branch alone
    (cleanup of the worktree/branch is explicit/opt-in only).

    All herdr I/O (hcli, get_pane) is mocked; manifest.json is written via
    the real save_manifest/load_manifest helpers into a tempdir, matching
    the pattern used elsewhere in this file.
    """

    def setUp(self):
        p = mock.patch.object(hd, "save_recovery")
        self.m_recovery = p.start()
        self.addCleanup(p.stop)

    @staticmethod
    def _args(task_id, rm_worktree):
        return types.SimpleNamespace(task_id=task_id, rm_worktree=rm_worktree,
                                     force_discard=False)

    def _make_task_dir(self, manifest_extra=None):
        d = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, d, ignore_errors=True)
        manifest = {
            "task_id": "dlg-test-kill",
            "agent": "claude",
            "pane_id": "w1:p1",
            "workspace_id": "ws1",
        }
        if manifest_extra:
            manifest.update(manifest_extra)
        hd.save_manifest(d, manifest)
        return d

    @staticmethod
    def _hcli_calls(m_hcli, *prefix):
        return [c for c in m_hcli.call_args_list if c.args[:len(prefix)] == prefix]

    def test_worktree_rm_with_pane_alive_removes_worktree_not_pane(self):
        """(rm_worktree=True, worktree_path set, pane exists): this is the
        core fix. worktree remove IS called; pane close is NOT (no
        redundant pane-close before worktree removal)."""
        d = self._make_task_dir({"worktree_path": "/tmp/wt/dlg-test-kill"})
        with mock.patch.object(hd, "task_dir_of", return_value=d), \
             mock.patch.object(hd, "get_pane", return_value={"agent_status": "idle"}), \
             mock.patch.object(hd, "hcli") as m_hcli:
            hd.cmd_kill(self._args("dlg-test-kill", rm_worktree=True))

        worktree_calls = self._hcli_calls(m_hcli, "worktree", "remove")
        pane_calls = self._hcli_calls(m_hcli, "pane", "close")
        self.assertEqual(len(worktree_calls), 1, m_hcli.call_args_list)
        self.assertEqual(len(pane_calls), 0, m_hcli.call_args_list)
        self.assertIsNotNone(hd.load_manifest(d).get("killed_at"))

    def test_worktree_rm_with_pane_already_gone_skips_both_calls(self):
        """(rm_worktree=True, worktree_path set, pane already gone /
        get_pane() returns None): documents CURRENT behavior. The
        `if pane:` guard inside the rm_worktree branch short-circuits, so
        NEITHER `worktree remove` NOR `pane close` is called. This means
        the worktree/branch is left un-cleaned-up in this edge case --
        see the task report for a note on whether this is worth a
        follow-up (this test intentionally does not fix it, only pins
        current behavior)."""
        d = self._make_task_dir({"worktree_path": "/tmp/wt/dlg-test-kill"})
        with mock.patch.object(hd, "task_dir_of", return_value=d), \
             mock.patch.object(hd, "get_pane", return_value=None), \
             mock.patch.object(hd, "hcli") as m_hcli:
            hd.cmd_kill(self._args("dlg-test-kill", rm_worktree=True))

        self.assertEqual(m_hcli.call_count, 0, m_hcli.call_args_list)

    def test_non_worktree_task_closes_pane_no_worktree_call(self):
        """(rm_worktree=False, no worktree_path, pane exists): plain kill
        of a non-worktree task. pane close IS called; worktree remove is
        NOT (there is no worktree to remove)."""
        d = self._make_task_dir()  # no worktree_path key at all
        with mock.patch.object(hd, "task_dir_of", return_value=d), \
             mock.patch.object(hd, "get_pane", return_value={"agent_status": "idle"}), \
             mock.patch.object(hd, "hcli") as m_hcli:
            hd.cmd_kill(self._args("dlg-test-kill", rm_worktree=False))

        pane_calls = self._hcli_calls(m_hcli, "pane", "close")
        worktree_calls = self._hcli_calls(m_hcli, "worktree", "remove")
        self.assertEqual(len(pane_calls), 1, m_hcli.call_args_list)
        self.assertEqual(pane_calls[0].args, ("pane", "close", "w1:p1"))
        self.assertEqual(len(worktree_calls), 0, m_hcli.call_args_list)

    def test_worktree_task_without_rm_flag_closes_pane_leaves_worktree(self):
        """(rm_worktree=False, worktree_path set, pane exists): plain kill
        of a worktree-mode task without --rm-worktree. pane close IS
        called (same as any plain kill), but worktree/branch cleanup is
        explicit/opt-in -- worktree remove is NOT called even though the
        task has a worktree_path."""
        d = self._make_task_dir({"worktree_path": "/tmp/wt/dlg-test-kill"})
        with mock.patch.object(hd, "task_dir_of", return_value=d), \
             mock.patch.object(hd, "get_pane", return_value={"agent_status": "idle"}), \
             mock.patch.object(hd, "hcli") as m_hcli:
            hd.cmd_kill(self._args("dlg-test-kill", rm_worktree=False))

        pane_calls = self._hcli_calls(m_hcli, "pane", "close")
        worktree_calls = self._hcli_calls(m_hcli, "worktree", "remove")
        self.assertEqual(len(pane_calls), 1, m_hcli.call_args_list)
        self.assertEqual(len(worktree_calls), 0, m_hcli.call_args_list)


class TempTaskCase(unittest.TestCase):
    """台帳 (tempdir の TASKS_DIR) を使うテストの共通土台."""

    def setUp(self):
        self.root = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.root, ignore_errors=True)
        self.tasks = os.path.join(self.root, "tasks")
        os.makedirs(self.tasks)
        p = mock.patch.object(hd, "TASKS_DIR", self.tasks)
        p.start()
        self.addCleanup(p.stop)
        p = mock.patch.object(hd, "save_recovery")
        self.m_recovery = p.start()
        self.addCleanup(p.stop)
        # 実 herdr を叩かない (pane 表示更新・枠切れ判定用の pane 末尾読み取り)
        p = mock.patch.object(hd, "report_pane_state")
        self.m_report = p.start()
        self.addCleanup(p.stop)
        p = mock.patch.object(hd, "pane_tail", return_value="")
        p.start()
        self.addCleanup(p.stop)

    def make_task(self, tid="dlg-1", **extra):
        d = os.path.join(self.tasks, tid)
        os.makedirs(d)
        m = {"task_id": tid, "agent": "claude", "pane_id": "w1:p1",
             "cwd": d, "workspace_id": "w1"}
        m.update(extra)
        hd.save_manifest(d, m)
        return d

    @staticmethod
    def write_result(d, text):
        with open(os.path.join(d, "result.md"), "w") as f:
            f.write(text)


class TestExitCodesAndDecideTimeout(unittest.TestCase):
    def test_exit_code_table(self):
        self.assertEqual(
            (hd.EXIT_DONE, hd.EXIT_FAILED, hd.EXIT_BLOCKED, hd.EXIT_STALLED, hd.EXIT_GONE,
             hd.EXIT_QUIET, hd.EXIT_TIMEOUT, hd.EXIT_LIMITED, hd.EXIT_CONFIG),
            (0, 1, 2, 3, 4, 5, 6, 7, 8))

    def test_timeout_replaces_continue_and_nudge(self):
        self.assertEqual(hd.decide("working", None, False, 0, timed_out=True), "TIMEOUT")
        self.assertEqual(hd.decide("idle", None, False, 0, timed_out=True), "TIMEOUT")
        self.assertEqual(hd.decide("idle", None, 1, 10, timed_out=True), "TIMEOUT")

    def test_timeout_does_not_mask_real_outcomes(self):
        self.assertEqual(hd.decide("idle", "done", False, 0, timed_out=True), "DONE")
        self.assertEqual(hd.decide("idle", "failed", False, 0, timed_out=True), "FAILED_RESULT")
        self.assertEqual(hd.decide("blocked", None, False, 0, timed_out=True), "BLOCKED")
        self.assertEqual(hd.decide(None, None, False, 0, timed_out=True), "PANE_GONE")
        self.assertEqual(hd.decide("idle", None, 2, 999, timed_out=True), "STALLED")

    def test_outcomes_map_every_terminal_action_to_state_and_code(self):
        self.assertEqual(hd.OUTCOMES["TIMEOUT"], ("timeout", 6))
        self.assertEqual(hd.OUTCOMES["STALLED"], ("stalled", 3))
        self.assertEqual(hd.OUTCOMES["PANE_GONE"], ("gone", 4))
        self.assertNotIn("CONTINUE", hd.OUTCOMES)
        self.assertNotIn("NUDGE", hd.OUTCOMES)


class TestApplyState(unittest.TestCase):
    def test_running_to_done_records_event_and_fields(self):
        m = {}
        self.assertTrue(hd.apply_state(m, "done", "T1", detail="x"))
        self.assertEqual(m["state"], "done")
        self.assertEqual(m["finished_at"], "T1")
        self.assertEqual(m["exit_reason"], "done")
        self.assertEqual(m["events"], [{"ts": "T1", "kind": "state", "state": "done", "detail": "x"}])

    def test_terminal_state_is_immutable(self):
        m = {"state": "done"}
        self.assertFalse(hd.apply_state(m, "failed", "T2"))
        self.assertEqual(m["state"], "done")
        self.assertNotIn("events", m)

    def test_same_state_adds_no_event(self):
        m = {"state": "blocked"}
        self.assertFalse(hd.apply_state(m, "blocked", "T2"))
        self.assertNotIn("events", m)

    def test_blocked_is_resumable_and_resume_clears_outcome(self):
        m = {"state": "blocked", "finished_at": "T1", "exit_reason": "blocked", "delivered_at": "T1"}
        self.assertTrue(hd.apply_state(m, "running", "T2"))
        self.assertEqual((m["finished_at"], m["exit_reason"], m["delivered_at"]), (None, None, None))
        self.assertTrue(hd.apply_state(m, "done", "T3"))

    def test_non_terminal_wait_outcomes_can_still_complete(self):
        for st in ("stalled", "quiet", "timeout", "limited"):
            m = {"state": st}
            self.assertTrue(hd.apply_state(m, "done", "T"), st)

    def test_force_reopens_done_but_never_killed_or_gone(self):
        m = {"state": "done"}
        self.assertTrue(hd.apply_state(m, "running", "T", force=True))
        for st in ("killed", "gone"):
            m = {"state": st}
            self.assertFalse(hd.apply_state(m, "running", "T", force=True), st)

    def test_exit_reason_override(self):
        m = {}
        hd.apply_state(m, "protocol_violation", "T", exit_reason="nudge exhausted")
        self.assertEqual(m["exit_reason"], "nudge exhausted")

    def test_effective_state_for_legacy_manifest(self):
        self.assertEqual(hd.effective_state({}), "running")
        self.assertEqual(hd.effective_state({"killed_at": "T"}), "killed")
        self.assertEqual(hd.effective_state({"state": "done", "killed_at": "T"}), "done")

    def test_apply_state_on_legacy_manifest_without_new_keys(self):
        m = {"task_id": "old", "killed_at": None}
        self.assertTrue(hd.apply_state(m, "done", "T"))


class TestReconcileState(unittest.TestCase):
    def test_terminal_untouched(self):
        self.assertIsNone(hd.reconcile_state("done", None, None))

    def test_pane_gone_without_result_is_gone(self):
        self.assertEqual(hd.reconcile_state("running", None, None), "gone")
        self.assertEqual(hd.reconcile_state("blocked", "blocked", None), "gone")

    def test_result_wins_when_not_working(self):
        self.assertEqual(hd.reconcile_state("running", "done", None), "done")
        self.assertEqual(hd.reconcile_state("running", "failed", {"agent_status": "idle"}), "failed")

    def test_working_pane_with_result_is_left_alone(self):
        self.assertIsNone(hd.reconcile_state("running", "done", {"agent_status": "working"}))

    def test_alive_without_result_is_left_alone(self):
        self.assertIsNone(hd.reconcile_state("running", None, {"agent_status": "idle"}))

    def test_gated_done_is_not_adopted_but_failed_is(self):
        idle = {"agent_status": "idle"}
        self.assertIsNone(hd.reconcile_state("running", "done", idle, needs_gate=True))
        self.assertEqual(hd.reconcile_state("running", "failed", idle, needs_gate=True), "failed")
        self.assertEqual(hd.reconcile_state("running", "done", None, needs_gate=True), "gone")

    def test_needs_verification(self):
        self.assertFalse(hd.needs_verification({}))
        for key in ("gates", "schema", "contract"):
            self.assertTrue(hd.needs_verification({key: ["x"] if key == "gates" else True}))


class TestArchiveResult(TempTaskCase):
    def test_none_when_no_result(self):
        d = self.make_task()
        self.assertIsNone(hd.archive_result(d))

    def test_archives_with_increasing_suffix(self):
        d = self.make_task()
        self.write_result(d, "status: done\n")
        self.assertEqual(hd.archive_result(d), os.path.join(d, "result.1.md"))
        self.assertFalse(os.path.exists(os.path.join(d, "result.md")))
        self.write_result(d, "status: failed\n")
        self.assertEqual(hd.archive_result(d), os.path.join(d, "result.2.md"))
        self.assertIsNone(hd.read_result_status(d))


class TestFormatReinject(unittest.TestCase):
    ROW = {"task_id": "dlg-1", "agent": "claude", "pane_id": "w1:p1", "state": "running",
           "cwd": "/repo", "branch": "delegate/x", "result_path": "/t/result.md"}

    def test_empty_gives_empty_string(self):
        self.assertEqual(hd.format_reinject([]), "")

    def test_header_and_row(self):
        out = hd.format_reinject([self.ROW]).splitlines()
        self.assertIn("REFERENCE ONLY", out[0])
        self.assertEqual(out[1], "- dlg-1 claude pane=w1:p1 state=running cwd=/repo "
                                 "branch=delegate/x result.md=/t/result.md")

    def test_row_cap_and_line_cap(self):
        rows = [dict(self.ROW, task_id=f"dlg-{i}", cwd="/" + "x" * 300) for i in range(4)]
        out = hd.format_reinject(rows, max_rows=2, max_line=80).splitlines()
        self.assertEqual(len(out), 4)  # header + 2 rows + 超過件数行
        self.assertTrue(all(len(ln) <= 80 for ln in out[1:3]))
        self.assertTrue(out[1].endswith("…"))
        self.assertIn("2 件", out[3])

    def test_branch_omitted_when_none(self):
        self.assertNotIn("branch=", hd.format_reinject([dict(self.ROW, branch=None)]))


class TestIdempotency(TempTaskCase):
    def test_first_claim_new_then_same_prompt_existing(self):
        sha = hd.prompt_sha("do x")
        verdict, path = hd.claim_idem("K", sha, "claude")
        self.assertEqual(verdict, "new")
        self.assertEqual(hd.claim_idem("K", sha, "claude")[0], "in_progress")
        hd.finish_idem(path, "dlg-1")
        verdict, rec = hd.claim_idem("K", sha, "claude")
        self.assertEqual((verdict, rec["task_id"]), ("existing", "dlg-1"))

    def test_different_prompt_conflicts(self):
        _, path = hd.claim_idem("K", hd.prompt_sha("a"), "claude")
        hd.finish_idem(path, "dlg-1")
        self.assertEqual(hd.claim_idem("K", hd.prompt_sha("b"), "claude")[0], "conflict")

    def test_empty_or_corrupt_record_is_in_progress(self):
        path = hd.idem_path("K")
        os.makedirs(hd.idem_dir(), exist_ok=True)
        for content in ("", "{broken"):
            with open(path, "w") as f:
                f.write(content)
            self.assertEqual(hd.claim_idem("K", "s", "claude")[0], "in_progress")

    def test_claim_leaves_no_tmp_files(self):
        hd.claim_idem("K", "s", "claude")
        hd.claim_idem("K", "s", "claude")
        self.assertEqual([n for n in os.listdir(hd.idem_dir()) if "tmp" in n], [])

    def test_release_allows_reclaim(self):
        _, path = hd.claim_idem("K", hd.prompt_sha("a"), "claude")
        hd.release_idem(path)
        self.assertEqual(hd.claim_idem("K", hd.prompt_sha("a"), "claude")[0], "new")

    def test_gc_removes_only_old_records(self):
        _, old = hd.claim_idem("old", "s", "claude")
        _, fresh = hd.claim_idem("fresh", "s", "claude")
        past = time.time() - hd.IDEM_TTL_SECS - 10
        os.utime(old, (past, past))
        hd.gc_idem()
        self.assertFalse(os.path.exists(old))
        self.assertTrue(os.path.exists(fresh))

    def test_idem_verdict_pure(self):
        self.assertEqual(hd.idem_verdict({"prompt_sha256": "a", "task_id": "t"}, "a"), "existing")
        self.assertEqual(hd.idem_verdict({"prompt_sha256": "a", "task_id": None}, "a"), "in_progress")
        self.assertEqual(hd.idem_verdict({"prompt_sha256": "a", "task_id": "t"}, "b"), "conflict")


class TestPreflight(unittest.TestCase):
    def test_ok(self):
        self.assertEqual(hd.preflight_problems(
            "claude", "/x", which=lambda e: "/bin/" + e, isdir=lambda p: True,
            herdr_ok=lambda: True), [])

    def test_reports_each_problem(self):
        probs = hd.preflight_problems("codex", "/nope", which=lambda e: None, isdir=lambda p: False)
        self.assertEqual(len(probs), 3)
        self.assertTrue(any("codex" in p for p in probs))
        self.assertTrue(any("/nope" in p for p in probs))


class TestGit(unittest.TestCase):
    def test_count_porcelain(self):
        self.assertEqual(hd.count_porcelain(" M a\n?? b\n\n"), 2)
        self.assertEqual(hd.count_porcelain(""), 0)
        self.assertEqual(hd.count_porcelain(None), 0)

    def test_non_repo_is_none(self):
        with tempfile.TemporaryDirectory() as d:
            self.assertIsNone(hd.run_git(d, "rev-parse", "HEAD"))
            self.assertEqual(hd.git_snapshot(d), {"branch": None, "head": None, "dirty": None})

    def test_missing_dir_is_none(self):
        self.assertIsNone(hd.run_git("/nonexistent-dir-xyz", "rev-parse", "HEAD"))

    def test_repo_snapshot_and_optional_locks_env(self):
        with tempfile.TemporaryDirectory() as d:
            for args in (["init", "-q"], ["-c", "user.name=t", "-c", "user.email=t@t",
                                          "commit", "-q", "--allow-empty", "-m", "x"]):
                subprocess.run(["git", "-C", d, *args], check=True, capture_output=True)
            open(os.path.join(d, "f"), "w").close()
            snap = hd.git_snapshot(d)
            self.assertEqual(len(snap["head"]), 40)
            self.assertEqual(snap["dirty"], 1)
            self.assertEqual(hd.run_git(d, "rev-parse", "HEAD"), snap["head"])
        with mock.patch.object(hd.subprocess, "run") as m_run:
            m_run.return_value = types.SimpleNamespace(returncode=0, stdout="x\n")
            hd.run_git("/r", "status")
        kw = m_run.call_args.kwargs
        self.assertEqual(kw["env"]["GIT_OPTIONAL_LOCKS"], "0")
        self.assertEqual(kw["stdin"], subprocess.DEVNULL)
        self.assertEqual(kw["timeout"], 5)

    def test_timeout_is_none(self):
        with mock.patch.object(hd.subprocess, "run",
                               side_effect=subprocess.TimeoutExpired("git", 5)):
            self.assertIsNone(hd.run_git("/r", "status"))



class TestManifestIO(TempTaskCase):
    def test_atomic_write_leaves_no_tmp_and_replaces(self):
        d = self.make_task()
        hd.save_manifest(d, {"a": 1})
        hd.save_manifest(d, {"a": 2})
        self.assertEqual(sorted(n for n in os.listdir(d) if n.startswith("manifest")),
                         ["manifest.json"])
        self.assertEqual(hd.load_manifest(d), {"a": 2})

    def test_set_state_persists_and_respects_immutability(self):
        d = self.make_task()
        self.assertTrue(hd.set_state(d, "done"))
        self.assertFalse(hd.set_state(d, "failed"))
        m = hd.load_manifest(d)
        self.assertEqual(m["state"], "done")
        self.assertEqual(len(m["events"]), 1)

    def test_transition_saves_recovery_only_when_changed_and_alive(self):
        d = self.make_task()
        hd.transition(d, "blocked", pane_alive=False)
        self.m_recovery.assert_not_called()
        hd.transition(d, "stalled", pane_alive=True)
        self.m_recovery.assert_called_once_with(d, "stalled")
        hd.transition(d, "stalled", pane_alive=True)
        self.assertEqual(self.m_recovery.call_count, 1)

    def test_mark_delivered_is_idempotent(self):
        d = self.make_task()
        hd.mark_delivered(d)
        first = hd.load_manifest(d)["delivered_at"]
        self.assertTrue(first)
        hd.mark_delivered(d)
        self.assertEqual(hd.load_manifest(d)["delivered_at"], first)

    def test_concurrent_updates_do_not_lose_events(self):
        import threading
        d = self.make_task()
        threads = [threading.Thread(target=hd.append_event, args=(d, "send", str(i)))
                   for i in range(8)]
        [t.start() for t in threads]
        [t.join() for t in threads]
        self.assertEqual(len(hd.load_manifest(d)["events"]), 8)


class TestHerdrIO(unittest.TestCase):
    def test_call_timeout_becomes_unavailable(self):
        sock = mock.MagicMock()
        sock.__enter__.return_value = sock
        sock.recv.side_effect = TimeoutError("timed out")
        with mock.patch.object(hd.socket, "socket", return_value=sock):
            with self.assertRaises(hd.HerdrUnavailable) as cm:
                hd.call("pane.get", {})
        self.assertIn("pane.get", str(cm.exception))
        sock.settimeout.assert_called_once_with(hd.CALL_TIMEOUT_SECS)

    def test_call_rejects_non_json(self):
        sock = mock.MagicMock()
        sock.__enter__.return_value = sock
        sock.recv.side_effect = [b"oops\n"]
        with mock.patch.object(hd.socket, "socket", return_value=sock):
            with self.assertRaises(RuntimeError):
                hd.call("pane.get", {})

    def test_hcli_non_json_output_is_clear_error(self):
        proc = types.SimpleNamespace(returncode=0, stdout="not json", stderr="")
        with mock.patch.object(hd.subprocess, "run", return_value=proc):
            with self.assertRaisesRegex(RuntimeError, "JSON 以外"):
                hd.hcli("pane", "list")

    def test_hcli_empty_output_is_empty_dict(self):
        proc = types.SimpleNamespace(returncode=0, stdout="  ", stderr="")
        with mock.patch.object(hd.subprocess, "run", return_value=proc):
            self.assertEqual(hd.hcli("x"), {})

    def test_hcli_missing_binary_is_unavailable(self):
        with mock.patch.object(hd.subprocess, "run", side_effect=FileNotFoundError("herdr")):
            with self.assertRaises(hd.HerdrUnavailable):
                hd.hcli("x")

    def test_get_pane_none_on_api_error_but_raises_on_unavailable(self):
        with mock.patch.object(hd, "call", side_effect=RuntimeError("pane_not_found")):
            self.assertIsNone(hd.get_pane("w1:p1"))
        with mock.patch.object(hd, "call", side_effect=hd.HerdrUnavailable("down")):
            with self.assertRaises(hd.HerdrUnavailable):
                hd.get_pane("w1:p1")


class TestObserveAndWaitStep(unittest.TestCase):
    def test_observe_retries_then_exits_with_config_code(self):
        with mock.patch.object(hd, "get_pane", side_effect=hd.HerdrUnavailable("down")) as m, \
             mock.patch.object(hd.time, "sleep"):
            with self.assertRaises(SystemExit) as cm:
                hd.observe_pane("w1:p1")
        self.assertEqual(cm.exception.code, hd.EXIT_CONFIG)
        self.assertEqual(m.call_count, hd.OBSERVE_RETRIES)

    def test_observe_recovers_after_transient_failure(self):
        with mock.patch.object(hd, "get_pane",
                               side_effect=[hd.HerdrUnavailable("x"), {"agent_status": "idle"}]), \
             mock.patch.object(hd.time, "sleep"):
            self.assertEqual(hd.observe_pane("w1:p1"), {"agent_status": "idle"})

    def test_wait_step_uses_herdr_blocking_wait_while_working(self):
        with mock.patch.object(hd.subprocess, "run",
                               return_value=types.SimpleNamespace(returncode=0)) as m_run, \
             mock.patch.object(hd.time, "sleep") as m_sleep:
            hd.wait_step("w1:p1", "working", None)
        cmd = m_run.call_args.args[0]
        self.assertEqual(cmd[:4], ["herdr", "wait", "agent-status", "w1:p1"])
        self.assertIn("idle", cmd)
        m_sleep.assert_not_called()

    def test_wait_step_falls_back_to_sleep_on_instant_failure(self):
        with mock.patch.object(hd.subprocess, "run",
                               return_value=types.SimpleNamespace(returncode=1)), \
             mock.patch.object(hd.time, "sleep") as m_sleep:
            hd.wait_step("w1:p1", "working", None)
        m_sleep.assert_called_once_with(hd.POLL_SECS)

    def test_wait_step_sleeps_when_not_working(self):
        with mock.patch.object(hd.subprocess, "run") as m_run, \
             mock.patch.object(hd.time, "sleep") as m_sleep:
            hd.wait_step("w1:p1", "idle", None)
        m_run.assert_not_called()
        m_sleep.assert_called_once_with(hd.POLL_SECS)


class TestCmdWaitRecordsOutcome(TempTaskCase):
    def run_wait(self, pane, result_text=None, timeout=None):
        d = self.make_task()
        if result_text:
            self.write_result(d, result_text)
        args = wait_args("dlg-1", timeout=timeout)
        with mock.patch.object(hd, "get_pane", return_value=pane), \
             mock.patch.object(hd, "pane_visible", return_value=""), \
             mock.patch.object(hd, "pane_tail", return_value="tail"), \
             mock.patch.object(hd, "nudge", return_value=True), \
             mock.patch.object(hd, "wait_step"), \
             mock.patch.object(hd.subprocess, "run"), \
             mock.patch("builtins.print"):
            with self.assertRaises(SystemExit) as cm:
                hd.cmd_wait(args)
        return cm.exception.code, hd.load_manifest(d), d

    def test_done(self):
        code, m, _ = self.run_wait({"agent_status": "idle"}, "status: done\n")
        self.assertEqual((code, m["state"], m["exit_reason"]), (0, "done", "done"))
        self.assertTrue(m["finished_at"])
        self.m_recovery.assert_called_once()

    def test_failed(self):
        code, m, _ = self.run_wait({"agent_status": "idle"}, "status: failed\n")
        self.assertEqual((code, m["state"]), (1, "failed"))

    def test_blocked_then_resumes_on_next_wait(self):
        code, m, d = self.run_wait({"agent_status": "blocked"})
        self.assertEqual((code, m["state"]), (2, "blocked"))
        self.write_result(d, "status: done\n")
        with mock.patch.object(hd, "get_pane", return_value={"agent_status": "idle"}), \
             mock.patch.object(hd, "pane_visible", return_value=""), \
             mock.patch("builtins.print"), self.assertRaises(SystemExit) as cm:
            hd.cmd_wait(wait_args("dlg-1"))
        self.assertEqual(cm.exception.code, 0)
        kinds = [e["state"] for e in hd.load_manifest(d)["events"]]
        self.assertEqual(kinds, ["blocked", "running", "done"])

    def test_pane_gone_exit_4_no_recovery_snapshot(self):
        code, m, _ = self.run_wait(None)
        self.assertEqual((code, m["state"]), (4, "gone"))
        self.m_recovery.assert_not_called()

    def test_timeout_exit_6(self):
        code, m, _ = self.run_wait({"agent_status": "working"}, timeout=-1000)
        self.assertEqual((code, m["state"]), (6, "timeout"))

    def test_done_wait_on_killed_task_keeps_killed(self):
        d = self.make_task(state="killed")
        with mock.patch.object(hd, "get_pane", return_value=None), \
             mock.patch("builtins.print"), self.assertRaises(SystemExit) as cm:
            hd.cmd_wait(wait_args("dlg-1"))
        self.assertEqual(cm.exception.code, 4)
        self.assertEqual(hd.load_manifest(d)["state"], "killed")


class TestCmdSend(TempTaskCase):
    def test_archives_result_records_event_and_resumes(self):
        d = self.make_task(state="done")
        self.write_result(d, "status: done\n")
        with mock.patch.object(hd, "get_pane", return_value={"agent_status": "idle"}), \
             mock.patch.object(hd, "send_line", return_value=True):
            hd.cmd_send(types.SimpleNamespace(task_id="dlg-1", text="追加で X も"))
        m = hd.load_manifest(d)
        self.assertEqual(m["state"], "running")
        self.assertIn({"kind": "send", "detail": "追加で X も"},
                      [{k: e[k] for k in ("kind", "detail")} for e in m["events"]])
        self.assertTrue(os.path.exists(os.path.join(d, "result.1.md")))
        self.assertFalse(os.path.exists(os.path.join(d, "result.md")))

    def test_resets_round_state(self):
        d = self.make_task(state="protocol_violation", schema_corrected_at=1.0,
                           schema_valid=False, schema_errors=["x"], stall_notified_at="t")
        for name in ("protocol_violation", "stop_nudges"):
            with open(os.path.join(d, name), "w") as f:
                f.write("2")
        with mock.patch.object(hd, "get_pane", return_value={"agent_status": "idle"}), \
             mock.patch.object(hd, "send_line", return_value=True):
            hd.cmd_send(types.SimpleNamespace(task_id="dlg-1", text="続き"))
        for name in ("protocol_violation", "stop_nudges"):
            self.assertFalse(os.path.exists(os.path.join(d, name)))
        m = hd.load_manifest(d)
        for k in hd.ROUND_MANIFEST_KEYS:
            self.assertNotIn(k, m)
        self.assertEqual(m["state"], "running")

    def test_failed_send_keeps_round_state(self):
        d = self.make_task()
        with open(os.path.join(d, "stop_nudges"), "w") as f:
            f.write("2")
        with mock.patch.object(hd, "get_pane", return_value={"agent_status": "idle"}), \
             mock.patch.object(hd, "send_line", return_value=False):
            with self.assertRaises(SystemExit):
                hd.cmd_send(types.SimpleNamespace(task_id="dlg-1", text="x"))
        self.assertTrue(os.path.exists(os.path.join(d, "stop_nudges")))

    def test_failed_send_restores_result(self):
        d = self.make_task()
        self.write_result(d, "status: done\n")
        with mock.patch.object(hd, "get_pane", return_value={"agent_status": "idle"}), \
             mock.patch.object(hd, "send_line", return_value=False):
            with self.assertRaises(SystemExit):
                hd.cmd_send(types.SimpleNamespace(task_id="dlg-1", text="x"))
        self.assertEqual(hd.read_result_status(d), "done")
        self.assertNotIn("events", hd.load_manifest(d))

    def test_send_exception_restores_result(self):
        d = self.make_task()
        self.write_result(d, "status: done\n")
        err = subprocess.CalledProcessError(1, "herdr")
        with mock.patch.object(hd, "get_pane", return_value={"agent_status": "idle"}), \
             mock.patch.object(hd, "send_line", side_effect=err):
            with self.assertRaises(subprocess.CalledProcessError):
                hd.cmd_send(types.SimpleNamespace(task_id="dlg-1", text="x"))
        self.assertEqual(hd.read_result_status(d), "done")

    def test_manual_send_resets_gate_failures(self):
        d = self.make_task()
        hd.update_manifest(d, lambda m: m.update(gate_failures=3))
        with mock.patch.object(hd, "get_pane", return_value={"agent_status": "idle"}), \
             mock.patch.object(hd, "send_line", return_value=True):
            hd.cmd_send(types.SimpleNamespace(task_id="dlg-1", text="x"))
        self.assertNotIn("gate_failures", hd.load_manifest(d))

    def test_gate_feedback_resend_keeps_gate_failures(self):
        d = self.make_task()
        hd.update_manifest(d, lambda m: m.update(gate_failures=2))
        with mock.patch.object(hd, "send_line", return_value=True):
            self.assertTrue(hd.resend(d, "w1:p1", "x"))
        self.assertEqual(hd.load_manifest(d)["gate_failures"], 2)

    def test_pane_gone_exits_4(self):
        self.make_task()
        with mock.patch.object(hd, "get_pane", return_value=None):
            with self.assertRaises(SystemExit) as cm:
                hd.cmd_send(types.SimpleNamespace(task_id="dlg-1", text="x"))
        self.assertEqual(cm.exception.code, hd.EXIT_GONE)


class TestCmdCollect(TempTaskCase):
    def test_collect_records_state_and_delivered(self):
        d = self.make_task()
        self.write_result(d, "status: failed\nsummary: x\n")
        with mock.patch("builtins.print"):
            hd.cmd_collect(types.SimpleNamespace(task_id="dlg-1"))
        m = hd.load_manifest(d)
        self.assertEqual(m["state"], "failed")
        self.assertTrue(m["delivered_at"])

    def test_collect_unfinished_result_is_not_delivered(self):
        d = self.make_task()
        self.write_result(d, "書きかけ (status 行なし)\n")
        with mock.patch("builtins.print"):
            hd.cmd_collect(types.SimpleNamespace(task_id="dlg-1"))
        m = hd.load_manifest(d)
        self.assertFalse(m.get("delivered_at"))
        self.assertNotEqual(hd.effective_state(m), "done")

    def test_collect_without_result_exits_3_and_logs_event(self):
        d = self.make_task()
        with mock.patch.object(hd, "pane_tail", return_value="t"), \
             mock.patch("builtins.print"), self.assertRaises(SystemExit) as cm:
            hd.cmd_collect(types.SimpleNamespace(task_id="dlg-1"))
        self.assertEqual(cm.exception.code, 3)
        m = hd.load_manifest(d)
        self.assertEqual(m["events"][0]["kind"], "collect")
        self.assertNotIn("state", m)


class TestCmdKillRecords(TempTaskCase):
    def test_kill_sets_state_and_delivered_and_snapshots_before_close(self):
        d = self.make_task()
        order = []
        self.m_recovery.side_effect = lambda *a: order.append("recovery")
        with mock.patch.object(hd, "get_pane", return_value={"agent_status": "idle"}), \
             mock.patch.object(hd, "hcli", side_effect=lambda *a: order.append("close")):
            hd.cmd_kill(types.SimpleNamespace(task_id="dlg-1", rm_worktree=False,
                                                       force_discard=False))
        self.assertEqual(order, ["recovery", "close"])
        m = hd.load_manifest(d)
        self.assertEqual((m["state"], m["exit_reason"]), ("killed", "killed"))
        self.assertTrue(m["killed_at"] and m["delivered_at"])

    def test_kill_does_not_overwrite_done(self):
        d = self.make_task(state="done")
        with mock.patch.object(hd, "get_pane", return_value=None):
            hd.cmd_kill(types.SimpleNamespace(task_id="dlg-1", rm_worktree=False,
                                                       force_discard=False))
        m = hd.load_manifest(d)
        self.assertEqual(m["state"], "done")
        self.assertTrue(m["killed_at"])


class TestReconcileCommand(TempTaskCase):
    def reconcile(self, panes, **flags):
        args = types.SimpleNamespace(mark_delivered=flags.get("mark_delivered", False),
                                     json=True)
        with mock.patch.object(hd, "hcli", return_value={"panes": panes}), \
             mock.patch("builtins.print") as m_print:
            hd.cmd_reconcile(args)
        return json.loads(m_print.call_args.args[0])

    def test_marks_gone_and_lists_undelivered(self):
        self.make_task("dlg-gone", pane_id="w9:p9", delivered_at=None)
        done = self.make_task("dlg-done", pane_id="w1:p1", delivered_at=None)
        self.write_result(done, "status: done\n")
        self.make_task("dlg-live", pane_id="w2:p2", delivered_at=None)
        out = self.reconcile([{"pane_id": "w2:p2", "agent_status": "working"}])
        self.assertEqual({r["task_id"]: r["state"] for r in out["reconciled"]},
                         {"dlg-gone": "gone", "dlg-done": "done"})
        self.assertEqual({r["task_id"] for r in out["undelivered"]}, {"dlg-gone", "dlg-done"})

    def test_mark_delivered_clears_list_next_time(self):
        self.make_task("dlg-gone", pane_id="w9:p9", delivered_at=None)
        self.reconcile([], mark_delivered=True)
        self.assertEqual(self.reconcile([])["undelivered"], [])

    def test_legacy_manifest_without_state_is_handled(self):
        d = self.make_task("dlg-old", pane_id="w9:p9", killed_at="T")
        out = self.reconcile([])
        self.assertEqual(out["reconciled"], [])  # killed 扱いで終端
        self.assertEqual(out["undelivered"], [])  # delivered_at キーの無い旧 task は未回収扱いにしない
        self.assertNotIn("state", hd.load_manifest(d))

    def test_legacy_gone_task_not_reinjected(self):
        self.make_task("dlg-old", pane_id="w9:p9")
        out = self.reconcile([])
        self.assertEqual(out["undelivered"], [])

    def test_provisional_manifest_without_pane_is_skipped(self):
        d = self.make_task("dlg-spawning", pane_id=None, state="running")
        out = self.reconcile([])
        self.assertEqual(out["reconciled"], [])
        self.assertEqual(hd.load_manifest(d)["state"], "running")

    def test_gated_done_result_is_not_adopted(self):
        d = self.make_task("dlg-gated", pane_id="w1:p1", gates=["false"], delivered_at=None)
        self.write_result(d, "status: done\n")
        out = self.reconcile([{"pane_id": "w1:p1", "agent_status": "idle"}])
        self.assertEqual(out["reconciled"], [])
        self.assertEqual(hd.effective_state(hd.load_manifest(d)), "running")


class TestCmdStatus(TempTaskCase):
    def run_status(self, **kw):
        args = types.SimpleNamespace(task_id=None, json=kw.get("json", False),
                                     active=kw.get("active", False),
                                     format=kw.get("format", "text"))
        with mock.patch.object(hd, "get_pane", return_value={"agent_status": "idle"}), \
             mock.patch("builtins.print") as m_print:
            hd.cmd_status(args)
        return "\n".join(str(c.args[0]) for c in m_print.call_args_list)

    def test_active_reinject_excludes_terminal(self):
        self.make_task("dlg-a", state="running")
        self.make_task("dlg-b", state="done")
        self.make_task("dlg-c", state="blocked")
        self.make_task("dlg-old")  # 旧 manifest: state なし = running 扱い
        out = self.run_status(active=True, format="reinject")
        self.assertIn("REFERENCE ONLY", out)
        self.assertIn("dlg-a", out)
        self.assertIn("dlg-c", out)
        self.assertIn("dlg-old", out)
        self.assertNotIn("dlg-b", out)

    def test_reinject_prints_nothing_when_no_active(self):
        self.make_task("dlg-b", state="done")
        self.assertEqual(self.run_status(active=True, format="reinject"), "")

    def test_json_includes_state(self):
        self.make_task("dlg-a", state="stalled")
        rows = json.loads(self.run_status(json=True))
        self.assertEqual(rows[0]["state"], "stalled")


class TestCmdSpawnIdempotency(TempTaskCase):
    def args(self, **kw):
        base = dict(agent="claude", model=None, worktree=None, cwd=self.root, label=None,
                    safe=False, idempotency_key="K", prompt="do x", prompt_file=None,
                    gate_timeout=300, gate_retries=3)
        base.update(kw)
        return types.SimpleNamespace(**base)

    def test_same_key_same_prompt_returns_existing_without_new_pane(self):
        d = self.make_task("dlg-1", state="running")
        _, path = hd.claim_idem("K", hd.prompt_sha("do x"), "claude")
        hd.finish_idem(path, "dlg-1")
        with mock.patch.object(hd, "preflight_problems", return_value=[]), \
             mock.patch.object(hd, "create_task") as m_create, \
             mock.patch("builtins.print") as m_print:
            hd.cmd_spawn(self.args())
        m_create.assert_not_called()
        out = json.loads(m_print.call_args.args[0])
        self.assertEqual((out["task_id"], out["existing"], out["task_dir"]), ("dlg-1", True, d))

    def test_different_prompt_is_config_error(self):
        _, path = hd.claim_idem("K", hd.prompt_sha("do x"), "claude")
        hd.finish_idem(path, "dlg-1")
        with mock.patch.object(hd, "preflight_problems", return_value=[]):
            with self.assertRaises(hd.ConfigError):
                hd.cmd_spawn(self.args(prompt="do y"))

    def test_failed_spawn_releases_key(self):
        with mock.patch.object(hd, "preflight_problems", return_value=[]), \
             mock.patch.object(hd, "create_task", side_effect=RuntimeError("boom")):
            with self.assertRaises(RuntimeError):
                hd.cmd_spawn(self.args())
        self.assertFalse(os.path.exists(hd.idem_path("K")))

    def test_success_records_task_id(self):
        with mock.patch.object(hd, "preflight_problems", return_value=[]), \
             mock.patch.object(hd, "create_task", return_value="dlg-9"):
            hd.cmd_spawn(self.args())
        with open(hd.idem_path("K")) as f:
            self.assertEqual(json.load(f)["task_id"], "dlg-9")

    def test_preflight_failure_is_config_error_before_idem_claim(self):
        with mock.patch.object(hd, "preflight_problems", return_value=["herdr が PATH に無い"]):
            with self.assertRaises(hd.ConfigError):
                hd.cmd_spawn(self.args())
        self.assertFalse(os.path.exists(hd.idem_path("K")))


# ---------- A2: stall 検出 ----------

def write_file(path, text="", mtime=None):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as f:
        f.write(text)
    if mtime is not None:
        os.utime(path, (mtime, mtime))
    return path


class TestDecideStallAndViolation(unittest.TestCase):
    def working(self, quiet, notified=False, stall=600, grace=300, **kw):
        return hd.decide("working", None, 0, 0, quiet_secs=quiet, stall_secs=stall,
                         stall_grace=grace, already_notified=notified, **kw)

    def test_below_threshold_continues(self):
        self.assertEqual(self.working(599), "CONTINUE")

    def test_disabled_when_zero(self):
        self.assertEqual(self.working(99999, stall=0), "CONTINUE")

    def test_first_crossing_notifies(self):
        self.assertEqual(self.working(600), "STALL_NOTIFY")

    def test_notified_waits_for_grace(self):
        self.assertEqual(self.working(899, notified=True), "CONTINUE")

    def test_notified_past_grace_is_quiet(self):
        self.assertEqual(self.working(900, notified=True), "QUIET")

    def test_timeout_beats_stall_actions(self):
        self.assertEqual(self.working(600, timed_out=True), "TIMEOUT")
        self.assertEqual(self.working(900, notified=True, timed_out=True), "TIMEOUT")

    def test_blocked_result_beats_working_stall(self):
        self.assertEqual(hd.decide("working", "blocked", 0, 0, quiet_secs=9999, stall_secs=1),
                         "BLOCKED")

    def test_protocol_violation_when_idle_without_result(self):
        self.assertEqual(hd.decide("idle", None, 0, 0, protocol_violation=True),
                         "PROTOCOL_VIOLATION")

    def test_result_beats_protocol_violation(self):
        self.assertEqual(hd.decide("idle", "done", 0, 0, protocol_violation=True), "DONE")

    def test_outcome_table_for_new_actions(self):
        self.assertEqual(hd.OUTCOMES["QUIET"], ("quiet", 5))
        self.assertEqual(hd.OUTCOMES["PROTOCOL_VIOLATION"], ("protocol_violation", 1))
        self.assertNotIn("STALL_NOTIFY", hd.OUTCOMES)
        self.assertEqual(hd.STATE_EXIT["done"], 0)
        self.assertEqual(hd.STATE_EXIT["killed"], hd.EXIT_GONE)


class TestTranscriptDetection(unittest.TestCase):
    def setUp(self):
        self.home = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.home, ignore_errors=True)

    def test_claude_slug_replaces_every_non_alnum(self):
        # 実測: ~/.claude/projects/-Users-jane-doe-projects ("." も "-")
        self.assertEqual(hd.claude_project_slug("/Users/jane.doe/projects"),
                         "-Users-jane-doe-projects")
        self.assertEqual(hd.claude_project_slug("/tmp/claude-502/-Users/a_b"),
                         "-tmp-claude-502--Users-a-b")

    def test_iso_to_epoch(self):
        self.assertEqual(hd.iso_to_epoch("1970-01-02T00:00:00+0000"), 86400)
        self.assertEqual(hd.iso_to_epoch(None), 0.0)
        self.assertEqual(hd.iso_to_epoch("garbage"), 0.0)

    def test_newest_file_respects_since(self):
        a = write_file(os.path.join(self.home, "a"), "x", mtime=1000)
        b = write_file(os.path.join(self.home, "b"), "yy", mtime=2000)
        self.assertEqual(hd.newest_file([a, b])[0], b)
        self.assertEqual(hd.newest_file([a, b])[2], 2)
        self.assertIsNone(hd.newest_file([a, b], since=3000))
        self.assertIsNone(hd.newest_file(["/nonexistent"]))

    def test_claude_transcript_picks_newest_after_since(self):
        slug = hd.claude_project_slug(self.home)
        old = write_file(os.path.join(self.home, ".claude/projects", slug, "old.jsonl"), mtime=1000)
        new = write_file(os.path.join(self.home, ".claude/projects", slug, "new.jsonl"), mtime=5000)
        self.assertEqual(hd.claude_transcript(self.home, 0, self.home)[0], new)
        self.assertIsNone(hd.claude_transcript(self.home, 6000, self.home))
        self.assertNotEqual(old, new)

    def test_codex_day_dirs_span_spawn_to_today(self):
        t0 = time.mktime((2026, 7, 10, 12, 0, 0, 0, 0, -1))
        dirs = hd.codex_day_dirs(t0, t0 + 2 * 86400, "/h")
        self.assertEqual([d.split("sessions/")[1] for d in dirs],
                         ["2026/07/10", "2026/07/11", "2026/07/12"])

    def test_codex_transcript_matches_cwd_and_since(self):
        now = time.time()
        day = time.strftime("%Y/%m/%d", time.localtime(now))
        base = os.path.join(self.home, ".codex/sessions", day)
        meta = lambda cwd: json.dumps({"payload": {"cwd": cwd}}) + "\n"
        mine = write_file(os.path.join(base, "rollout-a.jsonl"), meta("/work/x"), mtime=now - 10)
        write_file(os.path.join(base, "rollout-b.jsonl"), meta("/work/other"), mtime=now - 1)
        write_file(os.path.join(base, "rollout-c.jsonl"), meta("/work/x"), mtime=now - 9999)
        found = hd.codex_transcript("/work/x", now - 100, self.home, now=now)
        self.assertEqual(found[0], mine)
        self.assertIsNone(hd.codex_transcript("/work/none", now - 100, self.home, now=now))

    def test_codex_session_cwd_tolerates_garbage(self):
        bad = write_file(os.path.join(self.home, "bad.jsonl"), "not json\n")
        self.assertIsNone(hd.codex_session_cwd(bad))
        self.assertIsNone(hd.codex_session_cwd("/nonexistent"))

    def test_progress_signature_changes_with_transcript(self):
        cwd = os.path.join(self.home, "w")
        os.makedirs(cwd)
        tr = write_file(os.path.join(self.home, ".claude/projects",
                                     hd.claude_project_slug(cwd), "s.jsonl"), "a", mtime=time.time())
        m = {"agent": "claude", "cwd": cwd, "created_at": "1970-01-02T00:00:00+0000"}
        before = hd.progress_signature(m, self.home)
        write_file(tr, "ab", mtime=time.time() + 5)
        self.assertNotEqual(hd.progress_signature(m, self.home), before)
        self.assertTrue(before.startswith("transcript:"))

    def test_progress_signature_falls_back_to_git_then_none(self):
        repo = os.path.join(self.home, "repo")
        os.makedirs(repo)
        subprocess.run(["git", "-C", repo, "init", "-q"], check=True)
        m = {"agent": "claude", "cwd": repo}
        clean = hd.progress_signature(m, self.home)
        open(os.path.join(repo, "f"), "w").close()
        dirty = hd.progress_signature(m, self.home)
        self.assertTrue(clean.startswith("git:"))
        self.assertNotEqual(clean, dirty)
        plain = os.path.join(self.home, "plain")
        os.makedirs(plain)
        self.assertIsNone(hd.progress_signature({"agent": "claude", "cwd": plain}, self.home))


class TestProgressWatch(unittest.TestCase):
    def test_first_observation_is_baseline(self):
        w = hd.ProgressWatch(100)
        self.assertFalse(w.observe("a", 110))
        self.assertEqual(w.quiet_secs(150), 50)

    def test_change_resets_quiet(self):
        w = hd.ProgressWatch(100)
        w.observe("a", 100)
        self.assertTrue(w.observe("b", 200))
        self.assertEqual(w.quiet_secs(230), 30)

    def test_same_signature_keeps_counting(self):
        w = hd.ProgressWatch(0)
        w.observe("a", 0)
        self.assertFalse(w.observe("a", 500))
        self.assertEqual(w.quiet_secs(500), 500)

    def test_observation_failure_keeps_state(self):
        w = hd.ProgressWatch(0)
        w.observe("a", 0)
        self.assertFalse(w.observe(None, 400))
        self.assertEqual((w.sig, w.quiet_secs(400)), ("a", 400))

    def test_reset(self):
        w = hd.ProgressWatch(0)
        w.observe("a", 0)
        w.reset(300)
        self.assertEqual((w.sig, w.quiet_secs(300)), (None, 0))


class TestWatcherStall(TempTaskCase):
    def make_watcher(self, **kw):
        self.make_task("dlg-1", state="running", **kw.pop("manifest", {}))
        return hd.Watcher("dlg-1", wait_args("dlg-1", stall_secs=600, stall_grace=300, **kw))

    def tick(self, w, status="working", sig="s"):
        with mock.patch.object(hd, "get_pane", return_value={"agent_status": status}), \
             mock.patch.object(hd, "pane_visible", return_value=""), \
             mock.patch.object(hd, "progress_signature", return_value=sig), \
             mock.patch.object(hd.subprocess, "run") as m_run:
            return w.tick(), m_run

    def test_notifies_once_and_latches_then_quiet(self):
        w = self.make_watcher()
        self.tick(w)  # baseline
        w.progress.last = time.time() - 601
        action, m_run = self.tick(w)
        self.assertEqual(action, "STALL_NOTIFY")
        self.assertIn("notification", m_run.call_args.args[0])
        self.assertTrue(hd.load_manifest(w.task_dir)["stall_notified_at"])
        action, m_run = self.tick(w)  # 通知は繰り返さない
        self.assertEqual(action, "CONTINUE")
        m_run.assert_not_called()
        w.progress.last = time.time() - 901
        self.assertEqual(self.tick(w)[0], "QUIET")

    def test_progress_clears_latch(self):
        w = self.make_watcher()
        self.tick(w, sig="a")
        w.progress.last = time.time() - 601
        self.tick(w, sig="a")
        self.assertTrue(w.notified)
        self.tick(w, sig="b")
        self.assertFalse(w.notified)
        self.assertIsNone(hd.load_manifest(w.task_dir)["stall_notified_at"])

    def test_observation_failure_keeps_latch(self):
        w = self.make_watcher()
        self.tick(w, sig="a")
        w.progress.last = time.time() - 601
        self.tick(w, sig="a")
        self.tick(w, sig=None)
        self.assertTrue(w.notified)

    def test_latch_survives_new_wait_invocation(self):
        w = self.make_watcher(manifest={"stall_notified_at": "T"})
        self.assertTrue(w.notified)

    def test_idle_resets_progress_tracking(self):
        w = self.make_watcher()
        w.progress.last = time.time() - 9999
        action, _ = self.tick(w, status="idle")
        self.assertEqual(action, "NUDGE")
        self.assertLess(w.progress.quiet_secs(time.time()), 5)

    def test_stall_secs_zero_never_samples_progress(self):
        self.make_task("dlg-1")
        w = hd.Watcher("dlg-1", wait_args("dlg-1", stall_secs=0))
        with mock.patch.object(hd, "get_pane", return_value={"agent_status": "working"}), \
             mock.patch.object(hd, "pane_visible", return_value=""), \
             mock.patch.object(hd, "progress_signature") as m_sig:
            self.assertEqual(w.tick(), "CONTINUE")
        m_sig.assert_not_called()

    def test_quiet_exit_5_via_cmd_wait(self):
        d = self.make_task("dlg-1", state="running", stall_notified_at="T")
        args = wait_args("dlg-1", stall_secs=1, stall_grace=0)
        with mock.patch.object(hd, "get_pane", return_value={"agent_status": "working"}), \
             mock.patch.object(hd, "pane_visible", return_value=""), \
             mock.patch.object(hd, "pane_tail", return_value="t"), \
             mock.patch.object(hd, "progress_signature", return_value="s"), \
             mock.patch.object(hd.time, "sleep"), \
             mock.patch.object(hd, "wait_step", side_effect=lambda *a: time.sleep(0)), \
             mock.patch("builtins.print"):
            orig = hd.ProgressWatch.quiet_secs
            with mock.patch.object(hd.ProgressWatch, "quiet_secs",
                                   lambda self, now: orig(self, now) + 5):
                with self.assertRaises(SystemExit) as cm:
                    hd.cmd_wait(args)
        self.assertEqual(cm.exception.code, 5)
        self.assertEqual(hd.load_manifest(d)["state"], "quiet")


class TestNudgeLoopInWatcher(TempTaskCase):
    def test_two_nudges_then_stalled(self):
        self.make_task("dlg-1", state="running")
        w = hd.Watcher("dlg-1", wait_args("dlg-1"))
        actions = []
        with mock.patch.object(hd, "get_pane", return_value={"agent_status": "idle"}), \
             mock.patch.object(hd, "pane_visible", return_value=""), \
             mock.patch.object(hd, "nudge", return_value=True) as m_nudge:
            actions.append(w.tick())                      # 1回目
            w.idle_since = time.time() - 25
            actions.append(w.tick())                      # 2回目 (間隔経過)
            w.idle_since = time.time() - 61
            actions.append(w.tick())
        self.assertEqual(actions, ["NUDGE", "NUDGE", "STALLED"])
        self.assertEqual(m_nudge.call_count, 2)


# ---------- A2: Stop hook / worker settings / deny floor ----------

HOOK = os.path.join(os.path.dirname(__file__), "..", "hooks", "herdr-delegate-stop.py")
hook_loader = importlib.machinery.SourceFileLoader("herdr_delegate_stop", HOOK)
hook_spec = importlib.util.spec_from_loader("herdr_delegate_stop", hook_loader)
hook = importlib.util.module_from_spec(hook_spec)
hook_loader.exec_module(hook)


class TestStopHookLogic(TempTaskCase):
    def test_statuses_in_sync_with_delegate(self):
        self.assertEqual(hook.RESULT_STATUSES, hd.RESULT_STATUSES)

    def test_valid_status_parity_with_read_result_status(self):
        for text in ("status: done\n", "Status: BLOCKED\n", "status: nope\n", "", "summary: x\n"):
            d = self.make_task(f"dlg-{abs(hash(text))}")
            self.write_result(d, text)
            expected = hd.read_result_status(d) is not None
            self.assertEqual(hook.has_valid_status(os.path.join(d, "result.md"),
                                                   os.path.join(d, "result.json")), expected, text)

    def test_result_json_counts(self):
        d = self.make_task()
        write_file(os.path.join(d, "result.json"), '{"status": "done"}')
        self.assertTrue(hook.has_valid_status(os.path.join(d, "result.md"),
                                              os.path.join(d, "result.json")))
        write_file(os.path.join(d, "result.json"), "{broken")
        self.assertFalse(hook.has_valid_status(os.path.join(d, "result.md"),
                                               os.path.join(d, "result.json")))

    def test_decide_stop(self):
        d = self.make_task()
        md = os.path.join(d, "result.md")
        self.assertEqual(hook.decide_stop(d, md, 0)[0], "block")
        self.assertEqual(hook.decide_stop(d, md, 1)[0], "block")
        self.assertEqual(hook.decide_stop(d, md, 2), ("violation", None))
        self.write_result(d, "status: done\n")
        self.assertEqual(hook.decide_stop(d, md, 5), ("allow", None))


class TestStopHookMain(TempTaskCase):
    def run_hook(self, d, stdin='{"stop_hook_active": false}', env=True):
        out = io.StringIO()
        environ = {"HERDR_DELEGATE_TASK_DIR": d, "HERDR_DELEGATE_RESULT": os.path.join(d, "result.md")}
        with mock.patch.dict(os.environ, environ if env else {}, clear=not env), \
             mock.patch.object(hook.sys, "stdin", io.StringIO(stdin)), \
             mock.patch.object(hook.sys, "stdout", out):
            code = hook.main()
        return code, out.getvalue()

    def test_not_a_delegate_worker_is_noop(self):
        d = self.make_task()
        self.assertEqual(self.run_hook(d, env=False), (0, ""))

    def test_blocks_twice_then_allows_and_records_violation(self):
        d = self.make_task()
        for n in (1, 2):
            code, out = self.run_hook(d, stdin='{"stop_hook_active": true}')
            self.assertEqual(json.loads(out)["decision"], "block")
            self.assertEqual(hook.read_nudges(os.path.join(d, "stop_nudges")), n)
        self.assertEqual(self.run_hook(d), (0, ""))
        self.assertTrue(os.path.exists(os.path.join(d, "protocol_violation")))

    def test_valid_result_allows_without_nudge(self):
        d = self.make_task()
        self.write_result(d, "status: failed\n")
        self.assertEqual(self.run_hook(d), (0, ""))
        self.assertFalse(os.path.exists(os.path.join(d, "stop_nudges")))

    def test_broken_stdin_still_works(self):
        d = self.make_task()
        code, out = self.run_hook(d, stdin="not json")
        self.assertEqual(json.loads(out)["decision"], "block")

    def test_read_nudges_garbage_is_zero(self):
        d = self.make_task()
        self.assertEqual(hook.read_nudges(os.path.join(d, "nope")), 0)
        p = write_file(os.path.join(d, "stop_nudges"), "x")
        self.assertEqual(hook.read_nudges(p), 0)


class TestWorkerSettings(TempTaskCase):
    def test_build_includes_stop_hook_and_deny(self):
        st = hd.build_worker_settings("python3 /h.py", ["Bash(x*)"])
        self.assertEqual(st["hooks"]["Stop"][0]["hooks"][0],
                         {"type": "command", "command": "python3 /h.py"})
        self.assertEqual(st["permissions"]["deny"], ["Bash(x*)"])

    def test_build_without_deny_has_no_permissions(self):
        self.assertNotIn("permissions", hd.build_worker_settings("c", []))

    def test_deny_floor_covers_required_patterns(self):
        floor = " ".join(hd.DENY_FLOOR)
        for needle in ("git push --force", "rm -rf /", "herdr pane close",
                       "herdr worktree remove", "herdr server stop"):
            self.assertIn(needle, floor)

    def test_stop_hook_path_exists_and_is_executable(self):
        self.assertTrue(os.access(hd.STOP_HOOK, os.X_OK), hd.STOP_HOOK)

    def test_write_settings_file(self):
        d = self.make_task()
        path = hd.write_worker_settings(d, list(hd.DENY_FLOOR))
        self.assertEqual(path, os.path.join(d, "worker-settings.json"))
        with open(path) as f:
            data = json.load(f)
        self.assertIn(hd.STOP_HOOK, data["hooks"]["Stop"][0]["hooks"][0]["command"])
        self.assertEqual(data["permissions"]["deny"], list(hd.DENY_FLOOR))

    def wrapper_text(self, agent, settings):
        d = self.make_task()
        path = hd.build_wrapper(d, agent, None, False, d, settings)
        self.addCleanup(lambda: os.path.exists(path) and os.unlink(path))
        with open(path) as f:
            return d, f.read()

    def test_wrapper_passes_settings_and_exports_env_for_claude(self):
        d, text = self.wrapper_text("claude", "/t/worker-settings.json")
        self.assertIn("--settings /t/worker-settings.json", text)
        self.assertIn(f"export HERDR_DELEGATE_TASK_DIR={d}", text)
        self.assertIn(f"export HERDR_DELEGATE_RESULT={d}/result.md", text)

    def test_wrapper_without_settings_has_no_flag(self):
        _, text = self.wrapper_text("claude", None)
        self.assertNotIn("--settings", text)

    def test_wrapper_codex_ignores_settings(self):
        _, text = self.wrapper_text("codex", None)
        self.assertNotIn("--settings", text)
        self.assertIn("exec codex", text)


class CreateTaskCase(TempTaskCase):
    def spawn_args(self, **kw):
        base = dict(agent="claude", model=None, worktree=None, cwd=self.root, label="job",
                    safe=False, idempotency_key=None, prompt="do x", prompt_file=None,
                    schema=None, parent=None, group=None, max_concurrent=None,
                    no_deny_floor=False, verify=None, constraints=None, stop_when=None,
                    gate=None, gate_timeout=300, gate_retries=3, fallback_from=None)
        base.update(kw)
        return types.SimpleNamespace(**base)

    def create(self, **kw):
        pane = {"pane_id": "w1:p9", "tab_id": "t", "workspace_id": "w1", "cwd": self.root,
                "worktree_path": None, "worktree_branch": None}
        with mock.patch.object(hd, "open_pane", return_value=pane), \
             mock.patch.object(hd, "start_worker") as m_start, \
             mock.patch("builtins.print"):
            tid = hd.create_task(self.spawn_args(**kw), "do x", self.root)
        return tid, hd.load_manifest(os.path.join(self.tasks, tid)), m_start


class TestCreateTask(CreateTaskCase):
    def failing_create(self, pane, **patches):
        with mock.patch.object(hd, "open_pane", return_value=pane), \
             mock.patch.object(hd, "hcli") as m_hcli, \
             mock.patch("builtins.print"):
            with mock.patch.object(hd, "start_worker", side_effect=RuntimeError("boom")):
                with self.assertRaises(RuntimeError):
                    hd.create_task(self.spawn_args(), "do x", self.root)
        return m_hcli

    def test_start_failure_closes_pane_and_removes_task_dir(self):
        pane = {"pane_id": "w1:p9", "tab_id": "t", "workspace_id": "w1", "cwd": self.root,
                "worktree_path": None, "worktree_branch": None}
        m_hcli = self.failing_create(pane)
        m_hcli.assert_called_once_with("pane", "close", "w1:p9")
        self.assertEqual(os.listdir(self.tasks), [])

    def test_start_failure_removes_worktree_workspace(self):
        pane = {"pane_id": "w2:p1", "tab_id": "t", "workspace_id": "w2", "cwd": self.root,
                "worktree_path": "/wt", "worktree_branch": "b"}
        m_hcli = self.failing_create(pane)
        m_hcli.assert_called_once_with("worktree", "remove", "--workspace", "w2", "--force")
        self.assertEqual(os.listdir(self.tasks), [])

    def test_claude_gets_settings_and_deny_floor_recorded(self):
        tid, m, m_start = self.create()
        self.assertEqual(m["deny_floor"], list(hd.DENY_FLOOR))
        self.assertEqual(m["worker_settings"], os.path.join(self.tasks, tid, "worker-settings.json"))
        self.assertEqual(m_start.call_args.args[3], m["worker_settings"])
        self.assertTrue(os.path.exists(m["worker_settings"]))

    def test_no_deny_floor_keeps_hook_only(self):
        _, m, _ = self.create(no_deny_floor=True)
        self.assertEqual(m["deny_floor"], [])
        with open(m["worker_settings"]) as f:
            data = json.load(f)
        self.assertNotIn("permissions", data)
        self.assertIn("hooks", data)

    def test_codex_has_no_settings_or_floor(self):
        _, m, m_start = self.create(agent="codex")
        self.assertEqual((m["worker_settings"], m["deny_floor"]), (None, []))
        self.assertIsNone(m_start.call_args.args[3])

    def test_group_parents_schema_recorded(self):
        parent_dir = self.make_task("dlg-par", state="done")
        self.write_result(parent_dir, "status: done\nsummary: 親の結果\n")
        schema = write_file(os.path.join(self.root, "s.json"), '{"required": ["verdict"]}')
        tid, m, _ = self.create(group="g1", parent="dlg-par", schema=schema)
        self.assertEqual((m["group"], m["parents"], m["schema"]), ("g1", ["dlg-par"], True))
        d = os.path.join(self.tasks, tid)
        self.assertTrue(os.path.exists(os.path.join(d, "schema.json")))
        with open(os.path.join(d, "prompt.md")) as f:
            prompt = f.read()
        self.assertTrue(prompt.startswith("## Parent task results"))
        self.assertIn("親の結果", prompt)
        self.assertIn("result.json", prompt)  # 契約フッターが schema 用の指示を含む

    def test_unknown_parent_is_config_error_before_any_task_dir(self):
        with self.assertRaises(hd.ConfigError):
            self.create(parent="dlg-nope")
        self.assertEqual(os.listdir(self.tasks), [])

    def test_max_concurrent_refuses(self):
        self.make_task("dlg-a", state="running")
        with self.assertRaises(hd.ConfigError):
            self.create(max_concurrent=1)

    def test_reserved_task_counts_before_pane_exists(self):
        """manifest 本体が書かれる前でも、予約済み task が並列数に数えられる (fan-out 競合対策)."""
        args = self.spawn_args(max_concurrent=1)
        d = hd.reserve_task(args, "do x", self.root)
        m = hd.load_manifest(d)
        self.assertEqual((m["state"], m["pane_id"]), ("running", None))
        with self.assertRaises(hd.ConfigError):
            hd.reserve_task(args, "do y", self.root)
        self.assertEqual(len(os.listdir(self.tasks)), 1)

    def test_open_pane_failure_releases_reservation(self):
        with mock.patch.object(hd, "open_pane", side_effect=RuntimeError("boom")):
            with self.assertRaises(RuntimeError):
                hd.create_task(self.spawn_args(max_concurrent=1), "do x", self.root)
        self.assertEqual(os.listdir(self.tasks), [])
        self.create(max_concurrent=1)  # 枠は解放されている

    def test_max_concurrent_allows_below_limit(self):
        self.make_task("dlg-a", state="done")
        self.create(max_concurrent=1)


class TestConcurrency(unittest.TestCase):
    def test_count_active_skips_terminal(self):
        ms = [{"state": "running"}, {"state": "blocked"}, {"state": "done"}, {"state": "killed"},
              {}, {"killed_at": "T"}]
        self.assertEqual(hd.count_active(ms), 3)

    def test_count_active_by_group(self):
        ms = [{"state": "running", "group": "a"}, {"state": "running", "group": "b"},
              {"state": "running"}]
        self.assertEqual(hd.count_active(ms, "a"), 1)


# ---------- A2: preflight ----------

class TestPreflightExtended(unittest.TestCase):
    def check(self, **kw):
        base = dict(which=lambda e: "/bin/" + e, isdir=lambda p: True,
                    herdr_ok=lambda: True, is_git=lambda p: True)
        base.update(kw)
        return hd.preflight_problems("claude", "/x", **base)

    def test_herdr_unreachable(self):
        self.assertTrue(any("herdr サーバ" in p for p in self.check(herdr_ok=lambda: False)))

    def test_unreachable_not_probed_when_herdr_missing(self):
        probed = []
        probs = self.check(which=lambda e: None if e == "herdr" else "/b",
                           herdr_ok=lambda: probed.append(1) or False)
        self.assertEqual(probed, [])
        self.assertEqual(len(probs), 1)

    def test_worktree_requires_git(self):
        probs = self.check(worktree=True, is_git=lambda p: False)
        self.assertTrue(any("git" in p for p in probs))
        self.assertEqual(self.check(worktree=False, is_git=lambda p: False), [])

    def test_missing_cwd_short_circuits(self):
        self.assertEqual(len(self.check(isdir=lambda p: False)), 1)

    def test_herdr_reachable_wraps_errors(self):
        with mock.patch.object(hd, "hcli", side_effect=hd.HerdrUnavailable("x")):
            self.assertFalse(hd.herdr_reachable())
        with mock.patch.object(hd, "hcli", return_value={}):
            self.assertTrue(hd.herdr_reachable())

    def test_is_git_repo(self):
        with tempfile.TemporaryDirectory() as d:
            self.assertFalse(hd.is_git_repo(d))
            subprocess.run(["git", "-C", d, "init", "-q"], check=True)
            self.assertTrue(hd.is_git_repo(d))


class TestSpawnPreflightExit8(CreateTaskCase):
    def test_preflight_failure_creates_no_pane_or_task(self):
        with mock.patch.object(hd, "preflight_problems", return_value=["x"]), \
             mock.patch.object(hd, "open_pane") as m_open:
            with self.assertRaises(hd.ConfigError):
                hd.cmd_spawn(self.spawn_args())
        m_open.assert_not_called()
        self.assertEqual(os.listdir(self.tasks), [])

    def test_worktree_flag_reaches_preflight(self):
        with mock.patch.object(hd, "preflight_problems", return_value=[]) as m_pre, \
             mock.patch.object(hd, "create_task", return_value="t"):
            hd.cmd_spawn(self.spawn_args(worktree=""))
        self.assertTrue(m_pre.call_args.kwargs["worktree"])


# ---------- A2: doctor ----------

class TestDiagnoseTask(unittest.TestCase):
    LIMITS = {"max_runtime": 3600, "stranded": 600, "reap_after": 120}

    def diag(self, m, pane, result=None, idle_age=0, now=None):
        now = now or hd.iso_to_epoch("2026-01-01T01:00:00+0000")
        m = {"pane_id": "w1:p1", "created_at": "2026-01-01T00:59:00+0000", **m}
        return {f["code"] for f in hd.diagnose_task(m, pane, result, now, idle_age, self.LIMITS)}

    def test_crashed_when_pane_gone_without_result(self):
        self.assertEqual(self.diag({"state": "running"}, None), {"crashed"})

    def test_gone_but_delivered_is_not_crashed(self):
        self.assertEqual(self.diag({"state": "gone", "delivered_at": "T"}, None), set())

    def test_pane_gone_with_done_result_is_not_crashed(self):
        self.assertEqual(self.diag({"state": "running"}, None, result="done"), set())

    def test_killed_is_ignored(self):
        self.assertEqual(self.diag({"state": "killed"}, None), set())

    def test_overtime(self):
        m = {"state": "running", "created_at": "2025-12-31T00:00:00+0000"}
        self.assertIn("overtime", self.diag(m, {"agent_status": "working"}))
        self.assertNotIn("overtime", self.diag({"state": "running"}, {"agent_status": "working"}))

    def test_stranded_idle_without_result(self):
        pane = {"agent_status": "idle"}
        self.assertEqual(self.diag({"state": "running"}, pane, idle_age=601), {"stranded"})
        self.assertEqual(self.diag({"state": "running"}, pane, idle_age=599), set())

    def test_done_pane_alive_reapable(self):
        pane = {"agent_status": "idle"}
        self.assertEqual(self.diag({"state": "done"}, pane, result="done", idle_age=121),
                         {"done_pane_alive"})
        self.assertEqual(self.diag({"state": "done"}, pane, result="done", idle_age=5), set())

    def test_result_done_but_not_waited_is_not_reapable(self):
        pane = {"agent_status": "idle"}
        self.assertEqual(self.diag({"state": "running"}, pane, result="done", idle_age=500),
                         {"done_unwaited"})
        self.assertEqual(self.diag({"state": "running", "delivered_at": "T"}, pane,
                                   result="done", idle_age=500), {"done_pane_alive"})

    def test_provisional_manifest_without_pane_is_ignored(self):
        self.assertEqual(self.diag({"state": "running", "pane_id": None}, None), set())

    def test_working_done_pane_is_not_reaped(self):
        self.assertEqual(self.diag({"state": "done"}, {"agent_status": "working"},
                                   result="done", idle_age=999), set())

    def test_protocol_violation_reported(self):
        self.assertIn("protocol_violation",
                      self.diag({"state": "protocol_violation"}, {"agent_status": "idle"}))


WT_LIST = """worktree /repo
HEAD aaa
branch refs/heads/main

worktree /wt/one
HEAD bbb
branch refs/heads/delegate/dlg-1

worktree /wt/two
HEAD ccc
detached
"""


class TestOrphanWorktrees(unittest.TestCase):
    def test_parse_worktree_list_skips_main(self):
        self.assertEqual(hd.parse_worktree_list(WT_LIST),
                         [{"path": "/wt/one", "branch": "delegate/dlg-1"},
                          {"path": "/wt/two", "branch": ""}])
        self.assertEqual(hd.parse_worktree_list(None), [])

    def test_unknown_delegate_branch_is_orphan(self):
        wts = [{"path": "/wt/x", "branch": "delegate/dlg-9"}]
        orphans = hd.find_orphan_worktrees(wts, [{"task_id": "dlg-1"}])
        self.assertEqual([o["path"] for o in orphans], ["/wt/x"])

    def test_known_live_task_is_not_orphan(self):
        wts = [{"path": "/wt/x", "branch": "delegate/dlg-1"}]
        tasks = [{"task_id": "dlg-1", "worktree_path": "/wt/x", "state": "running", "removed": False}]
        self.assertEqual(hd.find_orphan_worktrees(wts, tasks), [])

    def test_killed_task_leftover_is_orphan(self):
        wts = [{"path": "/wt/x", "branch": "feat/user-named"}]
        tasks = [{"task_id": "dlg-1", "worktree_path": "/wt/x", "state": "killed", "removed": False}]
        self.assertEqual(len(hd.find_orphan_worktrees(wts, tasks)), 1)
        tasks[0]["removed"] = True
        self.assertEqual(hd.find_orphan_worktrees(wts, tasks), [])

    def test_user_branches_outside_ledger_are_ignored(self):
        self.assertEqual(hd.find_orphan_worktrees([{"path": "/p", "branch": "feat/x"}], []), [])


class TestCmdDoctor(TempTaskCase):
    def run_doctor(self, panes, **kw):
        args = types.SimpleNamespace(json=True, reap=kw.get("reap", False), max_runtime=3600,
                                     stranded_secs=600, reap_after=120, cwd=self.root)
        with mock.patch.object(hd, "hcli") as m_hcli, \
             mock.patch.object(hd, "save_recovery") as self.m_recovery, \
             mock.patch.object(hd, "last_activity_age", return_value=kw.get("age", 0)), \
             mock.patch("builtins.print") as m_print:
            m_hcli.side_effect = lambda *a: {"panes": panes} if a == ("pane", "list") else {}
            with self.assertRaises(SystemExit) as cm:
                hd.cmd_doctor(args)
        return cm.exception.code, json.loads(m_print.call_args.args[0]), m_hcli

    def test_clean_ledger_exit_0(self):
        code, out, _ = self.run_doctor([])
        self.assertEqual((code, out["findings"]), (0, []))

    def test_reports_crashed_exit_1(self):
        self.make_task("dlg-1", state="running", created_at="2026-01-01T00:00:00+0000")
        code, out, _ = self.run_doctor([])
        self.assertEqual(code, 1)
        self.assertEqual([(f["task_id"], f["code"]) for f in out["findings"]],
                         [("dlg-1", "crashed")])

    def test_reap_closes_done_pane_and_clears_finding(self):
        d = self.make_task("dlg-1", state="done", pane_id="w1:p1")
        self.write_result(d, "status: done\n")
        code, out, m_hcli = self.run_doctor([{"pane_id": "w1:p1", "agent_status": "idle"}],
                                            reap=True, age=500)
        self.assertEqual((code, out["reaped"], out["findings"]), (0, ["dlg-1"], []))
        self.assertIn(mock.call("pane", "close", "w1:p1"), m_hcli.call_args_list)
        self.m_recovery.assert_called_once_with(d, "done")  # close 前に復旧情報を保存

    def test_reap_skips_task_not_yet_waited(self):
        d = self.make_task("dlg-1", state="running", pane_id="w1:p1", created_at=hd.now_iso())
        self.write_result(d, "status: done\n")
        code, out, m_hcli = self.run_doctor([{"pane_id": "w1:p1", "agent_status": "idle"}],
                                            reap=True, age=500)
        self.assertEqual((code, out["reaped"]), (1, []))
        self.assertEqual([f["code"] for f in out["findings"]], ["done_unwaited"])
        self.assertNotIn(mock.call("pane", "close", "w1:p1"), m_hcli.call_args_list)
        self.m_recovery.assert_not_called()

    def test_without_reap_only_reports(self):
        d = self.make_task("dlg-1", state="done", pane_id="w1:p1")
        self.write_result(d, "status: done\n")
        code, out, m_hcli = self.run_doctor([{"pane_id": "w1:p1", "agent_status": "idle"}], age=500)
        self.assertEqual((code, out["findings"][0]["code"]), (1, "done_pane_alive"))
        self.assertNotIn(mock.call("pane", "close", "w1:p1"), m_hcli.call_args_list)

    def test_last_activity_age_uses_transcript_then_events(self):
        m = {"agent": "claude", "cwd": "/nonexistent", "created_at": "2026-01-01T00:00:00+0000",
             "events": [{"ts": "2026-01-01T00:10:00+0000"}]}
        now = hd.iso_to_epoch("2026-01-01T00:20:00+0000")
        self.assertEqual(hd.last_activity_age(m, now), 600)
        with mock.patch.object(hd, "claude_transcript", return_value=("p", int((now - 30) * 1e9), 1)):
            self.assertAlmostEqual(hd.last_activity_age(m, now), 30, places=3)


# ---------- A2: 安全な worktree 後始末 ----------

def git_cmd(cwd, *args):
    subprocess.run(["git", "-C", cwd, "-c", "user.name=t", "-c", "user.email=t@t", *args],
                   check=True, capture_output=True)


def make_repo_with_worktree(root, branch="delegate/dlg-1"):
    repo = os.path.join(root, "repo")
    os.makedirs(repo)
    git_cmd(repo, "init", "-q")
    git_cmd(repo, "commit", "-q", "--allow-empty", "-m", "base")
    wt = os.path.join(root, "wt")
    git_cmd(repo, "worktree", "add", "-q", "-b", branch, wt)
    base = hd.run_git(repo, "rev-parse", "HEAD")
    return repo, wt, base


class TestPlanWorktreeRemoval(unittest.TestCase):
    def plan(self, commits, dirty, force=False):
        return hd.plan_worktree_removal({"commits": commits, "dirty": dirty}, force)

    def test_clean_is_plain_remove(self):
        self.assertEqual(self.plan(0, 0), "remove")

    def test_commits_archive_without_force(self):
        self.assertEqual(self.plan(2, 0), "archive_remove")

    def test_dirty_needs_force(self):
        self.assertEqual(self.plan(0, 3), "refuse_dirty")
        self.assertEqual(self.plan(1, 3), "refuse_dirty")
        self.assertEqual(self.plan(0, 3, force=True), "archive_remove")

    def test_probe_failure_never_deletes(self):
        self.assertEqual(self.plan(None, 0), "inspection_failed")
        self.assertEqual(self.plan(0, None, force=True), "inspection_failed")


class TestWorktreeGit(unittest.TestCase):
    def setUp(self):
        self.root = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.root, ignore_errors=True)
        self.repo, self.wt, self.base = make_repo_with_worktree(self.root)

    def test_probe_clean(self):
        self.assertEqual(hd.probe_worktree(self.wt, self.base), {"commits": 0, "dirty": 0})

    def test_probe_commit_and_dirty(self):
        write_file(os.path.join(self.wt, "a"), "1")
        git_cmd(self.wt, "add", "a")
        git_cmd(self.wt, "commit", "-q", "-m", "c")
        write_file(os.path.join(self.wt, "u"), "untracked")
        self.assertEqual(hd.probe_worktree(self.wt, self.base), {"commits": 1, "dirty": 1})

    def test_probe_failures_are_none(self):
        self.assertEqual(hd.probe_worktree(self.wt, "deadbeef" * 5)["commits"], None)
        self.assertEqual(hd.probe_worktree(os.path.join(self.root, "no"), self.base),
                         {"commits": None, "dirty": None})

    def test_probe_without_base_counts_unpushed_commits(self):
        self.assertGreaterEqual(hd.probe_worktree(self.wt, None)["commits"], 1)

    def test_archive_head_and_dirty_snapshot_incl_untracked(self):
        write_file(os.path.join(self.wt, "tracked"), "1")
        git_cmd(self.wt, "add", "tracked")
        git_cmd(self.wt, "commit", "-q", "-m", "c")
        write_file(os.path.join(self.wt, "tracked"), "2")
        write_file(os.path.join(self.wt, "new.txt"), "untracked")
        probe = hd.probe_worktree(self.wt, self.base)
        refs = hd.archive_worktree(self.wt, "dlg-1", probe)
        self.assertEqual(refs, ["refs/herdr/archive/dlg-1", "refs/herdr/archive/dlg-1-dirty"])
        head = hd.run_git(self.repo, "rev-parse", "refs/herdr/archive/dlg-1")
        self.assertEqual(head, hd.run_git(self.wt, "rev-parse", "HEAD"))
        files = hd.run_git(self.repo, "ls-tree", "-r", "--name-only", refs[1]).split()
        self.assertIn("new.txt", files)
        self.assertEqual(hd.run_git(self.repo, "show", f"{refs[1]}:tracked"), "2")
        # 退避後に作業ツリーが変わっていない (index も汚さない)
        self.assertEqual(hd.count_porcelain(hd.run_git(self.wt, "status", "--porcelain")), 2)

    def test_archive_without_dirty_makes_one_ref(self):
        probe = {"commits": 1, "dirty": 0}
        self.assertEqual(hd.archive_worktree(self.wt, "dlg-1", probe), ["refs/herdr/archive/dlg-1"])

    def test_git_repo_root_and_remove_via_git(self):
        self.assertEqual(os.path.realpath(hd.git_repo_root(self.wt)), os.path.realpath(self.repo))
        hd.remove_worktree_via_git(self.wt)
        self.assertFalse(os.path.exists(self.wt))

    def test_remove_via_git_failure_raises(self):
        with self.assertRaises(RuntimeError):
            hd.remove_worktree_via_git(os.path.join(self.root, "not-a-wt"))


class TestCmdKillSafePrune(TempTaskCase):
    def setUp(self):
        super().setUp()
        self.repo, self.wt, self.base = make_repo_with_worktree(self.root)
        self.d = self.make_task("dlg-1", worktree_path=self.wt, base_commit=self.base,
                                workspace_id="ws1", state="running")

    def kill(self, pane=None, force=False):
        args = types.SimpleNamespace(task_id="dlg-1", rm_worktree=True, force_discard=force)
        with mock.patch.object(hd, "get_pane", return_value=pane), \
             mock.patch.object(hd, "hcli") as m_hcli, mock.patch("builtins.print"):
            try:
                hd.cmd_kill(args)
                code = 0
            except SystemExit as e:
                code = e.code
        return code, m_hcli, hd.load_manifest(self.d)

    def ref_exists(self, ref):
        return hd.run_git(self.repo, "rev-parse", "--verify", "-q", ref) is not None

    def test_pane_gone_clean_worktree_removed_via_git(self):
        # 旧実装は無音 no-op だった
        code, m_hcli, m = self.kill(pane=None)
        self.assertEqual(code, 0)
        self.assertFalse(os.path.exists(self.wt))
        m_hcli.assert_not_called()
        self.assertTrue(m["worktree_removed_at"])
        self.assertEqual(m["state"], "killed")
        self.assertFalse(self.ref_exists("refs/herdr/archive/dlg-1"))

    def test_commits_are_archived_before_removal(self):
        write_file(os.path.join(self.wt, "a"), "1")
        git_cmd(self.wt, "add", "a")
        git_cmd(self.wt, "commit", "-q", "-m", "work")
        code, _, m = self.kill(pane=None)
        self.assertEqual(code, 0)
        self.assertFalse(os.path.exists(self.wt))
        self.assertEqual(m["archive_refs"], ["refs/herdr/archive/dlg-1"])
        self.assertTrue(self.ref_exists("refs/herdr/archive/dlg-1"))

    def test_dirty_refused_without_force_and_nothing_changes(self):
        write_file(os.path.join(self.wt, "u"), "x")
        code, m_hcli, m = self.kill(pane={"agent_status": "idle"})
        self.assertEqual(code, hd.EXIT_FAILED)
        self.assertTrue(os.path.exists(self.wt))
        m_hcli.assert_not_called()
        self.assertEqual(m["state"], "running")
        self.m_recovery.assert_not_called()

    def test_dirty_with_force_archives_snapshot_then_removes(self):
        write_file(os.path.join(self.wt, "u"), "x")
        code, _, m = self.kill(pane=None, force=True)
        self.assertEqual(code, 0)
        self.assertFalse(os.path.exists(self.wt))
        self.assertEqual(m["archive_refs"][1], "refs/herdr/archive/dlg-1-dirty")
        self.assertTrue(self.ref_exists("refs/herdr/archive/dlg-1-dirty"))

    def test_probe_failure_keeps_worktree_and_flags_it(self):
        with mock.patch.object(hd, "probe_worktree", return_value={"commits": None, "dirty": 0}):
            code, m_hcli, m = self.kill(pane=None)
        self.assertEqual(code, hd.EXIT_FAILED)
        self.assertTrue(os.path.exists(self.wt))
        self.assertTrue(m["inspection_failed"])
        self.assertNotIn("worktree_removed_at", m)
        self.assertEqual(m["state"], "killed")

    def test_archive_failure_keeps_worktree(self):
        write_file(os.path.join(self.wt, "u"), "x")
        with mock.patch.object(hd, "archive_worktree", return_value=None):
            code, _, m = self.kill(pane=None, force=True)
        self.assertEqual(code, hd.EXIT_FAILED)
        self.assertTrue(os.path.exists(self.wt))
        self.assertTrue(m["inspection_failed"])

    def test_pane_alive_uses_herdr_remove_after_archive(self):
        write_file(os.path.join(self.wt, "a"), "1")
        git_cmd(self.wt, "add", "a")
        git_cmd(self.wt, "commit", "-q", "-m", "work")
        code, m_hcli, m = self.kill(pane={"agent_status": "idle"})
        self.assertEqual(code, 0)
        m_hcli.assert_called_once_with("worktree", "remove", "--workspace", "ws1", "--force")
        self.assertTrue(self.ref_exists("refs/herdr/archive/dlg-1"))

    def test_old_manifest_without_base_commit_is_archived_then_removed(self):
        hd.update_manifest(self.d, lambda m: m.pop("base_commit"))
        code, _, m = self.kill(pane=None)
        self.assertNotEqual(code, hd.EXIT_FAILED)
        self.assertTrue(self.ref_exists("refs/herdr/archive/dlg-1"))
        self.assertFalse(m.get("inspection_failed"))


# ---------- A2: 構造化結果 ----------

class TestParseResult(unittest.TestCase):
    MD = """status: done
summary: 3 行の結果
  詳細 2 行目
artifacts:
  - src/a.py
  - delegate/dlg-1, https://example.com/pr/1
"""

    def test_parse_md_fields(self):
        r = hd.parse_result_md(self.MD)
        self.assertEqual(r["status"], "done")
        self.assertEqual(r["summary"], "3 行の結果\n  詳細 2 行目")
        self.assertEqual(r["artifacts"], ["src/a.py", "delegate/dlg-1", "https://example.com/pr/1"])
        self.assertIsNone(r["kind"])

    def test_parse_md_blocked_kind_question(self):
        r = hd.parse_result_md("status: blocked\nkind: capability\nquestion: 権限ください\n")
        self.assertEqual((r["kind"], r["question"]), ("capability", "権限ください"))

    def test_parse_md_without_keys(self):
        self.assertEqual(hd.parse_result_md("ただの文章"),
                         {"status": None, "summary": "", "artifacts": [], "kind": None,
                          "question": None})

    def test_artifacts_stop_at_heading_and_keep_parens(self):
        text = ("status: done\nsummary: ok\nartifacts:\n- /r/fizz.py (new, untracked)\n- README.md（新規、未コミット）\n"
                "\n## Verification evidence\n実行: python3 -m unittest\nOK\n")
        res = hd.parse_result_md(text)
        self.assertEqual(res["artifacts"], ["/r/fizz.py (new, untracked)", "README.md（新規、未コミット）"])
        self.assertEqual(res["status"], "done")

    def test_split_artifacts(self):
        self.assertEqual(hd.split_artifacts("a, b\n* c\n\n"), ["a", "b", "c"])
        self.assertEqual(hd.split_artifacts(""), [])

    def test_normalize_kind(self):
        self.assertEqual(hd.normalize_kind("Transient"), "transient")
        self.assertEqual(hd.normalize_kind("weird"), "needs_input")
        self.assertEqual(hd.normalize_kind(None), "needs_input")

    def test_parse_json(self):
        r = hd.parse_result_json('{"status": "done", "summary": "s", "artifacts": ["a"]}')
        self.assertEqual((r["status"], r["summary"], r["artifacts"]), ("done", "s", ["a"]))

    def test_parse_json_rejects_invalid(self):
        for bad in ("{", "[]", '{"status": "weird"}', '{"summary": "x"}'):
            self.assertIsNone(hd.parse_result_json(bad), bad)

    def test_parse_json_tolerates_null_artifacts(self):
        self.assertEqual(hd.parse_result_json('{"status": "failed", "artifacts": null}')["artifacts"], [])


class TestLoadResult(TempTaskCase):
    def test_json_preferred_over_md(self):
        d = self.make_task()
        self.write_result(d, "status: failed\n")
        write_file(os.path.join(d, "result.json"), '{"status": "done", "summary": "j"}')
        res = hd.load_result(d)
        self.assertEqual((res["status"], res["source"]), ("done", "result.json"))
        self.assertEqual(hd.read_result_status(d), "done")

    def test_invalid_json_falls_back_to_md(self):
        d = self.make_task()
        self.write_result(d, "status: done\nsummary: m\n")
        write_file(os.path.join(d, "result.json"), "{half")
        self.assertEqual(hd.load_result(d)["source"], "result.md")

    def test_json_only_task_is_done(self):
        d = self.make_task()
        write_file(os.path.join(d, "result.json"), '{"status": "blocked", "kind": "dependency"}')
        self.assertEqual(hd.read_result_status(d), "blocked")

    def test_none_when_nothing_valid(self):
        d = self.make_task()
        self.write_result(d, "summary: 書きかけ\n")
        self.assertIsNone(hd.load_result(d))

    def test_archive_and_restore_cover_result_json(self):
        d = self.make_task()
        self.write_result(d, "status: done\n")
        write_file(os.path.join(d, "result.json"), '{"status": "done"}')
        archived = hd.archive_result(d)
        self.assertTrue(os.path.exists(os.path.join(d, "result.1.json")))
        self.assertIsNone(hd.read_result_status(d))  # 古い json を拾わない
        hd.restore_result(d, archived)
        self.assertEqual(hd.read_result_status(d), "done")
        self.assertTrue(os.path.exists(os.path.join(d, "result.json")))

    def test_archive_json_only(self):
        d = self.make_task()
        write_file(os.path.join(d, "result.json"), '{"status": "done"}')
        self.assertEqual(hd.archive_result(d), os.path.join(d, "result.1.json"))


class TestSchemaValidation(unittest.TestCase):
    SCHEMA = {"required": ["verdict", "score"],
              "properties": {"verdict": {"type": "string"}, "score": {"type": "integer"},
                             "tags": {"type": "array"}}}

    def test_valid(self):
        self.assertEqual(hd.validate_result({"verdict": "ok", "score": 3, "tags": []}, self.SCHEMA), [])

    def test_missing_required(self):
        errs = hd.validate_result({"verdict": "ok"}, self.SCHEMA)
        self.assertEqual(errs, ["必須キー score がない"])

    def test_type_mismatch(self):
        errs = hd.validate_result({"verdict": 1, "score": "x"}, self.SCHEMA)
        self.assertEqual(len(errs), 2)

    def test_bool_is_not_integer(self):
        self.assertEqual(len(hd.validate_result({"verdict": "a", "score": True}, self.SCHEMA)), 1)

    def test_number_accepts_float_and_int(self):
        s = {"properties": {"x": {"type": "number"}}}
        self.assertEqual(hd.validate_result({"x": 1.5}, s) + hd.validate_result({"x": 2}, s), [])

    def test_non_object(self):
        self.assertEqual(len(hd.validate_result([], self.SCHEMA)), 1)

    def test_error_cap(self):
        schema = {"required": [f"k{i}" for i in range(30)]}
        self.assertEqual(len(hd.validate_result({}, schema)), hd.MAX_SCHEMA_ERRORS)

    def test_unknown_type_is_not_checked(self):
        self.assertEqual(hd.validate_result({"a": 1}, {"properties": {"a": {"type": "weird"}}}), [])

    def test_describe_schema(self):
        self.assertIn("verdict(string)", hd.describe_schema(self.SCHEMA))
        self.assertEqual(hd.describe_schema({}), "追加の必須キーなし")

    def test_load_schema(self):
        with tempfile.TemporaryDirectory() as d:
            ok = write_file(os.path.join(d, "s.json"), '{"required": []}')
            self.assertEqual(hd.load_schema(ok), {"required": []})
            self.assertIsNone(hd.load_schema(None))
            for bad in (write_file(os.path.join(d, "b.json"), "[]"),
                        write_file(os.path.join(d, "c.json"), "{"), os.path.join(d, "none")):
                with self.assertRaises(hd.ConfigError):
                    hd.load_schema(bad)


class TestSchemaGateAction(unittest.TestCase):
    def test_ok(self):
        self.assertEqual(hd.schema_gate_action([], None, None, 100), "ok")

    def test_first_violation_corrects(self):
        self.assertEqual(hd.schema_gate_action(["e"], None, 50, 100), "correct")

    def test_waits_for_rewrite_after_correction(self):
        self.assertEqual(hd.schema_gate_action(["e"], 100, 50, 110), "wait")

    def test_rewritten_but_still_invalid_is_final(self):
        self.assertEqual(hd.schema_gate_action(["e"], 100, 105, 110), "invalid_final")

    def test_never_rewritten_after_settle_is_final(self):
        self.assertEqual(hd.schema_gate_action(["e"], 100, 50, 100 + hd.SCHEMA_SETTLE_SECS),
                         "invalid_final")


class TestSchemaGateInWatcher(TempTaskCase):
    SCHEMA = {"required": ["verdict"]}

    def setUp(self):
        super().setUp()
        self.d = self.make_task("dlg-1", state="running", schema=True)
        write_file(os.path.join(self.d, "schema.json"), json.dumps(self.SCHEMA))
        self.w = hd.Watcher("dlg-1", wait_args("dlg-1"))

    def tick(self):
        with mock.patch.object(hd, "get_pane", return_value={"agent_status": "idle"}), \
             mock.patch.object(hd, "pane_visible", return_value=""), \
             mock.patch.object(hd, "send_line", return_value=True) as m_send, \
             mock.patch("builtins.print"):
            return self.w.tick(), m_send

    def test_valid_result_done_and_recorded(self):
        write_file(os.path.join(self.d, "result.json"), '{"status": "done", "verdict": "ok"}')
        action, m_send = self.tick()
        self.assertEqual(action, "DONE")
        m_send.assert_not_called()
        self.assertTrue(hd.load_manifest(self.d)["schema_valid"])

    def test_violation_sends_one_correction_with_errors(self):
        write_file(os.path.join(self.d, "result.json"), '{"status": "done"}')
        action, m_send = self.tick()
        self.assertEqual(action, "CONTINUE")
        self.assertIn("verdict", m_send.call_args.args[1])
        self.assertTrue(hd.load_manifest(self.d)["schema_corrected_at"])
        action, m_send = self.tick()  # 直後は書き直し待ち。再送しない
        self.assertEqual(action, "CONTINUE")
        m_send.assert_not_called()

    def test_still_invalid_after_rewrite_is_done_with_flag_and_warning(self):
        write_file(os.path.join(self.d, "result.json"), '{"status": "done"}')
        self.tick()
        hd.update_manifest(self.d, lambda m: m.update(schema_corrected_at=time.time() - 5))
        write_file(os.path.join(self.d, "result.json"), '{"status": "done", "x": 1}')
        action, _ = self.tick()
        self.assertEqual(action, "DONE")
        m = hd.load_manifest(self.d)
        self.assertFalse(m["schema_valid"])
        self.assertTrue(m["schema_errors"])

    def test_md_only_result_counts_as_violation(self):
        self.write_result(self.d, "status: done\n")
        action, m_send = self.tick()
        self.assertEqual(action, "CONTINUE")
        self.assertIn("result.json", m_send.call_args.args[1])

    def test_task_without_schema_is_unaffected(self):
        d = self.make_task("dlg-2", state="running")
        self.write_result(d, "status: done\n")
        w = hd.Watcher("dlg-2", wait_args("dlg-2"))
        with mock.patch.object(hd, "get_pane", return_value={"agent_status": "idle"}), \
             mock.patch.object(hd, "pane_visible", return_value=""):
            self.assertEqual(w.tick(), "DONE")


class TestArtifacts(unittest.TestCase):
    def test_looks_like_path(self):
        for yes in ("/abs/x", "./x", "../x", "~/x", "src/a.py", "a.txt"):
            self.assertTrue(hd.looks_like_path(yes), yes)
        for no in ("https://e.com/a.py", "delegate/dlg-1", "PR #12", "feat/x", "text with space.py"):
            self.assertFalse(hd.looks_like_path(no), no)

    def test_resolve_artifacts(self):
        exists = lambda p: p == "/w/src/a.py"
        out = hd.resolve_artifacts(["src/a.py", "src/missing.py", "delegate/x", "/abs/b.txt"],
                                   "/w", exists=exists)
        self.assertEqual(out[0], {"raw": "src/a.py", "path": "/w/src/a.py", "missing": False})
        self.assertEqual(out[1]["missing"], True)
        self.assertEqual(out[2], {"raw": "delegate/x", "path": None, "missing": False})
        self.assertEqual(out[3]["path"], "/abs/b.txt")

    def test_normalizes_dotdot(self):
        out = hd.resolve_artifacts(["../x.py"], "/w/sub", exists=lambda p: True)
        self.assertEqual(out[0]["path"], "/w/x.py")


class TestCollectStructured(TempTaskCase):
    def collect(self, d):
        with mock.patch("builtins.print") as m_print:
            hd.cmd_collect(types.SimpleNamespace(task_id="dlg-1"))
        return "\n".join(str(c.args[0]) for c in m_print.call_args_list if not c.kwargs)

    def test_artifacts_absolute_and_missing_warned(self):
        d = self.make_task("dlg-1", cwd=self.root)
        write_file(os.path.join(self.root, "out.txt"), "x")
        self.write_result(d, "status: done\nsummary: s\nartifacts:\n  - out.txt\n  - gone.txt\n")
        with mock.patch("builtins.print") as m_print:
            hd.cmd_collect(types.SimpleNamespace(task_id="dlg-1"))
        out = "\n".join(str(c.args[0]) for c in m_print.call_args_list)
        self.assertIn(os.path.join(self.root, "out.txt"), out)
        warns = [c for c in m_print.call_args_list if c.kwargs.get("file") is hd.sys.stderr]
        self.assertEqual(len(warns), 1)
        self.assertIn("gone.txt", warns[0].args[0])
        self.assertEqual(hd.load_manifest(d)["artifacts_missing"],
                         [os.path.join(self.root, "gone.txt")])

    def test_json_only_result_is_printed_and_delivered(self):
        d = self.make_task("dlg-1", cwd=self.root)
        write_file(os.path.join(d, "result.json"), '{"status": "done", "summary": "js"}')
        out = self.collect(d)
        self.assertIn('"summary": "js"', out)
        self.assertTrue(hd.load_manifest(d)["delivered_at"])

    def test_no_artifacts_no_section(self):
        d = self.make_task("dlg-1", cwd=self.root)
        self.write_result(d, "status: done\nsummary: s\n")
        self.assertNotIn("artifacts (絶対パス)", self.collect(d))
        self.assertNotIn("artifacts_missing", hd.load_manifest(d))


class TestParentHandoff(TempTaskCase):
    def test_format_empty(self):
        self.assertEqual(hd.format_parent_section([]), "")

    def test_format_section(self):
        text = hd.format_parent_section([
            {"task_id": "dlg-a", "summary": "done it", "artifacts": ["x.py"], "branch": "delegate/dlg-a",
             "diffstat": " x.py | 2 +-"},
            {"task_id": "dlg-b", "summary": "", "artifacts": [], "branch": None, "diffstat": None}])
        self.assertTrue(text.startswith("## Parent task results"))
        self.assertIn("### dlg-a (branch: delegate/dlg-a)", text)
        self.assertIn("x.py | 2 +-", text)
        self.assertIn("### dlg-b\n", text)
        self.assertTrue(text.endswith("---\n\n"))

    def test_parent_info_collects_result_and_diffstat(self):
        repo = os.path.join(self.root, "repo")
        os.makedirs(repo)
        git_cmd(repo, "init", "-q")
        git_cmd(repo, "commit", "-q", "--allow-empty", "-m", "base")
        base = hd.run_git(repo, "rev-parse", "HEAD")
        write_file(os.path.join(repo, "f.txt"), "1\n")
        git_cmd(repo, "add", "f.txt")
        git_cmd(repo, "commit", "-q", "-m", "w")
        d = self.make_task("dlg-par", cwd=repo, base_commit=base, worktree_branch="delegate/dlg-par")
        write_file(os.path.join(d, "result.json"),
                   '{"status": "done", "summary": "ok", "artifacts": ["f.txt"]}')
        info = hd.parent_info("dlg-par")
        self.assertEqual((info["summary"], info["branch"], info["artifacts"]),
                         ("ok", "delegate/dlg-par", ["f.txt"]))
        self.assertIn("f.txt", info["diffstat"])

    def test_parent_without_result_or_base(self):
        self.make_task("dlg-par")
        info = hd.parent_info("dlg-par")
        self.assertEqual((info["summary"], info["diffstat"]), ("(result なし)", None))

    def test_unknown_parent(self):
        with self.assertRaises(hd.ConfigError):
            hd.parent_info("dlg-nope")


class TestBlockedKindOutput(TempTaskCase):
    def wait_blocked(self, result_text=None, status="blocked"):
        d = self.make_task("dlg-1")
        if result_text:
            self.write_result(d, result_text)
        with mock.patch.object(hd, "get_pane", return_value={"agent_status": status}), \
             mock.patch.object(hd, "pane_visible", return_value=""), \
             mock.patch.object(hd, "pane_tail", return_value="t"), \
             mock.patch.object(hd.subprocess, "run"), \
             mock.patch("builtins.print") as m_print, self.assertRaises(SystemExit) as cm:
            hd.cmd_wait(wait_args("dlg-1"))
        return cm.exception.code, json.loads(stdout_calls(m_print)[0].args[0]), d

    def test_kind_and_question_in_output(self):
        code, out, d = self.wait_blocked("status: blocked\nkind: dependency\nquestion: 親待ち\n", "idle")
        self.assertEqual((code, out["kind"], out["question"]), (2, "dependency", "親待ち"))
        events = hd.load_manifest(d)["events"]
        self.assertIn("kind=dependency", events[-1]["detail"])

    def test_missing_kind_defaults_to_needs_input(self):
        _, out, _ = self.wait_blocked("status: blocked\n", "idle")
        self.assertEqual(out["kind"], "needs_input")

    def test_agent_blocked_without_result(self):
        _, out, _ = self.wait_blocked(None, "blocked")
        self.assertEqual(out["kind"], "needs_input")
        self.assertNotIn("question", out)


class TestProtocolViolationWait(TempTaskCase):
    def test_marker_ends_wait_with_failed_code_and_state(self):
        d = self.make_task("dlg-1")
        write_file(os.path.join(d, "protocol_violation"), "{}")
        with mock.patch.object(hd, "get_pane", return_value={"agent_status": "idle"}), \
             mock.patch.object(hd, "pane_visible", return_value=""), \
             mock.patch.object(hd, "pane_tail", return_value="t"), \
             mock.patch("builtins.print") as m_print, self.assertRaises(SystemExit) as cm:
            hd.cmd_wait(wait_args("dlg-1"))
        self.assertEqual(cm.exception.code, 1)
        out = json.loads(stdout_calls(m_print)[0].args[0])
        self.assertEqual((out["status"], out["state"]), ("failed", "protocol_violation"))
        self.assertEqual(hd.load_manifest(d)["state"], "protocol_violation")


# ---------- A2: 複数 wait ----------

class TestWaitSingleTerminal(TempTaskCase):
    def test_terminal_task_reported_from_ledger_even_if_pane_gone(self):
        d = self.make_task("old1", pane_id="p1", state="done")
        self.write_result(d, "status: done\nsummary: ok\n")
        with mock.patch.object(hd, "get_pane", return_value=None), \
             mock.patch("builtins.print") as m_print, self.assertRaises(SystemExit) as cm:
            hd.cmd_wait(wait_args("old1"))
        out = json.loads(m_print.call_args.args[0])
        self.assertEqual((cm.exception.code, out["status"], out["state"]), (0, "done", "done"))


class TestWaitMany(TempTaskCase):
    def run_many(self, ids, panes, **kw):
        args = wait_args(*ids, **kw)
        with mock.patch.object(hd, "get_pane", side_effect=lambda pid: panes.get(pid)), \
             mock.patch.object(hd, "pane_visible", return_value=""), \
             mock.patch.object(hd, "pane_tail", return_value="t"), \
             mock.patch.object(hd.time, "sleep") as m_sleep, \
             mock.patch.object(hd.subprocess, "run"), \
             mock.patch("builtins.print") as m_print, self.assertRaises(SystemExit) as cm:
            hd.cmd_wait(args)
        stdout = [c for c in m_print.call_args_list if not c.kwargs]
        return cm.exception.code, json.loads(stdout[-1].args[0]), m_print, m_sleep

    def two_tasks(self):
        a = self.make_task("dlg-a", pane_id="p1", group="g")
        b = self.make_task("dlg-b", pane_id="p2", group="g")
        return a, b

    def test_all_done_exit_0_single_summary(self):
        a, b = self.two_tasks()
        self.write_result(a, "status: done\nsummary: A ok\nartifacts: x.py\n")
        self.write_result(b, "status: done\nsummary: B ok\n")
        code, out, _, _ = self.run_many(["dlg-a", "dlg-b"],
                                        {"p1": {"agent_status": "idle"}, "p2": {"agent_status": "idle"}})
        self.assertEqual(code, 0)
        self.assertEqual([r["task_id"] for r in out["results"]], ["dlg-a", "dlg-b"])
        self.assertEqual([r["summary"] for r in out["results"]], ["A ok", "B ok"])
        self.assertEqual(out["pending"], [])
        self.assertTrue(out["results"][0]["artifacts"][0].endswith("x.py"))

    def test_waits_for_slow_task_then_reports_first_nonzero(self):
        a, b = self.two_tasks()
        self.write_result(a, "status: failed\nsummary: bad\n")
        self.write_result(b, "status: done\n")
        seq = iter([{"agent_status": "working"}, {"agent_status": "idle"}])
        panes = {"p1": {"agent_status": "idle"}}
        with mock.patch.object(hd, "get_pane",
                               side_effect=lambda pid: panes["p1"] if pid == "p1" else next(seq)), \
             mock.patch.object(hd, "pane_visible", return_value=""), \
             mock.patch.object(hd.time, "sleep"), mock.patch.object(hd.subprocess, "run"), \
             mock.patch("builtins.print") as m_print, self.assertRaises(SystemExit) as cm:
            hd.cmd_wait(wait_args("dlg-a", "dlg-b"))
        self.assertEqual(cm.exception.code, 1)
        out = json.loads(m_print.call_args_list[-1].args[0])
        self.assertEqual([r["exit_code"] for r in out["results"]], [1, 0])

    def test_any_returns_at_first_finished_with_pending(self):
        a, b = self.two_tasks()
        self.write_result(a, "status: done\n")
        code, out, _, _ = self.run_many(
            ["dlg-a", "dlg-b"], {"p1": {"agent_status": "idle"}, "p2": {"agent_status": "working"}},
            any=True)
        self.assertEqual(code, 0)
        self.assertEqual([r["task_id"] for r in out["results"]], ["dlg-a"])
        self.assertEqual(out["pending"], ["dlg-b"])

    def test_group_resolves_from_ledger(self):
        a, b = self.two_tasks()
        self.make_task("dlg-c", pane_id="p3", group="other")
        self.write_result(a, "status: done\n")
        self.write_result(b, "status: done\n")
        code, out, _, _ = self.run_many(
            [], {"p1": {"agent_status": "idle"}, "p2": {"agent_status": "idle"}}, group="g")
        self.assertEqual(sorted(r["task_id"] for r in out["results"]), ["dlg-a", "dlg-b"])

    def test_blocked_prints_immediate_line_to_stderr_and_continues(self):
        a, b = self.two_tasks()
        self.write_result(a, "status: blocked\nkind: capability\nquestion: q\n")
        self.write_result(b, "status: done\n")
        code, out, m_print, _ = self.run_many(
            ["dlg-a", "dlg-b"], {"p1": {"agent_status": "idle"}, "p2": {"agent_status": "idle"}})
        self.assertEqual(code, 2)
        early = [json.loads(c.args[0]) for c in m_print.call_args_list
                 if c.kwargs.get("file") is hd.sys.stderr and c.args[0].startswith("{")]
        self.assertEqual((early[0]["event"], early[0]["kind"]), ("blocked", "capability"))
        self.assertEqual(out["results"][0]["kind"], "capability")

    def test_terminal_tasks_reported_from_ledger_without_observation(self):
        a, b = self.two_tasks()
        hd.update_manifest(a, lambda m: m.update(state="done"))
        self.write_result(a, "status: done\nsummary: earlier\n")
        self.write_result(b, "status: done\n")
        with mock.patch.object(hd, "get_pane", side_effect=lambda pid: (
                None if pid == "p1" else {"agent_status": "idle"})) as m_get, \
             mock.patch.object(hd, "pane_visible", return_value=""), \
             mock.patch.object(hd.time, "sleep"), mock.patch.object(hd.subprocess, "run"), \
             mock.patch("builtins.print") as m_print, self.assertRaises(SystemExit) as cm:
            hd.cmd_wait(wait_args("dlg-a", "dlg-b"))
        self.assertEqual(cm.exception.code, 0)
        self.assertNotIn(mock.call("p1"), m_get.call_args_list)
        out = json.loads(m_print.call_args_list[-1].args[0])
        self.assertEqual(out["results"][0]["summary"], "earlier")

    def test_no_ids_is_config_error(self):
        with self.assertRaises(SystemExit) as cm:
            hd.cmd_wait(wait_args())
        self.assertEqual(cm.exception.code, hd.EXIT_CONFIG)

    def test_single_id_keeps_legacy_output(self):
        d = self.make_task("dlg-a", pane_id="p1")
        self.write_result(d, "status: done\n")
        with mock.patch.object(hd, "get_pane", return_value={"agent_status": "idle"}), \
             mock.patch.object(hd, "pane_visible", return_value=""), \
             mock.patch("builtins.print") as m_print, self.assertRaises(SystemExit):
            hd.cmd_wait(wait_args("dlg-a"))
        out = json.loads(m_print.call_args_list[0].args[0])
        self.assertEqual(out["task_id"], "dlg-a")
        self.assertNotIn("results", out)

    def test_first_nonzero_exit(self):
        self.assertEqual(hd.first_nonzero_exit([{"exit_code": 0}, {"exit_code": 4}, {"exit_code": 2}]), 4)
        self.assertEqual(hd.first_nonzero_exit([{"exit_code": 0}]), 0)
        self.assertEqual(hd.first_nonzero_exit([]), 0)

    def test_group_task_ids(self):
        self.two_tasks()
        self.make_task("dlg-c", group="other")
        self.assertEqual(sorted(hd.group_task_ids("g")), ["dlg-a", "dlg-b"])

    def test_ledger_result_maps_state_to_exit(self):
        d = self.make_task("dlg-a", state="killed")
        r = hd.ledger_result("dlg-a", hd.load_manifest(d), d)
        self.assertEqual((r["exit_code"], r["state"]), (hd.EXIT_GONE, "killed"))



# ---------- A3: 契約 + gate ----------

class TestContractFooter(unittest.TestCase):
    def test_build_contract_drops_blank_and_none_when_empty(self):
        self.assertIsNone(hd.build_contract(None, " ", ""))
        self.assertEqual(hd.build_contract(" pytest ", None, "3回失敗"),
                         {"verify": "pytest", "stop_when": "3回失敗"})

    def test_footer_without_contract_has_no_section(self):
        self.assertNotIn("## Contract", hd.contract_footer("/t/dlg-1"))

    def test_footer_with_contract_lists_fields_and_requires_evidence(self):
        footer = hd.contract_footer("/t/dlg-1", contract={"verify": "pytest -q", "constraints": "x"})
        self.assertIn("## Contract", footer)
        self.assertIn("検証方法: pytest -q", footer)
        self.assertIn("制約: x", footer)
        self.assertNotIn("停止条件", footer)
        self.assertIn("## Verification evidence", footer)


class TestEvidence(TempTaskCase):
    def test_markdown_section_needs_content(self):
        self.assertTrue(hd.markdown_has_evidence("status: done\n## Verification evidence\n$ pytest\n3 passed\n"))
        self.assertTrue(hd.markdown_has_evidence("### verification evidence\nok\n## Other\nx"))

    def test_markdown_empty_or_missing_section(self):
        self.assertFalse(hd.markdown_has_evidence("status: done\nsummary: x\n"))
        self.assertFalse(hd.markdown_has_evidence("## Verification evidence\n\n## Next\nfoo\n"))
        self.assertFalse(hd.markdown_has_evidence("## Verification evidence"))

    def test_result_json_uses_key(self):
        d = self.make_task("dlg-1")
        write_file(os.path.join(d, "result.json"), '{"status": "done", "verification_evidence": "ok"}')
        self.assertTrue(hd.result_has_evidence(d))
        write_file(os.path.join(d, "result.json"), '{"status": "done", "verification_evidence": " "}')
        self.assertFalse(hd.result_has_evidence(d))


class TestGatePure(unittest.TestCase):
    def test_tail_bytes_keeps_end_and_valid_utf8(self):
        self.assertEqual(hd.tail_bytes("a" * 5000, 3072), "a" * 3072)
        out = hd.tail_bytes("あ" * 2000, 3072)  # 3byte 文字の途中で切れても壊れない
        self.assertLessEqual(len(out.encode()), 3072)
        self.assertTrue(out.endswith("あ"))

    def test_verdict_counts_total_failures(self):
        self.assertEqual(hd.gate_verdict(1, 3), "retry")
        self.assertEqual(hd.gate_verdict(2, 3), "retry")
        self.assertEqual(hd.gate_verdict(3, 3), "exhausted")
        self.assertEqual(hd.gate_verdict(1, 1), "exhausted")

    def test_feedback_mentions_cmd_rc_and_tail(self):
        msg = hd.gate_feedback("make test", 2, "x" * 9000 + "FAILED_END", 1, 3)
        self.assertIn("1/3", msg)
        self.assertIn("`make test`", msg)
        self.assertIn("終了コード 2", msg)
        self.assertIn("FAILED_END", msg)
        self.assertLess(len(msg.encode()), 3072 + 400)
        self.assertIn("タイムアウト", hd.gate_feedback("c", hd.GATE_TIMEOUT_RC, "", 1, 3))

    def test_first_failure_stops_at_first_and_none_when_all_pass(self):
        calls = []

        def run(cmd, cwd, timeout):
            calls.append(cmd)
            return (1, "boom") if cmd == "b" else (0, "")
        self.assertEqual(hd.first_gate_failure(["a", "b", "c"], "/x", 5, run), ("b", 1, "boom"))
        self.assertEqual(calls, ["a", "b"])
        self.assertIsNone(hd.first_gate_failure(["a"], "/x", 5, run))
        self.assertIsNone(hd.first_gate_failure([], "/x", 5, run))

    def test_run_gate_real_commands(self):
        with tempfile.TemporaryDirectory() as d:
            rc, out = hd.run_gate("echo hi; echo err >&2; pwd", d, 10)
            self.assertEqual(rc, 0)
            self.assertIn("hi", out)
            self.assertIn("err", out)
            self.assertIn(os.path.basename(d), out)
            self.assertEqual(hd.run_gate("exit 3", d, 10)[0], 3)

    def test_run_gate_timeout(self):
        rc, out = hd.run_gate("sleep 5", "/", 0.2)
        self.assertEqual(rc, hd.GATE_TIMEOUT_RC)
        self.assertIn("timeout", out)

    def test_run_gate_timeout_kills_children(self):
        rc, out = hd.run_gate("sleep 37 & echo pid=$!; wait", "/", 1)
        self.assertEqual(rc, hd.GATE_TIMEOUT_RC)
        pid = int(re.search(r"pid=(\d+)", out).group(1))
        for _ in range(20):  # SIGKILL 後の回収待ち
            try:
                os.kill(pid, 0)
            except ProcessLookupError:
                return
            time.sleep(0.1)
        os.kill(pid, signal.SIGKILL)
        self.fail("gate の子プロセスが生き残った")

    def test_clamp_gate_timeout(self):
        self.assertEqual(hd.clamp_gate_timeout(300, None, 100), 300)
        self.assertEqual(hd.clamp_gate_timeout(300, 160, 100), 60)
        self.assertEqual(hd.clamp_gate_timeout(30, 1000, 100), 30)
        self.assertEqual(hd.clamp_gate_timeout(300, 90, 100), 1)  # 期限超過でも最低 1 秒


class TestDecideLimited(unittest.TestCase):
    def test_limited_only_without_result_and_beats_nudge(self):
        self.assertEqual(hd.decide("idle", None, 0, 0, limited=True), "LIMITED")
        self.assertEqual(hd.decide("blocked", None, 0, 0, limited=True), "LIMITED")
        self.assertEqual(hd.decide("idle", "done", 0, 0, limited=True), "DONE")

    def test_limited_beats_timeout_but_not_pane_gone(self):
        self.assertEqual(hd.decide("idle", None, 0, 0, timed_out=True, limited=True), "LIMITED")
        self.assertEqual(hd.decide(None, None, 0, 0, limited=True), "PANE_GONE")

    def test_outcome_is_exit_7(self):
        self.assertEqual(hd.OUTCOMES["LIMITED"], ("limited", 7))
        self.assertEqual(hd.OUTCOMES["GATE_FAILED"], ("failed", 1))
        self.assertEqual(hd.STATE_EXIT["limited"], 7)


class GateWatcherCase(TempTaskCase):
    def setUp(self):
        super().setUp()
        self.d = self.make_task("dlg-1", state="running", **self.manifest_extra())

    def manifest_extra(self):
        return {"cwd": self.root}

    def tick(self, w=None, send_ok=True):
        w = w or hd.Watcher("dlg-1", wait_args("dlg-1"))
        with mock.patch.object(hd, "get_pane", return_value={"agent_status": "idle"}), \
             mock.patch.object(hd, "pane_visible", return_value=""), \
             mock.patch.object(hd, "send_line", return_value=send_ok) as m_send, \
             mock.patch("builtins.print"):
            return w, w.tick(), m_send


class TestContractEvidenceInWatcher(GateWatcherCase):
    def manifest_extra(self):
        return {"cwd": self.root, "contract": {"verify": "pytest"}}

    def test_done_without_evidence_is_blocked_needs_input(self):
        self.write_result(self.d, "status: done\nsummary: x\n")
        w, action, _ = self.tick()
        self.assertEqual(action, "BLOCKED")
        self.assertIn("Verification evidence", w.forced_block)
        with mock.patch.object(hd, "notify"), mock.patch.object(hd, "snapshot_post"), \
             mock.patch("builtins.print"):
            out, code = hd.record_outcome(w, action)
        self.assertEqual((out["state"], out["kind"], code), ("blocked", "needs_input", 2))
        self.assertIn("Verification evidence", out["question"])

    def test_done_with_evidence_passes(self):
        self.write_result(self.d, "status: done\nsummary: x\n## Verification evidence\n3 passed\n")
        self.assertEqual(self.tick()[1], "DONE")

    def test_blocked_result_keeps_its_own_kind(self):
        self.write_result(self.d, "status: blocked\nkind: capability\nquestion: q\n")
        w, action, _ = self.tick()
        self.assertEqual(action, "BLOCKED")
        self.assertIsNone(w.forced_block)


class TestCommandGatesInWatcher(GateWatcherCase):
    def manifest_extra(self):
        return {"cwd": self.root, "gates": ["exit 1"], "gate_retries": 2, "gate_timeout": 10}

    def test_failure_sends_back_archives_result_and_continues(self):
        self.write_result(self.d, "status: done\n")
        w, action, m_send = self.tick()
        self.assertEqual(action, "CONTINUE")
        self.assertIn("exit 1", m_send.call_args.args[1])
        self.assertFalse(os.path.exists(os.path.join(self.d, "result.md")))
        self.assertTrue(os.path.exists(os.path.join(self.d, "result.1.md")))
        self.assertEqual(hd.load_manifest(self.d)["gate_failures"], 1)

    def test_retries_exhausted_is_gate_failed_with_reason(self):
        self.write_result(self.d, "status: done\n")
        w, action, _ = self.tick()
        self.write_result(self.d, "status: done\n")  # worker が再度 done を主張
        w, action, m_send = self.tick(w)
        self.assertEqual(action, "GATE_FAILED")
        m_send.assert_not_called()
        with mock.patch.object(hd, "notify") as m_notify, mock.patch.object(hd, "snapshot_post"), \
             mock.patch("builtins.print"):
            out, code = hd.record_outcome(w, action)
        self.assertEqual((out["state"], out["status"], out["reason"], code),
                         ("failed", "failed", "gate_failed", 1))
        self.assertEqual(out["gate"]["cmd"], "exit 1")
        m_notify.assert_called_once()
        m = hd.load_manifest(self.d)
        self.assertEqual((m["state"], m["exit_reason"]), ("failed", "gate_failed"))

    def test_passing_gate_is_done(self):
        hd.update_manifest(self.d, lambda m: m.update(gates=["true", "echo ok"]))
        self.write_result(self.d, "status: done\n")
        w, action, m_send = self.tick()
        self.assertEqual(action, "DONE")
        m_send.assert_not_called()

    def test_gate_runs_in_manifest_cwd(self):
        marker = os.path.join(self.root, "ran-here")
        hd.update_manifest(self.d, lambda m: m.update(gates=["touch ran-here"]))
        self.write_result(self.d, "status: done\n")
        self.assertEqual(self.tick()[1], "DONE")
        self.assertTrue(os.path.exists(marker))

    def test_gates_not_run_when_result_not_done(self):
        self.write_result(self.d, "status: failed\n")
        self.assertEqual(self.tick()[1], "FAILED_RESULT")

    def test_send_failure_keeps_result_and_retries_via_next_tick(self):
        self.write_result(self.d, "status: done\n")
        w, action, _ = self.tick(send_ok=False)
        self.assertEqual(action, "CONTINUE")
        self.assertTrue(os.path.exists(os.path.join(self.d, "result.md")))

    def test_evidence_checked_before_gates(self):
        hd.update_manifest(self.d, lambda m: m.update(contract={"verify": "x"}))
        self.write_result(self.d, "status: done\n")
        w, action, m_send = self.tick()
        self.assertEqual(action, "BLOCKED")
        m_send.assert_not_called()


class TestCheckGateArgs(unittest.TestCase):
    def test_rejects_non_positive(self):
        ok = types.SimpleNamespace(gate_retries=3, gate_timeout=300)
        hd.check_gate_args(ok)
        for kw in (dict(gate_retries=0), dict(gate_timeout=0)):
            with self.assertRaises(hd.ConfigError):
                hd.check_gate_args(types.SimpleNamespace(**{**vars(ok), **kw}))


class TestCreateTaskContract(CreateTaskCase):
    def test_contract_gate_and_fallback_recorded_and_in_prompt(self):
        tid, m, _ = self.create(verify="pytest", stop_when="3回失敗", gate=["make lint"],
                                gate_retries=2, gate_timeout=60, fallback_from="claude")
        self.assertEqual(m["contract"], {"verify": "pytest", "stop_when": "3回失敗"})
        self.assertEqual((m["gates"], m["gate_retries"], m["gate_timeout"], m["fallback_from"]),
                         (["make lint"], 2, 60, "claude"))
        with open(os.path.join(self.tasks, tid, "prompt.md")) as f:
            self.assertIn("## Contract", f.read())

    def test_no_contract_means_none_and_no_gates(self):
        tid, m, _ = self.create()
        self.assertIsNone(m["contract"])
        self.assertEqual(m["gates"], [])
        self.assertIsNone(m["snapshot_pre"])  # 非 git の tempdir


# ---------- A3: LIMITED 検出 / health ----------
# 注意: 以下の枠切れ文言は想定 (実機未採取)。実画面と違えば hd.LIMIT_RES を直し fixture も更新する。

ASSUMED_LIMIT_SCREENS = {
    "claude_hit": "some output\nYou've hit your usage limit · resets at 3pm (Asia/Tokyo)\n",
    "claude_5h": "5-hour limit reached ∙ resets 7pm\n",
    "codex_usage": "■ You've hit your usage limit. Upgrade to Pro or try again at 15:30.\n",
    "generic_reached": "Error: usage limit reached. Your limit resets at 09:00.\n",
    "rate_exceeded": "API Error: rate limit exceeded, resets in 2 hours\n",
}
NOT_LIMIT_SCREENS = {
    "prose": "I added a rate limiter to the API; the usage limit config lives in settings.py\n",
    "resets_only": "The counter resets at midnight when the job runs.\n",
    "normal": "● Done. All tests pass.\n> \n",
    "empty": "",
}


class TestDetectLimit(unittest.TestCase):
    NOW = time.mktime((2026, 7, 10, 12, 0, 0, 0, 0, -1))

    def test_assumed_limit_screens_detected(self):
        for name, text in ASSUMED_LIMIT_SCREENS.items():
            with self.subTest(name):
                info = hd.detect_limit(text, self.NOW)
                self.assertIsNotNone(info)
                self.assertTrue(info["reason"])

    def test_non_limit_screens_not_detected(self):
        for name, text in NOT_LIMIT_SCREENS.items():
            with self.subTest(name):
                self.assertIsNone(hd.detect_limit(text, self.NOW))

    def test_only_last_20_lines_are_examined(self):
        old = "You've hit your usage limit\n" + "line\n" * 25
        self.assertIsNone(hd.detect_limit(old, self.NOW))
        self.assertIsNotNone(hd.detect_limit("line\n" * 25 + "You've hit your usage limit\n", self.NOW))

    def test_reason_is_trimmed_matching_line(self):
        info = hd.detect_limit("x\n   5-hour limit reached ∙ resets 7pm   \ny\n", self.NOW)
        self.assertEqual(info["reason"], "5-hour limit reached ∙ resets 7pm")

    def test_until_parsed_when_present(self):
        info = hd.detect_limit(ASSUMED_LIMIT_SCREENS["claude_hit"], self.NOW)
        self.assertEqual(time.localtime(info["until"])[3:5], (15, 0))
        self.assertIsNone(hd.detect_limit("limit reached\n", self.NOW)["until"])


class TestParseResetEpoch(unittest.TestCase):
    NOW = time.mktime((2026, 7, 10, 12, 0, 0, 0, 0, -1))

    def hm(self, text):
        e = hd.parse_reset_epoch(text, self.NOW)
        return e and time.localtime(e)[2:5]  # (日, 時, 分)

    def test_12h_and_24h_forms(self):
        self.assertEqual(self.hm("resets at 3pm"), (10, 15, 0))
        self.assertEqual(self.hm("resets 7pm"), (10, 19, 0))
        self.assertEqual(self.hm("resets at 15:30"), (10, 15, 30))
        self.assertEqual(self.hm("resets at 12am"), (11, 0, 0))   # 過ぎた時刻は翌日
        self.assertEqual(self.hm("resets at 9:00"), (11, 9, 0))

    def test_relative_form(self):
        self.assertEqual(hd.parse_reset_epoch("resets in 2 hours", self.NOW), self.NOW + 7200)
        self.assertEqual(hd.parse_reset_epoch("resets in 45 minutes", self.NOW), self.NOW + 2700)

    def test_unparseable_is_none(self):
        for text in ("resets at 3", "resets soon", "resets at 13pm", "resets at 25:00", ""):
            with self.subTest(text):
                self.assertIsNone(hd.parse_reset_epoch(text, self.NOW))


class TestHealthPure(unittest.TestCase):
    def test_active_limits_drops_expired_and_garbage(self):
        health = {"claude": {"until": 200, "reason": "r"}, "codex": {"until": 50, "reason": "r"},
                  "x": "bad"}
        self.assertEqual(list(hd.active_limits(health, 100)), ["claude"])

    def test_with_limit_uses_until_or_fallback_and_prunes(self):
        h = hd.with_limit({"codex": {"until": 1, "reason": "old"}}, "claude",
                          {"until": None, "reason": "r"}, 1000)
        self.assertEqual(h, {"claude": {"until": 1000 + hd.LIMIT_FALLBACK_SECS, "reason": "r"}})
        h = hd.with_limit({}, "claude", {"until": 5000, "reason": "r"}, 1000)
        self.assertEqual(h["claude"]["until"], 5000)

    def test_pick_agent_explicit_is_untouched(self):
        health = {"claude": {"until": 10**12, "reason": "r"}}
        self.assertEqual(hd.pick_agent("claude", health, 0), ("claude", None))

    def test_pick_agent_auto_prefers_claude_then_codex(self):
        self.assertEqual(hd.pick_agent("auto", {}, 0), ("claude", None))
        health = {"claude": {"until": 100, "reason": "r"}}
        self.assertEqual(hd.pick_agent("auto", health, 0), ("codex", "claude"))
        self.assertEqual(hd.pick_agent("auto", health, 200), ("claude", None))  # 期限切れで復帰

    def test_pick_agent_auto_all_limited_or_unavailable(self):
        both = {"claude": {"until": 100, "reason": "r"}, "codex": {"until": 100, "reason": "r"}}
        self.assertEqual(hd.pick_agent("auto", both, 0), (None, None))
        self.assertEqual(hd.pick_agent("auto", {}, 0, available=lambda c: c == "codex"),
                         ("codex", "claude"))


class TestHealthIO(TempTaskCase):
    def test_load_missing_or_corrupt_is_empty(self):
        self.assertEqual(hd.load_health(), {})
        write_file(hd.health_path(), "{not json")
        self.assertEqual(hd.load_health(), {})
        write_file(hd.health_path(), "[]")
        self.assertEqual(hd.load_health(), {})

    def test_save_roundtrip_beside_tasks_dir(self):
        hd.save_health({"claude": {"until": 1, "reason": "r"}})
        self.assertEqual(hd.health_path(), os.path.join(self.root, "health.json"))
        self.assertEqual(hd.load_health()["claude"]["reason"], "r")

    def test_resolve_agent_auto_falls_back_and_errors(self):
        hd.save_health({"claude": {"until": time.time() + 600, "reason": "r"}})
        with mock.patch.object(hd.shutil, "which", return_value="/bin/x"):
            self.assertEqual(hd.resolve_agent("auto"), ("codex", "claude"))
            self.assertEqual(hd.resolve_agent("claude"), ("claude", None))
            hd.save_health({c: {"until": time.time() + 600, "reason": "r"} for c in ("claude", "codex")})
            with self.assertRaises(hd.ConfigError):
                hd.resolve_agent("auto")

    def health_args(self, **kw):
        return types.SimpleNamespace(**{"reset": False, "cli": None, **kw})

    def test_cmd_health_lists_and_resets(self):
        until = time.time() + 600
        hd.save_health({"claude": {"until": until, "reason": "hit"},
                        "codex": {"until": until, "reason": "hit2"}})
        with mock.patch("builtins.print") as m_print:
            hd.cmd_health(self.health_args())
        self.assertIn("claude", m_print.call_args_list[0].args[0])
        with mock.patch("builtins.print"):
            hd.cmd_health(self.health_args(reset=True, cli="claude"))
        self.assertEqual(list(hd.load_health()), ["codex"])
        with mock.patch("builtins.print") as m_print:
            hd.cmd_health(self.health_args(reset=True))
        self.assertEqual(hd.load_health(), {})
        self.assertIn("正常", m_print.call_args.args[0])


class TestLimitedInWatcher(GateWatcherCase):
    def tick_with_tail(self, tail, status="idle"):
        w = hd.Watcher("dlg-1", wait_args("dlg-1"))
        with mock.patch.object(hd, "get_pane", return_value={"agent_status": status}), \
             mock.patch.object(hd, "pane_visible", return_value=""), \
             mock.patch.object(hd, "pane_tail", return_value=tail), \
             mock.patch.object(hd, "nudge"):
            return w, w.tick()

    def test_idle_with_limit_screen_is_limited(self):
        w, action = self.tick_with_tail(ASSUMED_LIMIT_SCREENS["claude_hit"])
        self.assertEqual(action, "LIMITED")
        self.assertIn("usage limit", w.limit_info["reason"])

    def test_blocked_with_limit_screen_is_limited(self):
        self.assertEqual(self.tick_with_tail(ASSUMED_LIMIT_SCREENS["codex_usage"], "blocked")[1], "LIMITED")

    def test_idle_normal_screen_still_nudges(self):
        self.assertEqual(self.tick_with_tail("● working on it\n")[1], "NUDGE")

    def test_result_present_skips_limit_check(self):
        self.write_result(self.d, "status: done\n")
        self.assertEqual(self.tick_with_tail(ASSUMED_LIMIT_SCREENS["claude_hit"])[1], "DONE")

    def test_working_never_checks_tail(self):
        w = hd.Watcher("dlg-1", wait_args("dlg-1"))
        with mock.patch.object(hd, "get_pane", return_value={"agent_status": "working"}), \
             mock.patch.object(hd, "pane_visible", return_value=""), \
             mock.patch.object(hd, "pane_tail") as m_tail:
            self.assertEqual(w.tick(), "CONTINUE")
        m_tail.assert_not_called()

    def test_record_outcome_writes_health_exit_7_and_resumable_state(self):
        w, action = self.tick_with_tail(ASSUMED_LIMIT_SCREENS["claude_hit"])
        with mock.patch.object(hd, "notify") as m_notify, mock.patch.object(hd, "snapshot_post"), \
             mock.patch("builtins.print"):
            out, code = hd.record_outcome(w, action)
        self.assertEqual((out["state"], code), ("limited", hd.EXIT_LIMITED))
        self.assertIn("claude", hd.load_health())
        self.assertGreater(hd.load_health()["claude"]["until"], time.time())
        m_notify.assert_called_once()
        self.assertNotIn(hd.effective_state(hd.load_manifest(self.d)), hd.TERMINAL_STATES)


# ---------- A3: pane 表示 ----------

class TestPaneMetadata(unittest.TestCase):
    def test_args_with_and_without_title(self):
        self.assertEqual(
            hd.metadata_args("w1:p1", "blocked", "my job"),
            ["pane", "report-metadata", "w1:p1", "--source", "herdr-delegate",
             "--custom-status", "blocked", "--title", "my job"])
        self.assertNotIn("--title", hd.metadata_args("w1:p1", "done"))

    def test_report_ignores_failures(self):
        for exc in (OSError("no herdr"), subprocess.TimeoutExpired("herdr", 1)):
            with mock.patch.object(hd.subprocess, "run", side_effect=exc):
                hd.report_pane_state("w1:p1", "done", "t")  # 例外を出さない

    def test_report_invokes_herdr_cli(self):
        with mock.patch.object(hd.subprocess, "run") as m_run:
            hd.report_pane_state("w1:p1", "running", "t")
        self.assertEqual(m_run.call_args.args[0][:3], ["herdr", "pane", "report-metadata"])


class TestSetStateReportsPane(TempTaskCase):
    def test_state_change_reports_with_label_then_task_id(self):
        d = self.make_task("dlg-1", state="running", label="my job")
        hd.set_state(d, "blocked", "x")
        self.m_report.assert_called_once_with("w1:p1", "blocked", "my job")
        self.m_report.reset_mock()
        d2 = self.make_task("dlg-2", state="running")
        hd.set_state(d2, "stalled", "x")
        self.m_report.assert_called_once_with("w1:p1", "stalled", "dlg-2")

    def test_no_change_gone_and_killed_do_not_report(self):
        d = self.make_task("dlg-1", state="running")
        hd.set_state(d, "running")
        hd.set_state(d, "gone")
        self.m_report.assert_not_called()
        d2 = self.make_task("dlg-2", state="running")
        hd.set_state(d2, "killed")
        self.m_report.assert_not_called()


class TestResendRefactor(TempTaskCase):
    def test_resend_archives_sends_and_runs_again(self):
        d = self.make_task("dlg-1", state="done")
        self.write_result(d, "status: done\n")
        with mock.patch.object(hd, "send_line", return_value=True) as m_send:
            self.assertTrue(hd.resend(d, "w1:p1", "fix it"))
        m_send.assert_called_once_with("w1:p1", "fix it")
        self.assertTrue(os.path.exists(os.path.join(d, "result.1.md")))
        self.assertEqual(hd.effective_state(hd.load_manifest(d)), "running")

    def test_resend_restores_result_when_echo_fails(self):
        d = self.make_task("dlg-1", state="running")
        self.write_result(d, "status: done\n")
        with mock.patch.object(hd, "send_line", return_value=False):
            self.assertFalse(hd.resend(d, "w1:p1", "x"))
        self.assertTrue(os.path.exists(os.path.join(d, "result.md")))


# ---------- A3: snapshot / diff / restore ----------

class TestSnapshotPure(unittest.TestCase):
    def test_parse_name_status_z(self):
        self.assertEqual(hd.parse_name_status_z("M\0a.txt\0A\0dir/b c.txt\0D\0gone\0"),
                         [("M", "a.txt"), ("A", "dir/b c.txt"), ("D", "gone")])
        self.assertEqual(hd.parse_name_status_z(""), [])

    def test_plan_restore_all_and_only(self):
        changes = [("M", "a"), ("A", "b"), ("D", "c")]
        self.assertEqual(hd.plan_restore(changes),
                         {"restore": ["a", "c"], "remove": ["b"], "unknown": []})
        self.assertEqual(hd.plan_restore(changes, ["b", "zzz"]),
                         {"restore": [], "remove": ["b"], "unknown": ["zzz"]})

    def test_repo_relative(self):
        with tempfile.TemporaryDirectory() as root:
            os.makedirs(os.path.join(root, "sub"))
            self.assertEqual(hd.repo_relative(root, os.path.join(root, "sub"), "f.txt"), "sub/f.txt")
            self.assertEqual(hd.repo_relative(root, os.path.join(root, "sub"), "../g"), "g")

    def test_ref_names(self):
        self.assertEqual(hd.SNAP_REF.format("dlg-1", "pre"), "refs/herdr-delegate/dlg-1/pre")


class SnapshotRepoCase(TempTaskCase):
    """実 git repo (非 worktree) で pre → worker 変更 → post/diff/restore を通す."""

    def setUp(self):
        super().setUp()
        self.repo = os.path.join(self.root, "repo")
        os.makedirs(self.repo)
        git_cmd(self.repo, "init", "-q")
        write_file(os.path.join(self.repo, "keep.txt"), "keep\n")
        write_file(os.path.join(self.repo, "edit.txt"), "orig\n")
        write_file(os.path.join(self.repo, "del.txt"), "to delete\n")
        git_cmd(self.repo, "add", "-A")
        git_cmd(self.repo, "commit", "-q", "-m", "base")
        write_file(os.path.join(self.repo, "dirty.txt"), "user wip\n")  # spawn 前から未追跡
        self.d = self.make_task("dlg-1", state="done", cwd=self.repo)
        self.pre = hd.take_snapshot(self.repo, "dlg-1", "pre")
        hd.update_manifest(self.d, lambda m: m.update(snapshot_pre=self.pre))

    def worker_edits(self):
        write_file(os.path.join(self.repo, "edit.txt"), "changed\n")
        write_file(os.path.join(self.repo, "new.txt"), "created\n")
        os.remove(os.path.join(self.repo, "del.txt"))

    def read(self, name):
        with open(os.path.join(self.repo, name)) as f:
            return f.read()

    def args(self, **kw):
        return types.SimpleNamespace(**{"task_id": "dlg-1", "file": [], "stat": False,
                                        "keep_snapshot": False, **kw})


class TestSnapshotGit(SnapshotRepoCase):
    def test_pre_is_commit_with_untracked_and_does_not_touch_real_index(self):
        self.assertTrue(self.pre)
        self.assertEqual(hd.run_git(self.repo, "show", f"{self.pre}:dirty.txt"), "user wip")
        self.assertEqual(hd.run_git(self.repo, "status", "--porcelain"), "?? dirty.txt")
        self.assertEqual(hd.snapshot_sha(self.repo, "dlg-1", "pre"), self.pre)

    def test_snapshot_pre_only_for_non_worktree_git(self):
        pane = {"cwd": self.repo}
        ns = types.SimpleNamespace
        self.assertTrue(hd.snapshot_pre(ns(worktree=None), pane, "dlg-2"))
        self.assertIsNone(hd.snapshot_pre(ns(worktree=""), pane, "dlg-3"))
        self.assertIsNone(hd.snapshot_pre(ns(worktree=None), {"cwd": self.root}, "dlg-4"))

    def test_snapshot_post_needs_pre_in_manifest(self):
        self.worker_edits()
        hd.snapshot_post(self.d)
        self.assertTrue(hd.snapshot_sha(self.repo, "dlg-1", "post"))
        d2 = self.make_task("dlg-2", state="done", cwd=self.repo)
        hd.snapshot_post(d2)
        self.assertIsNone(hd.snapshot_sha(self.repo, "dlg-2", "post"))

    def test_delete_refs_removes_all_names(self):
        hd.take_snapshot(self.repo, "dlg-1", "post")
        hd.delete_snapshot_refs(self.repo, "dlg-1")
        self.assertEqual(hd.run_git(self.repo, "for-each-ref", "refs/herdr-delegate/"), "")

    def test_take_snapshot_fails_softly_outside_git(self):
        self.assertIsNone(hd.take_snapshot(self.root, "dlg-1", "pre"))


class TestCmdDiffRestore(SnapshotRepoCase):
    def run_cmd(self, fn, **kw):
        with mock.patch("builtins.print") as m_print:
            fn(self.args(**kw))
        return m_print.call_args.args[0]

    def test_diff_shows_worker_changes_only(self):
        self.worker_edits()
        out = self.run_cmd(hd.cmd_diff)
        self.assertIn("edit.txt", out)
        self.assertIn("new.txt", out)
        self.assertIn("del.txt", out)
        self.assertNotIn("dirty.txt", out)  # spawn 前からあった変更は含まない
        self.assertIn("3 files changed", self.run_cmd(hd.cmd_diff, stat=True))

    def test_diff_without_snapshot_is_config_error(self):
        self.make_task("dlg-2", state="done", cwd=self.repo)
        with self.assertRaises(hd.ConfigError):
            hd.cmd_diff(self.args(task_id="dlg-2"))

    def test_restore_all_reverts_only_worker_files_and_takes_backup(self):
        self.worker_edits()
        hd.snapshot_post(self.d)
        write_file(os.path.join(self.repo, "keep.txt"), "edited after worker\n")  # post 後のユーザー編集
        out = json.loads(self.run_cmd(hd.cmd_restore))
        self.assertEqual(sorted(out["restored"]), ["del.txt", "edit.txt"])
        self.assertEqual(out["removed"], ["new.txt"])
        self.assertEqual(self.read("edit.txt"), "orig\n")
        self.assertEqual(self.read("del.txt"), "to delete\n")
        self.assertFalse(os.path.exists(os.path.join(self.repo, "new.txt")))
        self.assertEqual(self.read("keep.txt"), "edited after worker\n")  # 触っていない
        self.assertEqual(self.read("dirty.txt"), "user wip\n")
        self.assertEqual(hd.snapshot_sha(self.repo, "dlg-1", "pre-restore"), out["pre_restore"])
        self.assertEqual(hd.run_git(self.repo, "show", f"{out['pre_restore']}:edit.txt"), "changed")
        self.assertEqual(hd.run_git(self.repo, "diff", "--cached", "--name-only"), "")  # index 不変

    def test_restore_single_file_with_cwd_relative_path(self):
        self.worker_edits()
        hd.snapshot_post(self.d)
        out = json.loads(self.run_cmd(hd.cmd_restore, file=["edit.txt"]))
        self.assertEqual((out["restored"], out["removed"]), (["edit.txt"], []))
        self.assertEqual(self.read("edit.txt"), "orig\n")
        self.assertEqual(self.read("new.txt"), "created\n")  # 他は触らない

    def test_restore_unknown_file_aborts_without_changes(self):
        self.worker_edits()
        with self.assertRaises(SystemExit), mock.patch("builtins.print"):
            hd.cmd_restore(self.args(file=["keep.txt"]))
        self.assertEqual(self.read("edit.txt"), "changed\n")
        self.assertIsNone(hd.snapshot_sha(self.repo, "dlg-1", "pre-restore"))

    def test_restore_aborts_when_diff_fails(self):
        self.worker_edits()
        real = hd.run_git
        fake = lambda root, *a, **kw: None if "--name-status" in a else real(root, *a, **kw)
        with mock.patch.object(hd, "run_git", side_effect=fake), \
             self.assertRaises(SystemExit) as cm, mock.patch("builtins.print"):
            hd.cmd_restore(self.args())
        self.assertEqual(cm.exception.code, hd.EXIT_FAILED)
        self.assertEqual(self.read("edit.txt"), "changed\n")
        self.assertIsNone(hd.snapshot_sha(self.repo, "dlg-1", "pre-restore"))

    def test_restore_refuses_running_task(self):
        hd.update_manifest(self.d, lambda m: m.update(state="running"))
        self.worker_edits()
        with self.assertRaises(SystemExit), mock.patch("builtins.print"):
            hd.cmd_restore(self.args())
        self.assertEqual(self.read("edit.txt"), "changed\n")

    def test_restore_without_post_uses_live_tree(self):
        self.worker_edits()
        out = json.loads(self.run_cmd(hd.cmd_restore))  # post ref なし (kill 直後など)
        self.assertEqual(out["removed"], ["new.txt"])
        self.assertEqual(self.read("edit.txt"), "orig\n")


class TestKillSnapshots(SnapshotRepoCase):
    def test_gc_on_kill_by_default(self):
        hd.release_snapshots(self.args(), hd.load_manifest(self.d))
        self.assertIsNone(hd.snapshot_sha(self.repo, "dlg-1", "pre"))

    def test_keep_snapshot_preserves_pre_and_saves_post(self):
        self.worker_edits()
        hd.release_snapshots(self.args(keep_snapshot=True), hd.load_manifest(self.d))
        self.assertEqual(hd.snapshot_sha(self.repo, "dlg-1", "pre"), self.pre)
        self.assertTrue(hd.snapshot_sha(self.repo, "dlg-1", "post"))

    def test_task_without_snapshot_is_noop(self):
        d2 = self.make_task("dlg-2", cwd=self.repo)
        hd.release_snapshots(self.args(), hd.load_manifest(d2))
        self.assertEqual(hd.snapshot_sha(self.repo, "dlg-1", "pre"), self.pre)


class TestCreateTaskSnapshot(SnapshotRepoCase):
    def test_pre_snapshot_recorded_in_git_repo_and_gc_on_start_failure(self):
        pane = {"pane_id": "w1:p9", "tab_id": "t", "workspace_id": "w1", "cwd": self.repo,
                "worktree_path": None, "worktree_branch": None}
        args = types.SimpleNamespace(
            agent="claude", model=None, worktree=None, cwd=self.repo, label="job", safe=False,
            idempotency_key=None, prompt="x", prompt_file=None, schema=None, parent=None,
            group=None, max_concurrent=None, no_deny_floor=False, verify=None, constraints=None,
            stop_when=None, gate=None, gate_timeout=300, gate_retries=3, fallback_from=None)
        with mock.patch.object(hd, "open_pane", return_value=pane), \
             mock.patch.object(hd, "start_worker"), mock.patch("builtins.print"):
            tid = hd.create_task(args, "x", self.repo)
        m = hd.load_manifest(os.path.join(self.tasks, tid))
        self.assertEqual(hd.snapshot_sha(self.repo, tid, "pre"), m["snapshot_pre"])
        with mock.patch.object(hd, "open_pane", return_value=pane), \
             mock.patch.object(hd, "start_worker", side_effect=RuntimeError("boom")), \
             mock.patch.object(hd, "hcli"), mock.patch("builtins.print"):
            with self.assertRaises(RuntimeError):
                hd.create_task(args, "x", self.repo)
        refs = hd.run_git(self.repo, "for-each-ref", "--format=%(refname)", "refs/herdr-delegate/")
        self.assertIn(f"refs/herdr-delegate/{tid}/pre", refs.splitlines())
        self.assertEqual(len([r for r in refs.splitlines() if r.endswith("/pre")]), 2)  # dlg-1 + 成功分のみ


if __name__ == "__main__":
    unittest.main()
