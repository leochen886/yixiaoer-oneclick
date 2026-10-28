#requires -Version 5.1
<#
  视频号一键发布
  ------------------------------------------------------------------
  用法：
    1) 双击 视频号一键发布.cmd，按提示拖入 / 粘贴视频路径，回车
    2) 或把视频文件直接拖到 视频号一键发布.cmd / 桌面快捷方式上

  自动完成：首帧封面 -> 本地语音转写 -> AI 生成标题描述标签
            -> 上传视频和封面 -> validate -> dry-run -> 本机发布 -> 核实结果

  全自动，不中途确认；任何一步失败都会停下来并说明原因。
#>

param(
    [Parameter(ValueFromRemainingArguments = $true)]
    [string[]]$InputArgs,

    # 仅测试用：完成上传、校验、演练后停止，不正式发布
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
$script:LogFile = $null
$Utf8NoBom = New-Object System.Text.UTF8Encoding($false)

# ---------------------------------------------------------------- 配置
$cfg = [ordered]@{
    createType     = 1        # 1 = 声明原创；2 = 不声明
    declaration    = 0        # 0 = 不声明；AI 生成内容可按需改为 1
    publishChannel = 'local'  # local = 本机发布（默认）；cloud = 云发布
    fallbackTags   = @()      # AI 失败时使用的兜底标签，不带 #
    keepRuns       = 20       # 保留最近多少次运行的中间文件
    waitSeconds    = 900      # 发布后等待客户端结果的秒数
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

function Invoke-Yxer {
    param(
        [string[]]$Arguments,
        [switch]$Quiet
    )
    if (-not $Quiet) { Write-Log ('> yxer ' + ($Arguments -join ' ')) 'DarkGray' }

    $oldEap = $ErrorActionPreference
    $ErrorActionPreference = 'Continue'
    try {
        $raw = & yxer @Arguments
        $code = $LASTEXITCODE
    } catch {
        $raw = $null
        $code = 1
    } finally {
        $ErrorActionPreference = $oldEap
    }

    $text = (@($raw) | Out-String).Trim()
    $json = $null
    if ($text) {
        try { $json = $text | ConvertFrom-Json } catch { }
    }
    return [pscustomobject]@{ ExitCode = $code; Text = $text; Json = $json }
}

function Get-YxerError {
    param($Result)
    if ($Result.Json -and $Result.Json.error) {
        $err = $Result.Json.error
        if ($err -is [string]) { return $err }
        return ($err | ConvertTo-Json -Depth 8 -Compress)
    }
    if ($Result.Text) { return $Result.Text }
    return ('yxer 退出码 ' + $Result.ExitCode)
}

function Write-JsonFile {
    param([string]$Path, $Object)
    $json = $Object | ConvertTo-Json -Depth 12
    [System.IO.File]::WriteAllText($Path, $json, $Utf8NoBom)
}

function Escape-Html {
    param([string]$Text)
    return ($Text -replace '&', '&amp;' -replace '<', '&lt;' -replace '>', '&gt;')
}

function Get-UploadObject {
    param($Data)
    if ($null -eq $Data) { return $null }
    $names = @($Data.PSObject.Properties.Name)
    if ($names -contains 'video' -and $Data.video) { return $Data.video }
    if ($names -contains 'resource' -and $Data.resource) { return $Data.resource }
    return $Data
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
- title：24 个汉字以内，口语化、有观点或悬念，能让人想点开；不要引号，不要话题标签，不要句号
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
    if ($title.Length -gt 30) { $title = $title.Substring(0, 30) }

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
    Write-Host '  视频号一键发布' -ForegroundColor Cyan
    Write-Host '  ----------------------------------------' -ForegroundColor DarkGray
    Write-Host ''

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

    foreach ($bin in @('yxer', 'ffmpeg', 'ffprobe', 'python', 'claude')) {
        if (-not (Get-Command $bin -ErrorAction SilentlyContinue)) {
            throw ("找不到命令 {0}，请先确认它已安装并在 PATH 中" -f $bin)
        }
    }
    if (-not (Test-Path -LiteralPath $AsrScript)) { throw ("缺少转写脚本：{0}" -f $AsrScript) }

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

    # ---- 3. 账号 ----------------------------------------------------
    $acc = Invoke-Yxer -Arguments @('accounts', 'list', '视频号', '--status', '1', '--json') -Quiet
    if (-not $acc.Json -or -not $acc.Json.ok) { throw ("查询视频号账号失败：{0}" -f (Get-YxerError $acc)) }
    $accountList = @($acc.Json.data)
    $account = $accountList | Where-Object { $_.platformName -eq '视频号' -and $_.status -eq 1 } | Select-Object -First 1
    if (-not $account) { throw '没有在线的视频号账号，请先在蚁小二客户端确认账号状态为在线' }
    Write-Log ("账号：{0}（{1}）" -f $account.platformAccountName, $account.id) DarkGray

    # ---- 4. 视频信息 & 首帧封面 -------------------------------------
    $probe = Invoke-Process -FilePath 'ffprobe' -Arguments @(
        '-v', 'error', '-select_streams', 'v:0',
        '-show_entries', 'stream=width,height',
        '-show_entries', 'format=duration',
        '-of', 'json', $videoPath
    ) -TimeoutMs 120000
    if ($probe.ExitCode -ne 0) { throw ('读取视频信息失败：' + $probe.StdErr.Trim()) }

    $probeJson = $probe.StdOut | ConvertFrom-Json
    $videoWidth = [int]$probeJson.streams[0].width
    $videoHeight = [int]$probeJson.streams[0].height
    $videoDuration = [double]$probeJson.format.duration
    Write-Log ("规格：{0}x{1}，{2:N1} 秒" -f $videoWidth, $videoHeight, $videoDuration) DarkGray

    $coverPath = Join-Path $runDir ('cover-{0}.jpg' -f $runId)
    $ffCover = Invoke-Process -FilePath 'ffmpeg' -Arguments @(
        '-y', '-hide_banner', '-loglevel', 'error',
        '-i', $videoPath, '-frames:v', '1', '-q:v', '2', $coverPath
    ) -TimeoutMs 180000
    if ($ffCover.ExitCode -ne 0 -or -not (Test-Path -LiteralPath $coverPath)) {
        throw ('截取视频首帧做封面失败：' + $ffCover.StdErr.Trim())
    }
    Write-Log '首帧封面已生成' DarkGray

    # ---- 5. 文案：字幕或本地转写 ------------------------------------
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

    # ---- 6. AI 文案 -------------------------------------------------
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

    $descParts = @()
    if ($copy.Description) { $descParts += ('<p>' + (Escape-Html $copy.Description) + '</p>') }
    if ($copy.Tags.Count -gt 0) {
        $descParts += ('<p>' + (($copy.Tags | ForEach-Object { '#' + (Escape-Html $_) }) -join ' ') + '</p>')
    }
    $descriptionHtml = ($descParts -join '')

    # ---- 7. 上传视频和封面 ------------------------------------------
    # 用带时间戳的硬链接上传，避免同名视频在 OSS 里互相覆盖；不同盘符时退回原文件名
    $uploadVideoPath = $videoPath
    $linkPath = Join-Path $runDir ('video-{0}{1}' -f $runId, $ext)
    try {
        if (-not (Test-Path -LiteralPath $linkPath)) {
            [void](New-Item -ItemType HardLink -Path $linkPath -Target $videoPath -ErrorAction Stop)
        }
        $uploadVideoPath = $linkPath
    } catch {
        $uploadVideoPath = $videoPath
    }

    Write-Log ("上传视频（{0}），大文件需要几分钟，请稍等……" -f (Format-Size $videoSize)) Cyan
    $upVideo = Invoke-Yxer -Arguments @('upload', '--file', $uploadVideoPath, '--auto-meta') -Quiet
    if (-not $upVideo.Json -or -not $upVideo.Json.ok) { throw ('视频上传失败：' + (Get-YxerError $upVideo)) }
    $vd = Get-UploadObject $upVideo.Json.data
    if (-not $vd -or -not $vd.key) { throw '视频上传返回里没有 key，无法继续' }

    $videoFormat = $ext.TrimStart('.')
    if ($vd.format) { $videoFormat = [string]$vd.format }
    $videoSizeUp = [int64]$videoSize
    if ($vd.size) { $videoSizeUp = [int64]$vd.size }
    $videoWidthUp = $videoWidth
    if ($vd.width) { $videoWidthUp = [int]$vd.width }
    $videoHeightUp = $videoHeight
    if ($vd.height) { $videoHeightUp = [int]$vd.height }
    $videoDurationUp = $videoDuration
    if ($vd.duration) { $videoDurationUp = [double]$vd.duration }

    $videoObj = [ordered]@{
        key      = [string]$vd.key
        format   = $videoFormat
        size     = $videoSizeUp
        width    = $videoWidthUp
        height   = $videoHeightUp
        duration = $videoDurationUp
    }
    Write-Log '视频上传完成' Green

    Write-Log '上传封面（视频号会自动压到 512KB 以内）……' Cyan
    $upCover = Invoke-Yxer -Arguments @('upload', '--file', $coverPath, '--platform', '视频号', '--usage', 'cover') -Quiet
    if (-not $upCover.Json -or -not $upCover.Json.ok) { throw ('封面上传失败：' + (Get-YxerError $upCover)) }
    $cd = Get-UploadObject $upCover.Json.data
    if (-not $cd -or -not $cd.key) { throw '封面上传返回里没有 key，无法继续' }

    $coverFormat = 'jpg'
    if ($cd.format) { $coverFormat = [string]$cd.format }
    $coverSizeUp = [int64](Get-Item -LiteralPath $coverPath).Length
    if ($cd.size) { $coverSizeUp = [int64]$cd.size }
    $coverWidthUp = $videoWidth
    if ($cd.width) { $coverWidthUp = [int]$cd.width }
    $coverHeightUp = $videoHeight
    if ($cd.height) { $coverHeightUp = [int]$cd.height }

    $coverObj = [ordered]@{
        key    = [string]$cd.key
        format = $coverFormat
        size   = $coverSizeUp
        width  = $coverWidthUp
        height = $coverHeightUp
    }
    Write-Log '封面上传完成' Green

    # ---- 8. 组装 payload --------------------------------------------
    $payload = [ordered]@{
        action         = 'publish'
        publishType    = 'video'
        platforms      = @('视频号')
        publishChannel = [string]$cfg['publishChannel']
        publishArgs    = [ordered]@{
            accountForms = @(
                [ordered]@{
                    platformAccountId  = [string]$account.id
                    video              = $videoObj
                    cover              = $coverObj
                    coverKey           = [string]$coverObj.key
                    contentPublishForm = [ordered]@{
                        formType    = 'task'
                        createType  = [int]$cfg['createType']
                        pubType     = 1
                        declaration = [int]$cfg['declaration']
                        title       = [string]$copy.Title
                        description = $descriptionHtml
                    }
                }
            )
        }
    }

    $payloadPath = Join-Path $runDir 'payload.json'
    Write-JsonFile -Path $payloadPath -Object $payload
    Write-Log ('payload 已生成：{0}' -f $payloadPath) DarkGray

    $channelArgs = @('--publish-channel', [string]$cfg['publishChannel'])

    # ---- 9. 校验 & 演练 --------------------------------------------
    Write-Log '校验 payload……' Cyan
    $validateArgs = @('validate', '视频号', 'video', $payloadPath) + $channelArgs
    $validate = Invoke-Yxer -Arguments $validateArgs -Quiet
    if (-not $validate.Json -or -not $validate.Json.ok) { throw ('校验未通过：' + (Get-YxerError $validate)) }
    [System.IO.File]::WriteAllText((Join-Path $runDir 'validate.json'), $validate.Text, $Utf8NoBom)
    Write-Log '校验通过' Green

    Write-Log '演练（dry-run）……' Cyan
    $dryArgs = @('publish', 'video', '视频号', $payloadPath) + $channelArgs + @('--dry-run')
    $dryRun = Invoke-Yxer -Arguments $dryArgs -Quiet
    if (-not $dryRun.Json -or -not $dryRun.Json.ok) { throw ('演练未通过：' + (Get-YxerError $dryRun)) }
    [System.IO.File]::WriteAllText((Join-Path $runDir 'dryrun.json'), $dryRun.Text, $Utf8NoBom)

    $effective = $dryRun.Json.data.meta.effectivePublishChannel
    if ($effective -and $effective -ne [string]$cfg['publishChannel']) {
        throw ('发布通道不一致：配置为 {0}，实际解析为 {1}' -f $cfg['publishChannel'], $effective)
    }
    Write-Log '演练通过' Green

    if ($NoPublish) {
        Write-Log '测试模式：上传、校验、演练全部通过，按要求不正式发布' Yellow
        Write-Host ''
        Write-Host ('  标题  ：{0}' -f $copy.Title)
        Write-Host ('  记录  ：{0}' -f $runDir) -ForegroundColor DarkGray
        Write-Host ''
        exit 0
    }

    # ---- 10. 正式发布 ----------------------------------------------
    Write-Log '正式发布……' Cyan
    $publishArgs = @('publish', 'video', '视频号', $payloadPath) + $channelArgs
    $publish = Invoke-Yxer -Arguments $publishArgs
    [System.IO.File]::WriteAllText((Join-Path $runDir 'publish.json'), $publish.Text, $Utf8NoBom)
    if (-not $publish.Json -or -not $publish.Json.ok) { throw ('发布提交失败：' + (Get-YxerError $publish)) }

    $taskSetId = $null
    if ($publish.Json.data) {
        foreach ($name in @('taskSetId', 'task_set_id', 'id')) {
            $value = $publish.Json.data.$name
            if ($value) { $taskSetId = [string]$value; break }
        }
    }
    if ($taskSetId) {
        Write-Log ('任务已提交：{0}' -f $taskSetId) Green
    } else {
        Write-Log '任务已提交（未返回任务号）' Green
    }

    # ---- 11. 核实结果 ----------------------------------------------
    $finalRecord = $null
    $lastStatus = ''
    if ($taskSetId) {
        Write-Log ('等待客户端执行结果（最多 {0} 秒）……' -f [int]$cfg['waitSeconds']) Cyan
        $deadline = (Get-Date).AddSeconds([int]$cfg['waitSeconds'])
        while ((Get-Date) -lt $deadline) {
            Start-Sleep -Seconds 10
            $records = Invoke-Yxer -Arguments @('query', 'records', '--platform', '视频号', '--limit', '10', '--json') -Quiet
            if (-not $records.Json -or -not $records.Json.ok) { continue }
            $hit = @($records.Json.data.data) | Where-Object { $_.id -eq $taskSetId } | Select-Object -First 1
            if (-not $hit) { continue }
            if ($hit.taskSetStatus -ne $lastStatus) {
                $lastStatus = [string]$hit.taskSetStatus
                Write-Log ('当前状态：{0}' -f $lastStatus) DarkGray
            }
            if (@('allsuccessful', 'allfailed', 'partialsuccess') -contains $lastStatus) {
                $finalRecord = $hit
                break
            }
        }
    }

    # ---- 12. 失败时补充原因 ----------------------------------------
    $errorDetail = ''
    if ($finalRecord -and $finalRecord.taskSetStatus -ne 'allsuccessful' -and $taskSetId) {
        $details = Invoke-Yxer -Arguments @('query', 'details', $taskSetId, '--json') -Quiet
        if ($details.Json -and $details.Json.ok) {
            foreach ($task in @($details.Json.data.tasks)) {
                if ($task.errorMessage) { $errorDetail = [string]$task.errorMessage; break }
            }
        }
    }

    # ---- 13. 总结 ---------------------------------------------------
    Write-Host ''
    Write-Host '  ================ 发布结果 ================' -ForegroundColor Cyan
    Write-Host ('  视频  ：{0}' -f (Split-Path -Leaf $videoPath))
    Write-Host ('  标题  ：{0}' -f $copy.Title)
    if ($copy.Tags.Count -gt 0) {
        Write-Host ('  标签  ：{0}' -f (($copy.Tags | ForEach-Object { '#' + $_ }) -join ' '))
    }
    Write-Host ('  平台  ：视频号 / {0}' -f $account.platformAccountName)

    $channelLabel = '云发布'
    if ([string]$cfg['publishChannel'] -eq 'local') { $channelLabel = '本机发布' }
    Write-Host ('  通道  ：{0}' -f $channelLabel)

    if ($taskSetId) { Write-Host ('  任务号：{0}' -f $taskSetId) }

    if ($finalRecord) {
        if ($finalRecord.taskSetStatus -eq 'allsuccessful') {
            Write-Host '  状态  ：发布成功 ✅' -ForegroundColor Green
        } else {
            Write-Host ('  状态  ：{0} ❌' -f $finalRecord.taskSetStatus) -ForegroundColor Red
            if ($errorDetail) { Write-Host ('  原因  ：{0}' -f $errorDetail) -ForegroundColor Red }
        }
    } elseif ($taskSetId) {
        Write-Host '  状态  ：已提交，客户端仍在处理，结果未出' -ForegroundColor Yellow
        Write-Host ('          可稍后用 yxer query details {0} 查看' -f $taskSetId) -ForegroundColor DarkGray
    } else {
        Write-Host '  状态  ：已提交，但没拿到任务号，请用 yxer query records 核对' -ForegroundColor Yellow
    }

    Write-Host ('  记录  ：{0}' -f $runDir) -ForegroundColor DarkGray
    Write-Host '  ==========================================' -ForegroundColor Cyan
    Write-Host ''

    # ---- 14. 清理旧运行目录 -----------------------------------------
    try {
        $runs = @(Get-ChildItem -LiteralPath $RunRoot -Directory -ErrorAction SilentlyContinue | Sort-Object Name -Descending)
        $keep = [int]$cfg['keepRuns']
        if ($runs.Count -gt $keep) {
            $runs | Select-Object -Skip $keep | ForEach-Object {
                Remove-Item -LiteralPath $_.FullName -Recurse -Force -ErrorAction SilentlyContinue
            }
        }
    } catch { }

    if ($finalRecord -and $finalRecord.taskSetStatus -ne 'allsuccessful') { exit 1 }
    exit 0
} catch {
    Write-Host ''
    Write-Log ('失败：{0}' -f $_.Exception.Message) Red
    if ($_.ScriptStackTrace) { Write-Log $_.ScriptStackTrace DarkGray }
    Write-Host ''
    exit 1
}
