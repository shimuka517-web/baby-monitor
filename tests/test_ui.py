"""
タッチ画面まわり（アラートの「様子を見ました」・画面からの設定・明るさ）のテスト。カメラ不要。
    venv/bin/python -m unittest discover tests -v
"""
import json
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import baby_monitor as bm  # noqa: E402

A = dict(bm.DEFAULTS["alert"])


class TestAck(unittest.TestCase):
    def _alert(self):
        m = bm.Monitor(True, 0.0)
        t = 0.0
        while t < 12:            # 顔を12秒確認 → 見守り中
            m.update(True, t, A); t = round(t + 0.1, 2)
        while m.phase != bm.ALERT:
            m.update(False, t, A); t = round(t + 0.1, 2)
        return m, t

    def test_ack_stops_repeat_until_recovered(self):
        m, t = self._alert()
        self.assertTrue(m.ack())
        events = []
        for _ in range(3000):    # 300秒たっても再通知しない
            events += m.update(False, t, A); t = round(t + 0.1, 2)
        self.assertNotIn("repeat", events)
        self.assertEqual(m.phase, bm.ALERT)
        for _ in range(40):      # 顔が戻れば復帰
            events += m.update(True, t, A); t = round(t + 0.1, 2)
        self.assertIn("recovered", events)
        self.assertFalse(m.acked)

    def test_ack_ignored_when_not_alert(self):
        m = bm.Monitor(True, 0.0)
        self.assertFalse(m.ack())
        self.assertFalse(m.acked)

    def test_armed_since(self):
        m = bm.Monitor(False, 0.0)
        self.assertIsNone(m.armed_since)
        m.set_armed(True, 5.0)
        self.assertEqual(m.armed_since, 5.0)


class TestUiSettings(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.cfg = bm.Config()
        self.cfg.ui_path = os.path.join(self.tmp.name, "ui.json")
        self.cfg.ui = {}

    def tearDown(self):
        self.tmp.cleanup()

    def test_valid_values_saved(self):
        applied = self.cfg.set_ui({"absent_sec": "30", "night_mode": "off", "head": "left", "brightness_night": 150})
        self.assertEqual(applied, {"absent_sec": 30.0, "night_mode": "off", "head": "left", "brightness_night": 100})
        self.assertEqual(self.cfg.alert["absent_sec"], 30.0)
        self.assertEqual(self.cfg.s["display"]["night_mode"], "off")
        self.assertEqual(self.cfg.head, "left")
        with open(self.cfg.ui_path) as f:
            self.assertEqual(json.load(f)["absent_sec"], 30.0)

    def test_old_head_values(self):
        self.assertEqual(self.cfg.set_ui({"head": "opposite"}), {"head": "near"})
        self.assertEqual(self.cfg.set_ui({"head": "camera"}), {"head": "far"})
        self.assertEqual(bm.HEAD_ROTATE[self.cfg.head], 0)

    def test_invalid_values_ignored(self):
        applied = self.cfg.set_ui({"absent_sec": 7, "night_mode": "party", "unknown": 1, "head": "up"})
        self.assertEqual(applied, {})
        self.assertEqual(self.cfg.alert["absent_sec"], bm.DEFAULTS["alert"]["absent_sec"])


class TestBacklight(unittest.TestCase):
    def test_set_percent(self):
        with tempfile.TemporaryDirectory() as root:
            d = os.path.join(root, "panel")
            os.makedirs(d)
            open(os.path.join(d, "max_brightness"), "w").write("255\n")
            open(os.path.join(d, "brightness"), "w").write("255\n")
            b = bm.Backlight(root)
            self.assertTrue(b.available and b.writable)
            self.assertTrue(b.set(50))
            self.assertEqual(open(os.path.join(d, "brightness")).read(), "128")
            b.set(1)
            self.assertEqual(open(os.path.join(d, "brightness")).read(), "3")
            b.set(0)
            self.assertEqual(open(os.path.join(d, "brightness")).read(), "0")

    def test_no_panel(self):
        with tempfile.TemporaryDirectory() as root:
            b = bm.Backlight(root)
            self.assertFalse(b.available)
            self.assertFalse(b.set(50))


if __name__ == "__main__":
    unittest.main()
