#!/usr/bin/env bash
# =====================================================================
#  見守りカメラ セットアップスクリプト（Raspberry Pi 上で実行）
#    cd ~/baby-monitor && bash scripts/install.sh
#  何度実行しても大丈夫です（済んでいる作業は飛ばします）。
#  自動起動の登録は、試運転のあとに scripts/install-service.sh で行います。
# =====================================================================
set -euo pipefail

DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$DIR"
MODEL="models/face_detection_yunet_2023mar.onnx"
MODEL_URL="https://media.githubusercontent.com/media/opencv/opencv_zoo/main/models/face_detection_yunet/face_detection_yunet_2023mar.onnx"

step() { echo; echo "=== $* ==="; }

step "1/5 Python のバージョン確認"
if ! python3 -c 'import sys; sys.exit(0 if sys.version_info >= (3, 11) else 1)'; then
  echo "Python 3.11 以上が必要です。Raspberry Pi OS (Bookworm 以降, 64-bit) を使ってください。"
  exit 1
fi
python3 --version

step "2/5 必要なパッケージのインストール（sudo のパスワードを聞かれます）"
sudo apt update
sudo apt install -y python3-picamera2 python3-venv

step "3/5 Python 仮想環境（venv）の作成"
if [ ! -x venv/bin/python ]; then
  python3 -m venv --system-site-packages venv
fi
venv/bin/pip install -r requirements.txt

step "4/5 顔検出モデルのダウンロード"
if [ -f "$MODEL" ] && [ "$(stat -c %s "$MODEL")" -gt 100000 ]; then
  echo "ダウンロード済み: $MODEL"
else
  wget -q -O "$MODEL" "$MODEL_URL"
  size=$(stat -c %s "$MODEL")
  if [ "$size" -lt 100000 ]; then
    echo "モデルのダウンロードに失敗しました（${size} バイト）。ネット接続を確認して再実行してください。"
    exit 1
  fi
  echo "ダウンロード完了: $MODEL (${size} バイト)"
fi

step "5/5 通知設定ファイルの準備"
if [ ! -f config/secrets.toml ]; then
  cp config/secrets.example.toml config/secrets.toml
  echo "config/secrets.toml を作成しました。"
else
  echo "config/secrets.toml は作成済みです（上書きしません）。"
fi
chmod 600 config/secrets.toml

step "カメラの確認"
if rpicam-hello --list-cameras 2>&1 | grep -q "Available cameras"; then
  rpicam-hello --list-cameras 2>&1 | grep -A2 "Available cameras" || true
  echo "カメラを認識しています。"
else
  echo "⚠ カメラが見つかりません。docs/troubleshooting.md の「カメラが見つからない」を見てください。"
fi

cat <<EOF

セットアップ完了です。次の手順：
  1) 通知先を設定        nano config/secrets.toml       （docs/notifications.md）
  2) 通知のテスト        venv/bin/python baby_monitor.py --test-notify
  3) 試運転              venv/bin/python baby_monitor.py
                         → ブラウザで http://$(hostname -I | awk '{print $1}'):8000/
  4) 自動起動の登録      bash scripts/install-service.sh
EOF
