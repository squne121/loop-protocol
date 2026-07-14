"""#1516: weak path / ordinal AC signals must not create false collision routes."""

from __future__ import annotations

import hashlib
import json
import subprocess
import sys
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[4]
HELPER = REPO_ROOT / ".claude/skills/implement-issue/scripts/check_implementation_overlap.py"
DEFAULT_REPO = "squne121/loop-protocol"
CREATE_ISSUE_SCRIPTS = REPO_ROOT / ".claude/skills/create-issue/scripts"
sys.path.insert(0, str(CREATE_ISSUE_SCRIPTS))
from check_issue_overlap import (  # noqa: E402
    PathScopeKind,
    SOURCE_OK,
    classify_path_scope_kind,
    gh_search_candidates,
)


def _sha(body: str) -> str:
    return f"sha256:{hashlib.sha256(body.encode()).hexdigest()}"


def _body(*, outcome: str, scope: str, allowed_paths: list[str], ac: str = "AC1", schema: str = "") -> str:
    schema_line = f"\n{schema}" if schema else ""
    return (
        "## Machine-Readable Contract\n\n```yaml\ncontract_schema_version: v1\n"
        "issue_kind: implementation\nchange_kind: code\n```\n\n"
        f"## Outcome\n\n{outcome}{schema_line}\n\n## In Scope\n\n{scope}{schema_line}\n\n"
        f"## Acceptance Criteria\n\n- [ ] {ac}: behavior\n\n## Allowed Paths\n\n"
        + "\n".join(f"- {path}" for path in allowed_paths)
        + "\n"
    )


def _record(number: int, body: str) -> dict[str, object]:
    return {
        "number": number,
        "title": f"implementation {number}",
        "body": body,
        "body_sha256": _sha(body),
        "labels": [{"name": "phase/implementation"}],
        "updatedAt": "2026-07-14T00:00:00Z",
        "url": f"https://github.com/squne121/loop-protocol/issues/{number}",
        "state": "OPEN",
    }


def _run(tmp_path: Path, current_body: str, candidate_body: str) -> dict[str, object]:
    current = _record(1283, current_body)
    candidate = _record(1284, candidate_body)
    current_file = tmp_path / "current.json"
    candidates_file = tmp_path / "candidates.json"
    current_file.write_text(json.dumps(current), encoding="utf-8")
    candidates_file.write_text(json.dumps([candidate]), encoding="utf-8")
    result = subprocess.run(
        [sys.executable, str(HELPER), "--issue-number", "1283", "--dry-run",
         "--repo", DEFAULT_REPO, "--current-file", str(current_file),
         "--candidates-file", str(candidates_file)],
        capture_output=True,
        text=True,
        check=True,
    )
    return json.loads(result.stdout)


def test_exact_file_kind(tmp_path: Path) -> None:
    assert [classify_path_scope_kind(name) for name in ("README", "LICENSE", "Dockerfile", "Makefile")] == [PathScopeKind.EXACT] * 4
    current = _body(outcome="current", scope="current", allowed_paths=["README", "LICENSE", "Dockerfile", "Makefile"])
    payload = _run(tmp_path, current, _body(outcome="other", scope="other", allowed_paths=["README"]))
    assert payload["route"] == "proceed_with_collision_evidence"


def test_package_json_weak(tmp_path: Path) -> None:
    payload = _run(tmp_path, _body(outcome="alpha", scope="alpha", allowed_paths=["package.json"]), _body(outcome="beta", scope="beta", allowed_paths=["package.json"]))
    assert payload["route"] == "proceed_with_collision_evidence"
    assert "low_specificity_path_only" in payload["candidates"][0]["reasons"]


def test_package_json_strong(tmp_path: Path) -> None:
    payload = _run(tmp_path, _body(outcome="alpha", scope="same package script", allowed_paths=["package.json"], schema="BUILD_RESULT_V1"), _body(outcome="beta", scope="same package script", allowed_paths=["package.json"], schema="BUILD_RESULT_V1"))
    assert payload["route"] in {"human_review_required", "duplicate"}
    assert payload["candidates"][0]["structural_signals"]["has_structural_collision"] is True


def test_broad_prefix_weak(tmp_path: Path) -> None:
    payload = _run(tmp_path, _body(outcome="alpha", scope="alpha", allowed_paths=["tests/e2e/foo.spec.ts"]), _body(outcome="beta", scope="beta", allowed_paths=["tests/"]))
    assert payload["route"] == "proceed_with_collision_evidence"
    assert "broad_prefix_only" in payload["candidates"][0]["reasons"]


def test_broad_prefix_strong(tmp_path: Path) -> None:
    payload = _run(tmp_path, _body(outcome="alpha", scope="same test target", allowed_paths=["tests/e2e/foo.spec.ts"], schema="TEST_RESULT_V1"), _body(outcome="beta", scope="same test target", allowed_paths=["tests/"], schema="TEST_RESULT_V1"))
    assert payload["route"] == "human_review_required"


def test_ordinal_ac_id_only(tmp_path: Path) -> None:
    payload = _run(tmp_path, _body(outcome="alpha", scope="alpha", allowed_paths=["package.json"], ac="AC1"), _body(outcome="beta", scope="beta", allowed_paths=["package.json"], ac="AC1"))
    candidate = payload["candidates"][0]
    assert payload["route"] == "proceed_with_collision_evidence"
    assert candidate["structural_signals"]["has_structural_collision"] is False
    assert "ordinal_ac_id_only" in candidate["reasons"]


def test_ac_id_with_output_schema(tmp_path: Path) -> None:
    payload = _run(tmp_path, _body(outcome="alpha", scope="alpha", allowed_paths=["package.json"], ac="AC1", schema="OUTPUT_RESULT_V1"), _body(outcome="beta", scope="beta", allowed_paths=["package.json"], ac="AC1", schema="OUTPUT_RESULT_V1"))
    assert payload["route"] in {"human_review_required", "duplicate"}
    assert payload["candidates"][0]["structural_signals"]["has_structural_collision"] is True


def test_issue_1283_golden_fixture(tmp_path: Path) -> None:
    """Issue #1283 observed shape: weak shared package path and AC ordinal are non-blocking."""
    current = _body(outcome="source pagination", scope="collector", allowed_paths=["package.json", "scripts/collector.py"], ac="AC13")
    candidate = _body(outcome="unrelated package script", scope="unrelated", allowed_paths=["package.json"], ac="AC13")
    golden = {"current_body_sha256": _sha(current), "candidate_body_sha256": _sha(candidate)}
    assert all(value.startswith("sha256:") for value in golden.values())
    payload = _run(tmp_path, current, candidate)
    assert payload["route"] != "human_review_required"


def test_open_issue_source_uses_paginated_api_without_saturation(monkeypatch: object) -> None:
    """GIVEN two REST pages exceeding the former default limit
    WHEN the online source collects candidates
    THEN gh api --paginate is used and source status remains complete.
    """
    pages = [
        [{"number": number, "title": f"issue {number}", "body": "body", "labels": [],
          "state": "open", "html_url": f"https://example.test/{number}"}
         for number in range(1, 101)],
        [{"number": 101, "title": "issue 101", "body": "body", "labels": [],
          "state": "open", "html_url": "https://example.test/101"},
         {"number": 102, "title": "pull request", "body": "body", "labels": [],
          "state": "open", "pull_request": {}}],
    ]
    calls: list[list[str]] = []

    def fake_run(command: list[str], **_kwargs: object) -> subprocess.CompletedProcess[str]:
        calls.append(command)
        return subprocess.CompletedProcess(command, 0, stdout=json.dumps(pages))

    monkeypatch.setattr("check_issue_overlap.subprocess.run", fake_run)  # type: ignore[attr-defined]

    candidates, status = gh_search_candidates(DEFAULT_REPO, ("overlap",))

    assert calls == [[
        "gh", "api", "--paginate", "--slurp",
        "repos/squne121/loop-protocol/issues?state=open&per_page=100",
    ]]
    assert len(candidates) == 101
    assert candidates[-1].number == 101
    assert status.issue_search == SOURCE_OK
    assert status.issue_readback == SOURCE_OK
