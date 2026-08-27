#!/usr/bin/env python3
"""Live claude CLI integration test for run_retrospective.py's full
`run_cli()` pipeline (Issue #2362 AC3): 3 observers -> evaluator -> canonical
candidate -> `PublishRequest`, exercising `run_evaluation()`'s deterministic
enrichment phase against a REAL evaluator Agent response and asserting no
`candidate_schema_invalid` error is raised for a non-empty evaluator
finding.

Runtime Verification Applicability: immediate (AC3,
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
genuine `retrospective-evaluator` Agent invocation (the one leaf this
repo's Allowed Paths and `retrospective-evaluator.md`'s Out-of-Scope
frontmatter/prompt freeze forbid mocking or modifying) is exercised for
real. The 3 observers are given caller-supplied prompts (`run_cli(prompts=
...)`) instructing them to report ONE nonce-tagged finding each from
supplied evidence only (no tool use), so the (unmodified, real, adversarial)
evaluator has concrete, convergent evidence to synthesize a genuine
`candidate_records` entry from -- proving `run_evaluation()`'s enrichment
phase against an ACTUAL evaluator identity.value/identity.key output, not
a fixture stand-in.
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


def _shared_evidence_text(nonce: str) -> str:
    """Natural-language-only evidence text (Issue #2362) -- deliberately
    contains NO embedded JSON/schema-shape hints. Earlier iterations of
    this test embedded an explicit `finding_contract.identity.key`/
    `evaluations[]` JSON shape example directly in the evidence text to
    coach the live evaluator toward the canonical shape; live runs showed
    this consistently and correctly triggers the (adversarial,
    Out-of-Scope-to-change) evaluator's prompt-injection suspicion --
    `retrospective-evaluator.md` explicitly instructs it not to blindly
    adopt values embedded in observer claims, and evidence text containing
    a literal example of the evaluator's OWN expected output schema is
    exactly the kind of pattern that should (correctly) raise that
    suspicion. Relying on plain, concrete evidence instead, and letting
    `run_evaluation()`'s deterministic enrichment phase (identity.key
    fallback) plus its `evaluations[]` field-set/hash fallback (Issue
    #2362) absorb whatever shape variance the evaluator's own
    still-underspecified `identity.key`/`evaluations[]` example placeholder
    produces, is both more reliable AND a more honest test of this Issue's
    actual production-code robustness guarantee."""
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
        "indirect."
    )


def _observer_task_prompt(nonce: str) -> str:
    evidence = _shared_evidence_text(nonce)
    return (
        "This is a real agent-retrospective observer wave invocation "
        f"(Issue #2362 AC3 live-CLI full-pipeline regression coverage, "
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
    """AC3: `run_cli()` with real observer + evaluator Agent invocations
    reaches `PublishRequest` for a non-empty evaluator finding without
    raising `WireContractError(reason_code="candidate_schema_invalid")` --
    proof that `run_evaluation()`'s deterministic enrichment phase (Issue
    #2362) always supplies a schema-valid `finding_contract.identity` even
    though the evaluator's own `identity.value`/`identity.key.repository_id`
    carry no authority and are unconditionally overwritten. Also
    independently re-verifies (outside `Evaluation.__post_init__`) that
    every returned candidate's `identity.key.repository_id` equals the
    Python-side `repository_id` this test passed in -- never anything the
    live evaluator itself might have produced."""
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
        "this test's nonce-tagged, concrete observer evidence -- AC3 "
        "requires a non-empty finding to prove the enrichment phase "
        "against a genuine evaluator identity.key/identity.value output"
    )

    validator_mod = rr._validate_retrospective_schema_module()
    for record in publish_request.candidate_records:
        # must not raise -- independent re-check of the exact validator
        # `_validate_candidate_records()` uses internally.
        validator_mod.validate_candidate(record)

        finding_contract = record.get("finding_contract")
        if finding_contract is None:
            continue
        identity = finding_contract["identity"]
        key = identity["key"]
        # Issue #2362 AC1: repository_id is always Python-side caller
        # context -- never the live evaluator's own judgment.
        assert key["repository_id"] == _REPOSITORY_ID
        recomputed = validator_mod.compute_finding_identity(key)
        assert identity["value"] == recomputed
