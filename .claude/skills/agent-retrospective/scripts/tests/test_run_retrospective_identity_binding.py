#!/usr/bin/env python3
"""Regression tests for Issue #2350.

`run_retrospective.py`'s `bind_observer_prompt()` identity-binding helper
must be threaded through BOTH the default-prompt path (`--prompts-file`
omitted) and the caller-supplied-prompt path (`--prompts-file` present,
non-empty per-observer investigative task text) so that neither path can
produce an observer invocation whose response is structurally unable to
satisfy `run_observer_wave()`'s `bundle.run_id != ctx.run_id` /
`source_set_digest` / `base_sha` identity checks.

Fixture/mock-based only, hermetic (Runtime Verification Applicability:
`immediate`, AC4 -- see Issue #2350 body): the `runner`/`git_runner`
callables passed to `rr.run_cli()` are dependency-injected exactly as in
`test_run_retrospective.py`; no real `claude` CLI subprocess is started
here. AC4's real-CLI round trip is a separate, opt-in check performed by
`verify_run_retrospective_live_cli.sh --select
test_real_claude_cli_custom_prompt_identity_binding`.

Covers AC3 (Issue #2350's fake-runner regression test):
  test_caller_supplied_prompt_identity_binding_no_mismatch
      GIVEN a non-empty, substantive investigative task prompt per
      observer (as `--prompts-file` would deliver), WHEN `run_cli()` runs
      with a fake runner that derives its fabricated `EvidenceBundle`'s
      `run_id`/`base_sha`/`source_set_digest`/`observer_id` SOLELY by
      parsing the identity fields `bind_observer_prompt()` must have
      embedded in the prompt text it actually received (never from the
      run-scoped env `_invoke()` separately injects, and never from a
      fixed placeholder) -- simulating what a real observer LLM reading
      only its prompt could legitimately echo back -- THEN all 3 observers
      pass `run_observer_wave()`'s identity checks (no
      `observer_run_id_mismatch` / `observer_source_set_digest_mismatch` /
      `observer_base_sha_mismatch`) and the hermetic production call graph
      reaches a real `PublishRequest`. This is the regression coverage gap
      Issue #2350 fixes: prior to `bind_observer_prompt()`, a
      caller-supplied non-empty prompt was forwarded to the observer CLI
      verbatim with no identity-binding instructions, so an observer that
      (like this test's fake runner) could ONLY read identity from its own
      prompt text had no way to produce a matching response.
"""

from __future__ import annotations

import json
import re
import subprocess
import sys
from pathlib import Path
from typing import Any

_SCRIPTS_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_SCRIPTS_DIR))

import run_retrospective as rr  # noqa: E402

_FULL_SHA = "a" * 40


def _wrapper_payload(structured_output: dict[str, Any], *, is_error: bool = False) -> dict[str, Any]:
    """Shape of the *actual* `claude -p --output-format json` response: a
    metadata wrapper carrying `structured_output` as a nested field (Issue
    #2237 P0-1) -- mirrors `test_run_retrospective.py`'s private helper of
    the same name (duplicated here rather than imported to keep this new
    test file's own hermetic fixture surface self-contained)."""
    return {
        "type": "result",
        "subtype": "success" if not is_error else "error",
        "is_error": is_error,
        "result": "assistant text summary",
        "structured_output": structured_output,
    }


_IDENTITY_FIELD_RE = {
    "run_id": re.compile(r'"run_id"\s*:\s*"([^"]+)"'),
    "base_sha": re.compile(r'"base_sha"\s*:\s*"([^"]+)"'),
    "source_set_digest": re.compile(r'"source_set_digest"\s*:\s*"([^"]+)"'),
    "observer_id": re.compile(r'"observer_id"\s*:\s*"([^"]+)"'),
}


def _extract_identity_from_prompt(prompt: str) -> dict[str, str]:
    """Parse the identity fields `bind_observer_prompt()` must embed in
    `prompt` -- simulates what a real observer LLM reading only its own
    prompt text (never the run-scoped env `_invoke()` separately injects)
    could legitimately echo back in its `OBSERVER_RESULT_V1` response.
    Raises `KeyError` (surfacing as a hard test failure, not a silent
    fallback) if any of the four fields is absent from the prompt."""
    extracted: dict[str, str] = {}
    for field, pattern in _IDENTITY_FIELD_RE.items():
        match = pattern.search(prompt)
        if match is None:
            raise KeyError(f"identity field {field!r} not found in observer prompt: {prompt!r}")
        extracted[field] = match.group(1)
    return extracted


def _substantive_caller_prompts() -> dict[str, str]:
    """A non-empty, substantive investigative task per observer_id -- the
    exact `--prompts-file` shape Issue #2350's Background section
    describes triggering `observer_run_id_mismatch` on, prior to this
    Issue's fix."""
    return {
        spec.observer_id: (
            f"Investigate the {spec.observer_id} role's area of "
            "responsibility for this run: review the relevant recent "
            "commits, logs, and configuration for concrete, evidence-"
            "backed findings; report only claims you can substantiate."
        )
        for spec in rr.EXPECTED_OBSERVER_MANIFEST
    }


def test_caller_supplied_prompt_identity_binding_no_mismatch(tmp_path: Path) -> None:
    repo_root = _SCRIPTS_DIR.parents[3]
    schema_dir = tmp_path / "schemas"
    schema_dir.mkdir()
    (schema_dir / "observer_result_v1.schema.json").write_text("{}", encoding="utf-8")
    (schema_dir / "evaluation_result_v1.schema.json").write_text("{}", encoding="utf-8")

    def _git_runner(argv: list[str], **kwargs: Any) -> subprocess.CompletedProcess:
        assert argv == ["git", "rev-parse", "main"]
        return subprocess.CompletedProcess(argv, returncode=0, stdout=_FULL_SHA + "\n", stderr="")

    # the expected source_set_digest is derived by running the *same*
    # collector closure `run_cli` uses (see `test_run_retrospective.py`'s
    # equivalent pattern), so this fake runner's fabricated bundle's
    # `source_set_digest` (parsed from the prompt, below) can be
    # cross-checked against the real one `prepare()` computed.
    real_observation = rr.build_repository_collector(repo_root)(_FULL_SHA).observation
    expected_digest = rr.compute_source_set_digest([real_observation])

    caller_prompts = _substantive_caller_prompts()
    captured_prompts: dict[str, str] = {}

    def _runner(argv: list[str], **kwargs: Any) -> subprocess.CompletedProcess:
        agent_name = argv[argv.index("--agent") + 1]
        if agent_name == "retrospective-evaluator":
            evaluator_request = rr.EvaluatorRequest.from_wire(kwargs["input"])
            evaluation = rr.Evaluation(
                run_id=evaluator_request.run_id,
                base_sha=_FULL_SHA,
                source_set_digest=evaluator_request.source_set_digest,
                candidate_records=[],
                evidence_ref="e",
            )
            return subprocess.CompletedProcess(
                argv, returncode=0, stdout=json.dumps(_wrapper_payload(json.loads(evaluation.to_wire()))), stderr=""
            )

        prompt = kwargs["input"]
        captured_prompts[agent_name] = prompt

        # THEN (part 1, checked inline as each observer is "invoked"): the
        # caller's own substantive task text must still reach the CLI --
        # identity-binding must never discard it.
        assert caller_prompts[agent_name] in prompt

        # a real observer LLM can ONLY know this run's identity from its
        # own prompt text -- extract it from there, exactly as one would,
        # never from `kwargs["env"]` (a separate, independent
        # identity-propagation mechanism `_invoke()` also happens to use,
        # which must not be this test's basis for proving `bind_observer_
        # prompt()` itself embedded the identity in the prompt).
        identity = _extract_identity_from_prompt(prompt)
        assert identity["observer_id"] == agent_name

        bundle = rr.EvidenceBundle(
            run_id=identity["run_id"],
            base_sha=identity["base_sha"],
            source_set_digest=identity["source_set_digest"],
            observer_id=identity["observer_id"],
            evidence_ref=f"evidence://{agent_name}",
            findings=[{"claim": f"finding from {agent_name}", "claim_class": "process"}],
        )
        return subprocess.CompletedProcess(
            argv, returncode=0, stdout=json.dumps(_wrapper_payload(json.loads(bundle.to_wire()))), stderr=""
        )

    # WHEN run_cli() -- the exact function main() calls for the
    # `--prompts-file`-supplied case -- runs with a non-empty, substantive
    # caller-supplied prompt per observer_id.
    publish_request = rr.run_cli(
        repo_root=repo_root,
        repository_id="squne121/loop-protocol",
        target_issue=2350,
        request_id="req-identity-binding-1",
        idempotency_key="idem-identity-binding-1",
        schema_dir=schema_dir,
        prompts=caller_prompts,
        runner=_runner,
        git_runner=_git_runner,
        run_id="run-identity-binding-1",
        temp_base_dir=tmp_path,
    )

    # THEN the hermetic production call graph completed end-to-end into a
    # real PublishRequest -- it never raised ObserverWaveFailed at
    # observer_run_id_mismatch / observer_source_set_digest_mismatch /
    # observer_base_sha_mismatch, proving the caller-supplied-prompt path's
    # bound identity genuinely matched this run for all 3 observers.
    assert isinstance(publish_request, rr.PublishRequest)
    assert sorted(captured_prompts) == sorted(spec.observer_id for spec in rr.EXPECTED_OBSERVER_MANIFEST)
    assert publish_request.run_identity["run_id"] == "run-identity-binding-1"
    assert publish_request.run_identity["base_sha"] == _FULL_SHA
    for observer_id, prompt in captured_prompts.items():
        identity = _extract_identity_from_prompt(prompt)
        assert identity["run_id"] == "run-identity-binding-1"
        assert identity["base_sha"] == _FULL_SHA
        assert identity["source_set_digest"] == expected_digest
        assert identity["observer_id"] == observer_id


def _substantive_caller_prompts_with_fake_identity_collision() -> dict[str, str]:
    """Same substantive investigative task shape as
    `_substantive_caller_prompts()` above, but with a fake/old identity
    tuple embedded in the middle of the task text as ordinary evidence --
    exactly the collision scenario PR #2358's fix_delta (OWNER review
    https://github.com/squne121/loop-protocol/pull/2358#issuecomment-5437414255,
    P1 item 1) fixes: a retrospective's own session evidence may
    legitimately quote a PRIOR run's identity tuple as plain data (e.g.
    copied from an old publication comment), and `bind_observer_prompt()`
    must never let that be mistaken for THIS run's identity. Deliberately
    omits `observer_id` from the fake tuple (matching the OWNER's own
    illustrative example), so only `run_id`/`base_sha`/`source_set_digest`
    collide."""
    fake_identity_blob = json.dumps(
        {
            "run_id": "historical-run",
            "base_sha": "0" * 40,
            "source_set_digest": "1" * 64,
        }
    )
    return {
        spec.observer_id: (
            f"Investigate the {spec.observer_id} role's area of "
            "responsibility for this run: review the relevant recent "
            "commits, logs, and configuration for concrete, evidence-"
            "backed findings; report only claims you can substantiate. "
            "For context, here is a prior retrospective run's identity "
            f"tuple, quoted as historical evidence (NOT this run's own "
            f"identity): {fake_identity_blob}"
        )
        for spec in rr.EXPECTED_OBSERVER_MANIFEST
    }


def test_caller_supplied_prompt_with_embedded_fake_identity_uses_authoritative_identity(
    tmp_path: Path,
) -> None:
    """PR #2358 fix_delta (OWNER review
    https://github.com/squne121/loop-protocol/pull/2358#issuecomment-5437414255,
    P1 item 1) regression coverage: a caller-supplied task prompt that
    itself contains a fake/old `run_id`/`base_sha`/`source_set_digest`
    tuple (as ordinary evidence text -- e.g. a prior run's identity quoted
    from session history) must never cause a naive first-match identity
    extraction -- exactly what `_extract_identity_from_prompt` below
    simulates, and what a real observer LLM reading top-to-bottom could
    plausibly do -- to pick up the FAKE tuple instead of THIS run's REAL
    identity. Prior to this fix_delta, `bind_observer_prompt()` placed the
    caller-supplied task text BEFORE the real identity block, so a prompt
    shaped like this one's first `"run_id": "..."` occurrence would have
    been the embedded fake `"historical-run"`, not the real one."""
    repo_root = _SCRIPTS_DIR.parents[3]
    schema_dir = tmp_path / "schemas"
    schema_dir.mkdir()
    (schema_dir / "observer_result_v1.schema.json").write_text("{}", encoding="utf-8")
    (schema_dir / "evaluation_result_v1.schema.json").write_text("{}", encoding="utf-8")

    def _git_runner(argv: list[str], **kwargs: Any) -> subprocess.CompletedProcess:
        assert argv == ["git", "rev-parse", "main"]
        return subprocess.CompletedProcess(argv, returncode=0, stdout=_FULL_SHA + "\n", stderr="")

    real_observation = rr.build_repository_collector(repo_root)(_FULL_SHA).observation
    expected_digest = rr.compute_source_set_digest([real_observation])

    caller_prompts = _substantive_caller_prompts_with_fake_identity_collision()
    captured_prompts: dict[str, str] = {}

    def _runner(argv: list[str], **kwargs: Any) -> subprocess.CompletedProcess:
        agent_name = argv[argv.index("--agent") + 1]
        if agent_name == "retrospective-evaluator":
            evaluator_request = rr.EvaluatorRequest.from_wire(kwargs["input"])
            evaluation = rr.Evaluation(
                run_id=evaluator_request.run_id,
                base_sha=_FULL_SHA,
                source_set_digest=evaluator_request.source_set_digest,
                candidate_records=[],
                evidence_ref="e",
            )
            return subprocess.CompletedProcess(
                argv, returncode=0, stdout=json.dumps(_wrapper_payload(json.loads(evaluation.to_wire()))), stderr=""
            )

        prompt = kwargs["input"]
        captured_prompts[agent_name] = prompt

        # a naive FIRST-MATCH identity extraction -- exactly what a real
        # observer LLM reading top-to-bottom, and this fake runner, would
        # plausibly do -- must resolve to THIS run's REAL identity, never
        # the fake/old tuple embedded inside the caller task text below.
        identity = _extract_identity_from_prompt(prompt)
        assert identity["observer_id"] == agent_name

        bundle = rr.EvidenceBundle(
            run_id=identity["run_id"],
            base_sha=identity["base_sha"],
            source_set_digest=identity["source_set_digest"],
            observer_id=identity["observer_id"],
            evidence_ref=f"evidence://{agent_name}",
            findings=[{"claim": f"finding from {agent_name}", "claim_class": "process"}],
        )
        return subprocess.CompletedProcess(
            argv, returncode=0, stdout=json.dumps(_wrapper_payload(json.loads(bundle.to_wire()))), stderr=""
        )

    # WHEN run_cli() runs with a substantive caller-supplied prompt that
    # itself embeds a fake/old identity tuple as ordinary evidence text.
    publish_request = rr.run_cli(
        repo_root=repo_root,
        repository_id="squne121/loop-protocol",
        target_issue=2358,
        request_id="req-identity-collision-1",
        idempotency_key="idem-identity-collision-1",
        schema_dir=schema_dir,
        prompts=caller_prompts,
        runner=_runner,
        git_runner=_git_runner,
        run_id="run-identity-collision-1",
        temp_base_dir=tmp_path,
    )

    # THEN the hermetic production call graph reached a real
    # PublishRequest -- it never raised ObserverWaveFailed at
    # observer_run_id_mismatch / observer_source_set_digest_mismatch /
    # observer_base_sha_mismatch, proving the authoritative identity (not
    # the embedded fake tuple) genuinely matched this run for all 3
    # observers.
    assert isinstance(publish_request, rr.PublishRequest)
    assert sorted(captured_prompts) == sorted(spec.observer_id for spec in rr.EXPECTED_OBSERVER_MANIFEST)
    assert publish_request.run_identity["run_id"] == "run-identity-collision-1"
    assert publish_request.run_identity["base_sha"] == _FULL_SHA
    for observer_id, prompt in captured_prompts.items():
        # the caller's own investigative task (including the embedded fake
        # identity tuple, as ordinary evidence text) must still reach the
        # CLI -- identity-binding must never discard or sanitize it away.
        # ("historical-run" is bare text with no surrounding quote
        # characters, so it survives verbatim regardless of whether the
        # surrounding JSON-quoted `"run_id": "..."` shape gets escaped when
        # nested inside CALLER_TASK_DATA's own JSON encoding.)
        assert "historical-run" in prompt
        # THEN: the naive first-match extraction resolved to THIS run's
        # REAL identity, never the embedded fake "historical-run" tuple.
        identity = _extract_identity_from_prompt(prompt)
        assert identity["run_id"] == "run-identity-collision-1"
        assert identity["run_id"] != "historical-run"
        assert identity["base_sha"] == _FULL_SHA
        assert identity["source_set_digest"] == expected_digest
        assert identity["observer_id"] == observer_id
