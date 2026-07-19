/**
 * SessionTree — hierarchical subagent session navigator.
 *
 * CC-aligned: shows parent-child session tree with status icons,
 * descendant counts, and click-to-inspect navigation.
 * Max 5 levels deep (CC cap).
 */
import { useEffect } from "react";
import { useSessionStore } from "../stores/sessionStore";
import type { SessionTreeNode } from "../api/sessions";

const STATUS_ICONS: Record<string, string> = {
  running: "◎",
  completed: "✓",
  failed: "✗",
  queued: "○",
  cancelled: "◼",
};

const STATUS_COLORS: Record<string, string> = {
  running: "var(--accent)",
  completed: "var(--green, #4caf50)",
  failed: "var(--red, #f44336)",
  queued: "var(--text-muted)",
  cancelled: "var(--text-muted)",
};

function TreeNode({ node, depth, activeId, onSelect }: {
  node: SessionTreeNode;
  depth: number;
  activeId: string | null;
  onSelect: (id: string) => void;
}) {
  const icon = STATUS_ICONS[node.status] || "●";
  const color = STATUS_COLORS[node.status] || "inherit";
  const isActive = node.id === activeId;

  return (
    <div style={{ marginLeft: depth * 12 }}>
      <button
        type="button"
        onClick={() => onSelect(node.id)}
        style={{
          display: "flex",
          alignItems: "center",
          gap: 6,
          padding: "3px 8px",
          width: "100%",
          border: "none",
          borderRadius: 4,
          background: isActive ? "var(--accent-soft)" : "transparent",
          cursor: "pointer",
          fontSize: 12,
          color: "var(--text)",
          textAlign: "left" as const,
        }}
      >
        <span style={{ color, fontSize: 10 }}>{icon}</span>
        <span style={{ flex: 1, fontWeight: isActive ? 600 : 400 }}>
          {node.agent_name || "agent"}
        </span>
        {node.child_count > 0 && (
          <span style={{ fontSize: 10, color: "var(--text-muted)" }}>
            +{node.child_count}
          </span>
        )}
      </button>
      {node.children.map((child) => (
        <TreeNode
          key={child.id}
          node={child}
          depth={depth + 1}
          activeId={activeId}
          onSelect={onSelect}
        />
      ))}
    </div>
  );
}

export function SessionTree() {
  const activeId = useSessionStore((s) => s.activeId);
  const sessionTree = useSessionStore((s) => s.sessionTree);
  const fetchSessionTree = useSessionStore((s) => s.fetchSessionTree);
  const openSession = useSessionStore((s) => s.openSession);

  useEffect(() => {
    if (activeId) {
      fetchSessionTree(activeId);
    }
  }, [activeId, fetchSessionTree]);

  if (!sessionTree || sessionTree.child_count === 0) {
    return null;
  }

  return (
    <div style={{
      padding: "8px 0",
      borderBottom: "1px solid var(--border)",
      fontSize: 12,
    }}>
      <div style={{
        padding: "0 12px 6px",
        color: "var(--text-muted)",
        fontSize: 10,
        fontWeight: 600,
        textTransform: "uppercase",
        letterSpacing: "0.5px",
      }}>
        Agent Tree
      </div>
      <TreeNode
        node={sessionTree}
        depth={0}
        activeId={activeId}
        onSelect={(id) => openSession(id)}
      />
    </div>
  );
}
