/**
 * KeyboardHelp — ? key overlay showing all available shortcuts.
 * CC-aligned: VS Code / GitHub style shortcut reference.
 */
import { useEffect, useCallback } from "react";

interface Shortcut {
  keys: string;
  action: string;
  scope: string;
}

const SHORTCUTS: Shortcut[] = [
  { keys: "Enter", action: "Send message", scope: "Input focused" },
  { keys: "Shift+Enter", action: "New line", scope: "Input focused" },
  { keys: "/", action: "Slash command menu", scope: "Input empty" },
  { keys: "@", action: "File search panel", scope: "Input" },
  { keys: "Escape", action: "Close panel / cancel", scope: "Global" },
  { keys: "Y", action: "Approve tool call", scope: "HITL visible, not editing" },
  { keys: "N", action: "Deny tool call", scope: "HITL visible, not editing" },
  { keys: "Shift+Y", action: "Approve + remember", scope: "HITL visible, not editing" },
  { keys: "Ctrl+O", action: "Cycle view mode (Verbose→Normal→Summary)", scope: "Global" },
  { keys: "Ctrl+Shift+B", action: "Switch to Build mode", scope: "Global" },
  { keys: "Ctrl+Shift+P", action: "Switch to Plan mode", scope: "Global" },
  { keys: "Ctrl+Shift+E", action: "Switch to Explore mode", scope: "Global" },
  { keys: "?", action: "Show/hide this help", scope: "Global, not editing" },
];

interface Props {
  onClose: () => void;
}

export function KeyboardHelp({ onClose }: Props) {
  const handleKey = useCallback((e: KeyboardEvent) => {
    if (e.key === "Escape" || e.key === "?") { e.preventDefault(); onClose(); }
  }, [onClose]);

  useEffect(() => {
    document.addEventListener("keydown", handleKey);
    return () => document.removeEventListener("keydown", handleKey);
  }, [handleKey]);

  return (
    <div className="help-overlay" onClick={onClose} role="dialog" aria-label="Keyboard shortcuts">
      <div className="help-panel" onClick={(e) => e.stopPropagation()}>
        <div className="help-header">
          <h2>Keyboard Shortcuts</h2>
          <button className="help-close" onClick={onClose} aria-label="Close">×</button>
        </div>
        <div className="help-table">
          {SHORTCUTS.map((s) => (
            <div key={s.keys + s.action} className="help-row">
              <kbd className="help-keys">{s.keys}</kbd>
              <span className="help-action">{s.action}</span>
              <span className="help-scope">{s.scope}</span>
            </div>
          ))}
        </div>
      </div>
    </div>
  );
}
