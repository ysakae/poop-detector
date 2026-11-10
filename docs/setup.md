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

## カメラやトレーの小さなずれに追従する

トレーの縁を目印に、映像を登録時の座標に戻してから判定します。
現在の監視範囲がトレーの縁を含む状態で、トイレを空にして監視を停止し、初回に基準を登録します。
位置がすでにずれている場合は、先に `setup-roi --polygon --replace` で範囲を合わせてください。

```bash
uv run poop-detector capture-baseline --replace --track-tray
uv run poop-detector run --dog --codex --early-alert --track-tray
```

縁が隠れたり、監視範囲が画面から外れたりすると、その間は見守り通知と排泄判定を保留します。
位置が確認できると自動で再開します。状態はラベル GUI のライブ映像の下に表示されます。
赤外線の白黒映像では、明暗が逆転しても比較できる輪郭を使って追従します。
夜間の Codex 判定でも、新しい便、尿の流れ、または濡れ跡が画像で確認できた場合に候補を通知します。
保存済み動画を判定するときは、同じ基準で撮影した動画を `codex-review --track-tray` で処理できます。
