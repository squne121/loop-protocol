#!/usr/bin/env python3
"""Fail-closed policy check for required-CI VRT snapshot writes.

The checker deliberately models only the known execution boundary: ci.yml,
local composite actions it references, and package scripts reached by those
commands.  It does not attempt to interpret arbitrary shell programs.
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path
from typing import Any

import yaml


UPDATE_SCRIPT = "test:vrt:update:e2e"
WIRING_COMMAND = "uv run --locked python scripts/check-vrt-snapshot-policy.py"
INTERPOLATION = re.compile(r"\$\{\{|\$\{|\$[A-Za-z_]")
PLAYWRIGHT_UPDATE = re.compile(r"--update-snapshots(?:=(\S+)|\s+(\S+))?")
VITEST_UPDATE = re.compile(r"(?:(?<!-)--update\b|(?<![-\w])-u\b)(?:=(\S+)|\s+(\S+))?")


class PolicyResult:
    def __init__(self, errors: list[str]) -> None:
        self.errors = errors


def _without_comments(command: str) -> str:
    """Discard full-line YAML/shell comments without parsing arbitrary shell."""
    return "\n".join(line for line in command.splitlines() if not line.lstrip().startswith("#"))


def _is_safe_mode(match: re.Match[str]) -> bool:
    value = next((part for part in match.groups() if part is not None), None)
    return value == "none"


def _command_errors(command: str, source: str) -> list[str]:
    clean = _without_comments(command)
    errors: list[str] = []
    for line in clean.splitlines():
        lowered = line.lower()
        playwright_command = bool(re.search(r"\bplaywright\s+test\b", lowered))
        vitest_command = bool(re.search(r"\bvitest\s+(?:run|watch)\b", lowered))
        sensitive = playwright_command or vitest_command or UPDATE_SCRIPT in line
        if sensitive and INTERPOLATION.search(line):
            errors.append(f"{source}: unresolved interpolation in update-sensitive invocation")
        if playwright_command:
            for match in PLAYWRIGHT_UPDATE.finditer(line):
                if not _is_safe_mode(match):
                    errors.append(f"{source}: Playwright write-capable update mode is forbidden in required CI")
        if vitest_command:
            for match in VITEST_UPDATE.finditer(line):
                if not _is_safe_mode(match):
                    errors.append(f"{source}: Vitest write-capable update mode is forbidden in required CI")
    return errors


def _referenced_pnpm_scripts(command: str) -> list[str]:
    """Return only literal pnpm script references from a bounded command form."""
    tokens = re.findall(r"[^\s;&|()]+", _without_comments(command))
    names: list[str] = []
    for index, token in enumerate(tokens):
        if token != "pnpm" or index + 1 >= len(tokens):
            continue
        candidate_index = index + 1
        if tokens[candidate_index] == "run":
            candidate_index += 1
        elif tokens[candidate_index] == "exec":
            continue
        if candidate_index < len(tokens) and re.fullmatch(r"[A-Za-z0-9:_-]+", tokens[candidate_index]):
            names.append(tokens[candidate_index])
    return names


def _load_yaml(path: Path, errors: list[str], label: str) -> Any | None:
    try:
        return yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError) as exc:
        errors.append(f"{label}: YAML parse failure: {exc}")
        return None


def _scan_composite_action(
    path: Path, repo_root: Path, errors: list[str], visited: set[Path], package_refs: list[str]
) -> None:
    resolved = path.resolve()
    if resolved in visited:
        return
    visited.add(resolved)
    document = _load_yaml(path, errors, str(path.relative_to(repo_root)))
    if not isinstance(document, dict):
        errors.append(f"{path.relative_to(repo_root)}: composite action is not a mapping")
        return
    runs = document.get("runs")
    steps = runs.get("steps") if isinstance(runs, dict) else None
    if not isinstance(steps, list):
        errors.append(f"{path.relative_to(repo_root)}: composite action steps are missing")
        return
    _scan_steps(steps, repo_root, errors, visited, package_refs, str(path.relative_to(repo_root)))


def _scan_steps(
    steps: list[Any], repo_root: Path, errors: list[str], visited: set[Path], package_refs: list[str], source: str
) -> None:
    for index, step in enumerate(steps):
        if not isinstance(step, dict):
            errors.append(f"{source}: step {index} is not a mapping")
            continue
        run = step.get("run")
        if isinstance(run, str):
            errors.extend(_command_errors(run, f"{source}: step {index}"))
            package_refs.extend(_referenced_pnpm_scripts(run))
        uses = step.get("uses")
        if not isinstance(uses, str) or not uses.startswith("./.github/actions/"):
            continue
        action_dir = repo_root / uses[2:]
        candidates = [action_dir / "action.yml", action_dir / "action.yaml"]
        action_path = next((candidate for candidate in candidates if candidate.is_file()), None)
        if action_path is None:
            errors.append(f"{source}: local composite action is missing: {uses}")
            continue
        _scan_composite_action(action_path, repo_root, errors, visited, package_refs)


def _workflow_steps(document: Any, errors: list[str]) -> list[Any]:
    if not isinstance(document, dict):
        errors.append(".github/workflows/ci.yml: workflow is not a mapping")
        return []
    jobs = document.get("jobs")
    if not isinstance(jobs, dict):
        errors.append(".github/workflows/ci.yml: jobs mapping is missing")
        return []
    collected: list[Any] = []
    for job_name, job in jobs.items():
        steps = job.get("steps") if isinstance(job, dict) else None
        if not isinstance(steps, list):
            errors.append(f".github/workflows/ci.yml: jobs.{job_name}.steps is missing")
            continue
        collected.extend(steps)
    return collected


def _validate_ci_config(repo_root: Path, errors: list[str]) -> None:
    config = repo_root / "playwright.config.ts"
    try:
        text = config.read_text(encoding="utf-8")
    except OSError as exc:
        errors.append(f"playwright.config.ts: cannot read config: {exc}")
        return
    expected = re.compile(r"updateSnapshots\s*:\s*process\.env\.CI\s*\?\s*['\"]none['\"]\s*:")
    if not expected.search(text):
        errors.append("playwright.config.ts: required CI updateSnapshots must resolve to 'none'")


def _load_scripts(repo_root: Path, errors: list[str]) -> dict[str, str]:
    try:
        manifest = json.loads((repo_root / "package.json").read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        errors.append(f"package.json: cannot parse scripts: {exc}")
        return {}
    scripts = manifest.get("scripts") if isinstance(manifest, dict) else None
    if not isinstance(scripts, dict) or not all(isinstance(k, str) and isinstance(v, str) for k, v in scripts.items()):
        errors.append("package.json: scripts must be a string mapping")
        return {}
    return scripts


def _scan_reachable_scripts(initial: list[str], scripts: dict[str, str], errors: list[str]) -> None:
    queue = list(initial)
    visited: set[str] = set()
    while queue:
        name = queue.pop()
        if name in visited:
            continue
        visited.add(name)
        if name == UPDATE_SCRIPT:
            errors.append(f"package script: {UPDATE_SCRIPT} is reachable from required CI")
            continue
        command = scripts.get(name)
        if command is None:
            continue
        errors.extend(_command_errors(command, f"package script {name}"))
        queue.extend(_referenced_pnpm_scripts(command))


def check_policy(repo_root: Path) -> PolicyResult:
    """Check the required CI execution graph rooted at ``.github/workflows/ci.yml``."""
    errors: list[str] = []
    workflow = repo_root / ".github" / "workflows" / "ci.yml"
    document = _load_yaml(workflow, errors, ".github/workflows/ci.yml") if workflow.is_file() else None
    if document is None:
        if not workflow.is_file():
            errors.append(".github/workflows/ci.yml: required workflow is missing")
        return PolicyResult(errors)

    workflow_text = workflow.read_text(encoding="utf-8")
    if WIRING_COMMAND not in workflow_text:
        errors.append(".github/workflows/ci.yml: validator CI wiring is missing")

    steps = _workflow_steps(document, errors)
    visited_actions: set[Path] = set()
    initial: list[str] = []
    _scan_steps(steps, repo_root, errors, visited_actions, initial, ".github/workflows/ci.yml")
    _validate_ci_config(repo_root, errors)

    scripts = _load_scripts(repo_root, errors)
    _scan_reachable_scripts(initial, scripts, errors)
    return PolicyResult(errors)


def main(argv: list[str]) -> int:
    if len(argv) > 2 or (len(argv) == 2 and argv[1] != "--root"):
        print("usage: check-vrt-snapshot-policy.py [--root]")
        return 2
    result = check_policy(Path.cwd())
    print("VRT_SNAPSHOT_POLICY_CHECK_V1")
    print("status: pass" if not result.errors else "status: fail")
    for error in result.errors:
        print(f"error: {error}")
    return 0 if not result.errors else 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
