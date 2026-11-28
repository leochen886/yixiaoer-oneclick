# Claude Code 命令使用说明书

> 依据 `claude --help`（Claude Code 2.1.319）整理。覆盖全部启动**选项**（66 项）与**子命令**（18 项），按使用场景分类，附示例。
> 日常在会话里输入 `/` 弹出的那些命令（`/doctor`、`/clear`、`/model` 等）是**斜杠命令**，属于另一套；本文讲的是终端里 `claude ...` 的用法。

---

## 一、快速上手

| 命令 | 作用 |
|---|---|
| `claude` | 直接进入交互式会话（默认模式） |
| `claude "帮我写个脚本"` | 带上问题直接启动会话 |
| `claude -p "帮我写个脚本"` | **非交互**：一次性问答，打印结果后退出（适合管道、脚本调用） |
| `claude -c` | 继续当前目录最近一次对话 |
| `claude -v` / `claude --version` | 查看版本号 |
| `claude --help` | 查看本文对应的原始帮助 |

---

## 二、启动选项（按场景分类）

### 1. 会话与恢复

| 选项 | 作用 | 示例 |
|---|---|---|
| `-c, --continue` | 继续当前目录最近一次对话 | `claude -c` |
| `-r, --resume [值]` | 按会话 ID 或名称恢复对话；不填则打开选择器 | `claude -r` / `claude -r abc123` |
| `--fork-session` | 恢复时**新建**会话 ID（不覆盖原会话），配合 `--resume`/`--continue` | `claude -r abc --fork-session` |
| `--from-pr [值]` | 恢复某个 PR 关联的会话（按 PR 号/链接，或打开选择器） | `claude --from-pr 123` |
| `--teleport [会话]` | 恢复一个 teleport 会话 | `claude --teleport` |
| `-n, --name <名称>` | 给会话设显示名（显示在提示框、`/resume` 选择器和终端标题） | `claude -n "改登录bug"` |
| `--session-id <uuid>` | 指定一个固定的会话 UUID | `claude --session-id <uuid>` |
| `--cloud [描述\|id\|url]` | 创建云端会话（填描述），或按 ID / claude.ai/code 链接附加已有会话 | `claude --cloud "代码审查"` |
| `--no-session-persistence` | 不保存会话到磁盘、不可恢复（仅 `-p` 模式） | `claude -p --no-session-persistence "..."` |

### 2. 模型与推理

| 选项 | 作用 | 示例 |
|---|---|---|
| `--model <模型>` | 指定本次会话的模型（可用别名 `fable`/`opus`/`sonnet`，或完整模型名） | `claude --model sonnet` |
| `--fallback-model <模型>` | 主模型过载/不可用时自动回退，可传逗号分隔的多个依次尝试 | `claude --fallback-model haiku` |
| `--effort <级别>` | 推理努力级别：`low` / `medium` / `high` / `xhigh` / `max` | `claude --effort high` |
| `--betas <头...>` | 向 API 请求附加 beta 头（仅 API key 用户） | `claude --betas beta1 beta2` |
| `--max-budget-usd <金额>` | 本次 API 花费上限（美元，仅 `-p` 模式） | `claude -p --max-budget-usd 5 "..."` |
| `--autocompact <auto\|tokens>` | 自动压缩窗口大小（`auto`，或 100k–1M 的具体 token 数） | `claude --autocompact 200k` |

### 3. 权限与工具控制

| 选项 | 作用 | 示例 |
|---|---|---|
| `--permission-mode <模式>` | 权限模式：`acceptEdits` / `auto` / `bypassPermissions` / `manual` / `dontAsk` / `plan` | `claude --permission-mode plan` |
| `--allowedTools, --allowed-tools <工具...>` | 允许的工具列表（逗号或空格分隔） | `claude --allowedTools "Bash(git *)" Edit` |
| `--disallowedTools, --disallowed-tools <工具...>` | 禁止的工具列表 | `claude --disallowedTools "Bash(rm *)"` |
| `--tools <工具...>` | 指定可用内置工具集；`""` 禁用全部，`"default"` 用默认集 | `claude --tools "Bash,Edit,Read"` |
| `--dangerously-skip-permissions` | **直接跳过所有权限检查**（仅建议无网沙箱用） | `claude --dangerously-skip-permissions` |
| `--allow-dangerously-skip-permissions` | 把"跳过权限"做成一个**可选**项（默认不启用） | `claude --allow-dangerously-skip-permissions` |
| `--permission-prompts <目标>` | `-p` 模式下谁回答权限提示：`host`（SDK 宿主）或 `none`（一律自动拒绝） | `claude -p --permission-prompts none "..."` |
| `--disable-slash-commands` | 禁用所有技能和斜杠命令 | `claude --disable-slash-commands` |

### 4. 安全 / 隔离模式

| 选项 | 作用 | 示例 |
|---|---|---|
| `--restricted` | **受限模式**：移除 Bash/PowerShell 等执行命令的工具和 WebFetch（除非 `--tools` 指名）；忽略用户/项目/本地设置；文件工具限制在工作目录内 | `claude --restricted` |
| `--safe-mode` | **安全模式**：禁用所有自定义（CLAUDE.md、技能、插件、hooks、MCP、自定义命令等），用于排查坏掉的配置 | `claude --safe-mode` |
| `--bare` | **极简模式**：跳过 hooks、LSP、插件同步、自动记忆、CLAUDE.md 自动发现等；认证只认 `ANTHROPIC_API_KEY` 或 apiKeyHelper | `claude --bare` |

### 5. 输入 / 输出

| 选项 | 作用 | 示例 |
|---|---|---|
| `-p, --print` | 非交互：打印响应后退出（适合管道）。注意：非交互下会跳过工作区信任对话框 | `claude -p "总结这段"` |
| `--output-format <格式>` | 输出格式：`text`（默认）/ `json`（单结果）/ `stream-json`（实时流） | `claude -p --output-format json "..."` |
| `--input-format <格式>` | 输入格式（仅 `-p`）：`text`（默认）/ `stream-json` | `claude -p --input-format stream-json` |
| `--json-schema <schema>` | 结构化输出的 JSON Schema 校验（仅 `-p`） | `claude -p --json-schema '{...}' "..."` |
| `--verbose` | 覆盖配置，开启详细输出 | `claude --verbose` |
| `-d, --debug [过滤]` | 调试模式，可按类别过滤（如 `hooks,mcp`；`!` 表示排除） | `claude --debug=hooks,mcp` |
| `--debug-file <路径>` | 调试日志写到指定文件（隐含开启调试） | `claude --debug-file ./debug.log` |
| `--include-hook-events` | 输出流中包含所有 hook 生命周期事件（仅 `stream-json`） | `claude -p --output-format stream-json --include-hook-events` |
| `--include-partial-messages` | 输出流中包含部分消息块（仅 `-p` + `stream-json`） | `claude -p --output-format stream-json --include-partial-messages` |
| `--replay-user-messages` | 把 stdin 收到的用户消息回写到 stdout 确认（仅 stream-json 双向） | `claude -p --input-format stream-json --output-format stream-json --replay-user-messages` |
| `--forward-subagent-text` | 转发子代理文本和思考块（仅 `-p` + `stream-json`） | `claude -p --output-format stream-json --forward-subagent-text` |
| `--prompt-suggestions [值]` | 是否开启下一句提示建议（`print`/SDK 模式下每轮发一个建议） | `claude -p --prompt-suggestions` |

### 6. 系统提示 / 上下文

| 选项 | 作用 | 示例 |
|---|---|---|
| `--system-prompt <提示>` | 用自定义系统提示替换默认提示 | `claude --system-prompt "你是..."` |
| `--append-system-prompt <提示>` | 在默认系统提示后追加内容 | `claude --append-system-prompt "用中文回答"` |
| `--system-prompt-snapshot <on\|off>` | 系统提示是否每会话记录一次并逐次复用（`on` 默认；`off` 每次都重新渲染） | `claude --system-prompt-snapshot off` |
| `--exclude-dynamic-system-prompt-sections` | 把每台机器不同的段（cwd、env、内存路径、git 状态）从系统提示移到首条用户消息，利于跨用户缓存复用 | `claude --exclude-dynamic-system-prompt-sections` |
| `--add-dir <目录...>` | 额外允许工具访问的目录 | `claude --add-dir "D:\代码" "D:\文档"` |
| `--file <规格...>` | 启动时下载文件资源，格式 `file_id:相对路径` | `claude --file file_abc:doc.txt` |

### 7. 配置与扩展

| 选项 | 作用 | 示例 |
|---|---|---|
| `--settings <文件或JSON>` | 加载额外设置（JSON 文件路径或 JSON 字符串） | `claude --settings ./my-settings.json` |
| `--setting-sources <源>` | 加载哪些设置源：`user` / `project` / `local`（逗号分隔） | `claude --setting-sources user,project` |
| `--agents <JSON或文件>` | 定义自定义代理（JSON 或文件路径） | `claude --agents '{"reviewer":{"description":"...","prompt":"..."}}'` |
| `--agent <代理>` | 指定本次会话的代理（覆盖 `agent` 设置） | `claude --agent reviewer` |
| `--mcp-config <配置...>` | 从 JSON 文件或字符串加载 MCP 服务器（空格分隔） | `claude --mcp-config ./mcp.json` |
| `--strict-mcp-config` | 只用 `--mcp-config` 指定的 MCP，忽略其它所有 MCP 配置 | `claude --mcp-config ./mcp.json --strict-mcp-config` |
| `--plugin-dir <路径>` | 从目录或 .zip 加载插件（仅本次会话，可重复） | `claude --plugin-dir ./my-plugin` |
| `--plugin-url <url>` | 从 URL 拉取插件 .zip（仅本次会话，可重复） | `claude --plugin-url https://.../p.zip` |

### 8. 后台 / 远程 / 隔离工作区

| 选项 | 作用 | 示例 |
|---|---|---|
| `--bg, --background` | 后台启动会话并立即返回，打印 id（供 `attach`/`logs`/`stop`/`rm` 用） | `claude --bg "跑个长任务"` |
| `--remote-control [名称]` | 启动带远程控制的交互会话（可命名） | `claude --remote-control my-session` |
| `--remote-control-session-name-prefix <前缀>` | 自动生成远程会话名的前缀（默认主机名） | `claude --remote-control-session-name-prefix dev` |
| `-w, --worktree [名称]` | 为本次会话新建 git worktree（可命名） | `claude -w` |
| `--tmux` | 为 worktree 建 tmux 会话（需 `--worktree`；`--tmux=classic` 用传统 tmux） | `claude -w --tmux` |
| `--environment <id>` | 在指定自托管环境上创建云端会话（`ccpool_...`） | `claude --environment ccpool_abc` |

### 9. 界面 / 集成

| 选项 | 作用 | 示例 |
|---|---|---|
| `--desktop` | 在 Claude 桌面 App 打开（配合 `--continue`/`--resume` 选会话） | `claude --desktop -c` |
| `--ide` | 启动时自动连接 IDE（仅当恰好一个有效 IDE 时） | `claude --ide` |
| `--chrome` / `--no-chrome` | 开启 / 关闭 Claude in Chrome 集成 | `claude --chrome` |
| `--brief` | 启用 SendUserMessage 工具（代理与用户通信） | `claude --brief` |
| `--ax-screen-reader` | 屏幕阅读器友好输出（纯文本、无边框动画） | `claude --ax-screen-reader` |

---

## 三、子命令（Commands）

| 命令 | 作用 | 示例 |
|---|---|---|
| `agents` | 管理后台代理 | `claude agents` |
| `attach <id\|名称>` | 在终端打开一个后台会话 | `claude attach abc123` |
| `auth` | 管理认证（登录/切换/退出） | `claude auth` |
| `auto-mode` | 检查或重置 auto 模式配置 | `claude auto-mode` |
| `doctor` | 检查 Claude Code 安装健康（只读） | `claude doctor` |
| `gateway` | 运行企业认证/遥测网关 | `claude gateway` |
| `import [来源]` | 从其它 AI 编码工具导入配置 | `claude import cursor` |
| `install [版本]` | 安装原生构建（`stable` / `latest` / 具体版本） | `claude install` |
| `logs <id\|名称>` | 打印后台会话最近的终端输出 | `claude logs abc123` |
| `mcp` | 配置和管理 MCP 服务器 | `claude mcp list` |
| `plugin` / `plugins` | 管理 Claude Code 插件 | `claude plugin list` |
| `purge [路径]` | 删除某项目在本机的数据（记录、任务、文件历史等） | `claude purge ./项目` |
| `respawn [id]` | 重启后台会话（`--all` 重启全部），使其跑当前版本 | `claude respawn --all` |
| `rm <id>` | 删除后台会话（及安全的 worktree） | `claude rm abc123` |
| `setup-token` | 设置长期认证 token（需订阅） | `claude setup-token` |
| `stop` / `kill <id>` | 停止后台会话（对话保留，可 `attach`/`--resume` 恢复） | `claude stop abc123` |
| `ultrareview [目标]` | 云托管多代理代码审查当前分支（或 PR 号 / 基础分支） | `claude ultrareview` |
| `update` / `upgrade` | 检查并安装更新（包管理器安装的走包管理器） | `claude update` |

---

## 四、常见用法速查

```bash
# 一次性问答（脚本/管道常用）
claude -p "总结 README 内容"

# 结构化 JSON 输出
claude -p --output-format json "列出仓库里的 .js 文件"

# 后台跑长任务，之后回来取
claude --bg "重构 src/utils"        # 打印会话 id
claude logs <id>                     # 看输出
claude attach <id>                   # 回到前台
claude stop <id>                     # 停止

# 用指定模型 + 高努力度
claude --model opus --effort high

# 只读规划，不动文件
claude --permission-mode plan "评估这个改动"

# 排查坏掉的配置（临时禁用所有自定义）
claude --safe-mode

# 每个会话独立 worktree，互不干扰
claude -w --tmux
```
