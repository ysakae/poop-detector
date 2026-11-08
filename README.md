# ここちゃんのトイレ変化検知（poop-detector）

MacBook向けのMVPです。空のトイレとの差分が一定時間残った場合に「ウンチの可能性」を通知します。
**排泄物を識別するAIや排便姿勢予測は未実装です。食糞を確実に防ぐシステムとしては未検証です。**

## 準備

Python 3.11以上、uv、FFmpegが必要です。開発検証はApple Silicon / macOS 27.2 / Python 3.12.13です。

```bash
cd ~/dev/poop-detector
uv sync --frozen
cp config/config.example.yaml config/config.yaml
```

`config/config.yaml`は未作成でも既定値で起動します。既に設定がある場合は上書きせず編集してください。
`.env.example`は参考用で、自動読み込みしません。

## カメラの最小設定

2026-11-01時点、192.168.11.20 の554/TCPへの接続は成功しています。
ユーザー確認ではRTSPは有効です。URL未設定のため、認証・実機フレーム取得は未確認です。

1. Eufyアプリで対象カメラの「設定 → 一般 → ストレージ → NAS（RTSP）」を開き、表示されたURLを確認します。
2. アプリで選択できる場合は連続配信を選びます。動体検知時だけの配信は常時監視には適しません。
3. カメラをトイレに固定します。自動追尾・パン・チルト・ズームで画角が変わると基準が無効になります。
4. 同じLANで使用し、ルーターのRTSPポートを外部に公開しないでください。

設定画面の根拠：[Anker Japan公式FAQ](https://www.ankerjapan.com/blogs/faq/eufy-indoorcam-2k-pan-tilt)。
連続配信について：[Eufy RTSP設定ガイド](https://service.eufy.com/article-description/Device-NAS-RTSP-Configuration-Guide)。
アプリの版によって表示が異なる場合は、実際の画面に従ってください。

URLは環境変数で渡します。macOSのzshでは次の入力により、URLをシェル履歴へ直接記録せずに設定できます。

```zsh
read -rs 'POOP_RTSP_URL?EufyのRTSP URLを入力: '
export POOP_RTSP_URL
printf '\n'
uv run poop-detector check-camera
```

認証情報をチャットやGitに貼る必要はありません。起動済みアプリは別のターミナルで後から設定した環境変数を引き継がないため、コマンドは設定した同じターミナルで実行してください。

## 初回設定と監視

犬や物体がトイレにない状態で実行してください。ROIはトイレ全体を囲み、Enterで確定します。
GUI操作はMacホスト上で行います。

```bash
uv run poop-detector setup-roi
uv run poop-detector capture-baseline
uv run poop-detector test-notification
uv run poop-detector run
```

Ctrl+Cで停止します。通知テストの成功はOSへの要求成功を示します。通知表示はmacOSの通知設定・集中モードも確認してください。
ROI・基準を更新するときは明示的に指定します。基準は自動更新しません。

```bash
uv run poop-detector setup-roi --replace
uv run poop-detector capture-baseline --replace
```

ROIは正規化座標で保存するため解像度変更に追従します。ただし基準画像は再登録が必要です。
ROIを変更した場合も基準を再登録してください。異なるROI・解像度の基準を監視へ流用しません。

## 判定の仕組みと設定

OpenCVでグレースケール、ぼかし、差分、二値化、モルフォロジー、輪郭面積・縦横比の処理を行います。
ROI面積の18%以上が変わると犬などの遮蔽とみなし、広範囲の明るさの変化も保留します。
面積が18%未満でも、最小輪郭面積以上の差分がROIの端に接していれば遮蔽として保留します。
端の余白はROIの短辺の1%（最低1ピクセル）です。端から入り込む足・体の一部による誤通知を抑えます。
この間は内側の候補も通知せず、遮蔽がなくなってから継続確認をやり直します。
端に接した排泄物も保留されるため、ROIの端がトイレの検知したい範囲を切らないように設定してください。
小さな変化が既定3秒続くとSUSPECTEDとして1回通知します。scoreは差分画素率で、排便の確率ではありません。

状態はEMPTY / OCCUPIED / VERIFYING / SUSPECTED / ACKNOWLEDGEDです。
画像差分からCONFIRMEDにはしません。切断や遮蔽では継続確認のタイマーをリセットします。
通知後は空の基準状態が3秒続くまで同じ物体の通知を抑制し、確認済み操作だけでは再通知を許可しません。
通知抑止と空への復帰を保存し、再起動時にも現在有効な抑止状態のみ復元します。

取得専用スレッドが映像を継続して読み、最新の未処理フレーム1枚だけを保持します。
設定のfpsは**検出処理のFPS**です。RTSP受信・デコード自体はカメラの配信FPSで行い、映像の滞留を防ぎます。
接続・読込タイムアウトは既定5秒、再接続待機は2秒です。30秒ごとにCPU、RSSメモリー、処理時間をログ出力します。

`config/config.yaml`でfps、継続秒数、閾値、面積、遮蔽率、通知有効・無効、保存先を変更できます。
`min_area`はROI内のピクセル面積なので、解像度と設置位置に応じて調整してください。

## イベントと評価

保存先の既定値はプロジェクト直下の`data/`です。基準画像、ROI、イベント画像、SQLiteを保存します。
常時録画・動画クリップ保存・自動削除は実装していません。削除せず記録を蓄積するため、ディスク容量を確認してください。
GitとDockerビルドには認証情報・室内画像・データベースを含めません。

```bash
uv run poop-detector events
uv run poop-detector metrics
```

一覧のイベントIDで実際の結果を記録します。次のEVENT_IDは表示された実際のIDに置き換えてください。

```bash
uv run poop-detector feedback EVENT_ID true --start '2026-11-01T12:00:00+09:00' --end '2026-11-01T12:00:10+09:00'
uv run poop-detector feedback EVENT_ID false
uv run poop-detector acknowledge EVENT_ID
uv run poop-detector missed --start '2026-11-01T13:00:00+09:00' --end '2026-11-01T13:00:10+09:00'
```

フィードバックは実際に排便だったか、acknowledgeは飼い主が確認したかを記録する別の項目です。
UTCでイベント時刻・検出開始・通知時刻・処理時間を記録します。誤検知数はUTC日別です。
Precisionはラベル済み通知のTP/(TP+FP)、RecallはTP/(TP+手動登録した見逃し)です。
分母が0ならnullを返します。未ラベル数と見逃し数、開始・完了から通知までの秒数リストも出力します。
通知無効・失敗時には通知時刻を記録しません。全件の確認と見逃しの登録がなければ、指標は実際の精度を表しません。

## 検証

```bash
uv run pytest
uv run ruff check .
uv run ruff format --check .
```

人工画像・人工動画による差分検知、照明変動と遮蔽、状態遷移、重複抑制、ROI保存・拡大、基準との対応、
SQLite記録、精度・遅延集計、切断後の再接続、最新フレーム、CLI入力、通知処理を検証しています。
実測値は`docs/validation.json`を参照してください。人工画像の処理負荷で、RTSPデコード負荷は含みません。
実際の排便映像がないため、実際のPrecision・Recall・通知遅延は未測定です。
通知要求は実機で成功しましたが、通知センターでの表示はユーザー確認が必要です。

## Docker / Linux

Dockerfileを用意していますが、このMacにはDocker実行環境がなくビルド・実行は未検証です。
ROI設定・基準登録はMac側で先に済ませ、同じ解像度・画角でLinuxへコピーしてください。
macOS通知はLinuxでは無効になります。Telegramなどの通知先は未実装です。
設定例をコピーする前に既存設定を確認し、Docker内ではnotificationsをfalseにしてください。

```bash
docker build -t poop-detector .
docker run --rm --env POOP_RTSP_URL -v "$PWD/data:/app/data" -v "$PWD/config:/app/config:ro" poop-detector
```

Docker内からカメラのLANに到達できる必要があります。クラウドへの移行にはVPN等が必要で、RTSPの外部公開はしません。
GUIはコンテナ内で使用しません。コンテナのUIDとボリュームの書き込み権限も実環境で確認してください。

## 制限と次の改善

犬を直接検出していません。ROI内の小さな犬・影・シートのずれ、動かない犬、照明変化で誤検知・見逃しが起こりえます。
特に犬の全身がROI内に収まり、差分面積も遮蔽率未満の場合は、端の遮蔽対策でも除外できません。
犬がトイレを離れて変化が安定するまで通知できず、食糞までに間に合う保証はありません。
既定3秒の確認時間に、最大1フレーム間隔・通信・通知処理の遅延が加わります。

YOLO等による犬の検出は、一般モデルで犬と排泄物を同様に識別できるとは限らず、モデル・ライセンス・CPU負荷を評価してから導入します。
姿勢推定・VLM・排泄物専用モデル・事前通知・確定通知・Telegramは将来の開発対象です。
まず実機URLを設定してROIと空の基準を登録し、実映像のラベルを蓄積することが次のステップです。
