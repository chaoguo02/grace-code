import { useEffect, useMemo, useState } from "react";
import {
  approveAgentTeam,
  cancelDelegationTask,
  claimAgentTeamTask,
  executeAgentTeamTask,
  getMultiAgentSnapshot,
  rejectAgentTeam,
  retryDelegationTask,
  shutdownAgentTeam,
} from "../api/multiAgent";
import { useSessionStore } from "../stores/sessionStore";
import type {
  AgentCommunication,
  AgentBudgetProjection,
  DelegationTaskProjection,
  MultiAgentNode,
  MultiAgentSnapshot,
} from "../types/multiAgent";
import { SessionRequiredState } from "./SessionRequiredState";
import { ViewStatePanel } from "./ViewStatePanel";

export function orderTopologyNodes(nodes: MultiAgentNode[]): MultiAgentNode[] {
  return [...nodes].sort(
    (left, right) => left.depth - right.depth
      || left.created_at.localeCompare(right.created_at)
      || left.id.localeCompare(right.id),
  );
}

function shortId(id: string | null) {
  return id ? id.slice(0, 8) : "root";
}

function formatCount(value: number | null | undefined) {
  return typeof value === "number" ? value.toLocaleString() : "—";
}

function formatDuration(value: number | null | undefined) {
  if (typeof value !== "number") return "—";
  if (value < 1000) return `${value} ms`;
  return `${(value / 1000).toFixed(value < 10000 ? 1 : 0)} s`;
}

function budgetSummary(budget: AgentBudgetProjection | null | undefined) {
  if (!budget) return "No persisted budget";
  if (
    budget.max_spawn_per_session != null
    || budget.max_concurrent_subagents != null
    || budget.max_subagent_spawn_depth != null
    || budget.max_fanout_per_turn != null
  ) {
    return `spawn ${formatCount(budget.max_spawn_per_session)}`
      + ` · concurrent ${formatCount(budget.max_concurrent_subagents)}`
      + ` · depth ${formatCount(budget.max_subagent_spawn_depth)}`
      + ` · fan-out ${formatCount(budget.max_fanout_per_turn)}`;
  }
  return `${formatCount(budget.tokens_used)} / ${formatCount(budget.token_limit)} tokens`
    + ` · ${formatDuration(budget.elapsed_ms)} / ${formatDuration(budget.time_limit_ms)}`;
}

function taskLabel(task: DelegationTaskProjection) {
  return task.title || task.description || task.id;
}

function CommunicationRow({ item }: { item: AgentCommunication }) {
  return (
    <article className={`agent-message agent-message-${item.kind}`}>
      <div className="agent-message-route">
        <code>{shortId(item.source_session_id)}</code>
        <span>{item.kind === "completion" ? "↩" : "→"}</span>
        <code>{shortId(item.target_session_id)}</code>
      </div>
      <div>
        <strong>{item.kind === "completion" ? "Completion receipt" : "Delegation created"}</strong>
        <p>{item.summary || "No textual summary persisted."}</p>
      </div>
      <div className="agent-message-state">
        <span className={`agent-state state-${item.delivery_state}`}>{item.delivery_state}</span>
        <small>gen {item.generation}</small>
      </div>
    </article>
  );
}

interface MultiAgentControlPlaneProps {
  onOpenChanges?: (sessionId: string) => void;
}

export function MultiAgentControlPlane({
  onOpenChanges,
}: MultiAgentControlPlaneProps = {}) {
  const activeId = useSessionStore((state) => state.activeId);
  const [snapshot, setSnapshot] = useState<MultiAgentSnapshot | null>(null);
  const [selectedId, setSelectedId] = useState("");
  const [error, setError] = useState("");
  const [loading, setLoading] = useState(false);
  const [refreshRevision, setRefreshRevision] = useState(0);
  const [busyAction, setBusyAction] = useState("");

  useEffect(() => {
    if (!activeId) {
      setSnapshot(null);
      setSelectedId("");
      return;
    }
    const controller = new AbortController();
    setLoading(true);
    setError("");
    getMultiAgentSnapshot(activeId, controller.signal)
      .then((data) => {
        setSnapshot(data);
        setSelectedId((current) => (
          data.nodes.some((node) => node.id === current)
            ? current
            : data.selected_session_id
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
  }, [activeId, refreshRevision]);

  async function runAction(key: string, action: () => Promise<unknown>) {
    setBusyAction(key);
    setError("");
    try {
      await action();
      setRefreshRevision((value) => value + 1);
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : String(reason));
    } finally {
      setBusyAction("");
    }
  }

  const nodes = useMemo(
    () => orderTopologyNodes(snapshot?.nodes || []),
    [snapshot],
  );
  const selected = nodes.find((node) => node.id === selectedId) || nodes[0];
  const context = snapshot?.contexts.find(
    (item) => item.session_id === selected?.id,
  );
  const worktree = snapshot?.worktrees.find(
    (item) => item.session_id === selected?.id,
  );
  const routing = snapshot?.routing || snapshot?.routing_decision;
  const runs = snapshot?.delegation_runs || [];
  const tasks = snapshot?.delegation_tasks || [];
  const team = snapshot?.team;
  const failedTasks = tasks.filter((task) => (
    task.status === "failed" || task.status === "budget_exhausted"
  )).length;
  const retriedTasks = tasks.filter((task) => (task.retry_count || 0) > 0).length;

  if (!activeId) {
    return (
      <SessionRequiredState
        mark="A"
        title="Select a session to inspect its agent topology"
        description="Agent scheduling, communication, context isolation, and worktree consistency are runtime facts tied to one root session."
      />
    );
  }
  if (loading && !snapshot) {
    return (
      <ViewStatePanel
        tone="loading"
        title="Reading the agent control plane"
        description="Loading persisted topology, communication, and worktree evidence."
      />
    );
  }
  if (error && !snapshot) {
    return (
      <ViewStatePanel
        tone="error"
        title="Agent coordination evidence could not be loaded"
        description={error}
      />
    );
  }
  if (!snapshot) return null;

  return (
    <div className="agent-control-plane">
      <header className="agent-control-hero">
        <div>
          <span className="agent-control-eyebrow">Persisted coordination</span>
          <h1>Multi-Agent Control Plane</h1>
          <p>
            Inspect who delegated work, where each Agent ran, what context it
            received, and whether isolated changes have converged.
          </p>
        </div>
        <div className={`agent-consistency-chip state-${snapshot.consistency.state}`}>
          <i />
          {snapshot.consistency.state}
        </div>
      </header>

      {error && <div className="agent-control-warning">{error}</div>}

      <section className="agent-control-metrics">
        <article><strong>{snapshot.scheduler.total_agents}</strong><span>agents</span></article>
        <article><strong>{snapshot.scheduler.active_agents}</strong><span>active</span></article>
        <article><strong>{snapshot.scheduler.peak_observed_parallelism}</strong><span>peak overlap</span></article>
        <article><strong>{tasks.length || snapshot.communication_summary.delegations}</strong><span>delegated tasks</span></article>
        <article><strong>{failedTasks}</strong><span>failed</span></article>
        <article><strong>{retriedTasks}</strong><span>retried</span></article>
        <article><strong>{snapshot.consistency.unresolved_worktrees}</strong><span>unresolved worktrees</span></article>
      </section>

      {team && (team.available || team.state === "recovery_required") && (
        <section className="agent-control-card agent-team-board">
          <div className="agent-section-heading">
            <div>
              <span className="agent-control-eyebrow">Explicit opt-in capability</span>
              <h2>Agent Team</h2>
            </div>
            <span className={`agent-state state-${team.state || "inactive"}`}>
              {team.state || "inactive"}
            </span>
          </div>
          <p className="agent-team-explanation">
            Teams are reserved for peer messaging and shared task ownership.
            Ordinary fan-out remains parent-mediated and does not activate this channel.
          </p>
          {team.recovery_note && (
            <div className="agent-control-warning">{team.recovery_note}</div>
          )}
          {team.approval_required && (
            <div className="agent-team-approval">
              <div>
                <strong>Approval required</strong>
                <span>
                  {team.members.length || "Proposed"} member(s),{" "}
                  {team.task_board.length || "proposed"} task(s). No teammate has started.
                </span>
              </div>
              <button
                type="button"
                disabled={Boolean(busyAction)}
                onClick={() => runAction(
                  "team-approve",
                  () => approveAgentTeam(snapshot.root_session_id),
                )}
              >
                {busyAction === "team-approve" ? "Approving…" : "Approve team"}
              </button>
              <button
                type="button"
                className="secondary"
                disabled={Boolean(busyAction)}
                onClick={() => runAction(
                  "team-reject",
                  () => rejectAgentTeam(snapshot.root_session_id),
                )}
              >
                Reject
              </button>
            </div>
          )}
          {team.members.length > 0 && (
            <div className="agent-team-members">
              {team.members.map((member) => (
                <article key={member.id}>
                  <strong>{member.id === snapshot.root_session_id ? "Lead" : member.id}</strong>
                  <span>{member.role}</span>
                  <small>{member.state}</small>
                </article>
              ))}
            </div>
          )}
          {team.active && team.task_board.length > 0 && (
            <div className="agent-team-tasks">
              {team.task_board.map((task) => {
                const teammate = team.members.find(
                  (member) => member.id !== snapshot.root_session_id,
                );
                const actionKey = `team-task:${task.id}`;
                return (
                  <article key={task.id}>
                    <div>
                      <strong>{task.goal}</strong>
                      <span>{task.status}</span>
                      {task.result_summary && <p>{task.result_summary}</p>}
                    </div>
                    {task.status === "ready" && teammate && (
                      <button
                        type="button"
                        disabled={Boolean(busyAction)}
                        onClick={() => runAction(actionKey, async () => {
                          const claim = await claimAgentTeamTask(
                            snapshot.root_session_id,
                            task.id,
                            teammate.id,
                          );
                          await executeAgentTeamTask(
                            snapshot.root_session_id,
                            task.id,
                            teammate.id,
                            claim.lease_token,
                          );
                        })}
                      >
                        {busyAction === actionKey ? "Running…" : `Run as ${teammate.id}`}
                      </button>
                    )}
                  </article>
                );
              })}
            </div>
          )}
          {team.active && (
            <div className="agent-team-shutdown">
              <span>{team.mailbox?.pending || 0} pending peer message(s)</span>
              <button
                type="button"
                className="secondary"
                disabled={Boolean(busyAction)}
                onClick={() => runAction(
                  "team-shutdown",
                  () => shutdownAgentTeam(snapshot.root_session_id, false),
                )}
              >
                Complete team
              </button>
              <button
                type="button"
                className="secondary"
                disabled={Boolean(busyAction)}
                onClick={() => runAction(
                  "team-cancel",
                  () => shutdownAgentTeam(snapshot.root_session_id, true),
                )}
              >
                Cancel team
              </button>
            </div>
          )}
        </section>
      )}

      <section className="agent-control-card agent-routing-decision">
        <div className="agent-section-heading">
          <div>
            <span className="agent-control-eyebrow">Routing decision</span>
            <h2>{routing?.topology || (nodes.length > 1 ? "delegation tree" : "single")}</h2>
          </div>
          <span className={`agent-team-capability ${team?.active ? "active" : ""}`}>
            Team {team?.active ? "active" : team?.available ? "available" : "unavailable"}
          </span>
        </div>
        <div className="agent-routing-layout">
          <div>
            <strong>{routing?.reason_code || "legacy_session_projection"}</strong>
            <p>
              {routing?.explanation
                || "This session predates persisted routing decisions; topology is reconstructed from child sessions."}
            </p>
            {routing?.downgraded_from && (
              <small>Downgraded from {routing.downgraded_from}</small>
            )}
          </div>
          <dl>
            <div><dt>Runtime limits</dt><dd>{budgetSummary(snapshot.limits)}</dd></div>
            <div>
              <dt>Agent Team</dt>
              <dd>
                {team
                  ? `${team.direct_messaging ? "direct messaging" : "lead-mediated"} · ${team.shared_task_board ? "shared board" : "no shared board"}`
                  : "Capability not reported"}
              </dd>
            </div>
            <div>
              <dt>Approval</dt>
              <dd>{team?.approval_required ? "Required before activation" : "Not required / unavailable"}</dd>
            </div>
          </dl>
        </div>
      </section>

      <div className="agent-control-grid">
        <section className="agent-control-card agent-topology">
          <div className="agent-section-heading">
            <div>
              <span className="agent-control-eyebrow">Scheduling topology</span>
              <h2>Delegation tree</h2>
            </div>
            <code>root {shortId(snapshot.root_session_id)}</code>
          </div>
          <div className="agent-topology-list">
            {nodes.map((node) => (
              <button
                type="button"
                key={node.id}
                className={selected?.id === node.id ? "selected" : ""}
                style={{ marginLeft: node.depth * 22 }}
                onClick={() => setSelectedId(node.id)}
              >
                <i className={`agent-status status-${node.status}`} />
                <span>
                  <strong>{node.agent_name}</strong>
                  <small>{node.agent_kind} · gen {node.generation}</small>
                </span>
                <em>{node.execution_placement}</em>
                <code>{shortId(node.id)}</code>
              </button>
            ))}
          </div>
          <div className="agent-placement-summary">
            {Object.entries(snapshot.scheduler.placement_counts).map(([name, count]) => (
              <span key={name}><strong>{count}</strong> {name}</span>
            ))}
          </div>
        </section>

        <aside className="agent-control-card agent-node-inspector">
          <span className="agent-control-eyebrow">Selected execution contract</span>
          <h2>{selected?.agent_name}</h2>
          <p>{selected?.title || "Untitled agent task"}</p>
          <dl>
            <div><dt>Identity</dt><dd>{selected?.agent_kind}</dd></div>
            <div><dt>Status</dt><dd>{selected?.status}</dd></div>
            <div><dt>Context</dt><dd>{selected?.context_origin}</dd></div>
            <div><dt>Boundary</dt><dd>{context?.isolation_boundary || "unknown"}</dd></div>
            <div><dt>Placement</dt><dd>{selected?.execution_placement}</dd></div>
            <div><dt>Workspace</dt><dd>{selected?.workspace_mode}</dd></div>
            <div><dt>Messages</dt><dd>{context?.message_count ?? 0}</dd></div>
            <div><dt>Token estimate</dt><dd>{context?.token_estimate ?? 0}</dd></div>
          </dl>
          <div className="agent-contract-note">
            <strong>Tool contract</strong>
            <span>{context?.tool_contract_persisted ? "persisted snapshot" : "runtime/default contract"}</span>
          </div>
          {worktree && (
            <div className={`agent-worktree-note state-${worktree.consistency_state}`}>
              <div>
                <strong>{worktree.disposition}</strong>
                <span>{worktree.changed_files.length} changed file(s) · {worktree.consistency_state}</span>
              </div>
              {onOpenChanges && (
                <button
                  type="button"
                  onClick={() => onOpenChanges(worktree.session_id)}
                >
                  {worktree.consistency_state === "needs_resolution"
                    ? "Resolve changes"
                    : "Inspect changes"}
                </button>
              )}
            </div>
          )}
        </aside>
      </div>

      <section className="agent-control-card agent-task-board">
        <div className="agent-section-heading">
          <div>
            <span className="agent-control-eyebrow">Delegation task DAG</span>
            <h2>Tasks, dependencies, and delivery requirements</h2>
          </div>
          <span>{runs.length} run(s) · {tasks.filter((task) => task.required).length} required</span>
        </div>
        {runs.length > 0 && (
          <div className="agent-run-strip">
            {runs.map((run) => (
              <article key={run.id} className={`status-${run.status}`}>
                <div>
                  <strong>{run.topology || "delegation"}</strong>
                  <code>{shortId(run.id)}</code>
                </div>
                <span>{run.status}</span>
                <small>
                  {run.completed_count || 0}/{run.required_count || 0} required · {run.retry_count || 0} retries
                </small>
                <small>{budgetSummary(run.budget)}</small>
              </article>
            ))}
          </div>
        )}
        <div className="agent-task-list">
          {tasks.map((task) => (
            <article key={task.id} className={`agent-task status-${task.status}`}>
              <div className="agent-task-status">
                <i />
                <span>{task.status}</span>
              </div>
              <div className="agent-task-copy">
                <div>
                  <strong>{taskLabel(task)}</strong>
                  <span className={task.required ? "required" : "optional"}>
                    {task.required ? "required" : "optional"}
                  </span>
                </div>
                {task.description && task.description !== task.title && <p>{task.description}</p>}
                <small>
                  {task.dependencies.length
                    ? `Depends on ${task.dependencies.map(shortId).join(", ")}`
                    : "No dependencies"}
                </small>
              </div>
              <dl>
                <div><dt>Agent</dt><dd>{task.agent_name || "unassigned"}</dd></div>
                <div><dt>Generation</dt><dd>{task.generation ?? 0}</dd></div>
                <div><dt>Retries</dt><dd>{task.retry_count || 0}/{task.max_retries ?? 0}</dd></div>
                <div><dt>Tokens</dt><dd>{formatCount(task.tokens_used)} / {formatCount(task.token_budget)}</dd></div>
              </dl>
              {(task.failure_category || task.failure_detail) && (
                <div className="agent-task-failure">
                  <strong>{task.failure_category || "failure"}</strong>
                  <span>{task.failure_detail || "No failure detail persisted."}</span>
                </div>
              )}
              <div className="agent-task-actions">
                {["failed", "cancelled", "partial", "budget_exhausted"].includes(task.status) && (
                  <button
                    type="button"
                    disabled={Boolean(busyAction)}
                    onClick={() => runAction(
                      `retry:${task.id}`,
                      () => retryDelegationTask(snapshot.root_session_id, task.id),
                    )}
                  >
                    {busyAction === `retry:${task.id}` ? "Retrying…" : "Retry"}
                  </button>
                )}
                {["queued", "running"].includes(task.status) && (
                  <button
                    type="button"
                    className="secondary"
                    disabled={Boolean(busyAction)}
                    onClick={() => runAction(
                      `cancel:${task.id}`,
                      () => cancelDelegationTask(snapshot.root_session_id, task.id),
                    )}
                  >
                    {busyAction === `cancel:${task.id}` ? "Cancelling…" : "Cancel"}
                  </button>
                )}
              </div>
            </article>
          ))}
          {!tasks.length && (
            <p className="agent-control-empty">
              No structured delegation tasks were persisted for this session.
            </p>
          )}
        </div>
      </section>

      <div className="agent-control-lower-grid">
        <section className="agent-control-card agent-communications">
          <div className="agent-section-heading">
            <div>
              <span className="agent-control-eyebrow">Durable communication</span>
              <h2>Delegation and completion receipts</h2>
            </div>
            <span>{snapshot.communication_summary.pending_delivery} pending</span>
          </div>
          <div className="agent-message-list">
            {snapshot.communications.map((item) => (
              <CommunicationRow item={item} key={item.id} />
            ))}
            {!snapshot.communications.length && (
              <p className="agent-control-empty">This root session has no child-agent communication.</p>
            )}
          </div>
        </section>

        <section className="agent-control-card agent-consistency">
          <span className="agent-control-eyebrow">Consistency gates</span>
          <h2>Runtime invariants</h2>
          <div className="agent-check-list">
            {snapshot.consistency.checks.map((check) => (
              <article key={check.id}>
                <i className={check.passed ? "passed" : "failed"}>{check.passed ? "✓" : "!"}</i>
                <div><strong>{check.label}</strong><p>{check.detail}</p></div>
              </article>
            ))}
          </div>
        </section>
      </div>

      <section className="agent-control-card agent-context-matrix">
        <div className="agent-section-heading">
          <div>
            <span className="agent-control-eyebrow">Context isolation</span>
            <h2>Per-Agent context ledger</h2>
          </div>
          <span>facts, not prompt reconstruction</span>
        </div>
        <div className="agent-context-table">
          <div><span>Agent</span><span>Origin</span><span>Boundary</span><span>Generation</span><span>Messages</span><span>Tokens est.</span></div>
          {snapshot.contexts.map((item) => (
            <div key={item.session_id}>
              <code>{item.agent_name} / {shortId(item.session_id)}</code>
              <span>{item.origin}</span>
              <span>{item.isolation_boundary}</span>
              <span>{item.generation}</span>
              <span>{item.message_count}</span>
              <span>{item.token_estimate}</span>
            </div>
          ))}
        </div>
      </section>

      <footer className="agent-control-disclosure">
        Completion communication means the persisted direct-child receipt channel.{" "}
        {snapshot.disclosure.arbitrary_agent_message_bus
          ? "This run exposes an Agent Team message bus."
          : "Ordinary subagents report through their direct parent; no arbitrary message bus is implied."}{" "}
        Peak overlap is reconstructed from session intervals; no scheduler simulation ran.
      </footer>
    </div>
  );
}
