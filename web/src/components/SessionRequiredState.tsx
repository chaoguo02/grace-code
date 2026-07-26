import { useSessionStore } from "../stores/sessionStore";

interface SessionRequiredStateProps {
  mark: string;
  title: string;
  description: string;
}

function formatActivity(value?: string) {
  if (!value) return "No activity recorded";
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return value;
  return date.toLocaleString([], {
    month: "short",
    day: "numeric",
    hour: "2-digit",
    minute: "2-digit",
  });
}

export function SessionRequiredState({
  mark,
  title,
  description,
}: SessionRequiredStateProps) {
  const sessions = useSessionStore((state) => state.sessions);
  const openSession = useSessionStore((state) => state.openSession);
  const recent = [...sessions]
    .sort((left, right) => right.updated_at.localeCompare(left.updated_at))
    .slice(0, 4);

  return (
    <section className="session-required-state">
      <div className="session-required-mark">{mark}</div>
      <div className="session-required-copy">
        <span className="summary-label">Session scope required</span>
        <h2>{title}</h2>
        <p>{description}</p>
      </div>
      {recent.length > 0 && (
        <div className="session-required-list" aria-label="Recent sessions">
          <span>Continue with a recent session</span>
          {recent.map((session) => (
            <button
              type="button"
              key={session.id}
              onClick={() => void openSession(session.id)}
            >
              <i className={`status-${session.status}`} />
              <span>
                <strong>{session.title || session.agent_name}</strong>
                <small>{session.agent_name} · {formatActivity(session.updated_at)}</small>
              </span>
              <code>{session.id.slice(0, 8)}</code>
            </button>
          ))}
        </div>
      )}
      <small className="session-required-hint">
        You can also choose any session from the navigator.
      </small>
    </section>
  );
}
