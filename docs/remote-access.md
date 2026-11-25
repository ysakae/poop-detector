# 外から GUI を開く

GUI は既定で、監視するマシン（`127.0.0.1`）からしか開けません。スマートフォンなど外の端末から開くときは、HTTPS を終端するリバースプロキシやトンネルを前に置き、GUI はその背後で動かします。

> [!WARNING]
> GUI は室内の映像と記録を扱います。インターネットから開けるようにすると、世界中から接続を試みられます。GUI はパスキーでログインした登録者だけが使えますが、poop-detector とリバースプロキシを最新に保ち、使わなくなったユーザーは `users disable` で無効にしてください。家族だけが入れる VPN（Tailscale の tailnet など）の中から開く使い方が、最も安全です。

## 構成

```text
スマートフォン ──HTTPS──▶ リバースプロキシ（Caddy など）──HTTP──▶ GUI（127.0.0.1:8765）
                        証明書の取得と HTTPS の終端
```

- GUI は `127.0.0.1` で待ち受けたままにします。外からの接続は、同じマシンのリバースプロキシだけが受けます。
- リバースプロキシは、元の `Host` と、HTTPS で届いたことを示す `X-Forwarded-Proto: https` を GUI に渡します。
- GUI は、設定の `gui.trusted_proxies` から届いた転送のヘッダーだけを信頼します。HTTPS で届いた要求には、HSTS と、Cookie の `Secure` 属性を付けます。

## 設定

`config/config.yaml` の `gui` セクションに、外から開くときの URL を書きます。

```yaml
gui:
  host: 127.0.0.1
  port: 8765
  public_url: https://toilet.example.com
  trusted_proxies: 127.0.0.1
```

| 項目 | 内容 |
|---|---|
| `host` | 待ち受ける IP アドレス。リバースプロキシが同じマシンなら `127.0.0.1` のまま |
| `port` | 待ち受けるポート。`gui --port`・`run --gui-port` を指定すれば、そちらを使う |
| `public_url` | 外から開くときの URL。`https://` とドメイン名だけを書く（パスや IP アドレスは不可。パスキーはドメイン名でしか使えないため） |
| `trusted_proxies` | `X-Forwarded-For`・`X-Forwarded-Proto` を信頼するプロキシの IP アドレスかネットワーク（カンマ区切り）。すべてを信頼する `*` は使えない |

`public_url` を設定すると、GUI はそのドメイン名の `Host` と、そのオリジンからのフォームの送信を受け付けます。起動時には、外から開くときの URL も表示し、ブラウザーはその URL で開きます（`--no-browser` で開かない）。パスキーはドメインに結び付くので、外から使うパスキーは、外部の URL で開いた画面から登録してください（最初の管理者の登録用 URL と招待メールのリンクにも外部の URL を使います）。

ログインした状態（セッション）は、操作がないまま `gui.session_idle_hours`（既定168時間）か、ログインから `gui.session_max_days`（既定30日）で切れます。登録用 URL とパスキーの窓口、フォームの送信、すべての要求には、接続元ごとの回数の上限があります。リバースプロキシの背後では、`gui.trusted_proxies` から届いた `X-Forwarded-For` の接続元で数えます。

## Caddy の例

[Caddy](https://caddyserver.com/) は、証明書を自動で取得し、`X-Forwarded-Proto` などを既定で付けます。ドメイン名の DNS をこのマシンに向け、ポート 443 に外から届くようにしてから、次の `Caddyfile` で起動します。

```text
toilet.example.com {
	reverse_proxy 127.0.0.1:8765
}
```

GUI は、画面（HTML）・スタイル・スクリプト・JSON を自分で gzip に圧縮して送るので、リバースプロキシで圧縮を設定する必要はありません（Tailscale Serve のように圧縮できないプロキシでも、モバイル回線での転送量が減ります）。ライブ映像・JPEG の画像・動画は圧縮せず、そのまま流します。スクリプト・画像・録画の動画はブラウザーに預けてもらうので（[設計の概要](architecture.md) の GUI の節）、2回目からは取り直しません。

Cloudflare Tunnel や Tailscale などのトンネルを使う場合も、転送先を `http://127.0.0.1:8765` にします。元の `Host` と `X-Forwarded-Proto` の渡し方は、各サービスの説明を確かめてください。HTTPS で届いても `X-Forwarded-Proto` が渡らない構成では、HSTS と `Secure` 属性が付きません。

## Tailscale Serve の例

監視するマシンと閲覧端末を同じ tailnet に参加させます。Linux の監視マシンで未ログインの場合は `sudo tailscale up` を実行して認証します。続けて tailnet 内用の HTTPS 転送を設定します。

```bash
sudo tailscale serve --bg http://127.0.0.1:8765
tailscale serve status
```

HTTPS を有効にするための URL が表示されたら、ブラウザーで手続きしてから Serve コマンドを再実行します。出力に表示された HTTPS URL を `gui.public_url` に設定し、`gui.host` と `gui.trusted_proxies` は上の例どおり `127.0.0.1` に設定します。`--bg` を使うと、Tailscale の再起動後も転送が再開します。[Tailscale Serve の公式手順](https://tailscale.com/docs/reference/tailscale-cli/serve)

GUI を起動したあと、tailnet 内の端末から `/login` を開きます。Serve が `502` を返す場合は、まず監視マシン上の GUI が起動しているか、Serve の転送先と待ち受けポートが一致しているかを確かめます。systemd を使う場合は [Linux の常時運用手順](docker.md#linuxミニpcで常時運用する) の状態とログの確認を参照してください。

## Docker の中で動かす場合

コンテナの中では `127.0.0.1` に外から届かないので、`gui.host` を `0.0.0.0` にし、ポートは監視するマシンの `127.0.0.1` だけに公開します（`-p 127.0.0.1:8765:8765`）。リバースプロキシからの要求はコンテナのネットワークのアドレスから届くので、`gui.trusted_proxies` にそのアドレスかネットワーク（例：`172.17.0.1`）を書いてください。

## 確かめること

- `https://toilet.example.com` で開き、ブラウザーのアドレス欄に鍵のマークが出る
- `http://` で開くと、リバースプロキシが `https://` に移す
- ほかのドメイン名やサーバーの IP アドレスで開くと「アクセスできません」になる
- ルーターでポート 8765 を外に公開していない（外から届くのはリバースプロキシのポートだけ）
- ブラウザーの開発者ツールで、画面の応答に `Content-Encoding: gzip` が付いている
