#!/usr/bin/env python3
"""Verify that Ruff rules are owned by pyproject.toml (Issue #1764)."""

from __future__ import annotations

import argparse
import json
import re
import shlex
import subprocess
import tomllib
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

EXPECTED_SELECT = ("E", "F")
EXPECTED_TARGET_PATHS = (
    ".claude/scripts",
    "scripts",
    "schemas",
    ".claude/skills",
)
RULE_OVERRIDE_OPTIONS = {
    "--config",
    "--isolated",
    "--select",
    "--extend-select",
    "--ignore",
    "--extend-ignore",
    "--per-file-ignores",
    "--extend-per-file-ignores",
    "--preview",
}
FORBIDDEN_LINT_CONFIGURATION_KEYS = {
    "ignore",
    "extend-ignore",
    "extend-select",
    "extend-per-file-ignores",
}
E402_PROBE_TIMEOUT_SECONDS = 10
SKILL_RELATIVE_PATH = Path(".claude/skills/ci-test-performance/SKILL.md")
EXPECTED_SKILL_COMMAND = (
    "uv run --locked ruff check .claude/scripts scripts schemas .claude/skills"
)
EXPECTED_RUFF_COMMAND = (
    "uv",
    "run",
    "--locked",
    "ruff",
    "check",
    *EXPECTED_TARGET_PATHS,
)
EXPECTED_VERIFIER_COMMAND = (
    "uv",
    "run",
    "--locked",
    "python3",
    "scripts/ci/verify_ruff_configuration.py",
    "--root",
    ".",
)


@dataclass(frozen=True)
class Violation:
    """A stable failure key and the context needed to repair it."""

    key: str
    detail: str


def _as_code_list(value: Any) -> list[str] | None:
    if isinstance(value, list) and all(isinstance(code, str) for code in value):
        return value
    return None


def _contains_e402(value: Any) -> bool:
    codes = _as_code_list(value)
    return codes is not None and "E402" in codes


def _is_specific_path(path: Any) -> bool:
    return isinstance(path, str) and path and not any(token in path for token in "*?[")


def _is_expected_target_file(root: Path, candidate: Path) -> bool:
    """Return whether a per-file exception is a linted Python source file."""
    if not candidate.is_file() or candidate.suffix not in {".py", ".pyi"}:
        return False

    resolved_root = root.resolve()
    resolved_candidate = candidate.resolve()
    if not resolved_candidate.is_relative_to(resolved_root):
        return False

    return any(
        resolved_candidate.is_relative_to((root / target).resolve())
        for target in EXPECTED_TARGET_PATHS
    )


def _probe_e402_diagnostic(root: Path, relative_path: str) -> tuple[bool, str]:
    """Prove that an E402 exception suppresses a real isolated diagnostic.

    ``--isolated`` deliberately bypasses every repository configuration,
    including the per-file exception being validated.  This is a diagnostic
    oracle only; it is never used to infer configuration origin.
    """
    command = [
        "uv",
        "run",
        "--locked",
        "ruff",
        "check",
        "--isolated",
        "--select",
        "E402",
        "--output-format",
        "json",
        "--no-cache",
        relative_path,
    ]
    try:
        result = subprocess.run(
            command,
            cwd=root,
            capture_output=True,
            text=True,
            timeout=E402_PROBE_TIMEOUT_SECONDS,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return False, f"probe execution failed: {exc}"
    try:
        diagnostics = json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        return False, f"probe output was not JSON: {exc}"
    if not isinstance(diagnostics, list):
        return False, "probe output was not a diagnostic list"
    if any(isinstance(item, dict) and item.get("code") == "E402" for item in diagnostics):
        return True, "E402 diagnostic observed"
    return False, "isolated probe emitted no E402 diagnostic"


def _is_relevant_configuration_directory(root: Path, directory: Path) -> bool:
    """Return whether ``directory`` can affect one of the fixed lint targets."""
    return directory == root or any(
        directory.is_relative_to((root / target).resolve())
        for target in EXPECTED_TARGET_PATHS
    )


def _nested_pyproject_has_ruff_configuration(path: Path) -> bool:
    """Return whether a parseable nested pyproject participates in Ruff discovery."""
    try:
        parsed = tomllib.loads(path.read_text(encoding="utf-8"))
    except (OSError, tomllib.TOMLDecodeError):
        return False
    tool = parsed.get("tool")
    return isinstance(tool, dict) and isinstance(tool.get("ruff"), dict)


def _find_ruff_configuration_files(root: Path) -> list[Path]:
    """Find relevant config files which could override root pyproject authority.

    Ruff resolves the closest configuration for each linted file. A nested
    ``pyproject.toml`` participates only when it has a ``[tool.ruff]`` table.
    """
    candidates: list[Path] = []
    ignored_parts = {".git", ".venv", "node_modules"}
    for path in root.rglob("*"):
        if not path.is_file() or ignored_parts.intersection(path.relative_to(root).parts):
            continue
        relative = path.relative_to(root)
        if not _is_relevant_configuration_directory(root, path.parent.resolve()):
            continue
        if path.name in {"ruff.toml", ".ruff.toml"}:
            candidates.append(relative)
        elif (
            path.name == "pyproject.toml"
            and relative != Path("pyproject.toml")
            and _nested_pyproject_has_ruff_configuration(path)
        ):
            candidates.append(relative)
    return sorted(candidates)


def _validate_ruff_config(root: Path) -> list[Violation]:
    config_path = root / "pyproject.toml"
    if not config_path.is_file():
        return [Violation("ruff_config_missing", "pyproject.toml がありません")]

    try:
        parsed = tomllib.loads(config_path.read_text(encoding="utf-8"))
    except (OSError, tomllib.TOMLDecodeError) as exc:
        return [Violation("ruff_config_invalid", str(exc))]

    ruff = parsed.get("tool", {}).get("ruff", {})
    lint = ruff.get("lint", {}) if isinstance(ruff, dict) else {}
    violations: list[Violation] = []

    select = _as_code_list(lint.get("select")) if isinstance(lint, dict) else None
    if select is None or len(select) != len(EXPECTED_SELECT) or set(select) != set(EXPECTED_SELECT):
        violations.append(
            Violation("ruff_select_not_e_f", "[tool.ruff.lint].select は [\"E\", \"F\"] 必須")
        )

    if isinstance(ruff, dict) and "extend" in ruff:
        violations.append(
            Violation(
                "ruff_config_extend_not_allowed",
                "[tool.ruff].extend は設定 authority を分散させるため許可されません",
            )
        )

    if isinstance(lint, dict):
        for setting in FORBIDDEN_LINT_CONFIGURATION_KEYS:
            if setting in lint:
                violations.append(
                    Violation(
                        "ruff_rule_configuration_not_resolved",
                        f"[tool.ruff.lint].{setting} は E/F authority を変更するため許可されません",
                    )
                )

    for table_name, table in (("tool.ruff", ruff), ("tool.ruff.lint", lint)):
        if isinstance(table, dict) and table.get("preview") is True:
            violations.append(
                Violation(
                    "ruff_config_preview_not_allowed",
                    f"{table_name}.preview は設定 authority を変更するため許可されません",
                )
            )

    for path in _find_ruff_configuration_files(root):
        violations.append(
            Violation(
                "ruff_configuration_source_not_pyproject",
                f"Ruff 設定を上書きし得るファイルは許可されません: {path}",
            )
        )

    for table_name, table in (("tool.ruff", ruff), ("tool.ruff.lint", lint)):
        if not isinstance(table, dict):
            continue
        for setting in ("ignore", "extend-ignore"):
            if _contains_e402(table.get(setting)):
                violations.append(
                    Violation(
                        "ruff_global_e402_ignore",
                        f"{table_name}.{setting} に E402 を含められません",
                    )
                )

    if isinstance(lint, dict):
        for setting in ("per-file-ignores", "extend-per-file-ignores"):
            entries = lint.get(setting, {})
            if not isinstance(entries, dict):
                violations.append(
                    Violation(
                        "ruff_per_file_e402_exception_invalid",
                        f"{setting} は path から code list への mapping 必須",
                    )
                )
                continue
            for path, codes in entries.items():
                candidate = root / path if _is_specific_path(path) else root
                is_valid_exception = (
                    not _is_specific_path(path)
                    or _as_code_list(codes) != ["E402"]
                    or not _is_expected_target_file(root, candidate)
                )
                if is_valid_exception:
                    violations.append(
                        Violation(
                            "ruff_per_file_e402_exception_invalid",
                            f"{setting}.{path!r} は具体的 path と [\"E402\"] のみ許可",
                        )
                    )
                    continue
                has_diagnostic, detail = _probe_e402_diagnostic(root, path)
                if not has_diagnostic:
                    violations.append(
                        Violation(
                            "ruff_per_file_e402_diagnostic_missing",
                            f"{setting}.{path!r} は isolated E402 probe を満たしません: {detail}",
                        )
                    )
    return violations


def _workflow_run_blocks(workflow: Any) -> list[tuple[str, str, str]]:
    if not isinstance(workflow, dict):
        return []
    jobs = workflow.get("jobs", {})
    if not isinstance(jobs, dict):
        return []
    runs: list[tuple[str, str, str]] = []
    for job_name, job in jobs.items():
        if not isinstance(job, dict) or not isinstance(job.get("steps"), list):
            continue
        for index, step in enumerate(job["steps"]):
            if isinstance(step, dict):
                run = step.get("run")
                if isinstance(run, str):
                    name = step.get("name") if isinstance(step.get("name"), str) else f"step-{index}"
                    runs.append((str(job_name), name, run))
    return runs


@dataclass(frozen=True)
class WorkflowInvocation:
    """A Ruff or verifier mention discovered without executing shell syntax."""

    job_name: str
    step_name: str
    ordinal: int
    raw: str
    tokens: tuple[str, ...]
    kind: str
    parse_error: bool = False


def _commands_in_run_block(run_block: str) -> list[tuple[str, tuple[str, ...], bool]]:
    commands: list[tuple[str, tuple[str, ...], bool]] = []
    for line in run_block.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        try:
            commands.append((line, tuple(shlex.split(line)), False))
        except ValueError:
            # Keep malformed lines so they cannot erase a counted invocation.
            commands.append((line, tuple(), True))
    return commands


def _workflow_invocations(workflow: Any) -> list[WorkflowInvocation]:
    """Find all Ruff/verifier mentions, including shell-wrapper payloads.

    This performs lexical inspection only. It never expands variables or
    interprets shell syntax; indirection is invalid for this authority gate.
    """
    invocations: list[WorkflowInvocation] = []
    ordinal = 0
    for job_name, step_name, run_block in _workflow_run_blocks(workflow):
        for raw, tokens, parse_error in _commands_in_run_block(run_block):
            ordinal += 1
            if "scripts/ci/verify_ruff_configuration.py" in raw:
                kind = "verifier"
            elif "ruff" in raw.lower():
                kind = "ruff"
            else:
                continue
            invocations.append(
                WorkflowInvocation(
                    job_name=job_name,
                    step_name=step_name,
                    ordinal=ordinal,
                    raw=raw,
                    tokens=tokens,
                    kind=kind,
                    parse_error=parse_error,
                )
            )
    return invocations


def _has_shell_indirection(invocation: WorkflowInvocation) -> bool:
    if invocation.parse_error:
        return True
    if re.search(r"(?:^|[;&|]\s*|\s)(?:bash|sh)\s+-c\b", invocation.raw):
        return True
    if "$(" in invocation.raw or "`" in invocation.raw or "$" in invocation.raw:
        return True
    if re.search(r"&&|\|\||[;|&]", invocation.raw):
        return True
    return any(token.startswith("@") for token in invocation.tokens)


def _is_rule_override(token: str) -> bool:
    return token in RULE_OVERRIDE_OPTIONS or any(
        token.startswith(f"{option}=") for option in RULE_OVERRIDE_OPTIONS
    )


def _has_rule_override(invocation: WorkflowInvocation) -> bool:
    return any(_is_rule_override(token) for token in invocation.tokens) or any(
        re.search(rf"(?<![\w-]){re.escape(option)}(?:=|\s|$)", invocation.raw)
        for option in RULE_OVERRIDE_OPTIONS
    )


def _validate_ci_test_performance_skill(root: Path) -> list[Violation]:
    skill_path = root / SKILL_RELATIVE_PATH
    if not skill_path.is_file():
        return [
            Violation(
                "ci_test_performance_skill_missing",
                f"{SKILL_RELATIVE_PATH} がありません",
            )
        ]

    try:
        commands = [
            shlex.split(line.strip())
            for line in skill_path.read_text(encoding="utf-8").splitlines()
            if line.strip().startswith("uv run --locked ruff check")
        ]
    except (OSError, ValueError) as exc:
        return [Violation("ci_test_performance_skill_invalid", str(exc))]

    if len(commands) != 1:
        return [
            Violation(
                "ci_test_performance_skill_ruff_command_invalid",
                "Skill には Ruff の推奨 command がちょうど 1 つ必要です",
            )
        ]

    command = commands[0]
    if any(_is_rule_override(token) for token in command):
        return [
            Violation(
                "ci_test_performance_skill_cli_rule_override",
                "Skill の Ruff command に rule/config override を指定できません",
            )
        ]
    if " ".join(command) != EXPECTED_SKILL_COMMAND:
        return [
            Violation(
                "ci_test_performance_skill_ruff_command_invalid",
                "Skill の Ruff command は workflow と同じ対象 path 集合を使用する必要があります",
            )
        ]
    return []


def _validate_workflow(root: Path) -> list[Violation]:
    workflow_path = root / ".github/workflows/ci.yml"
    if not workflow_path.is_file():
        return [Violation("workflow_ci_missing", ".github/workflows/ci.yml がありません")]
    try:
        workflow = yaml.safe_load(workflow_path.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError) as exc:
        return [Violation("workflow_ci_invalid", str(exc))]

    violations: list[Violation] = []
    invocations = _workflow_invocations(workflow)
    verifier_invocations = [item for item in invocations if item.kind == "verifier"]
    if (
        len(verifier_invocations) != 1
        or verifier_invocations[0].tokens != EXPECTED_VERIFIER_COMMAND
        or _has_shell_indirection(verifier_invocations[0])
    ):
        violations.append(
            Violation(
                "workflow_ruff_verifier_root_step_invalid",
                "CI root verifier は exact command でちょうど 1 回実行する必要があります",
            )
        )
        if any(_has_shell_indirection(item) for item in verifier_invocations):
            violations.append(
                Violation(
                    "workflow_ruff_verifier_indirection_not_allowed",
                    "CI root verifier に shell wrapper・variable・argfile を使用できません",
                )
            )

    ruff_invocations = [item for item in invocations if item.kind == "ruff"]
    if len(ruff_invocations) != 1:
        violations.append(
            Violation(
                "workflow_ruff_invocation_count_invalid",
                "workflow 全体の Ruff invocation はちょうど 1 つ必要です",
            )
        )
        return violations

    ruff_invocation = ruff_invocations[0]
    if (ruff_invocation.job_name, ruff_invocation.step_name) != ("python-test", "Ruff check (timed)"):
        violations.append(
            Violation(
                "workflow_ruff_step_invalid",
                "Ruff invocation は python-test job の Ruff check (timed) step に限定されます",
            )
        )
    if _has_rule_override(ruff_invocation):
        violations.append(
            Violation(
                "workflow_cli_rule_override",
                "workflow の Ruff command に rule override を指定できません",
            )
        )

    expected_prefix = (
        "run_timed",
        "ruff_check",
        "uv",
        "run",
        "--locked",
        "ruff",
        "check",
    )
    if _has_shell_indirection(ruff_invocation):
        violations.append(
            Violation(
                "workflow_ruff_indirection_not_allowed",
                "Ruff invocation に shell wrapper・variable・argfile を使用できません",
            )
        )
        return violations
    if ruff_invocation.tokens[: len(expected_prefix)] != expected_prefix:
        violations.append(
            Violation(
                "workflow_ruff_command_invalid",
                "Ruff command は uv run --locked ruff check を使用する必要があります",
            )
        )
        return violations

    target_paths = ruff_invocation.tokens[len(expected_prefix) :]
    if (
        len(target_paths) != len(EXPECTED_TARGET_PATHS)
        or len(set(target_paths)) != len(target_paths)
        or set(target_paths) != set(EXPECTED_TARGET_PATHS)
    ):
        violations.append(
            Violation(
                "workflow_ruff_target_paths_changed",
                "Ruff 対象 path 集合が契約値と一致しません",
            )
        )
    elif verifier_invocations and verifier_invocations[0].ordinal >= ruff_invocation.ordinal:
        violations.append(
            Violation(
                "workflow_ruff_verifier_order_invalid",
                "CI root verifier は canonical Ruff command より前に 1 回実行する必要があります",
            )
        )
    return violations


def collect_violations(root: Path) -> list[Violation]:
    """Collect all deterministic contract violations for ``root``."""
    return (
        _validate_ruff_config(root)
        + _validate_workflow(root)
        + _validate_ci_test_performance_skill(root)
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, required=True)
    args = parser.parse_args(argv)

    violations = collect_violations(args.root.resolve())
    if violations:
        for violation in violations:
            print(f"FAILURE_KEY: {violation.key} - {violation.detail}")
        return 1
    print("Ruff configuration contract: PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
