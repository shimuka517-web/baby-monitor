"""
おむつ・体重・温湿度（logbook.py）と、SwitchBot・天気（home_env.py）のテスト。カメラ・ネット不要。
    venv/bin/python -m unittest discover tests -v
"""
import base64
import hashlib
import hmac
import os
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import home_env  # noqa: E402
import logbook  # noqa: E402

JST = timezone(timedelta(hours=9))


class Clock:
    def __init__(self, t):
        self.t = t

    def __call__(self):
        return self.t


class Sheet:
    def __init__(self):
        self.tables, self.calls, self.ok = {}, [], True

    def __call__(self, p):
        self.calls.append(p)
        if not self.ok:
            raise ConnectionError("offline")
        t = self.tables.setdefault(p["table"], {})
        if p["action"] == "row":
            t[p["id"]] = p["row"]
        elif p["action"] == "row_delete":
            t.pop(p["id"], None)
        return True


class TestLogbook(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.clock = Clock(datetime(2026, 11, 26, 12, 0, tzinfo=JST))
        self.sheet = Sheet()
        self.lb = logbook.Logbook(os.path.join(self.tmp.name, "d", "logbook.db"), sender=self.sheet, clock=self.clock)

    def tearDown(self):
        self.tmp.cleanup()

    def at(self, h, m, d=26):
        self.clock.t = datetime(2026, 11, d, h, m, tzinfo=JST)

    def test_diaper_counts(self):
        self.at(23, 0, d=25); self.lb.add_diaper("poo")
        self.at(7, 0); self.lb.add_diaper("pee")
        self.at(9, 30); self.lb.add_diaper("both")
        self.at(11, 0); self.lb.add_diaper("pee")
        self.at(12, 0); s = self.lb.diaper_summary()
        self.assertEqual((s["today_count"], s["today_pee"], s["today_poo"]), (3, 3, 1))
        self.assertEqual((s["last"]["time"], s["last"]["ago_text"]), ("11:00", "1時間前"))
        self.assertEqual(s["last_poo"]["time"], "09:30")

    def test_diapers_since(self):
        self.at(10, 0, d=25); self.lb.add_diaper("pee")
        self.at(13, 0, d=25); self.lb.add_diaper("poo")
        self.at(11, 0); self.lb.add_diaper("both")
        self.at(12, 0)
        self.assertEqual([r["kind"] for r in self.lb.diapers_since(24)], ["poo", "both"])

    def test_diaper_minutes_ago_and_invalid(self):
        r = self.lb.add_diaper("pee", minutes_ago=30)
        self.assertTrue(r["ts"].startswith("2026-11-26T11:30"))
        with self.assertRaises(ValueError):
            self.lb.add_diaper("other")

    def test_diaper_undo_and_sync(self):
        r = self.lb.add_diaper("pee")
        self.assertEqual(self.lb.sync_once(), 1)
        self.assertEqual(self.sheet.tables["おむつ"][r["id"]][1], "おしっこ")
        self.lb.undo_diaper(r["id"])
        self.lb.sync_once()
        self.assertNotIn(r["id"], self.sheet.tables["おむつ"])
        self.assertEqual(self.lb.diaper_summary()["today_count"], 0)
        with self.assertRaises(ValueError):
            self.lb.undo_diaper(self.lb.add_diaper("pee")["id"], within_sec=-1)

    def test_weight_one_per_day(self):
        self.lb.set_weight(3200, "2026-11-20")
        self.lb.set_weight(3350, "2026-11-26", note="健診")
        self.lb.set_weight(3360, "2026-11-26")
        self.assertEqual([(w["day"], w["grams"]) for w in self.lb.weights()], [("2026-11-20", 3200), ("2026-11-26", 3360)])
        self.lb.sync_once()
        self.assertEqual(self.sheet.tables["体重"]["w2026-11-26"][1], 3360)
        self.lb.delete_weight("2026-11-20")
        self.lb.sync_once()
        self.assertNotIn("w2026-11-20", self.sheet.tables["体重"])
        for bad in (500, 50000, "x"):
            with self.assertRaises(ValueError):
                self.lb.set_weight(bad)
        with self.assertRaises(ValueError):
            self.lb.set_weight(3000, "2026-12-01")

    def test_env_series_trend_and_hourly_sheet(self):
        for i in range(0, 80, 5):                 # 10:00〜11:15 に5分ごと
            self.clock.t = datetime(2026, 11, 26, 10, 0, tzinfo=JST) + timedelta(minutes=i)
            self.lb.add_env(22.0 + i / 100, 50 - i / 10)
        s = self.lb.env_summary()
        self.assertEqual(s["latest"]["time"], "11:15")
        self.assertAlmostEqual(s["latest"]["temp"], 22.75)
        self.assertEqual(s["trend"]["temp"], 0.6)
        self.assertTrue(len(s["series"]) >= 5)
        self.lb.sync_once()
        rows = self.sheet.tables["室温"]
        self.assertEqual(list(rows), ["e2026112610"])    # 10時台の平均が1行
        self.assertEqual(rows["e2026112610"][1], 22.3)

    def test_offline_queue_in_order(self):
        self.sheet.ok = False
        a = self.lb.add_diaper("pee"); b = self.lb.add_diaper("poo")
        self.assertEqual(self.lb.sync_once(), 0)
        self.assertEqual(self.lb.pending(), 2)
        self.sheet.ok = True
        self.assertEqual(self.lb.sync_once(), 2)
        ids = [c["id"] for c in self.sheet.calls if c.get("table") == "おむつ"]
        self.assertEqual(ids[-2:], [a["id"], b["id"]])


class TestSwitchBot(unittest.TestCase):
    def test_signature(self):
        h = home_env.switchbot_headers("TOKEN", "SECRET", t=1700000000000, nonce="abc")
        expect = base64.b64encode(hmac.new(b"SECRET", b"TOKEN1700000000000abc", hashlib.sha256).digest()).decode()
        self.assertEqual(h["sign"], expect)
        self.assertEqual((h["Authorization"], h["t"], h["nonce"]), ("TOKEN", "1700000000000", "abc"))

    def test_find_hub3_and_read(self):
        class R:
            def __init__(self, j): self.j = j
            def raise_for_status(self): pass
            def json(self): return self.j
        def get(url, headers, timeout):
            if url.endswith("/devices"):
                return R({"statusCode": 100, "body": {"deviceList": [
                    {"deviceId": "PLUG", "deviceType": "Plug Mini (JP)", "deviceName": "プラグ"},
                    {"deviceId": "METER", "deviceType": "Meter", "deviceName": "温湿度計"},
                    {"deviceId": "HUB3", "deviceType": "Hub 3", "deviceName": "ハブ3"}]}})
            if "HUB3" in url:
                return R({"statusCode": 100, "body": {"temperature": 22.4, "humidity": 48, "lightLevel": 5}})
            return R({"statusCode": 100, "body": {"temperature": 19.0, "humidity": 60}})
        sb = home_env.SwitchBot("t", "s", get=get)
        self.assertEqual(sb.read(), (22.4, 48.0))
        self.assertEqual(sb.device_name, "ハブ3")

    def test_api_error(self):
        class R:
            def raise_for_status(self): pass
            def json(self): return {"statusCode": 190, "message": "unauthorized"}
        sb = home_env.SwitchBot("t", "s", device_id="X", get=lambda *a, **k: R())
        with self.assertRaises(RuntimeError):
            sb.read()


class TestEnvAlert(unittest.TestCase):
    def test_range_alert_once_with_margin(self):
        a = home_env.RangeAlert("室温", "℃", 0.5)
        seq = [22, 26.5, 27, 26.8, 25.8, 25.4, 27]
        ev = [a.check(v, 20, 26) for v in seq]
        self.assertEqual(ev, [None, "high", None, None, None, "ok", "high"])

    def test_monitor_notifies(self):
        class Client:
            vals = [(22, 50), (27, 50), (27, 35)]
            def read(self): return self.vals.pop(0)
        class LB:
            def __init__(self): self.rows = []
            def add_env(self, t, h): self.rows.append((t, h))
        msgs = []
        cfg = {"alert": True, "temp_min": 20, "temp_max": 26, "hum_min": 40, "hum_max": 60}
        m = home_env.EnvMonitor(Client(), LB(), lambda: cfg, msgs.append)
        for _ in range(3):
            m.poll_once()
        self.assertEqual(len(msgs), 2)
        self.assertIn("暑く", msgs[0])
        self.assertIn("乾燥", msgs[1])


class TestMilestones(unittest.TestCase):
    def test_days(self):
        from datetime import date
        b = date(2026, 11, 14)
        self.assertEqual(logbook.milestone_on(b, date(2026, 11, 20)), "お七夜（7日目）")
        self.assertEqual(logbook.milestone_on(b, date(2026, 12, 14)), "生後1か月")
        self.assertEqual(logbook.milestone_on(b, date(2027, 2, 21)), "100日目（お食い初め）")
        self.assertEqual(logbook.milestone_on(b, date(2027, 11, 14)), "1歳のお誕生日")
        self.assertIsNone(logbook.milestone_on(b, date(2026, 11, 26)))
        m = logbook.milestones(b, date(2026, 12, 11))
        self.assertEqual((m["today"], m["next"]), (None, {"label": "生後1か月", "in_days": 3}))

    def test_month_end(self):
        from datetime import date
        b = date(2026, 1, 31)
        self.assertEqual(logbook.milestone_on(b, date(2026, 2, 28)), "生後1か月")   # 2月は月末


class TestHomeNetwork(unittest.TestCase):
    def test_lan_vs_tailscale(self):
        import baby_monitor as bm
        for ip, home in (("192.168.10.20", True), ("10.0.0.5", True), ("127.0.0.1", True), ("::1", True),
                         ("100.101.2.3", False), ("fd7a:115c:a1e0::5", False), ("8.8.8.8", False), ("::ffff:192.168.1.2", True)):
            self.assertEqual(bm.is_home_network(ip), home, ip)


class TestTailscaleAtHome(unittest.TestCase):
    def test_direct_lan_peer_is_home(self):
        import baby_monitor as bm
        st = {"Peer": {
            "a": {"TailscaleIPs": ["100.101.1.1", "fd7a:115c:a1e0::1"], "CurAddr": "192.168.10.55:41641"},   # 家のWi-Fi
            "b": {"TailscaleIPs": ["100.101.1.2"], "CurAddr": "203.0.113.9:41641"},                            # 携帯回線
            "c": {"TailscaleIPs": ["100.101.1.3"], "CurAddr": ""},                                              # 中継
            "d": {"TailscaleIPs": ["100.101.1.4"], "CurAddr": "[2001:db8::1]:41641"}}}
        self.assertEqual(bm.tailscale_peers_at_home(st, "192.168.10.138"), {"100.101.1.1", "fd7a:115c:a1e0::1"})
        self.assertEqual(bm.tailscale_peers_at_home(st, ""), set())


class TestWeather(unittest.TestCase):
    def test_parse(self):
        j = {"current": {"temperature_2m": 12.34, "relative_humidity_2m": 55, "weather_code": 2},
             "daily": {"time": ["2026-11-26", "2026-11-27", "2026-11-28"], "weather_code": [0, 61, 95],
                       "temperature_2m_max": [15.4, 12.6, 9.2], "temperature_2m_min": [3.2, 5.5, 1.0],
                       "precipitation_probability_max": [0, 80, 60]}}
        w = home_env.parse_weather(j)
        self.assertEqual(w["now"]["text"], "晴れ時々くもり")
        self.assertEqual([d["text"] for d in w["days"]], ["快晴", "雨", "雷雨"])
        self.assertEqual((w["days"][1]["max"], w["days"][1]["min"], w["days"][1]["pop"]), (13, 6, 80))


if __name__ == "__main__":
    unittest.main()
