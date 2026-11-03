#!/usr/bin/env bash
# =====================================================================
#  自動起動（systemd）の登録。試運転がうまくいってから実行してください。
#    cd ~/baby-monitor && bash scripts/install-service.sh
#  ユーザー名とフォルダの場所は自動で埋め込まれます。
#  解除するとき： sudo systemctl disable --now baby-monitor
# =====================================================================
set -euo pipefail

DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
USER_NAME="$(id -un)"
DEST=/etc/systemd/system/baby-monitor.service

if [ ! -x "$DIR/venv/bin/python" ]; then
  echo "先に scripts/install.sh を実行してください。"
  exit 1
fi

# 手動で起動したものが残っているとポートがぶつかるので止める
pkill -f "$DIR/baby_monitor.py" 2>/dev/null || true

sed -e "s#__USER__#${USER_NAME}#g" -e "s#__DIR__#${DIR}#g" \
  "$DIR/systemd/baby-monitor.service.template" | sudo tee "$DEST" > /dev/null
sudo systemctl daemon-reload
sudo systemctl enable --now baby-monitor

echo
echo "登録しました（ユーザー: ${USER_NAME} / 場所: ${DIR}）"
sleep 3
systemctl --no-pager --lines=5 status baby-monitor || true
cat <<EOF

よく使うコマンド：
  ログを見る       journalctl -u baby-monitor -f
  再起動           sudo systemctl restart baby-monitor
  止める           sudo systemctl stop baby-monitor
  自動起動を解除   sudo systemctl disable --now baby-monitor
EOF
