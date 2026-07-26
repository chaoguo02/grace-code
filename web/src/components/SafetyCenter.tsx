import { useEffect, useMemo, useState } from "react";

import { getSafetySnapshot } from "../api/safety";
import { selectSessionUi, useChatStore } from "../stores/chatStore";
import { useSessionStore } from "../stores/sessionStore";
import type {
  ApprovalAuditItem,
  SafetyLayer,
  SafetySnapshot,
  SafetyTool,
} from "../types/safety";
import { ViewStatePanel } from "./ViewStatePanel";

function titleCase(value?: string) {
  return (value || "unknown")
    .replace(/_/g, " ")
    .replace(/\b\w/g, (letter) => letter.toUpperCase());
}

function formatWait(ms: number) {
  if (!ms) return "—";
  return ms < 1000 ? `${Math.round(ms)} ms` : `${(ms / 1000).toFixed(1)} s`;
}

export function filterSafetyTools(
  tools: SafetyTool[],
  risk: string,
  query: string,
) {
  const normalized = query.trim().toLowerCase();
  return tools.filter((tool) => (
    (risk === "all" || tool.risk === risk)
    && (!normalized || [
      tool.name,
      tool.control,
      tool.path_access,
      ...tool.effects,
    ].some((value) => value.toLowerCase().includes(normalized)))
  ));
}

function AuthorityBadge({
  allow,
  deny,
}: {
  allow: boolean;
  deny: boolean;
}) {
  return (
    <div className="safety-authority-badges">
      <span className={allow ? "enabled" : ""}>allow</span>
      <span className={deny ? "enabled deny" : ""}>deny</span>
    </div>
  );
}

function LayerInspector({ layer }: { layer: SafetyLayer }) {
  return (
    <aside className="safety-layer-inspector">
      <span className="safety-eyebrow">Selected authority layer</span>
      <div className="safety-layer-index">{layer.order}</div>
      <h2>{layer.label}</h2>
      <p>{layer.detail}</p>
      <dl>
        <div><dt>Authority</dt><dd>{titleCase(layer.authority)}</dd></div>
        <div><dt>Can allow</dt><dd>{layer.can_allow ? "Yes" : "No"}</dd></div>
        <div><dt>Can deny</dt><dd>{layer.can_deny ? "Yes" : "No"}</dd></div>
      </dl>
      {layer.authority === "absolute" && (
        <div className="safety-absolute-note">
          This boundary cannot be overridden downstream.
        </div>
      )}
    </aside>
  );
}

function ApprovalItem({
  item,
  onInspect,
}: {
  item: ApprovalAuditItem;
  onInspect?: (sequence: number) => void;
}) {
  const decision = item.status === "timed_out"
    ? "timeout"
    : item.decision || "not recorded";
  return (
    <article className={`safety-approval-item status-${item.status}`}>
      <div className="safety-approval-rail">
        <i />
        <span>#{item.sequence}</span>
      </div>
      <div className="safety-approval-copy">
        <div>
          <code>{item.tool_name}</code>
          <span className={`safety-decision decision-${item.decision || item.status}`}>
            {titleCase(decision)}
          </span>
        </div>
        <strong>{item.decision_reason || "Interactive approval requested"}</strong>
        <p>{item.target || `Parameters: ${item.params_keys.join(", ") || "none"}`}</p>
        {item.note && <small>Approver note: {item.note}</small>}
      </div>
      <dl>
        <div><dt>Wait</dt><dd>{formatWait(item.wait_ms)}</dd></div>
        <div><dt>Input changed</dt><dd>{item.updated_input ? "Yes" : "No"}</dd></div>
        <div><dt>Request</dt><dd>{item.request_id.slice(0, 8)}</dd></div>
      </dl>
      {onInspect && item.sequence > 0 && (
        <button
          type="button"
          className="safety-approval-trace"
          onClick={() => onInspect(item.sequence)}
        >
          View in trace
        </button>
      )}
    </article>
  );
}

interface SafetyCenterProps {
  onInspectApproval?: (sequence: number) => void;
}

export function SafetyCenter({
  onInspectApproval,
}: SafetyCenterProps = {}) {
  const activeId = useSessionStore((state) => state.activeId);
  const liveApprovalKey = useChatStore((state) => {
    if (!activeId) return "";
    const events = selectSessionUi(state, activeId).events;
    const approval = events.find((event) => (
      event.type === "approval_required"
      || event.type === "approval_resolved"
      || event.type === "approval_timeout"
    ));
    return approval
      ? `${approval.type}:${approval.request_id}:${approval.sequence || 0}`
      : "";
  });
  const [snapshot, setSnapshot] = useState<SafetySnapshot | null>(null);
  const [selectedLayerId, setSelectedLayerId] = useState("input_validation");
  const [ruleTier, setRuleTier] = useState("all");
  const [toolRisk, setToolRisk] = useState("all");
  const [toolQuery, setToolQuery] = useState("");
  const [reloadKey, setReloadKey] = useState(0);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState("");

  useEffect(() => {
    const controller = new AbortController();
    setLoading(true);
    setError("");
    getSafetySnapshot(activeId, controller.signal)
      .then(setSnapshot)
      .catch((reason) => {
        if (reason?.name !== "AbortError") {
          setError(reason instanceof Error ? reason.message : String(reason));
        }
      })
      .finally(() => {
        if (!controller.signal.aborted) setLoading(false);
      });
    return () => controller.abort();
  }, [activeId, liveApprovalKey, reloadKey]);

  const layer = snapshot?.layers.find(
    (item) => item.id === selectedLayerId,
  ) || snapshot?.layers[0];
  const rules = (snapshot?.rules || []).filter(
    (rule) => ruleTier === "all" || rule.tier === ruleTier,
  );
  const tools = useMemo(
    () => filterSafetyTools(snapshot?.tools || [], toolRisk, toolQuery),
    [snapshot, toolRisk, toolQuery],
  );
  const riskCounts = useMemo(() => (
    (snapshot?.tools || []).reduce<Record<string, number>>((counts, tool) => {
      counts[tool.risk] = (counts[tool.risk] || 0) + 1;
      return counts;
    }, {})
  ), [snapshot]);

  if (loading && !snapshot) {
    return (
      <ViewStatePanel
        tone="loading"
        title="Reading authority boundaries"
        description="Loading configured policy and the selected session audit overlay."
      />
    );
  }
  if (error && !snapshot) {
    return (
      <ViewStatePanel
        tone="error"
        title="Safety evidence could not be loaded"
        description={error}
      />
    );
  }
  if (!snapshot || !layer) return null;

  const session = snapshot.session;

  return (
    <div className="safety-center">
      <header className="safety-hero">
        <div>
          <span className="safety-eyebrow">Runtime authority</span>
          <h1>Safety & Authority Center</h1>
          <p>
            Explain which layer can allow, deny, or transform a tool request,
            and audit the human decisions made for the selected session.
          </p>
        </div>
        <button type="button" onClick={() => setReloadKey((value) => value + 1)}>
          {loading ? "Refreshing…" : "Refresh policy"}
        </button>
      </header>

      {error && <div className="safety-error">{error}</div>}

      <section className="safety-metrics">
        <article><strong>{snapshot.rule_summary.total}</strong><span>active rules</span></article>
        <article><strong>{snapshot.rule_summary.by_tier.deny || 0}</strong><span>deny rules</span></article>
        <article><strong>{riskCounts.critical || 0}</strong><span>critical tools</span></article>
        <article><strong>{riskCounts.high || 0}</strong><span>high-risk tools</span></article>
        <article><strong>{session?.pending_approval_count ?? "—"}</strong><span>waiting approvals</span></article>
      </section>

      <div className="safety-pipeline-grid">
        <section className="safety-card safety-pipeline">
          <div className="safety-section-heading">
            <div>
              <span className="safety-eyebrow">Fail-closed evaluation</span>
              <h2>Permission pipeline</h2>
            </div>
            <span className="safety-precedence">deny → ask → allow</span>
          </div>
          <div className="safety-layer-flow">
            {snapshot.layers.map((item, index) => (
              <button
                type="button"
                key={item.id}
                className={item.id === layer.id ? "active" : ""}
                onClick={() => setSelectedLayerId(item.id)}
              >
                <i>{item.order}</i>
                <span>{item.label}</span>
                <small>{item.authority}</small>
                <AuthorityBadge allow={item.can_allow} deny={item.can_deny} />
                {index < snapshot.layers.length - 1 && <em>→</em>}
              </button>
            ))}
          </div>
        </section>
        <LayerInspector layer={layer} />
      </div>

      <div className="safety-main-grid">
        <section className="safety-card safety-session-authority">
          <div className="safety-section-heading">
            <div>
              <span className="safety-eyebrow">Effective session boundary</span>
              <h2>{session ? `${session.agent_name} authority` : "No session selected"}</h2>
            </div>
            {session && <code>{session.session_id.slice(0, 8)}</code>}
          </div>
          {session ? (
            <>
              <div className="safety-mode-chain">
                <div><span>Agent default</span><strong>{session.default_mode}</strong></div>
                <i>→</i>
                <div><span>Pending override</span><strong>{session.pending_mode || "none"}</strong></div>
                <i>→</i>
                <div className="effective"><span>Next-run mode</span><strong>{session.effective_next_mode}</strong></div>
              </div>
              <dl className="safety-session-facts">
                <div><dt>Agent kind</dt><dd>{titleCase(session.agent_kind)}</dd></div>
                <div><dt>Parent agent</dt><dd>{session.parent_agent_name || "none"}</dd></div>
                <div><dt>Deny inheritance</dt><dd>{session.deny_rules_inherited ? "enforced" : "root policy"}</dd></div>
                <div><dt>Path sandbox</dt><dd title={session.project_root}>{session.project_root}</dd></div>
              </dl>
            </>
          ) : (
            <p className="safety-empty-copy">
              Select a session to inspect its effective mode, parent authority,
              approval history, and project sandbox.
            </p>
          )}
        </section>

        <section className="safety-card safety-invariants">
          <span className="safety-eyebrow">Non-negotiable contracts</span>
          <h2>Safety invariants</h2>
          {snapshot.invariants.map((item, index) => (
            <article key={item.name}>
              <i>{index + 1}</i>
              <div><strong>{item.name}</strong><p>{item.detail}</p></div>
            </article>
          ))}
        </section>
      </div>

      <section className="safety-card safety-approval-audit">
        <div className="safety-section-heading">
          <div>
            <span className="safety-eyebrow">Control request → response</span>
            <h2>Approval audit trail</h2>
          </div>
          {session && (
            <div className="safety-approval-summary">
              <span><strong>{session.approval_summary.allowed}</strong>allowed</span>
              <span><strong>{session.approval_summary.denied}</strong>denied</span>
              <span><strong>{session.approval_summary.timed_out}</strong>timeout</span>
              <span><strong>{formatWait(session.approval_summary.average_wait_ms)}</strong>avg wait</span>
            </div>
          )}
        </div>
        {session?.approvals.length ? (
          <div className="safety-approval-list">
            {session.approvals.map((item) => (
              <ApprovalItem
                key={item.request_id}
                item={item}
                onInspect={onInspectApproval}
              />
            ))}
          </div>
        ) : (
          <p className="safety-empty-copy">
            {session
              ? "No interactive approval requests were persisted for this session."
              : "Select a session to view its approval audit trail."}
          </p>
        )}
        {!!session?.approval_summary.response_not_recorded && (
          <div className="safety-legacy-note">
            {session.approval_summary.response_not_recorded} historical request(s)
            predate response persistence; their outcome is intentionally shown as unknown.
          </div>
        )}
      </section>

      <div className="safety-catalog-grid">
        <section className="safety-card safety-rule-catalog">
          <div className="safety-section-heading">
            <div>
              <span className="safety-eyebrow">Live settings merge</span>
              <h2>Permission rules</h2>
            </div>
            <select value={ruleTier} onChange={(event) => setRuleTier(event.target.value)}>
              <option value="all">All tiers</option>
              <option value="deny">Deny</option>
              <option value="ask">Ask</option>
              <option value="allow">Allow</option>
            </select>
          </div>
          <div className="safety-rule-table">
            <div><span>Tier</span><span>Rule</span><span>Source</span><span>Priority</span></div>
            {rules.map((rule, index) => (
              <div key={`${rule.raw}-${rule.source}-${index}`}>
                <span className={`tier-${rule.tier}`}>{rule.tier}</span>
                <code>{rule.raw}</code>
                <span>{rule.source}</span>
                <strong>{rule.source_priority}</strong>
              </div>
            ))}
            {!rules.length && <p>No rules in this tier.</p>}
          </div>
        </section>

        <section className="safety-card safety-modes">
          <span className="safety-eyebrow">Session posture</span>
          <h2>Permission modes</h2>
          {snapshot.modes.map((mode) => (
            <article
              className={session?.effective_next_mode === mode.name ? "active" : ""}
              key={mode.name}
            >
              <div><code>{mode.name}</code><span>{mode.posture}</span></div>
              <p>{mode.detail}</p>
            </article>
          ))}
        </section>
      </div>

      <section className="safety-card safety-tool-matrix">
        <div className="safety-section-heading">
          <div>
            <span className="safety-eyebrow">Declarative effects</span>
            <h2>Tool risk and control matrix</h2>
          </div>
          <div className="safety-tool-filters">
            <input
              value={toolQuery}
              onChange={(event) => setToolQuery(event.target.value)}
              placeholder="Filter tools…"
            />
            <select value={toolRisk} onChange={(event) => setToolRisk(event.target.value)}>
              <option value="all">All risk levels</option>
              <option value="critical">Critical</option>
              <option value="high">High</option>
              <option value="medium">Medium</option>
              <option value="low">Low</option>
              <option value="unknown">Unknown</option>
            </select>
          </div>
        </div>
        <div className="safety-tool-table">
          <div><span>Tool</span><span>Risk</span><span>Control</span><span>Effects</span><span>Path scope</span></div>
          {tools.map((tool) => (
            <div key={tool.name}>
              <code>{tool.name}</code>
              <span className={`risk-${tool.risk}`}>{tool.risk}</span>
              <span>{titleCase(tool.control)}</span>
              <span>{tool.effects.join(", ") || "none"}</span>
              <span>{tool.path_access}{tool.path_parameter ? ` · ${tool.path_parameter}` : ""}</span>
            </div>
          ))}
        </div>
      </section>

      <footer className="safety-disclosure">
        Source: {snapshot.disclosure.source}. This view performs no tool call
        and does not simulate hooks or interactive decisions.
      </footer>
    </div>
  );
}
