from __future__ import annotations

import hashlib
import importlib.util
from pathlib import Path


_ROOT = Path(__file__).resolve().parents[4]
_PUBLISH = _ROOT / ".claude/skills/issue-refinement-loop/scripts/publish_termination_report.py"
_spec = importlib.util.spec_from_file_location("human_history_publish", _PUBLISH)
publisher = importlib.util.module_from_spec(_spec)
assert _spec and _spec.loader
_spec.loader.exec_module(publisher)


def _issue_identity() -> dict:
    return {
        "loop_kind": "impl-review-loop",
        "phase": "pre-PR-binding",
        "source_issue_number": 1908,
        "target_kind": "issue",
        "target_number": 1908,
        "route_or_termination_reason": "completed",
        "reviewed_ref": "a" * 64,
    }


def _render(identity: dict | None = None, **overrides):
    values = {
        "identity": identity or _issue_identity(),
        "result": "レビューを完了しました",
        "evidence_refs": ["https://github.com/squne121/loop-protocol/issues/1908"],
        "recommended_action": "次の実装工程へ進めてください",
        "recommended_reason": "契約と検証結果が一致しています",
        "impact_if_unaddressed": "作業判断が遅れる可能性があります",
    }
    values.update(overrides)
    return publisher.render_human_history_comment(**values)


def test_versioned_marker_jcs_identity_json_types_and_reason_enum_known_values():
    rendered, error = _render()
    assert error == ""
    assert rendered is not None
    identity = _issue_identity()
    expected_jcs = (
        b'{"loop_kind":"impl-review-loop","phase":"pre-PR-binding",'
        b'"reviewed_ref":"' + b"a" * 64 + b'","route_or_termination_reason":"completed",'
        b'"source_issue_number":1908,"target_kind":"issue","target_number":1908}'
    )
    assert publisher._human_history_jcs(identity) == expected_jcs
    digest = hashlib.sha256(expected_jcs).hexdigest()
    assert digest == "93e44b1d031bf70ce26588cd4ccd68306a13a8275b072d8d65a514564015658f"
    assert rendered["marker"] == f"<!-- loop-protocol/human-history:v1:sha256:{digest} -->"
    assert "\n" + rendered["marker"] + "\n" in rendered["body"]

    invalid_fields = (
        ("source_issue_number", "1908"),
        ("target_number", True),
        ("reviewed_ref", "sha256:" + "a" * 64),
    )
    for field, bad in invalid_fields:
        invalid = _issue_identity()
        invalid[field] = bad
        assert publisher._validate_human_history_identity(invalid)[0] is None
    for reason in publisher._HUMAN_HISTORY_REASONS:
        candidate = _issue_identity()
        candidate["route_or_termination_reason"] = reason
        valid, _ = publisher._validate_human_history_identity(candidate)
        assert (valid is not None) is (reason in {"completed", "needs_fix", "human_judgment"})


def test_stable_identity_excludes_rendered_content_and_time():
    first, error = _render()
    second, second_error = _render(
        result="検証結果を更新しました",
        recommended_action="人間が確認してください",
        recommended_reason="追加の確認が必要です",
        impact_if_unaddressed="確認漏れが残る可能性があります",
    )
    assert error == second_error == ""
    assert first and second
    assert first["marker"] == second["marker"]
    assert first["body"] != second["body"]
    assert "timestamp" not in publisher._HUMAN_HISTORY_FIELDS
    assert "random_run_id" not in publisher._HUMAN_HISTORY_FIELDS
    assert "rendered_body" not in publisher._HUMAN_HISTORY_FIELDS
