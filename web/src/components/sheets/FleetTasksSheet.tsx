import { useState } from "react";
import {
  AlertTriangle,
  Archive,
  CheckCircle2,
  ChevronDown,
  ChevronUp,
  Circle,
  GitBranch,
  Loader2,
  Sparkles,
  Trash2,
  X,
} from "lucide-react";
import type { OrchestratorState, TaskItem } from "../../lib/api";
import { HarnessBrandIcon } from "../BrandIcons";

interface FleetTasksSheetProps {
  state: OrchestratorState;
  onClose: () => void;
  onApprovePlan: (taskId: string, instruction: string) => void;
  onDismissTask?: (taskId: string) => void;
  onCancelTask?: (taskId: string) => void;
  onClearHistory?: () => void;
}

function formatRelativeAge(ageSeconds?: number): string {
  if (ageSeconds === undefined || ageSeconds === null || ageSeconds < 30) {
    return "just now";
  }
  if (ageSeconds < 3600) {
    return `${Math.max(1, Math.floor(ageSeconds / 60))}m ago`;
  }
  if (ageSeconds < 86400) {
    return `${Math.floor(ageSeconds / 3600)}h ago`;
  }
  return `${Math.floor(ageSeconds / 86400)}d ago`;
}

function getStatusBadgeConfig(task: TaskItem): {
  label: string;
  bg: string;
  color: string;
  border: string;
} {
  const ageStr = formatRelativeAge(task.age_seconds);
  if (task.status === "awaiting_input" || task.status === "awaiting_approval") {
    return {
      label: "Needs Approval",
      bg: "rgba(245, 158, 11, 0.2)",
      color: "#fbbf24",
      border: "rgba(245, 158, 11, 0.4)",
    };
  }
  if (task.status === "completed") {
    return {
      label: `Completed · ${ageStr}`,
      bg: "rgba(16, 185, 129, 0.18)",
      color: "#34d399",
      border: "rgba(16, 185, 129, 0.3)",
    };
  }
  if (task.status === "failed" || task.status === "orphaned") {
    return {
      label: `Failed · ${ageStr}`,
      bg: "rgba(248, 113, 113, 0.18)",
      color: "#f87171",
      border: "rgba(248, 113, 113, 0.35)",
    };
  }
  if (task.status === "cancelled") {
    return {
      label: `Cancelled · ${ageStr}`,
      bg: "rgba(148, 163, 184, 0.16)",
      color: "#94a3b8",
      border: "rgba(148, 163, 184, 0.28)",
    };
  }
  return {
    label: `Running · ${task.elapsed_seconds}s`,
    bg: "rgba(59, 130, 246, 0.18)",
    color: "#93c5fd",
    border: "var(--border-subtle)",
  };
}

export function FleetTasksSheet({
  state,
  onClose,
  onApprovePlan,
  onDismissTask,
  onCancelTask,
  onClearHistory,
}: FleetTasksSheetProps) {
  const [ledgerOpen, setLedgerOpen] = useState(false);

  // Active or freshly finished (< 10m and not dismissed) tasks appear in the primary Fleet view
  const activeTasks = state.tasks.filter(
    (t) =>
      t.status === "running" ||
      t.status === "awaiting_input" ||
      t.status === "awaiting_approval" ||
      !t.is_stale
  );

  // Complete ledger (newest first)
  const ledgerTasks = [...state.tasks].reverse();

  return (
    <div className="sheet-backdrop" onClick={onClose}>
      <div
        className="bottom-sheet-panel"
        onClick={(e) => e.stopPropagation()}
        data-testid="fleet-mission-sheet"
      >
        <div className="sheet-handle-bar" />
        <div className="sheet-header">
          <span className="sheet-title">Agent Fleet</span>
          <button
            type="button"
            className="sheet-close-btn"
            onClick={onClose}
            aria-label="Close Agent Fleet Sheet"
          >
            <X size={16} />
          </button>
        </div>

        <div className="sheet-body" style={{ display: "flex", flexDirection: "column", gap: 14 }}>
          {/* Primary Section: Active & Unacknowledged Runs */}
          {activeTasks.length === 0 ? (
            <div
              style={{
                padding: "24px 16px",
                textAlign: "center",
                color: "var(--text-muted)",
                fontSize: 13,
                lineHeight: 1.5,
                borderRadius: 16,
                background: "rgba(255, 255, 255, 0.02)",
                border: "1px dashed rgba(255, 255, 255, 0.08)",
              }}
            >
              No active agent runs in flight. Ask ADK Sonar by voice to inspect a repository, plan a feature, or fix a bug.
            </div>
          ) : (
            <div style={{ display: "flex", flexDirection: "column", gap: 10 }}>
              {activeTasks.map((task) => {
                const isAwaiting =
                  task.status === "awaiting_input" || task.status === "awaiting_approval";
                const isFailed =
                  task.status === "failed" || task.status === "orphaned";
                const isTerminal =
                  task.status === "completed" ||
                  task.status === "failed" ||
                  task.status === "cancelled" ||
                  task.status === "orphaned";
                const badge = getStatusBadgeConfig(task);

                return (
                  <div
                    key={task.task_id}
                    style={{
                      padding: "13px 14px",
                      borderRadius: 16,
                      background: isAwaiting
                        ? "rgba(245, 158, 11, 0.09)"
                        : isFailed
                          ? "rgba(248, 113, 113, 0.07)"
                          : "rgba(255, 255, 255, 0.03)",
                      border: `1px solid ${badge.border}`,
                      display: "flex",
                      flexDirection: "column",
                      gap: 10,
                    }}
                  >
                    {/* Header: Harness Icon + Task ID + Status Badge */}
                    <div
                      style={{
                        display: "flex",
                        alignItems: "center",
                        justifyContent: "space-between",
                        gap: 8,
                      }}
                    >
                      <div
                        style={{
                          display: "flex",
                          alignItems: "center",
                          gap: 8,
                          fontSize: 13.5,
                          fontWeight: 600,
                        }}
                      >
                        <HarnessBrandIcon name={task.harness} size={17} />
                        <span>{task.repo || "Workspace"}</span>
                      </div>

                      <span
                        style={{
                          fontSize: 11,
                          fontWeight: 600,
                          padding: "3px 8px",
                          borderRadius: 99,
                          background: badge.bg,
                          color: badge.color,
                          whiteSpace: "nowrap",
                        }}
                      >
                        {badge.label}
                      </span>
                    </div>

                    {/* Goal */}
                    <div
                      style={{
                        fontSize: 12.5,
                        color: "var(--text-primary)",
                        lineHeight: 1.35,
                      }}
                    >
                      {task.goal}
                    </div>

                    {/* Error or Summary for Failed/Completed tasks */}
                    {isFailed && (task.error || task.summary) && (
                      <div
                        style={{
                          display: "flex",
                          alignItems: "flex-start",
                          gap: 7,
                          padding: "8px 10px",
                          borderRadius: 10,
                          background: "rgba(248, 113, 113, 0.1)",
                          color: "#fca5a5",
                          fontSize: 11.5,
                          lineHeight: 1.35,
                        }}
                      >
                        <AlertTriangle size={13} style={{ flexShrink: 0, marginTop: 1 }} />
                        <span>{(task.error || task.summary || "").slice(0, 180)}</span>
                      </div>
                    )}

                    {/* High-Level Trajectory Milestones (for non-failed runs) */}
                    {!isFailed && task.milestones && task.milestones.length > 0 && (
                      <div
                        style={{
                          display: "flex",
                          flexDirection: "column",
                          gap: 6,
                          paddingTop: 6,
                          borderTop: "1px solid rgba(255, 255, 255, 0.06)",
                        }}
                      >
                        {task.milestones.map((m, idx) => (
                          <div
                            key={idx}
                            style={{
                              display: "flex",
                              alignItems: "center",
                              gap: 8,
                              fontSize: 11.5,
                              color:
                                m.state === "active"
                                  ? "#f8fafc"
                                  : m.state === "done"
                                    ? "var(--text-secondary)"
                                    : "var(--text-muted)",
                            }}
                          >
                            {m.state === "done" ? (
                              <CheckCircle2 size={13} color="#10b981" />
                            ) : m.state === "active" ? (
                              isAwaiting ? (
                                <Sparkles size={13} color="#f59e0b" />
                              ) : (
                                <Loader2
                                  size={13}
                                  color="#38bdf8"
                                  style={{ animation: "spin 1.4s linear infinite" }}
                                />
                              )
                            ) : (
                              <Circle size={12} color="#475569" />
                            )}
                            <span>{m.label}</span>
                          </div>
                        ))}
                      </div>
                    )}

                    {/* Actions Row: Approve Plan, Cancel Running, or Dismiss Finished/Failed */}
                    <div style={{ display: "flex", alignItems: "center", justifyContent: "space-between", gap: 8 }}>
                      {isAwaiting && (
                        <button
                          type="button"
                          className="small-action-btn"
                          style={{
                            background: "rgba(245, 158, 11, 0.22)",
                            borderColor: "rgba(245, 158, 11, 0.5)",
                            color: "#fde68a",
                          }}
                          onClick={() => {
                            onApprovePlan(
                              task.task_id,
                              task.questions[0] || "Approve and execute plan"
                            );
                            onClose();
                          }}
                        >
                          Approve &amp; Execute Plan
                        </button>
                      )}

                      {(task.status === "running" || isAwaiting) && onCancelTask && (
                        <button
                          type="button"
                          className="small-action-btn"
                          style={{
                            background: "rgba(255, 255, 255, 0.05)",
                            borderColor: "rgba(255, 255, 255, 0.12)",
                            color: "var(--text-secondary)",
                          }}
                          onClick={() => onCancelTask(task.task_id)}
                        >
                          Cancel
                        </button>
                      )}

                      {isTerminal && onDismissTask && (
                        <button
                          type="button"
                          className="small-action-btn"
                          style={{
                            marginLeft: "auto",
                            background: "rgba(255, 255, 255, 0.06)",
                            borderColor: "rgba(255, 255, 255, 0.12)",
                            color: "var(--text-secondary)",
                          }}
                          onClick={() => onDismissTask(task.task_id)}
                        >
                          Dismiss
                        </button>
                      )}
                    </div>
                  </div>
                );
              })}
            </div>
          )}

          {/* Bottom Footer: All Tasks Complete Ledger Drawer */}
          <div
            style={{
              marginTop: 2,
              paddingTop: 10,
              borderTop: "1px solid rgba(255, 255, 255, 0.08)",
              display: "flex",
              flexDirection: "column",
              gap: 10,
            }}
          >
            <div style={{ display: "flex", alignItems: "center", justifyContent: "space-between", gap: 8 }}>
              <button
                type="button"
                data-testid="toggle-all-tasks-ledger-btn"
                onClick={() => setLedgerOpen((prev) => !prev)}
                style={{
                  display: "flex",
                  alignItems: "center",
                  justifyContent: "space-between",
                  flex: 1,
                  padding: "10px 13px",
                  borderRadius: 13,
                  background: "rgba(255, 255, 255, 0.04)",
                  border: "1px solid rgba(255, 255, 255, 0.09)",
                  color: "#e2e8f0",
                  fontSize: 12.5,
                  fontWeight: 600,
                  cursor: "pointer",
                }}
              >
                <span style={{ display: "inline-flex", alignItems: "center", gap: 8 }}>
                  <Archive size={14} color="#93c5fd" />
                  <span>All Tasks Ledger ({ledgerTasks.length})</span>
                </span>
                {ledgerOpen ? (
                  <ChevronUp size={15} color="#94a3b8" />
                ) : (
                  <ChevronDown size={15} color="#94a3b8" />
                )}
              </button>

              {ledgerOpen && ledgerTasks.length > 0 && onClearHistory && (
                <button
                  type="button"
                  onClick={onClearHistory}
                  title="Clear task ledger history"
                  style={{
                    display: "inline-flex",
                    alignItems: "center",
                    gap: 5,
                    padding: "10px 12px",
                    borderRadius: 13,
                    background: "rgba(248, 113, 113, 0.12)",
                    border: "1px solid rgba(248, 113, 113, 0.3)",
                    color: "#fca5a5",
                    fontSize: 11.5,
                    fontWeight: 600,
                    cursor: "pointer",
                  }}
                >
                  <Trash2 size={13} />
                  <span>Clear</span>
                </button>
              )}
            </div>

            {ledgerOpen && (
              <div
                data-testid="all-tasks-ledger-list"
                style={{
                  display: "flex",
                  flexDirection: "column",
                  gap: 7,
                  maxHeight: 240,
                  overflowY: "auto",
                  paddingRight: 2,
                }}
              >
                {ledgerTasks.length === 0 ? (
                  <div
                    style={{
                      padding: "14px 12px",
                      fontSize: 12,
                      color: "var(--text-muted)",
                      textAlign: "center",
                    }}
                  >
                    Ledger is empty.
                  </div>
                ) : (
                  ledgerTasks.map((t) => {
                    const b = getStatusBadgeConfig(t);
                    return (
                      <div
                        key={`ledger-${t.task_id}`}
                        style={{
                          display: "flex",
                          flexDirection: "column",
                          gap: 4,
                          padding: "9px 12px",
                          borderRadius: 12,
                          background: "rgba(15, 23, 42, 0.55)",
                          border: "1px solid rgba(255, 255, 255, 0.06)",
                        }}
                      >
                        <div
                          style={{
                            display: "flex",
                            alignItems: "center",
                            justifyContent: "space-between",
                            gap: 8,
                          }}
                        >
                          <span
                            style={{
                              display: "inline-flex",
                              alignItems: "center",
                              gap: 6,
                              fontSize: 12,
                              fontWeight: 600,
                              color: "#f1f5f9",
                            }}
                          >
                            <HarnessBrandIcon name={t.harness} size={14} />
                            <span>{t.repo || "Workspace"}</span>
                            {t.branch && (
                              <span
                                style={{
                                  display: "inline-flex",
                                  alignItems: "center",
                                  gap: 3,
                                  fontSize: 10.5,
                                  color: "var(--text-muted)",
                                  fontFamily: "var(--font-mono)",
                                }}
                              >
                                <GitBranch size={10} />
                                {t.branch}
                              </span>
                            )}
                          </span>

                          <span
                            style={{
                              fontSize: 10.5,
                              fontWeight: 600,
                              padding: "2px 7px",
                              borderRadius: 99,
                              background: b.bg,
                              color: b.color,
                              whiteSpace: "nowrap",
                            }}
                          >
                            {b.label}
                          </span>
                        </div>

                        <div
                          style={{
                            fontSize: 11.5,
                            color: "var(--text-secondary)",
                            whiteSpace: "nowrap",
                            overflow: "hidden",
                            textOverflow: "ellipsis",
                          }}
                        >
                          {t.goal}
                        </div>

                        {(t.summary || t.error) && (
                          <div
                            style={{
                              fontSize: 11,
                              color:
                                t.status === "failed" ? "#fca5a5" : "var(--text-muted)",
                              whiteSpace: "nowrap",
                              overflow: "hidden",
                              textOverflow: "ellipsis",
                            }}
                          >
                            {t.error || t.summary}
                          </div>
                        )}
                      </div>
                    );
                  })
                )}
              </div>
            )}
          </div>
        </div>
      </div>
    </div>
  );
}
