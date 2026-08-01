#!/usr/bin/env python3
"""Fail-closed policy check for required-CI VRT snapshot writes.

The checker deliberately models only the known execution boundary: ci.yml,
local composite actions it references, and package scripts reached by those
commands. It normalizes shell quotes and line continuations, but never tries
to interpret arbitrary shell programs or dynamic GitHub Actions values.
"""

from __future__ import annotations

import json
import re
import shlex
import sys
from pathlib import Path
from typing import Any

import yaml


UPDATE_SCRIPT = "test:vrt:update:e2e"
WIRING_SCRIPT = "scripts/check-vrt-snapshot-policy.py"
INTERPOLATION = re.compile(r"\$\{\{|\$\{|\$[A-Za-z_]")
ASSIGNMENT = re.compile(r"[A-Za-z_][A-Za-z0-9_]*=")
SCRIPT_NAME = re.compile(r"[A-Za-z0-9:_-]+")
SHELL_SEPARATORS = {";", "&&", "||", "|", "&"}


class PolicyResult:
    def __init__(self, errors: list[str]) -> None:
        self.errors = errors


def _without_comments(command: str) -> str:
    """Discard full-line comments; quoted text remains command data."""
    return "\n".join(line for line in command.splitlines() if not line.lstrip().startswith("#"))


def _shell_segments(command: str, source: str, errors: list[str]) -> list[list[str]]:
    """Return bounded command segments after shell quote/continuation normalization."""
    normalized = re.sub(r"\\\r?\n", " ", _without_comments(command))
    try:
        lexer = shlex.shlex(normalized, posix=True, punctuation_chars=";&|()")
        lexer.whitespace_split = True
        lexer.commenters = "#"
        tokens = list(lexer)
    except ValueError as exc:
        errors.append(f"{source}: shell parse failure: {exc}")
        return []

    segments: list[list[str]] = []
    current: list[str] = []
    for token in tokens:
        if token in SHELL_SEPARATORS:
            if current:
                segments.append(current)
            current = []
        elif token not in {"(", ")"}:
            current.append(token)
    if current:
        segments.append(current)
    return segments


def _command_tokens(segment: list[str]) -> list[str]:
    """Drop simple leading assignments without evaluating shell control flow."""
    index = 0
    while index < len(segment) and ASSIGNMENT.fullmatch(segment[index]):
        index += 1
    if index < len(segment) and segment[index] == "env":
        index += 1
        while index < len(segment) and (segment[index].startswith("-") or ASSIGNMENT.fullmatch(segment[index])):
            index += 1
    return segment[index:]


def _has_interpolation(tokens: list[str]) -> bool:
    return any(INTERPOLATION.search(token) for token in tokens)


def _is_safe_mode(value: str | None) -> bool:
    return value in {"none", "false"}


def _option_value(args: list[str], index: int, option: str) -> tuple[bool, str | None]:
    token = args[index]
    if token == option:
        if index + 1 < len(args) and not args[index + 1].startswith("-"):
            return True, args[index + 1]
        return True, None
    prefix = f"{option}="
    if token.startswith(prefix):
        return True, token[len(prefix) :]
    return False, None


def _runner_command(tokens: list[str]) -> tuple[str, list[str]] | None:
    if not tokens:
        return None
    if tokens[0] in {"playwright", "vitest"}:
        return tokens[0], tokens[1:]
    if (
        len(tokens) >= 3
        and tokens[0] == "pnpm"
        and tokens[1] in {"exec", "dlx"}
        and tokens[2] in {"playwright", "vitest"}
    ):
        return tokens[2], tokens[3:]
    return None


def _runner_errors(tokens: list[str], source: str) -> list[str]:
    runner = _runner_command(tokens)
    if runner is None:
        return []
    name, args = runner
    if _has_interpolation(tokens):
        return [f"{source}: unresolved interpolation in update-sensitive invocation"]
    if name == "playwright":
        if not args or args[0] != "test":
            return []
        for index, token in enumerate(args):
            if token == "-u":
                return [f"{source}: Playwright write-capable update mode is forbidden in required CI"]
            matched, value = _option_value(args, index, "--update-snapshots")
            if matched and not _is_safe_mode(value):
                return [f"{source}: Playwright write-capable update mode is forbidden in required CI"]
        return []
    for index, _token in enumerate(args):
        if args[index] == "-u":
            return [f"{source}: Vitest write-capable update mode is forbidden in required CI"]
        matched, value = _option_value(args, index, "--update")
        if matched and not _is_safe_mode(value):
            return [f"{source}: Vitest write-capable update mode is forbidden in required CI"]
    return []


def _pnpm_script_references(tokens: list[str], source: str, errors: list[str]) -> list[str]:
    command = _command_tokens(tokens)
    if not command or command[0] != "pnpm":
        return []
    args = command[1:]
    if not args or args[0] in {"exec", "dlx", "install", "add", "remove"}:
        return []
    if args[0] == "run":
        if len(args) < 2:
            errors.append(f"{source}: pnpm run script name is missing")
            return []
        candidate = args[1]
    elif args[0].startswith("test:"):
        candidate = args[0]
    else:
        return []
    if INTERPOLATION.search(candidate):
        errors.append(f"{source}: unresolved interpolation in package script reference")
        return []
    if not SCRIPT_NAME.fullmatch(candidate):
        errors.append(f"{source}: unsupported package script reference")
        return []
    return [candidate]


def _command_errors(command: str, source: str, package_refs: list[str]) -> list[str]:
    errors: list[str] = []
    for segment in _shell_segments(command, source, errors):
        tokens = _command_tokens(segment)
        errors.extend(_runner_errors(tokens, source))
        package_refs.extend(_pnpm_script_references(segment, source, errors))
    return errors


def _load_yaml(path: Path, errors: list[str], label: str) -> Any | None:
    try:
        return yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError) as exc:
        errors.append(f"{label}: YAML parse failure: {exc}")
        return None


def _is_disabled(node: Any) -> bool:
    if not isinstance(node, dict) or "if" not in node:
        return False
    value = node["if"]
    return value is False or (isinstance(value, str) and value.strip().lower() in {"false", "${{ false }}"})


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
        if _is_disabled(step):
            continue
        run = step.get("run")
        if isinstance(run, str):
            errors.extend(_command_errors(run, f"{source}: step {index}", package_refs))
        uses = step.get("uses")
        if not isinstance(uses, str) or not uses.startswith("./.github/actions/"):
            continue
        with_values = step.get("with")
        if isinstance(with_values, dict) and any(
            isinstance(value, str) and INTERPOLATION.search(value) for value in with_values.values()
        ):
            errors.append(f"{source}: step {index}: unresolved interpolation in local composite action input")
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
        if not isinstance(job, dict):
            errors.append(f".github/workflows/ci.yml: jobs.{job_name} is not a mapping")
            continue
        if isinstance(job.get("uses"), str) and job["uses"].startswith("./.github/workflows/"):
            errors.append(
                f".github/workflows/ci.yml: jobs.{job_name} uses a local reusable workflow, which is unsupported"
            )
            continue
        if _is_disabled(job):
            continue
        steps = job.get("steps")
        if not isinstance(steps, list):
            errors.append(f".github/workflows/ci.yml: jobs.{job_name}.steps is missing")
            continue
        collected.extend(steps)
    return collected


def _is_wiring_command(command: str) -> bool:
    ignored: list[str] = []
    for segment in _shell_segments(command, "validator wiring", ignored):
        tokens = _command_tokens(segment)
        if len(tokens) >= 5 and tokens[0] == "uv" and tokens[1] == "run" and "--locked" in tokens[2:-2]:
            if tokens[-2] in {"python", "python3"} and tokens[-1] == WIRING_SCRIPT:
                return True
    return False


def _validate_wiring(steps: list[Any], errors: list[str]) -> None:
    for step in steps:
        if isinstance(step, dict) and not _is_disabled(step) and isinstance(step.get("run"), str):
            if _is_wiring_command(step["run"]):
                return
    errors.append(".github/workflows/ci.yml: validator CI wiring is missing or disabled")


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
        package_refs: list[str] = []
        errors.extend(_command_errors(command, f"package script {name}", package_refs))
        queue.extend(package_refs)


def check_policy(repo_root: Path) -> PolicyResult:
    """Check the required-CI execution graph rooted at ``.github/workflows/ci.yml``."""
    errors: list[str] = []
    workflow = repo_root / ".github" / "workflows" / "ci.yml"
    document = _load_yaml(workflow, errors, ".github/workflows/ci.yml") if workflow.is_file() else None
    if document is None:
        if not workflow.is_file():
            errors.append(".github/workflows/ci.yml: required workflow is missing")
        return PolicyResult(errors)

    steps = _workflow_steps(document, errors)
    _validate_wiring(steps, errors)
    visited_actions: set[Path] = set()
    initial: list[str] = []
    _scan_steps(steps, repo_root, errors, visited_actions, initial, ".github/workflows/ci.yml")

    scripts = _load_scripts(repo_root, errors)
    _scan_reachable_scripts(initial, scripts, errors)
    return PolicyResult(errors)


def main(argv: list[str]) -> int:
    if len(argv) != 1:
        print("usage: check-vrt-snapshot-policy.py")
        return 2
    result = check_policy(Path.cwd())
    print("VRT_SNAPSHOT_POLICY_CHECK_V1")
    print("status: pass" if not result.errors else "status: fail")
    for error in result.errors:
        print(f"error: {error}")
    return 0 if not result.errors else 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
