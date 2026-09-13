#Requires -Version 5.1
<#
    Cube J1 の Wi-Fi 接続先だけを書き換える。

    Cube が LAN から見えなくなると adb が使えないため、USB 自動実行が
    唯一の経路になる。このスクリプトは USB 上の wpa_supplicant.conf
    だけを作り直す。config.json（Bルート認証情報）には触らない。

    USB の production_tool は起動時にこの wpa_supplicant.conf を
    /data/misc/wifi/ へ配置して wpa_cli reconfigure を呼ぶので、
    差し替えて電源を入れるだけで反映される。

    注意: Cube は WPA-PSK のみ。周波数帯は仕様上 2.4GHz（b/g/n）と
    5GHz（ac）の両対応。
#>
[CmdletBinding()]
param(
    [string]$Drive,
    # -Add: 既存の接続先を残したまま追加する（同じ SSID があれば置き換え）。
    # 何も付けなければ従来どおり、接続先をこの1つに置き換える。
    [switch]$Add,
    # 大きいほど優先。-Add で既存の priority 無しの接続先は 1 として扱う。
    [int]$Priority = 10
)

$ErrorActionPreference = 'Stop'

function Say  { param([string]$m, [string]$c = 'Gray') Write-Host $m -ForegroundColor $c }
function Head { param([string]$m) Write-Host ''; Write-Host ("== {0} {1}" -f $m, ('-' * 8)) -ForegroundColor Cyan }
function Fail { param([string]$m) Write-Host ''; Write-Host "[中止] $m" -ForegroundColor Red; exit 1 }
function Ok   { param([string]$m) Write-Host "  [OK] $m" -ForegroundColor Green }

function Read-Value {
    param([string]$Label, [scriptblock]$Validate = $null, [string]$Hint = '')
    while ($true) {
        $v = Read-Host $Label
        if (-not $v) { Say '  空にはできません。' 'Yellow'; continue }
        if ($Validate -and -not (& $Validate $v)) {
            Say "  入力が不正です。$Hint" 'Yellow'
            if ((Read-Host '  このまま使いますか? (y/N)') -eq 'y') { return $v }
            continue
        }
        return $v
    }
}

function Read-Secret {
    param([string]$Label, [scriptblock]$Validate = $null, [string]$Hint = '')
    while ($true) {
        $sec = Read-Host $Label -AsSecureString
        $ptr = [Runtime.InteropServices.Marshal]::SecureStringToBSTR($sec)
        try   { $v = [Runtime.InteropServices.Marshal]::PtrToStringBSTR($ptr) }
        finally { [Runtime.InteropServices.Marshal]::ZeroFreeBSTR($ptr) }
        if (-not $v) { Say '  空にはできません。' 'Yellow'; continue }
        if ($Validate -and -not (& $Validate $v)) {
            Say "  入力が不正です。$Hint" 'Yellow'
            if ((Read-Host '  このまま使いますか? (y/N)') -eq 'y') { return $v }
            continue
        }
        return $v
    }
}

# wpa_supplicant のダブルクォート文字列内エスケープ（\ を先に、次に "）
function ConvertTo-WpaString {
    param([string]$Value)
    # 正規表現を通さない String.Replace で置き換える（-replace '\' は不正な正規表現）
    $Value.Replace('\', '\\').Replace('"', '\"')
}

function Write-LfFile {
    param([string]$Path, [string]$Text)
    $lf = $Text -replace "`r`n", "`n"
    [IO.File]::WriteAllText($Path, $lf, (New-Object Text.UTF8Encoding($false)))
}

$ValidPsk = { param($v) $v.Length -ge 8 -and $v.Length -le 63 }

Clear-Host
Head 'Cube J1 Wi-Fi 接続先の書き換え'
Say '  書き換えるのは wpa_supplicant.conf だけです。'
Say '  Bルート認証情報（config.json）には触れません。'
Say ''
Say '  Cube は WPA-PSK のみ対応です（2.4GHz / 5GHz とも仕様上は可）。' 'Yellow'

# ---- USB の選択 -------------------------------------------------------------
Head '1. 書き込み先の USB'
$removable = @(Get-CimInstance Win32_LogicalDisk -Filter 'DriveType=2' | Sort-Object DeviceID)
if ($removable.Count -eq 0) { Fail 'リムーバブルドライブが見つかりません。USB を挿してから再実行してください。' }
foreach ($d in $removable) {
    $gb = 0
    if ($d.Size) { $gb = [Math]::Round($d.Size / 1GB, 1) }
    $label = $d.VolumeName; if (-not $label) { $label = '(ラベルなし)' }
    Write-Host ("  {0}  {1,-16} {2,-10} {3} GB" -f $d.DeviceID, $label, $d.FileSystem, $gb)
}

if (-not $Drive) { $Drive = Read-Value -Label '  ドライブレター (例: D)' }
$letter = $Drive.TrimEnd(':').ToUpper()
$target = $removable | Where-Object { $_.DeviceID -eq ($letter + ':') }
if (-not $target) { Fail ($letter + ': はリムーバブルドライブとして見つかりません。') }

$ptDir = "${letter}:\production_tool"
if (-not (Test-Path (Join-Path $ptDir 'production_tool'))) {
    Fail "$ptDir にセットアップ済みの production_tool がありません。`n  これは Cube 用のセットアップ USB ではないようです。"
}
Ok "書き込み先: $ptDir"

# ---- 入力 -------------------------------------------------------------------
Head '2. 接続先の Wi-Fi'
Say ''
$ssid = Read-Value  -Label '  SSID'
$psk  = Read-Secret -Label '  パスワード' -Validate $ValidPsk -Hint 'WPA-PSK は 8〜63 文字です。'

# ---- 生成 -------------------------------------------------------------------
Head '3. wpa_supplicant.conf を生成'
$conf = Join-Path $ptDir 'wpa_supplicant.conf'
if (Test-Path $conf) {
    $bak = Join-Path $ptDir 'wpa_supplicant.conf.bak'
    Copy-Item $conf $bak -Force
    Ok "既存のものを wpa_supplicant.conf.bak へ退避"
}

# scan_ssid=1 は SSID を隠した（ステルス）AP にも名前を指定して探しに行く指定。
# 隠していない AP でも害は無いので常に付ける。
$newBlock = @"
network={
        ssid="$(ConvertTo-WpaString $ssid)"
        psk="$(ConvertTo-WpaString $psk)"
        key_mgmt=WPA-PSK
        scan_ssid=1
        priority=$Priority
}
"@

$blocks = @()
if ($Add -and -not $bak) { Say '  既存の wpa_supplicant.conf が無いので、この1件だけで作ります' 'Yellow' }
if ($Add -and $bak -and (Test-Path $bak)) {
    $old = [IO.File]::ReadAllText($bak, (New-Object Text.UTF8Encoding($false))) -replace "`r", ''
    $want = 'ssid="' + (ConvertTo-WpaString $ssid) + '"'
    foreach ($m in [regex]::Matches($old, '(?s)network=\{.*?\n\}')) {
        $b = $m.Value
        if ($b.Contains($want)) { Say "  同じ SSID の既存設定は置き換えます" 'Yellow'; continue }
        if ($b -notmatch '(?m)^\s*priority=') { $b = $b -replace '\n\}$', "`n        priority=1`n}" }
        $blocks += $b
    }
    Ok ("既存の接続先 {0} 件を残します" -f $blocks.Count)
}
$blocks += $newBlock

$text = "ctrl_interface=/data/misc/wifi/sockets`nupdate_config=1`n`n" + ($blocks -join "`n`n") + "`n"
Write-LfFile $conf $text

# ---- 検証 -------------------------------------------------------------------
Head '4. 検証'
$bytes = [IO.File]::ReadAllBytes($conf)
$problems = @()
if ($bytes -contains 13) { $problems += 'CR(改行コード) が混入しています。' }
if ($bytes.Length -ge 3 -and $bytes[0] -eq 0xEF -and $bytes[1] -eq 0xBB -and $bytes[2] -eq 0xBF) {
    $problems += 'BOM が付いています。'
}
$read = [IO.File]::ReadAllText($conf, (New-Object Text.UTF8Encoding($false)))
if ($read -notmatch '(?m)^\s*ssid="')     { $problems += 'ssid 行が生成されていません。' }
if ($read -notmatch '(?m)^\s*psk="')      { $problems += 'psk 行が生成されていません。' }
if ($read -notmatch 'key_mgmt=WPA-PSK')   { $problems += 'key_mgmt 行が生成されていません。' }

if ($problems.Count -gt 0) {
    Write-Host ''
    foreach ($p in $problems) { Say "  [NG] $p" 'Red' }
    Fail '検証で問題が見つかりました。'
}
Ok ("{0} バイト / CR なし / BOM なし" -f $bytes.Length)
Ok "SSID は伏せずに確認: $ssid"

# ---- 完了 -------------------------------------------------------------------
Head '完了'
Say ("  1. {0}: を「ハードウェアの安全な取り外し」で取り外す" -f $letter) 'White'
Say '  2. Cube の電源を抜き、USB を挿して電源を入れる' 'White'
Say '  3. 起動時に production_tool がこの設定を反映し、wpa_cli reconfigure を呼びます' 'White'
Say '  4. 2〜3分待ってから、LAN 上に現れるか確認してください' 'White'
Say ''
Say '  繋がらないときは、移動先にその SSID の電波が届いているかを' 'Yellow'
Say '  スマホなどで確かめてください。' 'Yellow'
Write-Host ''
