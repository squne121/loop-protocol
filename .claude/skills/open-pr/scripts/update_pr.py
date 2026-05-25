#!/usr/bin/env python3
"""update_pr.py — PR body update wrapper with validator pre-write hook.

LOOP_PROTOCOL の PR body update を決定論的に行う（validate_pr_body.py fail-closed enforcement）:
- PR body file 読み込み
- changed paths の決定論的解決（必要な場合）
- validator pre-write hook 実行 (fail-closed)
- gh pr edit --body-file または REST PATCH で body 更新
- KEY=VALUE stdout contract
"""
from __future__ import annotations

import argparse
import hashlib
import json
import subprocess
import sys
import tempfile
from pathlib import Path
import re

E_VALIDATION_FAILED = "E_VALIDATION_FAILED"
E_UPDATE_FAILURE = "E_UPDATE_FAILURE"
E_FILE_NOT_FOUND = "E_FILE_NOT_FOUND"


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Update PR body with validator pre-write hook")
    p.add_argument("--pr-number", required=True, type=int, help="PR number to update")
    p.add_argument("--body-file", required=True, type=Path, help="PR body file to write")
    p.add_argument("--repo", help="owner/repo (省略時は git remote から取得)")
    p.add_argument("--linked-issue", type=int, help="linked issue number (validator に渡す)")
    p.add_argument(
        "--changed-paths-file",
        type=Path,
        help="changed paths file (1 path per line, for validator)",
    )
    p.add_argument(
        "--changed-paths",
        nargs="*",
        default=None,
        help="変更ファイルパスのリスト。未指定時は git diff から決定論的に解決する。",
    )
    return p.parse_args(argv)


def emit_kv(key: str, value: object) -> None:
    s = str(value).replace("\n", "\\n").replace("\r", "\\r")
    print(f"{key}={s}")


def emit_error(code: str, detail: str = "") -> None:
    emit_kv("ERROR", code)
    if detail:
        emit_kv("ERROR_DETAIL", detail)


def run_gh(*args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    cmd = ["gh", *args]
    return subprocess.run(cmd, capture_output=True, text=True, check=check, timeout=60)


def resolve_repo() -> str:
    try:
        result = subprocess.run(
            ["git", "remote", "get-url", "origin"],
            capture_output=True,
            text=True,
            check=True,
            timeout=10,
        )
    except subprocess.SubprocessError:
        return ""
    url = result.stdout.strip()
    match = re.search(r"github\.com[:/]([\w.-]+/[\w.-]+?)(?:\.git)?$", url)
    return match.group(1) if match else ""


def resolve_changed_paths(provided_paths: list[str] | None = None) -> list[str] | None:
    if provided_paths is not None:
        return [path for path in provided_paths if path]

    try:
        merge_base = subprocess.run(
            ["git", "merge-base", "main", "HEAD"],
            capture_output=True,
            text=True,
            check=True,
            timeout=10,
        ).stdout.strip()
        if not merge_base:
            return None
        diff = subprocess.run(
            ["git", "diff", "--name-only", f"{merge_base}...HEAD"],
            capture_output=True,
            text=True,
            check=True,
            timeout=20,
        )
    except subprocess.SubprocessError:
        return None

    return [line.strip() for line in diff.stdout.splitlines() if line.strip()]


def _run_pr_body_validator(
    body_text: str,
    changed_paths: list[str] | None,
    linked_issue: int | None,
) -> dict[str, object]:
    """Run validate_pr_body.py validator and return result dict.

    Returns dict with keys: status, schema, target, body_sha256, errors, message (if internal error).
    """
    validator_script = (
        Path(__file__).resolve().parent / "validate_pr_body.py"
    )

    body_file = tempfile.NamedTemporaryFile(
        mode="w",
        suffix=".md",
        encoding="utf-8",
        delete=False,
    )
    changed_paths_file = None
    try:
        body_file.write(body_text)
        body_file.flush()
        body_file.close()

        cmd = [
            sys.executable,
            str(validator_script),
            "--body-file",
            body_file.name,
        ]

        if linked_issue is not None:
            cmd.extend(["--linked-issue", str(linked_issue)])

        if changed_paths is not None:
            changed_paths_file = tempfile.NamedTemporaryFile(
                mode="w",
                suffix=".txt",
                encoding="utf-8",
                delete=False,
            )
            changed_paths_file.write("\n".join(changed_paths))
            changed_paths_file.flush()
            changed_paths_file.close()
            cmd.extend(["--changed-paths-file", changed_paths_file.name])

        try:
            cp = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                check=False,
                timeout=60,
            )
        except subprocess.TimeoutExpired as exc:
            return {
                "status": "internal",
                "errors": [],
                "message": "Validator timeout",
                "stderr": (exc.stderr or "").strip() if exc.stderr else "Timeout expired",
            }
        except OSError as exc:
            return {
                "status": "internal",
                "errors": [],
                "message": "Validator spawn error",
                "stderr": str(exc),
            }

        # AC3: Handle validator exit code 1 (validation failure)
        # AC4: Handle validator exit code 2 or higher (internal error) — both are fail-closed
        if cp.returncode == 1:
            # Fail exit code: expect JSON with status="fail"
            pass
        elif cp.returncode == 0:
            # Pass exit code: expect JSON with status="pass"
            pass
        else:
            # AC4: returncode not in {0, 1} is treated as internal error
            return {
                "status": "internal",
                "errors": [],
                "message": f"Validator error (exit code {cp.returncode})",
                "stderr": (cp.stderr or "").strip(),
            }

        try:
            payload = json.loads(cp.stdout)
        except json.JSONDecodeError:
            return {
                "status": "internal",
                "errors": [],
                "message": "Validator returned non-JSON output",
                "stderr": (cp.stdout or "").strip(),
            }

        # AC5: Verify JSON schema integrity (same pattern as open_pr.py)
        if payload.get("schema") != "loop_body_lint/v1":
            return {
                "status": "internal",
                "errors": [],
                "message": f"Validator schema mismatch: {payload.get('schema')}",
                "stderr": "",
            }
        if payload.get("target") != "pr":
            return {
                "status": "internal",
                "errors": [],
                "message": f"Validator target mismatch: {payload.get('target')}",
                "stderr": "",
            }
        if payload.get("status") not in {"pass", "fail"}:
            return {
                "status": "internal",
                "errors": [],
                "message": f"Validator status invalid: {payload.get('status')}",
                "stderr": "",
            }
        if not isinstance(payload.get("errors"), list):
            return {
                "status": "internal",
                "errors": [],
                "message": "Validator errors field is not a list",
                "stderr": "",
            }

        # AC5: Verify body_sha256
        expected_sha256 = f"sha256:{hashlib.sha256(body_text.encode('utf-8')).hexdigest()}"
        if payload.get("body_sha256") != expected_sha256:
            return {
                "status": "internal",
                "errors": [],
                "message": "Validator body_sha256 mismatch",
                "stderr": f"expected {expected_sha256}, got {payload.get('body_sha256')}",
            }

        return payload
    finally:
        Path(body_file.name).unlink(missing_ok=True)
        if changed_paths_file is not None:
            Path(changed_paths_file.name).unlink(missing_ok=True)


def update_pr(repo: str, pr_number: int, body_file: Path) -> bool:
    """Update PR body using gh pr edit --body-file."""
    args = [
        "pr",
        "edit",
        str(pr_number),
        "--repo",
        repo,
        "--body-file",
        str(body_file),
    ]
    result = run_gh(*args, check=False)
    return result.returncode == 0


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)

    # AC1: Validate pr-number and body-file
    if not args.body_file.exists():
        emit_error(E_FILE_NOT_FOUND, f"body-file が存在しません: {args.body_file}")
        return 2

    body_text = args.body_file.read_text(encoding="utf-8")

    repo = args.repo or resolve_repo()
    if not repo:
        emit_error(E_UPDATE_FAILURE, "git remote から owner/repo を取得できませんでした")
        return 2

    # AC2: Run validator pre-write hook
    # Resolve changed paths if not provided as file
    changed_paths = None
    if args.changed_paths_file and args.changed_paths_file.exists():
        changed_paths = [
            line.strip()
            for line in args.changed_paths_file.read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]
    elif args.changed_paths is not None:
        changed_paths = resolve_changed_paths(args.changed_paths)

    validator_result = _run_pr_body_validator(body_text, changed_paths, args.linked_issue)

    # AC3/AC4: Fail-closed enforcement for both exit 1 (fail) and exit 2 (internal error)
    if validator_result.get("status") != "pass":
        errors = validator_result.get("errors", [])
        rule_ids = ",".join(error.get("rule_id", "") for error in errors if isinstance(error, dict))
        detail = validator_result.get("message", "PR body validation failed")
        if rule_ids:
            detail = f"{detail}; rule_ids={rule_ids}"
            emit_kv("VALIDATOR_RULE_IDS", rule_ids)
        emit_error(E_VALIDATION_FAILED, str(detail))
        return 1

    # AC8: If validator passes (exit 0), proceed with update
    if not update_pr(repo, args.pr_number, args.body_file):
        emit_error(E_UPDATE_FAILURE, f"gh pr edit 失敗: PR #{args.pr_number}")
        return 2

    emit_kv("PR_NUMBER", args.pr_number)
    emit_kv("REPO", repo)
    emit_kv("UPDATED", "true")
    return 0


if __name__ == "__main__":
    sys.exit(main())
