#!/usr/bin/env python3
"""agent_ops_audit_pack.py - Compact audit artifact generator for agent-ops monitoring.

Generates a task-kind-specific compact artifact (AGENT_OPS_AUDIT_PACK_V1) that
provides agent-ops reviewers with structured metadata without leaking raw
log/body content into main context.

Usage:
    uv run python3 scripts/agent_ops_audit_pack.py \
      --task-kind issue-refinement-ops-review \
      --issue-number 1014 \
      --repo squne121/loop-protocol \
      [--log-file /tmp/sample.log] \
      --artifact-out /tmp/audit_pack.json

stdout: EVIDENCE: <artifact_path>  (always <= 2048 UTF-8 bytes, no raw body)

exit codes: 0=ok, 1=error
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
from pathlib import Path
from typing import Optional

REPO_ROOT = Path(__file__).resolve().parent.parent

STDOUT_BUDGET_BYTES = 2048

# AC4: Fields redacted from artifact (raw content categories)
REDACTED_FIELDS = ["log_file_content", "raw_issue_body", "raw_comments"]

# AC4: Secret-like path patterns to redact from any string values
SECRET_PATH_PATTERN = re.compile(
    r"(/etc/passwd|\.env|credential|secret|password|token|api_key|apikey|private_key)",
    re.IGNORECASE,
)

# Tool availability checks: map of tool_id -> script path relative to repo root
TOOL_AVAILABILITY_SCRIPTS = {
    "contract_readiness_check": ".claude/skills/issue-contract-review/scripts/contract_readiness_check.py",
    "baseline_vc_preflight": ".claude/skills/issue-contract-review/scripts/baseline_vc_preflight.py",
    "vc_contract_syntax": ".claude/skills/issue-refinement-loop/scripts/run_refinement_preflight.py",
    "check_codex_agents": "scripts/check_codex_agent_config.py",
}


def _is_secret_like(value: str) -> bool:
    """Return True if value contains secret-like path or keyword patterns."""
    return bool(SECRET_PATH_PATTERN.search(value))


def _get_repo_root() -> Path:
    """Resolve repo root via git rev-parse, fall back to script-relative path."""
    try:
        result = subprocess.run(
            ["git", "rev-parse", "--show-toplevel"],
            capture_output=True,
            text=True,
            timeout=10,
        )
        if result.returncode == 0 and result.stdout.strip():
            return Path(result.stdout.strip())
    except (subprocess.TimeoutExpired, FileNotFoundError):
        pass
    return REPO_ROOT


def _check_cwd_valid() -> bool:
    """AC3: Verify current working directory exists."""
    try:
        return os.path.exists(os.getcwd())
    except OSError:
        return False


def _get_worktree_state() -> str:
    """AC3: Return 'clean' if git status is empty, else 'dirty'."""
    try:
        result = subprocess.run(
            ["git", "status", "--porcelain"],
            capture_output=True,
            text=True,
            timeout=15,
        )
        if result.returncode == 0:
            return "clean" if not result.stdout.strip() else "dirty"
    except (subprocess.TimeoutExpired, FileNotFoundError):
        pass
    return "unknown"


def _get_hook_scripts(repo_root: Path) -> list[dict]:
    """AC3: Parse .claude/settings.json hooks section.

    Returns list of {event, handler_id, path, exists, executable}.
    Raw command bodies are NOT included (AC4).
    """
    settings_path = repo_root / ".claude" / "settings.json"
    hooks: list[dict] = []

    try:
        with open(settings_path, encoding="utf-8") as f:
            settings = json.load(f)
    except (OSError, json.JSONDecodeError):
        return hooks

    hooks_section = settings.get("hooks", {})
    for event, event_hooks in hooks_section.items():
        if not isinstance(event_hooks, list):
            continue
        for idx, hook_entry in enumerate(event_hooks):
            # hook_entry may be a direct command string or a dict with "command"
            if isinstance(hook_entry, str):
                raw_command = hook_entry
                handler_id = f"{event}_{idx}"
            elif isinstance(hook_entry, dict):
                raw_command = hook_entry.get("command", "")
                handler_id = hook_entry.get("handler_id", f"{event}_{idx}")
            else:
                continue

            # Resolve ${CLAUDE_PROJECT_DIR} to repo root
            resolved_command = raw_command.replace("${CLAUDE_PROJECT_DIR}", str(repo_root))

            # Extract first token as potential script path
            tokens = resolved_command.strip().split()
            if not tokens:
                continue

            # Find the script path token (first .py / .sh / .mjs / path-like token)
            script_path: Optional[str] = None
            for token in tokens:
                # Skip interpreter names (python3, uv, node, bash, sh)
                if token in ("python3", "uv", "node", "bash", "sh", "python"):
                    continue
                # Skip flags
                if token.startswith("-"):
                    continue
                # If it looks like a path (contains / or starts with .)
                if "/" in token or token.startswith("."):
                    # Ensure not secret-like
                    if not _is_secret_like(token):
                        script_path = token
                    break
                # If it's a recognized sub-command skip it (e.g. "run" in "uv run")
                if token in ("run",):
                    continue

            if script_path is None:
                continue

            script_abs = Path(script_path)
            if not script_abs.is_absolute():
                script_abs = repo_root / script_path

            exists = script_abs.exists()
            executable = exists and os.access(script_abs, os.X_OK)

            hooks.append({
                "event": event,
                "handler_id": handler_id,
                "path": str(script_abs),
                "exists": exists,
                "executable": executable,
            })

    return hooks


def _get_codex_hook_surface(repo_root: Path) -> dict:
    """AC5: Report .codex/hooks.json events and config.toml inline hook status."""
    hooks_json_path = repo_root / ".codex" / "hooks.json"
    config_toml_path = repo_root / ".codex" / "config.toml"

    hooks_json_exists = hooks_json_path.exists()
    hooks_json_events: list[str] = []

    if hooks_json_exists:
        try:
            with open(hooks_json_path, encoding="utf-8") as f:
                data = json.load(f)
            hooks_json_events = list(data.get("hooks", {}).keys())
        except (OSError, json.JSONDecodeError):
            pass

    # AC5: Determine if config.toml has inline hooks (should not per project convention)
    config_toml_has_inline_hooks = False
    if config_toml_path.exists():
        try:
            with open(config_toml_path, encoding="utf-8") as f:
                toml_content = f.read()
            # Inline hooks in config.toml would appear as [hooks.*] sections
            config_toml_has_inline_hooks = bool(re.search(r"^\[hooks\.", toml_content, re.MULTILINE))
        except OSError:
            pass

    return {
        "hooks_json_exists": hooks_json_exists,
        "hooks_json_events": hooks_json_events,
        "config_toml_has_inline_hooks": config_toml_has_inline_hooks,
    }


def _get_related_issues(repo: str, issue_number: int) -> list[dict]:
    """AC3: Fetch issue metadata via gh api. Returns error dict on failure."""
    try:
        result = subprocess.run(
            [
                "gh", "api",
                f"repos/{repo}/issues/{issue_number}",
                "--jq",
                "{number:.number,title:.title,state:.state,updated_at:.updated_at,url:.html_url}",
            ],
            capture_output=True,
            text=True,
            timeout=30,
        )
        if result.returncode == 0 and result.stdout.strip():
            issue_data = json.loads(result.stdout.strip())
            # AC4: Do not include raw body or comments
            return [issue_data]
        else:
            return [{"error": "api_unavailable"}]
    except (subprocess.TimeoutExpired, FileNotFoundError, json.JSONDecodeError):
        return [{"error": "api_unavailable"}]


def _get_tool_availability(repo_root: Path) -> dict:
    """Check availability of key agent-ops tools by verifying script existence."""
    availability: dict[str, bool] = {}
    for tool_id, rel_path in TOOL_AVAILABILITY_SCRIPTS.items():
        availability[tool_id] = (repo_root / rel_path).exists()
    return availability


def _get_coverage_gaps(repo_root: Path, task_kind: str) -> list[dict]:
    """AC6: Run agent_ops_inventory.py as subprocess to get coverage gaps."""
    inventory_script = repo_root / "scripts" / "agent_ops_inventory.py"
    if not inventory_script.exists():
        return [{"error": "agent_ops_inventory.py not found"}]

    import tempfile
    with tempfile.NamedTemporaryFile(suffix=".json", delete=False) as tmp:
        tmp_path = tmp.name

    try:
        # Use a task kind that has an inventory profile
        inv_task_kind = task_kind if task_kind in (
            "agent-ops-review", "issue-refinement-ops-review"
        ) else "issue-refinement-ops-review"

        result = subprocess.run(
            [
                "uv", "run", "python3",
                str(inventory_script),
                "--task-kind", inv_task_kind,
                "--artifact-out", tmp_path,
            ],
            capture_output=True,
            text=True,
            timeout=60,
            cwd=str(repo_root),
        )

        if result.returncode not in (0, 2):  # 0=ok, 2=warn are both usable
            return [{"error": f"inventory exit {result.returncode}"}]

        with open(tmp_path, encoding="utf-8") as f:
            inventory = json.load(f)

        # Extract coverage gaps: coverage entries where coverage_ok is False
        coverage = inventory.get("coverage", [])
        gaps = [
            {
                "prefix": entry["prefix"],
                "target_type": entry.get("target_type", "dir"),
                "tracked_matches": entry.get("tracked_matches", 0),
                "included_matches": entry.get("included_matches", 0),
                "coverage_ok": entry.get("coverage_ok", False),
            }
            for entry in coverage
            if not entry.get("coverage_ok", True)
        ]

        # Also report missing_critical / missing_warn from inventory status
        missing_critical = inventory.get("missing_critical", [])
        missing_warn = inventory.get("missing_warn", [])
        if missing_critical:
            gaps.append({"kind": "missing_critical", "paths": missing_critical})
        if missing_warn:
            gaps.append({"kind": "missing_warn", "paths": missing_warn})

        return gaps

    except (subprocess.TimeoutExpired, FileNotFoundError, json.JSONDecodeError, OSError) as e:
        return [{"error": str(e)}]
    finally:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass


def build_audit_pack(
    task_kind: str,
    issue_number: int,
    repo: str,
    repo_root: Path,
    log_file: Optional[Path] = None,
) -> dict:
    """Build the AGENT_OPS_AUDIT_PACK_V1 artifact dict."""
    cwd_valid = _check_cwd_valid()
    worktree_state = _get_worktree_state()
    hook_scripts = _get_hook_scripts(repo_root)
    codex_hook_surface = _get_codex_hook_surface(repo_root)
    related_issues = _get_related_issues(repo, issue_number)
    tool_availability = _get_tool_availability(repo_root)
    coverage_gaps = _get_coverage_gaps(repo_root, task_kind)

    artifact: dict = {
        "schema": "AGENT_OPS_AUDIT_PACK_V1",
        "task_kind": task_kind,
        "issue_number": issue_number,
        "repo": repo,
        "repo_root": str(repo_root),
        "cwd_valid": cwd_valid,
        "worktree_state": worktree_state,
        "hook_scripts": hook_scripts,
        "codex_hook_surface": codex_hook_surface,
        "related_issues": related_issues,
        "tool_availability": tool_availability,
        "coverage_gaps": coverage_gaps,
        "redacted_fields": REDACTED_FIELDS,
    }

    # AC1: log_file is optional; if provided note its presence but not its content
    if log_file is not None:
        artifact["log_file_noted"] = str(log_file)
        artifact["log_file_exists"] = log_file.exists()
        # AC4: Do not include log file content in artifact

    return artifact


def write_artifact(artifact_path: Path, data: dict) -> None:
    """Write artifact JSON to disk."""
    artifact_path.parent.mkdir(parents=True, exist_ok=True)
    with open(artifact_path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
        f.write("\n")


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument(
        "--task-kind",
        required=True,
        help="Task kind for the audit (e.g. issue-refinement-ops-review)",
    )
    p.add_argument(
        "--issue-number",
        required=True,
        type=int,
        help="Issue number to include in audit pack",
    )
    p.add_argument(
        "--repo",
        required=True,
        help="GitHub repo in owner/name format (e.g. squne121/loop-protocol)",
    )
    p.add_argument(
        "--log-file",
        type=Path,
        default=None,
        help="Optional log file path (existence noted, content redacted)",
    )
    p.add_argument(
        "--artifact-out",
        required=True,
        type=Path,
        help="Output path for audit pack JSON artifact",
    )
    p.add_argument(
        "--repo-root",
        type=Path,
        default=None,
        help="Repository root override (default: auto-detected via git)",
    )
    return p


def main(argv: Optional[list[str]] = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    repo_root = args.repo_root if args.repo_root is not None else _get_repo_root()

    artifact = build_audit_pack(
        task_kind=args.task_kind,
        issue_number=args.issue_number,
        repo=args.repo,
        repo_root=repo_root,
        log_file=args.log_file,
    )

    write_artifact(args.artifact_out, artifact)

    # AC2: stdout is EVIDENCE: <path> only, <= 2048 bytes, no raw body
    stdout_line = f"EVIDENCE: {args.artifact_out}\n"
    if len(stdout_line.encode("utf-8")) > STDOUT_BUDGET_BYTES:
        stdout_line = f"EVIDENCE: {args.artifact_out}\n"  # path is always short enough

    sys.stdout.write(stdout_line)
    return 0


if __name__ == "__main__":
    sys.exit(main())
