"""instabox_tick 离线单测：不要凭证、不调 API。仓库根目录跑 python3 -m unittest discover -s tests"""
import os
import sys
import tempfile
import unittest
from datetime import datetime, timezone

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "scripts"))
import instabox_tick as t  # noqa: E402

NOW = 1_800_000_000.0


def iso(s):
    return datetime.fromtimestamp(s, tz=timezone.utc).isoformat()


def running(run0):
    return {"id": "m1", "state": "running", "runStartedAt": iso(run0)}


class Decide(unittest.TestCase):
    def test_first_exec(self):
        self.assertEqual(t.decide(running(NOW), {}, NOW)[0], "exec")

    def test_idle_until_exec_due(self):
        st = {"state": "running", "run0": NOW, "lastExec": NOW - 60}
        self.assertEqual(t.decide(running(NOW), st, NOW), (None, NOW - 60 + t.EXEC_EVERY))

    def test_rotate_at_470min(self):
        run0 = NOW - t.ROTATE_AT
        st = {"state": "running", "run0": run0, "lastExec": NOW}
        self.assertEqual(t.decide(running(run0), st, NOW)[0], "rotate")

    def test_follow_platform_run0(self):
        st = {"state": "running", "run0": NOW - t.ROTATE_AT - 10, "lastExec": NOW}
        self.assertIsNone(t.decide(running(NOW), st, NOW)[0])
        self.assertEqual(st["run0"], NOW)

    def test_exec_after_wake(self):
        st = {"state": "suspended", "lastExec": NOW - 10}
        self.assertEqual(t.decide(running(NOW - 1000), st, NOW)[0], "exec")
        self.assertEqual(st["run0"], NOW)

    def test_wait_in_cooldown(self):
        m = {"id": "m1", "state": "suspended", "cooldownUntil": iso(NOW + 300)}
        self.assertEqual(t.decide(m, {}, NOW), (None, NOW + 300 - t.WAKE_EARLY))

    def test_fast_retry_near_cooldown(self):
        m = {"id": "m1", "state": "suspended", "cooldownUntil": iso(NOW + 5)}
        self.assertEqual(t.decide(m, {}, NOW), ("resume", NOW + t.RETRY_FAST))

    def test_resume_without_cooldown(self):
        self.assertEqual(t.decide({"id": "m1", "state": "suspended"}, {}, NOW), ("resume", NOW + 60))

    def test_start_when_stopped(self):
        self.assertEqual(t.decide({"id": "m1", "state": "stopped"}, {}, NOW)[0], "start")

    def test_transient_and_unknown(self):
        self.assertEqual(t.decide({"id": "m1", "state": "resuming"}, {}, NOW), (None, NOW + 60))
        self.assertEqual(t.decide({"id": "m1", "state": "weird"}, {}, NOW)[0], "error")


class WriteWake(unittest.TestCase):
    def setUp(self):
        self.tmp, self.old = tempfile.TemporaryDirectory(), t.ROOT
        t.ROOT = self.tmp.name

    def tearDown(self):
        t.ROOT = self.old
        self.tmp.cleanup()

    def wake(self, book):
        t.write_wake(book, NOW)
        with open(os.path.join(self.tmp.name, "next_wake")) as f:
            return int(f.read())

    def test_empty_falls_back_60s(self):
        self.assertEqual(self.wake({}), NOW + 60)

    def test_ignores_past(self):
        self.assertEqual(self.wake({"a": {"m": {"next": NOW - 5}}}), NOW + 60)

    def test_earliest_future(self):
        book = {"a": {"m": {"next": NOW + 120}}, "b": {"n": {"next": NOW + 30}}}
        self.assertEqual(self.wake(book), NOW + 30)


class WaitState(unittest.TestCase):
    def test_survives_one_failed_poll(self):
        seq = [t.ApiError("抖动"), {"state": "suspended"}]
        real_call, real_sleep = t.call, t.time.sleep

        def fake(*a, **k):
            r = seq.pop(0)
            if isinstance(r, Exception):
                raise r
            return r
        t.call, t.time.sleep = fake, lambda s: None
        try:
            self.assertTrue(t.wait_state("m1", "c", "suspended", limit=5))
        finally:
            t.call, t.time.sleep = real_call, real_sleep


if __name__ == "__main__":
    unittest.main()
