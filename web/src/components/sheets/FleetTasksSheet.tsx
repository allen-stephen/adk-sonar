import { useState } from "react";
import {
  CheckCircle2,
  Circle,
  Loader2,
  Play,
  Sparkles,
  X,
} from "lucide-react";
import type { OrchestratorState } from "../../lib/api";
import { HarnessBrandIcon } from "../BrandIcons";

interface FleetTasksSheetProps {
  state: OrchestratorState;
  onClose: () => void;
  onApprovePlan: (taskId: string, instruction: string) => void;
  onCreateTask: (goal: string, repo: string, mode: string) => void;
}

export function FleetTasksSheet({
  state,
  onClose,
  onApprovePlan,
  onCreateTask,
}: FleetTasksSheetProps) {
  const [newGoal, setNewGoal] = useState("");
  const [newRepo, setNewRepo] = useState("auth-svc");

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

        <div className="sheet-body">
          {state.tasks.length === 0 ? (
            <div
              style={{
                padding: "28px 16px",
                textAlign: "center",
                color: "var(--text-muted)",
                fontSize: 13,
                lineHeight: 1.5,
              }}
            >
              No active or completed agent runs yet. Dispatch a task by voice or
              below to track its trajectory here.
            </div>
          ) : (
            <div style={{ display: "flex", flexDirection: "column", gap: 10 }}>
              {state.tasks.map((task) => {
                const isAwaiting = task.status === "awaiting_input";
                const isCompleted = task.status === "completed";
                return (
                  <div
                    key={task.task_id}
                    style={{
                      padding: "13px 14px",
                      borderRadius: 16,
                      background: isAwaiting
                        ? "rgba(245, 158, 11, 0.1)"
                        : "rgba(255, 255, 255, 0.03)",
                      border: isAwaiting
                        ? "1px solid rgba(245, 158, 11, 0.4)"
                        : "1px solid var(--border-subtle)",
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
                        <span>
                          {task.task_id} · {task.repo}
                        </span>
                      </div>

                      <span
                        style={{
                          fontSize: 11,
                          fontWeight: 600,
                          padding: "3px 8px",
                          borderRadius: 99,
                          background: isAwaiting
                            ? "rgba(245, 158, 11, 0.2)"
                            : isCompleted
                              ? "rgba(16, 185, 129, 0.18)"
                              : "rgba(59, 130, 246, 0.18)",
                          color: isAwaiting
                            ? "#fbbf24"
                            : isCompleted
                              ? "#34d399"
                              : "#93c5fd",
                        }}
                      >
                        {isAwaiting
                          ? "Needs Approval"
                          : isCompleted
                            ? "Completed"
                            : `Running · ${task.elapsed_seconds}s`}
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

                    {/* High-Level Trajectory Milestones (No Code Diffs) */}
                    {task.milestones && task.milestones.length > 0 && (
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
                                <Loader2 size={13} color="#38bdf8" />
                              )
                            ) : (
                              <Circle size={12} color="#475569" />
                            )}
                            <span>{m.label}</span>
                          </div>
                        ))}
                      </div>
                    )}

                    {/* One-Tap Plan Approval if Awaiting Input */}
                    {isAwaiting && (
                      <button
                        type="button"
                        className="small-action-btn"
                        style={{
                          alignSelf: "flex-start",
                          background: "rgba(245, 158, 11, 0.22)",
                          borderColor: "rgba(245, 158, 11, 0.5)",
                          color: "#fde68a",
                        }}
                        onClick={() => {
                          onApprovePlan(
                            task.task_id,
                            task.questions[0] || "Rotate JWT via Redis TTL"
                          );
                          onClose();
                        }}
                      >
                        Approve &amp; Execute Plan
                      </button>
                    )}
                  </div>
                );
              })}
            </div>
          )}

          {/* Quick Task Dispatch Bar */}
          <div className="vault-input-group">
            <select
              value={newRepo}
              onChange={(e) => setNewRepo(e.target.value)}
              className="harness-chip-select"
              style={{ padding: "0 10px" }}
            >
              {(state.workspaces.length > 0
                ? state.workspaces
                : [
                    { name: "auth-svc" },
                    { name: "web-app" },
                    { name: "analytics-svc" },
                  ]
              ).map((w) => (
                <option
                  key={w.name}
                  value={w.name}
                  style={{ background: "#111827" }}
                >
                  {w.name}
                </option>
              ))}
            </select>
            <input
              type="text"
              className="dark-input"
              placeholder="Dispatch new goal..."
              value={newGoal}
              onChange={(e) => setNewGoal(e.target.value)}
            />
            <button
              type="button"
              className="small-action-btn"
              onClick={() => {
                if (!newGoal.trim()) return;
                onCreateTask(newGoal.trim(), newRepo, "plan");
                setNewGoal("");
              }}
            >
              <Play size={13} />
            </button>
          </div>
        </div>
      </div>
    </div>
  );
}
