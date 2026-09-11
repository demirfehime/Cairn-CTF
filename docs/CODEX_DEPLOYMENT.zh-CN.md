# Cairn 的 Codex 本地部署与架构

## 部署结果

本分支把默认运行路径从“Docker Compose + 每项目 Worker 容器”调整为：

```text
Codex
  │ stdio MCP
  ▼
Cairn MCP Runtime（单个本机 Python 进程）
  ├─ FastAPI / Web UI     http://127.0.0.1:8792
  ├─ SQLite               data/cairn.db
  └─ Dispatcher
       └─ codex exec      runs/<project_id>/
```

Codex 和 Cairn 是双向连接：

- Codex 通过 MCP 工具创建、查询、暂停、恢复项目以及注入 Hint。
- Cairn Dispatcher 根据事实图生成任务，再调用本机已经登录的 `codex exec` 完成 Bootstrap、Reason、Explore。

默认部署不需要 Docker，也不需要在 YAML 中保存 OpenAI API Key。

首次安装或移动目录后可运行：

```powershell
.\scripts\install.ps1
.\scripts\register-codex.ps1
```

## 原项目架构与工作原理

### Cairn Server

FastAPI 服务负责事实图的一致性，不负责执行任务。SQLite 保存：

- Project：起点、目标、状态、Bootstrap 开关、Reason lease。
- Fact：已经确认的客观事实。
- Intent：从一个或多个 Fact 出发的探索方向；完成后指向新 Fact。
- Hint：人类或 Codex 随时注入的图外指导。

Intent claim 和 Reason lease 使用心跳与超时回收，避免多个 Worker 重复执行同一工作。

### Dispatcher

Dispatcher 周期性读取所有 active 项目，并按以下顺序调度：

1. 初始项目优先执行 Bootstrap；禁用或没有对应 Worker 时执行 Reason。
2. 图出现新 Fact、Hint 或所有开放 Intent 完成时执行 Reason。
3. Reason 判断目标是否已满足；否则最多创建配置数量的新 Intent。
4. Explore 认领一个 Intent，在独立工作目录中执行，成功后写入一个新 Fact。
5. 项目停止、完成或删除时，Dispatcher 取消相关本地任务。

Worker 之间不直接通信，只通过共享事实图协作，属于 Blackboard + Stigmergy 架构。

### Worker Driver

Driver 把统一任务提示转换为 Claude Code、Codex、Pi 或 Mock CLI 命令，并把最终输出解析为协议 JSON。任务超时或输出无法解析时，会尝试恢复同一会话并执行 conclude prompt，只总结已经确认的事实。

## 本次关键修改

- Docker SDK 从基础依赖移到可选的 `docker` extra。
- Dispatcher 只在容器模式下延迟导入 Docker 后端。
- 本地进程管理同时支持 Windows 进程组和 POSIX process group。
- 图快照写入每个项目自己的 `runs/<project_id>/.cairn/prompts/`，不再依赖 `/tmp`。
- Codex 本地 Worker 默认使用 `workspace-write`，不再无条件绕过审批和沙箱。
- Codex Worker 使用 JSONL 输出，能够提取 thread id 和最终 agent message。
- 新增 `cairn.codex_mcp`，一个进程同时托管 API、UI、Dispatcher 和 stdio MCP。
- 新增项目级 `.codex/config.toml`、Windows 安装/验证脚本和专用配置 `dispatch.codex.yaml`。

## Codex 中可用的 MCP 工具

- `cairn_status`
- `cairn_list_projects`
- `cairn_create_project`
- `cairn_get_project`
- `cairn_export_project`
- `cairn_add_hint`
- `cairn_set_project_status`
- `cairn_reopen_project`

## 后续个性化入口

- 调度并发、超时、Codex sandbox：`dispatch.codex.yaml`
- Agent 行为：`cairn/src/cairn/dispatcher/prompts/default/`
- Codex 命令参数：`cairn/src/cairn/dispatcher/workers/adapters/codex.py`
- 调度策略：`cairn/src/cairn/dispatcher/scheduler/loop.py`
- MCP 工具：`cairn/src/cairn/codex_mcp.py`
- API 与数据协议：`cairn/src/cairn/server/`
- Web UI：`cairn/src/cairn/server/static/index.html`

## 安全说明

本地 Worker 直接继承当前 Windows 用户的权限。默认使用 `workspace-write`。只有在隔离、明确授权的测试环境中，才应把 `CAIRN_CODEX_SANDBOX` 改成 `danger-full-access`。Cairn 不应对未授权目标执行安全测试。
