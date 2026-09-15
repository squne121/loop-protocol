"""scripts/agent-ops/tests/test_cleanup_exec_issue_2628_e2e.py

Issue #2628: squash-equivalence rename-detection bug + discard-lane
confirmation ``argv`` construction bug regression coverage.

Every test below drives the fix through the PUBLIC CLI entry points
(``cleanup_exec.py`` / ``materialize_cleanup_contract.py``) via real
``subprocess`` execution against a disposable fixture git repository and a
fake ``gh`` binary placed on ``PATH`` — never a bare helper-function call in
isolation — per the Issue's In Scope requirement that both fixes be verified
end-to-end through the public CLI, not merely at the helper-unit level.

Fix 1 (AC1/AC2/AC4): ``_squash_equivalence_path_set()`` (used by the normal
cleanup lane's ``_resolve_head_equivalence()``) now passes ``--no-renames``
to ``git diff --name-only``, so a locally-deleted rename-source path is never
silently folded away by git's default rename-detection heuristic and hidden
from the content-restricted equivalence comparison.

Fix 2 (AC3): ``materialize_cleanup_contract.py``'s discard-lane
``confirmation.argv`` construction now appends ``--linked-issue-number``
instead of splicing it into ``argv[3:3]`` (which lands BEFORE the
``--pr-number`` value at index 3 and corrupts it).

AC5 is a regression check (not a new behavior): un-integrated local changes
still fail closed (``pr_head_oid_mismatch``) via the normal cleanup public
CLI entry, and the discard lane's existing human-confirmed
``--check``/issuance/``--consume`` contract (Issue #1523, untouched by this
Issue) still reports discard candidacy via ``--check`` without performing
any destructive action.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[3]
AGENT_OPS_DIR = REPO_ROOT / "scripts" / "agent-ops"
CLEANUP_EXEC = AGENT_OPS_DIR / "cleanup_exec.py"
MATERIALIZE_CONTRACT = AGENT_OPS_DIR / "materialize_cleanup_contract.py"

sys.path.insert(0, str(AGENT_OPS_DIR))

from cleanup_exec import _squash_content_matches, _squash_equivalence_path_set  # noqa: E402
from worktree_catalog import Deadline  # noqa: E402


# ─── Fake `gh` CLI ──────────────────────────────────────────────────────────
# A tiny, deterministic stand-in for the real `gh` binary. The public CLI
# entry points exercised below only ever invoke `gh repo view` (resolve the
# trusted repo slug) and `gh pr view` (fetch PR state) — the fake mirrors
# exactly those two subcommands, reading the canned response from a JSON file
# whose path is passed through the `FAKE_GH_DATA` environment variable so
# each test supplies its own fixture PR shape.
_FAKE_GH_SCRIPT = '''#!/usr/bin/env python3
import json
import os
import sys


def main() -> int:
    data_path = os.environ.get("FAKE_GH_DATA")
    with open(data_path, "r", encoding="utf-8") as fh:
        data = json.load(fh)
    argv = sys.argv[1:]
    if len(argv) >= 2 and argv[0] == "repo" and argv[1] == "view":
        sys.stdout.write(data["repo"] + "\\n")
        return 0
    if len(argv) >= 2 and argv[0] == "pr" and argv[1] == "view":
        sys.stdout.write(json.dumps(data["pr"]) + "\\n")
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


def _init_repo(root: Path) -> str:
    """Initialize a fixture repo on ``main`` with a single seed commit.

    Returns the seed commit SHA.
    """
    root.mkdir(parents=True)
    _git("init", "-q", "-b", "main", cwd=root)
    _git("config", "user.email", "t@t.com", cwd=root)
    _git("config", "user.name", "T", cwd=root)
    # Pin rename detection ON at the repository level so AC1's sanity check
    # (asserting git's DEFAULT rename-detection behavior hides the rename
    # source) is deterministic regardless of the invoking user's/CI's
    # ambient `diff.renames` setting. `--no-renames` in the fixed
    # `_squash_equivalence_path_set()` overrides this per-invocation
    # regardless, so this only pins the "buggy" baseline this fixture
    # demonstrates — it does not weaken what the fix itself verifies.
    _git("config", "diff.renames", "true", cwd=root)
    _git("remote", "add", "origin", "https://github.com/squne121/loop-protocol.git", cwd=root)
    (root / "README.md").write_text("seed\n", encoding="utf-8")
    _git("add", "README.md", cwd=root)
    _git("commit", "-q", "-m", "seed", cwd=root)
    return _rev_parse(root, "HEAD")


def _write_fake_gh(bin_dir: Path) -> None:
    bin_dir.mkdir(parents=True, exist_ok=True)
    fake_gh = bin_dir / "gh"
    fake_gh.write_text(_FAKE_GH_SCRIPT, encoding="utf-8")
    fake_gh.chmod(0o755)


def _make_pr_json(
    *,
    branch_name: str,
    head_ref_oid: str,
    merge_commit_oid: str | None,
    linked_issue: int = 2628,
    base_ref: str = "main",
) -> dict:
    return {
        "state": "MERGED",
        "mergedAt": "2026-01-01T00:00:00Z",
        "headRefName": branch_name,
        "headRefOid": head_ref_oid,
        "baseRefName": base_ref,
        "isCrossRepository": False,
        "headRepositoryOwner": {"login": "squne121"},
        "closingIssuesReferences": [{"number": linked_issue}],
        "mergeCommit": {"oid": merge_commit_oid} if merge_commit_oid else None,
        "body": "",
    }


def _build_env(tmp_path: Path, project_root: Path, pr_json: dict) -> dict:
    bin_dir = tmp_path / "bin"
    _write_fake_gh(bin_dir)
    data_path = tmp_path / "gh_data.json"
    data_path.write_text(
        json.dumps({"repo": "squne121/loop-protocol", "pr": pr_json}), encoding="utf-8"
    )
    env = dict(os.environ)
    env["PATH"] = f"{bin_dir}{os.pathsep}{env['PATH']}"
    env["FAKE_GH_DATA"] = str(data_path)
    env["CLAUDE_PROJECT_DIR"] = str(project_root)
    return env


def _make_worktree(root: Path, branch_name: str, slug: str) -> Path:
    wt_parent = root / ".claude" / "worktrees"
    wt_parent.mkdir(parents=True, exist_ok=True)
    wt_path = wt_parent / f"issue-2628-{slug}"
    _git("worktree", "add", "-q", str(wt_path), branch_name, cwd=root)
    return wt_path


def _run_cleanup_exec_cli(
    *, root: Path, env: dict, pr_number: int, linked_issue: int, worktree_path: Path, branch_name: str
) -> subprocess.CompletedProcess:
    """Invoke the PUBLIC CLI entry of ``cleanup_exec.py`` via real subprocess."""
    return subprocess.run(
        [
            sys.executable, str(CLEANUP_EXEC),
            "--pr-number", str(pr_number),
            "--linked-issue-number", str(linked_issue),
            "--worktree-path", str(worktree_path),
            "--branch-name", branch_name,
            "--json",
        ],
        cwd=str(root), env=env, capture_output=True, text=True,
    )


def _branch_exists(root: Path, branch_name: str) -> bool:
    check = subprocess.run(
        ["git", "-C", str(root), "show-ref", "--verify", "--quiet", f"refs/heads/{branch_name}"],
    )
    return check.returncode == 0


# ─── AC1: rename-source deletion must not be lost from the equivalence path set ──


def test_rename_source_deletion_not_lost_from_equivalence_path_set(tmp_path):
    """GIVEN a fixture that reproduces the ACTUAL false-authorization this
    Issue fixes — not just a flagged-but-differently-rejected diff — WHEN
    the normal-cleanup public CLI entry runs THEN it correctly fails closed
    on the un-integrated ``A.txt`` deletion, whereas the OLD (rename-
    detection-on) path-set behavior, demonstrated via the real
    ``_squash_content_matches()`` helper fed the raw-git buggy path set,
    would have incorrectly authorized the same input.

    Fixture shape (common ancestor / M / L):

    ================  =======  =======
    state             A.txt    B.txt
    ================  =======  =======
    common ancestor   present  absent
    M (squash merge)  present  present (== L's content)
    L (local tip)     absent   present (renamed from A, same content)
    ================  =======  =======

    L renamed A to B (git-rename-detectable, identical content). M is built
    independently on top of the SAME common ancestor and adds B with
    matching content WITHOUT touching A — i.e. M never received the rename;
    it picked up B's content through an unrelated path, and A.txt's
    deletion on the local branch was never integrated into M. This is a
    genuine un-integrated difference, not a rename artifact.

    Under the OLD (rename-detection-on) path set, only B.txt is visible
    (A.txt's deletion is folded into the rename pair and lost), and B
    matches between M and L — so the old path-set behavior would
    incorrectly authorize cleanup despite A.txt's un-integrated deletion.
    Under the FIXED (``--no-renames``) path set, both A.txt and B.txt are
    visible; A.txt exists in M but not in L, so content-restricted
    comparison correctly detects the mismatch and refuses."""
    root = tmp_path / "root"
    sha_seed = _init_repo(root)
    (root / "A.txt").write_text("shared-content\n", encoding="utf-8")
    _git("add", "A.txt", cwd=root)
    _git("commit", "-q", "-m", "seed A", cwd=root)
    sha_seed = _rev_parse(root, "HEAD")

    branch_name = "issue-2628-ac1"
    _git("checkout", "-q", "-b", branch_name, cwd=root)
    _git("mv", "A.txt", "B.txt", cwd=root)
    _git("commit", "-q", "-m", "local rename A to B", cwd=root)
    sha_l = _rev_parse(root, "HEAD")

    # Sanity: git's DEFAULT (rename-detection-on, per _init_repo's pinned
    # diff.renames=true) --name-only view of this exact range hides the
    # deleted pre-image path — this is the bug being fixed, demonstrated on
    # the raw git primitive before asserting on the fixed helper below.
    buggy_paths = _git("diff", "--name-only", sha_seed, sha_l, cwd=root).splitlines()
    assert buggy_paths == ["B.txt"], "sanity: rename detection hides A.txt pre-fix"

    _git("checkout", "-q", "main", cwd=root)
    (root / "B.txt").write_text("shared-content\n", encoding="utf-8")
    _git("add", "B.txt", cwd=root)
    _git("commit", "-q", "-m", f"squash merge {branch_name}", cwd=root)
    sha_m = _rev_parse(root, "HEAD")
    parents = _git("rev-list", "--parents", "-n", "1", sha_m, cwd=root).split()
    assert parents[1:] == [sha_seed], "M must be a genuine squash-shaped (single-parent) commit"
    assert (root / "A.txt").exists(), "M must still contain A.txt (never received the rename)"

    wt_path = _make_worktree(root, branch_name, "ac1")
    assert _git("branch", "--show-current", cwd=root) == "main"

    # Counterfactual (old behavior): feeding the OLD, rename-collapsed path
    # set to the real (unmodified) production content-comparison helper
    # shows it would have matched — i.e. the pre-fix path set would have
    # caused a false authorization on this exact input. No production code
    # is monkeypatched; only the raw-git buggy path set (computed above via
    # git's default rename-detection primitive) is substituted as input to
    # demonstrate what the pre-fix path set would have produced.
    counterfactual_match = _squash_content_matches(str(root), sha_m, sha_l, buggy_paths, Deadline(20.0))
    assert counterfactual_match is True, (
        "counterfactual: the OLD rename-collapsed path set must appear to "
        "match (mis-authorize) despite A.txt's un-integrated deletion"
    )

    # Direct helper-level assertion: the FIXED path set includes A.txt.
    fixed_paths = _squash_equivalence_path_set(str(root), sha_seed, sha_l, Deadline(20.0))
    assert fixed_paths is not None
    assert "A.txt" in fixed_paths, "fixed path set must include the rename source deletion"
    assert "B.txt" in fixed_paths

    # And the fixed path set correctly detects the mismatch that the
    # counterfactual (buggy) path set above could not see.
    fixed_match = _squash_content_matches(str(root), sha_m, sha_l, fixed_paths, Deadline(20.0))
    assert fixed_match is False, "fixed path set must detect A.txt's un-integrated deletion"

    pr_json = _make_pr_json(branch_name=branch_name, head_ref_oid=sha_seed, merge_commit_oid=sha_m)
    env = _build_env(tmp_path, root, pr_json)

    result = _run_cleanup_exec_cli(
        root=root, env=env, pr_number=2628, linked_issue=2628,
        worktree_path=wt_path, branch_name=branch_name,
    )
    payload = json.loads(result.stdout)
    assert payload["status"] == "refused", payload
    assert payload["reason_code"] == "pr_head_oid_mismatch", payload
    assert payload["verified"]["head_equivalence_authorized"] is False, payload
    assert payload["verified"]["local_delta_paths_count"] == 2, payload
    assert payload["actions_taken"] == []
    # Fail-closed: worktree/branch must survive un-integrated deletion.
    assert wt_path.exists()
    assert _branch_exists(root, branch_name)


# ─── AC2: non-rename squash-equivalence success case has no regression ───────


def test_non_rename_squash_equivalence_still_authorizes(tmp_path):
    """GIVEN M and L agree on content (no rename involved — L is a
    message-only amend of the commit that became M's content, so H != L but
    trees are identical) WHEN the normal-cleanup public CLI entry runs THEN
    equivalence is still authorized (``squash_merge_delta_match``) and the
    destructive cleanup completes, proving ``--no-renames`` does not regress
    the plain non-rename success path (no deletions are present in this
    range at all, so rename detection was never a factor here either way)."""
    root = tmp_path / "root"
    _init_repo(root)

    branch_name = "issue-2628-ac2"
    _git("checkout", "-q", "-b", branch_name, cwd=root)
    (root / "A.txt").write_text("hello\n", encoding="utf-8")
    _git("add", "A.txt", cwd=root)
    _git("commit", "-q", "-m", "add A", cwd=root)
    sha_h = _rev_parse(root, "HEAD")
    # Message-only amend: new commit object, IDENTICAL tree, same parent.
    _git("commit", "--amend", "-q", "-m", "add A (amended message)", cwd=root)
    sha_l = _rev_parse(root, "HEAD")
    assert sha_h != sha_l

    _git("checkout", "-q", "main", cwd=root)
    (root / "A.txt").write_text("hello\n", encoding="utf-8")
    _git("add", "A.txt", cwd=root)
    _git("commit", "-q", "-m", f"squash merge {branch_name}", cwd=root)
    sha_m = _rev_parse(root, "HEAD")

    wt_path = _make_worktree(root, branch_name, "ac2")

    pr_json = _make_pr_json(branch_name=branch_name, head_ref_oid=sha_h, merge_commit_oid=sha_m)
    env = _build_env(tmp_path, root, pr_json)

    result = _run_cleanup_exec_cli(
        root=root, env=env, pr_number=2628, linked_issue=2628,
        worktree_path=wt_path, branch_name=branch_name,
    )
    payload = json.loads(result.stdout)
    assert result.returncode == 0, payload
    assert payload["status"] == "ok", payload
    assert payload["reason_code"] is None
    assert payload["verified"]["head_equivalence_authorized"] is True, payload
    assert payload["verified"]["head_equivalence_mode"] == "squash_merge_delta_match", payload
    assert payload["actions_taken"] == ["worktree_remove", "branch_delete"]
    assert not wt_path.exists()
    assert not _branch_exists(root, branch_name)


# ─── AC3: materialize_cleanup_contract.py confirmation.argv parses via the real CLI ──


def test_materialize_confirmation_argv_with_linked_issue_parses_via_real_cli(tmp_path):
    """GIVEN a discard-candidate fixture (H strictly and structurally an
    ancestor of L, with local-only commits beyond H) AND a linked issue
    number WHEN ``materialize_cleanup_contract.py``'s public CLI entry issues
    the discard confirmation THEN the returned ``confirmation.argv`` keeps
    ``--pr-number``'s value intact (index 3, not corrupted by the
    linked-issue insertion) AND that EXACT argv, re-invoked as a real
    subprocess against ``materialize_cleanup_contract.py``'s own argparse,
    parses successfully (no exit 2 / no "expected one argument" error)."""
    root = tmp_path / "root"
    _init_repo(root)

    branch_name = "issue-2628-ac3"
    _git("checkout", "-q", "-b", branch_name, cwd=root)
    (root / "A.txt").write_text("hello\n", encoding="utf-8")
    _git("add", "A.txt", cwd=root)
    _git("commit", "-q", "-m", "add A", cwd=root)
    sha_h = _rev_parse(root, "HEAD")
    (root / "A.txt").write_text("local only change\n", encoding="utf-8")
    _git("add", "A.txt", cwd=root)
    _git("commit", "-q", "-m", "local-only commit", cwd=root)
    _git("checkout", "-q", "main", cwd=root)

    wt_path = _make_worktree(root, branch_name, "ac3")

    pr_json = _make_pr_json(branch_name=branch_name, head_ref_oid=sha_h, merge_commit_oid=None)
    env = _build_env(tmp_path, root, pr_json)

    # Public CLI entry: plain (no --check / --consume) issuance invocation.
    issue_result = subprocess.run(
        [
            sys.executable, str(MATERIALIZE_CONTRACT),
            "--pr-number", "2628",
            "--linked-issue-number", "2628",
            "--worktree-path", str(wt_path),
            "--branch-name", branch_name,
            "--operation", "local_only_discard",
            "--json",
        ],
        cwd=str(root), env=env, capture_output=True, text=True,
    )
    issued = json.loads(issue_result.stdout)
    assert issue_result.returncode == 0, issued
    assert issued["status"] == "ok", issued
    argv = issued["confirmation"]["argv"]

    # The bug: argv[3:3] = [...] lands BEFORE index 3 (the --pr-number VALUE),
    # producing ["--pr-number", "--linked-issue-number", "2628", "2628", ...].
    # The fix: argv[3] must still be the --pr-number VALUE itself.
    assert argv[2] == "--pr-number", argv
    assert argv[3] == "2628", f"--pr-number value corrupted at index 3: {argv!r}"
    assert "--linked-issue-number" in argv
    li_index = argv.index("--linked-issue-number")
    assert argv[li_index + 1] == "2628"

    # Re-invoke the EXACT returned argv as a real subprocess against the
    # real argparse-based CLI — this is what a human confirming the discard
    # would literally run.
    consume_result = subprocess.run(argv, cwd=str(root), env=env, capture_output=True, text=True)
    assert consume_result.returncode != 2, consume_result.stderr
    assert "expected one argument" not in consume_result.stderr
    consumed = json.loads(consume_result.stdout)
    assert consumed.get("status") == "ok", consumed
    assert not wt_path.exists()
    assert not _branch_exists(root, branch_name)


# ─── AC4: PR #2623-style state — full CLI entry completes without human intervention ──


def test_public_entry_autonomous_cleanup_completes_for_squash_equivalent_history(tmp_path):
    """GIVEN a synthetic fixture built with a message-only amend (H and L
    are distinct commits with identical trees, so H is NOT a literal
    ancestor of L, while M and L agree on content restricted to the local
    delta path set) WHEN the normal-cleanup public CLI entry (subprocess,
    not a helper call) runs THEN cleanup completes (``git worktree remove``
    + a same-invocation fallback to expected-OID compare-and-delete when
    ``git branch -d`` refuses the historically-unmerged branch) with NO
    human confirmation step.

    This fixture constructs a state where H != L but the path-restricted
    content is integrated — it does not identify or reproduce the actual
    root cause of PR #2623 / Issue #2620's original ``pr_head_oid_mismatch``
    incident, which Issue #2628's Current Validated Scope records as
    unknown (no contemporaneous evidence of whether amend, rebase, a stale
    local branch, or a missing object was the actual cause). The
    message-only amend here is only one of several possible ways to
    construct "H != L, content already integrated"; it is not a claim about
    what happened in the historical incident.

    Completion here relies on the existing branch-only fallback: because H
    and L are historically unmerged (the amend rewrote L's own commit
    object), ``git branch -d`` fails, and ``run()``'s existing
    same-invocation re-authorization path (``verify_branch_only_cleanup_authorization``
    → expected-OID compare-and-delete, Issue #1523) completes the branch
    removal instead. This is the actual completion path this fixture
    exercises, not a plain ``git branch -d`` success."""
    root = tmp_path / "root"
    sha_base = _init_repo(root)

    branch_name = "issue-2628-ac4"
    _git("checkout", "-q", "-b", branch_name, cwd=root)
    (root / "A.txt").write_text("hello\n", encoding="utf-8")
    _git("add", "A.txt", cwd=root)
    _git("commit", "-q", "-m", "add A", cwd=root)
    sha_h = _rev_parse(root, "HEAD")
    _git("commit", "--amend", "-q", "-m", "add A (amended message)", cwd=root)
    sha_l = _rev_parse(root, "HEAD")
    assert sha_h != sha_l

    is_ancestor = subprocess.run(
        ["git", "merge-base", "--is-ancestor", sha_h, sha_l], cwd=str(root)
    ).returncode
    assert is_ancestor == 1, "H must NOT be a literal ancestor of L in this fixture (PR #2623 shape)"

    _git("checkout", "-q", "main", cwd=root)
    (root / "A.txt").write_text("hello\n", encoding="utf-8")
    _git("add", "A.txt", cwd=root)
    _git("commit", "-q", "-m", f"squash merge {branch_name}", cwd=root)
    sha_m = _rev_parse(root, "HEAD")
    parents = _git("rev-list", "--parents", "-n", "1", sha_m, cwd=root).split()
    assert parents[1:] == [sha_base]

    wt_path = _make_worktree(root, branch_name, "ac4")
    assert _git("branch", "--show-current", cwd=root) == "main"

    pr_json = _make_pr_json(branch_name=branch_name, head_ref_oid=sha_h, merge_commit_oid=sha_m)
    env = _build_env(tmp_path, root, pr_json)

    result = _run_cleanup_exec_cli(
        root=root, env=env, pr_number=2628, linked_issue=2628,
        worktree_path=wt_path, branch_name=branch_name,
    )
    payload = json.loads(result.stdout)
    assert result.returncode == 0, payload
    assert payload["status"] == "ok", payload
    assert payload["verified"]["head_oid_match"] is False, payload
    assert payload["verified"]["head_equivalence_authorized"] is True, payload
    assert payload["verified"]["head_equivalence_mode"] == "squash_merge_delta_match", payload
    assert payload["actions_taken"] == ["worktree_remove", "branch_delete"], payload

    # Postcondition: the worktree and branch are ACTUALLY gone — no human
    # ran `git worktree remove` / `git branch -d` by hand.
    assert not wt_path.exists()
    assert not _branch_exists(root, branch_name)


# ─── AC5: un-integrated work fails closed; discard lane stays human-confirmed ──


def test_public_entry_fails_closed_and_discard_lane_stays_human_confirmed_for_unpublished_work(
    tmp_path,
):
    """GIVEN L has a genuine local-only content change beyond H that M does
    NOT contain (real un-integrated work, not a rename artifact) WHEN the
    normal-cleanup public CLI entry runs THEN it fails closed with
    ``pr_head_oid_mismatch`` and takes NO destructive action, AND the
    discard lane's existing ``--check`` (non-destructive, human-confirmation
    precursor) still correctly reports discard candidacy without performing
    any destructive action itself — confirming Issue #1523's human-confirmed
    ``--consume`` contract is unaffected by this Issue's two fixes."""
    root = tmp_path / "root"
    _init_repo(root)

    branch_name = "issue-2628-ac5"
    _git("checkout", "-q", "-b", branch_name, cwd=root)
    (root / "A.txt").write_text("hello\n", encoding="utf-8")
    _git("add", "A.txt", cwd=root)
    _git("commit", "-q", "-m", "add A", cwd=root)
    sha_h = _rev_parse(root, "HEAD")
    (root / "A.txt").write_text("hello-modified-locally\n", encoding="utf-8")
    _git("add", "A.txt", cwd=root)
    _git("commit", "-q", "-m", "local-only unintegrated change", cwd=root)
    sha_l = _rev_parse(root, "HEAD")

    ancestor_rc = subprocess.run(
        ["git", "merge-base", "--is-ancestor", sha_h, sha_l], cwd=str(root)
    ).returncode
    assert ancestor_rc == 0, "H must be a genuine structural ancestor of L in this fixture"

    _git("checkout", "-q", "main", cwd=root)
    (root / "A.txt").write_text("hello\n", encoding="utf-8")
    _git("add", "A.txt", cwd=root)
    _git("commit", "-q", "-m", f"squash merge {branch_name}", cwd=root)
    sha_m = _rev_parse(root, "HEAD")

    wt_path = _make_worktree(root, branch_name, "ac5")

    pr_json = _make_pr_json(branch_name=branch_name, head_ref_oid=sha_h, merge_commit_oid=sha_m)
    env = _build_env(tmp_path, root, pr_json)

    # 1. Normal-cleanup public CLI entry: must fail closed, no destructive action.
    cleanup_result = _run_cleanup_exec_cli(
        root=root, env=env, pr_number=2628, linked_issue=2628,
        worktree_path=wt_path, branch_name=branch_name,
    )
    cleanup_payload = json.loads(cleanup_result.stdout)
    assert cleanup_result.returncode == 1, cleanup_payload
    assert cleanup_payload["status"] == "refused", cleanup_payload
    assert cleanup_payload["reason_code"] == "pr_head_oid_mismatch", cleanup_payload
    assert cleanup_payload["verified"]["head_equivalence_authorized"] is False, cleanup_payload
    assert cleanup_payload["actions_taken"] == []
    assert wt_path.exists()
    assert _branch_exists(root, branch_name)

    # 2. Discard lane public CLI entry: --check is non-destructive and
    #    correctly reports discard candidacy (regression check — Issue #1523's
    #    existing lane is untouched by this Issue's fixes).
    check_result = subprocess.run(
        [
            sys.executable, str(MATERIALIZE_CONTRACT),
            "--pr-number", "2628",
            "--linked-issue-number", "2628",
            "--worktree-path", str(wt_path),
            "--branch-name", branch_name,
            "--operation", "local_only_discard",
            "--check",
            "--json",
        ],
        cwd=str(root), env=env, capture_output=True, text=True,
    )
    check_payload = json.loads(check_result.stdout)
    assert check_result.returncode == 0, check_payload
    assert check_payload["status"] == "confirmation_required", check_payload
    assert check_payload["verified"]["discard_candidate"] is True, check_payload
    assert check_payload["verified"]["pr_head_is_ancestor"] is True, check_payload
    assert check_payload["verified"]["local_only_commit_count"] == 1, check_payload
    assert check_payload["actions_taken"] == []

    # --check performs NO destructive action: worktree/branch still present,
    # and no contract file / confirmation was issued by --check itself
    # (only the plain issuance invocation issues a contract — Issue #1523
    # AC1's existing separation, unaffected here).
    assert wt_path.exists()
    assert _branch_exists(root, branch_name)
