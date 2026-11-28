#requires -Version 5.1
<#
  待复盘提醒（SessionStart hook 调用）
  ------------------------------------------------------------------
  扫描 publish-runs/，找出「已发布（有 retro.json）但尚未拿到对应阶段数据」的视频，
  优先以 retro.json 中的 capturedAt 为发布时间，兼容目录名时间戳。
#>

$ErrorActionPreference = 'SilentlyContinue'
[Console]::OutputEncoding = New-Object System.Text.UTF8Encoding($false)

$ScriptDir = $PSScriptRoot
$Root = Split-Path -Parent $ScriptDir
$RunRoot = Join-Path $Root 'publish-runs'

if (-not (Test-Path -LiteralPath $RunRoot)) { exit 0 }

$now = [System.DateTimeOffset]::Now
$windowDays = 3
$pending3d = @()
$neverCollected24h = @()

Get-ChildItem -LiteralPath $RunRoot -Directory | ForEach-Object {
    if ($_.Name -eq '.submissions') { return }
    $dir = $_.FullName
    $retroPath = Join-Path $dir 'retro.json'
    if (-not (Test-Path -LiteralPath $retroPath)) { return }

    $retro = $null
    try {
        $retro = [System.IO.File]::ReadAllText($retroPath, [System.Text.Encoding]::UTF8) | ConvertFrom-Json
    } catch { }

    $pubDate = $null
    if ($retro -and $retro.capturedAt) {
        try {
            $pubDate = [System.DateTimeOffset]::Parse([string]$retro.capturedAt).ToLocalTime()
        } catch { $pubDate = $null }
    }
    if ($null -eq $pubDate) {
        $m = [regex]::Match($_.Name, '^(\d{8})-(\d{6})(?:-\d+)?_')
        if (-not $m.Success) { return }
        try {
            $dt = [datetime]::ParseExact($m.Groups[1].Value + $m.Groups[2].Value, 'yyyyMMddHHmmss', $null)
            $pubDate = [System.DateTimeOffset]$dt
        } catch { return }
    }

    $age = $now - $pubDate
    $reportPath = Join-Path $dir 'report.md'
    $snapPath = Join-Path $dir 'retro-snapshots.json'
    $hasReport = Test-Path -LiteralPath $reportPath

    $snaps = @()
    if (Test-Path -LiteralPath $snapPath) {
        try {
            $snapStore = [System.IO.File]::ReadAllText($snapPath, [System.Text.Encoding]::UTF8) | ConvertFrom-Json
            if ($snapStore -and $snapStore.snapshots) { $snaps = @($snapStore.snapshots) }
        } catch { }
    }

    $title = if ($retro -and $retro.publishTitle) { [string]$retro.publishTitle } else { '' }
    $has72h = (@($snaps | Where-Object { $_.ageHours -ge 72 -or $_.stage -in @('3天', '7天+') }).Count -gt 0)

    if ($age.TotalDays -ge $windowDays -and -not $hasReport -and -not $has72h) {
        $pending3d += [pscustomobject]@{
            Name  = $_.Name
            Title = $title
            Days  = [int][math]::Floor($age.TotalDays)
        }
    } elseif ($age.TotalHours -ge 24 -and -not $hasReport -and $snaps.Count -eq 0) {
        $neverCollected24h += [pscustomobject]@{
            Name  = $_.Name
            Title = $title
            Hours = [int][math]::Floor($age.TotalHours)
        }
    }
}

if ($pending3d.Count -eq 0 -and $neverCollected24h.Count -eq 0) { exit 0 }

if ($pending3d.Count -gt 0) {
    Write-Output ("⚠️ 待复盘：{0} 条视频发布已 ≥{1} 天、还没做满3天复盘。" -f $pending3d.Count, $windowDays)
    foreach ($p in $pending3d) {
        if ($p.Title) {
            Write-Output ("  - {0}（{1} · {2} 天）" -f $p.Name, $p.Title, $p.Days)
        } else {
            Write-Output ("  - {0}（{1} 天）" -f $p.Name, $p.Days)
        }
    }
}
if ($neverCollected24h.Count -gt 0) {
    Write-Output ("📊 待更新：另有 {0} 条视频发布已满 24 小时、尚未抓取任何后台数据。" -f $neverCollected24h.Count)
}
Write-Output ""
Write-Output '一键全部复盘：双击根目录 视频号一键复盘.cmd（或运行 powershell -NoProfile -ExecutionPolicy Bypass -File scripts\review-all-sph.ps1）'
