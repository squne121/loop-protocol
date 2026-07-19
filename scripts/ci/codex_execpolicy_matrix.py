#!/usr/bin/env python3
"""Fail-closed Codex execpolicy matrix runner for CI (Issue #1150).

This script is the SSOT for the required execpolicy gate. It installs/probes the
pinned Codex CLI, evaluates the execpolicy JSON contract strictly, and
cross-checks the local two-guard cleanup route decision against the expected
lane contract.

The script writes a JSON artifact incrementally on every run (including
install/probe/case failures) so the CI job can upload version / argv / expected /
actual / raw JSON / return code / provenance evidence.
"""

from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import os
import shlex
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any

SCHEMA = "codex_execpolicy_matrix_v1"
ALLOWED_DECISIONS = {"allow", "prompt", "forbidden"}
ALLOWED_TOP_LEVEL_KEYS = {"decision", "matchedRules"}

REPO_ROOT = Path(__file__).resolve().parents[2]
LOCAL_MAIN_BRANCH_GUARD_SH = REPO_ROOT / ".codex" / "hooks" / "local_main_branch_guard.sh"
WORKTREE_SCOPE_GUARD_SH = REPO_ROOT / ".claude" / "hooks" / "worktree_scope_guard.sh"
RULES_DEFAULT = REPO_ROOT / ".codex" / "rules" / "default.rules"
WORKTREE_REL = Path(".claude/worktrees/issue-1-x")
BRANCH_NAME = "issue-1-x"
ISSUE_NUMBER = "1"


class MatrixError(RuntimeError):
    """Raised for any contract drift or verification failure."""


@dataclass(frozen=True)
class FixtureRepo:
    """Temporary git repo + linked issue worktree used for hook-chain replay."""

    root: Path
    worktree: Path
    branch: str
    issue_number: str


@dataclass
class ArtifactWriter:
    """Incremental JSON artifact writer for install/probe/case evidence."""

    path: Path
    payload: dict[str, Any]

    @classmethod
    def create(cls, path: Path, *, expected_version: str, rules: Path) -> "ArtifactWriter":
        writer = cls(
            path=path,
            payload={
                "schema": SCHEMA,
                "status": "running",
                "expected_version": expected_version,
                "rules_path": str(rules),
                "skip_markers_present": [],
                "phases": {
                    "install": {"status": "pending"},
                    "probe": {"status": "pending"},
                    "cases": {"status": "pending", "completed": 0, "failed": 0},
                },
                "cases": [],
                "errors": [],
            },
        )
        writer.flush()
        return writer

    def flush(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps(self.payload, indent=2, sort_keys=True), encoding="utf-8")

    def update_phase(self, phase: str, data: dict[str, Any]) -> None:
        self.payload["phases"][phase] = data
        self.flush()

    def set_codex(self, codex: dict[str, Any]) -> None:
        self.payload["codex"] = codex
        self.flush()

    def append_case(self, case_result: dict[str, Any]) -> None:
        self.payload["cases"].append(case_result)
        phases = self.payload["phases"]["cases"]
        phases["completed"] = len(self.payload["cases"])
        if case_result.get("status") == "failed":
            phases["failed"] += 1
        self.flush()

    def finalize_ok(self) -> None:
        self.payload["status"] = "ok"
        self.payload["phases"]["cases"]["status"] = "ok"
        self.flush()

    def finalize_failed(self, error: str) -> None:
        self.payload["status"] = "failed"
        self.payload["error"] = error
        self.payload["errors"].append(error)
        if self.payload["phases"]["cases"]["status"] == "pending":
            self.payload["phases"]["cases"]["status"] = "failed"
        self.flush()


def _load_module(module_name: str, path: Path) -> Any:
    spec = importlib.util.spec_from_file_location(module_name, path)
    if spec is None or spec.loader is None:
        raise MatrixError(f"failed to import module at {path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _git(*args: str, cwd: Path) -> None:
    env = {
        **os.environ,
        "GIT_AUTHOR_NAME": "t",
        "GIT_AUTHOR_EMAIL": "t@t",
        "GIT_COMMITTER_NAME": "t",
        "GIT_COMMITTER_EMAIL": "t@t",
    }
    subprocess.run(
        ["git", *args],
        cwd=str(cwd),
        check=True,
        capture_output=True,
        text=True,
        env=env,
    )


def build_fixture_repo(base_dir: Path, issue_number: str = ISSUE_NUMBER, slug: str = "x") -> FixtureRepo:
    """Create a temporary repo with a real linked worktree on main."""
    root = base_dir / "repo"
    root.mkdir()
    _git("init", "-q", "-b", "main", cwd=root)
    (root / "README.md").write_text("seed\n", encoding="utf-8")
    _git("add", "README.md", cwd=root)
    _git("commit", "-q", "-m", "seed", cwd=root)
    branch = f"issue-{issue_number}-{slug}"
    worktree = root / ".claude" / "worktrees" / branch
    worktree.parent.mkdir(parents=True, exist_ok=True)
    _git("worktree", "add", "-q", "-b", branch, str(worktree), "main", cwd=root)
    return FixtureRepo(root=root, worktree=worktree, branch=branch, issue_number=issue_number)


def materialize_valid_contract(fixture: FixtureRepo, operation: str) -> dict[str, Any]:
    """Write a one-shot V3 cleanup contract for the fixture repo."""
    materialize_module = _load_module(
        "materialize_cleanup_contract_under_test",
        REPO_ROOT / "scripts" / "agent-ops" / "materialize_cleanup_contract.py",
    )
    result = materialize_module.materialize(
        pr_number=999,
        linked_issue_number=int(fixture.issue_number),
        worktree_path=str(fixture.worktree),
        branch_name=fixture.branch,
        operation=operation,
        ttl_seconds=300,
        project_root=str(fixture.root),
        verify=False,
    )
    if result.get("status") != "ok":
        raise MatrixError(f"failed to materialize cleanup contract: {result}")
    return result


def render_command(argv: list[str]) -> str:
    return shlex.join(argv)


def execpolicy_case_definitions(fixture: FixtureRepo) -> list[dict[str, Any]]:
    """Static lane contract for Issue #1150.

    The contract includes the "exact cleanup + valid contract" allow corridor and
    deny cases for force / extra argv / missing target mutations.
    """
    worktree = str(fixture.worktree)
    return [
        {
            "label": "read_only_branch_list",
            "argv": ["git", "branch", "--list"],
            "expected_execpolicy": ["allow"],
            "expected_guard_pair": "allow",
            "hook_cwd": "worktree",
        },
        {
            "label": "read_only_worktree_list",
            "argv": ["git", "worktree", "list"],
            "expected_execpolicy": ["allow"],
            "expected_guard_pair": "allow",
            "hook_cwd": "worktree",
        },
        {
            "label": "exact_worktree_remove",
            "argv": ["git", "worktree", "remove", worktree],
            "expected_execpolicy": ["prompt"],
            "expected_guard_pair": "allow",
            "operation": "worktree_remove",
            "materialize_contract": True,
        },
        {
            "label": "exact_branch_delete",
            "argv": ["git", "branch", "-d", fixture.branch],
            "expected_execpolicy": ["prompt"],
            "expected_guard_pair": "allow",
            "operation": "branch_delete",
            "materialize_contract": True,
        },
        {
            "label": "malformed_worktree_remove_contract",
            "argv": ["git", "worktree", "remove", worktree],
            "expected_execpolicy": ["prompt"],
            "expected_guard_pair": "deny",
            "expected_guard_reason": "cleanup_contract_present_but_invalid",
            "operation": "worktree_remove",
            "materialize_contract": True,
            "invalidate_contract": "truncate",
        },
        {
            "label": "worktree_remove_force_before_target",
            "argv": ["git", "worktree", "remove", "--force", worktree],
            "expected_execpolicy": ["prompt", "forbidden"],
            "expected_guard_pair": "deny",
        },
        {
            "label": "worktree_remove_force_after_target",
            "argv": ["git", "worktree", "remove", worktree, "--force"],
            "expected_execpolicy": ["prompt", "forbidden"],
            "expected_guard_pair": "deny",
        },
        {
            "label": "worktree_remove_extra_target",
            "argv": ["git", "worktree", "remove", worktree, "extra"],
            "expected_execpolicy": ["prompt", "forbidden"],
            "expected_guard_pair": "deny",
        },
        {
            "label": "worktree_remove_missing_target",
            "argv": ["git", "worktree", "remove"],
            "expected_execpolicy": ["prompt", "forbidden"],
            "expected_guard_pair": "deny",
        },
        {
            "label": "branch_delete_multiple_targets",
            "argv": ["git", "branch", "-d", fixture.branch, "other-branch"],
            "expected_execpolicy": ["prompt", "forbidden"],
            "expected_guard_pair": "deny",
        },
        {
            "label": "branch_delete_force_combined_flag",
            "argv": ["git", "branch", "-d", "-f", fixture.branch],
            "expected_execpolicy": ["prompt", "forbidden"],
            "expected_guard_pair": "deny",
        },
        {
            "label": "branch_delete_df_combined_flag",
            "argv": ["git", "branch", "-df", fixture.branch],
            "expected_execpolicy": [],
            "expected_guard_pair": "deny",
            "skip_execpolicy_strict": True,
        },
        {
            "label": "branch_delete_fd_combined_flag",
            "argv": ["git", "branch", "-fd", fixture.branch],
            "expected_execpolicy": [],
            "expected_guard_pair": "deny",
            "skip_execpolicy_strict": True,
        },
        {
            "label": "branch_delete_long_force",
            "argv": ["git", "branch", "--delete", "--force", fixture.branch],
            "expected_execpolicy": [],
            "expected_guard_pair": "deny",
            "skip_execpolicy_strict": True,
        },
        {
            "label": "branch_delete_long_unique_prefix",
            "argv": ["git", "branch", "--dele", "--forc", fixture.branch],
            "expected_execpolicy": [],
            "expected_guard_pair": "deny",
            "skip_execpolicy_strict": True,
        },
        {
            "label": "branch_delete_force_shortcut",
            "argv": ["git", "branch", "-D", fixture.branch],
            "expected_execpolicy": ["forbidden"],
            "expected_guard_pair": "deny",
        },
        {
            "label": "worktree_add",
            "argv": ["git", "worktree", "add", "../tmp", "main"],
            "expected_execpolicy": ["forbidden"],
            "expected_guard_pair": "deny",
        },
        {
            "label": "worktree_prune",
            "argv": ["git", "worktree", "prune"],
            "expected_execpolicy": ["forbidden"],
            "expected_guard_pair": "deny",
        },
        # ─── Issue #1611 (contract revision, AC9/AC14): raw git add/commit
        # and `rtk git add/commit` are narrowed to controlled-executor-only
        # -- `.codex/rules/default.rules` denies these shapes statically,
        # and `git_mutation_command_policy.classify_agent_lane_add_commit`
        # denies them at the PreToolUse guard layer too (defense in depth).
        {
            "label": "git_add_denied_outside_controlled_executor",
            "argv": ["git", "add", "tracked.txt"],
            "expected_execpolicy": ["forbidden"],
            "expected_guard_pair": "deny",
        },
        {
            "label": "git_commit_denied_outside_controlled_executor",
            "argv": ["git", "commit", "-m", "msg"],
            "expected_execpolicy": ["forbidden"],
            "expected_guard_pair": "deny",
        },
        {
            "label": "rtk_git_add_denied_outside_controlled_executor",
            "argv": ["rtk", "git", "add", "tracked.txt"],
            "expected_execpolicy": ["forbidden", "prompt"],
            "expected_guard_pair": "deny",
        },
        {
            "label": "rtk_git_commit_denied_outside_controlled_executor",
            "argv": ["rtk", "git", "commit", "-m", "msg"],
            "expected_execpolicy": ["forbidden", "prompt"],
            "expected_guard_pair": "deny",
        },
        {
            "label": "controlled_executor_exact_invocation_allowed",
            "argv": [
                "uv",
                "run",
                "--locked",
                "python3",
                "scripts/agent-guards/controlled_git_change_exec.py",
                "--cwd",
                worktree,
                "--snapshot-json",
                f"{worktree}/.claude/tmp/issue-{fixture.issue_number}-scope-snapshot.json",
                "--path",
                "scripts/agent-guards/example.py",
                "--message",
                f"feat: issue-{fixture.issue_number} example change",
                "--expected-head",
                "0" * 40,
            ],
            "expected_execpolicy": ["allow"],
            "expected_guard_pair": "allow",
        },
        {
            # execpolicy `prefix_rule` matching is strictly literal-prefix,
            # position-anchored from argv[0] -- it has no wildcard, no
            # substring/startswith matching, and no argv-length or "exact
            # shape" modifier (see the comment block above the `bash`
            # prefix_rule in `.codex/rules/default.rules`). Because the
            # allow rule for the controlled executor can only anchor on the
            # fixed `uv run --locked python3 <script>` prefix, it cannot,
            # by construction, distinguish this well-formed-prefix-plus-
            # trailing-unexpected-flag invocation from
            # `controlled_executor_exact_invocation_allowed` above -- both
            # share the same literal prefix, so execpolicy necessarily
            # returns the same decision (`allow`) for both. This mirrors
            # `controlled_executor_from_main_root_denied` /
            # `controlled_executor_wrong_issue_worktree_denied` below, which
            # already expect execpolicy=allow + guard_pair=deny for the same
            # reason (execpolicy cannot inspect argv *values*, only
            # `worktree_scope_guard.py`'s token-aware
            # `parse_controlled_git_change_exec_command()` can, and it is
            # the layer that actually rejects the unexpected extra flag).
            "label": "controlled_executor_extra_argv_denied",
            "argv": [
                "uv",
                "run",
                "--locked",
                "python3",
                "scripts/agent-guards/controlled_git_change_exec.py",
                "--cwd",
                worktree,
                "--snapshot-json",
                f"{worktree}/.claude/tmp/issue-{fixture.issue_number}-scope-snapshot.json",
                "--path",
                "scripts/agent-guards/example.py",
                "--message",
                "feat: example",
                "--expected-head",
                "0" * 40,
                "--unexpected-extra-flag",
            ],
            "expected_execpolicy": ["allow"],
            "expected_guard_pair": "deny",
        },
        {
            "label": "controlled_executor_via_bash_lc_denied",
            "argv": [
                "bash",
                "-lc",
                f"uv run --locked python3 scripts/agent-guards/controlled_git_change_exec.py --cwd {worktree}",
            ],
            "expected_execpolicy": ["forbidden", "prompt"],
            "expected_guard_pair": "deny",
        },
        {
            "label": "controlled_executor_from_main_root_denied",
            "argv": [
                "uv",
                "run",
                "--locked",
                "python3",
                "scripts/agent-guards/controlled_git_change_exec.py",
                "--cwd",
                str(fixture.root),
                "--snapshot-json",
                f"{worktree}/.claude/tmp/issue-{fixture.issue_number}-scope-snapshot.json",
                "--path",
                "scripts/agent-guards/example.py",
                "--message",
                "feat: example",
                "--expected-head",
                "0" * 40,
            ],
            "expected_execpolicy": ["allow"],
            "expected_guard_pair": "deny",
            "expected_guard_reason": "worktree_binding_mismatch",
        },
        {
            "label": "controlled_executor_wrong_issue_worktree_denied",
            "argv": [
                "uv",
                "run",
                "--locked",
                "python3",
                "scripts/agent-guards/controlled_git_change_exec.py",
                "--cwd",
                str(fixture.root / ".claude" / "worktrees" / "issue-9999-other"),
                "--snapshot-json",
                f"{worktree}/.claude/tmp/issue-{fixture.issue_number}-scope-snapshot.json",
                "--path",
                "scripts/agent-guards/example.py",
                "--message",
                "feat: example",
                "--expected-head",
                "0" * 40,
            ],
            "expected_execpolicy": ["allow"],
            "expected_guard_pair": "deny",
            "expected_guard_reason": "worktree_binding_mismatch",
        },
    ]


def run_command(
    argv: list[str], *, cwd: Path | None = None, env: dict[str, str] | None = None, timeout: int = 30
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        argv,
        cwd=str(cwd) if cwd is not None else None,
        env=env,
        capture_output=True,
        text=True,
        timeout=timeout,
    )


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as fh:
        while True:
            chunk = fh.read(1024 * 1024)
            if not chunk:
                break
            digest.update(chunk)
    return digest.hexdigest()


def _find_package_root(start: Path, expected_name: str) -> Path:
    for parent in start.parents:
        pkg_json = parent / "package.json"
        if not pkg_json.exists():
            continue
        data = json.loads(pkg_json.read_text(encoding="utf-8"))
        if data.get("name") == expected_name:
            return parent
    raise MatrixError(f"failed to locate {expected_name} package root from {start}")


def _find_selected_platform_package(umbrella_dir: Path) -> tuple[Path, dict[str, Any]]:
    openai_dir = umbrella_dir.parent
    umbrella_pkg = json.loads((umbrella_dir / "package.json").read_text(encoding="utf-8"))
    optional_names = set((umbrella_pkg.get("optionalDependencies") or {}).keys())
    optional_dirs = {name.split("/", 1)[1] for name in optional_names if "/" in name}
    candidates: list[tuple[Path, dict[str, Any]]] = []
    for pkg_json in openai_dir.glob("*/package.json"):
        data = json.loads(pkg_json.read_text(encoding="utf-8"))
        dir_name = pkg_json.parent.name
        if dir_name == umbrella_dir.name:
            continue
        name = data.get("name")
        if dir_name in optional_dirs or (isinstance(name, str) and name in optional_names):
            candidates.append((pkg_json.parent, data))
    if len(candidates) != 1:
        raise MatrixError(
            f"expected exactly one selected platform package under {openai_dir},"
            f" found {[path.name for path, _ in candidates]}"
        )
    return candidates[0]


def _bin_relpath_from_package_json(package_json: dict[str, Any]) -> str:
    bin_field = package_json.get("bin")
    if isinstance(bin_field, str):
        return bin_field
    if isinstance(bin_field, dict):
        value = bin_field.get("codex")
        if isinstance(value, str):
            return value
    return ""


def _resolve_native_executable(platform_dir: Path, package_json: dict[str, Any]) -> Path:
    native_relpath = _bin_relpath_from_package_json(package_json)
    if native_relpath:
        candidate = (platform_dir / native_relpath).resolve()
        if candidate.exists():
            return candidate
    for candidate in sorted(platform_dir.rglob("*")):
        if not candidate.is_file():
            continue
        if candidate.name in {"codex", "codex.exe"}:
            return candidate.resolve()
    raise MatrixError(f"failed to resolve native executable under {platform_dir}")


def gather_codex_provenance(codex_binary: Path, npm_prefix: Path | None) -> dict[str, Any]:
    binary_realpath = Path(os.path.realpath(codex_binary))
    umbrella_dir = _find_package_root(binary_realpath, "@openai/codex")
    umbrella_pkg = json.loads((umbrella_dir / "package.json").read_text(encoding="utf-8"))
    platform_dir, platform_pkg = _find_selected_platform_package(umbrella_dir)
    native_executable = _resolve_native_executable(platform_dir, platform_pkg)
    npm_prefix_effective = npm_prefix or umbrella_dir.parents[2]
    npm_ls_run = run_command(["npm", "ls", "--json", "--all", "--prefix", str(npm_prefix_effective)])
    return {
        "binary": str(codex_binary),
        "binary_realpath": str(binary_realpath),
        "package_route": "npm:@openai/codex",
        "umbrella_package": {
            "name": umbrella_pkg.get("name"),
            "version": umbrella_pkg.get("version"),
        },
        "selected_platform_package": {
            "name": platform_pkg.get("name"),
            "version": platform_pkg.get("version"),
            "alias_dir": platform_dir.name,
            "path": str(platform_dir),
        },
        "native_executable": {
            "realpath": str(native_executable),
            "sha256": _sha256_file(native_executable),
        },
        "runner": {
            "platform": sys.platform,
            "arch": os.uname().machine if hasattr(os, "uname") else "unknown",
        },
        "npm_ls": {
            "return_code": npm_ls_run.returncode,
            "stdout": npm_ls_run.stdout.strip(),
            "stderr": npm_ls_run.stderr.strip(),
        },
    }


def install_codex_cli(*, npm_prefix: Path, package_spec: str) -> dict[str, Any]:
    install_run = run_command(
        [
            "npm",
            "install",
            "--prefix",
            str(npm_prefix),
            "--package-lock=false",
            "--no-audit",
            "--no-fund",
            package_spec,
        ],
        cwd=REPO_ROOT,
        timeout=180,
    )
    codex_binary = npm_prefix / "node_modules" / ".bin" / "codex"
    result = {
        "status": "ok" if install_run.returncode == 0 else "failed",
        "package_spec": package_spec,
        "npm_prefix": str(npm_prefix),
        "return_code": install_run.returncode,
        "stdout": install_run.stdout.strip(),
        "stderr": install_run.stderr.strip(),
        "codex_binary": str(codex_binary),
    }
    if install_run.returncode != 0:
        raise MatrixError(f"codex npm install failed: {result}")
    if not codex_binary.exists():
        raise MatrixError(f"codex binary missing after install: {result}")
    return result


def validate_codex_install(
    codex_binary: Path, expected_version: str, *, npm_prefix: Path | None = None
) -> dict[str, Any]:
    """Verify the pinned Codex binary exists, reports the exact version, and probes help."""
    version_run = run_command([str(codex_binary), "--version"])
    help_run = run_command([str(codex_binary), "execpolicy", "check", "--help"])
    provenance = gather_codex_provenance(codex_binary, npm_prefix)
    result = {
        **provenance,
        "expected_version": expected_version,
        "version_return_code": version_run.returncode,
        "version_stdout": version_run.stdout.strip(),
        "version_stderr": version_run.stderr.strip(),
        "help_return_code": help_run.returncode,
        "help_stdout": help_run.stdout.strip(),
        "help_stderr": help_run.stderr.strip(),
    }
    if version_run.returncode != 0:
        raise MatrixError(f"codex --version failed: {result}")
    if result["version_stdout"] != expected_version:
        raise MatrixError(f"codex version mismatch: expected {expected_version!r}, got {result['version_stdout']!r}")
    if help_run.returncode != 0:
        raise MatrixError(f"codex execpolicy probe failed: {result}")
    return result


def parse_execpolicy_response(
    *, label: str, argv: list[str], completed: subprocess.CompletedProcess[str]
) -> dict[str, Any]:
    """Strict parser for the documented execpolicy JSON contract.

    stdout must contain the raw JSON contract; stderr JSON aliases are rejected.
    """
    raw_stdout = completed.stdout.strip()
    raw_stderr = completed.stderr.strip()
    payload = {
        "argv": argv,
        "command": render_command(argv),
        "return_code": completed.returncode,
        "raw_stdout": raw_stdout,
        "raw_stderr": raw_stderr,
    }
    if completed.returncode != 0:
        raise MatrixError(f"{label}: codex execpolicy returned non-zero: {payload}")
    if not raw_stdout:
        raise MatrixError(f"{label}: codex execpolicy produced empty stdout: {payload}")
    try:
        data = json.loads(raw_stdout)
    except json.JSONDecodeError as exc:
        raise MatrixError(f"{label}: codex execpolicy JSON parse error: {exc}: {payload}") from exc
    if not isinstance(data, dict):
        raise MatrixError(f"{label}: codex execpolicy output must be a JSON object: {payload}")
    extra_keys = sorted(set(data) - ALLOWED_TOP_LEVEL_KEYS)
    if extra_keys:
        raise MatrixError(f"{label}: codex execpolicy schema drift (extra keys {extra_keys}): {payload}")
    decision = data.get("decision")
    if decision not in ALLOWED_DECISIONS:
        raise MatrixError(f"{label}: invalid decision {decision!r}: {payload}")
    matched_rules = data.get("matchedRules")
    if not isinstance(matched_rules, list):
        raise MatrixError(f"{label}: matchedRules must be a list: {payload}")
    return {
        **payload,
        "decision": decision,
        "matchedRules": matched_rules,
    }


def run_execpolicy_case(codex_binary: Path, rules: Path, case: dict[str, Any]) -> dict[str, Any]:
    completed = run_command(
        [str(codex_binary), "execpolicy", "check", "--rules", str(rules), "--", *case["argv"]],
        cwd=REPO_ROOT,
    )
    return parse_execpolicy_response(label=case["label"], argv=case["argv"], completed=completed)


def _pretool_payload(command: str, cwd: Path) -> str:
    return json.dumps(
        {
            "event": "PreToolUse",
            "tool_name": "Bash",
            "tool_input": {"command": command},
            "cwd": str(cwd),
        }
    )


def _extract_guard_reason(stderr: str) -> str | None:
    for line in stderr.splitlines():
        if line.startswith("reason: "):
            return line.split(": ", 1)[1].strip()
    return None


def invalidate_cleanup_contract(fixture: FixtureRepo) -> None:
    contract_path = fixture.root / "artifacts" / "agent-ops" / "cleanup_contract.json"
    contract_path.write_text("{\n", encoding="utf-8")


def run_hook_chain(command: str, fixture: FixtureRepo, *, cwd: Path | None = None) -> dict[str, Any]:
    """Replay the local two-guard cleanup routing decision."""
    env = dict(os.environ)
    env["CLAUDE_PROJECT_DIR"] = str(fixture.root)
    env["LOOP_ISSUE_NUMBER"] = fixture.issue_number
    effective_cwd = cwd or fixture.root
    payload = _pretool_payload(command, effective_cwd)
    local_main = subprocess.run(
        ["bash", str(LOCAL_MAIN_BRANCH_GUARD_SH)],
        input=payload,
        text=True,
        capture_output=True,
        env=env,
        timeout=30,
    )
    if local_main.returncode not in (0, 2):
        raise MatrixError(f"local_main_branch_guard unexpected exit code for {command!r}: {local_main.returncode}")
    worktree_guard = subprocess.run(
        ["bash", str(WORKTREE_SCOPE_GUARD_SH)],
        input=payload,
        text=True,
        capture_output=True,
        env=env,
        timeout=30,
    )
    if worktree_guard.returncode not in (0, 2):
        raise MatrixError(f"worktree_scope_guard unexpected exit code for {command!r}: {worktree_guard.returncode}")
    decision = "allow" if local_main.returncode == 0 and worktree_guard.returncode == 0 else "deny"
    return {
        "decision": decision,
        "reason": _extract_guard_reason(worktree_guard.stderr.strip()),
        "local_main_branch_guard": {
            "return_code": local_main.returncode,
            "stderr": local_main.stderr.strip(),
        },
        "worktree_scope_guard": {
            "return_code": worktree_guard.returncode,
            "stderr": worktree_guard.stderr.strip(),
        },
    }


def evaluate_cases(codex_binary: Path, rules: Path, fixture: FixtureRepo, writer: ArtifactWriter) -> None:
    """Run every matrix case and incrementally persist artifact-ready results."""
    writer.update_phase("cases", {"status": "running", "completed": 0, "failed": 0})
    for case in execpolicy_case_definitions(fixture):
        if case.get("materialize_contract"):
            materialize_valid_contract(fixture, case["operation"])
        if case.get("invalidate_contract") == "truncate":
            invalidate_cleanup_contract(fixture)
        execpolicy_parse_error: str | None = None
        execpolicy: dict[str, Any]
        try:
            execpolicy = run_execpolicy_case(codex_binary, rules, case)
        except MatrixError as exc:
            if not case.get("skip_execpolicy_strict"):
                raise
            completed = run_command(
                [str(codex_binary), "execpolicy", "check", "--rules", str(rules), "--", *case["argv"]],
                cwd=REPO_ROOT,
            )
            execpolicy_parse_error = str(exc)
            execpolicy = {
                "decision": None,
                "return_code": completed.returncode,
                "raw_stdout": completed.stdout.strip(),
                "matchedRules": [],
            }
        hook_cwd = fixture.worktree if case.get("hook_cwd") == "worktree" else fixture.root
        guard_pair = run_hook_chain(render_command(case["argv"]), fixture, cwd=hook_cwd)
        execpolicy_ok = (
            bool(case.get("skip_execpolicy_strict")) or execpolicy["decision"] in case["expected_execpolicy"]
        )
        guard_ok = guard_pair["decision"] == case["expected_guard_pair"]
        expected_reason = case.get("expected_guard_reason")
        reason_ok = expected_reason is None or guard_pair.get("reason") == expected_reason
        case_result = {
            "label": case["label"],
            "status": "passed" if execpolicy_ok and guard_ok and reason_ok else "failed",
            "argv": case["argv"],
            "command": render_command(case["argv"]),
            "guard_pair_cwd": str(hook_cwd),
            "expected": {
                "execpolicy": case["expected_execpolicy"],
                "guard_pair_decision": case["expected_guard_pair"],
                "guard_pair_reason": expected_reason,
                "skip_execpolicy_strict": bool(case.get("skip_execpolicy_strict")),
            },
            "actual": {
                "execpolicy": execpolicy["decision"],
                "guard_pair_decision": guard_pair["decision"],
                "guard_pair_reason": guard_pair.get("reason"),
                "execpolicy_parse_error": execpolicy_parse_error,
            },
            "return_code": execpolicy["return_code"],
            "raw_json": execpolicy["raw_stdout"],
            "matchedRules": execpolicy["matchedRules"],
            "guard_pair_trace": guard_pair,
        }
        if case_result["status"] == "failed":
            case_result["error"] = (
                f"{case['label']}: expected execpolicy {case['expected_execpolicy']} / "
                f"guard_pair {case['expected_guard_pair']} / reason {expected_reason}, "
                f"got execpolicy {execpolicy['decision']} / "
                f"guard_pair {guard_pair['decision']} / reason {guard_pair.get('reason')}"
            )
        writer.append_case(case_result)
        if case_result["status"] == "failed":
            raise MatrixError(case_result["error"])


def build_artifact(
    *,
    codex_binary: Path | None,
    expected_version: str,
    rules: Path,
    writer: ArtifactWriter,
    npm_prefix: Path | None = None,
    package_spec: str | None = None,
) -> None:
    """Build the full artifact payload incrementally. Raises MatrixError on failure."""
    resolved_codex_binary = codex_binary
    if package_spec is not None:
        if npm_prefix is None:
            raise MatrixError("package_spec requires npm_prefix")
        install = install_codex_cli(npm_prefix=npm_prefix, package_spec=package_spec)
        writer.update_phase("install", install)
        resolved_codex_binary = Path(install["codex_binary"])
    else:
        writer.update_phase("install", {"status": "skipped", "reason": "external_codex_binary"})
    if resolved_codex_binary is None:
        raise MatrixError("codex binary not provided")
    install = validate_codex_install(resolved_codex_binary, expected_version, npm_prefix=npm_prefix)
    writer.update_phase(
        "probe",
        {
            "status": "ok",
            "codex_binary": str(resolved_codex_binary),
            "version_return_code": install["version_return_code"],
            "help_return_code": install["help_return_code"],
        },
    )
    writer.set_codex(install)
    with tempfile.TemporaryDirectory(prefix="codex_execpolicy_matrix_") as tmp:
        fixture = build_fixture_repo(Path(tmp))
        evaluate_cases(resolved_codex_binary, rules, fixture, writer)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Codex execpolicy matrix runner (Issue #1150)")
    parser.add_argument("--codex-binary", help="path to an already-installed pinned codex binary")
    parser.add_argument("--npm-prefix", help="npm prefix where the pinned codex CLI will be installed")
    parser.add_argument("--package-spec", help="exact npm package spec for the pinned codex CLI")
    parser.add_argument("--expected-version", required=True, help="exact stdout expected from codex --version")
    parser.add_argument("--rules", default=str(RULES_DEFAULT), help="path to the execpolicy rules file")
    parser.add_argument("--artifact", required=True, help="path to write the JSON artifact")
    args = parser.parse_args(argv)

    artifact_path = Path(args.artifact)
    writer = ArtifactWriter.create(artifact_path, expected_version=args.expected_version, rules=Path(args.rules))
    try:
        build_artifact(
            codex_binary=Path(args.codex_binary) if args.codex_binary else None,
            expected_version=args.expected_version,
            rules=Path(args.rules),
            writer=writer,
            npm_prefix=Path(args.npm_prefix) if args.npm_prefix else None,
            package_spec=args.package_spec,
        )
        writer.finalize_ok()
        return 0
    except Exception as exc:  # pragma: no cover - exercised via CLI tests
        writer.finalize_failed(str(exc))
        print(str(exc), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
