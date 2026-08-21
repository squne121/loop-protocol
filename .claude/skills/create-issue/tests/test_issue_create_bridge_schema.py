"""AC1: ISOLATION_ISSUE_CREATE_REQUEST_V1 / ISOLATION_ISSUE_CREATE_RESULT_V1 exact schema.

Key set, types, bounds, duplicate-key rejection, unknown-key rejection, and
bool-not-accepted-as-int are all covered here. Both the child-side client
(this file's primary subject) and the parent-side server implement the same
closed schema independently (see design note in issue_create_bridge_client.py).
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

import issue_create_bridge_client as bridge_client  # noqa: E402


def _valid_request_dict() -> dict:
    return {
        "schema": "ISOLATION_ISSUE_CREATE_REQUEST_V1",
        "request_id": "a" * 32,
        "run_nonce": "nonce-abc",
        "claimed_repo": "squne121/loop-protocol",
        "title": "実装: something",
        "body": "body text",
        "labels": ["enhancement"],
        "issue_kind": "implementation",
        "label_profile": "standard",
        "parent_issue_number": None,
        "dependency_issue_numbers": [],
        "blocking_issue_numbers": [],
    }


def _valid_result_dict() -> dict:
    return {
        "schema": "ISOLATION_ISSUE_CREATE_RESULT_V1",
        "request_id": "a" * 32,
        "status": "success",
        "issue_number": 42,
        "issue_url": "https://github.com/squne121/loop-protocol/issues/42",
        "node_id": "NODEID_42",
        "body_sha256": "0" * 64,
        "completed_steps": ["create"],
        "failure_stage": None,
        "failure_message": None,
        "reconciled": False,
    }


class TestRequestSchemaExact:
    def test_valid_request_passes(self) -> None:
        bridge_client.validate_request_payload(_valid_request_dict())

    def test_unknown_key_rejected(self) -> None:
        payload = _valid_request_dict()
        payload["extra_field"] = "x"
        with pytest.raises(bridge_client.BridgeSchemaError):
            bridge_client.validate_request_payload(payload)

    def test_missing_key_rejected(self) -> None:
        payload = _valid_request_dict()
        del payload["title"]
        with pytest.raises(bridge_client.BridgeSchemaError):
            bridge_client.validate_request_payload(payload)

    def test_duplicate_json_key_rejected_at_decode(self) -> None:
        raw = '{"schema": "ISOLATION_ISSUE_CREATE_REQUEST_V1", "schema": "dup"}'
        with pytest.raises(bridge_client.BridgeSchemaError):
            bridge_client._decode_strict_object(raw)

    def test_bool_rejected_as_int_for_parent_issue_number(self) -> None:
        payload = _valid_request_dict()
        payload["parent_issue_number"] = True
        with pytest.raises(bridge_client.BridgeSchemaError):
            bridge_client.validate_request_payload(payload)

    def test_bool_rejected_as_int_in_dependency_list(self) -> None:
        payload = _valid_request_dict()
        payload["dependency_issue_numbers"] = [True]
        with pytest.raises(bridge_client.BridgeSchemaError):
            bridge_client.validate_request_payload(payload)

    def test_title_too_long_rejected(self) -> None:
        payload = _valid_request_dict()
        payload["title"] = "x" * 257
        with pytest.raises(bridge_client.BridgeSchemaError):
            bridge_client.validate_request_payload(payload)

    def test_empty_title_rejected(self) -> None:
        payload = _valid_request_dict()
        payload["title"] = ""
        with pytest.raises(bridge_client.BridgeSchemaError):
            bridge_client.validate_request_payload(payload)

    def test_invalid_label_profile_rejected(self) -> None:
        payload = _valid_request_dict()
        payload["label_profile"] = "bogus"
        with pytest.raises(bridge_client.BridgeSchemaError):
            bridge_client.validate_request_payload(payload)

    def test_non_string_label_rejected(self) -> None:
        payload = _valid_request_dict()
        payload["labels"] = [123]
        with pytest.raises(bridge_client.BridgeSchemaError):
            bridge_client.validate_request_payload(payload)


class TestResultSchemaExact:
    def test_valid_result_passes(self) -> None:
        bridge_client.validate_result_payload(_valid_result_dict())

    def test_unknown_key_rejected(self) -> None:
        payload = _valid_result_dict()
        payload["extra"] = 1
        with pytest.raises(bridge_client.BridgeSchemaError):
            bridge_client.validate_result_payload(payload)

    def test_missing_key_rejected(self) -> None:
        payload = _valid_result_dict()
        del payload["node_id"]
        with pytest.raises(bridge_client.BridgeSchemaError):
            bridge_client.validate_result_payload(payload)

    def test_invalid_status_rejected(self) -> None:
        payload = _valid_result_dict()
        payload["status"] = "bogus"
        with pytest.raises(bridge_client.BridgeSchemaError):
            bridge_client.validate_result_payload(payload)

    def test_bool_rejected_as_int_for_issue_number(self) -> None:
        payload = _valid_result_dict()
        payload["issue_number"] = True
        with pytest.raises(bridge_client.BridgeSchemaError):
            bridge_client.validate_result_payload(payload)

    def test_reconciled_must_be_bool(self) -> None:
        payload = _valid_result_dict()
        payload["reconciled"] = 1
        with pytest.raises(bridge_client.BridgeSchemaError):
            bridge_client.validate_result_payload(payload)


class TestBuildRequest:
    def test_build_request_sets_schema_and_generates_request_id(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("CLAUDE_GPT_ISSUE_CREATE_RUN_NONCE", "run-nonce-xyz")
        request = bridge_client.build_request(
            claimed_repo="squne121/loop-protocol",
            title="実装: test",
            body="body",
        )
        payload = request.to_json_dict()
        assert payload["schema"] == "ISOLATION_ISSUE_CREATE_REQUEST_V1"
        assert len(payload["request_id"]) == 32
        assert payload["run_nonce"] == "run-nonce-xyz"
        bridge_client.validate_request_payload(payload)

    def test_build_request_without_run_nonce_env_raises(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("CLAUDE_GPT_ISSUE_CREATE_RUN_NONCE", raising=False)
        with pytest.raises(bridge_client.BridgeClientError):
            bridge_client.build_request(claimed_repo="squne121/loop-protocol", title="t", body="b")


class TestIsolationProfileDetection:
    def test_not_isolated_by_default(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("CLAUDE_GPT_ISOLATION_PROFILE", raising=False)
        assert bridge_client.is_isolated_profile() is False

    def test_isolated_when_flag_is_exactly_one(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("CLAUDE_GPT_ISOLATION_PROFILE", "1")
        assert bridge_client.is_isolated_profile() is True

    def test_not_isolated_for_other_truthy_strings(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("CLAUDE_GPT_ISOLATION_PROFILE", "true")
        assert bridge_client.is_isolated_profile() is False
