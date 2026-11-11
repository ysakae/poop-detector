# セキュリティポリシー

> **English:** Please do not report security vulnerabilities in public issues. Use GitHub's private vulnerability reporting ("Report a vulnerability" on the Security tab). If it is not available, open an issue that only says you want to report a security problem privately, without any details.

## 対象のバージョン

まだリリースを作っていないため、`main` ブランチの最新の状態だけを対象にします。

## 脆弱性の報告方法

**脆弱性の内容を公開の Issue やプルリクエストに書かないでください。**

1. GitHub のリポジトリの「Security」タブにある「Report a vulnerability」から、非公開で報告してください（GitHub の [非公開の脆弱性報告](https://docs.github.com/ja/code-security/security-advisories/guidance-on-reporting-and-writing-information-about-vulnerabilities/privately-reporting-a-security-vulnerability)）。
2. この機能が表示されない場合は、内容を伏せて「セキュリティについて非公開で連絡したい」とだけ書いた Issue を作ってください。メンテナーが非公開の連絡方法を案内します。

報告には、分かる範囲で次のことを書いてください。

- 問題の内容と、悪用されたときの影響
- 再現の手順（人工データで再現できる方法が望ましいです）
- 影響を受けるファイルや機能、コミット
- 考えられる対策（任意）

実際のカメラ映像、RTSP の URL、ntfy のトピック名は、非公開の報告にも含めないでください。

個人の実験的なプロジェクトのため、返信と修正はできる範囲で行います。修正を公開するまでは、内容を公表しないようお願いします。

## 想定している使い方と範囲

このツールは、利用者が自分の家の監視するマシンで動かすことを前提にしています。GUI は、[外から GUI を開く](docs/remote-access.md) の手順（HTTPS のリバースプロキシとパスキーでのログイン）でインターネットから開くこともできます。データの保存先と、外部に送られる情報は [docs/privacy.md](docs/privacy.md) にまとめています。

報告を歓迎する問題の例:

- RTSP URL の認証情報が、ログ、エラーメッセージ、画面、通知に出てしまう
- GUI のクロスサイトスクリプティング（XSS）、クロスサイトリクエストフォージェリ（CSRF）、DNS リバインディング、保存先の外のファイルを読み出せる不具合
- 犬の名前や設定の値を通じて、`osascript`、FFmpeg、Codex CLI に意図しないコマンドや引数を渡せる
- 犬検出モデルの取得で、ハッシュの検証を回避できる
- GUI のログイン（パスキー、登録用 URL、セッション）を回避できる、ほかのユーザーのパスキーを操作できる、無効にしたユーザーが使える
- 意図しない外部への送信

範囲外とする例:

- 文書の手順に反して、GUI のポートを HTTPS のリバースプロキシなしで直接公開した場合や、カメラの RTSP ポートをインターネットに公開した場合の問題
- 同じマシンで同じ OS ユーザーとして動くプログラムが、`data/` や `.env` を読める（OS の権限の範囲です）
- ntfy の無料トピックの名前を知っている人が通知を読める（ntfy の仕様です。推測しにくい名前を使ってください）
