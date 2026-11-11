# 外から GUI を開く

GUI は既定で、監視するマシン（`127.0.0.1`）からしか開けません。スマートフォンなど外の端末から開くときは、HTTPS を終端するリバースプロキシやトンネルを前に置き、GUI はその背後で動かします。

> [!WARNING]
> いまの GUI の認証は、起動時に端末に表示されるトークン付きの URL だけです。パスキーによるユーザー認証（[#84](https://github.com/ysakae/poop-detector/issues/84)）が入るまでは、インターネットに直接公開せず、家族だけが入れる VPN（Tailscale の tailnet など）の中から開く使い方にとどめてください。GUI は室内の映像と記録を扱います。

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

`public_url` を設定すると、GUI はそのドメイン名の `Host` と、そのオリジンからのフォームの送信を受け付けます。起動時には、外から開くためのトークン付きの URL も端末に表示します。

## Caddy の例

[Caddy](https://caddyserver.com/) は、証明書を自動で取得し、`X-Forwarded-Proto` などを既定で付けます。ドメイン名の DNS をこのマシンに向け、ポート 443 に外から届くようにしてから、次の `Caddyfile` で起動します。

```text
toilet.example.com {
	reverse_proxy 127.0.0.1:8765
}
```

Cloudflare Tunnel や Tailscale などのトンネルを使う場合も、転送先を `http://127.0.0.1:8765` にします。元の `Host` と `X-Forwarded-Proto` の渡し方は、各サービスの説明を確かめてください。HTTPS で届いても `X-Forwarded-Proto` が渡らない構成では、HSTS と `Secure` 属性が付きません。

## Docker の中で動かす場合

コンテナの中では `127.0.0.1` に外から届かないので、`gui.host` を `0.0.0.0` にし、ポートは監視するマシンの `127.0.0.1` だけに公開します（`-p 127.0.0.1:8765:8765`）。リバースプロキシからの要求はコンテナのネットワークのアドレスから届くので、`gui.trusted_proxies` にそのアドレスかネットワーク（例：`172.17.0.1`）を書いてください。

## 確かめること

- `https://toilet.example.com` で開き、ブラウザーのアドレス欄に鍵のマークが出る
- `http://` で開くと、リバースプロキシが `https://` に移す
- ほかのドメイン名やサーバーの IP アドレスで開くと「アクセスできません」になる
- ルーターでポート 8765 を外に公開していない（外から届くのはリバースプロキシのポートだけ）
