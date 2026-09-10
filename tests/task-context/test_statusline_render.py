"""Issue #2564 AC9 -- statusLine renderer pure-function tests (``render``
never touches the DB itself -- it only formats an already-fetched
projection dict)."""

from __future__ import annotations

import statusline


def test_given_degraded_projection_when_rendered_then_shows_degraded_reason():
    text = statusline.render({"degraded": True, "degraded_reason": "no_state_db"})
    assert "degraded" in text
    assert "no_state_db" in text


def test_given_unbound_projection_when_rendered_then_shows_unbound():
    text = statusline.render({"degraded": False, "task": None})
    assert "unbound" in text


def test_given_bound_task_with_ref_when_rendered_then_shows_osc8_hyperlink_and_activity():
    data = {
        "degraded": False,
        "task": {"id": "task_1", "title": "My Task"},
        "activity": {"kind": "impl"},
        "binding": {"runtime_health": "ACTIVE"},
        "task_refs": [{"repo": "owner/repo", "ref_kind": "issue", "ref_number": 42}],
        "attention": None,
    }
    text = statusline.render(data)
    assert "owner/repo#42" in text
    assert "https://github.com/owner/repo/issues/42" in text
    assert "activity=impl" in text
    assert "health=" not in text  # ACTIVE is the default/healthy state -- not surfaced


def test_given_pr_ref_when_rendered_then_pull_url_used():
    data = {
        "degraded": False,
        "task": {"id": "task_1", "title": "My Task"},
        "activity": {"kind": "impl"},
        "binding": None,
        "task_refs": [{"repo": "owner/repo", "ref_kind": "pr", "ref_number": 9}],
        "attention": None,
    }
    text = statusline.render(data)
    assert "https://github.com/owner/repo/pull/9" in text


def test_given_non_active_health_when_rendered_then_health_shown():
    data = {
        "degraded": False,
        "task": {"id": "task_1", "title": "My Task"},
        "activity": None,
        "binding": {"runtime_health": "SUSPENDED"},
        "task_refs": [],
        "attention": None,
    }
    text = statusline.render(data)
    assert "health=SUSPENDED" in text


def test_given_attention_present_when_rendered_then_surfaced():
    data = {
        "degraded": False,
        "task": {"id": "task_1", "title": "My Task"},
        "activity": {"kind": "impl"},
        "binding": None,
        "task_refs": [],
        "attention": "needs human review",
    }
    text = statusline.render(data)
    assert "needs human review" in text
