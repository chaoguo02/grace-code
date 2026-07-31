with open("docs/SKILL_SYSTEM_NORMALIZATION_DESIGN.md", "r", encoding="utf-8") as f:
    content = f.read()

content = content.replace(
    "return AgentKind.NAMED_SUBAGENT",
    'return self._frontmatter.get("agent_kind", AgentKind.NAMED_SUBAGENT)'
)
content = content.replace(
    "return TaskIntent.EDIT",
    'return self._frontmatter.get("intent", TaskIntent.EDIT)'
)
# Fix the description text
content = content.replace(
    "This is the minimum viable fix",
    "Defaults are NAMED_SUBAGENT and EDIT, but frontmatter can override via agent_kind/intent keys. This satisfies"
)

print("Phase 0 #1 precision fix applied")
with open("docs/SKILL_SYSTEM_NORMALIZATION_DESIGN.md", "w", encoding="utf-8") as f:
    f.write(content)
