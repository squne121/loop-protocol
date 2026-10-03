#!/usr/bin/env python3
"""open_pr.py — open-pr skill の Python wrapper.

LOOP_PROTOCOL の PR 起票を決定論的に行う。skill (SKILL.md) の手順を実装する:
- publish ゲート (人間承認)
- Linked Issue 状態確認 + reference authority evaluator（validate_pr_body.py）の結果に従う Closes / Refs 選択
- changed paths の決定論的解決
- final PR body の validator 実行 (fail-closed)
- Idempotency チェック (既存 PR 検出)
- gh pr create 実行
- KEY=VALUE stdout contract
"""

from __future__ import annotations

import argparse
import hashlib
import inspect
import json
import os
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
E_IMPLEMENTATION_SCOPE_COVERAGE_UNAVAILABLE = "E_IMPLEMENTATION_SCOPE_COVERAGE_UNAVAILABLE"

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
        result = run_gh("issue", "view", str(issue_number), "--repo", repo, "--json", "state")
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


REFERENCE_VALIDATOR_SCRIPT = Path(__file__).resolve().parent / "validate_pr_body.py"
NON_CLOSING_AUTHORITY_KEYS = (
    "decision",
    "level",
    "reason_code",
    "repo",
    "issue_number",
    "pr_number",
    "pr_body_sha256",
)
_REFERENCE_RESULT_KEYS = frozenset(
    {
        "decision",
        "level",
        "reason_code",
        "repo",
        "issue_number",
        "pr_number",
        "pr_body_sha256",
        "effective_kind",
        "body_verdict",
        "body_reason",
    }
)
_REFERENCE_DECISION_LINE_PREFIX = "Reference-Decision:"
_REFERENCE_DECISION_URL = re.compile(
    r"https://github\.com/([A-Za-z0-9][A-Za-z0-9._-]*)/([A-Za-z0-9._-]+)/issues/[0-9]+#issuecomment-([0-9]+)"
)


def append_linked_issue_reference(body: str, issue_number: int, link_kind: str) -> str:
    """Unconditionally append `<link_kind> #<issue_number>` (the evaluator decided it is needed)."""
    sep = "\n\n" if not body.endswith("\n") else "\n"
    return body + sep + f"{link_kind} #{issue_number}\n"


def _fail_closed_reference_result(
    body_text: str, repo: str, linked_issue: int, pr_number: int | None, reason_code: str = "facts_invalid"
) -> dict[str, object]:
    return {
        "decision": "fail_closed",
        "level": None,
        "reason_code": reason_code,
        "repo": repo,
        "issue_number": linked_issue,
        "pr_number": pr_number,
        "pr_body_sha256": hashlib.sha256(body_text.encode("utf-8")).hexdigest(),
        "effective_kind": "none",
        "body_verdict": "block",
        "body_reason": "not_evaluated",
    }


def fetch_reference_decision_comment(pr_body: str) -> dict[str, object] | None:
    """Fresh-fetch the A1 decision comment named by the PR body's single `Reference-Decision:` line.

    This only acquires a fact. Whether the comment is a valid A1 decision is judged solely by the
    evaluator in `validate_pr_body.py`; a missing / unfetchable comment is `None` (A1 invalid).
    """
    values = [
        line[len(_REFERENCE_DECISION_LINE_PREFIX):].strip()
        for line in re.split(r"\r\n|\n|\r", pr_body)
        if line.startswith(_REFERENCE_DECISION_LINE_PREFIX)
    ]
    if len(values) != 1:
        return None
    match = _REFERENCE_DECISION_URL.fullmatch(values[0])
    if match is None:
        return None
    try:
        result = run_gh("api", f"repos/{match.group(1)}/{match.group(2)}/issues/comments/{match.group(3)}")
        data = json.loads(result.stdout)
    except (subprocess.SubprocessError, OSError, json.JSONDecodeError):
        return None
    if not isinstance(data, dict):
        return None
    comment = {
        "url": data.get("html_url"),
        "id": data.get("id"),
        "issue_url": data.get("issue_url"),
        "author_association": data.get("author_association"),
        "body": data.get("body"),
    }
    if type(comment["id"]) is not int or not all(
        isinstance(comment[key], str) for key in ("url", "issue_url", "author_association", "body")
    ):
        return None
    return comment


def build_reference_facts(repo: str, issue_state: str, pr_body: str, pr_number: int | None) -> dict[str, object]:
    """Facts JSON for the reference policy evaluator (exact keys; `updated_at` is never a fact)."""
    return {
        "repo": repo,
        "issue_state": issue_state,
        "pr_number": pr_number,
        "decision_comment": fetch_reference_decision_comment(pr_body),
    }


def run_reference_policy_entrypoint(
    pr_body: str,
    linked_issue: int,
    linked_issue_body: str | None,
    facts: dict[str, object],
) -> dict[str, object]:
    """Run `validate_pr_body.py --evaluate-reference-policy` and return its (validated) JSON object.

    The evaluator is the only authority: this wrapper never re-implements the grammar. Any
    transport / shape problem is normalized to `fail_closed` / `facts_invalid`.
    """
    repo = str(facts.get("repo", ""))
    raw_pr_number = facts.get("pr_number")
    pr_number = raw_pr_number if type(raw_pr_number) is int else None
    unavailable = _fail_closed_reference_result(pr_body, repo, linked_issue, pr_number)
    if linked_issue_body is None:
        return unavailable
    body_bytes = pr_body.encode("utf-8")
    paths: list[str] = []
    try:
        for suffix, payload in (
            (".md", body_bytes),
            (".json", json.dumps(facts).encode("utf-8")),
            (".md", linked_issue_body.encode("utf-8")),
        ):
            handle = tempfile.NamedTemporaryFile(mode="wb", suffix=suffix, delete=False)
            paths.append(handle.name)
            handle.write(payload)
            handle.close()
        proc = subprocess.run(
            [
                sys.executable,
                str(REFERENCE_VALIDATOR_SCRIPT),
                "--evaluate-reference-policy",
                "--body-file",
                paths[0],
                "--linked-issue",
                str(linked_issue),
                "--linked-issue-body-file",
                paths[2],
                "--reference-facts-file",
                paths[1],
            ],
            capture_output=True,
            text=True,
            check=False,
            timeout=60,
        )
        if proc.returncode != 0:
            return unavailable
        result = json.loads(proc.stdout)
    except (subprocess.SubprocessError, OSError, ValueError):
        return unavailable
    finally:
        for path in paths:
            Path(path).unlink(missing_ok=True)
    if (
        not isinstance(result, dict)
        or set(result) != _REFERENCE_RESULT_KEYS
        or result.get("pr_body_sha256") != hashlib.sha256(body_bytes).hexdigest()
    ):
        return unavailable
    return result


def select_linked_issue_reference(
    original_body: str,
    linked_issue: int,
    linked_issue_body: str | None,
    facts: dict[str, object],
) -> tuple[str, str, dict[str, object]]:
    """Choose the `Closes` / `Refs` reference from the evaluator result (Issue #2878).

    Returns `(final_body, link_kind, evaluator_result)`. The body is only extended when the
    evaluator says a reference is missing (or must be repaired to `Closes`); an existing,
    valid reference is preserved exactly. `fail_closed` never mutates the body and never
    guesses a kind: the pre-write validator (policy-mode LP057, same evaluator) stops it.
    """
    result = run_reference_policy_entrypoint(original_body, linked_issue, linked_issue_body, facts)
    final_body = original_body
    decision = result.get("decision")
    if isinstance(decision, str) and decision in {"closing_required", "nonclosing_required"}:
        needs_append = result.get("body_reason") != "closing_for_other" and (
            result.get("body_verdict") == "repair"
            or result.get("body_reason") == "reference_missing"
            or result.get("effective_kind") == "notes-only"
        )
        if needs_append:
            link_kind = "Closes" if result.get("decision") == "closing_required" else "Refs"
            final_body = append_linked_issue_reference(original_body, linked_issue, link_kind)
            result = run_reference_policy_entrypoint(final_body, linked_issue, linked_issue_body, facts)
    effective_kind = result.get("effective_kind")
    if effective_kind == "closing":
        link_kind = "Closes"
    elif effective_kind == "non-closing":
        link_kind = "Refs"
    else:
        link_kind = "none"
    return final_body, link_kind, result


def _load_implementation_scope_evidence_module():
    """Load #2699's shared normalizer without creating a shared package."""
    import importlib.util

    path = Path(__file__).resolve().parents[2] / "impl-review-loop" / "scripts" / "implementation_landed_evidence.py"
    spec = importlib.util.spec_from_file_location("implementation_landed_evidence_for_open_pr", path)
    if spec is None or spec.loader is None:
        return None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def get_linked_issue_body(repo: str, issue_number: int) -> str | None:
    try:
        result = run_gh("issue", "view", str(issue_number), "--repo", repo, "--json", "body")
        payload = json.loads(result.stdout)
    except (subprocess.SubprocessError, OSError, json.JSONDecodeError):
        return None
    body = payload.get("body") if isinstance(payload, dict) else None
    return body if isinstance(body, str) else None


def resolve_head_sha() -> str | None:
    try:
        result = subprocess.run(["git", "rev-parse", "HEAD"], capture_output=True, text=True, check=True, timeout=10)
    except subprocess.SubprocessError:
        return None
    sha = result.stdout.strip()
    return sha if re.fullmatch(r"[0-9a-f]{40}", sha, re.IGNORECASE) else None


def append_implementation_scope_coverage(body: str, *, repo: str, linked_issue: int) -> str | None:
    """Embed the immutable publication-time marker before validation/create.

    Issue #2811: idempotency is decided by the canonical parser
    (`implementation_landed_evidence.py::_parse_marker()`), not by a bare
    substring check, and it is decided BEFORE any live dependency
    (`get_linked_issue_body()` / `resolve_head_sha()` /
    `build_scope_coverage_marker()` / `render_scope_coverage_marker()`).

    - marker present + canonical-parser valid: body is returned unchanged.
    - marker present + canonical-parser invalid (any reject class other than
      `scope_coverage_marker_missing`): body is ALSO returned unchanged. This
      function never regenerates / repairs a malformed marker and raises no
      exception / error code. Fail-closed rejection is delegated to
      `validate_pr_body.py`'s validate-if-present check (LP059), which both
      `open_pr.py::_validate_pr_body()` (create) and
      `update_pr.py::_run_pr_body_validator()` (update) always run next.
    - marker absent (`scope_coverage_marker_missing`, i.e. no parseable
      marker fence): the existing producer path below materializes one.
    """
    module = _load_implementation_scope_evidence_module()
    if module is not None:
        existing_marker, parse_errors = module._parse_marker(body, issue_number=linked_issue)
        if existing_marker is not None:
            return body
        if parse_errors != ["scope_coverage_marker_missing"]:
            return body

    issue_body = get_linked_issue_body(repo, linked_issue)
    head_sha = resolve_head_sha()
    if issue_body is None or head_sha is None or module is None:
        return None
    try:
        marker = module.build_scope_coverage_marker(
            issue_number=linked_issue, issue_body=issue_body, pr_head_sha=head_sha
        )
        block = module.render_scope_coverage_marker(marker)
    except (AttributeError, TypeError, ValueError):
        return None
    return body.rstrip() + "\n\n" + block + "\n"


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
    linked_issue_body: str | None = None,
    reference_facts: dict[str, object] | None = None,
) -> dict[str, object]:
    validator_script = Path(__file__).resolve().parent / "validate_pr_body.py"

    body_file = tempfile.NamedTemporaryFile(
        mode="w",
        suffix=".md",
        encoding="utf-8",
        delete=False,
    )
    changed_paths_file = None
    linked_issue_body_file = None
    reference_facts_file = None
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

        if linked_issue_body:
            # Issue #2808 AC4: create path applies the same safety-applicability minimum
            # floor input (changed_paths + PR body + linked Issue body) as the update path.
            linked_issue_body_file = tempfile.NamedTemporaryFile(
                mode="w",
                suffix=".md",
                encoding="utf-8",
                delete=False,
            )
            linked_issue_body_file.write(linked_issue_body)
            linked_issue_body_file.flush()
            linked_issue_body_file.close()
            cmd.extend(["--linked-issue-body-file", linked_issue_body_file.name])

        if reference_facts is not None:
            # Issue #2878: supplying facts switches LP057 to the single reference-policy
            # evaluator (missing / invalid facts or a missing linked Issue body fail closed).
            reference_facts_file = tempfile.NamedTemporaryFile(
                mode="w",
                suffix=".json",
                encoding="utf-8",
                delete=False,
            )
            reference_facts_file.write(json.dumps(reference_facts))
            reference_facts_file.flush()
            reference_facts_file.close()
            cmd.extend(["--reference-facts-file", reference_facts_file.name])

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
        if linked_issue_body_file is not None:
            Path(linked_issue_body_file.name).unlink(missing_ok=True)
        if reference_facts_file is not None:
            Path(reference_facts_file.name).unlink(missing_ok=True)


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
        Path(__file__).resolve().parent.parent.parent / "create-issue" / "scripts" / "validate_japanese_content.py"
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


def _relation_repo_identity(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    normalized = value.strip().lower()
    return normalized if re.fullmatch(r"[a-z0-9][a-z0-9.-]*/[a-z0-9][a-z0-9._-]*", normalized) else None


def non_closing_authority_binds(
    non_closing_authority: object,
    pull_request: dict,
    snapshot_repo: str,
    candidate_issue: int,
    pr_number: object,
    expected_repo: str | None = None,
) -> bool:
    """Whether an orchestrator-attested `non_closing_authority` binds to this exact snapshot (#2878).

    No PR body grammar lives here: the authority is the 7-key projection of the evaluator result
    (`validate_pr_body.py --evaluate-reference-policy`). It binds only when the decision is
    `nonclosing_required` at level A1 / A2 (both imply an OPEN Issue), the repository (compared
    case-insensitively) / Issue number / PR number equal the snapshot's, and `pr_body_sha256`
    equals the SHA-256 of the snapshot's `pullRequest.body` UTF-8 bytes. The consumer-side twin is
    `task_context_workflow_signal._merged_evidence`; a parity test keeps the two aligned.
    """
    if not isinstance(non_closing_authority, dict) or set(non_closing_authority) != set(NON_CLOSING_AUTHORITY_KEYS):
        return False
    # `isinstance(str)` before set membership: a list / object `level` is a structured rejection,
    # never a `TypeError` from hashing it.
    level = non_closing_authority["level"]
    if non_closing_authority["decision"] != "nonclosing_required":
        return False
    if not isinstance(level, str) or level not in {"A1", "A2"}:
        return False
    authority_repo = _relation_repo_identity(non_closing_authority["repo"])
    if authority_repo is None or authority_repo != snapshot_repo:
        return False
    if expected_repo is not None and authority_repo != expected_repo:
        return False
    authority_issue = non_closing_authority["issue_number"]
    if type(authority_issue) is not int or authority_issue != candidate_issue:
        return False
    if type(pr_number) is not int or type(non_closing_authority["pr_number"]) is not int:
        return False
    if non_closing_authority["pr_number"] != pr_number:
        return False
    body = pull_request.get("body")
    if not isinstance(body, str):
        return False
    digest = non_closing_authority["pr_body_sha256"]
    return isinstance(digest, str) and digest == hashlib.sha256(body.encode("utf-8")).hexdigest()


def classify_closing_issue_relation(
    snapshot: object,
    candidate_issue: int,
    candidate_repo: str | None = None,
    non_closing_authority: dict | None = None,
) -> tuple[str, str, dict | None]:
    """Total, bounded classifier for a fresh PR GraphQL snapshot (#2565).

    A closing reference is an ``(repository, issue number)`` fact.  Number
    equality alone is not sufficient because GitHub may close an Issue in a
    different repository.

    Issue #2878: when the PR has no closing node at all, an attested
    ``non_closing_authority`` (A1 / A2, bound to the snapshot's ``pullRequest.body`` hash) is the
    only other way to bind the Issue. With any closing node the legacy rules apply unchanged.
    """
    if not isinstance(snapshot, dict):
        return "deferred", "RELATION_UNAVAILABLE", None
    # GraphQL may return usable-looking partial `data` alongside a top-level
    # `errors` member. That is not fresh authoritative relation evidence.
    if "errors" in snapshot:
        return "deferred", "RELATION_UNAVAILABLE", None
    try:
        repository = snapshot["data"]["repository"]
        pull_request = repository["pullRequest"]
        relation = pull_request["closingIssuesReferences"]
        nodes = relation["nodes"]
        repo = _relation_repo_identity(repository["nameWithOwner"])
    except (KeyError, TypeError):
        return "deferred", "RELATION_UNAVAILABLE", None
    expected_repo = _relation_repo_identity(candidate_repo) if candidate_repo is not None else repo
    if not isinstance(nodes, list) or repo is None or expected_repo is None or not isinstance(pull_request, dict):
        return "deferred", "RELATION_UNAVAILABLE", None
    if any(
        not isinstance(node, dict)
        or type(node.get("number")) is not int
        or not isinstance(node.get("repository"), dict)
        or _relation_repo_identity(node["repository"].get("nameWithOwner")) is None
        for node in nodes
    ):
        return "deferred", "RELATION_UNAVAILABLE", None
    if len(nodes) == 0:
        number = pull_request.get("number")
        if non_closing_authority_binds(
            non_closing_authority, pull_request, repo, candidate_issue, number, expected_repo
        ):
            return "matched", "MATCHED", {"repo": repo, "issue_number": candidate_issue, "pr_number": number}
        return "deferred", "NO_LINK", None
    if len(nodes) >= 2:
        return "conflict", "MULTIPLE_CLOSING_ISSUES", None
    relation_repo = _relation_repo_identity(nodes[0]["repository"]["nameWithOwner"])
    if nodes[0]["number"] != candidate_issue or relation_repo != expected_repo:
        return "conflict", "RELATION_ISSUE_MISMATCH", None
    number = pull_request.get("number")
    if type(number) is not int or number <= 0:
        return "deferred", "RELATION_UNAVAILABLE", None
    return "matched", "MATCHED", {"repo": repo, "issue_number": candidate_issue, "pr_number": number}


def fetch_live_pr_body(repo: str, pr_number: int) -> str | None:
    """Fresh-fetch the PR's live body from GitHub (never the local `final_body`)."""
    try:
        result = run_gh("api", f"repos/{repo}/pulls/{pr_number}")
        data = json.loads(result.stdout)
    except (subprocess.SubprocessError, OSError, json.JSONDecodeError):
        return None
    body = data.get("body") if isinstance(data, dict) else None
    return body if isinstance(body, str) else None


def _snapshot_with_live_body(snapshot: object, live_body: str) -> object:
    """Copy `snapshot` with `pullRequest.body` set to the freshly fetched live body."""
    try:
        repository = dict(snapshot["data"]["repository"])  # type: ignore[index]
        repository["pullRequest"] = {**repository["pullRequest"], "body": live_body}
        return {**snapshot, "data": {**snapshot["data"], "repository": repository}}  # type: ignore[index]
    except (KeyError, TypeError):
        return snapshot


def resolve_live_non_closing_authority(
    *, repo: str, pr_number: int, linked_issue: int, snapshot: object
) -> tuple[dict | None, object]:
    """Evaluate the reference policy on the PR's live body; return `(authority, snapshot_with_body)`.

    `authority` is the 7-key projection only for `nonclosing_required` at level A1 / A2 whose live
    body is `valid` (it really carries `Refs` for the Issue), and is otherwise `None` (A3 / CLOSED /
    `fail_closed` / unavailable facts keep the legacy disposition).
    """
    live_body = fetch_live_pr_body(repo, pr_number)
    if live_body is None:
        return None, snapshot
    state = get_linked_issue_state(repo, linked_issue)
    linked_issue_body = get_linked_issue_body(repo, linked_issue)
    if not isinstance(state, str) or state not in {"OPEN", "CLOSED"} or linked_issue_body is None:
        return None, snapshot
    facts = build_reference_facts(repo, state, live_body, pr_number)
    result = run_reference_policy_entrypoint(live_body, linked_issue, linked_issue_body, facts)
    # The live body must itself carry the Refs for this Issue: `decision` alone says what the
    # authority requires, `body_verdict == valid` says the body actually satisfies it.
    level = result.get("level")
    if (
        result.get("decision") != "nonclosing_required"
        or not isinstance(level, str)
        or level not in {"A1", "A2"}
        or result.get("body_verdict") != "valid"
    ):
        return None, snapshot
    authority = {key: result.get(key) for key in NON_CLOSING_AUTHORITY_KEYS}
    return authority, _snapshot_with_live_body(snapshot, live_body)


def emit_implementation_pr_observed(
    *, repo: str, pr_number: int, linked_issue: int, non_closing_authority: dict | None = None
) -> tuple[str, str]:
    """Best-effort producer adapter; a Task Context outcome never rolls back PR work.

    Issue #2878: a non-closing (`Refs`) PR is observed only when the evaluator, run on the PR's
    live body fetched from GitHub (not the local `final_body`), returns `nonclosing_required`
    at A1 / A2 and the authority's `pr_body_sha256` binds to that live body. Otherwise the
    legacy `NO_LINK` / `RELATION_ISSUE_MISMATCH` disposition is returned and nothing is emitted.
    """
    origin = os.environ.get("CLAUDE_CODE_SESSION_ID")
    if not origin:
        return "deferred", "unbound"
    owner, sep, name = repo.partition("/")
    if not sep or not owner or not name:
        return "deferred", "RELATION_UNAVAILABLE"
    query = (
        "query($owner:String!,$name:String!,$number:Int!){repository(owner:$owner,name:$name)"
        "{nameWithOwner pullRequest(number:$number){number "
        "closingIssuesReferences(first:2,excludeUserLinked:false,userLinkedOnly:false)"
        "{nodes{number repository{nameWithOwner}}}}}}"
    )
    try:
        response = run_gh(
            "api",
            "graphql",
            "-f",
            f"query={query}",
            "-F",
            f"owner={owner}",
            "-F",
            f"name={name}",
            "-F",
            f"number={pr_number}",
        )
        snapshot = json.loads(response.stdout)
    except (subprocess.SubprocessError, OSError, json.JSONDecodeError):
        return "deferred", "RELATION_UNAVAILABLE"
    disposition, reason, evidence = classify_closing_issue_relation(snapshot, linked_issue, repo, non_closing_authority)
    if evidence is None and reason == "NO_LINK" and non_closing_authority is None:
        authority, bound_snapshot = resolve_live_non_closing_authority(
            repo=repo, pr_number=pr_number, linked_issue=linked_issue, snapshot=snapshot
        )
        if authority is not None:
            disposition, reason, evidence = classify_closing_issue_relation(
                bound_snapshot, linked_issue, repo, authority
            )
    if evidence is None:
        return disposition, reason
    ctl = Path(__file__).resolve().parents[4] / "scripts" / "task-context" / "task_contextctl.py"
    payload = {
        "signal_kind": "implementation_pr_observed",
        "source": "open-pr",
        "source_schema_version": "v1",
        "evidence": evidence,
    }
    try:
        proc = subprocess.run(
            [sys.executable, str(ctl), "signal", "apply"],
            input=json.dumps(payload),
            text=True,
            capture_output=True,
            timeout=10,
        )
        data = json.loads(proc.stdout.splitlines()[-1]) if proc.stdout.splitlines() else {}
        result = data.get("data", {}) if isinstance(data, dict) else {}
        return str(result.get("disposition", "deferred")), str(result.get("reason_code", "RELATION_UNAVAILABLE"))
    except (subprocess.SubprocessError, OSError, json.JSONDecodeError, IndexError):
        return "deferred", "RELATION_UNAVAILABLE"


def _call_pr_body_validator(
    validator_callable,
    body_text: str,
    changed_paths: list[str] | None,
    linked_issue: int | None,
    linked_issue_body: str | None,
    reference_facts: dict[str, object] | None = None,
) -> dict[str, object]:
    """Call `_run_pr_body_validator` with the Issue #2808 AC4 `linked_issue_body`
    argument (and the Issue #2878 `reference_facts` argument) when the bound callable
    supports them, and fall back to the pre-#2808 3-argument call otherwise.

    This indirection exists solely so that pre-existing tests which monkeypatch
    `_run_pr_body_validator` with a fixed 3-argument test double keep working
    unchanged after AC4 added a 4th parameter (and #2878 a 5th) to the real implementation.
    """
    try:
        accepted_parameters = len(inspect.signature(validator_callable).parameters)
    except (TypeError, ValueError):
        accepted_parameters = 3
    if accepted_parameters >= 5:
        return validator_callable(body_text, changed_paths, linked_issue, linked_issue_body, reference_facts)
    if accepted_parameters >= 4:
        return validator_callable(body_text, changed_paths, linked_issue, linked_issue_body)
    return validator_callable(body_text, changed_paths, linked_issue)


def _validate_pr_body(
    body: str,
    changed_paths: list[str] | None,
    linked_issue: int,
    linked_issue_body: str | None = None,
    reference_facts: dict[str, object] | None = None,
) -> tuple[bool, str | None, str | None]:
    """Run both PR-body validators against `body`.

    Returns `(passed, error_code, detail)`. `error_code`/`detail` are set only
    when `passed` is False, mirroring the two `emit_error(...)` call sites
    this replaces. Any `VALIDATOR_RULE_IDS` / `PR_BODY_PREFLIGHT_RESULT_V1`
    stdout emitted before the failure is still emitted here. `linked_issue_body`
    (Issue #2808 AC4) is best-effort input to the safety-applicability minimum floor;
    its absence never blocks validation on its own.
    """
    validator_result = _call_pr_body_validator(
        _run_pr_body_validator, body, changed_paths, linked_issue, linked_issue_body, reference_facts
    )
    if validator_result.get("status") != "pass":
        errors = validator_result.get("errors", [])
        rule_ids = ",".join(error.get("rule_id", "") for error in errors if isinstance(error, dict))
        detail = validator_result.get("message", "PR body validation failed")
        if rule_ids:
            detail = f"{detail}; rule_ids={rule_ids}"
            emit_kv("VALIDATOR_RULE_IDS", rule_ids)
            reference_messages = [
                str(error.get("message", ""))
                for error in errors
                if isinstance(error, dict) and error.get("rule_id") == "LP057"
            ]
            if reference_messages:
                detail = f"{detail}; {' '.join(reference_messages)}"
        return False, _classify_validator_errors(errors), str(detail)

    japanese_result = _run_japanese_content_validator(body)
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
        return False, E_PR_BODY_JAPANESE_VALIDATION_FAILED, japanese_result.get("stderr", "")

    return True, None, None


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

    # Keep resolving the linked Issue state first so the existing state/readback
    # hard gate remains active. Issue #2878: the Closes / Refs choice is no longer a
    # function of the Issue state alone. A single reference authority evaluator
    # (`validate_pr_body.py --evaluate-reference-policy`) decides from fresh facts; a valid
    # caller-provided reference is preserved exactly, and a missing one is appended with the
    # kind the evaluator requires. `fail_closed` never guesses a kind: the pre-write validator
    # below (policy-mode LP057, same evaluator) stops the run.
    linked_issue_body_for_validation = get_linked_issue_body(repo, args.linked_issue)
    reference_facts = build_reference_facts(repo, state, original_body, None)
    final_body, link_kind, _reference_result = select_linked_issue_reference(
        original_body, args.linked_issue, linked_issue_body_for_validation, reference_facts
    )

    # Issue #2699 P0-1 fix_delta: the durable IMPLEMENTATION_SCOPE_COVERAGE_V1
    # marker is only ever meaningful as a *publication-time* snapshot, so it
    # is computed later, immediately before `create_pr()`, and only on the
    # branch that actually creates a new PR. Validating this pre-marker body
    # first means dry-run, existing-PR resume, and a validator error-path
    # never depend on marker retrieval (live Issue body / branch HEAD /
    # shared normalizer availability).
    changed_paths = resolve_changed_paths(args.changed_paths)
    # Issue #2808 AC4: the linked Issue body (fetched above) is also the safety-applicability
    # minimum floor input surface (changed_paths + PR body + linked Issue body). For the
    # safety floor a fetch failure only narrows the text signal; for the reference policy
    # (Issue #2878) a missing Issue body is a `fail_closed` fact problem and blocks here.
    passed, error_code, detail = _validate_pr_body(
        final_body, changed_paths, args.linked_issue, linked_issue_body_for_validation, reference_facts
    )
    if not passed:
        emit_error(error_code, detail or "")
        return EXIT_BLOCKED

    draft = str(args.draft).strip().lower() == "true"

    # A preview has no producer side effects. In particular, do not inspect an
    # existing PR because that path emits implementation_pr_observed.
    if args.dry_run:
        emit_kv("DRY_RUN", "true")
        emit_kv("PR_TITLE_PREVIEW", args.pr_title)
        emit_kv("PR_BODY_PREVIEW_FIRST_LINES", "\\n".join(final_body.splitlines()[:5]))
        emit_kv("LINKED_ISSUE", args.linked_issue)
        emit_kv("LINK_KIND", link_kind)
        emit_kv("DRAFT", str(draft).lower())
        return 0

    existing = find_existing_pr(repo, branch)
    if existing:
        signal_disposition, signal_reason = emit_implementation_pr_observed(
            repo=repo, pr_number=int(existing["number"]), linked_issue=args.linked_issue
        )
        emit_kv("TASK_CONTEXT_SIGNAL_DISPOSITION", signal_disposition)
        emit_kv("TASK_CONTEXT_SIGNAL_REASON", signal_reason)
        emit_kv("EXISTING", "true")
        emit_kv("PR_URL", existing["url"])
        emit_kv("PR_NUMBER", existing["number"])
        emit_kv("LINKED_ISSUE", args.linked_issue)
        emit_kv("LINK_KIND", link_kind)
        return 0

    final_body_file = tempfile.NamedTemporaryFile(
        mode="w",
        suffix=".md",
        encoding="utf-8",
        delete=False,
    )
    try:
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
                signal_disposition, signal_reason = emit_implementation_pr_observed(
                    repo=target_repo, pr_number=int(canonical_existing["number"]), linked_issue=args.linked_issue
                )
                emit_kv("TASK_CONTEXT_SIGNAL_DISPOSITION", signal_disposition)
                emit_kv("TASK_CONTEXT_SIGNAL_REASON", signal_reason)
                emit_kv("EXISTING", "true")
                emit_kv("PR_URL", canonical_existing["url"])
                emit_kv("PR_NUMBER", canonical_existing["number"])
                emit_kv("LINKED_ISSUE", args.linked_issue)
                emit_kv("LINK_KIND", link_kind)
                return 0

        # Issue #2699 P0-1 fix_delta: a new PR is actually about to be
        # created (dry-run, existing-PR resume, and the canonical-existing
        # resume above have all already returned), so this is the only
        # branch where computing the durable IMPLEMENTATION_SCOPE_COVERAGE_V1
        # publication-time marker is required. Embedding it, and then
        # re-validating the body that will actually be published, keeps the
        # existing PR-body validator (Japanese ratio / Safety Claim Matrix
        # etc.) as the single source of truth for what `create_pr()` sends.
        marker_body = append_implementation_scope_coverage(final_body, repo=repo, linked_issue=args.linked_issue)
        if marker_body is None:
            emit_error(
                E_IMPLEMENTATION_SCOPE_COVERAGE_UNAVAILABLE,
                "live Issue body / branch HEAD / shared scope normalizer を取得できませんでした",
            )
            return EXIT_BLOCKED
        marker_passed, marker_error_code, marker_detail = _validate_pr_body(
            marker_body, changed_paths, args.linked_issue, linked_issue_body_for_validation, reference_facts
        )
        if not marker_passed:
            emit_error(marker_error_code, marker_detail or "")
            return EXIT_BLOCKED

        final_body_file.write(marker_body)
        final_body_file.flush()
        final_body_file.close()
        final_body_path = Path(final_body_file.name)

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
        if pr_number:
            signal_disposition, signal_reason = emit_implementation_pr_observed(
                repo=pr_create_repo, pr_number=int(pr_number), linked_issue=args.linked_issue
            )
            emit_kv("TASK_CONTEXT_SIGNAL_DISPOSITION", signal_disposition)
            emit_kv("TASK_CONTEXT_SIGNAL_REASON", signal_reason)

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
