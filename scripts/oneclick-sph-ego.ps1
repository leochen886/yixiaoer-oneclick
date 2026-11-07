#requires -Version 5.1
<#
  视频号一键发布（ego 版）
  ------------------------------------------------------------------
  用法：
    1) 双击 视频号一键发布.cmd，按提示拖入 / 粘贴视频路径，回车
    2) 或把视频文件直接拖到 视频号一键发布.cmd / 桌面快捷方式上

  自动完成：本地语音转写 -> AI 生成标题描述标签
            -> ego 浏览器登录视频号助手 -> 上传视频 -> 填文案
            -> 声明原创 -> 正式发表 -> 核实结果

  全自动，不中途确认；任何一步失败都会停下来并说明原因。
  本流程不经过 yxer，直接用 ego-browser 驱动视频号创作平台网页。
#>

param(
    [Parameter(ValueFromRemainingArguments = $true)]
    [string[]]$InputArgs,

    # 仅测试用：完成转写、上传、填表后停止，不正式发表
    [switch]$NoPublish
)

$ErrorActionPreference = 'Stop'
[Console]::OutputEncoding = New-Object System.Text.UTF8Encoding($false)
$OutputEncoding = New-Object System.Text.UTF8Encoding($false)
$ProgressPreference = 'SilentlyContinue'

# ---------------------------------------------------------------- 基础路径
$ScriptDir = $PSScriptRoot
$Root = Split-Path -Parent $ScriptDir
$ConfigPath = Join-Path $Root 'oneclick-config.json'
$RunRoot = Join-Path $Root 'publish-runs'
$AsrScript = Join-Path $ScriptDir 'oneclick_asr.py'

# ego 相关路径（脚本源码已纳入 scripts\ego\ 版本控制，相对脚本目录解析）
$EgoDir = Join-Path $ScriptDir 'ego'
$EgoInputPath = Join-Path $EgoDir 'publish-sph-ego-input.json'
$EgoScriptPath = Join-Path $EgoDir 'publish-sph-ego.mjs'
$LoginGatePath = Join-Path $EgoDir 'login-gate.mjs'
$EgoScriptUrl = 'file:///' + ($EgoScriptPath -replace '\\', '/')
$LoginGateUrl = 'file:///' + ($LoginGatePath -replace '\\', '/')

$script:LogFile = $null
$Utf8NoBom = New-Object System.Text.UTF8Encoding($false)

# ---------------------------------------------------------------- 配置
$cfg = [ordered]@{
    createType   = 1        # 1 = 声明原创；2 = 不声明
    fallbackTags = @()      # AI 失败时使用的兜底标签，不带 #
    keepRuns     = 20       # 保留最近多少次运行的中间文件
}

if (Test-Path -LiteralPath $ConfigPath) {
    try {
        $rawCfg = [System.IO.File]::ReadAllText($ConfigPath, [System.Text.Encoding]::UTF8)
        $parsedCfg = $rawCfg | ConvertFrom-Json
        foreach ($name in @($parsedCfg.PSObject.Properties.Name)) {
            if ($cfg.Contains($name)) { $cfg[$name] = $parsedCfg.$name }
        }
    } catch {
        Write-Host ("[提示] 配置文件解析失败，改用默认配置：{0}" -f $_.Exception.Message) -ForegroundColor Yellow
    }
}

# ---------------------------------------------------------------- 工具函数
function Write-Log {
    param(
        [Parameter(Position = 0)][string]$Message,
        [Parameter(Position = 1)][string]$Color = 'Gray'
    )
    $line = '[{0}] {1}' -f (Get-Date -Format 'HH:mm:ss'), $Message
    Write-Host $line -ForegroundColor $Color
    if ($script:LogFile) {
        try { Add-Content -LiteralPath $script:LogFile -Value $line -Encoding UTF8 } catch { }
    }
}

function Format-Size {
    param([double]$Bytes)
    if ($Bytes -ge 1GB) { return ('{0:N2} GB' -f ($Bytes / 1GB)) }
    if ($Bytes -ge 1MB) { return ('{0:N1} MB' -f ($Bytes / 1MB)) }
    return ('{0:N0} KB' -f ($Bytes / 1KB))
}

function ConvertTo-ArgString {
    param([string[]]$Items)
    $parts = @()
    foreach ($item in $Items) {
        if ($null -eq $item -or $item -eq '') {
            $parts += '""'
        } elseif ($item -match '[\s"]') {
            $parts += ('"' + ($item -replace '"', '\"') + '"')
        } else {
            $parts += $item
        }
    }
    return ($parts -join ' ')
}

function Invoke-Process {
    param(
        [string]$FilePath,
        [string[]]$Arguments,
        [int]$TimeoutMs = 600000,
        [string]$StdinText = $null
    )
    $psi = New-Object System.Diagnostics.ProcessStartInfo
    $psi.FileName = $FilePath
    $psi.Arguments = ConvertTo-ArgString $Arguments
    $psi.UseShellExecute = $false
    $psi.CreateNoWindow = $true
    $psi.RedirectStandardOutput = $true
    $psi.RedirectStandardError = $true
    $psi.StandardOutputEncoding = $Utf8NoBom
    $psi.StandardErrorEncoding = $Utf8NoBom
    if ($null -ne $StdinText) {
        $psi.RedirectStandardInput = $true
    }

    $proc = New-Object System.Diagnostics.Process
    $proc.StartInfo = $psi
    [void]$proc.Start()

    if ($null -ne $StdinText) {
        try {
            $writer = New-Object System.IO.StreamWriter($proc.StandardInput.BaseStream, $Utf8NoBom)
            $writer.Write($StdinText)
            $writer.Flush()
            $writer.Close()
        } catch { }
    }

    $outTask = $proc.StandardOutput.ReadToEndAsync()
    $errTask = $proc.StandardError.ReadToEndAsync()

    $finished = $proc.WaitForExit($TimeoutMs)
    if (-not $finished) {
        try { $proc.Kill() } catch { }
        return [pscustomobject]@{ ExitCode = -1; StdOut = ''; StdErr = ('执行超时（{0} 秒）' -f [int]($TimeoutMs / 1000)) }
    }
    $proc.WaitForExit()

    $stdout = ''
    $stderr = ''
    try { $stdout = $outTask.Result } catch { }
    try { $stderr = $errTask.Result } catch { }

    return [pscustomobject]@{ ExitCode = $proc.ExitCode; StdOut = $stdout; StdErr = $stderr }
}

# 流式运行 ego-browser，边跑边把 stdout 实时打印到终端，最后返回 RESULT= 行
function Invoke-Ego {
    param(
        [int]$TimeoutMs = 1500000,
        [string]$ScriptUrl = $EgoScriptUrl
    )
    $psi = New-Object System.Diagnostics.ProcessStartInfo
    $psi.FileName = 'ego-browser'
    $psi.Arguments = ConvertTo-ArgString @('nodejs', '-e', "await import('$ScriptUrl')")
    $psi.UseShellExecute = $false
    $psi.CreateNoWindow = $true
    $psi.RedirectStandardOutput = $true
    $psi.RedirectStandardError = $true
    $psi.StandardOutputEncoding = $Utf8NoBom
    $psi.StandardErrorEncoding = $Utf8NoBom

    $proc = New-Object System.Diagnostics.Process
    $proc.StartInfo = $psi
    [void]$proc.Start()

    # stderr 异步读，避免其缓冲填满而阻塞 stdout 的实时读取
    $errTask = $proc.StandardError.ReadToEndAsync()

    # stdout 逐行实时打印（登录检测、上传进度等状态行立即显示到终端）
    # 每次只发起一个 ReadLineAsync 并等它完成，避免并发读取导致丢行
    $stdout = New-Object System.Text.StringBuilder
    $deadline = [DateTime]::UtcNow.AddMilliseconds($TimeoutMs)
    while ($true) {
        $remaining = [int](($deadline - [DateTime]::UtcNow).TotalMilliseconds)
        if ($remaining -le 0) {
            try { $proc.Kill() } catch { }
            return 'RESULT=FAIL 执行超时'
        }
        $readTask = $proc.StandardOutput.ReadLineAsync()
        if (-not $readTask.Wait($remaining)) {
            try { $proc.Kill() } catch { }
            return 'RESULT=FAIL 执行超时'
        }
        $line = $null
        try { $line = $readTask.Result } catch { $line = $null }
        if ($null -eq $line) { break }   # 输出流结束
        [void]$stdout.AppendLine($line)
        Write-Host $line
    }

    $proc.WaitForExit()

    $stderr = ''
    try { $stderr = $errTask.Result } catch { }
    if ($stderr) { Write-Host $stderr }

    # 在 stdout 和 stderr 里都找 RESULT=（ego 的输出可能走任一通道）
    $resultLine = ''
    foreach ($text in @($stdout.ToString(), $stderr)) {
        foreach ($line in ($text -split "`r?`n")) {
            $trimmed = $line.Trim()
            if ($trimmed -match '^RESULT=') { $resultLine = $trimmed }
        }
    }

    if (-not $resultLine) {
        $resultLine = ('RESULT=FAIL 未收到结果（退出码 {0}）' -f $proc.ExitCode)
    }
    return $resultLine
}

function Write-JsonFile {
    param([string]$Path, $Object)
    $json = $Object | ConvertTo-Json -Depth 12
    [System.IO.File]::WriteAllText($Path, $json, $Utf8NoBom)
}

function Find-Srt {
    param([string]$VideoPath)
    $dir = Split-Path -Parent $VideoPath
    $base = [System.IO.Path]::GetFileNameWithoutExtension($VideoPath)
    $exact = Join-Path $dir ($base + '.srt')
    if (Test-Path -LiteralPath $exact) { return $exact }
    $found = @(Get-ChildItem -LiteralPath $dir -Filter ($base + '*.srt') -File -ErrorAction SilentlyContinue)
    if ($found.Count -eq 1) { return $found[0].FullName }
    return $null
}

function Get-SrtText {
    param([string]$Path)
    $raw = [System.IO.File]::ReadAllText($Path, [System.Text.Encoding]::UTF8)
    if ($raw.Contains([char]0xFFFD)) {
        try {
            $gb = [System.Text.Encoding]::GetEncoding('GB18030')
            $retry = [System.IO.File]::ReadAllText($Path, $gb)
            if (-not $retry.Contains([char]0xFFFD)) { $raw = $retry }
        } catch { }
    }
    $raw = $raw -replace '\r', ''
    $lines = $raw -split '\n'
    $buffer = New-Object System.Collections.Generic.List[string]
    foreach ($line in $lines) {
        $t = $line.Trim()
        if ($t -eq '') { continue }
        if ($t -match '^\d+$') { continue }
        if ($t -match '^\d{1,2}:\d{2}:\d{2}[,.]\d{1,3}\s*-->') { continue }
        $t = $t -replace '<[^>]+>', ''
        $buffer.Add($t)
    }
    return (($buffer -join '') -replace '\s+', ' ').Trim()
}

function Get-AiCopy {
    param(
        [string]$Transcript,
        [string]$VideoBase,
        [string]$PromptPath
    )

    $instructions = @'
你是资深的中文短视频运营。下面是一条口播视频的语音转写文本（可能有同音错别字，请按语义理解）。

请为视频号发布生成文案，只输出一个 JSON 对象，不要任何解释、不要代码块标记：
{"title":"...","description":"...","tags":["...","..."]}

要求：
- title：16 个字以内（这是"短标题"字段，限制 16 字），口语化、有观点或悬念，能让人想点开；尽量纯中文、不要英文；不要引号，不要话题标签，不要句号
- description：2 到 4 句话，80 到 180 字，概括核心观点，结尾留一个能引发讨论或共鸣的钩子；纯文本，不要 HTML，不要话题标签
- tags：3 到 5 个，每个 2 到 6 个汉字，不带 # 号
- 全部使用简体中文；不要编造转写文本里没有的事实或数字

转写文本：
'@

    $prompt = $instructions + $Transcript
    [System.IO.File]::WriteAllText($PromptPath, $prompt, $Utf8NoBom)

    $proc = Invoke-Process -FilePath 'claude' -Arguments @(
        '-p', '--output-format', 'json', '--tools', '', '--safe-mode', '--disable-slash-commands'
    ) -TimeoutMs 240000 -StdinText $prompt

    if ($proc.ExitCode -ne 0) {
        throw ('claude 调用失败（退出码 {0}）：{1}' -f $proc.ExitCode, ($proc.StdErr.Trim()))
    }

    $envelope = $null
    try { $envelope = $proc.StdOut | ConvertFrom-Json } catch { }
    if (-not $envelope -or -not ($envelope.PSObject.Properties.Name -contains 'result')) {
        $lines = $proc.StdOut -split "`n"
        for ($i = $lines.Count - 1; $i -ge 0; $i--) {
            $candidate = $lines[$i].Trim()
            if (-not $candidate.StartsWith('{')) { continue }
            try {
                $parsedLine = $candidate | ConvertFrom-Json
                if ($parsedLine.PSObject.Properties.Name -contains 'result') {
                    $envelope = $parsedLine
                    break
                }
            } catch { }
        }
    }
    if (-not $envelope) { throw '无法解析 claude 的输出' }

    $text = [string]$envelope.result
    if (-not $text) { throw 'claude 没有返回内容' }

    $start = $text.IndexOf('{')
    $end = $text.LastIndexOf('}')
    if ($start -lt 0 -or $end -le $start) { throw 'AI 返回内容里找不到 JSON' }
    $jsonText = $text.Substring($start, $end - $start + 1)

    $obj = $jsonText | ConvertFrom-Json

    $title = ''
    if ($obj.title) { $title = ([string]$obj.title).Trim().Trim('"').Trim("'").Trim() }
    if ($title -match '\r|\n') { $title = ($title -split '\r|\n')[0].Trim() }
    if ($title.Length -gt 16) { $title = $title.Substring(0, 16) }

    $description = ''
    if ($obj.description) {
        $description = ([string]$obj.description) -replace '<[^>]+>', ''
        $description = ($description -replace '\s+', ' ').Trim()
    }

    $tags = @()
    if ($obj.tags) {
        if ($obj.tags -is [string]) {
            $tags = @(([string]$obj.tags) -split '[,，、\s]+')
        } else {
            $tags = @($obj.tags | ForEach-Object { [string]$_ })
        }
        $tags = @($tags | ForEach-Object { $_.Trim().TrimStart('#').Trim() } | Where-Object { $_ } | Select-Object -First 5)
    }

    return [pscustomobject]@{
        Title       = $title
        Description = $description
        Tags        = $tags
        Raw         = $jsonText
    }
}

function Get-FallbackCopy {
    param([string]$Transcript, [string]$VideoBase)

    $title = $VideoBase
    if ($Transcript) {
        $first = ($Transcript -split '[。！？!?]')[0].Trim()
        if ($first) {
            if ($first.Length -gt 30) { $first = $first.Substring(0, 30) }
            $title = $first
        }
    }
    $desc = ''
    if ($Transcript) {
        if ($Transcript.Length -gt 200) { $desc = $Transcript.Substring(0, 200) } else { $desc = $Transcript }
    }
    $tags = @($cfg['fallbackTags'] | Where-Object { $_ })
    return [pscustomobject]@{ Title = $title; Description = $desc; Tags = $tags; Raw = '' }
}

# ================================================================ 主流程
try {
    Write-Host ''
    Write-Host '  视频号一键发布（ego）' -ForegroundColor Cyan
    Write-Host '  ----------------------------------------' -ForegroundColor DarkGray
    Write-Host ''

    # ---- 0. 先检测登录态（一打开就检测：已登录直接过，未登录先扫码，登录成功后再传视频） ----
    if (-not (Get-Command 'ego-browser' -ErrorAction SilentlyContinue)) {
        throw '找不到命令 ego-browser，请先确认它已安装并在 PATH 中'
    }
    if (-not (Test-Path -LiteralPath $LoginGatePath)) {
        throw ('缺少登录门禁脚本：' + $LoginGatePath)
    }
    Write-Log '正在检测视频号登录态……' Cyan
    $loginResult = Invoke-Ego -TimeoutMs 360000 -ScriptUrl $LoginGateUrl
    Write-Log ("登录检测结果：{0}" -f $loginResult) DarkGray
    if ($loginResult -notmatch '^RESULT=LOGIN_OK') {
        throw ('登录未完成：{0}' -f $loginResult)
    }

    # ---- 1. 输入 ----------------------------------------------------
    $rawInput = ''
    if ($InputArgs -and $InputArgs.Count -gt 0) {
        $rawInput = $InputArgs[0]
        if ($InputArgs.Count -gt 1) {
            Write-Log ('收到 {0} 个文件，只处理第一个：{1}' -f $InputArgs.Count, $rawInput) Yellow
        }
    }
    if (-not $rawInput) {
        Write-Host '把视频文件拖进这个窗口，或粘贴路径，然后回车：' -ForegroundColor Cyan
        $rawInput = Read-Host '视频路径'
    }

    $videoPath = $rawInput.Trim().Trim('"').Trim("'").Trim()
    if (-not $videoPath) { throw '没有提供视频路径' }

    if (-not [System.IO.Path]::IsPathRooted($videoPath)) {
        $videoPath = Join-Path (Get-Location).Path $videoPath
    }
    $videoPath = [System.IO.Path]::GetFullPath($videoPath)

    if (-not (Test-Path -LiteralPath $videoPath -PathType Leaf)) { throw ("找不到文件：{0}" -f $videoPath) }
    $ext = [System.IO.Path]::GetExtension($videoPath).ToLowerInvariant()
    if (@('.mp4', '.mov', '.m4v') -notcontains $ext) { throw ("只支持 mp4 / mov 视频，当前文件是 {0}" -f $ext) }

    foreach ($bin in @('ego-browser', 'ffmpeg', 'python', 'claude')) {
        if (-not (Get-Command $bin -ErrorAction SilentlyContinue)) {
            throw ("找不到命令 {0}，请先确认它已安装并在 PATH 中" -f $bin)
        }
    }
    if (-not (Test-Path -LiteralPath $AsrScript)) { throw ("缺少转写脚本：{0}" -f $AsrScript) }
    if (-not (Test-Path -LiteralPath $EgoScriptPath)) { throw ('缺少 ego 自动化脚本：' + $EgoScriptPath) }

    # ---- 2. 运行目录 ------------------------------------------------
    $videoBase = [System.IO.Path]::GetFileNameWithoutExtension($videoPath)
    $safeBase = $videoBase -replace '[\\/:*?"<>|]', '_'
    $runId = Get-Date -Format 'yyyyMMdd-HHmmss'
    $runDir = Join-Path $RunRoot ('{0}_{1}' -f $runId, $safeBase)
    if (-not (Test-Path -LiteralPath $runDir)) { [void](New-Item -ItemType Directory -Path $runDir -Force) }
    $script:LogFile = Join-Path $runDir 'publish.log'

    $videoSize = (Get-Item -LiteralPath $videoPath).Length
    Write-Log ("视频：{0}" -f $videoPath) White
    Write-Log ("大小：{0}" -f (Format-Size $videoSize)) DarkGray

    # ---- 3. 文案：字幕或本地转写 ------------------------------------
    $transcript = ''
    $srtPath = Find-Srt $videoPath
    if ($srtPath) {
        $transcript = Get-SrtText $srtPath
        Write-Log ("使用同名字幕：{0}（{1} 字）" -f (Split-Path -Leaf $srtPath), $transcript.Length) DarkGray
    } else {
        Write-Log '没有找到同名 .srt，开始本地语音转写……' Cyan
        $wavPath = Join-Path $runDir 'audio.wav'
        $ffAudio = Invoke-Process -FilePath 'ffmpeg' -Arguments @(
            '-y', '-hide_banner', '-loglevel', 'error',
            '-i', $videoPath, '-vn', '-ac', '1', '-ar', '16000', '-c:a', 'pcm_s16le', $wavPath
        ) -TimeoutMs 300000
        if ($ffAudio.ExitCode -ne 0 -or -not (Test-Path -LiteralPath $wavPath)) {
            throw ('抽取音频失败：' + $ffAudio.StdErr.Trim())
        }

        $transcriptPath = Join-Path $runDir 'transcript.txt'
        $asr = Invoke-Process -FilePath 'python' -Arguments @('-I', $AsrScript, $wavPath, $transcriptPath) -TimeoutMs 1800000
        if ($asr.ExitCode -ne 0 -or -not (Test-Path -LiteralPath $transcriptPath)) {
            throw ('语音转写失败：' + $asr.StdErr.Trim())
        }
        $transcript = [System.IO.File]::ReadAllText($transcriptPath, [System.Text.Encoding]::UTF8).Trim()
        Write-Log ("转写完成：{0} 字" -f $transcript.Length) DarkGray
    }

    # ---- 4. AI 文案 -------------------------------------------------
    $copy = $null
    if ($transcript.Length -ge 10) {
        Write-Log 'AI 生成标题、描述、标签……' Cyan
        try {
            $copy = Get-AiCopy -Transcript $transcript -VideoBase $videoBase -PromptPath (Join-Path $runDir 'prompt.txt')
            Write-Log 'AI 文案已生成' DarkGray
        } catch {
            Write-Log ('AI 文案失败，改用转写内容兜底：{0}' -f $_.Exception.Message) Yellow
        }
    }
    if (-not $copy -or -not $copy.Title) {
        $copy = Get-FallbackCopy -Transcript $transcript -VideoBase $videoBase
    }

    if (-not $copy.Description) { $copy.Description = '' }
    if (-not $copy.Tags) { $copy.Tags = @() }

    Write-Log ('标题：{0}' -f $copy.Title) White
    if ($copy.Description) { Write-Log ('描述：{0}' -f $copy.Description) DarkGray }
    if ($copy.Tags.Count -gt 0) { Write-Log ('标签：{0}' -f (($copy.Tags | ForEach-Object { '#' + $_ }) -join ' ')) DarkGray }

    # ---- 5. 组装 ego 输入 JSON --------------------------------------
    $egoInput = [ordered]@{
        videoPath       = $videoPath.Replace('\', '/')
        title           = [string]$copy.Title
        desc            = [string]$copy.Description
        tags            = @($copy.Tags | ForEach-Object { [string]$_ })
        declareOriginal = ([int]$cfg['createType'] -eq 1)
        noPublish       = [bool]$NoPublish
        runDir          = $runDir
    }
    Write-JsonFile -Path $EgoInputPath -Object $egoInput
    Write-JsonFile -Path (Join-Path $runDir 'ego-input.json') -Object $egoInput
    Write-Log ('ego 输入已生成：{0}' -f $EgoInputPath) DarkGray

    # ---- 6. ego 浏览器上传 + 发表 ------------------------------------
    if ($NoPublish) {
        Write-Log '测试模式：完成转写、上传、填表后停止，不正式发表' Yellow
    } else {
        Write-Log '开始通过 ego 浏览器上传并发表（大文件需要几分钟，请稍等）……' Cyan
    }
    $egoResult = Invoke-Ego -TimeoutMs 1500000
    [System.IO.File]::WriteAllText((Join-Path $runDir 'ego-result.txt'), $egoResult, $Utf8NoBom)
    Write-Log ('ego 结果：{0}' -f $egoResult) DarkGray

    # ---- 7. 总结 ---------------------------------------------------
    Write-Host ''
    Write-Host '  ================ 发布结果 ================' -ForegroundColor Cyan
    Write-Host ('  视频  ：{0}' -f (Split-Path -Leaf $videoPath))
    Write-Host ('  标题  ：{0}' -f $copy.Title)
    if ($copy.Tags.Count -gt 0) {
        Write-Host ('  标签  ：{0}' -f (($copy.Tags | ForEach-Object { '#' + $_ }) -join ' '))
    }
    Write-Host ('  平台  ：视频号')
    Write-Host ('  通道  ：ego 浏览器')

    $ok = $false
    switch -Regex ($egoResult) {
        '^RESULT=OK' { Write-Host '  状态  ：发布成功 ✅' -ForegroundColor Green; $ok = $true; break }
        '^RESULT=NO_PUBLISH' { Write-Host '  状态  ：测试模式，未发表' -ForegroundColor Yellow; $ok = $true; break }
        '^RESULT=NEED_LOGIN' { Write-Host '  状态  ：需要扫码登录' -ForegroundColor Yellow; break }
        '^RESULT=UNCONFIRMED' { Write-Host '  状态  ：已提交，结果未确认' -ForegroundColor Yellow; break }
        default { Write-Host ('  状态  ：失败 ❌  {0}' -f $egoResult) -ForegroundColor Red; break }
    }

    Write-Host ('  记录  ：{0}' -f $runDir) -ForegroundColor DarkGray
    Write-Host '  ==========================================' -ForegroundColor Cyan
    Write-Host ''

    # ---- 8. 清理旧运行目录 -----------------------------------------
    try {
        $runs = @(Get-ChildItem -LiteralPath $RunRoot -Directory -ErrorAction SilentlyContinue | Sort-Object Name -Descending)
        $keep = [int]$cfg['keepRuns']
        if ($runs.Count -gt $keep) {
            $runs | Select-Object -Skip $keep | ForEach-Object {
                Remove-Item -LiteralPath $_.FullName -Recurse -Force -ErrorAction SilentlyContinue
            }
        }
    } catch { }

    if ($ok) { exit 0 } else { exit 1 }
} catch {
    Write-Host ''
    Write-Log ('失败：{0}' -f $_.Exception.Message) Red
    if ($_.ScriptStackTrace) { Write-Log $_.ScriptStackTrace DarkGray }
    Write-Host ''
    exit 1
}
