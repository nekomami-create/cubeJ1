#Requires -Version 5.1
<#
    LAN 上の Cube J1 (meter_hub) を探す。

    ルーター管理画面を開かずに IP を突き止めるための補助。
    各ホストの :8080 に TCP 接続を試し、応答したものへ GET /healthz を投げ、
    "ok" を返したものを Cube と判定する。
#>
[CmdletBinding()]
param(
    [int]$Port    = 8080,
    [int]$TimeoutMs = 300,
    [string]$Subnet          # 例: 192.168.1  省略時は自動判定
)

$ErrorActionPreference = 'Stop'

if (-not $Subnet) {
    $cand = Get-NetIPAddress -AddressFamily IPv4 |
            Where-Object { $_.PrefixOrigin -ne 'WellKnown' -and $_.IPAddress -ne '127.0.0.1' } |
            Sort-Object -Property SkipAsSource
    if (-not $cand) { Write-Host '[中止] IPv4 アドレスが見つかりません。' -ForegroundColor Red; exit 1 }
    $me = $cand[0].IPAddress
    $Subnet = ($me -split '\.')[0..2] -join '.'
    Write-Host ("  このPC: {0}  -> {1}.0/24 を走査します" -f $me, $Subnet) -ForegroundColor Gray
}

Write-Host ("== {0}.1-254 の :{1} を探索中 ..." -f $Subnet, $Port) -ForegroundColor Cyan

# 非同期 TCP 接続を一斉に投げる (PS 5.1 には ForEach-Object -Parallel が無い)
$pending = @()
foreach ($i in 1..254) {
    $ip = "$Subnet.$i"
    $c  = New-Object Net.Sockets.TcpClient
    $pending += [pscustomobject]@{ Ip = $ip; Client = $c; Async = $c.BeginConnect($ip, $Port, $null, $null) }
}
Start-Sleep -Milliseconds $TimeoutMs

$open = @()
foreach ($p in $pending) {
    if ($p.Async.IsCompleted) {
        try { $p.Client.EndConnect($p.Async); $open += $p.Ip } catch { }
    }
    $p.Client.Close()
}

if ($open.Count -eq 0) {
    Write-Host ("  :{0} が開いているホストはありません。" -f $Port) -ForegroundColor Yellow
    Write-Host '  Cube がまだ Wi-Fi に繋がっていないか、別セグメントにいます。' -ForegroundColor Yellow
    Write-Host '  ゲスト/IoT VLAN に隔離している場合は -Subnet でそちらを指定してください。' -ForegroundColor Yellow
    exit 2
}

Write-Host ("  :{0} が開いていたホスト: {1}" -f $Port, ($open -join ', ')) -ForegroundColor Gray
Write-Host ''

$found = @()
foreach ($ip in $open) {
    $url = "http://${ip}:$Port/healthz"
    try {
        $r = Invoke-WebRequest -Uri $url -TimeoutSec 3 -UseBasicParsing
        if ($r.Content.Trim() -eq 'ok') {
            Write-Host ("  [Cube] {0}  /healthz -> ok" -f $ip) -ForegroundColor Green
            $found += $ip
        } else {
            Write-Host ("  [別物] {0}  /healthz -> {1}" -f $ip, $r.Content.Trim()) -ForegroundColor DarkGray
        }
    } catch {
        Write-Host ("  [別物] {0}  /healthz 応答なし" -f $ip) -ForegroundColor DarkGray
    }
}

if ($found.Count -eq 0) { Write-Host "`n  Cube は見つかりませんでした。" -ForegroundColor Yellow; exit 2 }

foreach ($ip in $found) {
    Write-Host ''
    Write-Host ("== {0} の状態" -f $ip) -ForegroundColor Cyan
    Write-Host ("  ダッシュボード : http://{0}:{1}/" -f $ip, $Port) -ForegroundColor White
    try {
        $api = Invoke-WebRequest -Uri ("http://${ip}:$Port/api") -TimeoutSec 3 -UseBasicParsing
        $j   = $api.Content | ConvertFrom-Json
        Write-Host ("  /api           : {0}" -f $api.Content.Trim()) -ForegroundColor Gray
        if ($null -eq $j.power_w) {
            Write-Host '  -> 電力値はまだ空です。Bルート認証情報がダミーのうちは正常な状態です。' -ForegroundColor Yellow
        } else {
            Write-Host ("  -> 現在 {0} W" -f $j.power_w) -ForegroundColor Green
        }
    } catch {
        Write-Host ("  /api 取得失敗: {0}" -f $_.Exception.Message) -ForegroundColor Yellow
    }
    Write-Host ''
    Write-Host '  ログを見るとき:' -ForegroundColor Gray
    Write-Host ("    adb connect {0}:5555" -f $ip) -ForegroundColor Gray
    Write-Host '    adb shell cat /data/local/meter_hub.log' -ForegroundColor Gray
}
