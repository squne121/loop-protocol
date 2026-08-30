"""Offline fake-runner regression test for run_retrospective.py (Issue #2240
fix_delta P1-4(b)).

Exercises the non-empty-findings path end-to-end WITHOUT invoking any real
``claude`` subprocess: a fake ``runner`` callable stands in for
``subprocess.run`` and returns canned ``claude --output-format json``-shaped
payloads keyed off the ``--agent`` argv the real code under test builds.
This directly regression-tests the P0-2 fix_delta (evidence_refs binding
correctness): a codebase-investigator finding's evidence ref must end up
bound to ``repository_blob``/``repository``, a web-researcher finding's to
``external_primary_source``/``web`` -- never cross-bound to ``runtime`` as
the pre-fix evaluator prompt hardcoded for every finding.

Also regression-tests P0-1(d): the standard (no ``runtime_evidence``) path
never invokes ``retrospective-runtime-observer``.

This is intentionally the minimum needed to protect this specific fragile
data flow -- not a general test harness. It has no permanent pytest home
elsewhere in this plugin's Allowed Paths (Issue #2240's Allowed Paths do not
include a top-level ``tests/`` directory), so it lives under
``skills/run/scripts/tests/`` (a subdirectory of the Allowed
``skills/run/scripts/`` path), mirroring the host repository's own project
Skill's own analogous ``scripts/tests/`` test layout convention.

Run with:

    cd plugins/agent-retrospective
    uv run --project . --locked --group test pytest skills/run/scripts/tests/ -q
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

_SCRIPTS_DIR = Path(__file__).resolve().parent.parent
_SCHEMAS_DIR = _SCRIPTS_DIR.parent / "schemas"
sys.path.insert(0, str(_SCRIPTS_DIR))

import run_retrospective as rr  # noqa: E402  (sys.path manipulated above)


def _extract_scoped_agent_name(argv: list[str]) -> str:
    return argv[argv.index("--agent") + 1].split(":", 1)[1]


def _extract_allowed_tools(argv: list[str]) -> frozenset[str]:
    if "--allowedTools" not in argv:
        return frozenset()
    start = argv.index("--allowedTools") + 1
    tools: list[str] = []
    for token in argv[start:]:
        if token.startswith("--"):
            break
        tools.append(token)
    return frozenset(tools)


def _completed_process(payload: dict) -> subprocess.CompletedProcess:
    return subprocess.CompletedProcess(
        args=[],
        returncode=0,
        stdout=json.dumps({"type": "result", "subtype": "success", "is_error": False, "structured_output": payload}),
        stderr="",
    )


def _parse_authoritative_context(prompt: str) -> dict:
    marker = "AUTHORITATIVE_RUN_CONTEXT\n"
    start = prompt.index(marker) + len(marker)
    end = prompt.index("\n\n", start)
    return json.loads(prompt[start:end])


_FAKE_REPO_FINDING = {
    "claim": "run_retrospective.py implements the deterministic phase engine",
    "claim_class": "code_content",
    "repository_path": "skills/run/scripts/run_retrospective.py",
}
_FAKE_WEB_FINDING = {
    "claim": "example.invalid publishes a fake primary source used by this fixture",
    "claim_class": "external_fact",
    "citation_url": "https://example.invalid/fake-primary-source",
}


def build_fake_runner(calls: list[str], argvs: list[list[str]]):
    """Fake ``runner`` (``subprocess.run``-shaped) that never launches a real
    ``claude`` subprocess. Branches purely on the scoped ``--agent`` name
    found in ``argv`` and (for observers) the ``AUTHORITATIVE_RUN_CONTEXT``
    identity block embedded in the prompt (``input=``)."""

    def _runner(argv, *, cwd, env, input, capture_output, text, timeout):  # noqa: A002 (matches subprocess.run's kw name)
        agent_name = _extract_scoped_agent_name(argv)
        calls.append(agent_name)
        argvs.append(argv)

        if agent_name == "retrospective-evaluator":
            request = json.loads(input)
            payload = {
                "schema_version": "evaluation_result/v1",
                "run_id": request["run_id"],
                "base_sha": request["base_sha"],
                "source_set_digest": request["source_set_digest"],
                "evidence_ref": "fake-evaluator-evidence-ref",
                "candidate_records": [
                    {
                        "candidate_id": "finding-fake-repo-001",
                        "title": "fake repository finding",
                        "description": "offline fake-runner regression fixture (Issue #2240 fix_delta P0-2)",
                        "claim_class": "code_content",
                        "subject_ref": {"kind": "repository_path", "value": "skills/run/scripts/run_retrospective.py"},
                        "rule_id": "code_content.example_rule",
                        "evidence_refs": [
                            {
                                "ref_type": "repository_blob",
                                "source_id": "repository",
                                "resource_identity": "skills/run/scripts/run_retrospective.py",
                            }
                        ],
                    },
                    {
                        "candidate_id": "finding-fake-web-001",
                        "title": "fake web finding",
                        "description": "offline fake-runner regression fixture (Issue #2240 fix_delta P0-2)",
                        "claim_class": "external_fact",
                        "subject_ref": {"kind": "external_resource", "value": "https://example.invalid/fake-primary-source"},
                        "rule_id": "external_fact.example_rule",
                        "evidence_refs": [
                            {
                                "ref_type": "external_primary_source",
                                "source_id": "web",
                                "resource_identity": "https://example.invalid/fake-primary-source",
                            }
                        ],
                    },
                ],
            }
            return _completed_process(payload)

        identity = _parse_authoritative_context(input)
        if agent_name == "codebase-investigator":
            findings = [_FAKE_REPO_FINDING]
        elif agent_name == "web-researcher":
            findings = [_FAKE_WEB_FINDING]
        elif agent_name == "retrospective-runtime-observer":  # pragma: no cover - regression guard below
            findings = []
        else:  # pragma: no cover - defensive
            raise AssertionError(f"unexpected agent invoked: {agent_name}")

        payload = {
            "schema_version": "observer_result/v1",
            "run_id": identity["run_id"],
            "base_sha": identity["base_sha"],
            "source_set_digest": identity["source_set_digest"],
            "observer_id": agent_name,
            "evidence_ref": f"fake-{agent_name}-evidence-ref",
            "findings": findings,
        }
        return _completed_process(payload)

    return _runner


@pytest.fixture()
def real_git_repo(tmp_path: Path) -> Path:
    """A REAL (but tiny, local-only) git repository -- only git operations
    are real in this test; only the ``claude`` subprocess boundary is faked."""
    root = tmp_path / "repo"
    root.mkdir()
    subprocess.run(["git", "init", "-q", "-b", "main", str(root)], check=True)
    subprocess.run(["git", "-C", str(root), "config", "user.email", "fake-runner@example.invalid"], check=True)
    subprocess.run(["git", "-C", str(root), "config", "user.name", "fake-runner"], check=True)
    (root / "README.md").write_text("fake-runner fixture repo\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(root), "add", "README.md"], check=True)
    subprocess.run(["git", "-C", str(root), "commit", "-q", "-m", "initial commit"], check=True)
    return root


def test_run_cli_offline_fake_runner_binds_evidence_to_real_sources(real_git_repo: Path) -> None:
    calls: list[str] = []
    argvs: list[list[str]] = []
    fake_runner = build_fake_runner(calls, argvs)

    publish_request = rr.run_cli(
        repo_root=real_git_repo,
        repository_id="example/fake-repo",
        target_issue=None,
        request_id="fake-request-id",
        idempotency_key="fake-idempotency-key",
        schema_dir=_SCHEMAS_DIR,
        plugin_root=None,
        base_ref="HEAD",
        task="find implementation improvement candidates",
        runner=fake_runner,
    )

    # P0-1(d): retrospective-runtime-observer is never invoked on the
    # standard (no runtime_evidence) path.
    assert "retrospective-runtime-observer" not in calls
    # P0-1(e): both active observers were invoked before the evaluator
    # (order between them is intentionally unasserted -- parallel wave).
    assert set(calls[:2]) == {"codebase-investigator", "web-researcher"}
    assert calls[2] == "retrospective-evaluator"
    assert len(calls) == 3

    # P1-1: each observer's argv carries its own least-privilege --allowedTools.
    argv_by_agent = {_extract_scoped_agent_name(argv): argv for argv in argvs}
    assert _extract_allowed_tools(argv_by_agent["codebase-investigator"]) == frozenset({"Read", "Grep", "Glob"})
    assert _extract_allowed_tools(argv_by_agent["web-researcher"]) == frozenset({"WebSearch", "WebFetch"})
    assert _extract_allowed_tools(argv_by_agent["retrospective-evaluator"]) == frozenset()

    assert publish_request.schema_version == "publish_request/v1"
    assert len(publish_request.candidate_records) == 2

    by_id = {c["candidate_id"]: c for c in publish_request.candidate_records}
    repo_candidate = by_id["finding-fake-repo-001"]
    web_candidate = by_id["finding-fake-web-001"]

    repo_refs = repo_candidate["finding_contract"]["evaluations"][-1]["evidence_refs"]
    assert len(repo_refs) == 1
    assert repo_refs[0]["ref_type"] == "repository_blob"
    assert repo_refs[0]["source_id"] == "repository"
    assert repo_refs[0]["projection_digest"].startswith("sha256:")

    web_refs = web_candidate["finding_contract"]["evaluations"][-1]["evidence_refs"]
    assert len(web_refs) == 1
    assert web_refs[0]["ref_type"] == "external_primary_source"
    assert web_refs[0]["source_id"] == "web"
    assert web_refs[0]["projection_digest"].startswith("sha256:")

    # Regression guard for the pre-fix bug (Issue #2240 fix_delta P0-2): a
    # repository/web finding's evidence ref must never bind to "runtime".
    assert repo_refs[0]["source_id"] != "runtime"
    assert web_refs[0]["source_id"] != "runtime"
    assert repo_refs[0]["projection_digest"] != web_refs[0]["projection_digest"]


def test_enrich_evidence_ref_rejects_cross_source_mismatch() -> None:
    """Unit-level regression test isolating the exact pre-fix defect: the
    evaluator hardcoding ``runtime_receipt``/``runtime`` for a finding that
    actually came from a different observer source must be dropped, not
    silently bound (Issue #2240 fix_delta P0-2(a)/(b))."""
    real_evidence_index = {
        "repository": [{"claim": "x", "claim_class": "code_content", "repository_path": "a/b.py"}],
        "web": [{"claim": "y", "claim_class": "external_fact", "citation_url": "https://example.invalid/y"}],
    }
    source_type_observer_ids = rr._source_type_observer_ids()

    mismatched = rr._enrich_evidence_ref(
        {"ref_type": "runtime_receipt", "source_id": "runtime", "resource_identity": "observer:codebase-investigator"},
        real_evidence_index=real_evidence_index,
        source_type_observer_ids=source_type_observer_ids,
    )
    assert mismatched is None

    wrong_ref_type_for_source_id = rr._enrich_evidence_ref(
        {"ref_type": "external_primary_source", "source_id": "repository", "resource_identity": "a/b.py"},
        real_evidence_index=real_evidence_index,
        source_type_observer_ids=source_type_observer_ids,
    )
    assert wrong_ref_type_for_source_id is None

    unrelated_resource_identity = rr._enrich_evidence_ref(
        {"ref_type": "repository_blob", "source_id": "repository", "resource_identity": "totally/unrelated/path.py"},
        real_evidence_index=real_evidence_index,
        source_type_observer_ids=source_type_observer_ids,
    )
    assert unrelated_resource_identity is None

    correct_repository_ref = rr._enrich_evidence_ref(
        {"ref_type": "repository_blob", "source_id": "repository", "resource_identity": "a/b.py"},
        real_evidence_index=real_evidence_index,
        source_type_observer_ids=source_type_observer_ids,
    )
    assert correct_repository_ref is not None
    assert correct_repository_ref["ref_type"] == "repository_blob"
    assert correct_repository_ref["source_id"] == "repository"

    correct_web_ref = rr._enrich_evidence_ref(
        {"ref_type": "external_primary_source", "source_id": "web", "resource_identity": "https://example.invalid/y"},
        real_evidence_index=real_evidence_index,
        source_type_observer_ids=source_type_observer_ids,
    )
    assert correct_web_ref is not None
    assert correct_web_ref["ref_type"] == "external_primary_source"
    assert correct_web_ref["source_id"] == "web"


def test_active_observer_manifest_excludes_runtime_by_default() -> None:
    manifest = rr.active_observer_manifest(include_runtime=False)
    observer_ids = {spec.observer_id for spec in manifest}
    assert observer_ids == {"codebase-investigator", "web-researcher"}

    full_manifest = rr.active_observer_manifest(include_runtime=True)
    assert {spec.observer_id for spec in full_manifest} == {
        "codebase-investigator",
        "web-researcher",
        "retrospective-runtime-observer",
    }
