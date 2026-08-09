"""
test_command_registry.py

Tests for command_registry.py — ISSUE_REFINEMENT_COMMAND_REGISTRY_V1

Covers AC1, AC2, AC3, AC5, AC6, AC8.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

SCRIPTS_DIR = Path(__file__).resolve().parent.parent / "scripts"
sys.path.insert(0, str(SCRIPTS_DIR))

import command_registry as reg  # noqa: E402


# ---------------------------------------------------------------------------
# AC1: --list returns ISSUE_REFINEMENT_COMMAND_REGISTRY_V1 JSON
# ---------------------------------------------------------------------------

class TestRegistryListOutput:
    def test_list_schema_version(self):
        """--list outputs schema: ISSUE_REFINEMENT_COMMAND_REGISTRY_V1."""
        result = subprocess.run(
            [sys.executable, str(SCRIPTS_DIR / "command_registry.py"), "--list"],
            capture_output=True,
            text=True,
        )
        assert result.returncode == 0, result.stderr
        data = json.loads(result.stdout)
        assert data["schema"] == "ISSUE_REFINEMENT_COMMAND_REGISTRY_V1"

    def test_list_has_commands_dict(self):
        """--list output contains 'commands' dict."""
        result = subprocess.run(
            [sys.executable, str(SCRIPTS_DIR / "command_registry.py"), "--list"],
            capture_output=True,
            text=True,
        )
        assert result.returncode == 0
        data = json.loads(result.stdout)
        assert "commands" in data
        assert isinstance(data["commands"], dict)

    def test_list_required_fields_per_entry(self):
        """Every registry entry has all required fields per AC1."""
        required_fields = {
            "id", "argv", "cwd_policy", "stdin_contract",
            "stdout_contract", "timeout_seconds", "mutation", "placeholders",
        }
        data = reg.export_registry()
        for cmd_id, entry in data["commands"].items():
            missing = required_fields - set(entry.keys())
            assert not missing, f"Entry {cmd_id!r} missing fields: {missing}"


# ---------------------------------------------------------------------------
# AC2: registry commands use argv: list[str] — no shell strings
# ---------------------------------------------------------------------------

class TestArgvOnlyCanonicalForm:
    def test_all_argv_are_lists(self):
        """Every registry entry has argv as list[str]."""
        for cmd_id, entry in reg.REGISTRY.items():
            argv = entry["argv"]
            assert isinstance(argv, list), f"{cmd_id}: argv must be list"
            for token in argv:
                assert isinstance(token, str), f"{cmd_id}: argv tokens must be str, got {type(token)}"

    def test_no_shell_string_in_argv(self):
        """argv tokens must not contain shell operators or compound expressions."""
        shell_chars = ["&&", "||", ";", "|", ">", "<", ">>", "<<", "`", "$("]
        for cmd_id, entry in reg.REGISTRY.items():
            argv = entry["argv"]
            for token in argv:
                for char in shell_chars:
                    assert char not in token, (
                        f"{cmd_id}: argv token {token!r} contains shell operator {char!r}"
                    )


# ---------------------------------------------------------------------------
# AC3: render_command — placeholder type validation, fail-closed
# ---------------------------------------------------------------------------

class TestRenderCommandValidation:
    def test_render_valid_preflight(self):
        """render_command returns valid argv for preflight.run with correct params."""
        argv = reg.render_command("preflight.run", {"issue_number": 42, "repo": "owner/repo"})
        assert isinstance(argv, list)
        assert "42" in argv
        assert "owner/repo" in argv

    def test_render_valid_gh_issue_view(self):
        """render_command works for gh.issue.view."""
        argv = reg.render_command("gh.issue.view", {"issue_number": 100, "repo": "foo/bar"})
        assert "100" in argv
        assert "foo/bar" in argv

    def test_render_invalid_issue_number_zero(self):
        """issue_number=0 is rejected (fail-closed)."""
        with pytest.raises(ValueError, match="positive_int|must be > 0"):
            reg.render_command("preflight.run", {"issue_number": 0, "repo": "owner/repo"})

    def test_render_invalid_issue_number_negative(self):
        """issue_number=-1 is rejected."""
        with pytest.raises(ValueError):
            reg.render_command("preflight.run", {"issue_number": -1, "repo": "owner/repo"})

    def test_render_invalid_issue_number_string(self):
        """issue_number='abc' is rejected."""
        with pytest.raises(ValueError):
            reg.render_command("preflight.run", {"issue_number": "abc", "repo": "owner/repo"})

    def test_render_invalid_repo_no_slash(self):
        """repo without slash is rejected."""
        with pytest.raises(ValueError, match="owner/repo"):
            reg.render_command("preflight.run", {"issue_number": 1, "repo": "notaslash"})

    def test_render_invalid_repo_empty(self):
        """empty repo is rejected."""
        with pytest.raises(ValueError):
            reg.render_command("preflight.run", {"issue_number": 1, "repo": ""})

    def test_render_unknown_command_id(self):
        """Unknown command_id raises KeyError."""
        with pytest.raises(KeyError):
            reg.render_command("nonexistent.command", {})

    def test_render_returns_list_not_string(self):
        """render_command returns list[str], not a joined shell string."""
        result = reg.render_command("pnpm.typecheck", {})
        assert isinstance(result, list)
        assert result == ["pnpm", "typecheck"]


# ---------------------------------------------------------------------------
# Issue #1579: scope_rollup.run invocation identity and request-time contract
# ---------------------------------------------------------------------------

class TestScopeRollupRunRegistryContract:
    _PARAMS = {
        "issue_number": 1579,
        "repo": "squne121/loop-protocol",
        "invocation_id": "scope-rollup-1579-20260718",
        "requested_at": "2026-07-18T00:00:00Z",
    }

    def test_render_exact_argv(self):
        """scope_rollup.run renders the canonical complete argv in order."""
        assert reg.render_command("scope_rollup.run", self._PARAMS) == [
            "uv", "run", "python3",
            "scripts/agent-guards/run_scope_rollup_preflight.py",
            "--issue-number", "1579",
            "--repo", "squne121/loop-protocol",
            "--invocation-id", "scope-rollup-1579-20260718",
            "--requested-at", "2026-07-18T00:00:00Z",
        ]

    def test_missing_invocation_id_is_rejected(self):
        """The identity field is mandatory and fails closed when absent."""
        params = dict(self._PARAMS)
        del params["invocation_id"]
        with pytest.raises(ValueError, match="invocation_id.*missing"):
            reg.render_command("scope_rollup.run", params)

    def test_missing_requested_at_is_rejected(self):
        """The request timestamp is mandatory and fails closed when absent."""
        params = dict(self._PARAMS)
        del params["requested_at"]
        with pytest.raises(ValueError, match="requested_at.*missing"):
            reg.render_command("scope_rollup.run", params)

    def test_extra_parameter_is_rejected(self):
        """scope_rollup.run does not silently accept an undefined parameter."""
        params = {**self._PARAMS, "unexpected": "value"}
        with pytest.raises(ValueError, match="Extra params"):
            reg.render_command("scope_rollup.run", params)

    def test_rendered_argv_has_no_unresolved_placeholder(self):
        """A valid render cannot retain a registry placeholder token."""
        argv = reg.render_command("scope_rollup.run", self._PARAMS)
        assert not any(token.startswith("{") and token.endswith("}") for token in argv)


# ---------------------------------------------------------------------------
# AC5: _commands_from_plan() returns source: registry (not static_wrapper_template)
# ---------------------------------------------------------------------------

class TestCommandsFromPlan:
    def test_commands_from_plan_source_registry(self):
        """_commands_from_plan() derives argv from ISSUE_REFINEMENT_COMMAND_REGISTRY_V1.

        AC5: argv comes from command_registry.py (entry 'preflight.run').
        The 'source' field retains 'static_wrapper_template' for schema compatibility
        (refinement_preflight_result_v1.schema.json const constraint); argv content
        is the observable proxy for registry derivation.
        """
        import run_refinement_preflight as wrapper
        plan = {}
        commands = wrapper._commands_from_plan(plan, issue_number=42, repo="owner/repo")
        assert isinstance(commands, list)
        assert len(commands) >= 1
        for cmd in commands:
            argv = cmd.get("argv", [])
            assert isinstance(argv, list)
            # argv must contain the registry-defined form (uv run python3 ... --issue-number N)
            assert "uv" in argv, "registry entry 'preflight.run' starts with 'uv'"
            assert "--issue-number" in argv
            assert "42" in argv or str(42) in argv
            # source must be 'registry' — argv is derived from ISSUE_REFINEMENT_COMMAND_REGISTRY_V1
            assert cmd.get("source") == "registry", (
                f"source must be 'registry', got {cmd.get('source')!r}"
            )

    def test_commands_from_plan_has_argv(self):
        """Commands returned by _commands_from_plan have argv field."""
        import run_refinement_preflight as wrapper
        commands = wrapper._commands_from_plan({}, issue_number=1, repo="a/b")
        for cmd in commands:
            assert "argv" in cmd
            assert isinstance(cmd["argv"], list)


# ---------------------------------------------------------------------------
# AC6: compact stdout uses COMMANDS_JSON: field, not shell-like string
# ---------------------------------------------------------------------------

class TestCompactStdoutCommandsJson:
    def _make_result_with_commands(self) -> dict:
        """Build a minimal result dict that includes commands."""
        return {
            "schema": "refinement_preflight_result/v1",
            "status": "pass",
            "next_action": "proceed",
            "commands": [
                {
                    "kind": "run_preflight",
                    "argv": ["uv", "run", "python3", "script.py"],
                    "source": "registry",
                }
            ],
            "must_read": [],
            "blockers": [],
            "artifacts": {},
        }

    def test_build_compact_stdout_contains_commands_json(self):
        """_build_compact_stdout emits COMMANDS_JSON: field."""
        import run_refinement_preflight as wrapper
        result = self._make_result_with_commands()
        output = wrapper._build_compact_stdout(result)
        assert "COMMANDS_JSON:" in output, (
            f"Expected COMMANDS_JSON: in compact stdout, got:\n{output}"
        )

    def test_build_compact_stdout_commands_json_is_valid_json_array(self):
        """COMMANDS_JSON: value is a valid JSON array."""
        import run_refinement_preflight as wrapper
        result = self._make_result_with_commands()
        output = wrapper._build_compact_stdout(result)
        for line in output.splitlines():
            if line.startswith("COMMANDS_JSON:"):
                json_part = line[len("COMMANDS_JSON:"):].strip()
                parsed = json.loads(json_part)
                assert isinstance(parsed, list)
                break
        else:
            pytest.fail("COMMANDS_JSON: line not found in compact stdout")

    def test_build_compact_stdout_no_shell_string_join(self):
        """Compact stdout does not emit a joined shell-like command string."""
        import run_refinement_preflight as wrapper
        result = self._make_result_with_commands()
        output = wrapper._build_compact_stdout(result)
        # The old format was: "  - [run_preflight] uv run python3 script.py"
        # This should NOT appear; COMMANDS_JSON: should be used instead
        for line in output.splitlines():
            if line.strip().startswith("- [") and "uv run python3" in line:
                pytest.fail(
                    f"Compact stdout emits old shell-like COMMANDS format: {line!r}"
                )


# ---------------------------------------------------------------------------
# AC8: uv, pnpm, gh registry entries have all required spec fields
# ---------------------------------------------------------------------------

class TestRegistryEntrySpecs:
    _REQUIRED_SPEC_FIELDS = {
        "id", "argv", "cwd_policy", "stdin_contract",
        "stdout_contract", "timeout_seconds", "mutation", "placeholders",
    }

    def _assert_entry_complete(self, cmd_id: str) -> None:
        assert cmd_id in reg.REGISTRY, f"{cmd_id!r} not in REGISTRY"
        entry = reg.REGISTRY[cmd_id]
        missing = self._REQUIRED_SPEC_FIELDS - set(entry.keys())
        assert not missing, f"Entry {cmd_id!r} missing: {missing}"
        assert isinstance(entry["argv"], list)
        assert isinstance(entry["mutation"], bool)
        assert isinstance(entry["timeout_seconds"], int)
        assert entry["timeout_seconds"] > 0

    def test_uv_pytest_entry(self):
        self._assert_entry_complete("uv.pytest")

    def test_pnpm_typecheck_entry(self):
        self._assert_entry_complete("pnpm.typecheck")

    def test_pnpm_lint_entry(self):
        self._assert_entry_complete("pnpm.lint")

    def test_pnpm_test_entry(self):
        self._assert_entry_complete("pnpm.test")

    def test_pnpm_build_entry(self):
        self._assert_entry_complete("pnpm.build")

    def test_gh_issue_view_entry(self):
        self._assert_entry_complete("gh.issue.view")

    def test_gh_issue_comment_entry(self):
        self._assert_entry_complete("gh.issue.comment")

    def test_preflight_run_entry(self):
        self._assert_entry_complete("preflight.run")

    def test_plan_run_entry(self):
        self._assert_entry_complete("plan.run")

    def test_decide_run_entry(self):
        self._assert_entry_complete("decide.run")

    def test_decide_run_argv_accepted_by_decide_script_argparse(self):
        """Regression for #1873: decide.run's argv flags must all be recognized
        by decide_next_loop_action.py's actual argparse definition. A dangling
        flag (e.g. removed --phase-state-file) would make render_command()
        produce an argv that decide_next_loop_action.py rejects at runtime.
        """
        import decide_next_loop_action as decide_mod

        _DUMMY_BY_TYPE = {
            "repo_relative_file": "dummy.json",
            "verdict": "approve",
            "positive_int": "1",
        }

        entry = reg.REGISTRY["decide.run"]
        argv_tokens = entry["argv"]
        placeholders = entry["placeholders"]

        # Map each "--flag" token in argv to its placeholder name by looking
        # at the "{placeholder_name}" token that immediately follows it.
        resolved_argv: list[str] = []
        i = 0
        while i < len(argv_tokens):
            tok = argv_tokens[i]
            if tok.startswith("--"):
                resolved_argv.append(tok)
                if i + 1 < len(argv_tokens) and argv_tokens[i + 1].startswith("{"):
                    placeholder_name = argv_tokens[i + 1].strip("{}")
                    ph_type = placeholders.get(placeholder_name, {}).get("type")
                    resolved_argv.append(_DUMMY_BY_TYPE.get(ph_type, "dummy"))
                    i += 2
                    continue
            i += 1

        try:
            decide_mod._parse_args(resolved_argv)
        except SystemExit as exc:
            pytest.fail(
                "decide_next_loop_action.py's argparse rejected argv derived "
                f"from command_registry.py's decide.run entry: {resolved_argv} "
                f"(exit={exc.code}). command_registry.py's placeholders/argv "
                "are out of sync with decide_next_loop_action.py's CLI."
            )

    def test_mutation_flag_semantics(self):
        """gh.issue.comment is mutation=True; read-only commands are mutation=False."""
        assert reg.REGISTRY["gh.issue.comment"]["mutation"] is True
        assert reg.REGISTRY["gh.issue.view"]["mutation"] is False
        assert reg.REGISTRY["preflight.run"]["mutation"] is False
        assert reg.REGISTRY["pnpm.typecheck"]["mutation"] is False

    def test_gh_issue_view_not_mutation(self):
        assert reg.REGISTRY["gh.issue.view"]["mutation"] is False

    def test_all_timeout_positive(self):
        for cmd_id, entry in reg.REGISTRY.items():
            assert entry["timeout_seconds"] > 0, f"{cmd_id} timeout must be > 0"


# ---------------------------------------------------------------------------
# #2053 P0 fix-delta (iteration 3, OWNER PR review): the router
# ("decide.run") and producer/consumer ("authority_transport.produce" /
# "authority_transport.consume") command_registry.py entries must actually
# be recognized by the privileged skill_runtime_command_policy.py runtime
# policy -- not merely declared in the registry.
# ---------------------------------------------------------------------------

_AGENT_GUARDS_DIR = Path(__file__).resolve().parent.parent.parent.parent.parent / "scripts" / "agent-guards"


def _load_skill_runtime_command_policy():
    sys.path.insert(0, str(_AGENT_GUARDS_DIR))
    import skill_runtime_command_policy as policy  # noqa: PLC0415

    return policy


class TestAuthorityTransportCommandIdsRegisteredInRuntimePolicy:
    """#2053 P0 fix-delta (iteration 3): decide.run / authority_transport.produce
    / authority_transport.consume must be in skill_runtime_command_policy.py's
    eligible_command_ids, with execution_class matching command_registry.py's
    own declared execution_class for each entry exactly."""

    @pytest.mark.parametrize(
        "command_id",
        ["decide.run", "authority_transport.produce", "authority_transport.consume"],
    )
    def test_command_id_registered(self, command_id):
        policy = _load_skill_runtime_command_policy()
        eligible = policy.SKILL_RUNTIME_COMMAND_POLICY_V2["eligible_command_ids"]
        assert command_id in eligible, (
            f"{command_id} is declared in command_registry.py but missing from "
            "skill_runtime_command_policy.py's eligible_command_ids -- the "
            "privileged runtime policy cannot recognize it."
        )

    @pytest.mark.parametrize(
        "command_id",
        ["decide.run", "authority_transport.produce", "authority_transport.consume"],
    )
    def test_execution_class_matches_registry(self, command_id):
        policy = _load_skill_runtime_command_policy()
        eligible = policy.SKILL_RUNTIME_COMMAND_POLICY_V2["eligible_command_ids"]
        registry_entry = reg.REGISTRY[command_id]
        policy_entry = eligible[command_id]
        assert policy_entry["execution_class"] == registry_entry["execution_class"], (
            f"{command_id}: skill_runtime_command_policy.py execution_class "
            f"{policy_entry['execution_class']!r} does not match "
            f"command_registry.py execution_class {registry_entry['execution_class']!r}"
        )


# ---------------------------------------------------------------------------
# Fresh review blocker P0-A: authority_transport.consume must carry
# --contract-patch-plan-file / --anchor-context-file, and its network_effect
# must reflect the real GitHub mutation the delegated lane performs.
# ---------------------------------------------------------------------------


class TestAuthorityTransportConsumeContractPatchPlanPlaceholders:
    """Fresh review blocker P0-A: without these two placeholders,
    render_command("authority_transport.consume", ...) could never produce
    an argv carrying a CONTRACT_PATCH_PLAN_V1 / anchor context to
    run_refinement_preflight.py's --consume-authority-transport CLI branch,
    so the real controlled-mutation lane (consume_trusted_anchor_contract_patch_plan
    -> edit_issue_txn.py) was structurally unreachable via the registry.
    """

    _BASE_PARAMS = {
        "issue_number": 2053,
        "repo": "squne121/loop-protocol",
        "invocation_id": "p0a-test-1",
        "git_head_sha": "abc123def456abc123def456abc123def456abc",
        "router_receipt_path": "/tmp/router_receipt.json",
    }

    def test_placeholders_declared(self):
        entry = reg.REGISTRY["authority_transport.consume"]
        assert "contract_patch_plan_file" in entry["placeholders"]
        assert "anchor_context_file" in entry["placeholders"]

    def test_render_without_files_is_byte_identical_to_pre_p0a_shape(self):
        """Omitting the two optional files renders the exact same argv as
        before this fix -- existing callers are unaffected."""
        argv = reg.render_command("authority_transport.consume", self._BASE_PARAMS)
        assert "--contract-patch-plan-file" not in argv
        assert "--anchor-context-file" not in argv
        assert argv == [
            "uv", "run", "python3",
            ".claude/skills/issue-refinement-loop/scripts/run_refinement_preflight.py",
            "--issue-number", "2053",
            "--repo", "squne121/loop-protocol",
            "--invocation-id", "p0a-test-1",
            "--git-head-sha", "abc123def456abc123def456abc123def456abc",
            "--consume-authority-transport", "/tmp/router_receipt.json",
        ]

    def test_render_with_files_carries_both_flags(self):
        """Supplying both files renders an argv that genuinely carries them
        through to run_refinement_preflight.py's CLI."""
        params = {
            **self._BASE_PARAMS,
            "contract_patch_plan_file": "/tmp/patch_plan.json",
            "anchor_context_file": "/tmp/anchor_context.json",
        }
        argv = reg.render_command("authority_transport.consume", params)
        assert "--contract-patch-plan-file" in argv
        assert "/tmp/patch_plan.json" in argv
        assert "--anchor-context-file" in argv
        assert "/tmp/anchor_context.json" in argv
        # Flags must be adjacent to their values (canonical argv shape, no
        # shell join / reordering).
        cp_idx = argv.index("--contract-patch-plan-file")
        assert argv[cp_idx + 1] == "/tmp/patch_plan.json"
        ac_idx = argv.index("--anchor-context-file")
        assert argv[ac_idx + 1] == "/tmp/anchor_context.json"

    def test_network_effect_is_github_mutation_not_local_only(self):
        """Fresh review blocker P0-A: this command's default execution path
        (when contract_patch_plan_file/anchor_context_file are supplied)
        performs a real GitHub issue mutation via edit_issue_txn.py's gh
        subprocess calls -- it must not be misclassified as local_only."""
        entry = reg.REGISTRY["authority_transport.consume"]
        assert entry["network_effect"] == "github_mutation"

    def test_network_effect_matches_runtime_policy(self):
        """command_registry.py and skill_runtime_command_policy.py must
        declare the identical network_effect for this command_id (mirrors
        the existing execution_class parity check)."""
        policy = _load_skill_runtime_command_policy()
        eligible = policy.SKILL_RUNTIME_COMMAND_POLICY_V2["eligible_command_ids"]
        registry_entry = reg.REGISTRY["authority_transport.consume"]
        policy_entry = eligible["authority_transport.consume"]
        assert policy_entry["network_effect"] == registry_entry["network_effect"]


class TestAuthorityTransportConsumeRealSubprocessReachesRealLane:
    """Fresh review blocker P0-A: prove -- with a REAL subprocess invocation
    of run_refinement_preflight.py using registry-rendered argv (not a
    direct in-process Python function call) -- that
    --contract-patch-plan-file / --anchor-context-file genuinely reach
    consume_trusted_anchor_contract_patch_plan()'s real (non-fixture,
    non-injectable-via-CLI) code path.

    A nonexistent issue number is used so the real path fails closed at the
    first genuine `gh issue view` read (proving the real, non-fixture lane
    was reached -- `callbacks` cannot be carried through a JSON file, so a
    CLI invocation can never inject a fixture callback) before any mutation
    could occur. This never performs a GitHub mutation.
    """

    def test_real_subprocess_reaches_real_contract_patch_plan_consumer_lane(self, tmp_path):
        import shutil as _shutil

        skill_root = Path(__file__).resolve().parent.parent
        repo_root = skill_root.parent.parent.parent
        scripts_dir = skill_root / "scripts"
        sys.path.insert(0, str(scripts_dir))
        import run_refinement_preflight as preflight  # noqa: PLC0415
        import decide_next_loop_action as router  # noqa: PLC0415

        repo = "squne121/loop-protocol"
        # Guaranteed not to exist -- the real fetch_current() must fail
        # closed with a read (never write) gh call before any mutation
        # could be attempted.
        issue_number = 999999999
        invocation_id = "p0a-real-subprocess-1"
        git_head_sha = "abc123def456abc123def456abc123def456abc"

        artifact_dir = (
            repo_root / ".claude" / "artifacts" / "issue-refinement-loop"
            / str(issue_number) / "authority-transport" / invocation_id
        )
        try:
            evidence = {
                "schema_version": "SCOPE_DELTA_AUTHORITY_EVIDENCE_V1",
                "source_kind": "issue_comment",
                "source_ref": f"https://github.com/{repo}/issues/{issue_number}#issuecomment-1",
                "source_issue_number": issue_number,
                "comment_id": 1,
                "comment_url": f"https://github.com/{repo}/issues/{issue_number}#issuecomment-1",
                "issue_url": f"https://github.com/{repo}/issues/{issue_number}",
                "body_sha256": "sha256:p0a",
                "author_login": "owner",
                "author_type": "User",
                "author_association": "OWNER",
                "captured_at": "2026-08-09T00:00:00Z",
                "directive_markers": ["revised acceptance criteria"],
                "extracted_directives": ["AC1: p0a directive"],
                "ambiguity_flags": [],
                "boundary_flags": [],
                "confidence": "explicit",
            }
            produced, error = preflight.generate_authority_transport_manifest(
                evidence=evidence,
                issue_number=issue_number,
                repo=repo,
                invocation_id=invocation_id,
                git_head_sha=git_head_sha,
                repo_root=repo_root,
            )
            assert error is None, error

            router_receipt = router.generate_router_receipt(
                transport_manifest_path=produced["manifest_path"],
                issue_number=issue_number,
                invocation_id=invocation_id,
                git_head_sha=git_head_sha,
                authority_expected=True,
                repo_root=repo_root,
            )
            assert router_receipt["status"] == "ok"
            router_receipt_path = artifact_dir / "scope_delta_router_receipt_v1.json"
            assert router_receipt_path.exists()

            patch_plan_file = tmp_path / "contract_patch_plan.json"
            patch_plan_file.write_text(json.dumps({"operations": []}), encoding="utf-8")
            anchor_context_file = tmp_path / "anchor_context.json"
            anchor_context_file.write_text(
                json.dumps(
                    {
                        "issue": {"body": "## Acceptance Criteria\n- [ ] AC1: existing\n"},
                        "anchor_url": f"https://github.com/{repo}/issues/{issue_number}#issuecomment-1",
                        "anchor_payload": {"id": 1, "author_association": "OWNER"},
                        "anchor_body": "trusted directive body",
                    }
                ),
                encoding="utf-8",
            )

            argv = reg.render_command(
                "authority_transport.consume",
                {
                    "issue_number": issue_number,
                    "repo": repo,
                    "invocation_id": invocation_id,
                    "git_head_sha": git_head_sha,
                    "router_receipt_path": str(router_receipt_path),
                    "contract_patch_plan_file": str(patch_plan_file),
                    "anchor_context_file": str(anchor_context_file),
                },
            )
            assert "--contract-patch-plan-file" in argv
            assert "--anchor-context-file" in argv

            result = subprocess.run(
                argv,
                cwd=str(repo_root),
                capture_output=True,
                text=True,
                timeout=60,
            )
            receipt = json.loads(result.stdout)
            # The real (non-fixture) fetch_current() must have been reached
            # and failed closed on the nonexistent issue -- proving the
            # registry-rendered argv genuinely carried both files all the
            # way to consume_trusted_anchor_contract_patch_plan(), which a
            # CLI invocation can never bypass via injected callbacks.
            assert receipt["status"] == "environment_failure"
            assert receipt["mutation_lane"] == "contract_patch_plan_consumer"
            assert receipt["mutation_applied"] is False
            assert "issue_readback_failed" in receipt.get("reason_code", "") or (
                receipt.get("reason_code") == "contract_patch_plan_consumer_failed"
            )
        finally:
            if artifact_dir.parent.exists():
                _shutil.rmtree(artifact_dir.parent.parent, ignore_errors=True)


# ---------------------------------------------------------------------------
# Fresh review blocker P0-B: decide.run / authority_transport.produce /
# authority_transport.consume must actually pass through
# skill_runtime_exec.py's privileged executor as a REAL subprocess -- not
# merely dict-membership / execution_class string parity in
# skill_runtime_command_policy.py.
# ---------------------------------------------------------------------------


class TestAuthorityTransportPrivilegedExecutorRealSubprocessDispatch:
    """Fresh review blocker P0-B.

    This is a REAL subprocess invocation of
    scripts/agent-guards/skill_runtime_exec.py -- the actual privileged
    executor binary, run against this worktree's own real production code
    (LOOP_DEFAULT_BRANCH / LOOP_ISSUE_NUMBER env overrides bind it to this
    worktree's own branch/issue so the canonical-root and active-issue-
    worktree checks pass against a real git worktree, with no synthetic
    fixture repo required).

    FINDING (documented, not silently worked around): skill_runtime_exec.py
    main()'s command dispatch has exactly three shapes -- fixture,
    anchor/contract_update, and the plain 10-token preflight.run shape.
    skill_runtime_command_policy.py's parse_exact_skill_runtime_command()
    only accepts the plain 10-token shape when the command_id's declared
    execution_class equals SKILL_RUNTIME_EXECUTION_CLASS (preflight.run's
    class) -- decide.run / authority_transport.produce /
    authority_transport.consume declare distinct execution classes, so this
    parser -- by explicit design (see its own docstring) -- rejects them
    unconditionally. main() also never builds the additional render_params
    (loop_state_file, verdict, invocation_id, git_head_sha,
    router_receipt_path, evidence_fixture_path, contract_patch_plan_file,
    anchor_context_file) these three command_ids' registry entries require.

    These three command_ids are therefore NOT reachable through the
    privileged executor's real subprocess dispatch today, despite being
    declared "eligible" in skill_runtime_command_policy.py and registered in
    command_registry.py. Wiring real dispatch support requires changes to
    scripts/agent-guards/skill_runtime_exec.py (a new command-shape branch
    and render_params derivation), which is outside Issue #2053's Allowed
    Paths -- this is recorded as a Stop Condition scope delta for a
    follow-up Issue, not silently patched here.

    This test pins the CURRENT (real, subprocess-verified) rejection so a
    future PR that actually wires dispatch support has a concrete
    regression signal to update.
    """

    @pytest.mark.parametrize(
        "command_id",
        ["decide.run", "authority_transport.produce", "authority_transport.consume"],
    )
    def test_privileged_executor_rejects_command_id_today(self, command_id):
        skill_root = Path(__file__).resolve().parent.parent
        repo_root = skill_root.parent.parent.parent

        # No LOOP_DEFAULT_BRANCH / LOOP_ISSUE_NUMBER overrides are needed:
        # skill_runtime_command_policy.py's parse_exact_skill_runtime_command()
        # rejects these three command_ids purely on execution_class mismatch,
        # entirely BEFORE any cwd/branch/active-issue-worktree check runs --
        # so this rejection is reached identically whether this repo checkout
        # is on a real branch (local dev worktree) or in detached HEAD (CI's
        # pull_request merge-ref checkout).
        result = subprocess.run(
            [
                sys.executable,
                str(repo_root / "scripts" / "agent-guards" / "skill_runtime_exec.py"),
                "--command-id", command_id,
                "--issue-number", "2053",
                "--repo", "squne121/loop-protocol",
            ],
            cwd=str(repo_root),
            capture_output=True,
            text=True,
            timeout=30,
        )
        assert result.returncode == 2, (
            f"{command_id}: expected the privileged executor's real subprocess "
            f"to reject with exit 2 ('exact command class rejected') today -- "
            f"got exit {result.returncode}, stdout={result.stdout!r}, "
            f"stderr={result.stderr!r}. If this now succeeds, the P0-B Stop "
            f"Condition scope delta has been resolved and this pinned "
            f"regression test must be updated to assert real dispatch success."
        )
        assert "exact command class rejected" in result.stderr

