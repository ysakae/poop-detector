# リリースの手順

メンテナー向けの手順です。バージョンは [セマンティック バージョニング](https://semver.org/lang/ja/) に従います。

## バージョンと互換性

- メジャーは、安定機能の既存の CLI 引数・コマンド、設定、保存データとの互換性を壊すときに上げます。変更点と移行・バックアップの手順をリリースノートに書きます。
- マイナーは、互換性を保った機能追加や非推奨化で上げます。非推奨にした CLI や設定は次のメジャーまで受け付け、代替手順を案内します。
- パッチは、互換性を保った不具合修正やセキュリティ修正で上げます。
- 同じメジャーの新しい版は既存の CLI と設定を受け付け、保存済みのデータを読み込めるようにします。必要な SQLite の移行は起動時に実施します。旧版に戻す場合は、更新前に作ったバックアップを使います。
- README で実験的と明記した機能は変更されることがあります。利用者に影響する変更は変更履歴に記載します。

## 最初に一度だけ行う設定

PyPI への公開は、GitHub Actions から [Trusted Publishing](https://docs.pypi.org/trusted-publishers/) で行います。API トークンは使いません。

1. PyPI にログインし、「Your account」→「Publishing」で、まだ存在しないプロジェクトの公開者（pending publisher）を追加します。

   | 項目 | 値 |
   |---|---|
   | PyPI Project Name | `poop-detector` |
   | Owner | `ysakae` |
   | Repository name | `poop-detector` |
   | Workflow name | `release.yml` |
   | Environment name | `pypi` |

2. GitHub のリポジトリの「Settings」→「Environments」で、`pypi` という環境を作ります。リポジトリを公開した後は、「Required reviewers」に自分を加えると、公開の前に承認を求められるようになります。

## リリースのたびに行うこと

1. `origin/main` から作業ブランチを作ります。`uv version 新しいバージョン` で `pyproject.toml` と `uv.lock` を更新します。`CHANGELOG.md` の `[Unreleased]` にある内容を新しい版の節に移し、日付と末尾の比較リンクを更新します。
2. `uv sync --frozen`、`uv run pytest --cov`、Ruff の検査と書式確認、`uv run mypy` を実行します。`uv build` と `uvx twine check --strict dist/*` で配布物を確認します。プルリクエストを作り、CI が通ってからマージします。
3. GitHub Releases で、CI が通った `main` のコミットを選んでリリースを作ります。タグはパッケージのバージョンに `v` を付けた値（初回は `v1.0.0`）にします。リリースノートには `CHANGELOG.md` の該当する節を使います。
4. 公開前に `pypi` 環境と PyPI の公開者の設定を確認します。「Publish release」を押すと `release.yml` が sdist と wheel をビルド・検査し、PyPI に公開します。承認者を設定した場合は Actions の画面で承認します。
5. ワークフローの完了と [PyPI のページ](https://pypi.org/project/poop-detector/) の版を確認します。最後のジョブは PyPI からその版を新しい環境にインストールし、CLI の起動とバージョンを確かめます。手元でも `uvx --from poop-detector==1.0.0 poop-detector --version` で確認できます。
6. ビルドや公開で止まったら Actions の失敗したステップを確認します。PyPI 側に配布物がなければ、設定を直して失敗したジョブを再実行できます。すでに公開された配布物の修正は、版を上げて行います。インストール確認だけが失敗した場合は確認ジョブを再実行できます。

## 注意

- 一度 PyPI に公開したバージョンは、同じ番号で上書きできません。誤りがあれば、番号を上げて公開し直してください。
- PyPI のプロジェクトページには README がそのまま表示されます。README の画像や `docs/` へのリンクは相対パスなので、PyPI では表示されません。詳しい説明は GitHub のリポジトリを見てもらうことになります。
- 手元で確認するときは `uv build` と `uvx twine check --strict dist/*` を実行します。CI でも同じ確認をしています。
