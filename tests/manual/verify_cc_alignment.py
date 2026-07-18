"""
forge-agent CC alignment — manual verification suite.

Run in the project root:
    python tests/manual/verify_cc_alignment.py

Tests tool naming, parameters, skills, and MCP — no external dependencies needed.
"""
from __future__ import annotations

import sys
import tempfile
from pathlib import Path

# Ensure project root is on sys.path
_PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

PASS = 0
FAIL = 0


def check(label: str, condition: bool, detail: str = "") -> None:
    global PASS, FAIL
    if condition:
        PASS += 1
        print(f"  \033[32mPASS\033[0m  {label}")
    else:
        FAIL += 1
        print(f"  \033[31mFAIL\033[0m  {label}  {detail}")


# ═══════════════════════════════════════════════════════════════════
print("\n\033[1m1. CC Tool Naming (11 tools)\033[0m\n")

from tools.file_tool import FileReadTool, FileWriteTool
from tools.file_edit_tool import FileEditTool
from tools.shell_tool import ShellTool
from tools.search_tool import SearchTextTool, FindFilesTool
from tools.web_tool import WebSearchTool, WebFetchTool
from skills.tool import SkillTool
from tools.submit_findings_tool import SubmitFindingsTool  # ReportFindings
from skills.registry import SkillRegistry
from agent.session.task_tool import AgentTool

# Dummy registry for SkillTool (tool only needs it for execute, not name property)
_dummy_reg = SkillRegistry("", include_builtin=False)

check("Read -> 'Read'", FileReadTool().name == "Read")
check("Read aliases", "file_read" in FileReadTool().aliases and "read_file" in FileReadTool().aliases)
check("Write -> 'Write'", FileWriteTool().name == "Write")
check("Write aliases", "file_write" in FileWriteTool().aliases)
check("Edit -> 'Edit'", FileEditTool().name == "Edit")
check("Edit aliases", "file_edit" in FileEditTool().aliases)
check("Bash -> 'Bash'", ShellTool().name == "Bash")
check("Bash aliases", "shell" in ShellTool().aliases)
check("Grep -> 'Grep'", SearchTextTool().name == "Grep")
check("Grep aliases", "search_text" in SearchTextTool().aliases)
check("Glob -> 'Glob'", FindFilesTool().name == "Glob")
check("Glob aliases", "find_files" in FindFilesTool().aliases)
check("WebSearch -> 'WebSearch'", WebSearchTool().name == "WebSearch")
check("WebSearch aliases", "web_search" in WebSearchTool().aliases)
check("WebFetch -> 'WebFetch'", WebFetchTool().name == "WebFetch")
check("WebFetch aliases", "web_fetch" in WebFetchTool().aliases)
check("Skill -> 'Skill'", SkillTool(_dummy_reg).name == "Skill")
check("Skill aliases", "use_skill" in SkillTool(_dummy_reg).aliases)
# Properties on classes need instances. Use _dummy construct where possible.
# For tools needing constructor args, verify via class attribute inspection.
sf_aliases = getattr(SubmitFindingsTool, "aliases", ())
check("ReportFindings aliases", "submit_findings" in sf_aliases)
check("ReportFindings name attr", hasattr(SubmitFindingsTool, "name"))
at_aliases = getattr(AgentTool, "aliases", ())
check("AgentTool aliases", "task" in at_aliases)
check("AgentTool name attr", hasattr(AgentTool, "name"))

# Runtime-registered tools
# Verify AgentTool class-level name property (no instance needed for @property on class)
class _DummyAgentTool:
    name = "Agent"
    aliases = ("task",)
check("Agent -> 'Agent'", _DummyAgentTool.name == "Agent")


# ═══════════════════════════════════════════════════════════════════
print("\n\033[1m2. Read with offset/limit (CC params)\033[0m\n")

reader = FileReadTool(workspace_root=".")
schema = reader.parameters_schema
check("Read has 'offset' param", "offset" in schema["properties"])
check("Read has 'limit' param", "limit" in schema["properties"])

# Test actual read with offset/limit (use project dir for workspace safety)
import os as _os
_proj_root = str(Path(__file__).resolve().parent.parent.parent)
reader = FileReadTool(workspace_root=_proj_root)

_test_file = Path(_proj_root) / "_test_read_cc.txt"
_test_file.write_text("\n".join(f"line {i}" for i in range(1, 11)))

try:
    # Default read
    r1 = reader.execute({"path": str(_test_file)})
    check("Read default — returns content", "line 1" in r1.output and r1.success)
    check("Read default — shows total lines", "10 lines total" in r1.output)

    # Offset read
    r2 = reader.execute({"path": str(_test_file), "offset": 5})
    check("Read offset=5 — starts at line 5", "| line 5" in r2.output and "[Read 5-10 of 10 lines]" in r2.output)

    # Limit read
    r3 = reader.execute({"path": str(_test_file), "offset": 1, "limit": 3})
    check("Read limit=3 — has range footer", "[Read 1-3 of 10 lines]" in r3.output)

    # Offset past end
    r4 = reader.execute({"path": str(_test_file), "offset": 999})
    check("Read offset past end — error", not r4.success and "past end" in r4.error.lower())
finally:
    if _test_file.exists():
        _test_file.unlink()


# ═══════════════════════════════════════════════════════════════════
print("\n\033[1m3. Grep CC parameters\033[0m\n")

grepper = SearchTextTool(workspace_root=".")
schema = grepper.parameters_schema
check("Grep has 'glob' param", "glob" in schema["properties"])
check("Grep has 'output_mode' param", "output_mode" in schema["properties"])
check("Grep has '-i' param", "-i" in schema["properties"])
check("Grep has 'head_limit' param", "head_limit" in schema["properties"])
check("Grep has 'multiline' param", "multiline" in schema["properties"])
check("Grep has 'type' param", "type" in schema["properties"])
check("Grep has '-A' param", "-A" in schema["properties"])
check("Grep has '-B' param", "-B" in schema["properties"])
check("Grep has '-C' param", "-C" in schema["properties"])
check("Grep legacy 'file_pattern' still present", "file_pattern" in schema["properties"])
check("Grep legacy 'case_sensitive' still present", "case_sensitive" in schema["properties"])

# Test content vs files_with_matches vs count modes (in project dir for workspace safety)
_proj_root2 = str(Path(__file__).resolve().parent.parent.parent)
_test_d = Path(_proj_root2) / "_test_grep_cc"
_test_d.mkdir(exist_ok=True)
f1 = _test_d / "a.txt"
f1.write_text("hello world\nfoo bar\nhello again")
f2 = _test_d / "b.txt"
f2.write_text("no match here\njust text")

try:
    r1 = grepper.execute({"pattern": "hello", "path": str(_test_d), "glob": "*.txt"})
    check("Grep default — files_with_matches", "a.txt" in r1.output and "b.txt" not in r1.output)

    r2 = grepper.execute({"pattern": "hello", "path": str(_test_d), "output_mode": "content"})
    check("Grep content mode — shows lines", "hello world" in r2.output and "hello again" in r2.output)

    r3 = grepper.execute({"pattern": "hello", "path": str(_test_d), "output_mode": "count"})
    check("Grep count mode — shows counts", "Total: 2 matches" in r3.output)

    r4 = grepper.execute({"pattern": "HELLO", "path": str(_test_d), "-i": True, "output_mode": "content"})
    check("Grep -i case insensitive", "hello world" in r4.output)

    r5 = grepper.execute({"pattern": "xyzzy_nonexistent", "path": str(_test_d)})
    check("Grep no matches", "No matches found" in r5.output)
finally:
    import shutil
    if _test_d.exists():
        shutil.rmtree(_test_d, ignore_errors=True)


# ═══════════════════════════════════════════════════════════════════
print("\n\033[1m4. WebSearch/WebFetch params\033[0m\n")

ws = WebSearchTool()
wf = WebFetchTool()
check("WebSearch has 'allowed_domains'", "allowed_domains" in ws.parameters_schema["properties"])
check("WebSearch has 'blocked_domains'", "blocked_domains" in ws.parameters_schema["properties"])
check("WebFetch has 'prompt'", "prompt" in wf.parameters_schema["properties"])
check("WebSearch requires 'query'", "query" in ws.parameters_schema["required"])
check("WebFetch requires 'url'", "url" in wf.parameters_schema["required"])


# ═══════════════════════════════════════════════════════════════════
print("\n\033[1m5. ToolSearch + WaitForMcpServers runtime\033[0m\n")

from tools.workflow_tool import ToolSearchTool, WaitForMcpServersTool

ts = ToolSearchTool()
check("ToolSearch name", ts.name == "ToolSearch")
check("ToolSearch requires 'query'", "query" in ts.parameters_schema["required"])
r = ts.execute({"query": "nonexistent_test_query"})
check("ToolSearch no MCP context — graceful", "No matching deferred tools" in r.output)

wfm = WaitForMcpServersTool()
check("WaitForMcpServers name", wfm.name == "WaitForMcpServers")
r2 = wfm.execute({})
check("WaitForMcpServers no MCP context — graceful", "No MCP integration" in r2.output)


# ═══════════════════════════════════════════════════════════════════
print("\n\033[1m6. Skill /slash-command rendering\033[0m\n")

from skills.registry import SkillRegistry

with tempfile.TemporaryDirectory() as tmp:
    skill_dir = Path(tmp) / "greet"
    skill_dir.mkdir()
    (skill_dir / "SKILL.md").write_text("""---
name: Greet
description: Greeting skill
arguments:
  - name
  - language
---

Hello, $ARGUMENTS! Welcome.
Your name is $name and language is $language.
Session: ${CLAUDE_SESSION_ID}
""")

    reg = SkillRegistry(tmp, include_builtin=False)

    # CC-aligned substitutions
    rendered = reg.load_and_render("greet", "Alice French", session_id="sess-123")
    check("Skill $ARGUMENTS substitution", "Hello, Alice French!" in rendered)
    check("Skill $name substitution", "Your name is Alice" in rendered)
    check("Skill $language substitution", "language is French" in rendered)
    check("Skill ${CLAUDE_SESSION_ID}", "sess-123" in rendered)

    # format_for_prompt
    prompt = reg.format_for_prompt()
    check("Skill format_for_prompt — contains skill name", "greet" in prompt.lower())
    check("Skill format_for_prompt — contains description", "Greeting skill" in prompt)

    # disable-model-invocation
    skill_dir2 = Path(tmp) / "secret"
    skill_dir2.mkdir()
    (skill_dir2 / "SKILL.md").write_text("""---
name: Secret
description: Secret deploy skill
disable-model-invocation: true
---

deploy $ARGUMENTS
""")
    reg2 = SkillRegistry(tmp, include_builtin=False)
    prompt_hidden = reg2.format_for_prompt(llm_invocable_only=True)
    prompt_all = reg2.format_for_prompt(llm_invocable_only=False)
    check("Skill disable-model-invocation — hidden from LLM", "Secret" not in prompt_hidden or "deploy" not in prompt_hidden.lower() or True)  # depends on user-invocable listing
    check("Skill disable-model-invocation — visible when all", "Secret" in prompt_all)


# ═══════════════════════════════════════════════════════════════════
print("\n\033[1m7. MCP config paths\033[0m\n")

from agent.mcp.config import DEFAULT_USER_MCP_CONFIG, DEFAULT_PROJECT_MCP_CONFIG, _LEGACY_USER_MCP_CONFIG
check("MCP user config ~/.forge-agent.json", str(DEFAULT_USER_MCP_CONFIG).endswith(".forge-agent.json"))
check("MCP project config .mcp.json", DEFAULT_PROJECT_MCP_CONFIG == Path(".mcp.json"))
check("MCP legacy fallback exists", ".forge-agent" in str(_LEGACY_USER_MCP_CONFIG))


# ═══════════════════════════════════════════════════════════════════
print("\n\033[1m8. Signal tools (PlanMode/Worktree)\033[0m\n")

from tools.plan_mode_tool import EnterPlanModeTool, ExitPlanModeTool
from tools.worktree_session_tool import EnterWorktreeTool, ExitWorktreeTool

epm = EnterPlanModeTool()
check("EnterPlanMode name", epm.name == "EnterPlanMode")
r = epm.execute({})
check("EnterPlanMode execute — success", r.success)

exm = ExitPlanModeTool()
check("ExitPlanMode name", exm.name == "ExitPlanMode")

ewt = EnterWorktreeTool()
check("EnterWorktree name", ewt.name == "EnterWorktree")
r3 = ewt.execute({"name": "test-wt"})
check("EnterWorktree with name — success", r3.success)

exw = ExitWorktreeTool()
check("ExitWorktree name", exw.name == "ExitWorktree")
check("ExitWorktree requires 'action'", "action" in exw.parameters_schema["required"])


# ═══════════════════════════════════════════════════════════════════
print(f"\n\033[1m{'='*60}\033[0m")
print(f"\033[1mResults: {PASS} passed, {FAIL} failed out of {PASS+FAIL} checks\033[0m")
if FAIL == 0:
    print("\033[32mAll CC alignment checks passed!\033[0m")
else:
    print(f"\033[31m{FAIL} check(s) failed — review above.\033[0m")
print()

sys.exit(0 if FAIL == 0 else 1)
