#!/usr/bin/env bash
# =====================================================================
#  ラズパイの画面に、起動時に見守り画面を全画面で出す設定（sudo 不要）
#    cd ~/baby-monitor && bash scripts/install-kiosk.sh          # 登録
#    cd ~/baby-monitor && bash scripts/install-kiosk.sh --remove # 解除
#  前提：デスクトップ版の Raspberry Pi OS で、自動ログインが有効なこと
#        （sudo raspi-config → System Options → Boot / Auto Login → Desktop Autologin）
# =====================================================================
set -euo pipefail
DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
DEST="$HOME/.config/autostart/baby-monitor-kiosk.desktop"

if [ "${1:-}" = "--remove" ]; then
  rm -f "$DEST"
  echo "解除しました（次の起動から全画面表示しません）"
  exit 0
fi

mkdir -p "$(dirname "$DEST")"
cat > "$DEST" <<DESKTOP
[Desktop Entry]
Type=Application
Name=見守りカメラ（全画面）
Exec=bash "$DIR/scripts/kiosk.sh"
X-GNOME-Autostart-enabled=true
DESKTOP
chmod +x "$DIR/scripts/kiosk.sh"
echo "登録しました：$DEST"
echo "次の起動から、デスクトップが出たあと自動で見守り画面が全画面で開きます。"
echo "今すぐ試すとき（ラズパイの画面で）： bash $DIR/scripts/kiosk.sh"
echo "全画面を抜けるとき：Alt+F4、または SSH で  pkill -f chromium"
