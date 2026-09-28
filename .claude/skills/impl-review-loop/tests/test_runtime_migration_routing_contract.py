"""Issue #2810 AC3/AC6/AC8 — routing-contract regression tests.

These are STATIC regression tests over the Allowed Paths documents (and one
behavioral check of `classify_runtime_migration()`) that fix the routing
contract described in `step-5-feedback-and-termination.md` and
`.claude/agents/implementation-worker.md`:

- AC3: bounded repair is executed by the mutation-capable Step 1 worker
  route, never by `test-runner` (whose read-only contract is unchanged),
  and repair completion always requires a fresh Step 2 canonical
  verification (stale evidence reuse is rejected).
- AC6: `human_action_required` is root-owned classification only; a bare
  SubAgent self-report has no stop authority, and the capability-blocker
  human-veto addition stays narrow (subtype of the existing
  `termination_reason: human_escalation`, no new enum value).
- AC8: no new `.claude/agents/*.md` file was added, `test-runner.md` /
  `scripts/claude-gpt/**` are untouched, and the new worker mode is scoped
  to exactly one addition among the pre-existing modes.
"""

from __future__ import annotations

import importlib.util
import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[4]
STEP5_PATH = REPO_ROOT / ".claude/skills/impl-review-loop/steps/step-5-feedback-and-termination.md"
STEP1_PATH = REPO_ROOT / ".claude/skills/impl-review-loop/steps/step-1-implementation.md"
IMPLEMENTATION_WORKER_PATH = REPO_ROOT / ".claude/agents/implementation-worker.md"
TEST_RUNNER_PATH = REPO_ROOT / ".claude/agents/test-runner.md"
AGENTS_DIR = REPO_ROOT / ".claude/agents"

SCRIPTS_DIR = Path(__file__).resolve().parents[1] / "scripts"
MODULE_PATH = SCRIPTS_DIR / "classify_runtime_migration.py"
_spec = importlib.util.spec_from_file_location(
    "impl_review_loop_classify_runtime_migration_2810_routing", MODULE_PATH
)
classify_runtime_migration_mod = importlib.util.module_from_spec(_spec)
assert _spec.loader is not None
_spec.loader.exec_module(classify_runtime_migration_mod)
classify_runtime_migration = classify_runtime_migration_mod.classify_runtime_migration


def _read(path: Path) -> str:
    assert path.is_file(), f"not found: {path}"
    return path.read_text(encoding="utf-8")


def _normalized(text: str) -> str:
    """Collapse all whitespace (including markdown hard line-wraps) to a
    single space, so a fixed phrase can be located regardless of where the
    source document happened to wrap a line."""
    return re.sub(r"\s+", " ", text)


# --- AC3: test_runner / fresh_step2 -----------------------------------------


def test_test_runner_contract_unchanged_read_only():
    """GIVEN test-runner.md WHEN inspected THEN it still declares no
    mutation tools (Edit/Write/MultiEdit disallowed) and never mentions
    executing repair_proxy.sh itself -- bounded repair stays on the Step 1
    mutation-capable worker route."""
    doc_text = _read(TEST_RUNNER_PATH)
    assert "disallowedTools:" in doc_text
    assert "- Edit" in doc_text
    assert "- Write" in doc_text
    assert "- MultiEdit" in doc_text
    assert "repair_proxy.sh" not in doc_text


def test_fresh_step2_required_after_repair():
    """GIVEN step-5-feedback-and-termination.md WHEN inspected THEN the
    agent-executable-migration routing explicitly requires a fresh Step 2
    canonical verification after repair, and rejects stale evidence
    reuse."""
    doc_text = _normalized(_read(STEP5_PATH))
    assert "repair 完了後は古い evidence を再利用せず、fresh Step 2" in doc_text
    assert "古い evidence の再利用は routing 上 reject" in doc_text


def test_fresh_step2_route_uses_existing_fix_delta_to_step1():
    """GIVEN step-5-feedback-and-termination.md WHEN inspected THEN
    agent-executable migration is routed through the EXISTING
    fix_delta -> Step 1 implementation-worker route (no new route/agent)."""
    doc_text = _normalized(_read(STEP5_PATH))
    assert "fix_delta -> Step 1 implementation-worker" in doc_text


# --- AC6: root_owned / no_manual_command_termination / human_veto ----------


def test_root_owned_classification_only():
    """GIVEN step-5-feedback-and-termination.md WHEN inspected THEN
    classification is explicitly root-owned, not a SubAgent self-report."""
    doc_text = _normalized(_read(STEP5_PATH))
    assert "root-owned の capability classification" in doc_text
    assert "SubAgent の自己申告ではなく" in doc_text


def test_no_manual_command_termination_regression_documented():
    """GIVEN step-5-feedback-and-termination.md WHEN inspected THEN the
    AC6 regression-prohibition sentence (do not terminate non-capability
    failures with a bare "run this manually" instruction) is present."""
    doc_text = _normalized(_read(STEP5_PATH))
    assert (
        "normal test failure・review finding・implementation defect を"
        "「人間が repair command を 手動実行してください」とだけ返して"
        "終了させてはならない" in doc_text
    )


def test_no_manual_command_termination_behavioral_unverified_self_report():
    """GIVEN a worker_result reporting a bare, unverified
    permission_denied self-report (no independently-verified deny
    evidence) WHEN classified THEN the classifier does NOT grant it stop
    authority (class != human_capability_blocker) -- this is the
    behavioral counterpart of the AC6 regression-prohibition sentence."""
    payload = {
        "failure_evidence": {
            "cause": "proxy_model_catalog_incompatible",
            "repair_command": "bash scripts/claude-gpt/repair_proxy.sh",
            "required_models": [],
            "missing_models": [],
        },
        "live_issue_authorizes_migration": True,
        "effective_env": {"claude_gpt_home": "/home/op/.claude-gpt", "override_vars_present": False},
        "probes": {"install_dir_writable": True, "host_reachable": True},
        "capability_flags": {
            "needs_credential": False,
            "needs_secret": False,
            "needs_privilege": False,
            "destructive_or_global": False,
        },
        "worker_result": {
            "status": "permission_blocked",
            "reason_code": "permission_denied",
            "exit_code": None,
            "deny_evidence_verified": False,
            "sudo_required_in_log": False,
        },
    }
    result = classify_runtime_migration(payload)
    assert result["class"] != "human_capability_blocker"


def test_human_veto_capability_blocker_is_narrow_addition_no_new_enum():
    """GIVEN step-5-feedback-and-termination.md WHEN inspected THEN the
    capability-blocker human-veto addition is documented as a narrow
    subtype of the existing termination_reason: human_escalation (no new
    enum value)."""
    doc_text = _normalized(_read(STEP5_PATH))
    assert "狭い追加項目" in doc_text
    assert "escalation_subtype: capability_blocker" in doc_text
    assert "新しい `termination_reason` enum 値は追加しない" in doc_text


def test_human_action_required_report_fields_required_in_step5():
    """GIVEN step-5-feedback-and-termination.md WHEN inspected THEN the
    termination report requires reason/required_human_action/
    target_environment/verification_command/resume_condition -- omitting
    any field is prohibited."""
    doc_text = _normalized(_read(STEP5_PATH))
    assert "いずれかのフィールドが欠落した状態で `human_action_required` を立ててはならない" in doc_text


# --- AC8: no_new_agent / worker_mode_scope ----------------------------------


_FORBIDDEN_NEW_AGENT_NAME_FRAGMENTS = (
    "runtime-migration",
    "migration-worker",
    "repair-worker",
    "pr-hygiene-fixer",
    "branch-syncer",
)


def test_no_new_agent_file_added_for_runtime_migration():
    """GIVEN .claude/agents/*.md WHEN listed THEN no new agent file whose
    name suggests a runtime-migration-specific persona exists (Issue #2810
    Out of Scope: no new .claude/agents/*.md; the mode lives inside
    implementation-worker.md)."""
    agent_files = sorted(p.name for p in AGENTS_DIR.glob("*.md"))
    for fragment in _FORBIDDEN_NEW_AGENT_NAME_FRAGMENTS:
        matches = [name for name in agent_files if fragment in name]
        assert matches == [], f"forbidden new agent file(s) found: {matches}"


def test_worker_mode_scope_is_exactly_four_modes():
    """GIVEN implementation-worker.md WHEN inspected THEN the
    IMPLEMENTATION_WORKER_REQUEST_V2.mode enum has exactly the 3
    pre-existing modes plus the 1 new apply_runtime_migration_fix_delta
    mode -- no additional new modes were introduced."""
    doc_text = _read(IMPLEMENTATION_WORKER_PATH)
    assert (
        "mode: update_pr_body_hygiene | update_branch | apply_pr_review_fix_delta"
        " | apply_runtime_migration_fix_delta" in doc_text
    )


def test_worker_mode_scope_existing_three_modes_untouched_markers_present():
    """GIVEN implementation-worker.md WHEN inspected THEN the pre-existing
    mode section headings/wrapper-enforcement text for the 3 untouched
    modes are still present verbatim (spot-check for accidental removal
    while adding the new mode)."""
    doc_text = _read(IMPLEMENTATION_WORKER_PATH)
    assert "## update_pr_body_hygiene mode（PR 本文衛生修正モード）" in doc_text
    assert "**`open-pr/scripts/update_pr.py` wrapper 経由での実行を必須とする。**" in doc_text
    assert "## update_branch mode（ブランチ更新モード）" in doc_text
    assert "### expected_head_sha 必須" in doc_text
    assert "## apply_pr_review_fix_delta mode（PR レビュー修正差分の適用モード）" in doc_text


def test_new_mode_section_heading_present():
    """GIVEN implementation-worker.md WHEN inspected THEN the new
    apply_runtime_migration_fix_delta mode section exists with its request
    field documentation."""
    doc_text = _read(IMPLEMENTATION_WORKER_PATH)
    assert (
        "## apply_runtime_migration_fix_delta mode（runtime migration 修正の適用モード、Issue #2810）"
        in doc_text
    )
    assert "repository 内の file 編集を一切行わない" in doc_text
