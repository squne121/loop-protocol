"""Regression coverage for #1321 termination-report follow-up materialization."""

from __future__ import annotations

import re
import sys
from pathlib import Path

import yaml


_SCRIPTS = Path(__file__).resolve().parent.parent / "scripts"
sys.path.insert(0, str(_SCRIPTS))

import publish_termination_report as publisher  # noqa: E402
import render_termination_report as renderer  # noqa: E402


_MARKER_PATTERN = re.compile(
    r"^<!-- FOLLOW_UP_MATERIALIZATION_RESULT_V1 -->\n"
    r"(`{3,}|~{3,})yaml\n"
    r"(.*?)\n"
    r"\1\s*$",
    re.DOTALL | re.MULTILINE,
)


def _render(reason: str) -> dict:
    data: dict[str, object] = {
        "termination_reason": reason,
        "issue_number": 1792,
    }
    if reason == "human_escalation":
        data["termination_cause"] = "human_judgment_required"
    return renderer.render(data)


def _follow_up_result(body: str) -> dict:
    matches = list(_MARKER_PATTERN.finditer(body))
    assert len(matches) == 1, "FOLLOW_UP_MATERIALIZATION_RESULT_V1 must appear exactly once"
    return yaml.safe_load(matches[0].group(2))


def _assert_canonical_empty_result(result: dict) -> None:
    assert result["publishable"] is True
    body = result["body"]
    assert isinstance(body, str)
    assert _follow_up_result(body) == {
        "FOLLOW_UP_MATERIALIZATION_RESULT_V1": {
            "schema_version": 1,
            "materialized_by": "issue-refinement-loop",
            "follow_up_issues": [],
            "note_only_observations": [],
        }
    }


def test_approved_includes_follow_up_materialization_result() -> None:
    _assert_canonical_empty_result(_render("approved"))


def test_human_escalation_includes_follow_up_materialization_result() -> None:
    _assert_canonical_empty_result(_render("human_escalation"))


def test_superseded_by_decision_includes_follow_up_materialization_result() -> None:
    _assert_canonical_empty_result(_render("superseded_by_decision"))


def test_empty_follow_up_arrays_are_not_omitted(monkeypatch) -> None:
    posted_bodies: list[str] = []

    def capture_post(*, issue_number: int, body: str, repo: str) -> int:
        assert issue_number == 1792
        assert repo == "squne121/loop-protocol"
        posted_bodies.append(body)
        return 0

    monkeypatch.setattr(publisher, "_post_github_comment", capture_post)

    exit_code = publisher.publish(
        issue_number=1792,
        input_data={"termination_reason": "approved", "issue_number": 1792},
        repo="squne121/loop-protocol",
    )

    assert exit_code == 0
    assert len(posted_bodies) == 1
    assert _follow_up_result(posted_bodies[0])["FOLLOW_UP_MATERIALIZATION_RESULT_V1"] == {
        "schema_version": 1,
        "materialized_by": "issue-refinement-loop",
        "follow_up_issues": [],
        "note_only_observations": [],
    }
