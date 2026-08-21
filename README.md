# Xi：一个可观测、可恢复、可评测的 Python 通用 Coding Agent

Xi 是一个使用 Python 实现的通用 Coding Agent Runtime。它通过自然语言理解开发任务，
并在受限工作区内自主完成代码搜索、文件读取、补丁修改和测试验证。

这个项目关注的不是简单封装大模型 API，而是 Coding Agent 的运行时机制：模型如何与
工具循环协作，工具调用如何受到权限策略约束，执行过程如何被记录、回放、续接、故障恢复和分叉，以及
不同上下文策略如何通过可重复实验进行比较。

## 项目定位

Xi 当前已经具备一个可运行、可研究的最小闭环：

- 交互模式与一次性无交互（Headless）模式共用同一个 `AgentRuntime`；
- 模型、工具、策略、执行器和 JSONL 会话存储都可以替换；
- 支持代码搜索、文件读取、补丁修改和测试命令执行；
- 支持受限本地执行与 Docker 隔离执行两种可替换后端；
- 记录可审计的 JSONL 事件 Trace，并支持离线回放；
- 支持从正常、失败或安全中断的 Trace 恢复上下文并继续执行；
- 支持从历史成功 Run 的结束事件创建独立 Session 分支；
- 支持可回放、可恢复的确定性长会话上下文压缩；
- 使用 Session Schema v2 区分稳定的 `session_id` 与每轮独立的 `run_id`；
- 提供基于证据的完成契约，避免模型只描述修改而没有真正执行；
- 提供 `search` 与 `repo-map` 两种上下文策略和可重复 Benchmark。

## 快速开始

Xi 需要 Python 3.11 或更高版本。Windows PowerShell 下可以这样安装：

```powershell
py -3.11 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -e ".[dev]"
Copy-Item .env.example .env
```

在本地 `.env` 中填写模型配置。`.env` 已加入忽略规则，不应提交到 Git：

```dotenv
XI_API_KEY=你的_API_Key
XI_MODEL=你的模型名称
XI_BASE_URL=https://你的服务地址/v1
XI_DOCKER_IMAGE=python:3.11-slim
```

安装完成后，可以直接进入自然语言交互，或者执行一次性任务：

```powershell
xi
xi -p "修复这个 bug 并运行测试" --workspace .
xi -p "修复固定 Bug" --allow-command "python -m pytest -q tests/test_bug.py"
xi -p "定位跨文件调用链问题" --context-strategy repo-map
xi -p "运行隔离测试" --executor docker --allow-command "python -m unittest -q"
xi resume .xi\traces\20260820-120000-abcd1234.jsonl
xi resume .xi\traces\20260820-120000-abcd1234.jsonl -p "继续检查刚才的修改"
xi fork .xi\traces\20260820-120000-abcd1234.jsonl --at-event <run_finished事件ID>
xi fork .xi\traces\20260820-120000-abcd1234.jsonl --at-event <run_finished事件ID> -p "尝试另一种方案"
xi -p "处理较长任务" --context-budget-chars 8000
```

## 核心运行流程

```text
自然语言任务
  → Context Builder 构建仓库上下文
  → Model 生成文本或工具调用
  → Policy 判断允许、询问或拒绝
  → Executor 在受限工作区执行工具
  → Observation/Event 写入 JSONL Trace
  → Model 根据结果继续推理
  → CompletionContract 判断是否具备完成证据
  → 最终结果
```

交互模式、一次性任务、Benchmark、Session Resume、Recovery 和 Session Fork 都复用同一个 `AgentRuntime`，
而不是各自维护一套 Agent 循环。

普通任务默认采用宽松完成语义。需要证据驱动完成时，可以明确要求文件改动和指定命令
成功；如果模型只描述下一步而没有实际执行，Runtime 会反馈缺失证据并继续循环：

```powershell
xi -p "修复这个 bug" `
  --allow-command "python -m pytest -q tests/test_bug.py" `
  --require-file-change `
  --require-successful-command "python -m pytest -q tests/test_bug.py"
```

Benchmark 自动启用该严格完成契约，普通 `xi` 与 `xi -p` 的默认行为保持不变。

默认 trace 写入工作区下的 `.xi/traces/`。真实模型使用 OpenAI-compatible
Chat Completions，并从 `XI_API_KEY`、`XI_MODEL` 和 `XI_BASE_URL` 读取配置：

```powershell
$env:XI_MODEL = "your-model"
$env:XI_API_KEY = "..."
xi -p "定位并修复失败测试"
```

`--script responses.json` 可切换到确定性的 `ScriptedModel`。脚本文件可为
JSON 数组或 JSONL，每项是最终文本，或包含 `tool_calls` 的对象。

仓库还附带一个最小脚本示例：[examples/scripted_bug_fix.json](examples/scripted_bug_fix.json)。

## Benchmark（代码修复评测）

Xi 内置一个面向开发和评测的 Core Benchmark Suite。Benchmark Case 表示一个
独立、可重复的 Bug 任务；Benchmark Suite 按固定顺序运行多个 Case，并汇总成功率、
耗时、工具调用和改动文件。它们是内部评测基础设施，普通用户仍使用 `xi` 或
`xi -p "任务"`。

当前 `search` 与 `repo-map` 的三轮真实模型 A/B 实验、失败分析和结论见
[上下文策略 A/B 实验](docs/experiments/context-strategy-ab.md)。原始 Trace 和本地路径不进入
公开仓库，只提交脱敏后的方法与聚合指标。

```powershell
# 默认/显式单 Case（离线 ScriptedModel）
.\.venv\Scripts\python.exe benchmarks\run.py --mode scripted
.\.venv\Scripts\python.exe benchmarks\run.py --case order_total_quantity --mode scripted

# 五道 Core Suite
.\.venv\Scripts\python.exe benchmarks\run.py --suite core --mode scripted
.\.venv\Scripts\python.exe benchmarks\run.py --suite core --mode live
```

Live Suite 会调用真实模型，本地确定性验证应使用 scripted。详细的 Case 列表、评分
标准和产物结构见 [benchmarks/README.md](benchmarks/README.md)。

上下文策略默认是 `search`：模型通过工具按需搜索和读取文件。研究模式可以切换为
`repo-map`：在首次模型请求前提供一份受限的仓库结构与符号地图，同时仍保留搜索工具。
地图只描述允许扫描的路径与代码符号，不注入完整源码，并跳过缓存、虚拟环境、隐藏与
敏感配置路径。
两种策略共用同一个 Runtime 和 Benchmark Suite，便于比较成功率、步骤、工具调用、
耗时与模型 token：

```powershell
.\.venv\Scripts\python.exe benchmarks\run.py --suite core --mode live --context-strategy search
.\.venv\Scripts\python.exe benchmarks\run.py --suite core --mode live --context-strategy repo-map
```

使用 Compare Runner 可自动重复两种策略、轮转每轮首发顺序，并生成 JSON 与 Markdown
研究报告。默认 `scripted`，不会访问网络：

```powershell
# 离线单轮比较
.\.venv\Scripts\python.exe benchmarks\compare.py --suite core --mode scripted

# 真实模型重复三轮（会产生 API 费用）
.\.venv\Scripts\python.exe benchmarks\compare.py --suite core --mode live --repeat 3
```

报告聚合 Case/Suite 成功率、步骤、工具调用、prompt/completion/total token、上下文
字符数、完成契约拒绝次数和耗时，并保留每次 Runner 的 exit code、stderr 与 Suite
产物路径。详细接口和
产物布局见 [benchmarks/README.md](benchmarks/README.md)。

长会话上下文压缩评测是独立于 Core Suite 的离线实验入口。它复用现有 Case Runner，
对同一个长会话修复任务比较关闭压缩和两个字符预算；字符预算是稳定 JSON 字符数，
不是 token 计数：

```powershell
.\.venv\Scripts\python.exe benchmarks\compaction.py --mode scripted
```

每个条件使用独立工作区，实验会生成 JSON/Markdown 报告和对应 Trace 产物；只有三种
条件都完成任务并通过唯一验证、关闭条件压缩次数为 0、至少一个预算条件发生实际字符
减少时才成功。实验结果记录在
[上下文压缩实验](docs/experiments/context-compaction.md)；这仍是内部评测基础设施，
普通用户继续使用 `xi` 与 `xi -p "任务"`。

执行隔离 A/B 实验复用同一套 Core Suite、ScriptedModel 轨迹、Policy 与 AgentRuntime，
只切换 Agent 的命令执行后端，并生成 JSON/Markdown 对照报告：

```powershell
.\.venv\Scripts\python.exe benchmarks\execution.py --mode scripted --suite core
```

实验默认比较 `local` 与使用 `python:3.11-slim` 的 `docker`。Docker 条件会把 Case 的
唯一测试命令适配为 Linux 容器内的 `python -m unittest ...`；baseline 和最终
verification 仍在宿主机运行，并在结果中与 Agent 命令位置明确区分。报告记录成功率、
步骤、工具调用、Agent/总耗时、Docker 失败、fallback，以及 local→docker 的差值与耗时
开销。研究设计和实际 scripted 结果见
[执行隔离评测](docs/experiments/execution-isolation.md)。这是执行后端 A/B，不是 Docker
绝对安全性证明，也不包含独立的宿主环境变量泄露实验。

## 安全边界

Xi 将“是否允许工具调用”的 Policy 与“如何执行操作”的 Executor 分开。默认
`--executor local` 使用受限本地执行器，约束工作目录、文件路径、超时、输出长度和
传递给子进程的环境变量，但它不是 OS 沙箱。

需要隔离命令进程时，可选择 Docker Executor：

```powershell
xi -p "运行项目测试并修复失败" `
  --executor docker `
  --docker-image python:3.11-slim `
  --allow-command "python -m unittest -q"
```

Docker Executor v1 继续通过宿主机执行经过路径校验的 `read_file`、`search_code` 和
`apply_patch`；风险最高的 `run_command` 会进入一次性 Linux 容器。容器默认断网，只把
当前工作区挂载到 `/workspace`，根文件系统只读，使用独立 `/tmp`，移除 Linux
capabilities、禁止提权，并限制 PID、内存和 CPU。宿主机 API Key 等敏感环境变量不会
自动传入容器；但工作区本身会完整挂载，因此工作区内的 `.env` 等文件仍属于容器可见
范围。容器命令仍先经过同一套 allow/ask/deny Policy，Runtime 和 Agent loop 不需要针对
执行后端分叉。

使用 Docker 后端需要 Docker Desktop/Engine 和一个包含任务依赖的 Linux 镜像。默认
镜像为 `XI_DOCKER_IMAGE`，未配置时使用 `python:3.11-slim`；也可以通过
`--docker-image` 覆盖（该选项必须与 `--executor docker` 同时使用）。Windows 上需要
Docker Desktop 运行 Linux containers，并允许当前盘符/目录用于 bind mount；容器内工作区
路径固定为 `/workspace`。命令应采用容器内可用的 POSIX 路径和工具，Windows `.venv`
不能直接在 Linux 容器中使用。Docker CLI 或 daemon 不可用、工作区挂载失败、镜像缺失或
容器执行失败时，Xi 会明确返回失败，**不会静默回退成本地执行**。

`run_started.payload.executor` 会记录本轮选择的后端，Docker 命令的 `tool_finished`
metadata 还会记录镜像、容器名、网络模式和资源限制结果，便于 Trace 审计。更完整的威胁
模型见 [Docker Executor v1 设计](docs/design/docker-executor.md)。

Headless 模式中，未知命令默认拒绝；固定评测请用 `--allow-command` 精确声明
唯一测试命令。交互模式会对未知命令询问确认。

## 会话续接与故障恢复（Resume / Recovery）

CLI 统一使用 `xi resume`，但会根据 Trace 尾部自动区分两种领域行为：正常完成的
`run_finished` 属于 Resume；`run_failed` 或安全中断的不完整 Run 属于 Recovery。
两者都使用 Trace 中记录的工作区、继续向原 JSONL 文件追加事件，并保持原
`session_id`；新一轮会创建独立 `run_id`，其 `run_started.parent_id` 指向恢复前的
最后事件：

```powershell
# 续接或故障恢复后进入自然语言交互
xi resume .xi\traces\20260820-120000-abcd1234.jsonl

# 续接或故障恢复后只执行一轮
xi resume .xi\traces\20260820-120000-abcd1234.jsonl -p "继续完成剩余工作"
```

Recovery 只接受可证明安全的持久化检查点：必须存在 `model_requested.messages`，并且
后续 assistant/tool 交换能够完整重建。已经完成的 assistant tool call 与
`tool_finished` 结果会作为历史消息交给模型，但 Xi **不会重新执行旧工具调用**。
如果存在没有对应 `tool_finished` 的 `tool_started`，工具副作用状态不确定，v1 会明确
拒绝恢复；同一 assistant 响应只有部分工具调用完成时也会拒绝，而不会静默丢弃历史。

Recovery 后首个 `run_started.payload.recovered_from` 会记录来源 Trace、`session_id`、
`run_id`、尾部 `event_id`、`state`（`failed` 或 `incomplete`）以及用于重建上下文的
`checkpoint_event_id`；后续正常 Run 不重复写入。旧版 v1 Trace 没有 `session_id` 时，
仍会用原 `run_id` 推导会话身份，新增事件继续采用 v2 格式。

三种历史操作的区别如下：

| 操作 | 来源状态 | Session 身份 | Trace 与因果链 |
| --- | --- | --- | --- |
| Resume | 正常完成的 Run | 保持不变 | 追加原 Trace，延续原 parent 链 |
| Recovery | 失败或安全中断的 Run | 保持不变 | 追加原 Trace，从安全历史继续且不重放工具 |
| Fork | 历史成功 Run 的终点 | 创建新身份 | 写入新 Trace，不跨 Trace 建立 parent 链 |

## 上下文压缩（Compaction）

上下文压缩默认关闭。需要为长会话设置显式预算时，使用
`--context-budget-chars`；单位是稳定、可解释的 JSON 字符数，**不是 token 计数**：

```powershell
xi -p "处理长会话任务" --context-budget-chars 8000
xi resume .xi\traces\long-session.jsonl -p "继续" --context-budget-chars 10000
```

Xi 使用本地确定性压缩器，不调用额外模型，也不需要 API Key。压缩仅发生在下一次
模型请求之前，并且必须位于完整模型响应及其所有工具结果都已落盘的安全检查点；不会
在 `tool_started` 与 `tool_finished` 之间压缩。首个 system 指令、当前任务意图和
可容纳的最近完整对话会保留，较旧消息会形成明确标记的历史摘要；assistant tool call
与对应 tool 结果不会被拆成非法消息。

每次压缩都追加一条 `context_compacted` 事件，包含预算、压缩前后消息数与字符数，以及
压缩后的完整模型消息快照。原始 Trace 事件不会被覆盖或删除，因此 Replay 仍能观察
完整历史。Resume、Recovery 和 Fork 都能继承最近的压缩检查点；Recovery 只恢复模型
上下文，绝不会重新执行已经完成的工具副作用。未显式传入新预算时，Resume/Fork 沿用
来源 Trace 记录的预算；显式参数会覆盖该轮预算。预算小到无法保留最小合法上下文时，
Xi 会明确失败，而不是静默丢弃 system 指令。

## 会话分叉（Session Fork）

`fork` 从历史成功 Run 的 `run_finished` 事件创建独立分支。它只继承目标事件及其
之前已经发送给模型的上下文，不会看见来源 Trace 中更晚的 Run：

```powershell
# 分叉后进入自然语言交互
xi fork .xi\traces\source.jsonl --at-event <run_finished事件ID>

# 分叉后只执行一轮
xi fork .xi\traces\source.jsonl --at-event <run_finished事件ID> -p "改用另一种修复方案"

# 可显式指定新的 Trace；目标文件必须尚不存在
xi fork .xi\traces\source.jsonl --at-event <run_finished事件ID> `
  --trace .xi\traces\alternative.jsonl -p "继续"
```

Fork 只接受 `success=true` 的 `run_finished`。新分支使用来源 Trace 记录的工作区，
但会创建新的 Trace、`session_id` 和 `run_id`；来源 Trace 保持只读。新 Trace 的首个
`run_started.parent_id` 为 `null`，不会跨 Trace 建立因果父链，并在
`run_started.payload.forked_from` 中记录来源 Trace、`session_id`、`run_id` 和
`event_id`。后续同一分支内的 Run 再按正常 Session 因果关系连接。

## Trace 回放（Trace Replay）

可以对已经保存的 JSONL trace 做离线观察：

```powershell
xi replay .xi\benchmarks\order-total-quantity-001\20260820-085219-19e2a9f3\trace.jsonl
```

Replay 只读取并校验 JSONL，按原事件顺序输出紧凑时间线和运行摘要；Fork Trace
会显示分支来源，Recovery Run 会显示恢复来源和安全检查点，压缩 Trace 会显示
`context_compacted` 时间线与压缩次数、预算和前后字符数。Replay 不会
调用模型、执行工具或命令，也不会修改工作区，因此不是 resume 或重新执行。
回放会显示会话 ID、运行轮数与各轮连接关系，并检查 JSON 合法性、单一
`session_id`、唯一 `event_id`，以及按顺序可解析的 `parent_id`。一个 v2 Trace 可包含
多个 `run_id`；旧版单轮 v1 Trace 仍可回放。损坏的 trace 会以非零退出码报告错误。

## 后续路线

- 将 Session Fork 扩展到更多安全的历史节点；
- 为 Docker Executor 增加预构建依赖镜像和 local/docker 对照实验；
- 在执行隔离稳定后探索 worktree 与后台任务。

## 许可证

本项目使用 [MIT License](LICENSE)。
