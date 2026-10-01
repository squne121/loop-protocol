"""#2842 AC1: trusted freeform text cannot become an unsafe contract append."""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
import run_refinement_preflight as preflight  # noqa: E402
from scope_signal_delta import derive_contract_patch_operations, extract_directive_items  # noqa: E402

REPO = "squne121/loop-protocol"
ISSUE = 2827
URL = f"https://github.com/{REPO}/issues/{ISSUE}#issuecomment-5891092074"
BODY = "## Acceptance Criteria\n\n- [ ] AC18: existing\n\n## Allowed Paths\n\n- `src/main.ts`\n"
# Representative of the ~30 freeform OWNER paragraphs that previously
# appeared as unbulleted AC lines after a single write/fresh-review approve.
OWNER = "\n".join(f"- {n}. この作業の背景と例を確認して修正してください。" for n in range(1, 31))
OWNER += "\n- Allowed Paths を調整してください: [Claude Code](https://code.claude.com/docs/en/hooks)"


def _consumer(plan, state, calls, known_context=None, *, anchor_body=OWNER):
    def fetch():
        return ({"body": state["body"], "updatedAt": "2026-09-29T00:00:00Z"},
                {"id": 5891092074, "body": anchor_body, "html_url": URL, "author_association": "OWNER"})

    def apply(_issue, body, _readiness):
        calls.append("write")
        state["body"] = body
        return {"status": "applied"}

    return preflight.consume_trusted_anchor_contract_patch_plan(
        repo=REPO, issue_number=ISSUE,
        issue={"body": state["body"], "updatedAt": "2026-09-29T00:00:00Z"},
        anchor_url=URL, anchor_payload={"id": 5891092074, "author_association": "OWNER"},
        anchor_body=anchor_body, contract_patch_plan={"operations": plan},
        callbacks={"fetch_current": fetch, "candidate_readiness": lambda _body: {"status": "go"},
                   "apply_transaction": apply},
        known_context=known_context,
    )


def test_given_unmapped_owner_directive_when_plan_derived_then_no_raw_ac_or_url_path():
    evidence = {"directive_markers": ["contract update", "revised ac", "allowed paths"],
                "extracted_directives": [line.removeprefix("- ") for line in OWNER.splitlines()]}
    operations = derive_contract_patch_operations([evidence])
    assert operations == []
    assert all("docs/en/hooks" not in str(op) for op in operations)


def test_given_only_revised_ac_marker_when_raw_prose_is_extracted_then_no_append_generated():
    evidence = {"directive_markers": ["revised ac"],
                "extracted_directives": ["- この背景と手順をそのまま転記してください。"]}
    assert derive_contract_patch_operations([evidence]) == []
    evidence["extracted_directives"] = ["AC19: 明示的に番号付けした受け入れ条件"]
    assert derive_contract_patch_operations([evidence])[0]["text"] == (
        "- AC19: 明示的に番号付けした受け入れ条件"
    )


def test_given_checkbox_ac_mentions_stop_conditions_when_derived_then_ac_operations_survive():
    # Issue #1270's OWNER evidence is evidence-only: a numbered Revised AC
    # mentions stop conditions in its content but is not a Stop directive.
    evidence = {"directive_markers": ["revised ac", "revised acceptance criteria", "stop condition", "前提条件"],
                "extracted_directives": [
                    "[ ] AC0: runtime order、eligible profiles、stop conditions が定義されている。",
                    "[ ] AC1: failure classes の対応表が追加されている。",
                ]}
    operations = derive_contract_patch_operations([evidence])
    assert [(op["section"], op["text"]) for op in operations] == [
        ("Acceptance Criteria", "- [ ] AC0: runtime order、eligible profiles、stop conditions が定義されている。"),
        ("Acceptance Criteria", "- [ ] AC1: failure classes の対応表が追加されている。"),
    ]


def test_given_checkbox_ac_and_unsafe_path_when_derived_then_no_partial_plan():
    evidence = {"directive_markers": ["revised ac", "allowed paths"],
                "extracted_directives": [
                    "[ ] AC0: safe numbered acceptance criterion",
                    "Allowed Paths を追加してください: `../../escape.py`",
                ]}
    assert derive_contract_patch_operations([evidence]) == []


def test_given_sectioned_owner_comment_when_extracted_then_stop_vc_ac_and_paths_are_preserved():
    comment = """## Revised Acceptance Criteria
- [ ] AC19: verify one outcome
  and check the readback remains stable.

## Stop Conditions
- Stop if an unknown authority changes the Issue.

## Verification Commands
- $ uv run --locked pytest tests/example.py

## Allowed Paths
- `scripts/agent-guards/skill_runtime_exec.py`
- `docs/dev/workflow.md`

## In Scope
- Preserve structured updates

## Out of Scope
- No permission widening
"""
    evidence = {"directive_markers": ["revised ac", "stop condition", "verification command", "allowed paths"],
                "extracted_directives": extract_directive_items(comment)}
    operations = derive_contract_patch_operations([evidence], source_body=comment)
    assert [(op["section"], op["text"]) for op in operations] == [
        ("Acceptance Criteria", "- [ ] AC19: verify one outcome\n  and check the readback remains stable."),
        ("Stop Conditions", "- Stop if an unknown authority changes the Issue."),
        ("Verification Commands", "- $ uv run --locked pytest tests/example.py"),
        ("Allowed Paths", "- `scripts/agent-guards/skill_runtime_exec.py`"),
        ("Allowed Paths", "- `docs/dev/workflow.md`"),
        ("In Scope", "- Preserve structured updates"),
        ("Out of Scope", "- No permission widening"),
    ]


def test_given_mixed_valid_and_invalid_structured_directives_when_derived_then_no_partial_plan():
    comment = """## Revised Acceptance Criteria
- [ ] AC19: safe

## Stop Conditions
- Forbidden write: `../../escape.py`

## Verification Commands
- $ uv run --locked pytest tests/example.py
"""
    evidence = {"directive_markers": ["revised ac", "stop condition", "verification command"],
                "extracted_directives": extract_directive_items(comment)}
    assert derive_contract_patch_operations([evidence], source_body=comment) == []


def test_given_real_comment_extraction_when_consumer_runs_then_all_sections_written():
    comment = """## Revised AC
- [ ] AC19: structured acceptance
## Stop Conditions
- Stop if new authority is required.
## Verification Commands
- $ uv run --locked pytest tests/example.py
"""
    evidence = {"directive_markers": ["revised ac", "stop condition", "verification command"],
                "extracted_directives": extract_directive_items(comment)}
    operations = derive_contract_patch_operations([evidence], source_body=comment)
    state = {"body": BODY + "\n## Stop Conditions\n\n- existing\n\n## Verification Commands\n\n- $ true\n"}
    calls = []
    result = _consumer(operations, state, calls, anchor_body=comment)
    assert result["status"] == "applied" and result["writes"] == 1 and calls == ["write"]
    for op in operations:
        assert op["text"] in state["body"]


def test_given_unstructured_plan_when_consumer_runs_then_no_write_and_body_unchanged():
    state = {"body": BODY}
    calls = []
    plan = [{"section": "Acceptance Criteria", "op": "append", "text": OWNER,
             "source_evidence_index": 0}]
    result = _consumer(plan, state, calls)
    assert result["failure"] == "unsafe_unstructured_patch_operation"
    assert result["status"] == "blocked"
    assert result["writes"] == 0
    assert state["body"] == BODY
    assert calls == []
    assert preflight._bounded_contract_update_handoff(result)["reason_code"] == result["failure"]


def test_given_empty_unsafe_freeform_plan_when_consumer_runs_then_write_zero_with_reason():
    state = {"body": BODY}
    calls = []
    evidence = preflight._build_scope_delta_authority_evidence(
        comment_payload={"id": 5891092074, "author_association": "OWNER",
                         "user": {"login": "squne121", "type": "User"}},
        comment_body=OWNER, repo=REPO, issue_number=ISSUE, anchor_url=URL,
        captured_at="2026-09-29T00:00:00Z", human_context_comment_urls=[URL],
    )
    assert derive_contract_patch_operations([evidence]) == []
    result = _consumer([], state, calls, known_context={
        "scope_delta_authority_evidence": [evidence], "human_context_comment_urls": [URL],
    })
    assert result["status"] == "blocked"
    assert result["failure"] == "unsafe_unstructured_patch_operation"
    assert result["writes"] == 0 and state["body"] == BODY and not calls


def test_given_structured_section_bound_ac_when_consumer_runs_then_existing_write_path_survives():
    state = {"body": BODY}
    calls = []
    result = _consumer([{"section": "Acceptance Criteria", "op": "append",
                         "text": "- [ ] AC19: structured", "source_evidence_index": 0}], state, calls)
    assert result["status"] == "applied"
    assert result["writes"] == 1
    assert calls == ["write"]
    assert "- [ ] AC19: structured" in state["body"]


def test_given_structured_section_replacement_when_consumer_runs_then_existing_write_path_survives():
    state = {"body": BODY}
    calls = []
    result = _consumer([{"section": "Acceptance Criteria", "op": "replace", "kind": "replace",
                         "text": "- [ ] AC18: replaced", "source_evidence_index": 0}], state, calls)
    assert result["status"] == "applied"
    assert result["writes"] == 1
    assert calls == ["write"]
    assert "- [ ] AC18: replaced" in state["body"]
    assert "- [ ] AC18: existing" not in state["body"]
