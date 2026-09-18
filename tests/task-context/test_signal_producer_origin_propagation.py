"""AC14: adapters carry origin internally and never add it to public signals."""

from __future__ import annotations

import ctl_client


def test_given_hook_origin_when_signal_apply_is_called_then_origin_is_transport_only_and_public_payload_stays_frozen(
    monkeypatch,
):
    captured = {}

    def fake_call(argv, operation, payload, *, timeout, origin_session_id=None):
        captured.update(
            argv=argv,
            operation=operation,
            payload=payload,
            timeout=timeout,
            origin_session_id=origin_session_id,
        )
        return {"status": "ok", "data": {"disposition": "applied"}}

    monkeypatch.setattr(ctl_client, "call", fake_call)
    payload = {
        "signal_kind": "implementation_pr_observed",
        "source": "open-pr",
        "source_schema_version": "v1",
        "evidence": {"repo": "squne121/loop-protocol", "issue_number": 20, "pr_number": 21},
    }
    assert ctl_client.call_signal_apply(payload, origin_session_id="session-1") == {
        "status": "ok",
        "data": {"disposition": "applied"},
    }
    assert captured["argv"] == ["signal", "apply"]
    assert captured["operation"] == "signal_apply"
    assert captured["origin_session_id"] == "session-1"
    assert set(captured["payload"]) == {"signal_kind", "source", "source_schema_version", "evidence"}
    assert "session_id" not in captured["payload"]
