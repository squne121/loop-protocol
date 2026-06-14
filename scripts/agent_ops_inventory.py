#!/usr/bin/env python3
"""agent_ops_inventory.py - Agent ops read plan generator.

Generates a task-kind-specific read plan (MUST_READ / DO_NOT_READ_INITIAL_ONLY)
and, for agent-ops-review, an inventory artifact JSON of tracked metadata.

Usage:
    uv run python3 scripts/agent_ops_inventory.py --task-kind issue-refinement
    uv run python3 scripts/agent_ops_inventory.py --task-kind agent-ops-review --artifact-out /tmp/inventory.json

stdout: EVIDENCE: <artifact_path>  (for agent-ops-review)
        or compact plan summary (for other task-kinds)
        Always <= 2048 UTF-8 bytes.

exit codes: 0=ok, 1=blocked (critical surface missing), 2=warn (expected file missing), 3=error
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
from pathlib import Path
from typing import NamedTuple


REPO_ROOT = Path(__file__).resolve().parent.parent
EXPECTATION_PATH = REPO_ROOT / "tests/fixtures/codex-agent-config/expected-runtime-contract.json"

VALID_TASK_KINDS = [
    "issue-refinement",
    "pr-review",
    "workflow-implementation",
    "product-implementation",
    "agent-ops-review",
]

# ──────────────────────────────────────────────────────────────────────────────
# PlanSpec registry (task-kind -> MUST_READ / DO_NOT_READ_INITIAL_ONLY)
# ──────────────────────────────────────────────────────────────────────────────

class PlanSpec(NamedTuple):
    task_kind: str
    must_read: list[str]
    do_not_read_initial_only: list[str]
    # do_not_read_initial_only = initial exclusion; additional reads are NOT forbidden.


PLAN_REGISTRY: dict[str, PlanSpec] = {
    "issue-refinement": PlanSpec(
        task_kind="issue-refinement",
        must_read=[
            ".claude/skills/issue-refinement-loop/SKILL.md",
            ".claude/skills/issue-refinement-loop/scripts/run_refinement_preflight.py",
            ".claude/skills/issue-refinement-loop/scripts/plan_refinement_loop.py",
            ".claude/skills/issue-refinement-loop/scripts/decide_next_loop_action.py",
            ".claude/agents/issue-author.md",
            ".claude/agents/issue-reviewer.md",
            ".claude/agents/review-issue.md",
        ],
        do_not_read_initial_only=[
            "docs/product/",
            "src/",
        ],
    ),
    "pr-review": PlanSpec(
        task_kind="pr-review",
        must_read=[
            ".claude/skills/implement-issue/SKILL.md",
            ".claude/agents/pr-reviewer.md",
            ".claude/agents/pr-reviewer-lite.md",
        ],
        do_not_read_initial_only=[
            "src/",
            "docs/product/",
        ],
    ),
    "workflow-implementation": PlanSpec(
        task_kind="workflow-implementation",
        must_read=[
            "CLAUDE.md",
            ".claude/rules/project-constitution.md",
            ".claude/skills/implement-issue/SKILL.md",
        ],
        do_not_read_initial_only=[
            "docs/product/",
        ],
    ),
    "product-implementation": PlanSpec(
        task_kind="product-implementation",
        must_read=[
            "CLAUDE.md",
            ".claude/rules/project-constitution.md",
            "docs/product/requirements.md",
        ],
        do_not_read_initial_only=[
            ".claude/skills/",
            ".agents/skills/",
        ],
    ),
    "agent-ops-review": PlanSpec(
        task_kind="agent-ops-review",
        must_read=[
            "tests/fixtures/codex-agent-config/expected-runtime-contract.json",
            "scripts/check_codex_agent_config.py",
        ],
        do_not_read_initial_only=[
            "src/",
            "docs/product/",
        ],
    ),
}


# ──────────────────────────────────────────────────────────────────────────────
# Tracked file helpers (git ls-files)
# ──────────────────────────────────────────────────────────────────────────────

def get_tracked_files(repo_root: Path) -> list[str]:
    """Return repo-relative paths of all tracked files via git ls-files -z."""
    result = subprocess.run(
        ["git", "ls-files", "-z"],
        cwd=str(repo_root),
        capture_output=True,
    )
    if result.returncode != 0:
        return []
    return [p for p in result.stdout.split(b"\0") if p]  # type: ignore[misc]


def get_tracked_paths_decoded(repo_root: Path) -> list[str]:
    """Return decoded repo-relative tracked file paths."""
    raw = get_tracked_files(repo_root)
    paths: list[str] = []
    for item in raw:
        if isinstance(item, bytes):
            try:
                paths.append(item.decode("utf-8"))
            except UnicodeDecodeError:
                continue
        else:
            paths.append(item)
    return paths


# ──────────────────────────────────────────────────────────────────────────────
# Security guards
# ──────────────────────────────────────────────────────────────────────────────

SECRET_LIKE_PATTERN = re.compile(
    r"(secret|password|token|api_key|apikey|private_key|credential)",
    re.IGNORECASE,
)


def is_secret_like(path: str) -> bool:
    return bool(SECRET_LIKE_PATTERN.search(path))


def is_containment_safe(repo_root: Path, path_str: str) -> bool:
    """Return True if path is safely under repo_root (no .. escape, no absolute path)."""
    if path_str.startswith("/"):
        return False
    if ".." in path_str.split("/"):
        return False
    try:
        resolved = (repo_root / path_str).resolve()
        repo_resolved = repo_root.resolve()
        resolved.relative_to(repo_resolved)
        return True
    except ValueError:
        return False


# ──────────────────────────────────────────────────────────────────────────────
# Agent-ops-review inventory
# ──────────────────────────────────────────────────────────────────────────────

def load_critical_surfaces_from_contract(repo_root: Path) -> list[str]:
    """
    Load .agents/skills/** surfaces from the expected-runtime-contract.json.
    These are derived from the route surface contract — no hand-written list.
    """
    contract_path = repo_root / EXPECTATION_PATH.relative_to(REPO_ROOT)
    if not contract_path.exists():
        return []
    data = json.loads(contract_path.read_text(encoding="utf-8"))
    surfaces: list[str] = []
    for agent_data in data.get("required_agents", {}).values():
        for surface in agent_data.get("repo_local_skill_surfaces", []):
            if surface not in surfaces:
                surfaces.append(surface)
    return surfaces


def classify_path_kind(path: str) -> str:
    """Classify a tracked path into a metadata kind."""
    if path.startswith(".agents/skills/"):
        return "agent_skill_surface"
    if path.startswith(".claude/skills/"):
        return "canonical_skill_body"
    if path.startswith(".codex/"):
        return "codex_config"
    if path.startswith("tests/fixtures/codex-agent-config/"):
        return "codex_agent_fixture"
    if path.startswith(".claude/agents/") or path.endswith(".toml"):
        return "agent_definition"
    if path.startswith("scripts/"):
        return "script"
    return "other"


def build_agent_ops_inventory(repo_root: Path, tracked_paths: list[str]) -> dict:
    """
    Build inventory artifact for agent-ops-review.
    Only tracked files, metadata only (no file contents).
    Fields: path, exists, kind, tracked.
    """
    # Gather the target paths
    target_prefixes = [
        ".agents/skills/",
        ".claude/skills/",
        ".codex/",
        "tests/fixtures/codex-agent-config/",
    ]
    # Also include the hooks discovered from the contract
    critical_surfaces = load_critical_surfaces_from_contract(repo_root)

    tracked_set = set(tracked_paths)

    # Collect inventory items
    items: list[dict] = []
    seen: set[str] = set()

    def add_item(rel_path: str) -> None:
        if rel_path in seen:
            return
        if not is_containment_safe(repo_root, rel_path):
            return
        if is_secret_like(rel_path):
            return
        seen.add(rel_path)
        abs_path = repo_root / rel_path
        items.append({
            "path": rel_path,
            "exists": abs_path.exists(),
            "kind": classify_path_kind(rel_path),
            "tracked": rel_path in tracked_set,
        })

    # Add all tracked paths matching target prefixes
    for p in tracked_paths:
        for prefix in target_prefixes:
            if p.startswith(prefix):
                add_item(p)
                break

    # Always add critical surfaces (from contract) even if not found above
    for surface in critical_surfaces:
        add_item(surface)

    # Add the expected-runtime-contract.json explicitly
    add_item("tests/fixtures/codex-agent-config/expected-runtime-contract.json")

    # Compute STATUS
    missing_tracked = [it for it in items if not it["exists"] and it["tracked"]]
    missing_critical = [
        it for it in missing_tracked
        if it["kind"] in ("agent_skill_surface", "codex_agent_fixture")
    ]

    if missing_critical:
        status = "blocked"
    elif missing_tracked:
        status = "warn"
    else:
        status = "ok"

    return {
        "schema_version": "agent_ops_inventory_v1",
        "task_kind": "agent-ops-review",
        "status": status,
        "critical_surfaces": critical_surfaces,
        "items": items,
        "missing_critical": [it["path"] for it in missing_critical],
        "missing_warn": [it["path"] for it in missing_tracked if it not in missing_critical],
    }


# ──────────────────────────────────────────────────────────────────────────────
# Plan output for non-inventory task-kinds
# ──────────────────────────────────────────────────────────────────────────────

def build_plan_output(spec: PlanSpec, repo_root: Path) -> dict:
    """Build machine-readable plan output for a given PlanSpec."""
    return {
        "schema_version": "agent_ops_read_plan_v1",
        "task_kind": spec.task_kind,
        "MUST_READ": spec.must_read,
        "DO_NOT_READ_INITIAL_ONLY": spec.do_not_read_initial_only,
        "note": "DO_NOT_READ_INITIAL_ONLY is initial exclusion only; additional reads are NOT forbidden.",
    }


# ──────────────────────────────────────────────────────────────────────────────
# CLI
# ──────────────────────────────────────────────────────────────────────────────

def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument(
        "--task-kind",
        required=True,
        choices=VALID_TASK_KINDS,
        help="Task kind for read plan / inventory generation",
    )
    p.add_argument(
        "--repo-root",
        type=Path,
        default=REPO_ROOT,
        help="Repository root (default: derived from script location)",
    )
    p.add_argument(
        "--artifact-out",
        type=Path,
        default=None,
        help="Output path for inventory artifact JSON (required for agent-ops-review)",
    )
    p.add_argument(
        "--json",
        action="store_true",
        dest="output_json",
        help="Output plan as JSON to stdout (for non-agent-ops-review task-kinds)",
    )
    return p


def write_artifact(artifact_path: Path, data: dict) -> None:
    """Write artifact JSON with 0600 permissions."""
    artifact_path.parent.mkdir(parents=True, exist_ok=True)
    artifact_path.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
    os.chmod(artifact_path, 0o600)


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()
    repo_root: Path = args.repo_root.resolve()

    if args.task_kind == "agent-ops-review":
        # Determine artifact output path
        if args.artifact_out:
            artifact_path = args.artifact_out
        else:
            artifact_path = Path("/tmp/agent_ops_inventory.json")

        tracked_paths = get_tracked_paths_decoded(repo_root)
        inventory = build_agent_ops_inventory(repo_root, tracked_paths)
        write_artifact(artifact_path, inventory)

        stdout_line = f"EVIDENCE: {artifact_path}\n"
        if len(stdout_line.encode("utf-8")) > 2048:
            stdout_line = "EVIDENCE: artifact_written\n"
        sys.stdout.write(stdout_line)

        if inventory["status"] == "blocked":
            return 1
        if inventory["status"] == "warn":
            return 2
        return 0

    else:
        spec = PLAN_REGISTRY[args.task_kind]
        plan = build_plan_output(spec, repo_root)

        if args.output_json:
            output = json.dumps(plan, indent=2, ensure_ascii=False) + "\n"
        else:
            lines = [
                f"TASK_KIND: {plan['task_kind']}",
                f"MUST_READ: {', '.join(plan['MUST_READ'])}",
                f"DO_NOT_READ_INITIAL_ONLY: {', '.join(plan['DO_NOT_READ_INITIAL_ONLY'])}",
                plan["note"],
            ]
            output = "\n".join(lines) + "\n"

        # Enforce 2048-byte stdout budget
        if len(output.encode("utf-8")) > 2048:
            # Truncate to fit; keep machine-readable first line
            output = f"TASK_KIND: {plan['task_kind']}\nSTATUS: ok\nEVIDENCE: use --json flag for full plan\n"

        sys.stdout.write(output)
        return 0


if __name__ == "__main__":
    sys.exit(main())
