import { useEffect, useMemo, useState } from "react";
import { getProjectOverview } from "../api/overview";
import { useSessionStore } from "../stores/sessionStore";
import type {
  DemoJourney,
  OverviewCapability,
  OverviewRoute,
  ProjectOverview as ProjectOverviewData,
} from "../types/overview";
import { ViewStatePanel } from "./ViewStatePanel";

export function evidenceLabel(state: string) {
  if (state === "observed") return "Observed evidence";
  if (state === "configured") return "Configured";
  return "Evidence unavailable";
}

function formatPercent(value: number | null) {
  return value == null ? "—" : `${(value * 100).toFixed(1)}%`;
}

function formatDuration(value: number | null) {
  if (value == null) return "—";
  if (value < 1000) return `${Math.round(value)}ms`;
  if (value < 60_000) return `${(value / 1000).toFixed(1)}s`;
  return `${(value / 60_000).toFixed(1)}m`;
}

function formatDate(value: string) {
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return value || "Unknown";
  return date.toLocaleString([], {
    month: "short",
    day: "numeric",
    hour: "2-digit",
    minute: "2-digit",
  });
}

function CapabilityCard({
  capability,
  onNavigate,
}: {
  capability: OverviewCapability;
  onNavigate: (view: OverviewRoute) => void;
}) {
  return (
    <article className={`overview-capability evidence-${capability.evidence_state}`}>
      <div className="overview-capability-head">
        <i>{capability.label.slice(0, 1)}</i>
        <span>{evidenceLabel(capability.evidence_state)}</span>
      </div>
      <h3>{capability.label}</h3>
      <p>{capability.claim}</p>
      <button type="button" onClick={() => onNavigate(capability.evidence_route)}>
        <span>{capability.evidence}</span>
        <b>Open evidence →</b>
      </button>
    </article>
  );
}

function JourneyCard({
  journey,
  onNavigate,
}: {
  journey: DemoJourney;
  onNavigate: (view: OverviewRoute) => void;
}) {
  return (
    <article className="overview-journey">
      <div className="overview-journey-number">{journey.number}</div>
      <div className="overview-journey-title">
        <div>
          <span className={`overview-readiness readiness-${journey.readiness}`}>
            {journey.readiness.replace(/_/g, " ")}
          </span>
          <small>{journey.duration_minutes} min</small>
        </div>
        <h3>{journey.title}</h3>
        <p>{journey.goal}</p>
      </div>
      <ol>
        {journey.steps.map((step, index) => (
          <li key={`${step.route}-${index}`}>
            <button type="button" onClick={() => onNavigate(step.route)}>
              <i>{index + 1}</i>
              <span><strong>{step.label}</strong><small>{step.proof}</small></span>
              <b>→</b>
            </button>
          </li>
        ))}
      </ol>
    </article>
  );
}

export function ProjectOverview({
  onNavigate,
}: {
  onNavigate: (view: OverviewRoute) => void;
}) {
  const activeId = useSessionStore((state) => state.activeId);
  const openSession = useSessionStore((state) => state.openSession);
  const [overview, setOverview] = useState<ProjectOverviewData | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState("");

  useEffect(() => {
    const controller = new AbortController();
    setLoading(true);
    setError("");
    getProjectOverview(activeId, controller.signal)
      .then(setOverview)
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

  const coveragePercent = useMemo(() => {
    if (!overview?.evidence_coverage.total) return 0;
    return Math.round(
      (overview.evidence_coverage.observed
        / overview.evidence_coverage.total) * 100,
    );
  }, [overview]);

  const openRecent = async (sessionId: string, route: OverviewRoute) => {
    await openSession(sessionId);
    onNavigate(route);
  };

  if (loading && !overview) {
    return (
      <ViewStatePanel
        tone="loading"
        title="Composing the project evidence map"
        description="Connecting runtime, control, and quality signals."
      />
    );
  }
  if (error && !overview) {
    return (
      <ViewStatePanel
        tone="error"
        title="Project evidence could not be loaded"
        description={error}
      />
    );
  }
  if (!overview) return null;

  const headline = overview.headline;
  const signals = overview.signals;
  return (
    <div className="project-overview">
      <header className="overview-hero">
        <div className="overview-hero-copy">
          <span className="overview-eyebrow">Engineering evidence map</span>
          <h1>{overview.project.product_name}</h1>
          <p>{overview.project.tagline}</p>
          <div className="overview-hero-actions">
            <button type="button" className="primary" onClick={() => onNavigate("chat")}>
              {activeId ? "Continue selected session" : "Start a live session"}
            </button>
            <button type="button" onClick={() => onNavigate("architecture")}>
              Explain the architecture
            </button>
          </div>
          <div className="overview-runtime-line">
            <i />
            <span>{overview.project.provider} / {overview.project.model}</span>
            <code>{overview.project.name}</code>
          </div>
        </div>
        <aside className="overview-evidence-score">
          <div
            className="overview-score-ring"
            style={{ "--overview-score": `${coveragePercent * 3.6}deg` } as React.CSSProperties}
          >
            <span><strong>{overview.evidence_coverage.observed}</strong>/{overview.evidence_coverage.total}</span>
          </div>
          <div>
            <span>Observed capability evidence</span>
            <strong>{overview.evidence_coverage.state.replace(/_/g, " ")}</strong>
            <small>
              {overview.evidence_coverage.configured} configured · {overview.evidence_coverage.unavailable} unavailable
            </small>
          </div>
        </aside>
      </header>

      {error && <div className="overview-warning">{error}</div>}
      {overview.section_errors.length > 0 && (
        <section className="overview-degraded">
          <strong>Partial evidence</strong>
          {overview.section_errors.map((item) => (
            <span key={item.section}>{item.section}: {item.message}</span>
          ))}
        </section>
      )}

      <section className="overview-headline">
        <article><strong>{headline.configured_agents}</strong><span>Agent definitions</span></article>
        <article><strong>{headline.registered_tools}</strong><span>Typed tools</span></article>
        <article><strong>{headline.persisted_runs_30d}</strong><span>Runs / 30d</span></article>
        <article><strong>{formatPercent(headline.run_success_rate)}</strong><span>Run success</span></article>
        <article><strong>{formatPercent(headline.evaluation_pass_rate)}</strong><span>Evaluation pass</span></article>
      </section>

      <section className="overview-section overview-capabilities">
        <div className="overview-section-heading">
          <div>
            <span className="overview-eyebrow">Claim → persisted proof</span>
            <h2>What the system can demonstrate</h2>
          </div>
          <p>Configured means the capability exists. Observed means this project has runtime evidence.</p>
        </div>
        <div className="overview-capability-grid">
          {overview.capabilities.map((capability) => (
            <CapabilityCard
              capability={capability}
              onNavigate={onNavigate}
              key={capability.id}
            />
          ))}
        </div>
      </section>

      <section className="overview-section overview-demo">
        <div className="overview-section-heading">
          <div>
            <span className="overview-eyebrow">Reusable interview flow</span>
            <h2>Three evidence-led demos</h2>
          </div>
          <p>Each step opens the exact page that supports the spoken claim.</p>
        </div>
        <div className="overview-journey-grid">
          {overview.journeys.map((journey) => (
            <JourneyCard
              journey={journey}
              onNavigate={onNavigate}
              key={journey.id}
            />
          ))}
        </div>
      </section>

      <div className="overview-lower-grid">
        <section className="overview-panel overview-signals">
          <div className="overview-section-heading compact">
            <div>
              <span className="overview-eyebrow">Current evidence</span>
              <h2>System signals</h2>
            </div>
          </div>
          <div className="overview-signal-grid">
            <button type="button" onClick={() => onNavigate("reliability")}>
              <span>Operations</span>
              <strong>{formatPercent(signals.reliability.success_rate)}</strong>
              <small>P95 {formatDuration(signals.reliability.duration_p95_ms)} · {signals.reliability.terminal_runs} terminal</small>
            </button>
            <button type="button" onClick={() => onNavigate("evaluations")}>
              <span>Regression</span>
              <strong>{formatPercent(signals.evaluation.latest_pass_rate)}</strong>
              <small>{signals.evaluation.run_count} artifact runs · {signals.evaluation.regression_count} regressions</small>
            </button>
            <button type="button" onClick={() => onNavigate("safety")}>
              <span>Authority</span>
              <strong>{signals.safety.layers} layers</strong>
              <small>{signals.safety.rules} rules · {signals.safety.session_approvals} selected approvals</small>
            </button>
            <button type="button" onClick={() => onNavigate("agents")}>
              <span>Coordination</span>
              <strong>{signals.multi_agent.available_for_selected_session ? `${signals.multi_agent.agents} agents` : "Select session"}</strong>
              <small>{signals.multi_agent.consistency || "No selected topology"}</small>
            </button>
            <button type="button" onClick={() => onNavigate("replay")}>
              <span>Replay</span>
              <strong>{signals.replay.available_for_selected_session ? `${signals.replay.contracts} contracts` : "Select session"}</strong>
              <small>{signals.replay.valid} valid · {signals.replay.runs} runs</small>
            </button>
            <button type="button" onClick={() => onNavigate("architecture")}>
              <span>Capability surface</span>
              <strong>{headline.registered_tools} tools</strong>
              <small>{headline.skills} skills · {headline.mcp_servers} MCP servers</small>
            </button>
          </div>
        </section>

        <section className="overview-panel overview-recent">
          <div className="overview-section-heading compact">
            <div>
              <span className="overview-eyebrow">Fast demo selection</span>
              <h2>Recent sessions</h2>
            </div>
            <span>{overview.recent_sessions.length}</span>
          </div>
          <div className="overview-session-list">
            {overview.recent_sessions.map((session) => (
              <article className={session.selected ? "selected" : ""} key={session.id}>
                <i className={`status-${session.status}`} />
                <button type="button" onClick={() => openRecent(session.id, "chat")}>
                  <strong>{session.title}</strong>
                  <span>{session.agent_name} · {session.message_count} messages · {formatDate(session.updated_at)}</span>
                </button>
                <button type="button" onClick={() => openRecent(session.id, "runs")}>Inspect</button>
              </article>
            ))}
            {!overview.recent_sessions.length && (
              <p>No session evidence yet. Start in Chat, then return here.</p>
            )}
          </div>
        </section>
      </div>

      <footer className="overview-disclosure">
        Overview is read-only and composes existing evidence services.
        Capability availability is not presented as runtime success, and missing
        evidence is not silently converted into a pass.
      </footer>
    </div>
  );
}
