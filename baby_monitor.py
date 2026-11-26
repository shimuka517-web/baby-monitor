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
import numpy as np
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
    "web": {"port": 8000, "jpeg_quality": 80, "camera_lan_only": True},
    "milk": {"default_ml": 100, "step_ml": 10, "breast_step_ml": 5, "max_ml": 300,
             "presets": [40, 60, 80, 100, 120, 160], "undo_sec": 10},
    "baby": {"birth_date": "", "name": ""},
    "env": {"enabled": True, "poll_sec": 300, "device_id": "", "alert": True,
            "temp_min": 20.0, "temp_max": 23.0, "hum_min": 40.0, "hum_max": 60.0},
    "weather": {"enabled": True, "latitude": 36.314, "longitude": 139.800, "place": "小山市"},
    "display": {"night_start": "21:00", "night_end": "07:00", "night_mode": "dim",
                "brightness_day": 100, "brightness_night": 10, "wake_sec": 30},
}
# タッチ画面の「設定」から変えられる項目（data/ui.json に保存し、settings.toml より優先）
UI_KEYS = {
    "absent_sec": ("alert", "absent_sec", lambda v: float(v) if float(v) in (10.0, 15.0, 30.0) else None),
    "night_mode": ("display", "night_mode", lambda v: v if v in ("dim", "off", "none") else None),
    "brightness_day": ("display", "brightness_day", lambda v: max(0, min(100, int(v)))),
    "brightness_night": ("display", "brightness_night", lambda v: max(0, min(100, int(v)))),
    "env_alert": ("env", "alert", lambda v: v if isinstance(v, bool) else str(v).lower() in ("1", "true", "on")),
    "discord_alert": (None, "discord_alert", lambda v: v if isinstance(v, bool) else str(v).lower() in ("1", "true", "on")),
    "discord_env": (None, "discord_env", lambda v: v if isinstance(v, bool) else str(v).lower() in ("1", "true", "on")),
    "head": (None, "head", lambda v: (LEGACY_HEAD.get(v, v) if LEGACY_HEAD.get(v, v) in HEAD_ROTATE else None)),
}
# 赤ちゃんの頭の向き（カメラの側から見て）→ 映像を回す角度（時計回り）
#   far=奥（映像の上） / right=右 / near=手前（カメラ側） / left=左
#   settings.toml の [camera] rotate は「奥が映像の上になる」ように合わせておく
HEAD_ROTATE = {"far": 0, "left": 90, "near": 180, "right": 270}
LEGACY_HEAD = {"camera": "far", "opposite": "near"}   # v2.4 までの値
SECRET_DEFAULTS = {
    "discord": {"webhook_url": ""},
    "gmail": {"user": "", "app_password": "", "to": []},
    "web": {"user": "", "password": ""},
    "sheets": {"gas_url": "", "token": ""},
    "switchbot": {"token": "", "secret": ""},
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
        self.ui_path = os.path.join(DATA_DIR, "ui.json")
        self.ui = self._load_ui()
        self.s = self._apply_ui(load_toml(SETTINGS_PATH, DEFAULTS, required=True))
        self.secrets = load_toml(SECRETS_PATH, SECRET_DEFAULTS)
        self._last_check = 0.0

    def _load_ui(self):
        try:
            with open(self.ui_path, encoding="utf-8") as f:
                d = json.load(f)
            d = {k: v for k, v in d.items() if k in UI_KEYS}
            if d.get("head") in LEGACY_HEAD:
                d["head"] = LEGACY_HEAD[d["head"]]
            return d
        except FileNotFoundError:
            return {}
        except Exception as e:
            log.warning("%s が読めないため無視します: %s", self.ui_path, e)
            return {}

    def _apply_ui(self, s):
        for key, v in self.ui.items():
            sec, name, _ = UI_KEYS[key]
            if sec:
                s[sec][name] = v
        return s

    def set_ui(self, changes):
        """タッチ画面の設定を保存して反映。不正な値は無視し、反映した項目を返す"""
        applied = {}
        for key, v in changes.items():
            if key not in UI_KEYS:
                continue
            try:
                v = UI_KEYS[key][2](v)
            except (TypeError, ValueError):
                v = None
            if v is not None:
                applied[key] = v
        if applied:
            with self.lock:
                self.ui.update(applied)
                self.s = self._apply_ui(self.s)
                os.makedirs(DATA_DIR, exist_ok=True)
                tmp = self.ui_path + ".tmp"
                with open(tmp, "w", encoding="utf-8") as f:
                    json.dump(self.ui, f, ensure_ascii=False, indent=1)
                os.replace(tmp, self.ui_path)
            log.info("画面から設定を変更しました: %s", applied)
        return applied

    @property
    def head(self):
        h = self.ui.get("head", "far")
        return h if h in HEAD_ROTATE else "far"

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
            self.s = self._apply_ui(new)
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


PUSH = None   # push.Push（アプリへの通知。main() で用意）
PUSH_STYLE = {   # 種類ごとの、アプリ通知のタイトルと開く画面
    "alert": ("見守りカメラ", "/cam"),
    "env": ("お部屋の温度・湿度", "/dash#roomCard"),
}


def notify(text, jpeg=None, kind="system"):
    """通知を送る（メインループを止めないよう別スレッドで）。
    kind: "alert"（見守りアラート）／"env"（お部屋の暑い・寒い）／"system"（起動・異常など）
    alert と env は、画面の設定に従ってアプリ（Web Push）と Discord/Gmail に振り分ける"""
    to_discord = kind == "system" or cfg.ui.get("discord_" + kind, True)
    if to_discord:
        threading.Thread(target=notify_sync, args=(text, jpeg), daemon=True).start()
    if PUSH is not None and kind in PUSH_STYLE:
        title, url = PUSH_STYLE[kind]
        PUSH.send_async(kind, title, text, url=url, tag=kind)


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
        self.acked = False           # アラート中に「様子を見ました」が押された
        self.armed_since = now if armed else None

    def set_armed(self, armed, now):
        self._enter(WAITING if armed else PAUSED, now)
        self.streak_start = None
        self.armed_since = now if armed else None

    def ack(self):
        """アラート中に「様子を見ました」：再通知を止め、顔が見えるまで待つ"""
        if self.phase == ALERT:
            self.acked = True
            return True
        return False

    def _enter(self, phase, now):
        self.phase = phase
        self.phase_since = now
        self.waiting_notified = False
        self.acked = False

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
            elif not visible and not self.acked and now - self.last_alert >= a["repeat_sec"]:
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
        self.ack_request = False  # Webからの「様子を見ました」

    def set_jpeg(self, data):
        with self.cond:
            self.jpeg = data
            self.cond.notify_all()

    def wait_jpeg(self, timeout=5):
        with self.cond:
            self.cond.wait(timeout=timeout)
            return self.jpeg


shared = Shared()


class Backlight:
    """液晶のバックライト（/sys/class/backlight/*）の明るさを 0〜100% で変える。
    書き込みには権限が必要（scripts/install-service.sh が udev ルールで video グループに許可）"""

    def __init__(self, root="/sys/class/backlight"):
        self.path, self.max = None, 0
        try:
            for name in sorted(os.listdir(root)):
                d = os.path.join(root, name)
                with open(os.path.join(d, "max_brightness")) as f:
                    self.max = int(f.read().strip())
                self.path = os.path.join(d, "brightness")
                break
        except (OSError, ValueError):
            self.path = None
        self.level = None

    @property
    def available(self):
        return self.path is not None and self.max > 0

    @property
    def writable(self):
        return self.available and os.access(self.path, os.W_OK)

    def set(self, pct):
        """明るさ（0〜100%）を設定。成功したら True"""
        pct = max(0, min(100, int(pct)))
        if not self.available:
            return False
        raw = round(self.max * pct / 100)
        if pct > 0:
            raw = max(1, raw)
        try:
            with open(self.path, "w") as f:
                f.write(str(raw))
            self.level = pct
            return True
        except OSError as e:
            log.warning("明るさを変えられません（%s）。scripts/install-service.sh をもう一度実行してください", e)
            return False


BACKLIGHT = None   # Backlight（main() で用意）
LAN_IP = ""        # ラズパイの家のLANでのIP（main() で調べる）
LOGBOOK = None     # logbook.Logbook（おむつ・体重・温湿度）
ENV = None         # home_env.EnvMonitor（SwitchBot）
WEATHER = None     # home_env.Weather
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


def init_home():
    """おむつ・体重の記録、部屋の温湿度（SwitchBot）、天気を用意する"""
    global LOGBOOK, ENV, WEATHER
    from logbook import Logbook
    from milk import make_gas_sender
    import home_env
    sh = cfg.secrets["sheets"]
    LOGBOOK = Logbook(os.path.join(DATA_DIR, "logbook.db"),
                      sender=make_gas_sender(sh["gas_url"], sh["token"]) if sh["gas_url"] else None)
    LOGBOOK.start_sync_thread()
    sb, e = cfg.secrets["switchbot"], cfg.s["env"]
    if e["enabled"] and sb["token"] and sb["secret"]:
        ENV = home_env.EnvMonitor(home_env.SwitchBot(sb["token"], sb["secret"], e["device_id"]), LOGBOOK,
                                  lambda: cfg.s["env"], lambda m: notify(m, kind="env"), poll_sec=max(60, e["poll_sec"]))
        ENV.start()
        log.info("部屋の温湿度: SwitchBot から %d 秒ごとに取得します", e["poll_sec"])
    else:
        log.info("部屋の温湿度: SwitchBot が未設定です（secrets.toml の [switchbot]）")
    w = cfg.s["weather"]
    if w["enabled"]:
        WEATHER = home_env.Weather(w["latitude"], w["longitude"], w["place"])
        WEATHER.start()


# ================================================================ Web配信
# ================================================================ 家のネットワークかどうか（カメラは家の中だけ）
import ipaddress
import socket

TAILSCALE_NETS = [ipaddress.ip_network("100.64.0.0/10"), ipaddress.ip_network("fd7a:115c:a1e0::/48")]


def is_home_network(ip):
    """家のLAN（192.168.x.x など）またはラズパイ自身なら True。Tailscale（100.64.0.0/10 など）やネットの向こうは False"""
    try:
        a = ipaddress.ip_address(ip.split("%")[0])
    except ValueError:
        return False
    if getattr(a, "ipv4_mapped", None):
        a = a.ipv4_mapped
    if any(a in n for n in TAILSCALE_NETS):
        return False
    return a.is_loopback or a.is_private or a.is_link_local


_TS_CACHE = {"t": 0.0, "peers": {}}


def tailscale_peers_at_home(status_json, lan):
    """`tailscale status --json` の結果から、家のLANで直接つながっている端末の Tailscale IP の集合を返す。
    （家のWi-Fiにいるスマホは、Tailscale の通り道が 192.168.x.x の直接接続になる。外出先では携帯回線や中継になる）"""
    home = set()
    try:
        lan_net = ipaddress.ip_network(lan + "/24", strict=False) if lan else None
    except ValueError:
        lan_net = None
    for peer in (status_json.get("Peer") or {}).values():
        addr = peer.get("CurAddr") or ""
        host = addr.rsplit(":", 1)[0].strip("[]") if addr else ""
        try:
            a = ipaddress.ip_address(host)
        except ValueError:
            continue
        if lan_net is not None and a.version == 4 and a in lan_net:
            home.update(peer.get("TailscaleIPs") or [])
    return home


def at_home_via_tailscale(ip):
    """Tailscale 経由のアクセスでも、その端末が家のWi-Fiにいれば True（10秒キャッシュ）"""
    now = time.monotonic()
    if now - _TS_CACHE["t"] > 10:
        _TS_CACHE["t"] = now
        try:
            import subprocess
            out = subprocess.run(["tailscale", "status", "--json"], capture_output=True, text=True, timeout=3)
            _TS_CACHE["peers"] = tailscale_peers_at_home(json.loads(out.stdout or "{}"), LAN_IP) if out.returncode == 0 else set()
        except Exception:
            _TS_CACHE["peers"] = set()
    return ip in _TS_CACHE["peers"]


def lan_ip():
    """ラズパイの家のLANでのIPアドレス（見つからなければ空）"""
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sk:
            sk.connect(("192.168.0.1", 9))   # 実際には送信しない。外向きのインターフェースを知るため
            ip = sk.getsockname()[0]
        return ip if is_home_network(ip) and not ipaddress.ip_address(ip).is_loopback else ""
    except OSError:
        return ""


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

    def _client_ip(self):
        ip = self.client_address[0]
        # tailscale serve などの中継を通ったときは、元の相手のアドレスで判断する
        fwd = self.headers.get("X-Forwarded-For")
        if ipaddress.ip_address(ip.split("%")[0]).is_loopback and (fwd or self.headers.get("Tailscale-User-Login")):
            return (fwd or "100.64.0.1").split(",")[0].strip()
        return ip

    def _at_home(self):
        ip = self._client_ip()
        return is_home_network(ip) or at_home_via_tailscale(ip)

    def _camera_info(self):
        return {"here": self._camera_ok(), "lan_url": (f"http://{LAN_IP}:{cfg.s['web']['port']}" if LAN_IP else "")}

    def _camera_ok(self):
        return not cfg.s["web"]["camera_lan_only"] or self._at_home()

    def do_GET(self):
        if not self._auth_ok():
            return
        path = self.path.split("?", 1)[0]
        if path in ("/", "/index.html") and not self._camera_ok():
            self.send_response(302)                      # 外出先からはカメラの画面ではなくダッシュボードへ
            self.send_header("Location", "/dash")
            self.end_headers()
        elif path in ("/stream.mjpg", "/snapshot.jpg") and not self._camera_ok():
            self._send(403, "text/plain; charset=utf-8", "カメラは家のWi-Fiにつないだときだけ見られます".encode())
        elif path == "/api/lan":
            # ダッシュボードが「いま家のWi-Fiにいるか」を確かめるための窓口（家の中からだけ答える）
            if self._at_home():
                self.send_response(200)
                self.send_header("Access-Control-Allow-Origin", "*")
                self.send_header("Content-Type", "application/json")
                self.send_header("Cache-Control", "no-store")
                self.end_headers()
                self.wfile.write(b'{"ok": true}')
            else:
                self._send(403, "application/json", b'{"ok": false}')
        elif path in ("/", "/index.html"):
            self._file("index.html")
        elif path == "/milk":
            self._file("milk.html")
        elif path in ("/dash", "/app"):
            self._file("dash.html")
        elif path == "/cam":
            self._file("cam.html")
        elif path == "/sw.js":
            with open(os.path.join(WEB_DIR, "sw.js"), "rb") as f:
                body = f.read()
            self.send_response(200)
            self.send_header("Content-Type", "application/javascript; charset=utf-8")
            self.send_header("Service-Worker-Allowed", "/")
            self.send_header("Cache-Control", "no-cache")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        elif path in STATIC:
            self._file(path.lstrip("/"), STATIC[path])
        elif path == "/api/dash":
            self._json(200, self._dash())
        elif path == "/status":
            with shared.lock:
                body = json.dumps(shared.status).encode()
            self._send(200, "application/json", body)
        elif path == "/api/state":
            self._json(200, self._state())
        elif path == "/api/milk":
            self._json(200, self._milk_state())
        elif path == "/snapshot.jpg":
            jpeg = shared.jpeg
            if jpeg:
                self._send(200, "image/jpeg", jpeg)
            else:
                self._send(503, "text/plain", b"no frame yet")
        elif path == "/stream.mjpg":
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
        elif self.path == "/api/ack":
            with shared.lock:
                shared.ack_request = True
            self._json(200, {"ok": True})
        elif self.path == "/api/settings":
            try:
                applied = cfg.set_ui(self._body())
                self._json(200, {"ok": True, "applied": applied, **self._state()})
            except (ValueError, TypeError) as e:
                self._json(400, {"ok": False, "error": str(e)})
        elif self.path == "/api/brightness":
            try:
                pct = int(self._body().get("level", 100))
            except (ValueError, TypeError):
                self._json(400, {"ok": False, "error": "level は 0〜100"})
                return
            ok = BACKLIGHT.set(pct) if BACKLIGHT else False
            self._json(200, {"ok": ok, "level": pct, "hardware": bool(BACKLIGHT and BACKLIGHT.available)})
        elif self.path == "/api/test-notify":
            notify("🔔 見守りカメラのテスト通知です（画面の「設定」から送りました）。")
            self._json(200, {"ok": True})
        elif self.path.startswith("/api/push/"):
            try:
                body = self._body()
                if PUSH is None:
                    raise ValueError("アプリ通知の準備ができていません")
                ep = str(body.get("endpoint") or "")
                if self.path == "/api/push/status":
                    out = PUSH.public_info(ep or None)
                elif self.path == "/api/push/subscribe":
                    out = PUSH.subscribe(body.get("subscription"), body.get("kinds"), body.get("name", ""))
                    PUSH.send_async("test", "育児の記録", "このスマホで通知を受け取れるようになりました 🎉", url="/dash", only=body["subscription"]["endpoint"])
                elif self.path == "/api/push/update":
                    out = PUSH.update(ep, body.get("kinds"))
                elif self.path == "/api/push/unsubscribe":
                    PUSH.unsubscribe(ep); out = PUSH.public_info()
                elif self.path == "/api/push/test":
                    PUSH.last_error = ""
                    n = PUSH.send("test", "テスト通知", "アプリの通知はこのように届きます", url="/dash", only=ep)
                    out = {**PUSH.public_info(ep), "sent": n, "error": PUSH.last_error}
                else:
                    self._send(404, "text/plain", b"not found")
                    return
                self._json(200, {"ok": True, **out, "routes": {k: cfg.ui.get(k, True) for k in ("discord_alert", "discord_env")}})
            except (ValueError, KeyError, TypeError) as e:
                self._json(400, {"ok": False, "error": str(e).strip("'\"")})
        elif self.path in ("/api/diaper", "/api/diaper/undo", "/api/weight", "/api/weight/delete"):
            try:
                body = self._body()
                src = str(body.get("source") or "スマホ・PC")[:40]
                if self.path == "/api/diaper":
                    rec = LOGBOOK.add_diaper(str(body.get("kind", "")), source=src, minutes_ago=body.get("minutes_ago", 0))
                elif self.path == "/api/diaper/undo":
                    rec = LOGBOOK.undo_diaper(str(body.get("id", "")))
                elif self.path == "/api/weight":
                    rec = LOGBOOK.set_weight(body.get("grams", 0), body.get("day") or None, body.get("note", ""))
                else:
                    rec = LOGBOOK.delete_weight(str(body.get("day", "")))
                self._json(200, {"ok": True, "record": rec, **self._dash()})
            except (ValueError, KeyError, TypeError) as e:
                self._json(400, {"ok": False, "error": str(e).strip("'\"")})
        elif self.path.startswith("/api/milk") or self.path.startswith("/api/feed"):
            try:
                body = self._body()
                src = str(body.get("source") or "ベッド横の画面")[:40]
                if self.path == "/api/feed/start":
                    rec = MILK.start(source=src)
                elif self.path == "/api/feed/finish":
                    rec = MILK.finish(str(body.get("id", "")), body.get("milk", 0), body.get("breast", 0),
                                      start=body.get("start") or None)
                elif self.path == "/api/feed/add":
                    rec = MILK.add(body.get("milk", 0), body.get("breast", 0), body.get("minutes_ago", 0), source=src)
                elif self.path == "/api/milk":          # v2.5 までの画面との互換（ミルクだけ）
                    rec = MILK.add(body.get("amount", 0), source=src)
                elif self.path in ("/api/milk/undo", "/api/feed/undo"):
                    rec = {"undo": MILK.undo(str(body.get("id", "")))}
                else:
                    self._send(404, "text/plain", b"not found")
                    return
                self._json(200, {"ok": True, "record": rec, **self._milk_state()})
            except (ValueError, KeyError, TypeError) as e:
                self._json(400, {"ok": False, "error": str(e).strip("'\"")})
        else:
            self._send(404, "text/plain", b"not found")

    def _body(self):
        n = int(self.headers.get("Content-Length") or 0)
        d = json.loads(self.rfile.read(n) or b"{}")
        if not isinstance(d, dict):
            raise ValueError("JSON object expected")
        return d

    def _file(self, name, ctype="text/html; charset=utf-8"):
        with open(os.path.join(WEB_DIR, name), "rb") as f:
            self._send(200, ctype, f.read())

    def _dash(self):
        """スマホ用ダッシュボード：記録・温湿度・天気・見守りの状態をまとめて返す"""
        from datetime import date as _date
        with shared.lock:
            st = dict(shared.status)
        from logbook import milestones
        b = cfg.s["baby"]
        days, ms = None, {"today": None, "next": None}
        try:
            if b["birth_date"]:
                birth = _date.fromisoformat(b["birth_date"])
                days = (_date.today() - birth).days
                ms = milestones(birth, _date.today())
        except ValueError:
            pass
        env = LOGBOOK.env_summary() if LOGBOOK else {"latest": None, "series": [], "trend": None}
        e = cfg.s["env"]
        env["range"] = {k: e[k] for k in ("temp_min", "temp_max", "hum_min", "hum_max")}
        env["alert"] = e["alert"]
        env["configured"] = ENV is not None
        env["error"] = ENV.last_error if ENV else ""
        return {
            "baby": {"name": b["name"], "birth_date": b["birth_date"], "days": days, "milestone": ms},
            "milk": MILK.summary() if MILK else None,
            "milk_config": cfg.s["milk"],
            "diaper": LOGBOOK.diaper_summary() if LOGBOOK else None,
            "weights": LOGBOOK.weights() if LOGBOOK else [],
            "env": env,
            "weather": WEATHER.summary() if WEATHER else None,
            "camera": self._camera_info(),
            "night": {"start": int(cfg.s["display"]["night_start"].split(":")[0]), "end": int(cfg.s["display"]["night_end"].split(":")[0])},
            "timeline": {"feeds": MILK.since(24) if MILK else [], "diapers": LOGBOOK.diapers_since(24) if LOGBOOK else []},
            "monitor": {"phase": st.get("phase"), "face_visible": st.get("face_visible"), "armed_sec": st.get("armed_sec")},
            "pending": (LOGBOOK.pending() if LOGBOOK and LOGBOOK.sender else 0),
            "local": self.client_address[0] in ("127.0.0.1", "::1"),
        }

    def _state(self):
        """タッチ画面用：見守りの状態・通知先・画面の設定・ミルクの前回をまとめて返す"""
        with shared.lock:
            st = dict(shared.status)
        d, g = cfg.secrets["discord"], cfg.secrets["gmail"]
        disp = cfg.s["display"]
        ms = MILK.summary() if MILK else {}
        return {
            "status": st,
            "notify": {"discord": bool(d["webhook_url"]), "mail": bool(g["user"] and g["app_password"] and g["to"])},
            "settings": {"absent_sec": cfg.alert["absent_sec"], "head": cfg.head, **disp},
            "backlight": {"hardware": bool(BACKLIGHT and BACKLIGHT.available),
                          "writable": bool(BACKLIGHT and BACKLIGHT.writable),
                          "level": BACKLIGHT.level if BACKLIGHT else None},
            "local": self.client_address[0] in ("127.0.0.1", "::1"),
            "camera": self._camera_info(),
            "milk_last": ms.get("last"),
            "milk_open": ms.get("open"),
            "milk_today": {k: ms.get("today_" + k, 0) for k in ("total", "milk", "breast", "count")},
        }

    def _json(self, code, obj):
        self._send(code, "application/json; charset=utf-8", json.dumps(obj, ensure_ascii=False).encode())

    def _milk_state(self):
        st = MILK.summary()
        st["config"] = cfg.s["milk"]
        return st


STATIC = {"/manifest.json": "application/manifest+json", "/icon-180.png": "image/png",
          "/icon-512.png": "image/png", "/icon-32.png": "image/png"}


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
    # 状態の文字はタッチ画面（右側）に出すので、映像には時刻だけ入れる（status は今は未使用）


# ================================================================ メイン
ROTATE_CODES = {90: cv2.ROTATE_90_CLOCKWISE, 180: cv2.ROTATE_180, 270: cv2.ROTATE_90_COUNTERCLOCKWISE}


CAMERA_RETRY_SEC = 15


def no_camera_jpeg(w, h):
    """カメラが見つからないときに配信する案内画像"""
    img = np.zeros((h, w, 3), np.uint8)
    put_label(img, "CAMERA NOT FOUND", (24, h // 2 - 10), YELLOW, 1.2)
    put_label(img, f"retrying every {CAMERA_RETRY_SEC}s", (24, h // 2 + 40), WHITE, 0.8)
    ok, buf = cv2.imencode(".jpg", img)
    return buf.tobytes() if ok else None


def open_camera(Picamera2, cam_cfg):
    """カメラが見つかるまで待つ（見つからない間もWeb画面とミルク記録は使える）"""
    notified = False
    placeholder = no_camera_jpeg(cam_cfg["width"], cam_cfg["height"])
    while True:
        try:
            picam2 = Picamera2()
            picam2.configure(picam2.create_video_configuration(
                main={"size": (cam_cfg["width"], cam_cfg["height"]), "format": "RGB888"}))  # メモリ上は B,G,R 順
            picam2.start()
            time.sleep(1.0)
            if notified:
                log.info("カメラが見つかりました")
            return picam2
        except Exception as e:
            if not notified:
                log.warning("カメラが見つかりません（%s）。%d秒ごとに探します", e, CAMERA_RETRY_SEC)
                notify("📷 カメラが見つかりません。接続を確認してください（見つかり次第、自動で見守りを始めます）。")
                notified = True
            with shared.lock:
                shared.status = {"phase": "NO_CAMERA"}
            if placeholder:
                shared.set_jpeg(placeholder)
            time.sleep(CAMERA_RETRY_SEC)


def main():
    from picamera2 import Picamera2  # ラズパイ上でのみ import

    cam_cfg, web_cfg = cfg.s["camera"], cfg.s["web"]
    detector = FaceDetector(MODEL_PATH, cfg.detection["score_threshold"])

    # 先にWeb画面とミルク記録を始める（カメラがなくても使えるように）
    global BACKLIGHT
    BACKLIGHT = Backlight()
    log.info("液晶の明るさ調整: %s", "使えます" if BACKLIGHT.writable else
             ("権限がありません（scripts/install-service.sh を再実行）" if BACKLIGHT.available else "対応する液晶なし"))
    global LAN_IP
    LAN_IP = lan_ip()
    log.info("家のネットワークでのアドレス: %s（カメラは%s）", LAN_IP or "不明",
             "家のWi-Fiからだけ" if cfg.s["web"]["camera_lan_only"] else "どこからでも")
    global PUSH
    from push import Push
    PUSH = Push(DATA_DIR)
    log.info("アプリ通知: %s（登録 %d 台）", "使えます" if PUSH.available else "使えません", len(PUSH.subs))
    init_milk()
    init_home()
    httpd = StreamingServer(("", web_cfg["port"]), Handler)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    log.info("配信開始: http://<ラズパイのIP>:%d/", web_cfg["port"])

    picam2 = open_camera(Picamera2, cam_cfg)

    mon = Monitor(cfg.alert["armed_on_start"], time.monotonic())
    notify(f"見守りカメラを起動しました（{PHASE_JA[mon.phase]}）")
    box = None  # 表示用の顔の枠（全体画像の座標）
    frames, t_fps, fps = 0, time.monotonic(), 0.0

    while True:
        if cfg.maybe_reload():
            detector.set_threshold(cfg.detection["score_threshold"])
        a, d = cfg.alert, cfg.detection

        frame = picam2.capture_array()
        rot = (cam_cfg["rotate"] + HEAD_ROTATE[cfg.head]) % 360   # 頭の向きに合わせて、頭が上に映るよう回す
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
            ack, shared.ack_request = shared.ack_request, False
        if req is not None:
            mon.set_armed(req, now)
            log.info("見守りを%sしました（ブラウザ操作）", "開始" if req else "一時停止")
        if ack and mon.ack():
            log.info("「様子を見ました」が押されました。顔が見えるまで再通知を止めます")

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
                notify(f"⚠️ 赤ちゃんの顔が {sec:.0f} 秒間見えていません。すぐに様子を確認してください。", jpeg, kind="alert")
            elif ev == "recovered":
                log.info("顔が再び見えるようになりました")
                notify("✅ 赤ちゃんの顔が再び見えるようになりました。", jpeg, kind="alert")
            elif ev == "waiting_long":
                log.warning("見守り開始から %.0f 秒、顔を確認できていません", a["waiting_notify_sec"])
                notify(f"ℹ️ 見守りを始めてから {a['waiting_notify_sec'] / 60:.0f} 分、まだ一度も顔を確認できていません。"
                       "カメラの向きや赤ちゃんの様子を確認してください。", jpeg, kind="alert")

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
                "acked": mon.acked,
                "armed_sec": None if mon.armed_since is None else round(now - mon.armed_since),
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
