/**
 * SessionTree — hierarchical subagent session navigator.
 *
 * Shows parent-child session tree with status icons,
 * descendant counts, and click-to-inspect navigation.
 *
 * CSS classes: .session-tree-* (Phase 7 Batch B)
 * Dynamic inline styles (marginLeft, color, fontWeight) are acceptable
 * exceptions — see RISK_REGISTER.md for details.
 */
import { useEffect } from "react";
import { useSessionStore } from "../stores/sessionStore";
import { selectSessionUi, useChatStore } from "../stores/chatStore";
import type { SessionTreeNode } from "../api/sessions";

const STATUS_ICONS: Record<string, string> = {
  running: "●",
  completed: "?",
  failed: "×",
  queued: "○",
  cancelled: "○",
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
  const color = STATUS_COLORS[node.status] || "inherit";
  const isActive = node.id === activeId;

  return (
    <div style={{ marginLeft: depth * 12 }}>  {/* dynamic — acceptable exception */}
      <button
        type="button"
        onClick={() => onSelect(node.id)}
        className={"session-tree-node-btn" + (isActive ? " active" : "")}
      >
        <span className="session-tree-node-icon" style={{ color }}>
          {STATUS_ICONS[node.status] || "○"}
        </span>
        <span className="session-tree-node-label" style={{ fontWeight: isActive ? 600 : 400 }}>
          {node.agent_name || "agent"}
        </span>
        {node.child_count > 0 && (
          <span className="session-tree-node-count">+{node.child_count}</span>
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
  const setViewingChild = useChatStore((s) => s.setViewingChild);

  const timeline = useChatStore((s) => selectSessionUi(s, activeId).timeline);

  useEffect(() => {
    if (!activeId) return;
    const last = timeline[timeline.length - 1];
    if (last?.source === "ws" && (last.ws.type === "subagent_start" || last.ws.type === "subagent_stop")) {
      const timer = setTimeout(() => fetchSessionTree(activeId), 500);
      return () => clearTimeout(timer);
    }
  }, [activeId, timeline.length, fetchSessionTree]);

  useEffect(() => {
    if (activeId) fetchSessionTree(activeId);
  }, [activeId, fetchSessionTree]);

  if (!sessionTree || sessionTree.child_count === 0) {
    return null;
  }

  return (
    <aside className="session-tree-panel">
      <div className="session-tree-heading">Agent Tree</div>
      <TreeNode
        node={sessionTree}
        depth={0}
        activeId={activeId}
        onSelect={(id) => {
          if (id === sessionTree.id) return;
          setViewingChild(id, activeId);
        }}
      />
    </aside>
  );
}
