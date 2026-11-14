# Docker / Linux

`Dockerfile` からイメージを作れます。GitHub Actions（`.github/workflows/ci.yml`）で、イメージのビルドと、コンテナ内での動画の作成・GUI・CLI の動作を人工データで確認しています。実際のカメラをつないだ監視は未確認です。

ROI の設定には画面が必要なため、画面のあるマシンで登録した `data/roi.json` をコピーしてから、コンテナ内で `capture-baseline` を実行してください（[画面のない環境で使う](setup.md#画面のない環境で使う)）。
Linux で通知する場合は `.env` に `POOP_NTFY_TOPIC` を設定します。macOS の通知は Linux では使えません。通知を使わない場合は `notifications.enabled: false` にしてください。

```bash
docker build -t poop-detector .
docker run --rm -v "$PWD/.env:/app/.env:ro" -v "$PWD/data:/app/data" -v "$PWD/config:/app/config:ro" poop-detector
```

- Docker 内からカメラの LAN に到達できる必要があります。クラウドへ移す場合は VPN などが必要で、RTSP のポートを外部に公開してはいけません。
- `.dockerignore` で `.env`、`data/`、`config/config.yaml`、`tests/` などをビルドから除外しているため、認証情報・室内の画像・データベース・個人の設定はイメージに含まれません。
- コンテナのタイムゾーンは通常 UTC です。日別の集計を自分の地域の日付で区切るには、`config/config.yaml` の `timezone`（例: `Asia/Tokyo`）を設定してください。
- GUI をコンテナ内で動かす場合は、[外から GUI を開く](remote-access.md#docker-の中で動かす場合)の設定（`gui.host` と `gui.trusted_proxies`）が必要です。
- コンテナの UID とボリュームの書き込み権限は、実際の環境で確認してください。
- イメージには GPL の構成の FFmpeg が入ります。ビルドしたイメージを他の人に配布する場合は、[ライセンスの条件](licenses.md#docker-イメージ)を満たしてください。

## インストール先での動作確認

カメラをつなぐ前に、インストールした環境で動画の作成、タイムゾーン、GUI、CLI が動くかを人工データで確かめられます。

```bash
uv run python scripts/smoke-check.py
docker run --rm --entrypoint python -v "$PWD/scripts:/scripts:ro" poop-detector /scripts/smoke-check.py
```

## LinuxミニPCで常時運用する

Ubuntu 24.04で、監視専用の一般ユーザーから `uv` と FFmpeg を使う構成を例にします。既存の作業ディレクトリに `.env`、`config/config.yaml`、`data/` をまとめます。GUI をスマホから開くときは、先に [HTTPS とパスキー](remote-access.md) を設定してください。

1. `uv sync --frozen` で依存関係を入れます。`uv run poop-detector gui` を起動し、管理者を登録します。
2. 管理 › 初回セットアップで映像の取得を確かめ、きれいな空のトレーを囲んで基準画像と一緒に登録します。GUI を一旦終了します。
3. `command -v uv` で絶対パスを調べ、次のユニットを `~/.config/systemd/user/poop-detector.service` に保存します。例の2つのパスは実際の場所に置き換えます。

```ini
[Unit]
Description=犬のトイレ見守り

[Service]
WorkingDirectory=/home/pet/poop-detector
ExecStart=/home/pet/.local/bin/uv run --no-sync poop-detector run --gui --no-browser
Restart=on-failure
RestartSec=5

[Install]
WantedBy=default.target
```

4. `systemctl --user daemon-reload` と `systemctl --user enable --now poop-detector.service` を実行します。ログイン前から起動する場合は `loginctl enable-linger "$USER"` も実行します。
5. `systemctl --user status poop-detector.service` と `journalctl --user -u poop-detector.service -n 30` で状態とログを確認します。家族の端末で映像を開き、テスト通知を受け取れることを確認します。

更新するときは、先にバックアップを作り、`systemctl --user stop poop-detector.service` で停止してから、作業ディレクトリで `git pull --ff-only` と `uv sync --frozen` を実行します。更新が済んだら `systemctl --user start poop-detector.service` で起動し、映像と通知を確認します。

別端末へ移すときは [バックアップと復元](records.md#書き出しとバックアップ) に沿って、移行先の監視とGUIが停止している状態で復元します。`.env` と動作設定は別途用意します。復元後は管理者のパスキー登録と各端末での通知購読を確認してください。
