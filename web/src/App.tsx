import { useState } from "react";
import { ChevronDown, ChevronUp, Layers, Mic, Pause, Play, SlidersHorizontal, X } from "lucide-react";
import { A2UISurfaceDeck } from "./components/a2ui/A2UISurfaceDeck";
import { HarnessBrandIcon } from "./components/BrandIcons";
import { ConnectionsSheet } from "./components/sheets/ConnectionsSheet";
import { FleetTasksSheet } from "./components/sheets/FleetTasksSheet";
import { GeminiAuroraCanvas } from "./components/stage/GeminiAuroraCanvas";
import { useQueryClient } from "@tanstack/react-query";
import {
  STATE_QUERY_KEY,
  useOrchestratorActions,
  useOrchestratorState,
  type OrchestratorState,
} from "./lib/api";
import { useGeminiLiveAudio } from "./lib/live-audio-client";

const FALLBACK_STATE: OrchestratorState = {
  default_harness: { name: "horizon", display_name: "Horizon" },
  harnesses: [
    { name: "horizon", display_name: "Horizon", is_default: true },
    { name: "antigravity", display_name: "Antigravity", is_default: false },
    { name: "claude", display_name: "Claude Code", is_default: false },
  ],
  task_counts: { running: 0, awaiting_input: 0, completed: 0, total: 0 },
  tasks: [],
  integrations: [],
  workspace_connection: {
    connected: true,
    token_preview: "ya2••••oken",
    active_surfaces_count: 5,
    surfaces: [
      { name: "google_calendar", label: "Calendar", enabled: true, ready: true },
      { name: "gmail", label: "Gmail", enabled: true, ready: true },
      { name: "google_drive", label: "Drive", enabled: true, ready: true },
      { name: "universal_search", label: "Search", enabled: true, ready: true },
    ],
  },
  secrets: [],
  workspaces: [],
  a2ui_surfaces: [],
};

function formatActivityStatus(
  status: string,
  activeToolCall: string | null,
  substatus: string
): string {
  if (activeToolCall) {
    if (activeToolCall.includes("dispatch")) return "Dispatching agent...";
    if (activeToolCall.includes("steer")) return "Executing approved plan...";
    if (activeToolCall.includes("repo") || activeToolCall.includes("workspace"))
      return "Checking workspace...";
    if (activeToolCall.includes("calendar")) return "Checking Calendar...";
    if (activeToolCall.includes("gmail")) return "Checking Gmail...";
    if (activeToolCall.includes("github")) return "Checking GitHub...";
    if (activeToolCall.includes("search")) return "Searching web...";
    return `Running ${activeToolCall}...`;
  }
  if (status === "speaking") return "Speaking...";
  if (status === "listening") return "Listening...";
  if (status === "connecting") return "Connecting...";
  if (status === "held") return "On Hold";
  return substatus;
}

export function App() {
  const qc = useQueryClient();
  const { data: serverState, isError: isHealthError } = useOrchestratorState();
  const actions = useOrchestratorActions();
  const liveAudio = useGeminiLiveAudio(() => {
    void qc.invalidateQueries({ queryKey: STATE_QUERY_KEY });
  });

  const [configOpen, setConfigOpen] = useState(false);
  const [fleetOpen, setFleetOpen] = useState(false);
  const [isStackCollapsed, setIsStackCollapsed] = useState(false);

  const state = serverState || FALLBACK_STATE;

  const awaitingCount =
    state.task_counts.awaiting_input + (state.task_counts.awaiting_approval || 0);
  const runningCount = state.task_counts.running + awaitingCount;
  const activeFleetCount = state.tasks.filter(
    (t) =>
      t.status === "running" ||
      t.status === "awaiting_input" ||
      t.status === "awaiting_approval" ||
      !t.is_stale
  ).length;
  const surfaceCount = state.a2ui_surfaces.length;
  const unreadyConnections = 0;

  const handleApprovePlan = (taskId: string, selectedOption: string) => {
    actions.steerTask.mutate({
      taskId,
      instruction: selectedOption,
      mode: "execute",
    });
    if (liveAudio.status !== "idle") {
      liveAudio.sendTextTurn(
        `Approved ${taskId} with ${selectedOption}. Execute the plan to completion.`
      );
    }
  };

  const handleSelectStarterPrompt = (prompt: string) => {
    setIsStackCollapsed(false);
    void liveAudio.startSession(prompt);
  };

  const activityLabel = formatActivityStatus(
    liveAudio.status,
    liveAudio.activeToolCall,
    liveAudio.substatus
  );

  return (
    <main className="orchestrator-viewport">
      <div className="mobile-device-shell">
        <h1
          style={{
            position: "absolute",
            width: 1,
            height: 1,
            overflow: "hidden",
            clip: "rect(0,0,0,0)",
          }}
        >
          ADK Sonar Voice Navigation and Fleet Command
        </h1>

        {/* Rounded Dark Gemini Live Stage Container */}
        <section className="gemini-stage-container" aria-label="ADK Sonar Stage">
          {/* Audio-Reactive Bottom Aurora Gradient Cloud (only visible once Live session is active) */}
          <GeminiAuroraCanvas
            energy={liveAudio.audioEnergy}
            status={liveAudio.status}
          />

          {/* 1. Top Bar: Dynamic Island Fleet Pill (toggles Stage Stack) + Configuration Button */}
          <header className="stage-top-bar">
            <div style={{ width: 36 }} />

            <div
              id="top-fleet-pill"
              className="live-fleet-pill"
              onClick={() => {
                if (surfaceCount > 0) {
                  setIsStackCollapsed((prev) => !prev);
                } else if (state.tasks.length > 0) {
                  setFleetOpen((prev) => !prev);
                } else {
                  setConfigOpen(true);
                }
              }}
              role="button"
              tabIndex={0}
              title={
                surfaceCount > 0
                  ? isStackCollapsed
                    ? "Expand active agent cards"
                    : "Collapse cards into pill"
                  : "Open Configuration"
              }
            >
              <span
                className={`status-dot ${
                  isHealthError
                    ? "coral"
                    : awaitingCount > 0
                      ? "amber"
                      : runningCount > 0
                        ? "blue"
                        : "emerald"
                }`}
              />
              {runningCount > 0 && (
                <>
                  <span>
                    {`${runningCount} ${runningCount === 1 ? "agent" : "agents"} active${
                      awaitingCount > 0 ? ` · ${awaitingCount} needs input` : ""
                    }`}
                  </span>
                  <span className="pill-divider">·</span>
                </>
              )}
              <span
                style={{
                  display: "inline-flex",
                  alignItems: "center",
                  gap: 6,
                  color: "#f1f5f9",
                  fontWeight: 600,
                }}
              >
                <HarnessBrandIcon name={state.default_harness.name} size={15} />
                {state.default_harness.name === "horizon"
                  ? "Horizon"
                  : state.default_harness.display_name}
              </span>
              {surfaceCount > 0 && (
                <span style={{ display: "inline-flex", color: "#94a3b8" }}>
                  {isStackCollapsed ? (
                    <ChevronDown size={14} />
                  ) : (
                    <ChevronUp size={14} />
                  )}
                </span>
              )}
            </div>

            <div style={{ display: "flex", gap: 8, alignItems: "center" }}>
              <button
                type="button"
                id="open-fleet-sheet-btn"
                className="top-config-btn"
                onClick={() => setFleetOpen((prev) => !prev)}
                aria-label="Open Agent Fleet Sheet"
                title="View background agent fleet"
              >
                <Layers size={16} />
                {activeFleetCount > 0 && (
                  <span className="dock-badge">{activeFleetCount}</span>
                )}
              </button>

              <button
                type="button"
                id="open-configuration-sheet-btn"
                className="top-config-btn"
                onClick={() => setConfigOpen((prev) => !prev)}
                aria-label="Open Configuration Sheet"
              >
                <SlidersHorizontal size={16} />
                {unreadyConnections > 0 && (
                  <span className="dock-badge">{unreadyConnections}</span>
                )}
              </button>
            </div>
          </header>

          {/* 2. Center Stage: Default ADK Sonar Splash (when idle) or Collapsible A2UI Agent Stack */}
          <div className="stage-middle-deck">
            <A2UISurfaceDeck
              surfaces={state.a2ui_surfaces}
              isSessionActive={liveAudio.status !== "idle"}
              isStackCollapsed={isStackCollapsed}
              activeHarnessName={
                state.default_harness.name === "horizon"
                  ? "Horizon"
                  : state.default_harness.display_name
              }
              onApprovePlan={handleApprovePlan}
              onDismissSurface={(surfaceId) =>
                actions.dismissSurface.mutate(surfaceId)
              }
              onSelectStarterPrompt={handleSelectStarterPrompt}
            />
          </div>

          {/* 3. Calm 2-Word Activity Pill Above Bottom Aurora (No Closed Captions) */}
          <div className="stage-caption-zone">
            <div className="caption-substatus" id="live-activity-pill">
              <span>{activityLabel}</span>
            </div>
          </div>

          {/* Slide-up Agent Fleet Sheet */}
          {fleetOpen && (
            <FleetTasksSheet
              state={state}
              onClose={() => setFleetOpen(false)}
              onApprovePlan={handleApprovePlan}
              onDismissTask={(taskId) =>
                actions.dismissSurface.mutate(`a2ui-${taskId}`)
              }
              onCancelTask={(taskId) => actions.cancelTask.mutate(taskId)}
              onClearHistory={() => actions.clearTasks.mutate()}
            />
          )}

          {/* Slide-up Configuration Sheet */}
          {configOpen && (
            <ConnectionsSheet
              state={state}
              onClose={() => setConfigOpen(false)}
              onSelectHarness={(harnessName) =>
                actions.setHarness.mutate(harnessName)
              }
              onToggleIntegration={(name, enabled) =>
                actions.toggleIntegration.mutate({ name, enabled })
              }
              onConfigureWorkspace={(payload) =>
                actions.configureWorkspace.mutate(payload)
              }
              onSaveSecret={(name, value) =>
                actions.putSecret.mutate({ name, value })
              }
              onImportDotenv={(content, filename) =>
                actions.importDotenv.mutate({ content, filename })
              }
            />
          )}
        </section>

        {/* 4. Pure Gemini Live Bottom Control Dock: Centered Live (when idle) or Hold (||) + End (✕) */}
        <nav className="gemini-bottom-dock" aria-label="Voice Controls">
          {liveAudio.status === "idle" ? (
            <div className="dock-control-item">
              <button
                type="button"
                id="start-live-session-btn"
                className="dock-circle-btn start-live-btn"
                onClick={() => void liveAudio.startSession()}
                aria-label="Start Gemini Live Session"
              >
                <Mic size={23} />
              </button>
              <span className="dock-label">Live</span>
            </div>
          ) : (
            <>
              <div className="dock-control-item">
                <button
                  type="button"
                  id="hold-voice-btn"
                  className={`dock-circle-btn ${
                    liveAudio.status === "held" ? "hold-active" : ""
                  }`}
                  onClick={() => liveAudio.toggleHold()}
                  aria-label={
                    liveAudio.status === "held" ? "Resume Voice" : "Hold Voice"
                  }
                >
                  {liveAudio.status === "held" ? (
                    <Play size={22} />
                  ) : (
                    <Pause size={22} />
                  )}
                </button>
                <span className="dock-label">
                  {liveAudio.status === "held" ? "Resume" : "Hold"}
                </span>
              </div>

              <div className="dock-control-item">
                <button
                  type="button"
                  id="end-voice-btn"
                  className="dock-circle-btn end-btn"
                  onClick={() => liveAudio.endSession()}
                  aria-label="End Gemini Live Session"
                >
                  <X size={24} />
                </button>
                <span className="dock-label">End</span>
              </div>
            </>
          )}
        </nav>
      </div>
    </main>
  );
}
