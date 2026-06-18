"""
tests/test_mcp_advanced.py

MCP 高级功能测试 — resources、prompts、虚拟工具。
"""

import pytest
from unittest.mock import MagicMock, AsyncMock, patch
from dataclasses import dataclass
from typing import Any

from tools.mcp_client import HAS_MCP

skip_no_mcp = pytest.mark.skipif(not HAS_MCP, reason="mcp package not installed")


# --- Mock types for testing without MCP server ---

@dataclass
class MockResource:
    uri: str
    name: str
    description: str = ""


@dataclass
class MockPrompt:
    name: str
    description: str = ""
    arguments: list = None


@dataclass
class MockResourceTemplate:
    uriTemplate: str
    name: str
    description: str = ""


@dataclass
class MockTextContent:
    text: str


@dataclass
class MockResourceContent:
    uri: str
    text: str


@skip_no_mcp
class TestMCPResourceListTool:
    """MCPResourceListTool 单元测试。"""

    def test_list_resources_empty(self):
        from tools.mcp_client import MCPResourceListTool, MCPClientManager
        mgr = MCPClientManager()
        mgr._resources = {"test-server": []}
        mgr._resource_templates = {"test-server": []}
        tool = MCPResourceListTool("test-server", mgr)

        result = tool.execute({})
        assert result.success
        assert "No resources" in result.output

    def test_list_resources_with_items(self):
        from tools.mcp_client import MCPResourceListTool, MCPClientManager
        mgr = MCPClientManager()
        mgr._resources = {
            "test-server": [
                MockResource(uri="file:///tmp/test.md", name="test.md", description="A test file"),
                MockResource(uri="db://users/schema", name="users schema"),
            ]
        }
        mgr._resource_templates = {"test-server": []}
        tool = MCPResourceListTool("test-server", mgr)

        result = tool.execute({})
        assert result.success
        assert "file:///tmp/test.md" in result.output

    def test_list_resources_with_templates(self):
        from tools.mcp_client import MCPResourceListTool, MCPClientManager
        mgr = MCPClientManager()
        mgr._resources = {"test-server": []}
        mgr._resource_templates = {
            "test-server": [
                MockResourceTemplate(
                    uriTemplate="db://users/{user_id}/profile",
                    name="user-profile",
                    description="Get user profile",
                ),
            ]
        }
        tool = MCPResourceListTool("test-server", mgr)

        result = tool.execute({})
        assert result.success
        assert "db://users/{user_id}/profile" in result.output
        assert "user_id" in result.output
        assert "Templates" in result.output

    def test_tool_name_format(self):
        from tools.mcp_client import MCPResourceListTool, MCPClientManager
        mgr = MCPClientManager()
        tool = MCPResourceListTool("my-server", mgr)
        assert tool.name == "mcp__my-server__list_resources"


@skip_no_mcp
class TestMCPResourceReadTool:
    """MCPResourceReadTool 单元测试。"""

    def test_read_requires_uri_or_template(self):
        from tools.mcp_client import MCPResourceReadTool, MCPClientManager
        mgr = MCPClientManager()
        session = MagicMock()
        tool = MCPResourceReadTool("test-server", session, mgr)

        result = tool.execute({})
        assert not result.success
        assert "uri" in result.error.lower() or "template" in result.error.lower()

    def test_read_resource_success(self):
        from tools.mcp_client import MCPResourceReadTool, MCPClientManager
        mgr = MCPClientManager()
        session = MagicMock()
        tool = MCPResourceReadTool("test-server", session, mgr)

        with patch("tools.mcp_client._run_async", return_value="hello world"):
            result = tool.execute({"uri": "file:///test"})
            assert result.success
            assert result.output == "hello world"

    def test_read_resource_with_template(self):
        from tools.mcp_client import MCPResourceReadTool, MCPClientManager
        mgr = MCPClientManager()
        session = MagicMock()
        tool = MCPResourceReadTool("test-server", session, mgr)

        with patch("tools.mcp_client._run_async", return_value="user data") as mock_run:
            result = tool.execute({
                "template": "db://users/{user_id}/profile",
                "variables": {"user_id": "42"},
            })
            assert result.success
            assert result.output == "user data"

    def test_read_template_missing_variables(self):
        from tools.mcp_client import MCPResourceReadTool, MCPClientManager
        mgr = MCPClientManager()
        session = MagicMock()
        tool = MCPResourceReadTool("test-server", session, mgr)

        result = tool.execute({
            "template": "db://users/{user_id}/posts/{post_id}",
            "variables": {"user_id": "42"},
        })
        assert not result.success
        assert "post_id" in result.error

    def test_tool_name_format(self):
        from tools.mcp_client import MCPResourceReadTool, MCPClientManager
        mgr = MCPClientManager()
        session = MagicMock()
        tool = MCPResourceReadTool("brave-search", session, mgr)
        assert tool.name == "mcp__brave-search__read_resource"


@skip_no_mcp
class TestMCPClientManagerResources:
    """MCPClientManager resources/prompts 属性测试。"""

    def test_initial_state(self):
        from tools.mcp_client import MCPClientManager
        mgr = MCPClientManager()
        assert mgr.resources == {}
        assert mgr.resource_templates == {}
        assert mgr.prompts == {}

    def test_resources_stored(self):
        from tools.mcp_client import MCPClientManager
        mgr = MCPClientManager()
        mgr._resources["server-a"] = [
            MockResource(uri="file:///a", name="a"),
        ]
        assert len(mgr.resources["server-a"]) == 1

    def test_resource_templates_stored(self):
        from tools.mcp_client import MCPClientManager
        mgr = MCPClientManager()
        mgr._resource_templates["server-a"] = [
            MockResourceTemplate(uriTemplate="db://x/{id}", name="item"),
        ]
        assert len(mgr.resource_templates["server-a"]) == 1
        assert mgr.resource_templates["server-a"][0].uriTemplate == "db://x/{id}"

    def test_prompts_stored(self):
        from tools.mcp_client import MCPClientManager
        mgr = MCPClientManager()
        mgr._prompts["server-b"] = [
            MockPrompt(name="summarize", description="Summarize content"),
        ]
        assert len(mgr.prompts["server-b"]) == 1
        assert mgr.prompts["server-b"][0].name == "summarize"


@skip_no_mcp
class TestMCPGetPrompt:
    """MCPClientManager.get_prompt 测试。"""

    def test_get_prompt_no_session(self):
        from tools.mcp_client import MCPClientManager
        mgr = MCPClientManager()
        result = mgr.get_prompt("nonexistent", "test")
        assert result is None

    def test_get_prompt_with_session(self):
        from tools.mcp_client import MCPClientManager
        mgr = MCPClientManager()

        mock_session = MagicMock()
        mgr._session_map["test-server"] = mock_session

        mock_msg = MagicMock()
        mock_msg.content = "Hello, world!"
        mock_resp = MagicMock()
        mock_resp.messages = [mock_msg]

        with patch("tools.mcp_client._run_async", return_value="Hello, world!"):
            result = mgr.get_prompt("test-server", "greet", {"name": "user"})
            assert result == "Hello, world!"


# --- 不依赖 mcp 包的基础测试 ---


class TestMCPClientManagerBasic:
    """MCPClientManager 基础结构测试（无 mcp 包依赖）。"""

    def test_import_without_mcp(self):
        """即使 mcp 包不存在，tools/mcp_client.py 也能被导入。"""
        import importlib
        import tools.mcp_client
        importlib.reload(tools.mcp_client)
        assert hasattr(tools.mcp_client, "MCPClientManager")
        assert hasattr(tools.mcp_client, "MCPResourceListTool")
        assert hasattr(tools.mcp_client, "MCPResourceReadTool")

    def test_manager_initial_properties(self):
        from tools.mcp_client import MCPClientManager
        mgr = MCPClientManager()
        assert mgr.proxies == []
        assert mgr.resources == {}
        assert mgr.resource_templates == {}
        assert mgr.prompts == {}
        assert len(mgr) == 0

    def test_server_config_creation(self):
        from tools.mcp_client import MCPServerConfig
        cfg = MCPServerConfig(
            name="test",
            command="python",
            args=["-m", "server"],
            env={"KEY": "val"},
            cwd="/tmp",
        )
        assert cfg.name == "test"
        assert cfg.command == "python"


class TestURITemplateHelpers:
    """URI 模板工具函数测试（无 mcp 包依赖）。"""

    def test_expand_simple(self):
        from tools.mcp_client import _expand_uri_template
        result = _expand_uri_template("db://users/{user_id}/profile", {"user_id": "42"})
        assert result == "db://users/42/profile"

    def test_expand_multiple_vars(self):
        from tools.mcp_client import _expand_uri_template
        result = _expand_uri_template(
            "api://{org}/{repo}/issues/{id}",
            {"org": "acme", "repo": "app", "id": "123"},
        )
        assert result == "api://acme/app/issues/123"

    def test_expand_missing_var_preserved(self):
        from tools.mcp_client import _expand_uri_template
        result = _expand_uri_template("db://users/{user_id}/{action}", {"user_id": "42"})
        assert result == "db://users/42/{action}"

    def test_extract_variables(self):
        from tools.mcp_client import _extract_template_variables
        vars = _extract_template_variables("db://users/{user_id}/posts/{post_id}")
        assert vars == ["user_id", "post_id"]

    def test_extract_no_variables(self):
        from tools.mcp_client import _extract_template_variables
        vars = _extract_template_variables("file:///static/config.json")
        assert vars == []


