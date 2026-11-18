"""
ミルク記録

- 記録はまずラズパイ本体の SQLite（data/milk.db）に保存する（ネットが切れていても失われない）
- 別スレッドで Google Apps Script（スプレッドシート）へ送る。失敗したら時間をおいて再送する
- 記録した直後は取り消しできる（取り消しもシートに反映する）

baby_monitor.py から使う。単体でテストできるよう、カメラや画面には依存しない。
"""
import json
import logging
import os
import sqlite3
import threading
import time
import uuid
from datetime import datetime, timedelta

import requests

log = logging.getLogger("baby-monitor.milk")


def now_local():
    return datetime.now().astimezone()


class MilkLog:
    def __init__(self, db_path, sender=None, retry_sec=60, clock=now_local):
        """sender: 1件の送信を行う関数 (payload dict) -> bool。None なら送信しない（ラズパイ内に保存だけ）"""
        os.makedirs(os.path.dirname(os.path.abspath(db_path)), exist_ok=True)
        self.db_path = db_path
        self.sender = sender
        self.retry_sec = retry_sec
        self.clock = clock
        self.lock = threading.Lock()
        self.wake = threading.Event()
        self.last_sync_ok = None      # 最後に送信が成功したか（None=まだ送っていない）
        self.last_error = ""
        with self._db() as c:
            c.execute("""CREATE TABLE IF NOT EXISTS feeds (
                id TEXT PRIMARY KEY,
                ts TEXT NOT NULL,            -- ISO8601（タイムゾーン付き）
                amount INTEGER NOT NULL,     -- ml
                kind TEXT NOT NULL,          -- ミルク など
                source TEXT NOT NULL,        -- 記録した場所
                deleted INTEGER NOT NULL DEFAULT 0,
                synced_add INTEGER NOT NULL DEFAULT 0,
                synced_del INTEGER NOT NULL DEFAULT 0,
                created REAL NOT NULL)""")

    def _db(self):
        c = sqlite3.connect(self.db_path, timeout=10)
        c.row_factory = sqlite3.Row
        return c

    # ------------------------------------------------------------ 記録
    def add(self, amount, kind="ミルク", source="ベッド横の画面"):
        amount = int(amount)
        if not (0 < amount <= 1000):
            raise ValueError("量は1〜1000mlで指定してください")
        rec = {"id": uuid.uuid4().hex[:12], "ts": self.clock().isoformat(timespec="seconds"),
               "amount": amount, "kind": kind, "source": source}
        with self.lock, self._db() as c:
            c.execute("INSERT INTO feeds (id, ts, amount, kind, source, created) VALUES (?,?,?,?,?,?)",
                      (rec["id"], rec["ts"], amount, kind, source, time.time()))
        log.info("ミルクを記録: %s %dml", rec["ts"], amount)
        self.wake.set()
        return rec

    def undo(self, rec_id, within_sec=600):
        """記録を取り消す。記録から within_sec 秒以内のものだけ（押し間違い用）"""
        with self.lock, self._db() as c:
            row = c.execute("SELECT created, deleted FROM feeds WHERE id=?", (rec_id,)).fetchone()
            if row is None:
                raise KeyError("その記録は見つかりません")
            if row["deleted"]:
                return False
            if time.time() - row["created"] > within_sec:
                raise ValueError("時間がたった記録は、スプレッドシート側で直してください")
            c.execute("UPDATE feeds SET deleted=1 WHERE id=?", (rec_id,))
        log.info("ミルクの記録を取り消し: %s", rec_id)
        self.wake.set()
        return True

    # ------------------------------------------------------------ 集計（画面用）
    def summary(self, recent=6):
        now = self.clock()
        day_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
        with self.lock, self._db() as c:
            rows = [dict(r) for r in c.execute(
                "SELECT id, ts, amount, kind FROM feeds WHERE deleted=0 ORDER BY ts DESC LIMIT 200")]
            pending = c.execute(
                "SELECT COUNT(*) FROM feeds WHERE (synced_add=0 AND deleted=0) OR (deleted=1 AND synced_add=1 AND synced_del=0)").fetchone()[0]
        today = [r for r in rows if datetime.fromisoformat(r["ts"]) >= day_start]
        last = rows[0] if rows else None
        out = {
            "now": now.isoformat(timespec="seconds"),
            "last": None,
            "today_total": sum(r["amount"] for r in today),
            "today_count": len(today),
            "recent": [{"id": r["id"], "time": datetime.fromisoformat(r["ts"]).strftime("%H:%M"),
                        "amount": r["amount"]} for r in today[:recent]],
            "pending": pending,
            "sync_enabled": self.sender is not None,
            "last_sync_ok": self.last_sync_ok,
        }
        if last:
            t = datetime.fromisoformat(last["ts"])
            mins = max(0, int((now - t).total_seconds() // 60))
            out["last"] = {"id": last["id"], "time": t.strftime("%H:%M"), "amount": last["amount"],
                           "minutes_ago": mins, "ago_text": ago_text(mins)}
        return out

    # ------------------------------------------------------------ 送信
    def sync_once(self):
        """未送信の記録を古い順に送る。送れた件数を返す"""
        if self.sender is None:
            return 0
        with self.lock, self._db() as c:
            rows = [dict(r) for r in c.execute("SELECT * FROM feeds ORDER BY created")]
        sent = 0
        for r in rows:
            if r["deleted"] and not r["synced_add"]:
                # 送る前に取り消された → シートには何も送らない
                if not r["synced_del"]:
                    self._mark(r["id"], synced_add=1, synced_del=1)
                continue
            if not r["synced_add"]:
                payload = {"action": "add", "id": r["id"], "time": r["ts"], "amount": r["amount"],
                           "kind": r["kind"], "source": r["source"]}
                if not self._send(payload):
                    return sent
                self._mark(r["id"], synced_add=1)
                sent += 1
            if r["deleted"] and not r["synced_del"]:
                if not self._send({"action": "delete", "id": r["id"]}):
                    return sent
                self._mark(r["id"], synced_del=1)
                sent += 1
        return sent

    def _send(self, payload):
        try:
            ok = bool(self.sender(payload))
        except Exception as e:
            ok = False
            self.last_error = str(e)
        self.last_sync_ok = ok
        if not ok:
            log.warning("スプレッドシートへの送信に失敗（あとで再送します）: %s", self.last_error)
        return ok

    def _mark(self, rec_id, **cols):
        sets = ", ".join(f"{k}=?" for k in cols)
        with self.lock, self._db() as c:
            c.execute(f"UPDATE feeds SET {sets} WHERE id=?", (*cols.values(), rec_id))

    def start_sync_thread(self):
        if self.sender is None:
            return

        def loop():
            while True:
                try:
                    self.sync_once()
                except Exception:
                    log.exception("ミルク記録の送信処理でエラー")
                self.wake.wait(self.retry_sec)
                self.wake.clear()
        threading.Thread(target=loop, daemon=True, name="milk-sync").start()


def ago_text(mins):
    if mins < 1:
        return "たった今"
    if mins < 60:
        return f"{mins}分前"
    h, m = divmod(mins, 60)
    return f"{h}時間{m}分前" if m else f"{h}時間前"


def make_gas_sender(url, token, timeout=20):
    """Google Apps Script のウェブアプリへ送る関数をつくる"""
    def send(payload):
        body = dict(payload, token=token)
        r = requests.post(url, data=json.dumps(body, ensure_ascii=False).encode("utf-8"),
                          headers={"Content-Type": "application/json"}, timeout=timeout)
        r.raise_for_status()
        try:
            res = r.json()
        except ValueError:
            raise RuntimeError("Apps Script の応答が読めません（URLやデプロイの設定を確認）")
        if not res.get("ok"):
            raise RuntimeError(res.get("error", "Apps Script がエラーを返しました"))
        return True
    return send
