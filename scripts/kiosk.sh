#!/usr/bin/env bash
# =====================================================================
#  見守り画面を全画面（キオスク）で開く。デスクトップにログインしたとき自動で実行されます
#  （登録は scripts/install-kiosk.sh）。手で試すときも： bash scripts/kiosk.sh
#  全画面を抜けるとき：キーボードで Alt+F4、または SSH で  pkill -f chromium
# =====================================================================
DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PORT="$("$DIR/venv/bin/python" -c "import tomllib;print(tomllib.load(open('$DIR/config/settings.toml','rb')).get('web',{}).get('port',8000))" 2>/dev/null || echo 8000)"
URL="http://localhost:${PORT}/"

BROWSER="$(command -v chromium || command -v chromium-browser || true)"
if [ -z "$BROWSER" ]; then
  echo "Chromium が見つかりません（sudo apt install -y chromium）"
  exit 1
fi

# 見守りプログラムが起動するまで待つ（最大2分）
for _ in $(seq 60); do
  curl -sf -o /dev/null "${URL}status" && break
  sleep 2
done

exec "$BROWSER" --kiosk "$URL" \
  --noerrdialogs --disable-infobars --no-first-run \
  --disable-session-crashed-bubble --disable-features=Translate \
  --password-store=basic --check-for-update-interval=31536000
