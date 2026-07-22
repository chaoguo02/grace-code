# Grace-Code 架构遗产所有权文档

> **版本**: 1.0 | **日期**: 2026-07-22
> **状态**: 初稿, 待 Tech Lead 确认
> **团队规模**: Solo Developer (guo)
> **角色映射**: 后端 Owner = guo, 前端 Owner = guo, QA Owner = guo, API Owner = guo

---

## 1. 角色定义 (Solo-Dev 模式)

| 角色 | 负责人 | ETA 机制 |
|------|--------|---------|
| 后端观测负责人 | guo | 自设里程碑, 以 PR merge 为激活信号 |
| API Owner | guo | 自审 SSOT 变更, PR 模板作为 checklist 提醒 |
| 前端 Tech Lead | guo | 自检 hook 规范, 合规清单在 PR template 中 |
| QA / 测试负责人 | guo | 新 E2E PR 必须通过 `ServerContext` 启动 |

> 由于无专职角色，所有权文档退化为 "自我约束清单" —— 每项遗产的维护契约以 checkable PR checklist item 体现。

---

## 2. 四大遗产与所有权

### L-1: `RetryMetrics` dataclass → Langfuse 接入

| 属性 | 值 |
|------|-----|
| **文件** | [llm/invoker.py](../llm/invoker.py) — `RetryMetrics` + `metrics_callback` |
| **所有者** | guo |
| **激活里程碑** | Langfuse tracer 实现 `on_metrics(RetryMetrics)` 回调, 合并到 `main` |
| **ETA** | Phase 7 Kickoff (not later than 2026-08-01) |
| **验收标准** | 1. `FORGE_OBSERVE_RETRIES=1` → Langfuse dashboard 可见 retry metrics<br>2. `FORGE_OBSERVE_RETRIES=0` → 零开销 (不创建 `RetryMetrics`) |
| **维护契约** | 新增 LLM backend 时, 必须实现 `metrics_callback` 兼容接口 |
| **PR Checklist** | `[ ] RetryMetrics callback wired — verify via FORGE_OBSERVE_RETRIES=1` |

### L-2: `/api/config/models` SSOT

| 属性 | 值 |
|------|-----|
| **文件** | [server/routers/config.py](../server/routers/config.py) — `_MODEL_CATALOG` + `GET /api/config/models` |
| **所有者** | guo |
| **激活里程碑** | SSOT 校验脚本加入 `tools/_check_ssot.sh`, PR template 中引用 |
| **ETA** | Phase 7 Kickoff |
| **验收标准** | 1. `_MODEL_CATALOG` 为前端模型选择的唯一数据源<br>2. 字段变更需同步更新 `agent/constants.py` 引用 (反向校验) <br>3. `Cache-Control: max-age=300` 在生产环境返回 |
| **维护契约** | 增删模型字段时: <br>1. 更新 `_MODEL_CATALOG`<br>2. 更新 `agent/constants.py` 中的 `DEFAULT_MAX_OUTPUT_TOKENS` 等关联常量<br>3. 运行 `tools/_check_ssot.sh` 通过 |
| **PR Checklist** | `[ ] /api/config/models SSOT unchanged — or sync verified` |

### L-3: `connectWebSocket` 冻结合约

| 属性 | 值 |
|------|-----|
| **文件** | [web/src/hooks/useWebSocket.ts](../web/src/hooks/useWebSocket.ts) — `connectWebSocket` / `disconnectWebSocket` / `scheduleReconnect` |
| **所有者** | guo |
| **激活里程碑** | Hook 使用规范写入前端开发文档 |
| **ETA** | Phase 7 Kickoff |
| **验收标准** | 1. 所有 WS 连接通过 `connectWebSocket()` 创建<br>2. 无文件直接访问 `new WebSocket()` (grep audit)<br>3. 新增 WS 消息类型必须在 `WsCallbacks` 接口中有对应 handler |
| **维护契约** | 新增 WS 消息类型时: <br>1. 更新 `WsCallbacks` 接口<br>2. 更新 `WsMessage` discriminated union type |
| **PR Checklist** | `[ ] No raw new WebSocket() — uses connectWebSocket` |

### L-4: `ServerContext` E2E 框架

| 属性 | 值 |
|------|-----|
| **文件** | [tests/manual/test_abort_e2e.py](../tests/manual/test_abort_e2e.py) — `ServerContext` |
| **所有者** | guo |
| **激活里程碑** | 所有新 E2E 测试 PR 必须引用 `ServerContext` |
| **ETA** | Phase 7 Kickoff |
| **验收标准** | 1. `test_abort_e2e.py` 可自包含运行 (无外部 server)<br>2. 新 E2E 测试文件导入 `ServerContext` 而非自建 `subprocess.Popen`<br>3. `ServerContext` 5 次连续执行 0 端口占用 |
| **维护契约** | 新增 E2E 测试时: <br>1. `from tests.manual.test_abort_e2e import ServerContext`<br>2. 在 `with ServerContext(...) as ctx:` 中编写测试 |
| **PR Checklist** | `[ ] E2E test uses ServerContext (no standalone server spawn)` |

---

## 3. Solo-Dev 自我约束机制

| 机制 | 触发条件 | 执行方式 |
|------|---------|---------|
| **PR Template** | 每次新建 PR | 4 项 Checklist 作为 `PULL_REQUEST_TEMPLATE.md` |
| **SSOT 校验脚本** | `tools/_check_ssot.sh` 运行时 | 检查 `_MODEL_CATALOG` ↔ `agent/constants.py` 一致性 |
| **grep audit** | 每月 1 次 | `grep -rn "new WebSocket" web/src/` → 应仅在 `useWebSocket.ts` 命中 |
| **E2E import audit** | 每月 1 次 | `grep -rn "import subprocess.*Popen" tests/manual/ → 应 0 hits (ServerContext wraps it) |

---

*本文档随 Phase 7 启动时正式激活。首次复审: 2026-08-01 (Phase 7 Kickoff)。*
