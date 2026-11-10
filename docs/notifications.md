# 通知

通知先は `.env` の設定で決まります。

| 設定 | 通知先 |
|---|---|
| `POOP_NTFY_TOPIC` なし | macOS の通知センター（Linux では通知されません） |
| `POOP_NTFY_TOPIC` あり | ntfy（macOS・Linux とも） |

どちらの場合も `config/config.yaml` の `notifications: true`（既定）が必要です。`pet_name` を設定すると、通知のタイトルと本文に犬の名前が入ります。通知を使わない場合は `notifications: false` にしてください。

## 通知のテスト

```bash
uv run poop-detector test-notification
```

成功は、OS または ntfy のサーバーが通知を受け付けたことを示します。受信端末で実際に表示されるかを確認してください。macOS では通知の設定や集中モードも確認してください。

## 家族のスマートフォンへ ntfy で通知する

家族のスマートフォンに ntfy アプリを入れ、全員で同じ `ntfy.sh` のトピックを購読します。無料トピックは公開扱いです。`openssl rand -hex 16` などで推測しにくい名前を一度作り、家族内だけで共有してください。

`.env` の `POOP_NTFY_TOPIC` にトピック名を設定し、送信を試します。

```bash
uv run poop-detector test-notification
```

リポジトリのディレクトリで普段の監視コマンドを起動すると、ntfy を通知先に使います。送信するのは短い本文で、映像やカメラの URL は添付しません。`--early-alert` を付けると見守り通知も届きます。

## 自分で運用する ntfy サーバーを使う

`.env` の `POOP_NTFY_SERVER` にサーバーの URL（例: `https://ntfy.example.com`）を設定すると、`ntfy.sh` の代わりにそのサーバーへ送ります。URL に認証情報や `?` 以降は含められません。`http://` も使えますが、通知の本文が暗号化されずに流れるため、信頼できる LAN 内のサーバーに限ってください。

## 見守り通知を別のトピックに分ける

見守り通知（`--early-alert`）を分ける場合は、別のトピック名を作って `.env` の `POOP_NTFY_EARLY_TOPIC` に設定します。見守り通知を受け取りたい人だけが、そのトピックを追加で購読してください。

```bash
uv run poop-detector test-notification --early-alert
```

両方のトピックを設定したら、`--early-alert` 付きの監視を再起動します。排便・排尿候補は通常のトピックに、見守り通知だけは別のトピックに届きます。`POOP_NTFY_EARLY_TOPIC` が未設定の場合、見守り通知も通常のトピックに届きます。

## Codex 判定の通知

`--codex` を付けて起動すると、差分だけの通知を抑え、Codex が新しい便や尿の根拠を確認した場合にだけ通知します。[動画](video.md#codexの判定候補と通知を使う) を参照してください。
