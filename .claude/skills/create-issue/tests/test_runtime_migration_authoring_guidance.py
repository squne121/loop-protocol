#!/usr/bin/env python3
"""Regression tests for Issue #2810 AC1.

`body-authoring.md` and `docs/dev/runtime-verification-policy.md` must
separate "repository code merged" from "target runtime migration/
acceptance completed", and must require an Issue whose actual/canonical
runtime AC needs local runtime state migration to explicitly state
migration ownership. The #2772 -> #2801 case (repository policy racing
ahead of local dependency capability) is fixed as a concrete worked
example in both documents.
"""

from pathlib import Path

BODY_AUTHORING_PATH = (
    Path(__file__).resolve().parents[1] / "references" / "body-authoring.md"
)
RUNTIME_VERIFICATION_POLICY_PATH = (
    Path(__file__).resolve().parents[4] / "docs" / "dev" / "runtime-verification-policy.md"
)


def _read(path: Path) -> str:
    assert path.is_file(), f"not found: {path}"
    return path.read_text(encoding="utf-8")


def test_body_authoring_separates_code_merge_from_runtime_migration_acceptance():
    """GIVEN body-authoring.md WHEN inspected THEN it separates "repository
    code merged" from "target runtime migration/acceptance completed", and
    requires migration ownership + human-action boundary to be declared for
    Issues whose actual/canonical runtime AC needs local runtime state
    migration."""
    doc_text = _read(BODY_AUTHORING_PATH)
    assert "ランタイム依存 migration の ownership 明記規則" in doc_text
    assert (
        "「repository code が merge された」ことと「target runtime が実際に\n"
        "migration/acceptance を完了した」ことは別の完了条件であり" in doc_text
    )
    assert "migration ownership と human-action boundary を" in doc_text


def test_body_authoring_prohibits_confusing_migration_action_with_verification_vc():
    """GIVEN body-authoring.md WHEN inspected THEN it explicitly prohibits
    pushing mutation into a read-only verifier (migration action vs.
    runtime verification VC separation)."""
    doc_text = _read(BODY_AUTHORING_PATH)
    assert "migration action と runtime verification VC の混同禁止" in doc_text
    assert "read-only であるべき" in doc_text
    assert "mutation を押し込んではならない" in doc_text


def test_body_authoring_fixes_2772_2801_worked_example():
    """GIVEN body-authoring.md WHEN inspected THEN the #2772 -> #2801
    example (repository policy racing ahead of local dependency
    capability) is fixed as a concrete worked example."""
    doc_text = _read(BODY_AUTHORING_PATH)
    assert "#2772 -> #2801" in doc_text
    assert "repair path" in doc_text
    assert "migration ownership を定義しないまま Out of Scope とした" in doc_text


def test_body_authoring_required_migration_ownership_fields():
    """GIVEN body-authoring.md WHEN inspected THEN it lists the required
    fields for migration ownership declaration: who executes, agent
    eligibility conditions, fresh re-verification requirement, and
    human-intervention stop-report fields."""
    doc_text = _read(BODY_AUTHORING_PATH)
    assert "誰が migration を実行するか" in doc_text
    assert "agent が実行してよい条件" in doc_text
    assert "pre-repair の evidence を\n  再利用しない" in doc_text or "pre-repair の evidence を" in doc_text
    assert "`reason` / `required_human_action` / `target_environment` /" in doc_text
    assert "`verification_command` / `resume_condition`" in doc_text


def test_runtime_verification_policy_has_one_shot_migration_ownership_section():
    """GIVEN docs/dev/runtime-verification-policy.md WHEN inspected THEN it
    has a dedicated section on runtime dependency migration's one-shot
    ownership, human intervention necessary conditions, and required
    report fields (Issue #2810)."""
    doc_text = _read(RUNTIME_VERIFICATION_POLICY_PATH)
    assert "runtime 依存関係 migration の一回限りの ownership（Issue #2810）" in doc_text
    assert "「コード統合」と「対象 runtime の migration/acceptance」の分離" in doc_text
    assert "#2772 -> #2801" in doc_text
    assert "migration ownership" in doc_text


def test_runtime_verification_policy_human_intervention_necessary_conditions():
    """GIVEN docs/dev/runtime-verification-policy.md WHEN inspected THEN it
    enumerates the human-intervention necessary conditions (credential,
    privilege escalation, destructive mutation, unreachable host, verified
    policy/hook denial) and prohibits terminating an unrelated failure with
    a bare "run this manually" instruction."""
    doc_text = _read(RUNTIME_VERIFICATION_POLICY_PATH)
    assert "human intervention の必要条件" in doc_text
    assert "本人 credential 操作" in doc_text
    assert "sudo / privilege escalation" in doc_text
    assert "destructive/global mutation" in doc_text
    assert "到達不能" in doc_text
    assert (
        "「人間が repair command を手動実行して\nください」とだけ報告して終了させてはならない"
        in doc_text
    )


def test_runtime_verification_policy_required_report_fields():
    """GIVEN docs/dev/runtime-verification-policy.md WHEN inspected THEN
    the required human-intervention report fields are listed: reason,
    required_human_action, target_environment, verification_command,
    resume_condition."""
    doc_text = _read(RUNTIME_VERIFICATION_POLICY_PATH)
    assert "`reason`: なぜ human intervention が必要か" in doc_text
    assert "`required_human_action`:" in doc_text
    assert "`target_environment`:" in doc_text
    assert "`verification_command`:" in doc_text
    assert "`resume_condition`:" in doc_text


def test_body_authoring_documents_machine_checkable_authorization_predicate():
    """GIVEN body-authoring.md WHEN inspected THEN it documents the
    deterministic predicate the impl-review-loop root uses for
    live_issue_authorizes_migration (exact literal + agent-execution
    allowance marker on one line, fail-closed otherwise) with a sample."""
    doc_text = _read(BODY_AUTHORING_PATH)
    assert "agent 実行許可の機械判定述語" in doc_text
    assert "bash scripts/claude-gpt/repair_proxy.sh" in doc_text
    assert "live_issue_authorizes_migration: true" in doc_text
    assert "fail-closed" in doc_text
