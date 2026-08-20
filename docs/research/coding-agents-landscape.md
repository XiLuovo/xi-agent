# 通用 Coding Agent 一手资料调研与 Xi 项目起点

> 调研日期：2026-08-20  
> 范围：OpenAI Codex CLI、Anthropic Claude Code、Pi Coding Agent、xAI Grok Build；补充 DeepSeek Harness、Aider、OpenHands 作为架构坐标。  
> 方法：只采用产品官方文档、项目官方仓库和官方发布资料。本文比较的是公开可验证的设计，不把 GitHub Star 或宣传口径当作真实使用率排名。

## 结论先行

这几类产品已经出现了高度稳定的共同内核：

1. 模型在“推理 → 调用工具 → 观察结果 → 继续推理”循环中工作；
2. 文件、Shell、搜索和补丁是最小工具面；
3. 仓库规则、技能和按需检索共同组成上下文；
4. 权限决策与 OS 级隔离是两个不同层次；
5. 会话需要持久化、压缩、恢复和机器可读事件；
6. 扩展能力逐渐收敛到 Skills、Hooks、MCP、插件和 SDK；
7. 真正能形成项目壁垒的不是再做一个 TUI，而是把上述机制做成**可解释、可替换、可回放、可评测**的运行时。

因此，Xi 不宜从“做一个功能齐全的 Claude Code 克隆”开始。更合适的起点是：

> 用 Python 实现一个小而完整的仓库级 Agent Runtime：最小工具循环 + 事件化会话 + 权限策略/执行隔离分层 + 可替换上下文策略 + 可重复 benchmark。

这条路线既能在较短时间内跑通真实任务，也天然产生三条可以深入追问的简历亮点：

- 可回放的事件化 Agent Runtime；
- 权限策略与执行隔离分层的安全模型；
- 可实验比较的仓库上下文编译器。

## 横向对比

| 维度 | Codex CLI | Claude Code | Pi Coding Agent | Grok Build |
| --- | --- | --- | --- | --- |
| 产品形态 | 本地终端 Agent，并提供非交互模式、SDK/App Server/MCP 等集成面 | CLI、IDE、桌面和 Web 共用同一 Agent 能力，并提供 Agent SDK | 强调“最小 harness”，可作为 CLI、JSON/RPC 进程或 SDK 嵌入 | 交互式 TUI、Headless 和 ACP Agent 三种入口 |
| 默认工作闭环 | 探索仓库、规划、编辑、运行本地工具并检查 diff | 读取代码库、编辑文件、运行命令、验证结果 | 默认只有 `read`、`write`、`edit`、`bash` 四个工具 | 本地工具调用循环，可结合计划、子 Agent、工作流和后台任务 |
| 上下文 | 分层 `AGENTS.md`、Skills 渐进加载、文件/IDE 上下文、压缩、子 Agent | `CLAUDE.md`、自动记忆、Skills、MCP、压缩与子 Agent | Context files、显式 `@file`、JSONL 树会话、compaction；核心保持简单 | 分层 `AGENTS.md`/兼容 `CLAUDE.md`、Skills、记忆、compact、子 Agent |
| 权限与隔离 | approval policy、execpolicy rules、read-only/workspace-write/full-access sandbox；不同 OS 有对应隔离实现 | allow/ask/deny 规则、多个 permission mode、hooks，并可启用 OS 级 sandbox | 核心明确声明**没有内置权限系统**，建议容器化或由扩展实现确认流 | Ask/Auto/Always-approve 权限层与独立 sandbox profile；Linux Landlock、macOS Seatbelt |
| 扩展 | Skills、Plugins、MCP、Hooks、Subagents、SDK/App Server | Skills、Plugins、Hooks、MCP、Subagents、Agent SDK | TypeScript Extensions、Skills、Prompt Templates、Pi Packages、SDK/RPC；刻意不内置 MCP/计划/子 Agent | Skills、Plugins、Hooks、MCP、LSP、Marketplaces、Subagents、ACP |
| 会话/轨迹 | 本地保存、resume/fork/compact；`codex exec --json` 输出细粒度 JSONL 状态事件 | resume/branch/compact/checkpoint/rewind；`stream-json` 可输出主 Agent 与子 Agent 事件 | 追加式 JSONL 树，`id/parentId` 支持原地分支；压缩点、分支摘要、扩展状态均为正式条目 | 自动保存 prompt、response、tool call 和 file snapshot；resume/fork/rewind/compact/export |
| 自动化模式 | 交互 TUI 与 `codex exec`；支持结构化输出 schema | 交互模式与 `claude -p`；JSON、stream-json、JSON Schema | interactive、print/JSON、RPC、SDK 四类 | TUI、Headless、ACP；另有 Plan、Auto、Always-approve 和 Workflows |
| 评测基础 | JSONL 事件和结构化最终输出可作为自建 eval 的输入 | 事件流提供 session、usage、估算成本和子 Agent 父子关系 | JSONL 轨迹、token/cache/cost 统计和 telemetry 包便于采集实验数据 | streaming-json 与包含工具调用、文件快照的持久会话可供离线分析 |
| 对 Xi 最有价值的启发 | 安全配置是“权限决策 × sandbox”的二维组合；事件流适合自动评测 | 产品级权限语义、checkpoint 与多入口共用引擎 | 最小核心、强扩展，以及很清晰的树形事件会话模型 | 会话快照、worktree 隔离、ACP 和兼容层的产品化组合 |

## 1. OpenAI Codex CLI

### 核心循环与入口

Codex CLI 官方定位是运行在本机的 coding agent；官方工作流明确包括探索陌生代码、规划变更、编辑文件、运行本地开发工具以及查看 diff。[CLI 功能](https://developers.openai.com/codex/cli/features)

非交互入口 `codex exec` 面向脚本和 CI。默认进度写入 `stderr`、最终消息写入 `stdout`；加 `--json` 后，`stdout` 变为 JSONL 事件流，包含 thread、turn、message、command execution、file change、MCP call、web search 和 plan update 等事件。[Non-interactive mode](https://developers.openai.com/codex/noninteractive)

这意味着 Codex 把“交互产品”和“可编程 Agent Runtime”放在同一个内核上。对 Xi 来说，比复制界面更值得借鉴的是：同一次运行应该既能供人阅读，也能输出稳定的机器事件协议。

### 上下文组织

Codex 启动时构建分层指令链：先读取用户级 `AGENTS.md`/`AGENTS.override.md`，再从项目根目录走到当前目录，每层最多选一个规则文件，越靠近当前目录的规则越具体；默认组合大小有限制。[AGENTS.md 指南](https://developers.openai.com/codex/guides/agents-md)

Skills 使用渐进披露：初始上下文只放技能的名称和描述，真正选中时才加载完整 `SKILL.md`。这一点很适合大量可选能力，因为它避免把每个工具说明和工作流全文都塞进系统提示词。[Codex Skills](https://developers.openai.com/codex/skills)

长会话可通过 `/compact` 压缩，`/status` 能查看剩余上下文；较大的探索任务还可以交给子 Agent，让主线程只接收总结，减少主上下文污染。[Developer commands](https://learn.chatgpt.com/docs/developer-commands) [Subagents](https://learn.chatgpt.com/docs/agent-configuration/subagents)

### 权限与 sandbox

Codex 把两件事明确分开：

- `approval_policy` 决定哪些行为需要询问、拒绝或自动处理；
- `sandbox_mode` 决定已经获准执行的进程，实际能读写哪些路径、能否访问网络。

官方组合包括 read-only、workspace-write 和 danger-full-access；非交互模式默认是 read-only。命令还可以通过 execpolicy rules 被设为 allow、prompt 或 forbidden，并能用 `codex execpolicy check` 单独验证规则命中结果。[Agent approvals & security](https://learn.chatgpt.com/docs/agent-approvals-security) [Rules](https://developers.openai.com/codex/exec-policy) [Non-interactive mode](https://developers.openai.com/codex/noninteractive)

这是 Xi 应直接采用的概念分层：**“是否允许调用”不能代替“调用后能做什么”**。

### 扩展、会话与可观测性

Codex 的公开扩展面包括 Skills、Plugins、MCP、Hooks 和 Subagents。插件可打包技能和 MCP 连接，MCP server 可以按服务器和工具配置可用性及审批策略。[Skills & Plugins](https://learn.chatgpt.com/docs/skills-and-plugins) [Configuration Reference](https://developers.openai.com/codex/config-reference)

会话支持 resume、fork、archive 和 compact；非交互任务同样可以根据 session ID 继续。机器事件流已经足够记录一次任务的工具调用、文件修改与错误，因此 Xi 的 benchmark 不应只保存“最终是否通过”，还应保存整条运行轨迹。[Developer commands](https://learn.chatgpt.com/docs/developer-commands) [Non-interactive mode](https://developers.openai.com/codex/noninteractive)

## 2. Anthropic Claude Code

### 核心循环与统一引擎

Claude Code 官方定义是能读取代码库、编辑文件、运行命令并与开发工具集成的 agentic coding tool；终端、IDE、桌面和 Web 是不同 surface，但共享底层 Claude Code engine。[Claude Code Overview](https://docs.anthropic.com/en/docs/claude-code/overview)

`claude -p` 把同一套能力暴露给脚本和 CI。Anthropic 还明确说明 Agent SDK 提供与 Claude Code 相同的 tools、agent loop 和 context management，并同时提供 Python 与 TypeScript SDK。[Programmatic usage](https://docs.anthropic.com/en/docs/claude-code/headless)

对 Xi 的启发是把 `AgentRuntime` 与 CLI 分离：终端只是 adapter，未来才能自然增加 benchmark runner、IDE/RPC 或 Web UI。

### 上下文、记忆和扩展

Claude Code 以 `CLAUDE.md` 存储项目规则，并支持自动记忆；Skills 封装可重复流程；MCP 连接外部数据与工具；Hooks 在工具调用和会话生命周期前后执行确定性逻辑。[Overview](https://docs.anthropic.com/en/docs/claude-code/overview) [Memory](https://docs.anthropic.com/en/docs/claude-code/memory) [Skills](https://docs.anthropic.com/en/docs/claude-code/skills) [MCP](https://docs.anthropic.com/en/docs/claude-code/mcp) [Hooks](https://docs.anthropic.com/en/docs/claude-code/hooks)

Skills 默认也是按需加载，而不是把完整内容常驻初始上下文。调用后的技能内容会留在会话里，并在自动压缩时按预算重新附加。[Skills](https://docs.anthropic.com/en/docs/claude-code/skills)

### 权限模型与 checkpoint

Claude Code 的权限规则分为 allow、ask、deny，优先级为 deny → ask → allow；裸工具 deny 可以直接把工具从模型可见上下文中移除。权限模式包括 Manual、Plan、Auto、dontAsk 和 bypassPermissions。官方强调权限由客户端强制执行，而不是依赖提示词约束模型。[Permissions](https://docs.anthropic.com/en/docs/claude-code/permissions)

Checkpoint 会在每个用户 prompt 前记录由文件编辑工具产生的文件状态，并与会话一起保存；用户可通过 `/rewind` 恢复会话、代码或二者。但 Bash、外部程序和多数后台子 Agent 造成的文件变化不在完整恢复保证内，官方仍建议使用 Git 作为长期版本控制。[Checkpointing](https://docs.anthropic.com/en/docs/claude-code/checkpointing)

这揭示了一个很适合 Xi 的研究问题：文件级 checkpoint 不能等同于完整事务回滚。Xi 可以把“可观察修改”限定为统一的 patch 工具，或用 Git worktree/commit 形成更可靠的回滚边界。

### 轨迹与自动化

`--output-format stream-json` 可以输出实时 JSONL；事件带有 session metadata，子 Agent 消息通过 `parent_tool_use_id` 关联父调用，允许重建嵌套执行树。最终结果还可带 usage 和估算成本。[Programmatic usage](https://docs.anthropic.com/en/docs/claude-code/headless)

这比普通文本日志更接近真正的 Agent trace：它保留因果关系，而不只是按时间打印字符串。

## 3. Pi Coding Agent

### 名称确认与设计哲学

用户所说的 Pi 可以确认是 **Pi Agent Harness / `pi-coding-agent`**。官方仓库当前位于 [`earendil-works/pi`](https://github.com/earendil-works/pi)，由统一多供应商 LLM API、Agent runtime、TUI 和 coding-agent CLI 等包组成。[Pi 官方仓库](https://github.com/earendil-works/pi)

Pi 明确把自己称为“minimal terminal coding harness”。默认只给模型四个工具：`read`、`write`、`edit` 和 `bash`。它刻意不内置 subagents、plan mode、permission popups 和 MCP，而是要求通过 Extensions、Skills 或第三方 Pi Packages 自行组合。[Pi Coding Agent README](https://github.com/earendil-works/pi/blob/main/packages/coding-agent/README.md)

这种“有意留白”对 Xi 很重要：项目亮点不取决于内置功能数量，而取决于核心是否足够小、扩展点是否真的能承载变化。

### Runtime 与扩展

Pi 把底层 Agent loop 和 state management 放在 `pi-agent-core`，上层 coding agent 可通过 interactive、print/JSON、RPC 和 SDK 四类方式运行。Extensions 是 TypeScript 模块，可注册工具、命令、快捷键、UI 和事件处理器，也能阻断/修改工具调用、替换 compaction、保存扩展状态。[Pi 官方仓库](https://github.com/earendil-works/pi) [Pi Coding Agent README](https://github.com/earendil-works/pi/blob/main/packages/coding-agent/README.md) [Extensions](https://github.com/earendil-works/pi/blob/main/packages/coding-agent/docs/extensions.md)

Pi Package 可以一起分发 extensions、skills、prompts 和 themes。它还支持统一多模型供应商与运行时模型切换。[Pi Coding Agent README](https://github.com/earendil-works/pi/blob/main/packages/coding-agent/README.md)

### 会话模型：最值得 Xi 借鉴的部分

Pi 的会话是追加式 JSONL。除 header 外，每个条目有 `id` 和 `parentId`，所以一个文件内部就是一棵树；分支不需要复制整段历史。正式条目类型包含 message、model change、thinking-level change、compaction、branch summary、extension state 和 extension-injected context。[Session File Format](https://github.com/earendil-works/pi/blob/main/packages/coding-agent/docs/session-format.md)

Compaction entry 既记录摘要，也可带保留的尾部消息；构建模型上下文时只沿当前 leaf 回溯，并应用最近压缩点。普通 extension state 不进入 LLM context，而 custom message 可以进入。[Session File Format](https://github.com/earendil-works/pi/blob/main/packages/coding-agent/docs/session-format.md)

这是一条非常适合 Xi 的面试亮点：

> 用 append-only event tree 同时解决持久化、分支、恢复、回放、压缩检查点和扩展状态，而不是只保存一份可变 messages 数组。

### 安全边界

Pi 官方明确声明它没有内置文件系统、进程、网络或凭证权限系统，默认拥有启动用户的权限；需要更强边界时应使用 Gondolin、Docker、OpenShell 等外部隔离，或自行写扩展确认流。[Pi 官方仓库：Permissions & Containerization](https://github.com/earendil-works/pi#permissions--containerization)

这不是单纯缺点，而是一种架构选择。不过 Xi 若想把“安全执行”作为简历亮点，就不能照抄 Pi 的默认做法，必须把 policy 与 executor 作为一等模块。

## 4. xAI Grok Build

### 名称歧义已经消除

“Grok Build”现在是 xAI 官方名称，不只是社区俗称。官方将其定义为可扩展 coding agent，可通过 TUI、Headless 或 Agent Client Protocol（ACP）使用。[Grok Build Overview](https://docs.x.ai/build/overview)

早期 coding 模型 `grok-code-fast-1` 在当前模型目录中是 `grok-build-0.1` 的别名；当前官方文档称 Grok Build 由 `grok-4.6` 驱动，并允许把同一模型用于自建 agent loop。这说明需要区分：**Grok Build 是 harness/产品，Grok Code Fast 或 Grok 4.6 是模型。** [xAI Docs Index](https://docs.x.ai/llms.txt) [Grok Build Overview](https://docs.x.ai/build/overview)

### 产品化组合

Grok Build 的 headless 模式支持 plain、json、streaming-json；ACP 通过 stdin/stdout JSON-RPC 把 Agent 嵌入 IDE 或其他工具。会话在 TUI、Headless、ACP 之间使用同一套存储语义。[Headless & Scripting](https://docs.x.ai/build/cli/headless-scripting) [Sessions](https://docs.x.ai/build/features/sessions)

它内置 Plan、Auto、Always-approve 模式；支持独立子 Agent、后台任务、持久 todo、工作流和多会话 Dashboard。[Modes and Commands](https://docs.x.ai/build/modes-and-commands) [Subagents](https://docs.x.ai/build/features/subagents) [Agent Dashboard](https://docs.x.ai/build/features/dashboard)

### 会话、快照与 worktree

Grok 自动保存 prompts、responses、tool calls 和 file snapshots；支持 resume、fork、rewind、compact 和 transcript export。Worktree session 在独立 Git checkout 中运行，使并行 Agent 不会覆盖彼此的文件。[Sessions](https://docs.x.ai/build/features/sessions) [Worktrees](https://docs.x.ai/build/features/worktrees)

对于 Xi，worktree 比“多 Agent”本身更值得优先：如果没有工作区隔离，并发 Agent 只是更快地制造竞态和不可解释的修改。

### 权限、sandbox 和兼容层

Grok 将 Ask/Auto/Always-approve 权限模式与 sandbox 分开。Sandbox 默认关闭，可选择 off、workspace、devbox、read-only 和 strict profile；Linux 使用 Landlock，macOS 使用 Seatbelt，但官方也列出了网络限制和凭证路径保护的边界。[Permissions](https://docs.x.ai/build/features/permissions) [Sandbox](https://docs.x.ai/build/features/sandbox)

Plugins 可提供 skills、agents、hooks、MCP 和 LSP；同时兼容 Claude Code 的 marketplaces、plugins、skills、MCP、agents、hooks 和规则文件，也支持 `AGENTS.md` 规则族。[Skills, Plugins & Marketplaces](https://docs.x.ai/build/features/skills-plugins-marketplaces) [Project Rules](https://docs.x.ai/build/features/project-rules)

这一点给 Xi 的启发不是去兼容所有生态，而是尽早把外部协议放在 adapter 层：例如先设计内部 `Tool` 协议，再决定是否接 MCP；先设计内部 run event，再决定是否暴露 ACP/RPC。

## 5. 三个补充架构坐标

### DeepSeek Harness：插件化与完整轨迹

DSH 的官方核心口号是 “Everything is a plugin”：模型、工具、Skills、Session、Sandbox、Storage、Loop、Scheduling 和 UI 都由 Cordis 插件提供；每次运行记录为 append-only session log，Trajectory view 可按来源检查，resume/fork/search/replay 共用同一事件流。其 Standard、Code、Minimal、Creator 模式分别面向完整执行、程序化工具编排、最小模型评测和 runtime/plugin 创作。该项目仍处于 developer preview，官方明确提示 API 会继续变化。[DeepSeek Harness 官方页](https://www.deepseek.com/harness/en/) [官方仓库](https://github.com/deepseek-ai/deepseek-harness)

Xi 不需要复制 Cordis，但可以借鉴两个可落地原则：稳定的小接口，以及所有副作用都生成可追踪事件。

### Aider：repo map 与 benchmark

Aider 为整个 Git 仓库生成包含重要类、函数、类型和调用签名的 concise repo map；大型仓库中再用依赖图排序，按 token budget 选择最相关部分。它还维护公开代码编辑 benchmark，任务要求模型生成可应用的编辑，并允许基于测试失败输出再修一次。[Repository Map](https://aider.chat/docs/repomap.html) [Code Editing Benchmarks](https://aider.chat/docs/benchmarks.html) [官方仓库](https://github.com/Aider-AI/aider)

这是 Xi 上下文研究最清楚的 baseline：`rg`/文件搜索驱动的按需探索，对比“符号图 + token budget”的 repo map。

### OpenHands：执行平台与部署边界

OpenHands 当前官方仓库把 Agent Canvas 定位为自托管的 coding-agent/automation 控制中心，可连接 OpenHands、Claude Code、Codex、Gemini 或其他 ACP Agent，并把 backend 放在本机、Docker、VM 或云环境；官方明确警告非 sandbox 方式会获得主机文件系统权限。[OpenHands 官方仓库](https://github.com/All-Hands-AI/OpenHands)

它提醒我们：当目标从“一个 CLI Agent”扩展为“多 Agent 平台”时，执行后端、持久服务、容器部署和调度会迅速成为主体工程。Xi 的第一阶段不应进入这个范围。

## 6. 从共性中提炼 Xi 的设计

四个重点产品都已经提供了足够丰富的运行数据，但其官方 CLI 文档主要面向执行和集成，而不是为 Xi 这样的研究项目直接提供统一 benchmark。Aider 的显式代码编辑 benchmark 和 DSH 的 Minimal mode 更接近评测参照。因此 Xi 应把 eval runner 作为自己的核心组件，而不是把“能输出 JSON”误认为“已经可评测”。

### 6.1 最小核心，而不是“大而全”

第一版只需要这条闭环：

```text
Task
  → Context Builder
  → Model
  → Tool Call
  → Policy Decision
  → Executor
  → Observation/Event
  → Model（重复）
  → Final Result
```

建议核心接口保持在五个：

```python
class Model(Protocol): ...
class Tool(Protocol): ...
class Policy(Protocol): ...
class Executor(Protocol): ...
class SessionStore(Protocol): ...
```

插件化的证据不是写了 `Plugin` 基类，而是每个 seam 至少有两个真实实现，例如：

- `OpenAICompatibleModel` / `ScriptedModel`；
- `LocalExecutor` / `DryRunExecutor`；
- `JsonlSessionStore` / `MemorySessionStore`；
- `SearchContextBuilder` / `RepoMapContextBuilder`。

### 6.2 Event 先于 UI

运行时先定义稳定事件，再做 Rich TUI：

```text
run_started
model_requested
model_responded
tool_proposed
policy_decided
tool_started
tool_finished
file_changed
context_compacted
run_finished / run_failed
```

每条事件至少带 `run_id`、`event_id`、`parent_id`、timestamp、payload 和 usage。`parent_id` 让工具调用、子任务和分支保持因果关系。第一版不必实现完整事件溯源数据库，JSONL 足够；但写入必须 append-only，模型上下文应由事件投影生成。

### 6.3 Policy 与 Executor 分层

建议不要把“危险命令字符串检查”叫作 sandbox：

- Policy：决定允许、询问、拒绝；检查路径、命令、预算和工具参数；
- Executor：真正执行；限定 cwd、超时、输出大小、环境变量，并在未来切换到 Docker/OS sandbox；
- Audit event：记录做出决定的规则和理由。

第一版可以安全地称为“受限本地执行器”，等接入 Docker 或 OS 隔离后再称 sandbox。

### 6.4 Context Builder 应该是研究主线

至少保留两套可比较策略：

1. `SearchContextBuilder`：Agent 用 `list/read/search` 自主找文件；
2. `RepoMapContextBuilder`：预先抽取符号和依赖关系，在 token budget 内提供相关仓库地图。

统一记录：送入文件、片段来源、token 数、检索耗时、无效读取次数。这样“上下文工程”就能从口号变成实验。

## 7. 推荐实施顺序

### 阶段 0：先固定实验任务

准备 3 个小型 Python 仓库任务，每个任务都包含：自然语言 issue、初始 commit、唯一确定的测试命令和期望行为。先固定任务，避免后面为了让 Agent 看起来成功而移动目标。

### 阶段 1：最小可运行切片

实现：

- `xi run "任务" --workspace PATH`；
- 一个 OpenAI-compatible model adapter；
- `read_file`、`search_code`、`apply_patch`、`run_command`；
- 最大步数、超时和工作区路径约束；
- JSONL 事件输出；
- 在一个固定 Bug 上完成“定位 → 修改 → 测试 → 总结”。

此时不要做动态插件市场、Web UI、多 Agent、长期记忆或 MCP。

### 阶段 2：让运行可解释、可恢复

实现 append-only session tree、resume/fork、事件投影和一次 context compaction。用 ScriptedModel 重放固定 tool-call 序列，证明 Agent loop 与真实模型解耦。

### 阶段 3：安全模型

加入 allow/ask/deny policy、命令解析、路径边界和 DryRunExecutor；再选择 Docker 作为第二执行后端。用相同任务证明更换 executor 不需要修改 Agent loop。

### 阶段 4：上下文实验

实现 repo map baseline，与纯工具搜索策略比较。建议至少记录：

- task success rate；
- pass@1；
- input/output tokens；
- tool call 数量；
- 首次定位正确文件所需步数；
- wall time；
- patch size；
- policy denial/approval 次数。

### 阶段 5：最后才做产品层

在核心和实验稳定后，再增加 Rich TUI、MCP adapter、插件发现、多 Agent 或 worktree。每增加一个能力都要回答：它改变了哪条实验结果，还是只是界面功能？

## 8. 可以预埋的三条简历亮点

以下是目标写法，只有完成对应实现和实验后才能放进简历：

1. **事件化运行时**  
   设计 append-only JSONL session tree，统一记录模型响应、工具调用、策略决策和文件修改，支持任务恢复、分支和确定性回放。

2. **分层安全执行**  
   将 tool authorization 与 process isolation 解耦，实现 allow/ask/deny policy、工作区路径约束与可替换本地/Docker executor，并记录可审计决策理由。

3. **可评测上下文工程**  
   实现 search-driven 与 dependency-aware repo map 两种上下文策略，在固定仓库级 Bug benchmark 上比较成功率、token、工具调用和定位步数。

这三条会自然引导面试官追问数据模型、幂等/恢复、命令安全、上下文裁剪和实验设计；都能落到代码与数据，而不是停留在“调用了大模型 API”。

## 9. 最终建议

继续使用 Python。DSH/Cordis 说明了插件化运行时可以做到什么，但没有证明 Xi 必须使用 TypeScript。Pi 甚至反向证明：真正重要的是核心边界和事件模型，而不是语言本身。

Xi 的第一个 milestone 应定义为：

> 在一个固定 Python Bug 仓库中，Xi 能在受限工作区内自主搜索、修改、运行唯一指定测试；全过程写入可重放 JSONL trace；相同 Agent loop 可切换真实模型与 ScriptedModel。

完成这个切片后，项目已经具备可演示的 Coding Agent；随后再按“安全执行 → session tree → repo map benchmark”的顺序，把它变成有研究和面试深度的项目。

## 主要一手资料索引

### OpenAI Codex

- [Codex CLI 功能](https://developers.openai.com/codex/cli/features)
- [Non-interactive mode](https://developers.openai.com/codex/noninteractive)
- [Agent approvals & security](https://learn.chatgpt.com/docs/agent-approvals-security)
- [Exec policy rules](https://developers.openai.com/codex/exec-policy)
- [AGENTS.md](https://developers.openai.com/codex/guides/agents-md)
- [Skills](https://developers.openai.com/codex/skills)
- [Developer commands](https://learn.chatgpt.com/docs/developer-commands)
- [Subagents](https://learn.chatgpt.com/docs/agent-configuration/subagents)

### Anthropic Claude Code

- [Overview](https://docs.anthropic.com/en/docs/claude-code/overview)
- [Permissions](https://docs.anthropic.com/en/docs/claude-code/permissions)
- [Programmatic/headless usage](https://docs.anthropic.com/en/docs/claude-code/headless)
- [Checkpointing](https://docs.anthropic.com/en/docs/claude-code/checkpointing)
- [MCP](https://docs.anthropic.com/en/docs/claude-code/mcp)
- [Skills](https://docs.anthropic.com/en/docs/claude-code/skills)
- [Hooks](https://docs.anthropic.com/en/docs/claude-code/hooks)

### Pi

- [Pi 官方仓库](https://github.com/earendil-works/pi)
- [Pi Coding Agent README](https://github.com/earendil-works/pi/blob/main/packages/coding-agent/README.md)
- [Session File Format](https://github.com/earendil-works/pi/blob/main/packages/coding-agent/docs/session-format.md)
- [Extensions](https://github.com/earendil-works/pi/blob/main/packages/coding-agent/docs/extensions.md)

### xAI Grok Build

- [Grok Build Overview](https://docs.x.ai/build/overview)
- [Modes and Commands](https://docs.x.ai/build/modes-and-commands)
- [Headless & Scripting](https://docs.x.ai/build/cli/headless-scripting)
- [Sessions](https://docs.x.ai/build/features/sessions)
- [Permissions](https://docs.x.ai/build/features/permissions)
- [Sandbox](https://docs.x.ai/build/features/sandbox)
- [Skills, Plugins & Marketplaces](https://docs.x.ai/build/features/skills-plugins-marketplaces)
- [Worktrees](https://docs.x.ai/build/features/worktrees)

### 补充坐标

- [DeepSeek Harness 官方介绍](https://www.deepseek.com/harness/en/)
- [Aider Repository Map](https://aider.chat/docs/repomap.html)
- [Aider Benchmarks](https://aider.chat/docs/benchmarks.html)
- [OpenHands 官方仓库](https://github.com/All-Hands-AI/OpenHands)
