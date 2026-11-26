"""
アプリ（ホーム画面に追加したダッシュボード）への通知（Web Push）

- 初回に VAPID 鍵（ラズパイの身分証のようなもの）を data/ に作る
- スマホがダッシュボードの設定で「このスマホに通知」を押すと、購読情報（送り先）が data/push.json に保存される
- 通知の種類ごとに、スマホごとにオン・オフできる："alert"（見守りアラート）／"env"（お部屋の暑い・寒い）
- 送れなくなった送り先（アプリを消した・通知を切った）は自動で消す
- スマホのブラウザは https のページからしか通知を許可しない（Tailscale の `tailscale serve` で https にする。docs/home.md）

baby_monitor.py から使う。送信部分は差し替えてテストできる。
"""
import base64
import json
import logging
import os
import threading
import time

log = logging.getLogger("baby-monitor.push")

KINDS = ("alert", "env")


def _b64(raw):
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode()


class Push:
    # 通知サーバーに名乗る連絡先。Apple は localhost などの仮の宛先を受け付けないので、https のURLにする
    def __init__(self, data_dir, contact="https://github.com/shimuka517-web/baby-monitor", sender=None):
        self.data_dir = data_dir
        self.key_path = os.path.join(data_dir, "vapid_private.pem")
        self.subs_path = os.path.join(data_dir, "push.json")
        self.contact = contact
        self.lock = threading.RLock()
        self.public_key = ""
        self.error = ""
        self.last_error = ""
        self._send = sender          # テスト用：sender(sub, payload_str) -> status_code
        try:
            self._load_or_create_key()
        except Exception as e:      # pywebpush が入っていないなど
            self.error = str(e)
            log.warning("アプリ通知は使えません（%s）。venv/bin/pip install -r requirements.txt を実行してください", e)
        self.subs = self._load_subs()

    @property
    def available(self):
        return bool(self.public_key)

    # ------------------------------------------------------------ 鍵と購読
    def _load_or_create_key(self):
        from py_vapid import Vapid01
        from cryptography.hazmat.primitives import serialization
        os.makedirs(self.data_dir, exist_ok=True)
        if os.path.exists(self.key_path):
            v = Vapid01.from_file(self.key_path)
        else:
            v = Vapid01()
            v.generate_keys()
            v.save_key(self.key_path)
            os.chmod(self.key_path, 0o600)
            log.info("アプリ通知の鍵を作りました: %s", self.key_path)
        raw = v.public_key.public_bytes(serialization.Encoding.X962, serialization.PublicFormat.UncompressedPoint)
        self.public_key = _b64(raw)

    def _load_subs(self):
        try:
            with open(self.subs_path, encoding="utf-8") as f:
                d = json.load(f)
            return [s for s in d if isinstance(s, dict) and s.get("endpoint")]
        except FileNotFoundError:
            return []
        except Exception as e:
            log.warning("%s が読めないため空にします: %s", self.subs_path, e)
            return []

    def _save(self):
        os.makedirs(self.data_dir, exist_ok=True)
        tmp = self.subs_path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(self.subs, f, ensure_ascii=False, indent=1)
        os.replace(tmp, self.subs_path)
        os.chmod(self.subs_path, 0o600)

    def subscribe(self, sub, kinds=None, name=""):
        """スマホの購読情報を登録（同じ送り先なら上書き）"""
        if not isinstance(sub, dict) or not str(sub.get("endpoint", "")).startswith("https://") \
                or not isinstance(sub.get("keys"), dict) or not sub["keys"].get("p256dh") or not sub["keys"].get("auth"):
            raise ValueError("通知の登録情報が正しくありません")
        kinds = {k: bool((kinds or {}).get(k, True)) for k in KINDS}
        rec = {"endpoint": sub["endpoint"], "keys": {"p256dh": sub["keys"]["p256dh"], "auth": sub["keys"]["auth"]},
               "kinds": kinds, "name": str(name or "")[:40], "created": time.time()}
        with self.lock:
            self.subs = [s for s in self.subs if s["endpoint"] != rec["endpoint"]] + [rec]
            self._save()
        log.info("アプリ通知の送り先を登録しました（%s・%d件）", rec["name"] or "名前なし", len(self.subs))
        return self.public_info(rec["endpoint"])

    def update(self, endpoint, kinds):
        with self.lock:
            for s in self.subs:
                if s["endpoint"] == endpoint:
                    for k in KINDS:
                        if k in (kinds or {}):
                            s["kinds"][k] = bool(kinds[k])
                    self._save()
                    return self.public_info(endpoint)
        raise KeyError("このスマホは登録されていません")

    def unsubscribe(self, endpoint):
        with self.lock:
            n = len(self.subs)
            self.subs = [s for s in self.subs if s["endpoint"] != endpoint]
            self._save()
            return n != len(self.subs)

    def public_info(self, endpoint=None):
        with self.lock:
            me = next((s for s in self.subs if s["endpoint"] == endpoint), None) if endpoint else None
            return {"available": self.available, "key": self.public_key, "count": len(self.subs),
                    "me": {"kinds": me["kinds"], "name": me["name"]} if me else None}

    # ------------------------------------------------------------ 送信
    def send(self, kind, title, body, url="/dash", tag=None, only=None):
        """kind の通知をオンにしている全員へ（only を渡すとその送り先だけ）。送れた件数を返す"""
        if not self.available and self._send is None:
            return 0
        payload = json.dumps({"title": title, "body": body, "url": url, "tag": tag or kind, "kind": kind}, ensure_ascii=False)
        with self.lock:
            targets = [s for s in self.subs if (only and s["endpoint"] == only) or (not only and s["kinds"].get(kind))]
        sent, gone = 0, []
        for s in targets:
            try:
                code = self._send(s, payload) if self._send else self._webpush(s, payload)
            except Exception as e:
                resp = getattr(e, "response", None)
                code = getattr(resp, "status_code", None)
                if code not in (404, 410):
                    detail = (getattr(resp, "text", "") or "")[:200]
                    self.last_error = f"{code or ''} {detail or e}".strip()
                    log.warning("アプリ通知を送れませんでした（%s）: %s", s.get("endpoint", "")[:40], self.last_error)
            if code in (404, 410):
                gone.append(s["endpoint"])          # アプリを消した・通知を切ったスマホ
            elif code and 200 <= code < 300:
                sent += 1
        if gone:
            with self.lock:
                self.subs = [s for s in self.subs if s["endpoint"] not in gone]
                self._save()
            log.info("届かなくなった送り先を %d 件削除しました", len(gone))
        return sent

    def send_async(self, *args, **kw):
        threading.Thread(target=self.send, args=args, kwargs=kw, daemon=True).start()

    def _webpush(self, sub, payload):
        from pywebpush import webpush
        r = webpush({"endpoint": sub["endpoint"], "keys": sub["keys"]}, data=payload,
                    vapid_private_key=self.key_path, vapid_claims={"sub": self.contact},
                    ttl=600, timeout=15, headers={"Urgency": "high"})
        return r.status_code
