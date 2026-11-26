"""
部屋の温湿度（SwitchBot ハブ3 などのクラウドAPI）と、天気（Open-Meteo）

- SwitchBot API v1.1：アプリで発行するトークンとシークレットで署名して、温湿度を5分ごとに取得
- 温湿度が目安の範囲を外れたら通知（範囲に戻るまで再通知しない。範囲の端で何度も鳴らないよう少し余裕をもたせる）
- 天気は Open-Meteo（無料・キー不要）から30分ごとに、今日〜明後日の天気・最高/最低気温・降水確率

baby_monitor.py から使う。ネットに依存する部分は関数を差し替えてテストできる。
"""
import base64
import hashlib
import hmac
import logging
import threading
import time
import uuid

import requests

log = logging.getLogger("baby-monitor.env")

API = "https://api.switch-bot.com/v1.1"
SENSOR_TYPES = ("Hub 3", "Hub 2", "Meter", "MeterPlus", "Meter Plus", "WoIOSensor", "Outdoor Meter",
                "MeterPro", "Meter Pro", "MeterPro(CO2)", "Meter Pro CO2")


def switchbot_headers(token, secret, t=None, nonce=None):
    """SwitchBot API v1.1 の認証ヘッダー（token + t + nonce を secret で HMAC-SHA256 → Base64）"""
    t = str(t or int(time.time() * 1000))
    nonce = nonce or uuid.uuid4().hex
    sign = base64.b64encode(hmac.new(secret.encode(), (token + t + nonce).encode(), hashlib.sha256).digest()).decode()
    return {"Authorization": token, "sign": sign, "t": t, "nonce": nonce,
            "Content-Type": "application/json; charset=utf8"}


class SwitchBot:
    def __init__(self, token, secret, device_id="", timeout=15, get=requests.get):
        self.token, self.secret, self.device_id = token, secret, device_id
        self.timeout, self.get = timeout, get
        self.device_name = ""

    def _call(self, path):
        r = self.get(API + path, headers=switchbot_headers(self.token, self.secret), timeout=self.timeout)
        r.raise_for_status()
        j = r.json()
        if j.get("statusCode") != 100:
            raise RuntimeError(f"SwitchBot API エラー {j.get('statusCode')}: {j.get('message')}")
        return j.get("body") or {}

    def find_device(self):
        """device_id が未設定なら、温湿度を返す機器を探す（ハブ3を優先）"""
        devs = self._call("/devices").get("deviceList", [])
        cands = [d for d in devs if d.get("deviceType") in SENSOR_TYPES]
        cands.sort(key=lambda d: 0 if d.get("deviceType") == "Hub 3" else 1)
        for d in cands:
            st = self._call(f"/devices/{d['deviceId']}/status")
            if "temperature" in st and "humidity" in st:
                self.device_id, self.device_name = d["deviceId"], d.get("deviceName", "")
                log.info("温湿度の取得元: %s（%s）", self.device_name, d.get("deviceType"))
                return self.device_id
        raise RuntimeError("温湿度を返す SwitchBot の機器が見つかりません（ハブ3のクラウド連携がオンか確認）")

    def read(self):
        if not self.device_id:
            self.find_device()
        st = self._call(f"/devices/{self.device_id}/status")
        return float(st["temperature"]), float(st["humidity"])


class RangeAlert:
    """範囲外になったら1回だけ知らせる。margin だけ内側に戻ったら解除"""

    def __init__(self, name, unit, margin):
        self.name, self.unit, self.margin = name, unit, margin
        self.state = "ok"   # ok / low / high

    def check(self, value, lo, hi):
        """発生したイベント名（'low' 'high' 'ok'）か None を返す"""
        if self.state == "ok":
            if value < lo:
                self.state = "low"
                return "low"
            if value > hi:
                self.state = "high"
                return "high"
        elif (self.state == "low" and value >= lo + self.margin) or (self.state == "high" and value <= hi - self.margin):
            self.state = "ok"
            return "ok"
        return None


class EnvMonitor:
    """温湿度を定期取得して logbook に保存し、範囲外なら notify する"""

    def __init__(self, client, logbook, get_cfg, notify, poll_sec=300):
        self.client, self.logbook, self.get_cfg, self.notify = client, logbook, get_cfg, notify
        self.poll_sec = poll_sec
        self.temp_alert = RangeAlert("室温", "℃", 0.5)
        self.hum_alert = RangeAlert("湿度", "%", 3)
        self.last_error = ""
        self.ok = None

    def poll_once(self):
        try:
            temp, hum = self.client.read()
        except Exception as e:
            self.ok, self.last_error = False, str(e)
            log.warning("温湿度を取得できません: %s", e)
            return None
        self.ok, self.last_error = True, ""
        self.logbook.add_env(temp, hum)
        c = self.get_cfg()
        if c.get("alert", True):
            words = {"室温": ("寒く", "暑く"), "湿度": ("乾燥して", "じめじめして")}
            for al, v, lo, hi in ((self.temp_alert, temp, c["temp_min"], c["temp_max"]),
                                  (self.hum_alert, hum, c["hum_min"], c["hum_max"])):
                ev = al.check(v, lo, hi)
                low, high = words[al.name]
                if ev in ("low", "high"):
                    w = low if ev == "low" else high
                    self.notify(f"🌡️ お部屋が{w}います：{al.name} {v:g}{al.unit}（目安 {lo:g}〜{hi:g}{al.unit}）")
                elif ev == "ok":
                    self.notify(f"✅ お部屋の{al.name}が目安の範囲に戻りました：{v:g}{al.unit}")
        return temp, hum

    def start(self):
        def loop():
            while True:
                self.poll_once()
                time.sleep(self.poll_sec)
        threading.Thread(target=loop, daemon=True, name="env").start()


# ================================================================ 天気（Open-Meteo）
WMO = [  # (コードの上限, 表示, アイコンの種類)
    (0, "快晴", "sun"), (1, "晴れ", "sun"), (2, "晴れ時々くもり", "partly"), (3, "くもり", "cloud"),
    (48, "霧", "fog"), (57, "霧雨", "rain"), (63, "雨", "rain"), (67, "強い雨", "rain"),
    (77, "雪", "snow"), (82, "にわか雨", "rain"), (86, "にわか雪", "snow"), (99, "雷雨", "thunder"),
]


def wmo_text(code):
    code = int(code)
    for top, text, icon in WMO:
        if code <= top:
            return text, icon
    return "不明", "cloud"


def parse_weather(j):
    d = j["daily"]
    days = []
    for i, day in enumerate(d["time"][:3]):
        text, icon = wmo_text(d["weather_code"][i])
        days.append({"date": day, "text": text, "icon": icon,
                     "max": round(d["temperature_2m_max"][i]), "min": round(d["temperature_2m_min"][i]),
                     "pop": d.get("precipitation_probability_max", [None] * 3)[i]})
    cur = j.get("current") or {}
    now = None
    if cur:
        text, icon = wmo_text(cur.get("weather_code", 3))
        now = {"temp": round(cur["temperature_2m"], 1), "text": text, "icon": icon,
               "hum": cur.get("relative_humidity_2m")}
    return {"now": now, "days": days}


class Weather:
    def __init__(self, lat, lon, place="", get=requests.get, refresh_sec=1800):
        self.lat, self.lon, self.place, self.get, self.refresh_sec = lat, lon, place, get, refresh_sec
        self.data, self.updated, self.error = None, None, ""

    def fetch(self):
        params = {"latitude": self.lat, "longitude": self.lon, "timezone": "Asia/Tokyo", "forecast_days": 3,
                  "current": "temperature_2m,relative_humidity_2m,weather_code",
                  "daily": "weather_code,temperature_2m_max,temperature_2m_min,precipitation_probability_max"}
        try:
            r = self.get("https://api.open-meteo.com/v1/forecast", params=params, timeout=15)
            r.raise_for_status()
            self.data = parse_weather(r.json())
            self.updated, self.error = time.time(), ""
        except Exception as e:
            self.error = str(e)
            log.warning("天気を取得できません: %s", e)

    def summary(self):
        if not self.data:
            return None
        return {"place": self.place, **self.data, "updated_min": int((time.time() - self.updated) // 60)}

    def start(self):
        def loop():
            while True:
                self.fetch()
                time.sleep(self.refresh_sec)
        threading.Thread(target=loop, daemon=True, name="weather").start()
