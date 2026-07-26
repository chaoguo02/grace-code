import { useEffect, useMemo, useState } from "react";
import { getArchitectureSnapshot } from "../api/architecture";
import { useSessionStore } from "../stores/sessionStore";
import type {
  ArchitectureAgent,
  ArchitectureComponent,
  ArchitectureSessionNode,
  ArchitectureSnapshot,
} from "../types/architecture";
import { ViewStatePanel } from "./ViewStatePanel";

const LAYERS = [
  ["interface", "Interface"],
  ["orchestration", "Orchestration"],
  ["reasoning", "Reasoning"],
  ["capability", "Capability"],
  ["safety", "Safety"],
  ["context", "Context"],
  ["persistence", "Persistence"],
] as const;

export function groupComponentsByLayer(components: ArchitectureComponent[]) {
  return LAYERS.map(([key, label]) => ({
    key,
    label,
    components: components.filter((component) => component.layer === key),
  })).filter((layer) => layer.components.length > 0);
}

function SessionTreeNode({
  node,
  depth = 0,
}: {
  node: ArchitectureSessionNode;
  depth?: number;
}) {
  return (
    <div className="arch-session-branch">
      <div className="arch-session-node" style={{ marginLeft: depth * 18 }}>
        <span className={`arch-status arch-status-${node.status || "unknown"}`} />
        <strong>{node.agent_name || "agent"}</strong>
        <span>{node.status || "unknown"}</span>
        <code>{node.id.slice(0, 8)}</code>
      </div>
      {(node.children || []).map((child) => (
        <SessionTreeNode key={child.id} node={child} depth={depth + 1} />
      ))}
    </div>
  );
}

function PillList({
  values,
  empty = "None declared",
}: {
  values: string[];
  empty?: string;
}) {
  if (!values.length) return <span className="arch-empty-inline">{empty}</span>;
  return (
    <div className="arch-pills">
      {values.map((value) => <code key={value}>{value}</code>)}
    </div>
  );
}

function AgentDetail({ agent }: { agent: ArchitectureAgent }) {
  return (
    <section className="arch-inspector">
      <div className="arch-section-heading">
        <div>
          <span className="arch-eyebrow">Agent contract</span>
          <h3>{agent.name}</h3>
        </div>
        <span className="arch-source-badge">{agent.source}</span>
      </div>
      <p className="arch-description">{agent.description || "No description declared."}</p>
      <div className="arch-kv-grid">
        <span>Kind<strong>{agent.kind}</strong></span>
        <span>Intent<strong>{agent.intent}</strong></span>
        <span>Context<strong>{agent.context_policy}</strong></span>
        <span>Workspace<strong>{agent.workspace_mode}</strong></span>
        <span>Permission<strong>{agent.permission_mode}</strong></span>
        <span>Model<strong>{agent.model || "inherit"}</strong></span>
        <span>Turn budget<strong>{agent.max_turns ?? "inherit"}</strong></span>
        <span>Token budget<strong>{agent.max_tokens ?? "inherit"}</strong></span>
      </div>
      <div className="arch-contract-row">
        <label>Delegates to</label>
        <PillList values={agent.delegates} />
      </div>
      <div className="arch-contract-row">
        <label>Visible tools</label>
        <PillList values={agent.tools} empty="Registry default" />
      </div>
      <div className="arch-contract-row">
        <label>Required on completion</label>
        <PillList
          values={[
            ...agent.required_tools.map((name) => `tool:${name}`),
            ...Object.entries(agent.completion_requires).map(
              ([key, value]) => `${key}:${String(value)}`,
            ),
          ]}
        />
      </div>
    </section>
  );
}

export function ArchitectureExplorer() {
  const activeId = useSessionStore((state) => state.activeId);
  const [snapshot, setSnapshot] = useState<ArchitectureSnapshot | null>(null);
  const [selectedComponent, setSelectedComponent] = useState("runtime");
  const [selectedAgent, setSelectedAgent] = useState("");
  const [toolCategory, setToolCategory] = useState("all");
  const [error, setError] = useState("");
  const [loading, setLoading] = useState(true);

  useEffect(() => {
    const controller = new AbortController();
    setLoading(true);
    setError("");
    getArchitectureSnapshot(activeId, controller.signal)
      .then((data) => {
        setSnapshot(data);
        setSelectedAgent((current) => (
          data.agents.some((agent) => agent.name === current)
            ? current
            : (data.agents[0]?.name || "")
        ));
      })
      .catch((reason) => {
        if (reason?.name !== "AbortError") {
          setError(reason instanceof Error ? reason.message : String(reason));
        }
      })
      .finally(() => {
        if (!controller.signal.aborted) setLoading(false);
      });
    return () => controller.abort();
  }, [activeId]);

  const layers = useMemo(
    () => groupComponentsByLayer(snapshot?.components || []),
    [snapshot],
  );
  const component = snapshot?.components.find(
    (item) => item.id === selectedComponent,
  ) || snapshot?.components[0];
  const agent = snapshot?.agents.find((item) => item.name === selectedAgent);
  const observedTools = new Set(
    snapshot?.session_overlay?.tool_usage.map((item) => item.name) || [],
  );
  const categories = Array.from(
    new Set(snapshot?.tools.map((tool) => tool.category) || []),
  ).sort();
  const filteredTools = (snapshot?.tools || []).filter(
    (tool) => toolCategory === "all" || tool.category === toolCategory,
  );

  if (loading && !snapshot) {
    return (
      <ViewStatePanel
        tone="loading"
        title="Reading runtime architecture"
        description="Loading configured registries and the optional session overlay."
      />
    );
  }
  if (error && !snapshot) {
    return (
      <ViewStatePanel
        tone="error"
        title="Architecture evidence could not be loaded"
        description={error}
      />
    );
  }
  if (!snapshot) return null;

  const overlay = snapshot.session_overlay;
  const inbound = snapshot.edges.filter((edge) => edge.target === component?.id);
  const outbound = snapshot.edges.filter((edge) => edge.source === component?.id);

  return (
    <div className="architecture-explorer">
      <header className="arch-hero">
        <div>
          <span className="arch-eyebrow">Runtime architecture</span>
          <h1>Agent Architecture Explorer</h1>
          <p>
            Live registries describe what Grace Code can do. The session overlay
            highlights only what the selected run actually used.
          </p>
        </div>
        <div className="arch-runtime-chip">
          <span className="arch-live-dot" />
          {snapshot.runtime.provider} / {snapshot.runtime.model}
        </div>
      </header>

      {error && <div className="arch-warning">{error}</div>}

      <section className="arch-metrics">
        <article><strong>{snapshot.agents.length}</strong><span>agents</span></article>
        <article><strong>{snapshot.tools.length}</strong><span>registered tools</span></article>
        <article><strong>{snapshot.skills.length}</strong><span>skills</span></article>
        <article>
          <strong>{snapshot.mcp.servers.length}</strong>
          <span>MCP servers</span>
        </article>
        <article>
          <strong>{overlay?.tool_usage.length ?? "—"}</strong>
          <span>tools observed</span>
        </article>
      </section>

      <div className="arch-two-column">
        <section className="arch-card arch-topology">
          <div className="arch-section-heading">
            <div>
              <span className="arch-eyebrow">Configured topology</span>
              <h2>Control and data plane</h2>
            </div>
            <div className="arch-legend">
              <span><i className="control" />control</span>
              <span><i className="data" />data</span>
            </div>
          </div>
          <div className="arch-layers">
            {layers.map((layer) => (
              <div className="arch-layer" key={layer.key}>
                <label>{layer.label}</label>
                <div>
                  {layer.components.map((item) => (
                    <button
                      type="button"
                      key={item.id}
                      className={`arch-node ${selectedComponent === item.id ? "selected" : ""}`}
                      onClick={() => setSelectedComponent(item.id)}
                    >
                      <span className={`arch-status arch-status-${item.status}`} />
                      {item.label}
                    </button>
                  ))}
                </div>
              </div>
            ))}
          </div>
        </section>

        <aside className="arch-card arch-component-detail">
          <span className="arch-eyebrow">Component inspector</span>
          <h2>{component?.label}</h2>
          <p>{component?.responsibility}</p>
          <div className="arch-component-status">
            <span className={`arch-status arch-status-${component?.status}`} />
            {component?.status}
          </div>
          <div className="arch-flow-list">
            <h4>Inbound</h4>
            {inbound.map((edge) => (
              <div key={`${edge.source}-${edge.target}`}>
                <code>{edge.source}</code><span>{edge.label}</span>
                <em>{edge.kind}</em>
              </div>
            ))}
            {!inbound.length && <span className="arch-empty-inline">No inbound edge</span>}
            <h4>Outbound</h4>
            {outbound.map((edge) => (
              <div key={`${edge.source}-${edge.target}`}>
                <code>{edge.target}</code><span>{edge.label}</span>
                <em>{edge.kind}</em>
              </div>
            ))}
            {!outbound.length && <span className="arch-empty-inline">No outbound edge</span>}
          </div>
        </aside>
      </div>

      <section className="arch-card arch-session-overlay">
        <div className="arch-section-heading">
          <div>
            <span className="arch-eyebrow">Observed execution</span>
            <h2>Selected session overlay</h2>
          </div>
          <span className={`arch-source-badge ${overlay ? "observed" : ""}`}>
            {overlay ? "persisted facts" : "no session selected"}
          </span>
        </div>
        {overlay ? (
          <div className="arch-overlay-grid">
            <div className="arch-session-tree">
              {overlay.tree && <SessionTreeNode node={overlay.tree} />}
            </div>
            <div className="arch-observation-metrics">
              <span><strong>{overlay.session_count}</strong>sessions in tree</span>
              <span><strong>{overlay.subagent_start_count}</strong>subagent starts</span>
              <span><strong>{overlay.approval_count}</strong>approvals</span>
              <span><strong>{overlay.memory_injected_count}</strong>memories injected</span>
              <span><strong>{overlay.mcp_usage.length}</strong>MCP tools used</span>
              <span><strong>{overlay.agent_names.length}</strong>agents observed</span>
            </div>
            <div className="arch-observed-tools">
              <label>Actual tool calls</label>
              {overlay.tool_usage.length ? overlay.tool_usage.map((item) => (
                <span key={item.name}><code>{item.name}</code><strong>{item.count}×</strong></span>
              )) : <span className="arch-empty-inline">No persisted tool calls</span>}
            </div>
          </div>
        ) : (
          <p className="arch-empty-copy">
            Select a session from the left rail to project its real agent tree,
            approvals, memory recalls, and tool calls over the configured system.
          </p>
        )}
      </section>

      <div className="arch-two-column arch-contracts">
        <section className="arch-card">
          <div className="arch-section-heading">
            <div>
              <span className="arch-eyebrow">Responsibility boundaries</span>
              <h2>Agent contracts</h2>
            </div>
            <select
              value={selectedAgent}
              onChange={(event) => setSelectedAgent(event.target.value)}
            >
              {snapshot.agents.map((item) => (
                <option key={item.name} value={item.name}>{item.name}</option>
              ))}
            </select>
          </div>
          {agent && <AgentDetail agent={agent} />}
        </section>

        <section className="arch-card arch-capability-health">
          <span className="arch-eyebrow">Capability health</span>
          <h2>Integration status</h2>
          <div className="arch-health-row">
            <span>HITL policy</span>
            <strong>{snapshot.hitl.rule_count} rules · {snapshot.hitl.base_approval_mode}</strong>
          </div>
          <div className="arch-health-row">
            <span>Memory</span>
            <strong>{snapshot.memory.enabled ? "enabled" : "disabled"} · {snapshot.memory.memory_count} items</strong>
          </div>
          <div className="arch-health-row">
            <span>MCP</span>
            <strong>
              {snapshot.mcp.initialized ? "initialized" : "not initialized"} · {snapshot.mcp.tool_names.length} tools
            </strong>
          </div>
          <div className="arch-health-row">
            <span>Context budget</span>
            <strong>{snapshot.runtime.request_context_budget.toLocaleString()} tokens</strong>
          </div>
          {snapshot.mcp.servers.map((server) => (
            <div className="arch-server" key={server.name}>
              <span className={`arch-status arch-status-${server.status === "connected" ? "available" : "warning"}`} />
              <code>{server.name}</code>
              <span>{server.tools.length} tools</span>
            </div>
          ))}
        </section>
      </div>

      <section className="arch-card arch-tool-catalog">
        <div className="arch-section-heading">
          <div>
            <span className="arch-eyebrow">Registry versus usage</span>
            <h2>Tool catalog</h2>
          </div>
          <select
            value={toolCategory}
            onChange={(event) => setToolCategory(event.target.value)}
          >
            <option value="all">All categories</option>
            {categories.map((category) => (
              <option key={category} value={category}>{category}</option>
            ))}
          </select>
        </div>
        <div className="arch-tool-table">
          <div className="arch-tool-header">
            <span>Tool</span><span>Category</span><span>Boundary</span><span>Availability</span>
          </div>
          {filteredTools.map((tool) => (
            <div className="arch-tool-row" key={tool.name}>
              <span><code>{tool.name}</code><small>{tool.description}</small></span>
              <span>{tool.category}</span>
              <span>{tool.path_access}{tool.requires_user_interaction ? " · HITL" : ""}</span>
              <span>
                <i className={observedTools.has(tool.name) ? "observed" : ""} />
                {observedTools.has(tool.name)
                  ? "observed"
                  : tool.deferred ? "deferred" : "registered"}
              </span>
            </div>
          ))}
        </div>
      </section>

      <footer className="arch-disclosure">
        Registry facts: {snapshot.disclosure.configured_facts}. Session facts:{" "}
        {snapshot.disclosure.session_facts}. Prompt contents are intentionally excluded.
      </footer>
    </div>
  );
}
