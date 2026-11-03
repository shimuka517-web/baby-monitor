"""
見守りの状態遷移（Monitor クラス）のテスト。カメラ不要。
    venv/bin/python -m unittest discover tests -v
設定（config/settings.toml）を変えても、テストは既定値（DEFAULTS）で動きます。
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import baby_monitor as bm  # noqa: E402

A = dict(bm.DEFAULTS["alert"])  # confirm 10 / tolerance 2 / absent 15 / repeat 120 / recover 3 / waiting 600


def run(visible_intervals, until, armed=True, dt=0.1):
    """visible_intervals: 顔が見えている区間 [(開始秒, 終了秒), ...]。発生イベントと最終状態を返す"""
    m = bm.Monitor(armed, 0.0)
    events, t = [], 0.0
    while t <= until:
        vis = any(s <= t < e for s, e in visible_intervals)
        events += [(round(t, 1), e) for e in m.update(vis, t, A)]
        t = round(t + dt, 2)
    return events, m.phase


def names(events):
    return [e for _, e in events]


class TestMonitor(unittest.TestCase):
    def test_no_alert_before_face_confirmed(self):
        ev, phase = run([], until=300)
        self.assertNotIn("alert", names(ev))
        self.assertEqual(phase, bm.WAITING)

    def test_short_glimpse_does_not_confirm(self):
        ev, _ = run([(0, 5)], until=60)
        self.assertEqual(names(ev), [])

    def test_confirm_after_10s(self):
        ev, phase = run([(0, 30)], until=30)
        self.assertEqual(ev, [(10.0, "confirmed")])
        self.assertEqual(phase, bm.WATCHING)

    def test_brief_flicker_keeps_confirming(self):
        ev, _ = run([(0, 4), (5.5, 14)], until=14)  # 1.5秒の途切れ（許容2秒以内）
        self.assertEqual(ev, [(10.0, "confirmed")])

    def test_long_gap_resets_confirming(self):
        ev, _ = run([(0, 5), (8, 20)], until=20)  # 3秒途切れ → 8秒から数え直し
        self.assertEqual(ev, [(18.0, "confirmed")])

    def test_alert_after_absent(self):
        ev, phase = run([(0, 12)], until=30)
        self.assertIn((27.0, "alert"), ev)  # 最後に見えたのが11.9秒 → 15秒後
        self.assertEqual(phase, bm.ALERT)

    def test_short_absence_no_alert(self):
        ev, _ = run([(0, 12), (22, 40)], until=40)  # 10秒だけ見えない
        self.assertNotIn("alert", names(ev))

    def test_false_detection_does_not_recover(self):
        ev, phase = run([(0, 12), (30, 30.5)], until=40)
        self.assertNotIn("recovered", names(ev))
        self.assertEqual(phase, bm.ALERT)

    def test_recover_after_3s(self):
        ev, phase = run([(0, 12), (35, 40)], until=40)
        self.assertIn((38.0, "recovered"), ev)
        self.assertEqual(phase, bm.WATCHING)

    def test_repeat_every_120s(self):
        ev, _ = run([(0, 12)], until=300)
        self.assertEqual([t for t, e in ev if e == "repeat"], [147.0, 267.0])

    def test_waiting_reminder_once(self):
        ev, _ = run([], until=1300)
        self.assertEqual(ev, [(600.0, "waiting_long")])

    def test_paused_does_nothing(self):
        ev, phase = run([(0, 12)], until=60, armed=False)
        self.assertEqual(ev, [])
        self.assertEqual(phase, bm.PAUSED)


if __name__ == "__main__":
    unittest.main()
