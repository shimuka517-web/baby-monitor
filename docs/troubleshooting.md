# トラブルシューティング

まずログを見ると原因が分かることが多いです。

```bash
journalctl -u baby-monitor -n 100        # 自動起動している場合：直近100行
journalctl -u baby-monitor -f            # リアルタイムで見る（Ctrl+C で終了）
systemctl status baby-monitor            # 動いているか
```

---

## カメラが見つからない（`no cameras available`）

`Failed to create egl preview` / `Preview window unavailable` は SSH で画面がないときに出るだけなので無視して OK です。問題は `no cameras available` の方です。

1. **電源を切ってからケーブルを見直す**（一番多い原因）
   ```bash
   sudo poweroff
   ```
   - 基板の「**CAMERA**」コネクタ（HDMI 端子とイヤホンジャックの間）に挿しているか。「DISPLAY」ではない
   - 銀色の端子の面が **HDMI 端子側**、青いテープの面がイヤホンジャック／LAN 側
   - 奥まで真っすぐ入って、黒いロックが押し込まれているか
   - カメラ基板側の差し込み口と、カメラモジュール上の細いフィルムコネクタが浮いていないか
2. **設定ファイルを確認**
   ```bash
   grep -n -E "camera_auto_detect|dtoverlay" /boot/firmware/config.txt
   ```
   `camera_auto_detect=1` がなければ `sudo nano /boot/firmware/config.txt` で末尾の `[all]` の下に追記して再起動。
3. **認識ログを見る**
   ```bash
   dmesg | grep -i -E "imx|ov5647|unicam|csi"
   ```
4. Arducam など他社製の一部（IMX519、64MP など）は自動認識されず、メーカーの手順で `dtoverlay` の設定が必要です。
5. SSH で映りを確認するには写真を撮ります：`rpicam-still -n -o test.jpg`（PC に `scp` でコピーして開く）

---

## `cd: ~/baby-monitor: そのようなファイルやディレクトリはありません`

リポジトリがまだラズパイに来ていません。[setup.md](setup.md) の手順4（git clone または zip）を行ってください。zip で持ってきた場合は、展開後のフォルダ名が `baby-monitor-main` になっていることがあるので `mv baby-monitor-main baby-monitor` で名前を変えてください。

## `顔検出モデルが見つかりません`

```bash
ls -l ~/baby-monitor/models/
```
約230KB の `face_detection_yunet_2023mar.onnx` がなければ `bash scripts/install.sh` を再実行。数百バイトしかない場合はダウンロード失敗なので、消してから再実行してください。

## `numpy` 関係のエラー

```bash
cd ~/baby-monitor && venv/bin/pip install -r requirements.txt
```
OpenCV のバージョンを `requirements.txt` で固定しています。別のバージョンを手で入れると picamera2 と合わなくなることがあります。

## `Address already in use`

同じプログラムがすでに動いています（自動起動版と手動起動版がぶつかっている、など）。

```bash
sudo systemctl stop baby-monitor          # 自動起動版を止めてから手動で試す場合
pkill -f baby_monitor.py                  # 手動で起動したものを止める
```

## `tomllib` がない（`ModuleNotFoundError: No module named 'tomllib'`）

Python 3.11 未満です。Raspberry Pi OS (Bookworm 以降) を使ってください。

## 通知が届かない

[notifications.md](notifications.md) の「うまく届かないとき」を見てください。まず `venv/bin/python baby_monitor.py --test-notify` で、設定だけを確認できます。

## ブラウザで開けない

- URL は `http://`（`https://` ではない）、末尾にポート番号 `:8000` が必要
- IP アドレスが変わっていないか（ラズパイで `hostname -I`）。ルーターで IP を固定するのがおすすめ
- PC・スマホがラズパイと同じ Wi-Fi につながっているか（ゲスト Wi-Fi だとつながらないことがあります）
- プログラムが動いているか（`systemctl status baby-monitor`）

## 映像がカクつく・遅れる

- `config/settings.toml` の `[camera]` の `width` / `height` と `[detection]` の `detect_width` を下げる（例：960 / 540 / 480）→ 再起動
- ラズパイが熱くなっていないか（`vcgencmd measure_temp`。80℃ 近いならファン付きケースに）
- Wi-Fi の電波が弱い場所なら、ルーターに近づけるか有線 LAN に

## 誤報や見逃しが多い

[tuning.md](tuning.md) の早見表を見てください。夜だけ多い場合は、暗さ（赤外線ライトの向き・明るさ）が原因のことが多いです。

## git pull でエラー（`Your local changes ... would be overwritten`）

`config/settings.toml` を自分で変えていると起こります。

```bash
cd ~/baby-monitor
cp config/settings.toml ~/settings.backup.toml     # 自分の設定を退避
git checkout config/settings.toml                  # いったん元に戻す
git pull
# 新しい settings.toml を開き、~/settings.backup.toml を見ながら自分の値を入れ直す
```

## 自動起動しない

```bash
systemctl status baby-monitor
```
`inactive` なら `bash scripts/install-service.sh` を再実行。フォルダを移動した場合も、移動先で再実行してください（場所が設定に埋め込まれるため）。
