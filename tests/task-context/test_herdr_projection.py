"""Issue #2564 AC10, AC14 -- Herdr projection consumer.

``resolve_current_tab_id``/``project_to_herdr`` never talk to a real
``herdr`` server in tests -- a small fake ``herdr`` executable is placed on
``PATH`` (via the ``herdr_bin`` argument) so this suite never mutates a real
human-operated Herdr session."""

from __future__ import annotations

import json
import stat
import textwrap

import pytest

import herdr_projection


@pytest.fixture
def fake_herdr_ok(tmp_path):
    """A fake `herdr` that answers `pane get <id>` with a fixed tab_id and
    accepts (no-ops, exit 0) `tab rename` / `pane report-metadata`, logging
    every invocation's argv to ``tmp_path/calls.jsonl`` for assertions."""
    calls_log = tmp_path / "calls.jsonl"
    script = tmp_path / "herdr-ok"
    script.write_text(
        textwrap.dedent(
            f"""\
            #!/usr/bin/env python3
            import json, sys
            args = sys.argv[1:]
            with open({str(calls_log)!r}, "a") as f:
                f.write(json.dumps(args) + "\\n")
            if args[:2] == ["pane", "get"]:
                print(json.dumps({{"result": {{"pane": {{"tab_id": "live-tab-42"}}}}}}))
                sys.exit(0)
            sys.exit(0)
            """
        )
    )
    script.chmod(script.stat().st_mode | stat.S_IEXEC)
    return str(script), calls_log


@pytest.fixture
def fake_herdr_pane_not_found(tmp_path):
    script = tmp_path / "herdr-missing"
    script.write_text("#!/usr/bin/env python3\nimport sys\nsys.exit(1)\n")
    script.chmod(script.stat().st_mode | stat.S_IEXEC)
    return str(script)


@pytest.fixture
def fake_herdr_rename_fails(tmp_path):
    """`pane get` succeeds, but `tab rename` itself exits non-zero -- AC10:
    the required Tab-label projection failed, so the caller must not ack."""
    script = tmp_path / "herdr-rename-fails"
    script.write_text(
        textwrap.dedent(
            """\
            #!/usr/bin/env python3
            import json, sys
            args = sys.argv[1:]
            if args[:2] == ["pane", "get"]:
                print(json.dumps({"result": {"pane": {"tab_id": "live-tab-42"}}}))
                sys.exit(0)
            if args[:2] == ["tab", "rename"]:
                sys.exit(1)
            sys.exit(0)
            """
        )
    )
    script.chmod(script.stat().st_mode | stat.S_IEXEC)
    return str(script)


@pytest.fixture
def fake_herdr_metadata_fails(tmp_path):
    """`tab rename` succeeds (required UX surface), but
    `pane report-metadata` (best-effort custom metadata) exits non-zero --
    must still be acked (AC10 only gates on the Tab label)."""
    script = tmp_path / "herdr-metadata-fails"
    script.write_text(
        textwrap.dedent(
            """\
            #!/usr/bin/env python3
            import json, sys
            args = sys.argv[1:]
            if args[:2] == ["pane", "get"]:
                print(json.dumps({"result": {"pane": {"tab_id": "live-tab-42"}}}))
                sys.exit(0)
            if args[:2] == ["tab", "rename"]:
                sys.exit(0)
            if args[:2] == ["pane", "report-metadata"]:
                sys.exit(1)
            sys.exit(0)
            """
        )
    )
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


def test_given_build_pane_tokens_when_called_then_includes_task_activity_health():
    """fix_delta 1: task/activity/health are free-form custom ``--token``
    metadata, not fixed-vocabulary ``--state-label`` values."""
    tokens = herdr_projection.build_pane_tokens(
        {"id": "task_1", "title": "My Task"}, {"kind": "impl"}, {"runtime_health": "ACTIVE"}
    )
    assert tokens["task"] == "My Task"
    assert tokens["activity"] == "impl"
    assert tokens["health"] == "ACTIVE"


@pytest.mark.parametrize(
    "runtime_health,expected_status",
    [
        ("ACTIVE", "working"),
        ("RESTORING", "working"),
        ("SUSPENDED", "idle"),
        ("RESTORE_BLOCKED", "blocked"),
        ("DETACHED", "unknown"),
    ],
)
def test_given_runtime_health_when_building_state_label_then_maps_to_fixed_vocabulary(
    runtime_health, expected_status
):
    """fix_delta 1: ``--state-label`` only ever carries the fixed five-value
    STATUS vocabulary the real Herdr CLI accepts."""
    status, text = herdr_projection.build_pane_state_label({"runtime_health": runtime_health})
    assert status == expected_status
    assert status in herdr_projection.HERDR_STATE_LABEL_STATUSES
    assert text == runtime_health.lower()


def test_given_no_binding_when_building_state_label_then_none():
    assert herdr_projection.build_pane_state_label(None) is None


def test_given_live_pane_when_resolving_current_tab_then_returns_tab_id(fake_herdr_ok):
    herdr_bin, _ = fake_herdr_ok
    tab_id = herdr_projection.resolve_current_tab_id("wV:p3", herdr_bin=herdr_bin)
    assert tab_id == "live-tab-42"


def test_given_pane_lookup_fails_when_resolving_current_tab_then_none(fake_herdr_pane_not_found):
    tab_id = herdr_projection.resolve_current_tab_id("wV:p3", herdr_bin=fake_herdr_pane_not_found)
    assert tab_id is None


def test_given_missing_herdr_binary_when_resolving_current_tab_then_none_no_crash():
    tab_id = herdr_projection.resolve_current_tab_id("wV:p3", herdr_bin="/nonexistent/herdr-binary")
    assert tab_id is None


def test_given_live_pane_when_projecting_then_success_true(fake_herdr_ok):
    herdr_bin, _ = fake_herdr_ok
    ok = herdr_projection.project_to_herdr(
        "wV:p3", "#1 · impl", {"task": "x"}, revision=3, herdr_bin=herdr_bin
    )
    assert ok is True


def test_given_unresolvable_pane_when_projecting_then_success_false_never_raises(fake_herdr_pane_not_found):
    """AC10: a failed projection must never raise/crash the caller -- it
    just reports failure so the caller does not ack the outbox marker."""
    ok = herdr_projection.project_to_herdr(
        "wV:p3", "#1 · impl", {}, revision=1, herdr_bin=fake_herdr_pane_not_found
    )
    assert ok is False


def test_given_tab_rename_fails_when_projecting_then_success_false_not_acked(fake_herdr_rename_fails):
    """fix_delta 1: ``herdr tab rename`` (the required UX surface) exiting
    non-zero must NOT be acked -- AC10's "never lose a projection update on
    failure"."""
    ok = herdr_projection.project_to_herdr(
        "wV:p3", "#1 · impl", {"task": "x"}, revision=1, herdr_bin=fake_herdr_rename_fails
    )
    assert ok is False


def test_given_pane_metadata_fails_when_projecting_then_still_acked(fake_herdr_metadata_fails):
    """fix_delta 1: ``herdr pane report-metadata`` (best-effort custom
    metadata) failing alone must NOT prevent the ack -- only the Tab label
    is the required UX surface."""
    ok = herdr_projection.project_to_herdr(
        "wV:p3", "#1 · impl", {"task": "x"}, revision=1, herdr_bin=fake_herdr_metadata_fails
    )
    assert ok is True


def test_given_custom_tokens_when_projecting_then_sent_as_token_not_state_label(fake_herdr_ok):
    """fix_delta 1: ``task``/``activity``/``health`` custom metadata must be
    sent via ``--token NAME=VALUE``, never ``--state-label`` (which only
    accepts the fixed five-value STATUS vocabulary)."""
    herdr_bin, calls_log = fake_herdr_ok
    ok = herdr_projection.project_to_herdr(
        "wV:p3",
        "#1 · impl",
        {"task": "My Task", "activity": "impl"},
        revision=7,
        state_label=("working", "active"),
        herdr_bin=herdr_bin,
    )
    assert ok is True
    calls = [json.loads(line) for line in calls_log.read_text().splitlines() if line.strip()]
    metadata_call = next(c for c in calls if c[:2] == ["pane", "report-metadata"])
    assert "--token" in metadata_call
    assert "task=My Task" in metadata_call
    assert "activity=impl" in metadata_call
    assert "--state-label" in metadata_call
    assert "working=active" in metadata_call
    # never a `task=`/`activity=` state-label (outside the fixed vocabulary)
    state_label_idx = metadata_call.index("--state-label")
    assert metadata_call[state_label_idx + 1] == "working=active"


def test_given_out_of_vocabulary_status_when_projecting_then_state_label_omitted(fake_herdr_ok):
    """A ``state_label`` whose status falls outside the fixed vocabulary must
    never be forwarded as ``--state-label`` (herdr_projection.py never emits
    an out-of-vocabulary STATUS)."""
    herdr_bin, calls_log = fake_herdr_ok
    ok = herdr_projection.project_to_herdr(
        "wV:p3",
        "#1 · impl",
        {},
        revision=1,
        state_label=("not_a_real_status", "whatever"),
        herdr_bin=herdr_bin,
    )
    assert ok is True
    calls = [json.loads(line) for line in calls_log.read_text().splitlines() if line.strip()]
    metadata_call = next(c for c in calls if c[:2] == ["pane", "report-metadata"])
    assert "--state-label" not in metadata_call
