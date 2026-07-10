"""Examples for creating and executing tools with the runtime package."""

from __future__ import annotations

import asyncio

from runtime import build_tool, ToolCall, ToolRegistry, ToolResult, ToolUseContext, execute_tool_calls


async def _file_read_call(input: dict, context: ToolUseContext) -> ToolResult[str]:
    return ToolResult(output=f"read {input.get('path')} from {context.working_dir}")


async def _bash_call(input: dict, context: ToolUseContext) -> ToolResult[str]:
    return ToolResult(output=f"ran {input.get('command')} in {context.working_dir}")


async def _memory_list_call(input: dict, context: ToolUseContext) -> ToolResult[str]:
    return ToolResult(output=f"listed memories with {input}")


file_read_tool = build_tool(
    name="file_read",
    input_schema={
        "type": "object",
        "properties": {
            "path": {"type": "string", "description": "File path to read"},
            "offset": {"type": "integer", "description": "Line offset", "default": 0},
            "limit": {"type": "integer", "description": "Max lines to read", "default": 2000},
        },
        "required": ["path"],
    },
    call_fn=_file_read_call,
    description_text="Read a file. By default reads up to 2000 lines...",
    is_concurrency_safe=lambda _: True,
    is_read_only=lambda _: True,
    max_result_size_chars=2_000_000,
)


bash_tool = build_tool(
    name="bash",
    input_schema={
        "type": "object",
        "properties": {
            "command": {"type": "string", "description": "Shell command to execute"},
        },
        "required": ["command"],
    },
    call_fn=_bash_call,
    description_text="Execute a shell command.",
    is_concurrency_safe=lambda _: False,
    is_read_only=lambda _: False,
    is_destructive=lambda input: any(
        keyword in (input or {}).get("command", "")
        for keyword in ["rm -rf", "DROP TABLE", "format"]
    ),
)


memory_list_tool = build_tool(
    name="memory_list",
    input_schema={
        "type": "object",
        "properties": {
            "type": {"type": "string", "description": "Filter by memory type"},
            "query": {"type": "string", "description": "Filter by keyword"},
            "limit": {"type": "integer", "description": "Max entries", "default": 20},
            "offset": {"type": "integer", "description": "Skip entries", "default": 0},
        },
    },
    call_fn=_memory_list_call,
    description_text="List memories with optional filtering and pagination...",
    is_concurrency_safe=lambda _: True,
    is_read_only=lambda _: True,
)


async def main() -> None:
    registry = ToolRegistry()
    registry.register(file_read_tool)
    registry.register(bash_tool)
    registry.register(memory_list_tool)

    api_defs = registry.get_api_definitions()
    print(api_defs)

    calls = [
        ToolCall(id="call_1", name="file_read", input={"path": "main.py"}),
        ToolCall(id="call_2", name="file_read", input={"path": "utils.py"}),
        ToolCall(id="call_3", name="bash", input={"command": "ls -la"}),
        ToolCall(id="call_4", name="file_read", input={"path": "config.yaml"}),
    ]

    context = ToolUseContext(working_dir=".", session_id="test")
    results = await execute_tool_calls(calls, registry, context)

    for result in results:
        print(f"{result.tool_name}({result.call_id}): {result.duration_ms:.1f}ms")


if __name__ == "__main__":
    asyncio.run(main())
