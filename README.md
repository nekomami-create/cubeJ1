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

`production_tool`（起動スクリプト）側の差分が5点、`mqtt_bridge.py` への
パッチが4種類です。**USB 上のファイルは上流とバイト同一のまま**で、
`/data/local/` へ配置する側だけを書き換えます。

production_tool 側:

- `meter_hub.py` と `meter_hub.rc` を配置し、bridge より**先に**起動する
  （bridge が接続先を見つけられて 15秒ごとの再接続ログを出さずに済む）
- `config.json` を `chmod 600` にする（認証情報が入っているため）
- init RC を書き終えた後に `mount -o ro,remount /` でルートを読み取り専用に戻す
  （上流は rw のまま放置している）
- 純正の `wisund.rc` / `ndeclite_agent.rc` / `gndctrl.rc` /
  `iptables_firewall.conf` / `iptables_firewall.sh` を上書き前に `/data/local/`
  へ退避する（上流は退避せず上書きする。純正の中身は他のどこにも残らない）
- 純正ファイアウォールを両アドレスファミリ対応にし、P2P AP 側を塞ぐ

mqtt_bridge.py へのパッチ:

- `LED_R` / `LED_G` / `LED_B` を `/dev/null` に向ける（3行。「LED の見かた」参照）
- `skscan()` のスキャン期限を実際の走査時間に合わせる（1行。「つながらないとき」参照）
- スキャン duration の上限を 10 → 8 に下げる（1周回が約11分から3分弱になる）
- ERXUDP が5回連続でタイムアウトしたら再接続する（「つながらないとき」参照）

パッチは常に素の上流を入力にし、生成物が期待する字句を含むことを確認してから
採用します。外れた場合は素の上流をそのまま配置します。

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

## LED の見かた

純正ファームの LED の意味は `/system/etc/init/led_state.rc` に定義されています。

| 状態 | LED |
|---|---|
| 起動中 | 青の点滅 |
| 起動完了 | 青の点灯 |
| ネットワーク接続済み | **緑の点灯** |
| ネットワーク切断 | 青の点灯 |
| リセット待ち | 緑と赤の点滅 |
| リセット中 / 更新失敗 | 赤の点灯 |
| ファーム更新中 | 青と赤の点滅 |

つまり通常運転は**緑の点灯**です。

### なぜ上流の LED 出力を切っているか

上流の `mqtt_bridge.py` は、SKSTACK コマンドを送っている間ずっと LED を
緑と青で点滅させます（`skcommand` と `SKJOIN`、200ms 間隔）。メーターに
つながるまでスキャンを繰り返すため、**約115秒ごとに約10秒の点滅が延々と続きます**。

さらに、この処理は点滅前に LED の値を保存して後で書き戻すので、保存した
瞬間が消灯フェーズだと**消灯したまま復元されます**。実機でこれが起きました。

そこで `LED_R` / `LED_G` / `LED_B` の3行だけを `/dev/null` に向け、ブリッジが
LED に触らないようにしています。ロジックは1行も変えていません。これにより
上の表の純正の表示（つながっていれば緑）がそのまま見えます。

実機で確認済み: パッチ後 5分31秒（点滅周期の2.9倍）にわたり LED 変化ゼロ。
同じ監視で意図的な変更は記録されたので、監視漏れではありません。

## AP を塞ぐ / gndctrl に触らない

ペアリング AP を消したくなりますが、それを作っている `gndctrl` は
**無効化してはいけません**。この常駐は次を全部兼ねています。

| 役割 | 無効化したときに起きること |
|---|---|
| ネットワーク管理（`NM`） | Wi-Fi 接続後に `udhcpc` が起動せず IPv4 が取れない |
| USB 自動実行の起動 | **USB からの復旧が効かなくなる** |
| リセットボタンの検出 | 長押しに反応しなくなる |
| P2P AP の作成 | AP は消える（唯一の目的どおり） |

実際に無効化して再起動し、Cube が LAN から消えました。復旧経路が3つ同時に
死ぬので、退避ファイルを置いてあっても読ませる手段が無くなります。

代わりに純正のインターフェース別ファイアウォールを使います。
`/usr/sbin/iptables_firewall.sh` が `/system/etc/iptables_firewall.conf` の
`POLICY` を読み、毎起動走る oneshot サービスなので USB を抜いても効きます。

```
POLICY="p2p+:5555,p2p+:8080,p2p+:8883,p2p+:80,p2p+:443"
```

**ただし純正スクリプトは `iptables` しか叩きません。** adb は `:::5555` で
待っており、P2P AP に繋いだ相手には IPv6 リンクローカルが配られるので、
IPv4 だけの規則は丸ごと迂回できます。そこで同じ POLICY を `ip6tables` にも
反映するよう、純正スクリプト自体に2行加えています（純正は退避してあり、
パッチは常にその退避を入力にするので、再実行しても積み重なりません）。

実機で再起動をまたいで確認済み: iptables 5本 / ip6tables 5本。
### IPv4 で見つからないときは IPv6 を試す

DHCP が動かなくても、**IPv6 の SLAAC は DHCP を必要としません**。Wi-Fi に
つながってさえいればアドレスが付き、しかも MAC から計算できます。
例として `6c:21:a2:00:11:22` なら、第1オクテットの bit を反転して `6e21:a2ff:fe00:1122`。

```sh
ping -6 fe80::6e21:a2ff:fe00:1122          # リンクローカル
adb connect [<ルータのprefix>:6e21:a2ff:fe00:1122]:5555
```

IPv4 のポートスキャンで出てこなくても、死んだとは限りません。

## 残っているリスク（塞げていないもの）

P2P AP 側は塞ぎましたが、**自宅LAN 側の adb は開いたままです**。

```
tcp  :::5555  LISTEN     ← wlan0 からも到達できる（IPv4/IPv6 とも）
```

自宅LAN に入れる人は誰でも root シェルを取れ、そこから
`/data/misc/wifi/wpa_supplicant.conf` の Wi-Fi PSK と、
`/data/local/config.json` の Bルート認証情報が読めます。上流の仕様です。

選べる対処は3つ。

| 方針 | 効果 | 代償 |
|---|---|---|
| **現状のまま**（← 採用） | 自宅LAN を信頼する | LAN 内の全機器が root を取れる |
| IoT用VLAN / ゲストネットへ隔離 | LAN 側からの到達を断つ | ダッシュボードもそちらからしか見られない |
| `POLICY` に `wlan0:5555` を足す | adb を完全に閉じる | 自分も入れなくなる。復旧は USB 経由のみ |

**2026-09-05、オーナーは1つ目（現状維持）を選択しました。** これは未対応の
宿題ではなく決定事項です。頼まれない限り塞ぎに行かないこと。

3つ目は `POLICY` に1項目足すだけで両系統に効きますが、adb を閉じると管理用
の経路も同時に閉じ、次に中を見たいときは USB でセットアップし直しになります。
Bルートの疎通確認が済むまでは開けておくほうが実務的です。

そのほか、上流の設計から来る以下は解消できません。

- カーネルが古く、ファーム更新はもう来ない
- ダッシュボードは平文 HTTP（家庭内LAN 限定の前提）
- ペアリング AP の電波自体は出続ける（中身には到達できない）

## つながらないとき（no PAN found）

ログに `SKSCAN no PAN found` が延々と並ぶとき、**距離や開通状態を疑う前に
ここを読んでください。** 2026-09-13、その二つを疑って半日溶かしました。

### 原因の多くは上流のバグでした

上流の `skscan()` はこう書かれていました。

```python
deadline = time.time() + duration
```

`duration` は SKSTACK のパラメータであって秒数ではありません。モジュールは
**1チャンネルあたり `0.01 × (2^duration + 1)` 秒**かけて走査し、`FFFFFFFF`
マスクは 33〜60 の全帯域を対象にします。

| duration | 上流の待ち時間 | 実際に必要な時間 |
|---|---|---|
| 4 | 4秒 | 約5.4秒 |
| 6 | 6秒 | 約21秒 |
| 10 | 10秒 | 約328秒 |

**毎回、掃き終わる前に打ち切っていました。** チャンネルの低いメーターなら
偶然間に合うので、上流では問題が表面化しません。こちらのメーターは
**0x2F（47）** にいて、帯域の後ろ寄りだったため 776 サイクル全部が空振りでした。

修正は1行です。`production_tool` が配置時に当てます。

```python
deadline = time.time() + 0.01 * (2 ** duration + 1) * 32 + 10
```

### 電波のせいかどうかを先に切り分ける

手で長めのスキャンをかければ一発で分かります。ブリッジを止めてから
`/dev/ttyS1` を 115200 8N1 で開き、こう送ります。

```
SKRESET
SKSETPWD C <12桁のパスワード>
SKSETRBID <32桁のID>
SKSCAN 2 FFFFFFFF 6 0
```

**60秒以上待ってください。** 応答があればこう返ります。

```
EVENT 20 ...
EPANDESC
  Channel:2F      メーターのチャンネル
  Pan ID:xxxx
  Addr:xxxxxxxxxxxxxxxx
  LQI:93          20〜30以下だと電波が弱すぎる。93 は良好
  PairID:xxxxxxxx BルートID末尾8桁と一致するはず（ここが要点）
EVENT 22 ...
```

ここで見つかるなら**電波の問題ではありません**。ブリッジ側の待ち時間の問題です。

`EVENT 22` だけが返って `EPANDESC` が出ないなら、初めて電波か開通状態を疑います。

### 受信系が生きているかを確かめる

ED スキャンでチャンネルごとの電波強度が測れます。

```
SKSCAN 0 FFFFFFFF 4 0
→ EEDSCAN
  0 21 1E 22 13 23 1D ... （チャンネル 強度 の並び）
```

33〜60 の値が並べば、アンテナも受信機も正常です。dBm への換算はおよそ
`0.275 × 値 − 104.27`。全チャンネルが -90 台後半なら、ただのノイズフロアです。

### モジュールの素性を見る

```
SKVER     → EVER 1.5.2
SKAPPVER  → EAPPVER rev15
SKINFO    → EINFO <IPv6> <MAC> <ch> <PAN ID> <side>
```

`SKINFO` の PAN ID が `FFFF` なら未参加、正常な待機状態です。

### 認証情報が受理されているか

```
SKSETPWD C <pwd>  → OK     C は桁数の16進（12桁なら C）
SKSETRBID <id>    → OK
```

どちらかが `FAIL` なら、その時点で原因が確定します。ブリッジはこの応答を
記録しないので、手で確かめる価値があります。

### 接続済みなのに値が来ない（No ERXUDP response が続く）

`SKJOIN: connected` の後に `No ERXUDP response (timeout)` だけが並ぶなら、
**認証までは通るがデータの往復が通らない、際どい電波**です。短い制御フレームは
届いても、応答のやりとりを取りこぼします。2026-09-13 に2階へ置いたとき、
一度だけ参加に成功し、その後10分間すべてタイムアウトしました。

上流はこの状態から抜けません。タイムアウトは例外ではないのでログを出すだけで、
再接続は例外のときにしか走らないためです。`production_tool` は5回連続で
タイムアウトしたら例外を投げて、既存の再接続経路に乗せるパッチを当てます。

それでも同じことを繰り返すなら、置き場所が電波的に足りていません。

### duration の上限

このファームでは **10 が上限**です。11 以上は `FAIL ER06` を返します
（ROHM の資料には 0〜14 とありますが、この個体では通りません）。

期限を正しくしたことで duration 9 は約2分、10 は約6分かかるようになり、
4→10 の1周回が11分に伸びました。`production_tool` は上限を 8 に下げます。
8 で聞こえないメーターは、どのみち実用になりません。

## 元に戻す

初回実行時に、純正の init 設定を上書きする前に `/data/local/` へ退避します。
2回目以降は既存の退避ファイルを温存します（無効化済みの版で潰さないため）。

```
/data/local/stock_wisund.rc          純正の wisund.rc
/data/local/stock_ndeclite_agent.rc  純正の ndeclite_agent.rc
```

`/data` にあるので USB を抜いても残ります。書き戻すときは adb から:

```sh
adb connect <CubeのIP>:5555
adb shell mount -o rw,remount /
adb shell cp /data/local/stock_wisund.rc /system/etc/init/wisund.rc
adb shell cp /data/local/stock_ndeclite_agent.rc /system/etc/init/ndeclite_agent.rc
adb shell rm /system/etc/init/meter_hub.rc /system/etc/init/mqtt_ha_bridge.rc
adb shell mount -o ro,remount /
adb shell setprop persist.adb.tcp.port ""     # adb の常時開放をやめる
adb reboot
```

退避が無いまま戻す羽目になった場合でも、純正版は無効化版から `disabled` の
1行を取り除いたものです（実機の退避ファイルで確認済み）。ただし退避ファイルが
あるなら、そちらを使うほうが確実です。

## 検証済みの内容

- 上流クライアントと同一のバイト列（CONNECT / PUBLISH QoS0 / PINGREQ / DISCONNECT）
  を投げて、CONNACK・PINGRESP・値の取り込み・履歴の蓄積を確認
- HA auto-discovery の retain メッセージを無視することを確認
- ダッシュボードを実ブラウザ（390x844）で描画。グラフ、切断時表示、
  コンソールエラーなしを確認
- ログ切り詰めが行境界で切れること、冪等であることを確認
- `production_tool` の shell 構文チェック

### 実機で確認済み（2026-09-13、Bルート開通）

- スマートメーターへの接続（`SKJOIN: connected`）と実測値の取得
- メーターは帯域の後ろ寄り（チャンネル 0x2F）、LQI 93 と良好
- 上流の `skscan()` のスキャン期限バグを特定して修正（「つながらないとき」）
- `SKSETPWD` / `SKSETRBID` はいずれも `OK` を返すことを直接確認
- ED スキャンで受信系と帯域（33〜60ch）が正常であることを確認

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


▼ **未設定のため Wi-Fi Direct のペアリング AP が立ちっぱなしになる**
純正クラウドに紐づいていない（ログに `User not yet configured`）ため、Cube は
SSID `CubeJ-<MACの下6桁>` の P2P グループを約3分ごとに作り直し続けます。
パスフレーズは機種共通の既定値で、`192.168.100.1/24` を配ります。adb は
`:::5555` で全インターフェースに開くため、この AP は root シェルへの経路に
なります。**IoT用VLANへの隔離では防げません** — Cube 自身が飛ばす別の電波
だからです。

対処として `POLICY="p2p+:5555"` を入れ、p2p インターフェース上の adb を
REJECT しています（「AP を塞ぐ」参照）。AP の電波自体は残ります。

▼ **純正の wisund / NDEcLiteAgent は停止・無効化される**
Wi-SUN の `/dev/ttyS1` が競合するためです。純正クラウドは2025年3月末で
終了済みなので実害は小さいものの、戻す手順は「元に戻す」を参照してください。

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
