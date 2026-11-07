#requires -Version 5.1
<#
  待复盘提醒（SessionStart hook 调用）
  ------------------------------------------------------------------
  扫描 publish-runs/，找出「已发布（有 retro.json）但未复盘（无 report.md）
  且发布已 ≥3 天」的视频，打印成提醒。stdout 会被注入到 Claude 的上下文。

  判定规则：
    - 有 retro.json   → 已发布（第 1 块跑过）
    - 无 report.md    → 还没复盘
    - 目录名时间戳（yyyyMMdd-HHmmss_视频名）距今 ≥ 3 天

  没有任何待复盘时，静默退出（不打扰）。
#>

$ErrorActionPreference = 'SilentlyContinue'
[Console]::OutputEncoding = New-Object System.Text.UTF8Encoding($false)

$ScriptDir = $PSScriptRoot
$Root = Split-Path -Parent $ScriptDir
$RunRoot = Join-Path $Root 'publish-runs'

if (-not (Test-Path -LiteralPath $RunRoot)) { exit 0 }

$now = Get-Date
$windowDays = 3
$pending = @()

Get-ChildItem -LiteralPath $RunRoot -Directory | ForEach-Object {
    $dir = $_.FullName
    $retroPath = Join-Path $dir 'retro.json'
    $reportPath = Join-Path $dir 'report.md'
    if (-not (Test-Path -LiteralPath $retroPath)) { return }   # 没发布
    if (Test-Path -LiteralPath $reportPath) { return }          # 已复盘

    # 从目录名解析发布时间：yyyyMMdd-HHmmss_视频名
    $m = [regex]::Match($_.Name, '^(\d{8})-(\d{6})_')
    if (-not $m.Success) { return }
    $pubStr = $m.Groups[1].Value + $m.Groups[2].Value
    try {
        $pubDate = [datetime]::ParseExact($pubStr, 'yyyyMMddHHmmss', $null)
    } catch { return }

    $age = $now - $pubDate
    if ($age.TotalDays -lt $windowDays) { return }

    # 读标题（尽力而为，读不到就算了）
    $title = ''
    try {
        $retro = [System.IO.File]::ReadAllText($retroPath, [System.Text.Encoding]::UTF8) | ConvertFrom-Json
        $title = [string]$retro.publishTitle
    } catch { }

    $pending += [pscustomobject]@{
        Name  = $_.Name
        Title = $title
        Days  = [int][math]::Floor($age.TotalDays)
    }
}

if ($pending.Count -eq 0) { exit 0 }

Write-Output ("⚠️ 待复盘：{0} 条视频发布已 ≥{1} 天、还没复盘。" -f $pending.Count, $windowDays)
foreach ($p in $pending) {
    if ($p.Title) {
        Write-Output ("  - {0}（{1} · {2} 天）" -f $p.Name, $p.Title, $p.Days)
    } else {
        Write-Output ("  - {0}（{1} 天）" -f $p.Name, $p.Days)
    }
}
Write-Output ""
Write-Output '复盘命令：powershell -NoProfile -ExecutionPolicy Bypass -File scripts\review-sph.ps1 "<视频名或目录名片段>"'
