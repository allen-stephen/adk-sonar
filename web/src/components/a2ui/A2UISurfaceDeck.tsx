import { useState } from "react";
import {
  AlertTriangle,
  Calendar,
  Check,
  CheckCircle2,
  ChevronRight,
  Circle,
  Clock,
  Compass,
  Disc3,
  ExternalLink,
  FileCode2,
  FileText,
  GitBranch,
  GitPullRequest,
  Hash,
  Loader2,
  Mail,
  MapPin,
  MessageSquare,
  Terminal,
  Video,
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
  onCancelTask?: (taskId: string) => void;
  onDismissSurface: (surfaceId: string) => void;
  onSelectStarterPrompt: (prompt: string) => void;
}

const SUGGESTIONS = [
  {
    category: "Coding",
    prompt: "Check the comments on my open PR and plan a fix",
    icon: Terminal,
    accent: "#60a5fa",
  },
  {
    category: "Productivity",
    prompt: "Check my calendar this afternoon and unread emails",
    icon: Calendar,
    accent: "#34d399",
  },
  {
    category: "Answers",
    prompt: "Check the latest game scores for me",
    icon: Compass,
    accent: "#fbbf24",
  },
];

function getDriveKindAccent(kindLabel?: string): { bg: string; color: string } {
  const k = (kindLabel || "").toLowerCase();
  if (k === "sheet") return { bg: "rgba(16, 185, 129, 0.18)", color: "#34d399" };
  if (k === "slides") return { bg: "rgba(245, 158, 11, 0.2)", color: "#fbbf24" };
  if (k === "pdf") return { bg: "rgba(248, 113, 113, 0.18)", color: "#f87171" };
  return { bg: "rgba(59, 130, 246, 0.18)", color: "#93c5fd" };
}

function getGitHubStateAccent(state?: string): { bg: string; color: string; label: string } {
  const s = (state || "open").toLowerCase();
  if (s === "merged") {
    return { bg: "rgba(168, 85, 247, 0.2)", color: "#c084fc", label: "Merged" };
  }
  if (s === "closed" || s === "failure") {
    return { bg: "rgba(248, 113, 113, 0.18)", color: "#f87171", label: s === "failure" ? "Failed" : "Closed" };
  }
  if (s === "success") {
    return { bg: "rgba(16, 185, 129, 0.18)", color: "#34d399", label: "Passed" };
  }
  return { bg: "rgba(16, 185, 129, 0.18)", color: "#34d399", label: "Open" };
}

/**
 * 3-Tier Adaptive Context Card Renderer:
 * Tier 1: Rich Medium-Specific Card (GitHub PRs/Issues/Branches, Calendar Agenda, Spotify Media, Gmail Inbox/Draft, Drive Files, Search/Maps)
 * Tier 2: Compact Status / Empty-State Banner (when `meta.empty_state` or `meta.status_label` without items)
 * Tier 3: Universal `BulletList` Fallback (handles arbitrary queries, MCP tools, or unstructured text)
 */
function ContextSurfaceBody({ surface }: { surface: A2UISurface }) {
  const ctx = surface.dataModel?.context || {};
  const subkind = (surface.subkind || ctx.brandIcon || "").toLowerCase();
  const items: Array<Record<string, any>> = Array.isArray(ctx.items) ? ctx.items : [];
  const meta: Record<string, any> = ctx.meta && typeof ctx.meta === "object" ? ctx.meta : {};
  const bullets: string[] = Array.isArray(ctx.bullets) ? ctx.bullets : [];

  // --- 1. SPOTIFY MEDIA PLAYER CARD ---
  if (subkind.includes("spotify") && (meta.track || meta.status_label)) {
    const isPlaying = Boolean(meta.is_playing);
    return (
      <div style={{ display: "flex", flexDirection: "column", gap: 10 }}>
        <div
          style={{
            display: "flex",
            alignItems: "center",
            gap: 12,
            padding: "12px 14px",
            borderRadius: 16,
            background: "linear-gradient(135deg, rgba(16, 185, 129, 0.14), rgba(12, 15, 22, 0.72))",
            border: "1px solid rgba(16, 185, 129, 0.28)",
          }}
        >
          {meta.album_art_url ? (
            <img
              src={meta.album_art_url}
              alt={meta.track || "Album Art"}
              style={{
                width: 48,
                height: 48,
                borderRadius: 11,
                objectFit: "cover",
                flexShrink: 0,
                boxShadow: "0 4px 12px rgba(0,0,0,0.4)",
              }}
            />
          ) : (
            <div
              style={{
                width: 46,
                height: 46,
                borderRadius: 12,
                background: "rgba(16, 185, 129, 0.18)",
                border: "1px solid rgba(16, 185, 129, 0.3)",
                display: "flex",
                alignItems: "center",
                justifyContent: "center",
                flexShrink: 0,
                color: "#34d399",
              }}
            >
              <Disc3 size={22} />
            </div>
          )}

          <div style={{ display: "flex", flexDirection: "column", gap: 2, minWidth: 0, flex: 1 }}>
            <div style={{ display: "flex", alignItems: "center", gap: 6 }}>
              <span
                style={{
                  fontSize: 10,
                  fontWeight: 700,
                  textTransform: "uppercase",
                  letterSpacing: "0.05em",
                  color: isPlaying ? "#34d399" : "#94a3b8",
                }}
              >
                {meta.status_label || (isPlaying ? "Now Playing" : "Paused")}
              </span>
              {meta.device_name && (
                <span style={{ fontSize: 10, color: "var(--text-muted)" }}>
                  · {meta.device_name}
                </span>
              )}
            </div>
            <div
              style={{
                fontSize: 14,
                fontWeight: 600,
                color: "#f8fafc",
                whiteSpace: "nowrap",
                overflow: "hidden",
                textOverflow: "ellipsis",
              }}
            >
              {meta.track || surface.subtitle}
            </div>
            {(meta.artist || meta.album) && (
              <div
                style={{
                  fontSize: 12,
                  color: "var(--text-secondary)",
                  whiteSpace: "nowrap",
                  overflow: "hidden",
                  textOverflow: "ellipsis",
                }}
              >
                {[meta.artist, meta.album].filter(Boolean).join(" · ")}
              </div>
            )}
          </div>

          {meta.external_url && (
            <a
              href={meta.external_url}
              target="_blank"
              rel="noreferrer"
              title="Open in Spotify"
              style={{
                padding: 8,
                borderRadius: 10,
                background: "rgba(255, 255, 255, 0.06)",
                color: "#34d399",
                display: "inline-flex",
              }}
            >
              <ExternalLink size={14} />
            </a>
          )}
        </div>
      </div>
    );
  }

  // --- 2. GITHUB ACTIVITY CARD (PRs, Issues, CI Runs, Fork Branches) ---
  if (subkind.includes("github") && items.length > 0) {
    return (
      <div style={{ display: "flex", flexDirection: "column", gap: 7 }}>
        <p className="a2ui-subtitle" style={{ fontSize: 12.5, color: "var(--text-secondary)" }}>
          {surface.subtitle}
        </p>

        <div style={{ display: "flex", flexDirection: "column", gap: 6 }}>
          {items.map((item, i) => {
            const badge = getGitHubStateAccent(item.state);
            const isBranch = item.kind === "branch";
            return (
              <div
                key={i}
                style={{
                  display: "flex",
                  alignItems: "center",
                  justifyContent: "space-between",
                  gap: 9,
                  padding: "9px 11px",
                  borderRadius: 12,
                  background: "rgba(12, 15, 22, 0.56)",
                  border: "1px solid rgba(255, 255, 255, 0.07)",
                }}
              >
                <div style={{ display: "flex", alignItems: "center", gap: 8, minWidth: 0, flex: 1 }}>
                  {isBranch ? (
                    <GitBranch size={14} color="#60a5fa" style={{ flexShrink: 0 }} />
                  ) : (
                    <GitPullRequest size={14} color={badge.color} style={{ flexShrink: 0 }} />
                  )}
                  <div style={{ display: "flex", flexDirection: "column", minWidth: 0, flex: 1 }}>
                    <div
                      style={{
                        fontSize: 12.5,
                        fontWeight: 600,
                        color: "#f1f5f9",
                        whiteSpace: "nowrap",
                        overflow: "hidden",
                        textOverflow: "ellipsis",
                      }}
                    >
                      {item.number ? `#${item.number} ` : ""}
                      {item.title}
                    </div>
                    <div style={{ display: "flex", alignItems: "center", gap: 6, fontSize: 11, color: "var(--text-muted)" }}>
                      {item.repo && <span>{item.repo}</span>}
                      {item.author && <span>· @{item.author}</span>}
                      {item.comments ? (
                        <span style={{ display: "inline-flex", alignItems: "center", gap: 3 }}>
                          · <MessageSquare size={10} /> {item.comments}
                        </span>
                      ) : null}
                    </div>
                  </div>
                </div>

                <div style={{ display: "flex", alignItems: "center", gap: 6, flexShrink: 0 }}>
                  <span
                    style={{
                      fontSize: 10.5,
                      fontWeight: 600,
                      padding: "2px 7px",
                      borderRadius: 99,
                      background: badge.bg,
                      color: badge.color,
                    }}
                  >
                    {isBranch ? "Branch" : badge.label}
                  </span>
                  {item.url && (
                    <a
                      href={item.url}
                      target="_blank"
                      rel="noreferrer"
                      style={{ color: "#94a3b8", display: "inline-flex" }}
                    >
                      <ExternalLink size={13} />
                    </a>
                  )}
                </div>
              </div>
            );
          })}
        </div>
      </div>
    );
  }

  // --- 3. GOOGLE CALENDAR TIMELINE CARD ---
  if (subkind.includes("calendar")) {
    if (meta.empty_state) {
      return (
        <div
          style={{
            display: "flex",
            alignItems: "center",
            gap: 10,
            padding: "12px 14px",
            borderRadius: 14,
            background: "rgba(16, 185, 129, 0.1)",
            border: "1px solid rgba(16, 185, 129, 0.28)",
            color: "#6ee7b7",
            fontSize: 13,
            fontWeight: 500,
          }}
        >
          <CheckCircle2 size={16} style={{ flexShrink: 0 }} />
          <span>{bullets[0] || surface.subtitle}</span>
        </div>
      );
    }

    if (items.length > 0) {
      return (
        <div style={{ display: "flex", flexDirection: "column", gap: 7 }}>
          {items.map((ev, i) => (
            <div
              key={i}
              style={{
                display: "flex",
                alignItems: "center",
                justifyContent: "space-between",
                gap: 10,
                padding: "9px 12px",
                borderRadius: 12,
                background: "rgba(12, 15, 22, 0.56)",
                borderLeft: "3px solid #3b82f6",
                borderTop: "1px solid rgba(255, 255, 255, 0.06)",
                borderRight: "1px solid rgba(255, 255, 255, 0.06)",
                borderBottom: "1px solid rgba(255, 255, 255, 0.06)",
              }}
            >
              <div style={{ display: "flex", flexDirection: "column", gap: 2, minWidth: 0, flex: 1 }}>
                <div
                  style={{
                    fontSize: 13,
                    fontWeight: 600,
                    color: "#f8fafc",
                    whiteSpace: "nowrap",
                    overflow: "hidden",
                    textOverflow: "ellipsis",
                  }}
                >
                  {ev.title}
                </div>
                <div style={{ display: "flex", alignItems: "center", gap: 6, fontSize: 11.5, color: "#93c5fd" }}>
                  <Clock size={11} />
                  <span>
                    {ev.start_time}
                    {ev.end_time ? ` – ${ev.end_time}` : ""}
                  </span>
                  {ev.date_label && (
                    <span style={{ color: "var(--text-muted)" }}>· {ev.date_label}</span>
                  )}
                </div>
              </div>

              {ev.meet_url && (
                <a
                  href={ev.meet_url}
                  target="_blank"
                  rel="noreferrer"
                  style={{
                    display: "inline-flex",
                    alignItems: "center",
                    gap: 4,
                    padding: "4px 8px",
                    borderRadius: 8,
                    background: "rgba(59, 130, 246, 0.18)",
                    color: "#93c5fd",
                    fontSize: 11,
                    fontWeight: 600,
                    textDecoration: "none",
                    flexShrink: 0,
                  }}
                >
                  <Video size={12} />
                  <span>Open</span>
                </a>
              )}
            </div>
          ))}
        </div>
      );
    }
  }

  // --- 4. GMAIL THREADS / DRAFT CARD ---
  if (subkind.includes("gmail")) {
    if (meta.is_draft) {
      return (
        <div
          style={{
            display: "flex",
            flexDirection: "column",
            gap: 6,
            padding: "12px 14px",
            borderRadius: 14,
            background: "rgba(12, 15, 22, 0.58)",
            border: "1px solid rgba(248, 113, 113, 0.28)",
          }}
        >
          <div style={{ display: "flex", alignItems: "center", justifyContent: "space-between", fontSize: 11.5 }}>
            <span style={{ color: "var(--text-muted)" }}>
              To: <strong style={{ color: "#f1f5f9" }}>{meta.recipient}</strong>
            </span>
            <span style={{ color: "#34d399", fontWeight: 600 }}>Draft Saved</span>
          </div>
          <div style={{ fontSize: 13, fontWeight: 600, color: "#f8fafc" }}>
            {meta.subject}
          </div>
          {meta.body && (
            <div style={{ fontSize: 12, color: "var(--text-secondary)", lineHeight: 1.4 }}>
              {meta.body}
            </div>
          )}
        </div>
      );
    }

    if (items.length > 0) {
      return (
        <div style={{ display: "flex", flexDirection: "column", gap: 7 }}>
          {items.map((m, i) => (
            <div
              key={i}
              style={{
                display: "flex",
                alignItems: "flex-start",
                gap: 10,
                padding: "9px 11px",
                borderRadius: 12,
                background: "rgba(12, 15, 22, 0.56)",
                border: "1px solid rgba(255, 255, 255, 0.07)",
              }}
            >
              <div
                style={{
                  width: 28,
                  height: 28,
                  borderRadius: 99,
                  background: "rgba(239, 68, 68, 0.18)",
                  color: "#fca5a5",
                  display: "flex",
                  alignItems: "center",
                  justifyContent: "center",
                  fontSize: 12,
                  fontWeight: 700,
                  flexShrink: 0,
                  marginTop: 1,
                }}
              >
                {(m.sender || "M").slice(0, 1).toUpperCase()}
              </div>
              <div style={{ display: "flex", flexDirection: "column", gap: 2, minWidth: 0, flex: 1 }}>
                <div style={{ display: "flex", alignItems: "center", justifyContent: "space-between", gap: 6 }}>
                  <span
                    style={{
                      fontSize: 12,
                      fontWeight: 600,
                      color: "#f8fafc",
                      whiteSpace: "nowrap",
                      overflow: "hidden",
                      textOverflow: "ellipsis",
                    }}
                  >
                    {m.sender}
                  </span>
                  {m.unread && (
                    <span
                      style={{
                        width: 7,
                        height: 7,
                        borderRadius: 99,
                        background: "#60a5fa",
                        flexShrink: 0,
                      }}
                    />
                  )}
                </div>
                <div
                  style={{
                    fontSize: 12.5,
                    fontWeight: 500,
                    color: "#e2e8f0",
                    whiteSpace: "nowrap",
                    overflow: "hidden",
                    textOverflow: "ellipsis",
                  }}
                >
                  {m.subject}
                </div>
                {m.snippet && (
                  <div
                    style={{
                      fontSize: 11,
                      color: "var(--text-muted)",
                      whiteSpace: "nowrap",
                      overflow: "hidden",
                      textOverflow: "ellipsis",
                    }}
                  >
                    {m.snippet}
                  </div>
                )}
              </div>
            </div>
          ))}
        </div>
      );
    }
  }

  // --- 5. GOOGLE DRIVE FILES CARD ---
  if (subkind.includes("drive") && items.length > 0) {
    return (
      <div style={{ display: "flex", flexDirection: "column", gap: 6 }}>
        {items.map((f, i) => {
          const accent = getDriveKindAccent(f.kind_label);
          return (
            <div
              key={i}
              style={{
                display: "flex",
                alignItems: "center",
                justifyContent: "space-between",
                gap: 10,
                padding: "8px 11px",
                borderRadius: 12,
                background: "rgba(12, 15, 22, 0.56)",
                border: "1px solid rgba(255, 255, 255, 0.07)",
              }}
            >
              <div style={{ display: "flex", alignItems: "center", gap: 9, minWidth: 0, flex: 1 }}>
                <FileText size={15} color={accent.color} style={{ flexShrink: 0 }} />
                <div style={{ display: "flex", flexDirection: "column", minWidth: 0, flex: 1 }}>
                  <span
                    style={{
                      fontSize: 12.5,
                      fontWeight: 600,
                      color: "#f1f5f9",
                      whiteSpace: "nowrap",
                      overflow: "hidden",
                      textOverflow: "ellipsis",
                    }}
                  >
                    {f.name}
                  </span>
                  {f.modified_time && (
                    <span style={{ fontSize: 10.5, color: "var(--text-muted)" }}>
                      Modified {f.modified_time}
                    </span>
                  )}
                </div>
              </div>

              <div style={{ display: "flex", alignItems: "center", gap: 6, flexShrink: 0 }}>
                <span
                  style={{
                    fontSize: 10.5,
                    fontWeight: 600,
                    padding: "2px 7px",
                    borderRadius: 99,
                    background: accent.bg,
                    color: accent.color,
                  }}
                >
                  {f.kind_label || "Doc"}
                </span>
                {f.url && (
                  <a
                    href={f.url}
                    target="_blank"
                    rel="noreferrer"
                    style={{ color: "#94a3b8", display: "inline-flex" }}
                  >
                    <ExternalLink size={13} />
                  </a>
                )}
              </div>
            </div>
          );
        })}
      </div>
    );
  }

  // --- 6. SLACK CHANNEL CARD ---
  if (subkind.includes("slack") && (items.length > 0 || meta.status_label)) {
    return (
      <div style={{ display: "flex", flexDirection: "column", gap: 7 }}>
        {(items.length > 0 ? items : bullets.map((b) => ({ channel: surface.repo, text: b }))).map(
          (msg, i) => (
            <div
              key={i}
              style={{
                display: "flex",
                alignItems: "flex-start",
                gap: 8,
                padding: "9px 11px",
                borderRadius: 12,
                background: "rgba(12, 15, 22, 0.56)",
                border: "1px solid rgba(255, 255, 255, 0.07)",
                fontSize: 12.5,
                color: "#e2e8f0",
              }}
            >
              <Hash size={14} color="#c084fc" style={{ flexShrink: 0, marginTop: 2 }} />
              <span>{msg.text}</span>
            </div>
          )
        )}
      </div>
    );
  }

  // --- 7. TIER 2 COMPACT EMPTY / STATUS CALLOUT OR TIER 3 UNIVERSAL BULLET LIST FALLBACK ---
  return (
    <>
      <p
        className="a2ui-subtitle"
        style={{ color: "#f8fafc", fontWeight: 600, fontSize: 13.5 }}
      >
        “{surface.subtitle}”
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
        {bullets.map((item, i) => (
          <div
            key={i}
            style={{
              display: "flex",
              alignItems: "flex-start",
              gap: 9,
              fontSize: 12.5,
              lineHeight: 1.4,
              color: "#e2e8f0",
            }}
          >
            {subkind.includes("maps") ? (
              <MapPin
                size={15}
                color="#34d399"
                style={{ flexShrink: 0, marginTop: 2 }}
              />
            ) : subkind.includes("gmail") ? (
              <Mail
                size={15}
                color="#f87171"
                style={{ flexShrink: 0, marginTop: 2 }}
              />
            ) : (
              <CheckCircle2
                size={15}
                color="#60a5fa"
                style={{ flexShrink: 0, marginTop: 2 }}
              />
            )}
            <span>{item}</span>
          </div>
        ))}

        {(meta.source_url || meta.maps_url) && (
          <div style={{ paddingTop: 4, display: "flex", justifyContent: "flex-end" }}>
            <a
              href={meta.maps_url || meta.source_url}
              target="_blank"
              rel="noreferrer"
              style={{
                display: "inline-flex",
                alignItems: "center",
                gap: 5,
                fontSize: 11,
                fontWeight: 600,
                color: "#93c5fd",
                textDecoration: "none",
              }}
            >
              <span>{meta.maps_url ? "Open in Google Maps" : "View Web Sources"}</span>
              <ExternalLink size={12} />
            </a>
          </div>
        )}
      </div>
    </>
  );
}

const SUBTITLE_CLAMP_STYLE: React.CSSProperties = {
  display: "-webkit-box",
  WebkitLineClamp: 3,
  WebkitBoxOrient: "vertical",
  overflow: "hidden",
};

function cleanDisplayText(raw?: string | null): string {
  if (!raw) return "";
  return raw
    .replace(/📎?\s*\[([^\]]+)\]\((?:file:\/\/|\/lha\/workspace\/download)[^\)]*\)/g, "$1")
    .replace(/\(?(?:file:\/\/|\/lha\/workspace\/download)[^\s\)]+\)?/g, "")
    .replace(/projects\/\d+\/locations\/\S+/g, "Vertex Sandbox")
    .replace(/\(X-Sandbox-Port:[^\)]*\)/g, "")
    .replace(/You can verify the (?:updated |created )?files here:.*?(?=(?:Let me know|$))/is, "")
    .replace(/\*\*/g, "")
    .replace(/`/g, "")
    .replace(/\s{2,}/g, " ")
    .trim();
}

interface ArtifactItem {
  name: string;
  title: string;
  content: string;
}

function ArtifactDetailReader({
  art,
  onBack,
  savedDocUrls,
  setSavedDocUrls,
}: {
  art: ArtifactItem;
  onBack: () => void;
  savedDocUrls: Record<string, string>;
  setSavedDocUrls: React.Dispatch<React.SetStateAction<Record<string, string>>>;
}) {
  const [copied, setCopied] = useState(false);
  const [savingDoc, setSavingDoc] = useState(false);
  const docUrl = savedDocUrls[art.name];
  const lines = (art.content || "").split(/\r?\n/);

  const handleSaveGoogleDoc = async () => {
    if (savingDoc || docUrl) return;
    setSavingDoc(true);
    try {
      const res = await fetch("/api/v1/artifacts/google-doc", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ title: art.title || art.name, content: art.content }),
      });
      const data = await res.json();
      if (res.ok && data.url) {
        setSavedDocUrls((prev) => ({ ...prev, [art.name]: data.url }));
        window.open(data.url, "_blank", "noopener,noreferrer");
      }
    } catch {
      // ignore network error
    } finally {
      setSavingDoc(false);
    }
  };

  const handleCopy = () => {
    void navigator.clipboard?.writeText(art.content);
    setCopied(true);
    setTimeout(() => setCopied(false), 1800);
  };

  return (
    <div style={{ display: "flex", flexDirection: "column", gap: 10 }}>
      <div style={{ display: "flex", alignItems: "center", justifyContent: "space-between", gap: 8 }}>
        <button
          type="button"
          onClick={onBack}
          style={{
            background: "rgba(255, 255, 255, 0.06)",
            border: "1px solid rgba(255, 255, 255, 0.1)",
            borderRadius: 99,
            padding: "4px 10px",
            color: "#93c5fd",
            fontSize: 11.5,
            fontWeight: 600,
            cursor: "pointer",
          }}
        >
          ← Back
        </button>
        <span
          style={{
            fontSize: 12.5,
            fontWeight: 600,
            color: "#f8fafc",
            whiteSpace: "nowrap",
            overflow: "hidden",
            textOverflow: "ellipsis",
          }}
        >
          {art.title}
        </span>
      </div>

      <div
        style={{
          display: "flex",
          flexDirection: "column",
          gap: 6,
          padding: "12px 14px",
          borderRadius: 14,
          background: "rgba(10, 13, 20, 0.72)",
          border: "1px solid rgba(255, 255, 255, 0.08)",
          maxHeight: 230,
          overflowY: "auto",
          fontSize: 12.5,
          lineHeight: 1.45,
          color: "#e2e8f0",
        }}
      >
        {lines.map((rawLine: string, idx: number) => {
          const trimmed = rawLine.trim();
          if (!trimmed) return <div key={idx} style={{ height: 4 }} />;
          if (trimmed.startsWith("#")) {
            const headingText = trimmed.replace(/^#+\s*/, "").replace(/\*\*/g, "");
            return (
              <div
                key={idx}
                style={{
                  fontSize: 13,
                  fontWeight: 700,
                  color: "#f8fafc",
                  marginTop: idx > 0 ? 6 : 0,
                }}
              >
                {headingText}
              </div>
            );
          }
          const isBullet = /^[-*•]\s+/.test(trimmed) || /^\d+[.)]\s+/.test(trimmed);
          const cleanLine = trimmed
            .replace(/^[-*•]\s+/, "")
            .replace(/^\[[ xX]\]\s+/, "")
            .replace(/\*\*/g, "");
          return (
            <div
              key={idx}
              style={{
                display: "flex",
                alignItems: "flex-start",
                gap: 7,
                color: isBullet ? "#cbd5e1" : "#e2e8f0",
              }}
            >
              {isBullet && (
                <span style={{ color: "#60a5fa", fontWeight: 700 }}>•</span>
              )}
              <span>{cleanLine}</span>
            </div>
          );
        })}
      </div>

      <div style={{ display: "flex", gap: 8 }}>
        {docUrl ? (
          <a
            href={docUrl}
            target="_blank"
            rel="noreferrer"
            className="a2ui-primary-btn"
            style={{
              flex: 1,
              textDecoration: "none",
              display: "inline-flex",
              alignItems: "center",
              justifyContent: "center",
              gap: 6,
            }}
          >
            <span>Open in Google Docs</span>
            <ExternalLink size={13} />
          </a>
        ) : (
          <button
            type="button"
            className="a2ui-primary-btn"
            style={{ flex: 1 }}
            onClick={() => void handleSaveGoogleDoc()}
          >
            {savingDoc ? "Saving to Drive..." : "Save to Google Docs"}
          </button>
        )}
        <button
          type="button"
          className="a2ui-option-pill"
          style={{ width: "auto", padding: "0 14px", fontWeight: 600 }}
          onClick={handleCopy}
        >
          {copied ? "Copied ✓" : "Copy"}
        </button>
      </div>
    </div>
  );
}

function ArtifactRowList({
  artifacts,
  onSelectArtifact,
}: {
  artifacts: ArtifactItem[];
  onSelectArtifact: (idx: number) => void;
}) {
  if (artifacts.length === 0) return null;
  return (
    <div style={{ display: "flex", flexDirection: "column", gap: 6 }}>
      {artifacts.map((art, idx) => (
        <button
          key={art.name}
          type="button"
          onClick={() => onSelectArtifact(idx)}
          style={{
            display: "flex",
            alignItems: "center",
            justifyContent: "space-between",
            gap: 10,
            width: "100%",
            padding: "10px 13px",
            borderRadius: 13,
            background: "rgba(255, 255, 255, 0.04)",
            border: "1px solid rgba(255, 255, 255, 0.09)",
            color: "#f8fafc",
            cursor: "pointer",
            textAlign: "left",
          }}
        >
          <span style={{ display: "inline-flex", alignItems: "center", gap: 9, minWidth: 0 }}>
            <FileText size={15} color="#60a5fa" style={{ flexShrink: 0 }} />
            <span
              style={{
                fontSize: 13,
                fontWeight: 600,
                whiteSpace: "nowrap",
                overflow: "hidden",
                textOverflow: "ellipsis",
              }}
            >
              {art.title}
            </span>
          </span>
          <span
            style={{
              display: "inline-flex",
              alignItems: "center",
              gap: 4,
              fontSize: 11,
              fontWeight: 600,
              color: "#93c5fd",
              flexShrink: 0,
            }}
          >
            <span>Read</span>
            <ChevronRight size={13} />
          </span>
        </button>
      ))}
    </div>
  );
}

function PlanApprovalBody({
  surface,
  planSteps,
  hasQuestions,
  options,
  currentChoice,
  onSelectChoice,
  onApprovePlan,
  onCancelTask,
  onDismissSurface,
}: {
  surface: A2UISurface;
  planSteps: string[];
  hasQuestions: boolean;
  options: string[];
  currentChoice: string;
  onSelectChoice: (choice: string) => void;
  onApprovePlan: (taskId: string, selectedOption: string) => void;
  onCancelTask?: (taskId: string) => void;
  onDismissSurface: (surfaceId: string) => void;
}) {
  const [activeArtifactIdx, setActiveArtifactIdx] = useState<number | null>(null);
  const [savedDocUrls, setSavedDocUrls] = useState<Record<string, string>>({});

  const planArtifacts: ArtifactItem[] =
    Array.isArray(surface.dataModel?.plan?.artifacts)
      ? surface.dataModel.plan.artifacts
      : [];

  if (activeArtifactIdx !== null && planArtifacts[activeArtifactIdx]) {
    return (
      <ArtifactDetailReader
        art={planArtifacts[activeArtifactIdx]}
        onBack={() => setActiveArtifactIdx(null)}
        savedDocUrls={savedDocUrls}
        setSavedDocUrls={setSavedDocUrls}
      />
    );
  }

  const cleanSub = cleanDisplayText(surface.subtitle);

  return (
    <>
      <p
        className="a2ui-subtitle"
        style={{ color: "#e2e8f0", fontWeight: 500, ...SUBTITLE_CLAMP_STYLE }}
      >
        {cleanSub}
      </p>

      {/* If the harness generated deliverable artifacts (e.g. a recipe, grocery list, or full plan doc), render tappable Artifact rows */}
      {planArtifacts.length > 0 && (
        <ArtifactRowList
          artifacts={planArtifacts}
          onSelectArtifact={(idx) => setActiveArtifactIdx(idx)}
        />
      )}

      {/* Show concise plan steps when no artifacts are attached or when reviewing a code diff */}
      {planSteps.length > 0 && (planArtifacts.length === 0 || surface.dataModel?.plan?.diffSummary) && (
        <div
          style={{
            display: "flex",
            flexDirection: "column",
            gap: 6,
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
            {surface.dataModel?.plan?.diffSummary
              ? "Sandbox Diff Summary"
              : "Key Steps"}
          </div>
          {planSteps.slice(0, 3).map((step, i) => (
            <div
              key={i}
              style={{
                fontSize: 12,
                color: "var(--text-secondary)",
                display: "flex",
                gap: 7,
                lineHeight: 1.4,
                minWidth: 0,
              }}
            >
              {planSteps.length > 1 && (
                <span style={{ color: "#60a5fa", fontWeight: 600, flexShrink: 0 }}>
                  {i + 1}.
                </span>
              )}
              <span
                style={{
                  overflowWrap: "anywhere",
                  wordBreak: "break-word",
                  minWidth: 0,
                }}
              >
                {step}
              </span>
            </div>
          ))}
        </div>
      )}

      {hasQuestions && (
        <div className="a2ui-options-list">
          {options.map((optionText) => {
            const isSelected = currentChoice === optionText;
            return (
              <button
                key={optionText}
                type="button"
                className={`a2ui-option-pill ${isSelected ? "selected" : ""}`}
                onClick={() => onSelectChoice(optionText)}
              >
                <span>{optionText}</span>
                {isSelected && <Check size={15} color="#93c5fd" />}
              </button>
            );
          })}
        </div>
      )}

      <div style={{ display: "flex", gap: 8 }}>
        <button
          type="button"
          id="approve-execute-plan-btn"
          className="a2ui-primary-btn"
          style={{ flex: 1 }}
          onClick={() =>
            onApprovePlan(surface.taskId || "task-1", currentChoice)
          }
        >
          {surface.dataModel?.plan?.diffSummary
            ? "Approve & Commit"
            : "Approve & Execute"}
        </button>
        <button
          type="button"
          className="a2ui-option-pill"
          style={{
            width: "auto",
            padding: "0 15px",
            fontWeight: 600,
            color: "var(--text-secondary)",
          }}
          onClick={() => {
            if (surface.taskId && onCancelTask) {
              onCancelTask(surface.taskId);
            } else {
              onDismissSurface(surface.surfaceId);
            }
          }}
        >
          {planArtifacts.length > 0 ? "Done" : "Cancel"}
        </button>
      </div>
    </>
  );
}

function TaskOutcomeBody({
  surface,
  isOutcomeFailed,
  outcomeError,
  outcomeVerification,
  outcomeFiles,
  onDismissSurface,
}: {
  surface: A2UISurface;
  isOutcomeFailed: boolean;
  outcomeError: string | null;
  outcomeVerification: string;
  outcomeFiles: string[];
  onDismissSurface: (surfaceId: string) => void;
}) {
  const [activeArtifactIdx, setActiveArtifactIdx] = useState<number | null>(null);
  const [showCodeFiles, setShowCodeFiles] = useState(false);
  const [savedDocUrls, setSavedDocUrls] = useState<Record<string, string>>({});

  const rawArtifacts: ArtifactItem[] =
    Array.isArray(surface.dataModel?.outcome?.artifacts)
      ? surface.dataModel.outcome.artifacts
      : [];

  // Fallback: if outcomeFiles has .md/.txt files and backend artifacts weren't populated yet
  const docFiles = outcomeFiles.filter((f) => /\.(md|txt|csv|html)$/i.test(f));
  const hasDeliverableDocs = rawArtifacts.length > 0 || docFiles.length > 0;
  const codeFiles = outcomeFiles.filter((f) => !/\.(md|txt|csv|html)$/i.test(f));

  const cleanSub = cleanDisplayText(surface.subtitle);

  if (activeArtifactIdx !== null && rawArtifacts[activeArtifactIdx]) {
    return (
      <ArtifactDetailReader
        art={rawArtifacts[activeArtifactIdx]}
        onBack={() => setActiveArtifactIdx(null)}
        savedDocUrls={savedDocUrls}
        setSavedDocUrls={setSavedDocUrls}
      />
    );
  }

  return (
    <>
      <p
        className="a2ui-subtitle"
        style={{ color: "#f1f5f9", lineHeight: 1.45, ...SUBTITLE_CLAMP_STYLE }}
      >
        {cleanSub}
      </p>

      {/* Failed Task Callout */}
      {isOutcomeFailed && (
        <div
          style={{
            display: "flex",
            alignItems: "flex-start",
            gap: 8,
            padding: "9px 12px",
            borderRadius: 12,
            background: "rgba(248, 113, 113, 0.12)",
            border: "1px solid rgba(248, 113, 113, 0.32)",
            color: "#fca5a5",
            fontSize: 12,
            fontWeight: 600,
            lineHeight: 1.4,
          }}
        >
          <AlertTriangle size={15} style={{ flexShrink: 0, marginTop: 1 }} />
          <span>{(outcomeError || cleanSub || "Task failed").slice(0, 180)}</span>
        </div>
      )}

      {/* Mode A: Deliverable Artifacts (Recipes, Grocery Lists, Research Docs, Briefs) */}
      {!isOutcomeFailed && rawArtifacts.length > 0 && (
        <ArtifactRowList
          artifacts={rawArtifacts}
          onSelectArtifact={(idx) => setActiveArtifactIdx(idx)}
        />
      )}

      {/* Mode B: Executive Engineering Receipt (when code files were updated, collapsed by default) */}
      {!isOutcomeFailed && !hasDeliverableDocs && codeFiles.length > 0 && (
        <div style={{ display: "flex", flexDirection: "column", gap: 6 }}>
          <button
            type="button"
            onClick={() => setShowCodeFiles((prev) => !prev)}
            style={{
              display: "flex",
              alignItems: "center",
              justifyContent: "space-between",
              gap: 8,
              width: "100%",
              padding: "9px 12px",
              borderRadius: 12,
              background: "rgba(16, 185, 129, 0.1)",
              border: "1px solid rgba(16, 185, 129, 0.25)",
              color: "#6ee7b7",
              fontSize: 12,
              fontWeight: 600,
              cursor: "pointer",
            }}
          >
            <span style={{ display: "inline-flex", alignItems: "center", gap: 7 }}>
              <CheckCircle2 size={14} />
              <span>{outcomeVerification}</span>
            </span>
            <span style={{ fontSize: 11, color: "#93c5fd" }}>
              {showCodeFiles ? "Hide files" : `${codeFiles.length} files`}
            </span>
          </button>

          {showCodeFiles && (
            <div style={{ display: "flex", flexWrap: "wrap", gap: 5 }}>
              {codeFiles.map((file) => (
                <span
                  key={file}
                  style={{
                    display: "inline-flex",
                    alignItems: "center",
                    gap: 5,
                    padding: "3px 8px",
                    borderRadius: 7,
                    background: "rgba(255, 255, 255, 0.04)",
                    border: "1px solid rgba(255, 255, 255, 0.07)",
                    fontSize: 10.5,
                    fontFamily: "var(--font-mono)",
                    color: "var(--text-secondary)",
                  }}
                >
                  <FileCode2 size={11} color="#93c5fd" />
                  {file}
                </span>
              ))}
            </div>
          )}
        </div>
      )}

      <button
        type="button"
        className="a2ui-option-pill"
        style={{ justifyContent: "center", fontWeight: 600 }}
        onClick={() => onDismissSurface(surface.surfaceId)}
      >
        {isOutcomeFailed ? "Dismiss" : "Done"}
      </button>
    </>
  );
}

export function A2UISurfaceDeck({
  surfaces,
  isSessionActive,
  isStackCollapsed,
  onApprovePlan,
  onCancelTask,
  onDismissSurface,
  onSelectStarterPrompt,
}: A2UISurfaceDeckProps) {
  const [selectedIdx, setSelectedIdx] = useState(0);
  const [selectedOptions, setSelectedOptions] = useState<Record<string, string>>({});

  // Backend already filters out stale (> 10m old) or dismissed terminal tasks
  const activeSurfaces = surfaces;

  // When no actionable A2UI surface is active on center stage:
  // - If Live session is active, keep stage open negative space above the bottom aurora
  // - If idle, show the ADK Sonar Hero Brand Lockup + 3 Centered Pillar Suggestions
  if (activeSurfaces.length === 0) {
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
                onClick={() => onSelectStarterPrompt(item.prompt)}
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

  const safeIdx = Math.min(selectedIdx, activeSurfaces.length - 1);
  const activeSurface = activeSurfaces[safeIdx];
  const secondarySurfaces = activeSurfaces
    .map((s, idx) => ({ surface: s, idx }))
    .filter((item) => item.idx !== safeIdx);

  const options: string[] = Array.isArray(activeSurface.dataModel?.plan?.options)
    ? activeSurface.dataModel.plan.options
    : [];
  const hasQuestions: boolean = Boolean(
    activeSurface.dataModel?.plan?.hasQuestions && options.length > 0
  );
  const planSteps: string[] = activeSurface.dataModel?.plan?.steps || [];
  const currentChoice =
    selectedOptions[activeSurface.surfaceId] ||
    activeSurface.dataModel?.plan?.selectedOption ||
    options[0] ||
    "Proceed with execution";
  const milestones: TaskMilestone[] =
    activeSurface.dataModel?.trajectory?.milestones ||
    activeSurface.dataModel?.outcome?.milestones ||
    [];
  const trajectoryElapsed: number =
    activeSurface.dataModel?.trajectory?.elapsedSeconds || 0;
  const outcomeFiles: string[] =
    activeSurface.dataModel?.outcome?.files || [];
  const outcomeStatus: string =
    activeSurface.dataModel?.outcome?.status || "completed";
  const outcomeError: string | null =
    activeSurface.dataModel?.outcome?.error || null;
  const outcomeVerification: string =
    activeSurface.dataModel?.outcome?.diffSummary ||
    activeSurface.dataModel?.outcome?.verification ||
    (outcomeFiles.length > 0
      ? `${outcomeFiles.length} file(s) updated in sandbox worktree`
      : "Completed in Vertex Sandbox");
  const isOutcomeFailed =
    outcomeStatus === "failed" ||
    outcomeStatus === "cancelled" ||
    outcomeStatus === "orphaned";

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
          {activeSurface.repo && (
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
              {activeSurface.repo}
            </span>
          )}
        </div>

        {/* 0. CONTEXT CARD (Adaptive Domain Card + Universal Fallback) */}
        {activeSurface.kind === "context_card" && (
          <>
            <ContextSurfaceBody surface={activeSurface} />

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

        {/* 1. SANDBOX RESPONSE / PLAN CHECKPOINT CARD */}
        {activeSurface.kind === "plan_approval" && (
          <PlanApprovalBody
            surface={activeSurface}
            planSteps={planSteps}
            hasQuestions={hasQuestions}
            options={options}
            currentChoice={currentChoice}
            onSelectChoice={(choice) =>
              setSelectedOptions((prev) => ({
                ...prev,
                [activeSurface.surfaceId]: choice,
              }))
            }
            onApprovePlan={onApprovePlan}
            onCancelTask={onCancelTask}
            onDismissSurface={onDismissSurface}
          />
        )}

        {/* 2. LIVE SANDBOX ACTIVITY CARD (Real-Time Harness Stream) */}
        {activeSurface.kind === "task_trajectory" && (
          <>
            <div style={{ display: "flex", alignItems: "center", justifyContent: "space-between", gap: 8 }}>
              <p className="a2ui-subtitle" style={{ color: "#e2e8f0", fontWeight: 500, margin: 0 }}>
                {activeSurface.subtitle}
              </p>
              {trajectoryElapsed > 0 && (
                <span style={{ fontSize: 11, color: "var(--text-muted)", whiteSpace: "nowrap" }}>
                  {trajectoryElapsed}s
                </span>
              )}
            </div>

            <div
              style={{
                display: "flex",
                flexDirection: "column",
                gap: 9,
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

            {activeSurface.taskId && onCancelTask && (
              <button
                type="button"
                className="a2ui-option-pill"
                style={{
                  justifyContent: "center",
                  fontWeight: 600,
                  color: "var(--text-secondary)",
                }}
                onClick={() => onCancelTask(activeSurface.taskId!)}
              >
                Cancel
              </button>
            )}
          </>
        )}

        {/* 3. SANDBOX OUTCOME CARD (Sleek Artifact Deliverables or Executive Receipt) */}
        {activeSurface.kind === "task_outcome" && (
          <TaskOutcomeBody
            surface={activeSurface}
            isOutcomeFailed={isOutcomeFailed}
            outcomeError={outcomeError}
            outcomeVerification={outcomeVerification}
            outcomeFiles={outcomeFiles}
            onDismissSurface={onDismissSurface}
          />
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
