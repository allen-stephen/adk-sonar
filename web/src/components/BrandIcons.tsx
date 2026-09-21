import React from "react";

export function GoogleLogo({ size = 18 }: { size?: number }) {
  return (
    <img
      src="/assets/google.svg"
      alt="Google"
      width={size}
      height={size}
      style={{ width: size, height: size, objectFit: "contain", display: "block" }}
    />
  );
}

export function GitHubLogo({ size = 18 }: { size?: number }) {
  return (
    <img
      src="/assets/github.png"
      alt="GitHub"
      width={size}
      height={size}
      style={{ width: size, height: size, objectFit: "contain", display: "block" }}
    />
  );
}

export function SlackLogo({ size = 18 }: { size?: number }) {
  return (
    <img
      src="/assets/slack.webp"
      alt="Slack"
      width={size}
      height={size}
      style={{ width: size, height: size, objectFit: "contain", display: "block" }}
    />
  );
}

export function GoogleMapsLogo({ size = 18 }: { size?: number }) {
  return (
    <img
      src="/assets/maps.webp"
      alt="Google Maps"
      width={size}
      height={size}
      style={{ width: size, height: size, objectFit: "contain", display: "block" }}
    />
  );
}

export function SpotifyLogo({ size = 18 }: { size?: number }) {
  return (
    <img
      src="/assets/spotify.png"
      alt="Spotify"
      width={size}
      height={size}
      style={{ width: size, height: size, objectFit: "contain", display: "block" }}
    />
  );
}

export function IntegrationBrandIcon({ name, size = 18 }: { name: string; size?: number }) {
  switch (name) {
    case "github":
      return <GitHubLogo size={size} />;
    case "slack":
      return <SlackLogo size={size} />;
    case "spotify":
      return <SpotifyLogo size={size} />;
    case "google_maps":
      return <GoogleMapsLogo size={size} />;
    case "google_search":
    case "google_calendar":
    case "gmail":
    case "google_drive":
    case "universal_search":
    default:
      return <GoogleLogo size={size} />;
  }
}

export function HarnessBrandIcon({ name, size = 18 }: { name: string; size?: number }) {
  if (name === "claude") {
    return (
      <img
        src="/assets/claude-ai.svg"
        alt="Claude Code"
        width={size}
        height={size}
        style={{ width: size, height: size, objectFit: "contain", display: "block" }}
      />
    );
  }
  if (name === "horizon") {
    return (
      <img
        src="/assets/adk-horizon.png"
        alt="ADK Long Horizon"
        width={size}
        height={size}
        style={{ width: size, height: size, objectFit: "contain", display: "block" }}
      />
    );
  }
  return (
    <img
      src="/assets/antigravity.png"
      alt="Antigravity"
      width={size}
      height={size}
      style={{ width: size, height: size, objectFit: "contain", display: "block" }}
    />
  );
}

export function SonarBrandEmblem({ size = 38 }: { size?: number }) {
  return (
    <img
      src="/assets/adk-sonar.png"
      alt="ADK Sonar"
      width={size}
      height={size}
      style={{ width: size, height: size, objectFit: "contain", display: "block" }}
    />
  );
}
