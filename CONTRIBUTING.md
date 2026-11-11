# コントリビューションの手引き

> **English:** Issues, pull requests, and commit messages are welcome in either Japanese or English. Please follow Conventional Commits (`fix: ...`, `feat: ...`), add tests first, and never attach real camera footage or data from your home. This is a personal, experimental project maintained on a best-effort basis; please open an issue before starting a large change.

poop-detector に関心を持っていただきありがとうございます。不具合の報告、改善の提案、プルリクエストを歓迎します。

## このプロジェクトについて

個人が自宅の犬のトイレを見守るために作っている、実験的なプロジェクトです。返信や対応は、できる範囲で行います。時間がかかったり、方針に合わない提案はお断りしたりすることがあります。

- 大きな変更（新しい機能、依存関係の追加、設計の変更）は、作業を始める前に Issue で相談してください。
- 小さな修正（誤字、文書の改善、明らかな不具合の修正）は、そのままプルリクエストを送ってください。
- 参加する人は [行動規範](CODE_OF_CONDUCT.md) に従ってください。

## 使える言語

Issue、プルリクエスト、コミットメッセージは、日本語でも英語でも構いません。画面や通知の文面、コード内のコメント、文書は日本語で書いています。

## 映像と個人情報を貼らない

このツールは室内のカメラ映像を扱います。Issue やプルリクエストに次のものを貼らないでください。公開リポジトリでは、誰でも読めます。

- 実際のカメラの画像や動画、`data/` の中身（SQLite のデータベースを含む）
- RTSP の URL（認証情報が含まれます）、ntfy のトピック名、`.env` の内容
- 住所や部屋の様子が分かる情報

不具合を再現する必要がある場合は、NumPy などで作った人工の画像や、人工データで撮ったスクリーンショットを使ってください。セキュリティ上の問題は、内容を Issue に書かず、[SECURITY.md](SECURITY.md) の方法で非公開で報告してください。

## 開発環境の準備

Python 3.11 以上、[uv](https://docs.astral.sh/uv/)、FFmpeg が必要です。macOS と Linux に対応しています。

```bash
uv sync --frozen
```

設計の概要は [docs/architecture.md](docs/architecture.md) にあります。

## 変更の進め方

1. `main` から作業ブランチを作ります。名前は `feat/...`、`fix/...`、`docs/...` などにしてください。
2. **テストを先に書きます。** `tests/test_*.py` に要件を確かめる `test_*` 関数を追加し、失敗することを確認してから実装してください。テストでは人工の画像と `tmp_path` などの一時ディレクトリを使い、実カメラや実データに依存させないでください。
3. 次の4つがすべて通ることを確認します。プルリクエストでは GitHub Actions でも同じ確認を行います。

   ```bash
   uv run pytest --cov
   uv run ruff check .
   uv run ruff format --check .
   uv run mypy
   ```

   `--cov` を付けると、テストが実行しなかった行を一覧し、全体のカバレッジが `pyproject.toml` の `fail_under` を下回ると失敗します。CI でも同じ下限で確認します。一部のテストだけを実行するときは `--cov` を付けないでください（下限を下回って失敗します）。

   文書だけを変える場合は `git diff --check` で確認してください。

   テストで共通に使うフィクスチャは `tests/conftest.py` に、人工画像や偽のカメラなどの補助は `tests/support.py` にあります。

4. 1つのプルリクエストは1つの目的に絞ってください。

### コードの書き方

- Python は4スペースで字下げし、変数・関数・モジュールは `snake_case`、クラスは `PascalCase` にします。行の長さは100文字までです（Ruff の設定）。
- `src/` の関数には、引数と戻り値の型注釈を付けてください。`uv run mypy` で確かめます。画像の型は `src/poop_detector/frames.py` にあります。
- モジュールと公開クラスには、目的を1行で書いた docstring を付けてください。関数の docstring は、名前と引数だけでは分からない前提や副作用があるときに書きます。
- Ruff の指摘を `# noqa` で抑えるときは、安全と判断した理由をすぐ上のコメントに書いてください。除外しているルールとその理由は `pyproject.toml` にあります。
- コード内のコメントは日本語で書いてください。英語で書いた場合は、レビューで日本語にすることがあります。
- 認証情報を含む RTSP URL がログやエラーメッセージに出ないようにしてください。外部ライブラリの例外メッセージをそのまま表示しないでください（[docs/architecture.md](docs/architecture.md) の「エラーの伝え方と、認証情報を出さない仕組み」）。

### コミットメッセージ

[Conventional Commits](https://www.conventionalcommits.org/ja/v1.0.0/) の形式で書いてください。

```text
fix: ROI端の誤通知を抑制
feat: add an option to change the ntfy server
```

主な種類は `feat`（機能の追加）、`fix`（不具合の修正）、`docs`（文書のみ）、`test`（テストのみ）、`refactor`（動作を変えない整理）、`ci`、`chore` です。

## プルリクエスト

テンプレートに沿って、次のことを書いてください。

- 何を変えたか、なぜ変えたか
- どう確認したか（実行したコマンドと結果）
- 関連する Issue（`Closes #番号` など）
- 画面を変えた場合は、人工データで撮ったスクリーンショット

メンテナーがレビューし、CI がすべて通ってからマージします。

## Issue

不具合の報告と機能の提案には、テンプレートを用意しています。不具合の報告では、OS、Python のバージョン、カメラの種類、起動したコマンドとオプションを書いてもらえると、原因を調べやすくなります。
