# 蚁小二 工作目录

这是**蚁小二（yxer）多平台内容分发**的工作目录，也是 `yxer` CLI 的 `workDir`。

## 这个文件夹是干嘛的

把一条内容（视频 / 图文 / 文章）通过 `yxer` CLI 分发到多个平台账号（账号列表见本机 `yxer accounts list`）。

## 怎么操作

发布、查询、草稿、素材全部走 `yxer` CLI，配合 Claude Code 的 `yixiaoer` skill。直接说"帮我发个视频到抖音"即可。

发布核心流程（默认**本机发布**；账号主人未充值云代理，云发布走不通，说"云发布"才切云）：

1. `yxer doctor` / `yxer accounts list` —— 确认环境和账号
2. `yxer prepare <平台> <类型>` —— 生成表单契约
3. 上传素材、查询动态字段（`yxer upload` / `yxer query ...`）
4. 组装 `payload.json`
5. `yxer validate` → `yxer publish --dry-run` → **用户授权** → `yxer publish`

铁律：正式发布前必须用同一份 payload、同一套通道参数先过 `validate` 和 `--dry-run`，不允许跳过。

## 一键发布（视频号，全自动 · ego）

日常发视频号不用再走上面的 yxer 手工流程，直接用 ego 浏览器一键发布（不走 yxer，绕过充费限制）：

- 桌面快捷方式「视频号一键发布」，或根目录的 `视频号一键发布.cmd`
- 双击后把视频路径粘贴进窗口回车；或者**直接把视频文件拖到图标/cmd 上**
- 之后全自动、不中途确认：同名 `.srt` 或本地 FunASR 转写 → `claude -p` 生成标题/描述/标签 → ego 浏览器登录视频号助手 → 上传视频（自动首帧封面）→ 填短标题/描述/标签 → 声明原创 → **正式发表** → 核实进入 `post/list`
- 任何一步失败会停下并打印原因，日志和中间文件留在 `publish-runs/<时间戳>_<视频名>/`
- 账号主人已明确授权该入口**免逐次确认**；首次需要扫码登录视频号助手（微信扫码），之后会话保持、无需再扫
- 配置：`oneclick-config.json`（`createType` 1=声明原创/2=不声明，`fallbackTags` 兜底标签，`keepRuns` 保留次数）
- 核心文件：编排脚本 `scripts\oneclick-sph-ego.ps1`，浏览器自动化 `scripts\ego\publish-sph-ego.mjs`（走 wujie shadow DOM + CDP 真实点击）
- 只测流程不发表：`powershell -File scripts\oneclick-sph-ego.ps1 <视频路径> -NoPublish`

## 目录结构

- `视频号一键发布.cmd` —— 一键发布入口（可拖拽视频）
- `oneclick-config.json` —— 一键发布配置
- `publish-runs/` —— 一键发布的运行记录（payload、日志、转写、封面，默认保留最近 20 次）
- `publish-test/` —— 一次测试发布的素材与产物（视频号 `payload-sph.json`、小红书 `payload-xhs.json`，以及抽帧图、音频、转写文本）
- `scripts/` —— 辅助脚本（一键发布 ego 版 `oneclick-sph-ego.ps1`、旧 yxer 版 `oneclick-sph.ps1`、本地转写 `oneclick_asr.py`、`transcribe*.py`、环境体检 `doctor_scan.py` 等）
- `CLAUDE.md` —— 本说明文件

## 常用命令

```bash
yxer doctor                      # 环境体检
yxer accounts list --all --json  # 查账号
yxer config get                  # 查配置
yxer prepare <平台> <类型>        # 生成发布表单契约
yxer validate payload.json       # 校验 payload
yxer publish payload.json --dry-run  # 演练（不真正发布）

# 一键发布（视频号，ego）
powershell -NoProfile -ExecutionPolicy Bypass -File scripts\oneclick-sph-ego.ps1 "D:\视频\xxx.mp4"
powershell -NoProfile -ExecutionPolicy Bypass -File scripts\oneclick-sph-ego.ps1 "D:\视频\xxx.mp4" -NoPublish
```
