# Cube J1 ローカル電力モニタ

サービス終了した NextDrive Cube J1 で、スマートメーターの消費電力を
**スマホのブラウザからリアルタイムに見る**ためのセットアップ USB 作成ツール。

Home Assistant も MQTT ブローカーも常時稼働サーバーも要りません。
24時間動く必要があるのは Cube 本体だけです。

```
スマートメーター --Wi-SUN--> Cube J1 --Wi-Fi--> スマホのブラウザ
                             ├ mqtt_bridge.py (上流のまま)
                             │        │ MQTT / 127.0.0.1:11883
                             └ meter_hub.py (このリポジトリ)
                                      │ HTTP / 0.0.0.0:8080
```

長期の蓄積・集計は電力会社の Web サービス（くらしTEPCO web など）に任せ、
こちらは「今どうなっているか」だけを担当する、という役割分担が前提です。

## 使い方

`setup-cube-j1.bat` をダブルクリック。聞かれるのは4つだけ。

| 項目 | 補足 |
|---|---|
| Bルート認証ID | 電力会社発行、英数字32文字 |
| Bルートパスワード | 12文字。入力は伏せ字 |
| Wi-Fi SSID / パスワード | 2.4GHz / WPA-PSK |
| 取得間隔 | 既定 60秒 |

出来上がった USB を Cube に挿して電源投入。白LEDが10回点滅すれば完了、
緑点灯で Wi-Fi 接続成功。あとはスマホで `http://<CubeのIP>:8080/` を開くだけ。
Android なら「ホーム画面に追加」でアプリのように使えます。

Cube の IP はルーターの管理画面で確認し、DHCP 予約で固定しておいてください。

## 画面

現在の消費電力を大きく表示（1.2kW超で黄色、3kW超で赤）、直近1時間の推移グラフ、
R相/T相の電流、積算電力量。5秒ごとに自動更新し、Cube と通信できなくなると
その旨を表示します。

## 構成

USB メモリの中身はこうなります。

```
USB/
├── CubeJMTS.txt              空ファイル。これがあると Cube が自動実行する
└── production_tool/
    ├── production_tool       ← このリポジトリ（上流 + hub の起動を追加）
    ├── meter_hub.py          ← このリポジトリ（MQTT受け皿 + HTTPサーバー）
    ├── meter_hub.rc          ← このリポジトリ（init サービス定義）
    ├── mqtt_bridge.py        上流のまま
    ├── led_effect.sh         上流のまま
    ├── mqtt_ha_bridge.rc     上流のまま
    ├── wisund_disabled.rc    上流のまま
    ├── ndeclite_disabled.rc  上流のまま
    ├── config.json           生成
    └── wpa_supplicant.conf   生成
```

### meter_hub.py

Cube 上に常駐して3つのことをします。

1. `127.0.0.1:11883` で MQTT 3.1.1 のサーバー側を最小限だけ話し、
   `mqtt_bridge.py` の publish を受ける（CONNECT / PUBLISH / PINGREQ / DISCONNECT のみ）
2. 最新値と直近24時間（既定1440サンプル）をメモリ上に保持
3. `0.0.0.0:8080` でダッシュボードと JSON API を配信

Python 2.7 の標準ライブラリだけ、しかも `mqtt_bridge.py` が使用実績で
証明している モジュール（socket / struct / threading / json / collections /
time / os / sys）だけで書いてあります。`BaseHTTPServer` すら使っていません。

エンドポイント:

```
GET /               ダッシュボード (HTML)
GET /api            最新値 (JSON)
GET /api/history    直近サンプルの配列 (JSON)
GET /healthz        "ok"
```

`/api` は CORS を許可しているので、後から Android アプリを書く場合も
そのまま叩けます。

### 上流との差分

`mqtt_bridge.py` は**1行も変更していません**。差分は `production_tool`
（起動スクリプト）の3点だけです。

- `meter_hub.py` と `meter_hub.rc` を配置し、bridge より**先に**起動する
  （bridge が接続先を見つけられて 15秒ごとの再接続ログを出さずに済む）
- `config.json` を `chmod 600` にする（認証情報が入っているため）
- init RC を書き終えた後に `mount -o ro,remount /` でルートを読み取り専用に戻す
  （上流は rw のまま放置している）

上流は**検証済みのコミット `3bbb6bf` に固定**してあります。main を追いかけると、
上流が変わった瞬間に未検証のコードが Cube 上で root 実行されるためです。

## オプション

```powershell
# 設定だけ書き換える（production_tool は再コピーしない）
.\setup-cube-j1.ps1 -ConfigOnly -Drive E

# ポートを変える
.\setup-cube-j1.ps1 -HttpPort 9000 -HubPort 21883

# 履歴を48時間ぶんにする
.\setup-cube-j1.ps1 -HistorySize 2880

# ネットに出ずローカルのクローンを使う
.\setup-cube-j1.ps1 -SourceDir C:\src\cube-j1-mqtt

# exFAT/NTFS のまま強行する（非推奨）
.\setup-cube-j1.ps1 -SkipFormat
```

## 検証済みの内容

- 上流クライアントと同一のバイト列（CONNECT / PUBLISH QoS0 / PINGREQ / DISCONNECT）
  を投げて、CONNACK・PINGRESP・値の取り込み・履歴の蓄積を確認
- HA auto-discovery の retain メッセージを無視することを確認
- ダッシュボードを実ブラウザ（390x844）で描画。グラフ、切断時表示、
  コンソールエラーなしを確認
- ログ切り詰めが行境界で切れること、冪等であることを確認
- `production_tool` の shell 構文チェック

Wi-SUN の実通信部分は実機がないと確認できないため未検証です。ここは上流の
コードをそのまま使っています。

## 前提と注意

▼ **Bルート利用申請が必要**
電力会社へ申請。ID はメール、パスワードは郵送で1〜2週間かかります。

▼ **config.json に Bルート認証情報が平文で入る**
USB 側と Cube 内 (`/data/local/config.json`, 600) の両方。セットアップ後は
USB を消すか施錠保管してください。

▼ **Cube は adb を 5555 で開放する**
上流の仕様です。LAN 内の誰でも root shell を取れる状態になり、そこから
`/data/misc/wifi/wpa_supplicant.conf` 経由で自宅 Wi-Fi の PSK も読めます。
IoT 用 VLAN かゲストネットワークへの隔離を強く推奨します。

▼ **純正の wisund / NDEcLiteAgent は停止・無効化される**
Wi-SUN の `/dev/ttyS1` が競合するためです。純正クラウドは2025年3月末で
終了済みなので実害は小さいものの、元に戻すのは手間です。

▼ **ファームウェア更新はもう来ない**
カーネルは古く、既知の脆弱性が出ても修正されません。

▼ **家庭内 LAN でのみ動作**
外出先から見るには VPN 等が別途必要です。HTTP は暗号化されていません。

実行は自己責任で。上流リポジトリの警告も参照してください。

## 動作要件

Windows 10 / 11、PowerShell 5.1（標準搭載）。追加インストール不要。

## クレジット

Cube J1 上で動く Wi-SUN / ECHONET Lite / MQTT ブリッジ本体は
[tsuyopon123/cube-j1-mqtt](https://github.com/tsuyopon123/cube-j1-mqtt)（MIT）。
root 化の仕組みの解説は
[NextDrive Cube J1を分解せずにrootを取りたい！](https://zenn.dev/tsuyopon123/articles/cube-j1-root)。
