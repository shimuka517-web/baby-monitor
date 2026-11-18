"""
ミルク記録（milk.py）のテスト。カメラ・ネット不要。
    venv/bin/python -m unittest discover tests -v
"""
import os
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import milk  # noqa: E402

JST = timezone(timedelta(hours=9))


class FakeClock:
    def __init__(self, t):
        self.t = t

    def __call__(self):
        return self.t


class FakeSheet:
    """Apps Script の代わり。ok=False の間は失敗する"""
    def __init__(self):
        self.rows = {}
        self.calls = []
        self.ok = True

    def __call__(self, p):
        self.calls.append(p)
        if not self.ok:
            raise ConnectionError("offline")
        if p["action"] == "add":
            self.rows.setdefault(p["id"], p)
        elif p["action"] == "delete":
            self.rows.pop(p["id"], None)
        return True


class TestMilk(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.clock = FakeClock(datetime(2026, 11, 18, 19, 16, tzinfo=JST))
        self.sheet = FakeSheet()
        self.m = milk.MilkLog(os.path.join(self.tmp.name, "data", "milk.db"), sender=self.sheet, clock=self.clock)

    def tearDown(self):
        self.tmp.cleanup()

    def test_add_and_summary(self):
        self.clock.t = datetime(2026, 11, 18, 8, 5, tzinfo=JST); self.m.add(100)
        self.clock.t = datetime(2026, 11, 18, 17, 1, tzinfo=JST); self.m.add(80)
        self.clock.t = datetime(2026, 11, 18, 19, 16, tzinfo=JST)
        s = self.m.summary()
        self.assertEqual(s["today_total"], 180)
        self.assertEqual(s["today_count"], 2)
        self.assertEqual(s["last"]["amount"], 80)
        self.assertEqual(s["last"]["time"], "17:01")
        self.assertEqual(s["last"]["ago_text"], "2時間15分前")
        self.assertEqual([r["time"] for r in s["recent"]], ["17:01", "08:05"])

    def test_yesterday_not_in_today(self):
        self.clock.t = datetime(2026, 11, 17, 23, 30, tzinfo=JST); self.m.add(120)
        self.clock.t = datetime(2026, 11, 18, 1, 0, tzinfo=JST); self.m.add(60)
        s = self.m.summary()
        self.assertEqual((s["today_total"], s["today_count"]), (60, 1))
        self.assertEqual(s["last"]["amount"], 60)

    def test_invalid_amount(self):
        for v in (0, -10, 5000):
            with self.assertRaises(ValueError):
                self.m.add(v)

    def test_sync_and_undo(self):
        r = self.m.add(100)
        self.assertEqual(self.m.summary()["pending"], 1)
        self.assertEqual(self.m.sync_once(), 1)
        self.assertIn(r["id"], self.sheet.rows)
        self.assertEqual(self.m.summary()["pending"], 0)
        self.assertTrue(self.m.undo(r["id"]))
        self.assertEqual(self.m.summary()["pending"], 1)
        self.m.sync_once()
        self.assertNotIn(r["id"], self.sheet.rows)
        self.assertEqual(self.m.summary()["today_count"], 0)

    def test_undo_before_sync_sends_nothing(self):
        r = self.m.add(100)
        self.m.undo(r["id"])
        self.m.sync_once()
        self.assertEqual(self.sheet.calls, [])
        self.assertEqual(self.m.summary()["pending"], 0)

    def test_offline_then_retry_in_order(self):
        self.sheet.ok = False
        a = self.m.add(100)
        self.clock.t += timedelta(minutes=1)
        b = self.m.add(80)
        self.assertEqual(self.m.sync_once(), 0)
        self.assertEqual(self.m.summary()["pending"], 2)
        self.assertFalse(self.m.last_sync_ok)
        self.sheet.ok = True
        self.assertEqual(self.m.sync_once(), 2)
        adds = [c["id"] for c in self.sheet.calls if c["action"] == "add" and c["id"] in self.sheet.rows]
        self.assertEqual(adds[-2:], [a["id"], b["id"]])   # 古い順に送る
        self.assertTrue(self.m.last_sync_ok)

    def test_persists_across_restart(self):
        self.m.add(100)
        m2 = milk.MilkLog(self.m.db_path, sender=None, clock=self.clock)
        self.assertEqual(m2.summary()["today_count"], 1)

    def test_undo_too_late(self):
        r = self.m.add(100)
        with self.assertRaises(ValueError):
            self.m.undo(r["id"], within_sec=-1)

    def test_ago_text(self):
        self.assertEqual(milk.ago_text(0), "たった今")
        self.assertEqual(milk.ago_text(45), "45分前")
        self.assertEqual(milk.ago_text(120), "2時間前")
        self.assertEqual(milk.ago_text(135), "2時間15分前")


if __name__ == "__main__":
    unittest.main()
