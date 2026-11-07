#requires -Version 5.1
<#
  视频号复盘（ego 版）—— 数据半环第 2 块
  ------------------------------------------------------------------
  用法：
    powershell -NoProfile -ExecutionPolicy Bypass -File scripts\review-sph.ps1 "视频名"
    powershell -NoProfile -ExecutionPolicy Bypass -File scripts\review-sph.ps1 "publish-runs\20261107-xxx_视频名"

  功能：根据发布时写的 retro.json（作品描述），复用已登录的 ego 浏览器，
  到视频号助手作品列表找到那条作品，抓当前播放/点赞/评论/转发/收藏，
  写入 <runDir>\report.md，并打印出来。
#>

param(
    [Parameter(ValueFromRemainingArguments = $true)]
    [string[]]$InputArgs
)

$ErrorActionPreference = 'Stop'
[Console]::OutputEncoding = New-Object System.Text.UTF8Encoding($false)
$OutputEncoding = New-Object System.Text.UTF8Encoding($false)

$ScriptDir = $PSScriptRoot
$Root = Split-Path -Parent $ScriptDir
$RunRoot = Join-Path $Root 'publish-runs'
$CollectInputPath = 'C:\Users\CL\ego\collect-sph-post-input.json'
$CollectScriptUrl = 'file:///C:/Users/CL/ego/collect-sph-post.mjs'
$Utf8NoBom = New-Object System.Text.UTF8Encoding($false)

function Write-JsonFile {
    param([string]$Path, $Object)
    $json = $Object | ConvertTo-Json -Depth 12
    [System.IO.File]::WriteAllText($Path, $json, $Utf8NoBom)
}

# 流式运行 ego-browser，实时打印 stdout，最后返回 RESULT= 行
function Invoke-EgoCollect {
    $psi = New-Object System.Diagnostics.ProcessStartInfo
    $psi.FileName = 'ego-browser'
    $psi.Arguments = "nodejs -e `"await import('$CollectScriptUrl')`""
    $psi.UseShellExecute = $false
    $psi.CreateNoWindow = $true
    $psi.RedirectStandardOutput = $true
    $psi.RedirectStandardError = $true
    $psi.StandardOutputEncoding = $Utf8NoBom
    $psi.StandardErrorEncoding = $Utf8NoBom

    $proc = New-Object System.Diagnostics.Process
    $proc.StartInfo = $psi
    [void]$proc.Start()

    $errTask = $proc.StandardError.ReadToEndAsync()
    $stdout = New-Object System.Text.StringBuilder
    $deadline = [DateTime]::UtcNow.AddMilliseconds(180000)
    while ($true) {
        $remaining = [int](($deadline - [DateTime]::UtcNow).TotalMilliseconds)
        if ($remaining -le 0) { try { $proc.Kill() } catch { }; return 'RESULT=FAIL 执行超时' }
        $readTask = $proc.StandardOutput.ReadLineAsync()
        if (-not $readTask.Wait($remaining)) { try { $proc.Kill() } catch { }; return 'RESULT=FAIL 执行超时' }
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
    # ---- 1. 定位 run 目录 -------------------------------------------
    $rawInput = ''
    if ($InputArgs -and $InputArgs.Count -gt 0) { $rawInput = ($InputArgs -join ' ').Trim() }
    if (-not $rawInput) {
        $rawInput = Read-Host '请输入视频名或 run 目录路径'
    }
    $rawInput = $rawInput.Trim().Trim('"').Trim("'").Trim()

    $runDir = $null
    if (Test-Path -LiteralPath $rawInput -PathType Container) {
        $runDir = [System.IO.Path]::GetFullPath($rawInput)
    } else {
        $matches = @(Get-ChildItem -LiteralPath $RunRoot -Directory -ErrorAction SilentlyContinue | Where-Object { $_.Name -like ('*' + $rawInput + '*') })
        if ($matches.Count -eq 0) {
            throw ("在 publish-runs 里没找到包含 '{0}' 的运行目录" -f $rawInput)
        } elseif ($matches.Count -gt 1) {
            $runDir = ($matches | Sort-Object Name -Descending | Select-Object -First 1).FullName
            Write-Host ("匹配到 {0} 个目录，取最新：{1}" -f $matches.Count, (Split-Path -Leaf $runDir)) -ForegroundColor Yellow
        } else {
            $runDir = $matches[0].FullName
        }
    }

    $retroPath = Join-Path $runDir 'retro.json'
    if (-not (Test-Path -LiteralPath $retroPath)) {
        throw ("该运行目录没有 retro.json（{0}）。只有第 1 块上线后的发布才会生成它。" -f $retroPath)
    }

    # ---- 2. 读 retro.json 组装采集输入 ------------------------------
    $retro = [System.IO.File]::ReadAllText($retroPath, [System.Text.Encoding]::UTF8) | ConvertFrom-Json
    $collectInput = [ordered]@{
        runDir           = $runDir
        publishDesc      = [string]$retro.publishDesc
        publishTitle     = [string]$retro.publishTitle
        publishDate      = [string]$retro.publishDate
        metricsAtPublish = @($retro.metricsAtPublish | ForEach-Object { $_ })
    }
    Write-JsonFile -Path $CollectInputPath -Object $collectInput

    # ---- 3. 跑 ego 采集 --------------------------------------------
    if (-not (Get-Command 'ego-browser' -ErrorAction SilentlyContinue)) {
        throw '找不到命令 ego-browser，请先确认它已安装并在 PATH 中'
    }
    Write-Host ('正在复盘：{0}' -f (Split-Path -Leaf $runDir)) -ForegroundColor Cyan
    $result = Invoke-EgoCollect

    if ($result -match '^RESULT=OK') {
        Write-Host ''
        Write-Host '  复盘完成，report.md 已写入：' -ForegroundColor Green
        Write-Host ('  {0}' -f (Join-Path $runDir 'report.md'))
        $reportPath = Join-Path $runDir 'report.md'
        if (Test-Path -LiteralPath $reportPath) {
            Write-Host ''
            Write-Host '  ================ 复盘报告 ================' -ForegroundColor Cyan
            Write-Host ([System.IO.File]::ReadAllText($reportPath, [System.Text.Encoding]::UTF8))
            Write-Host '  ==========================================' -ForegroundColor Cyan
        }
        exit 0
    } else {
        Write-Host ('  复盘失败：{0}' -f $result) -ForegroundColor Red
        exit 1
    }
} catch {
    Write-Host ''
    Write-Host ('失败：{0}' -f $_.Exception.Message) -ForegroundColor Red
    exit 1
}
