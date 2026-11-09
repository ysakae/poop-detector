# Repository Guidelines

## プロジェクト構成

- `src/poop_detector/` に Python の実装があります。`main.py` が CLI の入口で、`camera.py` が映像取得、`detection.py` が変化検知、`events.py` と `clips.py` が記録・録画を担当します。
- `label_gui.py` と `clip_gui.py` がラベル付け画面を提供します。ブラウザー用 JavaScript は `src/poop_detector/static/`、テストは `tests/` にあります。設定例は `config/config.example.yaml`、操作手順は `README.md` を参照してください。

## 開発・実行コマンド

Python 3.11 以上が必要です。開発時は `.python-version` の 3.12 を使い、依存関係と Python コマンドは `uv` で管理してください。

- `uv sync --frozen` — ロック済みの依存関係を導入します。
- `uv run poop-detector check-camera` — RTSP 接続を確認します。
- `uv run poop-detector run` — ROI と基準画像を登録した環境で監視を起動します。動画保存には FFmpeg の `libx264` が必要です。
- `uv run poop-detector label-gui` — ラベル付けと記録のローカル GUI を起動します。
- `docker build -t poop-detector .` — Linux での実行用イメージを作成します。

## コード規約とテスト

- Python は4スペースで字下げし、変数・関数・モジュールには `snake_case`、クラスには `PascalCase` を使ってください。Ruff の行長設定は100文字です。コード内のコメントは日本語で書いてください。
- 実装前に `tests/test_*.py` へ要件を検証する `test_*` 関数を追加し、失敗を確認してから最小限の修正を加えてください。人工画像や一時ディレクトリを使い、実カメラや保存済みデータに依存させないでください。数値のカバレッジ基準は設けていません。
- `uv run pytest` でテストを実行し、併せて `uv run ruff check .` と `uv run ruff format --check .` で静的解析と書式を確認してください。

## コミットとプルリクエスト

`feat/`、`fix/`、`docs/` などの作業ブランチを使い、`main` や `master` に直接コミットしないでください。コミット前に `git status` と `git diff`（ステージ後は `git diff --cached`）を確認し、`fix: ROI端の誤通知を抑制` のように Conventional Commits 形式で日本語の件名を書いてください。PR には変更理由、確認手順と結果、関連 Issue があればそのリンクを記載してください。GUI の変更には人工データで撮ったスクリーンショットを添えてください。

## ローカルデータと認証情報

RTSP URL は `POOP_RTSP_URL` で渡してください。認証情報、`.env`、実機設定、室内の画像・動画、`data/` の SQLite はコミットしないでください。一括削除、SQLite データの破壊的変更、Git 履歴の書き換え、リポジトリ外のファイル変更には事前に明示的な承認を得てください。
