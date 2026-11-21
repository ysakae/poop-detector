# 設計の概要

監視ツールの内部構造のうち、複数のファイルを読まないと分からない部分をまとめます。利用者向けの操作手順は [README](../README.md)、開発の規約は [AGENTS.md](../AGENTS.md) を参照してください。

## パッケージの構成

サブパッケージは、README の機能表の成熟度（中核・任意・実験的）に対応しています。

| サブパッケージ | 区分 | 中身 |
|---|---|---|
| `detection/` | 中核 | 差分の判定（`difference.py`）、継続確認の状態遷移、監視範囲（ROI）、基準画像、監視ループ（`monitoring.py`） |
| `capture/` | 中核 | カメラの映像の取得と再接続、GUI へのライブ映像 |
| `records/` | 中核 | 検知イベントと排泄の記録の台帳、集計、CSV の書き出しと期間の集計（`export.py`）、バックアップと復元（`backup.py`）、SQLite の接続とスキーマの版 |
| `video/` | 中核・任意 | 動画の台帳・録画・保持（中核）、動画の自動判定と判定器（任意） |
| `features/` | 任意 | 犬の検出、見守り通知、トレー追従、行動マップ、睡眠の記録 |
| `experimental/` | 実験的 | トレー外検知（`outside.py`）、旋回分析（`circling.py`）、軌跡の特徴量（`trajectory.py`）と旋回の比較（`circling_compare.py`）、判定の前のふるい分けの指標（`screening.py`） |
| `commands/`・`gui/` | — | CLI のサブコマンドと、GUI |

最上位には、設定（`config.py`）、通知（`notifications.py`）、画像の型（`frames.py`）、時刻（`timezones.py`）、対応 OS の判定（`platform_support.py`）、CLI の入口（`main.py`）を置いています。監視ループの本体（`detection.monitoring.Monitor`）は、任意・実験的な機能に依存しません。それらは `MonitorExtension` として差し込み、`features/monitoring.py` の `standard_extensions` が設定に応じて組み立てます。検知ループ自身が通知先に求めるのは、差分検知を伝える `send` だけです。

## 2つのプロセスが `data/` を介して連携する

GUI は `poop-detector gui` で単独起動できます。`poop-detector run --gui` では監視プロセス内の別スレッドで待ち受け、監視の終了時に閉じます。どちらの方式でも `config.data_dir`（既定 `data/`）を通じて監視と GUI がデータを共有します。

- `events.sqlite3` — 次の台帳が同じファイルにテーブルを持ちます。
  - `records.events.Store`：検知イベントと排泄の記録
  - `features.sleep_store.SleepStore`：睡眠の記録（`sleep_minutes`・`sleep_detection_hours`）
  - `records.foods`・`records.advice`・`records.profile`：ごはんの量の目安と相談の台帳（`foods`：フードのカロリー、`food_lookups`：カロリー探しの依頼、`advice_threads`・`advice_messages`：家族で共有する相談の会話、`pet_profile`：犬種・体格・成犬時の予想体重・去勢の有無・主に食べているフード）。どれも秘密ではないので `SECRET_TABLES` には入れず、バックアップに含める
  - `records.sheet_wear.SheetWearLog`：シートの汚れの積み重なり（`sheet_wear`）。数え直しは、自動の行（`reset`）とシート交換のお世話の記録の新しいほうから
  - `video.clips.ClipStore`：動画の保存要求と状態
  - `video.clip_reviews.AssessmentStore`：動画の自動判定（`video_assessments`）
  - `video.judge_runs.JudgeRunLog`：判定器を呼んだ記録（`judge_runs`。判定器・モデル・用途・時刻・成否と失敗の種類だけ。用途は `review`・`benchmark` に加えて、相談の `advice` とカロリー探しの `food_lookup`）
  - `video.clip_reviews.CirclingReviews`：旋回候補
  - `video.clip_screenings.ScreeningStore`：判定の前のふるい分けの指標（`clip_screenings`。記録だけで、判定には使わない）
  - `video.clip_labels.ClipLabels`：動画のラベルと、動画からの記録の確定
  - `video.review_dismissals.ReviewDismissals`：確認待ちから外した動画（`review_dismissals`）
  - `video.benchmark_candidates.BenchmarkCandidates`：判定器の比較に使う評価の候補と条件のタグ（`benchmark_candidates`）
  - `features.outages.OutageLog`：監視の停止・カメラの不調・判定器の停止の履歴（`outages`）
  - `video.clip_storage`：保持期間・保護・整理

  接続とスキーマは `records/database.py` が管理します。スキーマの版を `PRAGMA user_version` に記録し、開くときに未適用の移行処理を順に適用します（版管理を入れる前のデータベースは版0として扱い、版1への移行でいまの形にそろえます）。
- GUI からの「今の前後の動画を保存」は `clips` テーブルへ `queued` 行を入れるだけで、監視側の `ClipCollector.poll()` が拾います。監視の生存は `capture_status` のハートビートで判定します。
- ライブ映像は `LivePreview` が `.live-preview.jpg` を書き、GUI が `.live-preview-request` の更新時刻で要求を伝えます。ホームは認証済みのHTTP接続1本（`/live/stream`）で、新しいJPEGができるたびに受け取ります。`?fps=`（1〜30）を付けると、`gui/live_stream.send_interval` の間隔より詰めて送りません（外の回線で通信を抑える「通信を節約」が使う）。管理画面の静止画取得は別の経路を使います。
- 監視は `LivePreview.pulse` で `.live-preview-monitor` に稼働状態を書き、自分で終了したときは `mark_stopped` で `stopped` を残します。GUI のサーバーはこのファイルの更新時刻を見張り、途絶えたら監視の停止として知らせます（下の「監視の停止とカメラの不調の見張り」）。
- `activity.sqlite3` は行動マップ専用の別データベースです。
- 排他は `fcntl.flock` のロックファイル（`.capture.lock`、`.review.lock`、`.activity.lock`）で行います。動画収集を伴う監視は同時に1プロセスだけです。`restore` はこの3つのロックを取れたときだけ、データの保存先を入れ替えます。
- バックアップ（`records/backup.py`）は、データベースを SQLite のオンラインバックアップで一時ディレクトリに写し、セッション・登録用 URL・Web Push の購読をコピーから消して VACUUM してから ZIP に入れます。ZIP に入れてよい名前は `ALLOWED` の表で決め、復元はその表にない名前、目録とずれた中身、SHA-256 の合わないファイルを断ります。スキーマを変えたときは、`ALLOWED` と `SECRET_TABLES` に新しいファイルやテーブルを加えるかを確かめてください。

## CLI の構成

`main.py` は引数の定義（`parser()`）と振り分けだけを受け持ちます。各サブコマンドは `set_defaults(handler=...)` で `commands/` の関数に結び付きます。

| モジュール | サブコマンド |
|---|---|
| `commands/setup.py` | `check-camera`、`setup-roi`、`setup-activity-map`、`capture-baseline` |
| `commands/run.py` | `run` |
| `commands/records.py` | `events`、`metrics`、`feedback`、`acknowledge`、`missed`、`record`、`records`、`gui` |
| `commands/notify.py` | `test-notification` |
| `commands/show_config.py`・`commands/diagnose.py` | `show-config`、`diagnose` |
| `commands/backup.py` | `backup`、`restore` |
| `commands/users.py` | `users`（GUI のユーザーの一覧、招待、役割の変更、復旧、無効化） |
| `commands/review.py` | `video-review`、`circling-review`、`video-screening` |
| `commands/circling.py` | `circling-compare` |
| `commands/benchmark.py`・`commands/datasets.py` | `video-benchmark`、`benchmark-dataset` |
| `commands/dog_model.py` | `setup-dog-model`、`check-dog` |

処理は `(args, CommandContext)` を受け取り、終了コードを返します。`CommandContext` は設定とデータの保存先の場所を持ちます。設定（`config.Config`）は不変で、`run` は `--video-review` などのフラグを `config.with_flags` で設定に重ね、機能の依存関係を `config.check_monitor_features` の1か所で確かめます。設定ファイルの各項目（セクション・名前・型・許容範囲）は `config.SETTINGS` の表で定義し、検証のエラーはその表から項目名と範囲を示します。ROI と基準画像の読み込み、通知先の選択、カメラの接続など、複数のコマンドで使う準備は `commands/context.py` にあります。サブコマンドを足すときは、用途に対応する登録関数で `CommandGroup.add()` に名前・実行関数・ヘルプを渡し、戻り値のパーサーに引数を追加します。旧名も同じ呼び出しの `aliases` に指定できます。

## 監視ループ（`detection.monitoring.Monitor`）

`LatestCamera` が取得専用スレッドで RTSP を読み続け、最新の1フレームだけを保持します。全フレームはコールバックで `ClipCollector.feed`（リングバッファ）と `LivePreview` に渡ります。監視ループは `config.fps` の間隔で最新フレームだけを処理します。

`Monitor.step` が1回分の処理で、次の段に分かれています。

1. `_read` — カメラから読み、`TrayTracker.align` でトレーの縁を目印に登録時の座標へ戻す（`--track-tray`）。再接続（`generation` の変化）、カメラの不調によるフレームの欠落、追従の失敗は中断として扱う。補正後の ROI が画面端から `EDGE_TOLERANCE`（縮小映像上の3ピクセル）まではみ出すのは許し、画面の外にあたる画素は登録時の映像で埋めて差分の根拠にしない。保存済み動画の自動判定も同じ `align` を使う。
2. `_observe` — `roi.crop` と `roi.valid_mask` で切り出し、`IncrementalDetector.analyze` が `Observation.kind` を `empty` / `candidate` / `occupied` / `unstable` のいずれかで返す。続いて拡張が観測を受け取り、上書きできる。
3. `_settle` — フレーム間の動きがあれば継続確認をやり直す。`Machine.update` が、`candidate` が `verify_seconds` 続いたときだけ True を返すと、`_record` が記録・通知済み状態の更新・動画トリガー・通知を行う。
4. `_update_reference` — 空が `clear_seconds` 続き、通知済み状態と違えば（清掃・乾燥など）通知済み状態を更新する。

フレームをまたぐ状態は `LoopState` にまとめてあり、中断時のリセットは `LoopState.interrupt` の1か所で行います。時刻は `Clock`（既定は `SystemClock`）から読むので、テストでは偽の時計を渡せます。

任意の機能は `MonitorExtension` の派生クラスとして差し込みます。呼び出し口は、位置合わせ前の映像（`on_camera_frame`）、中断（`on_interrupt`）、観測後のフレーム（`on_frame`）、排泄候補の記録（`on_event`）、通知済み状態の更新（`on_reference`。理由は `event` か `settled`）、稼働ログ（`log_status`）です。

| 拡張 | オプション | 内容 |
|---|---|---|
| `EarlyAlertExtension` | `--early-alert` | 録画を待たない滞在通知 |
| `DogExtension` | `--dog`、`--detect-outside` | 犬が ROI に重なる間や推論エラーの間は `occupied` に上書きし、入退場とトレー外の滞在で動画を保存する。トレー外の滞在は、犬がトレーに重なっている間と離れてから `TRAY_GRACE_SECONDS` の間は数えず、前の録画と時間が重ならない時刻まで延ばして録る（`_record_outside`） |
| `ActivityExtension` | `--activity-map` | 位置合わせ前の映像で犬の位置を記録する。中断は接続の途切れだけで区切る |
| `SleepExtension` | `--sleep` | 位置合わせ前の映像で犬の在室と静止を1秒ごとに観測し、1分ごとの睡眠の状態を記録する。犬検出の推論は行動マップと共有する（`SharedPredict`）。中断は接続の途切れだけで区切る |
| `SheetWearExtension` | `sheet.enabled`（既定で有効） | 通知済み状態の更新のたびに汚れの割合（`IncrementalDetector.wear_ratio`）を記録し、交換どきを一度だけ知らせる（`features/sheet_wear.py`） |
| `CameraWatchdogExtension` | `watchdog.enabled`（既定で有効） | 位置合わせ前の映像が届かない時間を数え、カメラの不調として知らせる（`features/watchdog.py`） |

新しい機能は `monitor(..., extensions=[...])` に渡せば、`Monitor` を変えずに足せます。

## 睡眠の記録の在室の判断（`features/sleep_motion.py`・`features/sleep.py`）

睡眠は「寝ていると誤って数えない」側に倒して判定します。観測を作る `sleep_motion.py` と、静止の連続から状態を決める `sleep.py` に分かれています。

- 犬の居場所は、いなくなった証拠が出るまで保ちます。検出が途切れただけでは捨てません。
- 検出した時点の枠の画像を覚え、あとで枠の中がその画像と `BOX_GONE_RATIO` を超えて違えば、居場所を捨てます。
- 丸まって寝ている犬は犬検出（YOLOX）がほとんど当たらないので、検出がなくても床面の中で `MIN_RELOCATE_AREA` 以上の動きがあれば、犬がそこへ動いたとみなして居場所を移します（`_relocate`）。枠は直前の枠の大きさ（居場所がなければ直近に検出した犬の大きさ、既定は `DOG_SIZE`）に広げ、止まった最初の静かな場面の映像を犬の姿として覚えます。出ていった直後の `EXIT_GRACE_SECONDS` の間は、出ていく途中の動きかもしれないので移しません。
- 枠の中心か足元（下端の中央）が床面の中にあれば、床面の中の犬とみなします。柵に寄りかかると、上から写した体が柵の側に伸びて中心が床面の外に出るためです。
- 動きが画面の縁で起きたときは、犬が出た（`left`）とみなします。
- `left` にしてから `EXIT_GRACE_SECONDS`（30秒）の間は、動きが枠の周りにない限り `left` を保ちます。
- 静止の途中の動きは、`STIR_RECOVERY_SECONDS`（60秒）続けて静止するまで合算します。
- 判定できない観測（`unknown`）は静止の連続を切ります。
- トレーの交換中は拡張にフレームが届かないので、`commands/run.py` の `_TrayExchange` が新しいシーンを反映したときに `interrupt` で連続を切ります。

## 検知の考え方（変更時に壊しやすい不変条件）

- 差分検知は排泄物の識別ではなく「変化の候補」です。`score` は差分画素率で確率ではありません。画像差分だけで CONFIRMED にはしません。
- `IncrementalDetector` は「空の基準」と「通知済み状態（reference）」の2つを持ち、通知済みの跡からの追加分だけを評価します。基準へ戻る方向の変化（清掃・乾燥）は追加として扱いません。
- 通知済み状態は `scene_signature`（基準画像と ROI の SHA256）をキーに SQLite へ保存し、再起動時に復元します。ROI か基準が変われば旧状態は使いません。
- 監視範囲・差分の基準画像・トレー追従の基準画像の配置は `detection.scene.SceneFiles` が定義します。GUI、監視、録画評価は同じ登録先からこれらを読みます。
- 空の基準画像は自動更新しません。`setup-roi` と `capture-baseline` は `--replace` を明示したときだけ上書きします。`load_baseline` は保存時の ROI と一致しない基準を拒否します。
- ROI の端に接する差分は、面積が小さくても遮蔽（`occupied`）として保留します。

## ROI の種類（`detection/roi.py`）

`ROI`（矩形）、`PolygonROI`（旧版の四隅・射影補正あり）、`MaskedROI`（現行の多角形・射影補正なし・マスクあり）があり、`ROI.load` が JSON の内容から型を選びます。座標はすべて正規化して保存します。`mask` を持つかどうかで処理が分岐するため、ROI を扱うコードは3種類すべてで確認してください。

## 動画と自動判定

`ClipCollector` が JPEG 圧縮したフレームをメモリー上限付きで保持し、トリガー前後を切り出して作業スレッドで MP4 に変換します。クリップの `source`（`detected`、`manual`、`dog_enter`、`dog_exit`、`early_alert`、`outside_stay`）で後段の扱いが変わります。

録画中に届いた保存要求は、`ClipCollector.trigger` が新しい行を作らずに録画を延ばしてまとめます（最初の要求から `MAX_EXTENSION_SECONDS` まで）。まとめられる動画（`event_id` のない要求で作った動画）は、録画の終わりから `MERGE_GRACE_SECONDS` の間も確定させずにコマを集め、その間に届いた要求で延ばします。要求が来なければ `_finish` が元の終わりで切ります。延ばすとメモリーの上限を超える見込みのとき（`_fits`）は延ばさずに別の動画にし、メモリーが足りなくなったら待っている録画から確定します（`_enforce_memory_limit`）。長い動画のコンタクトシートは、`write_sheets` が `SHEET_SECONDS` ごとに1枚（最大 `MAX_SHEETS` 枚）に分け、コマの間隔（`FRAME_INTERVAL`）を保ちます。差分の比較画像も同じ区切りで分け、比較の基準は動画の最初の3コマにそろえます。きっかけは `merged_source` が決め、入場と退場は `dog_visit`、ほかは `SOURCE_RANK` の優先度の高いほうを残します（`clip_reviews.PRIORITY_ORDER` と同じ順で、トレー外の滞在の動画をトレーの中の判定で見直せるようにする）。`event_id` を持つ要求（差分検知）は検知の時刻を基準にするので、まとめません。GUI からの保存要求（`poll` が拾う `queued` 行）もまとめません。

変換が終わると、操作・検知の位置の1コマを小さな JPEG にして、動画と同じ名前の `.jpg` で `clips/` に置きます（`video/thumbnails.py`）。動画の一覧は `/clip-thumbnail` で受け取り、まだない動画はそこで作ります。録画の整理（`clip_storage.remove_video`）は動画と一緒に消します。動画から作り直せるので、バックアップの `ALLOWED` には入れていません。

容量の上限は `ClipCollector._worker` が変換の前後で確かめます。変換の前に `clip_storage.free_space` が、使用量が `AUTO_CLEANUP_START` に達していれば、整理の画面と同じ条件（`CANDIDATE_SQL`）で `AUTO_CLEANUP_MIN_DAYS` より古い動画を `CLEANUP_ORDER` の順に消します（`clips.auto_cleanup`）。`.review.lock` を取れないときは消しません。変換の後は `over_limit` が、`RESERVED_SOURCES`（差分検知と GUI からの保存）以外の動画を `RESERVE_RATIO` までに抑えます。

`--video-review` では `video.review.Review` が別スレッドで変換済みクリップを監視し、時系列のコンタクトシート画像を判定器（`video.review.VideoJudge`）に渡して JSON スキーマに沿った判定を受け取ります。判定器は差し替えられる作りで、既定の実装は Codex CLI（`video.codex_cli.CodexCliJudge`）です。このモードでは差分だけの通知を抑え、判定器が根拠を確認した場合にだけ通知します。シートを作るときに、トレー内の動画では前後の差分の指標（`review.endpoint_change`。最後の3コマのうち、最初の3コマの中央値との差が4階調を超える画素の割合が最も小さい値）も測り、判定と一緒に `video_assessments.endpoint_change` に残します。判定器を呼ぶ前のふるい分け（#152）の採否を、人のラベルがたまってから確かめるための記録で、判定と通知には使いません。`video-review` サブコマンドは同じ処理を保存済み動画に通知なしで適用します。

判定器の比較（`video-benchmark`）は、人が確認した録画の評価セット（`video/benchmark_sets.py`）を使います。`benchmark-dataset create` が台帳から下書きを作り（同じイベント・確定記録・30秒以内に重なる録画は1本にまとめる）、動画の画面で人が選んだ評価の候補と条件のタグ（`video/benchmark_candidates.py`）を反映し、`freeze` が版の番号と内容の SHA-256 を付け、録画を保護します。評価セットは録画ごとに撮影時の画角の署名を持ち、署名ごとの ROI・基準画像の場所（データの保存先と `tray-scenes/` の下の過去の登録、`--scene`）を記録します。比較は録画ごとに画像を一度だけ切り出し、同じ画像と指示をすべての判定器に渡します。結果には評価セットの版、指定したモデル、CLI の版（`video/cli_versions.py`）、画像と指示のハッシュ、抽出時刻を残し、運用の記録・ラベル・自動判定は書き換えません。

`circling-review` サブコマンドは保存済み動画に犬検出をかけ、トレー付近の旋回（排泄の前兆候補）を `circling_reviews` テーブルへ記録します（`experimental/circling.py`）。オフラインの分析だけで、監視ループや通知にはつながっていません。`circling-compare` は、人がラベルを付けた動画で、この規則と軌跡の特徴量による決定木（任意の依存の scikit-learn）を日ごとの交差検証で比べます（#127）。

### 判定の前のふるい分けの指標（記録だけ）

判定器に渡す動画を減らせるかを確かめるため、犬検出を使う監視（`dog.enabled`）では、`Review` が判定の直前に `experimental.screening.ClipScreener` で動画ごとの指標を求め、`clip_screenings` に残します。判定を省いたり通知を変えたりはしません。指標を求められなくても、判定はそのまま続けます。

監視の犬検出（`dog.confidence`、既定0.4）は、かがんだ犬の確信度（0.2〜0.4）を落とすので、トレー滞在時間（`clips.tray_stay_seconds`）は排泄している動画ほど短く測られます（#127。人が確認した排便の動画で1.0秒の例がある）。そこで指標は、別に作った低いしきい値（`WEAK_CONFIDENCE`＝0.2）の検出器で、動画を1/3秒ごとに（トレー追従があれば位置を合わせて）調べて求めます。

- `tray_frames`・`weak_tray_frames`：しきい値以上の犬がトレーに重なったコマ数と、しきい値未満の犬だけが重なったコマ数（かがんだ合図の候補）
- `tray_seconds`・`weak_tray_seconds`：しきい値以上だけで数えた滞在と、低い確信度も含めた滞在の最長の時間。1秒（`MAX_GAP_SECONDS`）までの途切れはつなぐ
- `located`・`net_turning`・`path_length`・`stop_seconds`：トレー付近の犬1頭の位置から求めた軌跡の特徴量（`trajectory.py`）

OpenCV のネットはスレッド間で共有しないので、`ClipScreener` は判定のスレッドで自分の検出器を作ります。1本あたり十数秒かかります。求め方を変えたら `SCREENING_VERSION` を上げてください。古い版の記録と混ぜて比べないためです。保存済みの動画は `video-screening` で求めます。画角は、データの保存先と `tray-scenes/` の下の過去の登録から、動画の署名で選びます。

### 判定器を呼ぶ回数の上限

監視中（`Review.run` に `since` を渡したとき）は、`review.max_per_hour` を守ります。`ReviewWorker` は起動の `RESUME_WINDOW`（1日）前からの未判定の動画を対象にするので、監視を起動し直しても、前の監視が判定しきれなかった動画を取り残しません（管理 › 判定器とモデルの判定待ちの数も同じ範囲で数えます）。`Review._waiting` が動画を1本選ぶたびに、直近1時間の呼び出しの回数（`JudgeRunLog.count_since`）を数え直し、`PRIORITY_SHARES` の割合を超えた優先度の動画を後回しにします（`AssessmentStore.pending` の `max_priority`）。後回しにした動画は未判定のまま残り、回数に余裕が戻ると選ばれます。`video-review` と `video-benchmark` には上限をかけませんが、その呼び出しも回数に数えます。

### 判定器が止まったとき

判定器の呼び出しに失敗したかどうかは `RecordingJudge.failed` で分かります。失敗の種類（タイムアウト・利用制限や混雑・異常終了・判定の形式の誤りなど）は `video/judge_failures.py` が決め、`judge_runs` の `failure` と `failure_detail`（終了コード・エラーの番号・停止の理由だけ。版29で追加）に残します。判定器は種類を付けた `JudgeCallError` を送出し、付けていない例外は型から推し量ります。利用制限や混雑は CLI の応答・標準エラーの言い回しで見分けますが、CLI の文言そのものは台帳にもログにも残しません。管理 › 判定器とモデルに、直近24時間の失敗の内訳を出します。`Review` は続けて失敗した回数（`judge_failures`）を数え、`ReviewWorker.next_wait` が `retry_delay` で次の判定までの間隔を20秒から最長30分まで倍に伸ばします。呼び出しに失敗した動画は `Review.retries` に覚えておき、監視中に `MAX_RETRIES` 回まで試し直します（メモリー上だけで、監視を起動し直すと引き継ぎません）。動画を読めない、判定の形式が合わないなど、呼び出し以外の失敗は試し直しません。監視中に判定する順番は `clip_reviews.PRIORITY_ORDER` で、検知と滞在の通知が先、トレー外の滞在が最後です。失敗が続いたときの通知は、下の「監視の停止とカメラの不調の見張り」にあります。

`review.fallback` に予備の判定器があるときは、`judge_source.FallbackJudges` が主の判定器の出どころを包みます。`Review` が続けて失敗した回数を `JudgeSource.note` で伝え、`SWITCH_AFTER` 回に達すると、使える状態の次の判定器に切り替えます。続けて失敗した回数は切り替えても 0 に戻さないので、切り替えた先も失敗すればすぐ次へ進み、待ち時間も伸び続けます。`RETURN_SECONDS` が過ぎたら主の判定器に戻して試します。

### 判定器の使用量

判定の台帳（`video_assessments`）は動画1本につき1行で、失敗した判定は再試行で上書きされるため、CLI を呼んだ回数は数えられません。そこで `video.judge_runs.RecordingJudge` が判定器を包み、`judge` を呼ぶたびに `judge_runs` へ1行を追記します。監視と `video-review`（`Review._run`、用途 `review`）と、`video-benchmark`（`commands/benchmark.record_runs`、用途 `benchmark`）の両方で記録します。比較は運用の判定・ラベルを書き換えませんが、この記録だけは残します。版15への移行では、それまでの判定を台帳の1行につき1回として写します。

トークン数は、CLI が出すときだけ同じ行に残します（`video/token_usage.py`。版17で `input_tokens`・`cached_tokens`・`output_tokens` の列を足した）。Codex CLI は `codex exec --json` の `turn.completed`、Claude Code CLI は結果の `usage` から、型と範囲を確かめた数だけを取り出します。判定器は直近の呼び出しの数を `last_usage` に置き、`RecordingJudge` が読んで記録します。`input_tokens` は、キャッシュから読んだ分（`cached_tokens`）を含む入力の合計にそろえています。

サブスクリプションの5時間・週間の消費割合は保存せず、管理 › 判定器とモデルで押したときだけ `video.plan_usage.PlanUsageBoard` が各 CLI に問い合わせ、GUI のメモリーに置きます。どの CLI もモデルを呼ばない方法で問い合わせます（Codex CLI は `codex app-server` の `account/rateLimits/read`、Claude Code CLI は `claude --print /usage --max-turns 0` の表示、Antigravity CLI は `agy --print /usage --output-format json`）。型と範囲を確かめた割合と時刻だけを取り出し、アカウントの ID や応答の本文は持ちません。形が合わない版は「不明」と示します。

## ごはんの目安と相談の作業（`features/advice/`）

目安の計算（`records/feeding_advice.py`）は副作用のない関数で、判定器がなくても動きます。相談の回答とフードのカロリー探しだけが、判定器の CLI を使います。

- 質問とカロリー探しの依頼は、`advice_messages`・`food_lookups` に `queued` の行を入れるだけで、すぐ応答します。仕事の取り出しと書き戻しは `features/advice/jobs.py` が受け持ちます。取り出しは `BEGIN IMMEDIATE` の中で行い、同じ仕事を2つの処理が取りません。`running` のまま `STALE_SECONDS` を過ぎた仕事は取り残しとして `failed`（`timeout`）にします。
- 作業スレッド（`gui/advice_worker.py` の `advice_worker`）は GUI の稼働中だけ動き、`features/advice/worker.py` の `AdviceWorker.poll_once` が1件ずつ処理します。相談の判定器は設定（`review.provider`・`review.model`）から作ります。カロリー探しは、主の判定器、`review.fallback` の順にウェブ検索に対応するもの（`supports_lookup`）を選びます（`configured_lookup_judge`。予備には主のモデル名を渡しません）。フードの画面も同じ選び方で、探す欄を出すかを決めます。呼び出しは `judge_runs` に用途 `advice`・`food_lookup` で記録します。回答の本文と指示文はログに出しません。
- 1時間あたりの回数は `HOURLY_LIMIT` で抑えます。動画の自動判定の上限（`review.max_per_hour`）が数えるのは用途 `review`・`benchmark` だけで（`JudgeRunLog.count_since` の `purposes`）、相談やカロリー探しの呼び出しは動画の判定の枠を使いません。
- 作業用の一時フォルダー `advice-work/` はデータの保存先に作ります。バックアップの `ALLOWED` にないので入れず、復元でも戻しません。強制終了などで残った中のフォルダーは、作業スレッドの起動時に `clean_workdir` が、`STALE_SECONDS` より古いものだけ消します。

## 監視の停止とカメラの不調の見張り

止まった監視は自分で知らせられないので、見張り役を分けています（`features/watchdog.py`）。

- カメラの不調は、監視ループの拡張 `CameraWatchdogExtension` が、映像の届かない時間で判断します。拡張の呼び出しが `PAUSE_SECONDS` より空いたとき（トレー交換の保留）は数え直します。`run` はトレーを交換して拡張を作り直すときも、この拡張を残します。
- 監視の停止は、GUI のサーバーの別スレッド（`watching`）で `MonitorWatchdog.check` が、ハートビートの更新時刻で判断します。`gui` と `run --gui` のどちらでも動き、`commands/context.monitor_watchdog` が準備します。
- 判定器の停止は、動画の自動判定のスレッド（`ReviewWorker.poll_once`）が、判定器の呼び出しが続けて失敗した回数を `JudgeWatchdog.observe` に伝えます。`MIN_FAILURES` 回続けて失敗し、最初の失敗からしきい値が過ぎたら停止とします。この間も差分だけの通知は抑えたままです。

いずれも `features/outages.OutageTracker` が、停止が `watchdog.minutes` 続いたら一度だけ知らせ、戻ったら長さと合わせて知らせます。続いている停止は `outages` テーブルに種類ごとに1行まで（部分一意インデックス）で、行を作った見張りだけが停止を、行を閉じた見張りだけが復旧を知らせます。GUI を開き直したり、2つの GUI が同じデータを見ていたりしても、同じ停止で通知を繰り返しません。通知の文面は `ToiletNotifier.send_outage`・`send_recovery` にあり、`_deliver_watchdog` で送ります。既定はトイレの通知と同じ送り先で、`WebPushNotifier` は受け取りを選んだ管理者（`users.push_watchdog`）だけに送ります。

## 通知先

`notifications.py` の `ToiletNotifier` が文面を持ち、`web_push.py` の `WebPushNotifier`（GUI で購読した端末へ Web Push）も同じ文面で送ります（監視は `FanOutNotifier` で ntfy・macOS の通知と並べる）。`MacNotifier`（osascript）と `NtfyNotifier`（ntfy サーバーへの HTTP POST、既定は ntfy.sh）が送信だけを実装します。文面の犬の名前は設定の `pet_name` から受け取り、`MacNotifier` は AppleScript の文字列としてエスケープしてから埋め込みます。`POOP_NTFY_TOPIC` があれば ntfy、なければ macOS 通知を使います。通知先を増やすときは `_deliver` を実装したサブクラスを足してください。ntfy の無料トピックは公開扱いなので、本文に映像やカメラの URL を入れないでください。

## GUI

Starlette（ASGI）のアプリを uvicorn で動かすサーバーで、HTML は Jinja2 のテンプレート（`gui/templates/`）で作ります。フロントエンドのビルドはありません。`gui/` は次のように分かれています。

| モジュール | 役割 |
|---|---|
| `gui/server.py` | `make_server`・`serve`・`background_gui`。127.0.0.1 で待ち受けるソケットを作り、uvicorn で動かす（アクセスログは出さない） |
| `gui/asgi.py` | ASGI アプリ。安全対策のミドルウェア（`SecurityHeaders`・`Admission`）、フォームの読み込みと CSRF、ルートの表への振り分け |
| `gui/exchange.py` | ルートに渡す要求ごとの `Exchange`。`respond`・`redirect`・`stream` で応答を決める |
| `gui/routes.py` | URL ごとの処理。`@get`・`@post` で `GET_ROUTES`・`POST_ROUTES` に登録する |
| `gui/pages.py`・`gui/clip_pages.py`・`gui/activity_page.py`・`gui/sleep_page.py` | 画面に出す値を整え、テンプレートに渡す（ホーム、記録と記録の詳細、動画、行動マップ、睡眠の草） |
| `gui/advice_routes.py`・`gui/advice_page.py`・`gui/advice_worker.py` | ごはんの目安と相談（`/advice`・`/advice/foods`）。ルートは、プロフィールの保存・フードの登録と停止・相談の質問・再質問・削除・状態の問い合わせを受け付ける。`advice_page.py` が画面の値（目安・実績・会話）とホームの「今日の目安」（`today_grams`）を整え、`advice_worker.py` が GUI の稼働中に作業スレッドを動かす |
| `gui/markdown_text.py` | AI の回答の Markdown を、許したタグと https のリンクだけの HTML にする（`Markup` を作る唯一の場所） |
| `gui/clip_routes.py` | 録画要求、ラベル付け、確定、保護、評価候補の選択、録画整理の各 POST を受け付ける |
| `gui/templates/` | Jinja2 のテンプレート。`base.html` が共通の枠、`_macros.html` が部品、`style.css` がスタイル（先頭にデザインの変数） |
| `gui/templating.py` | テンプレートの環境（自動エスケープ、`StrictUndefined`）。見ている人（`VIEWER`）から共通の枠の値を渡す |
| `gui/review_queue.py`・`gui/review_routes.py` | 確認待ち。未ラベルの検知と排泄に関わる動画を1つの列にまとめ（動画のある検知は1件）、保存後に次の件を開く。画角を変える前の動画は、撮影時の画角の署名ごとにまとめて確認待ちから外せる（ラベルは付けず、戻せる。外した動画は録画の整理の対象になる） |
| `gui/settings_routes.py` | 管理 › 設定。`config.SETTINGS` の表から入力欄を並べ、確認してから保存する。保存とコメントの保持、直前の設定への復元は最上位の `settings_file.py` |
| `gui/checks_routes.py` | 管理 › 確認と診断。通知のテスト送信・カメラの確認・診断情報を、CLI と同じ関数で行う |
| `gui/models_routes.py` | 管理 › 判定器とモデル。判定器の状態と選び直し（設定の書き換えは `settings_file.py`）、監視中の判定の状況（`video/review_status.py`）、比較の結果（`video/benchmark_results.py`）、犬検出モデルの状態（`features/dog_status.py`） |
| `gui/export_routes.py` | 管理 › 書き出しとバックアップ。確定記録の CSV（`records/export.py`）、印刷用の集計、バックアップの作成（`records/backup.py`）。復元は CLI の `restore` だけで行う |
| `gui/areas_routes.py`・`gui/snapshots.py` | 管理 › 範囲の登録。範囲ごとに何に使うかを示し、映像の上に描いた多角形で、監視範囲と基準画像・床面・任意のトレー外の範囲を登録する（トレー外の範囲がなければ、トレー外検知は床面を使う）。監視中のトレーの登録し直しはトレー交換（`/manage/areas/tray`、`gui/routes.py`）。画面に見せた映像を ID で預かり、保存に使う |
| `gui/themes.py`・`gui/icons.py` | 画面の表示（ライト・ダークの選択）と、同梱のアイコンの形。デザインは1つで、色と形は `style.css` の変数にある |
| `gui/push_routes.py` | PWA のマニフェスト・Service Worker・ホーム画面のアイコン（iPhone・iPad 用の `apple-touch-icon` を含む。テーマの色で `scripts/make-app-icons.py` が作る）と、Web Push の購読の登録・解除・受け取り方・テスト |
| `gui/navigation.py` | 共通の枠の値（メインメニュー、管理のメニュー、お世話などの場所の中のタブ（`SECTION_PAGES`）、ログイン中の人、監視の状態）。画面の配置は [画面の構成](screens.md) |
| `gui/access.py` | セッションの Cookie |
| `gui/account_routes.py` | 自分のアカウント（パスキーの一覧・追加・削除）と、管理の中の招待・ユーザー・監査ログの画面 |
| `gui/sessions.py` | サーバー側のセッション（`gui_sessions` テーブル。ID の SHA-256 だけを保存し、無操作と通算の有効期限を持つ） |
| `gui/rate_limit.py` | 接続元ごとの要求の回数の制限 |
| `gui/passkeys.py` | パスキー（WebAuthn）の登録とログイン。開いたドメインから RP ID を決め、やり取り（チャレンジ）を使い捨てでメモリーに持つ。署名の検証は py_webauthn |
| `gui/auth_api.py` | パスキーの JSON の窓口（`/webauthn/...`）。セッションがない相手なので、Origin の一致と JSON の本文を必須にする |
| `gui/accounts.py` | ユーザー（`users`）、パスキー（`passkeys`）、登録用 URL（`enrollments`）の台帳。登録用 URL は使い捨てで、値は SHA-256 だけを保存する。最後の有効な管理者は無効にも一般ユーザーにもできない |
| `gui/audit.py` | 管理者の操作の監査ログ（`audit_log`）。追記だけで、認証情報は入れない |
| `gui/static/` | 補助的な操作だけを担う JavaScript（押したことがすぐ分かる表示の `busy.js` は、ログイン後のすべての画面で読み込む） |

テンプレートはすべての値を自動でエスケープし、渡し忘れた変数はエラーになります。HTML を文字列で組み立てたり、`Markup` でエスケープを外したりしないでください。例外は、ごはんの相談の AI の回答だけです。`gui/markdown_text.answer_html` が、markdown-it-py で生の HTML を無効にして（`html: False`）Markdown を HTML にし、nh3 で `ALLOWED_TAGS` のタグと https の絶対 URL のリンクだけに絞ってから `Markup` にします。画像・`style`・`class`・`title` などの属性は捨てます。CSP の `style-src` には `'unsafe-inline'` があり、インラインの style を止めないので、nh3 で落とすことが唯一の守りです。https のリンクの後ろには、行き先のドメインを「（maker.example）」のように添えます（リンクの文字にドメインが入っていれば添えない）。markdown-it のトークンの段階で文字を足すので、HTML の文字列は組み立てません。変換後が `MAX_HTML_LENGTH`（100,000文字）を超えた回答は、文字のまま出します。家族が書いた質問は、これまでどおり文字のまま出します。`Markup` を作るのはこの関数だけにし、許すタグを増やすときは `tests/test_markdown_text.py` に危ない入力の例を足してください。値の整形と分岐の判断は Python 側で行い、テンプレートは構造だけを受け持ちます。`tests/test_gui_escaping.py` は、特殊文字を含む値が各画面でエスケープされることを確かめます。

ルートが使うストアや鍵は `GuiApp` にまとめ、`Exchange.app` からルートへ渡します。ルートの関数は同期の関数で、Starlette がスレッドプールで呼びます。ルートを足すときは、`gui/routes.py` に関数を1つ書いて `@get("/path")` か `@post("/path")` を付けます。管理者だけの操作には `role="admin"` を付けます（`@get("/manage", role="admin")`）。`asgi.dispatch` が、ルートの表に宣言した役割（`routes.required_role`）を持たない人を 403 で断ります。同じルートの中で役割によって分かれる操作（動画の保護の解除）は、ルートの中で `routes.is_admin` で確かめます。テンプレートは `viewer_admin` で管理者だけの入口を出し分けますが、拒否はサーバーで行います。管理者だけの画面は `/manage` の下に置き、`gui/navigation.py` の管理のメニューに加えます。`tests/test_gui_roles.py` は、ルートの表のすべてのルートで、宣言した役割が権限の表と一致し、一般ユーザーが管理者のルートを断られることを確かめます。管理者の操作は `routes.audit` で監査ログに残します。

待ち受けは既定で `127.0.0.1` です。外から開くときは、設定の `gui` セクション（`web_address.WebOptions`）で外部の URL と信頼するプロキシを決め、HTTPS はリバースプロキシで終端します（[外から GUI を開く](remote-access.md)）。受け付ける Host と Origin は、実際のポートと外部の URL から `web_address.Site` が決めます。HTTPS かどうかは、信頼するプロキシからの `X-Forwarded-Proto` を uvicorn が反映した値で判断し、HTTPS のときだけ HSTS と Cookie の `Secure` 属性を付けます。

Host ヘッダーの検査、POST の Origin の検査、パスキーでのログインとセッション、厳しい CSP を入れています。回数の制限は `asgi.Throttle` が、ほかは `asgi.Admission` がすべての要求の前に1か所で確かめます。POST は、`asgi.dispatch` が本文の大きさと CSRF トークンも確かめてからルートに渡します。CSP などの応答ヘッダーは、`asgi.SecurityHeaders` が動画やエラーを含むすべての応答に `SECURITY_HEADERS` から付けます。要求の URL にはトークンや画像の ID が入るので、uvicorn のアクセスログは無効にしています（`tests/test_gui_asgi.py` が確かめます）。

ブラウザーのキャッシュは、セルラー回線で画面を移るたびに同じファイルを取り直さないように使います（`gui/assets.py`、`tests/test_gui_cache.py`）。`SecurityHeaders` の `Cache-Control: no-store` は既定で、ルートが `Cache-Control` を決めた応答だけが預けられます。いずれも `private` で、端末の外の共有キャッシュには残しません。

- 同梱のスクリプト・スタイル・画像は、テンプレートの `asset("menu.js")` が内容の SHA-256 を `?v=` に付けた URL を出します。いまの版を指す要求には1年の `immutable` を、版のない URL や古い版には `no-cache` と `ETag` を返し、変わっていなければ 304 にします。`style.css` は画面に埋め込まず、`/assets/style.css` として配ります（CSP の `style-src 'self'`）。テンプレートでは `/assets/...` を直に書かず、`asset(...)` を使ってください。
- 動画のサムネイルと検知の画像は、ID ごとに中身が変わらないので `IMAGE_CACHE`（1日）を付けます。見つからない応答には付けません。
- 保存した録画の動画（`/clip-video`）も、見返すたびに取り直さないよう `VIDEO_CACHE`（1日）を付けます。ETag は動画の大きさと更新時刻から作り、変わっていなければ `If-None-Match` に 304 を返します（Range の要求も同じ）。整理で消した録画も、端末に預けた分は期限まで残ることがあります（ログアウトすれば消えます）。
- 画面（HTML）、JSON、ライブ映像、マニフェスト、Service Worker は預けません。ログアウトの応答は `Clear-Site-Data: "cache"` で、預けたファイルも消してもらいます。

応答は、`GZipMiddleware` が gzip に圧縮します（ブラウザーが `Accept-Encoding: gzip` を送ったときだけ）。圧縮しない形式は `asgi.UNCOMPRESSED` で、ライブ映像（`multipart/*`）は少しずつ流すので外し、画像・動画は Starlette の既定どおり外します。HTML も圧縮しますが、セッションの Cookie が `SameSite=Strict` なので、ほかのサイトからログインした画面を開かせて応答の長さを測る攻撃（BREACH）はできません（`tests/test_gui_compression.py`）。

POST の CSRF トークンはセッションごとで、`GuiApp.csrf_for` がセッションの ID と起動ごとの鍵の HMAC から導きます。`asgi.Admission` が確かめたセッションを scope の `state` でルートへ渡し、`Exchange.csrf` から画面のフォームに埋め込みます。パスキーでログインすると `SessionStore.create` がセッションを始め、その ID を HttpOnly の Cookie に入れます。以後の要求は、Cookie のセッションを `SessionStore.check` で確かめます。セッションはユーザーに結び付き（`user_id` 列）、ユーザーのないセッションは使えません。ログインしていなくても開けるのは、ログイン（`/login`）、パスキーの登録（`/enroll`）、その画面のスクリプト、パスキーの JSON の窓口だけです（`asgi.PUBLIC_PATHS`）。ほかの画面をブラウザーで開くとログインの画面へ移し、画像などには 403 を返します。無効にしたユーザーのセッションは使えません。最初の管理者の登録と、パスキーをなくしたときの復旧は、監視するマシンで実行する `users invite`・`users recover` が出す登録用 URL で行います。このマシンで実行できること自体を信頼の根拠にします。`tests/test_gui_routes.py` は、ルートの表にあるすべてのルートで安全対策が効くことを確かめます。インラインスクリプトは CSP で動かないため、JavaScript は `gui/static/` に置き、`gui/routes.py` の `asset` のパスに加えてください。

## 時刻とタイムゾーン

データベースには UTC のタイムゾーン付き ISO 8601 で保存し、画面の表示と日別集計だけをローカル時刻に変換します（`timezones.py`）。ローカル時刻はプロセスのタイムゾーンに従い、設定の `timezone` があれば起動時に `config.apply_timezone` が `TZ` 環境変数として反映します。テストは `tests/conftest.py` で日本時間に固定しているので、実行するマシンのタイムゾーンに左右されません。

## エラーの伝え方と、認証情報を出さない仕組み

RTSP URL には認証情報が含まれます。次の仕組みを重ねて、どの出力にも URL が出ないようにしています。この import 順と例外処理を崩さないでください。

- パッケージの `__init__.py` が、どのモジュールよりも先に OpenCV と FFmpeg のログを抑制します。
- 利用者に原因を伝える失敗は、`errors.UserError` の派生クラスで送出します。`main()` はその文言をそのまま表示し、終了コード 2 を返します。

  | 例外 | 使う場面 |
  |---|---|
  | `SetupRequiredError` | 監視範囲・基準画像などが未登録 |
  | `ReferenceMismatchError` | 基準画像がいまの ROI や映像と合わない |
  | `CameraUnavailableError` | カメラから映像を取得できない |
  | `InvalidInputError` | 引数や入力値の誤り |
  | `AlreadyRunningError` | 同じデータを使う別の処理が動いている |
  | `IncompatibleDataError` | データベースがこの版で扱えない |
  | `ConfigError`・`CommandError`・`TrayReferenceError` | 設定の誤り、コマンドの前提、トレーの基準画像 |

  `UserError` の文言には、外部の例外の文字列を入れないでください。`ConfigError` 以外の入力の誤りの派生クラスは `ValueError` も継承しているので、`except ValueError` で文言を受け取る GUI などはそのまま動きます。
- 外部ライブラリ由来の `ValueError`・`OSError`・`cv2.error` は、文字列を表示せず、例外の種類と確認すべき項目だけを出します（終了コード 2）。それ以外の例外はプログラムの誤りとして、種類と報告のお願いを出します（終了コード 1）。
- `--verbose`（`-v`）を付けると、ログを DEBUG にし、失敗したときにトレースバックを表示します。表示の前に `redaction.redact` でカメラの URL を丸ごと、ほかの URL はユーザー名とパスワードを伏せ字にします。
- ログは `redaction.install_log_redaction` が記録を作る時点で本文を伏せ字にするので、どのハンドラーにも URL が渡りません。トレースバックは `RedactingFormatter` が伏せます。
- コマンドの結果（JSON や完了の知らせ）は `print` で標準出力に、診断（警告や失敗の詳細）は `logging` で標準エラーに出します。

## テストの作り方

`Camera` の `factory`、`MacNotifier` の `runner`、`NtfyNotifier` の `opener`、`ClipCollector` の `encoder`、`inspect_clip` の `judge`（`CodexCliJudge` の `runner`）、`DogDetector` の `net` は差し替え用の引数です。`monitor` も `max_frames` と偽のカメラで実行できます。CLI 全体は `commands.context.Camera` を偽のカメラに差し替えて動かします（`tests/test_cli.py`、検知から動画保存までを通す `tests/test_pipeline.py`）。テストは NumPy で作った人工画像と `tmp_path` を使い、実カメラ・実データ・Codex CLI には依存させません。
