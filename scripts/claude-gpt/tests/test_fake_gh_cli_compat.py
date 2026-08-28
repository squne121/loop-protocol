"""Issue #2330: subprocess-based tests for `fake_gh.py`'s production
consumer `gh` CLI compatibility.

`.claude/skills/issue-refinement-loop/scripts/root_entry_router.py` (the
`issue-refinement-loop` root-entry `gh` consumer) issues two exact `gh`
invocation shapes that `scripts/claude-gpt/tests/fixtures/fake_gh.py`
previously did not understand -- a compatibility gap PR #2325's
pr-reviewer flagged as non-blocking (P1-2), later confirmed as the
authoritative scope for this Issue by an OWNER adversarial review
(https://github.com/squne121/loop-protocol/issues/2330#issuecomment-5456488711):

  - `gh repo view <owner>/<repo> --json nameWithOwner --jq .nameWithOwner`
    -- a POSITIONAL repo argument directly after `view` (not `--repo
    <repo>`), expecting a bare-string (`--jq`-filtered, not JSON-quoted)
    stdout.
  - `gh api repos/<owner>/<repo>/git/refs/heads/<base_ref> --jq
    .object.sha` -- `<base_ref>` may itself contain `/` (e.g.
    `release/next`, not hardcoded to `main`), expecting a bare 40-hex fake
    SHA string (not JSON-quoted) stdout.

These tests spawn `fake_gh.py` itself as a real, hermetic local Python
subprocess -- no real `gh` CLI, no network, no live GitHub API call
(`docs/dev/runtime-verification-policy.md` /
`docs/dev/extension-surface-runtime-policy.yaml`'s
`claude-gpt-lifecycle-invocation-change` rule, `default_decision:
immediate`). If the local Python subprocess execution environment itself is
unavailable (unexpected CI constraint), the tests SKIP (exit 77, `SKIP:`
stdout prefix) per the runtime-verification-policy SKIP convention instead
of silently passing or falling back to an in-process call.
"""

from __future__ import annotations

import os
import re
import subprocess
import sys
from pathlib import Path

import pytest

_TESTS_DIR = Path(__file__).resolve().parent
_FIXTURE = _TESTS_DIR / "fixtures" / "fake_gh.py"

# Matches the production consumer's exact repo identity
# (`root_entry_router.py` line ~612: `["gh", "repo", "view", self.repo,
# "--json", "nameWithOwner", "--jq", ".nameWithOwner"]`).
_REPO = "squne121/loop-protocol"

_HEX40_RE = re.compile(r"^[0-9a-f]{40}$")


def _run_fake_gh(tmp_path: Path, args: list[str]) -> subprocess.CompletedProcess:
    """Spawn `fake_gh.py` as a real subprocess with the given argv.

    `args` mirrors exactly what real `gh` would receive as `sys.argv[1:]`
    (the fixture only reads `sys.argv[1:]`, so invoking it directly via
    `python3 fake_gh.py <args>` is argv-equivalent to a PATH-shadowed `gh
    <args>` invocation -- no PATH shadowing scaffolding needed for these
    exact-argv-contract tests).
    """
    state_path = tmp_path / "fake_gh_state.json"
    env = dict(os.environ)
    env["FAKE_GH_STATE"] = str(state_path)
    try:
        return subprocess.run(
            [sys.executable, str(_FIXTURE), *args],
            capture_output=True,
            text=True,
            timeout=30,
            env=env,
        )
    except subprocess.TimeoutExpired as exc:
        # PR #2377 OWNER REQUEST_CHANGES fix_delta (P1-2):
        # `subprocess.TimeoutExpired` is a `subprocess.SubprocessError`
        # subclass, so a bare `except (OSError, subprocess.SubprocessError)`
        # previously mis-classified a genuinely hung `fake_gh.py` invocation
        # as an "environment unavailable" SKIP (exit 77). Per Issue #2330's
        # contract, SKIP is reserved for the local Python subprocess
        # execution environment itself being unavailable -- a real hang is a
        # FAIL, not a SKIP.
        pytest.fail(f"fake_gh.py subprocess timed out: {exc}")
    except OSError as exc:
        # Issue #2330 Stop Condition: SKIP (exit 77), never a silent pass
        # or an in-process fallback, when the local Python subprocess
        # execution environment itself is unavailable.
        pytest.exit(
            f"SKIP: python3 subprocess execution unavailable for fake_gh.py "
            f"CLI-compat tests: {exc!r}",
            returncode=77,
        )


# =============================================================================
# AC1 / GIVEN the production consumer's exact `gh repo view` argv shape
# (positional repo, `--json nameWithOwner --jq .nameWithOwner`), WHEN
# `fake_gh.py` is invoked as a subprocess with that argv, THEN stdout is the
# bare repo string (no JSON quoting) and exit code is 0.
# =============================================================================


def test_repo_view_positional_bare_string(tmp_path):
    proc = _run_fake_gh(
        tmp_path,
        ["repo", "view", _REPO, "--json", "nameWithOwner", "--jq", ".nameWithOwner"],
    )
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout.strip() == _REPO
    # Bare string: no JSON quoting characters present.
    assert '"' not in proc.stdout


# =============================================================================
# AC2 / GIVEN the production consumer's exact `gh api .../git/refs/heads/
# <base_ref> --jq .object.sha` argv shape, WHEN `fake_gh.py` is invoked as a
# subprocess with `main` and with a slash-containing `base_ref` (e.g.
# `release/next`), THEN stdout is a deterministic bare 40-hex fake SHA (no
# JSON quoting) and exit code is 0 for BOTH -- not hardcoded to `main`.
# =============================================================================


def test_api_refs_heads_bare_sha_variable_base_ref(tmp_path):
    proc_main = _run_fake_gh(
        tmp_path,
        ["api", f"repos/{_REPO}/git/refs/heads/main", "--jq", ".object.sha"],
    )
    assert proc_main.returncode == 0, proc_main.stderr
    sha_main = proc_main.stdout.strip()
    assert _HEX40_RE.match(sha_main), f"expected bare 40-hex SHA, got {sha_main!r}"
    assert '"' not in proc_main.stdout

    # `base_ref` containing `/` (e.g. `release/next`) must be handled as the
    # FULL suffix after `/git/refs/heads/`, not truncated at the first `/`.
    proc_release = _run_fake_gh(
        tmp_path,
        ["api", f"repos/{_REPO}/git/refs/heads/release/next", "--jq", ".object.sha"],
    )
    assert proc_release.returncode == 0, proc_release.stderr
    sha_release = proc_release.stdout.strip()
    assert _HEX40_RE.match(sha_release), f"expected bare 40-hex SHA, got {sha_release!r}"
    assert '"' not in proc_release.stdout

    # Deterministic: re-running the same argv reproduces the same fake SHA.
    proc_main_again = _run_fake_gh(
        tmp_path,
        ["api", f"repos/{_REPO}/git/refs/heads/main", "--jq", ".object.sha"],
    )
    assert proc_main_again.stdout.strip() == sha_main

    # Different `base_ref` values must not collapse to the same fake SHA
    # (proves `base_ref` is actually consumed, not ignored/hardcoded).
    assert sha_release != sha_main


# =============================================================================
# AC4 / GIVEN the pre-existing `gh repo view` `--repo <repo>` flag form
# (Issue #2273/#2306-era callers), WHEN `fake_gh.py` is invoked with that
# older shape, THEN it still exits 0 (backward compatibility is preserved
# alongside the new positional-repo shape).
# =============================================================================


def test_repo_view_legacy_flag_form_still_exits_zero(tmp_path):
    proc = _run_fake_gh(
        tmp_path,
        ["repo", "view", "--repo", _REPO, "--json", "nameWithOwner"],
    )
    assert proc.returncode == 0, proc.stderr


# =============================================================================
# PR #2377 OWNER REQUEST_CHANGES fix_delta (P1-1) / GIVEN near-miss argv that
# only superficially resembles the two exact-argv shapes above, WHEN
# `fake_gh.py` is invoked with that near-miss argv, THEN it fails closed
# (non-zero exit) instead of being silently accepted as a success.
# =============================================================================


def test_api_refs_heads_extra_trailing_token_is_rejected(tmp_path):
    proc = _run_fake_gh(
        tmp_path,
        [
            "api",
            f"repos/{_REPO}/git/refs/heads/main",
            "--jq",
            ".object.sha",
            "--method",
            "DELETE",
        ],
    )
    assert proc.returncode != 0


def test_api_refs_heads_empty_base_ref_is_rejected(tmp_path):
    proc = _run_fake_gh(
        tmp_path,
        ["api", f"repos/{_REPO}/git/refs/heads/", "--jq", ".object.sha"],
    )
    assert proc.returncode != 0


def test_api_refs_heads_missing_owner_repo_structure_is_rejected(tmp_path):
    proc = _run_fake_gh(
        tmp_path,
        ["api", "repos/owner/git/refs/heads/main", "--jq", ".object.sha"],
    )
    assert proc.returncode != 0


def test_api_refs_heads_unexpected_endpoint_before_marker_is_rejected(tmp_path):
    proc = _run_fake_gh(
        tmp_path,
        [
            "api",
            f"repos/{_REPO}/other/git/refs/heads/main",
            "--jq",
            ".object.sha",
        ],
    )
    assert proc.returncode != 0


def test_repo_view_wrong_jq_selector_is_rejected(tmp_path):
    proc = _run_fake_gh(
        tmp_path,
        ["repo", "view", _REPO, "--json", "nameWithOwner", "--jq", ".wrongSelector"],
    )
    assert proc.returncode != 0


def test_repo_view_extra_trailing_token_is_rejected(tmp_path):
    proc = _run_fake_gh(
        tmp_path,
        [
            "repo",
            "view",
            _REPO,
            "--json",
            "nameWithOwner",
            "--jq",
            ".nameWithOwner",
            "extra",
        ],
    )
    assert proc.returncode != 0
