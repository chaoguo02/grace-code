"""
tests/test_web_tool.py

测试联网工具：web_search 和 web_fetch。
全部使用 mock，不产生真实网络请求。
"""

from __future__ import annotations

import sys
import types
from unittest.mock import MagicMock, patch

import pytest

from tools.web_tool import (
    WebSearchTool, WebFetchTool,
    _validate_url, _extract_content, _strip_tags,
)
from tools.utils import truncate_output


# ---------------------------------------------------------------------------
# URL 安全校验
# ---------------------------------------------------------------------------

class TestValidateURL:

    def test_https_ok(self):
        safe, err = _validate_url("https://docs.python.org/3/library/re.html")
        assert safe
        assert err is None

    def test_http_ok(self):
        safe, err = _validate_url("http://example.com/page")
        assert safe
        assert err is None

    def test_file_scheme_blocked(self):
        safe, err = _validate_url("file:///etc/passwd")
        assert not safe
        assert "file" in err.lower()

    def test_localhost_blocked(self):
        safe, err = _validate_url("http://localhost:8080/admin")
        assert not safe
        assert "localhost" in err.lower()

    def test_loopback_ip_blocked(self):
        safe, err = _validate_url("http://127.0.0.1/api")
        assert not safe

    def test_private_10_blocked(self):
        safe, err = _validate_url("http://10.0.0.5/internal")
        assert not safe

    def test_private_192_blocked(self):
        safe, err = _validate_url("http://192.168.1.1/router")
        assert not safe

    def test_private_172_blocked(self):
        safe, err = _validate_url("http://172.16.0.1/admin")
        assert not safe

    def test_ipv6_loopback_blocked(self):
        safe, err = _validate_url("http://[::1]:8080/")
        assert not safe

    def test_link_local_blocked(self):
        safe, err = _validate_url("http://169.254.0.1/")
        assert not safe

    def test_ftp_scheme_blocked(self):
        safe, err = _validate_url("ftp://example.com/file")
        assert not safe

    def test_public_ip_ok(self):
        safe, err = _validate_url("https://8.8.8.8/")
        assert safe


# ---------------------------------------------------------------------------
# 截断
# ---------------------------------------------------------------------------

class TestTruncate:
    def test_short_unchanged(self):
        assert truncate_output("hello", 100) == "hello"

    def test_long_truncated(self):
        text = "x" * 10_000
        result = truncate_output(text, 1_000)
        assert len(result) < len(text)
        assert "truncated" in result


# ---------------------------------------------------------------------------
# HTML 标签去除
# ---------------------------------------------------------------------------

class TestStripTags:
    def test_removes_html_tags(self):
        assert _strip_tags("<p>Hello <b>World</b></p>") == "Hello World"

    def test_removes_scripts(self):
        html = "<html><script>alert(1)</script><p>content</p></html>"
        assert "alert" not in _strip_tags(html)
        assert "content" in _strip_tags(html)


# ---------------------------------------------------------------------------
# 正文提取
# ---------------------------------------------------------------------------

class TestExtractContent:
    def test_readability_preferred(self):
        html = "<html><head><title>Test</title></head><body><p>Hello world</p></body></html>"
        text = _extract_content(html, "http://example.com")
        assert "Test" in text or "Hello" in text

    def test_fallback_no_bs4(self):
        html = "<html><title>Fallback</title><body>Content here</body></html>"
        text = _strip_tags(html)
        assert "Fallback" in text or "Content here" in text


# ---------------------------------------------------------------------------
# WebSearchTool — fake ddgs 模块
# ---------------------------------------------------------------------------

SAMPLE_SEARCH_RESULTS = [
    {"title": "Python Official Docs", "href": "https://docs.python.org/3/", "body": "The official Python documentation."},
    {"title": "Stack Overflow", "href": "https://stackoverflow.com/q/12345", "body": "Community Q&A for programmers."},
    {"title": "Real Python Tutorial", "href": "https://realpython.com/tutorial", "body": "In-depth Python tutorials and guides."},
]


def _inject_fake_ddgs(text_results=None, side_effect=None):
    """向 sys.modules 注入一个 fake ddgs 模块。"""
    fake_mod = MagicMock()

    if side_effect:
        fake_mod.DDGS.side_effect = side_effect
    elif text_results is not None:
        ctx = MagicMock()
        ctx.text.return_value = text_results
        fake_mod.DDGS.return_value = ctx

    mod = types.ModuleType("ddgs")
    mod.DDGS = fake_mod.DDGS
    sys.modules["ddgs"] = mod
    return mod


class TestWebSearchTool:
    tool = WebSearchTool()

    def test_search_returns_results(self):
        _inject_fake_ddgs(text_results=SAMPLE_SEARCH_RESULTS)
        result = self.tool.execute({"query": "python docs"})
        assert result.success
        assert "Python Official Docs" in result.output
        assert "docs.python.org" in result.output

    def test_search_no_results(self):
        _inject_fake_ddgs(text_results=[])
        result = self.tool.execute({"query": "zzznoresultsatall"})
        assert result.success
        assert "No results found" in result.output

    def test_search_respects_count(self):
        _inject_fake_ddgs(text_results=SAMPLE_SEARCH_RESULTS[:2])
        result = self.tool.execute({"query": "test", "count": 2})
        assert result.success

    def test_search_requires_query(self):
        result = self.tool.execute({"query": ""})
        assert not result.success
        assert "required" in result.error.lower()

    def test_search_package_missing(self):
        """清除 fake 模块后，import 失败 → 优雅报错。"""
        sys.modules.pop("ddgs", None)
        with patch("builtins.__import__", side_effect=ImportError("no ddgs")):
            result = self.tool.execute({"query": "test"})
            assert not result.success

    def test_search_exception_graceful(self):
        _inject_fake_ddgs(side_effect=RuntimeError("API error"))
        result = self.tool.execute({"query": "test"})
        assert not result.success
        assert "Search failed" in result.error

    def test_schema_requires_query(self):
        schema = self.tool.to_llm_schema()
        assert schema.name == "web_search"
        assert "query" in schema.parameters["required"]


# ---------------------------------------------------------------------------
# WebFetchTool — fake requests inside execute()
# ---------------------------------------------------------------------------

SIMPLE_HTML = """\
<html>
<head><title>Example Page</title></head>
<body>
  <article>
    <h1>Hello World</h1>
    <p>This is the main content of the page.</p>
    <p>It has multiple paragraphs.</p>
  </article>
</body>
</html>
"""


def _make_fake_response(status_code=200, html=SIMPLE_HTML, side_effect=None):
    """造一个 fake requests.get 返回值。"""
    if side_effect:
        def _fake_get(url, headers=None, timeout=None, allow_redirects=True, stream=True):
            raise side_effect
        return _fake_get

    def _fake_get(url, headers=None, timeout=None, allow_redirects=True, stream=True):
        resp = MagicMock()
        resp.status_code = status_code
        resp.iter_content.return_value = [html.encode("utf-8")] if html else [b""]
        return resp
    return _fake_get


class TestWebFetchTool:
    tool = WebFetchTool()

    def test_fetch_extracts_content(self):
        with patch("requests.get", side_effect=_make_fake_response(200)):
            result = self.tool.execute({"url": "https://example.com/article"})
            assert result.success
            assert "Example Page" in result.output or "Hello World" in result.output

    def test_fetch_blocked_url(self):
        result = self.tool.execute({"url": "file:///etc/passwd"})
        assert not result.success
        assert "file" in result.error.lower()

    def test_fetch_private_ip_blocked(self):
        result = self.tool.execute({"url": "http://192.168.1.1/admin"})
        assert not result.success

    def test_fetch_requires_url(self):
        result = self.tool.execute({"url": ""})
        assert not result.success
        assert "required" in result.error.lower()

    def test_fetch_404(self):
        with patch("requests.get", side_effect=_make_fake_response(404)):
            result = self.tool.execute({"url": "https://example.com/notfound"})
            assert not result.success
            assert "404" in result.error

    def test_fetch_timeout(self):
        import requests as req_mod
        with patch("requests.get", side_effect=_make_fake_response(
            side_effect=req_mod.exceptions.Timeout("timed out")
        )):
            result = self.tool.execute({"url": "https://example.com"})
            assert not result.success
            assert "timed out" in result.error.lower()

    def test_fetch_connection_error(self):
        import requests as req_mod
        with patch("requests.get", side_effect=_make_fake_response(
            side_effect=req_mod.exceptions.ConnectionError("refused")
        )):
            result = self.tool.execute({"url": "https://example.com"})
            assert not result.success
            assert "connection" in result.error.lower()

    def test_schema_requires_url(self):
        schema = self.tool.to_llm_schema()
        assert schema.name == "web_fetch"
        assert "url" in schema.parameters["required"]
