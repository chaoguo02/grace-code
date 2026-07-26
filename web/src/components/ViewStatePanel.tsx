import type { ReactNode } from "react";

export type ViewStateTone = "loading" | "empty" | "error" | "partial";

interface ViewStatePanelProps {
  tone: ViewStateTone;
  mark?: string;
  title: string;
  description?: string;
  action?: ReactNode;
}

export function ViewStatePanel({
  tone,
  mark,
  title,
  description,
  action,
}: ViewStatePanelProps) {
  return (
    <section
      className={`view-state-panel view-state-${tone}`}
      role={tone === "error" ? "alert" : "status"}
      aria-live={tone === "loading" ? "polite" : undefined}
    >
      <div className="view-state-mark" aria-hidden="true">
        {tone === "loading" ? <i /> : mark || (tone === "error" ? "!" : "—")}
      </div>
      <div>
        <span className="summary-label">
          {tone === "loading" ? "Loading" : tone}
        </span>
        <h2>{title}</h2>
        {description && <p>{description}</p>}
      </div>
      {action && <div className="view-state-action">{action}</div>}
    </section>
  );
}
