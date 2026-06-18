---
name: Git Workflow
description: Git 操作规范，包括 commit message 格式、branch 策略和常用操作最佳实践
triggers:
  - git
  - commit
  - branch
  - merge
  - 提交
---

## Commit Message Format

Use [Conventional Commits](https://www.conventionalcommits.org/):

```
<type>(<scope>): <description>

[optional body]

[optional footer]
```

### Types
| Type | Use when |
|------|----------|
| `feat` | New feature for the user |
| `fix` | Bug fix |
| `refactor` | Code restructure without behavior change |
| `docs` | Documentation only |
| `test` | Adding or fixing tests |
| `chore` | Build, CI, dependencies, tooling |
| `perf` | Performance improvement |

### Rules
- Subject line: imperative mood ("add" not "added"), under 72 chars
- Body: explain WHY, not WHAT (the diff shows what)
- One logical change per commit (split unrelated changes)
- Reference issues: `fixes #123` or `closes #456` in footer

### Examples
```
feat(auth): add OAuth2 login flow

Implement Google OAuth2 for user authentication.
Session tokens stored in Redis with 24h TTL.

Closes #42
```

```
fix(parser): handle empty input without crashing

Previously threw IndexError on empty string.
Now returns an empty AST node.
```

## Branch Strategy

```
main (production-ready)
  └── feature/xxx (short-lived, one feature per branch)
  └── fix/xxx (bug fix branches)
```

- Branch from `main`, merge back to `main`
- Keep branches short-lived (< 1 week ideal)
- Rebase before merge to keep linear history: `git rebase main`
- Delete branch after merge

## Common Operations

| Task | Command |
|------|---------|
| Stage specific files | `git add file1 file2` (avoid `git add .`) |
| Amend last commit (unpushed) | `git commit --amend` |
| Undo last commit (keep changes) | `git reset --soft HEAD~1` |
| Stash work in progress | `git stash push -m "description"` |
| View changes before commit | `git diff --staged` |
| Cherry-pick a commit | `git cherry-pick <sha>` |

## Safety Rules
- Never force-push to `main`/`master`
- Never commit secrets (use `.env` + `.gitignore`)
- Always review `git diff --staged` before committing
- Run tests before pushing

### For: $ARGUMENTS
Apply these git workflow practices to the operation described above.