"""
Tests for GitHub metadata assertion in baseline_vc_preflight (Issue #942).

Test function names embed the AC verification substrings so each Verification
Command (`pytest ... -k <substring>`) collects at least one test:
- AC1: github_metadata_assert_recognized
- AC2: github_metadata_assert_endpoint_subset
- AC3: github_metadata_assert_not_contains
- AC4: github_metadata_assert_contains
- AC5: github_metadata_assert_dangerous_flags_blocked
- AC6: github_metadata_assert_mutating_method_blocked
- AC7: github_metadata_assert_environment_error

The AC1/AC7 wiring tests run the real main() dispatch via a subprocess with a fake
`gh` binary on PATH; no real GitHub access happens. All other tests mock subprocess.run.
"""

import json
import os
import stat
import subprocess
import sys
import tempfile
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[4]
_SCRIPTS_DIR = _REPO_ROOT / ".claude" / "skills" / "issue-contract-review" / "scripts"
if str(_SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS_DIR))

_SCRIPT_PATH = _SCRIPTS_DIR / "baseline_vc_preflight.py"

from baseline_vc_preflight import (
    classify_static_command,
    _is_github_metadata_assert_command,
    _is_allowed_github_metadata_assert,
    _check_github_metadata_assertion,
)


def _run_preflight_with_fake_gh(
    body_content: str, gh_stdout: str, gh_exit: int = 0, gh_stderr: str = ""
) -> dict:
    """
    Run baseline_vc_preflight.py as a subprocess (full classify->execute pipeline)
    with a fake ``gh`` binary injected on PATH so no real GitHub access happens.

    The fake gh ignores args, prints ``gh_stdout``/``gh_stderr`` and exits ``gh_exit``.
    This exercises the Issue #942 wiring: github_metadata_assert is classified as
    allowed (static_result is None) and dispatched to _check_github_metadata_assertion
    -> ``gh api``, rather than being literally executed (which would yield
    "No such file or directory").
    """
    tmpdir = tempfile.mkdtemp()
    fake_gh = Path(tmpdir) / "gh"
    fake_gh.write_text(
        "#!/usr/bin/env python3\n"
        "import sys\n"
        f"sys.stdout.write({gh_stdout!r})\n"
        f"sys.stderr.write({gh_stderr!r})\n"
        f"sys.exit({gh_exit})\n"
    )
    fake_gh.chmod(fake_gh.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)

    body_file = Path(tmpdir) / "body.md"
    body_file.write_text(body_content)

    env = dict(os.environ)
    env["PATH"] = f"{tmpdir}{os.pathsep}{env.get('PATH', '')}"

    result = subprocess.run(
        [sys.executable, str(_SCRIPT_PATH), "--body-file", str(body_file), "--issue", "942"],
        capture_output=True,
        text=True,
        timeout=30,
        env=env,
    )
    assert result.stdout, f"No output from preflight: stderr={result.stderr}"
    return json.loads(result.stdout)


# ---------------------------------------------------------------------------
# AC1: Recognition as a first-class command + classify->execute wiring
# ---------------------------------------------------------------------------

class TestGitHubMetadataAssertRecognized:
    """AC1: github_metadata_assert is recognized and wired to execution."""

    def test_github_metadata_assert_recognized(self):
        """AC1: github_metadata_assert command is recognized."""
        cmd = 'github_metadata_assert contains description "forbidden" repos/owner/repo/milestones/1'
        assert _is_github_metadata_assert_command(cmd) is True

    def test_github_metadata_assert_recognized_with_spaces(self):
        """AC1: leading/extra whitespace still recognized."""
        cmd = '  github_metadata_assert  contains  description  "t"  repos/owner/repo/milestones/1'
        assert _is_github_metadata_assert_command(cmd) is True

    def test_github_metadata_assert_recognized_other_command_is_not(self):
        """AC1: a raw gh api command is NOT a github_metadata_assert command."""
        assert _is_github_metadata_assert_command('gh api repos/owner/repo/milestones/1') is False

    def test_github_metadata_assert_recognized_classified_allowed(self):
        """AC1: a valid invocation is classified allowed (static_result is None)."""
        cmd = 'github_metadata_assert not_contains description forbidden repos/owner/repo/milestones/1'
        assert classify_static_command(cmd, _REPO_ROOT) is None

    def test_github_metadata_assert_recognized_wiring_executes_via_check(self):
        """
        AC1 wiring: an allowed github_metadata_assert VC flows classify -> execute via
        _check_github_metadata_assertion (gh api), NOT a literal exec of a non-existent
        'github_metadata_assert' binary. Proven end-to-end through main() with a fake gh.
        """
        body = (
            "## Verification Commands\n\n"
            "```bash\n"
            "# AC1\n"
            "$ github_metadata_assert not_contains description forbidden repos/owner/repo/milestones/1\n"
            "```\n"
        )
        # Description does NOT contain 'forbidden' -> not_contains holds -> exit 0.
        data = _run_preflight_with_fake_gh(
            body, json.dumps({"description": "a safe milestone"}), gh_exit=0
        )
        results = data["results"]
        assert len(results) == 1
        r = results[0]
        assert r["classification"] == "expected_pass", r
        assert r["category"] == "github_metadata_assert_pass", r
        assert r["decision"] == "go", r
        assert r["exit_code"] == 0, r
        # Must NOT have been literally executed as a missing binary.
        assert "No such file or directory" not in json.dumps(r)
        assert r["category"] != "file_not_found_unrunnable"

    def test_github_metadata_assert_recognized_wiring_fail_is_expected_fail(self):
        """
        AC1 wiring (companion): assertion-not-holding maps to expected_fail/go via the
        real main() dispatch, distinct from environment error.
        """
        body = (
            "## Verification Commands\n\n"
            "```bash\n"
            "# AC1\n"
            "$ github_metadata_assert not_contains description forbidden repos/owner/repo/milestones/1\n"
            "```\n"
        )
        # Description CONTAINS 'forbidden' -> not_contains fails -> exit 1.
        data = _run_preflight_with_fake_gh(
            body, json.dumps({"description": "this is forbidden text"}), gh_exit=0
        )
        r = data["results"][0]
        assert r["classification"] == "expected_fail", r
        assert r["category"] == "github_metadata_assert_fail", r
        assert r["decision"] == "go", r
        assert r["exit_code"] == 1, r


# ---------------------------------------------------------------------------
# AC2: Endpoint subset validation
# ---------------------------------------------------------------------------

class TestGitHubMetadataAssertEndpointSubset:
    """AC2: only repos/<owner>/<repo>/milestones/<number> is accepted."""

    def test_github_metadata_assert_endpoint_subset_accepts_valid(self):
        """AC2: valid milestone endpoint is accepted."""
        argv = 'github_metadata_assert contains description test repos/myowner/myrepo/milestones/123'.split()
        is_valid, _ = _is_allowed_github_metadata_assert(argv)
        assert is_valid is True

    def test_github_metadata_assert_endpoint_subset_rejects_absolute_url(self):
        """AC2: absolute URL endpoint is rejected."""
        argv = 'github_metadata_assert contains description test https://api.github.com/repos/o/r/milestones/1'.split()
        is_valid, msg = _is_allowed_github_metadata_assert(argv)
        assert is_valid is False
        assert "absolute" in msg.lower()

    def test_github_metadata_assert_endpoint_subset_rejects_query_string(self):
        """AC2: query string endpoint is rejected."""
        argv = 'github_metadata_assert contains description test repos/o/r/milestones/1?state=open'.split()
        is_valid, msg = _is_allowed_github_metadata_assert(argv)
        assert is_valid is False
        assert "query" in msg.lower()

    def test_github_metadata_assert_endpoint_subset_rejects_path_traversal(self):
        """AC2: path traversal endpoint is rejected."""
        argv = 'github_metadata_assert contains description test repos/o/../fake/milestones/1'.split()
        is_valid, msg = _is_allowed_github_metadata_assert(argv)
        assert is_valid is False
        assert ".." in msg

    def test_github_metadata_assert_endpoint_subset_rejects_placeholder(self):
        """AC2: placeholder endpoint is rejected."""
        argv = 'github_metadata_assert contains description test repos/<owner>/<repo>/milestones/<number>'.split()
        is_valid, msg = _is_allowed_github_metadata_assert(argv)
        assert is_valid is False
        assert "placeholder" in msg.lower()

    def test_github_metadata_assert_endpoint_subset_rejects_non_milestone(self):
        """AC2: non-milestone resource is rejected."""
        argv = 'github_metadata_assert contains description test repos/o/r/issues/1'.split()
        is_valid, msg = _is_allowed_github_metadata_assert(argv)
        assert is_valid is False
        assert "milestones" in msg.lower()


# ---------------------------------------------------------------------------
# AC3: not_contains assertion exit codes
# ---------------------------------------------------------------------------

class TestGitHubMetadataAssertNotContains:
    """AC3: not_contains -> present=non-zero, absent=zero."""

    @patch('subprocess.run')
    def test_github_metadata_assert_not_contains_present_exits_nonzero(self, mock_run):
        """AC3: not_contains with literal present returns exit 1."""
        mock_run.return_value = MagicMock(
            returncode=0, stdout='{"description": "forbidden phrase here"}', stderr=''
        )
        result = _check_github_metadata_assertion(
            'not_contains', 'description', 'forbidden phrase', 'repos/o/r/milestones/1'
        )
        assert result == 1

    @patch('subprocess.run')
    def test_github_metadata_assert_not_contains_absent_exits_zero(self, mock_run):
        """AC3: not_contains with literal absent returns exit 0."""
        mock_run.return_value = MagicMock(
            returncode=0, stdout='{"description": "safe description"}', stderr=''
        )
        result = _check_github_metadata_assertion(
            'not_contains', 'description', 'forbidden phrase', 'repos/o/r/milestones/1'
        )
        assert result == 0


# ---------------------------------------------------------------------------
# AC4: contains assertion exit codes
# ---------------------------------------------------------------------------

class TestGitHubMetadataAssertContains:
    """AC4: contains -> present=zero, absent=non-zero."""

    @patch('subprocess.run')
    def test_github_metadata_assert_contains_present_exits_zero(self, mock_run):
        """AC4: contains with literal present returns exit 0."""
        mock_run.return_value = MagicMock(
            returncode=0, stdout='{"description": "required phrase here"}', stderr=''
        )
        result = _check_github_metadata_assertion(
            'contains', 'description', 'required phrase', 'repos/o/r/milestones/1'
        )
        assert result == 0

    @patch('subprocess.run')
    def test_github_metadata_assert_contains_absent_exits_nonzero(self, mock_run):
        """AC4: contains with literal absent returns exit 1."""
        mock_run.return_value = MagicMock(
            returncode=0, stdout='{"description": "other text"}', stderr=''
        )
        result = _check_github_metadata_assertion(
            'contains', 'description', 'required phrase', 'repos/o/r/milestones/1'
        )
        assert result == 1


# ---------------------------------------------------------------------------
# AC5: Dangerous flags blocked
# ---------------------------------------------------------------------------

class TestGitHubMetadataAssertDangerousFlags:
    """AC5: data-modification / non-GET flags are blocked."""

    @pytest.mark.parametrize(
        "flag",
        [
            "-f", "-F", "--field", "--raw-field", "--input", "--header", "-H",
            "--include", "-i", "--paginate", "--slurp", "--cache", "--template", "--preview",
        ],
    )
    def test_github_metadata_assert_dangerous_flags_blocked(self, flag):
        """AC5: each dangerous flag is blocked."""
        argv = f'github_metadata_assert contains description test repos/o/r/milestones/1 {flag} val'.split()
        is_valid, _ = _is_allowed_github_metadata_assert(argv)
        assert is_valid is False

    def test_github_metadata_assert_dangerous_flags_blocked_graphql(self):
        """AC5: graphql keyword is blocked (rejected as an extra token by the exact-arity rule)."""
        argv = 'github_metadata_assert contains description test repos/o/r/milestones/1 graphql'.split()
        is_valid, _ = _is_allowed_github_metadata_assert(argv)
        assert is_valid is False

    def test_github_metadata_assert_dangerous_flags_blocked_equals_form(self):
        """AC5: '--cache=off' (=value form) is blocked."""
        argv = 'github_metadata_assert contains description test repos/o/r/milestones/1 --cache=off'.split()
        is_valid, _ = _is_allowed_github_metadata_assert(argv)
        assert is_valid is False

    def test_github_metadata_assert_dangerous_flags_blocked_via_classify(self):
        """AC5: dangerous flags are blocked through classify_static_command too."""
        cmd = 'github_metadata_assert contains description test repos/o/r/milestones/1 -f key=value'
        result = classify_static_command(cmd, _REPO_ROOT)
        assert result is not None
        assert result[2] == "blocked"


# ---------------------------------------------------------------------------
# AC6: Mutating methods blocked
# ---------------------------------------------------------------------------

class TestGitHubMetadataAssertMutatingMethod:
    """AC6: mutating HTTP methods are blocked."""

    @pytest.mark.parametrize("method", ["POST", "PATCH", "PUT", "DELETE"])
    def test_github_metadata_assert_mutating_method_blocked(self, method):
        """AC6: --method POST/PATCH/PUT/DELETE blocked."""
        argv = f'github_metadata_assert contains description test repos/o/r/milestones/1 --method {method}'.split()
        is_valid, _ = _is_allowed_github_metadata_assert(argv)
        assert is_valid is False

    @pytest.mark.parametrize("method", ["POST", "PATCH", "PUT", "DELETE"])
    def test_github_metadata_assert_mutating_method_blocked_dash_x(self, method):
        """AC6: -X <method> blocked."""
        argv = f'github_metadata_assert contains description test repos/o/r/milestones/1 -X {method}'.split()
        is_valid, _ = _is_allowed_github_metadata_assert(argv)
        assert is_valid is False

    def test_github_metadata_assert_mutating_method_blocked_equals(self):
        """AC6: --method=POST variant blocked."""
        argv = 'github_metadata_assert contains description test repos/o/r/milestones/1 --method=POST'.split()
        is_valid, _ = _is_allowed_github_metadata_assert(argv)
        assert is_valid is False

    def test_github_metadata_assert_mutating_method_blocked_lowercase(self):
        """AC6: lowercase --method post variant blocked."""
        argv = 'github_metadata_assert contains description test repos/o/r/milestones/1 --method post'.split()
        is_valid, _ = _is_allowed_github_metadata_assert(argv)
        assert is_valid is False

    def test_github_metadata_assert_mutating_method_blocked_via_classify(self):
        """AC6: mutating method is blocked through classify_static_command too."""
        cmd = 'github_metadata_assert contains description test repos/o/r/milestones/1 --method POST'
        result = classify_static_command(cmd, _REPO_ROOT)
        assert result is not None
        assert result[2] == "blocked"


# ---------------------------------------------------------------------------
# AC7: Environment errors classified separately (never a false pass)
# ---------------------------------------------------------------------------

class TestGitHubMetadataAssertEnvironmentError:
    """AC7: env errors -> distinct non-(0/1) codes; never a false pass."""

    @patch('subprocess.run')
    def test_github_metadata_assert_environment_error_gh_not_found(self, mock_run):
        """AC7: gh not found returns 2."""
        mock_run.side_effect = FileNotFoundError()
        assert _check_github_metadata_assertion('contains', 'desc', 't', 'repos/o/r/milestones/1') == 2

    @patch('subprocess.run')
    def test_github_metadata_assert_environment_error_auth(self, mock_run):
        """AC7: auth failure returns 3."""
        mock_run.return_value = MagicMock(returncode=401, stdout='', stderr='401 authentication failed')
        assert _check_github_metadata_assertion('contains', 'desc', 't', 'repos/o/r/milestones/1') == 3

    @patch('subprocess.run')
    def test_github_metadata_assert_environment_error_404(self, mock_run):
        """AC7: 404 returns 4."""
        mock_run.return_value = MagicMock(returncode=404, stdout='', stderr='404 not found')
        assert _check_github_metadata_assertion('contains', 'desc', 't', 'repos/o/r/milestones/1') == 4

    @patch('subprocess.run')
    def test_github_metadata_assert_environment_error_rate_limit(self, mock_run):
        """AC7: rate limit returns 5."""
        mock_run.return_value = MagicMock(returncode=429, stdout='', stderr='429 rate limit')
        assert _check_github_metadata_assertion('contains', 'desc', 't', 'repos/o/r/milestones/1') == 5

    @patch('subprocess.run')
    def test_github_metadata_assert_environment_error_timeout(self, mock_run):
        """AC7: timeout returns 6."""
        mock_run.side_effect = subprocess.TimeoutExpired('gh', 10)
        assert _check_github_metadata_assertion('contains', 'desc', 't', 'repos/o/r/milestones/1') == 6

    @patch('subprocess.run')
    def test_github_metadata_assert_environment_error_invalid_json(self, mock_run):
        """AC7: invalid JSON returns 7."""
        mock_run.return_value = MagicMock(returncode=0, stdout='not valid json {', stderr='')
        assert _check_github_metadata_assertion('contains', 'desc', 't', 'repos/o/r/milestones/1') == 7

    def test_github_metadata_assert_environment_error_not_false_pass_wiring(self):
        """
        AC7 wiring: when gh fails (404), the full main() dispatch classifies the VC as
        human_judgment (environment error) and never go (false pass). Proven end-to-end.
        """
        body = (
            "## Verification Commands\n\n"
            "```bash\n"
            "# AC7\n"
            "$ github_metadata_assert not_contains description forbidden repos/owner/repo/milestones/1\n"
            "```\n"
        )
        data = _run_preflight_with_fake_gh(body, "", gh_exit=1, gh_stderr="HTTP 404: Not Found")
        r = data["results"][0]
        assert r["classification"] == "human_judgment", r
        assert r["category"] == "github_metadata_assert_environment_error", r
        assert r["decision"] == "human_judgment", r
        assert r["decision"] != "go", r
        assert r["exit_code"] == 4, r


# ---------------------------------------------------------------------------
# Review hardening: false-pass holes closed (BLOCKER 1/2/3, MAJOR 1/2)
# ---------------------------------------------------------------------------

class TestGitHubMetadataAssertNoFalsePass:
    """Field allowlist, exact arity, and environment-vs-semantic failure split."""

    def test_github_metadata_assert_field_typo_rejected(self):
        """BLOCKER 1/MAJOR 2: a field outside the allowlist (e.g. a 'description' typo) is rejected."""
        argv = 'github_metadata_assert not_contains descripton forbidden repos/o/r/milestones/1'.split()
        is_valid, msg = _is_allowed_github_metadata_assert(argv)
        assert is_valid is False
        assert "field" in msg.lower()

    @patch('subprocess.run')
    def test_github_metadata_assert_missing_field_is_not_absent_pass(self, mock_run):
        """
        BLOCKER 1: when the API response lacks the requested field, not_contains must NOT
        return exit 0 (absent). A field missing from the parsed JSON is a schema/environment
        error, not 'literal absent' - otherwise a schema drift would silently false-pass.
        """
        mock_run.return_value = MagicMock(
            returncode=0, stdout='{"title": "m", "state": "open"}', stderr=''
        )
        result = _check_github_metadata_assertion(
            'not_contains', 'description', 'forbidden', 'repos/o/r/milestones/1'
        )
        assert result not in (0, 1), result
        assert result == 9, result

    def test_github_metadata_assert_exactly_five_argv_rejects_extra_positional(self):
        """BLOCKER 2: an extra positional token beyond the 4 arguments is rejected."""
        argv = 'github_metadata_assert contains description test repos/o/r/milestones/1 extra'.split()
        is_valid, _ = _is_allowed_github_metadata_assert(argv)
        assert is_valid is False

    @pytest.mark.parametrize(
        "stderr",
        [
            "HTTP 500: internal server error",
            "connection reset by peer",
            "could not resolve host",
            "x509: certificate has expired",
        ],
    )
    @patch('subprocess.run')
    def test_github_metadata_assert_unknown_gh_failure_is_environment_error(self, mock_run, stderr):
        """
        BLOCKER 3: an unknown nonzero gh failure (raw exit 1) must NOT be reported as a
        semantic assertion failure (exit 1). It is an environment error (exit 8).
        """
        mock_run.return_value = MagicMock(returncode=1, stdout='', stderr=stderr)
        result = _check_github_metadata_assertion(
            'not_contains', 'description', 'forbidden', 'repos/o/r/milestones/1'
        )
        assert result not in (0, 1), (stderr, result)
        assert result == 8, (stderr, result)

    @patch('subprocess.run')
    def test_github_metadata_assert_unknown_gh_failure_empty_stderr_is_environment_error(self, mock_run):
        """BLOCKER 3: bare nonzero exit with no recognizable stderr is still an env error, not exit 1."""
        mock_run.return_value = MagicMock(returncode=1, stdout='', stderr='')
        result = _check_github_metadata_assertion(
            'contains', 'description', 'required', 'repos/o/r/milestones/1'
        )
        assert result == 8, result
