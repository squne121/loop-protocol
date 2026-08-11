#!/usr/bin/env python3
"""open_pr.py — open-pr skill の Python wrapper.

LOOP_PROTOCOL の PR 起票を決定論的に行う。skill (SKILL.md) の手順を実装する:
- publish ゲート (人間承認)
- Linked Issue 状態確認 + Closes / Refs 自動 downgrade
- changed paths の決定論的解決
- final PR body の validator 実行 (fail-closed)
- Idempotency チェック (既存 PR 検出)
- gh pr create 実行
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

E_APPROVAL_MISSING = "E_APPROVAL_MISSING"
E_PR_BODY_VALIDATION_FAILED = "E_PR_BODY_VALIDATION_FAILED"
E_LINKED_ISSUE_STATE_UNKNOWN = "E_LINKED_ISSUE_STATE_UNKNOWN"
E_GH_FAILURE = "E_GH_FAILURE"
E_SCHEMA_CONSUMER_INVENTORY_MISSING = "E_SCHEMA_CONSUMER_INVENTORY_MISSING"
E_PR_BODY_JAPANESE_VALIDATION_FAILED = "E_PR_BODY_JAPANESE_VALIDATION_FAILED"

# fail-closed exit code for hard failures (publish approval missing, pr body
# file missing, gh/repo/branch resolution failure, validator failure,
# canonical repository resolution failure, gh pr create failure, etc).
EXIT_BLOCKED = 2

# #1679: canonical repository resolution failure（PR mutation target の
# binding failure）は target-only executor の一部として、peer OPEN Issue
# 走査とは独立した fail-closed safety boundary として維持する（Issue #1470
# 由来）。
E_CANONICAL_REPOSITORY_RESOLUTION_FAILED = "E_CANONICAL_REPOSITORY_RESOLUTION_FAILED"


def _classify_validator_errors(errors: list[object]) -> str:
    """Classify validator errors list into an error code.

    Returns E_SCHEMA_CONSUMER_INVENTORY_MISSING if any error is LP050, or
    if any LP052 error references the Schema Consumer Inventory section.
    Returns E_PR_BODY_VALIDATION_FAILED for all other failures.
    """
    for error in errors:
        if not isinstance(error, dict):
            continue
        rule_id = error.get("rule_id", "")
        if rule_id == "LP050":
            return E_SCHEMA_CONSUMER_INVENTORY_MISSING
        if rule_id == "LP052":
            message = error.get("message", "")
            if message.strip() == "Missing required section: Schema Consumer Inventory":
                return E_SCHEMA_CONSUMER_INVENTORY_MISSING
    return E_PR_BODY_VALIDATION_FAILED


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Open PR (LOOP_PROTOCOL open-pr skill wrapper)")
    p.add_argument("--pr-title", required=True)
    p.add_argument("--linked-issue", required=True, type=int)
    p.add_argument("--publish", required=True, help="`yes` で人間承認確認")
    p.add_argument("--pr-body-file", required=True, type=Path)
    p.add_argument("--draft", default="true", help="`true` (default) で Draft PR")
    p.add_argument("--branch", help="head branch 名 (省略時は現在の HEAD)")
    p.add_argument("--repo", help="owner/repo (省略時は git remote から取得)")
    p.add_argument("--dry-run", action="store_true", help="gh pr create を実行しない")
    p.add_argument(
        "--link-kind",
        choices=("auto", "Refs"),
        default="auto",
        help="Issue link kind。auto（既定）は Issue state に従い、Refs は caller の明示指定を保持する。",
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


def resolve_branch() -> str:
    try:
        result = subprocess.run(
            ["git", "rev-parse", "--abbrev-ref", "HEAD"],
            capture_output=True,
            text=True,
            check=True,
            timeout=10,
        )
    except subprocess.SubprocessError:
        return ""
    return result.stdout.strip()


def _canonicalize_repo_static(repo: object) -> str | None:
    """`owner/name` を小文字化した canonical 形へ静的に正規化する（Issue #1470）。

    canonical repository resolution（PR mutation target binding）が独自に
    持つ、小さな pure な正規化 function。fail-closed で `None` を返す
    （呼び出し側が分岐しやすいように）。owner/name 形式でない、または
    いずれかの segment が空の場合に `None` を返す。
    """
    if not isinstance(repo, str):
        return None
    raw = repo.strip()
    if "/" not in raw:
        return None
    owner, _, name = raw.partition("/")
    owner = owner.strip()
    name = name.strip()
    if not owner or not name or "/" in name:
        return None
    return f"{owner.lower()}/{name.lower()}"


def resolve_canonical_repository(requested_repo: str) -> str | None:
    """`requested_repo` を GitHub Repository API の canonical `full_name` の
    小文字化形へ一度だけ解決する（Issue #1470）。

    rename / transfer 後の alias もこの単一の API 呼び出しで現在の
    `full_name` へ解決される。producer 側 `_canonicalize_repo(..., online=True)`
    と異なり、consumer 側はオンライン解決に失敗した場合に静的正規化への
    fallback を **行わない**（fresh evidence / gh pr create --repo に使う
    PR mutation target の identity を、GitHub の現在の応答一本に束縛する
    ため）。失敗時は `None` を返し、呼び出し元は停止する。
    """
    static = _canonicalize_repo_static(requested_repo)
    if static is None:
        return None
    try:
        result = run_gh(
            "api",
            f"repos/{static}",
            "-H",
            "Accept: application/vnd.github+json",
            "-H",
            "X-GitHub-Api-Version: 2022-11-28",
        )
    except (subprocess.SubprocessError, OSError):
        return None
    try:
        data = json.loads(result.stdout)
    except json.JSONDecodeError:
        return None
    if not isinstance(data, dict):
        return None
    return _canonicalize_repo_static(data.get("full_name"))


def get_linked_issue_state(repo: str, issue_number: int) -> str | None:
    try:
        result = run_gh(
            "issue", "view", str(issue_number), "--repo", repo, "--json", "state"
        )
        data = json.loads(result.stdout)
    except (subprocess.SubprocessError, json.JSONDecodeError):
        return None
    if not isinstance(data, dict):
        return None
    return data.get("state")


def find_existing_pr(repo: str, branch: str) -> dict | None:
    try:
        result = run_gh(
            "pr",
            "list",
            "--repo",
            repo,
            "--head",
            branch,
            "--state",
            "open",
            "--json",
            "number,url",
        )
        items = json.loads(result.stdout)
    except (subprocess.SubprocessError, json.JSONDecodeError):
        return None
    return items[0] if items else None


def apply_linked_issue_reference(body: str, issue_number: int, link_kind: str) -> str:
    pattern = re.compile(rf"(Closes|Refs|Fixes|Resolves)\s+#{issue_number}\b", re.IGNORECASE)
    if pattern.search(body):
        return pattern.sub(f"{link_kind} #{issue_number}", body, count=1)
    sep = "\n\n" if not body.endswith("\n") else "\n"
    return body + sep + f"{link_kind} #{issue_number}\n"


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
    linked_issue: int,
) -> dict[str, object]:
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
            "--linked-issue",
            str(linked_issue),
        ]

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

        if cp.returncode not in {0, 1}:
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

        # B3: Verify JSON schema integrity
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

        # B3: Verify body_sha256
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



def _run_japanese_content_validator(
    body_text: str,
    threshold: float = 0.1,
) -> dict[str, object]:
    """Run validate_japanese_content.py against body_text.

    Returns dict with keys:
      - status: "pass" | "fail" | "internal"
      - failed_blocks: int
      - aggregate_ratio: float
      - threshold: float
      - body_sha256: str
      - stderr: str (on fail/internal)
    """
    validator_script = (
        Path(__file__).resolve().parent.parent.parent
        / "create-issue" / "scripts" / "validate_japanese_content.py"
    )

    body_sha256 = f"sha256:{hashlib.sha256(body_text.encode('utf-8')).hexdigest()}"

    body_file = tempfile.NamedTemporaryFile(
        mode="w",
        suffix=".md",
        encoding="utf-8",
        delete=False,
    )
    try:
        body_file.write(body_text)
        body_file.flush()
        body_file.close()

        cmd = [
            sys.executable,
            str(validator_script),
            "--file",
            body_file.name,
            "--threshold",
            str(threshold),
            "--verbose",
        ]

        try:
            cp = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                check=False,
                timeout=60,
            )
        except subprocess.TimeoutExpired:
            return {
                "status": "internal",
                "failed_blocks": 0,
                "aggregate_ratio": 0.0,
                "threshold": threshold,
                "body_sha256": body_sha256,
                "stderr": "Timeout expired",
            }
        except OSError as exc:
            return {
                "status": "internal",
                "failed_blocks": 0,
                "aggregate_ratio": 0.0,
                "threshold": threshold,
                "body_sha256": body_sha256,
                "stderr": str(exc),
            }

        stderr_text = (cp.stderr or "").strip()

        if cp.returncode == 0:
            # Parse aggregate_ratio from stderr (verbose mode)
            ratio = 0.0
            for line in stderr_text.splitlines():
                if line.startswith("aggregate_ratio:"):
                    try:
                        ratio = float(line.split(":", 1)[1].strip())
                    except (ValueError, IndexError):
                        pass
            return {
                "status": "pass",
                "failed_blocks": 0,
                "aggregate_ratio": ratio,
                "threshold": threshold,
                "body_sha256": body_sha256,
                "stderr": stderr_text,
            }
        elif cp.returncode == 1:
            # Parse aggregate_ratio and failed_blocks from stderr (verbose mode)
            ratio = 0.0
            failed_blocks = 0
            for line in stderr_text.splitlines():
                if line.startswith("aggregate_ratio:"):
                    try:
                        ratio = float(line.split(":", 1)[1].strip())
                    except (ValueError, IndexError):
                        pass
                elif line.startswith("failed_blocks:"):
                    try:
                        failed_blocks = int(line.split(":", 1)[1].strip())
                    except (ValueError, IndexError):
                        pass
            return {
                "status": "fail",
                "failed_blocks": failed_blocks,
                "aggregate_ratio": ratio,
                "threshold": threshold,
                "body_sha256": body_sha256,
                "stderr": stderr_text,
            }
        else:
            return {
                "status": "internal",
                "failed_blocks": 0,
                "aggregate_ratio": 0.0,
                "threshold": threshold,
                "body_sha256": body_sha256,
                "stderr": stderr_text,
            }
    finally:
        Path(body_file.name).unlink(missing_ok=True)

def create_pr(repo: str, title: str, body_file: Path, branch: str, draft: bool) -> str:
    args = [
        "pr",
        "create",
        "--repo",
        repo,
        "--title",
        title,
        "--body-file",
        str(body_file),
        "--head",
        branch,
        "--base",
        "main",
    ]
    if draft:
        args.append("--draft")
    result = run_gh(*args)
    return result.stdout.strip().splitlines()[-1] if result.stdout.strip() else ""


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)

    if args.publish.strip().lower() != "yes":
        emit_error(E_APPROVAL_MISSING, "publish: yes が指定されていません")
        return EXIT_BLOCKED

    if not args.pr_body_file.exists():
        emit_error(E_PR_BODY_VALIDATION_FAILED, f"pr-body-file が存在しません: {args.pr_body_file}")
        return EXIT_BLOCKED

    original_body = args.pr_body_file.read_text(encoding="utf-8")

    repo = args.repo or resolve_repo()
    if not repo:
        emit_error(E_GH_FAILURE, "git remote から owner/repo を取得できませんでした")
        return EXIT_BLOCKED
    branch = args.branch or resolve_branch()
    if not branch:
        emit_error(E_GH_FAILURE, "現在のブランチ名を取得できませんでした")
        return EXIT_BLOCKED

    state = get_linked_issue_state(repo, args.linked_issue)
    if state is None:
        emit_error(
            E_LINKED_ISSUE_STATE_UNKNOWN,
            f"linked issue #{args.linked_issue} の state を取得できませんでした",
        )
        return EXIT_BLOCKED

    # `Refs` is an explicit caller override for OPEN issues.  Keep resolving
    # the linked Issue state first so the existing state/readback hard gate
    # remains active for every invocation.
    link_kind = "Refs" if args.link_kind == "Refs" else ("Closes" if state == "OPEN" else "Refs")
    final_body = apply_linked_issue_reference(original_body, args.linked_issue, link_kind)

    changed_paths = resolve_changed_paths(args.changed_paths)
    validator_result = _run_pr_body_validator(final_body, changed_paths, args.linked_issue)
    if validator_result.get("status") != "pass":
        errors = validator_result.get("errors", [])
        rule_ids = ",".join(error.get("rule_id", "") for error in errors if isinstance(error, dict))
        detail = validator_result.get("message", "PR body validation failed")
        if rule_ids:
            detail = f"{detail}; rule_ids={rule_ids}"
            emit_kv("VALIDATOR_RULE_IDS", rule_ids)
        error_code = _classify_validator_errors(errors)
        emit_error(error_code, str(detail))
        return EXIT_BLOCKED

    japanese_result = _run_japanese_content_validator(final_body)
    if japanese_result.get("status") != "pass":
        _jap_status = japanese_result.get("status")
        preflight = {
            "schema": "PR_BODY_PREFLIGHT_RESULT_V1",
            "status": _jap_status if _jap_status in {"fail", "internal"} else "internal",
            "body_sha256": japanese_result.get("body_sha256", ""),
            "failed_blocks": japanese_result.get("failed_blocks", 0),
            "aggregate_ratio": japanese_result.get("aggregate_ratio", 0.0),
            "threshold": japanese_result.get("threshold", 0.1),
        }
        emit_kv("PR_BODY_PREFLIGHT_RESULT_V1", json.dumps(preflight, ensure_ascii=False))
        emit_error(E_PR_BODY_JAPANESE_VALIDATION_FAILED, japanese_result.get("stderr", ""))
        return EXIT_BLOCKED

    existing = find_existing_pr(repo, branch)
    if existing:
        emit_kv("EXISTING", "true")
        emit_kv("PR_URL", existing["url"])
        emit_kv("PR_NUMBER", existing["number"])
        emit_kv("LINKED_ISSUE", args.linked_issue)
        emit_kv("LINK_KIND", link_kind)
        return 0

    draft = str(args.draft).strip().lower() == "true"

    final_body_file = tempfile.NamedTemporaryFile(
        mode="w",
        suffix=".md",
        encoding="utf-8",
        delete=False,
    )
    try:
        final_body_file.write(final_body)
        final_body_file.flush()
        final_body_file.close()
        final_body_path = Path(final_body_file.name)

        if args.dry_run:
            emit_kv("DRY_RUN", "true")
            emit_kv("PR_TITLE_PREVIEW", args.pr_title)
            emit_kv("PR_BODY_PREVIEW_FIRST_LINES", "\\n".join(final_body.splitlines()[:5]))
            emit_kv("LINKED_ISSUE", args.linked_issue)
            emit_kv("LINK_KIND", link_kind)
            emit_kv("DRAFT", str(draft).lower())
            return 0

        # #1679: canonical repository resolution / PR mutation target
        # binding (Issue #1470) is an independent fail-closed safety
        # boundary that is kept after peer OPEN Issue overlap preflight is
        # removed (In Scope item 3). PR mutation target を GitHub
        # Repository API の canonical full_name (小文字化形) として一度だけ
        # 解決し、`gh pr create --repo` に同じ値を使う。失敗した場合は
        # fallback せず fail-closed で停止する。
        target_repo = resolve_canonical_repository(repo)
        if target_repo is None:
            emit_error(
                E_CANONICAL_REPOSITORY_RESOLUTION_FAILED,
                f"canonical repository を解決できませんでした: {repo}",
            )
            return EXIT_BLOCKED

        pr_create_repo = target_repo
        if target_repo != repo:
            # mixed-case / rename alias の場合、既存 PR を canonical target
            # で再確認する（idempotency チェックの canonical target 追従）。
            canonical_existing = find_existing_pr(target_repo, branch)
            if canonical_existing:
                emit_kv("EXISTING", "true")
                emit_kv("PR_URL", canonical_existing["url"])
                emit_kv("PR_NUMBER", canonical_existing["number"])
                emit_kv("LINKED_ISSUE", args.linked_issue)
                emit_kv("LINK_KIND", link_kind)
                return 0

        try:
            pr_url = create_pr(pr_create_repo, args.pr_title, final_body_path, branch, draft)
        except subprocess.CalledProcessError as exc:
            emit_error(E_GH_FAILURE, f"gh pr create 失敗: exit {exc.returncode}")
            if exc.stderr:
                emit_kv("COMMAND_STDERR", exc.stderr.strip()[:500])
            return EXIT_BLOCKED

        if not pr_url:
            emit_error(E_GH_FAILURE, "gh pr create が URL を返しませんでした")
            return EXIT_BLOCKED

        match = re.search(r"/pull/(\d+)", pr_url)
        pr_number = match.group(1) if match else ""

        emit_kv("PR_URL", pr_url)
        emit_kv("PR_NUMBER", pr_number)
        emit_kv("LINKED_ISSUE", args.linked_issue)
        emit_kv("LINK_KIND", link_kind)
        emit_kv("EXISTING", "false")
        emit_kv("DRY_RUN", "false")

        return 0
    finally:
        Path(final_body_file.name).unlink(missing_ok=True)


if __name__ == "__main__":
    sys.exit(main())
