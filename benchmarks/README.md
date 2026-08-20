# Xi Benchmarks

这里存放 Xi 的可重复 Coding Agent 内部评测基础设施。普通用户仍通过 `xi`
或 `xi -p "任务"` 进行自然语言交互，benchmark 不属于产品 CLI。

## Case 与 Suite

Benchmark Case 是一个独立任务，包含稳定失败的 fixture、自然语言任务、唯一验证命令、
受保护路径、允许改动路径，以及不访问真实 API 的 ScriptedModel 响应。

Runner 会为每个 Case 自动启用证据驱动的完成契约：至少产生一次成功文件改动，并在
最后一次改动后成功运行该 Case 的唯一验证命令。模型若只说“接下来修改并测试”，
Runtime 不会结束，而会把缺失证据反馈给模型继续执行。该约束只影响 Benchmark；普通
产品交互默认仍采用宽松完成语义。

Benchmark Suite 是一个按稳定顺序列出多个 Case 的 manifest。Runner 会为每个 Case
创建全新工作区；某个 Case 失败后仍继续执行后续 Case，最终生成一份聚合结果。

Core Suite 包含五类不同任务：

| Case | 类型 | 预期根因文件 |
| --- | --- | --- |
| `order_total_quantity` | 单文件金额逻辑遗漏数量 | `order_total.py` |
| `boundary_condition` | 分页空输入/整页 off-by-one | `pagination.py` |
| `multi_file_call_chain` | 跨 checkout/pricing/discounts 调用链 | `pricing.py` |
| `exception_handling` | 配置错误被吞掉，异常契约错误 | `settings.py` |
| `api_contract` | 提供方字段名和类型与消费方不一致 | `profile_service.py` |

## 运行命令

不指定目标时仍兼容原行为，运行默认单 Case：

```powershell
.\.venv\Scripts\python.exe benchmarks\run.py --mode scripted
```

显式运行一个 Case：

```powershell
.\.venv\Scripts\python.exe benchmarks\run.py --case order_total_quantity --mode scripted
```

离线确定性运行整个 Core Suite：

```powershell
.\.venv\Scripts\python.exe benchmarks\run.py --suite core --mode scripted
```

使用真实 OpenAI-compatible 模型运行 Core Suite：

```powershell
.\.venv\Scripts\python.exe benchmarks\run.py --suite core --mode live
```

选择上下文策略：

```powershell
# 现有基线：模型按需调用 search_code/read_file
.\.venv\Scripts\python.exe benchmarks\run.py --suite core --mode live --context-strategy search

# 实验策略：首次请求附带受限路径/符号地图，之后仍可按需搜索
.\.venv\Scripts\python.exe benchmarks\run.py --suite core --mode live --context-strategy repo-map
```

`search` 是默认值，因此旧命令行为不变。公平对比时应保持 Suite、模型、模型参数和
Case 顺序一致，只改变 `--context-strategy`。建议至少重复多轮真实模型运行，避免把一次
采样波动当作策略收益。

## 多轮策略对比

`compare.py` 是 Suite Runner 外的一层编排器。它不会复制 Case 或 Suite 执行逻辑，
而是为每个“轮次 × 策略”创建独立 `--output-root`，调用 `run.py` 并读取该调用唯一的
Suite `result.json`。各轮会轮转首发策略，减少总由同一种策略先运行带来的顺序偏差。

完全离线比较默认两种策略：

```powershell
.\.venv\Scripts\python.exe benchmarks\compare.py --suite core --mode scripted
```

重复三轮真实模型比较：

```powershell
.\.venv\Scripts\python.exe benchmarks\compare.py --suite core --mode live --repeat 3
```

也可以显式指定策略和报告根目录：

```powershell
.\.venv\Scripts\python.exe benchmarks\compare.py `
  --suite core `
  --mode scripted `
  --strategies search repo-map `
  --repeat 2 `
  --output-root .xi\benchmark-comparisons
```

Compare 会为每种策略统计 Case 成功率、整套 Suite 通过率，以及每 Suite 的步骤、工具
调用、prompt/completion/total token、首次上下文字符数和耗时；连续多轮时输出
完成契约拒绝次数、mean、median、min、max 和总体标准差。最终同时生成 `report.json`
和 `report.md`。

某个 Runner 返回非零（例如 Suite 中存在失败 Case）不会中断后续策略，也不会丢失
报告。Compare 的退出码只表示是否成功取得全部调用的 Suite 结果：全部产物可读取时为
0；Runner 未启动、未生成结果或结果损坏时为非零。`scripted` 模式使用固定响应，完全
离线；`live` 会产生真实 API 调用与费用。

`--case` 与 `--suite` 互斥。Live 模式读取项目根目录的 `.env`，可能产生真实
API 调用与费用；Scripted 模式完全离线。

## 评分与产物

每个 Case 至少检查：

- `baseline_bug_reproduced`：原始 fixture 的测试稳定失败；
- `agent_completed`：Xi 正常完成任务；
- `verification_passed`：唯一验证命令最终通过；
- `protected_files_unchanged`：测试和规则等受保护文件未变；
- `allowed_changed_paths_respected`：最终文件变化只出现在 manifest 允许范围内。

工作区快照忽略 `__pycache__`、`.pyc` 和 `.pyo`，不会把解释器缓存误算成 Agent
补丁。单 Case 产物保持原布局：

```text
.xi/benchmarks/<case-id>/<run-id>/
├── workspace/
├── trace.jsonl
├── scripted_responses.json   # 仅 scripted
└── result.json
```

Suite 产物采用稳定布局：

```text
.xi/benchmarks/suites/core/<suite-run-id>/
├── cases/
│   ├── order_total_quantity/
│   │   ├── workspace/
│   │   ├── trace.jsonl
│   │   └── result.json
│   └── ...其余 Case
└── result.json                # 成功率、耗时、工具调用和逐 Case 摘要
```

Suite 的 `result.json` 记录上下文策略、总 Case 数、通过/失败数、成功率、总耗时、
汇总工具调用、模型 token 用量，以及每个 Case 的步骤、上下文大小、耗时、改动文件、
失败标准/原因和产物路径。

Compare 产物按一次实验隔离：

```text
.xi/benchmark-comparisons/core/<comparison-id>/
├── runs/
│   ├── round-001/
│   │   ├── 01-search/       # 内含 run.py 的独立 Suite 产物
│   │   └── 02-repo-map/
│   └── ...
├── report.json              # 机器可读原始调用与聚合统计
└── report.md                # 面试/研究记录可直接阅读的对比表
```
