# Repository Guidelines

この文書は AI エージェント（Codex、Claude Code）向けの規約です。人間のコントリビューター向けの手順は `CONTRIBUTING.md` にあります。規約を変えるときは、両方の内容をそろえてください。

## プロジェクト構成

- `src/poop_detector/` に Python の実装があります。`main.py` が CLI の入口（引数の解析と振り分け）で、各サブコマンドの処理は `commands/`、監視ループは `monitoring.py` にあります。`camera.py` が映像取得、`detection.py` が変化検知、`events.py` と `clips.py` が記録・録画を担当します。
- `label_gui.py` と `clip_gui.py` がラベル付け画面を提供します。ブラウザー用 JavaScript は `src/poop_detector/static/`、テストは `tests/` にあります。設定例は `config/config.example.yaml`、利用手順は `README.md`、設計の概要は `docs/architecture.md` を参照してください。

## 開発・実行コマンド

Python 3.11 以上が必要です。開発には `.python-version` の 3.12 を使い、依存関係と実行環境は `uv` で管理してください。

- `uv sync --frozen` でロック済みの依存関係を導入します。
- `uv run poop-detector check-camera` で RTSP 接続を確認します。
- `uv run poop-detector run` で監視を起動します。先に ROI と基準画像を登録してください。動画保存には FFmpeg の `libx264` が必要です。
- `uv run poop-detector label-gui` でラベル付けと記録のローカル GUI を起動します。
- `docker build -t poop-detector .` で Linux 用イメージを作成します。

## コード規約とテスト

- Python は4スペースで字下げし、変数・関数・モジュールは `snake_case`、クラスは `PascalCase` とします。Ruff の行長設定は100文字です。ユーザーへの説明とコード内のコメントは日本語で書いてください。
- モジュールと公開クラスには、目的を1行で書いた docstring を付けます（Ruff の `D100`、`D101`）。関数の docstring は、名前と引数だけでは分からない前提や副作用があるときに書き、全関数に機械的に付けることはしません。
- Ruff で除外したルールと理由は `pyproject.toml` に書いてあります。個別に `# noqa` を付けるときは、安全と判断した理由をすぐ上のコメントに書いてください。
- コード変更では `tests/test_*.py` に `test_*` を先に追加し、失敗を確認してから最小限の実装とリファクタリングを行います。人工画像や一時ディレクトリを使い、実カメラや運用データに依存させないでください。複数のファイルで使うフィクスチャは `tests/conftest.py`、人工画像や偽のカメラなどの補助は `tests/support.py` に置きます。
- CI は `uv run pytest --cov` でカバレッジを計測し、`pyproject.toml` の `fail_under` を下回ると失敗します。下限を下げる変更には所有者の承認が必要です。テストを足して実測が上がったら、下限も引き上げてください。
- `uv run pytest` と併せて `uv run ruff check .`、`uv run ruff format --check .`、`uv run mypy` を実行し、指摘に対処してください。
- `src/` の関数には、引数と戻り値の型注釈を付けてください（mypy の `disallow_untyped_defs`）。共通の画像の型は `frames.py` にあります。OpenCV の型スタブと合わずに `# type: ignore` を付けるときは、理由をすぐ上のコメントに書いてください。文書のみの変更は `git diff --check` で確認します。

## GitHub Flow

Codex と Claude Code は以下の手順に従います。

1. 作業前に `git status` を確認し、既存の変更を保全します。`git fetch origin` で既定ブランチを取得し、最新の `origin/main` から `feat/`、`fix/`、`docs/` などの作業ブランチを作成します。
2. 変更を一つの目的に絞り、コミット前に `git status`、`git diff`、`git diff --cached` を確認します。件名は `fix: ROI端の誤通知を抑制` のように Conventional Commits 形式で日本語にします。
3. 作業ブランチを push し、`main` 宛てに Pull Request を作成します。変更理由、検証手順と結果、関連 Issue があればそのリンクを記載します。GUI の変更には人工データで撮ったスクリーンショットを添えます。
4. レビューと設定済みのチェックを確認します。`main` や `master` への直接コミットと直接 push を禁止します。AI が PR をマージする場合は、ユーザーの明示的な指示を得てください。

## ローカルデータと安全性

RTSP URL と通知トピックは `.env` で渡してください。RTSP URL、通知トピック、`.env`、実機設定、室内の画像・動画、`data/` の SQLite などのデータはコミットしないでください。既存データや未コミットの変更を失う恐れがある操作（一括削除、SQLite データの破壊的変更を含む）、Git 履歴の書き換え、リポジトリ外のファイル変更には事前に明示的な承認を得てください。
