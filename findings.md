# 確認した事実
- 現在の記録はイベント画像・排泄ラベル・時刻・メモ。動画保存は未実装。
- LatestCameraの取得スレッドは配信を継続取得し、検知は既定3fps。
- 全身動作用の動画は検知ROIで切り取らず、元映像を縮小して保存する必要がある。
- 既定30秒前/30秒後/5fps、長辺1280px、PNGではなくJPEGのリングバッファを使う。
- FFmpegでブラウザ再生用H.264 MP4へ変換する。保存は別スレッドで行う。
- MobileNet-SSDをOpenCV DNNで評価したが、旧画角の柵越しの犬を検出できず採用しない。試したモデルファイルはdata/models/dogに保持し、削除はしていない。
- OpenCV Zooの固定版YOLOX-sを比較し、旧画角の犬画像でbbox検出を確認。反復推論約73ms、RSS約421MiB。無地人工画像は検出なし。
- 新しい俯瞰画角での犬検出精度、実機のRTSPを含めた負荷、姿勢/排泄予測の精度は未確認。
- 採用モデルの出典：https://github.com/opencv/opencv_zoo/tree/48b10fa505aeb51fbd127fc60a5c660040e823b7/models/object_detection_yolox
