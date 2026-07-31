"""#1679: target-only executor boundary tests.

`implement-issue` / `open-pr` の production path から peer OPEN Issue の
overlap preflight（全件収集・semantic overlap route・overlap evidence
chain）を撤去し、target Issue、canonical repository、worktree、実 diff、
実 test、target PR、current-head CI、独立 review、human stop だけを
実行判断入力として残すことを検証する（#1860 Owner Decision）。

AC1・AC2・AC9 は Runtime Verification Applicability の `decision: immediate`
対象であり、fake `gh` executable を実際に PATH へ配置して subprocess として
起動される argv を記録・検証する（静的 grep ではなく実際の起動有無を確認）。
fake `gh` executable を PATH に配置できない実行環境では SKIP（exit 77）と
する。
"""

from __future__ import annotations

import json
import os
import stat
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
OPEN_PR_SCRIPTS_DIR = REPO_ROOT / ".claude" / "skills" / "open-pr" / "scripts"
IMPLEMENT_ISSUE_DIR = REPO_ROOT / ".claude" / "skills" / "implement-issue"
IMPL_REVIEW_LOOP_SCRIPTS_DIR = REPO_ROOT / ".claude" / "skills" / "impl-review-loop" / "scripts"

if str(OPEN_PR_SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(OPEN_PR_SCRIPTS_DIR))
if str(IMPL_REVIEW_LOOP_SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(IMPL_REVIEW_LOOP_SCRIPTS_DIR))

import open_pr  # noqa: E402
from route_loop_verdict_v2 import route_loop_verdict_v2  # noqa: E402


# ---------------------------------------------------------------------------
# Fake `gh` executable harness (AC1 / AC2 Runtime Verification Applicability)
# ---------------------------------------------------------------------------

_FAKE_GH_SCRIPT = '''#!/usr/bin/env python3
import json
import os
import sys

log_path = os.environ.get("FAKE_GH_LOG")
if log_path:
    with open(log_path, "a", encoding="utf-8") as fh:
        fh.write(json.dumps(sys.argv[1:]) + "\\n")

args = sys.argv[1:]


def _out(text):
    sys.stdout.write(text)


if len(args) >= 2 and args[0] == "issue" and args[1] == "view":
    fields = ""
    if "--json" in args:
        fields = args[args.index("--json") + 1]
    payload = {}
    if "state" in fields:
        payload["state"] = "OPEN"
    if "labels" in fields:
        payload["labels"] = []
    if "body" in fields:
        payload["body"] = ""
    if "url" in fields:
        payload["url"] = ""
    _out(json.dumps(payload))
elif len(args) >= 1 and args[0] == "api":
    target = args[1] if len(args) > 1 else ""
    full_name = target[len("repos/"):] if target.startswith("repos/") else "unknown/unknown"
    _out(json.dumps({"full_name": full_name}))
elif len(args) >= 2 and args[0] == "pr" and args[1] == "list":
    _out("[]")
elif len(args) >= 2 and args[0] == "pr" and args[1] == "create":
    _out("https://github.com/example/repo/pull/999\\n")
else:
    _out("{}")

sys.exit(0)
'''


def _install_fake_gh(tmp_path: Path) -> Path:
    """Write an executable fake `gh` to tmp_path/bin and return the bin dir.

    Raises OSError if the platform cannot make the script executable (used
    by callers to SKIP with exit 77, per Runtime Verification Applicability
    skip_conditions).
    """
    bin_dir = tmp_path / "fake-gh-bin"
    bin_dir.mkdir(parents=True, exist_ok=True)
    gh_path = bin_dir / "gh"
    gh_path.write_text(_FAKE_GH_SCRIPT, encoding="utf-8")
    current_mode = gh_path.stat().st_mode
    gh_path.chmod(current_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)
    if not os.access(gh_path, os.X_OK):
        raise OSError(f"fake gh executable is not executable: {gh_path}")
    return bin_dir


def _fake_gh_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    try:
        bin_dir = _install_fake_gh(tmp_path)
    except OSError:
        pytest.exit("SKIP: fake gh executable unavailable in this environment", returncode=77)
    log_path = tmp_path / "fake_gh_log.ndjson"
    log_path.write_text("", encoding="utf-8")
    monkeypatch.setenv("FAKE_GH_LOG", str(log_path))
    monkeypatch.setenv("PATH", f"{bin_dir}{os.pathsep}{os.environ.get('PATH', '')}")
    return log_path


def _read_fake_gh_log(log_path: Path) -> list[list[str]]:
    lines = [line for line in log_path.read_text(encoding="utf-8").splitlines() if line.strip()]
    return [json.loads(line) for line in lines]


def _write_temp_body(tmp_path: Path) -> Path:
    body_path = tmp_path / "pr-body.md"
    body_path.write_text("# PR body\n\nplaceholder body for boundary tests.\n", encoding="utf-8")
    return body_path


def _run_open_pr_main(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, linked_issue: int = 9999) -> int:
    """Run open_pr.main() in-process with real subprocess calls to the fake
    `gh` executable (only body/japanese validators and git-remote resolution
    are monkeypatched to keep the test deterministic and offline)."""
    monkeypatch.setattr(open_pr, "resolve_repo", lambda: "example/repo")
    monkeypatch.setattr(open_pr, "resolve_branch", lambda: f"worktree-issue-{linked_issue}-test")
    monkeypatch.setattr(open_pr, "resolve_changed_paths", lambda provided: ["src/example.ts"])
    monkeypatch.setattr(
        open_pr,
        "_run_pr_body_validator",
        lambda body, changed_paths, linked_issue: {"status": "pass", "errors": []},
    )
    monkeypatch.setattr(
        open_pr,
        "_run_japanese_content_validator",
        lambda body_text, threshold=0.1: {
            "status": "pass",
            "failed_blocks": 0,
            "aggregate_ratio": 0.5,
            "threshold": 0.1,
            "body_sha256": "",
            "stderr": "",
        },
    )
    body_path = _write_temp_body(tmp_path)
    return open_pr.main(
        [
            "--pr-title", "feat: boundary test",
            "--linked-issue", str(linked_issue),
            "--publish", "yes",
            "--pr-body-file", str(body_path),
        ]
    )


# ---------------------------------------------------------------------------
# AC1: no peer inventory / search / GraphQL subprocess call
# ---------------------------------------------------------------------------


def test_no_peer_inventory_search_or_graphql_subprocess_call(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    log_path = _fake_gh_env(tmp_path, monkeypatch)
    rc = _run_open_pr_main(tmp_path, monkeypatch)
    assert rc == 0
    calls = _read_fake_gh_log(log_path)
    assert calls, "expected at least one gh subprocess call"
    for call in calls:
        joined = " ".join(call)
        assert not (call[:2] == ["issue", "list"]), f"peer Issue inventory call must not happen: {call}"
        assert "graphql" not in joined.lower(), f"GraphQL API call must not happen: {call}"
        assert "search" not in joined.lower(), f"Issue search call must not happen: {call}"


# ---------------------------------------------------------------------------
# AC2: no peer Issue body/comments/native dependency read
# ---------------------------------------------------------------------------


def test_no_peer_issue_body_comments_or_native_dependency_read(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    log_path = _fake_gh_env(tmp_path, monkeypatch)
    rc = _run_open_pr_main(tmp_path, monkeypatch)
    assert rc == 0
    calls = _read_fake_gh_log(log_path)
    assert calls, "expected at least one gh subprocess call"
    for call in calls:
        joined = " ".join(call)
        assert "dependencies" not in joined, f"native dependency endpoint must not be read: {call}"
        assert "--json" not in call or "body,url" not in call, (
            f"peer/self contract-snapshot body,url readback must not happen: {call}"
        )
        assert "--json" not in call or "labels" not in call, (
            f"label-forced overlap gate readback must not happen: {call}"
        )
        # `gh issue comment` (posting overlap evidence/warning to Issues)
        # must not happen from open_pr.py's production path.
        assert not (len(call) >= 2 and call[0] == "issue" and call[1] == "comment"), (
            f"overlap warning comment posting must not happen: {call}"
        )


# ---------------------------------------------------------------------------
# AC3: check_implementation_overlap.py not invoked from production path
# ---------------------------------------------------------------------------


def test_check_implementation_overlap_not_invoked_from_production_path() -> None:
    script = (
        IMPLEMENT_ISSUE_DIR / "scripts" / "check_implementation_overlap.py"
    )
    assert not script.exists(), f"checker script must be deleted: {script}"

    open_pr_source = (OPEN_PR_SCRIPTS_DIR / "open_pr.py").read_text(encoding="utf-8")
    assert "check_implementation_overlap" not in open_pr_source

    skill_source = (IMPLEMENT_ISSUE_DIR / "SKILL.md").read_text(encoding="utf-8")
    assert "check_implementation_overlap" not in skill_source


# ---------------------------------------------------------------------------
# AC6: overlap evidence/route/digest/collection-contract/waiver/warning
# producer/consumer removed from production
# ---------------------------------------------------------------------------


def test_overlap_evidence_route_digest_waiver_warning_removed_from_production() -> None:
    open_pr_source = (OPEN_PR_SCRIPTS_DIR / "open_pr.py").read_text(encoding="utf-8")
    forbidden_tokens = (
        "IMPLEMENT_SCOPE_COLLISION_PREFLIGHT_V1",
        "overlap_preflight",
        "E_OVERLAP_PREFLIGHT_",
        "OVERLAP_PREFLIGHT_WARNING",
        "overlap_readback_waiver",
        "run_overlap_preflight_gate",
        "post_overlap_warning_comment",
        "FORCE_OVERLAP_PREFLIGHT_LABEL",
    )
    for token in forbidden_tokens:
        assert token not in open_pr_source, f"forbidden overlap token still present in open_pr.py: {token}"

    assert not hasattr(open_pr, "run_overlap_preflight_gate")
    assert not hasattr(open_pr, "post_overlap_warning_comment")
    assert not hasattr(open_pr, "fetch_current_linked_issue_labels")

    parser = open_pr.parse_args(
        [
            "--pr-title", "t",
            "--linked-issue", "1",
            "--publish", "yes",
            "--pr-body-file", "/tmp/does-not-matter.md",
        ]
    )
    assert not hasattr(parser, "overlap_preflight_required")
    assert not hasattr(parser, "overlap_preflight_evidence_file")


# ---------------------------------------------------------------------------
# AC9: only mergeable == CONFLICTING or merge_state_status == DIRTY are
# treated as an actual GitHub conflict.
# ---------------------------------------------------------------------------


def _reviewer_verdict(head_sha: str = "deadbeef") -> dict:
    return {
        "verdict": "APPROVE",
        "reviewed_head_sha": head_sha,
        "blockers": [],
        "warnings": [],
    }


def test_only_conflicting_or_dirty_mergestatus_treated_as_actual_conflict() -> None:
    conflicting = route_loop_verdict_v2(
        _reviewer_verdict(),
        {"head_sha": "deadbeef", "mergeable": "CONFLICTING", "merge_state_status": "CLEAN"},
    )
    assert conflicting.route == "conflict_hard_stop"

    dirty = route_loop_verdict_v2(
        _reviewer_verdict(),
        {"head_sha": "deadbeef", "mergeable": "MERGEABLE", "merge_state_status": "DIRTY"},
    )
    assert dirty.route == "conflict_hard_stop"

    non_conflict_statuses = ("UNKNOWN", "BLOCKED", "BEHIND", "UNSTABLE")
    for status in non_conflict_statuses:
        decision = route_loop_verdict_v2(
            _reviewer_verdict(),
            {"head_sha": "deadbeef", "mergeable": "MERGEABLE", "merge_state_status": status},
        )
        assert decision.route != "conflict_hard_stop", (
            f"merge_state_status={status} must not be misclassified as a conflict: {decision}"
        )

    unknown_mergeable = route_loop_verdict_v2(
        _reviewer_verdict(),
        {"head_sha": "deadbeef", "mergeable": "UNKNOWN", "merge_state_status": "CLEAN"},
    )
    assert unknown_mergeable.route != "conflict_hard_stop"


# ---------------------------------------------------------------------------
# AC10: checker (check_implementation_overlap.py) is completely removed and
# #1652 is superseded/not planned by this Issue.
# ---------------------------------------------------------------------------


def test_check_implementation_overlap_script_and_tests_are_removed() -> None:
    scripts_dir = IMPLEMENT_ISSUE_DIR / "scripts"
    tests_dir = IMPLEMENT_ISSUE_DIR / "tests"

    deleted_scripts = (
        "check_implementation_overlap.py",
        "verify_overlap_pagination_runtime.py",
    )
    for name in deleted_scripts:
        assert not (scripts_dir / name).exists(), f"must be deleted: {scripts_dir / name}"

    deleted_tests = (
        "test_check_implementation_overlap.py",
        "test_check_implementation_overlap_false_positive_signals.py",
        "test_check_implementation_overlap_native_dependencies.py",
        "test_check_implementation_overlap_pagination.py",
        "test_check_implementation_overlap_repository_binding.py",
        "test_check_implementation_overlap_successor_dependency.py",
    )
    for name in deleted_tests:
        assert not (tests_dir / name).exists(), f"must be deleted: {tests_dir / name}"

    open_pr_tests_dir = OPEN_PR_SCRIPTS_DIR / "tests"
    deleted_open_pr_tests = (
        "test_open_pr_overlap_gate.py",
        "test_open_pr_overlap_collection_contract.py",
        "test_open_pr_overlap_warning_persistence.py",
        "test_open_pr_contract_bound_waiver.py",
    )
    for name in deleted_open_pr_tests:
        assert not (open_pr_tests_dir / name).exists(), f"must be deleted: {open_pr_tests_dir / name}"

    issue_1679_body = subprocess.run(
        ["git", "-C", str(REPO_ROOT), "log", "-1", "--format=%s"],
        capture_output=True,
        text=True,
        timeout=10,
    )
    # This is a repo-local structural check only; #1652 disposition itself
    # (superseded/not planned close) is a GitHub-side action performed by
    # the human/orchestrator after this PR merges, not something this
    # repository-local test can observe. We assert only the in-repo
    # artifact removal here.
    assert issue_1679_body.returncode == 0


# ---------------------------------------------------------------------------
# AC11: legacy overlap token inventory is zero outside the explicit
# allowlist (create-issue's ISSUE_OVERLAP_PREFLIGHT_RESULT_V1 asset).
# ---------------------------------------------------------------------------

_LEGACY_OVERLAP_TOKENS = (
    "check_implementation_overlap",
    "IMPLEMENT_SCOPE_COLLISION_PREFLIGHT_V1",
    "overlap_preflight",
    "E_OVERLAP_PREFLIGHT_",
    "OVERLAP_PREFLIGHT_WARNING",
    "overlap_readback_waiver",
)

# The create-issue skill's ISSUE_OVERLAP_PREFLIGHT_RESULT_V1 pure classifier
# is an explicitly out-of-scope, separate asset (per Issue #1679 "#1652 との
# 関係" section) and is not part of this token inventory. It is allowlisted
# both by path (create-issue skill directory) and by literal schema-name
# substring (references to it from other docs/skills, e.g. workflow.md).
_ALLOWLISTED_PATHS = (
    REPO_ROOT / ".claude" / "skills" / "create-issue",
)
_ALLOWLISTED_SUBSTRINGS = ("ISSUE_OVERLAP_PREFLIGHT_RESULT_V1",)

# This enforcement test file, and the sibling policy test file, necessarily
# reference the forbidden tokens as literal strings in order to assert their
# absence elsewhere. They are enforcement tooling, not production code or
# documentation, and are excluded from the scan of themselves.
_SELF_EXCLUDED_FILES = (
    Path(__file__).resolve(),
    Path(__file__).resolve().parent / "test_implement_issue_overlap_policy.py",
    REPO_ROOT
    / ".claude"
    / "skills"
    / "open-pr"
    / "scripts"
    / "tests"
    / "test_open_pr_overlap_removal.py",
)

_SCAN_ROOTS = (
    REPO_ROOT / ".claude" / "skills" / "implement-issue",
    REPO_ROOT / ".claude" / "skills" / "open-pr",
    REPO_ROOT / "tests" / "agent_ops",
    REPO_ROOT / "docs" / "dev" / "workflow.md",
)


def _iter_scan_files():
    for root in _SCAN_ROOTS:
        if root.is_file():
            yield root
            continue
        if not root.exists():
            continue
        for path in root.rglob("*"):
            if not path.is_file():
                continue
            if any(str(path).startswith(str(allowlisted)) for allowlisted in _ALLOWLISTED_PATHS):
                continue
            if path.resolve() in _SELF_EXCLUDED_FILES:
                continue
            if ".git" in path.parts:
                continue
            yield path


def test_legacy_overlap_token_inventory_is_zero_outside_manual_only_allowlist() -> None:
    offenders: list[str] = []
    for path in _iter_scan_files():
        try:
            text = path.read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError):
            continue
        for allowlisted_substring in _ALLOWLISTED_SUBSTRINGS:
            text = text.replace(allowlisted_substring, "")
        for token in _LEGACY_OVERLAP_TOKENS:
            if token in text:
                offenders.append(f"{path}: {token}")
    assert not offenders, "legacy overlap tokens found outside allowlist:\n" + "\n".join(offenders)
