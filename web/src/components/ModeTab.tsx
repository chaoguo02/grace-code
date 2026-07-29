/**
 * ModeTab — external mode switcher placed above the composer.
 *
 * Mode is session-level context, not message-level. Placing it above
 * the composer makes this clear: choose mode first, then type.
 *
 * Keyboard: Ctrl+Shift+B/P/M  (CC Shift+Tab equivalent for Web)
 */
import type { ModeKey } from "./ChatView";

interface ModeTabProps {
  mode: ModeKey;
  onChange: (mode: ModeKey) => void;
  disabled?: boolean;
}

const OPTIONS: Array<{ key: ModeKey; label: string; title: string; placeholder: string }> = [
  { key: "build",       label: "Build",       title: "Implement, edit, and ship changes",         placeholder: "描述要实现的功能…" },
  { key: "plan",        label: "Plan",        title: "Think first — generate an implementation plan", placeholder: "描述要规划的任务…" },
  { key: "multi-agent", label: "Multi-Agent", title: "Coordinate specialist tasks, integrate changes, and verify the result", placeholder: "描述要由多个 Agent 协作完成的复杂任务…" },
];

export function getPlaceholder(mode: ModeKey): string {
  return OPTIONS.find((o) => o.key === mode)?.placeholder || "描述你想要做的事情…";
}

export function ModeTab({ mode, onChange, disabled }: ModeTabProps) {
  return (
    <div className="mode-tab-row" role="tablist" aria-label="Agent mode">
      {OPTIONS.map((opt) => (
        <button
          key={opt.key}
          role="tab"
          type="button"
          className={`mode-tab ${mode === opt.key ? "active" : ""}`}
          aria-selected={mode === opt.key}
          title={opt.title}
          disabled={disabled}
          onClick={() => onChange(opt.key)}
        >
          {opt.label}
        </button>
      ))}
    </div>
  );
}
