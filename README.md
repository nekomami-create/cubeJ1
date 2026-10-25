# Cube J1 ローカル電力モニタ

サービス終了した NextDrive Cube J1 を、スマートメーターの電力モニタとして
生き返らせるためのセットアップ一式。

- **家では**、スマホで「今何W使っているか」と今日・昨日の使用量、電気代のめやすを見る
- **外では**、専用アプリが家で貯めた履歴を見る（最後に家にいた時点まで）
- おまけとして、Cube の Bluetooth で**近くの BLE 機器の台数**を数える

Home Assistant も MQTT ブローカーも常時稼働のサーバーも要りません。
24時間動く必要があるのは Cube 本体だけです。

```
スマートメーター --Wi-SUN--> Cube J1 -------Wi-Fi(家の中)-------> スマホ
                             ├ mqtt_bridge.py  メーターと話す        ├ ブラウザ: ダッシュボード
                             ├ ble_monitor.py  BLE を数える          └ アプリ:  ダッシュボード
                             │      │ MQTT / 127.0.0.1:11883                    ＋ 履歴を端末に保存
                             └ meter_hub.py    受けて貯めて配る
                                    │ HTTP / 0.0.0.0:8080
```

請求額の正や何年分もの集計は、電力会社の Web サービス（くらしTEPCO web など）に
任せる前提です。

| ファイル | 中身 |
|---|---|
| `setup-cube-j1.bat` / `.ps1` | セットアップ USB を作る（Windows） |
| `payload/` | USB 経由で Cube に入るもの（下の「Cube の中身」） |
| `android/` | スマホアプリ（[android/README.md](android/README.md)） |
| `set-wifi.ps1` / `probe-wifi.ps1` | Wi-Fi の接続先を変える / 置き場所から見える AP を調べる |
| `find-cube.ps1` | LAN 内の Cube を探す |
| `recovery/` | ネットワークを失った Cube を USB から戻す（[recovery/README.md](recovery/README.md)） |

## 使い方

`setup-cube-j1.bat` をダブルクリック。聞かれるのは4つだけ。

| 項目 | 補足 |
|---|---|
| Bルート認証ID | 電力会社発行、英数字32文字 |
| Bルートパスワード | 12文字。入力は伏せ字 |
| Wi-Fi SSID / パスワード | WPA-PSK。2.4GHz（b/g/n）と 5GHz（ac）の両対応 |
| 取得間隔 | 既定 60秒 |

出来上がった USB を Cube に挿して電源投入。白LEDが10回点滅すれば完了、
緑点灯で Wi-Fi 接続成功。

あとはスマホで `http://<CubeのIP>:8080/` を開くか、アプリを入れます
（アプリの入手は [android/README.md](android/README.md)）。
Cube の IP はルーターの管理画面か `find-cube.ps1` で調べ、DHCP 予約で固定してください。

Bルートの認証情報が届く前でも、ダミー（英数32文字 / 12文字）で USB は作れます。
Webサーバーまでは動き、電力値だけが空のままになります。本物が届いたら
`-ConfigOnly` で設定だけ差し替えます（「設定を変える」参照）。

## ダッシュボード（Cube が配るページ）

上から順に:

1. **いまの消費電力** と目安（1.2kW 以上「多め」、3kW 以上「かなり多め」）
2. **今日（ここまで）／昨日／直近7日の平均**。今日は昨日の同時刻との差も出す
3. **電気代のめやす**。今日・昨日の金額と、今の検針期間の見込み（「電気代のめやす」参照）
4. **今日の30分ごと**。今日を棒、昨日を線で重ねる
5. **直近24時間の電力**。最大値とその時刻つき
6. **日ごとの使用量**。平均線つき、途中の日は薄い色
7. **くわしく**。R相/T相の電流、積算の買電・売電、メーター値の時刻、稼働時間
8. **近くの BLE 機器**。種類ごとの台数と24時間の推移（「BLE 機器のモニタリング」参照）

グラフは触れる（または矢印キー）と値が出て、「表で見る」で数表にもなります。
ダーク／ライトはスマホの設定に従います。外部ライブラリは使わず、ページ1枚
（SVG と素の JavaScript）で完結しています。ブラウザの「ホーム画面に追加」用の
アイコンも配ります。

更新間隔は、最新値10秒、24時間の推移60秒（新しい分だけ取る）、日ごと5分、
BLE 15秒。通信が途切れても直前の表示を薄くして残し、上部に「接続できません」と出します。

## Cube の中身

USB の中身はこうなり、`production_tool` が起動時に Cube へ入れます。

```
USB/
├── CubeJMTS.txt              空ファイル。これがあると Cube が自動実行する
└── production_tool/
    ├── production_tool       ← このリポジトリ（配置と純正設定の退避・無効化）
    ├── meter_hub.py          ← このリポジトリ（MQTT の受け皿 + HTTP サーバー）
    ├── meter_hub.rc          ← このリポジトリ（init サービス定義）
    ├── mqtt_bridge.py        ← このリポジトリ（上流からのフォーク）
    ├── ble_monitor.py        ← このリポジトリ（BLE 機器のモニタリング）
    ├── ble_monitor.rc        ← このリポジトリ（init サービス定義）
    ├── ndblued_disabled.rc   ← このリポジトリ（純正 Bluetooth 常駐を無効化）
    ├── blueye_disabled.rc    ← このリポジトリ（同上）
    ├── led_effect.sh         上流のまま
    ├── mqtt_ha_bridge.rc     上流のまま（mqtt_bridge.py のサービス定義）
    ├── wisund_disabled.rc    上流のまま
    ├── ndeclite_disabled.rc  上流のまま
    ├── config.json           生成
    ├── wpa_supplicant.conf   生成
    └── wifi_probe.sh         任意。probe-wifi.ps1 が置いたときだけ実行（Wi-Fi 調査）
```

Cube 上ではこの3つが init のサービスとして常駐します。プログラムは `/data/local/` に入り、
USB を抜いても動き続けます。

| サービス | プログラム | 役目 |
|---|---|---|
| `mqtt_ha_bridge` | `mqtt_bridge.py` | Wi-SUN でメーターから実測値と30分履歴を取り、MQTT で送る |
| `meter_hub` | `meter_hub.py` | MQTT を受けて保持・保存し、ダッシュボードと API を配る |
| `ble_monitor` | `ble_monitor.py` | Bluetooth で周りの BLE 機器を数え、MQTT で送る |

MQTT は `127.0.0.1` の中だけで完結し、LAN には出ません。

### meter_hub.py

1. `127.0.0.1:11883` で MQTT 3.1.1 のサーバー側を最小限だけ話す
   （CONNECT / PUBLISH / PINGREQ / DISCONNECT のみ）
2. 最新値、1分ごとの値（既定7200件＝5日）、メーターの30分履歴（400日）、
   BLE の台数（28日）を保持し、`/data/local/` に保存する
3. `0.0.0.0:8080` でダッシュボードと JSON API を配る

Python 2.7 の標準ライブラリだけで書いてあり、`BaseHTTPServer` すら使っていません。
この機体で動くと確かめられたモジュールだけを使うためです。

```
GET /                   ダッシュボード (HTML)
GET /api                最新値 (JSON)
GET /api/history        1分ごとの値。?since=TS でそれより新しい分だけ (JSON)
GET /api/daily          30分ごとの履歴、日単位。?days=N (JSON)
GET /api/tariff         電気代のめやすに使う料金表 (JSON)
GET /api/ble            いま見えている BLE 機器と種類ごとの台数 (JSON)
GET /api/ble/history    種類ごとの台数、10分ごと。?days=N (JSON)
GET /healthz            "ok"
```

`/api` 系は CORS を許可しています。アプリはこれを叩いて履歴を貯めます。

### mqtt_bridge.py と上流との差分

`mqtt_bridge.py` は上流（[tsuyopon123/cube-j1-mqtt](https://github.com/tsuyopon123/cube-j1-mqtt)、
固定コミット `3bbb6bf`）から**フォークした、このリポジトリのファイル**です。
上流のサービスは終了していて、追随する利点はありません。
`led_effect.sh` などの上流ファイルも `3bbb6bf` に固定して取得します
（main を追うと、未検証のコードが Cube 上で root 実行されるため）。

上流との違い（すべて実機で見つけたもの）:

- `LED_R` / `LED_G` / `LED_B` を `/dev/null` に向ける（「LED の見かた」参照）
- `skscan()` のスキャン期限を実際の走査時間に合わせる（「つながらないとき」参照）
- スキャン duration の上限を 10 → 8 に下げる（1周回が約11分から3分弱になる）
- ERXUDP が5回連続でタイムアウトしたら再接続する
- 応答を TID と送信元オブジェクトで照合する（メーターの通知を応答と取り違えない）
- メーターの30分履歴（`0xE2`）を取り込んで `meter_hub` へ送る

`production_tool` が上流から変えている点:

- `meter_hub` を bridge より**先に**起動する（bridge が接続先を見つけられるように）
- `config.json` を `chmod 600` にする（認証情報が入っているため）
- 純正の init 設定を上書きする前に `/data/local/stock_*` へ退避する（「元に戻す」参照）
- 純正ファイアウォールを IPv6 にも効くようにし、ペアリング AP 側を塞ぐ（「ネットワークの安全」参照）
- init 設定を書き終えたらルートを読み取り専用（`ro`）に戻す

## 30分ごとの履歴

スマートメーターは積算電力量を**30分ごとに記録**していて、過去の分を問い合わせられます。
ブリッジがこれを取り込み、`meter_hub` が日ごとに保存します。Cube を再起動しても、
過去のグラフは最初から埋まった状態で立ち上がります。

- `0xE5` に「何日前か」を書いてから `0xE2` を読むと、その日の 0:00〜23:30 の積算値が
  48コマまとめて返る。この個体は**今日＋8日＝9日ぶん**。それより古いと記録なし
- 時刻は**日本時間**。Cube の時計は UTC なので、ブリッジは +9時間で日付を決め、
  メーターが日付を切り替える午前0時の前後2分は問い合わせを避ける
- 接続後に9日ぶんを古い順に取り込み（1周回3日ずつ）、以後は30分の境界を90秒過ぎたら
  当日ぶん、日付が変わったら前日と当日を取り直す
- 保存は `/data/local/meter_daily.json`、**最大400日**。同じ日を受け取り直しても、
  既に持っているコマは失わない
- 1分ごとの値は `/data/local/meter_ring.json`。保持数に比例した間隔で丸ごと書き直す
  （7200件なら50分おき）。予期せぬ再起動では最大でその間隔ぶんを失う
- どちらも一時ファイルに書いてから置き換えるので、書き込み中の電源断で壊れない

```json
GET /api/daily?days=9
{"days": [
  {"date": "2026-09-12",
   "cum_kwh":  [58306.1, 58306.6, ...],   // 48コマ、各時刻の積算値。記録なしは null
   "use_kwh":  [0.5, 0.4, ...],           // 48コマ、その30分の使用量
   "total_kwh": 27.2,
   "complete": true}
]}
```

`use_kwh[i]` は `cum_kwh[i+1] - cum_kwh[i]`。最後のコマ（23:30〜24:00）は翌日 0:00 の値が
要るので、翌日を受け取るまで `null` です。`complete` は48コマ揃ったときに `true`。

## 電気代のめやす

30分ごとの使用量から電気代を計算します。料金表は `meter_hub.py` の `DEFAULT_TARIFF` に
内蔵していて、既定は東京電力の**電化上手（7〜10kVA、検針日 22日）** です
（2026-09-13 に公式ページで確認）。

| 項目 | 値（税込） |
|---|---|
| 基本料金 | 2,457.50 円/月 |
| 昼間 10〜17時 | 夏季（7〜9月）43.93 円/kWh、その他季 40.44 円/kWh |
| 朝晩 7〜10時・17〜23時 | 35.87 円/kWh |
| 夜間 23〜7時 | 28.85 円/kWh |
| 再エネ賦課金 | 4.18 円/kWh（2026年5月分〜2027年4月分） |
| 燃料費調整 | 9月分 ▲10.96、10月分 ▲9.30 円/kWh（国の値引きを含む） |

時間帯は土日祝も同じです。1日の金額は「時間帯ごとの使用量 × 単価」に、
その日の使用量 × (燃料費調整 + 再エネ賦課金) を足したもの。燃料費調整は
検針日のある月を「N月分」とみなして選びます。今月分の見込みは
「基本料金 + 記録のある丸1日の平均 × 検針期間の日数」。実際の請求は月の合計で
端数を処理するので、少しずれます（請求書との突き合わせはまだしていません）。

### 燃料費調整を毎月足す

わからない月は最新の月の値で仮に計算し、内訳に「（N月分の値で仮）」と出します。
新しい月の値は `/data/local/tariff.json` に書けば、再起動なしで反映されます
（ページは1時間ごとに料金表を読み直します）。

```json
{ "fuel_yen_kwh": { "2026-11": -8.00 } }
```

```bash
adb push tariff.json /data/local/tariff.json
```

別のプランなら `basic_yen`・`rates`・`summer_rates`・`hours`（0〜23時の時間帯）・
`meter_day` も同じファイルで変えられます。ファイルが壊れていれば内蔵の値を使います。

## BLE 機器のモニタリング

Cube の Bluetooth（BCM4345C0、BT 4.1）で、周りの機器が出している BLE の電波を
数えます。接続やペアリングはしない、受信専用です。

- 起動時に純正の Bluetooth 常駐（`ndblued` / `blueye`）を止めて `/dev/ttyS0` を握り、
  `brcm_patchram_plus2` でファームウェアを読み込んで `hci0` を作り、LE スキャンを回す
- 30秒ごとに、公開情報（会社ID・サービスUUID・名前）だけで種類を判定して
  MQTT `cubej/<id>/ble/state` へ送る。種類は Apple / Windows / Android/Google /
  Samsung / Meta/Quest / Tile/Tracker / Nature Remo / 名前つき / その他
- **個体は特定しない**。スマホはアドレスを数分ごとに変えるので、同じ機器を続けて
  数えられないこともある。アドレスは「まだ見えているか」の判定にだけ使い、
  ログにも API にも残さない
- 1階に置いた Cube から2階の機器はほぼ見えない（床暖房の層が遮る）
- `stop ble_monitor` すると、自分で純正の常駐を起動し直す

純正の常駐は無効化しますが、`/data/local/stock_ndblued.rc` / `stock_blueye.rc` に
退避してあり、戻せます（「元に戻す」参照）。純正クラウドは終了しているので、
純正の Bluetooth はもともと何もしていません。

## Kindle 用の画面（kindash）

脱獄した Kindle 3 を、天気と電力の1画面の表示機にしています。Cube が15分ごとに
600×800・8bit グレーの PNG を描き、Kindle がそれを取ってきて全画面に出します
（2026-10-25 から）。コードはこのリポジトリではなく、別の kindash リポジトリにあります
（このリポジトリの payload からは入りません）。

- `meter_hub` には手を入れず、4つ目の init サービス `kindash` として動く。
  電力は `127.0.0.1:8080` の `/api`・`/api/history`・`/api/daily` を読むだけ
- 天気（気象庁・Open-Meteo）は HTTPS なので `/system/bin/curl` に任せる。
  Cube の CA が古く Open-Meteo を検証できないため、新しい `cacert.pem` を一緒に置いている
- `GET http://<CubeのIP>:8090/screen.png` で配る。ペアリング AP 側は 8090 も塞いである

```
/data/local/kindash/           本体（kindash.py、文字と天気マークの素材、cacert.pem、ログ）
/system/etc/init/kindash.rc    init サービス定義（644）
```

```sh
adb shell stop kindash     # 止める
adb shell start kindash    # 動かす
adb shell tail /data/local/kindash/kindash.log
```

`ps` には引数が出ないので、`python` のプロセスがどれかは `/proc/<pid>/cmdline` で
見分けます（`meter_hub`・`mqtt_bridge`・`ble_monitor` も同じ `python`）。

## 設定を変える

```powershell
# 設定だけ書き換える（Bルートの本物が届いたとき等。production_tool は再コピーしない）
.\setup-cube-j1.ps1 -ConfigOnly -Drive E

# ポートを変える
.\setup-cube-j1.ps1 -HttpPort 9000 -HubPort 21883

# 1分ごとの値の保持数を変える（既定 7200 = 5日）
.\setup-cube-j1.ps1 -HistorySize 2880

# ネットに出ずローカルのクローンを使う
.\setup-cube-j1.ps1 -SourceDir C:\src\cube-j1-mqtt

# exFAT/NTFS のまま強行する（非推奨）
.\setup-cube-j1.ps1 -SkipFormat
```

### 稼働中の Cube の保持数を変える

USB を作り直さずに済ませたいときは、`config.json` を直接書き換えて `meter_hub` を入れ直します。

```bash
adb connect <CubeのIP>:5555
adb shell "sed -i 's/\"history_size\": *[0-9]*/\"history_size\": 7200/' /data/local/config.json"
adb shell stop meter_hub
adb shell start meter_hub
adb shell tail -3 /data/local/meter_hub.log     # history=7200 ≈5.0d ring_save=3000s
```

7200件でメモリはおよそ 1.8MB。保存間隔を保持数に比例させてあるので、eMMC への
1日あたりの書き込み量はほぼ変わりません。

### Wi-Fi の接続先を変える・足す

**まず、Cube の置き場所から何が見えるかを調べる**（接続は変えません）:

```powershell
.\probe-wifi.ps1 -Ssid <SSID>[,<SSID>]   # USB に仕込み、Cube を USB 付きで起動
.\probe-wifi.ps1 -Result                  # USB を PC に戻して結果を読む
.\probe-wifi.ps1 -Remove                  # 片付け
```

SSID を隠した AP も名前を指定して探すので見つかります。結果には周波数と
信号強度（dBm）が出ます。SSID は USB 上の `wifi_probe_ssid.txt` にだけ書かれます。

**接続先を足す**（今の設定は残し、優先度の高いものから試す）:

```powershell
.\set-wifi.ps1 -Add -Priority 20   # SSID とパスワードを聞かれる
```

`-Add` を付けなければ、接続先をその1つに置き換えます。

**電波は強いところを選んでください。** -80 dBm の AP につながっていた間は、
ping が数%落ち、HTTP が数秒止まり、adb がすぐ `offline` になりました。
-57 dBm の AP に替えたら全部消えました。

この Cube の `wpa_cli` は命令がタイムアウトすることがあり、設定の読み込み直しが
効かない場合があります。切り替わらなければ USB を挿したまま電源を入れ直してください。

## LED の見かた

純正ファームの意味表（`/system/etc/init/led_state.rc`）のとおりです。

| 状態 | LED |
|---|---|
| 起動中 | 青の点滅 |
| 起動完了 / ネットワーク切断 | 青の点灯 |
| ネットワーク接続済み | **緑の点灯**（通常運転） |
| リセット待ち | 緑と赤の点滅 |
| リセット中 / 更新失敗 | 赤の点灯 |
| ファーム更新中 | 青と赤の点滅 |

上流の `mqtt_bridge.py` は SKSTACK コマンドを送るたびに LED を緑⇄青で点滅させ、
しかも消灯フェーズを保存して消えたまま戻すことがありました。このリポジトリの版は
LED に触れないので、上の表の表示がそのまま見えます。

## ネットワークの安全

### 決めたこと

| 項目 | 状態 |
|---|---|
| LAN 側の adb（5555、認証なし、root） | **開けたまま**（2026-09-05 オーナー判断。自宅LAN を信頼する） |
| 純正の mosquitto（8883）と nginx（80/443）の LAN 側 | **そのまま**（2026-09-14 オーナー判断。実害はない） |
| ペアリング AP（`CubeJ-<MAC下6桁>`）側の全待受ポート | **塞いである**（IPv4/IPv6 とも） |
| ダッシュボード | 平文 HTTP。家庭内 LAN だけで使う前提 |
| Kindle 用の画面（kindash、8090）の LAN 側 | 開けたまま。平文 HTTP で PNG を配るだけ |

LAN 側の adb が開いているので、自宅LAN に入れる人は root シェルを取れ、
Wi-Fi のパスワードと Bルート認証情報が読めます。塞ぐと管理用の adb も閉じ、
中を見るたびに USB を作り直すことになるため、開けたままにしています。

### ペアリング AP を塞いでいる仕組み

純正クラウドに紐づいていないため、Cube はペアリング用の Wi-Fi Direct AP を
約3分ごとに作り直し続けます。パスフレーズは機種共通の既定値で、つないだ相手には
adb まで届くので、電波の届く範囲の第三者が root を取れる経路でした。

純正のインターフェース別ファイアウォール（`/usr/sbin/iptables_firewall.sh` が
`/system/etc/iptables_firewall.conf` の `POLICY` を読む、毎起動の oneshot）で塞いでいます。

```
POLICY="p2p+:5555,p2p+:8080,p2p+:8883,p2p+:80,p2p+:443,p2p+:8090"
```

8090 は Kindle 用の画面（kindash、下の「Kindle 用の画面」）です。動かしていない Cube でも
入れておくので、セットアップをやり直しても開きません。

純正スクリプトは `iptables` しか叩かず、IPv6 で丸ごと迂回できたので、同じ規則を
`ip6tables` にも入れる2行を純正スクリプトに足しています（退避した純正を常に入力にするので、
再実行しても積み重なりません）。再起動をまたいで IPv4 6本 / IPv6 6本が入っていることを
確認済みです（2026-10-25、8090 を足したあと）。AP の電波自体は出続けます。

### gndctrl は無効化しない

AP を作っている `gndctrl` を止めたくなりますが、**無効化してはいけません。**
ネットワーク管理（Wi-Fi 接続後の DHCP）、USB 自動実行、リセットボタンの検出を
兼ねていて、止めると**復旧経路が3つ同時に消えます**。実際に一度、Cube が LAN から消えました。
そのときの戻し方は [recovery/README.md](recovery/README.md)。

**IPv4 で見つからなくても死んだとは限りません。** IPv6 のアドレスは DHCP なしで付き、
MAC から計算できます（例: `6c:21:a2:00:11:22` → 下位64ビット `6e21:a2ff:fe00:1122`）。

```sh
ping -6 fe80::6e21:a2ff:fe00:1122%<IF>
adb connect [<ルータのprefix>:6e21:a2ff:fe00:1122]:5555
```

## つながらないとき（no PAN found）

ログに `SKSCAN no PAN found` が並ぶとき、**距離や開通状態を疑う前に、手でスキャンしてください。**
2026-09-13、その二つを疑って半日溶かしました（原因は上流のスキャン期限のバグで、修正済み）。

ブリッジを止めてから `/dev/ttyS1` を 115200 8N1 で開き、こう送って**60秒以上待ちます**。

```
SKRESET
SKSETPWD C <12桁のパスワード>      → OK（C は桁数の16進）
SKSETRBID <32桁のID>               → OK
SKSCAN 2 FFFFFFFF 6 0
```

応答があればこう返ります。

```
EPANDESC
  Channel:2F      メーターのチャンネル
  LQI:93          20〜30以下だと電波が弱すぎる
  PairID:xxxxxxxx BルートID末尾8桁と一致するはず
EVENT 22 ...
```

- `EPANDESC` が出る → 電波は届いている。ブリッジ側を疑う
- `EVENT 22` だけ → ここで初めて電波か開通状態を疑う
- `SKSETPWD` / `SKSETRBID` が `FAIL` → 認証情報の誤り
- メーターは Pairing ID が一致する相手にしか応えない。ダミーIDの間は見つからないのが正常

ほかの手がかり:

- `SKSCAN 0 FFFFFFFF 4 0`（ED スキャン）で 33〜60ch の電波強度が並べば受信系は正常
  （dBm ≒ `0.275 × 値 − 104.27`）
- `SKJOIN: connected` の後に `No ERXUDP response (timeout)` だけが続くなら、認証は通るが
  往復が通らない際どい電波。ブリッジは5回で再接続するが、繰り返すなら置き場所が足りない
- duration はこのファームでは 10 が上限（11 以上は `FAIL ER06`）

設置場所は、メーターと壁1枚の1階の部屋だけが実用になりました。2階は一度参加できても、
その後のデータは全滅しました。

## 元に戻す

初回実行時に、純正の設定を上書きする前に `/data/local/` へ退避します。2回目以降は
既存の退避を温存します（無効化済みの版で潰さないため）。

```
/data/local/stock_wisund.rc                 純正の wisund.rc
/data/local/stock_ndeclite_agent.rc         純正の ndeclite_agent.rc
/data/local/stock_ndblued.rc                純正の ndblued.rc
/data/local/stock_blueye.rc                 純正の blueye.rc
/data/local/stock_gndctrl.rc                純正の gndctrl.rc（触らないが念のため）
/data/local/stock_iptables_firewall.conf    純正のファイアウォール設定
/data/local/stock_iptables_firewall.sh      純正のファイアウォールスクリプト
```

書き戻すときは adb から:

```sh
adb connect <CubeのIP>:5555
adb shell stop ble_monitor          # 純正の Bluetooth 常駐が起動し直す
adb shell mount -o rw,remount /
adb shell cp /data/local/stock_wisund.rc /system/etc/init/wisund.rc
adb shell cp /data/local/stock_ndeclite_agent.rc /system/etc/init/ndeclite_agent.rc
adb shell cp /data/local/stock_ndblued.rc /system/etc/init/ndblued.rc
adb shell cp /data/local/stock_blueye.rc /system/etc/init/blueye.rc
adb shell cp /data/local/stock_iptables_firewall.conf /system/etc/iptables_firewall.conf
adb shell cp /data/local/stock_iptables_firewall.sh /usr/sbin/iptables_firewall.sh
adb shell rm /system/etc/init/meter_hub.rc /system/etc/init/mqtt_ha_bridge.rc /system/etc/init/ble_monitor.rc
adb shell rm -f /system/etc/init/kindash.rc       # Kindle 用の画面（入れている場合）
adb shell mount -o ro,remount /
adb shell setprop persist.adb.tcp.port ""     # adb の常時開放をやめる
adb reboot
```

退避が無い場合でも、純正の rc は無効化版から `disabled` の1行を除いたものです
（実機の退避で確認済み）。

## 前提と注意

- **Bルート利用申請が必要。** 電力会社へ申請し、ID はメール、パスワードは郵送で1〜2週間
- **config.json に Bルート認証情報が平文で入る。** USB 側と Cube 内（`/data/local/config.json`、600）
  の両方。セットアップ後は USB を消すか施錠保管
- **純正の wisund / NDEcLiteAgent / ndblued / blueye は無効化される。** 純正クラウドは
  2025年3月末で終了済み。戻し方は「元に戻す」
- **ファームウェア更新はもう来ない。** カーネルは古く、脆弱性が出ても修正されない
- 実行は自己責任で。上流リポジトリの警告も参照してください

動作要件: Windows 10 / 11、PowerShell 5.1（標準搭載）。追加インストール不要。

## クレジット

Cube J1 上で動く Wi-SUN / ECHONET Lite / MQTT ブリッジの元は
[tsuyopon123/cube-j1-mqtt](https://github.com/tsuyopon123/cube-j1-mqtt)（MIT）。
root 化の仕組みの解説は
[NextDrive Cube J1を分解せずにrootを取りたい！](https://zenn.dev/tsuyopon123/articles/cube-j1-root)。
