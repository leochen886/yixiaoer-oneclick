# 视频号一键发布 & 复盘

把微信视频号的「发布」和「数据复盘」做成一条自动流水线：

- **发布**：拖入口播原片 → 剪气口、加速、烧字幕 → 用成片字幕生成标题、描述、标签 → 浏览器上传成片并发表。
- **复盘**：发布 3 天后一条命令，自动拉回播放 / 点赞 / 评论 / 转发 / 收藏 + 热门评论，生成一份报告。
- **提醒**：开会话时自动提醒「哪些视频发了 ≥3 天还没复盘」。

```
发布：原片 → 剪口播 → 带字幕成片与 SRT → AI 文案 → ego 浏览器上传发表 → 自动留档（retro.json + 口播文案）
复盘：3 天后 → review-sph.ps1 "视频名" → 抓数据 + 评论 → report.md
```

---

## 环境依赖

| 依赖 | 用途 | 说明 |
|------|------|------|
| Windows 10 / 11 | 运行环境 | 脚本基于 Windows PowerShell 5.1 |
| ego-browser（ego-lite） | 浏览器自动化 | 首次需扫码登录视频号助手，之后会话保持 |
| ffmpeg | 抽首帧封面、抽音频 | 需在 PATH 中 |
| Python 3.10+ | 本地语音转写 | 使用 FunASR（离线）或 faster-whisper |
| Claude Code CLI（`claude`） | 生成标题 / 描述 / 标签 | 需可用的 Claude 账号 |

> 默认剪口播会把所选原片的音轨发给 DeepSeek 官方语音识别，再把听写全文、字词位置和候选停顿发给 DeepSeek Flash。画面在本机剪辑和导出。发布文案使用本机 `claude` 命令生成。

---

## 剪口播 SOP

把原片拖到 `剪口播.cmd` 可以单独剪辑并预览。也可以双击它，将视频路径粘贴或拖进窗口后按回车。完成后会自动打开成片文件夹。桌面「视频号一键发布」处理原片时使用同一套默认流程。成片、同名 SRT 和 `剪辑记录.json` 保存在 `output/<原名>_cut/`。重复导出会另外建目录。

1. 按原速提取音轨，发送给 DeepSeek ASR，按原片顺序拼成听写全文。本机用 FunASR 获取字词时间，并从波形测出连续安静区间。
2. DeepSeek Flash 读取全文和原声位置，校正有依据的错词，标出重说的前后两遍、需要缩短的气口和希望保留的停顿长度。
3. 程序把标记对回原片。重说要能定位两遍发音，并在前一遍两端找到合适的剪点才会删除。气口只缩短连续安静的部分。模型建议和实际执行结果均写入记录。
4. 画面和声音共用同一张原片剪点表，固定以 **1.3 倍速**导出，保留人声音调。记录中的 `cut_decisions` 列出每一刀的原因、原片起止、剪点 ID 和成片位置。
5. 把保留词的时间换算到成片上，按语意和停顿换幕，优先单行，保护完整词和称呼。较长分句可在同一幕换行。MP4 烧录的字幕和同名 SRT 使用同一份排版。

用 `--cut-report` 可以沿用已校对的原速逐字稿和剪点，在本机重新排版导出。要恢复某一刀，先在记录里找到该剪点的 `id`，再加上 `--restore-cut`。可以一次恢复多刀。

```powershell
python -X utf8 scripts/koubo.py "原片.mp4" --cut-report "成片目录/剪辑记录.json" --restore-cut "起始帧-结束帧"
```

默认导出在剪辑记录中标为 `proofreading.input_mode = deepseek_audio_first`。`audio_asr` 保存各段原声听写及其请求编号，`request_ids` 保存 Flash 的请求编号。`correction_audit` 和 `cut_audit` 分别记录校字、重说与气口的处理结果。`display_cues` 和同名 SRT 给出成片的字幕与播放时间。

如果手头已有与该原片绑定的原声听写记录，可用 `--asr-reference <本片核对依据.json>` 做专项复核。程序核对原片的 SHA-256，在本机对齐听写和剪点，再用 DeepSeek Pro 核对字幕文字。这个选项只读取已有记录。若单独复听某条原声音轨，可将该片段的原片时间及听写结果随记录一起用于校对。

密钥优先使用环境变量 `DEEPSEEK_API_KEY`，也可以复用本机 Claude Code 中指向 DeepSeek 官方接口的密钥。如果需要离线导出，可在命令行加 `--local-subtitles`，沿用本地转写。

命令行也可运行，例如把停顿阈值调到 0.6 秒：

```powershell
python -X utf8 scripts/koubo.py "D:\视频\原片.mp4" --min-pause 0.6
```

本入口需要 FFmpeg、ffprobe、NumPy、FunASR，以及本机已缓存的 paraformer-zh、fsmn-vad 和 ct-punc 模型。

## 一键发布

把视频文件**拖到 `视频号一键发布.cmd` 图标上**（或双击后粘贴路径回车），全自动完成：

1. 拖入原片后，先剪气口、固定 1.3 倍速并烧入字幕；拖入已有剪辑记录和同名 SRT 的项目成片时，直接使用所选版本。剪辑失败会停止发布。
2. 调 `claude` 生成标题（≤16 字）、描述、标签。
3. ego 浏览器登录视频号助手 → 上传视频（自动首帧封面）→ 填短标题 / 描述 / 标签。
4. 声明原创（按配置）→ **正式发表** → 核实已进入作品列表。

任何一步失败会停下并打印原因，日志和中间文件留在 `publish-runs/<时间戳>_<视频名>/`。

桌面入口对应的命令如下。已用其他软件剪好的视频也可以从命令行省略 `-CutKoubo`，直接使用所选视频。

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File scripts\oneclick-sph-ego.ps1 -CutKoubo "D:\视频\xxx.mp4"
```

从正式入口导出成片并停在上传前，可以运行下面的命令。此模式照常调用 DeepSeek，完成后在打开视频号前退出。运行目录里的 `prepared-video.txt` 记录实际成片路径。

```powershell
& ".\视频号一键发布.cmd" -PrepareOnly "D:\视频\原片.mp4"
```

需要测试上传和填表但不正式发表，可使用以下命令。

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File scripts\oneclick-sph-ego.ps1 -CutKoubo "D:\视频\xxx.mp4" -NoPublish
```

---

## 复盘

发布 3 天后，看这条跑得怎么样：

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File scripts\review-sph.ps1 "视频名"
```

`视频名` 是视频文件名（或 `publish-runs` 目录名片段，模糊匹配）。脚本会复用已登录的 ego 浏览器，到视频号助手找到那条作品，生成 `report.md`，内容包含：

- **数据**：播放 / 点赞 / 评论 / 转发 / 收藏（含发布时 vs 现在 vs 增量）
- **口播文案**：发布时留档的完整文案，方便对着数据按内容拆解
- **评论**：Top 20 热门评论

报告位置：`publish-runs/<时间戳>_<视频名>/report.md`。

---

## 自动提醒（可选）

`.claude/settings.json` 配了一个 SessionStart hook：每次在 Claude Code 里开会话，会自动扫 `publish-runs/`，提示哪些视频「已发布未复盘且 ≥3 天」。不用时删除 `.claude/settings.json` 即可。

---

## 配置

`oneclick-config.json`：

```json
{
  "createType": 1,
  "fallbackTags": [],
  "keepRuns": 20
}
```

| 字段 | 含义 | 默认 |
|------|------|------|
| `createType` | `1` = 声明原创，`2` = 不声明 | `1` |
| `fallbackTags` | AI 文案失败时兜底的标签（不带 `#`） | `[]` |
| `keepRuns` | 保留最近多少次运行的中间文件 | `20` |

---

## 目录结构

```
├── 视频号一键发布.cmd        一键发布入口（可拖拽视频）
├── oneclick-config.json      一键发布配置
├── publish-runs/             每次发布的运行记录（日志、retro.json、文案、报告）
├── scripts/
│   ├── oneclick-sph-ego.ps1  一键发布编排脚本（ego 版）
│   ├── review-sph.ps1        复盘入口
│   ├── check-pending-retros.ps1  待复盘提醒（SessionStart hook）
│   ├── oneclick_asr.py       本地转写（FunASR）
│   ├── transcribe*.py        本地转写（faster-whisper / FunASR）
│   └── ego/                  浏览器自动化脚本
│       ├── publish-sph-ego.mjs   上传 + 发表
│       ├── login-gate.mjs        登录态检测
│       └── collect-sph-post.mjs  复盘数据采集
└── .claude/settings.json     SessionStart 自动提醒配置
```

---

## 已知限制

- **只针对视频号一个平台**（原多平台 `yxer` CLI 已废弃移除）。
- **完播率 / 3 秒跳出率暂未采集**——它在视频号助手后台的 WebSocket 数据里，当前只采集了列表页可见的 5 项计数 + 评论。
- 视频号助手前端结构偶尔改版，`scripts/ego/` 下的选择器可能需要跟着更新。

## License

[MIT](LICENSE)
