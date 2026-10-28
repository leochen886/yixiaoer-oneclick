# 蚁小二 工作目录

这是**蚁小二（yxer）多平台内容分发**的工作目录，也是 `yxer` CLI 的 `workDir`。

## 这个文件夹是干嘛的

把一条内容（视频 / 图文 / 文章）通过 `yxer` CLI 分发到多个平台账号。目前已绑定 5 个平台账号，全部在线可用：

| 平台 | 账号昵称 | 粉丝数 | 代理 |
|------|---------|--------|------|
| 抖音 | 咬金战AI | 81,821 | 北京 |
| 视频号 | 咬金战AI | 8,810 | 北京 |
| 快手 | 咬金战AI | 212,859 | 天津 |
| 小红书 | 咬金战AI | 1 | 重庆 |
| 哔哩哔哩 | 第一条上岸的鱼 | 32 | 重庆 |

## 怎么操作

发布、查询、草稿、素材全部走 `yxer` CLI，配合 Claude Code 的 `yixiaoer` skill（位于 `C:\Users\CL\.claude\skills\yixiaoer`）。直接说"帮我发个视频到抖音"即可。

发布核心流程（默认**本机发布**；账号主人未充值云代理，云发布走不通，说"云发布"才切云）：

1. `yxer doctor` / `yxer accounts list` —— 确认环境和账号
2. `yxer prepare <平台> <类型>` —— 生成表单契约
3. 上传素材、查询动态字段（`yxer upload` / `yxer query ...`）
4. 组装 `payload.json`
5. `yxer validate` → `yxer publish --dry-run` → **用户授权** → `yxer publish`

铁律：正式发布前必须用同一份 payload、同一套通道参数先过 `validate` 和 `--dry-run`，不允许跳过。

## 一键发布（视频号，全自动）

日常发视频号不用再走上面的手工流程，直接用一键发布：

- 桌面快捷方式「视频号一键发布」，或根目录的 `视频号一键发布.cmd`
- 双击后把视频路径粘贴进窗口回车；或者**直接把视频文件拖到图标/cmd 上**
- 之后全自动、不中途确认：抽首帧封面 → 同名 `.srt` 或本地 FunASR 转写 → `claude -p` 生成标题/描述/标签 → 上传视频和封面 → `validate` → `publish --dry-run` → **正式发布**（本机通道）→ 轮询 `query records` 核实 `allsuccessful`
- 任何一步失败会停下并打印原因，日志和中间文件留在 `publish-runs/<时间戳>_<视频名>/`
- 账号主人已明确授权该入口**免逐次确认**；`validate` + `dry-run` 仍会强制先跑
- 配置：`oneclick-config.json`（`createType` 1=声明原创，`publishChannel` 默认 local，`fallbackTags` 兜底标签，`waitSeconds` 等待时长，`keepRuns` 保留次数）
- 只测流程不发布：`powershell -File scripts\oneclick-sph.ps1 <视频路径> -NoPublish`

## 目录结构

- `视频号一键发布.cmd` —— 一键发布入口（可拖拽视频）
- `oneclick-config.json` —— 一键发布配置
- `publish-runs/` —— 一键发布的运行记录（payload、日志、转写、封面，默认保留最近 20 次）
- `publish-test/` —— 一次测试发布的素材与产物（视频号 `payload-sph.json`、小红书 `payload-xhs.json`，以及抽帧图、音频、转写文本）
- `scripts/` —— 辅助脚本（一键发布 `oneclick-sph.ps1`、本地转写 `oneclick_asr.py`、`transcribe*.py`、环境体检 `doctor_scan.py` 等）
- `CLAUDE.md` —— 本说明文件

## 常用命令

```bash
yxer doctor                      # 环境体检
yxer accounts list --all --json  # 查账号
yxer config get                  # 查配置
yxer prepare <平台> <类型>        # 生成发布表单契约
yxer validate payload.json       # 校验 payload
yxer publish payload.json --dry-run  # 演练（不真正发布）

# 一键发布（视频号）
powershell -NoProfile -ExecutionPolicy Bypass -File scripts\oneclick-sph.ps1 "D:\视频\xxx.mp4"
powershell -NoProfile -ExecutionPolicy Bypass -File scripts\oneclick-sph.ps1 "D:\视频\xxx.mp4" -NoPublish
```
