import { useEffect, useState } from "react";
import {
  ArrowLeft,
  Calendar,
  Check,
  ChevronDown,
  ChevronRight,
  ChevronUp,
  Clock,
  Copy,
  Globe,
  HardDrive,
  KeyRound,
  Mail,
  MessageSquare,
  Sliders,
  Volume2,
  X,
} from "lucide-react";
import { useQueryClient } from "@tanstack/react-query";
import {
  STATE_QUERY_KEY,
  type IntegrationItem,
  type OrchestratorState,
} from "../../lib/api";
import {
  GoogleLogo,
  HarnessBrandIcon,
  IntegrationBrandIcon,
} from "../BrandIcons";

interface ConfigurationSheetProps {
  state: OrchestratorState;
  onClose: () => void;
  onSelectHarness: (harnessName: string) => void;
  onToggleIntegration: (name: string, enabled: boolean) => void;
  onConfigureWorkspace: (payload: {
    token?: string;
    disconnect?: boolean;
    surfaces?: Record<string, boolean>;
  }) => void;
  onSaveSecret: (name: string, value: string) => void;
  onImportDotenv?: (content: string, filename?: string) => void;
}

// Explicit ordering:
// 1. Google Workspace (top row)
// 2. Google Search
// 3. Google Maps
// 4. GitHub
// 5. Spotify
// 6. Slack
const PRIMARY_CONNECTORS = [
  "google_search",
  "google_maps",
  "github",
  "spotify",
  "slack",
];

const FRIENDLY_NAMES: Record<string, { title: string; subtitle: string }> = {
  google_search: {
    title: "Google Search",
    subtitle: "Live web & documentation grounding",
  },
  google_maps: {
    title: "Google Maps",
    subtitle: "Places, routes & local search",
  },
  github: {
    title: "GitHub",
    subtitle: "Pull requests, issues & CI checks",
  },
  spotify: {
    title: "Spotify",
    subtitle: "Playback, focus playlists & queue control",
  },
  slack: {
    title: "Slack",
    subtitle: "Channel threads & status updates",
  },
};

const HARNESS_META: Record<string, { short: string; tagline: string }> = {
  claude: {
    short: "Claude Code",
    tagline: "Anthropic headless CLI worker for fast repository edits & PRs",
  },
  horizon: {
    short: "Horizon",
    tagline: "ADK A2A long-horizon agent for multi-step research & coding",
  },
  antigravity: {
    short: "Antigravity",
    tagline: "DeepMind agentic harness for deep verification & refactors",
  },
};

export function ConnectionsSheet({
  state,
  onClose,
  onSelectHarness,
  onToggleIntegration,
  onConfigureWorkspace,
  onSaveSecret,
}: ConfigurationSheetProps) {
  const qc = useQueryClient();
  const ws = state.workspace_connection;
  const [wsExpanded, setWsExpanded] = useState(false);

  // Only ONE connector drawer can ever be open at a time, and ONLY when clicked by the user
  const [editingConnector, setEditingConnector] = useState<string | null>(null);
  const [clientIdInput, setClientIdInput] = useState("");
  const [clientSecretInput, setClientSecretInput] = useState("");
  const [inlineToken, setInlineToken] = useState("");
  const [copiedUri, setCopiedUri] = useState(false);
  const [isBusy, setIsBusy] = useState(false);

  // Listen for OAuth popup completion
  useEffect(() => {
    const handler = (event: MessageEvent) => {
      const data = event.data;
      if (data && data.type === "adk_oauth_complete") {
        void qc.invalidateQueries({ queryKey: STATE_QUERY_KEY });
        if (data.ok) {
          setEditingConnector(null);
        }
      }
    };
    window.addEventListener("message", handler);
    return () => window.removeEventListener("message", handler);
  }, [qc]);

  const primaryList: IntegrationItem[] = [...state.integrations]
    .filter((item) => PRIMARY_CONNECTORS.includes(item.name))
    .sort(
      (a, b) =>
        PRIMARY_CONNECTORS.indexOf(a.name) - PRIMARY_CONNECTORS.indexOf(b.name)
    );

  const openOAuthPopup = async (
    provider: string,
    cid?: string,
    csec?: string
  ) => {
    setIsBusy(true);
    try {
      const res = await fetch(`/api/v1/auth/${provider}/authorize`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          client_id: cid?.trim() || undefined,
          client_secret: csec?.trim() || undefined,
        }),
      });
      const data = await res.json();
      if (res.ok && data.authorize_url) {
        setClientIdInput("");
        setClientSecretInput("");
        setEditingConnector(null);
        void qc.invalidateQueries({ queryKey: STATE_QUERY_KEY });
        window.open(
          data.authorize_url,
          `adk_oauth_${provider}`,
          "width=520,height=660,menubar=no,toolbar=no,location=no,status=no"
        );
      }
    } finally {
      setIsBusy(false);
    }
  };

  const handleToggleRow = async (item: IntegrationItem) => {
    const isActive = item.enabled && (!item.auth_env || item.has_credentials);

    // Turning OFF an active integration
    if (isActive) {
      setEditingConnector(null);
      onToggleIntegration(item.name, false);
      return;
    }

    // Turning ON an integration that already has credentials (or needs none)
    if (!item.auth_env || item.has_credentials) {
      onToggleIntegration(item.name, true);
      return;
    }

    const providerKey = item.auth?.provider || item.name;

    // Fast-path 1: If OAuth Client ID & Secret are already in .env (e.g. Spotify/GitHub/Slack),
    // immediately open the OAuth popup without expanding any form!
    if (item.auth?.oauth_ready) {
      onToggleIntegration(item.name, true);
      await openOAuthPopup(providerKey);
      return;
    }

    // Fast-path 2: For GitHub on local dev, try 1-click `gh auth token` automatically first
    if (item.name === "github" && item.auth?.cli_available) {
      const res = await fetch("/api/v1/auth/github/detect-cli", {
        method: "POST",
      });
      if (res.ok) {
        await qc.invalidateQueries({ queryKey: STATE_QUERY_KEY });
        return;
      }
    }

    // Otherwise, open ONLY this row's compact inline setup
    setEditingConnector((prev) => (prev === item.name ? null : item.name));
    setInlineToken("");
    setClientIdInput("");
    setClientSecretInput("");
  };

  const handleSaveToken = async (item: IntegrationItem) => {
    const tok = inlineToken.trim();
    if (!tok) return;
    setIsBusy(true);
    const providerKey = item.auth?.provider || item.name;
    try {
      const res = await fetch(`/api/v1/auth/${providerKey}/token`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ token: tok }),
      });
      if (res.ok) {
        await qc.invalidateQueries({ queryKey: STATE_QUERY_KEY });
      } else if (item.auth_env) {
        onSaveSecret(item.auth_env, tok);
        onToggleIntegration(item.name, true);
      }
      setInlineToken("");
      setEditingConnector(null);
    } finally {
      setIsBusy(false);
    }
  };

  const handleCopyRedirectUri = async (uri: string) => {
    try {
      await navigator.clipboard.writeText(uri);
      setCopiedUri(true);
      setTimeout(() => setCopiedUri(false), 1500);
    } catch {
      // ignore
    }
  };

  const [activeView, setActiveView] = useState<"connections" | "preferences">(
    "connections"
  );

  const browserTimezone =
    typeof Intl !== "undefined"
      ? Intl.DateTimeFormat().resolvedOptions().timeZone || "America/Chicago"
      : "America/Chicago";

  const prefs = state.preferences || {
    timezone: browserTimezone,
    tz_abbrev: "Local",
    tz_offset: "UTC",
    local_time_formatted: "",
    local_clock_short: "",
    voice_name: "Aoede",
    speech_style: "concise" as const,
    ack_async_tools: true,
    available_voices: [
      { name: "Aoede", tagline: "Warm & Articulate" },
      { name: "Puck", tagline: "Crisp & Brisk" },
      { name: "Kore", tagline: "Clear & Direct" },
      { name: "Charon", tagline: "Deep & Calm" },
      { name: "Fenrir", tagline: "Dynamic & Fast" },
      { name: "Orbit", tagline: "Balanced & Neutral" },
    ],
    common_timezones: [
      "America/Los_Angeles",
      "America/Denver",
      "America/Chicago",
      "America/New_York",
      "UTC",
      "Europe/London",
      "Europe/Berlin",
      "Asia/Tokyo",
    ],
  };

  const handleUpdatePreferences = async (patch: {
    timezone?: string;
    voice_name?: string;
    speech_style?: "concise" | "balanced" | "detailed";
    ack_async_tools?: boolean;
  }) => {
    setIsBusy(true);
    try {
      await fetch("/api/v1/preferences", {
        method: "PATCH",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(patch),
      });
      await qc.invalidateQueries({ queryKey: STATE_QUERY_KEY });
    } finally {
      setIsBusy(false);
    }
  };

  return (
    <div className="sheet-backdrop" onClick={onClose}>
      <div
        className="bottom-sheet-panel"
        onClick={(e) => e.stopPropagation()}
        data-testid="configuration-sheet"
      >
        <div className="sheet-handle-bar" />
        <div className="sheet-header">
          {activeView === "preferences" ? (
            <button
              type="button"
              onClick={() => setActiveView("connections")}
              style={{
                display: "inline-flex",
                alignItems: "center",
                gap: 6,
                background: "transparent",
                border: "none",
                color: "#60a5fa",
                fontSize: 14,
                fontWeight: 600,
                cursor: "pointer",
                padding: 0,
              }}
            >
              <ArrowLeft size={16} />
              <span>Advanced Settings</span>
            </button>
          ) : (
            <span className="sheet-title">Configuration</span>
          )}
          <button
            type="button"
            className="sheet-close-btn"
            onClick={onClose}
            aria-label="Close Configuration Sheet"
          >
            <X size={16} />
          </button>
        </div>

        {activeView === "preferences" ? (
          <div className="sheet-body" style={{ gap: 18 }}>
            {/* 1. Timezone */}
            <div style={{ display: "flex", flexDirection: "column", gap: 8 }}>
              <div
                style={{
                  display: "flex",
                  alignItems: "center",
                  justifyContent: "space-between",
                }}
              >
                <span
                  style={{
                    fontSize: 11.5,
                    fontWeight: 600,
                    textTransform: "uppercase",
                    letterSpacing: "0.06em",
                    color: "var(--text-secondary)",
                  }}
                >
                  Timezone
                </span>
                {prefs.local_clock_short && (
                  <span
                    style={{
                      fontSize: 11.5,
                      fontWeight: 500,
                      color: "var(--text-muted)",
                    }}
                  >
                    {prefs.local_clock_short}
                  </span>
                )}
              </div>

              <div style={{ display: "flex", gap: 8 }}>
                <select
                  className="dark-input"
                  value={prefs.timezone}
                  onChange={(e) =>
                    void handleUpdatePreferences({ timezone: e.target.value })
                  }
                  style={{
                    flex: 1,
                    cursor: "pointer",
                    fontFamily: "var(--font-sans)",
                    fontSize: 13,
                    padding: "10px 12px",
                    borderRadius: 12,
                  }}
                >
                  {Array.from(
                    new Set([
                      browserTimezone,
                      prefs.timezone,
                      ...(prefs.common_timezones || []),
                    ])
                  ).map((tz) => (
                    <option key={tz} value={tz} style={{ background: "#0f141d" }}>
                      {tz.replace(/_/g, " ")} {tz === browserTimezone ? "· Auto" : ""}
                    </option>
                  ))}
                </select>

                {prefs.timezone !== browserTimezone && (
                  <button
                    type="button"
                    className="small-action-btn"
                    onClick={() =>
                      void handleUpdatePreferences({ timezone: browserTimezone })
                    }
                    style={{ borderRadius: 12 }}
                  >
                    Auto
                  </button>
                )}
              </div>
            </div>

            {/* 2. Voice */}
            <div style={{ display: "flex", flexDirection: "column", gap: 8 }}>
              <div
                style={{
                  fontSize: 11.5,
                  fontWeight: 600,
                  textTransform: "uppercase",
                  letterSpacing: "0.06em",
                  color: "var(--text-secondary)",
                }}
              >
                Voice
              </div>

              <div
                style={{
                  display: "grid",
                  gridTemplateColumns: "repeat(3, 1fr)",
                  padding: 4,
                  borderRadius: 14,
                  background: "rgba(10, 12, 18, 0.75)",
                  border: "1px solid var(--border-subtle)",
                  gap: 4,
                }}
              >
                {(prefs.available_voices || []).map((v) => {
                  const isSelected = prefs.voice_name === v.name;
                  return (
                    <button
                      key={v.name}
                      type="button"
                      onClick={() =>
                        void handleUpdatePreferences({ voice_name: v.name })
                      }
                      style={{
                        display: "flex",
                        alignItems: "center",
                        justifyContent: "center",
                        gap: 6,
                        padding: "9px 8px",
                        borderRadius: 10,
                        border: isSelected
                          ? "1px solid rgba(96, 165, 250, 0.45)"
                          : "1px solid transparent",
                        background: isSelected
                          ? "rgba(59, 130, 246, 0.22)"
                          : "transparent",
                        color: isSelected ? "#f8fafc" : "var(--text-secondary)",
                        fontSize: 12.5,
                        fontWeight: isSelected ? 600 : 500,
                        cursor: "pointer",
                        transition: "all 0.15s ease",
                      }}
                    >
                      <span>{v.name}</span>
                    </button>
                  );
                })}
              </div>
            </div>

            {/* 3. Response Style */}
            <div style={{ display: "flex", flexDirection: "column", gap: 8 }}>
              <div
                style={{
                  fontSize: 11.5,
                  fontWeight: 600,
                  textTransform: "uppercase",
                  letterSpacing: "0.06em",
                  color: "var(--text-secondary)",
                }}
              >
                Response Style
              </div>

              <div
                style={{
                  display: "grid",
                  gridTemplateColumns: "repeat(3, 1fr)",
                  padding: 4,
                  borderRadius: 14,
                  background: "rgba(10, 12, 18, 0.75)",
                  border: "1px solid var(--border-subtle)",
                  gap: 4,
                }}
              >
                {(
                  [
                    { id: "concise", label: "Concise" },
                    { id: "balanced", label: "Balanced" },
                    { id: "detailed", label: "Detailed" },
                  ] as const
                ).map((opt) => {
                  const active = prefs.speech_style === opt.id;
                  return (
                    <button
                      key={opt.id}
                      type="button"
                      onClick={() =>
                        void handleUpdatePreferences({ speech_style: opt.id })
                      }
                      style={{
                        padding: "9px 8px",
                        borderRadius: 10,
                        border: active
                          ? "1px solid rgba(96, 165, 250, 0.45)"
                          : "1px solid transparent",
                        background: active
                          ? "rgba(59, 130, 246, 0.22)"
                          : "transparent",
                        color: active ? "#f8fafc" : "var(--text-secondary)",
                        fontSize: 12.5,
                        fontWeight: active ? 600 : 500,
                        cursor: "pointer",
                        transition: "all 0.15s ease",
                      }}
                    >
                      {opt.label}
                    </button>
                  );
                })}
              </div>
            </div>
          </div>
        ) : (
        <div className="sheet-body" style={{ gap: 20 }}>
          {/* SECTION 1: Segmented Coding Engine Picker */}
          <div style={{ display: "flex", flexDirection: "column", gap: 8 }}>
            <div
              style={{
                fontSize: 11.5,
                fontWeight: 600,
                textTransform: "uppercase",
                letterSpacing: "0.06em",
                color: "var(--text-secondary)",
              }}
            >
              Coding Harness
            </div>

            <div
              style={{
                display: "grid",
                gridTemplateColumns: "repeat(3, 1fr)",
                padding: 4,
                borderRadius: 14,
                background: "rgba(10, 12, 18, 0.75)",
                border: "1px solid var(--border-subtle)",
                gap: 4,
              }}
            >
              {[...state.harnesses]
                .sort((a, b) => {
                  const rank: Record<string, number> = {
                    horizon: 0,
                    antigravity: 1,
                    claude: 2,
                  };
                  return (rank[a.name] ?? 99) - (rank[b.name] ?? 99);
                })
                .map((h) => {
                  const isSelected = state.default_harness.name === h.name;
                  const meta = HARNESS_META[h.name] || { short: h.display_name };
                  return (
                    <button
                      key={h.name}
                      type="button"
                      onClick={() => onSelectHarness(h.name)}
                      style={{
                        display: "flex",
                        alignItems: "center",
                        justifyContent: "center",
                        gap: 6,
                        padding: "9px 8px",
                        borderRadius: 10,
                        border: isSelected
                          ? "1px solid rgba(96, 165, 250, 0.45)"
                          : "1px solid transparent",
                        background: isSelected
                          ? "rgba(59, 130, 246, 0.22)"
                          : "transparent",
                        color: isSelected ? "#f8fafc" : "var(--text-secondary)",
                        fontSize: 12.5,
                        fontWeight: isSelected ? 600 : 500,
                        cursor: "pointer",
                        transition: "all 0.15s ease",
                      }}
                    >
                      <HarnessBrandIcon name={h.name} size={16} />
                      <span>{meta.short}</span>
                    </button>
                  );
                })}
            </div>
          </div>

          {/* SECTION 2: Connected Apps (Workspace -> Search -> Maps -> GitHub -> Spotify -> Slack) */}
          <div style={{ display: "flex", flexDirection: "column", gap: 8 }}>
            <div
              style={{
                fontSize: 11.5,
                fontWeight: 600,
                textTransform: "uppercase",
                letterSpacing: "0.06em",
                color: "var(--text-secondary)",
              }}
            >
              Connected Apps
            </div>

            {/* 1. Google Workspace Row */}
            <div
              style={{
                borderRadius: 16,
                background: "rgba(255, 255, 255, 0.03)",
                border: "1px solid var(--border-subtle)",
                padding: "12px 14px",
                display: "flex",
                flexDirection: "column",
                gap: 10,
              }}
            >
              <div
                style={{
                  display: "flex",
                  alignItems: "center",
                  justifyContent: "space-between",
                  gap: 10,
                }}
              >
                <div
                  style={{
                    display: "flex",
                    alignItems: "center",
                    gap: 11,
                    cursor: "pointer",
                    flex: 1,
                  }}
                  onClick={() => setWsExpanded((prev) => !prev)}
                >
                  <div
                    style={{
                      width: 36,
                      height: 36,
                      borderRadius: 10,
                      background: "rgba(255, 255, 255, 0.05)",
                      border: "1px solid rgba(255, 255, 255, 0.07)",
                      display: "flex",
                      alignItems: "center",
                      justifyContent: "center",
                      flexShrink: 0,
                    }}
                  >
                    <GoogleLogo size={19} />
                  </div>
                  <div>
                    <div
                      style={{
                        display: "flex",
                        alignItems: "center",
                        gap: 6,
                        fontSize: 14,
                        fontWeight: 600,
                      }}
                    >
                      <span>Google Workspace</span>
                      {wsExpanded ? (
                        <ChevronUp size={14} color="#94a3b8" />
                      ) : (
                        <ChevronDown size={14} color="#94a3b8" />
                      )}
                    </div>
                    <div
                      style={{
                        fontSize: 12,
                        color: ws.connected ? "#10b981" : "var(--text-muted)",
                      }}
                    >
                      {ws.connected
                        ? `${ws.active_surfaces_count} services enabled (Calendar, Gmail, Drive)`
                        : "Tap toggle to connect Calendar, Gmail & Drive"}
                    </div>
                  </div>
                </div>

                <button
                  type="button"
                  role="switch"
                  aria-checked={ws.connected}
                  className={`toggle-switch ${ws.connected ? "checked" : ""}`}
                  onClick={async () => {
                    if (ws.connected) {
                      onConfigureWorkspace({ disconnect: true });
                    } else if (ws.auth?.cli_available) {
                      const res = await fetch(
                        "/api/v1/auth/google_workspace/detect-cli",
                        { method: "POST" }
                      );
                      if (res.ok) {
                        void qc.invalidateQueries({ queryKey: STATE_QUERY_KEY });
                        return;
                      }
                    } else if (ws.auth?.oauth_ready) {
                      await openOAuthPopup("google_workspace");
                    }
                  }}
                >
                  <span className="toggle-thumb" />
                </button>
              </div>

              {wsExpanded && (
                <div
                  className="surface-pills-grid"
                  style={{
                    paddingTop: 10,
                    borderTop: "1px solid rgba(255,255,255,0.06)",
                  }}
                >
                  {ws.surfaces.map((surf) => {
                    const SurfaceIcon =
                      surf.name === "google_calendar"
                        ? Calendar
                        : surf.name === "gmail"
                          ? Mail
                          : surf.name === "google_drive"
                            ? HardDrive
                            : MessageSquare;

                    return (
                      <button
                        key={surf.name}
                        type="button"
                        className={`surface-pill-btn ${surf.enabled ? "active" : ""}`}
                        onClick={() =>
                          onConfigureWorkspace({
                            surfaces: { [surf.name]: !surf.enabled },
                          })
                        }
                      >
                        <span className="surface-pill-left">
                          <SurfaceIcon
                            size={13.5}
                            style={{
                              color: surf.enabled ? "#60a5fa" : "#64748b",
                              flexShrink: 0,
                            }}
                          />
                          <span>{surf.label}</span>
                        </span>
                        <span
                          className={`surface-pill-led ${surf.enabled ? "on" : ""}`}
                        />
                      </button>
                    );
                  })}
                </div>
              )}
            </div>

            {/* 2..6: Google Search, Google Maps, GitHub, Spotify, Slack */}
            {primaryList.map((item) => {
              const meta = FRIENDLY_NAMES[item.name] || {
                title: item.display_name,
                subtitle: item.description,
              };
              const isReady =
                item.enabled && (!item.auth_env || item.has_credentials);
              const isEditing = editingConnector === item.name;
              const providerKey = item.auth?.provider || item.name;
              const redirectUri =
                item.auth?.redirect_uri ||
                `${window.location.origin}/api/v1/auth/${providerKey}/callback`;

              return (
                <div
                  key={item.name}
                  style={{
                    borderRadius: 16,
                    background: "rgba(255, 255, 255, 0.03)",
                    border: isEditing
                      ? "1px solid rgba(96, 165, 250, 0.35)"
                      : "1px solid var(--border-subtle)",
                    padding: "12px 14px",
                    display: "flex",
                    flexDirection: "column",
                    gap: 10,
                  }}
                >
                  <div
                    style={{
                      display: "flex",
                      alignItems: "center",
                      justifyContent: "space-between",
                      gap: 12,
                    }}
                  >
                    <div style={{ display: "flex", alignItems: "center", gap: 11 }}>
                      <div
                        style={{
                          width: 36,
                          height: 36,
                          borderRadius: 10,
                          background: "rgba(255, 255, 255, 0.05)",
                          border: "1px solid rgba(255, 255, 255, 0.07)",
                          display: "flex",
                          alignItems: "center",
                          justifyContent: "center",
                          flexShrink: 0,
                        }}
                      >
                        <IntegrationBrandIcon name={item.name} size={19} />
                      </div>
                      <div className="integration-info">
                        <div className="integration-name-line">
                          <span>{meta.title}</span>
                          {isReady && (
                            <span
                              style={{
                                width: 6,
                                height: 6,
                                borderRadius: "50%",
                                background: "#10b981",
                                display: "inline-block",
                              }}
                            />
                          )}
                        </div>
                        <div
                          style={{
                            fontSize: 12,
                            color: "var(--text-muted)",
                          }}
                        >
                          {isReady && item.auth?.account_label
                            ? `${item.auth.account_label} · ${meta.subtitle}`
                            : meta.subtitle}
                        </div>
                      </div>
                    </div>

                    <div style={{ display: "flex", alignItems: "center", gap: 8 }}>
                      {item.auth_env && !item.auth?.oauth_ready && !item.has_credentials && (
                        <button
                          type="button"
                          onClick={() =>
                            setEditingConnector((prev) =>
                              prev === item.name ? null : item.name
                            )
                          }
                          style={{
                            background: "rgba(255,255,255,0.05)",
                            border: "1px solid rgba(255,255,255,0.08)",
                            borderRadius: 8,
                            padding: "5px 7px",
                            color: isEditing ? "#60a5fa" : "#94a3b8",
                            cursor: "pointer",
                            display: "flex",
                            alignItems: "center",
                          }}
                          title={`Configure ${meta.title}`}
                        >
                          <KeyRound size={13} />
                        </button>
                      )}
                      <button
                        type="button"
                        role="switch"
                        aria-checked={isReady}
                        className={`toggle-switch ${isReady ? "checked" : ""}`}
                        onClick={() => void handleToggleRow(item)}
                      >
                        <span className="toggle-thumb" />
                      </button>
                    </div>
                  </div>

                  {/* Single Compact Drawer: Only opens when credentials are NOT already in .env */}
                  {isEditing && item.auth_env && (
                    <div
                      style={{
                        display: "flex",
                        flexDirection: "column",
                        gap: 6,
                        paddingTop: 8,
                        borderTop: "1px solid rgba(255,255,255,0.06)",
                      }}
                    >
                      {item.name === "spotify" ? (
                        item.auth?.oauth_ready ? (
                          <div
                            style={{
                              display: "flex",
                              alignItems: "center",
                              justifyContent: "space-between",
                              gap: 8,
                            }}
                          >
                            <span style={{ fontSize: 12, color: "#94a3b8" }}>
                              Client credentials loaded from <code>.env</code>
                            </span>
                            <button
                              type="button"
                              className="small-action-btn"
                              disabled={isBusy}
                              style={{
                                display: "inline-flex",
                                alignItems: "center",
                                gap: 5,
                                padding: "7px 14px",
                              }}
                              onClick={() => void openOAuthPopup("spotify")}
                            >
                              <Check size={13} />
                              Authorize Spotify
                            </button>
                          </div>
                        ) : (
                          <div
                            style={{
                              display: "flex",
                              flexDirection: "column",
                              gap: 8,
                            }}
                          >
                            <input
                              type="text"
                              className="dark-input"
                              style={{ width: "100%", boxSizing: "border-box" }}
                              placeholder="Spotify Client ID"
                              value={clientIdInput}
                              onChange={(e) => setClientIdInput(e.target.value)}
                            />
                            <input
                              type="password"
                              className="dark-input"
                              style={{ width: "100%", boxSizing: "border-box" }}
                              placeholder="Spotify Client Secret"
                              value={clientSecretInput}
                              onChange={(e) =>
                                setClientSecretInput(e.target.value)
                              }
                            />
                            <div
                              style={{
                                display: "flex",
                                alignItems: "center",
                                justifyContent: "space-between",
                                gap: 8,
                                paddingTop: 2,
                              }}
                            >
                              <button
                                type="button"
                                onClick={() =>
                                  void handleCopyRedirectUri(redirectUri)
                                }
                                title={redirectUri}
                                style={{
                                  background: "rgba(255, 255, 255, 0.04)",
                                  border: "1px solid rgba(255, 255, 255, 0.08)",
                                  borderRadius: 8,
                                  color: copiedUri ? "#10b981" : "#94a3b8",
                                  fontSize: 11.5,
                                  cursor: "pointer",
                                  display: "inline-flex",
                                  alignItems: "center",
                                  gap: 5,
                                  padding: "6px 10px",
                                }}
                              >
                                <Copy size={11.5} />
                                {copiedUri
                                  ? "Copied Callback URI"
                                  : "Copy Redirect URI"}
                              </button>
                              <button
                                type="button"
                                className="small-action-btn"
                                disabled={isBusy}
                                style={{
                                  display: "inline-flex",
                                  alignItems: "center",
                                  gap: 5,
                                  padding: "7px 14px",
                                }}
                                onClick={() =>
                                  void openOAuthPopup(
                                    "spotify",
                                    clientIdInput,
                                    clientSecretInput
                                  )
                                }
                              >
                                <Check size={13} />
                                Authorize Spotify
                              </button>
                            </div>
                          </div>
                        )
                      ) : (
                        <div style={{ display: "flex", gap: 8 }}>
                          <input
                            type="password"
                            className="dark-input"
                            placeholder={
                              item.name === "slack"
                                ? "Paste Slack Bot Token (xoxb-...)"
                                : "Paste GitHub token (ghp_...)"
                            }
                            value={inlineToken}
                            onChange={(e) => setInlineToken(e.target.value)}
                          />
                          <button
                            type="button"
                            className="small-action-btn"
                            disabled={isBusy}
                            style={{
                              display: "inline-flex",
                              alignItems: "center",
                              gap: 5,
                            }}
                            onClick={() => void handleSaveToken(item)}
                          >
                            <Check size={13} />
                            Connect
                          </button>
                        </div>
                      )}
                    </div>
                  )}
                </div>
              );
            })}
          </div>

          {/* SECTION 3: Advanced Settings */}
          <div style={{ display: "flex", flexDirection: "column", gap: 8 }}>
            <div
              style={{
                fontSize: 11.5,
                fontWeight: 600,
                textTransform: "uppercase",
                letterSpacing: "0.06em",
                color: "var(--text-secondary)",
              }}
            >
              Advanced Settings
            </div>
            <button
              type="button"
              onClick={() => setActiveView("preferences")}
              data-testid="open-preferences-btn"
              style={{
                width: "100%",
                borderRadius: 16,
                background: "rgba(255, 255, 255, 0.03)",
                border: "1px solid var(--border-subtle)",
                padding: "12px 14px",
                display: "flex",
                alignItems: "center",
                justifyContent: "space-between",
                gap: 12,
                cursor: "pointer",
                color: "var(--text-primary)",
                textAlign: "left",
                transition: "all 0.15s ease",
              }}
            >
              <div style={{ display: "flex", alignItems: "center", gap: 11 }}>
                <div
                  style={{
                    width: 36,
                    height: 36,
                    borderRadius: 10,
                    background: "rgba(59, 130, 246, 0.12)",
                    border: "1px solid rgba(96, 165, 250, 0.25)",
                    display: "flex",
                    alignItems: "center",
                    justifyContent: "center",
                    color: "#60a5fa",
                    flexShrink: 0,
                  }}
                >
                  <Sliders size={17} />
                </div>
                <div className="integration-info">
                  <div className="integration-name-line">
                    <span>Advanced Settings</span>
                  </div>
                  <div style={{ fontSize: 12, color: "var(--text-muted)" }}>
                    {prefs.timezone} ({prefs.tz_abbrev}) · {prefs.voice_name} Voice ·{" "}
                    {prefs.speech_style.charAt(0).toUpperCase() +
                      prefs.speech_style.slice(1)}
                  </div>
                </div>
              </div>

              <ChevronRight size={17} color="#94a3b8" />
            </button>
          </div>
        </div>
        )}
      </div>
    </div>
  );
}
