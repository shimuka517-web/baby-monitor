"""
授乳（ミルク・母乳）の記録

- 1回の授乳 = 開始時刻・終了時刻・ミルク(ml)・母乳(ml)。合計＝ミルク＋母乳
- あげ始めに「あげ始める」→ 飲み終わったら量を入れて「あげ終わった」。あとからまとめて記録もできる
- 記録はまずラズパイ本体の SQLite（data/milk.db）に保存する（ネットが切れていても失われない）
- 別スレッドで Google Apps Script（スプレッドシート）へ送る。授乳中も1行送り（終了が空欄）、
  終わったら同じ行を上書きする。失敗したら時間をおいて再送する
- 押し間違いは取り消せる（終わったものは「授乳中」に戻る。授乳中のものは消える）

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

MAX_ML = 1000


def now_local():
    return datetime.now().astimezone()


def _ml(v, name):
    try:
        v = int(v or 0)
    except (TypeError, ValueError):
        raise ValueError(f"{name}の量が数字ではありません")
    if not (0 <= v <= MAX_ML):
        raise ValueError(f"{name}の量は0〜{MAX_ML}mlで指定してください")
    return v


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
                ts TEXT NOT NULL,            -- 開始時刻 ISO8601（タイムゾーン付き）
                amount INTEGER NOT NULL,     -- 合計 ml（ミルク＋母乳）
                kind TEXT NOT NULL,
                source TEXT NOT NULL,        -- 記録した場所
                deleted INTEGER NOT NULL DEFAULT 0,
                synced_add INTEGER NOT NULL DEFAULT 0,
                synced_del INTEGER NOT NULL DEFAULT 0,
                created REAL NOT NULL)""")
            cols = {r[1] for r in c.execute("PRAGMA table_info(feeds)")}
            # v2.6：授乳中／終了、ミルクと母乳を分けて記録（古い記録はミルクとして引き継ぐ）
            for name, ddl in (("end_ts", "TEXT"), ("milk_ml", "INTEGER"), ("breast_ml", "INTEGER NOT NULL DEFAULT 0"),
                              ("status", "TEXT NOT NULL DEFAULT 'done'"), ("live", "INTEGER NOT NULL DEFAULT 0"),
                              ("version", "INTEGER NOT NULL DEFAULT 1"), ("synced_ver", "INTEGER NOT NULL DEFAULT 0"),
                              ("updated", "REAL")):
                if name not in cols:
                    c.execute(f"ALTER TABLE feeds ADD COLUMN {name} {ddl}")
            if "milk_ml" not in cols:
                c.execute("UPDATE feeds SET milk_ml=amount, end_ts=ts, synced_ver=synced_add, updated=created")

    def _db(self):
        c = sqlite3.connect(self.db_path, timeout=10)
        c.row_factory = sqlite3.Row
        return c

    def _now(self):
        return self.clock().isoformat(timespec="seconds")

    def _get(self, c, rec_id):
        row = c.execute("SELECT * FROM feeds WHERE id=? AND deleted=0", (rec_id,)).fetchone()
        if row is None:
            raise KeyError("その記録は見つかりません")
        return dict(row)

    # ------------------------------------------------------------ 記録
    def start(self, source="ベッド横の画面"):
        """あげ始め。授乳中のものがあれば、それを返す（二重に始めない）"""
        with self.lock, self._db() as c:
            row = c.execute("SELECT * FROM feeds WHERE status='open' AND deleted=0 ORDER BY ts DESC LIMIT 1").fetchone()
            if row:
                return self._public(dict(row))
            rec = {"id": uuid.uuid4().hex[:12], "ts": self._now()}
            c.execute("""INSERT INTO feeds (id, ts, amount, kind, source, created, milk_ml, breast_ml, status, live, updated)
                         VALUES (?,?,0,'授乳',?,?,NULL,0,'open',1,?)""",
                      (rec["id"], rec["ts"], source, time.time(), time.time()))
            row = self._get(c, rec["id"])
        log.info("授乳を開始: %s", rec["ts"])
        self.wake.set()
        return self._public(row)

    def finish(self, rec_id, milk=0, breast=0, start=None):
        """あげ終わり。ミルクと母乳の量を入れる（start で開始時刻を直せる）"""
        milk, breast = _ml(milk, "ミルク"), _ml(breast, "母乳")
        if milk + breast <= 0:
            raise ValueError("ミルクか母乳の量を入れてください")
        with self.lock, self._db() as c:
            r = self._get(c, rec_id)
            ts = self._check_start(start) if start else r["ts"]
            c.execute("""UPDATE feeds SET ts=?, end_ts=?, milk_ml=?, breast_ml=?, amount=?, status='done',
                         version=version+1, updated=? WHERE id=?""",
                      (ts, self._now(), milk, breast, milk + breast, time.time(), rec_id))
            row = self._get(c, rec_id)
        log.info("授乳を記録: %s ミルク%dml 母乳%dml", ts, milk, breast)
        self.wake.set()
        return self._public(row)

    def add(self, milk=0, breast=0, minutes_ago=0, source="ベッド横の画面", kind=None):
        """あとからまとめて記録（開始＝minutes_ago 分前、終了＝今）"""
        milk, breast = _ml(milk, "ミルク"), _ml(breast, "母乳")
        if milk + breast <= 0:
            raise ValueError("ミルクか母乳の量を入れてください")
        minutes_ago = max(0, min(24 * 60, int(minutes_ago or 0)))
        now = self.clock()
        rec_id = uuid.uuid4().hex[:12]
        ts = (now - timedelta(minutes=minutes_ago)).isoformat(timespec="seconds")
        with self.lock, self._db() as c:
            c.execute("""INSERT INTO feeds (id, ts, end_ts, amount, kind, source, created, milk_ml, breast_ml, status, live, updated)
                         VALUES (?,?,?,?,'授乳',?,?,?,?,'done',0,?)""",
                      (rec_id, ts, now.isoformat(timespec="seconds"), milk + breast, source, time.time(), milk, breast, time.time()))
            row = self._get(c, rec_id)
        log.info("授乳を記録（あとから）: %s ミルク%dml 母乳%dml", ts, milk, breast)
        self.wake.set()
        return self._public(row)

    def undo(self, rec_id, within_sec=600):
        """押し間違いの取り消し。終わった記録は「授乳中」に戻し（あげ始めから記録したもの）、
        それ以外は消す。最後の操作から within_sec 秒以内のものだけ"""
        with self.lock, self._db() as c:
            r = self._get(c, rec_id)
            if r["status"] == "done" and time.time() - (r["updated"] or r["created"]) > within_sec:
                raise ValueError("時間がたった記録は、スプレッドシート側で直してください")
            if r["status"] == "done" and r["live"]:
                c.execute("""UPDATE feeds SET status='open', end_ts=NULL, version=version+1, updated=? WHERE id=?""",
                          (time.time(), rec_id))
                result = "reopened"
            else:
                c.execute("UPDATE feeds SET deleted=1, updated=? WHERE id=?", (time.time(), rec_id))
                result = "deleted"
        log.info("授乳の記録を取り消し（%s）: %s", result, rec_id)
        self.wake.set()
        return result

    def _check_start(self, start):
        t = datetime.fromisoformat(start)
        now = self.clock()
        if t.tzinfo is None:
            t = t.replace(tzinfo=now.tzinfo)
        if t > now + timedelta(minutes=1) or t < now - timedelta(hours=24):
            raise ValueError("開始時刻が正しくありません")
        return t.isoformat(timespec="seconds")

    # ------------------------------------------------------------ 集計（画面用）
    def _public(self, r):
        t = datetime.fromisoformat(r["ts"])
        out = {"id": r["id"], "ts": r["ts"], "time": t.strftime("%H:%M"), "status": r["status"],
               "milk": r["milk_ml"] or 0, "breast": r["breast_ml"] or 0, "amount": r["amount"]}
        if r.get("end_ts"):
            e = datetime.fromisoformat(r["end_ts"])
            out["end_ts"] = r["end_ts"]
            out["end_time"] = e.strftime("%H:%M")
            out["duration_min"] = max(0, int((e - t).total_seconds() // 60))
        return out

    def summary(self, recent=6):
        now = self.clock()
        day_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
        with self.lock, self._db() as c:
            rows = [dict(r) for r in c.execute("SELECT * FROM feeds WHERE deleted=0 ORDER BY ts DESC LIMIT 200")]
            pending = c.execute(
                "SELECT COUNT(*) FROM feeds WHERE (deleted=0 AND version>synced_ver) OR (deleted=1 AND synced_ver>0 AND synced_del=0)").fetchone()[0]
        done = [r for r in rows if r["status"] == "done"]
        opened = [r for r in rows if r["status"] == "open"]
        today = [r for r in done if datetime.fromisoformat(r["ts"]) >= day_start]
        out = {
            "now": now.isoformat(timespec="seconds"),
            "last": None,
            "open": None,
            "today_total": sum(r["amount"] for r in today),
            "today_milk": sum(r["milk_ml"] or 0 for r in today),
            "today_breast": sum(r["breast_ml"] or 0 for r in today),
            "today_count": len(today),
            "recent": [self._public(r) for r in today[:recent]],
            "pending": pending,
            "sync_enabled": self.sender is not None,
            "last_sync_ok": self.last_sync_ok,
        }
        if done:
            # 「前回から」は前回の開始時刻から数える
            last = self._public(done[0])
            mins = max(0, int((now - datetime.fromisoformat(done[0]["ts"])).total_seconds() // 60))
            last.update(minutes_ago=mins, ago_text=ago_text(mins))
            out["last"] = last
        if opened:
            o = self._public(opened[0])
            o["elapsed_min"] = max(0, int((now - datetime.fromisoformat(opened[0]["ts"])).total_seconds() // 60))
            # 量の初期値：前回と同じ（なければ0）
            o["suggest_milk"] = (done[0]["milk_ml"] or 0) if done else 0
            o["suggest_breast"] = (done[0]["breast_ml"] or 0) if done else 0
            out["open"] = o
        return out

    # ------------------------------------------------------------ 送信
    def sync_once(self):
        """未送信の変更を古い順に送る。送れた件数を返す"""
        if self.sender is None:
            return 0
        with self.lock, self._db() as c:
            rows = [dict(r) for r in c.execute("SELECT * FROM feeds ORDER BY created")]
        sent = 0
        for r in rows:
            if r["deleted"]:
                if r["synced_ver"] == 0:
                    if not r["synced_del"]:          # 送る前に取り消された → 何も送らない
                        self._mark(r["id"], synced_del=1)
                    continue
                if not r["synced_del"]:
                    if not self._send({"action": "delete", "id": r["id"]}):
                        return sent
                    self._mark(r["id"], synced_del=1)
                    sent += 1
                continue
            if r["version"] > r["synced_ver"]:
                done = r["status"] == "done"
                payload = {"action": "upsert", "id": r["id"], "start": r["ts"], "end": r["end_ts"] if done else "",
                           "milk": r["milk_ml"] if done else "", "breast": r["breast_ml"] if done else "",
                           "total": r["amount"] if done else "", "source": r["source"]}
                if not self._send(payload):
                    return sent
                self._mark(r["id"], synced_ver=r["version"])
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
                    log.exception("授乳記録の送信処理でエラー")
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
            err = res.get("error", "Apps Script がエラーを返しました")
            if "不明な action" in err:
                err += "（Apps Script が古いままです。gas/milk_sheet.gs を貼り直して、新しいバージョンでデプロイしてください）"
            raise RuntimeError(err)
        return True
    return send
