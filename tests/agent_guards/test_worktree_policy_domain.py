#!/usr/bin/env python3
"""BDD contract tests for the agent-neutral worktree policy domain (Issue #1670)."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts" / "agent-guards"))
import worktree_policy_domain as domain  # noqa: E402


CLAUDE_DIGEST = "sha256:7a6792dfc33b57b2b347bee70db61e54ba37ce0b263b6e79cb1466a4a67df6e0"
CODEX_DIGEST = "sha256:c9de2e1296733b5600d5ef9cc9c2b56d51aafae15bc51d63d295c3155f02263c"


def _intent(runtime: str = "claude", identity: str = "Write", path: str = "/repo/wt/file.py") -> dict[str, object]:
    return domain.make_intent(
        runtime=runtime,
        runtime_version="2.1.216" if runtime == "claude" else "0.145.0",
        tool_identity=identity,
        canonical_identity="Write" if runtime == "claude" else "apply_patch",
        mutation_kind="write" if runtime == "claude" else "apply_patch",
        target_paths=[path],
        path_flavor="posix",
        capture_digest=CLAUDE_DIGEST if runtime == "claude" else CODEX_DIGEST,
    )


def _binding(state: str = "bound", expected: str | None = "/repo/wt") -> dict[str, object]:
    if state != "bound":
        expected = None
    return domain.make_binding(
        state=state,
        expected_worktree=expected,
        path_flavor="posix",
        resolver_digest=domain.digest_for_resolver(state, expected),
    )


def test_given_normalized_claude_write_when_bound_inside_then_allow_without_vendor_dto() -> None:
    decision = domain.policy_decide(_intent(), _binding())
    assert decision == {
        "schema": domain.DECISION_SCHEMA,
        "decision": "allow",
        "reason_code": "mutation_inside_worktree",
    }


@pytest.mark.parametrize("state", ["unknown", "ambiguous", "resolution_failed"])
def test_given_indeterminate_binding_when_policy_evaluates_then_fail_closed(state: str) -> None:
    decision = domain.policy_decide(_intent(), _binding(state))
    assert decision["decision"] == "deny"
    assert decision["reason_code"] == f"binding_{state}"


def test_given_verified_unbound_when_write_then_deny_but_codex_apply_patch_preserves_allow() -> None:
    assert domain.policy_decide(_intent(), _binding("verified_unbound"))["reason_code"] == "issue_context_required"
    assert domain.policy_decide(_intent("codex", "apply_patch"), _binding("verified_unbound")) == {
        "schema": domain.DECISION_SCHEMA,
        "decision": "allow",
        "reason_code": "no_issue_no_scope",
    }


@pytest.mark.parametrize(
    "mutator",
    [
        lambda value: value.update({"unknown": "raw DTO field"}),
        lambda value: value.update({"target_paths": ["relative.py"]}),
        lambda value: value.update({"capture_digest": "sha256:not-a-digest"}),
        lambda value: value.update({"target_paths": [True]}),
    ],
)
def test_given_malformed_or_vendor_shaped_intent_when_validated_then_reject(mutator) -> None:
    value = _intent()
    mutator(value)
    with pytest.raises(domain.ContractValidationError):
        domain.validate_intent(value)


def test_given_unsupported_version_or_identity_when_adapter_builds_intent_then_fail_closed() -> None:
    with pytest.raises(domain.ContractValidationError):
        domain.make_intent(
            runtime="codex", runtime_version="0.999.0", tool_identity="ApplyPatch",
            canonical_identity="apply_patch", mutation_kind="apply_patch", target_paths=["/repo/wt/a"],
            path_flavor="posix", capture_digest=CODEX_DIGEST,
        )


def test_given_move_source_and_destination_when_one_escapes_then_deny() -> None:
    intent = _intent("codex", "apply_patch")
    intent["target_paths"] = ["/repo/wt/old.py", "/repo/outside/new.py"]
    decision = domain.policy_decide(intent, _binding())
    assert decision["decision"] == "deny"
    assert decision["reason_code"] == "target_outside_worktree"


def test_given_windows_paths_when_flavor_is_windows_then_component_boundary_is_preserved() -> None:
    intent = domain.make_intent(
        runtime="codex", runtime_version="0.145.0", tool_identity="apply_patch", canonical_identity="apply_patch",
        mutation_kind="apply_patch", target_paths=[r"C:\\repo\\wt\\file.py"], path_flavor="windows", capture_digest=CODEX_DIGEST,
    )
    binding = domain.make_binding(
        state="bound", expected_worktree=r"C:\\repo\\wt", path_flavor="windows",
        resolver_digest=domain.digest_for_resolver("bound", r"C:\\repo\\wt"),
    )
    assert domain.policy_decide(intent, binding)["decision"] == "allow"
