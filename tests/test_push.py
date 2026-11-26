"""
アプリ通知（push.py）と、通知の振り分け（baby_monitor.notify）のテスト。ネット不要。
    venv/bin/python -m unittest discover tests -v
"""
import json
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import push  # noqa: E402

SUB = lambda n: {"endpoint": f"https://push.example/{n}", "keys": {"p256dh": "BKEY" + str(n), "auth": "AUTH"}}


class TestPush(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.sent = []
        self.codes = {}
        def sender(sub, payload):
            self.sent.append((sub["endpoint"], json.loads(payload)))
            return self.codes.get(sub["endpoint"], 201)
        self.p = push.Push(self.tmp.name, sender=sender)

    def tearDown(self):
        self.tmp.cleanup()

    def test_key_created_and_kept(self):
        self.assertTrue(self.p.available)
        self.assertEqual(len(self.p.public_key), 87)            # 65バイトの公開鍵（Base64URL）
        p2 = push.Push(self.tmp.name)
        self.assertEqual(p2.public_key, self.p.public_key)       # 再起動しても同じ鍵

    def test_kinds_per_phone(self):
        self.p.subscribe(SUB(1), {"alert": True, "env": True}, "パパ")
        self.p.subscribe(SUB(2), {"alert": True, "env": False}, "ママ")
        self.assertEqual(self.p.send("env", "t", "暑い"), 1)
        self.assertEqual([e for e, _ in self.sent], ["https://push.example/1"])
        self.sent.clear()
        self.assertEqual(self.p.send("alert", "t", "顔が見えません", url="/cam"), 2)
        self.assertEqual(self.sent[0][1]["url"], "/cam")
        self.p.update(SUB(2)["endpoint"], {"env": True})
        self.sent.clear()
        self.assertEqual(self.p.send("env", "t", "寒い"), 2)

    def test_resubscribe_overwrites_and_persists(self):
        self.p.subscribe(SUB(1), {"alert": True, "env": True})
        self.p.subscribe(SUB(1), {"alert": False, "env": True})
        self.assertEqual(len(self.p.subs), 1)
        p2 = push.Push(self.tmp.name)
        self.assertEqual(p2.public_info(SUB(1)["endpoint"])["me"]["kinds"], {"alert": False, "env": True})

    def test_gone_removed(self):
        self.p.subscribe(SUB(1)); self.p.subscribe(SUB(2))
        self.codes[SUB(1)["endpoint"]] = 410
        self.assertEqual(self.p.send("alert", "t", "b"), 1)
        self.assertEqual([s["endpoint"] for s in self.p.subs], [SUB(2)["endpoint"]])

    def test_only_one_phone(self):
        self.p.subscribe(SUB(1), {"alert": False, "env": False}); self.p.subscribe(SUB(2))
        self.assertEqual(self.p.send("test", "t", "b", only=SUB(1)["endpoint"]), 1)
        self.assertEqual(self.sent[0][0], SUB(1)["endpoint"])

    def test_invalid_subscription(self):
        for bad in (None, {"endpoint": "http://x"}, {"endpoint": "https://x", "keys": {}}):
            with self.assertRaises(ValueError):
                self.p.subscribe(bad)
        with self.assertRaises(KeyError):
            self.p.update("https://nope", {"env": False})


class TestNotifyRouting(unittest.TestCase):
    def test_route_by_kind(self):
        import baby_monitor as bm
        calls = {"discord": [], "push": []}
        class P:
            def send_async(self, kind, title, body, **kw): calls["push"].append(kind)
        orig_sync, orig_push, orig_ui = bm.notify_sync, bm.PUSH, dict(bm.cfg.ui)
        try:
            bm.notify_sync = lambda text, jpeg=None: calls["discord"].append(text)
            bm.PUSH = P()
            bm.cfg.ui["discord_env"] = False
            bm.cfg.ui["discord_alert"] = True
            bm.notify("暑い", kind="env")
            bm.notify("顔", kind="alert")
            bm.notify("起動", kind="system")
            import time; time.sleep(0.2)
            self.assertEqual(sorted(calls["discord"]), ["起動", "顔"])
            self.assertEqual(calls["push"], ["env", "alert"])
        finally:
            bm.notify_sync, bm.PUSH = orig_sync, orig_push
            bm.cfg.ui.clear(); bm.cfg.ui.update(orig_ui)


if __name__ == "__main__":
    unittest.main()
