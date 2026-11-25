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

1. Ubuntu では、システムの依存関係を `sudo apt install libgl1 ffmpeg` で入れます。`uv sync --frozen` で Python の依存関係を入れます。録画を使う場合は `ffmpeg -hide_banner -encoders` に `libx264` があることを確認します。`uv run poop-detector gui` を起動し、管理者を登録します。
2. 管理 › 初回セットアップで映像の取得を確かめ、きれいな空のトレーを囲んで基準画像と一緒に登録します。GUI を一旦終了します。
3. `command -v uv` で絶対パスを調べ、次のユニットを `~/.config/systemd/user/poop-detector.service` に保存します。例のユーザー名とパスは実際の環境に合わせます。

```ini
[Unit]
Description=犬のトイレ見守り

[Service]
WorkingDirectory=/home/pet/poop-detector
Environment="PATH=/home/pet/.local/bin:/usr/local/bin:/usr/bin:/bin"
UMask=0077
ExecStart=/home/pet/.local/bin/uv run --no-sync poop-detector run --gui --no-browser
Restart=on-failure
RestartSec=15

[Install]
WantedBy=default.target
```

4. シェルの初期化ファイルは systemd から読み込まれません。判定器の CLI を使う場合は、ユニットの `PATH` に実際のインストール先を入れ、同じユーザーと `PATH` で `uv run --no-sync poop-detector diagnose` を実行してから有効にします。`systemctl --user daemon-reload` と `systemctl --user enable --now poop-detector.service` を実行します。ログイン前から起動する場合は `loginctl enable-linger "$USER"` も実行します。
5. `systemctl --user status poop-detector.service` と `journalctl --user -u poop-detector.service -n 30` で状態とログを確認します。家族の端末で映像を開き、テスト通知を受け取れることを確認します。

稼働中の負荷は `systemd-cgtop` でサービスの CPU とメモリを、`free -h` の `available` でホストのメモリ余力を確認できます。常時運用の判断には、通常の監視中に加えて録画や自動判定が動く時間帯も測ります。

更新するときは、先にバックアップを作り、`systemctl --user stop poop-detector.service` で停止してから、作業ディレクトリで `git pull --ff-only` と `uv sync --frozen` を実行します。更新が済んだら `systemctl --user start poop-detector.service` で起動し、映像と通知を確認します。

## GUI から PyPI の安定版へ更新する

固定ポートで `gui` または `run --gui` を起動する Linux・macOS の環境で、`uv` を PATH に追加し、`--update-dir` を指定すると GUI から更新できます。コマンドは同じ作業ディレクトリと起動引数で毎回起動してください。更新先には、データ保存先とは別に、起動ユーザーだけが書き込める専用ディレクトリを各サービスに一つ指定します。

```bash
uv run --no-sync poop-detector --update-dir "$HOME/.local/share/poop-detector/app-updates" run --gui --no-browser
```

「管理 › 稼働状況」に現在と最新の安定版が表示されます。対象の版と再起動を確認して「更新する」を押すと、PyPI から別の仮想環境へダウンロードし、CLI を検査します。準備中も現在の監視は続きます。検査を通過すると録画と書き込みを閉じてから、同じ設定ファイルと保存先で再起動します。画面で適用済みの版を確認できます。Docker で運用する場合はイメージを更新してください。

更新するときはバックアップを作ってください。取得や検査が失敗した場合は、現行版で稼働を続けます。起動に失敗した場合はサービスを停止し、更新ディレクトリの `previous` を `active` にコピーして、元の起動コマンドまたはサービスを起動します。`previous` が空の場合は初回の起動環境に戻ります。旧版で保存データの互換性に問題がある場合は、更新前のバックアップを使って復元してください。

## MacBook などから Linux に移行する

1. 移行先をバックアップ作成元と同じ版か、それより新しい版にそろえます。移行元では普段監視を実行している作業ディレクトリで [バックアップ](records.md#書き出しとバックアップ)を作ります。過去の動画も移す場合は `--clips` を付けます。転送量と展開先の空き容量を見積もり、切り替え時点までの変更も移す場合は移行元の監視と GUI を停止してから最終バックアップを作ります。
2. バックアップと、必要な場合は `.env`、`config/config.yaml` を SSH の `scp` などで移行先の所有者専用ディレクトリに転送します。設定は既存ファイルを退避してから反映し、`data_dir`、`gui.public_url`、`gui.host`、`gui.trusted_proxies` を移行先に合わせます。`.env` はサービスの実行ユーザーが読めるようにし、権限を `0600` にします。
3. 移行先の監視と GUI を止め、作業ディレクトリで `uv run poop-detector restore /path/to/backup.zip` を実行します。復元前のディレクトリはリネームされて残るので、Git 管理外の場所に保管します。
4. 犬検出を使う場合は `uv run poop-detector setup-dog-model` でモデルを取得します。鳴き声の分類を使う場合は `uv sync --frozen --extra audio-model` と `uv run poop-detector setup-vocal-model` を実行します。CLI 判定器は移行先で認証し、`uv run poop-detector diagnose` で確認します。その後 `uv run poop-detector check-camera` で映像を取得できることを確かめます。
5. 移行元の監視を止め、移行先の監視を起動します。復元先の GUI のドメインが変わった場合は、既存の管理者に `uv run poop-detector users recover 登録名またはメールアドレス` で移行先用のパスキーを追加します。映像を開き、必要な端末で Web Push を再購読します。
