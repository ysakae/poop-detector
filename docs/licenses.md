# 依存ライブラリと取得物のライセンス

このプロジェクトのコードは [MIT License](../LICENSE) です。ここでは、依存ライブラリ、実行時に取得するファイル、外部プログラムのライセンスと、MIT License と矛盾しない理由をまとめます。2026年11月の `uv.lock` で確認しました。依存を追加・更新したときは、この表も見直してください。

## 配布物に含まれるもの

PyPI に公開する sdist と wheel、およびリポジトリには、このプロジェクトのコード、文書、人工データで撮ったスクリーンショットだけが入っています。第三者のコード、画像、モデルは含めていません。`gui/static/` の JavaScript も自作で、外部のライブラリは使っていません。

依存ライブラリは、利用者の環境に `uv` や `pip` が PyPI から別に導入します。このため、このプロジェクトの配布物に第三者のライセンス表示（NOTICE）を同梱する必要はありません。

## 実行時の依存

| パッケージ | ライセンス | 備考 |
|---|---|---|
| opencv-python | Apache-2.0 | wheel に FFmpeg などのライブラリが同梱されています（[後述](#opencv-python-の-wheel-が同梱するライブラリ)） |
| numpy | BSD-3-Clause AND 0BSD AND MIT AND Zlib AND CC0-1.0 | Python 3.11 では 2.4 系、3.12 以上では 2.5 系が選ばれます |
| PyYAML | MIT | |
| ruamel.yaml | MIT | 管理 › 設定で、コメントを残したまま設定ファイルを書き換える |
| psutil | BSD-3-Clause | |
| python-dotenv | BSD-3-Clause | |
| Jinja2 | BSD-3-Clause | GUI の HTML テンプレート |
| MarkupSafe | BSD-3-Clause | Jinja2 の依存。HTML のエスケープ |
| Starlette | BSD-3-Clause | GUI の ASGI フレームワーク |
| uvicorn | BSD-3-Clause | GUI の ASGI サーバー |
| anyio | MIT | Starlette の依存。非同期処理とスレッドプール |
| h11 | MIT | uvicorn の依存。HTTP/1.1 の処理 |
| click | BSD-3-Clause | uvicorn の依存 |
| idna | BSD-3-Clause | anyio の依存 |
| webauthn | BSD-3-Clause | パスキー（WebAuthn）の検証 |
| http-ece | MIT | Web Push の本文の暗号化（RFC 8291） |
| py-vapid | MPL-2.0 | Web Push の送信元の署名（VAPID、RFC 8292） |
| cbor2 | MIT | webauthn の依存。CBOR の読み書き |
| cryptography | Apache-2.0 OR BSD-3-Clause | webauthn の依存。署名の検証 |
| pyOpenSSL | Apache-2.0 | webauthn の依存。証明書の検証 |
| pyasn1、pyasn1-modules | BSD-2-Clause | webauthn の依存。ASN.1 の読み込み |
| cffi | MIT-0 | cryptography の依存 |
| pycparser | BSD-3-Clause | cffi の依存 |

## 開発用の依存

配布物には含まれず、利用者が導入する必要もありません。

| パッケージ | ライセンス |
|---|---|
| pytest | MIT |
| ruff | MIT |
| pluggy | MIT |
| iniconfig | MIT |
| packaging | Apache-2.0 OR BSD-2-Clause |
| Pygments | BSD-2-Clause |
| colorama（Windows のみ） | BSD-3-Clause |
| hatchling（ビルド時） | MIT |

## opencv-python の wheel が同梱するライブラリ

opencv-python の wheel には、FFmpeg、Qt 5（Linux と macOS の画面あり版）、libjpeg-turbo、libpng、libwebp、OpenSSL などが同梱されています。各ライブラリのライセンス全文は、インストール先の `opencv_python-<版>.dist-info/LICENSE-3RD-PARTY.txt` にあります。FFmpeg と Qt 5 は LGPL として表示されています。

macOS の wheel（4.14.0.94 で確認）には、GPL のライブラリである `libx264`、`libx265`、`libpostproc`、`librubberband`、`libvidstab` も含まれています。これらは opencv-python が配布しているもので、このプロジェクトは依存として指定しているだけです。このプロジェクトのコードを利用者が自分の環境で動かすことについて、GPL の条件は課されません。opencv-python を同梱した製品やイメージを再配布する場合は、配布する側が同梱物の条件を確認してください。

## 実行時に取得するファイル

| 取得物 | ライセンス | 取得の仕方 |
|---|---|---|
| 犬検出モデル（[OpenCV Zoo の YOLOX](https://github.com/opencv/opencv_zoo/tree/48b10fa505aeb51fbd127fc60a5c660040e823b7/models/object_detection_yolox)） | Apache-2.0 | `setup-dog-model` を実行したときだけ、固定したリビジョンから取得し、SHA256 を検証します。モデルの `LICENSE` も `data/models/dog-yolox/` に保存します |

モデルはリポジトリにも配布物にも含めていません。

## 外部プログラム

次のプログラムは利用者が別に導入し、このプロジェクトは外部プロセスとして呼び出すだけです。ライブラリとしてリンクしないため、このプロジェクトのライセンスには影響しません。

| プログラム | ライセンス | 用途 |
|---|---|---|
| FFmpeg（`libx264` を有効にした構成） | GPL-2.0 以降 | 動画の MP4 への変換 |
| Codex CLI | Apache-2.0 | 動画の自動判定の既定の判定器（`--video-review`、`video-review`） |

## Docker イメージ

`Dockerfile` から作るイメージには、Debian の `ffmpeg` パッケージ（`libx264` を含む GPL の構成）、Linux 版の opencv-python、Debian と Python の基本イメージが入ります。このプロジェクトではイメージを配布しておらず、利用者が手元でビルドする前提です。

ビルドしたイメージを他の人に配布する場合は、GPL などの条件を満たしてください。たとえば、イメージに含まれる GPL のソフトウェアのソースコードを入手できるようにする必要があります。Debian のパッケージのライセンスは、イメージ内の `/usr/share/doc/<パッケージ名>/copyright` で確認できます。

## 確認の仕方

インストール済みのパッケージのライセンスは、次のコマンドで一覧できます。

```bash
uv run python -c "import importlib.metadata as m; [print(d.metadata['Name'], d.version, d.metadata.get('License-Expression') or d.metadata.get('License')) for d in m.distributions()]"
```
