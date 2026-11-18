#!/usr/bin/env python3
"""
赤ちゃん見守りカメラ v2 (Raspberry Pi 4 + Raspberry Pi Camera)

見守りの流れ（状態遷移）
  PAUSED   一時停止中。何もしない。
  WAITING  顔の確認待ち。顔が confirm_sec 秒続けて見えたら WATCHING へ。
           この間はアラートを送らない（起動直後・寝かせる前の誤報防止）。
  WATCHING 見守り中。顔が absent_sec 秒続けて見えなければ ALERT へ。
  ALERT    通知済み。見えないままなら repeat_sec ごとに再通知。
           顔が recover_sec 秒続けて見えたら「復帰」を通知して WATCHING へ。

画面表示
  顔を検出しているとき → 緑の枠で顔を囲む
  顔を検出できないとき → 最後に顔があった位置を赤の枠で表示

設定は config/settings.toml（調整用）と config/secrets.toml（通知先・パスワード）。
"""
import base64
import copy
import json
import logging
import os
import smtplib
import threading
import time
import tomllib
from datetime import datetime
from email.message import EmailMessage
from http import server
from socketserver import ThreadingMixIn

import cv2
import requests

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
CONFIG_DIR = os.environ.get("BABY_MONITOR_CONFIG_DIR", os.path.join(BASE_DIR, "config"))
SETTINGS_PATH = os.path.join(CONFIG_DIR, "settings.toml")
SECRETS_PATH = os.path.join(CONFIG_DIR, "secrets.toml")
MODEL_PATH = os.environ.get(
    "BABY_MONITOR_MODEL", os.path.join(BASE_DIR, "models", "face_detection_yunet_2023mar.onnx"))

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger("baby-monitor")


# ================================================================ 設定の読み込み
DEFAULTS = {
    "alert": {
        "confirm_sec": 10.0,
        "dropout_tolerance_sec": 2.0,
        "absent_sec": 15.0,
        "repeat_sec": 120.0,
        "recover_sec": 3.0,
        "waiting_notify_sec": 600.0,
        "armed_on_start": True,
    },
    "detection": {"score_threshold": 0.6, "try_rotations": True, "detect_width": 640},
    "camera": {"width": 1280, "height": 720, "rotate": 0},
    "web": {"port": 8000, "jpeg_quality": 80},
    "milk": {"default_ml": 100, "step_ml": 10, "max_ml": 300,
             "presets": [40, 60, 80, 100, 120, 160], "undo_sec": 10},
}
SECRET_DEFAULTS = {
    "discord": {"webhook_url": ""},
    "gmail": {"user": "", "app_password": "", "to": []},
    "web": {"user": "", "password": ""},
    "sheets": {"gas_url": "", "token": ""},
}
DATA_DIR = os.environ.get("BABY_MONITOR_DATA_DIR", os.path.join(BASE_DIR, "data"))
WEB_DIR = os.path.join(BASE_DIR, "web")


def _merge(defaults, loaded, where):
    out = copy.deepcopy(defaults)
    for sec, values in loaded.items():
        if sec not in out or not isinstance(values, dict):
            log.warning("%s: 不明なセクション [%s] は無視します", where, sec)
            continue
        for k, v in values.items():
            if k not in out[sec]:
                log.warning("%s: 不明な項目 [%s] %s は無視します", where, sec, k)
                continue
            d = out[sec][k]
            try:
                if isinstance(d, bool):
                    if not isinstance(v, bool):
                        raise TypeError
                elif isinstance(d, float):
                    v = float(v)
                elif isinstance(d, int):
                    v = int(v)
                elif isinstance(d, list) and isinstance(v, str):
                    v = [v]
            except (TypeError, ValueError):
                log.warning("%s: [%s] %s の値 %r が不正なため既定値 %r を使います", where, sec, k, v, d)
                continue
            out[sec][k] = v
    return out


def load_toml(path, defaults, required=False):
    if not os.path.exists(path):
        if required:
            log.warning("%s が見つからないため既定値で動作します", path)
        return copy.deepcopy(defaults)
    with open(path, "rb") as f:
        return _merge(defaults, tomllib.load(f), os.path.basename(path))


class Config:
    """settings.toml を監視し、変更されたら [alert] [detection] を自動で反映する"""

    def __init__(self):
        self.lock = threading.Lock()
        self.mtime = self._mtime()
        self.s = load_toml(SETTINGS_PATH, DEFAULTS, required=True)
        self.secrets = load_toml(SECRETS_PATH, SECRET_DEFAULTS)
        self._last_check = 0.0

    @staticmethod
    def _mtime():
        try:
            return os.path.getmtime(SETTINGS_PATH)
        except OSError:
            return None

    def maybe_reload(self):
        """変更があれば再読み込みして True を返す（2秒に1回だけ確認）"""
        now = time.monotonic()
        if now - self._last_check < 2.0:
            return False
        self._last_check = now
        m = self._mtime()
        if m == self.mtime:
            return False
        self.mtime = m
        try:
            new = load_toml(SETTINGS_PATH, DEFAULTS, required=True)
        except Exception as e:
            log.error("settings.toml の読み込みに失敗（書式エラー？）。前の設定のまま動作します: %s", e)
            return False
        with self.lock:
            for sec in ("camera", "web"):
                if new[sec] != self.s[sec]:
                    log.warning("[%s] の変更は再起動後に反映されます", sec)
                new[sec] = self.s[sec]
            self.s = new
        log.info("設定を再読み込みしました: alert=%s detection=%s", new["alert"], new["detection"])
        return True

    @property
    def alert(self):
        return self.s["alert"]

    @property
    def detection(self):
        return self.s["detection"]


cfg = Config()


# ================================================================ 通知
def _send_discord(text, jpeg):
    url = cfg.secrets["discord"]["webhook_url"]
    if not url:
        return
    try:
        if jpeg:
            r = requests.post(url, data={"content": text},
                              files={"file": ("snapshot.jpg", jpeg, "image/jpeg")}, timeout=15)
        else:
            r = requests.post(url, json={"content": text}, timeout=15)
        r.raise_for_status()
        log.info("Discord通知を送信しました")
    except Exception as e:
        log.error("Discord通知に失敗: %s", e)


def _send_gmail(text, jpeg):
    g = cfg.secrets["gmail"]
    user, pw, to = g["user"], g["app_password"].replace(" ", ""), [a for a in g["to"] if a]
    if not (user and pw and to):
        return
    try:
        msg = EmailMessage()
        msg["Subject"] = "[見守りカメラ] " + text.splitlines()[0]
        msg["From"] = user
        msg["To"] = ", ".join(to)
        msg.set_content(text)
        if jpeg:
            msg.add_attachment(jpeg, maintype="image", subtype="jpeg", filename="snapshot.jpg")
        with smtplib.SMTP_SSL("smtp.gmail.com", 465, timeout=20) as s:
            s.login(user, pw)
            s.send_message(msg)
        log.info("Gmail通知を送信しました")
    except Exception as e:
        log.error("Gmail通知に失敗: %s", e)


def notify_sync(text, jpeg=None):
    _send_discord(text, jpeg)
    _send_gmail(text, jpeg)


def notify(text, jpeg=None):
    """メインループを止めないよう別スレッドで送信"""
    threading.Thread(target=notify_sync, args=(text, jpeg), daemon=True).start()


# ================================================================ 顔検出 (OpenCV YuNet)
class FaceDetector:
    def __init__(self, model_path, score_threshold):
        if not os.path.exists(model_path):
            raise FileNotFoundError(f"顔検出モデルが見つかりません: {model_path}")
        self.det = cv2.FaceDetectorYN.create(model_path, "", (320, 320), score_threshold, 0.3, 5000)

    def set_threshold(self, th):
        self.det.setScoreThreshold(float(th))

    def _run(self, img):
        h, w = img.shape[:2]
        self.det.setInputSize((w, h))
        _, faces = self.det.detect(img)
        if faces is None:
            return []
        return [(float(f[0]), float(f[1]), float(f[2]), float(f[3]), float(f[14])) for f in faces]

    def detect(self, img, try_rotations):
        """顔のリスト [(x, y, w, h, score), ...] を img の座標系で返す"""
        faces = self._run(img)
        if faces or not try_rotations:
            return faces
        h, w = img.shape[:2]
        faces = self._run(cv2.rotate(img, cv2.ROTATE_90_CLOCKWISE))
        if faces:
            return [(fy, h - fx - fw, fh, fw, s) for (fx, fy, fw, fh, s) in faces]
        faces = self._run(cv2.rotate(img, cv2.ROTATE_90_COUNTERCLOCKWISE))
        if faces:
            return [(w - fy - fh, fx, fh, fw, s) for (fx, fy, fw, fh, s) in faces]
        return []


# ================================================================ 見守りの状態管理
PAUSED, WAITING, WATCHING, ALERT = "PAUSED", "WAITING", "WATCHING", "ALERT"
PHASE_JA = {PAUSED: "一時停止中", WAITING: "顔の確認待ち", WATCHING: "見守り中", ALERT: "アラート中"}


class Monitor:
    """顔が見えたか否かの時系列から、状態遷移と通知のタイミングを決める（カメラに依存しない）"""

    def __init__(self, armed, now):
        self.phase = WAITING if armed else PAUSED
        self.phase_since = now
        self.last_seen = None        # 最後に顔が見えた時刻
        self.streak_start = None     # 「続けて見えている」区間の開始時刻
        self.last_alert = 0.0
        self.waiting_notified = False

    def set_armed(self, armed, now):
        self._enter(WAITING if armed else PAUSED, now)
        self.streak_start = None

    def _enter(self, phase, now):
        self.phase = phase
        self.phase_since = now
        self.waiting_notified = False

    def streak_sec(self, now):
        return 0.0 if self.streak_start is None else now - self.streak_start

    def absent_sec(self, now):
        return 0.0 if self.last_seen is None else now - self.last_seen

    def update(self, visible, now, a):
        """1フレーム分の更新。発生したイベント名のリストを返す"""
        events = []
        # 「続けて見えている」区間の管理（dropout_tolerance_sec 以内の途切れは許容）
        if visible:
            if self.streak_start is None:
                self.streak_start = now
            self.last_seen = now
        elif self.streak_start is not None and now - self.last_seen > a["dropout_tolerance_sec"]:
            self.streak_start = None

        if self.phase == WAITING:
            if self.streak_sec(now) >= a["confirm_sec"]:
                self._enter(WATCHING, now)
                events.append("confirmed")
            elif (a["waiting_notify_sec"] > 0 and not self.waiting_notified
                  and now - self.phase_since >= a["waiting_notify_sec"]):
                self.waiting_notified = True
                events.append("waiting_long")

        elif self.phase == WATCHING:
            if not visible and self.absent_sec(now) >= a["absent_sec"]:
                self._enter(ALERT, now)
                self.last_alert = now
                events.append("alert")

        elif self.phase == ALERT:
            # 復帰判定は、アラート後に見え始めてから recover_sec 続いたか
            if self.streak_start is not None and now - max(self.streak_start, self.phase_since) >= a["recover_sec"]:
                self._enter(WATCHING, now)
                events.append("recovered")
            elif not visible and now - self.last_alert >= a["repeat_sec"]:
                self.last_alert = now
                events.append("repeat")
        return events


# ================================================================ 共有状態（Web用）
class Shared:
    def __init__(self):
        self.lock = threading.Lock()
        self.cond = threading.Condition()
        self.jpeg = None
        self.status = {}
        self.arm_request = None  # Webからの開始/停止要求

    def set_jpeg(self, data):
        with self.cond:
            self.jpeg = data
            self.cond.notify_all()

    def wait_jpeg(self, timeout=5):
        with self.cond:
            self.cond.wait(timeout=timeout)
            return self.jpeg


shared = Shared()
MILK = None   # milk.MilkLog（main() で用意）


def init_milk():
    """ミルク記録を用意する（スプレッドシートの設定があれば送信も開始）"""
    global MILK
    from milk import MilkLog, make_gas_sender
    sh = cfg.secrets["sheets"]
    sender = make_gas_sender(sh["gas_url"], sh["token"]) if sh["gas_url"] else None
    MILK = MilkLog(os.path.join(DATA_DIR, "milk.db"), sender=sender)
    MILK.start_sync_thread()
    log.info("ミルク記録: %s", "スプレッドシートへ送信します" if sender else "ラズパイ内にだけ保存します（送信先未設定）")
    return MILK


# ================================================================ Web配信
PAGE = """<!doctype html>
<html lang="ja"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>見守りカメラ</title>
<style>
 html,body{margin:0;height:100%;background:#000;color:#eee;font-family:sans-serif}
 #wrap{display:flex;flex-direction:column;height:100%}
 #video{flex:1;display:flex;align-items:center;justify-content:center;overflow:hidden;border:6px solid transparent;box-sizing:border-box}
 #video.alert{border-color:#ff1744;animation:blink 1s infinite}
 @keyframes blink{50%{border-color:transparent}}
 #video img{max-width:100%;max-height:100%}
 #bar{display:flex;gap:12px;align-items:center;padding:8px 12px;background:#111;font-size:18px;flex-wrap:wrap}
 #st{flex:1}
 .ng{color:#ff5252;font-weight:bold}.ok{color:#69f0ae}.wait{color:#ffd740}.gray{color:#aaa}
 button{font-size:16px;padding:6px 14px}
 #milk{display:flex;flex-direction:column;justify-content:center;min-height:56px;padding:4px 18px;border-radius:14px;background:#E4EEFA;color:#12407E;text-decoration:none;font-weight:700;font-size:22px;line-height:1.2}
 #milk small{font-size:14px;font-weight:500}
</style></head><body>
<div id="wrap">
 <div id="video"><img src="/stream.mjpg" alt="stream"></div>
 <div id="bar">
  <span id="st">接続中…</span>
  <a id="milk" href="/milk">ミルク<small id="milklast">記録なし</small></a>
  <button onclick="setArm(true)">見守り開始</button>
  <button onclick="setArm(false)">一時停止</button>
 </div>
</div>
<script>
async function pollMilk(){
 try{
  const m=await (await fetch('/api/milk')).json();
  document.getElementById('milklast').textContent=m.last?('前回 '+m.last.ago_text+'・'+m.last.amount+'ml'):'記録なし';
 }catch(e){}
}
setInterval(pollMilk,30000);pollMilk();
async function setArm(on){await fetch(on?'/arm':'/disarm',{method:'POST'});setTimeout(poll,300);}
async function poll(){
 try{
  const s=await (await fetch('/status')).json();
  let t='';
  if(s.phase==='PAUSED') t='<span class="gray">一時停止中</span>';
  else if(s.phase==='WAITING') t='<span class="wait">顔の確認待ち（'+s.confirm_progress+' / '+s.confirm_sec+'秒）</span>';
  else if(s.phase==='WATCHING') t=s.face_visible?'<span class="ok">見守り中・顔OK</span>'
        :'<span class="ng">見守り中・顔が見えません（'+s.absent_sec+' / '+s.absent_limit+'秒）</span>';
  else if(s.phase==='ALERT') t='<span class="ng">⚠ アラート中・顔が見えません（'+s.absent_sec+'秒）</span>';
  t+=' ｜ '+s.fps+' fps';
  document.getElementById('st').innerHTML=t;
  document.getElementById('video').className=s.phase==='ALERT'?'alert':'';
 }catch(e){document.getElementById('st').textContent='接続が切れました（再接続中）';}
}
setInterval(poll,1000);poll();
document.querySelector('#video img').onerror=()=>setTimeout(()=>location.reload(),3000);
</script>
</body></html>
"""


class Handler(server.BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        pass

    def _auth_ok(self):
        u, p = cfg.secrets["web"]["user"], cfg.secrets["web"]["password"]
        if not u:
            return True
        token = base64.b64encode(f"{u}:{p}".encode()).decode()
        if self.headers.get("Authorization") == f"Basic {token}":
            return True
        self.send_response(401)
        self.send_header("WWW-Authenticate", 'Basic realm="baby-monitor"')
        self.end_headers()
        return False

    def _send(self, code, ctype, body):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-cache")
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if not self._auth_ok():
            return
        if self.path in ("/", "/index.html"):
            self._send(200, "text/html; charset=utf-8", PAGE.encode())
        elif self.path == "/status":
            with shared.lock:
                body = json.dumps(shared.status).encode()
            self._send(200, "application/json", body)
        elif self.path == "/milk":
            with open(os.path.join(WEB_DIR, "milk.html"), "rb") as f:
                self._send(200, "text/html; charset=utf-8", f.read())
        elif self.path == "/api/milk":
            self._json(200, self._milk_state())
        elif self.path == "/snapshot.jpg":
            jpeg = shared.jpeg
            if jpeg:
                self._send(200, "image/jpeg", jpeg)
            else:
                self._send(503, "text/plain", b"no frame yet")
        elif self.path == "/stream.mjpg":
            self.send_response(200)
            self.send_header("Age", "0")
            self.send_header("Cache-Control", "no-cache, private")
            self.send_header("Pragma", "no-cache")
            self.send_header("Content-Type", "multipart/x-mixed-replace; boundary=FRAME")
            self.end_headers()
            try:
                while True:
                    jpeg = shared.wait_jpeg()
                    if not jpeg:
                        continue
                    self.wfile.write(b"--FRAME\r\nContent-Type: image/jpeg\r\n")
                    self.wfile.write(f"Content-Length: {len(jpeg)}\r\n\r\n".encode())
                    self.wfile.write(jpeg)
                    self.wfile.write(b"\r\n")
            except (BrokenPipeError, ConnectionResetError):
                pass
        else:
            self._send(404, "text/plain", b"not found")

    def do_POST(self):
        if not self._auth_ok():
            return
        if self.path in ("/arm", "/disarm"):
            armed = self.path == "/arm"
            with shared.lock:
                shared.arm_request = armed
            self._send(200, "application/json", json.dumps({"armed": armed}).encode())
        elif self.path in ("/api/milk", "/api/milk/undo"):
            try:
                n = int(self.headers.get("Content-Length") or 0)
                body = json.loads(self.rfile.read(n) or b"{}")
                if self.path == "/api/milk":
                    rec = MILK.add(body.get("amount", 0), kind=body.get("kind", "ミルク"),
                                   source=body.get("source", "ベッド横の画面"))
                    self._json(200, {"ok": True, "record": rec, **self._milk_state()})
                else:
                    MILK.undo(str(body.get("id", "")))
                    self._json(200, {"ok": True, **self._milk_state()})
            except (ValueError, KeyError, TypeError) as e:
                self._json(400, {"ok": False, "error": str(e).strip("'\"")})
        else:
            self._send(404, "text/plain", b"not found")

    def _json(self, code, obj):
        self._send(code, "application/json; charset=utf-8", json.dumps(obj, ensure_ascii=False).encode())

    def _milk_state(self):
        st = MILK.summary()
        st["config"] = cfg.s["milk"]
        return st


class StreamingServer(ThreadingMixIn, server.HTTPServer):
    allow_reuse_address = True
    daemon_threads = True


# ================================================================ 描画
GREEN, RED, YELLOW, GRAY, WHITE = (0, 220, 0), (0, 0, 255), (0, 215, 255), (180, 180, 180), (255, 255, 255)


def smooth_box(prev, new, alpha=0.5):
    """枠のちらつきを抑える（位置が大きく飛んだら追従し直す）"""
    if prev is None:
        return new
    px, py, pw, ph = prev
    nx, ny, nw, nh = new
    if abs((nx + nw / 2) - (px + pw / 2)) > max(pw, nw) or abs((ny + nh / 2) - (py + ph / 2)) > max(ph, nh):
        return new
    return tuple(alpha * n + (1 - alpha) * p for p, n in zip(prev, new))


def put_label(frame, text, org, color, scale=0.8):
    (tw, th), _ = cv2.getTextSize(text, cv2.FONT_HERSHEY_SIMPLEX, scale, 2)
    x, y = org
    cv2.rectangle(frame, (x - 4, y - th - 6), (x + tw + 4, y + 6), (0, 0, 0), -1)
    cv2.putText(frame, text, (x, y), cv2.FONT_HERSHEY_SIMPLEX, scale, color, 2, cv2.LINE_AA)


def draw_overlay(frame, box, visible, mon, now, a):
    fh, fw = frame.shape[:2]
    thick = max(2, fw // 320)
    if box is not None:
        x, y, w, h = (int(v) for v in box)
        color = GREEN if visible else RED
        cv2.rectangle(frame, (x, y), (x + w, y + h), color, thick)
        label = "FACE" if visible else f"LOST {mon.absent_sec(now):.0f}s"
        put_label(frame, label, (x, max(y - 10, 24)), color, 0.6)

    if mon.phase == PAUSED:
        status, color = "PAUSED", GRAY
    elif mon.phase == WAITING:
        status, color = f"CONFIRMING {min(mon.streak_sec(now), a['confirm_sec']):.0f}/{a['confirm_sec']:.0f}s", YELLOW
    elif mon.phase == WATCHING:
        status, color = ("WATCHING", GREEN) if visible else (
            f"NO FACE {mon.absent_sec(now):.0f}/{a['absent_sec']:.0f}s", RED)
    else:
        status, color = f"ALERT  NO FACE {mon.absent_sec(now):.0f}s", RED
        cv2.rectangle(frame, (0, 0), (fw - 1, fh - 1), RED, thick * 3)

    put_label(frame, datetime.now().strftime("%Y-%m-%d %H:%M:%S"), (12, 32), WHITE)
    put_label(frame, status, (12, 68), color)


# ================================================================ メイン
ROTATE_CODES = {90: cv2.ROTATE_90_CLOCKWISE, 180: cv2.ROTATE_180, 270: cv2.ROTATE_90_COUNTERCLOCKWISE}


def main():
    from picamera2 import Picamera2  # ラズパイ上でのみ import

    cam_cfg, web_cfg = cfg.s["camera"], cfg.s["web"]
    detector = FaceDetector(MODEL_PATH, cfg.detection["score_threshold"])

    picam2 = Picamera2()
    picam2.configure(picam2.create_video_configuration(
        main={"size": (cam_cfg["width"], cam_cfg["height"]), "format": "RGB888"}))  # メモリ上は B,G,R 順
    picam2.start()
    time.sleep(1.0)

    init_milk()
    httpd = StreamingServer(("", web_cfg["port"]), Handler)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    log.info("配信開始: http://<ラズパイのIP>:%d/", web_cfg["port"])

    mon = Monitor(cfg.alert["armed_on_start"], time.monotonic())
    notify(f"見守りカメラを起動しました（{PHASE_JA[mon.phase]}）")
    box = None  # 表示用の顔の枠（全体画像の座標）
    frames, t_fps, fps = 0, time.monotonic(), 0.0

    while True:
        if cfg.maybe_reload():
            detector.set_threshold(cfg.detection["score_threshold"])
        a, d = cfg.alert, cfg.detection

        frame = picam2.capture_array()
        rot = cam_cfg["rotate"]
        if rot in ROTATE_CODES:
            frame = cv2.rotate(frame, ROTATE_CODES[rot])

        h, w = frame.shape[:2]
        scale = d["detect_width"] / w
        small = cv2.resize(frame, (d["detect_width"], int(h * scale)))
        faces = detector.detect(small, d["try_rotations"])
        visible = bool(faces)
        now = time.monotonic()

        if visible:
            fx, fy, fw_, fh_, _ = max(faces, key=lambda f: f[4])  # 一番確からしい顔
            box = smooth_box(box, (fx / scale, fy / scale, fw_ / scale, fh_ / scale))

        with shared.lock:
            req, shared.arm_request = shared.arm_request, None
        if req is not None:
            mon.set_armed(req, now)
            log.info("見守りを%sしました（ブラウザ操作）", "開始" if req else "一時停止")

        events = mon.update(visible, now, a)

        draw_overlay(frame, box, visible, mon, now, a)
        ok, buf = cv2.imencode(".jpg", frame, [cv2.IMWRITE_JPEG_QUALITY, web_cfg["jpeg_quality"]])
        jpeg = buf.tobytes() if ok else None
        if jpeg:
            shared.set_jpeg(jpeg)

        for ev in events:
            if ev == "confirmed":
                log.info("顔を %.0f 秒確認しました。見守りを開始します", a["confirm_sec"])
            elif ev in ("alert", "repeat"):
                sec = mon.absent_sec(now)
                log.warning("顔が %.0f 秒見えません。通知します", sec)
                notify(f"⚠️ 赤ちゃんの顔が {sec:.0f} 秒間見えていません。すぐに様子を確認してください。", jpeg)
            elif ev == "recovered":
                log.info("顔が再び見えるようになりました")
                notify("✅ 赤ちゃんの顔が再び見えるようになりました。", jpeg)
            elif ev == "waiting_long":
                log.warning("見守り開始から %.0f 秒、顔を確認できていません", a["waiting_notify_sec"])
                notify(f"ℹ️ 見守りを始めてから {a['waiting_notify_sec'] / 60:.0f} 分、まだ一度も顔を確認できていません。"
                       "カメラの向きや赤ちゃんの様子を確認してください。", jpeg)

        frames += 1
        if now - t_fps >= 5:
            fps, frames, t_fps = frames / (now - t_fps), 0, now
        with shared.lock:
            shared.status = {
                "phase": mon.phase,
                "face_visible": visible,
                "confirm_progress": round(min(mon.streak_sec(now), a["confirm_sec"]), 1),
                "confirm_sec": a["confirm_sec"],
                "absent_sec": round(mon.absent_sec(now), 1),
                "absent_limit": a["absent_sec"],
                "fps": round(fps, 1),
            }


def test_notify():
    """通知先の設定確認用：カメラを使わずにテスト通知だけ送る"""
    d, g = cfg.secrets["discord"], cfg.secrets["gmail"]
    if not d["webhook_url"] and not (g["user"] and g["app_password"]):
        print("通知先が設定されていません。config/secrets.toml を確認してください。")
        return 1
    print("Discord:", "設定あり" if d["webhook_url"] else "未設定",
          "/ Gmail:", "設定あり" if g["user"] and g["app_password"] else "未設定")
    notify_sync("🔔 見守りカメラのテスト通知です。これが届けば通知の設定は完了です。")
    print("送信しました。上に「〜に失敗」と出ていなければ、Discord/Gmailを確認してください。")
    return 0


if __name__ == "__main__":
    import sys
    if "--test-notify" in sys.argv:
        sys.exit(test_notify())
    try:
        main()
    except KeyboardInterrupt:
        log.info("終了します")
    except Exception as e:
        log.exception("異常終了")
        notify_sync(f"🛑 見守りカメラが異常停止しました（自動再起動を試みます）: {e}")
        raise
