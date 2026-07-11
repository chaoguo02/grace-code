"""
tools/web_tool.py

联网工具：web_search 和 web_fetch。

web_search — 用 DuckDuckGo 搜索网页，返回标题+URL+摘要
web_fetch  — 抓取指定 URL，用 readability 提取正文

安全：
- 所有校验逻辑已提取到 tools/web_utils.py
- URL scheme 白名单（仅 http/https）
- DNS 解析验证（域名解析后检查是否指向内网 IP）
- 重定向目标安全校验（禁止重定向到内网）
- 内网 IP 拦截（10.x / 172.16-31.x / 192.168.x / 127.x）
- 响应体大小限制
- 可配置超时 + 重试
"""

from __future__ import annotations

import logging
import time
from typing import Any

from tools.base import BaseTool, ToolResult
from tools.utils import truncate_output
from tools.web_utils import (
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

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# WebSearchTool
# ---------------------------------------------------------------------------

class WebSearchTool(BaseTool):
    is_read_only = True
    """
    用 DuckDuckGo 搜索网页。

    params:
        query (str):  搜索关键词
        count  (int): 返回结果数（默认 5，最大由配置决定）
    """

    def __init__(self, max_results: int = DEFAULT_SEARCH_MAX_RESULTS) -> None:
        self._max_results = max_results

    @property
    def name(self) -> str:
        return "web_search"

    @property
    def description(self) -> str:
        return (
            "Search the web using DuckDuckGo. Returns a list of results "
            "with title, URL, and snippet. Use this to find up-to-date "
            "information, documentation, or solutions that are not in "
            "the local codebase. Prefer this over file_search for external "
            "knowledge (API docs, library versions, error messages, etc.)."
        )

    @property
    def parameters_schema(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "Search query string",
                },
                "count": {
                    "type": "integer",
                    "description": f"Number of results (default 5, max {self._max_results})",
                },
            },
            "required": ["query"],
        }

    def execute(self, params: dict[str, Any]) -> ToolResult:
        query: str = params.get("query", "").strip()
        count: int = min(int(params.get("count", 5)), self._max_results)

        if not query:
            return ToolResult(success=False, output="", error="query is required")

        try:
            from ddgs import DDGS
        except ImportError:
            return ToolResult(
                success=False, output="",
                error="ddgs not installed. Run: pip install ddgs",
            )

        # 带重试的搜索
        last_exc: Exception | None = None
        for attempt in range(1, DEFAULT_MAX_RETRIES + 1):
            try:
                results = list(DDGS().text(query, max_results=count))
                break
            except Exception as exc:
                last_exc = exc
                logger.warning(
                    "web_search attempt %d/%d failed for %r: %s",
                    attempt, DEFAULT_MAX_RETRIES, query, exc,
                )
                if attempt < DEFAULT_MAX_RETRIES:
                    time.sleep(DEFAULT_RETRY_DELAY * attempt)
        else:
            return ToolResult(
                success=False, output="",
                error=f"Search failed after {DEFAULT_MAX_RETRIES} attempts: {last_exc}",
            )

        if not results:
            return ToolResult(
                success=True,
                output=f"No results found for: {query}",
            )

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

        return ToolResult(success=True, output="\n".join(lines))


# ---------------------------------------------------------------------------
# WebFetchTool
# ---------------------------------------------------------------------------

class WebFetchTool(BaseTool):
    is_read_only = True
    """
    抓取指定 URL 并提取正文。

    params:
        url (str): 要抓取的网页 URL
    """

    def __init__(
        self,
        max_chars: int = DEFAULT_MAX_FETCH_CHARS,
        timeout: int = DEFAULT_FETCH_TIMEOUT,
        max_retries: int = DEFAULT_MAX_RETRIES,
    ) -> None:
        self._max_chars = max_chars
        self._timeout = timeout
        self._max_retries = max_retries

    @property
    def name(self) -> str:
        return "web_fetch"

    @property
    def description(self) -> str:
        return (
            "Fetch the content of a web page and extract the main text "
            "(using Mozilla's readability algorithm). Use this after "
            "web_search to read a specific page in detail, e.g. official "
            "documentation, blog posts, or API references. "
            f"Timeout is {self._timeout}s, max output is ~{self._max_chars // 1000}KB."
        )

    @property
    def parameters_schema(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "url": {
                    "type": "string",
                    "description": "The URL to fetch (must be http or https)",
                },
            },
            "required": ["url"],
        }

    def execute(self, params: dict[str, Any]) -> ToolResult:
        url: str = params.get("url", "").strip()

        if not url:
            return ToolResult(success=False, output="", error="url is required")

        # 安全校验
        safe, err = validate_url(url)
        if not safe:
            return ToolResult(success=False, output="", error=err)

        try:
            import requests
        except ImportError:
            return ToolResult(
                success=False, output="",
                error="requests not installed. Run: pip install requests",
            )

        # 带重试的请求
        last_exc: Exception | None = None
        resp = None
        for attempt in range(1, self._max_retries + 1):
            try:
                resp = requests.get(
                    url,
                    headers={"User-Agent": USER_AGENT},
                    timeout=(5, self._timeout),  # (connect_timeout, read_timeout)
                    allow_redirects=True,
                    stream=True,
                )
                # 可重试的状态码
                if is_retryable_status(resp.status_code):
                    logger.warning(
                        "web_fetch attempt %d/%d got HTTP %d for %s",
                        attempt, self._max_retries, resp.status_code, url,
                    )
                    resp.close()
                    if attempt < self._max_retries:
                        time.sleep(DEFAULT_RETRY_DELAY * attempt)
                        continue
                    return ToolResult(
                        success=False, output="",
                        error=f"HTTP {resp.status_code} for {url} (after {self._max_retries} retries)",
                    )
                break
            except requests.exceptions.Timeout:
                last_exc = requests.exceptions.Timeout(
                    f"Request timed out after {self._timeout}s: {url}"
                )
                if attempt < self._max_retries:
                    time.sleep(DEFAULT_RETRY_DELAY * attempt)
                    continue
            except requests.exceptions.ConnectionError as exc:
                last_exc = exc
                if attempt < self._max_retries:
                    time.sleep(DEFAULT_RETRY_DELAY * attempt)
                    continue
            except Exception as exc:
                return ToolResult(
                    success=False, output="",
                    error=f"Request failed: {exc}",
                )
        else:
            err_msg = str(last_exc) if last_exc else "Unknown error"
            return ToolResult(
                success=False, output="",
                error=f"Fetch failed after {self._max_retries} retries: {err_msg}",
            )

        # 重定向安全校验
        safe, err = validate_redirect(resp)
        if not safe:
            resp.close()
            return ToolResult(success=False, output="", error=err)

        if resp.status_code != 200:
            resp.close()
            return ToolResult(
                success=False, output="",
                error=f"HTTP {resp.status_code} for {url}",
            )

        # 读原始 HTML（限制大小）
        raw_bytes = b""
        for chunk in resp.iter_content(chunk_size=8192):
            raw_bytes += chunk
            if len(raw_bytes) > self._max_chars * 4:
                break
        resp.close()

        html = raw_bytes.decode("utf-8", errors="replace")

        # 提取正文
        text = extract_content(html, url)
        text = truncate_output(text, self._max_chars)

        if not text.strip():
            return ToolResult(
                success=True,
                output=f"(No readable content extracted from {url})",
            )

        return ToolResult(success=True, output=text)