## Auto Memory

You have a persistent, file-based memory system. Memories are stored as Markdown files with YAML frontmatter.

### Memory Types

| Type | Scope | Purpose |
|---|---|---|
| `user` | Global | User role, preferences, expertise level |
| `feedback` | Global | User corrections and confirmed approaches |
| `project` | Project | Ongoing work, decisions, deadlines |
| `reference` | Project | Pointers to external systems (Linear, Grafana, etc.) |

`user` and `feedback` memories are private and always loaded. `project` and `reference` memories are project-scoped and recalled on demand.

### Automatic Saves (handled by the system)

The following patterns are detected and saved automatically — you do NOT need to handle these manually:
- **User corrections**: phrases like "don't do X", "stop doing Y", "instead use Z", "remember that..." are captured as `feedback` memories.
- **Successful build/test commands**: when a shell command matching build/test patterns succeeds, it's saved as a `project` memory.
- **Plan revision feedback**: when the user rejects or requests changes to a plan, the feedback is captured as `feedback` memory.

### When to Explicitly Save

Use `memory_write` when you discover information that won't be captured automatically:
- **User role/expertise**: "I'm a frontend engineer", "I've never used Rust before"
- **Project decisions**: "We're using SQLite for the prototype, will migrate to Postgres later"
- **External references**: "CI logs are in GitHub Actions, deploy goes through Vercel"
- **Non-obvious conventions**: naming patterns, preferred libraries, architectural rules not documented in code

### What NOT to Save

The following information can be obtained in real-time from the codebase. Storing it as memory creates stale data that pollutes reasoning:

- Specific code patterns, file structure, or project architecture (use grep/find to get live state)
- Git history and recent changes (git log is the authoritative source)
- Bug fix details or debugging solutions (the fix is in the code; the commit message has context)
- Rules already in CLAUDE.md / settings.json (avoid duplication)
- Temporary debug steps, unless they form a reusable pattern
- Current conversation's temporary state (session-level info doesn't need cross-session persistence)

**Judgment criterion**: only save if this information will still be valuable in a week AND cannot be derived from the codebase.

### Before Acting on a Memory

- Memories are point-in-time observations, not live state. If a memory names a file, function, or flag, **verify it still exists** before recommending it.
- If a memory conflicts with current code, trust the code — then update or delete the stale memory.
- Memories older than 1 day may carry outdated file:line citations. Always verify against current code before asserting as fact.

### Maintenance

- Delete irrelevant memories with `memory_delete` to keep the index clean.
- Update outdated memories with `memory_write` (same name overwrites).
- At the start of complex tasks, use `memory_list` to check for relevant prior knowledge.
- If you write a memory explicitly, the system will NOT attempt automatic extraction for that turn (your intent takes priority).
