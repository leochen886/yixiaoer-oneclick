# 视频号一键发布 & 复盘

把微信视频号的「发布」和「数据复盘」做成一条自动流水线：

- **发布**：拖一个视频进去 → 自动转写 / 读字幕 → 生成标题、描述、标签 → 浏览器自动上传并发表。
- **复盘**：发布 3 天后一条命令，自动拉回播放 / 点赞 / 评论 / 转发 / 收藏 + 热门评论，生成一份报告。
- **提醒**：开会话时自动提醒「哪些视频发了 ≥3 天还没复盘」。

```
发布：视频 → 字幕/本地转写 → AI 文案 → ego 浏览器上传发表 → 自动留档（retro.json + 口播文案）
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

> 说明：转写走**本地模型**，不联网、不上传音视频；AI 文案生成走本机 `claude` 命令。

---

## 一键发布

把视频文件**拖到 `视频号一键发布.cmd` 图标上**（或双击后粘贴路径回车），全自动完成：

1. 找同名 `.srt` 字幕；没有则本地语音转写。
2. 调 `claude` 生成标题（≤16 字）、描述、标签。
3. ego 浏览器登录视频号助手 → 上传视频（自动首帧封面）→ 填短标题 / 描述 / 标签。
4. 声明原创（按配置）→ **正式发表** → 核实已进入作品列表。

任何一步失败会停下并打印原因，日志和中间文件留在 `publish-runs/<时间戳>_<视频名>/`。

命令行等价用法：

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File scripts\oneclick-sph-ego.ps1 "D:\视频\xxx.mp4"
```

只测流程、不真正发表（会停在上传+填表之后）：

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File scripts\oneclick-sph-ego.ps1 "D:\视频\xxx.mp4" -NoPublish
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

- **只针对视频号一个平台**（其它平台见 `yxer` CLI）。
- **完播率 / 3 秒跳出率暂未采集**——它在视频号助手后台的 WebSocket 数据里，当前只采集了列表页可见的 5 项计数 + 评论。
- 视频号助手前端结构偶尔改版，`scripts/ego/` 下的选择器可能需要跟着更新。

## License

[MIT](LICENSE)
