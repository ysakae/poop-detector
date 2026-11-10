# docs

| ファイル | 内容 |
|---|---|
| [architecture.md](architecture.md) | 設計の概要。監視とラベル GUI の連携、監視ループの流れ、検知の不変条件など |
| [validation.json](validation.json) | 初期 MVP の差分検知の処理時間と負荷の実測値 |
| [dog-model-validation.json](dog-model-validation.json) | 犬検出モデル（OpenCV Zoo YOLOX-s）の検出結果と推論時間の実測値 |

## 検証結果の測定条件

どちらも開発者の Apple Silicon の Mac で測定した参考値です。環境や映像によって結果は変わります。

- **validation.json**: 640×480 の人工画像で、差分検知1,000回の平均処理時間、プロセスの CPU 使用率とメモリー、監視ループを24フレーム回したときの負荷を記録しています。RTSP の受信・デコードの負荷と、実際の排泄からの通知遅延は含みません。
- **dog-model-validation.json**: カメラの設置位置を変える前に保存した画像8枚と無地の人工画像で、犬の検出枠と1枚あたりの推論時間を記録しています。`file` は保存時に付けたランダムな ID で、画像そのものはリポジトリに含めていません。現在の設置位置での検出精度と、RTSP の処理を含めた総負荷は測定していません。

各ファイルの `scope` にも測定の範囲を記載しています。
