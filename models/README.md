# models/

顔検出モデル `face_detection_yunet_2023mar.onnx`（OpenCV Zoo の YuNet、約230KB）を置く場所です。
ファイル自体は GitHub に入れていません（`.gitignore` 済み）。`scripts/install.sh` が自動でダウンロードします。

手動で入れる場合：
```bash
wget -O models/face_detection_yunet_2023mar.onnx \
  https://media.githubusercontent.com/media/opencv/opencv_zoo/main/models/face_detection_yunet/face_detection_yunet_2023mar.onnx
ls -l models/   # 約230KBあればOK（数百バイトならダウンロード失敗）
```
