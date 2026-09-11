# Cairn CTF Agent 项目地图

先阅读本文件和 `AGENTS.md`，再按任务进入相关源码。

## 结构和入口

- `cairn/src/cairn/server/`：FastAPI、数据库、业务逻辑和路由。
- `cairn/src/cairn/dispatcher/`：调度器、Bootstrap/Reason/Explore、Worker 和执行后端。
- `cairn/src/cairn/server/static/`：Web UI 和本地前端依赖。
- `cairn/tests/`：自动化测试。
- `start-cairn-ctf.cmd`：独立 CTF 启动器，默认端口 8001。
- `scripts/start-cairn.ps1`：可选本地配置入口，默认端口 8792。

## 数据流

Parent 拆分目标、分配 Child、审阅结构化事实和 Flag 候选。
Child 使用隔离工作区和 Agent 会话执行 Bootstrap、Reason、Explore。
Child 通过 Parent Blackboard 和转述 Hint 交换证据。
Fact 是确认的事实，Intent 是探索方向，Hint 是人工或上层指导。
Flag 候选需要证据并交由人工确认。

## 执行记录

`dispatcher/runtime/execution_trace.py` 保存 Worker 调用的 manifest、
prompt、可选 stdin 以及标准输出和错误输出。
本地记录位于 `.cairn-launcher/logs/workers/<project>/<run-id>/`，
容器后端的回退位置是 `.cairn-launcher/logs/traces/`。
`server/routers/logs.py` 按项目展示本地记录并校验路径边界。
manifest 对常见命令行凭据参数脱敏，不保存环境变量。
完整 prompt 和输出属于本地运行数据，不应提交到 Git。

## 工作约定

保持 fact/intent/hint 协议兼容；不要共享两个版本的数据库或工作区。
运行日志不是默认上下文，只有排查相应问题时才读取。
测试使用临时数据库，不对正在运行的实例执行写入测试。

```powershell
.\.venv\Scripts\python.exe -m pytest -q .\cairn\tests
```
