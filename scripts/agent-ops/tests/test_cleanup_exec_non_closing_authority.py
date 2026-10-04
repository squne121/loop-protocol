"""scripts/agent-ops/tests/test_cleanup_exec_non_closing_authority.py

Issue #2891: ``cleanup_exec`` linked-Issue authorization accepts an
orchestrator-attested ``non_closing_authority`` (the 7-key contract fixed by
#2878) for a ``Refs``-bound PR, delivered through the optional
``--non-closing-authority-file`` CLI argument.

Every behavioral test below drives the PRODUCTION CLI (``cleanup_exec.py``) via a real
``subprocess`` against a disposable fixture git repository and a fake ``gh``
placed on ``PATH``. The argv is mechanically extracted from the production
command block of ``.claude/skills/post-merge-cleanup-executor/SKILL.md`` so the
Skill and the CLI cannot drift apart; injecting authority into a Python ``req``
dict never stands in for that path (the only in-process calls are the explicitly
function-level checks: twin parity, discard lane, fall-through unit checks).

The fake ``gh`` is stateful: it records every invocation and can switch the PR
body per ``pr view`` fetch, so a same-invocation re-authorization that re-reads
the PR body can be exercised and its second fetch observed.
"""

from __future__ import annotations

import hashlib
import importlib.util
import inspect
import json
import os
import re
import shlex
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[3]
AGENT_OPS_DIR = REPO_ROOT / "scripts" / "agent-ops"
CLEANUP_EXEC = AGENT_OPS_DIR / "cleanup_exec.py"
EXECUTOR_SKILL = REPO_ROOT / ".claude" / "skills" / "post-merge-cleanup-executor" / "SKILL.md"
OPEN_PR_SCRIPT = REPO_ROOT / ".claude" / "skills" / "open-pr" / "scripts" / "open_pr.py"


def _load_module(unique_name: str, path: Path):
    """Load a module under a UNIQUE name (a bare ``import cleanup_exec`` / ``import open_pr``
    could collide with a same-named module cached by another test in a shared pytest session)."""
    if str(path.parent) not in sys.path:
        sys.path.insert(0, str(path.parent))
    spec = importlib.util.spec_from_file_location(unique_name, str(path))
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[unique_name] = module
    spec.loader.exec_module(module)
    return module


cleanup_exec = _load_module("cleanup_exec_issue_2891", CLEANUP_EXEC)
open_pr = _load_module("open_pr_issue_2891", OPEN_PR_SCRIPT)
from worktree_catalog import Deadline  # noqa: E402  (resolved through the agent-ops sys.path entry)

ISSUE = 2891
PR_NUMBER = 2950
REPO_LOWER = "squne121/loop-protocol"
PR_BODY = "## 概要\n\nRefs #2891\n\n通常の本文。\n"
LINKED_MISMATCH = "linked_issue_mismatch"
AUTHORITY_KEYS = ("decision", "level", "reason_code", "repo", "issue_number", "pr_number", "pr_body_sha256")


# --- stateful fake gh ----------------------------------------------------------------------
_FAKE_GH_SCRIPT = '''#!/usr/bin/env python3
import json
import os
import sys


def main() -> int:
    with open(os.environ["FAKE_GH_DATA"], "r", encoding="utf-8") as fh:
        data = json.load(fh)
    log_path = os.environ["FAKE_GH_LOG"]
    argv = sys.argv[1:]
    prior = []
    if os.path.exists(log_path):
        with open(log_path, "r", encoding="utf-8") as fh:
            prior = [json.loads(line) for line in fh if line.strip()]
    with open(log_path, "a", encoding="utf-8") as fh:
        fh.write(json.dumps(argv) + "\\n")
    if argv[:2] == ["repo", "view"]:
        sys.stdout.write(data["repo"] + "\\n")
        return 0
    if argv[:2] == ["pr", "view"]:
        fetch_index = sum(1 for entry in prior if entry[:2] == ["pr", "view"])
        pr = dict(data["pr"])
        bodies = data.get("pr_bodies")
        if bodies is not None:
            pr["body"] = bodies[min(fetch_index, len(bodies) - 1)]
        if data.get("pr_omit_body"):
            pr.pop("body", None)
        sys.stdout.write(json.dumps(pr) + "\\n")
        return 0
    if argv[:2] == ["issue", "view"]:
        issue = data.get("issue")
        if issue is None:
            sys.stderr.write("fake gh: issue view failed\\n")
            return 1
        sys.stdout.write(json.dumps(issue) + "\\n")
        return 0
    sys.stderr.write("fake gh: unsupported invocation: " + " ".join(argv) + "\\n")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
'''


def _git(*args: str, cwd: Path) -> str:
    result = subprocess.run(["git", *args], cwd=str(cwd), capture_output=True, text=True)
    if result.returncode != 0:
        raise RuntimeError(f"git {args} failed: {result.stderr}")
    return result.stdout.strip()


def _rev_parse(cwd: Path, ref: str) -> str:
    return _git("rev-parse", ref, cwd=cwd)


def _branch_exists(root: Path, branch: str) -> bool:
    return subprocess.run(
        ["git", "-C", str(root), "show-ref", "--verify", "--quiet", f"refs/heads/{branch}"]
    ).returncode == 0


def _init_repo(root: Path) -> None:
    root.mkdir(parents=True)
    _git("init", "-q", "-b", "main", cwd=root)
    _git("config", "user.email", "t@t.com", cwd=root)
    _git("config", "user.name", "T", cwd=root)
    _git("remote", "add", "origin", "https://github.com/squne121/loop-protocol.git", cwd=root)
    (root / "README.md").write_text("seed\n", encoding="utf-8")
    _git("add", "README.md", cwd=root)
    _git("commit", "-q", "-m", "seed", cwd=root)


@dataclass
class Scenario:
    root: Path
    branch: str
    worktree: Path
    env: dict
    data_path: Path
    log_path: Path
    head_oid: str

    def write_gh_data(self, **overrides) -> None:
        data = json.loads(self.data_path.read_text(encoding="utf-8"))
        data.update(overrides)
        self.data_path.write_text(json.dumps(data), encoding="utf-8")

    def gh_calls(self, subcommand: str) -> list[list[str]]:
        if not self.log_path.exists():
            return []
        entries = [json.loads(line) for line in self.log_path.read_text(encoding="utf-8").splitlines() if line]
        return [entry for entry in entries if entry[:2] == [subcommand, "view"]]


def _pr_json(branch: str, head_oid: str, merge_oid: str | None, closing: list[dict], body: object) -> dict:
    return {
        "state": "MERGED",
        "mergedAt": "2026-01-01T00:00:00Z",
        "headRefName": branch,
        "headRefOid": head_oid,
        "baseRefName": "main",
        "isCrossRepository": False,
        "headRepositoryOwner": {"login": "squne121"},
        "closingIssuesReferences": closing,
        "mergeCommit": {"oid": merge_oid} if merge_oid else None,
        "body": body,
    }


def _finish_scenario(
    tmp_path: Path, root: Path, branch: str, slug: str, pr: dict, head_oid: str, **gh_extra
) -> Scenario:
    wt_parent = root / ".claude" / "worktrees"
    wt_parent.mkdir(parents=True, exist_ok=True)
    worktree = wt_parent / f"issue-2891-{slug}"
    _git("worktree", "add", "-q", str(worktree), branch, cwd=root)
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir(parents=True, exist_ok=True)
    fake_gh = bin_dir / "gh"
    fake_gh.write_text(_FAKE_GH_SCRIPT, encoding="utf-8")
    fake_gh.chmod(0o755)
    data_path = tmp_path / "gh_data.json"
    data_path.write_text(json.dumps({"repo": REPO_LOWER, "pr": pr, **gh_extra}), encoding="utf-8")
    log_path = tmp_path / "gh_calls.jsonl"
    env = dict(os.environ)
    env["PATH"] = f"{bin_dir}{os.pathsep}{env['PATH']}"
    env["FAKE_GH_DATA"] = str(data_path)
    env["FAKE_GH_LOG"] = str(log_path)
    env["CLAUDE_PROJECT_DIR"] = str(root)
    return Scenario(root, branch, worktree, env, data_path, log_path, head_oid)


def _merged_scenario(
    tmp_path: Path, slug: str, *, closing: list[dict] | None = None, body: object = PR_BODY, **gh_extra
) -> Scenario:
    """Branch fast-forward merged into main: ``git branch -d`` succeeds after ``worktree remove``."""
    root = tmp_path / "root"
    _init_repo(root)
    branch = f"issue-2891-{slug}"
    _git("checkout", "-q", "-b", branch, cwd=root)
    (root / "A.txt").write_text("hello\n", encoding="utf-8")
    _git("add", "A.txt", cwd=root)
    _git("commit", "-q", "-m", "add A", cwd=root)
    tip = _rev_parse(root, "HEAD")
    _git("checkout", "-q", "main", cwd=root)
    _git("merge", "-q", "--ff-only", branch, cwd=root)
    pr = _pr_json(branch, tip, tip, closing if closing is not None else [], body)
    return _finish_scenario(tmp_path, root, branch, slug, pr, tip, **gh_extra)


def _squash_scenario(
    tmp_path: Path, slug: str, *, closing: list[dict] | None = None, body: object = PR_BODY, **gh_extra
) -> Scenario:
    """Squash-shaped history (H != L, content integrated into M): ``git branch -d`` fails after
    ``worktree remove`` so the same-invocation branch-only re-authorization path is exercised."""
    root = tmp_path / "root"
    _init_repo(root)
    branch = f"issue-2891-{slug}"
    _git("checkout", "-q", "-b", branch, cwd=root)
    (root / "A.txt").write_text("hello\n", encoding="utf-8")
    _git("add", "A.txt", cwd=root)
    _git("commit", "-q", "-m", "add A", cwd=root)
    sha_h = _rev_parse(root, "HEAD")
    _git("commit", "--amend", "-q", "-m", "add A (amended message)", cwd=root)
    _git("checkout", "-q", "main", cwd=root)
    (root / "A.txt").write_text("hello\n", encoding="utf-8")
    _git("add", "A.txt", cwd=root)
    _git("commit", "-q", "-m", f"squash merge {branch}", cwd=root)
    sha_m = _rev_parse(root, "HEAD")
    pr = _pr_json(branch, sha_h, sha_m, closing if closing is not None else [], body)
    return _finish_scenario(tmp_path, root, branch, slug, pr, sha_h, **gh_extra)


def _sha256(body: str) -> str:
    return hashlib.sha256(body.encode("utf-8")).hexdigest()


def _authority(body: str = PR_BODY, **overrides) -> dict:
    authority = {
        "decision": "nonclosing_required",
        "level": "A1",
        "reason_code": "a1_decision_comment_valid",
        "repo": REPO_LOWER,
        "issue_number": ISSUE,
        "pr_number": PR_NUMBER,
        "pr_body_sha256": _sha256(body),
    }
    authority.update(overrides)
    return authority


def _write_authority(tmp_path: Path, payload: object, *, raw: str | None = None) -> Path:
    path = tmp_path / "non_closing_authority.json"
    path.write_text(raw if raw is not None else json.dumps(payload), encoding="utf-8")
    return path


# --- argv mechanically extracted from the executor Skill's production command block ----------
def _skill_cleanup_exec_argv(sc: Scenario, authority_file: Path | str | None) -> list[str]:
    text = EXECUTOR_SKILL.read_text(encoding="utf-8")
    blocks = [
        b
        for b in re.findall(r"```bash\n(.*?)```", text, flags=re.DOTALL)
        if "scripts/agent-ops/cleanup_exec.py" in b
    ]
    assert len(blocks) == 1, "exactly one production cleanup_exec command block expected in the executor Skill"
    command = blocks[0].replace("\\\n", " ")
    optional = "[--non-closing-authority-file <non_closing_authority_file>]"
    assert optional in command, "the executor Skill must show the optional --non-closing-authority-file argument"
    if authority_file is None:
        command = command.replace(optional, "")
    else:
        command = command.replace(optional, "--non-closing-authority-file <non_closing_authority_file>")
    values = {
        "<pr>": str(PR_NUMBER),
        "<issue>": str(ISSUE),
        "<絶対 worktree path>": str(sc.worktree),
        "<branch>": sc.branch,
        "<non_closing_authority_file>": str(authority_file) if authority_file is not None else "",
    }
    for placeholder, value in values.items():
        command = command.replace(placeholder, shlex.quote(value) if value else value)
    tokens = shlex.split(command)
    assert tokens[:5] == ["uv", "run", "--locked", "python3", "scripts/agent-ops/cleanup_exec.py"], tokens[:5]
    assert not any(t.startswith("<") or t.endswith(">") or "[" in t for t in tokens), tokens
    # same script, but run with the current interpreter and the absolute production path
    return [sys.executable, str(CLEANUP_EXEC), *tokens[5:]]


def _run_cli(sc: Scenario, authority_file: Path | str | None) -> tuple[subprocess.CompletedProcess, dict]:
    argv = _skill_cleanup_exec_argv(sc, authority_file)
    proc = subprocess.run(argv, cwd=str(sc.root), env=sc.env, capture_output=True, text=True)
    try:
        payload = json.loads(proc.stdout)
    except ValueError:
        pytest.fail(f"CLI did not emit JSON: rc={proc.returncode} stdout={proc.stdout!r} stderr={proc.stderr!r}")
    return proc, payload


def _assert_refused_untouched(proc, payload, sc: Scenario, label: str) -> None:
    assert proc.returncode == 1, (label, payload)
    assert payload["status"] == "refused", (label, payload)
    assert payload["reason_code"] == LINKED_MISMATCH, (label, payload)
    assert payload["actions_taken"] == [], (label, payload)
    assert sc.worktree.exists(), label
    assert _branch_exists(sc.root, sc.branch), label


# --- AC1 ------------------------------------------------------------------------------------
def test_cli_authorized_non_closing_normal_lane(tmp_path):
    """GIVEN a Refs-bound PR (no closing node) and a valid A1 authority passed through the Skill's argv
    WHEN the production CLI runs THEN the normal lane authorizes the linked Issue and cleans up."""
    sc = _merged_scenario(tmp_path, "ac1-normal")
    authority_file = _write_authority(tmp_path, _authority())
    proc, payload = _run_cli(sc, authority_file)
    assert proc.returncode == 0, payload
    assert payload["status"] == "ok", payload
    assert payload["reason_code"] is None
    assert payload["verified"]["linked_issue_match"] is True, payload
    assert payload["actions_taken"] == ["worktree_remove", "branch_delete"], payload
    assert not sc.worktree.exists()
    assert not _branch_exists(sc.root, sc.branch)


def test_cli_authorized_non_closing_branch_only_lane(tmp_path):
    """GIVEN the worktree is already gone (partial cleanup) WHEN the production CLI runs with a valid A2
    authority THEN the standalone branch-only lane authorizes the linked Issue and deletes the branch."""
    sc = _squash_scenario(tmp_path, "ac1-branch-only")
    _git("worktree", "remove", str(sc.worktree), cwd=sc.root)
    assert not sc.worktree.exists() and _branch_exists(sc.root, sc.branch)
    authority_file = _write_authority(tmp_path, _authority(level="A2", reason_code="a2_contract_deferred"))
    proc, payload = _run_cli(sc, authority_file)
    assert proc.returncode == 0, payload
    assert payload["status"] == "ok", payload
    assert payload["branch_only"] is True, payload
    assert payload["verified"]["linked_issue_match"] is True, payload
    assert payload["actions_taken"] == ["branch_delete"], payload
    assert not _branch_exists(sc.root, sc.branch)


def test_cli_authorized_non_closing_same_invocation_reauthorization(tmp_path):
    """The same authority is re-evaluated by the same-invocation branch-only re-authorization (second PR fetch)."""
    sc = _squash_scenario(tmp_path, "ac1-reauth")
    authority_file = _write_authority(tmp_path, _authority())
    proc, payload = _run_cli(sc, authority_file)
    assert proc.returncode == 0, payload
    assert payload["status"] == "ok", payload
    assert payload["actions_taken"] == ["worktree_remove", "branch_delete"], payload
    assert len(sc.gh_calls("pr")) == 2, "re-authorization must re-fetch the PR (and re-evaluate the authority)"
    assert not sc.worktree.exists()
    assert not _branch_exists(sc.root, sc.branch)


# --- AC2 ------------------------------------------------------------------------------------
_OTHER_CLOSING = [{"number": 9999}]


def _rejection_cases() -> list[tuple[str, object, dict]]:
    """(label, authority payload | ("raw", text) | None | ("path", p), scenario overrides)."""
    base = _authority()
    crlf_body = PR_BODY.replace("\n", "\r\n")
    cases: list[tuple[str, object, dict]] = [
        ("authority_absent", None, {}),
        ("file_not_found", ("path", "does-not-exist.json"), {}),
        ("file_invalid_json", ("raw", "{not json"), {}),
        ("file_json_list", ("raw", "[1, 2]"), {}),
        ("file_json_string", ("raw", '"nonclosing_required"'), {}),
        ("file_empty", ("raw", ""), {}),
        ("key_missing_sha", {k: v for k, v in base.items() if k != "pr_body_sha256"}, {}),
        ("key_missing_reason_code", {k: v for k, v in base.items() if k != "reason_code"}, {}),
        ("key_extra", {**base, "issue_state": "OPEN"}, {}),
        ("sha_trailing_newline_removed", _authority(PR_BODY.rstrip("\n")), {}),
        ("sha_extra_trailing_newline", _authority(PR_BODY + "\n"), {}),
        ("sha_crlf_variant", _authority(crlf_body), {}),
        ("sha_stripped", _authority(PR_BODY.strip()), {}),
        ("sha_wrong_type", _authority(pr_body_sha256=123), {}),
        ("issue_mismatch", _authority(issue_number=ISSUE + 1), {}),
        ("pr_mismatch", _authority(pr_number=PR_NUMBER + 1), {}),
        ("repo_mismatch", _authority(repo="other-owner/loop-protocol"), {}),
        ("repo_wrong_type_list", _authority(repo=[REPO_LOWER]), {}),
        ("issue_bool", _authority(issue_number=True), {}),
        ("issue_string", _authority(issue_number=str(ISSUE)), {}),
        ("pr_bool", _authority(pr_number=True), {}),
        ("pr_string", _authority(pr_number=str(PR_NUMBER)), {}),
        ("level_list", _authority(level=["A1"]), {}),
        ("level_object", _authority(level={"A1": 1}), {}),
        ("level_A3", _authority(level="A3"), {}),
        ("level_CLOSED", _authority(level="CLOSED"), {}),
        ("level_lowercase", _authority(level="a1"), {}),
        ("decision_closing_required", _authority(decision="closing_required"), {}),
        ("decision_list", _authority(decision=["nonclosing_required"]), {}),
        ("pr_body_missing", _authority(), {"pr_omit_body": True}),
        ("pr_body_none", _authority(), {"pr_bodies": [None]}),
        ("pr_body_not_string", _authority(), {"pr_bodies": [12345]}),
        ("other_closing_node_valid_authority", _authority(), {"closing": _OTHER_CLOSING}),
    ]
    return cases


def test_rejected_before_first_destructive_operation(tmp_path):
    """GIVEN invalid / missing / mismatched authority (or a different closing node) WHEN the production CLI runs
    THEN it is refused with the EXISTING LINKED_ISSUE_MISMATCH before any destructive operation (actions_taken == [])
    and the worktree and branch survive. A valid-authority control at the end proves the fixture itself is
    authorizable (no false-green from an unrelated refusal)."""
    sc = _merged_scenario(tmp_path, "ac2")
    original_pr = json.loads(sc.data_path.read_text(encoding="utf-8"))["pr"]
    for label, payload, overrides in _rejection_cases():
        overrides = dict(overrides)
        closing = overrides.pop("closing", [])
        pr = {**original_pr, "closingIssuesReferences": closing}
        reset = {"pr": pr, "pr_omit_body": False, "pr_bodies": None}
        reset.update(overrides)
        sc.write_gh_data(**reset)
        if payload is None:
            authority_arg: Path | str | None = None
        elif isinstance(payload, tuple) and payload[0] == "raw":
            authority_arg = _write_authority(tmp_path, None, raw=payload[1])
        elif isinstance(payload, tuple) and payload[0] == "path":
            authority_arg = str(tmp_path / payload[1])
        else:
            authority_arg = _write_authority(tmp_path, payload)
        proc, result = _run_cli(sc, authority_arg)
        _assert_refused_untouched(proc, result, sc, label)

    # control: restore the Refs-bound PR and pass a valid authority -> the same fixture is authorized.
    sc.write_gh_data(pr=original_pr, pr_omit_body=False, pr_bodies=None)
    proc, result = _run_cli(sc, _write_authority(tmp_path, _authority()))
    assert proc.returncode == 0 and result["status"] == "ok", result
    assert result["actions_taken"] == ["worktree_remove", "branch_delete"], result


def _parity_fixtures() -> list[tuple[str, dict, object, int, int]]:
    """(label, authority, pull_request-body, issue, pr). Repo identities are all semantically equal to / different
    from the trusted ``squne121/loop-protocol``; whitespace-padded repo strings are intentionally excluded because
    producer/adapter/twin legitimately differ there (the twin is the strict, fail-closed side)."""
    crlf_body = PR_BODY.replace("\n", "\r\n")
    good = _authority()
    fixtures: list[tuple[str, dict, object, int, int]] = []

    def add(label: str, authority: object, body: object = PR_BODY, issue: int = ISSUE, pr: int = PR_NUMBER):
        fixtures.append((label, authority, body, issue, pr))  # type: ignore[arg-type]

    add("valid_A1", good)
    add("valid_A2", _authority(level="A2"))
    add("valid_repo_uppercase", _authority(repo="SQUNE121/LOOP-PROTOCOL"))
    add("valid_repo_mixed_case", _authority(repo="Squne121/Loop-Protocol"))
    add("valid_crlf_body", _authority(crlf_body), crlf_body)
    add("valid_unicode_body", _authority("日本語 ✓\n"), "日本語 ✓\n")
    add("authority_none", None)
    add("authority_list", [good])
    add("authority_empty_dict", {})
    add("key_missing", {k: v for k, v in good.items() if k != "reason_code"})
    add("key_extra", {**good, "issue_state": "OPEN"})
    add("sha_mismatch_trailing_newline", _authority(PR_BODY.rstrip("\n")))
    add("sha_mismatch_crlf", _authority(crlf_body))
    add("sha_uppercase_hex", _authority(pr_body_sha256=_sha256(PR_BODY).upper()))
    add("sha_non_string", _authority(pr_body_sha256=7))
    add("issue_mismatch", _authority(issue_number=ISSUE + 1))
    add("pr_mismatch", _authority(pr_number=PR_NUMBER + 1))
    add("repo_mismatch", _authority(repo="other-owner/loop-protocol"))
    add("repo_none", _authority(repo=None))
    add("repo_list", _authority(repo=[REPO_LOWER]))
    add("repo_empty", _authority(repo=""))
    add("issue_bool", _authority(issue_number=True))
    add("issue_string", _authority(issue_number=str(ISSUE)))
    add("pr_bool", _authority(pr_number=True))
    add("pr_string", _authority(pr_number=str(PR_NUMBER)))
    for level in ("A3", "CLOSED", "a1", "", None, 1, ["A1"], {"A1": 1}):
        add(f"level_{level!r}", _authority(level=level))
    for decision in ("closing_required", "fail_closed", None, ["nonclosing_required"], {}):
        add(f"decision_{decision!r}", _authority(decision=decision))
    add("body_missing", good, None)
    add("body_non_string", good, 99)
    return fixtures


def test_authority_twin_parity_with_open_pr():
    """The cleanup_exec twin and ``open_pr.non_closing_authority_binds`` must agree on the SAME fixtures, fed with
    semantically identical trusted repo identity (producer: normalized lowercase snapshot repo; twin: canonical-case
    ``repo_slug`` — compared case-insensitively)."""
    twin_repo_slug = "Squne121/Loop-Protocol"
    results: list[bool] = []
    for label, authority, body, issue, pr in _parity_fixtures():
        pull_request = {} if body is None else {"body": body}
        expected = open_pr.non_closing_authority_binds(authority, pull_request, REPO_LOWER, issue, pr)
        # `ISSUE` / `PR_NUMBER` are the identities the authority is bound to; the producer receives the very same.
        actual = cleanup_exec._non_closing_authority_binds(authority, pull_request, twin_repo_slug, issue, pr)
        assert actual is expected, (label, expected, actual)
        results.append(expected)
    # not vacuous: both outcomes occur
    assert any(results) and not all(results)
    assert results.count(True) >= 6


# --- AC3 ------------------------------------------------------------------------------------
_RESEARCH_BODY = (
    "## Machine-Readable Contract\n\n```yaml\ncontract_schema_version: v1\nissue_kind: research\n"
    'parent_issue: "none"\ngoal_ref: "test"\nchange_kind: code\n```\n'
)
_IMPLEMENTATION_BODY = _RESEARCH_BODY.replace("issue_kind: research", "issue_kind: implementation")


def test_unchanged_existing_paths_without_authority(tmp_path_factory):
    """Closing fast path (no extra Issue fetch) and the #2508 research fallback keep authorizing without any
    authority; an authority that accompanies an already-authorized path changes nothing; a different closing node
    never falls through to the authority path."""
    # (a) closing fast path, no authority: authorized and NO `gh issue view` call.
    tmp = tmp_path_factory.mktemp("fast")
    sc = _merged_scenario(tmp, "ac3-fast", closing=[{"number": ISSUE}])
    proc, payload = _run_cli(sc, None)
    assert proc.returncode == 0 and payload["status"] == "ok", payload
    assert payload["actions_taken"] == ["worktree_remove", "branch_delete"], payload
    assert sc.gh_calls("issue") == [], "closing relation fast path must not fetch the Issue"

    # (b) closing fast path with an authority also supplied (even an unusable one): same result, still no fetch.
    tmp = tmp_path_factory.mktemp("fast-auth")
    sc = _merged_scenario(tmp, "ac3-fast-auth", closing=[{"number": ISSUE}])
    proc, payload = _run_cli(sc, _write_authority(tmp, _authority(level="A3")))
    assert proc.returncode == 0 and payload["status"] == "ok", payload
    assert payload["actions_taken"] == ["worktree_remove", "branch_delete"], payload
    assert sc.gh_calls("issue") == []

    # (c) research fallback (Refs + issue_kind: research + CLOSED/COMPLETED), no authority.
    tmp = tmp_path_factory.mktemp("research")
    issue = {"body": _RESEARCH_BODY, "state": "CLOSED", "stateReason": "COMPLETED"}
    sc = _merged_scenario(tmp, "ac3-research", issue=issue)
    proc, payload = _run_cli(sc, None)
    assert proc.returncode == 0 and payload["status"] == "ok", payload
    assert payload["actions_taken"] == ["worktree_remove", "branch_delete"], payload
    assert len(sc.gh_calls("issue")) == 1

    # (d) research fallback with an authority also supplied: unchanged (authorized by the fallback first).
    tmp = tmp_path_factory.mktemp("research-auth")
    sc = _merged_scenario(tmp, "ac3-research-auth", issue=issue)
    proc, payload = _run_cli(sc, _write_authority(tmp, _authority()))
    assert proc.returncode == 0 and payload["status"] == "ok", payload
    assert payload["actions_taken"] == ["worktree_remove", "branch_delete"], payload

    # (e) a DIFFERENT closing node with a valid authority is still refused (no fall-through).
    tmp = tmp_path_factory.mktemp("other-closing")
    sc = _merged_scenario(tmp, "ac3-other", closing=_OTHER_CLOSING)
    proc, payload = _run_cli(sc, _write_authority(tmp, _authority()))
    _assert_refused_untouched(proc, payload, sc, "other_closing_node")


@pytest.mark.parametrize(
    "issue",
    [
        None,  # `gh issue view` fails
        {"body": _RESEARCH_BODY, "state": "OPEN", "stateReason": None},  # research but still OPEN
        {"body": _IMPLEMENTATION_BODY, "state": "CLOSED", "stateReason": "COMPLETED"},  # CLOSED but not research
        {"body": _RESEARCH_BODY, "state": "CLOSED", "stateReason": "NOT_PLANNED"},  # research, not COMPLETED
        [],  # malformed (non-object) payload
    ],
    ids=["fetch_failure", "open_research", "closed_non_research", "closed_not_planned", "malformed_payload"],
)
def test_research_fallback_miss_falls_through_to_authority(tmp_path, issue):
    """A research-fallback miss (fetch failure, OPEN, non-research, ...) still reaches the authority evaluation."""
    sc = _merged_scenario(tmp_path, "ac3-fall", issue=issue)
    proc, payload = _run_cli(sc, _write_authority(tmp_path, _authority()))
    assert proc.returncode == 0, payload
    assert payload["status"] == "ok", payload
    assert len(sc.gh_calls("issue")) == 1, "the research fallback must have been evaluated first"
    assert payload["actions_taken"] == ["worktree_remove", "branch_delete"], payload


def test_research_fallback_timeout_falls_through_to_authority(monkeypatch):
    """A timeout while fetching the linked Issue is a fallback miss, and the authority is still evaluated."""
    pr = {"closingIssuesReferences": [], "body": PR_BODY}
    real_run = subprocess.run

    def fake_run(args, *a, **kw):
        if isinstance(args, (list, tuple)) and list(args[1:3]) == ["issue", "view"]:
            raise subprocess.TimeoutExpired(cmd=args, timeout=1)
        return real_run(args, *a, **kw)

    monkeypatch.setattr(cleanup_exec.shutil, "which", lambda name: "/usr/bin/gh")
    monkeypatch.setattr(cleanup_exec.subprocess, "run", fake_run)
    deadline = Deadline(60.0)
    req = {"linked_issue_number": ISSUE, "pr_number": PR_NUMBER, "non_closing_authority": _authority()}
    assert cleanup_exec._verify_linked_issue(req, pr, "/tmp", REPO_LOWER, deadline) == (True, None)
    no_authority = {"linked_issue_number": ISSUE, "pr_number": PR_NUMBER}
    assert cleanup_exec._verify_linked_issue(no_authority, pr, "/tmp", REPO_LOWER, deadline) == (
        False,
        LINKED_MISMATCH,
    )
    # a different closing node never reaches the authority path
    other = {"closingIssuesReferences": _OTHER_CLOSING, "body": PR_BODY}
    assert cleanup_exec._verify_linked_issue(req, other, "/tmp", REPO_LOWER, deadline) == (False, LINKED_MISMATCH)


# --- AC4 ------------------------------------------------------------------------------------
def test_partial_success_preserved_when_reauthorization_rejected(tmp_path):
    """GIVEN worktree remove succeeds, `branch -d` then fails, and the PR body re-fetched for the same-invocation
    branch-only re-authorization no longer matches the authority hash WHEN the production CLI runs THEN the
    already-succeeded worktree_remove stays in actions_taken, the original branch_delete_failed is kept, the branch
    is left, and no force delete runs. This is NOT the 'rejected before any deletion' case."""
    sc = _squash_scenario(tmp_path, "ac4", pr_bodies=[PR_BODY, PR_BODY + "\nsilently edited after first fetch\n"])
    tip_before = _rev_parse(sc.root, sc.branch)
    proc, payload = _run_cli(sc, _write_authority(tmp_path, _authority()))
    assert len(sc.gh_calls("pr")) == 2, "the PR body must have been fetched a second time (re-authorization)"
    assert proc.returncode == 1, payload
    assert payload["status"] == "error", payload
    assert payload["reason_code"].startswith("branch_delete_failed"), payload
    assert payload["actions_taken"] == ["worktree_remove"], payload
    assert not sc.worktree.exists(), "worktree_remove really happened"
    assert _branch_exists(sc.root, sc.branch), "the branch must be left in place"
    assert _rev_parse(sc.root, sc.branch) == tip_before, "no force delete / ref change"


# --- AC5 ------------------------------------------------------------------------------------
def _discard_scenario(tmp_path: Path, slug: str, *, closing: list[dict]) -> Scenario:
    root = tmp_path / "root"
    _init_repo(root)
    branch = f"issue-2891-{slug}"
    _git("checkout", "-q", "-b", branch, cwd=root)
    (root / "A.txt").write_text("hello\n", encoding="utf-8")
    _git("add", "A.txt", cwd=root)
    _git("commit", "-q", "-m", "add A", cwd=root)
    sha_h = _rev_parse(root, "HEAD")
    (root / "A.txt").write_text("local only change\n", encoding="utf-8")
    _git("add", "A.txt", cwd=root)
    _git("commit", "-q", "-m", "local-only commit", cwd=root)
    _git("checkout", "-q", "main", cwd=root)
    pr = _pr_json(branch, sha_h, None, closing, PR_BODY)
    return _finish_scenario(tmp_path, root, branch, slug, pr, sha_h)


def _discard_verdict(sc: Scenario, monkeypatch, with_authority: bool):
    for key in ("PATH", "FAKE_GH_DATA", "FAKE_GH_LOG", "CLAUDE_PROJECT_DIR"):
        monkeypatch.setenv(key, sc.env[key])
    req = {
        "pr_number": PR_NUMBER,
        "linked_issue_number": ISSUE,
        "worktree_path": str(sc.worktree),
        "branch_name": sc.branch,
    }
    if with_authority:
        req["non_closing_authority"] = _authority()
    return cleanup_exec.verify_discard_authorization(req, str(sc.root), Deadline(60.0))


def test_discard_lane_unchanged_with_authority(tmp_path_factory, monkeypatch):
    """`verify_discard_authorization` ignores `non_closing_authority`: a Refs-bound PR stays refused with the same
    verdict, and a closing-relation PR stays a discard candidate with the same verdict. No discard CLI was added."""
    assert "non_closing" not in inspect.getsource(cleanup_exec.verify_discard_authorization)

    refs_bound = _discard_scenario(tmp_path_factory.mktemp("discard-refs"), "ac5-refs", closing=[])
    without = _discard_verdict(refs_bound, monkeypatch, with_authority=False)
    with_auth = _discard_verdict(refs_bound, monkeypatch, with_authority=True)
    assert without[:2] == (False, LINKED_MISMATCH)
    assert with_auth == without

    closing = _discard_scenario(tmp_path_factory.mktemp("discard-closing"), "ac5-closing", closing=[{"number": ISSUE}])
    without = _discard_verdict(closing, monkeypatch, with_authority=False)
    with_auth = _discard_verdict(closing, monkeypatch, with_authority=True)
    assert without[0] is True and without[2]["discard_candidate"] is True
    assert with_auth == without

    help_text = subprocess.run(
        [sys.executable, str(CLEANUP_EXEC), "--help"], capture_output=True, text=True, check=True
    ).stdout
    assert "discard" not in help_text.lower()
