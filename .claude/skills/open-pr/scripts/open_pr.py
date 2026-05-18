#!/usr/bin/env python3
"""CLI wrapper for deterministic PR opening workflow.

The wrapper owns deterministic PR start-up steps that can be executed in
repeatable form:
- publish gate validation
- template guard
- linked issue state checks and Closes/Refs downgrade
- optional dependency override (parent_issue / is_dependent)
- idempotency handling
- gh pr create
- machine-readable KEY=VALUE stdout contract
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
from typing import Any


REPO_ROOT = Path(__file__).resolve().parents[4]
CANONICAL_TEMPLATE_PATH = REPO_ROOT / ".github" / "PULL_REQUEST_TEMPLATE.md"
SYNC_EVIDENCE_TEMPLATE_PATH = REPO_ROOT / "scripts" / "sync-pr-evidence-template.py"

REQUIRED_TEMPLATE_HEADERS = [
    "Linked Issue",
    "Summary",
    "Acceptance Criteria -> Evidence",
    "Commands Run",
    "Changed Paths",
    "Risks",
    "Rollback",
    "Follow-ups Intentionally Deferred",
    "類似 Issue 統合方針",
    "Knowledge Harvesting",
    "Process / Skill / Agent Improvements",
    "Renumbering / Identifier Migration",
    "Long-form Evidence",
]

ERROR_APPROVAL_MISSING = "E_APPROVAL_MISSING"
ERROR_TEMPLATE_GUARD = "E_PR_TEMPLATE_GUARD"
ERROR_LINKED_ISSUE_STATE_UNKNOWN = "E_LINKED_ISSUE_STATE_UNKNOWN"
ERROR_CANONICAL_AMBIGUOUS = "E_CANONICAL_PR_AMBIGUOUS"
ERROR_CANONICAL_INVALID = "E_CANONICAL_PR_INVALID"
ERROR_BRANCH_NOT_FOUND = "E_BRANCH_NOT_FOUND"
ERROR_GH_PR_CREATE = "E_GH_PR_CREATE_FAILED"
ERROR_GH = "E_GH_COMMAND_FAILED"
ERROR_CHANGE_KIND_INVALID = "E_OPEN_PR_CHANGE_KIND_INVALID"
ERROR_ARGUMENT_INVALID = "E_OPEN_PR_ARGUMENT_INVALID"
PREFLIGHT_TEMPLATE_GUARD = "template-required-sections"
PREFLIGHT_EVIDENCE_DRIFT_GUARD = "template-evidence-drift-check"
COMMANDS_RUN_HEADER = "Commands Run"
COMMANDS_RUN_TABLE_HEADER = "| Command | Exit Code | Scope | Notes |"
COMMANDS_RUN_TABLE_SEPARATOR = "|---|---:|---|---|"
COMMANDS_RUN_PLACEHOLDER_GIT_DIFF = (
    "`git diff --check`",
    "`<exit-code>`",
    "all lanes",
    "format 崩れなし",
)
COMMANDS_RUN_PLACEHOLDER_JUST_CHECK = (
    "`just check <target>`",
    "`<exit-code-or-n/a>`",
    "code/mixed lanes default",
    "docs/rules-only lane では targeted check または `対象外` の理由を書く",
)
COMMANDS_RUN_PLACEHOLDER_ISSUE_DEFINED = (
    "`<verification command>`",
    "`<exit-code>`",
    "issue-defined",
    "VC と一致するコマンドを記載",
)


class OpenPRError(Exception):
    def __init__(self, code: str, stderr_message: str, diagnostics: dict[str, str]) -> None:
        super().__init__(stderr_message)
        self.code = code
        self.stderr_message = stderr_message
        self.diagnostics = diagnostics

    def __str__(self) -> str:
        return self.stderr_message


def _to_bool(value: str) -> bool:
    if isinstance(value, bool):
        return value
    normalized = value.strip().lower()
    if normalized in {"true", "1", "yes", "y", "on"}:
        return True
    if normalized in {"false", "0", "no", "n", "off"}:
        return False
    raise argparse.ArgumentTypeError(f"expected boolean value, got {value!r}")


def _to_boolish(value: object) -> bool:
    try:
        return _to_bool(str(value))
    except argparse.ArgumentTypeError:
        return False


class OpenPRArgumentParser(argparse.ArgumentParser):
    def error(self, message: str) -> None:
        raise OpenPRError(
            code=ERROR_ARGUMENT_INVALID,
            stderr_message=f"[ERROR] open-pr argument parsing failed: {message}",
            diagnostics={
                "ERROR": ERROR_ARGUMENT_INVALID,
                "DIAGNOSTIC_STAGE": "argument-parsing",
                "DIAGNOSTIC_KIND": "input-validation-failure",
                "ERROR_DETAIL": "argument-parse-error",
            },
        )


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = OpenPRArgumentParser(description="Deterministic open-pr wrapper")
    parser.add_argument("--pr_title", required=True)
    parser.add_argument("--linked_issue", required=True)
    parser.add_argument("--publish", required=True, help="must be 'yes' for execution")
    parser.add_argument("--pr_body", default="")
    parser.add_argument("--pr_body_file", default="")
    parser.add_argument("--dry_run", type=_to_bool, default=False)
    parser.add_argument("--change_kind", default="mixed")
    parser.add_argument("--parent_issue", default="")
    parser.add_argument("--is_dependent", default="false")
    parser.add_argument("--canonical_pr_url", default="")
    parser.add_argument("--superseded_prs", default="")
    parser.add_argument("--repair_context", default="")
    parser.add_argument("--github_bin", default="gh")
    return parser.parse_args(argv)


def _emit_stdout(key: str, value: str) -> None:
    sys.stdout.write(f"{key}={value}\n")


def _emit_stderr(message: str) -> None:
    sys.stderr.write(f"{message}\n")


def _emit_diagnostics(diagnostics: dict[str, str]) -> None:
    for key, value in diagnostics.items():
        if value:
            _emit_stdout(key, _sanitize_diagnostic_value(value))


def _run_command(args: list[str], check: bool = True, capture_output: bool = True) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        args,
        check=check,
        text=True,
        capture_output=capture_output,
    )


def _command_preview(args: list[str], *, limit: int = 4) -> str:
    preview = " ".join(args[:limit])
    return preview if len(args) <= limit else f"{preview} ..."


def _sanitize_diagnostic_value(value: str) -> str:
    return (
        value.replace("\\", "\\\\")
        .replace("\r", "\\r")
        .replace("\n", "\\n")
        .replace("\t", "\\t")
        .replace("=", "\\x3d")
    )


def _raise_gh_failure(
    *,
    stage: str,
    command_kind: str,
    args: list[str],
    detail: str,
    stderr_text: str = "",
    code: str = ERROR_GH,
) -> None:
    message = (
        f"[ERROR] open-pr {command_kind} stage={stage} "
        f"command='{_command_preview(args)}' detail={detail}"
    )
    if stderr_text:
        message = f"{message}: {stderr_text}"
    diagnostics = {
        "ERROR": code,
        "DIAGNOSTIC_STAGE": stage,
        "DIAGNOSTIC_KIND": command_kind,
        "FAILED_COMMAND": _command_preview(args),
        "ERROR_DETAIL": detail,
    }
    if stderr_text:
        diagnostics["COMMAND_STDERR"] = stderr_text.strip()
    raise OpenPRError(code=code, stderr_message=message, diagnostics=diagnostics)


def _run_json_command(args: list[str], *, stage: str) -> Any:
    result = _run_command(args, check=False)
    if result.returncode != 0:
        _raise_gh_failure(
            stage=stage,
            command_kind="json-command-failure",
            args=args,
            detail="non-zero-exit",
            stderr_text=result.stderr.strip(),
        )
    text = result.stdout.strip()
    if not text:
        _raise_gh_failure(
            stage=stage,
            command_kind="json-parse-failure",
            args=args,
            detail="empty-json-output",
        )
    try:
        return json.loads(text)
    except json.JSONDecodeError as exc:
        _raise_gh_failure(
            stage=stage,
            command_kind="json-parse-failure",
            args=args,
            detail="invalid-json-output",
            stderr_text=str(exc),
        )


def _git_branch() -> str:
    result = _run_command(["git", "branch", "--show-current"], check=False)
    if result.returncode != 0:
        raise RuntimeError("git branch --show-current failed")
    branch = result.stdout.strip()
    if not branch:
        raise RuntimeError("current branch is empty")
    return branch


def _load_pr_body(args: argparse.Namespace) -> str:
    if args.pr_body_file:
        path = Path(args.pr_body_file)
        if not path.is_file():
            raise OpenPRError(
                code=ERROR_TEMPLATE_GUARD,
                stderr_message=f"[ERROR] open-pr template preflight failed: pr_body_file not found: {path}",
                diagnostics={
                    "ERROR": ERROR_TEMPLATE_GUARD,
                    "DIAGNOSTIC_STAGE": "pr-template-preflight",
                    "DIAGNOSTIC_KIND": "template-preflight-failure",
                    "PREFLIGHT_CHECK": PREFLIGHT_TEMPLATE_GUARD,
                    "ERROR_DETAIL": "missing-pr-body-file",
                },
            )
        return path.read_text(encoding="utf-8")
    if args.pr_body:
        return args.pr_body
    raise OpenPRError(
        code=ERROR_TEMPLATE_GUARD,
        stderr_message="[ERROR] open-pr template preflight failed: pr_body or pr_body_file must be provided",
        diagnostics={
            "ERROR": ERROR_TEMPLATE_GUARD,
            "DIAGNOSTIC_STAGE": "pr-template-preflight",
            "DIAGNOSTIC_KIND": "template-preflight-failure",
            "PREFLIGHT_CHECK": PREFLIGHT_TEMPLATE_GUARD,
            "ERROR_DETAIL": "missing-pr-body",
        },
    )


def _read_template_headers() -> list[str]:
    template_path = CANONICAL_TEMPLATE_PATH
    if not template_path.is_file():
        raise RuntimeError("missing .github/PULL_REQUEST_TEMPLATE.md")
    lines: list[str] = []
    for line in template_path.read_text(encoding="utf-8").splitlines():
        if line.startswith("## "):
            lines.append(line[3:].strip())
    return lines


def _extract_headers(text: str) -> list[str]:
    return [line[3:].strip() for line in text.splitlines() if line.startswith("## ")]


def _find_section_range(lines: list[str], section_header: str) -> tuple[int, int] | None:
    section_start = None
    for idx, line in enumerate(lines):
        if line.startswith(f"## {section_header}"):
            section_start = idx
            break
    if section_start is None:
        return None

    section_end = section_start + 1
    while section_end < len(lines) and not lines[section_end].startswith("## "):
        section_end += 1
    return section_start, section_end


def _template_guard(pr_body: str) -> list[str]:
    template_headers = _read_template_headers()
    body_headers = set(_extract_headers(pr_body))
    return [header for header in template_headers if header not in body_headers]


def _gh_issue_state(bin_name: str, issue_number: str) -> str:
    data = _run_json_command(
        [bin_name, "issue", "view", str(issue_number), "--json", "state"],
        stage="linked-issue-state",
    )
    state = str(data.get("state", "")).strip().upper()
    if not state:
        _raise_gh_failure(
            stage="linked-issue-state",
            command_kind="json-parse-failure",
            args=[bin_name, "issue", "view", str(issue_number), "--json", "state"],
            detail="missing-state-field",
        )
    if state not in {"OPEN", "CLOSED"}:
        raise OpenPRError(
            code=ERROR_LINKED_ISSUE_STATE_UNKNOWN,
            stderr_message=f"[ERROR] open-pr linked issue state is unknown: issue=#{issue_number} state={state}",
            diagnostics={
                "ERROR": ERROR_LINKED_ISSUE_STATE_UNKNOWN,
                "DIAGNOSTIC_STAGE": "linked-issue-state",
                "DIAGNOSTIC_KIND": "json-contract-failure",
                "FAILED_COMMAND": _command_preview([bin_name, "issue", "view", str(issue_number), "--json", "state"]),
                "ERROR_DETAIL": f"unexpected-state-{state.lower()}",
            },
        )
    return state


def _determine_linked_action(args: argparse.Namespace, github_bin: str) -> tuple[str, bool]:
    downgraded = False
    linked_issue = str(args.linked_issue).strip()
    if not linked_issue:
        raise RuntimeError("linked_issue is required")

    parent_issue = str(args.parent_issue).strip()
    is_dependent = _to_boolish(args.is_dependent)

    if parent_issue:
        parent_state = _gh_issue_state(github_bin, parent_issue)
        if parent_state == "CLOSED":
            return "Refs", True
        if is_dependent:
            return "Refs", True

    linked_state = _gh_issue_state(github_bin, linked_issue)
    if linked_state == "OPEN":
        return "Closes", downgraded
    if linked_state == "CLOSED":
        return "Refs", True
    raise RuntimeError(f"{ERROR_LINKED_ISSUE_STATE_UNKNOWN}: {linked_issue}")


def _normalize_linked_issue_section(pr_body: str, linked_issue: str, linked_action: str, change_kind: str) -> str:
    link_line = f"{linked_action} #{linked_issue}"
    kind_line = f"change_kind: {change_kind}"

    lines = pr_body.splitlines()
    section_range = _find_section_range(lines, "Linked Issue")
    if section_range is None:
        prefix = ["## Linked Issue", link_line, kind_line, ""]
        if lines and lines[0].strip() == "":
            lines = lines[1:]
        return "\n".join(prefix + lines).rstrip("\n") + "\n"

    section_start, section_end = section_range
    section_body = lines[section_start + 1 : section_end]
    cleaned_body = [
        line
        for line in section_body
        if not (line.startswith("Closes #") or line.startswith("Refs #") or line.startswith("change_kind:"))
    ]

    rebuilt = ["## Linked Issue", link_line, kind_line]
    if cleaned_body:
        if cleaned_body and cleaned_body[0].strip() == "":
            cleaned_body = cleaned_body[1:]
        while cleaned_body and cleaned_body[0].strip() == "":
            cleaned_body = cleaned_body[1:]
        rebuilt.extend(cleaned_body)

    rebuilt_lines = lines[:section_start] + rebuilt + [""] + lines[section_end:]
    return "\n".join(rebuilt_lines).strip("\n") + "\n"


def _parse_markdown_table_row(line: str) -> list[str] | None:
    stripped = line.strip()
    if not stripped.startswith("|") or not stripped.endswith("|"):
        return None
    return [cell.strip() for cell in stripped.strip("|").split("|")]


def _is_markdown_table_separator(cells: list[str]) -> bool:
    if not cells:
        return False
    for cell in cells:
        if not cell:
            return False
        if set(cell) - {"-", ":"}:
            return False
    return True


def _commands_run_row(command: str, exit_code: str, scope: str, notes: str) -> str:
    return f"| {command} | {exit_code} | {scope} | {notes} |"


def _git_diff_check_row() -> str:
    result = _run_command(["git", "diff", "--check"], check=False)
    note = "format 崩れなし" if result.returncode == 0 else "format 崩れあり"
    return _commands_run_row("`git diff --check`", str(result.returncode), "all lanes", note)


def _normalize_commands_run_section(pr_body: str, change_kind: str) -> str:
    lines = pr_body.splitlines()
    section_range = _find_section_range(lines, COMMANDS_RUN_HEADER)
    if section_range is None:
        return pr_body

    section_start, section_end = section_range
    section_body = lines[section_start + 1 : section_end]

    table_start = None
    for idx, line in enumerate(section_body):
        if _parse_markdown_table_row(line) is not None:
            table_start = idx
            break

    generated_git_diff_row = _git_diff_check_row()
    if table_start is None:
        if change_kind != "spec_only":
            return pr_body
        rebuilt_body = section_body + [
            COMMANDS_RUN_TABLE_HEADER,
            COMMANDS_RUN_TABLE_SEPARATOR,
            generated_git_diff_row,
        ]
        rebuilt_lines = lines[: section_start + 1] + rebuilt_body + lines[section_end:]
        return "\n".join(rebuilt_lines).strip("\n") + "\n"

    table_end = table_start
    while table_end < len(section_body) and _parse_markdown_table_row(section_body[table_end]) is not None:
        table_end += 1

    table_lines = section_body[table_start:table_end]
    if not table_lines:
        return pr_body

    prefix_lines = section_body[:table_start]
    suffix_lines = section_body[table_end:]

    header_line = table_lines[0]
    data_lines = table_lines[1:]
    rebuilt_data_lines: list[str] = []
    replaced_git_diff = False
    had_placeholder_row = False
    had_non_placeholder_row = False

    for line in data_lines:
        cells = _parse_markdown_table_row(line)
        if cells is None:
            rebuilt_data_lines.append(line)
            continue
        if _is_markdown_table_separator(cells):
            rebuilt_data_lines.append(line)
            continue

        row = tuple(cells)
        if row == COMMANDS_RUN_PLACEHOLDER_GIT_DIFF:
            rebuilt_data_lines.append(generated_git_diff_row)
            replaced_git_diff = True
            had_placeholder_row = True
            continue
        if row in {COMMANDS_RUN_PLACEHOLDER_JUST_CHECK, COMMANDS_RUN_PLACEHOLDER_ISSUE_DEFINED}:
            had_placeholder_row = True
            if change_kind == "spec_only":
                continue
            rebuilt_data_lines.append(line)
            continue

        had_non_placeholder_row = True
        rebuilt_data_lines.append(line)

    if change_kind == "spec_only" and had_placeholder_row and not had_non_placeholder_row and not replaced_git_diff:
        rebuilt_data_lines.append(generated_git_diff_row)

    rebuilt_table = [header_line]
    if rebuilt_data_lines and _parse_markdown_table_row(rebuilt_data_lines[0]) is not None:
        first_cells = _parse_markdown_table_row(rebuilt_data_lines[0])
        if first_cells is not None and _is_markdown_table_separator(first_cells):
            rebuilt_table.extend(rebuilt_data_lines)
        else:
            rebuilt_table.append(COMMANDS_RUN_TABLE_SEPARATOR)
            rebuilt_table.extend(rebuilt_data_lines)
    else:
        rebuilt_table.append(COMMANDS_RUN_TABLE_SEPARATOR)
        rebuilt_table.extend(rebuilt_data_lines)

    rebuilt_body = prefix_lines + rebuilt_table + suffix_lines
    rebuilt_lines = lines[: section_start + 1] + rebuilt_body + lines[section_end:]
    return "\n".join(rebuilt_lines).strip("\n") + "\n"


def _pr_list_open_head(head: str, github_bin: str) -> list[dict[str, Any]]:
    return _run_json_command(
        [github_bin, "pr", "list", "--head", head, "--state", "open", "--json", "number,title,url,state,headRefName,updatedAt,isDraft"],
        stage="exact-branch-open-pr-discovery",
    )


def _pr_list_for_issue(linked_issue: str, github_bin: str) -> list[dict[str, Any]]:
    return _run_json_command(
        [
            github_bin,
            "pr", "list", "--state", "open", "--search", f"#{linked_issue} in:body", "--json", "number,title,url,state,headRefName,body,updatedAt,isDraft",
        ],
        stage="same-issue-open-pr-discovery",
    )


def _branch_exists_on_remote(branch: str, github_bin: str) -> bool:
    result = _run_command([github_bin, "api", f"repos/{{owner}}/{{repo}}/branches/{branch}"], check=False)
    return result.returncode == 0


def _pr_mentions_linked_issue_in_body(pr_body: str, linked_issue: str) -> bool:
    pattern = re.compile(r"(?m)^##\s+Linked Issue\s*$")
    lines = pr_body.splitlines()
    start = None
    issue_token_pattern = rf"(?<!\w){re.escape(f'#{linked_issue}')}(?!\w)"
    for i, line in enumerate(lines):
        if pattern.match(line):
            start = i + 1
            break
    if start is None:
        return False

    for line in lines[start:]:
        if line.startswith("## "):
            break
        if re.search(rf"(?:(?:Closes|Refs))\s+{issue_token_pattern}", line):
            return True
    return False


def _pr_matches_linked_issue(pr: dict[str, Any], linked_issue: str) -> bool:
    issue_token = f"#{linked_issue}"
    title = str(pr.get("title", "") or "")
    head_ref = str(pr.get("headRefName", "") or "")
    issue_token_pattern = rf"(?<!\w){re.escape(issue_token)}(?!\w)"
    branch_issue_pattern = rf"(?:^|[/_-])issue[/_-]?{re.escape(linked_issue)}(?:$|[/_-])"
    if re.search(issue_token_pattern, title) or re.search(branch_issue_pattern, head_ref):
        return True
    pr_body = str(pr.get("body", "") or "")
    return _pr_mentions_linked_issue_in_body(pr_body, linked_issue)


def _parse_repair_context(raw: str) -> dict[str, str]:
    if not raw:
        return {}
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"repair_context must be JSON: {exc}") from exc
    if not isinstance(data, dict):
        return {}
    return {
        "reason": str(data.get("reason", "") or ""),
        "previous_pr_url": str(data.get("previous_pr_url", "") or ""),
        "mode": str(data.get("mode", "") or ""),
    }


def _parse_superseded_prs(raw: str) -> list[str]:
    if not raw:
        return []
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"superseded_prs must be JSON: {exc}") from exc
    if not isinstance(data, list):
        return []
    return [str(item).strip() for item in data if str(item).strip()]


def _pr_urls(prs: list[dict[str, Any]]) -> set[str]:
    urls: set[str] = set()
    for pr in prs:
        url = str(pr.get("url", "") or "").strip()
        if url:
            urls.add(url)
    return urls


def _resolve_existing_pr(args: argparse.Namespace, branch_prs: list[dict[str, Any]], issue_prs: list[dict[str, Any]]) -> tuple[dict[str, Any] | None, str, bool]:
    if branch_prs:
        return branch_prs[0], "exact-branch", True

    repair_context = _parse_repair_context(args.repair_context)
    previous_pr_url = repair_context.get("previous_pr_url", "")
    repair_mode = repair_context.get("mode", "")
    issue_urls = _pr_urls(issue_prs)
    issue_matches = [pr for pr in issue_prs if _pr_matches_linked_issue(pr, str(args.linked_issue))]

    if repair_mode == "create-replacement" and previous_pr_url:
        if previous_pr_url in issue_urls:
            other_issue_matches = [
                pr for pr in issue_matches if str(pr.get("url", "")).strip() != previous_pr_url
            ]
            if other_issue_matches:
                raise RuntimeError(ERROR_CANONICAL_AMBIGUOUS)
            return None, "repair-replacement", False
        raise RuntimeError(f"{ERROR_CANONICAL_INVALID}: previous_pr_url not found")

    if args.canonical_pr_url:
        canonical = args.canonical_pr_url.strip()
        if canonical not in issue_urls:
            raise RuntimeError(f"{ERROR_CANONICAL_INVALID}: canonical_pr_url mismatch")
        for pr in issue_matches:
            if str(pr.get("url", "")).strip() == canonical:
                return pr, "caller-specified", True
        raise RuntimeError(f"{ERROR_CANONICAL_INVALID}: canonical_pr_url not found")

    if len(issue_matches) == 1:
        return issue_matches[0], "same-issue-open-pr", True

    if issue_matches:
        raise RuntimeError(ERROR_CANONICAL_AMBIGUOUS)

    if len(issue_prs) >= 1:
        raise RuntimeError(ERROR_CANONICAL_AMBIGUOUS)

    return None, "new-pr", False


def _create_pr(pr_title: str, pr_body: str, github_bin: str) -> str:
    with tempfile.NamedTemporaryFile("w", encoding="utf-8", delete=False) as fp:
        fp.write(pr_body)
        path = fp.name

    try:
        result = _run_command(
            [
                github_bin,
                "pr",
                "create",
                "--draft",
                "--title",
                pr_title,
                "--body-file",
                path,
            ],
            check=False,
        )
        if result.returncode != 0:
            _raise_gh_failure(
                stage="pr-create",
                command_kind="non-json-command-failure",
                args=[github_bin, "pr", "create", "--draft", "--title", pr_title, "--body-file", path],
                detail="gh-pr-create-non-zero-exit",
                stderr_text=result.stderr.strip(),
                code=ERROR_GH_PR_CREATE,
            )

        pr_url = result.stdout.strip().splitlines()[0].strip() if result.stdout else ""
        if not pr_url:
            _raise_gh_failure(
                stage="pr-create",
                command_kind="non-json-command-failure",
                args=[github_bin, "pr", "create", "--draft", "--title", pr_title, "--body-file", path],
                detail="missing-pr-url",
                code=ERROR_GH_PR_CREATE,
            )
        return pr_url
    finally:
        os.unlink(path)


def _run_template_preflight(pr_body: str) -> None:
    missing_sections = _template_guard(pr_body)
    if missing_sections:
        raise OpenPRError(
            code=ERROR_TEMPLATE_GUARD,
            stderr_message="[ERROR] open-pr template preflight failed: required sections missing",
            diagnostics={
                "ERROR": ERROR_TEMPLATE_GUARD,
                "DIAGNOSTIC_STAGE": "pr-template-preflight",
                "DIAGNOSTIC_KIND": "template-preflight-failure",
                "PREFLIGHT_CHECK": PREFLIGHT_TEMPLATE_GUARD,
                "MISSING_SECTIONS": ",".join(missing_sections),
            },
        )


def _run_evidence_template_drift_check() -> None:
    result = _run_command(
        [sys.executable, str(SYNC_EVIDENCE_TEMPLATE_PATH), "--check"],
        check=False,
    )
    if result.returncode != 0:
        diagnostics = {
            "ERROR": ERROR_TEMPLATE_GUARD,
            "DIAGNOSTIC_STAGE": "pr-template-preflight",
            "DIAGNOSTIC_KIND": "template-preflight-failure",
            "PREFLIGHT_CHECK": PREFLIGHT_EVIDENCE_DRIFT_GUARD,
            "ERROR_DETAIL": "template-drift-check-failed",
        }
        stderr_text = result.stderr.strip() or result.stdout.strip()
        if stderr_text:
            diagnostics["COMMAND_STDERR"] = stderr_text
        raise OpenPRError(
            code=ERROR_TEMPLATE_GUARD,
            stderr_message="[ERROR] open-pr template preflight failed: evidence template drift detected",
            diagnostics=diagnostics,
        )


def main(argv: list[str] | None = None) -> int:
    try:
        args = parse_args(argv)

        if str(args.publish).lower() != "yes":
            _emit_stderr("[ERROR] publish: yes が未指定です。PR 作成は実行しません。")
            _emit_stdout("ERROR", ERROR_APPROVAL_MISSING)
            return 1

        pr_body = _load_pr_body(args)
        _run_template_preflight(pr_body)
        _run_evidence_template_drift_check()

        linked_issue = str(args.linked_issue).strip()
        if not linked_issue:
            raise OpenPRError(
                code=ERROR_GH,
                stderr_message="[ERROR] open-pr linked_issue is required",
                diagnostics={
                    "ERROR": ERROR_GH,
                    "DIAGNOSTIC_STAGE": "argument-validation",
                    "DIAGNOSTIC_KIND": "input-validation-failure",
                    "ERROR_DETAIL": "linked_issue-required",
                },
            )

        if re.fullmatch(r"\d+", linked_issue) is None:
            raise OpenPRError(
                code=ERROR_GH,
                stderr_message="[ERROR] open-pr linked_issue must be numeric",
                diagnostics={
                    "ERROR": ERROR_GH,
                    "DIAGNOSTIC_STAGE": "argument-validation",
                    "DIAGNOSTIC_KIND": "input-validation-failure",
                    "ERROR_DETAIL": "linked_issue-not-numeric",
                },
            )

        linked_action, downgraded = _determine_linked_action(args, args.github_bin)
        normalized_body = _normalize_linked_issue_section(pr_body, linked_issue, linked_action, args.change_kind)
        normalized_body = _normalize_commands_run_section(normalized_body, args.change_kind)

        _emit_stdout("LINKED_ISSUE_ACTION", linked_action)
        _emit_stdout("change_kind", args.change_kind)

        if downgraded:
            _emit_stdout("WARN_DOWNGRADE", "Closes->Refs")

        if args.change_kind not in {"spec_only", "code", "mixed"}:
            raise OpenPRError(
                code=ERROR_CHANGE_KIND_INVALID,
                stderr_message="[ERROR] open-pr invalid change_kind",
                diagnostics={
                    "ERROR": ERROR_CHANGE_KIND_INVALID,
                    "DIAGNOSTIC_STAGE": "argument-validation",
                    "DIAGNOSTIC_KIND": "input-validation-failure",
                    "ERROR_DETAIL": "invalid-change-kind",
                },
            )

        superseded_prs = _parse_superseded_prs(args.superseded_prs)
        superseded_prs_out = ",".join(superseded_prs) if superseded_prs else "none"

        branch = _git_branch()
        branch_prs = _pr_list_open_head(branch, args.github_bin)
        issue_prs = _pr_list_for_issue(linked_issue, args.github_bin)
        existing_pr, source, skip_create = _resolve_existing_pr(args, branch_prs, issue_prs)

        if skip_create:
            pr_url = str(existing_pr.get("url", ""))
            _emit_stdout("PR_URL", f"{pr_url} (existing)")
            _emit_stdout("CANONICAL_PR_URL", pr_url)
            _emit_stdout("CANONICAL_PR_SOURCE", source)
            _emit_stdout("EXISTING_PR_BODY_UPDATED", "false")
            _emit_stdout("SUPERSEDED_PRS", superseded_prs_out)
            if args.dry_run:
                _emit_stdout("DRY_RUN", "true")
            return 0

        if args.dry_run:
            _emit_stdout("DRY_RUN", "true")
            return 0

        if not _branch_exists_on_remote(branch, args.github_bin):
            raise OpenPRError(
                code=ERROR_BRANCH_NOT_FOUND,
                stderr_message="[ERROR] open-pr current branch is not published on remote",
                diagnostics={
                    "ERROR": ERROR_BRANCH_NOT_FOUND,
                    "DIAGNOSTIC_STAGE": "branch-publication-check",
                    "DIAGNOSTIC_KIND": "non-json-command-failure",
                    "FAILED_COMMAND": _command_preview([args.github_bin, "api", "repos/{owner}/{repo}/branches/<branch>"]),
                    "ERROR_DETAIL": "remote-branch-not-found",
                },
            )

        pr_url = _create_pr(args.pr_title, normalized_body, args.github_bin)
        _emit_stdout("PR_URL", pr_url)
        _emit_stdout("CANONICAL_PR_URL", pr_url)
        _emit_stdout("CANONICAL_PR_SOURCE", source)
        _emit_stdout("SUPERSEDED_PRS", superseded_prs_out)

        repair_context = _parse_repair_context(args.repair_context)
        previous_pr_url = repair_context.get("previous_pr_url", "")
        if repair_context.get("mode") == "create-replacement" and previous_pr_url:
            _emit_stdout("SUPERSEDED_PR_URL", previous_pr_url)

        return 0
    except OpenPRError as exc:
        _emit_stderr(exc.stderr_message)
        _emit_diagnostics(exc.diagnostics)
        return 1
    except RuntimeError as exc:
        message = str(exc)
        if message.startswith(ERROR_CANONICAL_AMBIGUOUS):
            error = OpenPRError(
                code=ERROR_CANONICAL_AMBIGUOUS,
                stderr_message="[ERROR] open-pr canonical PR is ambiguous",
                diagnostics={
                    "ERROR": ERROR_CANONICAL_AMBIGUOUS,
                    "DIAGNOSTIC_STAGE": "canonical-pr-selection",
                    "DIAGNOSTIC_KIND": "canonical-pr-resolution-failure",
                    "ERROR_DETAIL": "ambiguous-canonical-pr",
                },
            )
        elif message.startswith(ERROR_CANONICAL_INVALID):
            detail = "canonical-pr-invalid"
            if ":" in message:
                detail = message.split(":", 1)[1].strip().replace(" ", "-")
            error = OpenPRError(
                code=ERROR_CANONICAL_INVALID,
                stderr_message=f"[ERROR] open-pr canonical PR is invalid: {message}",
                diagnostics={
                    "ERROR": ERROR_CANONICAL_INVALID,
                    "DIAGNOSTIC_STAGE": "canonical-pr-selection",
                    "DIAGNOSTIC_KIND": "canonical-pr-resolution-failure",
                    "ERROR_DETAIL": detail,
                },
            )
        elif message.startswith("missing .github/PULL_REQUEST_TEMPLATE.md"):
            error = OpenPRError(
                code=ERROR_TEMPLATE_GUARD,
                stderr_message="[ERROR] open-pr template preflight failed: missing .github/PULL_REQUEST_TEMPLATE.md",
                diagnostics={
                    "ERROR": ERROR_TEMPLATE_GUARD,
                    "DIAGNOSTIC_STAGE": "pr-template-preflight",
                    "DIAGNOSTIC_KIND": "template-preflight-failure",
                    "PREFLIGHT_CHECK": PREFLIGHT_TEMPLATE_GUARD,
                    "ERROR_DETAIL": "missing-pr-template",
                },
            )
        else:
            error = OpenPRError(
                code=ERROR_GH,
                stderr_message=f"[ERROR] open-pr unexpected runtime failure: {message}",
                diagnostics={
                    "ERROR": ERROR_GH,
                    "DIAGNOSTIC_STAGE": "runtime",
                    "DIAGNOSTIC_KIND": "unexpected-runtime-failure",
                    "ERROR_DETAIL": "unexpected-runtime-error",
                },
            )

        _emit_stderr(error.stderr_message)
        _emit_diagnostics(error.diagnostics)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
