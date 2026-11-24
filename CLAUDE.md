# 视频号一键发布 & 复盘 工作目录

这里是视频号「一键发布 + 数据复盘」的工作目录。

> 原多平台 `yxer` CLI（蚁小二）已废弃并卸载，日常发布只走下面的 ego 浏览器一键发布，不再经过 yxer。

## 一键发布（视频号，全自动 · ego）

日常发视频号直接把视频拖到 `视频号一键发布.cmd`（或双击后粘贴路径回车），全自动完成、不中途确认：

- 剪口播默认导出 1080 × 1920 的 9:16 竖屏，中间是 4:3 画面，下方白字字幕。DeepSeek Pro 先通读成片并用一句话概括主旨，再一起拟上方白字和黄字。白字优先交代具体处境，90 像素黄字接住它，提出口播能回应的问题。白字只会重复或揭底时可省略。标题区两行合计最多 24 个全角字，均无标点。从原片开始发布时只接受当前版式的成片。
- 同名 `.srt` 或本地 FunASR 转写 → `claude -p` 生成标题/描述/标签 → ego 浏览器登录视频号助手 → 上传视频（自动首帧封面）→ 填短标题/描述/标签 → 声明原创 → **正式发表** → 核实进入 `post/list`
- 任何一步失败会停下并打印原因，日志和中间文件留在 `publish-runs/<时间戳>_<视频名>/`
- 账号主人已授权该入口**免逐次确认**；首次需扫码登录视频号助手（微信扫码），之后会话保持、无需再扫
- 配置：`oneclick-config.json`（`createType` 1=声明原创/2=不声明，`fallbackTags` 兜底标签，`keepRuns` 保留次数）
- 核心文件：编排脚本 `scripts\oneclick-sph-ego.ps1`，浏览器自动化 `scripts\ego\publish-sph-ego.mjs`（wujie shadow DOM + CDP 真实点击）

## 复盘

发布 3 天后一条命令复盘：

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File scripts\review-sph.ps1 "<视频名或目录名片段>"
```

会话开始时自动提醒「哪些视频发了 ≥3 天还没复盘」（`.claude/settings.json` 的 SessionStart hook）。

## 常用命令

```bash
# 一键发布（视频号，ego）
powershell -NoProfile -ExecutionPolicy Bypass -File scripts\oneclick-sph-ego.ps1 "D:\视频\xxx.mp4"
powershell -NoProfile -ExecutionPolicy Bypass -File scripts\oneclick-sph-ego.ps1 "D:\视频\xxx.mp4" -NoPublish

# 复盘
powershell -NoProfile -ExecutionPolicy Bypass -File scripts\review-sph.ps1 "视频名"
```
