import { useEffect, useMemo, useRef, useState } from "react";
import { useSessionStore } from "../stores/sessionStore";
import { useChatStore } from "../stores/chatStore";
import { MessageBubble } from "./MessageBubble";
import { WsEventBlock } from "./WsEventBlock";
import { ToolApprovalCard } from "./ToolApprovalCard";
import * as sessionApi from "../api/sessions";

type ComposerMenu = "closed" | "actions" | "mode" | "model" | "context" | "settings";
type ModeKey = "build" | "plan" | "explore";
type EffortKey = "low" | "medium" | "high";

interface ContextChip {
  id: string;
  label: string;
  kind: "upload" | "project";
  meta?: string;
}

const MODE_OPTIONS: Array<{ key: ModeKey; title: string; description: string; intent?: string }> = [
  { key: "build", title: "Build", description: "Implement, edit, and ship changes." },
  { key: "plan", title: "Plan", description: "Think first and generate an implementation plan.", intent: "analysis" },
  { key: "explore", title: "Explore", description: "Read the repo, inspect files, and report findings.", intent: "analysis" },
];

const MODEL_OPTIONS = [
  { key: "deepseek-v4-flash", family: "Fast", note: "Quick iteration and lower latency." },
  { key: "deepseek-v4", family: "Balanced", note: "General coding and reasoning." },
  { key: "gpt-5-codex", family: "Strong", note: "Best for long multi-step tasks." },
];

const PROJECT_FILE_SUGGESTIONS = [
  "agent/core.py",
  "entry/cli.py",
  "server/main.py",
  "server/routers/sessions.py",
  "web/src/App.tsx",
  "web/src/components/ChatView.tsx",
  "web/src/styles.css",
  ".grace/agents/build.md",
];

const SLASH_COMMANDS = [
  { key: "/build", title: "Switch to build mode", description: "Use the main implementation agent." },
  { key: "/plan", title: "Switch to plan mode", description: "Prepare a plan before execution." },
  { key: "/explore", title: "Switch to explore mode", description: "Read and inspect without editing." },
  { key: "/clear", title: "Clear local timeline", description: "Reset the current chat view." },
  { key: "/new", title: "Create a new session", description: "Open a fresh conversation." },
  { key: "/help", title: "Show composer help", description: "Insert a short cheatsheet into the draft." },
];

const HERO_CARDS = [
  {
    label: "Start",
    title: "Create a new session",
    body: "Open a fresh workspace and let the agent get to work.",
    icon: "▶",
    tone: "start",
  },
  {
    label: "Trace",
    title: "See live execution",
    body: "Follow thoughts, actions, and observations as the loop progresses.",
    icon: "◌",
    tone: "trace",
  },
  {
    label: "Review",
    title: "Approve and steer",
    body: "Review plans, approve tool actions, and guide the run with feedback.",
    icon: "✓",
    tone: "review",
  },
  {
    label: "Knowledge",
    title: "Connect context",
    body: "Mention files, attach assets, and ground the task in project knowledge.",
    icon: "▣",
    tone: "knowledge",
  },
];

const SUGGESTED_PROMPTS = [
  "Review the system architecture",
  "Add authentication with OAuth",
  "Optimize database queries",
  "Add tests for new features",
];

const COMPOSER_QUICK_TOOLS = [
  { key: "attach", icon: "⊕" },
  { key: "mention", icon: "@" },
  { key: "code", icon: "</>" },
  { key: "more", icon: "+" },
] as const;

function summarizeStatus(status?: string) {
  if (!status) return "Idle";
  if (status === "completed") return "Completed";
  if (status === "running") return "Running";
  if (status === "failed") return "Failed";
  if (status === "queued") return "Queued";
  return status;
}

function formatBytes(size: number) {
  if (size < 1024) return `${size} B`;
  if (size < 1024 * 1024) return `${(size / 1024).toFixed(1)} KB`;
  return `${(size / (1024 * 1024)).toFixed(1)} MB`;
}

function formatRuntime(createdAt?: string | null) {
  if (!createdAt) return "00:00";
  const start = new Date(createdAt).getTime();
  if (Number.isNaN(start)) return "00:00";
  const deltaSec = Math.max(0, Math.floor((Date.now() - start) / 1000));
  const min = Math.floor(deltaSec / 60);
  const sec = deltaSec % 60;
  return `${String(min).padStart(2, "0")}:${String(sec).padStart(2, "0")}`;
}

function intentForMode(mode: ModeKey) {
  return MODE_OPTIONS.find((option) => option.key === mode)?.intent;
}

function modeTitle(mode: ModeKey) {
  return MODE_OPTIONS.find((option) => option.key === mode)?.title ?? mode;
}

function ComposerPanelHeader({
  title,
  detail,
  onBack,
}: {
  title: string;
  detail?: string;
  onBack?: () => void;
}) {
  return (
    <div className="composer-panel-header">
      <div className="composer-panel-title-group">
        {onBack ? (
          <button type="button" className="composer-back-btn" onClick={onBack}>
            ←
          </button>
        ) : null}
        <div>
          <div className="composer-panel-title">{title}</div>
          {detail ? <div className="composer-panel-detail">{detail}</div> : null}
        </div>
      </div>
    </div>
  );
}

export function ChatView() {
  const { activeId, activeDetail, createSession } = useSessionStore();
  const {
    timeline,
    isRunning,
    error,
    planApproval,
    steps,
    tokens,
    sendChat,
    loadMessages,
    connectWs,
    disconnectWs,
    approvePlan,
    rejectPlan,
    toolApprovals,
    resolveToolApproval,
    clear,
  } = useChatStore();

  const fileInputRef = useRef<HTMLInputElement>(null);
  const bottomRef = useRef<HTMLDivElement>(null);
  const draftRef = useRef<HTMLTextAreaElement>(null);
  const composerRef = useRef<HTMLDivElement>(null);

  const [draft, setDraft] = useState("");
  const [composerMenu, setComposerMenu] = useState<ComposerMenu>("closed");
  const [mode, setMode] = useState<ModeKey>("build");
  const [model, setModel] = useState("deepseek-v4-flash");
  const [effort, setEffort] = useState<EffortKey>("high");
  const [thinking, setThinking] = useState(true);
  const [editAutomatically, setEditAutomatically] = useState(true);
  const [contextQuery, setContextQuery] = useState("");
  const [contextChips, setContextChips] = useState<ContextChip[]>([]);
  const [selectedSlashIndex, setSelectedSlashIndex] = useState(0);

  useEffect(() => {
    if (activeId) {
      loadMessages(activeId);
      connectWs(activeId);
      useSessionStore.getState().refreshActive();
    }
    return () => {
      disconnectWs();
    };
  }, [activeId, loadMessages, connectWs, disconnectWs]);

  useEffect(() => {
    bottomRef.current?.scrollIntoView({ behavior: "smooth" });
  }, [timeline, isRunning, error]);

  useEffect(() => {
    const nextMode = activeDetail?.agent_name;
    if (nextMode === "plan" || nextMode === "explore" || nextMode === "build") {
      setMode(nextMode);
    }
  }, [activeDetail?.agent_name]);

  useEffect(() => {
    const onPointerDown = (event: MouseEvent) => {
      if (!composerRef.current) return;
      if (!composerRef.current.contains(event.target as Node)) {
        setComposerMenu("closed");
      }
    };
    document.addEventListener("mousedown", onPointerDown);
    return () => document.removeEventListener("mousedown", onPointerDown);
  }, []);

  useEffect(() => {
    if (!draftRef.current) return;
    draftRef.current.style.height = "0px";
    const nextHeight = Math.min(draftRef.current.scrollHeight, 220);
    draftRef.current.style.height = `${Math.max(nextHeight, 96)}px`;
  }, [draft]);

  const toolResults = useMemo(() => {
    const map = new Map<string, string>();
    for (const item of timeline) {
      if (item.source === "message" && item.msg.role === "tool" && item.msg.tool_call_id) {
        map.set(item.msg.tool_call_id, item.msg.content);
      }
    }
    return map;
  }, [timeline]);

  const slashMatches = useMemo(() => {
    if (!draft.startsWith("/")) return [];
    const lower = draft.toLowerCase();
    return SLASH_COMMANDS.filter((command) => command.key.startsWith(lower));
  }, [draft]);

  useEffect(() => {
    setSelectedSlashIndex(0);
  }, [draft]);

  const filteredProjectFiles = useMemo(() => {
    const q = contextQuery.trim().toLowerCase();
    if (!q) return PROJECT_FILE_SUGGESTIONS;
    return PROJECT_FILE_SUGGESTIONS.filter((path) => path.toLowerCase().includes(q));
  }, [contextQuery]);

  const progressRatio = Math.min(100, Math.max(0, steps ? steps * 10 : isRunning ? 50 : 0));
  const runtimeLabel = formatRuntime(activeDetail?.created_at);

  const buildPrompt = () => {
    const trimmed = draft.trim();
    if (!trimmed) return "";
    if (!contextChips.length) return trimmed;
    const contextBlock = contextChips
      .map((chip) =>
        chip.kind === "project"
          ? `- project file: ${chip.label}`
          : `- attached file: ${chip.label}${chip.meta ? ` (${chip.meta})` : ""}`,
      )
      .join("\n");
    return `${trimmed}\n\nContext references:\n${contextBlock}`;
  };

  const removeContextChip = (chipId: string) => {
    setContextChips((prev) => prev.filter((chip) => chip.id !== chipId));
  };

  const addProjectFileChip = (path: string) => {
    setContextChips((prev) => {
      if (prev.some((chip) => chip.label === path && chip.kind === "project")) return prev;
      return [
        ...prev,
        { id: `${path}-${Date.now()}`, label: path, kind: "project", meta: "Project path" },
      ];
    });
    setDraft((current) => {
      const suffix = current.trim().length ? "\n" : "";
      return `${current}${suffix}Please consider ${path} as relevant context.`;
    });
    setComposerMenu("closed");
  };

  const handleAttachClick = () => {
    fileInputRef.current?.click();
  };

  const handleFileInput = (e: React.ChangeEvent<HTMLInputElement>) => {
    const files = Array.from(e.target.files || []);
    if (!files.length) return;
    const newChips = files.map((file) => ({
      id: `${file.name}-${file.size}-${Date.now()}`,
      label: file.name,
      kind: "upload" as const,
      meta: formatBytes(file.size),
    }));
    setContextChips((prev) => [...prev, ...newChips]);
    setComposerMenu("closed");
    e.target.value = "";
  };

  const handleSend = () => {
    const text = buildPrompt();
    if (!text || !activeId || isRunning) return;
    setDraft("");
    sendChat(activeId, text, intentForMode(mode));
  };

  const handleCancel = async () => {
    if (!activeId || !isRunning) return;
    try {
      await sessionApi.cancelSession(activeId, "Cancelled from web composer");
    } catch {
      // UI-only fallback
    }
  };

  const handleClearConversation = () => {
    clear();
    setDraft("");
    setContextChips([]);
    setComposerMenu("closed");
  };

  const executeSlash = async (command: string) => {
    if (command === "/clear") {
      handleClearConversation();
      return;
    }
    if (command === "/new") {
      await createSession();
      setDraft("");
      setComposerMenu("closed");
      return;
    }
    if (command === "/build") {
      setMode("build");
      setDraft("");
      setComposerMenu("closed");
      return;
    }
    if (command === "/plan") {
      setMode("plan");
      setDraft("");
      setComposerMenu("closed");
      return;
    }
    if (command === "/explore") {
      setMode("explore");
      setDraft("");
      setComposerMenu("closed");
      return;
    }
    if (command === "/help") {
      setDraft(
        "Composer shortcuts:\n/build switch to build mode\n/plan switch to plan mode\n/explore switch to explore mode\n/clear clear the local timeline\n/new create a fresh session",
      );
      setComposerMenu("closed");
    }
  };

  const handleKeyDown = async (e: React.KeyboardEvent<HTMLTextAreaElement>) => {
    if (draft.startsWith("/") && slashMatches.length) {
      if (e.key === "ArrowDown") {
        e.preventDefault();
        setSelectedSlashIndex((current) => (current + 1) % slashMatches.length);
        return;
      }
      if (e.key === "ArrowUp") {
        e.preventDefault();
        setSelectedSlashIndex((current) => (current - 1 + slashMatches.length) % slashMatches.length);
        return;
      }
      if (e.key === "Enter" && !e.shiftKey) {
        e.preventDefault();
        await executeSlash(slashMatches[selectedSlashIndex]?.key ?? slashMatches[0].key);
        return;
      }
    }

    if (e.key === "Enter" && !e.shiftKey) {
      e.preventDefault();
      handleSend();
    }

    if (e.key === "Escape") {
      setComposerMenu("closed");
    }
  };

  const openMenu = (menu: ComposerMenu) => {
    setComposerMenu((current) => (current === menu ? "closed" : menu));
  };

  const handleQuickTool = (tool: (typeof COMPOSER_QUICK_TOOLS)[number]["key"]) => {
    if (tool === "attach") {
      handleAttachClick();
      return;
    }
    if (tool === "mention") {
      openMenu("context");
      return;
    }
    if (tool === "code") {
      setDraft((current) => `${current}${current ? "\n" : ""}\`\`\`\n\n\`\`\``);
      return;
    }
    openMenu("actions");
  };

  const renderComposerMenu = () => {
    if (composerMenu === "closed") return null;

    if (composerMenu === "actions") {
      return (
        <div className="composer-panel">
          <ComposerPanelHeader
            title="Quick actions"
            detail="Common session and context actions around the composer."
          />
          <div className="composer-action-list">
            <button type="button" className="composer-action-item" onClick={() => openMenu("context")}>
              <span className="composer-action-icon">+</span>
              <span>
                <strong>Context</strong>
                <small>Attach files or mention repo paths.</small>
              </span>
            </button>
            <button type="button" className="composer-action-item" onClick={() => openMenu("mode")}>
              <span className="composer-action-icon">M</span>
              <span>
                <strong>Mode</strong>
                <small>Switch between build, plan, and explore.</small>
              </span>
            </button>
            <button type="button" className="composer-action-item" onClick={() => openMenu("model")}>
              <span className="composer-action-icon">AI</span>
              <span>
                <strong>Model</strong>
                <small>Pick the model preset for this run.</small>
              </span>
            </button>
            <button type="button" className="composer-action-item" onClick={() => openMenu("settings")}>
              <span className="composer-action-icon">S</span>
              <span>
                <strong>Runtime settings</strong>
                <small>Thinking, effort, and execution style.</small>
              </span>
            </button>
            <button type="button" className="composer-action-item" onClick={handleClearConversation}>
              <span className="composer-action-icon">C</span>
              <span>
                <strong>Clear conversation</strong>
                <small>Reset the local timeline and draft.</small>
              </span>
            </button>
          </div>
        </div>
      );
    }

    if (composerMenu === "mode") {
      return (
        <div className="composer-panel">
          <ComposerPanelHeader title="Choose mode" detail="The mode shapes the next task." onBack={() => setComposerMenu("actions")} />
          <div className="composer-option-list">
            {MODE_OPTIONS.map((option) => (
              <button
                key={option.key}
                type="button"
                className={`composer-option-card ${mode === option.key ? "active" : ""}`}
                onClick={() => {
                  setMode(option.key);
                  setComposerMenu("closed");
                }}
              >
                <div className="composer-option-topline">
                  <span>{option.title}</span>
                  {mode === option.key ? <span className="composer-option-badge">Selected</span> : null}
                </div>
                <small>{option.description}</small>
              </button>
            ))}
          </div>
        </div>
      );
    }

    if (composerMenu === "model") {
      return (
        <div className="composer-panel">
          <ComposerPanelHeader title="Switch model" detail="UI presets for the current run." onBack={() => setComposerMenu("actions")} />
          <div className="composer-option-list">
            {MODEL_OPTIONS.map((option) => (
              <button
                key={option.key}
                type="button"
                className={`composer-option-card ${model === option.key ? "active" : ""}`}
                onClick={() => {
                  setModel(option.key);
                  setComposerMenu("closed");
                }}
              >
                <div className="composer-option-topline">
                  <span>{option.key}</span>
                  <span className="composer-option-hint">{option.family}</span>
                </div>
                <small>{option.note}</small>
              </button>
            ))}
          </div>
        </div>
      );
    }

    if (composerMenu === "context") {
      return (
        <div className="composer-panel">
          <ComposerPanelHeader title="Add context" detail="Attach files or mention project paths." onBack={() => setComposerMenu("actions")} />
          <div className="composer-context-toolbar">
            <button type="button" className="btn-secondary composer-mini-btn" onClick={handleAttachClick}>
              Attach file...
            </button>
            <input
              className="composer-search-input"
              placeholder="Mention file from this project..."
              value={contextQuery}
              onChange={(e) => setContextQuery(e.target.value)}
            />
          </div>
          <div className="composer-file-list">
            {filteredProjectFiles.map((path) => (
              <button key={path} type="button" className="composer-file-item" onClick={() => addProjectFileChip(path)}>
                <span className="composer-file-path">{path}</span>
                <span className="composer-file-action">Mention</span>
              </button>
            ))}
          </div>
        </div>
      );
    }

    return (
      <div className="composer-panel">
        <ComposerPanelHeader title="Runtime settings" detail="Shape the next run." onBack={() => setComposerMenu("actions")} />
        <div className="composer-settings-list">
          <div className="composer-setting-row">
            <div>
              <div className="composer-setting-label">Thinking</div>
              <div className="composer-setting-help">Expose deeper reasoning for the next task.</div>
            </div>
            <button type="button" className={`toggle-switch ${thinking ? "on" : ""}`} onClick={() => setThinking((current) => !current)}>
              <span />
            </button>
          </div>
          <div className="composer-setting-row">
            <div>
              <div className="composer-setting-label">Edit automatically</div>
              <div className="composer-setting-help">Bias toward taking action instead of stopping early.</div>
            </div>
            <button
              type="button"
              className={`toggle-switch ${editAutomatically ? "on" : ""}`}
              onClick={() => setEditAutomatically((current) => !current)}
            >
              <span />
            </button>
          </div>
          <div className="composer-effort-group">
            <div className="composer-setting-label">Effort</div>
            <div className="composer-segmented">
              {(["low", "medium", "high"] as EffortKey[]).map((level) => (
                <button
                  key={level}
                  type="button"
                  className={`composer-segment ${effort === level ? "active" : ""}`}
                  onClick={() => setEffort(level)}
                >
                  {level}
                </button>
              ))}
            </div>
          </div>
        </div>
      </div>
    );
  };

  return (
    <>
      <div className="chat-shell">
        <div className="chat-summary-bar chat-summary-bar-rich">
          <div className="summary-card summary-card-session">
            <div className="summary-session-title-row">
              <div className="summary-session-title">
                {activeDetail?.title || activeId?.slice(0, 8) || "Session 21:02:47"}
              </div>
              <button type="button" className="summary-edit-btn" aria-label="Edit session title">
                ✎
              </button>
            </div>
            <div className="summary-subtle">
              {activeDetail?.created_at ? "Started recently" : "Create or open a session to begin"}
            </div>
          </div>

          <div className="summary-card">
            <div className="summary-label">Status</div>
            <div className="summary-status-line">
              <span className={`summary-status-dot ${isRunning ? "running" : error ? "failed" : "idle"}`} />
              <div className="summary-value">{summarizeStatus(activeDetail?.status || (isRunning ? "running" : ""))}</div>
            </div>
          </div>

          <div className="summary-card">
            <div className="summary-label">Steps</div>
            <div className="summary-value">{steps ? `${steps} / 10` : "5 / 10"}</div>
          </div>

          <div className="summary-card">
            <div className="summary-label">Tokens</div>
            <div className="summary-value">{(tokens || activeDetail?.total_tokens_estimate || 5792).toLocaleString()}</div>
          </div>

          <div className="summary-card">
            <div className="summary-label">Runtime</div>
            <div className="summary-value">{runtimeLabel}</div>
          </div>

          <div className="summary-card summary-card-progress">
            <div className="summary-label">Progress</div>
            <div className="summary-progress-row">
              <div className="summary-progress-track">
                <div className="summary-progress-fill" style={{ width: `${progressRatio}%` }} />
              </div>
              <div className="summary-progress-number">{progressRatio}%</div>
            </div>
          </div>
        </div>

        <section className="chat view active" data-view-name="chat">
          {timeline.length === 0 && (
            <div className="welcome welcome-hero">
              <div className="welcome-hero-badge">✦</div>
              <h1>Welcome to Grace Code</h1>
              <p>
                Your AI software engineer that plans, builds, and ships with clarity.
                Describe what you want to build or explore.
              </p>

              <div className="welcome-grid welcome-grid-four">
                {HERO_CARDS.map((card) => (
                  <div key={card.title} className={`welcome-card welcome-feature-card tone-${card.tone}`}>
                    <div className="welcome-feature-icon">{card.icon}</div>
                    <div className="welcome-card-title">{card.label}</div>
                    <div className="welcome-feature-subtitle">{card.title}</div>
                    <div className="welcome-card-body">{card.body}</div>
                    <div className="welcome-feature-arrow">→</div>
                  </div>
                ))}
              </div>

              <div className="welcome-suggestions">
                <div className="summary-label">Suggested Prompts</div>
                <div className="welcome-chip-row welcome-chip-grid">
                  {SUGGESTED_PROMPTS.map((prompt) => (
                    <button key={prompt} className="welcome-chip action-chip prompt-chip" type="button" onClick={() => setDraft(prompt)}>
                      <span className="prompt-chip-icon">◌</span>
                      <span>{prompt}</span>
                    </button>
                  ))}
                </div>
              </div>
            </div>
          )}

          <div id="messages">
            {timeline.map((item, i) =>
              item.source === "message" ? (
                <MessageBubble key={`m-${i}`} message={item.msg} toolResults={toolResults} />
              ) : (
                <WsEventBlock key={`ws-${i}`} event={item.ws} />
              ),
            )}

            {isRunning && (
              <div className="trace-block">
                <div className="trace-card trace-thought">
                  <div className="trace-header">
                    <div className="trace-icon">◌</div>
                    <div className="trace-title">Agent is thinking</div>
                    <div className="trace-meta">
                      <span className="trace-pill">Live</span>
                    </div>
                  </div>
                  <div className="trace-content">
                    <span className="loading-dots">Reasoning through the next move</span>
                  </div>
                </div>
              </div>
            )}

            {error && (
              <div className="trace-block">
                <div className="trace-card trace-reflection">
                  <div className="trace-header">
                    <div className="trace-icon">!</div>
                    <div className="trace-title">Runtime error</div>
                    <div className="trace-meta">
                      <span className="trace-pill">Needs action</span>
                    </div>
                  </div>
                  <div className="trace-content">{error}</div>
                </div>
              </div>
            )}
          </div>
          <div ref={bottomRef} />
        </section>
      </div>

      {Object.keys(toolApprovals).length > 0 && (
        <div style={{ padding: "0 20px", borderTop: "2px solid var(--accent)", background: "var(--bg-elev)" }}>
          {Object.values(toolApprovals).map((ta) => (
            <ToolApprovalCard
              key={ta.requestId}
              requestId={ta.requestId}
              toolName={ta.toolName}
              params={ta.params}
              thought={ta.thought}
              onApprove={(note) => resolveToolApproval(ta.requestId, "allow", { note })}
              onAlwaysAllow={(note) => resolveToolApproval(ta.requestId, "allow", { note, always: true })}
              onDeny={(note) => resolveToolApproval(ta.requestId, "deny", { note })}
            />
          ))}
        </div>
      )}

      <footer className="composer">
        {planApproval?.isWaiting ? (
          <div className="plan-actions">
            <textarea
              ref={draftRef}
              value={draft}
              placeholder="Optional feedback before approving"
              rows={1}
              autoComplete="off"
              disabled={isRunning}
              onChange={(e) => setDraft(e.target.value)}
              onKeyDown={(e) => {
                if (e.key === "Enter" && e.shiftKey) {
                  e.preventDefault();
                  rejectPlan(draft || "Request revision");
                  setDraft("");
                }
              }}
            />
            <button className="btn-approve" type="button" disabled={isRunning} onClick={() => { approvePlan(draft.trim()); setDraft(""); }}>
              Approve & Build
            </button>
            <button className="btn-reject" type="button" disabled={isRunning} onClick={() => { rejectPlan(draft.trim() || "Please revise the plan"); setDraft(""); }}>
              Reject
            </button>
          </div>
        ) : (
          <div className="composer-shell">
            <div ref={composerRef} className="composer-card composer-card-elevated">
              <input ref={fileInputRef} type="file" hidden multiple onChange={handleFileInput} />

              {renderComposerMenu()}

              {contextChips.length ? (
                <div className="composer-context-chips">
                  {contextChips.map((chip) => (
                    <div key={chip.id} className={`context-chip ${chip.kind}`}>
                      <span className="context-chip-icon">{chip.kind === "project" ? "@@" : "F"}</span>
                      <span className="context-chip-label">{chip.label}</span>
                      {chip.meta ? <span className="context-chip-meta">{chip.meta}</span> : null}
                      <button type="button" className="context-chip-remove" onClick={() => removeContextChip(chip.id)}>
                        ×
                      </button>
                    </div>
                  ))}
                </div>
              ) : null}

              <div className="composer-main">
                <textarea
                  ref={draftRef}
                  id="prompt-input"
                  placeholder="Ask Grace Code to inspect, plan, or change something..."
                  rows={1}
                  autoComplete="off"
                  value={draft}
                  disabled={isRunning || !activeId}
                  onChange={(e) => {
                    setDraft(e.target.value);
                    if (e.target.value.startsWith("/")) setComposerMenu("closed");
                  }}
                  onKeyDown={handleKeyDown}
                />

                <div className="composer-actions composer-actions-floating">
                  {isRunning ? (
                    <button className="btn-secondary composer-stop-btn" type="button" onClick={handleCancel}>
                      Stop
                    </button>
                  ) : null}
                  <div className="send-cluster">
                    <button className="btn-send composer-send-btn" type="button" disabled={isRunning || !activeId || !draft.trim()} onClick={handleSend}>
                      <span className="send-btn-icon">➤</span>
                      <span>Send</span>
                    </button>
                    <button className="composer-send-caret" type="button" disabled={isRunning || !activeId} aria-label="More send actions">
                      ▾
                    </button>
                  </div>
                </div>
              </div>

              <div className="composer-bottom-row">
                <div className="composer-bottom-left">
                  {COMPOSER_QUICK_TOOLS.map((tool) => (
                    <button key={tool.key} type="button" className="composer-tool-btn" onClick={() => handleQuickTool(tool.key)}>
                      <span className="composer-tool-icon">{tool.icon}</span>
                    </button>
                  ))}
                </div>

                <div className="composer-bottom-right">
                  <button type="button" className={`composer-chip-btn composer-bottom-pill ${composerMenu === "mode" ? "active" : ""}`} onClick={() => openMenu("mode")}>
                    {modeTitle(mode)}
                    <span className="composer-chip-caret">▾</span>
                  </button>
                  <button type="button" className={`composer-chip-btn composer-bottom-pill ${composerMenu === "model" ? "active" : ""}`} onClick={() => openMenu("model")}>
                    Model: {model}
                  </button>
                  <button type="button" className={`composer-pill composer-bottom-pill ${thinking ? "on" : ""}`} onClick={() => setThinking((current) => !current)}>
                    Thinking
                  </button>
                  <button type="button" className={`composer-pill composer-bottom-pill ${editAutomatically ? "on" : ""}`} onClick={() => setEditAutomatically((current) => !current)}>
                    Edit automatically
                  </button>
                  <button type="button" className={`composer-pill composer-bottom-pill ${composerMenu === "settings" ? "active" : ""}`} onClick={() => openMenu("settings")}>
                    Effort {effort}
                  </button>
                </div>
              </div>

              {draft.startsWith("/") && slashMatches.length ? (
                <div className="slash-menu">
                  {slashMatches.map((command, index) => (
                    <button key={command.key} type="button" className={`slash-item ${selectedSlashIndex === index ? "active" : ""}`} onClick={() => void executeSlash(command.key)}>
                      <div className="slash-item-title">{command.key}</div>
                      <div className="slash-item-body">
                        <strong>{command.title}</strong>
                        <small>{command.description}</small>
                      </div>
                    </button>
                  ))}
                </div>
              ) : null}
            </div>
          </div>
        )}

        <div className="composer-meta">
          <span>{activeDetail ? `${activeDetail.agent_name} · ${activeDetail.execution_placement || activeDetail.status}` : ""}</span>
          <span className="composer-meta-stack">
            <span>{activeDetail?.message_count != null ? `${activeDetail.message_count} msgs` : ""}</span>
            <span>{activeDetail?.total_tokens_estimate != null ? `~${activeDetail.total_tokens_estimate} tok` : ""}</span>
            <span>{modeTitle(mode)} mode</span>
            <span>Enter to send</span>
          </span>
        </div>
      </footer>
    </>
  );
}
