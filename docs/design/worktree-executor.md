# Worktree Executor v1 设计

## 目标与 seam

Worktree 是代码工作区生命周期 Adapter，不是新的 Agent loop，也不是命令执行器。
`WorktreeManager` 对调用者提供三个小接口：`create()` 创建 detached worktree，
`summary()` 返回可审计元数据，`remove()` 按 cleanup policy 回收或保留。Git 命令、
路径约束、错误处理和回收细节集中在该模块内；CLI 只做参数适配，AgentRuntime 继续
接收普通 `workspace` 路径和同一套 Executor/Policy。

## 生命周期

1. `create()` 校验源仓库可解析为 Git 根目录并读取 `HEAD`。
2. 在显式 worktree root 下生成唯一目录，执行 `git worktree add --detach`。
3. Runtime 使用新目录执行文件工具和 `run_command`；源仓库保持不变。
4. 事件记录 `workspace_mode`、`source_repo`、`worktree_path`、`base_revision` 和
   `cleanup_policy`，并追加 `worktree_created` / `worktree_removed`。
5. 默认执行 `git worktree remove --force`。`--keep-worktree` 不删除目录，生命周期
   状态为 `retained`，由用户自行处理。

worktree root 是显式删除范围：回收只操作本次 Manager 生成的路径，不能通过参数把
`git worktree remove` 指向 root 外部。回收失败保留结构化错误，不静默吞掉。

## 边界

v1 使用 detached worktree，不创建分支、不 commit、不 merge、不自动提交 PR。保留的
worktree 适合人工查看 `git diff` 和自行提交。Worktree 只隔离代码目录；它不隔离宿主
进程、环境变量、工作区凭据或 Docker 容器。需要进程限制时仍应单独选择 Docker Executor，
两种隔离可以组合但语义不同。
