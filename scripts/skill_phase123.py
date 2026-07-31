"""Apply Skill Phase 1-3 changes to skills/registry.py."""
with open("skills/registry.py", "r", encoding="utf-8") as f:
    c = f.read()

# 1. _source_errors dict in __init__
if "self._mcp_dependency_cache: dict[str, frozenset[str]] = {}" in c:
    c = c.replace(
        "self._mcp_dependency_cache: dict[str, frozenset[str]] = {}",
        "self._mcp_dependency_cache: dict[str, frozenset[str]] = {}\n        self._source_errors: dict[str, str] = {}  # Phase 2 #8"
    )
    print("1. _source_errors dict added")
else:
    print("1. _source_errors SKIP — pattern not found")

# 2. triggers deprecation (Phase 3 #7)
old2 = "        fm_dict: dict = yaml.safe_load(frontmatter) or {}"
new2 = """        fm_dict: dict = yaml.safe_load(frontmatter) or {}
        # Phase 3 #7: triggers deprecation
        if "triggers" in fm_dict:
            logger.info(
                "Skill %s has deprecated 'triggers' field — ignored. "
                "Use 'description' for LLM semantic matching instead.",
                dir_name,
            )"""
c = c.replace(old2, new2)
print("2. triggers deprecation added")

# 3. Legacy description fallback (Phase 2 #5)
helper = """
def _legacy_description_fallback(content: str, name: str) -> str:
    \"\"\"Phase 2 #5: extract description from body for commands without frontmatter.\"\"\"
    lines = content.split(chr(10))
    body_lines = []
    for line in lines:
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        if stripped.startswith("```"):
            continue
        body_lines.append(stripped)
    desc = " ".join(body_lines[:3])[:100].strip()
    return desc if desc else f"(no description — {name})"

"""
# Insert before @dataclass class SkillMetadata
old3 = "@dataclass\nclass SkillMetadata:"
new3 = helper + old3
c = c.replace(old3, new3)
print("3. legacy fallback helper added")

# 4. Use the fallback in the no-frontmatter path
old4 = "display_name=dir_name,\n                description=\"\",\n                dir_path=str(skill_file.parent),\n                source=source.name,\n                trusted=source.trusted,\n                file_path=str(skill_file),\n                _frontmatter={},"
new4 = "display_name=dir_name,\n                # Phase 2 #5: legacy description fallback\n                description=_legacy_description_fallback(content, dir_name),\n                dir_path=str(skill_file.parent),\n                source=source.name,\n                trusted=source.trusted,\n                file_path=str(skill_file),\n                _frontmatter={},"
c = c.replace(old4, new4)
print("4. legacy fallback used in no-frontmatter path")

with open("skills/registry.py", "w", encoding="utf-8") as f:
    f.write(c)
print("DONE — all Skill Phase 1-3 registry changes applied")
