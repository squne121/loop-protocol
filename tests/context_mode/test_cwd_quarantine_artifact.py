"""
tests/context_mode/test_cwd_quarantine_artifact.py

Artifact schema tests for Issue #826: context-mode cwd quarantine.
Verifies that cwd-comparison-result.json satisfies all required fields and
does not contain forbidden values.
"""
import json
import re
import pytest
from pathlib import Path

ARTIFACT_PATH = Path(".claude/artifacts/context-mode/cwd-comparison-result.json")
HOME_PATTERN = re.compile(r"/home/[a-z][a-z0-9_-]*/(?!projects/LOOP_PROTOCOL)")
TOKEN_PATTERN = re.compile(r"(?:ghp_|ghs_|sk-|xoxb-|xoxp-)[A-Za-z0-9]{10,}")


@pytest.fixture(scope="module")
def artifact():
    assert ARTIFACT_PATH.exists(), f"Artifact not found: {ARTIFACT_PATH}"
    with ARTIFACT_PATH.open() as f:
        return json.load(f)


class TestRequiredTopLevelFields:
    """AC2, AC6: Required top-level fields must be present and non-null."""

    def test_schema_present(self, artifact):
        assert artifact.get("schema") == "context_mode_cwd_quarantine_v1"

    def test_issue_present(self, artifact):
        assert artifact.get("issue") == "#826"

    def test_generated_at_non_empty(self, artifact):
        assert artifact.get("generated_at") and artifact["generated_at"] != "null"

    def test_fetched_at_non_empty(self, artifact):
        assert artifact.get("fetched_at") and artifact["fetched_at"] != "null"

    def test_context_mode_version_non_null(self, artifact):
        v = artifact.get("context_mode_version")
        assert v and v not in ("null", "unknown", "pending")

    def test_upstream_756_state(self, artifact):
        state = artifact.get("upstream_issue_756_state")
        assert state in ("open", "closed"), f"Unexpected state: {state}"

    def test_upstream_756_fetched_at(self, artifact):
        ft = artifact.get("upstream_issue_756_fetched_at") or artifact.get("fetched_at")
        assert ft and ft not in ("null", "pending", "unknown")


class TestProjectPolicy:
    """AC1: ctx_execute must be explicitly deny in project_policy."""

    def test_ctx_execute_deny(self, artifact):
        policy = artifact.get("project_policy", {})
        assert policy.get("ctx_execute") == "deny", (
            f"Expected 'deny', got: {policy.get('ctx_execute')}"
        )

    def test_policy_source_present(self, artifact):
        policy = artifact.get("project_policy", {})
        assert policy.get("policy_source") or policy.get("policy_source_note")


class TestProbe:
    """AC3: probe profile_committed must be False."""

    def test_profile_committed_false(self, artifact):
        probe = artifact.get("probe", {})
        local_probe = artifact.get("local_probe_result", {})
        assert (
            probe.get("profile_committed") is False
            or local_probe.get("profile_committed") is False
        ), "profile_committed must be False"


class TestCases:
    """AC4: Required case keys must be present."""

    REQUIRED_CASES = ["main_checkout", "linked_worktree", "nested_repo"]

    def test_all_cases_present(self, artifact):
        cases = artifact.get("cases", {})
        for key in self.REQUIRED_CASES:
            assert key in cases, f"Missing case: {key}"

    def test_main_checkout_bash_evidence(self, artifact):
        case = artifact["cases"]["main_checkout"]
        bash = case.get("bash_evidence", {})
        for field in ["pwd", "git_show_toplevel", "git_branch_current",
                      "git_rev_parse_head", "git_is_inside_work_tree"]:
            assert bash.get(field) not in (None, "null", "pending", "unknown"), (
                f"main_checkout.bash_evidence.{field} is null/stale"
            )

    def test_linked_worktree_bash_evidence(self, artifact):
        case = artifact["cases"]["linked_worktree"]
        bash = case.get("bash_evidence", {})
        # Either real values OR probe_blocked_by_policy status
        if "status" in bash:
            assert bash["status"] == "probe_blocked_by_policy"
        else:
            for field in ["pwd", "git_show_toplevel", "git_branch_current",
                          "git_rev_parse_head", "git_is_inside_work_tree"]:
                assert bash.get(field) not in (None, "null", "pending", "unknown"), (
                    f"linked_worktree.bash_evidence.{field} is null/stale"
                )

    def test_nested_repo_bash_evidence(self, artifact):
        case = artifact["cases"]["nested_repo"]
        bash = case.get("bash_evidence", {})
        if "status" in bash:
            assert bash["status"] == "probe_blocked_by_policy"
        else:
            for field in ["pwd", "git_show_toplevel", "git_branch_current",
                          "git_rev_parse_head", "git_is_inside_work_tree"]:
                assert bash.get(field) not in (None, "null", "pending", "unknown"), (
                    f"nested_repo.bash_evidence.{field} is null/stale"
                )


class TestInformationalOnly:
    """AC5: CLAUDE_PROJECT_DIR must be informational_only, not used for cwd judgment."""

    def test_informational_only_present(self, artifact):
        assert "informational_only" in artifact

    def test_claude_project_dir_present(self, artifact):
        info = artifact.get("informational_only", {})
        assert "claude_project_dir" in info


class TestVerdict:
    """AC8: verdict must be quarantine_continue or probe_blocked_quarantine_continue."""

    def test_verdict_valid(self, artifact):
        verdict = artifact.get("verdict")
        assert verdict in ("quarantine_continue", "probe_blocked_quarantine_continue"), (
            f"Unexpected verdict: {verdict}"
        )


class TestForbiddenValues:
    """AC10: Artifact must not contain forbidden values."""

    def _dump(self, artifact):
        return json.dumps(artifact)

    def test_no_null_string(self, artifact):
        text = self._dump(artifact)
        # Allow JSON null (None), but string "null" in values is forbidden
        # Check string "null" appears only where expected (not as a field value)
        import re
        # Find string "null" values specifically
        null_pattern = re.compile(r':\s*"null"')
        matches = null_pattern.findall(text)
        assert not matches, f"Found string 'null' values: {matches}"

    def test_no_pending_string(self, artifact):
        text = self._dump(artifact)
        assert '"pending"' not in text, "Found string 'pending' in artifact"

    def test_no_token_like_values(self, artifact):
        text = self._dump(artifact)
        match = TOKEN_PATTERN.search(text)
        assert not match, f"Found token-like value: {match.group()}"

    def test_no_unredacted_home_path(self, artifact):
        text = self._dump(artifact)
        match = HOME_PATTERN.search(text)
        assert not match, f"Found unredacted home path: {match.group()}"

    def test_no_stale_placeholder(self, artifact):
        """Angle-bracket placeholders like <worktree_path> must not appear in values.

        "<unset>" is a legitimate sentinel for env vars not set and is excluded.
        """
        text = self._dump(artifact)
        # Find angle-bracket placeholders in string values; exclude "<unset>" sentinel
        placeholder_pattern = re.compile(r'"<(?!unset>)[a-z][a-z0-9_]*>"')
        matches = placeholder_pattern.findall(text)
        assert not matches, f"Found stale placeholders: {matches}"

class TestAC883:
    """AC4, AC5 (Issue #883): SHA length and project_policy match effective settings."""

    SHA_PATTERN = re.compile(r"^[0-9a-f]{40}$")
    SETTINGS_PATH = Path(".claude/settings.json")
    EXECUTION_LIKE_TOOLS = [
        "mcp__context-mode__ctx_execute",
        "mcp__context-mode__ctx_batch_execute",
        "mcp__context-mode__ctx_execute_file",
        "mcp__context-mode__ctx_fetch_and_index",
    ]

    def test_head_sha_full_length(self, artifact):
        """AC4: All cases without bash_evidence.status must have 40-char lowercase hex SHA."""
        cases = artifact.get("cases", {})
        failures = []
        for case_name, case in cases.items():
            bash = case.get("bash_evidence", {})
            if "status" in bash:
                # probe_blocked_by_policy — no SHA available; skip
                continue
            sha = bash.get("git_rev_parse_head", "")
            if not self.SHA_PATTERN.match(sha):
                failures.append(
                    f"{case_name}.bash_evidence.git_rev_parse_head={sha!r} "
                    f"(expected 40-char lowercase hex)"
                )
        assert not failures, "Short or invalid SHAs found:\n" + "\n".join(failures)

    def test_project_policy_matches_effective_settings(self, artifact):
        """AC5: artifact.project_policy deny entries must match .claude/settings.json deny list."""
        assert self.SETTINGS_PATH.exists(), f"Settings not found: {self.SETTINGS_PATH}"
        with self.SETTINGS_PATH.open() as f:
            settings = json.load(f)
        effective_deny: list[str] = settings.get("permissions", {}).get("deny", [])

        policy = artifact.get("project_policy", {})
        mismatches = []
        for tool in self.EXECUTION_LIKE_TOOLS:
            tool_short = tool.replace("mcp__context-mode__", "")
            artifact_val = policy.get(tool_short)
            in_settings_deny = tool in effective_deny
            if artifact_val == "deny" and not in_settings_deny:
                mismatches.append(
                    f"{tool_short}: artifact says 'deny' but {tool!r} not in settings.json deny"
                )
            elif artifact_val != "deny" and in_settings_deny:
                mismatches.append(
                    f"{tool_short}: {tool!r} is in settings.json deny but artifact says {artifact_val!r}"
                )
        assert not mismatches, "project_policy mismatch with settings.json:\n" + "\n".join(mismatches)
