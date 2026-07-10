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
