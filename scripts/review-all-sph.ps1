#requires -Version 5.1
<#
  视频号一键复盘（批量采集 + AI 诊断 + 可视化看板 + 账号经验沉淀）
  ------------------------------------------------------------------
  用法：
    双击根目录 视频号一键复盘.cmd
    或命令行：
      powershell -NoProfile -ExecutionPolicy Bypass -File scripts\review-all-sph.ps1
      powershell -NoProfile -ExecutionPolicy Bypass -File scripts\review-all-sph.ps1 -All
      powershell -NoProfile -ExecutionPolicy Bypass -File scripts\review-all-sph.ps1 -AnalyzeOnly
#>

param(
    [switch]$All,
    [switch]$AnalyzeOnly,
    [switch]$NoOpen,
    [int]$MaxVideos = 30
)

$ErrorActionPreference = 'Stop'
[Console]::OutputEncoding = New-Object System.Text.UTF8Encoding($false)
$OutputEncoding = New-Object System.Text.UTF8Encoding($false)
$ProgressPreference = 'SilentlyContinue'

$ScriptDir = $PSScriptRoot
$Root = Split-Path -Parent $ScriptDir
$RunRoot = Join-Path $Root 'publish-runs'
$BatchScriptPath = Join-Path (Join-Path $ScriptDir 'ego') 'collect-sph-batch.mjs'
$AnalyzeScriptPath = Join-Path $ScriptDir 'retro_analyze.py'
$HistoryScriptPath = Join-Path $ScriptDir 'history_db.py'
$HistoryDir = Join-Path $Root 'history'
$Utf8NoBom = New-Object System.Text.UTF8Encoding($false)

function Read-JsonUtf8 {
    param([string]$Path)
    $raw = [System.IO.File]::ReadAllText($Path, [System.Text.Encoding]::UTF8)
    return ($raw | ConvertFrom-Json)
}

function Write-JsonUtf8 {
    param([string]$Path, $Object)
    $json = $Object | ConvertTo-Json -Depth 12
    [System.IO.File]::WriteAllText($Path, $json, $Utf8NoBom)
}

function Get-PublishedTime {
    param($Retro, [string]$DirName)
    if ($Retro.capturedAt) {
        try {
            return [System.DateTimeOffset]::Parse([string]$Retro.capturedAt).ToUniversalTime()
        } catch { }
    }
    $m = [regex]::Match($DirName, '^(\d{8})-(\d{6})(?:-\d+)?_')
    if ($m.Success) {
        try {
            $localDt = [datetime]::ParseExact($m.Groups[1].Value + $m.Groups[2].Value, 'yyyyMMddHHmmss', $null)
            return ([System.DateTimeOffset]$localDt).ToUniversalTime()
        } catch { }
    }
    return $null
}

function Get-HookTitleForRun {
    param([string]$RunDir)
    $candidates = @()
    $prep = Join-Path $RunDir 'prepared-video.txt'
    if ([IO.File]::Exists($prep)) {
        try {
            $vp = [IO.File]::ReadAllText($prep, [Text.Encoding]::UTF8).Trim()
            if ($vp) { $candidates += (Join-Path (Split-Path -Parent $vp) '剪辑记录.json') }
        } catch { }
    }
    $egoIn = Join-Path $RunDir 'ego-input.json'
    if ([IO.File]::Exists($egoIn)) {
        try {
            $inp = Read-JsonUtf8 $egoIn
            if ($inp.videoPath) { $candidates += (Join-Path (Split-Path -Parent ([string]$inp.videoPath)) '剪辑记录.json') }
        } catch { }
    }
    foreach ($cand in $candidates) {
        if ([IO.File]::Exists($cand)) {
            try {
                $rec = Read-JsonUtf8 $cand
                if ($rec.hook_title) { return [string]$rec.hook_title }
            } catch { }
        }
    }
    return ''
}

function Invoke-EgoBatch {
    param([string]$BatchUrl, [int]$TimeoutMs = 600000)
    $psi = New-Object System.Diagnostics.ProcessStartInfo
    $psi.FileName = 'ego-browser'
    $psi.Arguments = ('nodejs -e "await import(''{0}'')"' -f $BatchUrl)
    $psi.UseShellExecute = $false
    $psi.CreateNoWindow = $true
    $psi.RedirectStandardInput = $true
    $psi.RedirectStandardOutput = $true
    $psi.RedirectStandardError = $true
    $psi.StandardOutputEncoding = $Utf8NoBom
    $psi.StandardErrorEncoding = $Utf8NoBom

    $proc = New-Object System.Diagnostics.Process
    $proc.StartInfo = $psi
    [void]$proc.Start()
    try { $proc.StandardInput.Close() } catch { }

    $errTask = $proc.StandardError.ReadToEndAsync()
    $stdout = New-Object System.Text.StringBuilder
    $deadline = [DateTime]::UtcNow.AddMilliseconds($TimeoutMs)
    while ($true) {
        $remaining = [int](($deadline - [DateTime]::UtcNow).TotalMilliseconds)
        if ($remaining -le 0) {
            try { $proc.Kill() } catch { }
            return 'RESULT=FAIL 批量采集超时'
        }
        $readTask = $proc.StandardOutput.ReadLineAsync()
        if (-not $readTask.Wait($remaining)) {
            try { $proc.Kill() } catch { }
            return 'RESULT=FAIL 批量采集超时'
        }
        $line = $null
        try { $line = $readTask.Result } catch { $line = $null }
        if ($null -eq $line) { break }
        [void]$stdout.AppendLine($line)
        Write-Host $line
    }
    $proc.WaitForExit()
    $stderr = ''
    try { $stderr = $errTask.Result } catch { }
    if ($stderr) { Write-Host $stderr }

    $resultLine = ''
    foreach ($text in @($stdout.ToString(), $stderr)) {
        foreach ($l in ($text -split "`r?`n")) {
            $t = $l.Trim()
            if ($t -match '^RESULT=') { $resultLine = $t }
        }
    }
    if (-not $resultLine) { $resultLine = ('RESULT=FAIL 未收到结果（退出码 {0}）' -f $proc.ExitCode) }
    return $resultLine
}

try {
    Write-Host ''
    Write-Host '  视频号一键复盘（全自动采集 + 诊断看板）' -ForegroundColor Cyan
    Write-Host '  ----------------------------------------' -ForegroundColor DarkGray
    Write-Host ''

    if (-not (Get-Command 'python' -ErrorAction SilentlyContinue)) {
        throw '找不到命令 python，请先确认它已安装并在 PATH 中'
    }
    if (-not (Test-Path -LiteralPath $AnalyzeScriptPath)) {
        throw ('缺少复盘分析脚本：' + $AnalyzeScriptPath)
    }

    $targets = New-Object System.Collections.Generic.List[object]
    $nowUtc = [System.DateTimeOffset]::UtcNow

    if (Test-Path -LiteralPath $RunRoot) {
        $dirs = Get-ChildItem -LiteralPath $RunRoot -Directory |
            Where-Object { $_.Name -ne '.submissions' -and -not ($_.Attributes -band [IO.FileAttributes]::ReparsePoint) } |
            Sort-Object Name -Descending

        foreach ($d in $dirs) {
            $retroPath = Join-Path $d.FullName 'retro.json'
            if (-not [IO.File]::Exists($retroPath)) { continue }

            $retro = $null
            try { $retro = Read-JsonUtf8 $retroPath } catch { continue }

            $pubTime = Get-PublishedTime -Retro $retro -DirName $d.Name
            $ageHours = if ($null -ne $pubTime) { ($nowUtc - $pubTime).TotalHours } else { 9999.0 }

            $shouldCollect = $false
            if ($All) {
                $shouldCollect = $true
            } else {
                $snapPath = Join-Path $d.FullName 'retro-snapshots.json'
                if (-not [IO.File]::Exists($snapPath)) {
                    $shouldCollect = $true
                } else {
                    try {
                        $snapStore = Read-JsonUtf8 $snapPath
                        $snaps = @($snapStore.snapshots)
                        if ($snaps.Count -eq 0) {
                            $shouldCollect = $true
                        } elseif ($ageHours -ge 168 -and (@($snaps | Where-Object { $_.ageHours -ge 168 -or $_.stage -eq '7天+' }).Count -eq 0)) {
                            $shouldCollect = $true
                        } elseif ($ageHours -ge 72 -and (@($snaps | Where-Object { $_.ageHours -ge 72 -or $_.stage -in @('3天', '7天+') }).Count -eq 0)) {
                            $shouldCollect = $true
                        } elseif ($ageHours -ge 24 -and (@($snaps | Where-Object { $_.ageHours -ge 24 -or $_.stage -in @('1天', '3天', '7天+') }).Count -eq 0)) {
                            $shouldCollect = $true
                        } elseif ($ageHours -lt 24) {
                            $latestAt = $snapStore.latest.collectedAt
                            if (-not $latestAt) {
                                $shouldCollect = $true
                            } else {
                                $lastTime = [System.DateTimeOffset]::Parse([string]$latestAt).ToUniversalTime()
                                if (($nowUtc - $lastTime).TotalHours -ge 6) { $shouldCollect = $true }
                            }
                        }
                    } catch {
                        $shouldCollect = $true
                    }
                }
            }

            if (-not $shouldCollect) { continue }

            $transcript = ''
            $transcriptPath = Join-Path $d.FullName 'transcript.txt'
            if ([IO.File]::Exists($transcriptPath)) {
                $transcript = [System.IO.File]::ReadAllText($transcriptPath, [Text.Encoding]::UTF8).Trim()
            }

            $item = [ordered]@{
                runDir           = $d.FullName
                publishDesc      = [string]$retro.publishDesc
                publishTitle     = [string]$retro.publishTitle
                publishDate      = [string]$retro.publishDate
                capturedAt       = [string]$retro.capturedAt
                hookTitle        = Get-HookTitleForRun $d.FullName
                transcript       = $transcript
                metricsAtPublish = @($retro.metricsAtPublish | ForEach-Object { $_ })
            }
            if ($retro.matchMethod -eq 'top-row') {
                $item.publishDate = ''
                $item.metricsAtPublish = @()
            } else {
                $item['postId'] = [string]$retro.postId
            }
            $targets.Add($item)
            if ($targets.Count -ge $MaxVideos) { break }
        }
    }

    if (-not $AnalyzeOnly) {
        if ($targets.Count -eq 0) {
            Write-Host '当前没有新增待抓数据的视频，直接刷新复盘看板……' -ForegroundColor Yellow
        } else {
            if (-not (Get-Command 'ego-browser' -ErrorAction SilentlyContinue)) {
                throw '找不到命令 ego-browser，请先确认它已安装并在 PATH 中'
            }
            if (-not (Test-Path -LiteralPath $BatchScriptPath)) {
                throw ('缺少批量采集脚本：' + $BatchScriptPath)
            }

            Write-Host ('准备采集 {0} 条视频的最新播放、互动和评论……' -f $targets.Count) -ForegroundColor Cyan
            $guid = [guid]::NewGuid().ToString('N')
            $batchInputPath = Join-Path $RunRoot ('batch-input-' + $guid + '.json')
            $batchOutputPath = Join-Path $RunRoot ('batch-output-' + $guid + '.json')
            $targetArray = $targets.ToArray()
            $batchPayload = [ordered]@{
                targets         = $targetArray
                outputPath      = $batchOutputPath
                includeComments = $true
                probeStats      = $true
            }
            Write-JsonUtf8 -Path $batchInputPath -Object $batchPayload
            $batchUrl = ([System.Uri]$BatchScriptPath).AbsoluteUri + '?input=' + [Uri]::EscapeDataString($batchInputPath)

            try {
                $res = Invoke-EgoBatch -BatchUrl $batchUrl -TimeoutMs 600000
                if ($res -notmatch '^RESULT=OK') {
                    throw ('批量数据采集失败：' + $res)
                }
            } finally {
                if ([IO.File]::Exists($batchInputPath)) { [IO.File]::Delete($batchInputPath) }
                if ([IO.File]::Exists($batchOutputPath)) { [IO.File]::Delete($batchOutputPath) }
            }
        }
    }

    Write-Host ''
    if ([IO.File]::Exists($HistoryScriptPath)) {
        Write-Host '正在同步最新抓取数据到 history/ 数据库……' -ForegroundColor Cyan
        $prevPrefHist = $ErrorActionPreference
        try {
            $ErrorActionPreference = 'Continue'
            & python -X utf8 $HistoryScriptPath sync-runs --runs-dir $RunRoot --history-dir $HistoryDir
            if ($LASTEXITCODE -ne 0) { Write-Host 'history 同步返回非零退出码（已继续）' -ForegroundColor Yellow }
        } finally {
            $ErrorActionPreference = $prevPrefHist
        }
    }

    Write-Host '正在生成大白话复盘诊断、账号经验小本本与可视化看板……' -ForegroundColor Cyan
    $pyArgs = @('-u', '-X', 'utf8', $AnalyzeScriptPath, '--runs-dir', $RunRoot)
    if (-not $NoOpen) { $pyArgs += '--open-html' }

    $prevPref = $ErrorActionPreference
    try {
        $ErrorActionPreference = 'Continue'
        & python @pyArgs
        $pyExit = $LASTEXITCODE
        if ($pyExit -eq 0 -and [IO.File]::Exists($HistoryScriptPath)) {
            & python -u -X utf8 $AnalyzeScriptPath --history-dir $HistoryDir
            if ($LASTEXITCODE -ne 0) { Write-Host 'history 复盘更新返回非零退出码' -ForegroundColor Yellow }
        }
    } finally {
        $ErrorActionPreference = $prevPref
    }
    if ($pyExit -ne 0) {
        throw ('复盘分析脚本退出码 ' + $pyExit)
    }

    Write-Host ''
    Write-Host '  ✅ 一键复盘完成！' -ForegroundColor Green
    Write-Host ('  复盘看板：{0}' -f (Join-Path $RunRoot '复盘看板.html')) -ForegroundColor White
    Write-Host ('  文字总览：{0}' -f (Join-Path $RunRoot '复盘总览.md')) -ForegroundColor DarkGray
    Write-Host ''
    exit 0
} catch {
    Write-Host ''
    Write-Host ('失败：{0}' -f $_.Exception.Message) -ForegroundColor Red
    exit 1
}
