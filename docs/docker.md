# Docker / Linux

`Dockerfile` を用意していますが、Docker でのビルドと実行は未検証です。

ROI の設定と基準の登録には画面が必要なため、画面のあるマシンで先に済ませ、同じ解像度・画角のまま `data/` を Linux へコピーしてください。
Linux で通知する場合は `.env` に `POOP_NTFY_TOPIC` を設定します。macOS の通知は Linux では使えません。通知を使わない場合は `notifications: false` にしてください。

```bash
docker build -t poop-detector .
docker run --rm -v "$PWD/.env:/app/.env:ro" -v "$PWD/data:/app/data" -v "$PWD/config:/app/config:ro" poop-detector
```

- Docker 内からカメラの LAN に到達できる必要があります。クラウドへ移す場合は VPN などが必要で、RTSP のポートを外部に公開してはいけません。
- `.dockerignore` で `.env`、`data/`、`tests/` などをビルドから除外しているため、認証情報・室内の画像・データベースはイメージに含まれません。
- コンテナのタイムゾーンは通常 UTC です。日別の集計を自分の地域の日付で区切るには、`config/config.yaml` の `timezone`（例: `Asia/Tokyo`）を設定してください。
- ラベル GUI はコンテナ内で使用しません。
- コンテナの UID とボリュームの書き込み権限は、実際の環境で確認してください。
