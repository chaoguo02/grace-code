import { Component, type ReactNode } from "react";

interface Props {
  children: ReactNode;
  fallback?: ReactNode;
}

interface State {
  hasError: boolean;
  error: Error | null;
}

export class ErrorBoundary extends Component<Props, State> {
  state: State = { hasError: false, error: null };

  static getDerivedStateFromError(error: Error): State {
    return { hasError: true, error };
  }

  componentDidCatch(error: Error, info: { componentStack?: string }) {
    console.error("React error:", error, info.componentStack || "");
  }

  render() {
    if (this.state.hasError) {
      if (this.props.fallback) return this.props.fallback;
      return (
        <div style={{
          padding: 40, color: "var(--error, #c95a64)",
          fontFamily: "monospace", fontSize: 14, lineHeight: 1.6,
        }}>
          <h2 style={{ margin: "0 0 12px" }}>React Error</h2>
          <pre style={{ whiteSpace: "pre-wrap" }}>
            {this.state.error?.message || "Unknown error"}
          </pre>
          <p style={{ color: "var(--text-dim)", marginTop: 12 }}>
            Check the browser console (F12) for details.
          </p>
        </div>
      );
    }
    return this.props.children;
  }
}
