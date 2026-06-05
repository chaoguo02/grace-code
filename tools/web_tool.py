"""
tools/web_tool.py

联网工具：web_search 和 web_fetch。

web_search — 用 DuckDuckGo 搜索网页，返回标题+URL+摘要
web_fetch  — 抓取指定 URL，用 readability 提取正文

安全：
- URL scheme 白名单（仅 http/https）
- DNS 解析验证（域名解析后检查是否指向内网 IP）
- 重定向目标安全校验（禁止重定向到内网）
- 内网 IP 拦截（10.x / 172.16-31.x / 192.168.x / 127.x）
- 响应体大小限制
- 可配置超时 + 重试
"""

from __future__ import annotations

import ipaddress
import logging
import re
import socket
import time
from typing import Any
from urllib.parse import urlparse

from tools.base import BaseTool, ToolResult
from tools.utils import truncate_output

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# 默认配置（可被 yaml config 覆盖）
# ---------------------------------------------------------------------------

_DEFAULT_MAX_FETCH_CHARS = 100_000
_DEFAULT_FETCH_TIMEOUT = 15
_DEFAULT_SEARCH_MAX_RESULTS = 10
_DEFAULT_MAX_RETRIES = 3
_DEFAULT_RETRY_DELAY = 1.0

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
# IP 安全检查
# ---------------------------------------------------------------------------

def _is_private_ip(addr: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
    """检查 IP 地址是否属于内网/保留范围。"""
    for net in _PRIVATE_NETS:
        if addr in net:
            return True
    return False


def _resolve_and_check(hostname: str) -> tuple[bool, str | None]:
    """
    对域名做 DNS 解析，检查解析结果是否指向内网 IP。

    Returns:
        (is_safe, error_message)
    """
    try:
        infos = socket.getaddrinfo(hostname, None, socket.AF_UNSPEC, socket.SOCK_STREAM)
    except socket.gaierror:
        return True, None

    for info in infos:
        ip_str = info[4][0]
        try:
            addr = ipaddress.ip_address(ip_str)
            if _is_private_ip(addr):
                return False, f"Blocked: {hostname} resolves to private IP {ip_str}"
        except ValueError:
            continue

    return True, None


# ---------------------------------------------------------------------------
# URL 安全校验
# ---------------------------------------------------------------------------

def _validate_url(url: str) -> tuple[bool, str | None]:
    """
    校验 URL 是否安全（scheme + hostname + DNS 解析验证）。

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

    # localhost 显式拦截
    if hostname in ("localhost", "127.0.0.1", "::1"):
        return False, f"Blocked hostname: {hostname}"

    # 检查是否为 IP 字面量
    try:
        addr = ipaddress.ip_address(hostname)
        if _is_private_ip(addr):
            return False, f"Blocked private IP: {hostname}"
        return True, None
    except ValueError:
        pass

    # 域名 — 做 DNS 解析验证
    safe, err = _resolve_and_check(hostname)
    if not safe:
        return False, err

    return True, None


def _validate_redirect(response) -> tuple[bool, str | None]:
    """
    检查重定向链中是否有跳转到内网的情况。

    Args:
        response: requests.Response 对象（已完成重定向）
    """
    if not hasattr(response, "history"):
        return True, None

    for r in response.history:
        location = r.headers.get("Location", "")
        if location:
            safe, err = _validate_url(location)
            if not safe:
                return False, f"Blocked redirect: {err}"

    # 也检查最终 URL
    safe, err = _validate_url(response.url)
    if not safe:
        return False, f"Blocked final URL after redirect: {err}"

    return True, None


# ---------------------------------------------------------------------------
# 重试机制
# ---------------------------------------------------------------------------

def _is_retryable(exc: Exception) -> bool:
    """判断异常是否值得重试。"""
    import requests
    if isinstance(exc, requests.exceptions.Timeout):
        return True
    if isinstance(exc, requests.exceptions.ConnectionError):
        return True
    exc_str = str(exc).lower()
    if any(code in exc_str for code in ("503", "502", "429", "500")):
        return True
    return False


def _is_retryable_status(status_code: int) -> bool:
    """判断 HTTP 状态码是否值得重试。"""
    return status_code in (429, 500, 502, 503, 504)


# ---------------------------------------------------------------------------
# WebSearchTool
# ---------------------------------------------------------------------------

class WebSearchTool(BaseTool):
    """
    用 DuckDuckGo 搜索网页。

    params:
        query (str):  搜索关键词
        count  (int): 返回结果数（默认 5，最大由配置决定）
    """

    def __init__(self, max_results: int = _DEFAULT_SEARCH_MAX_RESULTS) -> None:
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
        for attempt in range(1, _DEFAULT_MAX_RETRIES + 1):
            try:
                results = list(DDGS().text(query, max_results=count))
                break
            except Exception as exc:
                last_exc = exc
                logger.warning(
                    "web_search attempt %d/%d failed for %r: %s",
                    attempt, _DEFAULT_MAX_RETRIES, query, exc,
                )
                if attempt < _DEFAULT_MAX_RETRIES:
                    time.sleep(_DEFAULT_RETRY_DELAY * attempt)
        else:
            return ToolResult(
                success=False, output="",
                error=f"Search failed after {_DEFAULT_MAX_RETRIES} attempts: {last_exc}",
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

    def __init__(
        self,
        max_chars: int = _DEFAULT_MAX_FETCH_CHARS,
        timeout: int = _DEFAULT_FETCH_TIMEOUT,
        max_retries: int = _DEFAULT_MAX_RETRIES,
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
        safe, err = _validate_url(url)
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
                if _is_retryable_status(resp.status_code):
                    logger.warning(
                        "web_fetch attempt %d/%d got HTTP %d for %s",
                        attempt, self._max_retries, resp.status_code, url,
                    )
                    resp.close()
                    if attempt < self._max_retries:
                        time.sleep(_DEFAULT_RETRY_DELAY * attempt)
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
                    time.sleep(_DEFAULT_RETRY_DELAY * attempt)
                    continue
            except requests.exceptions.ConnectionError as exc:
                last_exc = exc
                if attempt < self._max_retries:
                    time.sleep(_DEFAULT_RETRY_DELAY * attempt)
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
        safe, err = _validate_redirect(resp)
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
        text = _extract_content(html, url)
        text = truncate_output(text, self._max_chars)

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
    """
    # 优先用 readability
    try:
        from readability import Document
        doc = Document(html)
        title = doc.title() or ""
        summary = doc.summary()
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

    # Fallback：BeautifulSoup
    try:
        from bs4 import BeautifulSoup
        soup = BeautifulSoup(html, "html.parser")
        for tag in soup(["script", "style", "nav", "footer", "header"]):
            tag.decompose()
        title_tag = soup.find("title")
        title = title_tag.get_text().strip() if title_tag else ""
        body = soup.get_text(separator="\n")
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
