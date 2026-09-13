#Requires -Version 5.1
<#
    Cube J1 から見える Wi-Fi を調べる（接続先は変えない）。

    USB に wifi_probe.sh と探したい SSID を置く。Cube は次の起動時に
    production_tool からそれを実行し、スキャン結果を
    /data/local/wifi_probe.txt と USB の wifi_probe.txt に書く。
    ステルス SSID も名前を指定して探すので見つかる。

    SSID はリポジトリに入れない。USB 上の wifi_probe_ssid.txt にだけ書く。
    パスワードは不要（スキャンだけなので）。

        .\probe-wifi.ps1 -Ssid <SSID>[,<SSID>]  USB に仕込む（複数可）
        .\probe-wifi.ps1 -Result               USB の結果を表示
        .\probe-wifi.ps1 -Remove               USB から取り除く
#>
[CmdletBinding(DefaultParameterSetName = 'Add')]
param(
    [Parameter(ParameterSetName = 'Add', Mandatory = $true)][string[]]$Ssid,
    [Parameter(ParameterSetName = 'Result')][switch]$Result,
    [Parameter(ParameterSetName = 'Remove')][switch]$Remove,
    [string]$Drive
)

$ErrorActionPreference = 'Stop'
$Payload = Join-Path $PSScriptRoot 'payload'

function Ok   { param([string]$m) Write-Host "  [OK] $m" -ForegroundColor Green }
function Fail { param([string]$m) Write-Host "[中止] $m" -ForegroundColor Red; exit 1 }

function Write-LfFile {
    param([string]$Path, [string]$Text)
    [IO.File]::WriteAllText($Path, ($Text -replace "`r`n", "`n"), (New-Object Text.UTF8Encoding($false)))
}

# ---- USB の特定 -------------------------------------------------------------
$removable = @(Get-CimInstance Win32_LogicalDisk -Filter 'DriveType=2' | Sort-Object DeviceID)
$usb = @($removable | Where-Object { Test-Path ("{0}\production_tool\production_tool" -f $_.DeviceID) })
if ($Drive) { $usb = @($usb | Where-Object { $_.DeviceID -eq ($Drive.TrimEnd(':').ToUpper() + ':') }) }
if ($usb.Count -eq 0) { Fail 'Cube 用のセットアップ USB が見つかりません。USB を PC に挿してください。' }
if ($usb.Count -gt 1) { Fail ('候補が複数あります。-Drive で指定してください: ' + (($usb | ForEach-Object DeviceID) -join ' ')) }
$root  = $usb[0].DeviceID
$ptDir = "$root\production_tool"
Ok "USB: $root"

if ($Result) {
    $f = "$root\wifi_probe.txt"
    if (-not (Test-Path $f)) { Fail "$f がありません（USB への書き込みができなかった可能性。adb で /data/local/wifi_probe.txt を読みます）" }
    Get-Content $f -Encoding UTF8
    exit 0
}

if ($Remove) {
    foreach ($n in 'wifi_probe.sh', 'wifi_probe_ssid.txt') {
        $p = Join-Path $ptDir $n
        if (Test-Path $p) { Remove-Item $p; Ok "削除: $n" }
    }
    Ok 'production_tool はそのまま（wifi_probe.sh が無ければ何もしません）'
    exit 0
}

# ---- 仕込む -----------------------------------------------------------------
foreach ($n in 'production_tool', 'wifi_probe.sh') {
    $src = Join-Path $Payload $n
    $bytes = [IO.File]::ReadAllBytes($src)
    if ($bytes -contains 13) { Fail "$n に CR が混じっています。" }
    Copy-Item $src (Join-Path $ptDir $n) -Force
    Ok "コピー: $n"
}

# 1行に1つ "<hex> <名前>"。Cube 側で16進に変換する手段が無いのでここで作る
$lines = foreach ($s in $Ssid) {
    $hex = -join ([Text.Encoding]::UTF8.GetBytes($s) | ForEach-Object { $_.ToString('x2') })
    Ok "探す SSID: $s ($hex)"
    "$hex $s"
}
Write-LfFile (Join-Path $ptDir 'wifi_probe_ssid.txt') (($lines -join "`n") + "`n")

Write-Host ''
Write-Host '  1. USB を「安全な取り外し」で外す' -ForegroundColor White
Write-Host '  2. Cube の電源を抜き、USB を挿して電源を入れる（今の場所のままで OK）' -ForegroundColor White
Write-Host '  3. 白の点滅が終わったら完了。結果は adb か、USB を PC に戻して -Result で読む' -ForegroundColor White
