"""
mcp_servers/web_search_server.py

MCP (Model Context Protocol) Web Search Server。

提供两个工具：
- web_search: 用 DuckDuckGo 搜索网页
- web_fetch:  抓取指定 URL 并提取正文

通过 stdio transport 运行，可被任何 MCP 客户端（Claude Desktop 等）连接。

依赖：
    pip install mcp ddgs requests readability-lxml beautifulsoup4

安全：
- 继承 tools/web_utils.py 的全部安全校验（内网 IP 拦截、DNS 验证等）
- URL scheme 白名单（仅 http/https）
- 重定向安全校验
- 响应体大小限制
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from core.web_utils import (
    DEFAULT_FETCH_TIMEOUT,
    DEFAULT_MAX_FETCH_CHARS,
    DEFAULT_MAX_RETRIES,
    DEFAULT_RETRY_DELAY,
    DEFAULT_SEARCH_MAX_RESULTS,
    USER_AGENT,
    extract_content,
    is_retryable_status,
    validate_redirect,
    validate_url,
)
from core.utils import truncate_output

from mcp.server import Server
from mcp.server.stdio import stdio_server
from mcp.types import (
    Tool,
    TextContent,
)

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# 工具配置（可通过环境变量覆盖默认值）
# ---------------------------------------------------------------------------

import os

_MAX_SEARCH_RESULTS = int(
    os.environ.get("SEARCH_MAX_RESULTS", str(DEFAULT_SEARCH_MAX_RESULTS))
)
_MAX_FETCH_CHARS = int(
    os.environ.get("FETCH_MAX_CHARS", str(DEFAULT_MAX_FETCH_CHARS))
)
_FETCH_TIMEOUT = int(
    os.environ.get("FETCH_TIMEOUT", str(DEFAULT_FETCH_TIMEOUT))
)

# ---------------------------------------------------------------------------
# 工具定义（MCP Tools schema）
# ---------------------------------------------------------------------------

SEARCH_TOOL = Tool(
    name="web_search",
    description=(
        "Search the web using DuckDuckGo. Returns a list of results "
        "with title, URL, and snippet. Use this to find up-to-date "
        "information, documentation, or solutions that are not in "
        "the local codebase."
    ),
    inputSchema={
        "type": "object",
        "properties": {
            "query": {
                "type": "string",
                "description": "Search query string",
            },
            "count": {
                "type": "integer",
                "description": (
                    f"Number of results (default 5, max {_MAX_SEARCH_RESULTS})"
                ),
            },
        },
        "required": ["query"],
    },
)

FETCH_TOOL = Tool(
    name="web_fetch",
    description=(
        "Fetch the content of a web page and extract the main text "
        "(using Mozilla's readability algorithm). Use this after "
        "web_search to read a specific page in detail, e.g. official "
        "documentation, blog posts, or API references. "
        f"Timeout is {_FETCH_TIMEOUT}s, max output is ~{_MAX_FETCH_CHARS // 1000}KB."
    ),
    inputSchema={
        "type": "object",
        "properties": {
            "url": {
                "type": "string",
                "description": "The URL to fetch (must be http or https)",
            },
        },
        "required": ["url"],
    },
)

# ---------------------------------------------------------------------------
# MCP Server
# ---------------------------------------------------------------------------

server = Server("web-search-server")


@server.list_tools()
async def handle_list_tools() -> list[Tool]:
    """返回可用工具列表。"""
    return [SEARCH_TOOL, FETCH_TOOL]


@server.call_tool()
async def handle_call_tool(
    name: str, arguments: dict[str, Any]
) -> list[TextContent]:
    """处理工具调用请求。"""
    if name == "web_search":
        return await _do_search(arguments)
    elif name == "web_fetch":
        return await _do_fetch(arguments)
    else:
        return [TextContent(type="text", text=f"Unknown tool: {name}")]


# ---------------------------------------------------------------------------
# 工具实现（异步包装器，复用 web_utils 共享逻辑）
# ---------------------------------------------------------------------------

async def _do_search(params: dict[str, Any]) -> list[TextContent]:
    """执行 web_search。"""
    query: str = str(params.get("query", "")).strip()
    count: int = min(int(params.get("count", 5)), _MAX_SEARCH_RESULTS)

    if not query:
        return [TextContent(type="text", text="Error: query is required")]

    try:
        from ddgs import DDGS
    except ImportError:
        return [TextContent(
            type="text",
            text="Error: ddgs not installed. Run: pip install ddgs",
        )]

    # 带重试的搜索（异步包装同步阻塞调用）
    last_exc: Exception | None = None
    for attempt in range(1, DEFAULT_MAX_RETRIES + 1):
        try:
            results = await asyncio.to_thread(
                lambda: list(DDGS().text(query, max_results=count))
            )
            break
        except Exception as exc:
            last_exc = exc
            logger.warning(
                "web_search attempt %d/%d failed for %r: %s",
                attempt, DEFAULT_MAX_RETRIES, query, exc,
            )
            if attempt < DEFAULT_MAX_RETRIES:
                await asyncio.sleep(DEFAULT_RETRY_DELAY * attempt)
    else:
        return [TextContent(
            type="text",
            text=(
                f"Search failed after {DEFAULT_MAX_RETRIES} attempts: {last_exc}"
            ),
        )]

    if not results:
        return [TextContent(
            type="text", text=f"No results found for: {query}"
        )]

    lines = [f"Web search results for: {query}\n"]
    for i, r in enumerate(results, start=1):
        title = r.get("title", "(no title)")
        href = r.get("href", "")
        body = r.get("body", "")
        snippet = body[:200] + ("..." if len(body) > 200 else "")
        lines.append(f"{i}. {title}")
        lines.append(f"   URL: {href}")
        lines.append(f"   {snippet}")
        lines.append("")

    return [TextContent(type="text", text="\n".join(lines))]


async def _do_fetch(params: dict[str, Any]) -> list[TextContent]:
    """执行 web_fetch。"""
    url: str = str(params.get("url", "")).strip()

    if not url:
        return [TextContent(type="text", text="Error: url is required")]

    # 安全校验（复用 web_utils 的同步函数，用 to_thread 避免阻塞）
    safe, err = await asyncio.to_thread(validate_url, url)
    if not safe:
        return [TextContent(type="text", text=f"Error: {err}")]

    try:
        import requests
    except ImportError:
        return [TextContent(
            type="text",
            text="Error: requests not installed. Run: pip install requests",
        )]

    # 带重试的 HTTP 请求（异步包装）
    last_exc: Exception | None = None
    resp = None
    for attempt in range(1, DEFAULT_MAX_RETRIES + 1):
        try:
            resp = await asyncio.to_thread(
                lambda: requests.get(
                    url,
                    headers={"User-Agent": USER_AGENT},
                    timeout=(5, _FETCH_TIMEOUT),
                    allow_redirects=True,
                    stream=True,
                )
            )
            if resp is None:
                continue

            if is_retryable_status(resp.status_code):
                logger.warning(
                    "web_fetch attempt %d/%d got HTTP %d for %s",
                    attempt, DEFAULT_MAX_RETRIES, resp.status_code, url,
                )
                resp.close()
                resp = None
                if attempt < DEFAULT_MAX_RETRIES:
                    await asyncio.sleep(DEFAULT_RETRY_DELAY * attempt)
                    continue
                return [TextContent(
                    type="text",
                    text=(
                        f"HTTP {resp.status_code} for {url} "
                        f"(after {DEFAULT_MAX_RETRIES} retries)"
                    ),
                )]
            break
        except requests.exceptions.Timeout:
            last_exc = requests.exceptions.Timeout(
                f"Request timed out after {_FETCH_TIMEOUT}s: {url}"
            )
            if attempt < DEFAULT_MAX_RETRIES:
                await asyncio.sleep(DEFAULT_RETRY_DELAY * attempt)
                continue
        except requests.exceptions.ConnectionError as exc:
            last_exc = exc
            if attempt < DEFAULT_MAX_RETRIES:
                await asyncio.sleep(DEFAULT_RETRY_DELAY * attempt)
                continue
        except Exception as exc:
            return [TextContent(
                type="text", text=f"Request failed: {exc}"
            )]
    else:
        err_msg = str(last_exc) if last_exc else "Unknown error"
        return [TextContent(
            type="text",
            text=f"Fetch failed after {DEFAULT_MAX_RETRIES} retries: {err_msg}",
        )]

    if resp is None:
        return [TextContent(type="text", text="Error: no response")]

    # 重定向安全校验
    safe, err = validate_redirect(resp)
    if not safe:
        resp.close()
        return [TextContent(type="text", text=f"Error: {err}")]

    if resp.status_code != 200:
        resp.close()
        return [TextContent(
            type="text",
            text=f"HTTP {resp.status_code} for {url}",
        )]

    # 读取 HTML（限制大小）
    raw_bytes = b""
    for chunk in resp.iter_content(chunk_size=8192):
        raw_bytes += chunk
        if len(raw_bytes) > _MAX_FETCH_CHARS * 4:
            break
    resp.close()

    html = raw_bytes.decode("utf-8", errors="replace")

    # 提取正文
    text = await asyncio.to_thread(extract_content, html, url)
    text = truncate_output(text, _MAX_FETCH_CHARS)

    if not text.strip():
        return [TextContent(
            type="text",
            text=f"(No readable content extracted from {url})",
        )]

    return [TextContent(type="text", text=text)]


# ---------------------------------------------------------------------------
# 入口
# ---------------------------------------------------------------------------

def main() -> None:
    """启动 MCP server（stdio transport）。"""
    logging.basicConfig(level=logging.WARNING)
    logger.info("Starting MCP web-search server...")
    asyncio.run(_run())


async def _run() -> None:
    """异步启动 stdio 传输层。"""
    async with stdio_server() as (read_stream, write_stream):
        await server.run(
            read_stream,
            write_stream,
            server.create_initialization_options(),
        )


if __name__ == "__main__":
    main()