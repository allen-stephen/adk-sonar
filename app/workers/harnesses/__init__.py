"""Pluggable coding harnesses sub-package."""

from app.workers.harnesses.antigravity import AntigravityHarness
from app.workers.harnesses.base import (
    CodingHarness,
    HarnessEvent,
    SandboxContext,
    collect_worktree_changes,
    ensure_repo_and_worktree,
    parse_stream_json_line,
    release_worktree,
)
from app.workers.harnesses.claude import ClaudeCodeHarness
from app.workers.harnesses.horizon import HorizonA2AHarness
from app.workers.harnesses.prompts import (
    PLAN_OUTPUT_SCHEMA,
    PLAN_OUTPUT_SCHEMA_JSON,
    build_harness_system_prompt,
    build_harness_user_prompt,
    extract_plan_from_json_output,
)
from app.workers.harnesses.provisioner import (
    HORIZON_GITHUB_SPEC,
    HarnessNotProvisionedError,
    HarnessProvisionStatus,
    SandboxProvisioner,
    get_default_provision_recipe,
    get_sandbox_provisioner,
    reset_sandbox_provisioner,
)
from app.workers.harnesses.registry import (
    HarnessRegistry,
    get_coding_harness,
    get_harness_registry,
    reset_harness_registry,
)

__all__ = [
    "HORIZON_GITHUB_SPEC",
    "PLAN_OUTPUT_SCHEMA",
    "PLAN_OUTPUT_SCHEMA_JSON",
    "AntigravityHarness",
    "ClaudeCodeHarness",
    "CodingHarness",
    "HarnessEvent",
    "HarnessNotProvisionedError",
    "HarnessProvisionStatus",
    "HarnessRegistry",
    "HorizonA2AHarness",
    "SandboxContext",
    "SandboxExecResult",
    "SandboxProvisioner",
    "build_harness_system_prompt",
    "build_harness_user_prompt",
    "collect_worktree_changes",
    "ensure_repo_and_worktree",
    "exec_in_sandbox",
    "extract_plan_from_json_output",
    "get_coding_harness",
    "get_default_provision_recipe",
    "get_harness_registry",
    "get_sandbox_provisioner",
    "parse_stream_json_line",
    "release_worktree",
    "reset_harness_registry",
    "reset_sandbox_provisioner",
]
