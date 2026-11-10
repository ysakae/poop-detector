# リリースの手順

メンテナー向けの手順です。バージョンは [セマンティック バージョニング](https://semver.org/lang/ja/) に従います。1.0.0 より前は、マイナーバージョンでも互換性を壊す変更をすることがあります。

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

1. `origin/main` から作業ブランチを作り、次を変更します。
   - `pyproject.toml` の `version`
   - `CHANGELOG.md`: `[Unreleased]` の内容を新しいバージョンの節に移し、日付を書く。末尾の比較用リンクも更新する
2. プルリクエストを作り、CI が通ってからマージします。
3. GitHub の「Releases」→「Draft a new release」で、`main` に `v0.1.0` の形式のタグを作ります。タグは `pyproject.toml` のバージョンと一致させてください。一致しないと公開のワークフローが止まります。
4. リリースノートには `CHANGELOG.md` の該当する節を貼り、「Publish release」を押します。
5. `.github/workflows/release.yml` が動き、sdist と wheel を作って PyPI に公開します。`pypi` 環境に承認者を設定した場合は、Actions の画面で承認します。
6. [PyPI のページ](https://pypi.org/project/poop-detector/) に新しいバージョンが出たことを確認します。

## 注意

- 一度 PyPI に公開したバージョンは、同じ番号で上書きできません。誤りがあれば、番号を上げて公開し直してください。
- PyPI のプロジェクトページには README がそのまま表示されます。README の画像や `docs/` へのリンクは相対パスなので、PyPI では表示されません。詳しい説明は GitHub のリポジトリを見てもらうことになります。
- 手元で確認するときは `uv build` と `uvx twine check --strict dist/*` を実行します。CI でも同じ確認をしています。
