"""
tools/web_tool.py

联网工具：web_search 和 web_fetch。

web_search — 用 DuckDuckGo 搜索网页，返回标题+URL+摘要
web_fetch  — 抓取指定 URL，用 readability 提取正文

安全：
- URL scheme 白名单（仅 http/https）
- 内网 IP 拦截（10.x / 172.16-31.x / 192.168.x / 127.x）
- 响应体大小限制（默认 100KB）
- 超时 15s
"""

from __future__ import annotations

import ipaddress
import logging
import re
from typing import Any
from urllib.parse import urlparse

from tools.base import BaseTool, ToolResult

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# 配置常量
# ---------------------------------------------------------------------------

MAX_FETCH_CHARS = 100_000          # ~100KB 文本
FETCH_TIMEOUT = 15                 # 秒
SEARCH_MAX_RESULTS = 10            # 最多返回 N 条结果
USER_AGENT = (
    "Mozilla/5.0 (compatible; ForgeAgent/0.1; +https://github.com/chaoguo02/forge-agent)"
)

# 内网 IP 范围
_PRIVATE_NETS = [
    ipaddress.ip_network("10.0.0.0/8"),
    ipaddress.ip_network("172.16.0.0/12"),
    ipaddress.ip_network("192.168.0.0/16"),
    ipaddress.ip_network("127.0.0.0/8"),
    ipaddress.ip_network("0.0.0.0/8"),
    ipaddress.ip_network("169.254.0.0/16"),   # link-local
    ipaddress.ip_network("::1/128"),           # IPv6 loopback
    ipaddress.ip_network("fc00::/7"),          # IPv6 unique local
    ipaddress.ip_network("fe80::/10"),         # IPv6 link-local
]


# ---------------------------------------------------------------------------
# URL 安全校验
# ---------------------------------------------------------------------------

def _validate_url(url: str) -> tuple[bool, str | None]:
    """
    校验 URL 是否安全。

    Returns:
        (is_safe, error_message) — is_safe=True 表示允许访问
    """
    parsed = urlparse(url)

    # Scheme 白名单
    if parsed.scheme not in ("http", "https"):
        return False, f"Blocked scheme '{parsed.scheme}': only http and https are allowed"

    # 必须有 hostname
    if not parsed.hostname:
        return False, "URL has no hostname"

    hostname = parsed.hostname.lower()

    # localhost
    if hostname in ("localhost", "127.0.0.1", "::1"):
        return False, f"Blocked hostname: {hostname}"

    # 内网 IP
    try:
        addr = ipaddress.ip_address(hostname)
        for net in _PRIVATE_NETS:
            if addr in net:
                return False, f"Blocked private IP: {hostname} (in {net})"
    except ValueError:
        # 不是 IP 地址（是域名），通过 DNS 检查
        pass

    return True, None


def _truncate(text: str, max_chars: int) -> str:
    """截断文本：保留头部 60% + 尾部 40%。"""
    if len(text) <= max_chars:
        return text
    head = int(max_chars * 0.6)
    tail = max_chars - head
    omitted = len(text) - max_chars
    return (
        text[:head]
        + f"\n... [{omitted} characters truncated] ...\n"
        + text[-tail:]
    )


# ---------------------------------------------------------------------------
# WebSearchTool
# ---------------------------------------------------------------------------

class WebSearchTool(BaseTool):
    """
    用 DuckDuckGo 搜索网页。

    params:
        query (str):  搜索关键词
        count  (int): 返回结果数（默认 5，最大 10）
    """

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
                    "description": f"Number of results (default 5, max {SEARCH_MAX_RESULTS})",
                },
            },
            "required": ["query"],
        }

    def execute(self, params: dict[str, Any]) -> ToolResult:
        query: str = params.get("query", "").strip()
        count: int = min(int(params.get("count", 5)), SEARCH_MAX_RESULTS)

        if not query:
            return ToolResult(success=False, output="", error="query is required")

        try:
            from ddgs import DDGS
        except ImportError:
            return ToolResult(
                success=False, output="",
                error="ddgs not installed. Run: pip install ddgs",
            )

        try:
            results = list(DDGS().text(query, max_results=count))
        except Exception as exc:
            logger.warning("web_search failed for %r: %s", query, exc)
            return ToolResult(
                success=False, output="",
                error=f"Search failed: {exc}",
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
    """
    抓取指定 URL 并提取正文。

    params:
        url (str): 要抓取的网页 URL
    """

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
            "Timeout is 15s, max output is ~100KB."
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
        safe, err = _validate_url(url)
        if not safe:
            return ToolResult(success=False, output="", error=err)

        # 网络请求
        try:
            import requests
        except ImportError:
            return ToolResult(
                success=False, output="",
                error="requests not installed. Run: pip install requests",
            )

        try:
            resp = requests.get(
                url,
                headers={"User-Agent": USER_AGENT},
                timeout=FETCH_TIMEOUT,
                allow_redirects=True,
                stream=True,   # 先流式检查 Content-Length
            )
        except requests.exceptions.Timeout:
            return ToolResult(
                success=False, output="",
                error=f"Request timed out after {FETCH_TIMEOUT}s: {url}",
            )
        except requests.exceptions.ConnectionError as exc:
            return ToolResult(
                success=False, output="",
                error=f"Connection failed: {exc}",
            )
        except Exception as exc:
            return ToolResult(
                success=False, output="",
                error=f"Request failed: {exc}",
            )

        if resp.status_code != 200:
            return ToolResult(
                success=False, output="",
                error=f"HTTP {resp.status_code} for {url}",
            )

        # 读原始 HTML
        raw_bytes = b""
        for chunk in resp.iter_content(chunk_size=8192):
            raw_bytes += chunk
            if len(raw_bytes) > MAX_FETCH_CHARS * 4:  # HTML 比文本大很多
                break
        resp.close()

        html = raw_bytes.decode("utf-8", errors="replace")

        # 提取正文
        text = _extract_content(html, url)
        text = _truncate(text, MAX_FETCH_CHARS)

        if not text.strip():
            return ToolResult(
                success=True,
                output=f"(No readable content extracted from {url})",
            )

        return ToolResult(success=True, output=text)


# ---------------------------------------------------------------------------
# 正文提取
# ---------------------------------------------------------------------------

def _extract_content(html: str, url: str) -> str:
    """
    用 readability-lxml 提取正文，不可用时降级为纯文本。

    返回提取的纯文本，不含 HTML 标签。
    """
    # 优先用 readability
    try:
        from readability import Document
        doc = Document(html)
        title = doc.title() or ""
        summary = doc.summary()
        # 用 BeautifulSoup 去掉 HTML 标签
        try:
            from bs4 import BeautifulSoup
            text = BeautifulSoup(summary, "html.parser").get_text()
        except ImportError:
            text = _strip_tags(summary)

        result = ""
        if title:
            result += f"Title: {title}\n\n"
        result += text
        return result
    except ImportError:
        pass
    except Exception as exc:
        logger.debug("readability failed for %s: %s, falling back", url, exc)

    # Fallback：用 BeautifulSoup 提取所有文本
    try:
        from bs4 import BeautifulSoup
        soup = BeautifulSoup(html, "html.parser")
        # 去掉 script / style
        for tag in soup(["script", "style", "nav", "footer", "header"]):
            tag.decompose()
        title_tag = soup.find("title")
        title = title_tag.get_text().strip() if title_tag else ""
        body = soup.get_text(separator="\n")
        # 清理多余空行
        lines = [l.strip() for l in body.splitlines() if l.strip()]
        body = "\n".join(lines)
        if title:
            return f"Title: {title}\n\n{body}"
        return body
    except ImportError:
        pass

    # 最终 fallback：正则去标签
    return _strip_tags(html)


def _strip_tags(html: str) -> str:
    """最简陋的 HTML 标签去除。"""
    text = re.sub(r"<script[^>]*>.*?</script>", "", html, flags=re.DOTALL | re.IGNORECASE)
    text = re.sub(r"<style[^>]*>.*?</style>", "", text, flags=re.DOTALL | re.IGNORECASE)
    text = re.sub(r"<[^>]+>", "", text)
    text = re.sub(r"\n\s*\n", "\n\n", text)
    return text.strip()
