"""AC12: projection_outbox flush/ack is revision-aware and conditional --
an enqueue() that advances the desired revision between a flush()'s read
and the matching ack() must not be lost.

fix_delta finding 4: projection_outbox holds ONLY the
(projection_key, desired_revision) marker -- there is no payload column and
`enqueue_projection`/`read_projection`/`flush_projection` take/return no
payload. The projector re-derives actual content to project from canonical
DB state at flush time; these tests assert the marker-only revision
semantics, not any payload snapshot."""

from __future__ import annotations

import task_context_service as service


def test_given_single_enqueue_when_flushed_and_acked_then_row_is_removed(conn):
    service.enqueue_projection(conn, "herdr:tab-1", revision=1)
    flushed = service.flush_projection(conn, "herdr:tab-1")
    assert flushed["desired_revision"] == 1
    assert "payload_json" not in flushed

    result = service.ack_projection(conn, "herdr:tab-1", read_revision=1)
    assert result["acked"] is True
    assert service.read_projection(conn, "herdr:tab-1") is None


def test_given_race_between_flush_read_and_ack_when_enqueue_advances_revision_then_ack_does_not_delete_it(conn):
    service.enqueue_projection(conn, "herdr:tab-2", revision=1)
    flushed = service.flush_projection(conn, "herdr:tab-2")
    read_revision = flushed["desired_revision"]
    assert read_revision == 1

    # Simulate a concurrent enqueue() advancing desired_revision to 2
    # *between* the flush read above and the ack below (external I/O for
    # revision 1 is still "in flight" conceptually).
    service.enqueue_projection(conn, "herdr:tab-2", revision=2)

    result = service.ack_projection(conn, "herdr:tab-2", read_revision=read_revision)
    assert result["acked"] is False

    remaining = service.read_projection(conn, "herdr:tab-2")
    assert remaining is not None
    assert remaining["desired_revision"] == 2


def test_given_lower_revision_enqueue_when_higher_revision_already_desired_then_lower_is_ignored(conn):
    service.enqueue_projection(conn, "herdr:tab-3", revision=5)
    service.enqueue_projection(conn, "herdr:tab-3", revision=3)
    current = service.read_projection(conn, "herdr:tab-3")
    assert current["desired_revision"] == 5


def test_given_second_flush_after_successful_ack_when_nothing_enqueued_then_no_row(conn):
    service.enqueue_projection(conn, "herdr:tab-4", revision=1)
    flushed = service.flush_projection(conn, "herdr:tab-4")
    service.ack_projection(conn, "herdr:tab-4", read_revision=flushed["desired_revision"])
    assert service.flush_projection(conn, "herdr:tab-4") is None
