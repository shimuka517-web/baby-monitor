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
        if p["action"] == "upsert":
            self.rows[p["id"]] = p
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

    def at(self, h, mi, d=18):
        self.clock.t = datetime(2026, 11, d, h, mi, tzinfo=JST)

    def test_start_then_finish(self):
        self.at(17, 0); r = self.m.start()
        self.assertEqual(r["status"], "open")
        self.assertEqual(self.m.start()["id"], r["id"])          # 二重に始めない
        self.at(17, 12)
        o = self.m.summary()["open"]
        self.assertEqual((o["id"], o["elapsed_min"]), (r["id"], 12))
        self.at(17, 25); f = self.m.finish(r["id"], milk=80, breast=30)
        self.assertEqual((f["milk"], f["breast"], f["amount"], f["time"], f["end_time"], f["duration_min"]),
                         (80, 30, 110, "17:00", "17:25", 25))
        self.at(19, 16); s = self.m.summary()
        self.assertIsNone(s["open"])
        self.assertEqual((s["today_total"], s["today_milk"], s["today_breast"], s["today_count"]), (110, 80, 30, 1))
        self.assertEqual(s["last"]["ago_text"], "2時間16分前")    # 前回の開始から数える

    def test_add_later_and_summary(self):
        self.at(8, 30); self.m.add(milk=100, minutes_ago=25)
        self.at(17, 20); self.m.add(milk=60, breast=20, minutes_ago=19)
        self.at(19, 16); s = self.m.summary()
        self.assertEqual((s["today_total"], s["today_count"]), (180, 2))
        self.assertEqual(s["last"]["time"], "17:01")
        self.assertEqual(s["last"]["ago_text"], "2時間15分前")
        self.assertEqual([r["time"] for r in s["recent"]], ["17:01", "08:05"])

    def test_open_not_counted_in_total(self):
        self.m.add(milk=100)
        self.m.start()
        s = self.m.summary()
        self.assertEqual((s["today_total"], s["today_count"]), (100, 1))
        self.assertEqual(s["open"]["suggest_milk"], 100)

    def test_yesterday_not_in_today(self):
        self.at(23, 30, d=17); self.m.add(milk=120)
        self.at(1, 0); self.m.add(milk=60)
        s = self.m.summary()
        self.assertEqual((s["today_total"], s["today_count"]), (60, 1))

    def test_invalid_amount(self):
        r = self.m.start()
        for milk_v, breast_v in ((0, 0), (-10, 0), (5000, 0), ("abc", 0)):
            with self.assertRaises(ValueError):
                self.m.finish(r["id"], milk_v, breast_v)

    def test_change_start_time(self):
        self.at(17, 30); r = self.m.start()
        f = self.m.finish(r["id"], milk=100, start="2026-11-18T17:10:00+09:00")
        self.assertEqual((f["time"], f["duration_min"]), ("17:10", 20))
        with self.assertRaises(ValueError):
            self.m.finish(r["id"], milk=100, start="2026-11-19T10:00:00+09:00")   # 未来

    def test_sync_open_then_done(self):
        r = self.m.start()
        self.assertEqual(self.m.sync_once(), 1)
        self.assertEqual(self.sheet.rows[r["id"]]["end"], "")      # 授乳中は終了が空欄
        self.m.finish(r["id"], milk=80, breast=30)
        self.assertEqual(self.m.summary()["pending"], 1)
        self.assertEqual(self.m.sync_once(), 1)
        row = self.sheet.rows[r["id"]]
        self.assertEqual((row["milk"], row["breast"], row["total"]), (80, 30, 110))
        self.assertEqual(self.m.summary()["pending"], 0)

    def test_undo_finish_reopens(self):
        r = self.m.start()
        self.m.finish(r["id"], milk=80)
        self.assertEqual(self.m.undo(r["id"]), "reopened")
        s = self.m.summary()
        self.assertEqual((s["open"]["id"], s["today_count"]), (r["id"], 0))

    def test_undo_open_or_later_deletes(self):
        r = self.m.start()
        self.m.sync_once()
        self.assertEqual(self.m.undo(r["id"]), "deleted")
        self.m.sync_once()
        self.assertNotIn(r["id"], self.sheet.rows)
        a = self.m.add(milk=50)
        self.assertEqual(self.m.undo(a["id"]), "deleted")
        self.assertIsNone(self.m.summary()["open"])

    def test_undo_before_sync_sends_nothing(self):
        r = self.m.add(milk=100)
        self.m.undo(r["id"])
        self.m.sync_once()
        self.assertEqual(self.sheet.calls, [])
        self.assertEqual(self.m.summary()["pending"], 0)

    def test_offline_then_retry_in_order(self):
        self.sheet.ok = False
        a = self.m.add(milk=100)
        self.clock.t += timedelta(minutes=1)
        b = self.m.add(milk=80)
        self.assertEqual(self.m.sync_once(), 0)
        self.assertEqual(self.m.summary()["pending"], 2)
        self.assertFalse(self.m.last_sync_ok)
        self.sheet.ok = True
        self.assertEqual(self.m.sync_once(), 2)
        ups = [c["id"] for c in self.sheet.calls if c["action"] == "upsert" and c["id"] in self.sheet.rows]
        self.assertEqual(ups[-2:], [a["id"], b["id"]])   # 古い順に送る
        self.assertTrue(self.m.last_sync_ok)

    def test_persists_across_restart(self):
        self.m.add(milk=100)
        self.m.start()
        m2 = milk.MilkLog(self.m.db_path, sender=None, clock=self.clock)
        s = m2.summary()
        self.assertEqual(s["today_count"], 1)
        self.assertIsNotNone(s["open"])

    def test_undo_too_late(self):
        r = self.m.add(milk=100)
        with self.assertRaises(ValueError):
            self.m.undo(r["id"], within_sec=-1)

    def test_migrate_old_db(self):
        """v2.5 までの形式の記録を、ミルクとして引き継ぐ（送信済みは送り直さない）"""
        import sqlite3, time as _t
        path = os.path.join(self.tmp.name, "old.db")
        c = sqlite3.connect(path)
        c.execute("""CREATE TABLE feeds (id TEXT PRIMARY KEY, ts TEXT NOT NULL, amount INTEGER NOT NULL, kind TEXT NOT NULL,
                     source TEXT NOT NULL, deleted INTEGER NOT NULL DEFAULT 0, synced_add INTEGER NOT NULL DEFAULT 0,
                     synced_del INTEGER NOT NULL DEFAULT 0, created REAL NOT NULL)""")
        c.execute("INSERT INTO feeds VALUES ('old1','2026-11-18T17:01:00+09:00',130,'ミルク','画面',0,1,0,?)", (_t.time(),))
        c.execute("INSERT INTO feeds VALUES ('old2','2026-11-18T18:01:00+09:00',90,'ミルク','画面',0,0,0,?)", (_t.time(),))
        c.commit(); c.close()
        m = milk.MilkLog(path, sender=self.sheet, clock=self.clock)
        s = m.summary()
        self.assertEqual((s["today_total"], s["today_milk"], s["today_breast"]), (220, 220, 0))
        self.assertEqual(m.sync_once(), 1)
        self.assertEqual(list(self.sheet.rows), ["old2"])

    def test_ago_text(self):
        self.assertEqual(milk.ago_text(0), "たった今")
        self.assertEqual(milk.ago_text(45), "45分前")
        self.assertEqual(milk.ago_text(120), "2時間前")
        self.assertEqual(milk.ago_text(135), "2時間15分前")


if __name__ == "__main__":
    unittest.main()
