# Xi：一个可观测、可恢复、可评测的 Python 通用 Coding Agent

Xi 是一个使用 Python 实现的通用 Coding Agent Runtime。它通过自然语言理解开发任务，
并在受限工作区内自主完成代码搜索、文件读取、补丁修改和测试验证。

这个项目关注的不是简单封装大模型 API，而是 Coding Agent 的运行时机制：模型如何与
工具循环协作，工具调用如何受到权限策略约束，执行过程如何被记录、回放和恢复，以及
不同上下文策略如何通过可重复实验进行比较。

## 项目定位

Xi 当前已经具备一个可运行、可研究的最小闭环：

- 交互模式与一次性无交互（Headless）模式共用同一个 `AgentRuntime`；
- 模型、工具、策略、执行器和 JSONL 会话存储都可以替换；
- 支持代码搜索、文件读取、补丁修改和测试命令执行；
- 记录可审计的 JSONL 事件 Trace，并支持离线回放；
- 支持从 Trace 恢复上下文并继续执行；
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
```

安装完成后，可以直接进入自然语言交互，或者执行一次性任务：

```powershell
xi
xi -p "修复这个 bug 并运行测试" --workspace .
xi -p "修复固定 Bug" --allow-command "python -m pytest -q tests/test_bug.py"
xi -p "定位跨文件调用链问题" --context-strategy repo-map
xi resume .xi\traces\20260820-120000-abcd1234.jsonl
xi resume .xi\traces\20260820-120000-abcd1234.jsonl -p "继续检查刚才的修改"
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

交互模式、一次性任务、Benchmark 和 Session Resume 都复用同一个 `AgentRuntime`，
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

## 安全边界

Xi 的本地执行器会约束工作目录、文件路径、超时和输出长度，但它不是 OS
沙箱；不受信任任务应放入容器或其他隔离环境。

Headless 模式中，未知命令默认拒绝；固定评测请用 `--allow-command` 精确声明
唯一测试命令。交互模式会对未知命令询问确认。

## 会话恢复（Session Resume）

正常完成的 trace 可以恢复成模型对话并继续执行。`resume` 默认使用 trace 中记录的
工作区，并继续向原 JSONL 文件追加事件；新的 `run_started` 会把上一条事件作为
`parent_id`，同一会话也会保持同一个 `run_id`：

```powershell
# 恢复后进入自然语言交互
xi resume .xi\traces\20260820-120000-abcd1234.jsonl

# 恢复后只执行一轮
xi resume .xi\traces\20260820-120000-abcd1234.jsonl -p "继续完成剩余工作"
```

Session Projection 会从事件流中恢复最近一次发送给模型的消息，并合入该轮最终响应。
当前最小版本只恢复正常完成且保存了 `model_requested.messages` 的 trace；失败会话、
fork 和 compaction 留在后续阶段。

## Trace 回放（Trace Replay）

可以对已经保存的 JSONL trace 做离线观察：

```powershell
xi replay .xi\benchmarks\order-total-quantity-001\20260820-085219-19e2a9f3\trace.jsonl
```

Replay 只读取并校验 JSONL，按原事件顺序输出紧凑时间线和运行摘要；它不会
调用模型、执行工具或命令，也不会修改工作区，因此不是 resume 或重新执行。
回放会检查 JSON 合法性、单一 `run_id`、唯一 `event_id`，以及按顺序可解析的
`parent_id`。损坏的 trace 会以非零退出码报告错误。

## 后续路线

- Session Schema v2：区分整个会话的 `session_id` 与每轮执行的 `run_id`；
- Session Fork：从历史事件节点创建独立分支；
- 失败或中断会话恢复；
- 长会话上下文压缩（Compaction）及对应实验；
- Docker Executor，进一步加强进程与文件系统隔离。

## 许可证

本项目使用 [MIT License](LICENSE)。
