# P0 #2: MCP Streamable HTTP 传输迁移 — 实施计划

> 状态：计划阶段 | 日期：2026-08-01
> 来源：跨模块安全审计 — MCP 模块 P0 发现（协议级不合规）
> 预计工作量：7–12 人日（传输替换）+ 4–6 人日（conformance 测试）
> 前置依赖：MCP SDK 版本升级（如需）

---

## 1. 问题陈述

### 1.1 根因

`agent/mcp/client.py` 中的三种远程传输实现（`HttpMCPBridge`、`SseMCPBridge`、`WsMCPBridge`）均为自研 JSON-RPC 2.0 实现，不与 MCP Python SDK 集成。与[官方 MCP Transport 规范 (2025-06-18)](https://spec.modelcontextprotocol.io/specification/2025-06-18/basic/transports/) 对标后，存在以下协议级不合规：

| 差距 | 严重度 | 当前行为 | 协议要求 |
|------|--------|---------|---------|
| Session ID | 🔴 | 从 `InitializeResult` JSON body 中提取，但**从不回传**给后续请求 | 从 `Mcp-Session-Id` **响应头**取值，后续所有请求通过 `Mcp-Session-Id` **请求头**回传 |
| 协议版本 | 🔴 | 硬编码 `"2024-11-05"`；后续请求**不发送** `MCP-Protocol-Version` 头 | 使用服务器返回的版本；后续所有请求必须携带 `MCP-Protocol-Version` 头 |
| 初始化生命周期 | 🔴 | 调用 `initialize` 后**不发送** `notifications/initialized` | `initialize` 响应后，客户端**必须**发送 `notifications/initialized` |
| 响应类型协商 | 🔴 | HTTP bridge 假定所有响应为 JSON | POST 响应可以是 JSON、SSE（`Accept: text/event-stream`），或 `202 Accepted` + `Location` 头 |
| URL 端点 | 🟡 | 强制拼接 `/mcp` 到配置 URL | 应使用配置 URL 原值作为 endpoint |
| SSE 响应关联 | 🟡 | `_sse_responses` dict 有写入无读取 | 必须按 JSON-RPC `id` 匹配异步响应 |
| WS 并发读取 | 🟡 | 每调用自行 `send/recv`，无单 reader | 需要单 reader task + `id` 到 Future 的分发映射 |
| SSE 断线重连 | 🟡 | SSE stream 退出时仅 debug 日志 | 需要检测 stream 故障并自动重连 |
| Resource/Prompt 链路 | 🟡 | Text resource 只读文本；prompt 有实现但无同步层暴露 | 需保留 blob/mimeType/annotations；暴露 prompt 到 Agent |

### 1.2 受影响文件

| 文件 | 行号 | 当前实现 |
|------|------|---------|
| `agent/mcp/client.py:556-711` | HttpMCPBridge | 自研 HTTP JSON-RPC |
| `agent/mcp/client.py:718-821` | SseMCPBridge | 自研 GET /sse + POST /mcp |
| `agent/mcp/client.py:827-917` | WsMCPBridge | 自定义 WebSocket JSON-RPC |
| `agent/mcp/client.py:151-549` | MCPToolBridge | stdio bridge（使用 SDK，**无问题**） |
| `agent/mcp/sync_bridge.py:149-353` | SyncMCPToolManager | 聚合、重连、watchdog |
| `agent/session/mcp_integration.py:1-386` | MCPToolIntegration | Session 级 MCP 生命周期 |

---

## 2. 目标架构

```
create_mcp_bridge(config)
  ├─ "stdio"        → MCPToolBridge (保留，SDK 驱动)
  ├─ "streamable"   → StreamableHttpBridge (新增，SDK streamable HTTP client)
  ├─ "sse"          → DEPRECATED — mapping to "streamable" with 2024 compat fallback
  ├─ "http"         → DEPRECATED — mapping to "streamable"
  └─ "ws"           → 自定义传输保留，但明确标注为非标准 MCP transport
```

**StreamableHttpBridge 核心行为**：
- 使用 MCP SDK 的 Streamable HTTP client（如果 SDK ≥ 1.x 提供），否则实现 spec-compliant 版本
- 单一 endpoint URL（不拼接 `/mcp`）
- POST 请求携带 `Accept: application/json, text/event-stream`、`Mcp-Session-Id`、`MCP-Protocol-Version` 头
- 处理 JSON 响应、SSE 流式响应、`202 Accepted`（long-running operation）
- 初始化：`initialize` → 保存版本/capability → `notifications/initialized` → capability-gated 操作
- Session 管理：从 `Mcp-Session-Id` 响应头建立；`404` 错误重建 session；`DELETE` 终止 session
- 单 reader task + pending Future map 处理并发请求分发（如 SSE/WS 通道）

---

## 3. 实施步骤

### Step 1: 协议 conformance 测试服务器 (2–3 人日)

**文件**: 新建 `tests/mcp_conformance/`

**3.1.1** 构建一个最小 MCP streamable HTTP 测试服务器，用于验证所有传输行为：

```python
# tests/mcp_conformance/server.py
class ConformanceTestServer:
    """MCP Streamable HTTP server that asserts client behavior.

    Validates:
    - Mcp-Session-Id header presence on requests after initialize
    - MCP-Protocol-Version header uses server-negotiated version
    - notifications/initialized sent before tools/list
    - Accept header includes both application/json and text/event-stream
    - DELETE /mcp terminates session
    - 404 triggers session recreation
    """
```

**3.1.2** 覆盖这些测试场景：
- `test_initialize_handshake` — 完整的 initialize → initialized 序列
- `test_session_id_carried_on_all_requests` — Session ID 在每个请求中通过 header 传递
- `test_protocol_version_carried` — 使用协商版本，而非硬编码
- `test_json_response` — POST 返回 JSON
- `test_sse_response` — POST 返回 SSE 流
- `test_accepted_response` — POST 返回 202 + Location
- `test_rejects_tools_list_before_initialized` — 初始化前调用 tools/list 被拒绝
- `test_notifications_received` — 服务端通知被正确处理
- `test_session_delete` — DELETE 终止 session

### Step 2: 实现 StreamableHttpBridge (3–5 人日)

**文件**: `agent/mcp/client.py`（新增类）

**3.2.1** 首先检查 MCP SDK 版本。在 `pyproject.toml` 中将 `mcp>=1.0.0,<2.0.0` 升级到包含 Streamable HTTP 客户端的最低版本（如 `>=1.5.0`）。如果官方 SDK 还不支持 Streamable HTTP 客户端：

- 选项 A: 使用官方 SDK 的 `streamablehttp_client`（若存在）
- 选项 B: 基于 `httpx.AsyncClient` 实现规范合规版本，遵循[官方 Streamable HTTP 传输规范](https://spec.modelcontextprotocol.io/specification/2025-06-18/basic/transports/#streamable-http)

**3.2.2** 新 `StreamableHttpBridge` 的关键方法签名：

```python
class StreamableHttpBridge(MCPToolBridge):
    """MCP Streamable HTTP transport — spec compliant."""

    def __init__(self, config: MCPServerConfig):
        super().__init__(config)
        self._client: httpx.AsyncClient | None = None
        self._session_id: str | None = None
        self._protocol_version: str = "2025-06-18"  # preferred, negotiate down
        self._server_capabilities: dict = {}
        self._pending: dict[int, asyncio.Future] = {}  # request ID -> response future
        self._reader_task: asyncio.Task | None = None   # SSE reader
        self._endpoint_url: str = config.url  # AS-IS, no /mcp append

    async def _initialize(self) -> dict:
        """Send initialize + await response + send notifications/initialized."""
        result = await self._rpc_call("initialize", {
            "protocolVersion": self._protocol_version,
            "capabilities": {...},
            "clientInfo": {"name": "grace-code", "version": "1.0"},
        })
        negotiated = result.get("protocolVersion", self._protocol_version)
        self._protocol_version = negotiated
        self._server_capabilities = result.get("capabilities", {})
        # Extract session ID from RESPONSE HEADER, not JSON body
        # (handled in _rpc_call response processing)
        await self._send_notification("notifications/initialized", {})
        return result

    async def _rpc_call(self, method: str, params: dict) -> dict:
        """JSON-RPC call over Streamable HTTP.

        Key behaviors:
        - Single URL (self._endpoint_url), no path append
        - Headers: Accept (json+sse), Mcp-Session-Id, MCP-Protocol-Version
        - Handle JSON response, SSE response, 202 Accepted
        - On 404: clear session, re-initialize, retry once
        """
        ...

    async def _send_notification(self, method: str, params: dict) -> None:
        """Fire-and-forget notification (no id field)."""
        ...

    async def _read_sse_stream(self) -> None:
        """Background task: GET endpoint with Accept: text/event-stream.
        Route responses by id to self._pending futures.
        Dispatch notifications to handlers.
        """
        ...

    async def close(self) -> None:
        """DELETE session, cancel reader task, close HTTP client."""
        ...
```

**3.2.3** HTTP header 处理：

```python
def _build_headers(self) -> dict[str, str]:
    headers = {
        "Accept": "application/json, text/event-stream",
        "Content-Type": "application/json",
        "MCP-Protocol-Version": self._protocol_version,
    }
    if self._session_id:
        headers["Mcp-Session-Id"] = self._session_id
    if self._config.headers:
        headers.update(self._config.headers)
    return headers
```

**3.2.4** 响应处理逻辑：

```python
async def _process_response(self, response: httpx.Response) -> dict:
    content_type = response.headers.get("content-type", "")

    if response.status_code == 202:
        # Long-running operation — poll Location header
        location = response.headers.get("Location")
        ...

    if "text/event-stream" in content_type:
        # Read full SSE stream, collect final response
        ...

    if "application/json" in content_type:
        data = response.json()
        if "error" in data:
            raise MCPToolCallError(...)
        return data.get("result", {})

    raise MCPTransportError(f"Unexpected content-type: {content_type}")
```

### Step 3: 废弃旧传输并建立迁移路径 (1–2 人日)

**文件**: `agent/mcp/client.py`, `agent/mcp/types.py`

**3.3.1** 在 `create_mcp_bridge()` 工厂中更新 transport 映射：

```python
def create_mcp_bridge(config: MCPServerConfig) -> MCPToolBridge:
    transport = config.transport or "stdio"
    if transport == "stdio":
        return MCPToolBridge(config)
    elif transport in ("http", "sse", "streamable"):
        # All remote HTTP transports go through StreamableHttpBridge
        logger.warning(
            "Transport '%s' is deprecated — using 'streamable' instead. "
            "Update your MCP server config.",
            transport,
        )
        return StreamableHttpBridge(config)
    elif transport == "ws":
        logger.warning(
            "WebSocket transport is non-standard. "
            "MCP spec only defines stdio and Streamable HTTP."
        )
        return WsMCPBridge(config)
    raise ValueError(f"Unknown transport: {transport}")
```

**3.3.2** 保留 `HttpMCPBridge` 和 `SseMCPBridge` 的代码但标记为 deprecated，并添加 `DeprecationWarning`。两个发布周期后删除。

**3.3.3** 保留 `WsMCPBridge` 但重命名 transport key 为 `"ws-custom"` 并添加文档说明"非标准 MCP 传输，仅用于自定义后端"。

### Step 4: Resource/Prompt 能力完善 (2–3 人日)

**文件**: `agent/mcp/client.py`, `agent/mcp/sync_bridge.py`

**3.4.1** Resource 增强 — 保留 blob 和 annotations：

```python
# 当前 (line 388): 只保留 text
# 修复后:
@dataclass
class MCPResourceContent:
    uri: str
    mime_type: str | None
    text: str | None
    blob: bytes | None
    annotations: dict | None

class MCPResourceEntry:
    uri: str
    name: str
    description: str | None
    mime_type: str | None
    size: int | None
    annotations: dict | None
    # templates for parameterized resources
    uri_template: str | None
```

转换为统一 `ContentBlock`，带入 mimeType 和 annotations。

**3.4.2** 将 Prompt API 暴露到同步管理层：

```python
# agent/mcp/sync_bridge.py
class SyncMCPToolManager:
    def list_prompts(self) -> list[MCPPromptInfo]: ...
    def get_prompt(self, name: str, arguments: dict) -> MCPPromptResult: ...
```

**3.4.3** Resource/Prompt 的热变更通知（`notifications/resources/list_changed`、`notifications/prompts/list_changed`）接入 watchdog 循环。

### Step 5: 集成与回归测试 (2–3 人日)

**3.5.1** StreamableHttpBridge 与 conformance 服务器的集成测试：
- `test_full_lifecycle` — initialize → tools/list → tools/call → close
- `test_session_recovery_on_404` — 模拟 session 过期后的自动重建
- `test_sse_streaming_response` — 服务端返回 SSE 流
- `test_concurrent_requests` — 两个并发 RPC，响应按 id 正确关联
- `test_notification_handling` — 服务端推送 `tools/list_changed`

**3.5.2** 回归测试：
- stdio bridge 行为不变（与 conformance 服务器的 stdio 模式测试）
- 现有 MCP 集成测试持续通过
- 68 项基础测试保持通过

---

## 4. SDK 版本决策

**关键先决条件**：需要先确认当前 `mcp>=1.0.0,<2.0.0` 的最新版本是否包含 `streamablehttp_client`。

```bash
pip index versions mcp  # 检查可用版本
```

**决策矩阵**：

| SDK 包含 Streamable HTTP | 方案 | 工作量 |
|--------------------------|------|--------|
| ✅ 是 | 直接使用官方 `streamablehttp_client`，替换自研 HTTP/SSE bridge | 低（3–5 人日） |
| ❌ 否 | 参照官方规范自研 + 标注 "待 SDK 正式支持后替换" | 高（7–12 人日） |

如果是方案 B，应在 `agent/mcp/client.py` 文件顶部添加：

```python
# STREAMABLE_HTTP: Custom spec-compliant implementation.
# TODO: Replace with official MCP SDK streamablehttp_client when available.
# Track: https://github.com/modelcontextprotocol/python-sdk/issues/XXXX
```

---

## 5. 风险与回滚

| 风险 | 概率 | 影响 | 缓解措施 |
|------|------|------|---------|
| 现有 MCP server（2024-11-05 旧协议）与新 client 不兼容 | 中 | 高 | `StreamableHttpBridge` 支持协议版本协商（宣告 2025-06-18，接受降级）；初始化时检测旧 server，回退到旧 `HttpMCPBridge` |
| MCP SDK Streamable HTTP client 有未知 bug | 低 | 中 | Conformance 测试服务器覆盖所有生命周期和响应类型 |
| Resource/Prompt 变更影响下游 Tool 注册逻辑 | 中 | 中 | ContentBlock 转换层隔离变更；先通过 feature flag 默认禁用 Prompt |
| 性能退化（header 开销、SSE 持久连接） | 低 | 低 | HTTP/2 多路复用；SSE 连接复用（一 session 一流） |

**回滚方式**: 配置中添加 `mcp.transport_mode: legacy`；设置后 `create_mcp_bridge()` 使用旧 `HttpMCPBridge`/`SseMCPBridge`。两个 release 后删除旧代码。

---

## 6. 验证清单

- [ ] Conformance 服务器全部 9 个测试通过
- [ ] StreamableHttpBridge 初始化和完整调用生命周期
- [ ] Session ID 通过 HTTP header 正确回传
- [ ] 404 错误触发 session 重建 + 一次重试
- [ ] `DELETE` 请求正确发送以关闭 session
- [ ] 并发 RPC 调用的响应按 JSON-RPC `id` 正确关联
- [ ] SSE 响应被正确流式读取和解析
- [ ] `notifications/initialized` 在 `initialize` 之后、`tools/list` 之前发送
- [ ] 服务端通知（如 `tools/list_changed`）被正确分发给回调
- [ ] stdio bridge 行为零退化（回归）
- [ ] 68 项现有测试保持通过
- [ ] 手动连接第三方 MCP server（如 GitHub MCP server）验证真实互操作性

---

## 7. 文件变更汇总

| 文件 | 变更类型 | 行数估计 |
|------|---------|---------|
| `agent/mcp/client.py` | 新增 `StreamableHttpBridge`；废弃标记 Http/SseMCPBridge | +400/-30 |
| `agent/mcp/sync_bridge.py` | 新增 prompt list/get 同步接口；resource blob 保留 | +80/-20 |
| `agent/mcp/types.py` | 新增 `MCPResourceContent`、`MCPPromptInfo` | +60 |
| `agent/mcp/__init__.py` | 导出新 transport 类 | +10 |
| `agent/session/mcp_integration.py` | 适配新 transport type | +20/-10 |
| `pyproject.toml` | MCP SDK 版本升级（如需） | +1/-1 |
| `tests/mcp_conformance/server.py` | 新增 conformance 测试服务器 | +250 |
| `tests/mcp_conformance/test_streamable_http.py` | 新增 9+ 个测试 | +300 |
| `tests/mcp_conformance/test_regression.py` | stdio 回归测试 | +50 |
| **总计** | | **~1170/+60** |
