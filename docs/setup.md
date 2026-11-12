# 初期設定

カメラの準備から、監視領域（ROI）と空のトイレの基準画像の登録までを説明します。最短の手順は [README のクイックスタート](../README.md#クイックスタート) にあります。

## 設定ファイル

| ファイル | 内容 |
|---|---|
| `.env` | カメラの RTSP URL と ntfy のトピック名。`.env.example` をコピーして作ります |
| `config/config.yaml` | 検知の閾値や動画の設定。未作成でも既定値で起動します。項目は `config/config.example.yaml` を参照してください |

コマンドを実行したディレクトリの `.env` を起動時に読み込みます。同じ名前の環境変数がすでにある場合は、空の値も含めて環境変数を優先します。値を変更したら監視を再起動してください。

すでにターミナルで RTSP URL や通知トピックを `export` している場合は、そのターミナルから次を実行すると `.env` に保存できます。空の値は対象外です。既存の値を更新する場合だけ `--replace` を付けてください。

```bash
uv run python scripts/export-dotenv.py
```

## 犬の名前とタイムゾーン

`config/config.yaml` で次の2つを設定できます。どちらも未設定で動きます。

| 項目 | 内容 | 未設定のとき |
|---|---|---|
| `pet_name` | 通知と画面に表示する犬の名前（32文字まで） | 「トイレに変化を検出しました」のように名前を出さない文面 |
| `timezone` | 日別集計と画面の時刻のタイムゾーン（`Asia/Tokyo` などの IANA 名） | マシンのタイムゾーン |

```yaml
pet_name: ポチ
timezone: Asia/Tokyo
```

データベースには時刻を UTC で保存しているため、`timezone` を変えると、これまでの記録も新しいタイムゾーンで表示・集計されます。ただし、行動マップの日別集計は記録したときの日付で保存しているので、過去の分の日付の区切りは変わりません。

## 使う機能を設定ファイルで有効にする

動画の自動判定や見守り通知などの機能は、`run` のフラグのほかに、`config/config.yaml` の `features` セクションでも有効にできます。普段使う機能を書いておけば、起動は `uv run poop-detector run` だけで済みます。

```yaml
features:
  video_review: true   # --video-review と同じ（clips.enabled が必要。以前の名前 codex も読める）
  early_alert: true    # --early-alert と同じ
  track_tray: true     # --track-tray と同じ（capture-baseline にも効く）
  activity_map: false  # --activity-map と同じ（犬の検出を自動で有効にする）
  detect_outside: false  # --detect-outside と同じ（video_review が必要）
dog:
  enabled: true        # --dog と同じ
```

- フラグは、設定で `false` の機能も、その起動のときだけ有効にします（フラグで無効にはできません）。
- 必要な条件が足りないときは、起動時に設定の項目名とフラグの両方を示して止まります。
- 起動時のログに「有効な機能」を出します。設定ファイルから読んだ値は、次のコマンドで確認できます。

```bash
uv run poop-detector show-config
```

設定ファイルは `camera`・`detection`・`notifications`・`clips`・`dog`・`features` のセクションに分かれています。項目と許容範囲は `config/config.example.yaml` にあります。範囲外の値や存在しない項目は、その項目名と許容範囲を表示して止まります。

管理者は GUI の「管理 › 設定」でも、項目を確かめて変更できます（変更前に差分を確認し、コメントは残ります。外からの公開に関わる項目と `data_dir` は設定ファイルで変えます）。詳しくは [画面の構成](screens.md#管理--設定) を参照してください。

0.1.0 の開発中の、セクションのない書き方（`clip_enabled: true` など）も読み込めます。その場合は、新しい書き方（`clips.enabled`）を警告で案内します。

## カメラの設定

RTSP で映像を配信できるカメラが必要です。以下は Eufy のカメラでの設定例です。

1. Eufy アプリで対象カメラの「設定 → 一般 → ストレージ → NAS（RTSP）」を開き、表示された URL を確認します。
2. アプリで選択できる場合は連続配信を選びます。動体検知時だけの配信は常時監視には適しません。
3. カメラをトイレに固定します。自動追尾・パン・チルト・ズームで画角が変わると基準が無効になります。
4. 同じ LAN で使用し、ルーターの RTSP ポートを外部に公開しないでください。

設定画面の根拠：[Anker Japan 公式 FAQ](https://www.ankerjapan.com/blogs/faq/eufy-indoorcam-2k-pan-tilt)。
連続配信について：[Eufy RTSP 設定ガイド](https://service.eufy.com/article-description/Device-NAS-RTSP-Configuration-Guide)。
アプリの版によって表示が異なる場合は、実際の画面に従ってください。

`.env` の `POOP_RTSP_URL` に URL を入力し、リポジトリのディレクトリで接続を確認します。

```bash
uv run poop-detector check-camera
```

## 監視領域と基準画像の登録

犬や物体がトイレにない状態で実行してください。ROI はトイレ全体を囲み、Enter で確定します。ROI の登録には画面のあるマシンが必要です。

```bash
uv run poop-detector setup-roi
uv run poop-detector capture-baseline
```

ROI・基準を更新するときは明示的に指定します。基準は自動更新しません。

```bash
uv run poop-detector setup-roi --replace
uv run poop-detector capture-baseline --replace
```

### 多角形の監視領域

斜めに映るトイレや複雑な形の監視領域は、次のコマンドで多角形の ROI を設定できます。
画面上で**外周に沿って時計回り、または反時計回り**に3点以上をクリックし、Enter で確定してください。
六角形なら6点、八角形なら8点を指定します。最後に最初の点をもう一度クリックする必要はありません。
右クリックまたは Backspace で1点戻り、R で最初から、Esc またはウィンドウを閉じると取消です。
辺が交差・重複している場合は確定せず、R でやり直せます。くぼみのある多角形も指定できます。

```bash
uv run poop-detector setup-roi --polygon --replace
uv run poop-detector capture-baseline --replace
uv run poop-detector run
```

トイレ面を囲み、餌皿や周囲の床をできるだけ含めないでください。
多角形の内側だけを監視し、外側は差分の面積や照明変動の計算から除外します。
斜辺やくぼみも遮蔽判定の境界として扱います。多角形では射影補正せず、元の映像の形を保ちます。
頂点は正規化して保存し、次回から自動で使用します。
従来の矩形 ROI と、旧版で保存した四隅の射影補正 ROI もそのまま読み込めます。
旧版の四隅指定から新しい多角形方式へ設定し直した場合は、同じ4点でも基準画像を再登録してください。
既存 ROI・基準画像はコマンドを実行して確定するまで変更しません。

### 解像度や ROI を変えたとき

ROI は正規化座標で保存するため解像度変更に追従します。ただし基準画像は再登録が必要です。
ROI を変更した場合も基準を再登録してください。異なる ROI・解像度の基準を監視へ流用しません。

新しいシートの位置や柄が変わった場合も、監視を停止して空の状態で `capture-baseline --replace` を実行し、再起動してください。

## 画面のない環境で使う

範囲を指定する `setup-roi`（`--polygon`、`--outside` を含む）と `setup-activity-map` はウィンドウを開くため、画面が必要です。画面のない Linux（サーバーや Docker）で実行すると、案内を表示して終了します。`capture-baseline` はウィンドウを開かないので、画面がなくても実行できます。

画面のない環境では、GUI の「管理 › 範囲の登録」を使うのが簡単です。ほかの端末のブラウザーで GUI を開き、映像の上に範囲を描いて登録できます（[画面の構成](screens.md#管理--範囲の登録)）。CLI で準備する場合は、次の手順です。

1. 画面のあるマシン（Mac など）に同じリポジトリを用意し、同じカメラにつないで `setup-roi` を実行します。行動マップを使うなら `setup-activity-map` も実行します。
2. できた `data/roi.json`（トレー外の検知を使うなら `data/outside-roi.json`、行動マップを使うなら `data/activity-layout.json` と `data/activity-*.jpg` も）を、監視するマシンの `data/` へコピーします。
3. 監視するマシンで、トイレが空の状態で `capture-baseline` を実行し、`run` で監視を始めます。

ROI は正規化座標で保存するので、カメラの解像度が変わっても使えます。ただし画角は登録したときと同じにしてください。

## カメラやトレーの小さなずれに追従する

トレーの縁を目印に、映像を登録時の座標に戻してから判定します。
現在の監視範囲がトレーの縁を含む状態で、トイレを空にして監視を停止し、初回に基準を登録します。
位置がすでにずれている場合は、先に `setup-roi --polygon --replace` で範囲を合わせてください。

```bash
uv run poop-detector capture-baseline --replace --track-tray
uv run poop-detector run --dog --video-review --early-alert --track-tray
```

縁が隠れたり、監視範囲が画面から外れたりすると、その間は見守り通知と排泄判定を保留します。
位置が確認できると自動で再開します。状態は GUI のライブ映像の下に表示されます。
監視範囲が画面の端に接していると、丸め誤差や数ピクセルの揺れで範囲がわずかに画面からはみ出します。
縁を照合できていれば、追従用の縮小画像（長辺640px）で3ピクセルまでのはみ出しは追従を続けます。
画面の外にあたる部分には実際の映像がないため、登録時の映像で埋め、新しい跡として扱いません。
それを超えてはみ出す場合は保留します。端に余裕を持たせて範囲を描くと、保留が起きにくくなります。
保留の理由（縁を照合できない、変形が大きい、画面の外へ出た）は `--verbose` のログに出ます。
赤外線の白黒映像では、明暗が逆転しても比較できる輪郭を使って追従します。
夜間の自動判定でも、新しい便、尿の流れ、または濡れ跡が画像で確認できた場合に候補を通知します。
保存済み動画を判定するときは、同じ基準で撮影した動画を `video-review --track-tray` で処理できます。

### 監視を再起動せずにトレーを交換する

初回登録を済ませて監視を起動した状態で、GUI の「管理」からトレー交換を開始できます。ライブ映像で検知範囲を確認し、必要ならトレーの外周に沿って多角形を描き、空のトレーを確認して反映します。画面には監視・カメラ接続・追従と交換の状態が表示されます。

端末から操作する場合は、次のコマンドで交換作業を開始します。

```bash
uv run poop-detector tray-registration begin
```

「監視を一時保留しました」と表示されたらトレーを交換し、犬が乗っていない、汚れのない状態にしてください。カメラのライブ映像は交換中も確認できます。

```bash
uv run poop-detector tray-registration apply --polygon
uv run poop-detector tray-registration status
```

`--polygon` はライブ映像から新しい範囲を指定します。画面のない環境では別途作成した ROI ファイルを `--roi PATH` で渡せます。範囲を変えない場合は、オプションを省略すると現在の ROI を使います。

新しい基準は監視側が続くフレームで確認し、ROI とまとめて反映します。失敗した場合は監視が保留されたままなので、空のトレーと範囲を確認して `apply` をやり直してください。取り消すときはトレーを直前の登録に合う状態に戻してから `tray-registration cancel` を実行します。過去の録画と記録はそのまま参照できます。
