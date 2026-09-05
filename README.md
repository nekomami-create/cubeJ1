# Cube J1 → Home Assistant セットアップ USB 作成ツール

NextDrive Cube J1 を Home Assistant の MQTT デバイスとして使うための
セットアップ USB を、Windows 上で一発で作るスクリプト。

実際に Cube 上で動くブリッジ本体は
[tsuyopon123/cube-j1-mqtt](https://github.com/tsuyopon123/cube-j1-mqtt)（MIT）。
このツールはそれを取得して設定を流し込み、USB に正しく配置するだけ。

## 使い方

`setup-cube-j1.bat` をダブルクリック。管理者権限を求められたら許可する
（FAT32 フォーマットに必要。既に FAT32 の USB を使うなら拒否しても動く）。

対話で聞かれるのは以下。

| 項目 | 補足 |
|---|---|
| Bルート認証ID | 電力会社発行、英数字32文字 |
| Bルートパスワード | 12文字。入力は伏せ字 |
| Wi-Fi SSID / パスワード | 2.4GHz / WPA-PSK |
| MQTT ホスト / ポート | Home Assistant の IP、既定 1883 |
| MQTT ユーザー / パスワード | 未設定なら Enter で空 |
| device_id / ポーリング間隔 | 既定 `cubej1` / 60秒 |

終わったら USB を安全に取り外して Cube J1 に挿し、電源投入。
白 LED 10回点滅 = セットアップ完了、緑点灯 = Wi-Fi 接続成功。

## オプション

```powershell
# 設定だけ書き換える（production_tool は再コピーしない）
.\setup-cube-j1.ps1 -ConfigOnly -Drive E

# ネットに出ずローカルのクローンを使う
.\setup-cube-j1.ps1 -SourceDir C:\src\cube-j1-mqtt

# exFAT/NTFS のまま強行する（非推奨）
.\setup-cube-j1.ps1 -SkipFormat
```

## このスクリプトがやること

1. cube-j1-mqtt を ZIP で取得（`-SourceDir` でローカル指定も可）
2. 入力値を検証して `config.json` を生成（JSON エスケープは `ConvertTo-Json` 任せ）
3. `wpa_supplicant.conf` を生成（SSID / PSK 内の `\` `"` はエスケープ）
4. 両ファイルを **LF + UTF-8 BOMなし** で書き出す（CRLF だと Cube 側で壊れる）
5. リムーバブルドライブを一覧表示し、二重確認のうえ選択
6. FAT32 でなければ確認のうえフォーマット（32GB 以下のみ）
7. `CubeJMTS.txt` と `production_tool/` をルート直下へ配置
8. ファイル存在・CR混入・BOM・JSON妥当性・FS種別を検証

## やらないこと

- Bルート利用申請（電力会社。ID はメール、パスワードは郵送で1〜2週間かかる）
- Home Assistant 側の Mosquitto broker / MQTT 統合の導入
- Cube への USB 挿入と電源投入
- 32GB 超の USB の FAT32 化（Windows 標準機能の制約。diskpart 等で事前に）

## 注意

- `config.json` には Bルート認証情報と MQTT パスワードが **平文** で入る。USB の保管に注意。
- セットアップ後、Cube は **adb を 5555 で開放** する。LAN 内の誰でも root shell を取れるので、
  IoT 用 VLAN に隔離するのが望ましい。
- Cube 純正の `wisund` / `NDEcLiteAgent` は停止・無効化される（Wi-SUN の `/dev/ttyS1` 競合のため）。
  純正クラウドは 2025年3月末で終了済みなので実害は小さいが、元に戻すのは手間。
- 実行は自己責任。上流リポジトリの警告も参照のこと。

## 動作要件

Windows 10 / 11、PowerShell 5.1（標準搭載）。追加インストール不要。
