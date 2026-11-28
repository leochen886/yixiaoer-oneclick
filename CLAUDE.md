# 视频号一键发布 & 复盘 工作目录

这里是视频号「一键发布 + 数据复盘」的工作目录。

> 原多平台 `yxer` CLI（蚁小二）已废弃并卸载，日常发布只走下面的 ego 浏览器一键发布，不再经过 yxer。

## 一键发布（视频号，全自动 · ego）

日常发视频号直接把视频拖到 `视频号一键发布.cmd`（或双击后粘贴路径回车），全自动完成、不中途确认：

- 剪口播默认导出 1080 × 1920 的 9:16 竖屏，中间是 4:3 画面，下方白字字幕。DeepSeek Pro 先通读成片并用一句话概括主旨，再一起拟上方白字和黄字。白字优先交代具体处境，90 像素黄字接住它，提出口播能回应的问题。白字只会重复或揭底时可省略。标题区两行合计最多 24 个全角字，均无标点。从原片开始发布时只接受当前版式的成片。
- 前置秒级查重 → 登录态检测 → 自动剪口播（带 `koubo-glossary.json` 词表与复盘经验；已有校验通过的同原片成片则直接复用）→ 读取成片 `.srt`（外部成片无同名 `.srt` 时才走本地 ASR）→ `claude -p` 生成与顶部黄字呼应的标题/描述/标签 → ego 浏览器上传 → 填短标题/描述/标签 → 声明原创 → **正式发表** → 核实进入 `post/list` 并顺带同步首屏老视频数据
- 任何一步失败会停下并打印原因，日志和中间文件留在 `publish-runs/<时间戳>_<视频名>/`
- 账号主人已授权该入口**免逐次确认**；首次需扫码登录视频号助手（微信扫码），之后会话保持、无需再扫
- 配置：`oneclick-config.json`（`createType` 1=声明原创/2=不声明，`fallbackTags` 兜底标签，`keepRuns` 保留次数）、`koubo-glossary.json`（专名纠错词表）
- 核心文件：编排脚本 `scripts\oneclick-sph-ego.ps1`，剪辑核心 `scripts\koubo.py`，浏览器自动化 `scripts\ego\publish-sph-ego.mjs`（wujie shadow DOM + CDP 真实点击）

## 复盘与 `history/` 数据库（交互工作台 + AI 上下文闭环）

所有已发布视频的完整档案与复盘策略记忆统一保存在 `history/`（JSON 数据库，Git 忽略）：
- `history/index.json`：AI 必读总账（含每条视频紧凑指标与分阶段汇总）；`history/videos/<id>.json`：单片全档（口播、白黄字、时间线、窗口对照、评论与备注）；`history/playbook.json`：账号策略记忆（支持 `approved` / `proposed` / `rejected` 审核状态）。
- **交互工作台**：双击根目录 [视频号复盘工作台.cmd](视频号复盘工作台.cmd) 启动本地服务（默认 `http://127.0.0.1:8765/`），支持一键导入视频号后台下载的 `视频号动态数据明细.csv`（区分 `7d` / `30d` / `custom` 窗口保护全周期数据）、查看走势与口播全文、剔除测试视频、打标写备注、采纳或划掉 AI 规则。
- **命令行常用入口**：
  - 导入官方数据表：`python -X utf8 scripts/history_db.py import-csv "C:\Users\CL\Downloads\视频号动态数据明细.csv" --window 30d`（7 天导出务必用 `--window 7d`）
  - 同步本地发布记录：`python -X utf8 scripts/history_db.py sync-runs`
  - 对 `history/` 执行 AI 诊断：`python -X utf8 scripts/retro_analyze.py --history-dir history`
- **写文案 & 复盘纪律（必读）**：每当进行**数据复盘**、**定选题**、**写口播文案**或**拟短标题/白黄字**时，必须遵循 [.claude/skills/sph-retro/SKILL.md](.claude/skills/sph-retro/SKILL.md)：先读 `history/index.json` 与 `history/playbook.json`（跳过 `rejected`），分清 `<24h` 观察期与成熟期，在写文案前主动列出最近有效套路、避坑红线与开头前 3 秒建议。

> 原批量采集入口 `视频号一键复盘.cmd` / `scripts\review-all-sph.ps1` 仍保留用于 ego 抓评论与收藏，并会在抓完后自动同步写入 `history/`。

## 常用命令

```powershell
# 启动交互式复盘工作台
.\视频号复盘工作台.cmd

# 导入官方 CSV（7天用 7d，30天用 30d）并同步本地发布
python -X utf8 scripts\history_db.py sync-runs
python -X utf8 scripts\history_db.py import-csv "C:\Users\CL\Downloads\视频号动态数据明细.csv" --window 30d

# 一键发布（视频号，ego）
powershell -NoProfile -ExecutionPolicy Bypass -File scripts\oneclick-sph-ego.ps1 -CutKoubo "D:\视频\xxx.mp4"
powershell -NoProfile -ExecutionPolicy Bypass -File scripts\oneclick-sph-ego.ps1 -CutKoubo "D:\视频\xxx.mp4" -NoPublish

# 复盘（ego 抓评论与收藏 + 更新 history/）
powershell -NoProfile -ExecutionPolicy Bypass -File scripts\review-all-sph.ps1
powershell -NoProfile -ExecutionPolicy Bypass -File scripts\review-sph.ps1 "视频名"
```
