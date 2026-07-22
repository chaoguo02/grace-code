# Visual Regression Baselines

Subagent components (SubagentDetail, SubagentProgress, SessionTree) visual
reference images. Captured at 1440x900 (desktop) and 375x812 (mobile).

## Update Protocol

1. Intentionally change the UI?
2. Run: `UPDATE_BASELINE=1 bash tools/_quality_gate.sh`
3. Open the follow-up issue the tool creates.
4. Commit the updated baselines.

## Never Regenerate Without Review

Golden baselines serve as the single source of truth for visual
regression testing. Regenerating them without review erases the
audit trail of what changed.

## Files

| File | Viewport | Component |
|------|----------|-----------|
| `subagent-desktop-1440.png` | 1440×900 | ChatView with SessionSidebar |
| `subagent-mobile-375.png` | 375×812 | ChatView with SessionSidebar (mobile) |
