"""Issue #2811: append_implementation_scope_coverage() canonical-parser idempotency tests."""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[4]
EVIDENCE = ROOT / ".claude/skills/impl-review-loop/scripts/implementation_landed_evidence.py"
OPEN_PR = ROOT / ".claude/skills/open-pr/scripts/open_pr.py"
REPO = "squne121/loop-protocol"
ISSUE = 2811


def _load(path: Path, name: str):
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _issue_body() -> str:
    return """## Machine-Readable Contract
```yaml
goal_ref: marker goal
change_kind: code
```
## In Scope
- validate marker
## Acceptance Criteria
- [ ] AC1: marker validates
## Allowed Paths
- `.claude/a.py`
"""


def _valid_marker_body(evidence) -> str:
    marker = evidence.build_scope_coverage_marker(issue_number=ISSUE, issue_body=_issue_body(), pr_head_sha="a" * 40)
    return "## Summary\n\n本文の説明です。\n\n" + evidence.render_scope_coverage_marker(marker) + "\n"


def _malformed_marker_body() -> str:
    zeros = "0" * 64
    return (
        "## Summary\n\n本文の説明です。\n\n"
        "```yaml\n"
        "IMPLEMENTATION_SCOPE_COVERAGE_V1:\n"
        '  schema_version: "WRONG_SCHEMA"\n'
        f"  issue_number: {ISSUE}\n"
        f'  issue_body_sha256: "sha256:{zeros}"\n'
        f'  normalized_scope_manifest_sha256: "sha256:{zeros}"\n'
        '  pr_head_sha: "not-a-valid-sha"\n'
        "  scope_manifest: {}\n"
        "```\n"
    )


def _forbid_live_dependencies(monkeypatch, open_pr):
    """Make every live dependency of the producer path raise if it is ever called."""

    def _boom(name: str):
        def _raise(*_args, **_kwargs):
            raise AssertionError(f"{name} must not be called")

        return _raise

    monkeypatch.setattr(open_pr, "get_linked_issue_body", _boom("get_linked_issue_body"))
    monkeypatch.setattr(open_pr, "resolve_head_sha", _boom("resolve_head_sha"))
    module = open_pr._load_implementation_scope_evidence_module()
    monkeypatch.setattr(module, "build_scope_coverage_marker", _boom("build_scope_coverage_marker"))
    monkeypatch.setattr(module, "render_scope_coverage_marker", _boom("render_scope_coverage_marker"))
    monkeypatch.setattr(open_pr, "_load_implementation_scope_evidence_module", lambda: module)


def test_valid_marker_returns_unchanged_body_without_live_dependency(monkeypatch):
    """AC1: marker present + canonical-parser valid is returned byte-for-byte, with no live call."""
    open_pr = _load(OPEN_PR, "open_pr_marker_valid")
    evidence = _load(EVIDENCE, "evidence_marker_valid")
    body = _valid_marker_body(evidence)
    _, errors = evidence._parse_marker(body, issue_number=ISSUE)
    assert errors == []
    _forbid_live_dependencies(monkeypatch, open_pr)

    result = open_pr.append_implementation_scope_coverage(body, repo=REPO, linked_issue=ISSUE)

    assert result == body


def test_malformed_marker_passthrough_no_live_fetch_no_regeneration(monkeypatch):
    """AC2: marker present + invalid is passed through unchanged; no live call, no error signal."""
    open_pr = _load(OPEN_PR, "open_pr_marker_malformed")
    evidence = _load(EVIDENCE, "evidence_marker_malformed")
    body = _malformed_marker_body()
    marker, errors = evidence._parse_marker(body, issue_number=ISSUE)
    assert marker is None
    assert errors and errors != ["scope_coverage_marker_missing"]
    _forbid_live_dependencies(monkeypatch, open_pr)

    # (c) no exception; a None return would be the E_IMPLEMENTATION_SCOPE_COVERAGE_UNAVAILABLE signal.
    result = open_pr.append_implementation_scope_coverage(body, repo=REPO, linked_issue=ISSUE)

    assert result is not None
    assert result == body


def test_malformed_marker_each_reject_class_passthrough(monkeypatch):
    """AC2 (per reject class): each non-missing `_parse_marker()` error class is passthrough."""
    open_pr = _load(OPEN_PR, "open_pr_marker_classes")
    evidence = _load(EVIDENCE, "evidence_marker_classes")
    base = _valid_marker_body(evidence)
    zeros = "0" * 64
    cases = {
        "scope_coverage_schema_version_invalid": base.replace(
            'schema_version: "IMPLEMENTATION_SCOPE_COVERAGE_V1"', 'schema_version: "OTHER"'
        ),
        "scope_coverage_issue_identity_mismatch": base.replace(f"issue_number: {ISSUE}", "issue_number: 1"),
        "scope_coverage_pr_head_invalid": base.replace("a" * 40, "zz"),
    }
    digest_line = next(line for line in base.splitlines() if "issue_body_sha256" in line)
    cases["scope_coverage_issue_body_digest_invalid"] = base.replace(digest_line, '  issue_body_sha256: "sha256:xyz"')
    manifest_line = next(line for line in base.splitlines() if "normalized_scope_manifest_sha256" in line)
    cases["scope_coverage_manifest_digest_mismatch"] = base.replace(
        manifest_line, f'  normalized_scope_manifest_sha256: "sha256:{zeros}"'
    )
    cases["scope_coverage_marker_ambiguous_or_invalid"] = base + base
    _forbid_live_dependencies(monkeypatch, open_pr)

    for expected_error, body in cases.items():
        _, errors = evidence._parse_marker(body, issue_number=ISSUE)
        assert expected_error in errors, (expected_error, errors)
        assert open_pr.append_implementation_scope_coverage(body, repo=REPO, linked_issue=ISSUE) == body


def test_marker_absent_generation_path_preserved(monkeypatch):
    """AC3: marker absent still goes through the existing build/render producer path."""
    open_pr = _load(OPEN_PR, "open_pr_marker_absent")
    evidence = _load(EVIDENCE, "evidence_marker_absent")
    calls: list[str] = []

    def _issue(_repo, _issue_number):
        calls.append("get_linked_issue_body")
        return _issue_body()

    def _head():
        calls.append("resolve_head_sha")
        return "b" * 40

    monkeypatch.setattr(open_pr, "get_linked_issue_body", _issue)
    monkeypatch.setattr(open_pr, "resolve_head_sha", _head)
    body = "## Summary\n\n本文の説明です。\n"

    result = open_pr.append_implementation_scope_coverage(body, repo=REPO, linked_issue=ISSUE)

    assert calls == ["get_linked_issue_body", "resolve_head_sha"]
    assert result is not None
    assert result.startswith(body.rstrip())
    coverage = evidence.coverage_from_pr_body(pr_body=result, issue_number=ISSUE, live_issue_body=_issue_body())
    assert coverage["exact_coverage"] is True
    assert coverage["marker"]["pr_head_sha"] == "b" * 40

    # Retrying on the produced body is idempotent (no second snapshot, no live call).
    monkeypatch.setattr(open_pr, "get_linked_issue_body", lambda *_a, **_k: (_ for _ in ()).throw(AssertionError()))
    assert open_pr.append_implementation_scope_coverage(result, repo=REPO, linked_issue=ISSUE) == result


def test_marker_absent_live_dependency_failure_still_signals_unavailable(monkeypatch):
    """AC3: E_IMPLEMENTATION_SCOPE_COVERAGE_UNAVAILABLE semantics (None return) stay for absent markers."""
    open_pr = _load(OPEN_PR, "open_pr_marker_absent_unavailable")
    monkeypatch.setattr(open_pr, "get_linked_issue_body", lambda _repo, _issue_number: None)
    monkeypatch.setattr(open_pr, "resolve_head_sha", lambda: "c" * 40)

    assert open_pr.append_implementation_scope_coverage("## Summary\n\n本文\n", repo=REPO, linked_issue=ISSUE) is None


def _validate_after_append(result: str):
    """Real create path: `append_implementation_scope_coverage()` output goes to the validator."""
    name = "validate_pr_body_marker_create_path"
    spec = importlib.util.spec_from_file_location(name, ROOT / ".claude/skills/open-pr/scripts/validate_pr_body.py")
    assert spec and spec.loader
    validator = importlib.util.module_from_spec(spec)
    sys.modules[name] = validator  # dataclasses need the module registered
    try:
        spec.loader.exec_module(validator)
        return validator._validate_lp059(result, ISSUE)
    finally:
        sys.modules.pop(name, None)


def test_quoted_key_and_unparsable_fence_fail_closed_on_create_path(monkeypatch):
    """Issue #2811 P1: quoted-key invalid / unparsable fence are neither repaired nor regenerated,
    and the validator (LP059) that runs next fails closed."""
    open_pr = _load(OPEN_PR, "open_pr_marker_create_path")
    evidence = _load(EVIDENCE, "evidence_marker_create_path")
    quoted = _malformed_marker_body().replace(
        "IMPLEMENTATION_SCOPE_COVERAGE_V1:", "'IMPLEMENTATION_SCOPE_COVERAGE_V1':", 1
    )
    assert "IMPLEMENTATION_SCOPE_COVERAGE_V1:" not in quoted
    unparsable = (
        "## Summary\n\n本文の説明です。\n\n```yaml\nIMPLEMENTATION_SCOPE_COVERAGE_V1:\n  schema_version: [\n```\n"
    )
    _forbid_live_dependencies(monkeypatch, open_pr)

    expected_error = {
        "quoted": None,  # any non-missing reject class
        "unparsable": "scope_coverage_marker_ambiguous_or_invalid",
    }
    for label, body in (("quoted", quoted), ("unparsable", unparsable)):
        _, errors = evidence._parse_marker(body, issue_number=ISSUE)
        assert errors and errors != ["scope_coverage_marker_missing"], (label, errors)
        if expected_error[label]:
            assert errors == [expected_error[label]]

        # Producer path: body unchanged (no valid marker appended behind the malformed fence, no live call).
        result = open_pr.append_implementation_scope_coverage(body, repo=REPO, linked_issue=ISSUE)
        assert result == body, label

        # Validator: LP059 fails closed on the exact output of the producer path.
        lp059 = _validate_after_append(result)
        assert len(lp059) == 1, label
        assert lp059[0].rule_id == "LP059"
