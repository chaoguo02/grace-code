with open("docs/SKILL_SYSTEM_NORMALIZATION_DESIGN.md", "r", encoding="utf-8") as f:
    c = f.read()

items = [
    "- [ ] `SkillMetadata.agent_kind` reads from frontmatter",
    "- [ ] `SkillMetadata.intent` reads from frontmatter",
    "- [ ] `SkillMetadata.tools` reads from frontmatter",
    "- [ ] CLI",
    "- [ ] Frontmatter `agent_kind: READONLY_SUBAGENT`",
    "- [ ] `Any` imported at top level",
    "- [ ] No NameError on type introspection",
    "- [ ] Missing skill preload: ERROR log + runtime notice",
    "- [ ] Agent sees explicit",
    "- [ ] `_sanitize_untrusted_content()` strips injection",
    "- [ ] Builtin skills bypass sanitization",
    "- [ ] Sanitized segments logged via audit trail",
    "- [ ] Description validation rejects empty",
    "- [ ] Non-compliant skills marked degraded",
    "- [ ] `triggers` field detection",
]
for item in items:
    c = c.replace(item, item.replace("[ ]", "[x]"))

with open("docs/SKILL_SYSTEM_NORMALIZATION_DESIGN.md", "w", encoding="utf-8") as f:
    f.write(c)
print("Done")
