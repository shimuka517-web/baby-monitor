# 赤ちゃん見守りカメラ（Raspberry Pi 4）

Raspberry Pi 4 とラズパイ用カメラで作る、自作の見守りカメラです。

- 赤ちゃんの顔を常に検出し、**顔が見えなくなったら**（寝返り・うつ伏せなど）Discord と Gmail に写真付きで通知します
- 別の部屋のPC・タブレット・テレビのブラウザに、ライブ映像と見守りの状態を表示します
- 顔を検出しているときは**緑の枠**、見えなくなると**赤の枠**で表示します
- **ミルクの記録**：あげた量と時刻をボタンで記録し、Googleスプレッドシートで家族と共有できます

> ⚠️ **これは自作の補助ツールで、医療機器ではありません。** 見逃し（通知が来ない）も誤報も起こります。「顔が見えている＝呼吸に問題がない」という意味でもありません。安全な寝かせ方（仰向け寝・固めの寝具・顔まわりに物を置かない）を前提に、大人が様子を見る手段の一つとして使ってください。カメラとケーブルは赤ちゃんの手が絶対に届かない場所に固定してください。

---

## はじめて読む人へ：どこから読めばいい？

| やりたいこと | 読むもの |
|---|---|
| 何ができるのか、どう動くのかを知りたい | このページ → [docs/architecture.md](docs/architecture.md)（仕組みと各ファイルの役割） |
| ゼロから組み立てて設置したい | [docs/setup.md](docs/setup.md)（セットアップ手順） |
| Discord / Gmail の通知を設定したい | [docs/notifications.md](docs/notifications.md) |
| 毎日の使い方・アラートが来たときの対応 | [docs/usage.md](docs/usage.md) |
| ミルクの記録・スプレッドシート連携 | [docs/milk.md](docs/milk.md) |
| 通知が多すぎる／少なすぎるので調整したい | [docs/tuning.md](docs/tuning.md) |
| エラーが出た・うまく動かない | [docs/troubleshooting.md](docs/troubleshooting.md) |
| カメラマウントを3Dプリントしたい | [hardware/README.md](hardware/README.md) |

---

## 見守りの流れ（概要）

1. 起動すると **「顔の確認待ち」**（黄）。顔が10秒続けて見えるまでは、顔が見えなくても通知しません（寝かせる前の誤報を防ぐため）。
2. 顔を確認できたら **「見守り中」**（緑）。
3. 顔が15秒続けて見えなくなったら **「アラート」**（赤）。Discord / Gmail に写真付きで通知し、見えないままなら2分ごとに再通知します。
4. 顔が3秒続けて見えたら **「復帰」** を通知して「見守り中」に戻ります。
5. 抱っこなどで寝床にいないときは、ブラウザの **「一時停止」** ボタンで止めます。

秒数はすべて `config/settings.toml` で変えられます。詳しい理由や設計は [docs/architecture.md](docs/architecture.md) にあります。

---

## フォルダ構成と役割

```
baby-monitor/
├── README.md                     このページ（入口）
├── baby_monitor.py               プログラム本体（カメラ・顔検出・判定・通知・Web配信）
├── milk.py                       ミルクの記録（ラズパイ内に保存・スプレッドシートへ送信）
├── web/milk.html                 ミルクの記録画面
├── gas/milk_sheet.gs             スプレッドシート側のスクリプト（Google Apps Script）
├── requirements.txt              必要な Python ライブラリ
├── config/                       ★ 利用者が触るのはここだけ
│   ├── settings.toml             調整用の設定（秒数・検出の感度など）。保存すると自動反映
│   ├── secrets.example.toml      通知先・パスワード・スプレッドシートURLのひな形
│   └── secrets.toml              ↑をコピーして作る本物（GitHubには上がらない）
├── scripts/
│   ├── install.sh                セットアップを自動で行う
│   └── install-service.sh        電源を入れたら自動で起動するよう登録する
├── systemd/
│   └── baby-monitor.service.template   自動起動の設定のひな形（install-service.sh が使う）
├── models/                       顔検出モデルの置き場（install.sh がダウンロード）
├── data/                         ミルクの記録など（自動で作られる。GitHubには上がらない）
├── tests/
│   ├── test_monitor.py           見守り判定のテスト（カメラ不要）
│   └── test_milk.py              ミルク記録のテスト
├── hardware/                     カメラマウントの3Dプリント用データ
└── docs/                         説明書
```

---

## クイックスタート（慣れている人向け）

Raspberry Pi OS (64-bit, Bookworm 以降) にカメラを接続した状態で：

```bash
git clone https://github.com/<あなたのユーザー名>/baby-monitor.git ~/baby-monitor
cd ~/baby-monitor
bash scripts/install.sh                         # パッケージ・モデル・設定ファイルの準備
nano config/secrets.toml                        # 通知先を記入（docs/notifications.md）
venv/bin/python baby_monitor.py --test-notify   # 通知テスト
venv/bin/python baby_monitor.py                 # 試運転 → ブラウザで http://<ラズパイのIP>:8000/
bash scripts/install-service.sh                 # 自動起動の登録
```

初めての人や、途中でつまずいた人は [docs/setup.md](docs/setup.md) を一歩ずつ進めてください。

---

## 必要なもの

| 物 | 備考 |
|---|---|
| Raspberry Pi 4（4GB で動作確認） | ヒートシンク／ファン付きケース推奨（24時間動かすため） |
| 純正 5V/3A 電源、microSD 32GB 以上 | |
| ラズパイ用カメラ（Camera Module 3 など） | **夜も使うなら NoIR タイプ＋赤外線ライト（850nm）が必須** |
| 表示用の端末 | ブラウザが使えれば何でも可（PC・タブレット・スマホ・Fire TV など） |
| カメラマウント（任意） | [hardware/](hardware/README.md) のデータを3Dプリント |

---

## 共有するときの注意

- `config/secrets.toml`（Discord の URL・Gmail のアプリパスワード）は `.gitignore` 済みで、GitHub には上がりません。**他の人に渡すときも、このファイルは渡さず、各自で作ってもらってください。**
- このリポジトリはプライベートです。他の人に見せるときは、GitHub の Settings → Collaborators から招待してください。
- 映像は家の中（同じ Wi-Fi）だけで見る設計です。ルーターのポート開放などで外部に公開しないでください。

## 変更履歴

[CHANGELOG.md](CHANGELOG.md) を見てください。
