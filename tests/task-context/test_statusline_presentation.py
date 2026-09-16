"""Issue #2634 -- Herdr Tab label / statusLine presentation contract.

Covers:
  * OSC8 hyperlink sequence shape (open sequence / label / close sequence),
    verified individually.
  * AC1: ``build_tab_label()`` bound/unbound shortened forms.
  * AC2: ``render()`` bound ref label has no repo name and no leading
    ``"[Task Context] "`` prefix.
  * AC3: ``render()`` unbound / degraded and ``main()`` no-session /
    query-failure presentation.
  * AC4: ``render()`` activity kind -> human-readable label via
    ``_ACTIVITY_LABELS``, with raw-value fallback for unknown kinds.

``render()`` is a pure function (Issue #2564 AC9 precedent) -- ``main()`` is
exercised here by monkeypatching the one external dependency it has
(``ctl_client.call_query_current_by_session``, a subprocess boundary) and
stdin, per ``tests/CLAUDE.md``'s "外部依存... のみモック化可".
"""

from __future__ import annotations

import io

import herdr_projection
import statusline


# --- OSC8 hyperlink sequence shape (open / label / close individually) -----


def test_given_url_and_label_when_osc8_link_built_then_open_sequence_present():
    text = statusline._osc8_link("https://github.com/o/r/issues/1", "#1")
    assert text.startswith("\033]8;;https://github.com/o/r/issues/1\033\\")


def test_given_url_and_label_when_osc8_link_built_then_label_present_between_sequences():
    text = statusline._osc8_link("https://github.com/o/r/issues/1", "#1")
    # strip the leading open sequence and trailing close sequence, leaving
    # only the label in between.
    open_seq = "\033]8;;https://github.com/o/r/issues/1\033\\"
    close_seq = "\033]8;;\033\\"
    assert text[len(open_seq) : -len(close_seq)] == "#1"


def test_given_url_and_label_when_osc8_link_built_then_close_sequence_present():
    text = statusline._osc8_link("https://github.com/o/r/issues/1", "#1")
    assert text.endswith("\033]8;;\033\\")


def test_given_bound_ref_when_rendered_then_full_osc8_sequence_wraps_number_label():
    data = {
        "degraded": False,
        "task": {"id": "task_1", "title": "My Task"},
        "activity": None,
        "binding": None,
        "task_refs": [{"repo": "owner/repo", "ref_kind": "issue", "ref_number": 42}],
        "attention": None,
    }
    text = statusline.render(data)
    expected = (
        "\033]8;;https://github.com/owner/repo/issues/42\033\\" "#42" "\033]8;;\033\\"
    )
    assert expected in text


# --- AC1: build_tab_label() ------------------------------------------------


def test_given_bound_task_with_refs_when_tab_label_built_then_ident_only():
    label = herdr_projection.build_tab_label(
        {"id": "task_1", "title": "x"},
        {"kind": "impl"},
        [{"repo": "o/r", "ref_number": 2625, "ref_kind": "issue"}],
    )
    assert label == "#2625"


def test_given_bound_task_no_refs_when_tab_label_built_then_adhoc():
    label = herdr_projection.build_tab_label({"id": "task_1", "title": "x"}, None, [])
    assert label == "adhoc"


def test_given_unbound_task_when_tab_label_built_then_unbound():
    label = herdr_projection.build_tab_label(None, {"kind": "impl"}, [])
    assert label == "Unbound"


# --- AC2: render() ref label / prefix --------------------------------------


def test_given_bound_ref_when_rendered_then_label_excludes_repo_name():
    data = {
        "degraded": False,
        "task": {"id": "task_1", "title": "My Task"},
        "activity": None,
        "binding": None,
        "task_refs": [{"repo": "owner/repo", "ref_kind": "issue", "ref_number": 2625}],
        "attention": None,
    }
    text = statusline.render(data)
    assert "#2625" in text
    assert "owner/repo#2625" not in text
    assert "repo#" not in text


def test_given_any_projection_when_rendered_then_no_task_context_prefix():
    bound = statusline.render(
        {
            "degraded": False,
            "task": {"id": "task_1", "title": "My Task"},
            "activity": None,
            "binding": None,
            "task_refs": [],
            "attention": None,
        }
    )
    unbound = statusline.render({"degraded": False, "task": None})
    degraded = statusline.render({"degraded": True, "degraded_reason": "no_state_db"})
    for text in (bound, unbound, degraded):
        assert not text.startswith("[Task Context")
        assert "[Task Context]" not in text


# --- AC3: render() unbound/degraded + main() no-session/query-failure ------


def test_given_task_none_when_rendered_then_unbound():
    assert statusline.render({"degraded": False, "task": None}) == "Unbound"


def test_given_degraded_no_binding_for_session_when_rendered_then_unbound():
    text = statusline.render({"degraded": True, "degraded_reason": "no_binding_for_session"})
    assert text == "Unbound"


def test_given_degraded_query_failed_when_rendered_then_degraded():
    text = statusline.render({"degraded": True, "degraded_reason": "query_failed"})
    assert text == "Degraded"


def test_given_degraded_reason_missing_when_rendered_then_degraded_not_unbound():
    """A ``degraded: True`` payload with no (or unrecognized) reason must
    never default to the friendlier "Unbound" -- fail toward the more
    conservative "Degraded" presentation."""
    text = statusline.render({"degraded": True})
    assert text == "Degraded"


def test_given_degraded_reason_kept_internally_when_rendered_then_not_leaked_in_text():
    """AC3: diagnostic reason stays out of the presentation string but is
    still present, unmodified, on the input dict itself (never deleted)."""
    data = {"degraded": True, "degraded_reason": "no_state_db"}
    text = statusline.render(data)
    assert "no_state_db" not in text
    assert data["degraded_reason"] == "no_state_db"


def test_given_no_session_id_when_main_invoked_then_prints_unbound(monkeypatch, capsys):
    monkeypatch.setattr(statusline.sys, "stdin", io.StringIO("{}"))

    def _fail_if_called(*_args, **_kwargs):
        raise AssertionError("ctl_client must not be called when session_id is missing")

    monkeypatch.setattr(statusline.ctl_client, "call_query_current_by_session", _fail_if_called)

    exit_code = statusline.main([])
    assert exit_code == 0
    assert capsys.readouterr().out.strip() == "Unbound"


def test_given_query_transport_failure_when_main_invoked_then_prints_degraded(monkeypatch, capsys):
    monkeypatch.setattr(statusline.sys, "stdin", io.StringIO('{"session_id": "sess-1"}'))
    monkeypatch.setattr(
        statusline.ctl_client, "call_query_current_by_session", lambda *_a, **_kw: None
    )

    exit_code = statusline.main([])
    assert exit_code == 0
    assert capsys.readouterr().out.strip() == "Degraded"


def test_given_query_status_not_ok_when_main_invoked_then_prints_degraded(monkeypatch, capsys):
    monkeypatch.setattr(statusline.sys, "stdin", io.StringIO('{"session_id": "sess-1"}'))
    monkeypatch.setattr(
        statusline.ctl_client,
        "call_query_current_by_session",
        lambda *_a, **_kw: {"status": "error"},
    )

    exit_code = statusline.main([])
    assert exit_code == 0
    assert capsys.readouterr().out.strip() == "Degraded"


def test_given_query_ok_bound_when_main_invoked_then_prints_rendered_line(monkeypatch, capsys):
    monkeypatch.setattr(statusline.sys, "stdin", io.StringIO('{"session_id": "sess-1"}'))
    monkeypatch.setattr(
        statusline.ctl_client,
        "call_query_current_by_session",
        lambda *_a, **_kw: {
            "status": "ok",
            "data": {
                "degraded": False,
                "task": {"id": "task_1", "title": "My Task"},
                "activity": {"kind": "impl"},
                "binding": None,
                "task_refs": [],
                "attention": None,
            },
        },
    )

    exit_code = statusline.main([])
    assert exit_code == 0
    out = capsys.readouterr().out.strip()
    assert out == "My Task · impl"


# --- AC4: activity kind -> human-readable label + unknown fallback ---------


def test_given_known_run_kind_vocabulary_when_activity_rendered_then_mapped_label():
    data = {
        "degraded": False,
        "task": {"id": "task_1", "title": "My Task"},
        "activity": {"kind": "native_operator"},
        "binding": None,
        "task_refs": [],
        "attention": None,
    }
    text = statusline.render(data)
    assert "native" in text
    assert "native_operator" not in text
    assert "activity=" not in text  # PR #2640 review fix_delta: no `activity=` prefix


def test_given_known_free_form_kind_when_activity_rendered_then_mapped_label():
    data = {
        "degraded": False,
        "task": {"id": "task_1", "title": "My Task"},
        "activity": {"kind": "refine"},
        "binding": None,
        "task_refs": [],
        "attention": None,
    }
    text = statusline.render(data)
    assert "refine" in text
    assert "activity=" not in text  # PR #2640 review fix_delta: no `activity=` prefix


def test_given_unknown_activity_kind_when_rendered_then_falls_back_to_raw_value_no_raise():
    """AC4: unmapped kinds never raise and never silently drop the activity
    information -- they fall back to the raw kind value, appended bare (no
    ``activity=`` prefix, PR #2640 review fix_delta)."""
    data = {
        "degraded": False,
        "task": {"id": "task_1", "title": "My Task"},
        "activity": {"kind": "some_future_kind"},
        "binding": None,
        "task_refs": [],
        "attention": None,
    }
    text = statusline.render(data)
    assert "some_future_kind" in text
    assert "activity=" not in text  # PR #2640 review fix_delta: no `activity=` prefix


def test_given_activity_labels_table_when_looked_up_directly_then_matches_render_output():
    assert statusline._ACTIVITY_LABELS["native_operator"] != "native_operator"
    assert statusline._activity_label("totally_unknown_kind") == "totally_unknown_kind"
