import importlib.machinery
import importlib.util
import os
import tempfile
import types
import unittest
from unittest import mock

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

    @staticmethod
    def _args(task_id):
        return types.SimpleNamespace(task_id=task_id, timeout=None)

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
        with mock.patch.object(hd, "task_dir_of", return_value="/fake/task/dir"), \
             mock.patch.object(hd, "load_manifest", return_value={"pane_id": "w1:p1"}), \
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
        with mock.patch.object(hd, "task_dir_of", return_value="/fake/task/dir"), \
             mock.patch.object(hd, "load_manifest", return_value={"pane_id": "w1:p1"}), \
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


if __name__ == "__main__":
    unittest.main()
