# 復旧用 USB

`gndctrl` を無効化したせいで Cube が Wi-Fi に接続できなくなった場合に、
`/data/local/` の退避ファイルから純正の状態へ書き戻すための USB。

ネットワークが死んでいても USB 自動実行なら届く、というのが要点。

## 作り方

1. Cube の電源を抜く
2. Cube から USB を抜いて PC に挿す
3. USB の `production_tool\production_tool` を、このフォルダの同名ファイルで置き換える
   （`CubeJMTS.txt` と他のファイルはそのまま。触らない）
4. USB を Cube に挿して電源投入
5. 白LEDが3回点滅したら完了。緑点灯になれば Wi-Fi 接続成功

## 何をするか

- adb を 5555 で開ける（復旧後に手を入れられるように）
- `/data/local/stock_gndctrl.rc` を `/system/etc/init/gndctrl.rc` へ書き戻す
- `/data/local/stock_iptables_firewall.conf` を `/system/etc/` へ書き戻す
- `gndctrl` を起動する

`meter_hub` と `mqtt_bridge` には触れないので、電力モニタ側の設定は保たれる。

## 退避ファイルが無い場合

`[ -s ... ]` で存在確認しているので、無ければ何もせず素通りする。
その場合は純正 `gndctrl.rc` を手で書く（無効化版から `disabled` の1行を除いたもの）:

```
service gndctrl /usr/sbin/gndctrl
    class late_start
    user root
    group root
```
