#!/usr/bin/env python3
"""BDD contract tests for the agent-neutral worktree policy domain (Issue #1670)."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts" / "agent-guards"))
import worktree_policy_domain as domain  # noqa: E402
import worktree_scope_guard  # noqa: E402
import codex_apply_patch_adapter  # noqa: E402


CLAUDE_DIGEST = "sha256:7a6792dfc33b57b2b347bee70db61e54ba37ce0b263b6e79cb1466a4a67df6e0"
CODEX_DIGEST = "sha256:c9de2e1296733b5600d5ef9cc9c2b56d51aafae15bc51d63d295c3155f02263c"
CLAUDE_OFFICIAL_2_1_218 = {
    "Write": {
        "sanitized": "sha256:819d65d60a8282dddeb25ab308852198cabd804312b8a3b293bc3029c2eafd73",
        "raw": "sha256:161b8a8beb999269831c09c3e3050459e549e85221111c04c9687ce21d6367b8",
    },
    "Edit": {
        "sanitized": "sha256:01f222e4e123868ab617899db8553ab23e0a5427c21776b299165f450a550e31",
        "raw": "sha256:4e571d256fc56f5dc1cf4cc01554972c479cffdd46eb3dc8a7a71e16d9878e4c",
    },
    "Bash": {
        "sanitized": "sha256:223140cf2b4ead326ef731333f49c6fbf5789a4bae586f4d67badc718c639779",
        "raw": "sha256:bf330713ca708ad68674fffcfcc539e192e8c02f7d09966f163d3282a5e52929",
    },
}


def _claude_capture_digest(version: str, identity: str, prefer: str = "sanitized") -> str:
    if version == "2.1.218":
        return CLAUDE_OFFICIAL_2_1_218[identity][prefer]
    return CLAUDE_DIGEST


def _intent(
    runtime: str = "claude",
    identity: str = "Write",
    path: str = "/repo/wt/file.py",
    runtime_version: str | None = None,
    capture_digest: str | None = None,
    canonical_identity: str | None = None,
    mutation_kind: str | None = None,
) -> dict[str, object]:
    if runtime_version is None:
        runtime_version = "2.1.216" if runtime == "claude" else "0.145.0"
    if canonical_identity is None:
        canonical_identity = "Write" if runtime == "claude" else "apply_patch"
    if mutation_kind is None:
        mutation_kind = "bash" if identity == "Bash" else ("write" if runtime == "claude" else "apply_patch")
    if capture_digest is None:
        if runtime == "claude":
            capture_digest = _claude_capture_digest(runtime_version, identity)
        else:
            capture_digest = CODEX_DIGEST
    return domain.make_intent(
        runtime=runtime,
        runtime_version=runtime_version,
        tool_identity=identity,
        canonical_identity=canonical_identity,
        mutation_kind=mutation_kind,
        target_paths=[path],
        path_flavor="posix",
        capture_digest=capture_digest,
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


@pytest.mark.parametrize(
    ("runtime", "version", "identity", "canonical", "kind", "digest"),
    [
        ("claude", "2.1.217", "Write", "Write", "write", CLAUDE_DIGEST),
        ("claude", "2.1.217", "Bash", "Bash", "bash", CLAUDE_DIGEST),
        ("codex", "0.999.0", "ApplyPatch", "apply_patch", "apply_patch", CODEX_DIGEST),
    ],
)
def test_given_unsupported_version_or_identity_when_adapter_builds_intent_then_fail_closed(
    runtime: str, version: str, identity: str, canonical: str, kind: str, digest: str
) -> None:
    with pytest.raises(domain.ContractValidationError):
        domain.make_intent(
            runtime=runtime, runtime_version=version, tool_identity=identity,
            canonical_identity=canonical, mutation_kind=kind, target_paths=["/repo/wt/a"], path_flavor="posix", capture_digest=digest,
        )


@pytest.mark.parametrize(
    ("runtime", "version", "identity", "canonical", "kind", "digest"),
    [
        ("claude", "2.1.216", "Write", "Write", "write", CLAUDE_DIGEST),
        ("claude", "2.1.216", "Edit", "Write", "write", CLAUDE_DIGEST),
        ("claude", "2.1.216", "MultiEdit", "Write", "write", CLAUDE_DIGEST),
        ("claude", "2.1.218", "Write", "Write", "write", CLAUDE_OFFICIAL_2_1_218["Write"]["sanitized"]),
        ("claude", "2.1.218", "Edit", "Write", "write", CLAUDE_OFFICIAL_2_1_218["Edit"]["sanitized"]),
        ("claude", "2.1.218", "Bash", "Bash", "bash", CLAUDE_OFFICIAL_2_1_218["Bash"]["sanitized"]),
        ("codex", "0.145.0", "apply_patch", "apply_patch", "apply_patch", CODEX_DIGEST),
    ],
)
def test_given_supported_runtime_profile_when_validated_then_allow_inside_worktree(
    runtime: str, version: str, identity: str, canonical: str, kind: str, digest: str
) -> None:
    intent = domain.make_intent(
        runtime=runtime,
        runtime_version=version,
        tool_identity=identity,
        canonical_identity=canonical,
        mutation_kind=kind,
        target_paths=["/repo/wt/file.py"],
        path_flavor="posix", capture_digest=digest,
    )
    decision = domain.policy_decide(intent, _binding())
    assert decision == {
        "schema": domain.DECISION_SCHEMA,
        "decision": "allow",
        "reason_code": "mutation_inside_worktree",
    }


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


def test_given_equivalent_intents_from_claude_and_codex_with_shared_binding_parity() -> None:
    expected_worktree = "/repo/wt"
    binding = _binding("bound", expected_worktree)
    for target in ["/repo/wt/file.py", "/repo/outside/file.py"]:
        claude = domain.policy_decide(
            _intent(runtime="claude", identity="Write", path=target),
            binding,
        )
        codex = domain.policy_decide(
            _intent(runtime="codex", identity="apply_patch", path=target),
            binding,
        )
        assert claude == codex


def test_given_adapters_generate_matching_bound_binding_and_parity_for_claude_vs_codex_adapter(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    expected_worktree = "/repo/wt"
    project_root = "/repo"
    issue = "942"
    resolution = worktree_scope_guard.WorktreeResolution(expected_worktree, 1, True)

    recorded: dict[str, list[dict[str, object]]] = {"intent": [], "binding": []}

    original_make_intent = domain.make_intent
    original_make_binding = domain.make_binding

    def capture_make_intent(**kwargs: object) -> dict[str, object]:
        value = original_make_intent(**kwargs)  # type: ignore[arg-type]
        recorded["intent"].append(value)  # type: ignore[arg-type]
        return value

    def capture_make_binding(**kwargs: object) -> dict[str, object]:
        value = original_make_binding(**kwargs)  # type: ignore[arg-type]
        recorded["binding"].append(value)  # type: ignore[arg-type]
        return value

    monkeypatch.setattr(domain, "make_intent", capture_make_intent)
    monkeypatch.setattr(domain, "make_binding", capture_make_binding)
    monkeypatch.setattr(worktree_scope_guard, "local_main_scratch_allow_v1", lambda *args, **kwargs: False)
    monkeypatch.setattr(worktree_scope_guard, "resolve_project_root", lambda: project_root)
    monkeypatch.setattr(worktree_scope_guard, "resolve_current_issue", lambda _cwd, _root: issue)
    monkeypatch.setattr(worktree_scope_guard, "resolve_expected_worktree", lambda _issue, _root, _deadline=None: resolution)

    with pytest.raises(SystemExit) as claude_exit:
        worktree_scope_guard._decide_write(
            {"file_path": "inside.py"},
            f"{expected_worktree}/sub",
            issue,
            resolution,
            "Write",
            project_root=project_root,
        )
    assert claude_exit.value.code == 0

    patch_body = "*** Begin Patch\n*** Add File: inside.py\n+print('ok')\n*** End Patch\n"
    payload = {"tool_name": "apply_patch", "tool_input": {"command": patch_body}, "cwd": f"{expected_worktree}/sub"}
    with pytest.raises(SystemExit) as codex_exit:
        codex_apply_patch_adapter._decide_apply_patch(payload)
    assert codex_exit.value.code == 0

    assert len(recorded["intent"]) == 2
    assert len(recorded["binding"]) == 2

    claude_intent = domain.validate_intent(recorded["intent"][0])
    codex_intent = domain.validate_intent(recorded["intent"][1])
    claude_binding = domain.validate_binding(recorded["binding"][0])
    codex_binding = domain.validate_binding(recorded["binding"][1])

    assert claude_binding == codex_binding
    assert claude_binding["state"] == "bound"
    assert claude_binding["expected_worktree"] == expected_worktree

    claude_decision = domain.policy_decide(claude_intent, claude_binding)
    codex_decision = domain.policy_decide(codex_intent, codex_binding)
    assert codex_decision["decision"] == claude_decision["decision"]
    assert codex_decision["reason_code"] == claude_decision["reason_code"] == "mutation_inside_worktree"


def test_given_adapters_generate_matching_deny_for_shared_binding(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    expected_worktree = "/repo/wt"
    project_root = "/repo"
    issue = "942"
    resolution = worktree_scope_guard.WorktreeResolution(expected_worktree, 1, True)

    recorded: dict[str, list[dict[str, object]]] = {"intent": [], "binding": []}

    original_make_intent = domain.make_intent
    original_make_binding = domain.make_binding

    def capture_make_intent(**kwargs: object) -> dict[str, object]:
        value = original_make_intent(**kwargs)  # type: ignore[arg-type]
        recorded["intent"].append(value)  # type: ignore[arg-type]
        return value

    def capture_make_binding(**kwargs: object) -> dict[str, object]:
        value = original_make_binding(**kwargs)  # type: ignore[arg-type]
        recorded["binding"].append(value)  # type: ignore[arg-type]
        return value

    monkeypatch.setattr(domain, "make_intent", capture_make_intent)
    monkeypatch.setattr(domain, "make_binding", capture_make_binding)
    monkeypatch.setattr(worktree_scope_guard, "local_main_scratch_allow_v1", lambda *args, **kwargs: False)
    monkeypatch.setattr(worktree_scope_guard, "resolve_project_root", lambda: project_root)
    monkeypatch.setattr(worktree_scope_guard, "resolve_current_issue", lambda _cwd, _root: issue)
    monkeypatch.setattr(worktree_scope_guard, "resolve_expected_worktree", lambda _issue, _root, _deadline=None: resolution)

    with pytest.raises(SystemExit) as claude_exit:
        worktree_scope_guard._decide_write(
            {"file_path": "../../outside.py"},
            f"{expected_worktree}/sub",
            issue,
            resolution,
            "Write",
            project_root=project_root,
        )
    assert claude_exit.value.code == 2

    patch_body = "*** Begin Patch\n*** Add File: ../../outside.py\n+print('ok')\n*** End Patch\n"
    payload = {"tool_name": "apply_patch", "tool_input": {"command": patch_body}, "cwd": f"{expected_worktree}/sub"}
    with pytest.raises(SystemExit) as codex_exit:
        codex_apply_patch_adapter._decide_apply_patch(payload)
    assert codex_exit.value.code == 2

    assert len(recorded["intent"]) == 2
    assert len(recorded["binding"]) == 2

    claude_intent = domain.validate_intent(recorded["intent"][0])
    codex_intent = domain.validate_intent(recorded["intent"][1])
    claude_binding = domain.validate_binding(recorded["binding"][0])
    codex_binding = domain.validate_binding(recorded["binding"][1])

    assert claude_binding == codex_binding

    claude_decision = domain.policy_decide(claude_intent, claude_binding)
    codex_decision = domain.policy_decide(codex_intent, codex_binding)
    assert codex_decision["decision"] == claude_decision["decision"] == "deny"
    assert codex_decision["reason_code"] == claude_decision["reason_code"] == "target_outside_worktree"
