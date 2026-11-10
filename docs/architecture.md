# 設計の概要

監視ツールの内部構造のうち、複数のファイルを読まないと分からない部分をまとめます。利用者向けの操作手順は [README](../README.md)、開発の規約は [AGENTS.md](../AGENTS.md) を参照してください。

## 2つのプロセスが `data/` を介して連携する

監視（`poop-detector run`）とラベル GUI（`poop-detector label-gui`）は別プロセスで、直接は通信しません。`config.data_dir`（既定 `data/`）の中身だけが接点です。

- `events.sqlite3` — `events.Store` と `clips.ClipStore` が同じファイルにテーブルを持ちます。スキーマは各クラスの `__init__` の `CREATE TABLE IF NOT EXISTS` と列追加で管理し、マイグレーションの仕組みはありません。
- GUI からの「今の前後の動画を保存」は `clips` テーブルへ `queued` 行を入れるだけで、監視側の `ClipCollector.poll()` が拾います。監視の生存は `capture_status` のハートビートで判定します。
- ライブ映像は `LivePreview` が `.live-preview.jpg` を書き、GUI が `.live-preview-request` の更新時刻で要求を伝えます。
- `activity.sqlite3` は行動マップ専用の別データベースです。
- 排他は `fcntl.flock` のロックファイル（`.capture.lock`、`.codex.lock`、`.activity.lock`）で行います。動画収集を伴う監視は同時に1プロセスだけです。

## 監視ループ（`main.monitor`）

`LatestCamera` が取得専用スレッドで RTSP を読み続け、最新の1フレームだけを保持します。全フレームはコールバックで `ClipCollector.feed`（リングバッファ）と `LivePreview` に渡り、`monitor` は `config.fps` の間隔で最新フレームだけを処理します。1フレームの流れは次のとおりです。

1. `TrayTracker.align` — トレーの縁を目印に登録時の座標へ戻す（`--track-tray`）。合わせられなければ中断扱い。
2. `roi.crop` と `roi.mask` — ROI で切り出す。多角形 ROI は外側をマスクで除外。
3. `EarlyAlert.update` — 録画を待たない滞在通知（`--early-alert`）。
4. `IncrementalDetector.analyze` — `Observation.kind` を `empty` / `candidate` / `occupied` / `unstable` のいずれかで返す。
5. `DogObserver.update` — 犬の検出枠が ROI に重なる間、または推論エラーの間は `occupied` に上書き（`--dog`）。入退場は動画保存のトリガーにもなる。
6. `Machine.update` — `candidate` が `verify_seconds` 続いたときだけ True を返し、`Store.record` と通知、動画トリガーへ進む。

カメラの再接続（`camera.generation` の変化）、追従の失敗、フレーム間の動きは、いずれも継続確認のタイマーをリセットします。

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

RTSP URL には認証情報が含まれます。`main.py` は OpenCV の読み込み前に FFmpeg のログを抑制し、`main()` の最上位の例外処理は外部例外の文字列を表示せず固定の文言だけを出します。この import 順と例外処理を崩さないでください。エラーの詳細を出したい場合も、例外メッセージをそのまま表示しないでください。

## テストの作り方

`Camera` の `factory`、`MacNotifier` の `runner`、`NtfyNotifier` の `opener`、`ClipCollector` の `encoder`、`inspect_clip` の `runner`、`DogDetector` の `net` は差し替え用の引数です。`monitor` も `max_frames` と偽のカメラで実行できます。CLI 全体は `main.Camera` を偽のカメラに差し替えて動かします（`tests/test_cli.py`、検知から動画保存までを通す `tests/test_pipeline.py`）。テストは NumPy で作った人工画像と `tmp_path` を使い、実カメラ・実データ・Codex CLI には依存させません。
