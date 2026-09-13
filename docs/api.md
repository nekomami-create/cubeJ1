# JSON API の契約

`meter_hub.py` が Cube 上で公開するエンドポイント。Android アプリを書くときは
これに従う。下の実例はすべて実際のレスポンスから採った。

ベースURL は `http://<CubeのIP>:8080`（既定。`-HttpPort` で変更可）。

## 共通

- HTTP/1.1、**TLS なし**。家庭内 LAN 専用
- **認証なし**。LAN に入れる者は誰でも読める
- `Access-Control-Allow-Origin: *`（WebView からも叩ける）
- `Cache-Control: no-store`
- `Connection: close`。keep-alive しないので、接続を保持しようとしないこと
- `GET` と `HEAD` のみ。他は `405`

## GET /api

最新値。

```json
{
  "power_w": 3020.0,
  "current_r_a": 14.2,
  "current_t_a": 13.8,
  "energy_forward_kwh": 12347.678,
  "energy_reverse_kwh": 0.0,
  "ts": 1788592260,
  "now": 1788592260,
  "samples": 3,
  "publishes": 12,
  "uptime_s": 29
}
```

| キー | 意味 |
|---|---|
| `power_w` | 瞬時電力 (W) |
| `current_r_a` / `current_t_a` | 瞬時電流 R相 / T相 (A) |
| `energy_forward_kwh` / `energy_reverse_kwh` | 積算電力量 正方向 / 逆方向 (kWh) |
| `ts` | 最後に値が更新された時刻 (epoch秒) |
| `now` | hub の現在時刻 (epoch秒) |
| `samples` | 履歴バッファに入っているサンプル数 |
| `publishes` | 起動以降に受け取った publish の総数 |
| `uptime_s` | hub の稼働秒数 |

### 実装上の注意

**値のキーは、最初の測定が届くまで存在しない。** `null` が入るのではなく
キーごと無い。起動直後やスマートメーターへの接続中（PANスキャンは数分かかる）は
`power_w` も `ts` も無い状態でレスポンスが返る。`samples` と `uptime_s` と
`now` は常にある。

```
samples == 0 かつ power_w が無い  -> まだメーターにつながっていない
```

**データの鮮度は `now - ts` で判定する。** 端末の時計と比較しないこと。Cube の
時計がずれていても、この差なら正しく出る。`poll_interval`（既定60秒）の
2〜3倍を超えていたら異常とみなしてよい。

## GET /api/history

リングバッファの中身。既定で直近1440件（60秒間隔なら24時間ぶん）。

```json
{"samples": [[1788592259, 450.0, 2.3, 2.1],
             [1788592319, 1310.0, 6.1, 5.9],
             [1788592379, 3020.0, 14.2, 13.8]]}
```

1件は `[ts, power_w, current_r_a, current_t_a]` の固定順。古い順。

**各要素は `null` になりうる。** 履歴は `power` の publish を合図に1件追加され、
その時点で他の値がまだ届いていなければ `null` が入る。描画前に弾くこと。

メモリ上にしか無いので、**Cube が再起動すると消える**。長期の推移は
電力会社の Web サービス側で見る前提。

## GET /healthz

`200 OK` と本文 `ok`。値が取れていなくても hub が生きていれば返る。
死活監視用。

## GET /favicon.svg

タブ用アイコン。`/favicon.ico` は `204` を返す。

## Cube の IP を見つける

mDNS は実装していない。ルーターの管理画面で Cube の MAC に対して
**DHCP 予約で IP を固定**し、アプリ側にはその IP を設定させるのが確実。

サブネットを総当たりして `/healthz` を叩く探索はリポジトリの
`find-cube.ps1` が実装済み（Windows 用）。アプリ側で同じことをする場合も、
判定は `/healthz` が `ok` を返すかどうかで足りる。

IPv4 で見つからないときは IPv6 を試すこと。Cube のリンクローカルアドレスは
MAC から EUI-64 で計算できる（詳細は README「IPv4 で見つからないときは
IPv6 を試す」）。

## ポーリング間隔の目安

| 用途 | 間隔 |
|---|---|
| 画面を開いている間 | 5秒（Web版と同じ） |
| ウィジェット | 1〜5分 |
| 履歴の再取得 | 60秒 |

hub 側の値が変わるのは `poll_interval` ごと（既定60秒）なので、
5秒より短くしても新しい値は出てこない。
