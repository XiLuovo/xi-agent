# Benchmark workspace rules

- `profile_view.py` 是既有消费方，定义当前返回契约，不得修改。
- `profile_repository.py` 是数据来源，不得修改。
- 只修改 `profile_service.py` 使提供方满足字段名和类型契约。
- 不得修改、删除或绕过测试文件。
- 只能使用 Python 标准库。
- 完成修改后运行任务给出的验证命令。
