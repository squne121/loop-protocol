#!/usr/bin/env python3
"""
ci_verdict_summary.py — CI checks を compact verdict に変換する CLI script。

PR 番号・expected head SHA・任意のチェック名を入力に CI checks を
CI_VERDICT_SUMMARY_V1 schema の compact JSON として stdout に出力する。

exit codes:
  0: all_pass
  10: failed / no_required_evidence
  20: pending_or_queued
  30: stale_head_sha
  40: gh_error

優先順位: stale_head_sha > gh_error > pending_or_queued > failed > all_pass
"""

import argparse
import hashlib
import json
import re
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

# Conclusion → bucket mapping
CONCLUSION_BUCKET: dict[str, str] = {
    "success": "pass",
    "failure": "fail",
    "timed_out": "fail",
    "cancelled": "cancel",
    "action_required": "fail",
    "neutral": "fail",   # approve evidence として不可
    "skipped": "fail",   # approve evidence として不可
    "stale": "fail",     # approve evidence として不可
}

# Status → pending indicator
PENDING_STATUSES: set[str] = {
    "queued",
    "in_progress",
    "waiting",
    "requested",
    "pending",
}

# Exit codes
EXIT_ALL_PASS = 0
EXIT_FAILED = 10
EXIT_NO_REQUIRED_EVIDENCE = 10  # same exit as failed — no required CI evidence
EXIT_PENDING = 20
EXIT_STALE = 30
EXIT_GH_ERROR = 40

# Allowlist: (workflow_name, check_name) tuples for head_sha=None + skipped exclusion.
# These checks are conditional/retrospective and are excluded from verdict calculation
# when they have head_sha=None and conclusion=skipped.
# workflow names correspond to .github/workflows/ file `name:` fields.
HEAD_SHA_NULL_SKIPPED_EXCLUDE_RULES: frozenset[tuple[str, str]] = frozenset({
    ("deploy-pages", "deploy-main"),
    ("deploy-pages", "cleanup-pr"),
    ("agent-retro-index", "build-index"),
    ("agent-retro-index", "upsert-parent-comment"),
    ("Check Japanese Content", "Issue Body Japanese Check (retrospective)"),
    ("Check Japanese Content", "Issue Comment Japanese Check (retrospective)"),
    ("Check Japanese Content", "PR Review Japanese Check (retrospective)"),
})

# Artifact truncation limit (bytes)
LOG_TRUNCATE_BYTES = 64 * 1024  # 64KB


def sanitize_check_name(name: str) -> str:
    """Path traversal 不可な safe ファイル名に変換する。"""
    # Replace path separators and dangerous chars
    safe = re.sub(r"[/\\:*?\"<>|]", "_", name)
    # Remove dot-dot sequences (path traversal prevention)
    safe = re.sub(r"\.\.+", "_", safe)
    # Remove leading dots (hidden files / relative path tricks)
    safe = re.sub(r"^\.+", "", safe)
    # Collapse multiple underscores
    safe = re.sub(r"_+", "_", safe)
    # Trim to 128 chars
    safe = safe[:128].strip("_") or "unnamed"
    return safe


def classify_gh_error(stderr: str) -> str:
    """
    B6: 共通 gh エラー分類関数。
    fetch_head_sha, fetch_checks, fetch_run_details, fetch_job_log で統一使用する。
    """
    s = stderr.lower()
    if "unauthorized" in s or "authentication" in s or "credentials" in s:
        return "auth_failed"
    if "403" in s or "permission" in s or "forbidden" in s:
        return "permission_denied"
    if "rate limit" in s or "429" in s:
        return "rate_limited"
    if "404" in s or "not found" in s:
        return "not_found"
    if "json" in s or "parse" in s or "decode" in s:
        return "json_parse_error"
    return "gh_other_error"


def run_gh(args: list[str]) -> tuple[bool, Any, str]:
    """gh コマンドを実行し (success, parsed_json_or_None, raw_text) を返す。"""
    try:
        result = subprocess.run(
            ["gh"] + args,
            capture_output=True,
            text=True,
            timeout=60,
        )
        if result.returncode != 0:
            detail = (result.stderr or result.stdout or "").strip()
            return False, None, detail[:1024]
        text = result.stdout.strip()
        try:
            return True, json.loads(text), text
        except json.JSONDecodeError:
            return True, None, text
    except subprocess.TimeoutExpired:
        return False, None, "gh command timed out"
    except FileNotFoundError:
        return False, None, "gh not found in PATH"
    except Exception as e:
        return False, None, str(e)[:512]


def get_repo_root() -> Path:
    """B5: git rev-parse --show-toplevel で repo root を取得する。"""
    try:
        result = subprocess.check_output(
            ["git", "rev-parse", "--show-toplevel"],
            text=True,
            stderr=subprocess.DEVNULL,
        )
        return Path(result.strip())
    except Exception:
        # fallback to cwd if git is not available (e.g., in tests)
        return Path.cwd()


def fetch_head_sha(pr_number: int, repo: str) -> tuple[Optional[str], Optional[dict]]:
    """PR の headRefOid を取得する。失敗時は (None, error_entry)。"""
    ok, data, raw = run_gh([
        "pr", "view", str(pr_number),
        "--repo", repo,
        "--json", "headRefOid",
    ])
    if not ok or data is None:
        # B6: 共通エラー分類を使用
        kind = classify_gh_error(raw)
        return None, {"kind": kind, "detail": f"gh pr view headRefOid failed: {raw}"}
    head = data.get("headRefOid")
    if not head:
        return None, {"kind": "gh_other_error", "detail": "headRefOid missing in gh pr view output"}
    return head, None


def fetch_checks(pr_number: int, repo: str) -> tuple[Optional[list], Optional[dict]]:
    """gh pr checks を取得する。失敗時は (None, error_entry)。"""
    ok, data, raw = run_gh([
        "pr", "checks", str(pr_number),
        "--repo", repo,
        "--json", "bucket,name,state,workflow,link,event,startedAt,completedAt",
    ])
    if not ok:
        # B6: 共通エラー分類を使用
        kind = classify_gh_error(raw)
        return None, {"kind": kind, "detail": raw[:512]}
    if data is None:
        # gh pr checks は JSON array ではなく table 形式を返す場合がある
        return None, {"kind": "json_parse_error", "detail": f"gh pr checks output: {raw[:256]}"}
    if not isinstance(data, list):
        return None, {"kind": "json_parse_error", "detail": f"expected list, got {type(data).__name__}"}
    return data, None


def fetch_run_details(run_id: int, repo: str) -> tuple[Optional[dict], Optional[dict]]:
    """gh run view で run の詳細（headSha / conclusion / jobs）を取得する。"""
    ok, data, raw = run_gh([
        "run", "view", str(run_id),
        "--repo", repo,
        "--json", "headSha,conclusion,status,workflowName,jobs,databaseId",
    ])
    if not ok or data is None:
        # B6: 共通エラー分類を使用
        kind = classify_gh_error(raw)
        return None, {"kind": kind, "detail": f"gh run view {run_id}: {raw}"}
    return data, None


def fetch_job_log(job_id: int, repo: str) -> tuple[Optional[str], Optional[dict]]:
    """gh run view --log でジョブログを取得する（raw text）。"""
    ok, _, raw = run_gh([
        "run", "view",
        "--repo", repo,
        "--job", str(job_id),
        "--log",
    ])
    if not ok:
        # B6: 共通エラー分類を使用
        kind = classify_gh_error(raw)
        return None, {"kind": "log_fetch_error", "detail": f"gh run view --log job {job_id}: {raw}", "gh_kind": kind}
    return raw, None


def extract_run_id_from_link(link: str) -> Optional[int]:
    """gh pr checks の link フィールドから run_id を抽出する。"""
    if not link:
        return None
    # https://github.com/{owner}/{repo}/actions/runs/{run_id}
    m = re.search(r"/actions/runs/(\d+)", link)
    if m:
        return int(m.group(1))
    return None


def classify_check(check: dict, pr_head_sha: str) -> dict:
    """
    gh pr checks の1エントリを解析し、check_entry dict を返す。

    check は以下のキーを持つ（gh pr checks --json 出力）:
      bucket, name, state, workflow, link, event, startedAt, completedAt
    """
    name = check.get("name") or "unknown"
    bucket = check.get("bucket")          # pass | fail | pending | skipping | cancel | null
    state = check.get("state")            # gh pr checks の state フィールド（bucket 別名的）
    workflow = check.get("workflow") or None
    link = check.get("link") or None
    started_at = check.get("startedAt") or None
    completed_at = check.get("completedAt") or None

    # run_id は link から抽出
    run_id = extract_run_id_from_link(link) if link else None

    # bucket → conclusion / status mapping
    # gh pr checks の bucket: pass / fail / pending / skipping / cancel / null
    conclusion: Optional[str] = None
    status: Optional[str] = None
    check_head_sha: Optional[str] = None

    if bucket == "pass":
        conclusion = "success"
        status = "completed"
    elif bucket == "fail":
        conclusion = "failure"
        status = "completed"
    elif bucket == "cancel":
        conclusion = "cancelled"
        status = "completed"
    elif bucket == "skipping":
        conclusion = "skipped"
        status = "completed"
    elif bucket == "pending":
        status = "in_progress"
    # null bucket → unknown, will rely on run details

    entry: dict[str, Any] = {
        "name": name,
        "workflow": workflow,
        "state": state,
        "bucket": bucket,
        "status": status,
        "conclusion": conclusion,
        "head_sha": check_head_sha,
        "run_id": run_id,
        "job_id": None,
        "details_url": link,
        "started_at": started_at,
        "completed_at": completed_at,
    }
    return entry


def determine_check_verdict(entry: dict, pr_head_sha: str) -> str:
    """
    check entry から verdict bucket を決定する。
    returns: "all_pass" | "failed" | "pending_or_queued" | "stale_head_sha" | "excluded"
    """
    # stale: head SHA mismatch
    head_sha = entry.get("head_sha")
    if head_sha and head_sha != pr_head_sha:
        return "stale_head_sha"

    bucket = entry.get("bucket")
    conclusion = entry.get("conclusion")
    status = entry.get("status")
    name = entry.get("name") or ""
    workflow = entry.get("workflow") or ""

    # Allowlist exclusion: head_sha=None + conclusion=skipped + (workflow, name) in rules
    # These are conditional/retrospective checks that do not run on PR commits.
    if head_sha is None and conclusion == "skipped":
        if (workflow, name) in HEAD_SHA_NULL_SKIPPED_EXCLUDE_RULES:
            return "excluded"

    # pending
    if bucket == "pending" or status in PENDING_STATUSES:
        return "pending_or_queued"

    # conclusion-based
    if conclusion == "success":
        return "all_pass"
    if conclusion in ("failure", "timed_out", "action_required", "neutral", "skipped", "stale"):
        return "failed"
    if conclusion == "cancelled":
        return "failed"

    # bucket fallback
    if bucket == "pass":
        return "all_pass"
    if bucket in ("fail", "cancel", "skipping"):
        return "failed"

    # null / unknown → treat as pending
    return "pending_or_queued"


def compute_overall_status(verdicts: list[str]) -> str:
    """優先順位: stale_head_sha > gh_error > pending_or_queued > failed > all_pass > no_required_evidence"""
    if "stale_head_sha" in verdicts:
        return "stale_head_sha"
    if "gh_error" in verdicts:
        return "gh_error"
    if "pending_or_queued" in verdicts:
        return "pending_or_queued"
    if "failed" in verdicts:
        return "failed"

    # Filter out excluded verdicts to determine effective evidence
    effective = [v for v in verdicts if v != "excluded"]
    if not effective and verdicts:
        # All checks are excluded — no required CI evidence to confirm pass
        return "no_required_evidence"

    return "all_pass"


def next_action_for(status: str) -> str:
    mapping = {
        "all_pass": "none",
        "failed": "inspect_failed_log_artifacts",
        "no_required_evidence": "inspect_failed_log_artifacts",
        "pending_or_queued": "wait_for_ci",
        "stale_head_sha": "refresh_head_sha",
        "gh_error": "manual_review_gh_error",
    }
    return mapping.get(status, "manual_review_gh_error")


def save_log_artifact(
    log_text: str,
    pr_number: int,
    head_sha: str,
    check_name: str,
    job_id: Optional[int],
    artifacts_base: Path,
) -> dict:
    """ログを artifact ファイルに保存し log_artifacts エントリを返す。"""
    safe_name = sanitize_check_name(check_name)
    artifact_dir = artifacts_base / "ci-verdict" / f"pr-{pr_number}" / f"head-{head_sha}"
    artifact_dir.mkdir(parents=True, exist_ok=True)
    artifact_path = artifact_dir / f"{safe_name}.log"

    raw_bytes = log_text.encode("utf-8", errors="replace")
    truncated = len(raw_bytes) > LOG_TRUNCATE_BYTES
    content_bytes = raw_bytes[:LOG_TRUNCATE_BYTES] if truncated else raw_bytes

    artifact_path.write_bytes(content_bytes)

    sha256 = hashlib.sha256(content_bytes).hexdigest()

    # B5: path は repo-root 相対の文字列で返す
    try:
        repo_root = get_repo_root()
        relative_path = artifact_path.relative_to(repo_root)
        path_str = str(relative_path)
    except ValueError:
        # artifact_path が repo_root 配下でない場合は絶対パスを使用
        path_str = str(artifact_path)

    return {
        "check_name": check_name,
        "job_id": job_id,
        "path": path_str,
        "sha256": f"sha256:{sha256}",
        "bytes": len(content_bytes),
        "truncated": truncated,
    }


def find_failed_job_id(jobs: list[dict], check_name: Optional[str]) -> Optional[int]:
    """
    B3: jobs リストから適切な job_id を取得する。
    jobs[0] を盲目的に使わず、failed job（conclusion != success）を優先する。
    check_name との対応が取れる場合はそれを優先する。
    """
    if not jobs:
        return None

    # まず check_name と一致する job を探す
    if check_name:
        for job in jobs:
            job_name = job.get("name") or ""
            if job_name == check_name or check_name in job_name:
                return job.get("databaseId")

    # 次に failed job を探す
    for job in jobs:
        conclusion = job.get("conclusion") or ""
        if conclusion and conclusion != "success":
            return job.get("databaseId")

    # fallback: first job
    return jobs[0].get("databaseId") if jobs else None


def main() -> int:
    parser = argparse.ArgumentParser(
        description="CI checks を CI_VERDICT_SUMMARY_V1 compact JSON に変換する"
    )
    parser.add_argument("--pr", type=int, required=True, help="PR 番号")
    parser.add_argument("--repo", type=str, required=True, help="owner/repo")
    parser.add_argument("--expected-head-sha", type=str, required=True, help="期待する HEAD SHA")
    parser.add_argument("--check-name", type=str, default=None, help="exact match フィルタ")
    parser.add_argument(
        "--include-log-excerpt",
        action="store_true",
        help="artifact materialization のみ（stdout に raw log 出力禁止）",
    )

    args = parser.parse_args()

    pr_number: int = args.pr
    repo: str = args.repo
    expected_head_sha: str = args.expected_head_sha
    check_name_filter: Optional[str] = args.check_name
    include_log_excerpt: bool = args.include_log_excerpt

    generated_at = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
    errors: list[dict] = []
    log_artifacts: list[dict] = []

    # Step 1: PR headRefOid を確定
    head_sha, head_err = fetch_head_sha(pr_number, repo)
    if head_err:
        errors.append(head_err)

    # stale check: expected vs actual head
    stale_from_head = False
    if head_sha and head_sha != expected_head_sha:
        stale_from_head = True

    # Step 2: gh pr checks
    raw_checks, checks_err = [], None
    if head_sha is not None:
        raw_checks, checks_err = fetch_checks(pr_number, repo)
        if checks_err:
            errors.append(checks_err)
            raw_checks = []
    elif not errors:
        # head_sha missing but no error recorded — shouldn't happen, safeguard
        errors.append({"kind": "gh_other_error", "detail": "head_sha unavailable"})

    # Step 3: classify checks
    check_entries: list[dict] = []
    verdicts: list[str] = []

    if stale_from_head:
        verdicts.append("stale_head_sha")

    if errors:
        verdicts.append("gh_error")

    # B1: --check-name フィルタ: 0件 → gh_error、複数件 → gh_error
    if check_name_filter and raw_checks is not None:
        matched = [raw for raw in raw_checks if (raw.get("name") or "unknown") == check_name_filter]
        if len(matched) == 0:
            errors.append({"kind": "check_not_found", "detail": f"check-name not found: {check_name_filter}"})
            verdicts.append("gh_error")
            raw_checks = []
        elif len(matched) > 1:
            errors.append({(
                "kind"
            ): "ambiguous_check_name", "detail": f"check-name matched multiple checks: {check_name_filter}"})
            verdicts.append("gh_error")
            raw_checks = []
        else:
            raw_checks = matched

    for raw in (raw_checks or []):
        entry = classify_check(raw, head_sha or expected_head_sha)

        # 失敗・pending の場合、または expected_head_sha 指定時の pass check は run details を補完
        bucket = entry.get("bucket")
        # B2: pass check で expected_head_sha が指定されている場合も補完して head SHA 確認
        needs_details = (
            bucket in ("fail", "pending", None)
            or (bucket == "pass" and expected_head_sha is not None)
        )
        if needs_details and entry["run_id"] is not None:
            run_data, run_err = fetch_run_details(entry["run_id"], repo)
            if run_err:
                errors.append(run_err)
                verdicts.append("gh_error")
                # B2: pass check で補完失敗 → all_pass の根拠にしない → pending_or_queued 扱い
                if bucket == "pass":
                    entry["bucket"] = None
                    entry["conclusion"] = None
                    entry["status"] = "unknown"
            else:
                # Update head_sha from run
                run_head = run_data.get("headSha")
                if run_head:
                    entry["head_sha"] = run_head
                # Update conclusion if available
                run_conclusion = run_data.get("conclusion")
                if run_conclusion and entry["conclusion"] is None:
                    entry["conclusion"] = run_conclusion
                    mapped_bucket = CONCLUSION_BUCKET.get(run_conclusion)
                    if mapped_bucket:
                        entry["bucket"] = mapped_bucket
                run_status = run_data.get("status")
                if run_status and entry["status"] is None:
                    entry["status"] = run_status
                # B3: Extract job_id — 失敗 job を特定する（jobs[0] を盲目的に使わない）
                jobs = run_data.get("jobs") or []
                if jobs:
                    entry["job_id"] = find_failed_job_id(jobs, entry["name"])

        verdict = determine_check_verdict(entry, head_sha or expected_head_sha)
        verdicts.append(verdict)

        check_entries.append(entry)

        # log artifact: failed check のみ、--include-log-excerpt 指定時
        if include_log_excerpt and verdict == "failed" and entry.get("job_id"):
            log_text, log_err = fetch_job_log(entry["job_id"], repo)
            if log_err:
                # B4: ログ取得失敗時は errors に記録し next_action を manual_review_gh_error にする
                errors.append(log_err)
                verdicts.append("gh_error")
            elif log_text:
                # B5: repo root から絶対パスで artifacts ディレクトリを作成
                repo_root = get_repo_root()
                artifacts_base = repo_root / "artifacts"
                artifacts_base.mkdir(exist_ok=True)
                artifact_entry = save_log_artifact(
                    log_text,
                    pr_number,
                    head_sha or expected_head_sha,
                    entry["name"],
                    entry["job_id"],
                    artifacts_base,
                )
                log_artifacts.append(artifact_entry)

    # Overall status
    if not verdicts:
        # No checks found (check_name_filter が一致しない場合は B1 で gh_error 処理済み)
        # verdicts が空 = フィルタなしで checks も 0 件 → 証跡なし → all_pass
        overall_status = "all_pass"
    else:
        overall_status = compute_overall_status(verdicts)

    # Build categorized lists
    effective_sha = head_sha or expected_head_sha
    failed_checks = [e["name"] for e in check_entries if determine_check_verdict(e, effective_sha) == "failed"]
    pending_checks = [e["name"] for e in check_entries if determine_check_verdict(
        e,
        effective_sha
    ) == "pending_or_queued"]
    stale_checks = [e["name"] for e in check_entries if determine_check_verdict(e, effective_sha) == "stale_head_sha"]
    excluded_checks = [e["name"] for e in check_entries if determine_check_verdict(e, effective_sha) == "excluded"]

    summary: dict[str, Any] = {
        "schema": "CI_VERDICT_SUMMARY_V1",
        "generated_at": generated_at,
        "repo": repo,
        "pr": pr_number,
        "expected_head_sha": expected_head_sha,
        "head_sha": head_sha or "",
        "status": overall_status,
        "checks": check_entries,
        "failed_checks": failed_checks,
        "pending_checks": pending_checks,
        "stale_checks": stale_checks,
        "excluded_checks": excluded_checks,
        "excluded_count": len(excluded_checks),
        "log_artifacts": log_artifacts,
        "errors": errors,
        "next_action": next_action_for(overall_status),
    }

    print(json.dumps(summary, indent=2, ensure_ascii=False))

    # exit code
    exit_map = {
        "all_pass": EXIT_ALL_PASS,
        "failed": EXIT_FAILED,
        "no_required_evidence": EXIT_NO_REQUIRED_EVIDENCE,
        "pending_or_queued": EXIT_PENDING,
        "stale_head_sha": EXIT_STALE,
        "gh_error": EXIT_GH_ERROR,
    }
    return exit_map.get(overall_status, EXIT_GH_ERROR)


if __name__ == "__main__":
    sys.exit(main())
