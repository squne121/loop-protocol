#!/usr/bin/env python3
"""Live claude CLI integration test for run_retrospective.py's full
`run_cli()` pipeline (Issue #2362 AC5, renumbered from the pre-Scope-Reframe
AC3): 3 observers -> evaluator (judgment-only output) -> deterministic
enrichment -> canonical candidate -> `PublishRequest`, exercising
`run_evaluation()`'s deterministic enrichment phase against a REAL
evaluator Agent response and asserting no `candidate_schema_invalid` error
is raised for a non-empty evaluator finding -- the actual proof that the
Scope Reframe architecture (judgment-only wire schema + rewritten evaluator
prompt + 100% Python-side deterministic assembly) resolves the vocabulary-
drift root cause PR #2367's OWNER review and fix_delta blocked report
identified (see this Issue's "Scope Reframe" section).

Runtime Verification Applicability: immediate (AC5,
docs/dev/runtime-verification-policy.md). This module is marked
`claude_live` (registered in `pyproject.toml`) and is therefore excluded
from the default pytest run (`-m 'not github_live and not claude_live'`
addopts). It is invoked only via
`verify_run_evaluation_enrichment_live_cli.sh`, which performs the
`skip_conditions` preflight (claude binary present in PATH; `claude auth
status` exits 0) BEFORE ever invoking pytest -- once pytest starts here,
every failure is a real FAIL (wrapper exit 1), never converted to a SKIP
(`fallback_policy`: SKIP never promotes to PASS).

Unlike `test_run_retrospective_live_cli.py` (which calls the observer
adapter, `invoke_agent()`, directly, one layer below `run_observer_wave()`),
this module calls the production `run_cli()` call graph itself, so a
genuine `retrospective-evaluator` Agent invocation is exercised for real
(the evaluator prompt is now IN this Issue's Allowed Paths and has been
rewritten to match the judgment-only wire contract -- see the Scope Reframe
section). The 3 observers are given caller-supplied prompts (`run_cli(prompts=
...)`) instructing them to report ONE nonce-tagged finding each from
supplied evidence only (no tool use), so the (unmodified per-run, real,
adversarial) evaluator has concrete, convergent evidence to synthesize a
genuine `candidate_records` entry from -- proving `run_evaluation()`'s
enrichment phase against an ACTUAL evaluator judgment output, not a fixture
stand-in.
"""
from __future__ import annotations

import sys
import uuid
from pathlib import Path

import pytest

_SCRIPTS_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_SCRIPTS_DIR))

import run_retrospective as rr  # noqa: E402

pytestmark = pytest.mark.claude_live

_SCHEMA_DIR = _SCRIPTS_DIR / "schemas"
_REPO_ROOT = _SCRIPTS_DIR.parents[3]
_REPOSITORY_ID = "squne121/loop-protocol"
_TARGET_ISSUE = 2362

_OBSERVER_IDS = tuple(spec.observer_id for spec in rr.EXPECTED_OBSERVER_MANIFEST)

#: explicit prohibitions (Issue #2362) -- known synthetic/fallback marker
#: substrings the now-DELETED PR #2367-superseded fallback code paths
#: (`_fallback_subject_ref`/`_fallback_rule_id`/`_fallback_evaluation_entry`)
#: used to emit. This Scope Reframe's `run_evaluation()` has no fallback
#: code path left at all, so none of these should ever appear in a
#: genuinely enriched candidate -- kept as an explicit regression guard
#: against silently reintroducing one.
_SYNTHETIC_MARKER_SUBSTRINGS = (
    "agent-retrospective-finding:",
    "agent-retrospective-deterministic-enrichment-fallback:",
    "unidentified-candidate",
)

_CLAIM_CLASS_ENUM = frozenset(
    {
        "code_content",
        "code_authorship_timing",
        "internal_loop_review_verdict",
        "github_native_review_state",
        "review_comment",
        "mergeability",
        "issue_intent",
        "external_fact",
        "runtime_behavior",
    }
)
_SUBJECT_REF_KIND_ENUM = frozenset(
    {"repository_path", "issue", "pull_request", "workflow", "runtime", "external_resource"}
)


def _shared_evidence_text(nonce: str) -> str:
    """Natural-language-only evidence text (Issue #2362) -- deliberately
    contains NO embedded JSON/schema-shape hints; the evaluator's own
    prompt (`retrospective-evaluator.md`, Scope Reframe rewrite) already
    supplies a concrete, correctly-shaped judgment-only output example, so
    this evidence text only needs to describe a real, concrete defect for
    the evaluator to reason about -- not coach it toward any particular
    JSON shape (which would risk triggering the evaluator's own
    prompt-injection suspicion instructions)."""
    return (
        f"Verified fact (nonce NONCE_{nonce}): a fixture harness file at "
        f"agent-retrospective/fixtures/NONCE_{nonce}_enrichment_probe.py "
        "contains a SKIP(77)/FAIL(1)/PASS(0) wrapper whose exception "
        "handler is a bare `except Exception: return 0`, which silently "
        "converts a genuine subprocess failure into a fabricated PASS exit "
        "code. This directly violates this repository's "
        "docs/dev/runtime-verification-policy.md fallback_policy "
        '("SKIP は PASS に変換しない") and the SKIP/FAIL/PASS wrapper '
        "contract every other verification script in this repository "
        "follows. This is a concrete, genuine runtime-verification-"
        "swallowing defect worth flagging as an improvement candidate. "
        "The evidence above was gathered directly by reading the file's "
        "contents -- it is complete, direct evidence, not partial or "
        "indirect. The subject of this finding is the repository-relative "
        f"path agent-retrospective/fixtures/NONCE_{nonce}_enrichment_probe.py."
    )


def _observer_task_prompt(nonce: str) -> str:
    evidence = _shared_evidence_text(nonce)
    return (
        "This is a real agent-retrospective observer wave invocation "
        f"(Issue #2362 AC5 live-CLI full-pipeline regression coverage, "
        f"nonce {nonce}). You have no tools available for this run and "
        "must NOT attempt to browse the repository, run any command, or "
        "search the web -- use ONLY the following supplied evidence as "
        "your investigation source (do not invent additional evidence, "
        "do not contradict it):\n\n"
        f"{evidence}\n\n"
        "Report EXACTLY one finding in your OBSERVER_RESULT_V1 JSON "
        f'response whose "claim" field explicitly includes the exact '
        f'token NONCE_{nonce} verbatim (quote it, do not paraphrase or '
        f'alter it) and describes the defect above, with "claim_class" '
        'set to "runtime_behavior". Do not report an empty findings list '
        "-- concrete evidence was supplied above; findings must reflect it."
    )


def test_real_claude_cli_full_pipeline_enrichment_no_candidate_schema_invalid() -> None:
    """AC5: `run_cli()` with real observer + evaluator Agent invocations
    reaches `PublishRequest` for a non-empty evaluator finding without
    raising `WireContractError(reason_code="candidate_schema_invalid")` --
    proof that `run_evaluation()`'s deterministic enrichment phase (Issue
    #2362 Scope Reframe: judgment-only wire schema + rewritten evaluator
    prompt + 100% Python-side deterministic identity/evaluations[]/
    evidence_refs assembly) always supplies a schema-valid
    `finding_contract` even though the evaluator's own output carries no
    identity/history/digest authority at all any more. Also independently
    re-verifies (outside `Evaluation.__post_init__`) that every returned
    candidate's `identity.key.repository_id` equals the Python-side
    `repository_id` this test passed in, that `identity.value` matches an
    independent `compute_finding_identity()` recomputation, that no
    synthetic/fallback marker string appears anywhere in the final
    candidate, and that the nonce-tagged candidate's `claim_class`/
    `subject_ref`/`rule_id` genuinely reflect the live evaluator's own
    judgment (not a Python-invented value -- which by this Issue's
    architecture is now structurally impossible for these three fields,
    since `_enrich_candidate_record` raises `candidate_schema_invalid`
    rather than substituting anything when they are absent/malformed)."""
    nonce = uuid.uuid4().hex
    prompts = {observer_id: _observer_task_prompt(nonce) for observer_id in _OBSERVER_IDS}

    request_id = f"live-enrichment-req-{nonce}"
    idempotency_key = f"live-enrichment-idem-{nonce}"

    publish_request = rr.run_cli(
        repo_root=_REPO_ROOT,
        repository_id=_REPOSITORY_ID,
        target_issue=_TARGET_ISSUE,
        request_id=request_id,
        idempotency_key=idempotency_key,
        schema_dir=_SCHEMA_DIR,
        prompts=prompts,
    )

    print(
        "test_real_claude_cli_full_pipeline_enrichment_no_candidate_schema_invalid: "
        f"reached PublishRequest with {len(publish_request.candidate_records)} candidate_records"
    )

    assert isinstance(publish_request, rr.PublishRequest)
    assert publish_request.request_id == request_id
    assert publish_request.repository_id == _REPOSITORY_ID
    assert publish_request.candidate_records, (
        "the live evaluator returned an empty candidate_records list for "
        "this test's nonce-tagged, concrete observer evidence -- AC5 "
        "requires a non-empty finding to prove the enrichment phase "
        "against a genuine evaluator judgment output"
    )

    validator_mod = rr._validate_retrospective_schema_module()
    full_wire_text = "\n".join(
        rr.json.dumps(record, sort_keys=True) if not isinstance(record, str) else record
        for record in publish_request.candidate_records
    )
    for marker in _SYNTHETIC_MARKER_SUBSTRINGS:
        assert marker not in full_wire_text, (
            f"synthetic/fallback marker {marker!r} found in the final PublishRequest "
            "candidate_records -- this Issue's architecture has NO fallback code path; "
            "any occurrence indicates one was reintroduced (explicit prohibition)"
        )

    for record in publish_request.candidate_records:
        # must not raise -- independent re-check of the exact validator
        # `_validate_candidate_records()` uses internally.
        validator_mod.validate_candidate(record)

    # Issue #2367 fix_delta item 5 (preserved under the Scope Reframe):
    # identify the SPECIFIC nonce-tagged candidate this test's own
    # observer evidence produced -- looping over `candidate_records` with
    # `continue` on a missing `finding_contract` (the previous design)
    # could let a single legacy-shaped candidate satisfy the whole loop
    # without the identity-enrichment assertions below ever running for
    # THIS test's finding. The assertions are mandatory (not skippable)
    # for the nonce-tagged target.
    # Issue #2362 AC5: the evaluator is judgment-only now and free to
    # paraphrase the observer claim text into title/description while
    # placing the identifying nonce token in a DIFFERENT judgment field it
    # considers more precise (observed live: `subject_ref.value` -- a
    # concrete repository-relative path derived from the nonce-tagged
    # fixture filename the evidence text named) -- so this match searches
    # the candidate's full JSON representation, not only `description`.
    target = next(
        (
            record
            for record in publish_request.candidate_records
            if f"NONCE_{nonce}" in rr.json.dumps(record, ensure_ascii=False)
        ),
        None,
    )
    assert target is not None, (
        f"no candidate_records entry contained the nonce token NONCE_{nonce} "
        "anywhere in its JSON representation -- AC5 requires the "
        "nonce-tagged finding produced from this test's own observer "
        "evidence to be identifiable in the final PublishRequest output, "
        "not merely SOME candidate (possibly unrelated/legacy) to be present"
    )
    assert target.get("finding_contract"), (
        f"the nonce-tagged (NONCE_{nonce}) candidate_records entry has no "
        "finding_contract -- AC5 requires identity enrichment to be "
        "exercised for the finding this test's evidence produced, not "
        "silently skipped"
    )
    finding_contract = target["finding_contract"]
    identity = finding_contract["identity"]
    key = identity["key"]
    # Issue #2362 AC1/AC3: repository_id is always Python-side caller
    # context -- never the live evaluator's own judgment.
    assert key["repository_id"] == _REPOSITORY_ID
    recomputed = validator_mod.compute_finding_identity(key)
    assert identity["value"] == recomputed

    # AC5: claim_class/subject_ref/rule_id genuinely reflect the live
    # evaluator's own judgment -- structurally guaranteed by this Issue's
    # architecture (no fallback/synthesis path exists any more for these
    # three fields; a missing/malformed value would have raised
    # candidate_schema_invalid before reaching this PublishRequest at all).
    assert key["claim_class"] == finding_contract["claim_class"]
    assert key["claim_class"] in _CLAIM_CLASS_ENUM
    assert key["subject_ref"]["kind"] in _SUBJECT_REF_KIND_ENUM
    assert key["rule_id"]
    # a genuine evaluator judgment is never a mechanical slug of the
    # candidate_id (the pre-#2367 fallback design's degenerate behavior).
    assert key["rule_id"] != target["candidate_id"].lower().replace("-", "_")

    # AC5: no evaluation entry was fabricated -- every entry's
    # evidence_refs[].projection_digest is a genuine sha256 hex digest
    # (Python-recomputed from real observer evidence), never empty/absent
    # when the entry is classified.
    for entry in finding_contract["evaluations"]:
        if entry["evaluation_status"] == "classified":
            assert entry["evidence_refs"], "a classified entry must carry non-empty, real evidence_refs"
        for evidence_ref in entry["evidence_refs"]:
            assert evidence_ref["projection_digest"].startswith("sha256:")
            assert len(evidence_ref["projection_digest"]) == len("sha256:") + 64
