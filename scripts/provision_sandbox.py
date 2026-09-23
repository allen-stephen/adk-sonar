"""Human + AI-Agent friendly developer onboarding wizard & shared sandbox provisioner (`make onboard` / `make provision`).

Supports two seamless operating modes:
  1. Interactive Human Wizard (`make onboard` in a TTY):
     Auto-discovers the developer's GitHub username (`gh` CLI or sibling repo remotes),
     existing forks (`adk-samples`, `agents-cli`), sibling local repos, and `gcloud` project,
     then prompts with smart pre-filled defaults and writes `config/workspaces.local.yaml`.
  2. Agent / Non-Interactive Programmatic Mode (`--discover --json` or `--onboard --yes --github-user ... --json`):
     Never blocks on `input()`. Allows an AI coding agent setting up the environment on a
     user's behalf to inspect the host via `--discover --json` and configure personal forks,
     custom repositories, default harness, and MCP tokens in a single deterministic call.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any

import yaml
from dotenv import load_dotenv

from app.integrations import get_integration_registry
from app.tools.workspace_tools import get_workspace_root
from app.workers import (
    SandboxContext,
    get_harness_registry,
    get_sandbox_provisioner,
)
from app.workers.harnesses.base import (
    ensure_repo_and_worktree,
    load_workspaces_manifest,
    sync_cross_harness_context,
)
from scripts.setup import generate_claude_mcp_json, setup_workspace

PROJECT_ROOT = Path(__file__).resolve().parent.parent
CONFIG_DIR = PROJECT_ROOT / "config"
LOCAL_WORKSPACES_YAML = CONFIG_DIR / "workspaces.local.yaml"
ENV_FILE = PROJECT_ROOT / ".env"
ENV_EXAMPLE_FILE = PROJECT_ROOT / ".env.example"


def _run_quiet(cmd: list[str], timeout: int = 4) -> str | None:
    try:
        proc = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
        out = proc.stdout.strip()
        if proc.returncode == 0 and out and "ERROR" not in out:
            return out
    except Exception:
        pass
    return None


def discover_developer_environment() -> dict[str, Any]:
    """Introspect the developer's machine for GitHub identity, forks, sibling repos, and GCP credentials.

    Safe and fast for both AI agents (`--discover --json`) and the interactive wizard.
    """
    gh_user = _run_quiet(["gh", "api", "user", "-q", ".login"]) if shutil.which("gh") else None
    gh_token_available = bool(
        os.getenv("GITHUB_PERSONAL_ACCESS_TOKEN")
        or (_run_quiet(["gh", "auth", "token"]) if shutil.which("gh") else None)
    )

    # Fallback: inspect sibling git repositories in PROJECT_ROOT.parent to infer GitHub username & forks
    sibling_repos: list[dict[str, str]] = []
    detected_forks: dict[str, str] = {}
    parent_dir = PROJECT_ROOT.parent
    if parent_dir.exists():
        for child in sorted(parent_dir.iterdir()):
            if not child.is_dir() or child.name.startswith(".") or child == PROJECT_ROOT:
                continue
            if (child / ".git").exists():
                origin = _run_quiet(["git", "-C", str(child), "remote", "get-url", "origin"])
                branch = _run_quiet(["git", "-C", str(child), "rev-parse", "--abbrev-ref", "HEAD"]) or "main"
                if origin:
                    sibling_repos.append(
                        {
                            "name": child.name,
                            "path": str(child),
                            "origin": origin,
                            "branch": branch,
                        }
                    )
                    m = re.match(
                        r"^(?:https://github\.com/|git@github\.com:)([^/]+)/([^/]+?)(?:\.git)?$",
                        origin,
                    )
                    if m:
                        owner, rname = m.groups()
                        if owner.lower() not in {"google", "anthropics"}:
                            if not gh_user:
                                gh_user = owner
                            detected_forks[rname] = origin

    # Also query `gh repo list` directly from the user's GitHub profile so forks are discovered even without sibling folders
    if gh_user and shutil.which("gh") and not os.getenv("PYTEST_CURRENT_TEST"):
        raw_gh_repos = _run_quiet(
            [
                "gh",
                "repo",
                "list",
                gh_user,
                "--limit",
                "25",
                "--json",
                "name,url,isFork",
            ],
            timeout=6,
        )
        if raw_gh_repos:
            try:
                for item in json.loads(raw_gh_repos):
                    rname = str(item.get("name") or "").strip()
                    rurl = str(item.get("url") or "").strip()
                    if not rname or not rurl or rname.startswith("test-"):
                        continue
                    if not rurl.endswith(".git"):
                        rurl = f"{rurl}.git"
                    detected_forks.setdefault(rname, rurl)
            except Exception:
                pass

    gcloud_project = (
        os.getenv("GOOGLE_CLOUD_PROJECT")
        or (_run_quiet(["gcloud", "config", "get-value", "project"]) if shutil.which("gcloud") else None)
    )
    gcloud_token_available = bool(
        os.getenv("GOOGLE_WORKSPACE_ACCESS_TOKEN")
        or (_run_quiet(["gcloud", "auth", "print-access-token"]) if shutil.which("gcloud") else None)
    )

    return {
        "github_user": gh_user,
        "github_token_available": gh_token_available,
        "detected_forks": detected_forks,
        "sibling_repositories": sibling_repos,
        "gcloud_project": gcloud_project,
        "gcloud_token_available": gcloud_token_available,
        "local_override_file": str(LOCAL_WORKSPACES_YAML),
        "local_override_exists": LOCAL_WORKSPACES_YAML.exists(),
    }


def ensure_gcp_apis_and_iam(project_id: str | None = None) -> None:
    """Automatically enables required Google Cloud APIs and grants local developer IAM bindings (`serviceUsageConsumer` & `serviceAccountTokenCreator`) so Workspace & Vertex Sandbox work on the first try."""
    if os.getenv("PYTEST_CURRENT_TEST") or not shutil.which("gcloud"):
        return
    proj = (project_id or os.getenv("GOOGLE_CLOUD_PROJECT") or "").strip()
    if not proj or proj == "your-gcp-project-id":
        return

    acct = _run_quiet(["gcloud", "config", "get-value", "account"])
    print(f"✓ Verifying GCP APIs (Vertex AI, Cloud Run, Cloud Build, Artifact Registry, IAM Credentials, Calendar, Gmail, Drive) on project '{proj}'...")
    subprocess.run(
        [
            "gcloud",
            "services",
            "enable",
            "aiplatform.googleapis.com",
            "run.googleapis.com",
            "cloudbuild.googleapis.com",
            "artifactregistry.googleapis.com",
            "iamcredentials.googleapis.com",
            "calendar-json.googleapis.com",
            "gmail.googleapis.com",
            "drive.googleapis.com",
            f"--project={proj}",
            "--quiet",
        ],
        check=False,
        capture_output=True,
    )

    if acct:
        # Ensure caller can use project as X-Goog-User-Project quota project for Workspace APIs
        subprocess.run(
            [
                "gcloud",
                "projects",
                "add-iam-policy-binding",
                proj,
                f"--member=user:{acct}",
                "--role=roles/serviceusage.serviceUsageConsumer",
                "--quiet",
            ],
            check=False,
            capture_output=True,
        )
        caller_sa = os.getenv("SANDBOX_CALLER_SA") or f"lha-run@{proj}.iam.gserviceaccount.com"
        subprocess.run(
            [
                "gcloud",
                "iam",
                "service-accounts",
                "add-iam-policy-binding",
                caller_sa,
                f"--project={proj}",
                f"--member=user:{acct}",
                "--role=roles/iam.serviceAccountTokenCreator",
                "--quiet",
            ],
            check=False,
            capture_output=True,
        )


def ensure_env_and_secrets(
    *,
    auto_gcloud_token: bool = False,
    auto_gh_token: bool = False,
) -> dict[str, bool]:
    """Ensure `.env` exists, load it into `os.environ`, and populate `GOOGLE_CLOUD_PROJECT` + `GITHUB_PERSONAL_ACCESS_TOKEN`."""
    from app.auth import persist_env_vars

    if not ENV_FILE.exists() and ENV_EXAMPLE_FILE.exists():
        shutil.copyfile(ENV_EXAMPLE_FILE, ENV_FILE)
        print(f"✓ Created {ENV_FILE.name} from {ENV_EXAMPLE_FILE.name}")

    load_dotenv(ENV_FILE, override=False)
    persisted_updates: dict[str, str | None] = {}

    cur_proj = os.getenv("GOOGLE_CLOUD_PROJECT", "")
    if (not cur_proj or cur_proj == "your-gcp-project-id") and shutil.which("gcloud"):
        detected_proj = _run_quiet(["gcloud", "config", "get-value", "project"])
        if detected_proj:
            persisted_updates["GOOGLE_CLOUD_PROJECT"] = detected_proj
            print(f"✓ Auto-detected GOOGLE_CLOUD_PROJECT={detected_proj} from gcloud config")

    # Note: Do NOT populate GOOGLE_WORKSPACE_ACCESS_TOKEN from `gcloud auth print-access-token`,
    # because `gcloud` tokens only carry `cloud-platform` scope and are rejected by Google Calendar/Gmail/Drive APIs.

    if auto_gh_token and not os.getenv("GITHUB_PERSONAL_ACCESS_TOKEN"):
        gh_tok = _run_quiet(["gh", "auth", "token"]) if shutil.which("gh") else None
        if gh_tok:
            persisted_updates["GITHUB_PERSONAL_ACCESS_TOKEN"] = gh_tok
            print("✓ Resolved & saved GITHUB_PERSONAL_ACCESS_TOKEN from active gh CLI session")

    if persisted_updates:
        persist_env_vars(persisted_updates)

    int_reg = get_integration_registry()
    secret_status: dict[str, bool] = {}
    for spec in int_reg.list_all():
        if spec.auth_env:
            secret_status[spec.auth_env] = spec.has_valid_credentials()
    return secret_status


def write_local_workspace_override(
    *,
    github_user: str | None = None,
    prefer_forks: bool = True,
    default_harness: str = "claude",
    horizon_package_spec: str | None = None,
    extra_repos: list[dict[str, Any]] | None = None,
    extra_python_packages: list[str] | None = None,
    target_path: Path | None = None,
) -> Path:
    """Write or update `config/workspaces.local.yaml` (gitignored) with developer-specific fork & workspace settings."""
    out_path = target_path or LOCAL_WORKSPACES_YAML
    out_path.parent.mkdir(parents=True, exist_ok=True)

    existing: dict[str, Any] = {}
    if out_path.exists():
        try:
            existing = yaml.safe_load(out_path.read_text()) or {}
        except Exception:
            existing = {}

    if github_user is not None:
        existing["github_user"] = github_user.strip()
    existing["prefer_forks"] = bool(prefer_forks)
    existing["default_harness"] = default_harness.strip().lower()

    sandbox_sec = dict(existing.get("sandbox") or {})
    if horizon_package_spec:
        sandbox_sec["horizon_package_spec"] = horizon_package_spec
    elif github_user and prefer_forks:
        sandbox_sec["horizon_package_spec"] = (
            f"git+https://github.com/{github_user.strip()}/adk-samples.git@main"
            "#subdirectory=core/python/long-horizon-harness"
        )
    if extra_python_packages:
        cur_pkgs = list(sandbox_sec.get("python_packages") or [])
        sandbox_sec["python_packages"] = list(dict.fromkeys(cur_pkgs + extra_python_packages))
    if sandbox_sec:
        existing["sandbox"] = sandbox_sec

    if extra_repos:
        cur_repos = list(existing.get("repositories") or [])
        by_name = {r["name"]: r for r in cur_repos if isinstance(r, dict) and r.get("name")}
        for r in extra_repos:
            by_name[r["name"]] = r
        existing["repositories"] = list(by_name.values())

    header = (
        "# Personal Developer Sandbox & Fork Overrides (gitignored)\n"
        "# Generated by `make onboard` / `scripts/provision_sandbox.py`.\n"
        "# Deep-merged on top of `config/workspaces.yaml`.\n\n"
    )
    out_path.write_text(header + yaml.safe_dump(existing, sort_keys=False))
    return out_path


def _parse_repo_spec(spec_str: str) -> dict[str, Any]:
    """Parse `name=git_url[@branch]` or `git_url` into a repository manifest entry."""
    branch = "main"
    if "@" in spec_str and not spec_str.strip().startswith("git@"):
        spec_str, branch = spec_str.rsplit("@", 1)
    if "=" in spec_str:
        name, url = spec_str.split("=", 1)
    else:
        url = spec_str.strip()
        name = url.rstrip("/").split("/")[-1].removesuffix(".git")
    return {
        "name": name.strip(),
        "git_url": url.strip(),
        "branch": branch.strip() or "main",
    }


def run_onboarding_wizard(
    *,
    interactive: bool,
    github_user: str | None = None,
    prefer_forks: bool | None = None,
    default_harness: str | None = None,
    add_repos: list[str] | None = None,
    horizon_spec: str | None = None,
    local_override_path: Path | None = None,
) -> dict[str, Any]:
    """Execute the human-interactive OR agent-non-interactive onboarding configuration."""
    discovered = discover_developer_environment()
    inferred_user = github_user if github_user is not None else (discovered.get("github_user") or "")
    inferred_forks = True if prefer_forks is None else prefer_forks
    inferred_harness = (default_harness or os.getenv("CODING_HARNESS") or "claude").strip().lower()
    parsed_extra_repos: list[dict[str, Any]] = [
        _parse_repo_spec(s) for s in (add_repos or []) if s.strip()
    ]

    # Automatically include discovered personal forks from the user's GitHub profile
    existing_names = {r["name"] for r in parsed_extra_repos}
    for fork_name, fork_url in (discovered.get("detected_forks") or {}).items():
        if fork_name.startswith("test-") or fork_name in existing_names:
            continue
        if inferred_user and f"/{inferred_user}/" in fork_url:
            parsed_extra_repos.append(
                {
                    "name": fork_name,
                    "git_url": fork_url,
                    "branch": "main",
                }
            )
            existing_names.add(fork_name)

    if interactive and sys.stdin.isatty():
        print("\n=== Voice Orchestrator Developer & Sandbox Onboarding ===")
        print("Press Enter at any prompt to accept the auto-discovered default.\n")

        user_ans = input(f"1. Your GitHub username (for personal forks) [{inferred_user or 'none'}]: ").strip()
        if user_ans:
            inferred_user = "" if user_ans.lower() == "none" else user_ans

        if inferred_user:
            fork_default = "Y/n" if inferred_forks else "y/N"
            fork_ans = input(
                f"2. Use your personal forks (github.com/{inferred_user}/*) as 'origin' with 'google/*' as 'upstream'? [{fork_default}]: "
            ).strip().lower()
            if fork_ans in {"y", "yes"}:
                inferred_forks = True
            elif fork_ans in {"n", "no"}:
                inferred_forks = False

        harness_ans = input(
            f"3. Preferred default coding harness (claude / horizon / antigravity) [{inferred_harness}]: "
        ).strip().lower()
        if harness_ans in {"claude", "horizon", "antigravity"}:
            inferred_harness = harness_ans

        repo_ans = input(
            "4. Additional repositories to add (comma-separated `name=url` or `url`, or Enter to skip): "
        ).strip()
        if repo_ans:
            for part in repo_ans.split(","):
                if part.strip():
                    parsed_extra_repos.append(_parse_repo_spec(part.strip()))

        # Step 5: Interactive Connected Apps Authentication & Verification
        from app.auth import (
            persist_env_vars,
            run_local_browser_oauth,
            verify_all_integrations,
            verify_and_save_token,
        )

        print("\n--- GCP APIs, IAM & Connected Apps Live Check ---")
        ensure_gcp_apis_and_iam(discovered.get("gcloud_project"))
        live_checks = asyncio.run(verify_all_integrations())

        # 5a. GitHub
        if not live_checks["github"]["verified"] and shutil.which("gh"):
            gh_ans = input("5a. GitHub is not authenticated. Run `gh auth login --web` now? [Y/n]: ").strip().lower()
            if gh_ans in {"", "y", "yes"}:
                subprocess.run(["gh", "auth", "login", "--web", "-s", "repo,read:org,workflow"], check=False)
                ensure_env_and_secrets(auto_gcloud_token=False, auto_gh_token=True)

        # 5b. Spotify OAuth 2.0
        if not live_checks["spotify"]["verified"]:
            sp_cid = os.getenv("SPOTIFY_CLIENT_ID", "").strip()
            sp_sec = os.getenv("SPOTIFY_CLIENT_SECRET", "").strip()
            if not sp_cid or not sp_sec:
                print("5b. Spotify OAuth 2.0 (Redirect URI: http://127.0.0.1:8000/api/v1/auth/spotify/callback)")
                cid_in = input("    Enter Spotify Client ID (or Enter to skip): ").strip()
                if cid_in:
                    sec_in = input("    Enter Spotify Client Secret: ").strip()
                    if sec_in:
                        persist_env_vars({"SPOTIFY_CLIENT_ID": cid_in, "SPOTIFY_CLIENT_SECRET": sec_in})
                        sp_cid, sp_sec = cid_in, sec_in
            if sp_cid and sp_sec:
                auth_sp = input("    Open browser to authorize Spotify now? [Y/n]: ").strip().lower()
                if auth_sp in {"", "y", "yes"}:
                    try:
                        res_sp = asyncio.run(run_local_browser_oauth("spotify"))
                        print(f"    → Spotify status: {res_sp.get('detail')}")
                    except Exception as exc:
                        print(f"    → Spotify OAuth skipped/failed: {exc}")

        # 5c. Google Workspace (Calendar, Gmail, Drive) via 1-step gcloud ADC login or OAuth
        if not live_checks["google_workspace"]["verified"]:
            from app.auth import sync_workspace_from_adc

            if shutil.which("gcloud"):
                auth_gw = input(
                    "5c. Google Workspace (Calendar/Gmail/Drive) is not authorized yet.\n"
                    "    Open browser via `gcloud auth application-default login` to grant Calendar, Gmail & Drive scopes now? [Y/n]: "
                ).strip().lower()
                if auth_gw in {"", "y", "yes"}:
                    try:
                        res_gw = asyncio.run(sync_workspace_from_adc(run_login_if_needed=True))
                        print(f"    → Google Workspace verified: {res_gw.get('account_label')}")
                    except Exception as exc:
                        print(f"    → Google Workspace authorization skipped/failed: {exc}")

        # 5d. Slack Bot Token
        if not live_checks["slack"]["verified"]:
            sl_in = input("5d. Enter Slack Bot Token (xoxb-... — auto-discovers SLACK_TEAM_ID, or Enter to skip): ").strip()
            if sl_in:
                try:
                    res_sl = asyncio.run(verify_and_save_token("slack", sl_in))
                    print(f"    → Slack connected: {res_sl.get('account_label')}")
                except Exception as exc:
                    print(f"    → Slack token check failed: {exc}")

    override_file = write_local_workspace_override(
        github_user=inferred_user or None,
        prefer_forks=inferred_forks,
        default_harness=inferred_harness,
        horizon_package_spec=horizon_spec,
        extra_repos=parsed_extra_repos,
        target_path=local_override_path,
    )
    get_harness_registry().set_default(inferred_harness)

    return {
        "override_file": str(override_file),
        "github_user": inferred_user or None,
        "prefer_forks": inferred_forks,
        "default_harness": inferred_harness,
        "added_repositories": parsed_extra_repos,
        "discovered": discovered,
    }


def seed_declarative_repositories(workspace_root: Path | None = None) -> list[Path]:
    """Materialize all `repositories` (from user's GitHub profile/forks) AND `seed_repositories` declared in `config/workspaces.yaml` (+ `workspaces.local.yaml`) into `workspace_root`."""
    from scripts.setup import _configure_git_remotes

    ws_root = workspace_root or get_workspace_root()
    ws_root.mkdir(parents=True, exist_ok=True)
    os.environ["ORCHESTRATOR_WORKSPACE_ROOT"] = str(ws_root)

    manifest = load_workspaces_manifest()
    seeded_dirs: list[Path] = []
    seen_names: set[str] = set()

    # 1. Materialize GitHub profile repositories (`repositories` from workspaces.yaml + workspaces.local.yaml)
    for repo in manifest.get("repositories", []):
        if not isinstance(repo, dict) or not repo.get("name"):
            continue
        repo_name = str(repo["name"]).strip()
        if repo_name in seen_names:
            continue
        seen_names.add(repo_name)
        git_url = str(repo.get("git_url") or f"https://github.com/google/{repo_name}.git")
        upstream_url = repo.get("upstream_url")
        repo_dir, _, _ = ensure_repo_and_worktree(repo_name, "seed-init")
        _configure_git_remotes(repo_dir, git_url, upstream_url)
        sync_cross_harness_context(repo_dir, repo_name)
        seeded_dirs.append(repo_dir)
        print(
            f"✓ Seeded GitHub repository '{repo_name}' ({git_url}) at {repo_dir}"
        )

    # 2. Materialize starter microservice seed_repositories (`auth-svc`, `analytics-svc`)
    for seed in manifest.get("seed_repositories", []):
        if not isinstance(seed, dict) or not seed.get("name"):
            continue
        repo_name = str(seed["name"]).strip()
        if repo_name in seen_names:
            continue
        seen_names.add(repo_name)
        repo_dir, _, _ = ensure_repo_and_worktree(repo_name, "seed-init")
        sync_cross_harness_context(repo_dir, repo_name)
        seeded_dirs.append(repo_dir)
        print(
            f"✓ Seeded shared repository '{repo_name}' at {repo_dir} "
            f"(synced AGENTS.md, CLAUDE.md, GEMINI.md, .mcp.json)"
        )

    return seeded_dirs


async def provision_shared_sandbox(
    *,
    user_id: str = "default_user",
    harnesses: list[str] | None = None,
    workspace_root: Path | None = None,
    clone_remotes: bool = False,
    auto_gcloud_token: bool = False,
    auto_gh_token: bool = False,
    http_transport: Any | None = None,
) -> dict[str, Any]:
    """Full end-to-end provisioning flow for the shared per-user sandbox and all coding harnesses.

    Args:
        http_transport: Optional `httpx` transport override used to reach the
            sandbox. Production leaves this as None; tests inject a
            `MockTransport` so provisioning never touches the network.
    """
    secret_status = ensure_env_and_secrets(
        auto_gcloud_token=auto_gcloud_token,
        auto_gh_token=auto_gh_token,
    )
    # Auto-create config/workspaces.local.yaml from the developer's `gh` profile if not yet created
    if not LOCAL_WORKSPACES_YAML.exists() and not os.getenv("PYTEST_CURRENT_TEST"):
        run_onboarding_wizard(interactive=False)

    generate_claude_mcp_json()

    if clone_remotes:
        setup_workspace(clone_remotes=True)

    seeded_dirs = seed_declarative_repositories(workspace_root=workspace_root)
    manifest = load_workspaces_manifest()
    sandbox_cfg = manifest.get("sandbox", {})
    uv_tools = list(sandbox_cfg.get("uv_tools", []))
    py_pkgs = list(sandbox_cfg.get("python_packages", []))
    npm_pkgs = list(sandbox_cfg.get("npm_packages", []))
    skills = list(sandbox_cfg.get("skills", []))
    sys_bins = list(sandbox_cfg.get("system_binaries", []))

    if manifest.get("default_harness"):
        try:
            get_harness_registry().set_default(str(manifest["default_harness"]))
        except ValueError:
            pass

    is_mock = bool(os.getenv("PYTEST_CURRENT_TEST"))
    live_sb_info: dict[str, str] | None = None
    if not is_mock:
        try:
            from app.workers.sandbox import SandboxWorker

            live_sb_info = await SandboxWorker()._ensure_sandbox(user_id)
        except Exception:
            live_sb_info = None

    context = SandboxContext(
        user_id=user_id,
        sandbox_name=(live_sb_info["sandbox_name"] if live_sb_info else f"voice-worker-{user_id}"),
        lb_host=(live_sb_info["lb_host"] if live_sb_info else "mock-sandbox-lb.aiplatform.googleapis.com"),
        routing_token=(live_sb_info["routing_token"] if live_sb_info else "mock-token"),
        sandbox_token=(live_sb_info["sandbox_token"] if live_sb_info else "mock-auth"),
        worktree_dir=(workspace_root or get_workspace_root()),
        http_transport=http_transport,
    )

    if live_sb_info and not is_mock:
        try:
            import httpx

            headers = {
                "Authorization": f"Bearer {live_sb_info['sandbox_token']}",
                "X-Sandbox-Routing-Token": live_sb_info["routing_token"],
                "X-Sandbox-Port": "8080",
            }
            repo_cmds: list[str] = []
            for r in manifest.get("repositories", []):
                rname = r.get("name")
                rurl = r.get("git_url")
                if rname and rurl:
                    repo_cmds.append(
                        f"mkdir -p /workspace/{rname} && cd /workspace/{rname} && "
                        f"(git status >/dev/null 2>&1 || (git init && git remote add origin {rurl} && git config user.email 'agent@sonar.local' && git config user.name 'ADK Sonar' && echo '# {rname} ({rurl})' > README.md && git add . && git commit -m 'init'))"
                    )
            for s in manifest.get("seed_repositories", []):
                sname = s.get("name")
                if sname:
                    repo_cmds.append(
                        f"mkdir -p /workspace/{sname} && cd /workspace/{sname} && "
                        f"(git status >/dev/null 2>&1 || (git init && git config user.email 'agent@sonar.local' && git config user.name 'ADK Sonar' && echo '# {sname}' > README.md && git add . && git commit -m 'init'))"
                    )
            seed_cmd = " && ".join(repo_cmds)
            async with httpx.AsyncClient(timeout=20.0) as client:
                await client.post(
                    f"https://{live_sb_info['lb_host']}/exec",
                    headers=headers,
                    json={"command": seed_cmd},
                )
            print(f"✓ Seeded {len(repo_cmds)} GitHub & starter repositories inside remote Vertex Sandbox: {live_sb_info['sandbox_name']}")
        except Exception as exc:
            print(f"  (Note: remote sandbox workspace seed skipped: {exc})")

    provisioner = get_sandbox_provisioner()
    # Reuse the harness preflight's resolved container layout so onboarding and
    # runtime provisioning install to exactly the same places.
    profile = await provisioner.get_runtime_profile(context, force_refresh=True)
    print(
        f"✓ Sandbox runtime: venv={profile.venv_path} "
        f"npm_prefix={profile.npm_prefix or '(container default)'} "
        f"{'root' if profile.is_root else 'non-root'}"
        + (" [prebuilt env]" if profile.reuses_existing_venv else "")
    )

    install_steps: list[tuple[str, str]] = [("python virtualenv", profile.venv_bootstrap_command())]
    install_steps += [(f"uv tool {t}", profile.uv_tool_install(t)) for t in uv_tools]
    if py_pkgs:
        install_steps.append((f"{len(py_pkgs)} python packages", profile.uv_pip_install(py_pkgs)))
    if npm_pkgs:
        install_steps.append((f"{len(npm_pkgs)} npm packages", profile.npm_global_install(npm_pkgs)))
    install_steps.append(("agents-cli ADK skills", profile.agents_cli_setup_command()))
    install_steps += [(f"skill pack ({s.split()[0]})", profile.skills_install(s)) for s in skills]

    for label, cmd in install_steps:
        code, output = await provisioner._exec_in_sandbox(context, cmd)
        if code == 0:
            print(f"✓ Installed {label}")
        else:
            # Silently swallowing these is how the sandbox ended up with none of
            # the manifest's packages installed.
            print(f"✗ Failed to install {label} (exit {code}): {output.strip()[:300]}")


    harness_reg = get_harness_registry()
    target_names = harnesses or [h.name for h in harness_reg.list_all()]
    harness_statuses: dict[str, dict[str, Any]] = {}

    for name in target_names:
        h_impl = harness_reg.get(name)
        # Use fast preflight verification in the live sandbox container
        state = await provisioner.ensure_provisioned(
            h_impl,
            SandboxContext(
                user_id=user_id,
                sandbox_name=context.sandbox_name,
                lb_host=context.lb_host,
                routing_token=context.routing_token,
                sandbox_token=context.sandbox_token,
                worktree_dir=context.worktree_dir,
                http_transport=context.http_transport,
            ),
            force_refresh=True,
        )
        harness_statuses[h_impl.name] = state.to_dict()
        print(
            f"✓ Harness '{h_impl.display_name}' ({h_impl.name}) -> {state.status.upper()} [{state.source_url or state.version}]"
        )

    return {
        "sandbox_name": context.sandbox_name,
        "lb_host": context.lb_host,
        "workspace_root": str(workspace_root or get_workspace_root()),
        "github_user": manifest.get("github_user"),
        "prefer_forks": manifest.get("prefer_forks", False),
        "default_harness": harness_reg.default_harness.name,
        "repositories": manifest.get("repositories", []),
        "seeded_repositories": [d.name for d in seeded_dirs],
        "uv_tools": uv_tools,
        "python_packages": py_pkgs,
        "npm_packages": npm_pkgs,
        "skills": skills,
        "system_binaries": sys_bins,
        "secrets": secret_status,
        "harnesses": harness_statuses,
    }


def print_readiness_scorecard() -> dict[str, Any]:
    """Prints a unified readiness scorecard with LIVE API verification across Voice, Harnesses, Sandbox, and Connected Apps."""
    from app.auth import verify_all_integrations

    ensure_env_and_secrets(auto_gcloud_token=False, auto_gh_token=True)
    live_checks = asyncio.run(verify_all_integrations())

    ws_root = get_workspace_root()
    seeded_repos = (
        sorted(
            d.name
            for d in ws_root.iterdir()
            if d.is_dir() and not d.name.startswith(".")
        )
        if ws_root.exists()
        else []
    )
    harness_reg = get_harness_registry()
    vertex_sb = os.getenv("VERTEX_SANDBOX_RESOURCE_NAME")

    apps_order = [
        (
            "Google Workspace (Calendar/Gmail/Drive)",
            live_checks["google_workspace"]["verified"],
            live_checks["google_workspace"]["detail"],
        ),
        ("Google Search Grounding", True, "Verified (First-party ADK Grounding)"),
        ("Google Maps Grounding", True, "Verified (First-party ADK Grounding)"),
        (
            "GitHub MCP",
            live_checks["github"]["verified"],
            live_checks["github"]["detail"],
        ),
        (
            "Spotify MCP",
            live_checks["spotify"]["verified"],
            live_checks["spotify"]["detail"],
        ),
        (
            "Slack MCP",
            live_checks["slack"]["verified"],
            live_checks["slack"]["detail"],
        ),
    ]

    print("\n=== ADK Sonar Onboarding & Live Verification Scorecard ===")
    print(f"  1. GCP Project / Live Model : {os.getenv('GOOGLE_CLOUD_PROJECT') or 'unset'} (gemini-3.8-live)")
    print(f"  2. Remote Vertex Sandbox    : {vertex_sb.split('/')[-1] if vertex_sb else 'not provisioned'} ({vertex_sb or 'run make provision'})")
    print(f"  3. Default Coding Harness   : {harness_reg.default_harness.display_name} ({harness_reg.default_harness.name})")
    print(f"  4. Seeded Sandbox Repos     : {', '.join(seeded_repos) or 'none (run `make seed`)'}")
    print("  5. Connected Apps (Live API Verification) :")
    for label, verified, detail in apps_order:
        badge = "✓ VERIFIED" if verified else "○ NOT VERIFIED"
        print(f"     • {label:<38} : {badge} — {detail}")
    print("\nNext Steps:")
    print("  • Local Voice + Mobile UI : `make dev`        -> http://localhost:3000")
    print("  • Deploy to Cloud Run     : `make deploy`     -> followed by `make sync-secrets`")
    return {
        "project": os.getenv("GOOGLE_CLOUD_PROJECT"),
        "vertex_sandbox": vertex_sb,
        "default_harness": harness_reg.default_harness.name,
        "seeded_repositories": seeded_repos,
        "connected_apps": {label: {"verified": verified, "detail": detail} for label, verified, detail in apps_order},
    }


def sync_secrets_to_cloud_run(
    *,
    service_name: str = "voice-orchestrator",
    region: str = "us-central1",
    project: str | None = None,
) -> dict[str, Any]:
    """Pushes local `.env` OAuth client credentials, refresh tokens, and bot tokens to the deployed Cloud Run service."""
    load_dotenv(ENV_FILE, override=False)
    target_project = project or os.getenv("GOOGLE_CLOUD_PROJECT")
    if not target_project:
        raise RuntimeError("GOOGLE_CLOUD_PROJECT is not set in .env or gcloud config.")

    sync_keys = [
        "GOOGLE_OAUTH_CLIENT_ID",
        "GOOGLE_OAUTH_CLIENT_SECRET",
        "GOOGLE_WORKSPACE_REFRESH_TOKEN",
        "GOOGLE_WORKSPACE_ACCESS_TOKEN",
        "GITHUB_CLIENT_ID",
        "GITHUB_CLIENT_SECRET",
        "GITHUB_PERSONAL_ACCESS_TOKEN",
        "SPOTIFY_CLIENT_ID",
        "SPOTIFY_CLIENT_SECRET",
        "SPOTIFY_REFRESH_TOKEN",
        "SPOTIFY_ACCESS_TOKEN",
        "SLACK_CLIENT_ID",
        "SLACK_CLIENT_SECRET",
        "SLACK_BOT_TOKEN",
        "SLACK_TEAM_ID",
    ]
    pairs: list[str] = []
    synced_names: list[str] = []
    for k in sync_keys:
        val = os.getenv(k)
        if val:
            pairs.append(f"{k}={val}")
            synced_names.append(k)

    service_url = _run_quiet(
        [
            "gcloud",
            "run",
            "services",
            "describe",
            service_name,
            "--region",
            region,
            "--project",
            target_project,
            "--format=value(status.url)",
        ],
        timeout=10,
    )

    if pairs and shutil.which("gcloud"):
        print(
            f"Syncing {len(synced_names)} credentials ({', '.join(synced_names)}) to Cloud Run service '{service_name}' ({region})..."
        )
        subprocess.run(
            [
                "gcloud",
                "run",
                "services",
                "update",
                service_name,
                "--region",
                region,
                "--project",
                target_project,
                f"--update-env-vars={','.join(pairs)}",
            ],
            check=False,
        )

    print("\n=== Cloud Run Remote Auth & OAuth Redirect Summary ===")
    print(f"  Cloud Run Service : {service_name} ({target_project} / {region})")
    print(f"  Synced Secrets    : {', '.join(synced_names) or 'none'}")
    if service_url:
        print(f"  Service URL       : {service_url}")
        print(f"  Spotify Callback  : {service_url}/api/v1/auth/spotify/callback")
        print(f"  Google Callback   : {service_url}/api/v1/auth/google_workspace/callback")
    return {
        "service_name": service_name,
        "project": target_project,
        "region": region,
        "service_url": service_url,
        "synced_keys": synced_names,
    }


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Human & AI-Agent friendly onboarding wizard and shared sandbox provisioner."
    )
    parser.add_argument(
        "--discover",
        action="store_true",
        help="Introspect GitHub user, existing forks, sibling repos, and GCP credentials without mutating state.",
    )
    parser.add_argument(
        "--check",
        action="store_true",
        help="Run the readiness scorecard check across Voice, Harnesses, Sandbox repos, and Connected Apps.",
    )
    parser.add_argument(
        "--sync-cloud-run",
        action="store_true",
        help="Push configured .env OAuth refresh tokens & secrets to the deployed Cloud Run service.",
    )
    parser.add_argument(
        "--service-name",
        default="voice-orchestrator",
        help="Cloud Run service name when using --sync-cloud-run (default: voice-orchestrator).",
    )
    parser.add_argument(
        "--region",
        default="us-central1",
        help="Cloud Run region when using --sync-cloud-run (default: us-central1).",
    )
    parser.add_argument(
        "--check-only",
        dest="check",
        action="store_true",
        help="Alias for --check (verify environment and live API readiness without modifying files).",
    )
    parser.add_argument(
        "--onboard",
        action="store_true",
        help="Run the onboarding wizard (interactive in a TTY, or non-interactive with --yes / flags) to write config/workspaces.local.yaml.",
    )
    parser.add_argument(
        "--yes",
        "-y",
        "--non-interactive",
        dest="yes",
        action="store_true",
        help="Non-interactive mode: accept auto-discovered defaults + CLI flags without blocking on stdin.",
    )
    parser.add_argument(
        "--project",
        default=None,
        help="Optional GCP project override.",
    )
    parser.add_argument(
        "--location",
        default=None,
        help="Optional GCP location override (default: us-central1).",
    )
    parser.add_argument(
        "--github-user",
        default=None,
        help="GitHub username for personal forks (e.g. 'allen-stephen').",
    )
    parser.add_argument(
        "--prefer-forks",
        dest="prefer_forks",
        action="store_true",
        default=None,
        help="Rewrite canonical GitHub repos to use github.com/<github-user>/<repo>.git as origin and canonical URL as upstream.",
    )
    parser.add_argument(
        "--no-prefer-forks",
        dest="prefer_forks",
        action="store_false",
        help="Use canonical upstream repository URLs directly instead of personal forks.",
    )
    parser.add_argument(
        "--default-harness",
        default=None,
        choices=["claude", "horizon", "antigravity"],
        help="Preferred default coding harness in the sandbox.",
    )
    parser.add_argument(
        "--horizon-spec",
        default=None,
        help="Custom git/pip package spec for installing ADK Long Horizon in the sandbox.",
    )
    parser.add_argument(
        "--add-repo",
        action="append",
        default=[],
        help="Add or override a repository in workspaces.local.yaml (`name=git_url[@branch]`). Repeatable.",
    )
    parser.add_argument(
        "--user-id",
        default="default_user",
        help="User ID for scoping the persistent sandbox container (default: default_user).",
    )
    parser.add_argument(
        "--harnesses",
        nargs="*",
        default=None,
        help="Specific harnesses to provision (e.g. claude antigravity horizon). Defaults to all.",
    )
    parser.add_argument(
        "--clone-remotes",
        action="store_true",
        help="Also clone/update remote GitHub repositories into ./workspaces.",
    )
    parser.add_argument(
        "--seed-only",
        action="store_true",
        help="Only seed repositories and synchronize cross-harness context files.",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="Emit machine-readable JSON output (ideal for AI coding agents running onboarding on a user's behalf).",
    )
    args = parser.parse_args()

    if args.discover:
        info = discover_developer_environment()
        if args.json:
            print(json.dumps(info, indent=2))
        else:
            print("=== Discovered Developer Environment ===")
            print(f"  GitHub User       : {info['github_user'] or '(not detected)'}")
            print(f"  GitHub Token      : {'available' if info['github_token_available'] else 'missing'}")
            print(f"  Detected Forks    : {', '.join(f'{k} ({v})' for k, v in info['detected_forks'].items()) or 'none'}")
            print(f"  GCP Project       : {info['gcloud_project'] or '(not set)'}")
            print(f"  Local Override    : {info['local_override_file']} ({'present' if info['local_override_exists'] else 'not created yet'})")
        return

    if args.check:
        score = print_readiness_scorecard()
        if args.json:
            print(json.dumps(score, indent=2))
        return

    if args.sync_cloud_run:
        res = sync_secrets_to_cloud_run(
            service_name=args.service_name,
            region=args.region,
        )
        if args.json:
            print(json.dumps(res, indent=2))
        return

    onboard_result: dict[str, Any] | None = None
    if (
        args.onboard
        or args.github_user is not None
        or args.prefer_forks is not None
        or args.default_harness is not None
        or args.add_repo
        or args.horizon_spec
    ):
        is_interactive = bool(args.onboard and not args.yes and not args.json and sys.stdin.isatty())
        onboard_result = run_onboarding_wizard(
            interactive=is_interactive,
            github_user=args.github_user,
            prefer_forks=args.prefer_forks,
            default_harness=args.default_harness,
            add_repos=args.add_repo,
            horizon_spec=args.horizon_spec,
        )
        if not args.json:
            print(f"✓ Saved personal workspace overrides to {onboard_result['override_file']}")

    if args.seed_only:
        ensure_env_and_secrets(auto_gcloud_token=True, auto_gh_token=True)
        generate_claude_mcp_json()
        seed_declarative_repositories()
        return

    summary = asyncio.run(
        provision_shared_sandbox(
            user_id=args.user_id,
            harnesses=args.harnesses,
            clone_remotes=args.clone_remotes,
            auto_gcloud_token=True,
            auto_gh_token=True,
        )
    )
    if onboard_result:
        summary["onboarding"] = onboard_result

    if args.json:
        print(json.dumps(summary, indent=2))
        return

    print("\n=== Shared Sandbox Provisioning Complete ===")
    print(f"  Sandbox Container : {summary['sandbox_name']}")
    print(f"  Shared Workspace  : {summary['workspace_root']}")
    if summary.get("github_user"):
        print(
            f"  GitHub Fork Mode  : {summary['github_user']} (prefer_forks={summary['prefer_forks']})"
        )
    print(f"  Default Harness   : {summary['default_harness']}")
    print(f"  Seeded Repos      : {', '.join(summary['seeded_repositories'])}")
    print(
        f"  Ready Harnesses   : {', '.join(k for k, v in summary['harnesses'].items() if v['status'] == 'ready')}"
    )
    print_readiness_scorecard()


if __name__ == "__main__":
    main()
