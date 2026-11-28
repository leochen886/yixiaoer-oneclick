#requires -Version 5.1
<#
  一键发布（麦田版）
  ------------------------------------------------------------------
  用法：
    1) 双击 一键发布.cmd，按提示拖入 / 粘贴原片路径，回车
    2) 或把原片直接拖到 一键发布.cmd / 桌面「一键发布」快捷方式上

  与「视频号一键发布」并行、互不影响，原路径照常可用作备份。
  流程：剪口播（koubo.py）剪好成片 → 无头唤起 Claude 助手
        → 助手按麦田口播流程写八平台文案、选封面 → 麦田发八个平台
        → 取链接，结果推飞书/大屏。

  全自动，不中途确认；用户唯一动作就是拖入原片。
#>

param(
    [Parameter(ValueFromRemainingArguments = $true)]
    [string[]]$InputArgs
)

$ErrorActionPreference = 'Stop'
[Console]::OutputEncoding = New-Object System.Text.UTF8Encoding($false)

$ScriptDir = $PSScriptRoot
$Root = Split-Path -Parent $ScriptDir
$MaitianRoot = 'E:\BaiduSyncdisk\Codetools\咬金战AI\自媒体工作台'
$BindingPath = Join-Path $MaitianRoot 'data\口播发布\对话.json'
$SkillPath = Join-Path $MaitianRoot 'skills\maitian-publish\原片到八平台.md'

# ---------------------------------------------------------------- 取原片路径
$raw = ''
if ($InputArgs -and $InputArgs.Count -gt 0) { $raw = $InputArgs[0] }
if (-not $raw) {
    Write-Host '把原视频拖进这个窗口，或粘贴路径，然后回车：' -ForegroundColor Cyan
    $raw = Read-Host '原片路径'
}
$videoPath = $raw.Trim().Trim('"').Trim("'")
if (-not $videoPath) { throw '没有提供视频路径' }
if (-not [System.IO.Path]::IsPathRooted($videoPath)) { $videoPath = Join-Path (Get-Location).Path $videoPath }
$videoPath = [System.IO.Path]::GetFullPath($videoPath)
if (-not (Test-Path -LiteralPath $videoPath -PathType Leaf)) { throw "找不到文件：$videoPath" }

# ---------------------------------------------------------------- 依赖与绑定校验
foreach ($bin in @('claude', 'python', 'ffmpeg', 'node')) {
    if (-not (Get-Command $bin -ErrorAction SilentlyContinue)) { throw "找不到命令 $bin，请先确认已安装并在 PATH 中" }
}
if (-not (Test-Path -LiteralPath $BindingPath)) { throw "缺少口播绑定：$BindingPath（请先在麦田里完成口播发布绑定）" }
if (-not (Test-Path -LiteralPath $SkillPath)) { throw "缺少口播流程文档：$SkillPath" }

# ---------------------------------------------------------------- 组装无头调用
$env:KOUBO_FROM_SHORTCUT = '1'

$addDirs = @($MaitianRoot, (Split-Path -Parent $videoPath))
$configPath = Join-Path $MaitianRoot 'config.json'
if (Test-Path -LiteralPath $configPath) {
    try {
        $cfg = Get-Content -LiteralPath $configPath -Raw -Encoding UTF8 | ConvertFrom-Json
        if ($cfg.rootPath) { $addDirs += [string]$cfg.rootPath }
    } catch { }
}

$prompt = @"
你是口播发布助手。用户把原片拖到桌面「一键发布」图标上，这就是明确的发布授权；全程不要向用户提问、不要等待任何确认，封面字、标题、各平台文案都由你自己定。

原片路径（完整）：$videoPath

请严格按 $SkillPath 的流程，把这条原片剪好并发布到八个平台（B站/抖音/小红书/视频号/YouTube/X/Instagram/Threads）：
1. 先运行 node scripts/koubo-publish.mjs start "$videoPath" 开始剪片（或接续已有工程）。
2. 校对字幕与剪点，需要时用 --cut-report 重导。
3. node scripts/koubo-publish.mjs prepare "$videoPath" "<剪辑记录>" "<封面秒数>" "<上行>" "<主标题>" 生成成片、字幕、封面。
4. 按 skills/maitian-publish/SKILL.md 模板写八个平台的发布素材.md。
5. node src/make-publish.mjs ... 与 node scripts/koubo-publish.mjs publish "$videoPath"。
6. node scripts/koubo-publish.mjs receipt "$videoPath"。

平台要本人扫码验证时，用飞书提醒并在其余平台继续，不要停下来等。最后输出各平台链接回执。

工具限制：你只能用 node、python、ffmpeg、ffprobe 这几条命令（通过 Bash），以及 Read/Write/Edit/Glob/Grep 处理文件；不要用其它工具。
"@

$claudeArgs = @('-p', '--model', 'deepseek-flash', $prompt)
foreach ($d in ($addDirs | Select-Object -Unique)) { $claudeArgs += '--add-dir'; $claudeArgs += $d }
$claudeArgs += '--allowedTools'
foreach ($t in @('Bash(node *)','Bash(python *)','Bash(ffmpeg *)','Bash(ffprobe *)','Read','Write','Edit','Glob','Grep')) { $claudeArgs += $t }

Write-Host ''
Write-Host '  一键发布（麦田）' -ForegroundColor Cyan
Write-Host '  ----------------------------------------' -ForegroundColor DarkGray
Write-Host ("  原片  ：{0}" -f $videoPath)
Write-Host '  目标  ：B站 / 抖音 / 小红书 / 视频号 / YouTube / X / Instagram / Threads'
Write-Host ''

Push-Location $MaitianRoot
$exitCode = 0
# 无头模式不会剥掉 settings 里模型名的 [1M] 后缀，这里显式用纯净模型名覆盖
$env:ANTHROPIC_MODEL = 'deepseek-flash'
$env:CLAUDE_CODE_SUBAGENT_MODEL = 'deepseek-flash'
try {
    & claude @claudeArgs
    $exitCode = $LASTEXITCODE
} finally {
    Pop-Location
    Remove-Item Env:\KOUBO_FROM_SHORTCUT -ErrorAction SilentlyContinue
}

Write-Host ''
if ($exitCode -eq 0) {
    Write-Host '  已交给助手跑完，各平台链接见飞书 / 麦田大屏。' -ForegroundColor Green
} else {
    Write-Host ("  助手执行中断（退出码 {0}），请查看上面的输出。" -f $exitCode) -ForegroundColor Red
}
exit $exitCode
