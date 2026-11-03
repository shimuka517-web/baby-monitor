# セットアップ手順（ゼロから）

初めての人向けに、microSD の準備から自動起動まで順番に説明します。所要時間は1～2時間ほどです。
コマンドは、PC から SSH でラズパイに接続して実行する前提です。

---

## 0. 用意するもの

- Raspberry Pi 4（4GB）、純正 5V/3A 電源、microSD（32GB 以上）、ヒートシンク／ファン付きケース
- ラズパイ用カメラ（Camera Module 3 など）とフラットケーブル。夜も使うなら **NoIR タイプ＋赤外線ライト（850nm）**
- 作業用の PC（Windows / Mac）と、家の Wi-Fi
- 通知を受け取るスマホ（Discord アプリ、または Gmail）

---

## 1. microSD に OS を書き込む

1. PC に「Raspberry Pi Imager」を入れて起動する
2. デバイス：Raspberry Pi 4 ／ OS：**Raspberry Pi OS (64-bit)**（Bookworm 以降）
3. 「設定を編集する」で次を入れておく
   - ホスト名（例：`babycam`）
   - **ユーザー名とパスワード**（好きな名前で OK。この説明書のコマンドはユーザー名に関係なく動きます）
   - Wi-Fi の名前とパスワード、国：JP
   - 「サービス」タブで **SSH を有効化**
4. 書き込んだ microSD をラズパイに挿す

---

## 2. カメラをつなぐ（必ず電源を切った状態で）

- 基板に「**CAMERA**」と印字されたコネクタ（HDMI 端子とイヤホンジャックの間）に挿します。反対側の「DISPLAY」は画面用なので間違えないでください。
- ケーブルの向き：**銀色の端子の面を HDMI 端子側**、青いテープの面をイヤホンジャック／LAN 側。
- 奥まで真っすぐ挿して、黒いロックを押し込んで固定します。カメラ基板側の差し込み口も同様に確認します。

---

## 3. ラズパイに接続してカメラを確認

電源を入れて1～2分待ち、PC のターミナル（Windows は PowerShell）から接続します。

```bash
ssh <ユーザー名>@<ホスト名>.local      # 例：ssh shim@babycam.local
```

カメラが認識されているか確認します。

```bash
rpicam-hello --list-cameras
```

`imx708` などのカメラ名が表示されれば OK です。`no cameras available` と出たら [troubleshooting.md](troubleshooting.md) の「カメラが見つからない」を見てください。

> `Failed to create egl preview` などのプレビュー関係のメッセージは、SSH では画面がないため出るもので、問題ありません。

---

## 4. このリポジトリをラズパイに持ってくる

プライベートリポジトリなので、GitHub のアカウントで認証が必要です。どちらかの方法で。

**方法A：git clone（おすすめ。あとで更新するのが楽）**

```bash
sudo apt install -y git gh
gh auth login            # 画面の指示どおり「GitHub.com」→「HTTPS」→「ブラウザでログイン」
git clone https://github.com/<オーナー名>/baby-monitor.git ~/baby-monitor
```

他の人のリポジトリを使う場合は、事前にオーナーから Collaborator として招待してもらってください。

**方法B：zip で持ってくる**

GitHub のページで「Code」→「Download ZIP」で PC に保存し、PC から送ります。

```bash
# PC 側で実行（ダウンロードフォルダで）
scp baby-monitor-main.zip <ユーザー名>@<ホスト名>.local:~/
# ラズパイ側で実行
cd ~ && unzip baby-monitor-main.zip && mv baby-monitor-main baby-monitor
```

---

## 5. 自動セットアップを実行

```bash
cd ~/baby-monitor
bash scripts/install.sh
```

次のことを自動で行います（何度実行しても大丈夫です）。

1. Python のバージョン確認
2. 必要なパッケージ（picamera2 など）のインストール
3. Python の仮想環境（`venv/`）の作成とライブラリのインストール
4. 顔検出モデルのダウンロード（`models/`）
5. 通知設定ファイル `config/secrets.toml` の作成
6. カメラの認識確認

---

## 6. 通知先を設定する

[notifications.md](notifications.md) を見ながら Discord（おすすめ）か Gmail を設定し、`config/secrets.toml` に記入します。

```bash
nano config/secrets.toml          # 保存は Ctrl+O → Enter、終了は Ctrl+X
venv/bin/python baby_monitor.py --test-notify
```

スマホに「テスト通知です」が届けば OK です。

---

## 7. 試運転

```bash
venv/bin/python baby_monitor.py
```

1. 起動の通知（「見守りカメラを起動しました（顔の確認待ち）」）が届く
2. 別の部屋のブラウザで `http://<ラズパイのIP>:8000/` を開くと映像が出る（IP は別の SSH で `hostname -I`）
3. カメラに顔を向けると緑の枠が出て、10秒後に表示が「見守り中・顔OK」（緑）に変わる
4. 顔を隠すと枠が赤になり、15秒後に写真付きのアラートが届く
5. 顔を戻すと、3秒後に「復帰」の通知が届く

確認できたら `Ctrl + C` で止めます。

---

## 8. 自動起動を登録する

```bash
bash scripts/install-service.sh
```

ユーザー名とフォルダの場所は自動で設定されます。最後に `sudo reboot` で再起動し、電源を入れただけで起動の通知が届くことを確認してください。

---

## 9. 仕上げ

- **ルーターでラズパイの IP アドレスを固定**しておくと、ブラウザの URL が変わりません（ルーターの「DHCP 固定割当」などの機能）。
- 寝床に設置し、[usage.md](usage.md) の「設置後に必ずやるテスト」を行ってください。
- 家族がブラウザで見る場合、パスワードをかけたいときは `config/secrets.toml` の `[web]` に ID とパスワードを入れて `sudo systemctl restart baby-monitor`。

---

## 更新するとき（方法A で入れた場合）

```bash
cd ~/baby-monitor
git pull
bash scripts/install.sh                 # ライブラリが増えた場合に備えて
sudo systemctl restart baby-monitor
```

`config/settings.toml` を自分で変えている場合、`git pull` でぶつかることがあります。その場合は [troubleshooting.md](troubleshooting.md) の「git pull でエラー」を見てください。
