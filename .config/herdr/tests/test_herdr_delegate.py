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


if __name__ == "__main__":
    unittest.main()
