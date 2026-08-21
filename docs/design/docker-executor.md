# Docker Executor v1 设计

## 目标

Docker Executor 是 Xi 在现有 `ToolExecutor.execute(tool_name, arguments)` seam 上增加的
第二个真实执行 Adapter。它用于证明 Policy 与进程隔离是两层独立机制：Policy 决定某次
工具调用能否发生，Executor 决定获准调用在哪里、以什么限制执行。切换 Adapter 不需要
修改 Model、Agent loop、工具定义或完成契约。

v1 将最需要隔离的 `run_command` 放入短生命周期 Linux 容器；文件读取、代码搜索和补丁
修改仍复用 `RestrictedLocalExecutor` 的工作区路径校验。这样既保留现有精确补丁语义，
也避免测试命令直接获得宿主机进程权限。

## 默认隔离配置

每次 `run_command` 都使用独立的 `docker run --rm`：

- 仅将 Xi 当前工作区读写挂载到 `/workspace`；
- 容器网络为 `none`；
- 容器根文件系统只读，`/tmp` 使用 64 MiB tmpfs；
- 丢弃全部 Linux capabilities，并启用 `no-new-privileges`；
- 限制为 256 个 PID、512 MiB 内存（不追加 swap）和 1 个 CPU；
- 覆盖镜像原有 ENTRYPOINT，固定通过 `/bin/sh -lc` 执行获准命令；
- 只注入 `PYTHONUNBUFFERED` 与 `PYTHONDONTWRITEBYTECODE`，不继承模型 API Key；
- 沿用 Xi 的命令超时和输出截断限制，超时时主动删除容器。

Linux 主机上容器进程使用当前宿主用户的 UID/GID，以避免在 bind mount 中产生 root
所有者文件。Windows 上必须让 Docker Desktop 运行 Linux containers，并允许当前盘符或
目录用于 bind mount；容器内统一使用 `/workspace`，不能复用 Windows `.venv` 或路径。
挂载权限不足会作为 Docker 失败返回，不会切换到本地进程。

## 失败语义

Docker 是显式选择的执行后端，不是“失败后再尝试”的优化层。以下情况都会形成失败的
`ToolResult`：

- Docker CLI 不存在；
- Docker daemon 未运行；
- 指定镜像不存在且无法拉取；
- 镜像中缺少命令或依赖；
- 容器命令超时或返回非零退出码。

这些失败不会触发本地执行。`metadata.fallback=false` 使这一点可以在 Trace 中审计。

## 非目标和剩余风险

Docker Executor v1 不把 Docker daemon 本身当作不可信对象，也不防御 Docker Engine
或宿主内核漏洞。工作区以读写方式挂载，因此获准运行的容器命令可以修改工作区内任意
文件，也能读取工作区内的 `.env` 等凭据文件；这正是 Coding Agent 运行测试和构建工具
所需能力带来的边界。宿主机环境变量不会自动注入容器，但用户仍应使用精确
`--allow-command`、审查未知命令，并为真实项目准备不含凭据的工作区与只包含必要依赖的
可信镜像。

默认 `python:3.11-slim` 只提供基础 Python 环境，不包含项目的第三方依赖。Windows
虚拟环境也不能在 Linux 容器中复用；自定义镜像必须提供 `/bin/sh`。后续里程碑可提供
预构建 Xi Executor 镜像，并在相同 benchmark 上比较本地与容器后端的成功率、启动开销
和安全审计信息。
