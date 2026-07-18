"""executor — model-context-protocol bridge + legacy process layer.

Only the MCP subsystem and process management are production code.
CC reference implementations (tool_executor, query_loop, context_compression,
tool_partition, tool_registry, streaming_executor) have been absorbed into
the main agent path and removed.

Keep this file for backward compatibility — new code should import
directly from core/, agent/, or executor/mcp/.
"""

from executor.tool import (
    ConcreteTool,
    PermissionDecision,
    Tool,
    ToolCall,
    ToolExecutionResult,
    ToolResult,
    ToolUseContext,
    build_tool,
)
from agent.mcp import (
    ExecutionPolicy,
    MCPCallResult,
    MCPConfigLoadResult,
    MCPNotInstalledError,
    MCPServerConfig,
    MCPServerConnection,
    MCPServerPolicy,
    MCPToolBridge,
    MCPToolCallError,
    MCPToolExhaustedError,
    MCPToolInfo,
    MCPToolTimeoutError,
    SyncMCPToolManager,
    adapt_mcp_tools,
    assemble_tool_pool,
    deferred_mcp_tool,
    expand_mcp_env_vars,
    filter_built_in_tools,
    filter_mcp_tools,
    find_tool,
    is_deferred_tool,
    is_mcp_server_allowed,
    load_allowed_mcp_server_configs,
    load_mcp_config,
    mcp_tool_to_runtime_tool,
    slugify_mcp_name,
    tools_to_api_schemas,
)

SiblingStreamingToolExecutor = None
