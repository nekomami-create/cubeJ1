# 引き継ぎ: Cube J1 ローカル電力モニタ

このリポジトリは `nekomami-create/cubeJ1`（public、既定ブランチ `main`）。
サービス終了した NextDrive Cube J1 でスマートメーターを読み、Cube 自身が
HTTP で答える。Home Assistant も MQTT ブローカーも常時稼働サーバーも使わない。

この文書は**なぜそうなったか**と**現在地**。仕様と使い方は `README.md`、
JSON API の契約は `docs/api.md`。

## ファイルの地図

```
setup-cube-j1.bat   入口。UAC 昇格して .ps1 を呼ぶだけ
setup-cube-j1.ps1   USB 作成本体。対話で4項目聞いて config を生成し検証する
payload/            USB に置かれ、Cube 上で実行されるもの
  production_tool   起動スクリプト。上流からのフォーク（差分は README）
  meter_hub.py      MQTT 受け皿 + HTTP サーバー。このプロジェクトの本体。
                    ホーム画面用アイコン3枚とマニフェストも中に埋め込んである
  meter_hub.rc      init サービス定義
find-cube.ps1       LAN を舐めて /healthz が ok を返すホストを探す
recovery/           gndctrl を壊したときの復旧 USB（recovery/README.md）
docs/api.md         JSON API の契約。Android を書くときはここを見る
```

上流 `tsuyopon123/cube-j1-mqtt` の `mqtt_bridge.py` は**1行も変更していない**。
コミット `3bbb6bf` に固定して取得する。改変はすべて `production_tool` 側で、
デプロイ後のコピーに対して行う。

## オーナーの目的（これがスコープの境界）

「今この瞬間、家で何W使っているか」をスマホで見たい。それだけ。

- 長期の蓄積・集計は**電力会社のWebサービス**（くらしTEPCO web 等）に任せる。
  自前で貯める必要はない。Cube 側は直近24時間だけ持てば足りる。
- **家庭内 LAN からのみ**見られればいい。外出先からは見ない。
- 家電の自動化やスマートホーム化には関心がない。

この3点から外れる提案（長期DB、外部公開、自動化基盤）は、頼まれない限り
持ち込まないこと。

## 検討して却下した選択肢と、その理由

| 案 | 却下理由 |
|---|---|
| Home Assistant を導入 | 目的に対して重すぎる。24時間動くPCが1台必要になる |
| Mosquitto を別途立てる | 同上。ブローカーを外に置く必然性がない |
| GPD Pocket 2 を HA 専用機にする | 上が不要になったので前提ごと消滅 |
| Proxmox で汎用サーバー化 | 同上。Pocket 2 は eMMC / USBポート / CPU の面で汎用向きでもない |
| VPS・クラウドに置く | Cube は MQTT を平文でしか喋れず、TLS も VPN も入れる手段がない。 |
|                     | そもそも Wi-SUN は物理的に自宅のメーターと通信するので移設不可 |
| スマホ上でブローカーを動かす | Android が背景プロセスを殺すため常時稼働できない |
| 市販の Nature Remo E lite を買う | 有力な代替として提示済み。オーナーは Cube 活用を選んだ |
| adb を wlan0 でも遮断する | 自分も入れなくなり、中を見るたび USB 作り直しになる |
| Cube を IoT用VLAN / ゲストネットに隔離 | ダッシュボードがそちらからしか見られなくなる |

結論として **Cube 自身が HTTP で答える**構成にした。24時間動く必要があるのは
Cube だけになり、外部依存がゼロになった。

## 現在の状態（2026-09-13 時点）

- 実装、ローカル検証、実機での通電・Wi-Fi 接続・Web サーバ疎通まで到達済み。
  **残っているのは Bルートの本番接続ただ一つ**。ここが通れば完成
- Bルート利用申請は 2026-09-05 の時点で**オーナーが提出済み**。認証情報の
  到着待ち（ID はメールで数日、パスワードは郵送で1〜2週間）。申請から
  8日経っているので、そろそろ届く頃

### 別リポジトリに古いコピーがある

`nekomami-create/secretary` の `tools/cube-j1/` が、このリポジトリの切り出し元。
`meter_hub.py` と `setup-cube-j1.*` は同一だが、`production_tool` は**古い**
（LED 対処・純正 rc 退避・ファイアウォールのパッチがすべて入っていない）。
`find-cube.ps1` と `recovery/` も無い。**参照しないこと。** 削除してよいが
オーナーの判断待ちで残してある。
- 認証情報が揃う前でも USB は作れる。スクリプトは空入力を拒むだけなので、
  ダミー（英数32文字 / 12文字）で通る。`meter_hub.py` は `br_id`/`br_pwd` を
  一切読まないため、Webサーバの疎通確認はダミーのまま可能。ブリッジだけが
  認証に失敗し、`/api` の電力値が null のままになる。本物が届いたら
  `-ConfigOnly -Drive <letter>` で設定だけ差し替える

### 検証済み

- 上流クライアントと同一のバイト列（CONNECT / PUBLISH QoS0 / PINGREQ /
  DISCONNECT）を投げ、CONNACK・PINGRESP・値の取り込み・履歴蓄積を確認
- HA auto-discovery の retain メッセージを無視することを確認
- ダッシュボードを実ブラウザ 390x844 で描画。グラフ、切断時表示、
  コンソールエラーなしを確認
- ログ切り詰めが行境界で切れること、冪等であることを確認
- `production_tool` の `sh -n` 構文チェック
- 途中で発見・修正したバグ: `Content-Length` を文字数で計算しており、
  UTF-8 の日本語ページがブラウザで切り詰められていた

### ローカル Windows で追加検証（2026-09-05）

- `setup-cube-j1.ps1` を実機の PowerShell 5.1.26100 で構文解析。エラー0
- ps1 に UTF-8 BOM あり。BOM 無しだと PS 5.1 が ANSI 扱いして日本語が壊れ、
  パースが落ちる（find-cube.ps1 を書いたとき実際に踏んだ）。追加時は BOM 必須
- `.bat` は CRLF 28/28
- `core.autocrlf=true` の環境でも `.gitattributes` の `* -text` が効き、
  payload 3ファイルの CR バイトは 0
- `ConvertTo-WpaString` のエスケープと入力バリデータ4種を境界値で確認
- config.json 生成→検証の往復（CR なし / BOM なし / mqtt_port が Int32）
- 上流の固定コミット `3bbb6bf` は到達可能で、必要な5ファイルすべて存在
- 純正 rc 退避のガードを偽ツリーで2回実行し、冪等性と復元一致を確認

### 実機で確認（2026-09-05、ダミー認証で通電・Wi-Fi 接続まで）

- Webサーバ疎通。`/healthz`=ok、`/`=7386文字、`<title>電力モニタ</title>`
- `/` は `ro` に戻っている。`/data` は 12G 中 42M 使用
- `0.0.0.0:8080` と `127.0.0.1:11883` が LISTEN。MQTT は LAN に出ていない
- Wi-SUN の実通信: `SKSCAN` が発行され応答が返る。`/dev/ttyS1` は開けている。
  `no PAN found` はメーター側の Bルートが未開通のためで、認証失敗ではない
- 純正 rc の退避が動作。中身は `disabled` を除いた純正そのものだった
  （README に「推測」と書いていたが、実物で確定した）
- USB を挿したままだと起動のたびに production_tool が再実行される。
  init が起動+5秒で meter_hub を自動起動しており、USB 無しでも動く

### LED の緑青点滅の原因（2026-09-05）

**上流 `mqtt_bridge.py` の仕業**で、純正ファームではない。`skcommand` と
`SKJOIN` が SKSTACK コマンド送出中ずっと緑⇄青を 200ms で点滅させる。
スキャン再試行のため約115秒周期・約10秒のバーストが延々と続いていた。
同関数の保存・復元が消灯フェーズを掴むと LED が消えたままにもなる。

調査中に2度、純正ファーム側の表示だと誤って結論づけた。`led_effect.sh` が
白しか出さないことだけ見て、上流ブリッジの LED 処理を見落としたため。
対処は `LED_R/G/B` を `/dev/null` に向ける3行のみ。README「LED の見かた」参照。

LED の公式な意味表は `/system/etc/init/led_state.rc` にある。

### 未検証・未確定（ここだけが残り）

上の「実機で確認」で、`/` の ro 復帰・`/data` の空き・ポートの占有は
すべて確定済み。まだ残っているのは以下。

- **Bルートの本番接続**。ダミー認証で `SKSCAN` の発行と応答までは見えている
  が、本物の認証情報で PANA 接続し `/api` に電力値が出るところは未到達。
  `no PAN found` はメーター側が未開通なためで、コードの失敗ではない
- **USB 作成の通し実行**。`setup-cube-j1.bat` を最初から最後まで走らせた
  実績がまだない（構文と各部品はローカル検証済み、認証情報待ち）
- **`meter_fw.rc` が init に登録されなかった件**。原因未特定のまま、純正
  `iptables_firewall.sh` を patch する方式へ切り替えて回避している。
  **再起動をまたいで ip6tables の規則が残るか、毎回確認すること**
- **Android アプリ**。未着手

## 再現テスト手順

`meter_hub.py` は Python 2.7 向けだが、以下の置換で py3 でも動く。
これで実機なしにプロトコルと画面を検証できる。

```
CONFIG_PATH / LOG_PATH / BRIDGE_LOG を一時パスに
data = ""            -> data = b""
"\r\n\r\n" not in    -> b"\r\n\r\n" not in
data.split("\r\n",1) -> data.split(b"\r\n",1)[0].decode("latin-1")
cast(payload.strip()) -> bytes なら decode してから cast
```

その上で、上流の `_encode_remaining` / `_encode_str` / `_make_pkt` を
そのまま写したクライアントから CONNECT・PUBLISH・PINGREQ を投げる。

## ロードマップ

```
[済] USB 作成スクリプトと meter_hub.py
[済] 実機で通電・Wi-Fi・Web サーバ疎通（ダミー認証）
[済] LED、P2P AP、純正 rc 退避、IPv6 復旧経路
[待] Bルート認証情報の到着（申請済み、郵送待ち）
[次] 本物の認証情報で -ConfigOnly して USB を作り直し、実測値を出す
[次] 実測を見ながらダッシュボード調整（閾値の色分けは 1.2kW / 3kW 固定）
[済] ホーム画面アイコン（マニフェスト + PNG を meter_hub に埋め込み）
[後] Android APK。ウィジェット常時表示、閾値通知、グラフ、全画面表示
```

Android で Chrome のアドレスバーが消えないのは仕様。HTTPS でないと
Chrome が PWA のインストールを許さないため。全画面が欲しくなったら
WebView をかぶせた APK を作る。それ以外の理由で HTTPS 化を検討しないこと
（家庭内 LAN 専用であり、自己署名証明書は警告が出て体験が悪化する）。

認証情報が届いたときの最短手順:

```powershell
.\setup-cube-j1.ps1 -ConfigOnly -Drive <letter>
```

USB を Cube に挿して電源投入。PAN スキャンに数分かかる。`/api` の
`power_w` が出れば完了。出なければ `adb shell cat /data/local/mqtt_bridge.log`。

### 実機がないとできないこと

`adb connect <IP>:5555` を使う調査、USB の作成と挿入、LED の目視確認。
リモート環境のセッションではこれらができないので、そこに踏み込む作業は
ローカルで開いたセッションに回すこと。

## gndctrl は無効化してはいけない（2026-09-05 の事故）

P2P ペアリング AP を消す目的で `gndctrl` を無効化して再起動したところ、
**Cube が LAN から消えた**。原因は gndctrl が単なる LED/検出の常駐ではなく、
この機体のほぼ全部を握っているため。分かった範囲でこれだけ兼ねている:

- **ネットワーク管理**（ログのタグが `NM`）。Wi-Fi アソシエート完了を受けて
  DHCP クライアント `udhcpc` を起動する。無効だと IPv4 が永久に取れない
- **USB 自動実行の起動**。`CubeJMTS.txt` を見て production test mode に入る
  のも gndctrl。無効だと **USB からの復旧が効かなくなる**
- **物理リセットボタンの検出**。無効だと長押しに反応しない
- P2P ペアリング AP の作成（約3分ごとに作り直す）
- LED の uevent 監視と `led.control.state` の設定

つまり無効化すると、**Wi-Fi・USB・リセットボタンという復旧経路が同時に死ぬ**。
退避ファイルを用意していても、それを読ませる手段が無くなる。

### 助かった経路: IPv6

IPv4 は DHCP が動かず取れなかったが、**IPv6 の SLAAC は DHCP を必要としない**。
Wi-Fi にアソシエートさえしていればアドレスは付く。アドレスは MAC から EUI-64 で
決まるので、事前に知らなくても計算できる（例: `6c:21:a2:00:11:22` →
`fe80::6e21:a2ff:fe00:1122`、グローバルは同じ下位64ビットにルータの prefix）。

```
ping -6 <prefix>:6e21:a2ff:fe00:1122
adb connect [<prefix>:6e21:a2ff:fe00:1122]:5555
```

これで root shell に入り、`stock_gndctrl.rc` を書き戻して復旧した。
gndctrl を起動しただけでは DHCP が走らず、
`wpa_cli -p /data/misc/wifi/sockets -i wlan0 reassociate` で
アソシエーション完了イベントを作り直す必要があった。

**IPv4 で見つからなくても死んだと判断しないこと。** 先に IPv6 を試す。

### P2P AP への対処は無効化ではなくファイアウォール

**IPv4 だけでは足りない。** adb は `:::5555` で待ち、P2P AP に繋いだ相手には
IPv6 リンクローカルが配られるので、`iptables` の規則は迂回できる。純正の
`iptables_firewall.sh` は `ip6tables` を一切叩かないため、同じ POLICY を
両系統へ流すよう純正スクリプトに2行足してある（純正は
`/data/local/stock_iptables_firewall.sh` に退避）。

最初は独自の oneshot サービス（meter_fw.rc）でやろうとしたが、init が
その rc を登録せず（`init.svc.meter_fw` が空）、再起動後に IPv6 側だけ
規則が消えた。原因は特定できていない。**再起動をまたいで確認すること。**

純正の `/usr/sbin/iptables_firewall.sh` が `/system/etc/iptables_firewall.conf` の
`POLICY="netif:port,..."` を読んでインターフェース単位で REJECT する。
毎起動走る oneshot なので USB を抜いても効く。`POLICY="p2p+:5555"` で
AP 側の adb だけを塞ぐ。gndctrl は動かしたままなので Wi-Fi は無事。

## LAN 側 adb は「開けたまま」でよい（2026-09-05 オーナー判断）

adb は `:::5555` で待っており、自宅LAN に入れる人は誰でも root を取れる。
そこから Wi-Fi の PSK と Bルート認証情報が読める。上流の仕様。

選択肢（現状維持 / VLAN隔離 / `POLICY` に `wlan0:5555` 追加）を代償つきで
提示したうえで、**オーナーは「現状のまま。自宅LAN を信頼する」を選んだ。**

**これは未対応の宿題ではなく、決定事項。** 頼まれない限り塞ぎに行かないこと。
塞ぐと管理用の adb も同時に閉じ、中を見るたび USB を作り直すことになる。

なお P2P ペアリング AP 側は別問題として塞いである（IPv4/IPv6 とも）。
こちらは自宅LAN の外、電波の届く範囲の第三者が対象なので判断が異なる。

## 既知のリスクと、対応状況

| リスク | 状態 |
|---|---|
| adb が 5555 で認証なしに開く（LAN側） | **許容**（2026-09-05 オーナー判断）。下の却下表を参照 |
| 未設定ゆえ P2P AP が常時起動 | 対応済み（POLICY で全待受ポートを REJECT。IPv4/IPv6 とも） |
| gndctrl の無効化 | **禁止**。Wi-Fi・USB自動実行・リセットボタンが同時に死ぬ（上の節） |
| ルートFSが rw のまま | 対応済み（init RC 書き込み後に ro へ戻す） |
| 純正 init RC が戻せない | 対応済み（上書き前に /data/local へ退避。README「元に戻す」） |
| ログの無限増殖 | 対応済み（hub が5分おきに 1MB で切り詰め） |
| config.json の権限 | 対応済み（chmod 600） |
| 上流 main を毎回取得 | 対応済み（コミット 3bbb6bf に固定） |
| USB 上に認証情報が平文で残る | **運用でカバー**。README に記載 |
| MQTT が平文 | 解消（127.0.0.1 で完結し LAN に出ない） |

## オーナーとのやり取りで気をつけること

- 専門用語をそのまま並べない。過去に HA / MQTT / Mosquitto を略語のまま
  使って一度混乱させている。初出の語は一行で説明する
- 目的に対して過剰な構成を提案しない。上の却下リストを先に確認する
- 未検証のことを「動きます」と言わない。実機がないと確かめられない部分は
  そう明示する
