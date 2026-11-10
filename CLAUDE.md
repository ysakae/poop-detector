# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

犬のトイレを RTSP カメラで監視し、画像差分で排泄の「候補」を通知するローカルツールです。
このリポジトリの開発手順と GitHub Flow は、以下の共通ルールに従ってください。利用者向けの操作手順は `README.md` にあります。

@AGENTS.md

以下では共通ルールと重複させず、コマンドの補足だけを記します。設計の全体像は末尾で読み込む `docs/architecture.md` にあります。

## コマンド

```bash
uv sync --frozen                                   # 依存関係を導入
uv run pytest                                      # 全テスト（約20秒、カメラ・FFmpeg不要のものが中心）
uv run pytest tests/test_core.py                   # 1ファイル
uv run pytest tests/test_core.py -k occlusion      # 名前で絞り込み
uv run ruff check . && uv run ruff format --check . # テストと必ず併せて実行
uv run poop-detector <サブコマンド> --help          # CLI（入口は main.py の parser()）
```

- `run` や `setup-roi` などカメラを使うコマンドは `POOP_RTSP_URL` が必須です。接続情報は実行ディレクトリの `.env` から読み込み、同名の環境変数があればそちらを優先します（`config.load_connection_settings`）。
- テストや手元の確認でカメラ用コマンドを実行すると、実際の `.env` が読まれます。テストでは `monkeypatch.chdir(tmp_path)` などで作業ディレクトリを切り替えてください。
- `label-gui`、`events`、`metrics`、`record` などの記録系コマンドはカメラなしで動きます。
- 動画の保存には FFmpeg の `libx264`、`--codex` には Codex CLI のログインが必要です。

## 設計の全体像

@docs/architecture.md
