"""
tools/web_utils.py

Web 工具共享函数库。

被 web_tool.py 和 MCP web_search server 共用，避免代码重复。

包含：
- IP 安全检查（内网拦截）
- URL 安全校验（scheme 白名单 + DNS 验证）
- 重定向安全校验
- 重试判断逻辑
- HTML 正文提取（readability-lxml + BeautifulSoup fallback）
"""

from __future__ import annotations

import ipaddress
import logging
import re
import socket
from urllib.parse import urlparse

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# 默认配置
# ---------------------------------------------------------------------------

DEFAULT_MAX_FETCH_CHARS = 100_000
DEFAULT_FETCH_TIMEOUT = 15
DEFAULT_SEARCH_MAX_RESULTS = 10
DEFAULT_MAX_RETRIES = 3
DEFAULT_RETRY_DELAY = 1.0

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

def is_private_ip(addr: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
    """检查 IP 地址是否属于内网/保留范围。"""
    for net in _PRIVATE_NETS:
        if addr in net:
            return True
    return False


def resolve_and_check(hostname: str) -> tuple[bool, str | None]:
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
            if is_private_ip(addr):
                return False, f"Blocked: {hostname} resolves to private IP {ip_str}"
        except ValueError:
            continue

    return True, None


# ---------------------------------------------------------------------------
# URL 安全校验
# ---------------------------------------------------------------------------

def validate_url(url: str) -> tuple[bool, str | None]:
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
        if is_private_ip(addr):
            return False, f"Blocked private IP: {hostname}"
        return True, None
    except ValueError:
        pass

    # 域名 — 做 DNS 解析验证
    safe, err = resolve_and_check(hostname)
    if not safe:
        return False, err

    return True, None


def validate_redirect(response) -> tuple[bool, str | None]:
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
            safe, err = validate_url(location)
            if not safe:
                return False, f"Blocked redirect: {err}"

    # 也检查最终 URL
    safe, err = validate_url(response.url)
    if not safe:
        return False, f"Blocked final URL after redirect: {err}"

    return True, None


# ---------------------------------------------------------------------------
# 重试机制
# ---------------------------------------------------------------------------

def is_retryable(exc: Exception) -> bool:
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


def is_retryable_status(status_code: int) -> bool:
    """判断 HTTP 状态码是否值得重试。"""
    return status_code in (429, 500, 502, 503, 504)


# ---------------------------------------------------------------------------
# 正文提取
# ---------------------------------------------------------------------------

def extract_content(html: str, url: str) -> str:
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