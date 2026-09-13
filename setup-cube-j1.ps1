#Requires -Version 5.1
<#
    Cube J1 ローカル電力モニタ用 セットアップ USB 作成スクリプト

    出来上がるもの:
      Cube J1 が自宅Wi-Fiにつながり、スマートメーターから電力を読み、
      本体上の小さなWebサーバー (http://<CubeのIP>:8080) で公開する。
      Home Assistant も MQTTブローカーも常時稼働サーバーも不要。

    構成:
      mqtt_bridge.py (このリポジトリ) --MQTT--> meter_hub.py --HTTP--> スマホ
                              127.0.0.1:11883          0.0.0.0:8080

    やらないこと: Bルート開通申請、Cube への挿入と電源投入。
#>
[CmdletBinding()]
param(
    [string]$Drive,
    [string]$SourceDir,
    [switch]$ConfigOnly,
    [switch]$SkipFormat,
    [int]$HttpPort    = 8080,
    [int]$HubPort     = 11883,
    [string]$DeviceId = 'cubej1',
    [int]$HistorySize = 1440
)

$ErrorActionPreference = 'Stop'

# 検証済みのコミットに固定する。main を追いかけると、上流が変わった瞬間に
# 見知らぬコードが Cube 上で root 実行されることになるため。
$UpstreamCommit = '3bbb6bfe00975f7a854fa8fa8aaa57276c888ae3'
$RepoZipUrl     = "https://codeload.github.com/tsuyopon123/cube-j1-mqtt/zip/$UpstreamCommit"
$WorkRoot       = Join-Path $env:TEMP 'cube-j1-setup'
$PayloadDir     = Join-Path $PSScriptRoot 'payload'

# 上流からそのまま使うファイル
$FromUpstream = @(
    'led_effect.sh',
    'mqtt_ha_bridge.rc',
    'wisund_disabled.rc',
    'ndeclite_disabled.rc'
)
# こちらで用意するファイル（production_tool は上流を差し替える）
$FromPayload = @('production_tool', 'meter_hub.py', 'meter_hub.rc', 'mqtt_bridge.py')

# ---- 表示ヘルパ -------------------------------------------------------------
function Say {
    param([string]$Message, [string]$Color = 'Gray')
    Write-Host $Message -ForegroundColor $Color
}
function Head {
    param([string]$Message)
    Write-Host ''
    Write-Host ("== {0} {1}" -f $Message, ('-' * 8)) -ForegroundColor Cyan
}
function Fail {
    param([string]$Message)
    Write-Host ''
    Write-Host "[中止] $Message" -ForegroundColor Red
    exit 1
}
function Ok {
    param([string]$Message)
    Write-Host "  [OK] $Message" -ForegroundColor Green
}

# ---- 入力ヘルパ -------------------------------------------------------------
function Read-Value {
    param(
        [string]$Label,
        [string]$Default = '',
        [scriptblock]$Validate = $null,
        [string]$Hint = '',
        [switch]$AllowEmpty
    )
    while ($true) {
        $prompt = $Label
        if ($Default) { $prompt = "$Label [$Default]" }
        $v = Read-Host $prompt
        if (-not $v -and $Default) { $v = $Default }
        if (-not $v -and -not $AllowEmpty) {
            Say '  空にはできません。' 'Yellow'
            continue
        }
        if ($v -and $Validate -and -not (& $Validate $v)) {
            Say "  入力が不正です。$Hint" 'Yellow'
            if ((Read-Host '  このまま使いますか? (y/N)') -eq 'y') { return $v }
            continue
        }
        return $v
    }
}

function Read-Secret {
    param([string]$Label, [switch]$AllowEmpty, [scriptblock]$Validate = $null, [string]$Hint = '')
    while ($true) {
        $sec = Read-Host $Label -AsSecureString
        $ptr = [Runtime.InteropServices.Marshal]::SecureStringToBSTR($sec)
        try   { $v = [Runtime.InteropServices.Marshal]::PtrToStringBSTR($ptr) }
        finally { [Runtime.InteropServices.Marshal]::ZeroFreeBSTR($ptr) }
        if (-not $v -and -not $AllowEmpty) {
            Say '  空にはできません。' 'Yellow'
            continue
        }
        if ($v -and $Validate -and -not (& $Validate $v)) {
            Say "  入力が不正です。$Hint" 'Yellow'
            if ((Read-Host '  このまま使いますか? (y/N)') -eq 'y') { return $v }
            continue
        }
        return $v
    }
}

# ---- ファイル出力（LF / UTF-8 BOMなし）--------------------------------------
function Write-LfFile {
    param([string]$Path, [string]$Text)
    $lf = $Text -replace "`r`n", "`n"
    [IO.File]::WriteAllText($Path, $lf, (New-Object Text.UTF8Encoding($false)))
}

# wpa_supplicant のダブルクォート文字列内エスケープ（\ を先に、次に "）
function ConvertTo-WpaString {
    param([string]$Value)
    $escaped = $Value -replace '\\', '\\'
    $escaped -replace '"', '\"'
}

function Test-Admin {
    $id = [Security.Principal.WindowsIdentity]::GetCurrent()
    (New-Object Security.Principal.WindowsPrincipal($id)).IsInRole(
        [Security.Principal.WindowsBuiltInRole]::Administrator)
}

$ValidBrId  = { param($v) $v -match '^[0-9A-Za-z]{32}$' }
$ValidBrPwd = { param($v) $v.Length -eq 12 }
$ValidPsk   = { param($v) $v.Length -ge 8 -and $v.Length -le 63 }
$ValidPoll  = { param($v) $n = $v -as [int]; $n -ne $null -and $n -ge 10 -and $n -le 3600 }

# ---- 0. はじめに ------------------------------------------------------------
Clear-Host
Head 'Cube J1 ローカル電力モニタ セットアップUSB作成'
Say '  選んだ USB メモリの中身は消える場合があります。'
Say '  Bルート認証ID / パスワードは電力会社から発行されたものが必要です。'
Say '  Home Assistant も MQTTブローカーも要りません。'
if (-not (Test-Admin)) {
    Say '  ※ 管理者権限で起動していません。FAT32 フォーマットは実行できません。' 'Yellow'
}

if (-not (Test-Path $PayloadDir)) {
    Fail "payload フォルダが見つかりません: $PayloadDir`n  リポジトリを丸ごと配置してから実行してください。"
}
foreach ($f in $FromPayload) {
    if (-not (Test-Path (Join-Path $PayloadDir $f))) { Fail "payload に $f がありません。" }
}

# ---- 1. 上流ソースの取得 ----------------------------------------------------
$src = $null
if (-not $ConfigOnly) {
    Head '1. 上流 cube-j1-mqtt を取得'
    if ($SourceDir) {
        if (-not (Test-Path (Join-Path $SourceDir 'production_tool\mqtt_bridge.py'))) {
            Fail "指定フォルダに production_tool\mqtt_bridge.py がありません: $SourceDir"
        }
        $src = (Resolve-Path $SourceDir).Path
        Ok "ローカルフォルダを使用: $src"
    } else {
        if (Test-Path $WorkRoot) { Remove-Item $WorkRoot -Recurse -Force }
        New-Item -ItemType Directory -Path $WorkRoot -Force | Out-Null
        $zip = Join-Path $WorkRoot 'repo.zip'
        Say "  コミット $($UpstreamCommit.Substring(0,7)) を取得中..."
        try {
            [Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12
            Invoke-WebRequest -Uri $RepoZipUrl -OutFile $zip -UseBasicParsing
        } catch {
            Fail ("ダウンロードに失敗しました: " + $_.Exception.Message +
                  "`n  手動で ZIP を落として -SourceDir で指定してください。")
        }
        Expand-Archive -Path $zip -DestinationPath $WorkRoot -Force
        $found = Get-ChildItem $WorkRoot -Directory |
                 Where-Object { Test-Path (Join-Path $_.FullName 'production_tool\mqtt_bridge.py') }
        if (-not $found) { Fail 'ZIP の中身が想定と異なります。' }
        $src = $found[0].FullName
        Ok "展開完了 (固定コミット $($UpstreamCommit.Substring(0,7)))"
    }
    foreach ($f in $FromUpstream) {
        if (-not (Test-Path (Join-Path $src "production_tool\$f"))) {
            Fail "上流に production_tool\$f がありません。"
        }
    }
}

# ---- 2. 設定入力 ------------------------------------------------------------
Head '2. 接続情報の入力'

Say ''
Say '  [スマートメーター Bルート]' 'White'
$brId  = Read-Value  -Label '  Bルート認証ID (32文字)' -Validate $ValidBrId -Hint '英数字32文字のはずです。'
$brPwd = Read-Secret -Label '  Bルートパスワード (12文字)' -Validate $ValidBrPwd -Hint '12文字のはずです。'

Say ''
Say '  [自宅 Wi-Fi (WPA-PSK / 2.4GHz・5GHz)]' 'White'
$ssid    = Read-Value  -Label '  SSID'
$wifiPsk = Read-Secret -Label '  Wi-Fi パスワード' -Validate $ValidPsk -Hint 'WPA-PSK は 8〜63 文字です。'

Say ''
Say '  [取得間隔]' 'White'
$poll = [int](Read-Value -Label '  何秒ごとに測るか' -Default '60' -Validate $ValidPoll)

Say ''
Say ("  ダッシュボードは http://<CubeのIP>:{0}/ で公開されます。" -f $HttpPort) 'White'

# ---- 3. 設定ファイル生成 ----------------------------------------------------
Head '3. 設定ファイルを生成'
$stage = Join-Path $env:TEMP ('cube-j1-stage-' + [Guid]::NewGuid().ToString('N').Substring(0, 8))
New-Item -ItemType Directory -Path $stage -Force | Out-Null

# mqtt_host を 127.0.0.1 にすることで、bridge は同じ Cube 上の meter_hub に
# つなぎに行く。LAN に MQTT が一切流れず、認証情報も不要になる。
$cfg = [ordered]@{
    br_id         = $brId
    br_pwd        = $brPwd
    mqtt_host     = '127.0.0.1'
    mqtt_port     = $HubPort
    mqtt_user     = ''
    mqtt_pass     = ''
    device_id     = $DeviceId
    serial_port   = '/dev/ttyS1'
    poll_interval = $poll
    http_port     = $HttpPort
    history_size  = $HistorySize
}
$cfgJson = ConvertTo-Json -InputObject $cfg -Depth 3
Write-LfFile (Join-Path $stage 'config.json') $cfgJson
Ok 'config.json'

$ssidEsc = ConvertTo-WpaString $ssid
$pskEsc  = ConvertTo-WpaString $wifiPsk
$wpa = @"
ctrl_interface=/data/misc/wifi/sockets
update_config=1

network={
        ssid="$ssidEsc"
        psk="$pskEsc"
        key_mgmt=WPA-PSK
}
"@
Write-LfFile (Join-Path $stage 'wpa_supplicant.conf') $wpa
Ok 'wpa_supplicant.conf'

# ---- 4. USB ドライブ選択 ----------------------------------------------------
Head '4. 書き込み先の USB メモリを選択'
$removable = @(Get-CimInstance Win32_LogicalDisk -Filter 'DriveType=2' | Sort-Object DeviceID)
if ($removable.Count -eq 0) {
    Fail 'リムーバブルドライブが見つかりません。USB メモリを挿してから再実行してください。'
}
foreach ($d in $removable) {
    $gb = 0
    if ($d.Size) { $gb = [Math]::Round($d.Size / 1GB, 1) }
    $label = $d.VolumeName
    if (-not $label) { $label = '(ラベルなし)' }
    $fs = $d.FileSystem
    if (-not $fs) { $fs = '(未フォーマット)' }
    Write-Host ("  {0}  {1,-16} {2,-16} {3} GB" -f $d.DeviceID, $label, $fs, $gb)
}

if (-not $Drive) { $Drive = Read-Value -Label '  ドライブレター (例: E)' }
$letter = $Drive.TrimEnd(':').ToUpper()
$target = $removable | Where-Object { $_.DeviceID -eq ($letter + ':') }
if (-not $target) { Fail ($letter + ': はリムーバブルドライブとして見つかりません。') }

$root   = $letter + ':\'
$sizeGb = [Math]::Round($target.Size / 1GB, 1)
Say ''
Say ("  書き込み先: {0}:  ({1} / {2} / {3} GB)" -f $letter, $target.VolumeName, $target.FileSystem, $sizeGb) 'Yellow'
$again = Read-Host '  確認のため、もう一度ドライブレターを入力してください'
if ($again.TrimEnd(':').ToUpper() -ne $letter) { Fail '確認が一致しませんでした。' }

# ---- 5. FAT32 の確認 / フォーマット -----------------------------------------
if (-not $ConfigOnly) {
    Head '5. ファイルシステムの確認'
    if ($target.FileSystem -eq 'FAT32') {
        Ok 'FAT32 です。フォーマットは不要。'
    } elseif ($SkipFormat) {
        Say ("  警告: {0} のままです。Cube J1 が認識しない可能性があります。" -f $target.FileSystem) 'Yellow'
    } else {
        Say ("  現在 {0} です。Cube J1 は FAT32 のみ読み込みます。" -f $target.FileSystem) 'Yellow'
        if ($sizeGb -gt 32) {
            Fail '32GB 超のため Windows 標準機能では FAT32 にできません。32GB 以下の USB を使うか、diskpart 等で FAT32 にしてから -SkipFormat 付きで再実行してください。'
        }
        if (-not (Test-Admin)) {
            Fail '管理者権限が必要です。setup-cube-j1.bat から起動し直してください。'
        }
        Say ("  {0}: の中身はすべて消えます。" -f $letter) 'Red'
        if ((Read-Host '  実行するなら FORMAT と入力') -cne 'FORMAT') { Fail 'フォーマットを中止しました。' }
        Format-Volume -DriveLetter $letter -FileSystem FAT32 -NewFileSystemLabel 'CUBEJ1' -Force -Confirm:$false | Out-Null
        Ok 'FAT32 でフォーマットしました。'
    }
}

# ---- 6. 配置 ----------------------------------------------------------------
Head '6. USB メモリへ配置'
$ptDir = Join-Path $root 'production_tool'

if ($ConfigOnly) {
    if (-not (Test-Path (Join-Path $ptDir 'meter_hub.py'))) {
        Fail ($letter + ': にセットアップ済みの production_tool がありません。-ConfigOnly を外してください。')
    }
    Ok '既存の production_tool を再利用します。'
} else {
    # CubeJMTS.txt はこのファイルが存在すること自体が起動条件（中身は空）
    Write-LfFile (Join-Path $root 'CubeJMTS.txt') ''
    Ok 'CubeJMTS.txt'

    if (Test-Path $ptDir) { Remove-Item $ptDir -Recurse -Force }
    New-Item -ItemType Directory -Path $ptDir -Force | Out-Null

    foreach ($f in $FromUpstream) {
        Copy-Item -Path (Join-Path $src "production_tool\$f") -Destination $ptDir -Force
        Ok "$f  (上流のまま)"
    }
    foreach ($f in $FromPayload) {
        Copy-Item -Path (Join-Path $PayloadDir $f) -Destination $ptDir -Force
        Ok "$f  (このリポジトリ)"
    }
}

Copy-Item -Path (Join-Path $stage 'config.json')         -Destination $ptDir -Force
Copy-Item -Path (Join-Path $stage 'wpa_supplicant.conf') -Destination $ptDir -Force
Ok 'config.json / wpa_supplicant.conf'
Remove-Item $stage -Recurse -Force

# ---- 7. 検証 ----------------------------------------------------------------
Head '7. 検証'
$problems = @()
$required = @('CubeJMTS.txt') + ($FromUpstream + $FromPayload + @('config.json', 'wpa_supplicant.conf') |
             ForEach-Object { "production_tool\$_" })
foreach ($f in $required) {
    if (Test-Path (Join-Path $root $f)) { Ok $f } else { $problems += "不足: $f" }
}

# Cube 側の Linux が読むテキストは CR が混ざると壊れる
$mustBeLf = @('production_tool\production_tool', 'production_tool\meter_hub.py', 'production_tool\mqtt_bridge.py',
              'production_tool\meter_hub.rc', 'production_tool\config.json',
              'production_tool\wpa_supplicant.conf')
foreach ($f in $mustBeLf) {
    $p = Join-Path $root $f
    if (-not (Test-Path $p)) { continue }
    $bytes = [IO.File]::ReadAllBytes($p)
    if ($bytes -contains 13) { $problems += "$f に CR(改行コード) が混入しています。" }
    if ($bytes.Length -ge 3 -and $bytes[0] -eq 0xEF -and $bytes[1] -eq 0xBB -and $bytes[2] -eq 0xBF) {
        $problems += "$f に BOM が付いています。"
    }
}

try {
    $parsed = [IO.File]::ReadAllText((Join-Path $root 'production_tool\config.json')) | ConvertFrom-Json
    if ($parsed.mqtt_host -ne '127.0.0.1') { $problems += 'config.json の mqtt_host が 127.0.0.1 ではありません。' }
    if ($parsed.mqtt_port -ne $HubPort)    { $problems += 'config.json の mqtt_port が hub と一致しません。' }
    Ok 'config.json は妥当な JSON'
} catch {
    $problems += ('config.json が JSON として壊れています: ' + $_.Exception.Message)
}

$fsNow = (Get-CimInstance Win32_LogicalDisk -Filter ("DeviceID='" + $letter + ":'")).FileSystem
if ($fsNow -ne 'FAT32') { $problems += "ファイルシステムが $fsNow です（FAT32 推奨）。" }

if ($problems.Count -gt 0) {
    Write-Host ''
    foreach ($p in $problems) { Say "  [NG] $p" 'Red' }
    Fail '検証で問題が見つかりました。'
}

# ---- 完了 -------------------------------------------------------------------
Head '完了'
Say ("  1. {0}: を「ハードウェアの安全な取り外し」で取り外す" -f $letter) 'White'
Say '  2. Cube J1 に挿して電源投入' 'White'
Say '  3. 白LEDが10回点滅 = セットアップ完了 / 緑点灯 = Wi-Fi接続成功' 'White'
Say '  4. ルーターの管理画面で Cube の IP アドレスを調べる（DHCP固定推奨）' 'White'
Say ("  5. スマホのブラウザで  http://<CubeのIP>:{0}/  を開く" -f $HttpPort) 'White'
Say '     Android なら「ホーム画面に追加」でアプリのように使えます' 'White'
Say ''
Say '  スマートメーターへの初回接続（PANスキャン）に数分かかります。' 'White'
Say '  つながらないとき:' 'White'
Say '    adb connect <CubeのIP>:5555' 'White'
Say '    adb shell cat /data/local/meter_hub.log' 'White'
Say '    adb shell cat /data/local/mqtt_bridge.log' 'White'
Say ''
Say ("  注意: {0}:\production_tool\config.json には Bルート認証情報が平文で入ります。" -f $letter) 'Yellow'
Say '        セットアップ後は USB を消すか、施錠して保管してください。' 'Yellow'
Say '        Cube は adb を 5555 で開放します。IoT用VLANへの隔離を推奨します。' 'Yellow'
Write-Host ''
