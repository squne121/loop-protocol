#!/usr/bin/env python3
"""Transaction helper for deterministic issue creation.

Performs a single create-issue transaction for create-issue skill automation:
1) dedupe search
2) create issue
3) apply labels
4) register as sub-issue (optional)
5) register dependencies (optional)
6) read-back verification
7) post partial-failure audit comment (if needed)
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal


class TransactionError(Exception):
    def __init__(self, stage: str, message: str, *, command: str | None = None, output: str | None = None) -> None:
        super().__init__(message)
        self.stage = stage
        self.message = message
        self.command = command
        self.output = output


@dataclass
class TransactionResult:
    status: str
    issue_number: int | None
    issue_url: str | None
    completed_steps: list[str]
    failure_stage: str | None = None
    failure_message: str | None = None
    dedupe_number: int | None = None
    parent_verified: bool | None = None
    dependency_verified: bool | None = None


def run_command(command: list[str], *, check: bool = False, capture_output: bool = True) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        command,
        text=True,
        check=check,
        capture_output=capture_output,
    )


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Create issue transaction helper")
    parser.add_argument("--repo", required=True, help="owner/repo or full repo path")
    parser.add_argument("--title", required=True)
    parser.add_argument("--body", default="")
    parser.add_argument("--body-file", default="")
    parser.add_argument("--label", action="append", default=[])
    parser.add_argument("--parent-issue", type=int, default=0)
    parser.add_argument("--dependency", action="append", dest="dependency", type=int, default=[])
    parser.add_argument("--blocked-by", action="append", dest="dependency", type=int)
    parser.add_argument("--gh", default="gh")
    return parser.parse_args(argv)


def _normalize_dependency_numbers(dependency_issue_numbers: list[int | str]) -> list[int]:
    normalized: list[int] = []
    for value in dependency_issue_numbers:
        try:
            normalized.append(int(value))
        except (TypeError, ValueError) as exc:
            raise TransactionError(
                stage="dependency-parse",
                message="invalid dependency issue number",
                output=str(value),
            ) from exc
    return normalized


def _github_owner_repo(repo: str) -> tuple[str, str]:
    if "/" not in repo:
        raise ValueError("--repo must be <owner>/<name>")
    owner, name = repo.split("/", 1)
    if not owner or not name:
        raise ValueError("--repo must be <owner>/<name>")
    return owner, name


def _run_gh_json(args: list[str], *, stage: str) -> Any:
    cp = run_command(args)
    if cp.returncode != 0:
        raise TransactionError(stage=stage, message=f"{stage} failed", command=" ".join(args), output=(cp.stderr or cp.stdout).strip())

    text = (cp.stdout or "").strip()
    if not text:
        raise TransactionError(stage=stage, message=f"{stage} returned empty output", command=" ".join(args))

    try:
        return json.loads(text)
    except json.JSONDecodeError as exc:
        raise TransactionError(
            stage=stage,
            message=f"{stage} returned non-json output",
            command=" ".join(args),
            output=str(exc),
        ) from exc


def _run_gh_text(args: list[str], *, stage: str) -> str:
    cp = run_command(args)
    if cp.returncode != 0:
        raise TransactionError(stage=stage, message=f"{stage} failed", command=" ".join(args), output=(cp.stderr or cp.stdout).strip())
    return (cp.stdout or "").strip()


# Saturation guard: if result count >= this limit, we cannot guarantee complete exact-match coverage.
# Use `in:title "<escaped title>"` qualifier to scope search to title field; client-side exact match
# is still required because GitHub search tokenizes rather than literal-matches.
#
# _DEDUPE_SEARCH_LIMIT rationale:
# (a) 閾値 200 の根拠: GitHub issue search は tokenize ベースのため完全一致保証がなく、
#     in:title qualifier で絞り込んでも同リポジトリで 200 件超の同語 title が存在する場合は
#     誤判定リスクが無視できない。200 件未満であれば client-side exact-match で完全性を保証できる
#     実運用上の上限として設定している（issue 作成頻度と運用規模から導出）。
# (b) `gh issue list --limit` の実上限との関係: `gh issue list` の CLI レベルに --limit の上限はないが、
#     --search オプション使用時は GitHub Search API 経由になり 1000 件が事実上の上限となる
#     （gh CLI 内部で SearchCapped として処理）。本実装は --search を併用するため Search API 上限の制約を受ける。
#     200 を上限とすることで「件数が多すぎて安全な dedupe 判定不能」な状態を早期検知し、
#     saturation guard として fail-close する（1000 まで許容すると誤ヒット混入リスクが増大する）。
# (c) saturation guard と client-side filter の関係: `gh issue list` が返す件数が _DEDUPE_SEARCH_LIMIT に
#     達した場合、未取得 issue が存在する可能性があるため TransactionError を raise して処理を中断する。
#     件数が上限未満の場合のみ client-side の title 完全一致フィルタが「全件検索済み」として機能する。
_DEDUPE_SEARCH_LIMIT = 200


def _find_open_issues_by_title(repo: str, title: str, gh_bin: str) -> list[int]:
    # Normalize title for search: strip newlines (gh search does not support multi-line queries),
    # escape internal double-quotes.
    normalized = title.replace("\n", " ").replace("\r", " ")
    if normalized.strip() == "":
        raise TransactionError(stage="dedupe-search", message="title is empty or whitespace-only")
    escaped = normalized.replace('"', '\\"')
    search_query = f'in:title "{escaped}"'

    args = [
        gh_bin,
        "issue",
        "list",
        "--repo",
        repo,
        "--state",
        "open",
        "--search",
        search_query,
        "--limit",
        str(_DEDUPE_SEARCH_LIMIT),
        "--json",
        "number,title,url",  # --json は comma-separated フィールド名を 1 引数で渡す仕様（3 つに分けて渡すと CLI エラー）
    ]
    result = _run_gh_json(args, stage="dedupe-search")
    # Saturation guard: if we hit the limit, we cannot guarantee completeness for exact-match.
    if len(result) >= _DEDUPE_SEARCH_LIMIT:
        raise TransactionError(
            stage="dedupe-search",
            message="dedupe search result saturated; cannot guarantee exact-match completeness",
            output=f"result count={len(result)} >= limit={_DEDUPE_SEARCH_LIMIT}; search_query={search_query!r}",
        )
    # Compare normalized titles to handle GitHub-side newline normalization
    target = normalized.strip()
    issue_numbers: list[int] = []
    for item in result:
        item_title = str(item.get("title", "")).replace("\n", " ").replace("\r", " ").strip()
        if item_title == target:
            issue_numbers.append(int(item["number"]))
    return issue_numbers


# Default polling parameters for post-create race detection.
# Sized to cover GitHub search index propagation delay (typically 5-30 seconds).
# Total worst-case wall time = sum(delays) ≈ 32 seconds.
# Override via run_transaction(sleep_fn=...) or _poll_for_created_issue kwargs.
_DEFAULT_MAX_ATTEMPTS: int = 5
_DEFAULT_RACE_DETECTION_DELAYS: tuple[float, ...] = (0.0, 2.0, 5.0, 10.0, 15.0)


def _poll_for_created_issue(
    repo: str,
    title: str,
    expected_issue_number: int,
    gh_bin: str,
    *,
    max_attempts: int = _DEFAULT_MAX_ATTEMPTS,
    delays: tuple[float, ...] = _DEFAULT_RACE_DETECTION_DELAYS,
    sleep_fn: Callable[[float], None] = time.sleep,
) -> tuple[Literal["confirmed", "race", "inconclusive"], list[int]]:
    """Poll for the newly created issue, absorbing GitHub search index propagation delay.

    Returns:
        ("confirmed", [expected_issue_number]) if only our issue is visible.
        ("race", matching) if another issue with the same title is detected.
        ("inconclusive", last_matching) if we could not confirm after max_attempts.

    Raises:
        TransactionError: if the underlying dedupe-search raises (e.g., saturation guard).
    """
    last_matching: list[int] = []
    for attempt in range(max_attempts):
        delay = delays[attempt] if attempt < len(delays) else delays[-1]
        if delay > 0.0:
            sleep_fn(delay)

        # May raise TransactionError(stage="dedupe-search") for saturation — let it propagate.
        matching = _find_open_issues_by_title(repo, title, gh_bin)
        last_matching = matching

        if len(matching) == 1 and matching[0] == expected_issue_number:
            # Only our issue is visible: confirmed.
            return ("confirmed", matching)

        if len(matching) > 1 and expected_issue_number in matching:
            # Our issue exists but there are others with the same title: race.
            return ("race", matching)

        if len(matching) >= 1 and expected_issue_number not in matching:
            # Other issues visible but ours is not: race (we may be the duplicate).
            return ("race", matching)

        # matching is empty: search index hasn't propagated yet — retry.

    return ("inconclusive", last_matching)


def _dedupe_search(repo: str, title: str, gh_bin: str) -> int | None:
    issue_numbers = _find_open_issues_by_title(repo, title, gh_bin)
    return issue_numbers[0] if issue_numbers else None


def _issue_create(repo: str, title: str, body: str, body_file: str, gh_bin: str) -> str:
    create_args: list[str] = [
        gh_bin,
        "issue",
        "create",
        "--repo",
        repo,
        "--title",
        title,
    ]

    if body_file:
        path = Path(body_file)
        if not path.is_file():
            raise TransactionError(stage="issue-create", message="body-file not found", output=str(path))
        create_args.extend(["--body-file", str(path)])
    else:
        create_args.extend(["--body", body or ""])  # create-issue text body is always explicit

    issue_url = _run_gh_text(create_args, stage="issue-create")
    if not issue_url:
        raise TransactionError(stage="issue-create", message="issue-create returned empty URL")
    return issue_url


def _issue_number_from_url(issue_url: str) -> int:
    match = re.search(r"/issues/(\d+)(?:/.*)?$", issue_url.strip())
    if not match:
        raise TransactionError(stage="issue-create", message="failed to parse issue number", output=issue_url)
    return int(match.group(1))


def _issue_apply_labels(repo: str, issue_number: int, labels: list[str], gh_bin: str) -> None:
    if not labels:
        return
    args = [
        gh_bin,
        "issue",
        "edit",
        "--repo",
        repo,
        str(issue_number),
        "--add-label",
        ",".join(labels),
    ]
    _run_gh_text(args, stage="label-apply")


def _issue_graphql_ids(repo: str, issue_number: int, gh_bin: str) -> tuple[str, int]:
    owner, name = _github_owner_repo(repo)
    query = (
        "query($owner:String!,$name:String!,$number:Int!){"
        "repository(owner:$owner,name:$name){"
        "issue(number:$number){id databaseId}"
        "}}"
    )
    args = [
        gh_bin,
        "api",
        "graphql",
        "-f",
        "query=\n" + query,
        "-F",
        f"owner={owner}",
        "-F",
        f"name={name}",
        "-F",
        f"number={issue_number}",
    ]
    payload = _run_gh_json(args, stage="issue-ids")
    try:
        issue = payload["data"]["repository"]["issue"]
        node_id = issue["id"]
        database_id = int(issue["databaseId"])
    except (KeyError, TypeError, ValueError) as exc:
        raise TransactionError(stage="issue-ids", message="invalid issue id payload", output=str(payload)) from exc
    if not node_id or not database_id:
        raise TransactionError(stage="issue-ids", message="missing node/database id", output=str(payload))
    return str(node_id), int(database_id)


def _issue_register_sub_issue(repo: str, parent_issue_number: int, child_database_id: int, gh_bin: str) -> None:
    endpoint = f"repos/{repo}/issues/{parent_issue_number}/sub_issues"
    args = [
        gh_bin,
        "api",
        endpoint,
        "--method",
        "POST",
        "-F",
        f"sub_issue_id={child_database_id}",
    ]
    _run_gh_text(args, stage="sub-issue-register")


def _issue_register_dependency(repo: str, child_node_id: str, dependency_node_id: str, gh_bin: str) -> None:
    query = "mutation($input:AddBlockedByInput!){addBlockedBy(input:$input){clientMutationId}}"
    args = [
        gh_bin,
        "api",
        "graphql",
        "-f",
        f"query={query}",
        "-F",
        f"input[issueId]={child_node_id}",
        "-F",
        f"input[blockingIssueId]={dependency_node_id}",
    ]
    _run_gh_text(args, stage="dependency-register")


def _readback_labels(repo: str, issue_number: int, labels: list[str], gh_bin: str) -> bool:
    if not labels:
        return True
    owner, name = _github_owner_repo(repo)
    query = (
        "query($owner:String!,$name:String!,$number:Int!){"
        "repository(owner:$owner,name:$name){"
        "issue(number:$number){labels(first:100){nodes{name}}}"
        "}}"
    )
    args = [
        gh_bin,
        "api",
        "graphql",
        "-f",
        "query=\n" + query,
        "-F",
        f"owner={owner}",
        "-F",
        f"name={name}",
        "-F",
        f"number={issue_number}",
    ]
    payload = _run_gh_json(args, stage="label-readback")
    try:
        labels_payload = (
            payload["data"]["repository"]["issue"]
            .get("labels", {})
            .get("nodes", [])
        )
        current_labels = {str(item.get("name")) for item in labels_payload if item.get("name") is not None}
    except (KeyError, TypeError) as exc:
        raise TransactionError(stage="label-readback", message="invalid label read-back payload", output=str(payload)) from exc
    return set(labels).issubset(current_labels)


def _readback_parent_issue(repo: str, issue_number: int, parent_issue_number: int, gh_bin: str) -> bool:
    args = [
        gh_bin,
        "api",
        f"repos/{repo}/issues/{issue_number}/parent",
        "-F",
        "accept=application/vnd.github+json",
    ]
    cp = run_command(args)
    if cp.returncode != 0:
        return False
    try:
        data = json.loads((cp.stdout or "").strip() or "{}")
        return int(data.get("number", -1)) == int(parent_issue_number)
    except (json.JSONDecodeError, TypeError, ValueError):
        return False


def _readback_dependencies(repo: str, issue_number: int, dependency_issue_numbers: list[int], gh_bin: str) -> bool:
    if not dependency_issue_numbers:
        return True

    owner, name = _github_owner_repo(repo)
    query = (
        "query($owner:String!,$name:String!,$number:Int!){"
        "repository(owner:$owner,name:$name){"
        "issue(number:$number){blockedBy(first:100){nodes{number}}}"
        "}}"
    )
    args = [
        gh_bin,
        "api",
        "graphql",
        "-f",
        "query=\n" + query,
        "-F",
        f"owner={owner}",
        "-F",
        f"name={name}",
        "-F",
        f"number={issue_number}",
    ]
    payload = _run_gh_json(args, stage="dependency-readback")
    try:
        nodes = payload["data"]["repository"]["issue"].get("blockedBy", {}).get("nodes", [])
        readback_numbers = {int(item.get("number")) for item in nodes if item.get("number") is not None}
    except (KeyError, TypeError):
        return False
    return set(dependency_issue_numbers).issubset(readback_numbers)


def _recovery_hint_for_stage(
    failed_stage: str,
    repo: str,
    issue_number: int,
    requested_parent_issue_number: int,
    requested_dependency_issue_numbers: list[int],
) -> str:
    """Return a stage-specific recovery hint string."""
    owner_repo = repo  # format: owner/repo
    if failed_stage == "sub-issue-readback":
        parent = requested_parent_issue_number
        return (
            f"Recovery hint: sub-issue-readback failed for #{issue_number} under parent #{parent}.\n"
            f"  Manual re-register command (idempotent: 既存関係 readback で確認後に再実行（重複登録は API がエラーを返す可能性あり）):\n"
            f"  1. まず readback で関係の有無を確認する:\n"
            f"    gh api repos/{owner_repo}/issues/{issue_number}/parent\n"
            f"  2. 未登録が確認できた場合のみ登録 mutation を実行する:\n"
            f"    gh api repos/{owner_repo}/issues/{parent}/sub_issues --method POST -F sub_issue_id=<child_db_id>\n"
            f"  Lookup child database ID:\n"
            f"    gh api graphql -f query='query{{repository(owner:\"<owner>\",name:\"<repo>\"){{issue(number:{issue_number}){{databaseId}}}}}}'\n"
            f"  Verify:\n"
            f"    gh api repos/{owner_repo}/issues/{issue_number}/parent"
        )
    if failed_stage in ("dependency-readback", "dependency-register"):
        deps = ", ".join(f"#{d}" for d in requested_dependency_issue_numbers) if requested_dependency_issue_numbers else "(none)"
        return (
            f"Recovery hint: {failed_stage} failed for #{issue_number} (blockers: {deps}).\n"
            "  Manual re-register command (idempotent: 既存関係 readback で確認後に再実行（重複登録は API がエラーを返す可能性あり）):\n"
            f"  1. まず readback で blockedBy 関係の有無を確認する:\n"
            f"    gh api graphql -f query='query{{repository(owner:\"<owner>\",name:\"<repo>\"){{issue(number:{issue_number}){{blockedBy(first:10){{nodes{{number}}}}}}}}}}'\n"
            "  2. 未登録が確認できた場合のみ登録 mutation を実行する:\n"
            "    gh api graphql -f query='mutation($input:AddBlockedByInput!){addBlockedBy(input:$input){clientMutationId}}' \\\n"
            "      -F 'input[issueId]=<child_node_id>' -F 'input[blockingIssueId]=<blocker_node_id>'\n"
            "  Lookup node IDs:\n"
            f"    gh api graphql -f query='query{{repository(owner:\"<owner>\",name:\"<repo>\"){{issue(number:<N>){{id}}}}}}'\n"
            "  Verify:\n"
            f"    gh api graphql -f query='query{{repository(owner:\"<owner>\",name:\"<repo>\"){{issue(number:{issue_number}){{blockedBy(first:10){{nodes{{number}}}}}}}}}}'"
        )
    if failed_stage == "label-readback":
        return (
            f"Recovery hint: label-readback failed for #{issue_number}.\n"
            "  Manual re-apply command (idempotent: yes):\n"
            f"    gh issue edit {issue_number} --repo {owner_repo} --add-label <labels>\n"
            "  Verify:\n"
            f"    gh issue view {issue_number} --repo {owner_repo} --json labels"
        )
    if failed_stage in ("dedupe-search", "dedupe-race-detection"):
        return (
            f"Recovery hint: {failed_stage} — automatic recovery is not possible.\n"
            "  Manual action required: inspect open issues with the same title and decide which to close.\n"
            f"    gh issue list --repo {owner_repo} --state open --search '<title>'\n"
            "  idempotent re-run: no (requires manual deduplication first)"
        )
    return (
        f"Recovery hint: stage '{failed_stage}' encountered an unexpected failure.\n"
        "  Generic guidance: verify the issue state manually and re-run the transaction if safe.\n"
        f"    gh issue view {issue_number} --repo {owner_repo} --json number,title,labels,state\n"
        "  idempotent re-run: depends on the specific failure"
    )


def _post_partial_failure_comment(
    repo: str,
    issue_number: int,
    failed_stage: str,
    failure_message: str,
    gh_bin: str,
    *,
    completed_steps: list[str],
    requested_labels: list[str],
    requested_parent_issue_number: int,
    requested_dependency_issue_numbers: list[int],
    failure_context: str | None = None,
) -> None:
    requested_labels_text = ", ".join(requested_labels) if requested_labels else "(none)"
    requested_parent_text = f"#{requested_parent_issue_number}" if requested_parent_issue_number else "(none)"
    requested_dependencies_text = (
        ", ".join(f"#{int(dep)}" for dep in requested_dependency_issue_numbers)
        if requested_dependency_issue_numbers
        else "(none)"
    )
    completed_steps_text = ", ".join(completed_steps) if completed_steps else "(none)"

    recovery_hint = _recovery_hint_for_stage(
        failed_stage=failed_stage,
        repo=repo,
        issue_number=issue_number,
        requested_parent_issue_number=requested_parent_issue_number,
        requested_dependency_issue_numbers=requested_dependency_issue_numbers,
    )

    comment = (
        "create-issue transaction partial-failure\n\n"
        f"Issue: #{issue_number}\n"
        f"Failure stage: {failed_stage}\n"
        f"Message: {failure_message}\n\n"
        "Requested:\n"
        f"- labels: {requested_labels_text}\n"
        f"- parent: {requested_parent_text}\n"
        f"- dependencies: {requested_dependencies_text}\n"
        f"Completed steps: {completed_steps_text}\n\n"
        f"Failure context: {failure_context or '(none)'}\n\n"
        f"{recovery_hint}\n\n"
        "Please recover deterministically before re-running the create issue transaction."
    )
    args = [
        gh_bin,
        "issue",
        "comment",
        "--repo",
        repo,
        str(issue_number),
        "--body",
        comment,
    ]
    cp = run_command(args)
    if cp.returncode != 0:
        raise TransactionError(
            stage="partial-failure-comment",
            message="partial-failure audit comment failed",
            output=(cp.stderr or cp.stdout).strip(),
        )


def _report_partial_failure(
    *,
    repo: str,
    issue_number: int,
    issue_url: str,
    failed_exc: TransactionError,
    completed_steps: list[str],
    labels: list[str],
    parent_issue_number: int,
    dependency_issue_numbers: list[int],
    gh_bin: str,
    parent_verified: bool | None,
    dependency_verified: bool | None,
    failure_context: str | None = None,
) -> TransactionResult:
    failure_stage = failed_exc.stage
    failure_message = failed_exc.message
    try:
        _post_partial_failure_comment(
            repo=repo,
            issue_number=issue_number,
            failed_stage=failed_exc.stage,
            failure_message=failed_exc.message,
            gh_bin=gh_bin,
            completed_steps=completed_steps,
            requested_labels=labels,
            requested_parent_issue_number=parent_issue_number,
            requested_dependency_issue_numbers=dependency_issue_numbers,
            failure_context="\n".join(
                [line for line in [f"command={failed_exc.command}", f"output={failed_exc.output}"] if line != "output=None" and line != "command=None"]
            )
            + (f"\n{failure_context}" if failure_context else ""),
        )
    except TransactionError as comment_error:
        failure_stage = comment_error.stage
        failure_message = (
            f"{failed_exc.message}; audit-comment-failed={comment_error.message} output={comment_error.output}"
        )

    return TransactionResult(
        status="partial_failure",
        issue_number=issue_number,
        issue_url=issue_url,
        completed_steps=completed_steps,
        failure_stage=failure_stage,
        failure_message=failure_message,
        parent_verified=parent_verified,
        dependency_verified=dependency_verified,
    )


def _reconcile_issue_links(
    repo: str,
    issue_number: int,
    labels: list[str],
    parent_issue_number: int,
    dependency_issue_numbers: list[int],
    gh_bin: str,
    *,
    sleep_fn: Callable[[float], None] = time.sleep,
) -> tuple[list[str], bool | None, bool | None]:
    completed: list[str] = []
    parent_verified = True if parent_issue_number else None
    dependency_verified = True if dependency_issue_numbers else None

    if labels:
        labels_verified = _readback_labels(repo, issue_number, labels, gh_bin)
        if not labels_verified:
            raise TransactionError(stage="dedupe-label-readback", message="label read-back mismatch")
        completed.append("label-readback")

    if parent_issue_number:
        parent_verified = _readback_parent_issue(repo, issue_number, parent_issue_number, gh_bin)
        if not parent_verified:
            # Retry once after a brief delay (GitHub API propagation)
            sleep_fn(2.0)
            parent_verified = _readback_parent_issue(repo, issue_number, parent_issue_number, gh_bin)
        if not parent_verified:
            raise TransactionError(stage="sub-issue-readback", message="parent read-back mismatch")
        completed.append("sub-issue-readback")

    if dependency_issue_numbers:
        dependency_verified = _readback_dependencies(repo, issue_number, dependency_issue_numbers, gh_bin)
        if not dependency_verified:
            # Retry once after a brief delay (GitHub API propagation)
            sleep_fn(2.0)
            dependency_verified = _readback_dependencies(repo, issue_number, dependency_issue_numbers, gh_bin)
        if not dependency_verified:
            raise TransactionError(stage="dependency-readback", message="dependency read-back mismatch")
        completed.append("dependency-readback")

    return completed, parent_verified, dependency_verified


def run_transaction(
    *,
    repo: str,
    title: str,
    body: str,
    body_file: str,
    labels: list[str],
    parent_issue_number: int,
    dependency_issue_numbers: list[int | str],
    gh_bin: str,
    sleep_fn: Callable[[float], None] = time.sleep,
) -> TransactionResult:
    normalized_dependency_issue_numbers = _normalize_dependency_numbers(dependency_issue_numbers)
    completed: list[str] = []
    try:
        dedupe_issue_numbers = _find_open_issues_by_title(repo, title, gh_bin)
    except TransactionError as exc:
        return TransactionResult(
            status="failure",
            issue_number=None,
            issue_url=None,
            completed_steps=[],
            failure_stage=exc.stage,
            failure_message=exc.message,
        )
    dedupe_number: int | None = dedupe_issue_numbers[0] if dedupe_issue_numbers else None
    if len(dedupe_issue_numbers) > 1:
        dedupe_collision_issue_number = dedupe_issue_numbers[0]
        dedupe_failure = TransactionError(
            stage="dedupe-search",
            message="multiple exact-title open issues before create",
            output=str(dedupe_issue_numbers),
        )
        return _report_partial_failure(
            repo=repo,
            issue_number=dedupe_collision_issue_number,
            issue_url=f"https://github.com/{repo}/issues/{dedupe_collision_issue_number}",
            failed_exc=dedupe_failure,
            completed_steps=["dedupe"],
            labels=labels,
            parent_issue_number=parent_issue_number,
            dependency_issue_numbers=normalized_dependency_issue_numbers,
            gh_bin=gh_bin,
            parent_verified=None,
            dependency_verified=None,
            failure_context=f"candidate_issue_numbers={dedupe_issue_numbers}",
        )

    if dedupe_number is not None:
        dedupe_completed = ["dedupe"]
        parent_verified = None
        dependency_verified = None
        dedupe_issue_url = f"https://github.com/{repo}/issues/{dedupe_number}"
        try:
            reconcile_steps, parent_verified, dependency_verified = _reconcile_issue_links(
                repo=repo,
                issue_number=dedupe_number,
                labels=labels,
                parent_issue_number=parent_issue_number,
                dependency_issue_numbers=normalized_dependency_issue_numbers,
                gh_bin=gh_bin,
                sleep_fn=sleep_fn,
            )
            dedupe_completed.extend(reconcile_steps)
            return TransactionResult(
                status="dedupe",
                issue_number=dedupe_number,
                issue_url=dedupe_issue_url,
                completed_steps=dedupe_completed,
                dedupe_number=dedupe_number,
                parent_verified=parent_verified,
                dependency_verified=dependency_verified,
            )
        except TransactionError as exc:
            return _report_partial_failure(
                repo=repo,
                issue_number=dedupe_number,
                issue_url=dedupe_issue_url,
                failed_exc=exc,
                completed_steps=dedupe_completed,
                labels=labels,
                parent_issue_number=parent_issue_number,
                dependency_issue_numbers=normalized_dependency_issue_numbers,
                gh_bin=gh_bin,
                parent_verified=parent_verified,
                dependency_verified=dependency_verified,
            )

    issue_url = _issue_create(repo, title, body, body_file, gh_bin)
    issue_number = _issue_number_from_url(issue_url)
    completed.append("create")
    matching_issue_numbers: list[int] = []

    try:
        try:
            poll_verdict, matching_issue_numbers = _poll_for_created_issue(
                repo, title, issue_number, gh_bin, sleep_fn=sleep_fn
            )
        except TransactionError as exc:
            if exc.stage == "dedupe-search":
                raise TransactionError(
                    stage="dedupe-race-detection",
                    message=exc.message,
                    command=exc.command,
                    output=exc.output,
                ) from exc
            raise

        if poll_verdict == "race":
            raise TransactionError(
                stage="dedupe-race-detection",
                message="duplicate open issue title collision after create",
                output=f"matching={matching_issue_numbers}",
            )

        if poll_verdict == "inconclusive":
            n_attempts = _DEFAULT_MAX_ATTEMPTS
            sys.stderr.write(
                f"[WARN] create_issue_txn: post-create race detection inconclusive after {n_attempts} attempts"
                f" (search index propagation delay)."
                f" issue=#{issue_number} title={title}."
                " Manual verification recommended.\n"
            )
            _observability_comment = (
                f"⚠️ create-issue transaction: post-create race detection was inconclusive"
                f" due to GitHub search index propagation delay (polled {n_attempts} attempts)."
                " 同タイトルの open issue が後から見えた場合は手動でクローズ判定してください。"
            )
            _obs_args = [
                gh_bin,
                "issue",
                "comment",
                "--repo",
                repo,
                str(issue_number),
                "--body",
                _observability_comment,
            ]
            run_command(_obs_args)  # best-effort; ignore failure
            completed.append("race-detection-inconclusive")

        _issue_apply_labels(repo, issue_number, labels, gh_bin)
        completed.append("label")

        if labels:
            labels_verified = _readback_labels(repo, issue_number, labels, gh_bin)
            if not labels_verified:
                raise TransactionError(stage="label-readback", message="label read-back mismatch")
            completed.append("label-readback")

        child_node_id = ""
        child_db_id: int | None = None

        if normalized_dependency_issue_numbers:
            child_node_id, child_db_id = _issue_graphql_ids(repo, issue_number, gh_bin)

        if parent_issue_number:
            if child_db_id is None:
                _, child_db_id = _issue_graphql_ids(repo, issue_number, gh_bin)
            _issue_register_sub_issue(repo, parent_issue_number, child_db_id, gh_bin)
            completed.append("sub_issue")

        dependency_registered = True
        if normalized_dependency_issue_numbers:
            dep_ids: list[str] = []
            for dependency in normalized_dependency_issue_numbers:
                dep_node_id, _ = _issue_graphql_ids(repo, dependency, gh_bin)
                dep_ids.append(dep_node_id)

            if len(dep_ids) != len(normalized_dependency_issue_numbers):
                raise TransactionError(stage="dependency-register", message="dependency id query mismatch")

            for dep_node_id in dep_ids:
                _issue_register_dependency(repo, child_node_id, dep_node_id, gh_bin)
            completed.append("dependency")

            dependency_registered = _readback_dependencies(
                repo,
                issue_number,
                normalized_dependency_issue_numbers,
                gh_bin,
            )
            if not dependency_registered:
                # Retry once after a brief delay (GitHub API propagation)
                sleep_fn(2.0)
                dependency_registered = _readback_dependencies(
                    repo,
                    issue_number,
                    normalized_dependency_issue_numbers,
                    gh_bin,
                )
            if not dependency_registered:
                raise TransactionError(stage="dependency-readback", message="dependency read-back mismatch")

            completed.append("dependency-readback")

        parent_verified = True
        if parent_issue_number:
            parent_verified = _readback_parent_issue(repo, issue_number, parent_issue_number, gh_bin)
            if not parent_verified:
                # Retry once after a brief delay (GitHub API propagation)
                sleep_fn(2.0)
                parent_verified = _readback_parent_issue(repo, issue_number, parent_issue_number, gh_bin)
            if not parent_verified:
                raise TransactionError(
                    stage="sub-issue-readback",
                    message="parent read-back mismatch",
                )
            completed.append("sub-issue-readback")

        return TransactionResult(
            status="success",
            issue_number=issue_number,
            issue_url=issue_url,
            completed_steps=completed,
            parent_verified=parent_verified if parent_issue_number else None,
            dependency_verified=dependency_registered if normalized_dependency_issue_numbers else None,
        )

    except TransactionError as exc:
        return _report_partial_failure(
            repo=repo,
            issue_number=issue_number,
            issue_url=issue_url,
            failed_exc=exc,
            completed_steps=completed,
            labels=labels,
            parent_issue_number=parent_issue_number,
            dependency_issue_numbers=normalized_dependency_issue_numbers,
            gh_bin=gh_bin,
            parent_verified=None,
            dependency_verified=None,
            failure_context="\n".join(
                [
                    f"matching_issue_numbers={matching_issue_numbers}",
                    f"issue_number={issue_number}",
                ]
            ),
        )


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    result = run_transaction(
        repo=args.repo,
        title=args.title,
        body=args.body,
        body_file=args.body_file,
        labels=args.label,
        parent_issue_number=args.parent_issue,
        dependency_issue_numbers=args.dependency,
        gh_bin=args.gh,
    )

    sys.stdout.write(f"{json.dumps(result.__dict__, ensure_ascii=False, sort_keys=True)}\n")
    if result.status in {"dedupe", "success"}:
        return 0
    if result.status == "partial_failure":
        return 2
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
