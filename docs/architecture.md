# 設計の概要

監視ツールの内部構造のうち、複数のファイルを読まないと分からない部分をまとめます。利用者向けの操作手順は [README](../README.md)、開発の規約は [AGENTS.md](../AGENTS.md) を参照してください。

## パッケージの構成

サブパッケージは、README の機能表の成熟度（中核・任意・実験的）に対応しています。

| サブパッケージ | 区分 | 中身 |
|---|---|---|
| `detection/` | 中核 | 差分の判定（`difference.py`）、継続確認の状態遷移、監視範囲（ROI）、基準画像、監視ループ（`monitoring.py`） |
| `capture/` | 中核 | カメラの映像の取得と再接続、GUI へのライブ映像 |
| `records/` | 中核 | 検知イベントと排泄の記録の台帳、集計、SQLite の接続とスキーマの版 |
| `video/` | 中核・任意 | 動画の台帳・録画・保持（中核）、動画の自動判定と判定器（任意） |
| `features/` | 任意 | 犬の検出、見守り通知、トレー追従、行動マップ |
| `experimental/` | 実験的 | トレー外検知（`outside.py`）、旋回分析（`circling.py`） |
| `commands/`・`gui/` | — | CLI のサブコマンドと、GUI |

最上位には、設定（`config.py`）、通知（`notifications.py`）、画像の型（`frames.py`）、時刻（`timezones.py`）、対応 OS の判定（`platform_support.py`）、CLI の入口（`main.py`）を置いています。監視ループの本体（`detection.monitoring.Monitor`）は、任意・実験的な機能に依存しません。それらは `MonitorExtension` として差し込み、設定から拡張を組み立てる `standard_extensions` だけが各機能を読み込みます。

## 2つのプロセスが `data/` を介して連携する

GUI は `poop-detector gui` で単独起動できます。`poop-detector run --gui` では監視プロセス内の別スレッドで待ち受け、監視の終了時に閉じます。どちらの方式でも `config.data_dir`（既定 `data/`）を通じて監視と GUI がデータを共有します。

- `events.sqlite3` — 次の台帳が同じファイルにテーブルを持ちます。
  - `records.events.Store`：検知イベントと排泄の記録
  - `video.clips.ClipStore`：動画の保存要求と状態
  - `video.clip_reviews.AssessmentStore`：動画の自動判定（`video_assessments`）
  - `video.clip_reviews.CirclingReviews`：旋回候補
  - `video.clip_labels.ClipLabels`：動画のラベルと、動画からの記録の確定
  - `video.clip_storage`：保持期間・保護・整理

  接続とスキーマは `records/database.py` が管理します。スキーマの版を `PRAGMA user_version` に記録し、開くときに未適用の移行処理を順に適用します（版管理を入れる前のデータベースは版0として扱い、版1への移行でいまの形にそろえます）。
- GUI からの「今の前後の動画を保存」は `clips` テーブルへ `queued` 行を入れるだけで、監視側の `ClipCollector.poll()` が拾います。監視の生存は `capture_status` のハートビートで判定します。
- ライブ映像は `LivePreview` が `.live-preview.jpg` を書き、GUI が `.live-preview-request` の更新時刻で要求を伝えます。
- `activity.sqlite3` は行動マップ専用の別データベースです。
- 排他は `fcntl.flock` のロックファイル（`.capture.lock`、`.review.lock`、`.activity.lock`）で行います。動画収集を伴う監視は同時に1プロセスだけです。

## CLI の構成

`main.py` は引数の定義（`parser()`）と振り分けだけを受け持ちます。各サブコマンドは `set_defaults(handler=...)` で `commands/` の関数に結び付きます。

| モジュール | サブコマンド |
|---|---|
| `commands/setup.py` | `check-camera`、`setup-roi`、`setup-activity-map`、`capture-baseline` |
| `commands/run.py` | `run` |
| `commands/records.py` | `events`、`metrics`、`feedback`、`acknowledge`、`missed`、`record`、`records`、`gui` |
| `commands/notify.py` | `test-notification` |
| `commands/show_config.py`・`commands/diagnose.py` | `show-config`、`diagnose` |
| `commands/users.py` | `users`（GUI のユーザーの一覧、登録用 URL、復旧、無効化） |
| `commands/review.py` | `video-review`、`circling-review` |
| `commands/dog_model.py` | `setup-dog-model`、`check-dog` |

処理は `(args, CommandContext)` を受け取り、終了コードを返します。`CommandContext` は設定とデータの保存先の場所を持ちます。設定（`config.Config`）は不変で、`run` は `--video-review` などのフラグを `config.with_flags` で設定に重ね、機能の依存関係を `config.check_monitor_features` の1か所で確かめます。設定ファイルの各項目（セクション・名前・型・許容範囲）は `config.SETTINGS` の表で定義し、検証のエラーはその表から項目名と範囲を示します。ROI と基準画像の読み込み、通知先の選択、カメラの接続など、複数のコマンドで使う準備は `commands/context.py` にあります。サブコマンドを足すときは、`parser()` に引数を、`HANDLERS` に処理を加えてください。

## 監視ループ（`detection.monitoring.Monitor`）

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

## ROI の種類（`detection/roi.py`）

`ROI`（矩形）、`PolygonROI`（旧版の四隅・射影補正あり）、`MaskedROI`（現行の多角形・射影補正なし・マスクあり）があり、`ROI.load` が JSON の内容から型を選びます。座標はすべて正規化して保存します。`mask` を持つかどうかで処理が分岐するため、ROI を扱うコードは3種類すべてで確認してください。

## 動画と自動判定

`ClipCollector` が JPEG 圧縮したフレームをメモリー上限付きで保持し、トリガー前後を切り出して作業スレッドで MP4 に変換します。クリップの `source`（`detected`、`manual`、`dog_enter`、`dog_exit`、`early_alert`、`outside_stay`）で後段の扱いが変わります。

`--video-review` では `video.review.Review` が別スレッドで変換済みクリップを監視し、時系列のコンタクトシート画像を判定器（`video.review.VideoJudge`）に渡して JSON スキーマに沿った判定を受け取ります。判定器は差し替えられる作りで、既定の実装は Codex CLI（`video.codex_cli.CodexCliJudge`）です。このモードでは差分だけの通知を抑え、判定器が根拠を確認した場合にだけ通知します。`video-review` サブコマンドは同じ処理を保存済み動画に通知なしで適用します。

`circling-review` サブコマンドは保存済み動画に犬検出をかけ、トレー付近の旋回（排泄の前兆候補）を `circling_reviews` テーブルへ記録します（`experimental/circling.py`）。オフラインの分析だけで、監視ループや通知にはつながっていません。

## 通知先

`notifications.py` の `_ToiletNotifier` が文面を持ち、`MacNotifier`（osascript）と `NtfyNotifier`（ntfy サーバーへの HTTP POST、既定は ntfy.sh）が送信だけを実装します。文面の犬の名前は設定の `pet_name` から受け取り、`MacNotifier` は AppleScript の文字列としてエスケープしてから埋め込みます。`POOP_NTFY_TOPIC` があれば ntfy、なければ macOS 通知を使います。通知先を増やすときは `_deliver` を実装したサブクラスを足してください。ntfy の無料トピックは公開扱いなので、本文に映像やカメラの URL を入れないでください。

## GUI

Starlette（ASGI）のアプリを uvicorn で動かすサーバーで、HTML は Jinja2 のテンプレート（`gui/templates/`）で作ります。フロントエンドのビルドはありません。`gui/` は次のように分かれています。

| モジュール | 役割 |
|---|---|
| `gui/server.py` | `make_server`・`serve`・`background_gui`。127.0.0.1 で待ち受けるソケットを作り、uvicorn で動かす（アクセスログは出さない） |
| `gui/asgi.py` | ASGI アプリ。安全対策のミドルウェア（`SecurityHeaders`・`Admission`）、フォームの読み込みと CSRF、ルートの表への振り分け |
| `gui/exchange.py` | ルートに渡す要求ごとの `Exchange`。`respond`・`redirect`・`stream` で応答を決める |
| `gui/routes.py` | URL ごとの処理。`@get`・`@post` で `GET_ROUTES`・`POST_ROUTES` に登録する |
| `gui/pages.py`・`gui/clip_pages.py`・`gui/activity_page.py` | 画面に出す値を整え、テンプレートに渡す（トップと記録、動画、行動マップ） |
| `gui/templates/` | Jinja2 のテンプレート。`base.html` が共通の枠、`_macros.html` が部品、`style.css` がスタイル |
| `gui/templating.py` | テンプレートの環境（自動エスケープ、`StrictUndefined`） |
| `gui/access.py` | 起動トークンと、セッションの Cookie |
| `gui/sessions.py` | サーバー側のセッション（`gui_sessions` テーブル。ID の SHA-256 だけを保存し、無操作と通算の有効期限を持つ） |
| `gui/rate_limit.py` | 接続元ごとの要求の回数の制限 |
| `gui/passkeys.py` | パスキー（WebAuthn）の登録とログイン。開いたドメインから RP ID を決め、やり取り（チャレンジ）を使い捨てでメモリーに持つ。署名の検証は py_webauthn |
| `gui/auth_api.py` | パスキーの JSON の窓口（`/webauthn/...`）。セッションがない相手なので、Origin の一致と JSON の本文を必須にする |
| `gui/accounts.py` | ユーザー（`users`）、パスキー（`passkeys`）、登録用 URL（`enrollments`）の台帳。登録用 URL は使い捨てで、値は SHA-256 だけを保存する。最後の有効な管理者は無効にできない |
| `gui/static/` | 補助的な操作だけを担う JavaScript |

テンプレートはすべての値を自動でエスケープし、渡し忘れた変数はエラーになります。HTML を文字列で組み立てたり、`Markup` でエスケープを外したりしないでください。値の整形と分岐の判断は Python 側で行い、テンプレートは構造だけを受け持ちます。`tests/test_gui_escaping.py` は、特殊文字を含む値が各画面でエスケープされることを確かめます。

ルートが使うストアやトークンは `GuiApp` にまとめ、`Exchange.app` からルートへ渡します。ルートの関数は同期の関数で、Starlette がスレッドプールで呼びます。ルートを足すときは、`gui/routes.py` に関数を1つ書いて `@get("/path")` か `@post("/path")` を付けます。

待ち受けは既定で `127.0.0.1` です。外から開くときは、設定の `gui` セクション（`web_address.WebOptions`）で外部の URL と信頼するプロキシを決め、HTTPS はリバースプロキシで終端します（[外から GUI を開く](remote-access.md)）。受け付ける Host と Origin は、実際のポートと外部の URL から `web_address.Site` が決めます。HTTPS かどうかは、信頼するプロキシからの `X-Forwarded-Proto` を uvicorn が反映した値で判断し、HTTPS のときだけ HSTS と Cookie の `Secure` 属性を付けます。

Host ヘッダーの検査、POST の Origin の検査、起動ごとのトークン、厳しい CSP を入れています。回数の制限は `asgi.Throttle` が、ほかは `asgi.Admission` がすべての要求の前に1か所で確かめます。POST は、`asgi.dispatch` が本文の大きさと CSRF トークンも確かめてからルートに渡します。CSP などの応答ヘッダーは、`asgi.SecurityHeaders` が動画やエラーを含むすべての応答に `SECURITY_HEADERS` から付けます。要求の URL にはトークンや画像の ID が入るので、uvicorn のアクセスログは無効にしています（`tests/test_gui_asgi.py` が確かめます）。

トークンは2つあり、POST の CSRF 対策と、セッションを始めるための起動トークンです。CSRF トークンはセッションごとで、`GuiApp.csrf_for` がセッションの ID と起動ごとの鍵の HMAC から導きます。`asgi.Admission` が確かめたセッションを scope の `state` でルートへ渡し、`Exchange.csrf` から画面のフォームに埋め込みます。起動時の URL のトークンを受け取ると `SessionStore.create` がセッションを始め、その ID を HttpOnly の Cookie に入れます。以後の要求は、Cookie のセッションを `SessionStore.check` で確かめます。セッションの台帳は、パスキー認証（#84）でユーザーとひも付けます（`user_id` 列）。ログインしていなくても開けるのは、ログイン（`/login`）、パスキーの登録（`/enroll`）、その画面のスクリプト、パスキーの JSON の窓口だけです（`asgi.PUBLIC_PATHS`）。ほかの画面をブラウザーで開くとログインの画面へ移し、画像などには 403 を返します。無効にしたユーザーのセッションは使えません。最初の管理者の登録と、パスキーをなくしたときの復旧は、監視するマシンで実行する `users invite`・`users recover` が出す登録用 URL で行います。このマシンで実行できること自体を信頼の根拠にします。`tests/test_gui_routes.py` は、ルートの表にあるすべてのルートで安全対策が効くことを確かめます。インラインスクリプトは CSP で動かないため、JavaScript は `gui/static/` に置き、`gui/routes.py` の `asset` のパスに加えてください。

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
