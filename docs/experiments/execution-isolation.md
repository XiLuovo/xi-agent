# Xi 执行隔离评测 v1

## 研究问题

在保持 AgentRuntime、Agent loop、任务、ScriptedModel 响应模板、Policy 和评分标准不变
时，把 Agent 的 `run_command` 从受限本地进程切换为 Docker 容器，是否仍能完成同一组
代码修复任务？切换后端带来的步骤、工具调用、耗时、失败和 fallback 差异是多少？

## 变量与条件

- 评测目标：Xi Core Benchmark Suite，共 5 个稳定 Python Bug Case。
- 模式：`scripted`，固定响应，不调用真实模型 API。
- 条件：`local` 与 `docker`；Docker 镜像为 `python:3.11-slim`。
- 控制变量：Case fixture、自然语言任务模板、工具调用轨迹、Policy 类型、完成契约、
  AgentRuntime 和 Case/Suite 评分逻辑保持一致。
- 自变量：Agent 的 ToolExecutor；测试命令占位符仅按执行环境适配为等价命令。
- 产物隔离：两个条件使用独立 output-root 和工作区，由 `benchmarks/execution.py`
  调用现有 `benchmarks/run.py`，不复制执行或评分逻辑。

Local Agent 使用宿主机 Python 命令；Docker Agent 使用容器内的
`python -m unittest -q <test_file>`。每个 Case 的原始 Bug baseline 和最终
verification 都仍在宿主机执行，结果与报告会显式标记执行位置。因此本实验比较的是
Agent 命令执行后端，不把最终评分误称为容器内验证。

## 指标与判定

每个条件记录：Case 成功率、Agent 完成数、最终 verification 通过数、步骤、工具调用、
Agent 耗时、包含宿主 baseline/verification 的总耗时、Docker 命令与失败数、fallback
次数、镜像和产物路径。报告另外计算 local→docker 的绝对差值及可定义时的开销百分比。

实验通过标准：两次 Runner 都正常生成可读结果；结果和每个 Case Trace 都记录预期
Executor；所有 Case 的 Agent、verification、受保护路径和允许改动路径契约通过；Docker
条件 `fallback_count == 0`。Runner 非零、结果缺失或 JSON 损坏都必须留下明确失败原因。

## 本次 scripted 结果

本节只记录唯一离线 A/B 命令的实际产物，不推断 live 模型结论：

```powershell
.\.venv\Scripts\python.exe benchmarks\execution.py --mode scripted --suite core
```

本次实验 ID：`20260821-094754-1f6f5023`。报告目录为
`.xi/benchmark-execution/core/20260821-094754-1f6f5023/`，其中包含两套独立 Runner
产物以及 `report.json`、`report.md`。

| 条件 | Case 成功率 | Agent 完成 | verification | 步骤 | 工具调用 | Agent 耗时(s) | 总耗时(s) | Docker 命令/失败/fallback |
| --- | ---: | --- | --- | ---: | ---: | ---: | ---: | ---: |
| `local` | 100% (5/5) | 是 | 是 | 37 | 32 | 1.750 | 3.031 | 0 / 0 / 0 |
| `docker` (`python:3.11-slim`) | 100% (5/5) | 是 | 是 | 37 | 32 | 4.500 | 5.782 | 5 / 0 / 0 |

Docker 相对 local 的 Agent 耗时差为 `+2.750s`（`+157.14%`），包含宿主 baseline 与
verification 的总耗时差为 `+2.751s`（`+90.76%`）；步骤和工具调用均无差异。两套
结果和五个 Case 的 Trace Executor 均与条件一致，宿主 baseline/verification 位置断言
通过，Docker fallback 断言通过，实验总体判定为通过。

## 解释边界

Docker Executor v1 默认关闭容器网络、使用只读根文件系统和独立 tmpfs，并移除
capabilities、禁止提权、限制 PID/内存/CPU/超时；工作区仍以读写 bind mount 挂载。
因此工作区内的 `.env` 或其他凭据文件可能被容器读取。本实验没有增加独立的环境变量
泄露探针，不据 metadata 推断“所有宿主环境变量均不可见”。

该结果证明的是同一 Agent loop 可替换执行后端及其在固定 Suite 上的可重复行为，不是
Docker 的绝对安全证明，也不能替代威胁建模、镜像供应链审计、恶意工作负载测试或真实
模型重复实验。
