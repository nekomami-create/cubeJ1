#Requires -Version 5.1
<#
    Cube J1 -> Home Assistant (MQTT) セットアップ USB を作成する。

    やること:
      1. tsuyopon123/cube-j1-mqtt を ZIP で取得（-SourceDir 指定で既存ローカルも可）
      2. Bルート認証情報 / Wi-Fi / MQTT 接続先を対話で入力
      3. config.json と wpa_supplicant.conf を LF + UTF-8(BOMなし) で生成
      4. USB メモリを選択（必要なら FAT32 フォーマット）
      5. CubeJMTS.txt と production_tool/ をルート直下へ配置して検証

    やらないこと: Bルート開通申請、Cube への挿入と電源投入。
#>
[CmdletBinding()]
param(
    [string]$Drive,
    [string]$SourceDir,
    [switch]$ConfigOnly,
    [switch]$SkipFormat
)

$ErrorActionPreference = 'Stop'
$RepoZipUrl = 'https://codeload.github.com/tsuyopon123/cube-j1-mqtt/zip/refs/heads/main'
$WorkRoot   = Join-Path $env:TEMP 'cube-j1-mqtt-setup'

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
    param(
        [string]$Label,
        [switch]$AllowEmpty,
        [scriptblock]$Validate = $null,
        [string]$Hint = ''
    )
    while ($true) {
        $sec = Read-Host $Label -AsSecureString
        $ptr = [Runtime.InteropServices.Marshal]::SecureStringToBSTR($sec)
        try {
            $v = [Runtime.InteropServices.Marshal]::PtrToStringBSTR($ptr)
        } finally {
            [Runtime.InteropServices.Marshal]::ZeroFreeBSTR($ptr)
        }
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
    $pr = New-Object Security.Principal.WindowsPrincipal($id)
    $pr.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)
}

# ---- 検証ルール -------------------------------------------------------------
$ValidBrId    = { param($v) $v -match '^[0-9A-Za-z]{32}$' }
$ValidBrPwd   = { param($v) $v.Length -eq 12 }
$ValidPsk     = { param($v) $v.Length -ge 8 -and $v.Length -le 63 }
$ValidHost    = { param($v) $v -match '^[A-Za-z0-9\.\-]+$' }
$ValidPort    = { param($v) $n = $v -as [int]; $n -ne $null -and $n -ge 1 -and $n -le 65535 }
$ValidDevId   = { param($v) $v -match '^[a-z0-9_\-]+$' }
$ValidPoll    = { param($v) $n = $v -as [int]; $n -ne $null -and $n -ge 10 -and $n -le 3600 }

# ---- 0. はじめに ------------------------------------------------------------
Clear-Host
Head 'Cube J1 -> Home Assistant セットアップ USB 作成'
Say '  選んだ USB メモリの中身は消える場合があります。'
Say '  事前に Home Assistant 側で Mosquitto broker と MQTT 統合を有効にしておいてください。'
Say '  Bルート認証ID / パスワードは電力会社から発行されたものが必要です。'
if (-not (Test-Admin)) {
    Say '  ※ 管理者権限で起動していません。FAT32 フォーマットは実行できません。' 'Yellow'
}

# ---- 1. ソース取得 ----------------------------------------------------------
$src = $null
if (-not $ConfigOnly) {
    Head '1. cube-j1-mqtt を取得'
    if ($SourceDir) {
        if (-not (Test-Path (Join-Path $SourceDir 'production_tool\production_tool'))) {
            Fail "指定フォルダに production_tool が見つかりません: $SourceDir"
        }
        $src = (Resolve-Path $SourceDir).Path
        Ok "ローカルフォルダを使用: $src"
    } else {
        if (Test-Path $WorkRoot) { Remove-Item $WorkRoot -Recurse -Force }
        New-Item -ItemType Directory -Path $WorkRoot -Force | Out-Null
        $zip = Join-Path $WorkRoot 'repo.zip'
        Say "  ダウンロード中: $RepoZipUrl"
        try {
            [Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12
            Invoke-WebRequest -Uri $RepoZipUrl -OutFile $zip -UseBasicParsing
        } catch {
            Fail ("ダウンロードに失敗しました: " + $_.Exception.Message + "`n  手動で ZIP を落として -SourceDir で指定してください。")
        }
        Expand-Archive -Path $zip -DestinationPath $WorkRoot -Force
        $found = Get-ChildItem $WorkRoot -Directory | Where-Object { Test-Path (Join-Path $_.FullName 'production_tool') }
        if (-not $found) { Fail 'ZIP 内に production_tool が見つかりませんでした。' }
        $src = $found[0].FullName
        Ok "展開完了: $src"
    }
}

# ---- 2. 設定入力 ------------------------------------------------------------
Head '2. 接続情報の入力'

Say ''
Say '  [スマートメーター Bルート]' 'White'
$brId  = Read-Value  -Label '  Bルート認証ID (32文字)' -Validate $ValidBrId -Hint '英数字32文字のはずです。'
$brPwd = Read-Secret -Label '  Bルートパスワード (12文字)' -Validate $ValidBrPwd -Hint '12文字のはずです。'

Say ''
Say '  [自宅 Wi-Fi (2.4GHz / WPA-PSK)]' 'White'
$ssid    = Read-Value  -Label '  SSID'
$wifiPsk = Read-Secret -Label '  Wi-Fi パスワード' -Validate $ValidPsk -Hint 'WPA-PSK は 8〜63 文字です。'

Say ''
Say '  [MQTT ブローカー (Home Assistant)]' 'White'
$mqttHost = Read-Value  -Label '  ホスト (IPアドレス推奨)' -Validate $ValidHost -Hint 'Cube 側は名前解決が弱いので IP 直指定を推奨。'
$mqttPort = [int](Read-Value -Label '  ポート' -Default '1883' -Validate $ValidPort)
$mqttUser = Read-Value  -Label '  MQTT ユーザー名 (未設定なら Enter)' -AllowEmpty
$mqttPass = Read-Secret -Label '  MQTT パスワード (未設定なら Enter)' -AllowEmpty

Say ''
Say '  [その他]' 'White'
$deviceId = Read-Value -Label '  device_id (HA上の識別子)' -Default 'cubej1' -Validate $ValidDevId -Hint '英小文字・数字・_ - のみ。'
$poll     = [int](Read-Value -Label '  ポーリング間隔 (秒)' -Default '60' -Validate $ValidPoll)

# ---- 3. 設定ファイル生成 ----------------------------------------------------
Head '3. 設定ファイルを生成'
$stage = Join-Path $env:TEMP ('cube-j1-stage-' + [Guid]::NewGuid().ToString('N').Substring(0, 8))
New-Item -ItemType Directory -Path $stage -Force | Out-Null

$cfg = [ordered]@{
    br_id         = $brId
    br_pwd        = $brPwd
    mqtt_host     = $mqttHost
    mqtt_port     = $mqttPort
    mqtt_user     = $mqttUser
    mqtt_pass     = $mqttPass
    device_id     = $deviceId
    serial_port   = '/dev/ttyS1'
    poll_interval = $poll
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
    $gb    = 0
    if ($d.Size) { $gb = [Math]::Round($d.Size / 1GB, 1) }
    $label = $d.VolumeName
    if (-not $label) { $label = '(ラベルなし)' }
    $fs    = $d.FileSystem
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
    if (-not (Test-Path (Join-Path $ptDir 'production_tool'))) {
        Fail ($letter + ': に production_tool が見つかりません。-ConfigOnly を外して実行してください。')
    }
    Ok '既存の production_tool を再利用します。'
} else {
    Copy-Item -Path (Join-Path $src 'CubeJMTS.txt') -Destination $root -Force
    Ok 'CubeJMTS.txt'
    if (Test-Path $ptDir) { Remove-Item $ptDir -Recurse -Force }
    Copy-Item -Path (Join-Path $src 'production_tool') -Destination $root -Recurse -Force
    Ok 'production_tool/'
}

Copy-Item -Path (Join-Path $stage 'config.json')         -Destination $ptDir -Force
Copy-Item -Path (Join-Path $stage 'wpa_supplicant.conf') -Destination $ptDir -Force
Ok 'config.json / wpa_supplicant.conf を書き込み'
Remove-Item $stage -Recurse -Force

# ---- 7. 検証 ----------------------------------------------------------------
Head '7. 検証'
$problems = @()
$required = @(
    'CubeJMTS.txt',
    'production_tool\production_tool',
    'production_tool\mqtt_bridge.py',
    'production_tool\led_effect.sh',
    'production_tool\mqtt_ha_bridge.rc',
    'production_tool\wisund_disabled.rc',
    'production_tool\ndeclite_disabled.rc',
    'production_tool\config.json',
    'production_tool\wpa_supplicant.conf'
)
foreach ($f in $required) {
    if (Test-Path (Join-Path $root $f)) { Ok $f } else { $problems += "不足: $f" }
}

foreach ($f in @('production_tool\config.json', 'production_tool\wpa_supplicant.conf')) {
    $p = Join-Path $root $f
    if (-not (Test-Path $p)) { continue }
    $bytes = [IO.File]::ReadAllBytes($p)
    if ($bytes -contains 13) { $problems += "$f に CR(改行コード) が混入しています。" }
    if ($bytes.Length -ge 3 -and $bytes[0] -eq 0xEF) { $problems += "$f に BOM が付いています。" }
}

try {
    $null = [IO.File]::ReadAllText((Join-Path $root 'production_tool\config.json')) | ConvertFrom-Json
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
Say '  3. 白 LED が10回点滅 = セットアップ完了 / 緑点灯 = Wi-Fi 接続成功' 'White'
Say '  4. Home Assistant に MQTT 自動検出でセンサーが登録される' 'White'
Say ''
Say '  つながらないとき:' 'White'
Say '    adb connect <CubeのIP>:5555' 'White'
Say '    adb shell cat /data/local/mqtt_bridge.log' 'White'
Say ''
Say ("  注意: {0}:\production_tool\config.json には Bルート認証情報と MQTT パスワードが" -f $letter) 'Yellow'
Say '        平文で保存されます。USB の保管に注意してください。' 'Yellow'
Say '        また adb が 5555 で開放され、LAN 内から root shell が取れる状態になります。' 'Yellow'
Write-Host ''
