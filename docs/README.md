# docs

## 利用者向け

| ファイル | 内容 |
|---|---|
| [setup.md](setup.md) | 初期設定。カメラ、`.env`、監視領域、基準画像、トレー追従 |
| [detection.md](detection.md) | 判定の仕組み。差分検知、排尿の検知、見守り通知、設定項目、制限 |
| [records.md](records.md) | 記録とラベル付け。ラベル GUI、排泄の記録、評価指標 |
| [notifications.md](notifications.md) | 通知。macOS の通知と ntfy |
| [video.md](video.md) | 動画。保存、容量の管理、動画の自動判定 |
| [dog.md](dog.md) | 犬の検出。犬の存在、トレー外の検知、行動マップ、旋回候補 |
| [docker.md](docker.md) | Docker / Linux での実行 |
| [data-collection.md](data-collection.md) | データ収集の方針と今後 |
| [privacy.md](privacy.md) | データの保存先と、機能ごとに外部へ送られる情報。ラベル GUI の安全対策と限界 |
| [licenses.md](licenses.md) | 依存ライブラリ、犬検出モデル、FFmpeg、Docker イメージのライセンス |

## 開発者向け

| ファイル | 内容 |
|---|---|
| [architecture.md](architecture.md) | 設計の概要。監視とラベル GUI の連携、監視ループの流れ、検知の不変条件など |
| [releasing.md](releasing.md) | リリースと PyPI への公開の手順（メンテナー向け） |
| [validation.json](validation.json) | 初期 MVP の差分検知の処理時間と負荷の実測値 |
| [dog-model-validation.json](dog-model-validation.json) | 犬検出モデル（OpenCV Zoo YOLOX-s）の検出結果と推論時間の実測値 |

## 検証結果の測定条件

どちらも開発者の Apple Silicon の Mac で測定した参考値です。環境や映像によって結果は変わります。

- **validation.json**: 640×480 の人工画像で、差分検知1,000回の平均処理時間、プロセスの CPU 使用率とメモリー、監視ループを24フレーム回したときの負荷を記録しています。RTSP の受信・デコードの負荷と、実際の排泄からの通知遅延は含みません。
- **dog-model-validation.json**: カメラの設置位置を変える前に保存した画像8枚と無地の人工画像で、犬の検出枠と1枚あたりの推論時間を記録しています。`file` は保存時に付けたランダムな ID で、画像そのものはリポジトリに含めていません。現在の設置位置での検出精度と、RTSP の処理を含めた総負荷は測定していません。

各ファイルの `scope` にも測定の範囲を記載しています。
