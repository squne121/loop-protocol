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
import tempfile
from pathlib import Path
from typing import NamedTuple, Optional


REPO_ROOT = Path(__file__).resolve().parent.parent
EXPECTATION_PATH = REPO_ROOT / "tests/fixtures/codex-agent-config/expected-runtime-contract.json"

# stdout budget for compact plan / EVIDENCE lines (AC1).
STDOUT_BUDGET_BYTES = 2048

# AC4: machine-readable read policy. DO_NOT_READ_INITIAL_ONLY is an initial-context
# exclusion only; additional reads are NOT absolutely forbidden.
READ_POLICY_INITIAL_EXCLUSION = "initial_exclusion_not_absolute_forbid"

VALID_TASK_KINDS = [
    "issue-refinement",
    "issue-refinement-ops-review",
    "pr-review",
    "workflow-implementation",
    "product-implementation",
    "agent-ops-review",
]

# ──────────────────────────────────────────────────────────────────────────────
# PlanSpec registry (task-kind -> MUST_READ / DO_NOT_READ_INITIAL_ONLY)
# ──────────────────────────────────────────────────────────────────────────────

class CoverageTarget(NamedTuple):
    """A coverage target the inventory must account for.

    target_type:
      - "dir":  matched by path prefix (e.g. ".codex/agents/")
      - "file": matched by exact path equality (e.g. a single contract fixture)
    empty_ok: whether it is acceptable for this target to have zero tracked matches.
    """
    path: str
    target_type: str  # "dir" | "file"
    empty_ok: bool = True


class InventoryProfile(NamedTuple):
    """Inventory profile supplied by the registry/spec (AC7).

    The builder reads target_prefixes / coverage_targets / expected_paths /
    critical_surface_source from here instead of hard-coding them, so both
    agent-ops-review and issue-refinement-ops-review draw their inventory
    profile from the same registry source of truth.
    """
    target_prefixes: tuple[str, ...]
    coverage_targets: tuple[CoverageTarget, ...]
    expected_paths: tuple[str, ...]
    critical_surface_source: str = "none"  # "none" | "codex_runtime_contract"


class PlanSpec(NamedTuple):
    task_kind: str
    must_read: list[str]
    do_not_read_initial_only: list[str]
    # do_not_read_initial_only = initial exclusion; additional reads are NOT forbidden.
    read_policy: str = READ_POLICY_INITIAL_EXCLUSION
    inventory_profile: Optional[InventoryProfile] = None


# Shared inventory profile for the ops-review family (AC7): both agent-ops-review
# and issue-refinement-ops-review reference this same object, so target prefixes,
# coverage targets, expected paths and critical-surface source are single-sourced.
OPS_REVIEW_INVENTORY_PROFILE = InventoryProfile(
    target_prefixes=(
        ".claude/agents/",
        ".agents/skills/",
        ".claude/skills/",
        ".claude/hooks/",
        ".claude/rules/",
        ".codex/",
        "tests/fixtures/codex-agent-config/",
    ),
    coverage_targets=(
        CoverageTarget(".claude/agents/", "dir", empty_ok=False),
        CoverageTarget(".claude/rules/", "dir", empty_ok=False),
        CoverageTarget(".claude/hooks/", "dir", empty_ok=False),
        CoverageTarget(".claude/skills/", "dir", empty_ok=False),
        CoverageTarget(".agents/skills/", "dir", empty_ok=False),
        CoverageTarget(".codex/agents/", "dir", empty_ok=False),
        CoverageTarget(
            "tests/fixtures/codex-agent-config/expected-runtime-contract.json",
            "file",
            empty_ok=False,
        ),
    ),
    expected_paths=(
        "tests/fixtures/codex-agent-config/expected-runtime-contract.json",
        ".claude/settings.json",
    ),
    critical_surface_source="codex_runtime_contract",
)


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
    "issue-refinement-ops-review": PlanSpec(
        task_kind="issue-refinement-ops-review",
        must_read=[
            "AGENTS.md",
            "CLAUDE.md",
            ".claude/skills/issue-refinement-loop/SKILL.md",
            ".claude/agents/issue-reviewer.md",
            ".claude/agents/issue-author.md",
            ".claude/skills/issue-contract-review/scripts/contract_readiness_check.py",
            ".claude/skills/issue-contract-review/scripts/baseline_vc_preflight.py",
            ".claude/skills/issue-refinement-loop/scripts/run_refinement_preflight.py",
            ".claude/skills/issue-refinement-loop/scripts/plan_refinement_loop.py",
            ".claude/skills/issue-refinement-loop/scripts/decide_next_loop_action.py",
            ".claude/skills/issue-refinement-loop/scripts/compact_review_result.py",
            ".claude/skills/issue-refinement-loop/scripts/compact_author_result.py",
        ],
        do_not_read_initial_only=[
            "docs/product/",
            "src/",
        ],
        inventory_profile=OPS_REVIEW_INVENTORY_PROFILE,
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
        inventory_profile=OPS_REVIEW_INVENTORY_PROFILE,
    ),
}


# ──────────────────────────────────────────────────────────────────────────────
# Tracked file helpers (git ls-files)
# ──────────────────────────────────────────────────────────────────────────────

def get_tracked_files(repo_root: Path) -> list[bytes]:
    """Return repo-relative paths of all tracked files via git ls-files -z (raw bytes)."""
    result = subprocess.run(
        ["git", "ls-files", "-z"],
        cwd=str(repo_root),
        capture_output=True,
    )
    if result.returncode != 0:
        return []
    return [p for p in result.stdout.split(b"\0") if p]


def get_tracked_paths_decoded(repo_root: Path) -> list[str]:
    """Return decoded repo-relative tracked file paths.

    Uses surrogateescape to avoid silent drops on non-UTF-8 filenames.
    Paths that cannot be decoded cleanly are included as surrogate strings
    (they will fail containment checks and be excluded from inventory items,
    but they are NOT silently dropped).
    """
    raw = get_tracked_files(repo_root)
    paths: list[str] = []
    for item in raw:
        if isinstance(item, bytes):
            try:
                # Prefer strict UTF-8 first
                paths.append(item.decode("utf-8"))
            except UnicodeDecodeError:
                # Fall back to surrogateescape so nothing is silently dropped.
                paths.append(item.decode("utf-8", errors="surrogateescape"))
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


def _is_symlink_escape(repo_root: Path, rel_path: str) -> bool:
    """Return True if the path is a symlink that resolves outside repo_root.

    Checks using lstat() to detect symlinks before resolve().
    """
    abs_path = repo_root / rel_path
    try:
        if abs_path.is_symlink():
            resolved = abs_path.resolve()
            repo_resolved = repo_root.resolve()
            try:
                resolved.relative_to(repo_resolved)
                return False  # stays inside repo
            except ValueError:
                return True  # escapes repo boundary
    except OSError:
        pass
    return False


def is_containment_safe(repo_root: Path, path_str: str) -> bool:
    """Return True if path is safely under repo_root.

    Checks:
    - Not an absolute path
    - No .. components
    - If it is a symlink, it must not escape repo_root
    - resolve() must stay under repo_root
    """
    if path_str.startswith("/"):
        return False
    if ".." in path_str.split("/"):
        return False
    # Check for symlink escape before resolving
    if _is_symlink_escape(repo_root, path_str):
        return False
    try:
        resolved = (repo_root / path_str).resolve()
        repo_resolved = repo_root.resolve()
        resolved.relative_to(repo_resolved)
        return True
    except (ValueError, OSError):
        return False


# ──────────────────────────────────────────────────────────────────────────────
# Agent-ops-review inventory
# ──────────────────────────────────────────────────────────────────────────────

def load_contract_surfaces_with_errors(repo_root: Path) -> tuple[list[str], list[str]]:
    """
    Load .agents/skills/** surfaces from the expected-runtime-contract.json,
    returning (surfaces, contract_errors).

    Robust against a missing/corrupt fixture (MAJOR 2): a JSON decode error or
    a schema-shape problem is reported as a structured contract_error string
    instead of raising a traceback, so the CLI can emit a machine-readable
    blocked inventory and keep stdout compact.
    """
    contract_path = repo_root / EXPECTATION_PATH.relative_to(REPO_ROOT)
    if not contract_path.exists():
        # Absent fixture is handled gracefully (no critical surfaces, no error),
        # preserving prior behaviour. Only a PRESENT-but-corrupt fixture is an error.
        return [], []
    try:
        raw = contract_path.read_text(encoding="utf-8")
    except OSError as exc:
        return [], [f"contract_fixture_unreadable: {type(exc).__name__}"]
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        return [], [f"contract_fixture_invalid_json: line {exc.lineno} col {exc.colno}"]
    if not isinstance(data, dict) or "required_agents" not in data:
        return [], ["contract_fixture_schema_missing_required_agents"]
    surfaces: list[str] = []
    required = data.get("required_agents", {})
    if not isinstance(required, dict):
        return [], ["contract_fixture_required_agents_not_object"]
    for agent_data in required.values():
        if not isinstance(agent_data, dict):
            continue
        for surface in agent_data.get("repo_local_skill_surfaces", []):
            if surface not in surfaces:
                surfaces.append(surface)
    return surfaces, []


def load_critical_surfaces_from_contract(repo_root: Path) -> list[str]:
    """
    Load .agents/skills/** surfaces from the expected-runtime-contract.json.
    These are derived from the route surface contract — no hand-written list.

    Thin, exception-safe wrapper over load_contract_surfaces_with_errors that
    returns only the surfaces (errors are surfaced separately by the builder).
    """
    surfaces, _errors = load_contract_surfaces_with_errors(repo_root)
    return surfaces


def classify_path_kind(path: str) -> str:
    """Classify a tracked path into a metadata kind.

    Order matters: agent definitions under .codex/agents/ and .claude/agents/
    are classified before the broader .codex/ config bucket so that custom
    agent definitions are not mis-labelled as generic config (MAJOR 1).
    """
    if path.startswith(".agents/skills/"):
        return "agent_skill_surface"
    if path.startswith(".claude/skills/"):
        return "canonical_skill_body"
    if path.startswith(".claude/hooks/"):
        return "claude_hook"
    if path == ".claude/settings.json":
        return "claude_settings"
    if path.startswith(".codex/agents/") and path.endswith(".toml"):
        return "codex_agent_definition"
    if path.startswith(".claude/agents/"):
        return "claude_agent_definition"
    if path.startswith(".codex/"):
        return "codex_config"
    if path.startswith("tests/fixtures/codex-agent-config/"):
        return "codex_agent_fixture"
    if path.endswith(".toml"):
        return "agent_definition"
    if path.startswith("scripts/"):
        return "script"
    return "other"


def _coverage_target_matches(target: CoverageTarget, path: str) -> bool:
    """File targets match by exact equality; directory targets by prefix.

    This prevents a file target like
    tests/fixtures/codex-agent-config/expected-runtime-contract.json from being
    falsely satisfied by a sibling such as ...expected-runtime-contract.json.bak
    (BLOCKER 5).
    """
    if target.target_type == "file":
        return path == target.path
    return path.startswith(target.path)


def build_agent_ops_inventory(
    repo_root: Path,
    tracked_paths: list[str],
    task_kind: str = "agent-ops-review",
    spec: Optional[PlanSpec] = None,
) -> dict:
    """
    Build inventory artifact for the ops-review task-kind family.
    Only tracked files, metadata only (no file contents).
    Item fields: path, exists, kind, tracked.

    The inventory profile (target prefixes, coverage targets, expected paths,
    critical-surface source) is drawn from PlanSpec.inventory_profile in the
    registry (AC7) — the builder does not hard-code task-kind-specific lists.

    Status logic (OR semantics):
    - blocked: any critical surface is missing from disk OR not in tracked_set
               OR is a symlink escaping the repo, OR the contract fixture is
               unreadable/invalid (contract_errors non-empty).
    - warn:    any expected path is missing from disk OR not in tracked_set,
               OR any coverage target is not satisfied (coverage_ok == False).
    - ok:      all present, tracked, no symlink escape, coverage satisfied.
    """
    if spec is None:
        spec = PLAN_REGISTRY.get(task_kind)
    profile = (spec.inventory_profile if spec is not None else None) or OPS_REVIEW_INVENTORY_PROFILE

    target_prefixes = list(profile.target_prefixes)

    # Critical surfaces: sourced per profile. Robust against a corrupt fixture.
    if profile.critical_surface_source == "codex_runtime_contract":
        critical_surfaces, contract_errors = load_contract_surfaces_with_errors(repo_root)
    else:
        critical_surfaces, contract_errors = [], []

    tracked_set = set(tracked_paths)

    # Expected paths that are not critical but should warn if missing
    expected_paths: list[str] = list(profile.expected_paths)

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

    # Add expected paths
    for ep in expected_paths:
        add_item(ep)

    # Compute STATUS — Fix 1: use OR semantics + symlink escape = blocked
    critical_set = set(critical_surfaces)
    missing_critical: list[str] = []
    for path in critical_set:
        abs_path = repo_root / path
        # A symlink that escapes the repo is treated as a security violation (blocked)
        if _is_symlink_escape(repo_root, path):
            missing_critical.append(path)
        elif not abs_path.exists() or path not in tracked_set:
            missing_critical.append(path)

    missing_expected: list[str] = []
    for path in expected_paths:
        abs_path = repo_root / path
        if not abs_path.exists() or path not in tracked_set:
            missing_expected.append(path)

    # Determine warn items: tracked items that are missing from disk (non-critical)
    missing_warn_items = [
        it for it in items
        if not it["exists"] and it["tracked"] and it["path"] not in critical_set
    ]

    # AC6 / BLOCKER 5: per-target coverage that reflects what actually landed in
    # the artifact. included_matches counts tracked matches that survived the
    # add_item() filters (containment-safe, not secret-like). filtered_matches is
    # the difference, so a "tracked but dropped by security filter" situation is
    # observable, and coverage_ok / empty_ok make "absent" vs "present-but-missing"
    # machine-decidable. File targets use exact match (no .bak false positives).
    included_paths = {it["path"] for it in items}
    coverage: list[dict] = []
    coverage_all_ok = True
    for target in profile.coverage_targets:
        matched = [tp for tp in tracked_paths if _coverage_target_matches(target, tp)]
        tracked_matches = len(matched)
        included_matches = sum(1 for m in matched if m in included_paths)
        filtered_matches = tracked_matches - included_matches
        if tracked_matches == 0:
            coverage_ok = bool(target.empty_ok)
        else:
            coverage_ok = filtered_matches == 0
        if not coverage_ok:
            coverage_all_ok = False
        coverage.append({
            "prefix": target.path,
            "target_type": target.target_type,
            "tracked_matches": tracked_matches,
            "included_matches": included_matches,
            "filtered_matches": filtered_matches,
            "empty_ok": bool(target.empty_ok),
            "coverage_ok": coverage_ok,
        })

    # coverage_ok is per-target machine-decidable info; it does not by itself
    # change the overall status (which stays governed by critical surfaces,
    # expected paths, and a corrupt contract fixture). coverage_all_ok is kept
    # available for callers/summaries.
    _ = coverage_all_ok
    if missing_critical or contract_errors:
        status = "blocked"
    elif missing_expected or missing_warn_items:
        status = "warn"
    else:
        status = "ok"

    # Build missing_warn list from items (for backward compat)
    missing_critical_path_set = set(missing_critical)
    missing_warn: list[str] = []
    seen_warn: set[str] = set()
    for it in items:
        p = it["path"]
        if not it["exists"] and it["tracked"] and p not in missing_critical_path_set:
            if p not in seen_warn:
                seen_warn.add(p)
                missing_warn.append(p)
    for ep in missing_expected:
        if ep not in missing_critical_path_set and ep not in seen_warn:
            seen_warn.add(ep)
            missing_warn.append(ep)

    return {
        "schema_version": "agent_ops_inventory_v1",
        "task_kind": task_kind,
        "status": status,
        "critical_surfaces": critical_surfaces,
        "items": items,
        "missing_critical": list(missing_critical),
        "missing_warn": missing_warn,
        "coverage": coverage,
        "contract_errors": contract_errors,
    }


# ──────────────────────────────────────────────────────────────────────────────
# Plan output for non-inventory task-kinds
# ──────────────────────────────────────────────────────────────────────────────

def build_plan_output(spec: PlanSpec, repo_root: Path) -> dict:
    """Build machine-readable plan output for a given PlanSpec.

    Emits an explicit machine-readable ``read_policy`` field (AC4) in addition
    to the human-readable note, so consumers can assert the policy without
    string-matching prose.
    """
    return {
        "schema_version": "agent_ops_read_plan_v1",
        "task_kind": spec.task_kind,
        "MUST_READ": list(spec.must_read),
        "DO_NOT_READ_INITIAL_ONLY": list(spec.do_not_read_initial_only),
        "read_policy": spec.read_policy,
        "note": "DO_NOT_READ_INITIAL_ONLY is initial exclusion only; additional reads are NOT forbidden.",
    }


def emit_json_under_budget(payload: dict, max_bytes: int = STDOUT_BUDGET_BYTES) -> str:
    """Serialize ``payload`` to compact JSON, degrading to a smaller JSON object
    if it would exceed the stdout budget.

    AC1 / BLOCKER 1: when ``--json`` is requested the output is ALWAYS valid
    JSON. On budget overflow we never fall back to a non-JSON ``KEY: value``
    string; instead we return a compact JSON object that records the overflow
    and points the consumer at the artifact path / inventory requirement.
    """
    output = json.dumps(payload, ensure_ascii=False, separators=(",", ":")) + "\n"
    if len(output.encode("utf-8")) <= max_bytes:
        return output

    fallback = {
        "schema_version": payload.get("schema_version", "agent_ops_read_plan_v1"),
        "task_kind": payload.get("task_kind"),
        "status": "blocked",
        "error": "stdout_budget_exceeded",
        "read_policy": payload.get("read_policy"),
        "inventory_required": True,
    }
    if "inventory_artifact" in payload:
        fallback["inventory_artifact"] = payload["inventory_artifact"]
    return json.dumps(fallback, ensure_ascii=False, separators=(",", ":")) + "\n"


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


def _path_is_under(child: Path, parent: Path) -> bool:
    try:
        child.relative_to(parent)
        return True
    except ValueError:
        return False


def validate_artifact_destination(artifact_path: Path, repo_root: Path) -> None:
    """Validate a user-supplied --artifact-out destination (BLOCKER 6).

    O_NOFOLLOW only guards the trailing component; a symlink anywhere in the
    parent chain would still be followed by open()/mkdir(). So we additionally:

    - reject if ANY existing component of the path is a symlink, and
    - require the nearest existing ancestor (realpath) to live under the repo
      root or the system temp dir.

    Raises ValueError on violation. Callers treat --artifact-out outside these
    trusted roots as out of the security guarantee.
    """
    cur = artifact_path if artifact_path.is_absolute() else (Path.cwd() / artifact_path)

    # Reject symlinks among existing path components.
    probe = cur
    while True:
        if probe.is_symlink():
            raise ValueError(f"artifact destination path component is a symlink: {probe}")
        if probe.parent == probe:
            break
        probe = probe.parent

    # Nearest existing ancestor must resolve under a trusted root.
    nearest = cur
    while not nearest.exists():
        if nearest.parent == nearest:
            raise ValueError(f"artifact destination has no existing ancestor: {cur}")
        nearest = nearest.parent

    nearest_real = nearest.resolve()
    allowed_roots = [repo_root.resolve(), Path(tempfile.gettempdir()).resolve()]
    if not any(_path_is_under(nearest_real, root) for root in allowed_roots):
        raise ValueError(
            f"artifact destination must be under repo root or temp dir: {cur}"
        )


def write_artifact(artifact_path: Path, data: dict) -> None:
    """Write artifact JSON with 0600 permissions using O_CREAT|O_EXCL|O_NOFOLLOW.

    Security properties:
    - O_CREAT | O_EXCL: fails if path already exists (no clobbering).
    - O_NOFOLLOW: rejects symlink at the final path component (no TOCTOU via symlinks).
    - Mode 0o600: only owner can read/write.

    The parent-chain trust boundary is enforced separately by
    validate_artifact_destination(); callers MUST call it first for
    user-supplied --artifact-out paths.

    Windows: O_NOFOLLOW is not available on Windows, and write_bytes() can
    overwrite an existing file, so secure-artifact semantics are best-effort on
    Windows only (documented limitation; O_EXCL/O_NOFOLLOW tests are platform-skipped).
    """
    import platform
    artifact_path.parent.mkdir(parents=True, exist_ok=True)
    content = json.dumps(data, indent=2, ensure_ascii=False).encode("utf-8")

    if platform.system() == "Windows":
        # Best-effort on Windows: no O_NOFOLLOW; reject pre-existing path explicitly.
        if artifact_path.exists():
            raise FileExistsError(f"artifact path already exists: {artifact_path}")
        artifact_path.write_bytes(content)
        try:
            os.chmod(artifact_path, 0o600)
        except NotImplementedError:
            pass
    else:
        flags = os.O_CREAT | os.O_EXCL | os.O_WRONLY
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        fd = os.open(str(artifact_path), flags, 0o600)
        try:
            os.write(fd, content)
        finally:
            os.close(fd)


def _status_exit(status: str) -> int:
    return {"blocked": 1, "warn": 2}.get(status, 0)


def _write_evidence(artifact_path: Path) -> None:
    line = f"EVIDENCE: {artifact_path}\n"
    if len(line.encode("utf-8")) > STDOUT_BUDGET_BYTES:
        line = "EVIDENCE: artifact_written\n"
    sys.stdout.write(line)


def _emit_error_json_or_stderr(output_json: bool, task_kind: str, message: str) -> int:
    if output_json:
        sys.stdout.write(emit_json_under_budget({
            "schema_version": "agent_ops_read_plan_v1",
            "task_kind": task_kind,
            "status": "blocked",
            "error": message,
        }))
    else:
        sys.stderr.write(message + "\n")
    return 3


def _inventory_summary(inventory: dict) -> dict:
    """Compact, stdout-safe summary of the inventory artifact (no raw file list)."""
    return {
        "status": inventory["status"],
        "item_count": len(inventory["items"]),
        "contract_errors": inventory.get("contract_errors", []),
        "coverage": [
            {
                "prefix": c["prefix"],
                "coverage_ok": c["coverage_ok"],
                "tracked_matches": c["tracked_matches"],
                "filtered_matches": c["filtered_matches"],
            }
            for c in inventory.get("coverage", [])
        ],
    }


def _compact_text_plan(plan: dict) -> str:
    lines = [
        f"TASK_KIND: {plan['task_kind']}",
        f"MUST_READ: {', '.join(plan['MUST_READ'])}",
        f"DO_NOT_READ_INITIAL_ONLY: {', '.join(plan['DO_NOT_READ_INITIAL_ONLY'])}",
        f"READ_POLICY: {plan['read_policy']}",
        plan["note"],
    ]
    output = "\n".join(lines) + "\n"
    if len(output.encode("utf-8")) > STDOUT_BUDGET_BYTES:
        output = (
            f"TASK_KIND: {plan['task_kind']}\n"
            f"READ_POLICY: {plan['read_policy']}\n"
            "EVIDENCE: use --json flag for full plan\n"
        )
    return output


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()
    repo_root: Path = args.repo_root.resolve()
    task_kind: str = args.task_kind
    spec = PLAN_REGISTRY[task_kind]

    # agent-ops-review: legacy artifact (EVIDENCE) mode, preserved for backward
    # compatibility. Internally spec-driven via spec.inventory_profile.
    if task_kind == "agent-ops-review":
        if args.artifact_out:
            artifact_path = args.artifact_out
        else:
            tmpdir = tempfile.mkdtemp(prefix="agent-ops-")
            artifact_path = Path(tmpdir) / "agent_ops_inventory.json"
        tracked_paths = get_tracked_paths_decoded(repo_root)
        inventory = build_agent_ops_inventory(repo_root, tracked_paths, task_kind=task_kind, spec=spec)
        try:
            if args.artifact_out:
                validate_artifact_destination(artifact_path, repo_root)
            write_artifact(artifact_path, inventory)
        except (ValueError, OSError) as exc:
            return _emit_error_json_or_stderr(args.output_json, task_kind, f"artifact_write_failed: {exc}")
        _write_evidence(artifact_path)
        return _status_exit(inventory["status"])

    # issue-refinement-ops-review: read-plan first; optional inventory artifact.
    # --json ALWAYS yields JSON (AC1); --json + --artifact-out yields JSON plan
    # augmented with inventory_artifact + inventory_summary (BLOCKER 4).
    if task_kind == "issue-refinement-ops-review":
        plan = build_plan_output(spec, repo_root)
        inventory = None
        if args.artifact_out:
            tracked_paths = get_tracked_paths_decoded(repo_root)
            inventory = build_agent_ops_inventory(repo_root, tracked_paths, task_kind=task_kind, spec=spec)
            try:
                validate_artifact_destination(args.artifact_out, repo_root)
                write_artifact(args.artifact_out, inventory)
            except (ValueError, OSError) as exc:
                return _emit_error_json_or_stderr(args.output_json, task_kind, f"artifact_write_failed: {exc}")
            plan = {
                **plan,
                "inventory_artifact": str(args.artifact_out),
                "inventory_summary": _inventory_summary(inventory),
            }

        if args.output_json:
            sys.stdout.write(emit_json_under_budget(plan))
        elif args.artifact_out:
            _write_evidence(args.artifact_out)
        else:
            sys.stdout.write(_compact_text_plan(plan))

        if inventory is not None:
            return _status_exit(inventory["status"])
        return 0

    # Other read-plan task-kinds (issue-refinement, pr-review, …).
    plan = build_plan_output(spec, repo_root)
    if args.output_json:
        sys.stdout.write(emit_json_under_budget(plan))
    else:
        sys.stdout.write(_compact_text_plan(plan))
    return 0


if __name__ == "__main__":
    sys.exit(main())
