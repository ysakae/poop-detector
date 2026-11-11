# 設計の概要

監視ツールの内部構造のうち、複数のファイルを読まないと分からない部分をまとめます。利用者向けの操作手順は [README](../README.md)、開発の規約は [AGENTS.md](../AGENTS.md) を参照してください。

## 2つのプロセスが `data/` を介して連携する

監視（`poop-detector run`）とラベル GUI（`poop-detector label-gui`）は別プロセスで、直接は通信しません。`config.data_dir`（既定 `data/`）の中身だけが接点です。

- `events.sqlite3` — `events.Store` と `clips.ClipStore` が同じファイルにテーブルを持ちます。スキーマは各クラスの `__init__` の `CREATE TABLE IF NOT EXISTS` と列追加で管理し、マイグレーションの仕組みはありません。
- GUI からの「今の前後の動画を保存」は `clips` テーブルへ `queued` 行を入れるだけで、監視側の `ClipCollector.poll()` が拾います。監視の生存は `capture_status` のハートビートで判定します。
- ライブ映像は `LivePreview` が `.live-preview.jpg` を書き、GUI が `.live-preview-request` の更新時刻で要求を伝えます。
- `activity.sqlite3` は行動マップ専用の別データベースです。
- 排他は `fcntl.flock` のロックファイル（`.capture.lock`、`.codex.lock`、`.activity.lock`）で行います。動画収集を伴う監視は同時に1プロセスだけです。

## CLI の構成

`main.py` は引数の定義（`parser()`）と振り分けだけを受け持ちます。各サブコマンドは `set_defaults(handler=...)` で `commands/` の関数に結び付きます。

| モジュール | サブコマンド |
|---|---|
| `commands/setup.py` | `check-camera`、`setup-roi`、`setup-activity-map`、`capture-baseline` |
| `commands/run.py` | `run` |
| `commands/records.py` | `events`、`metrics`、`feedback`、`acknowledge`、`missed`、`record`、`records`、`label-gui` |
| `commands/notify.py` | `test-notification` |
| `commands/review.py` | `codex-review`、`circling-review` |
| `commands/dog_model.py` | `setup-dog-model`、`check-dog` |

処理は `(args, CommandContext)` を受け取り、終了コードを返します。`CommandContext` は設定とデータの保存先の場所を持ちます。設定は書き換えず、`--dog` などを反映した新しい文脈を作ります（`with_dog_detection`）。ROI と基準画像の読み込み、通知先の選択、カメラの接続など、複数のコマンドで使う準備は `commands/context.py` にあります。サブコマンドを足すときは、`parser()` に引数を、`HANDLERS` に処理を加えてください。

## 監視ループ（`monitoring.Monitor`）

`LatestCamera` が取得専用スレッドで RTSP を読み続け、最新の1フレームだけを保持します。全フレームはコールバックで `ClipCollector.feed`（リングバッファ）と `LivePreview` に渡ります。監視ループは `config.fps` の間隔で最新フレームだけを処理します。

`Monitor.step` が1回分の処理で、次の段に分かれています。

1. `_read` — カメラから読み、`TrayTracker.align` でトレーの縁を目印に登録時の座標へ戻す（`--track-tray`）。再接続（`generation` の変化）、カメラの不調によるフレームの欠落、追従の失敗は中断として扱う。
2. `_observe` — `roi.crop` と `roi.valid_mask` で切り出し、`IncrementalDetector.analyze` が `Observation.kind` を `empty` / `candidate` / `occupied` / `unstable` のいずれかで返す。続いて拡張が観測を受け取り、上書きできる。
3. `_settle` — フレーム間の動きがあれば継続確認をやり直す。`Machine.update` が、`candidate` が `verify_seconds` 続いたときだけ True を返すと、`_record` が記録・通知済み状態の更新・動画トリガー・通知を行う。
4. `_update_reference` — 空が `clear_seconds` 続き、通知済み状態と違えば（清掃・乾燥など）通知済み状態を更新する。

フレームをまたぐ状態は `LoopState` にまとめてあり、中断時のリセットは `LoopState.interrupt` の1か所で行います。時刻は `Clock`（既定は `SystemClock`）から読むので、テストでは偽の時計を渡せます。

任意の機能は `MonitorExtension` の派生クラスとして差し込みます。呼び出し口は、位置合わせ前の映像（`on_camera_frame`）、中断（`on_interrupt`）、観測後のフレーム（`on_frame`）、稼働ログ（`log_status`）の4つです。

| 拡張 | オプション | 内容 |
|---|---|---|
| `EarlyAlertExtension` | `--early-alert` | 録画を待たない滞在通知 |
| `DogExtension` | `--dog`、`--detect-outside` | 犬が ROI に重なる間や推論エラーの間は `occupied` に上書きし、入退場とトレー外の滞在で動画を保存する |
| `ActivityExtension` | `--activity-map` | 位置合わせ前の映像で犬の位置を記録する。中断は接続の途切れだけで区切る |

新しい機能は `monitor(..., extensions=[...])` に渡せば、`Monitor` を変えずに足せます。

## 検知の考え方（変更時に壊しやすい不変条件）

- 差分検知は排泄物の識別ではなく「変化の候補」です。`score` は差分画素率で確率ではありません。画像差分だけで CONFIRMED にはしません。
- `IncrementalDetector` は「空の基準」と「通知済み状態（reference）」の2つを持ち、通知済みの跡からの追加分だけを評価します。基準へ戻る方向の変化（清掃・乾燥）は追加として扱いません。
- 通知済み状態は `scene_signature`（基準画像と ROI の SHA256）をキーに SQLite へ保存し、再起動時に復元します。ROI か基準が変われば旧状態は使いません。
- 空の基準画像は自動更新しません。`setup-roi` と `capture-baseline` は `--replace` を明示したときだけ上書きします。`load_baseline` は保存時の ROI と一致しない基準を拒否します。
- ROI の端に接する差分は、面積が小さくても遮蔽（`occupied`）として保留します。

## ROI の種類（`roi.py`）

`ROI`（矩形）、`PolygonROI`（旧版の四隅・射影補正あり）、`MaskedROI`（現行の多角形・射影補正なし・マスクあり）があり、`ROI.load` が JSON の内容から型を選びます。座標はすべて正規化して保存します。`mask` を持つかどうかで処理が分岐するため、ROI を扱うコードは3種類すべてで確認してください。

## 動画と Codex 判定

`ClipCollector` が JPEG 圧縮したフレームをメモリー上限付きで保持し、トリガー前後を切り出して作業スレッドで MP4 に変換します。クリップの `source`（`detected`、`manual`、`dog_enter`、`dog_exit`、`early_alert`、`outside_stay`）で後段の扱いが変わります。

`--codex` では `codex_review.Review` が別スレッドで変換済みクリップを監視し、時系列のコンタクトシート画像を Codex CLI に渡して JSON スキーマに沿った判定を受け取ります。このモードでは差分だけの通知を抑え、Codex が根拠を確認した場合にだけ通知します。`codex-review` サブコマンドは同じ処理を保存済み動画に通知なしで適用します。

`circling-review` サブコマンドは保存済み動画に犬検出をかけ、トレー付近の旋回（排泄の前兆候補）を `circling_reviews` テーブルへ記録します（`circling.py`）。オフラインの分析だけで、監視ループや通知にはつながっていません。

## 通知先

`notifications.py` の `_ToiletNotifier` が文面を持ち、`MacNotifier`（osascript）と `NtfyNotifier`（ntfy サーバーへの HTTP POST、既定は ntfy.sh）が送信だけを実装します。文面の犬の名前は設定の `pet_name` から受け取り、`MacNotifier` は AppleScript の文字列としてエスケープしてから埋め込みます。`POOP_NTFY_TOPIC` があれば ntfy、なければ macOS 通知を使います。通知先を増やすときは `_deliver` を実装したサブクラスを足してください。ntfy の無料トピックは公開扱いなので、本文に映像やカメラの URL を入れないでください。

## ラベル GUI

`label_gui.make_server` は標準ライブラリの `http.server` だけで作ったサーバーで、HTML は Python の文字列で組み立てます（テンプレートエンジンやフロントエンドのビルドはありません）。画面ごとに `clip_gui.py`（動画）、`activity_gui.py`（行動マップ）へ分かれ、`static/` の JavaScript は補助的な操作だけを担います。

外部公開しない前提でも、Host ヘッダーの検査、起動ごとのトークン、厳しい CSP を入れています。インラインスクリプトは CSP で動かないため、JavaScript は `static/` に置き、`/assets/` の許可リストへ追加してください。

## 時刻とタイムゾーン

データベースには UTC のタイムゾーン付き ISO 8601 で保存し、画面の表示と日別集計だけをローカル時刻に変換します（`timezones.py`）。ローカル時刻はプロセスのタイムゾーンに従い、設定の `timezone` があれば起動時に `config.apply_timezone` が `TZ` 環境変数として反映します。テストは `tests/conftest.py` で日本時間に固定しているので、実行するマシンのタイムゾーンに左右されません。

## 認証情報をログに出さない仕組み

RTSP URL には認証情報が含まれます。パッケージの `__init__.py` が、どのモジュールよりも先に OpenCV と FFmpeg のログを抑制します。`main()` の最上位の例外処理は、外部例外の文字列を表示せず固定の文言だけを出します。サブコマンドが理由を伝えて終えるときは `commands.context.CommandError` を送出し、その文言だけを表示します。`CommandError` には外部の例外の文字列を入れないでください。この import 順と例外処理を崩さないでください。エラーの詳細を出したい場合も、例外メッセージをそのまま表示しないでください。

## テストの作り方

`Camera` の `factory`、`MacNotifier` の `runner`、`NtfyNotifier` の `opener`、`ClipCollector` の `encoder`、`inspect_clip` の `runner`、`DogDetector` の `net` は差し替え用の引数です。`monitor` も `max_frames` と偽のカメラで実行できます。CLI 全体は `commands.context.Camera` を偽のカメラに差し替えて動かします（`tests/test_cli.py`、検知から動画保存までを通す `tests/test_pipeline.py`）。テストは NumPy で作った人工画像と `tmp_path` を使い、実カメラ・実データ・Codex CLI には依存させません。
