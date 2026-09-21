import { useState } from "react";
import {
  Calendar,
  Check,
  CheckCircle2,
  ChevronDown,
  ChevronRight,
  Circle,
  Compass,
  FileCode2,
  Loader2,
  Terminal,
} from "lucide-react";
import type { A2UISurface, TaskMilestone } from "../../lib/api";
import {
  HarnessBrandIcon,
  IntegrationBrandIcon,
  SonarBrandEmblem,
} from "../BrandIcons";

interface A2UISurfaceDeckProps {
  surfaces: A2UISurface[];
  isSessionActive: boolean;
  isStackCollapsed: boolean;
  activeHarnessName: string;
  onApprovePlan: (taskId: string, selectedOption: string) => void;
  onDismissSurface: (surfaceId: string) => void;
  onSelectStarterPrompt: (prompt: string, isPlanDemo?: boolean) => void;
}

const SUGGESTIONS = [
  {
    category: "Coding",
    prompt: "Check the comments on my open PR and plan a fix",
    icon: Terminal,
    accent: "#60a5fa",
    isPlanDemo: true,
  },
  {
    category: "Productivity",
    prompt: "Check my calendar this afternoon and unread emails",
    icon: Calendar,
    accent: "#34d399",
    isPlanDemo: false,
  },
  {
    category: "Answers",
    prompt: "Check the latest game scores for me",
    icon: Compass,
    accent: "#fbbf24",
    isPlanDemo: false,
  },
];

export function A2UISurfaceDeck({
  surfaces,
  isSessionActive,
  isStackCollapsed,
  onApprovePlan,
  onDismissSurface,
  onSelectStarterPrompt,
}: A2UISurfaceDeckProps) {
  const [selectedIdx, setSelectedIdx] = useState(0);
  const [selectedOptions, setSelectedOptions] = useState<Record<string, string>>({});

  // When no A2UI surface is active:
  // - If Live session is active, keep stage open negative space above the bottom aurora
  // - If idle, show the ADK Sonar Hero Brand Lockup + 3 Centered Pillar Suggestions
  if (surfaces.length === 0) {
    if (isSessionActive) {
      return null;
    }
    return (
      <div
        data-testid="starter-suggestions-centered"
        style={{
          display: "flex",
          flexDirection: "column",
          justifyContent: "center",
          gap: 18,
          width: "100%",
        }}
      >
        {/* ADK Sonar Hero Lockup (Logo stacked above Wordmark) */}
        <div
          style={{
            display: "flex",
            flexDirection: "column",
            alignItems: "center",
            textAlign: "center",
            gap: 8,
          }}
        >
          <SonarBrandEmblem size={56} />
          <div
            style={{
              fontSize: 23,
              fontWeight: 700,
              letterSpacing: "-0.025em",
              color: "#f8fafc",
              marginTop: 2,
            }}
          >
            ADK <span style={{ color: "#93c5fd", fontWeight: 600 }}>Sonar</span>
          </div>
          <p
            style={{
              fontSize: 13,
              color: "var(--text-secondary)",
              maxWidth: 265,
              lineHeight: 1.4,
            }}
          >
            A live voice agent for work in motion.
          </p>
        </div>

        {/* 3 Core Pillar Suggestions */}
        <div style={{ display: "flex", flexDirection: "column", gap: 9 }}>
          {SUGGESTIONS.map((item) => {
            const Icon = item.icon;
            return (
              <button
                key={item.category}
                type="button"
                onClick={() => onSelectStarterPrompt(item.prompt, item.isPlanDemo)}
                style={{
                  display: "flex",
                  alignItems: "center",
                  gap: 13,
                  width: "100%",
                  padding: "12px 14px",
                  borderRadius: 18,
                  background: "rgba(255, 255, 255, 0.035)",
                  border: "1px solid rgba(255, 255, 255, 0.08)",
                  color: "var(--text-primary)",
                  textAlign: "left",
                  cursor: "pointer",
                  backdropFilter: "blur(16px)",
                  WebkitBackdropFilter: "blur(16px)",
                  transition: "all 0.16s ease",
                }}
              >
                <div
                  style={{
                    width: 34,
                    height: 34,
                    borderRadius: 10,
                    background: "rgba(255, 255, 255, 0.05)",
                    border: "1px solid rgba(255, 255, 255, 0.07)",
                    display: "flex",
                    alignItems: "center",
                    justifyContent: "center",
                    flexShrink: 0,
                    color: item.accent,
                  }}
                >
                  <Icon size={16} />
                </div>

                <div style={{ display: "flex", flexDirection: "column", gap: 2 }}>
                  <span
                    style={{
                      fontSize: 10.5,
                      fontWeight: 600,
                      textTransform: "uppercase",
                      letterSpacing: "0.05em",
                      color: "var(--text-muted)",
                    }}
                  >
                    {item.category}
                  </span>
                  <span
                    style={{
                      fontSize: 13.5,
                      fontWeight: 500,
                      lineHeight: 1.35,
                      color: "rgba(244, 246, 251, 0.92)",
                    }}
                  >
                    {item.prompt}
                  </span>
                </div>
              </button>
            );
          })}
        </div>
      </div>
    );
  }

  // If the user tapped the top Dynamic Island pill to collapse the stack, keep the stage open
  if (isStackCollapsed) {
    return null;
  }

  const safeIdx = Math.min(selectedIdx, surfaces.length - 1);
  const activeSurface = surfaces[safeIdx];
  const secondarySurfaces = surfaces
    .map((s, idx) => ({ surface: s, idx }))
    .filter((item) => item.idx !== safeIdx);

  const options: string[] =
    activeSurface.dataModel?.plan?.options || [
      "Rotate JWT via Redis TTL (Recommended)",
      "Stateless JWKS endpoint with 15m grace window",
    ];
  const planSteps: string[] = activeSurface.dataModel?.plan?.steps || [];
  const currentChoice =
    selectedOptions[activeSurface.surfaceId] ||
    activeSurface.dataModel?.plan?.selectedOption ||
    options[0];
  const milestones: TaskMilestone[] =
    activeSurface.dataModel?.trajectory?.milestones ||
    activeSurface.dataModel?.outcome?.milestones ||
    [];
  const outcomeFiles: string[] =
    activeSurface.dataModel?.outcome?.files || [];
  const contextBullets: string[] =
    activeSurface.dataModel?.context?.bullets || [];

  return (
    <div style={{ display: "flex", flexDirection: "column", gap: 10, width: "100%" }}>
      {/* Primary Active A2UI Card in the Stack */}
      <div className="a2ui-card" data-testid="a2ui-surface-card">
        <div className="a2ui-card-header">
          <span className="a2ui-eyebrow" style={{ gap: 7 }}>
            {activeSurface.kind === "context_card" ? (
              <IntegrationBrandIcon
                name={activeSurface.harness || "google_search"}
                size={16}
              />
            ) : (
              <HarnessBrandIcon name={activeSurface.harness || "horizon"} size={16} />
            )}
            <span>{activeSurface.title}</span>
          </span>
          <span
            style={{
              fontSize: 11,
              fontWeight: 600,
              padding: "3px 8px",
              borderRadius: 99,
              background: "rgba(255, 255, 255, 0.06)",
              color: "var(--text-secondary)",
            }}
          >
            {activeSurface.repo || "auth-svc"}
          </span>
        </div>

        {/* 0. CONTEXT CARD (Google Search Grounding, Maps Places, Workspace Repos) */}
        {activeSurface.kind === "context_card" && (
          <>
            <p
              className="a2ui-subtitle"
              style={{ color: "#f8fafc", fontWeight: 600, fontSize: 14 }}
            >
              “{activeSurface.subtitle}”
            </p>

            <div
              style={{
                display: "flex",
                flexDirection: "column",
                gap: 8,
                padding: "12px 14px",
                borderRadius: 14,
                background: "rgba(12, 15, 22, 0.52)",
                border: "1px solid rgba(255, 255, 255, 0.07)",
              }}
            >
              {contextBullets.map((item, i) => (
                <div
                  key={i}
                  style={{
                    display: "flex",
                    alignItems: "flex-start",
                    gap: 9,
                    fontSize: 13,
                    lineHeight: 1.4,
                    color: "#e2e8f0",
                  }}
                >
                  <CheckCircle2
                    size={15}
                    color="#60a5fa"
                    style={{ flexShrink: 0, marginTop: 2 }}
                  />
                  <span>{item}</span>
                </div>
              ))}
            </div>

            <button
              type="button"
              className="a2ui-option-pill"
              style={{ justifyContent: "center", fontWeight: 600 }}
              onClick={() => onDismissSurface(activeSurface.surfaceId)}
            >
              Dismiss
            </button>
          </>
        )}

        {/* 1. PLAN APPROVAL CARD (Human Checkpoint) */}
        {activeSurface.kind === "plan_approval" && (
          <>
            {planSteps.length > 0 && (
              <div
                style={{
                  display: "flex",
                  flexDirection: "column",
                  gap: 5,
                  padding: "10px 12px",
                  borderRadius: 13,
                  background: "rgba(12, 15, 22, 0.48)",
                  border: "1px solid rgba(255, 255, 255, 0.06)",
                }}
              >
                <div
                  style={{
                    fontSize: 10.5,
                    fontWeight: 600,
                    textTransform: "uppercase",
                    letterSpacing: "0.05em",
                    color: "var(--text-muted)",
                    marginBottom: 2,
                  }}
                >
                  Proposed Plan
                </div>
                {planSteps.map((step, i) => (
                  <div
                    key={i}
                    style={{
                      fontSize: 12,
                      color: "var(--text-secondary)",
                      display: "flex",
                      gap: 7,
                      lineHeight: 1.35,
                    }}
                  >
                    <span style={{ color: "#60a5fa", fontWeight: 600 }}>
                      {i + 1}.
                    </span>
                    <span>{step}</span>
                  </div>
                ))}
              </div>
            )}

            <div className="a2ui-options-list">
              {options.map((optionText) => {
                const isSelected = currentChoice === optionText;
                return (
                  <button
                    key={optionText}
                    type="button"
                    className={`a2ui-option-pill ${isSelected ? "selected" : ""}`}
                    onClick={() =>
                      setSelectedOptions((prev) => ({
                        ...prev,
                        [activeSurface.surfaceId]: optionText,
                      }))
                    }
                  >
                    <span>{optionText}</span>
                    {isSelected && <Check size={15} color="#93c5fd" />}
                  </button>
                );
              })}
            </div>

            <button
              type="button"
              id="approve-execute-plan-btn"
              className="a2ui-primary-btn"
              onClick={() =>
                onApprovePlan(activeSurface.taskId || "task-1", currentChoice)
              }
            >
              Approve &amp; Execute Plan
            </button>
          </>
        )}

        {/* 2. LIVE TRAJECTORY CARD (Autonomous Execution in Progress) */}
        {activeSurface.kind === "task_trajectory" && (
          <>
            <p className="a2ui-subtitle" style={{ color: "#e2e8f0", fontWeight: 500 }}>
              {activeSurface.subtitle}
            </p>

            <div
              style={{
                display: "flex",
                flexDirection: "column",
                gap: 10,
                padding: "12px 14px",
                borderRadius: 14,
                background: "rgba(12, 15, 22, 0.52)",
                border: "1px solid rgba(255, 255, 255, 0.07)",
              }}
            >
              {milestones.map((m, i) => (
                <div
                  key={i}
                  style={{
                    display: "flex",
                    alignItems: "center",
                    gap: 10,
                    fontSize: 12.5,
                    color:
                      m.state === "active"
                        ? "#f8fafc"
                        : m.state === "done"
                          ? "#94a3b8"
                          : "#64748b",
                    fontWeight: m.state === "active" ? 600 : 400,
                  }}
                >
                  {m.state === "done" ? (
                    <CheckCircle2 size={15} color="#10b981" style={{ flexShrink: 0 }} />
                  ) : m.state === "active" ? (
                    <Loader2
                      size={15}
                      color="#38bdf8"
                      style={{ flexShrink: 0, animation: "spin 1.4s linear infinite" }}
                    />
                  ) : (
                    <Circle size={14} color="#475569" style={{ flexShrink: 0 }} />
                  )}
                  <span>{m.label}</span>
                </div>
              ))}
            </div>
          </>
        )}

        {/* 3. EXECUTIVE OUTCOME CARD (Diff-Free Completion Summary) */}
        {activeSurface.kind === "task_outcome" && (
          <>
            <p className="a2ui-subtitle" style={{ color: "#f1f5f9" }}>
              {activeSurface.subtitle}
            </p>

            <div
              style={{
                display: "flex",
                alignItems: "center",
                gap: 8,
                padding: "9px 12px",
                borderRadius: 12,
                background: "rgba(16, 185, 129, 0.12)",
                border: "1px solid rgba(16, 185, 129, 0.3)",
                color: "#6ee7b7",
                fontSize: 12,
                fontWeight: 600,
              }}
            >
              <CheckCircle2 size={15} />
              <span>All tests passed · Changes applied automatically</span>
            </div>

            {outcomeFiles.length > 0 && (
              <div style={{ display: "flex", flexWrap: "wrap", gap: 6 }}>
                {outcomeFiles.map((file) => (
                  <span
                    key={file}
                    style={{
                      display: "inline-flex",
                      alignItems: "center",
                      gap: 5,
                      padding: "4px 9px",
                      borderRadius: 8,
                      background: "rgba(255, 255, 255, 0.05)",
                      border: "1px solid rgba(255, 255, 255, 0.08)",
                      fontSize: 11,
                      fontFamily: "var(--font-mono)",
                      color: "var(--text-secondary)",
                    }}
                  >
                    <FileCode2 size={12} color="#93c5fd" />
                    {file}
                  </span>
                ))}
              </div>
            )}

            <button
              type="button"
              className="a2ui-option-pill"
              style={{ justifyContent: "center", fontWeight: 600 }}
              onClick={() => onDismissSurface(activeSurface.surfaceId)}
            >
              Done
            </button>
          </>
        )}
      </div>

      {/* Collapsible iOS Notification-Style Stack Strips for Concurrent Agents */}
      {secondarySurfaces.length > 0 && (
        <div style={{ display: "flex", flexDirection: "column", gap: 6 }}>
          {secondarySurfaces.map(({ surface, idx }) => {
            const isPlan = surface.kind === "plan_approval";
            const isRunning = surface.kind === "task_trajectory";
            const activeMilestone =
              surface.dataModel?.trajectory?.milestones?.find(
                (m: TaskMilestone) => m.state === "active"
              )?.label || surface.subtitle;

            return (
              <button
                key={surface.surfaceId}
                type="button"
                onClick={() => setSelectedIdx(idx)}
                style={{
                  display: "flex",
                  alignItems: "center",
                  justifyContent: "space-between",
                  gap: 10,
                  width: "95%",
                  margin: "0 auto",
                  padding: "10px 14px",
                  borderRadius: 16,
                  background: "rgba(24, 29, 40, 0.78)",
                  backdropFilter: "blur(16px)",
                  WebkitBackdropFilter: "blur(16px)",
                  border: isPlan
                    ? "1px solid rgba(245, 158, 11, 0.4)"
                    : "1px solid rgba(255, 255, 255, 0.09)",
                  color: "var(--text-primary)",
                  textAlign: "left",
                  cursor: "pointer",
                  boxShadow: "0 8px 20px rgba(0, 0, 0, 0.35)",
                  transition: "all 0.15s ease",
                }}
              >
                <div
                  style={{
                    display: "flex",
                    alignItems: "center",
                    gap: 9,
                    minWidth: 0,
                    flex: 1,
                  }}
                >
                  {surface.kind === "context_card" ? (
                    <IntegrationBrandIcon
                      name={surface.harness || "google_search"}
                      size={16}
                    />
                  ) : (
                    <HarnessBrandIcon name={surface.harness || "horizon"} size={16} />
                  )}
                  <div
                    style={{
                      display: "flex",
                      flexDirection: "column",
                      minWidth: 0,
                      flex: 1,
                    }}
                  >
                    <span
                      style={{
                        fontSize: 11.5,
                        fontWeight: 600,
                        color: isPlan ? "#fbbf24" : "#f8fafc",
                      }}
                    >
                      {surface.title} · {surface.repo}
                    </span>
                    <span
                      style={{
                        fontSize: 11.5,
                        color: "var(--text-secondary)",
                        whiteSpace: "nowrap",
                        overflow: "hidden",
                        textOverflow: "ellipsis",
                      }}
                    >
                      {activeMilestone}
                    </span>
                  </div>
                </div>

                <div style={{ display: "flex", alignItems: "center", gap: 6 }}>
                  {isRunning && (
                    <Loader2
                      size={13}
                      color="#38bdf8"
                      style={{ animation: "spin 1.4s linear infinite" }}
                    />
                  )}
                  <ChevronRight size={14} color="#94a3b8" />
                </div>
              </button>
            );
          })}
        </div>
      )}
    </div>
  );
}
