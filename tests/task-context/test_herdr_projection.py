"""Issue #2564 AC10, AC14 -- Herdr projection consumer.

``resolve_current_tab_id``/``project_to_herdr`` never talk to a real
``herdr`` server in tests -- a small fake ``herdr`` executable is placed on
``PATH`` (via the ``herdr_bin`` argument) so this suite never mutates a real
human-operated Herdr session."""

from __future__ import annotations

import stat
import textwrap

import pytest

import herdr_projection


@pytest.fixture
def fake_herdr_ok(tmp_path):
    """A fake `herdr` that answers `pane get <id>` with a fixed tab_id and
    accepts (no-ops) `tab rename` / `pane report-metadata`."""
    script = tmp_path / "herdr-ok"
    script.write_text(
        textwrap.dedent(
            """\
            #!/usr/bin/env python3
            import json, sys
            args = sys.argv[1:]
            if args[:2] == ["pane", "get"]:
                print(json.dumps({"result": {"pane": {"tab_id": "live-tab-42"}}}))
                sys.exit(0)
            sys.exit(0)
            """
        )
    )
    script.chmod(script.stat().st_mode | stat.S_IEXEC)
    return str(script)


@pytest.fixture
def fake_herdr_pane_not_found(tmp_path):
    script = tmp_path / "herdr-missing"
    script.write_text("#!/usr/bin/env python3\nimport sys\nsys.exit(1)\n")
    script.chmod(script.stat().st_mode | stat.S_IEXEC)
    return str(script)


def test_given_build_tab_label_with_refs_when_called_then_ident_and_activity_kind():
    label = herdr_projection.build_tab_label(
        {"id": "task_1", "title": "x"}, {"kind": "impl"}, [{"repo": "o/r", "ref_number": 42, "ref_kind": "issue"}]
    )
    assert label == "#42 · impl"


def test_given_build_tab_label_no_refs_when_called_then_adhoc_label():
    label = herdr_projection.build_tab_label({"id": "task_1", "title": "x"}, {"kind": "refine"}, [])
    assert label == "adhoc · refine"


def test_given_build_tab_label_no_task_when_called_then_unbound_label():
    label = herdr_projection.build_tab_label(None, None, [])
    assert label == "task-ctx: unbound"


def test_given_build_pane_state_labels_when_called_then_includes_task_activity_health():
    labels = herdr_projection.build_pane_state_labels(
        {"id": "task_1", "title": "My Task"}, {"kind": "impl"}, {"runtime_health": "ACTIVE"}
    )
    assert labels["task"] == "My Task"
    assert labels["activity"] == "impl"
    assert labels["health"] == "ACTIVE"


def test_given_live_pane_when_resolving_current_tab_then_returns_tab_id(fake_herdr_ok):
    tab_id = herdr_projection.resolve_current_tab_id("wV:p3", herdr_bin=fake_herdr_ok)
    assert tab_id == "live-tab-42"


def test_given_pane_lookup_fails_when_resolving_current_tab_then_none(fake_herdr_pane_not_found):
    tab_id = herdr_projection.resolve_current_tab_id("wV:p3", herdr_bin=fake_herdr_pane_not_found)
    assert tab_id is None


def test_given_missing_herdr_binary_when_resolving_current_tab_then_none_no_crash():
    tab_id = herdr_projection.resolve_current_tab_id("wV:p3", herdr_bin="/nonexistent/herdr-binary")
    assert tab_id is None


def test_given_live_pane_when_projecting_then_success_true(fake_herdr_ok):
    ok = herdr_projection.project_to_herdr(
        "wV:p3", "#1 · impl", {"task": "x"}, revision=3, herdr_bin=fake_herdr_ok
    )
    assert ok is True


def test_given_unresolvable_pane_when_projecting_then_success_false_never_raises(fake_herdr_pane_not_found):
    """AC10: a failed projection must never raise/crash the caller -- it
    just reports failure so the caller does not ack the outbox marker."""
    ok = herdr_projection.project_to_herdr(
        "wV:p3", "#1 · impl", {}, revision=1, herdr_bin=fake_herdr_pane_not_found
    )
    assert ok is False
