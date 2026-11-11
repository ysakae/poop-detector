# Docker / Linux

`Dockerfile` からイメージを作れます。GitHub Actions（`.github/workflows/ci.yml`）で、イメージのビルドと、コンテナ内での動画の作成・ラベル GUI・CLI の動作を人工データで確認しています。実際のカメラをつないだ監視は未確認です。

ROI の設定には画面が必要なため、画面のあるマシンで登録した `data/roi.json` をコピーしてから、コンテナ内で `capture-baseline` を実行してください（[画面のない環境で使う](setup.md#画面のない環境で使う)）。
Linux で通知する場合は `.env` に `POOP_NTFY_TOPIC` を設定します。macOS の通知は Linux では使えません。通知を使わない場合は `notifications.enabled: false` にしてください。

```bash
docker build -t poop-detector .
docker run --rm -v "$PWD/.env:/app/.env:ro" -v "$PWD/data:/app/data" -v "$PWD/config:/app/config:ro" poop-detector
```

- Docker 内からカメラの LAN に到達できる必要があります。クラウドへ移す場合は VPN などが必要で、RTSP のポートを外部に公開してはいけません。
- `.dockerignore` で `.env`、`data/`、`config/config.yaml`、`tests/` などをビルドから除外しているため、認証情報・室内の画像・データベース・個人の設定はイメージに含まれません。
- コンテナのタイムゾーンは通常 UTC です。日別の集計を自分の地域の日付で区切るには、`config/config.yaml` の `timezone`（例: `Asia/Tokyo`）を設定してください。
- ラベル GUI はコンテナ内で使用しません。
- コンテナの UID とボリュームの書き込み権限は、実際の環境で確認してください。
- イメージには GPL の構成の FFmpeg が入ります。ビルドしたイメージを他の人に配布する場合は、[ライセンスの条件](licenses.md#docker-イメージ)を満たしてください。

## インストール先での動作確認

カメラをつなぐ前に、インストールした環境で動画の作成、タイムゾーン、ラベル GUI、CLI が動くかを人工データで確かめられます。

```bash
uv run python scripts/smoke-check.py
docker run --rm --entrypoint python -v "$PWD/scripts:/scripts:ro" poop-detector /scripts/smoke-check.py
```
