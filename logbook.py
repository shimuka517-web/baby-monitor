"""
育児の記録（おむつ・体重）と、部屋の温湿度の履歴

- 記録はまずラズパイ本体の SQLite（data/logbook.db）に保存する（ネットが切れていても失われない）
- スプレッドシートへの送信は「送信待ちの列（outbox）」に積んで、古い順に送る。失敗したら時間をおいて再送
- 温湿度は5分ごとの値をラズパイ内に30日分残し、シートには1時間ごとの平均を送る

baby_monitor.py から使う。単体でテストできるよう、カメラや画面には依存しない。
"""
import json
import logging
import os
import sqlite3
import threading
import time
import uuid
from datetime import date, datetime, timedelta

from milk import ago_text, now_local

log = logging.getLogger("baby-monitor.logbook")

DIAPER_KINDS = {"pee": "おしっこ", "poo": "うんち", "both": "おしっこ＋うんち"}
SHEETS = {
    "おむつ": ["日時", "種類", "記録した場所", "ID"],
    "体重": ["日付", "体重(g)", "メモ", "ID"],
    "室温": ["日時", "室温(℃)", "湿度(%)", "ID"],
}
ENV_KEEP_DAYS = 30


class Logbook:
    def __init__(self, db_path, sender=None, retry_sec=60, clock=now_local):
        os.makedirs(os.path.dirname(os.path.abspath(db_path)), exist_ok=True)
        self.db_path = db_path
        self.sender = sender
        self.retry_sec = retry_sec
        self.clock = clock
        self.lock = threading.Lock()
        self.wake = threading.Event()
        self.last_sync_ok = None
        self.last_error = ""
        with self._db() as c:
            c.executescript("""
            CREATE TABLE IF NOT EXISTS diapers (
                id TEXT PRIMARY KEY, ts TEXT NOT NULL, kind TEXT NOT NULL, source TEXT NOT NULL,
                deleted INTEGER NOT NULL DEFAULT 0, created REAL NOT NULL);
            CREATE TABLE IF NOT EXISTS weights (
                day TEXT PRIMARY KEY, grams INTEGER NOT NULL, note TEXT NOT NULL DEFAULT '', created REAL NOT NULL);
            CREATE TABLE IF NOT EXISTS env (
                ts TEXT NOT NULL, temp REAL, hum REAL);
            CREATE INDEX IF NOT EXISTS env_ts ON env(ts);
            CREATE TABLE IF NOT EXISTS outbox (
                seq INTEGER PRIMARY KEY AUTOINCREMENT, payload TEXT NOT NULL, created REAL NOT NULL);
            CREATE TABLE IF NOT EXISTS meta (k TEXT PRIMARY KEY, v TEXT);
            """)

    def _db(self):
        c = sqlite3.connect(self.db_path, timeout=10)
        c.row_factory = sqlite3.Row
        return c

    def _queue(self, c, payload):
        # シート未設定でも積んでおく（あとで設定したときに、それまでの記録も送られる）
        if True:
            c.execute("INSERT INTO outbox (payload, created) VALUES (?, ?)",
                      (json.dumps(payload, ensure_ascii=False), time.time()))

    @staticmethod
    def _row(table, rec_id, row):
        return {"action": "row", "table": table, "header": SHEETS[table], "id": rec_id, "row": row}

    @staticmethod
    def _row_delete(table, rec_id):
        return {"action": "row_delete", "table": table, "header": SHEETS[table], "id": rec_id}

    # ------------------------------------------------------------ おむつ
    def add_diaper(self, kind, source="ベッド横の画面", minutes_ago=0):
        if kind not in DIAPER_KINDS:
            raise ValueError("おむつの種類は pee / poo / both のどれか")
        minutes_ago = max(0, min(24 * 60, int(minutes_ago or 0)))
        ts = (self.clock() - timedelta(minutes=minutes_ago)).isoformat(timespec="seconds")
        rec_id = "d" + uuid.uuid4().hex[:11]
        with self.lock, self._db() as c:
            c.execute("INSERT INTO diapers (id, ts, kind, source, created) VALUES (?,?,?,?,?)",
                      (rec_id, ts, kind, source, time.time()))
            self._queue(c, self._row("おむつ", rec_id, [ts, DIAPER_KINDS[kind], source, rec_id]))
        log.info("おむつを記録: %s %s", ts, DIAPER_KINDS[kind])
        self.wake.set()
        return {"id": rec_id, "ts": ts, "kind": kind, "label": DIAPER_KINDS[kind]}

    def undo_diaper(self, rec_id, within_sec=600):
        with self.lock, self._db() as c:
            r = c.execute("SELECT * FROM diapers WHERE id=? AND deleted=0", (rec_id,)).fetchone()
            if r is None:
                raise KeyError("その記録は見つかりません")
            if time.time() - r["created"] > within_sec:
                raise ValueError("時間がたった記録は、スプレッドシート側で直してください")
            c.execute("UPDATE diapers SET deleted=1 WHERE id=?", (rec_id,))
            self._queue(c, self._row_delete("おむつ", rec_id))
        self.wake.set()
        return True

    def diaper_summary(self, recent=8):
        now = self.clock()
        day_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
        with self.lock, self._db() as c:
            rows = [dict(r) for r in c.execute(
                "SELECT id, ts, kind FROM diapers WHERE deleted=0 ORDER BY ts DESC LIMIT 300")]
        today = [r for r in rows if datetime.fromisoformat(r["ts"]) >= day_start]

        def last_of(kinds):
            for r in rows:
                if r["kind"] in kinds:
                    t = datetime.fromisoformat(r["ts"])
                    mins = max(0, int((now - t).total_seconds() // 60))
                    return {"id": r["id"], "time": t.strftime("%H:%M"), "kind": r["kind"],
                            "label": DIAPER_KINDS[r["kind"]], "minutes_ago": mins, "ago_text": ago_text(mins)}
            return None
        return {
            "last": last_of(("pee", "poo", "both")),
            "last_poo": last_of(("poo", "both")),
            "today_count": len(today),
            "today_pee": sum(1 for r in today if r["kind"] in ("pee", "both")),
            "today_poo": sum(1 for r in today if r["kind"] in ("poo", "both")),
            "recent": [{"id": r["id"], "time": datetime.fromisoformat(r["ts"]).strftime("%H:%M"),
                        "kind": r["kind"], "label": DIAPER_KINDS[r["kind"]]} for r in today[:recent]],
        }

    # ------------------------------------------------------------ 体重（1日1件。同じ日に入れ直したら上書き）
    def set_weight(self, grams, day=None, note=""):
        grams = int(grams)
        if not (1000 <= grams <= 30000):
            raise ValueError("体重は1000〜30000g（グラム）で入れてください")
        day = day or self.clock().date().isoformat()
        d = date.fromisoformat(day)
        if d > self.clock().date():
            raise ValueError("未来の日付は入れられません")
        note = str(note or "")[:60]
        with self.lock, self._db() as c:
            c.execute("INSERT INTO weights (day, grams, note, created) VALUES (?,?,?,?) "
                      "ON CONFLICT(day) DO UPDATE SET grams=excluded.grams, note=excluded.note",
                      (d.isoformat(), grams, note, time.time()))
            self._queue(c, self._row("体重", "w" + d.isoformat(), [d.isoformat(), grams, note, "w" + d.isoformat()]))
        log.info("体重を記録: %s %dg", d, grams)
        self.wake.set()
        return {"day": d.isoformat(), "grams": grams, "note": note}

    def delete_weight(self, day):
        d = date.fromisoformat(day).isoformat()
        with self.lock, self._db() as c:
            if c.execute("DELETE FROM weights WHERE day=?", (d,)).rowcount == 0:
                raise KeyError("その日の記録はありません")
            self._queue(c, self._row_delete("体重", "w" + d))
        self.wake.set()
        return True

    def weights(self):
        with self.lock, self._db() as c:
            return [dict(r) for r in c.execute("SELECT day, grams, note FROM weights ORDER BY day")]

    # ------------------------------------------------------------ 室温・湿度
    def add_env(self, temp, hum):
        now = self.clock()
        ts = now.isoformat(timespec="seconds")
        hour = now.replace(minute=0, second=0, microsecond=0)
        with self.lock, self._db() as c:
            c.execute("INSERT INTO env (ts, temp, hum) VALUES (?,?,?)", (ts, temp, hum))
            c.execute("DELETE FROM env WHERE ts < ?", ((now - timedelta(days=ENV_KEEP_DAYS)).isoformat(),))
            # 時間が変わったら、前の1時間の平均をシートへ
            last = c.execute("SELECT v FROM meta WHERE k='env_hour'").fetchone()
            if last and last["v"] != hour.isoformat():
                prev = datetime.fromisoformat(last["v"])
                avg = c.execute("SELECT AVG(temp) t, AVG(hum) h FROM env WHERE ts >= ? AND ts < ?",
                                (prev.isoformat(), hour.isoformat())).fetchone()
                if avg["t"] is not None:
                    rid = "e" + prev.strftime("%Y%m%d%H")
                    self._queue(c, self._row("室温", rid, [prev.isoformat(timespec="seconds"),
                                                         round(avg["t"], 1), round(avg["h"]), rid]))
                    self.wake.set()
            c.execute("INSERT INTO meta (k, v) VALUES ('env_hour', ?) ON CONFLICT(k) DO UPDATE SET v=excluded.v",
                      (hour.isoformat(),))

    def env_summary(self, hours=24, step_min=15):
        now = self.clock()
        since = now - timedelta(hours=hours)
        with self.lock, self._db() as c:
            rows = [dict(r) for r in c.execute(
                "SELECT ts, temp, hum FROM env WHERE ts >= ? ORDER BY ts", (since.isoformat(),))]
        if not rows:
            return {"latest": None, "series": [], "trend": None}
        # step_min ごとの平均にまとめる（グラフ用）
        buckets = {}
        for r in rows:
            t = datetime.fromisoformat(r["ts"])
            k = int((t - since).total_seconds() // (step_min * 60))
            buckets.setdefault(k, []).append(r)
        series = []
        for k in sorted(buckets):
            b = buckets[k]
            t = since + timedelta(minutes=step_min * k + step_min / 2)
            series.append({"t": t.isoformat(timespec="minutes"),
                           "temp": round(sum(x["temp"] for x in b) / len(b), 1),
                           "hum": round(sum(x["hum"] for x in b) / len(b), 1)})
        last = rows[-1]
        lt = datetime.fromisoformat(last["ts"])
        # 1時間前との差（上がっている／下がっている）
        hour_ago = [r for r in rows if datetime.fromisoformat(r["ts"]) <= lt - timedelta(minutes=55)]
        trend = None
        if hour_ago:
            p = hour_ago[-1]
            trend = {"temp": round(last["temp"] - p["temp"], 1), "hum": round(last["hum"] - p["hum"], 1)}
        return {"latest": {"temp": last["temp"], "hum": last["hum"], "time": lt.strftime("%H:%M"),
                           "minutes_ago": max(0, int((now - lt).total_seconds() // 60))},
                "series": series, "trend": trend}

    # ------------------------------------------------------------ 送信
    def pending(self):
        with self.lock, self._db() as c:
            return c.execute("SELECT COUNT(*) FROM outbox").fetchone()[0]

    def sync_once(self):
        if self.sender is None:
            return 0
        sent = 0
        while True:
            with self.lock, self._db() as c:
                r = c.execute("SELECT seq, payload FROM outbox ORDER BY seq LIMIT 1").fetchone()
            if r is None:
                return sent
            try:
                ok = bool(self.sender(json.loads(r["payload"])))
            except Exception as e:
                ok = False
                self.last_error = str(e)
            self.last_sync_ok = ok
            if not ok:
                log.warning("記録のスプレッドシート送信に失敗（あとで再送します）: %s", self.last_error)
                return sent
            with self.lock, self._db() as c:
                c.execute("DELETE FROM outbox WHERE seq=?", (r["seq"],))
            sent += 1

    def start_sync_thread(self):
        if self.sender is None:
            return

        def loop():
            while True:
                try:
                    self.sync_once()
                except Exception:
                    log.exception("記録の送信処理でエラー")
                self.wake.wait(self.retry_sec)
                self.wake.clear()
        threading.Thread(target=loop, daemon=True, name="logbook-sync").start()
