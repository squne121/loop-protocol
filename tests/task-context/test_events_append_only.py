"""AC7: events is append-only and must never store raw prompt/transcript/
full command/message body content."""

from __future__ import annotations

import sqlite3

import pytest

import task_context_errors as errors
import task_context_service as service


def test_given_allowed_metadata_when_event_appended_then_it_is_stored(conn):
    task = service.create_task(conn)
    event = service.append_event(
        conn, event_type="hook:UserPromptSubmit", task_id=task["id"], metadata={"status": "ok", "count": 1}
    )
    assert event["event_type"] == "hook:UserPromptSubmit"


@pytest.mark.parametrize("forbidden_key", ["prompt", "transcript", "command", "message_body", "raw_input", "text"])
def test_given_forbidden_metadata_key_when_event_appended_then_validation_error_raised(conn, forbidden_key):
    with pytest.raises(errors.ValidationError):
        service.append_event(conn, event_type="hook:test", metadata={forbidden_key: "some content"})


def test_given_oversized_string_value_when_event_appended_then_validation_error_raised(conn):
    long_value = "x" * 5000
    with pytest.raises(errors.ValidationError):
        service.append_event(conn, event_type="hook:test", metadata={"status": long_value})


def test_given_non_scalar_metadata_value_when_event_appended_then_validation_error_raised(conn):
    with pytest.raises(errors.ValidationError):
        service.append_event(conn, event_type="hook:test", metadata={"status": {"nested": "dict"}})


def test_given_event_row_when_update_attempted_directly_then_db_trigger_aborts(conn):
    event = service.append_event(conn, event_type="hook:test", metadata={"status": "ok"})
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("UPDATE events SET event_type = 'mutated' WHERE id = ?", (event["id"],))


def test_given_event_row_when_delete_attempted_directly_then_db_trigger_aborts(conn):
    event = service.append_event(conn, event_type="hook:test", metadata={"status": "ok"})
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("DELETE FROM events WHERE id = ?", (event["id"],))
