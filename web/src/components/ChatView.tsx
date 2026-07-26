import React, { useEffect, useMemo, useRef, useState } from "react";
import { useSessionStore } from "../stores/sessionStore";
import { selectSessionUi, useChatStore } from "../stores/chatStore";
import { ToolApprovalCard } from "./ToolApprovalCard";
import { PlanApprovalBar } from "./PlanApprovalBar";
import { SubagentDetail } from "./SubagentDetail";
import { SubagentProgress } from "./SubagentProgress";
import { MarkdownRenderer } from "./MarkdownRenderer";
import { BlocksMessage } from "./BlocksMessage";
import { RunOutcomeBar } from "./RunOutcomeBar";
import { ModeTab, getPlaceholder } from "./ModeTab";
import { KeyboardHelp } from "./KeyboardHelp";
import { apiPost } from "../api/client";
import { cancelSession, fetchSkills, updateSession } from "../api/sessions";
import { getModelCatalog } from "../api/config";
import { formatBytes, formatRuntime, runtimeSeconds } from "../utils/format";
import { summarizeStatus } from "../utils/status";
import { fuzzyFilter } from "../utils/fuzzy";

type ComposerMenu = "closed" | "actions" | "mode" | "model" | "context" | "settings";
export type ModeKey = "build" | "plan" | "explore";
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

const MODEL_FALLBACK: Array<{ key: string; family: string; note: string }> = [
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

const BUILTIN_SLASH_COMMANDS = [
  { key: "/build", title: "Switch to build mode", description: "Use the main implementation agent." },
  { key: "/plan", title: "Switch to plan mode", description: "Prepare a plan before execution." },
  { key: "/explore", title: "Switch to explore mode", description: "Read and inspect without editing." },
  { key: "/compact", title: "Compact context", description: "Compress conversation history to free up context window." },
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

const COMPOSER_QUICK_TOOLS = [
  { key: "attach", icon: "⊕" },
  { key: "mention", icon: "@" },
  { key: "code", icon: "</>" },
  { key: "more", icon: "+" },
] as const;

function intentForMode(mode: ModeKey) {
  return MODE_OPTIONS.find((option) => option.key === mode)?.intent;
}

function modeTitle(mode: ModeKey) {
  return MODE_OPTIONS.find((option) => option.key === mode)?.title ?? mode;
}

function ContextUsageBar() {
  const activeId = useSessionStore((s) => s.activeId);
  const activeDetail = useSessionStore((s) => s.activeDetail);
  const contextTotal = useChatStore((s) => selectSessionUi(s, activeId).contextTotal);

  const used = activeDetail?.total_tokens_estimate ?? 0;
  const ratio = contextTotal > 0 ? Math.min(100, Math.round((used / contextTotal) * 100)) : 0;
  const isHigh = ratio > 80;
  const isCritical = ratio > 95;
  const lastUpdated = activeDetail?.updated_at
    ? new Date(activeDetail.updated_at).toLocaleTimeString()
    : null;

  if (!activeId || !activeDetail) return null;

  return (
    <div className="summary-card summary-card-progress">
      <div className="summary-label">Context</div>
      <div className="summary-progress-row">
        <div className="summary-progress-track">
          <div
            className={`summary-progress-fill${isCritical ? " context-critical" : isHigh ? " context-high" : ""}`}
            style={{ width: `${Math.max(4, ratio)}%` }}
          />
        </div>
        <div
          className="summary-progress-number"
          style={{ color: isCritical ? "var(--error)" : isHigh ? "var(--accent)" : "var(--text)" }}
          title={lastUpdated ? `Updated ${lastUpdated}` : ""}
        >
          {used.toLocaleString()} / {(contextTotal / 1000).toFixed(0)}K
        </div>
      </div>
      {lastUpdated && (
        <div style={{ fontSize: 9, color: "var(--text-dim)", marginTop: 2 }}>
          Updated {lastUpdated}
        </div>
      )}
    </div>
  );
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
    toolApprovals,
    currentMode,
    currentModel,
    viewingChildSessionId,
    backgroundAgents,
    draft: storedDraft,
    streamingThought,
    activeTurn,
    completedTurns,
    viewMode,
    events,
  } = useChatStore((s) => selectSessionUi(s, activeId));
  const {
    sendChat,
    loadTimeline,
    connectWs,
    disconnectWs,
    approvePlan,
    rejectPlan,
    savePlan,
    abortPlan,
    resolveToolApproval,
    clear,
    switchModel,
    setViewingChild,
    compactSession,
    setDraft: setStoredDraft,
    setMode: setSessionMode,
    cycleViewMode,
    setRunning,
    handleWsEvent,
  } = useChatStore();

  const fileInputRef = useRef<HTMLInputElement>(null);
  const bottomRef = useRef<HTMLDivElement>(null);
  const draftRef = useRef<HTMLTextAreaElement>(null);
  const composerRef = useRef<HTMLDivElement>(null);

  const [draft, setLocalDraft] = useState(storedDraft);
  const latestDraftRef = useRef(draft);
  latestDraftRef.current = draft;
  const lastSyncedSessionRef = useRef<string | null>(null);  // one-time mode sync per session load

  // Sync local draft changes back to store so they survive tab switches
  const updateDraft = (value: string | ((prev: string) => string)) => {
    setLocalDraft(value);
    // Use ref to get the latest draft, avoiding stale closure
    const resolved = typeof value === "function" ? value(latestDraftRef.current) : value;
    setStoredDraft(resolved, activeId);
  };
  const [composerMenu, setComposerMenu] = useState<ComposerMenu>("closed");
  const [mode, setMode] = useState<ModeKey>("build");
  const [model, setModel] = useState("deepseek-v4-flash");
  const [effort, setEffort] = useState<EffortKey>("high");
  const [thinking, setThinking] = useState(true);
  const [editAutomatically, setEditAutomatically] = useState(true);
  const [contextQuery, setContextQuery] = useState("");
  const [contextChips, setContextChips] = useState<ContextChip[]>([]);
  const [selectedSlashIndex, setSelectedSlashIndex] = useState(0);
  const [modelOptions, setModelOptions] = useState(MODEL_FALLBACK);
  const [helpOpen, setHelpOpen] = useState(false);

  useEffect(() => {
    const controller = new AbortController();
    getModelCatalog(controller.signal)
      .then((models) => {
        if (Array.isArray(models) && models.length > 0) setModelOptions(models);
      })
      .catch(() => {});  // fallback to hardcoded list
    return () => controller.abort();
  }, []);
  const [dynamicSkills, setDynamicSkills] = useState<Array<{ key: string; title: string; description: string }>>([]);
  const [editingTitle, setEditingTitle] = useState(false);
  const [titleDraft, setTitleDraft] = useState("");

  useEffect(() => {
    const controller = new AbortController();
    fetchSkills(controller.signal).then((skills) => {
      setDynamicSkills(
        skills
          .filter((s) => s.user_invocable)
          .map((s) => ({
            key: `/${s.name}`,
            title: s.display_name || s.name,
            description: s.description || "Invoke skill",
          }))
      );
    }).catch(() => {});
    return () => controller.abort();
  }, []);

  useEffect(() => {
    const controller = new AbortController();
    if (activeId) {
      // Load the backend-composed timeline first; it is the durable source of
      // truth for messages + typed trace events + plan approval state.
      // WebSocket then appends live deltas.
      loadTimeline(activeId, controller.signal);
      connectWs(activeId);
      useSessionStore.getState().refreshActive();
    }
    return () => {
      controller.abort();
      disconnectWs();
    };
  }, [activeId]);  // stable refs — connectWs/disconnectWs excluded to avoid re-connects

  // Sync isRunning from the session detail so that a refresh during an
  // active run shows the running indicator.  The trace API filters
  // task_start, and the WS won't re-emit "running" for a session that
  // was already in-flight before the page load.
  useEffect(() => {
    if (activeId && activeDetail?.status === "running") {
      setRunning(activeId, true);
    }
  }, [activeId, activeDetail?.status, setRunning]);

  useEffect(() => {
    bottomRef.current?.scrollIntoView({ behavior: "smooth" });
  }, [timeline, isRunning, error]);

  // After round completion: run_terminal has archived activeTurn → completedTurns.
  // Do an incremental DB sync (afterSeq > 0 → merge, not replace) to verify
  // consistency between live streaming blocks and persisted DB data.
  const prevRunning = useRef(isRunning);
  useEffect(() => {
    if (prevRunning.current && !isRunning && activeId) {
      useSessionStore.getState().refreshActive();
      const lastSeq = useChatStore.getState().sessionStateById[activeId]?.lastTraceSeq ?? 0;
      if (lastSeq > 0) {
        void loadTimeline(activeId, undefined, lastSeq);
      }
    }
    prevRunning.current = isRunning;
  }, [isRunning, activeId, loadTimeline]);

  // Refresh after compaction (manual /compact or auto-compaction).
  // Compaction updates sessions.updated_at before emitting the WS event,
  // so refreshActive() sees the latest data immediately.
  useEffect(() => {
    const last = events[0];
    if (
      last?.type === "status" &&
      (last as { status?: string }).status === "compacted" &&
      activeId
    ) {
      useSessionStore.getState().refreshActive();
    }
  }, [events, activeId]);

  // One-time sync: on first load of a session, set local mode from the
  // session's persisted agent_name.  After that, temporary in-memory
  // switches (via slash command or mode menu) are authoritative.
  const nextMode = activeDetail?.agent_name;
  useEffect(() => {
    if (!activeId) { lastSyncedSessionRef.current = null; return; }
    if (nextMode && (nextMode === "plan" || nextMode === "explore" || nextMode === "build")) {
      // Only sync when the session identity changes (new session or reopen)
      if (lastSyncedSessionRef.current !== activeId) {
        lastSyncedSessionRef.current = activeId;
        setMode(nextMode);
        setSessionMode(nextMode, activeId);
      }
      }
    }, [activeDetail?.agent_name, activeId]);

  useEffect(() => {
    if (currentMode === "plan" || currentMode === "explore" || currentMode === "build") {
      setMode(currentMode);
    }
  }, [currentMode]);

  useEffect(() => {
    if (currentModel) setModel(currentModel);
  }, [currentModel]);

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

  // Global keyboard shortcuts (non-editing state only)
  useEffect(() => {
    const onKeyDown = (e: KeyboardEvent) => {
      // Skip when user is typing in an input/textarea/contenteditable
      const tag = (e.target as HTMLElement)?.tagName?.toLowerCase();
      const isEditing = tag === "input" || tag === "textarea" ||
        (e.target as HTMLElement)?.getAttribute("contenteditable") === "true";
      if (isEditing) return;

      // ? = keyboard help
      if (e.key === "?") { e.preventDefault(); setHelpOpen((v) => !v); return; }
      // Ctrl+O = cycle view mode
      if (e.ctrlKey && e.key === "o") { e.preventDefault(); cycleViewMode(activeId); return; }
      // Mod+Shift+B/P/E = switch mode
      if (e.ctrlKey && e.shiftKey) {
        if (e.key === "b") { e.preventDefault(); setMode("build"); setSessionMode("build", activeId); return; }
        if (e.key === "p") { e.preventDefault(); setMode("plan"); setSessionMode("plan", activeId); return; }
        if (e.key === "e") { e.preventDefault(); setMode("explore"); setSessionMode("explore", activeId); return; }
      }
    };
    document.addEventListener("keydown", onKeyDown);
    return () => document.removeEventListener("keydown", onKeyDown);
  }, [activeId, cycleViewMode, setSessionMode]);

  useEffect(() => {
    if (!draftRef.current) return;
    draftRef.current.style.height = "0px";
    const nextHeight = Math.min(draftRef.current.scrollHeight, 220);
    draftRef.current.style.height = `${Math.max(nextHeight, 96)}px`;
  }, [draft]);

  // Single data source for welcome/messages toggle.
  // Both read from the same blocks state — no more timeline/welcome mismatch.
  const hasContent = activeTurn !== null || completedTurns.length > 0;
  const isLoadingSession = !!activeId && !activeDetail;

  const slashMatches = useMemo(() => {
    if (!draft.startsWith("/")) return [];
    const allCommands = [...BUILTIN_SLASH_COMMANDS, ...dynamicSkills];
    const lower = draft.trimStart().split(/\s+/, 1)[0].toLowerCase();
    return allCommands.filter((command) => command.key.startsWith(lower));
  }, [draft, dynamicSkills]);

  useEffect(() => {
    setSelectedSlashIndex(0);
  }, [draft]);

  const filteredProjectFiles = useMemo(() => {
    const q = contextQuery.trim();
    if (!q) return PROJECT_FILE_SUGGESTIONS.slice(0, 8);
    return fuzzyFilter(PROJECT_FILE_SUGGESTIONS, q, (p) => p, 8);
  }, [contextQuery]);

  const runtimeLabel = formatRuntime(activeDetail?.created_at, activeDetail?.completed_at);
  const pendingApprovals = Object.keys(toolApprovals).length;
  const runtimeSec = runtimeSeconds(activeDetail?.created_at, activeDetail?.completed_at);

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
    updateDraft((current) => {
      const suffix = current.trim().length ? "\n" : "";
      return `${current}${suffix}Please consider ${path} as relevant context.`;
    });
    setComposerMenu("closed");
  };

  const updateSettings = async (settings: Record<string, unknown>) => {
    if (!activeId) return;
    try {
      await apiPost(`/api/sessions/${encodeURIComponent(activeId)}/settings`, settings);
    } catch { /* best-effort */ }
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
    updateDraft("");
    sendChat(activeId, text, intentForMode(mode));
  };

  const handleCancel = async () => {
    if (!activeId || !isRunning) return;
    try {
      const result = await cancelSession(activeId, "Cancelled from web composer");
      // If the backend confirmed cancellation, inject a synthetic status event
      // so the UI updates immediately (the real WS event may still be en route).
      // If the backend had already completed (cancelled=false), the WS
      // status event has already cleared isRunning — do NOT inject a spurious error.
      if (result.cancelled) {
        handleWsEvent({
          type: "status",
          status: "cancelled",
          message: "Cancelled from web composer",
          timestamp: new Date().toISOString(),
        });
      } else {
        // Read isRunning fresh from the store — the closure value may be
        // stale if WS events arrived during the await (I2).
        const stillRunning = selectSessionUi(useChatStore.getState(), activeId).isRunning;
        if (stillRunning) {
          handleWsEvent({
            type: "status",
            status: "failed",
            error: "Cancel request did not stop the active run",
            timestamp: new Date().toISOString(),
          });
        }
      }
    } catch {
      handleWsEvent({
        type: "status",
        status: "failed",
        error: "Cancel request failed",
        timestamp: new Date().toISOString(),
      });
    }
  };

  const handleClearConversation = () => {
    clear(activeId);
    updateDraft("");
    setContextChips([]);
    setComposerMenu("closed");
  };

  const executeSlash = async (command: string) => {
    const dynamicSkill = dynamicSkills.find((skill) => skill.key === command);
    if (dynamicSkill) {
      if (!activeId || isRunning) return;
      const argumentsText = draft.slice(command.length).trim();
      const visiblePrompt = argumentsText
        ? `${command} ${argumentsText}`
        : command;
      updateDraft("");
      setComposerMenu("closed");
      await sendChat(
        activeId,
        visiblePrompt,
        intentForMode(mode),
        {
          name: command.slice(1),
          arguments: argumentsText,
        },
      );
      return;
    }
    if (command === "/clear") {
      handleClearConversation();
      return;
    }
    if (command === "/new") {
      await createSession();
      updateDraft("");
      setComposerMenu("closed");
      return;
    }
    if (command === "/build") {
      setMode("build");
      setSessionMode("build", activeId);
      updateDraft("");
      setComposerMenu("closed");
      return;
    }
    if (command === "/plan") {
      setMode("plan");
      setSessionMode("plan", activeId);
      updateDraft("");
      setComposerMenu("closed");
      return;
    }
    if (command === "/explore") {
      setMode("explore");
      setSessionMode("explore", activeId);
      updateDraft("");
      setComposerMenu("closed");
      return;
    }
    if (command === "/compact") {
      if (!activeId) return;
      const ok = await compactSession(activeId);
      if (ok) {
        updateDraft("");
        setComposerMenu("closed");
      }
      return;
    }
    if (command === "/help") {
      updateDraft(
        "Composer shortcuts:\n/build switch to build mode\n/plan switch to plan mode\n/explore switch to explore mode\n/compact compress conversation history\n/clear clear the local timeline\n/new create a fresh session",
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
      updateDraft((current) => `${current}${current ? "\n" : ""}\`\`\`\n\n\`\`\``);
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
            <button
              type="button"
              className="composer-action-item"
              disabled={!activeId || isRunning}
              onClick={async () => {
                if (!activeId) return;
                await compactSession(activeId);
                setComposerMenu("closed");
              }}
            >
              <span className="composer-action-icon">Z</span>
              <span>
                <strong>Compact context</strong>
                <small>Compress conversation to free context window.</small>
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
                  setSessionMode(option.key, activeId);
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
      const tiers = ["Active", "Fast", "Balanced", "Strong"];
      const grouped = new Map<string, typeof modelOptions>();
      for (const opt of modelOptions) {
        const tier = opt.family || "Other";
        if (!grouped.has(tier)) grouped.set(tier, []);
        grouped.get(tier)!.push(opt);
      }
      // Ensure tiers in order
      const orderedTiers = tiers.filter((t) => grouped.has(t)).concat(
        [...grouped.keys()].filter((k) => !tiers.includes(k))
      );
      return (
        <div className="composer-panel">
          <ComposerPanelHeader title="Switch model" detail="Tiers + full model list." onBack={() => setComposerMenu("actions")} />
          <div className="composer-option-list">
            {orderedTiers.map((tier) => (
              <div key={tier}>
                <div className="composer-tier-label">{tier}</div>
                {(grouped.get(tier) || []).map((option) => (
                  <button
                    key={option.key}
                    type="button"
                    className={`composer-option-card ${model === option.key ? "active" : ""}`}
                    onClick={() => {
                      setModel(option.key);
                      switchModel(option.key, undefined, activeId);
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
            <button type="button" className={`toggle-switch ${thinking ? "on" : ""}`} onClick={() => {
              const next = !thinking;
              setThinking(next);
              updateSettings({ thinking: next });
            }}>
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
              onClick={() => {
                const next = !editAutomatically;
                setEditAutomatically(next);
                updateSettings({ permission_mode: next ? "acceptEdits" : "default" });
              }}
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
                  onClick={() => { setEffort(level); updateSettings({ effort: level }); }}
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
        <section className="chat view active" data-view-name="chat">
          {/* Compact permission bar — only visible when there are pending approvals */}
          {pendingApprovals > 0 && (
            <div style={{
              padding: "6px 20px", fontSize: 12, background: "var(--accent-soft)",
              borderBottom: "1px solid var(--border)", color: "var(--accent)",
              display: "flex", alignItems: "center", gap: 12,
            }}>
              <span>⏳ {pendingApprovals} tool approval{pendingApprovals > 1 ? "s" : ""} pending</span>
              {planApproval?.isWaiting && <span>· Plan waiting for review</span>}
            </div>
          )}

          {/* Welcome: only when no messages and session is loaded */}
          {!hasContent && !isLoadingSession && (
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
            </div>
          )}

          {/* Loading: session selected but detail not yet loaded */}
          {!hasContent && isLoadingSession && (
            <div className="welcome welcome-hero">
              <div className="welcome-hero-badge">◌</div>
              <h1>Loading session…</h1>
            </div>
          )}

          {/* Plan mode progress indicator */}
          {isRunning && mode === "plan" && (
            <div style={{
              margin: "0 20px 12px",
              padding: "10px 16px",
              background: "var(--accent-soft)",
              border: "1px solid var(--accent)",
              borderRadius: 8,
              fontSize: 13,
              display: "flex",
              alignItems: "center",
              gap: 10,
            }}>
              <span style={{
                color: "var(--accent)",
                fontSize: 14,
              }}>◎</span>
              <span style={{ color: "var(--accent)", fontWeight: 600 }}>
                Planning in progress…
              </span>
              <span style={{ color: "var(--text-muted)", fontSize: 11 }}>
                Step {steps} · {tokens.toLocaleString()} tokens
              </span>
            </div>
          )}

          <div id="messages">
            <div className="blocks-container">
              {/* Past turns from DB */}
              {completedTurns.map((turn) => (
                <React.Fragment key={turn.id}>
                  <BlocksMessage blocks={turn.userMessage.blocks} role="user" />
                  <BlocksMessage
                    blocks={turn.assistantResponse.blocks}
                    role="assistant"
                  />
                  <RunOutcomeBar
                    outcome={turn.meta.outcome}
                    steps={turn.meta.steps}
                    tokens={turn.meta.tokens}
                  />
                </React.Fragment>
              ))}
              {/* Active streaming turn */}
              {activeTurn && (
                <React.Fragment>
                  <BlocksMessage blocks={activeTurn.userMessage.blocks} role="user" />
                  <BlocksMessage
                    blocks={activeTurn.assistantResponse.blocks}
                    role="assistant"
                    metadata={activeTurn.meta.steps > 0 ? {
                      steps: activeTurn.meta.steps,
                      tokens: activeTurn.meta.tokens,
                      durationMs: activeTurn.meta.startedAt
                        ? Date.now() - activeTurn.meta.startedAt
                        : 0,
                    } : undefined}
                  />
                  <RunOutcomeBar
                    outcome={activeTurn.meta.outcome}
                    steps={activeTurn.meta.steps}
                    tokens={activeTurn.meta.tokens}
                  />
                </React.Fragment>
              )}

            {isRunning && !hasContent && (
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
                    {streamingThought ? (
                      <MarkdownRenderer content={streamingThought} />
                    ) : (
                      <span className="loading-dots">Reasoning through the next move</span>
                    )}
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
          </div>
          <div ref={bottomRef} />
        </section>
      </div>

      {Object.keys(toolApprovals).length > 0 && (
        <div className="permission-dock">
          {Object.values(toolApprovals).map((ta) => (
            <ToolApprovalCard
              key={ta.requestId}
              requestId={ta.requestId}
              toolName={ta.toolName}
              params={ta.params}
              thought={ta.thought}
              decisionReason={ta.decisionReason}
              toolUseId={ta.toolUseId}
              permissionMode={ta.permissionMode}
              riskLevel={ta.riskLevel}
              onApprove={(note) => resolveToolApproval(ta.requestId, "allow", { note })}
              onAlwaysAllow={(note) => resolveToolApproval(ta.requestId, "allow", { note, always: true })}
              onDeny={(note) => resolveToolApproval(ta.requestId, "deny", { note })}
            />
          ))}
        </div>
      )}

      <footer className="composer">
        {planApproval?.isWaiting ? (
          <PlanApprovalBar
            approval={planApproval}
            feedback={draft}
            disabled={isRunning}
            onFeedbackChange={updateDraft}
            onApprove={(feedback) => {
              approvePlan(activeId, feedback);
              updateDraft("");
            }}
            onReject={(feedback) => {
              rejectPlan(activeId, feedback);
              updateDraft("");
            }}
            onSave={() => savePlan(activeId)}
            onDiscard={() => abortPlan(activeId)}
          />
        ) : (
          <div className="composer-shell">
            <ModeTab mode={mode} onChange={(m) => { setMode(m); setSessionMode(m, activeId); }} disabled={isRunning || !activeId} />
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
                  placeholder={activeDetail?.status === "running" ? "Agent is working… (spectator mode)" : getPlaceholder(mode)}
                  rows={1}
                  autoComplete="off"
                  value={draft}
                  disabled={isRunning || !activeId || activeDetail?.status === "running"}
                  onChange={(e) => {
                    updateDraft(e.target.value);
                    if (e.target.value.startsWith("/")) setComposerMenu("closed");
                    // Detect @mention — open context panel for file selection
                    const cursorPos = e.target.selectionStart || 0;
                    const textBeforeCursor = e.target.value.slice(0, cursorPos);
                    const atMatch = textBeforeCursor.match(/@(\S*)$/);
                    if (atMatch) {
                      setContextQuery(atMatch[1]);
                      setComposerMenu("context");
                    }
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
                    <button className="btn-send composer-send-btn" type="button" disabled={isRunning || !activeId || !draft.trim() || activeDetail?.status === "running"} onClick={handleSend}>
                      <span className="send-btn-icon">➤</span>
                      <span>Send</span>
                    </button>
                    <button className="composer-send-caret" type="button" disabled={isRunning || !activeId || activeDetail?.status === "running"} aria-label="More send actions">
                      ▾
                    </button>
                  </div>
                </div>
              </div>

              <div className="composer-bottom-row">
                <div className="composer-bottom-right">
                  <button type="button" className={`composer-chip-btn composer-bottom-pill ${composerMenu === "mode" ? "active" : ""}`} onClick={() => openMenu("mode")}>
                    {modeTitle(mode)}
                    <span className="composer-chip-caret">▾</span>
                  </button>
                  <button type="button" className={`composer-chip-btn composer-bottom-pill ${composerMenu === "model" ? "active" : ""}`} onClick={() => openMenu("model")}>
                    Model: {model}
                  </button>
                  <button type="button" className={`composer-pill composer-bottom-pill ${composerMenu === "settings" ? "active" : ""}`} onClick={() => openMenu("settings")}>
                    ⋯
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

        {/* Compact inline status — one line */}
        <div className="composer-status-line">
          <span className="composer-status-title" title={activeDetail?.title || activeId || ""}>
            {activeDetail?.title || activeId?.slice(0, 8) || "Session"}
          </span>
          <span className={`composer-status-dot ${isRunning ? "busy" : error ? "error" : ""}`} />
          <span>{isRunning ? "Running" : error ? "Error" : "Idle"}</span>
          <span className="composer-status-sep">·</span>
          <span>{steps || activeDetail?.message_count || 0} steps</span>
          <span className="composer-status-sep">·</span>
          <span>~{(tokens || activeDetail?.total_tokens_estimate || 0).toLocaleString()} tok</span>
          <span className="composer-status-sep">·</span>
          <button
            type="button"
            className="composer-status-viewmode"
            onClick={() => cycleViewMode(activeId)}
            title={`View: ${viewMode} (Ctrl+O to cycle)`}
          >
            {viewMode === "verbose" ? "≡" : viewMode === "summary" ? "···" : "□"}
          </button>
          <span className="composer-status-sep">·</span>
          <span>{runtimeLabel}</span>
          {runtimeSec > 0 && <span className="composer-status-sep">·</span>}
          {runtimeSec > 0 && <span>{runtimeSec}s</span>}
        </div>
      </footer>

      {/* Subagent detail overlay */}
      {viewingChildSessionId && (
        <SubagentDetail
          childSessionId={viewingChildSessionId}
          onClose={() => setViewingChild(null, activeId)}
        />
      )}

      {/* Background subagent progress */}
      <SubagentProgress
        agents={Object.values(backgroundAgents)}
        onViewChild={(childId) => setViewingChild(childId, activeId)}
      />

      {/* Keyboard shortcut help */}
      {helpOpen && <KeyboardHelp onClose={() => setHelpOpen(false)} />}
    </>
  );
}
