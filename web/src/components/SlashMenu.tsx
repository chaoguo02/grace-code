import type { SlashCommand } from "../hooks/useSlashCommands";

interface SlashMenuProps {
  visible: boolean;
  commands: SlashCommand[];
  selectedIndex: number;
  matchPrefix: string;
}

export function SlashMenu({ visible, commands, selectedIndex, matchPrefix }: SlashMenuProps) {
  if (!visible || commands.length === 0) return null;

  return (
    <div
      style={{
        position: "absolute",
        bottom: "100%",
        left: 0,
        right: 0,
        marginBottom: 4,
        background: "var(--bg-elev, #fff)",
        border: "1px solid var(--border, #ddd)",
        borderRadius: 8,
        boxShadow: "0 4px 16px rgba(0,0,0,0.12)",
        maxHeight: 200,
        overflowY: "auto",
        zIndex: 100,
      }}
    >
      {commands.map((cmd, i) => (
        <div
          key={cmd.name}
          style={{
            padding: "8px 12px",
            cursor: "pointer",
            background: i === selectedIndex ? "var(--accent-soft, #d178271a)" : "transparent",
            borderBottom: i < commands.length - 1 ? "1px solid var(--border, #ddd)" : "none",
          }}
          onMouseDown={(e) => {
            e.preventDefault();
            cmd.handler("");
          }}
        >
          <div style={{ fontWeight: 600, fontSize: 13, color: "var(--accent, #d17827)" }}>
            /{cmd.name}
          </div>
          <div style={{ fontSize: 12, color: "var(--text-dim, #6b5d4f)", marginTop: 2 }}>
            {cmd.description}
          </div>
          {cmd.usage && (
            <div style={{ fontSize: 11, color: "var(--text-muted, #988775)", marginTop: 1 }}>
              Usage: {cmd.usage}
            </div>
          )}
        </div>
      ))}
    </div>
  );
}
