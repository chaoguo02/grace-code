"""
tools/mcp_client.py

MCP (Model Context Protocol) 客户端：
- 连接独立的 MCP Server（子进程，stdio JSON-RPC 传输）
- 自动发现其提供的工具
- 将每个远程工具包装成本地 BaseTool，注册到 ToolRegistry
- 代理工具调用请求给远程 MCP Server

支持多种 MCP Server：
- 我们自己写的 web_search_server
- 第三方提供的 MCP Server（如 Brave Search、Postgres 等）
- Claude Desktop 兼容的 MCP Server

设计：
- MCPToolProxy 继承 BaseTool，对 agent core 完全透明
- 所有 JSON-RPC 通信通过 mcp Python SDK 处理
- 同步 API，适配现有同步 BaseTool 接口
"""

from __future__ import annotations

import asyncio
import logging
import os
import subprocess
from dataclasses import dataclass
from typing import Any, AsyncGenerator, Optional, List

try:
    from mcp.client.stdio import stdio_client, StdioServerParameters
    from mcp.client.session import ClientSession
    from mcp.types import (
        Tool as MCPTool,
        TextContent,
        CallToolResult,
    )
    HAS_MCP = True
except ImportError:
    HAS_MCP = False
    stdio_client = None
    StdioServerParameters = None
    ClientSession = None
    MCPTool = None
    TextContent = None
    CallToolResult = None

try:
    from mcp.types import Resource, ResourceTemplate, Prompt
except ImportError:
    Resource = None
    ResourceTemplate = None
    Prompt = None

from tools.base import BaseTool, ToolResult

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# 持久事件循环：MCP session 必须在同一个 event loop 中执行所有操作
# ---------------------------------------------------------------------------

import threading

_mcp_loop: asyncio.AbstractEventLoop | None = None
_mcp_thread: threading.Thread | None = None


def _get_mcp_loop() -> asyncio.AbstractEventLoop:
    """获取或创建 MCP 专用的后台事件循环。"""
    global _mcp_loop, _mcp_thread
    if _mcp_loop is not None and _mcp_loop.is_running():
        return _mcp_loop

    _mcp_loop = asyncio.new_event_loop()

    def _run_loop():
        asyncio.set_event_loop(_mcp_loop)
        _mcp_loop.run_forever()

    _mcp_thread = threading.Thread(target=_run_loop, daemon=True)
    _mcp_thread.start()
    return _mcp_loop


def _run_async(coro):
    """在 MCP 专用事件循环中执行协程，从同步代码调用。"""
    loop = _get_mcp_loop()
    future = asyncio.run_coroutine_threadsafe(coro, loop)
    return future.result(timeout=60)


# ---------------------------------------------------------------------------
# Configuration: MCPServerConfig
# ---------------------------------------------------------------------------

@dataclass
class MCPServerConfig:
    """
    MCP Server 连接配置。

    对应 Claude Desktop config.json 中每个 server 条目：
    {
        "command": "python",
        "args": ["-m", "mcp_servers.web_search_server"],
        "env": {"SEARCH_MAX_RESULTS": "10"},
        "cwd": "/path/to/cwd",
    }

    Attributes:
        name:    服务器名称（用于日志和错误信息）
        command: 启动命令，如 "python" 或 "npx"
        args:    命令行参数列表
        env:     额外环境变量（可选，会继承当前进程环境）
        cwd:     工作目录（可选，默认当前目录）
    """
    name: str
    command: str
    args: list[str]
    env: dict[str, str] | None = None
    cwd: str | None = None


# ---------------------------------------------------------------------------
# Proxy: MCPToolProxy (BaseTool subclass)
# ---------------------------------------------------------------------------

class MCPToolProxy(BaseTool):
    """
    把一个远程 MCP tool 包装成本地 BaseTool。

    对 forge-agent core 完全透明 — 就像使用本地工具一样使用远程工具。
    """

    def __init__(
        self,
        mcp_tool: MCPTool,
        session: ClientSession,
        server_name: str,
    ) -> None:
        self._mcp_tool = mcp_tool
        self._session = session
        self._server_name = server_name
        self._name = mcp_tool.name
        self._description = mcp_tool.description or f"MCP tool {mcp_tool.name} from {server_name}"
        self._parameters_schema = mcp_tool.inputSchema

    @property
    def name(self) -> str:
        return self._name

    @property
    def description(self) -> str:
        return self._description

    @property
    def parameters_schema(self) -> dict[str, Any]:
        return self._parameters_schema

    def execute(self, params: dict[str, Any]) -> ToolResult:
        """
        同步执行：调用远程 MCP Server 的工具调用。

        会阻塞当前线程等待响应，适配现有同步 API。
        """
        try:
            result = _run_async(self._call_remote(params))
        except Exception as exc:
            return ToolResult(
                success=False,
                output="",
                error=f"MCP tool '{self._name}' from server '{self._server_name}' failed: {exc}",
            )

        # 解析 CallToolResult — 拼接所有 text content
        output_text = ""
        has_error = False
        for content in result.content:
            if isinstance(content, TextContent):
                output_text += content.text + "\n"
            # 其他类型（如 image）暂时不支持，忽略
        output_text = output_text.strip()

        if getattr(result, 'isError', False):
            return ToolResult(
                success=False,
                output=output_text,
                error=output_text or f"MCP tool '{self._name}' returned error",
            )

        return ToolResult(
            success=True,
            output=output_text,
        )

    async def _call_remote(self, params: dict[str, Any]) -> CallToolResult:
        """异步封装，供 asyncio.run() 调用。"""
        return await self._session.call_tool(self._name, params)


# ---------------------------------------------------------------------------
# Manager: MCPClientManager
# ---------------------------------------------------------------------------

def _expand_uri_template(template: str, variables: dict[str, str]) -> str:
    """RFC 6570 Level 1 URI 模板展开（简易实现，只支持 {var} 格式）。"""
    import re
    def _replace(match):
        var_name = match.group(1)
        return variables.get(var_name, match.group(0))
    return re.sub(r'\{(\w+)\}', _replace, template)


def _extract_template_variables(template: str) -> list[str]:
    """从 URI 模板中提取变量名列表。"""
    import re
    return re.findall(r'\{(\w+)\}', template)


class MCPResourceListTool(BaseTool):
    """列出某个 MCP Server 的所有 resources 和 resource templates。"""

    def __init__(self, server_name: str, manager: "MCPClientManager") -> None:
        self._server_name = server_name
        self._manager = manager

    @property
    def name(self) -> str:
        return f"mcp__{self._server_name}__list_resources"

    @property
    def description(self) -> str:
        return f"List available resources and resource templates from MCP server '{self._server_name}'"

    @property
    def parameters_schema(self) -> dict[str, Any]:
        return {"type": "object", "properties": {}}

    def execute(self, params: dict[str, Any]) -> ToolResult:
        resources = self._manager._resources.get(self._server_name, [])
        templates = self._manager._resource_templates.get(self._server_name, [])
        if not resources and not templates:
            return ToolResult(success=True, output="No resources available.")
        lines = []
        if resources:
            lines.append("## Resources (static)")
            for r in resources:
                uri = getattr(r, 'uri', str(r))
                rname = getattr(r, 'name', '')
                desc = getattr(r, 'description', '')
                line = f"- {uri}"
                if rname:
                    line += f"  ({rname})"
                if desc:
                    line += f"  {desc}"
                lines.append(line)
        if templates:
            lines.append("## Resource Templates (parameterized)")
            for t in templates:
                uri_tpl = getattr(t, 'uriTemplate', str(t))
                tname = getattr(t, 'name', '')
                desc = getattr(t, 'description', '')
                variables = _extract_template_variables(uri_tpl)
                line = f"- {uri_tpl}"
                if tname:
                    line += f"  ({tname})"
                if desc:
                    line += f"  {desc}"
                if variables:
                    line += f"  [params: {', '.join(variables)}]"
                lines.append(line)
        return ToolResult(success=True, output="\n".join(lines))


class MCPResourceReadTool(BaseTool):
    """读取某个 MCP Server 的指定资源内容，支持静态 URI 和模板 URI 展开。"""

    def __init__(self, server_name: str, session: ClientSession, manager: "MCPClientManager") -> None:
        self._server_name = server_name
        self._session = session
        self._manager = manager

    @property
    def name(self) -> str:
        return f"mcp__{self._server_name}__read_resource"

    @property
    def description(self) -> str:
        templates = self._manager._resource_templates.get(self._server_name, [])
        if templates:
            tpl_info = "; ".join(
                getattr(t, 'uriTemplate', '') for t in templates[:3]
            )
            return (
                f"Read a resource by URI from MCP server '{self._server_name}'. "
                f"For template URIs ({tpl_info}...), provide variables to expand them."
            )
        return f"Read a resource by URI from MCP server '{self._server_name}'"

    @property
    def parameters_schema(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "uri": {
                    "type": "string",
                    "description": "Resource URI to read (static URI or expanded template URI)",
                },
                "template": {
                    "type": "string",
                    "description": "URI template to expand (e.g. 'db://users/{user_id}/profile'). If provided, 'variables' must also be given.",
                },
                "variables": {
                    "type": "object",
                    "description": "Variables to fill into the URI template (e.g. {\"user_id\": \"42\"})",
                    "additionalProperties": {"type": "string"},
                },
            },
        }

    def execute(self, params: dict[str, Any]) -> ToolResult:
        uri = params.get("uri", "")
        template = params.get("template", "")
        variables = params.get("variables", {})

        # 模板展开优先
        if template:
            required_vars = _extract_template_variables(template)
            missing = [v for v in required_vars if v not in variables]
            if missing:
                return ToolResult(
                    success=False, output="",
                    error=f"Missing template variables: {', '.join(missing)}. Required: {', '.join(required_vars)}",
                )
            uri = _expand_uri_template(template, variables)

        if not uri:
            return ToolResult(
                success=False, output="",
                error="Either 'uri' or 'template' + 'variables' is required",
            )

        try:
            result = _run_async(self._read_resource(uri))
            return ToolResult(success=True, output=result)
        except Exception as exc:
            return ToolResult(
                success=False, output="",
                error=f"Failed to read resource '{uri}': {exc}",
            )

    async def _read_resource(self, uri: str) -> str:
        resp = await self._session.read_resource(uri)
        parts = []
        for content in resp.contents:
            if hasattr(content, 'text'):
                parts.append(content.text)
            elif hasattr(content, 'blob'):
                parts.append(f"[binary data, {len(content.blob)} bytes]")
        return "\n".join(parts) if parts else "(empty)"


class MCPClientManager:
    """
    管理多个 MCP Server 连接：
    1. 启动每个 server 子进程（via stdio_client）
    2. 初始化 session
    3. 调用 tools/list 发现工具
    4. 发现 resources 和 prompts
    5. 返回 MCPToolProxy 列表供注册

    使用后必须调用 close() 关闭所有连接和子进程。
    """

    def __init__(self) -> None:
        self._configs: list[MCPServerConfig] = []
        self._processes: list[subprocess.Popen] = []
        self._sessions: list[ClientSession] = []
        self._session_map: dict[str, ClientSession] = {}
        self._proxies: list[BaseTool] = []
        self._context_managers: list[Any] = []
        self._connected = False
        self._resources: dict[str, list] = {}
        self._resource_templates: dict[str, list] = {}
        self._prompts: dict[str, list] = {}

    def add_server(self, config: MCPServerConfig) -> "MCPClientManager":
        """添加一个 MCP Server 配置。"""
        if self._connected:
            raise RuntimeError("Cannot add servers after connect()")
        self._configs.append(config)
        return self

    async def connect_server(
        self, config: MCPServerConfig,
    ) -> AsyncGenerator[BaseTool, None]:
        """连接单个 MCP Server 并发现工具。"""
        # 准备环境变量 — 继承当前环境 + 额外覆盖
        env = dict(os.environ)
        if config.env:
            env.update(config.env)

        # 使用 stdio_client 启动子进程并获取 transport streams
        logger.info(f"Starting MCP server '{config.name}': {config.command} {' '.join(config.args)}")
        server_params = StdioServerParameters(
            command=config.command,
            args=config.args,
            env=env,
            cwd=config.cwd or os.getcwd(),
        )
        cm = stdio_client(server_params)
        read_stream, write_stream = await cm.__aenter__()
        self._context_managers.append(cm)

        # 创建 session 并初始化
        session = ClientSession(read_stream, write_stream)
        session_cm = session
        await session_cm.__aenter__()
        self._sessions.append(session)

        await session.initialize()
        logger.debug(f"Initialized MCP server '{config.name}'")

        # 记录 session
        self._session_map[config.name] = session

        # 列出所有工具
        tools_resp = await session.list_tools()
        logger.info(
            f"MCP server '{config.name}' discovered {len(tools_resp.tools)} tools: "
            f"{[t.name for t in tools_resp.tools]}"
        )

        # 创建 proxy 给每个工具
        for mcp_tool in tools_resp.tools:
            proxy = MCPToolProxy(mcp_tool, session, config.name)
            self._proxies.append(proxy)
            yield proxy

        # 发现 resources + resource templates
        has_resources = False
        try:
            resources_resp = await session.list_resources()
            self._resources[config.name] = resources_resp.resources
            if resources_resp.resources:
                has_resources = True
                logger.info(
                    f"MCP server '{config.name}' has {len(resources_resp.resources)} resources"
                )
        except Exception as e:
            logger.debug(f"MCP server '{config.name}' does not support resources: {e}")

        try:
            templates_resp = await session.list_resource_templates()
            self._resource_templates[config.name] = templates_resp.resourceTemplates
            if templates_resp.resourceTemplates:
                has_resources = True
                logger.info(
                    f"MCP server '{config.name}' has {len(templates_resp.resourceTemplates)} resource templates"
                )
        except Exception as e:
            logger.debug(f"MCP server '{config.name}' does not support resource templates: {e}")

        if has_resources:
            res_list_tool = MCPResourceListTool(config.name, self)
            res_read_tool = MCPResourceReadTool(config.name, session, self)
            self._proxies.append(res_list_tool)
            self._proxies.append(res_read_tool)
            yield res_list_tool
            yield res_read_tool

        # 发现 prompts
        try:
            prompts_resp = await session.list_prompts()
            self._prompts[config.name] = prompts_resp.prompts
            if prompts_resp.prompts:
                logger.info(
                    f"MCP server '{config.name}' has {len(prompts_resp.prompts)} prompts"
                )
        except Exception as e:
            logger.debug(f"MCP server '{config.name}' does not support prompts: {e}")

    async def connect_all(self) -> AsyncGenerator[BaseTool, None]:
        """连接所有已添加的服务器，yield 所有工具 proxy。"""
        for config in self._configs:
            async for proxy in self.connect_server(config):
                yield proxy
        self._connected = True

    def connect_and_discover_sync(self) -> list[BaseTool]:
        """同步版 connect_all，供入口代码调用。"""
        proxies: list[BaseTool] = []

        async def _run():
            async for proxy in self.connect_all():
                proxies.append(proxy)

        _run_async(_run())
        return proxies

    def close(self) -> None:
        """关闭所有连接和子进程。"""
        logger.info(f"Closing {len(self._context_managers)} MCP server connections...")

        async def _close_all():
            for session in self._sessions:
                try:
                    await session.__aexit__(None, None, None)
                except Exception as exc:
                    logger.warning(f"Error closing MCP session: {exc}")
            for cm in self._context_managers:
                try:
                    await cm.__aexit__(None, None, None)
                except Exception as exc:
                    logger.warning(f"Error closing MCP transport: {exc}")

        try:
            _run_async(_close_all())
        except Exception:
            pass

        self._context_managers.clear()
        self._sessions.clear()
        self._proxies.clear()
        self._connected = False

    @property
    def proxies(self) -> list[BaseTool]:
        """已发现的工具代理列表。"""
        return self._proxies

    @property
    def resources(self) -> dict[str, list]:
        """每个 server 的 resources 列表。"""
        return self._resources

    @property
    def resource_templates(self) -> dict[str, list]:
        """每个 server 的 resource templates 列表。"""
        return self._resource_templates

    @property
    def prompts(self) -> dict[str, list]:
        """每个 server 的 prompts 列表。"""
        return self._prompts

    def get_prompt(self, server_name: str, prompt_name: str, arguments: dict[str, str] | None = None) -> str | None:
        """获取 MCP server 上某个 prompt 的渲染结果。"""
        session = self._session_map.get(server_name)
        if not session:
            return None
        try:
            result = _run_async(self._get_prompt_async(session, prompt_name, arguments))
            return result
        except Exception as e:
            logger.warning(f"Failed to get prompt '{prompt_name}' from '{server_name}': {e}")
            return None

    async def _get_prompt_async(self, session: ClientSession, name: str, arguments: dict[str, str] | None) -> str:
        resp = await session.get_prompt(name, arguments or {})
        parts = []
        for msg in resp.messages:
            content = msg.content
            if hasattr(content, 'text'):
                parts.append(content.text)
            elif isinstance(content, str):
                parts.append(content)
        return "\n".join(parts) if parts else ""

    def __len__(self) -> int:
        return len(self._proxies)


# ---------------------------------------------------------------------------
# Helper: 从配置字典批量创建
# ---------------------------------------------------------------------------

def create_manager_from_config(
    servers_config: dict[str, dict[str, Any]],
    base_dir: str | None = None,
) -> MCPClientManager:
    """
    从 yaml 配置的 mcp_servers 字典创建 Manager。

    格式示例：
    mcp_servers:
      web-search:
        command: python
        args: ["-m", "mcp_servers.web_search_server"]
        env:
          SEARCH_MAX_RESULTS: "10"
        cwd: "/absolute/path"

    Returns:
        已添加所有 server 配置的 MCPClientManager（还未连接）
    """
    manager = MCPClientManager()
    base_cwd = base_dir or os.getcwd()

    for name, cfg in servers_config.items():
        command = cfg.get("command")
        args = cfg.get("args", [])
        env = cfg.get("env")
        cwd = cfg.get("cwd")

        if not command:
            logger.warning(f"Skipping MCP server '{name}': missing 'command'")
            continue

        # 相对 cwd 相对于项目根目录
        if cwd and not os.path.isabs(cwd) and base_dir:
            cwd = os.path.join(base_dir, cwd)
        elif not cwd:
            cwd = base_cwd

        manager.add_server(MCPServerConfig(
            name=name,
            command=command,
            args=args,
            env=env,
            cwd=cwd,
        ))

    return manager