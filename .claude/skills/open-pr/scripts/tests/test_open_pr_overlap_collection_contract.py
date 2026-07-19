#!/usr/bin/env python3
"""Tests for the overlap preflight collection contract verification in
open_pr.py (#1493 AC3).

`check_implementation_overlap.py`（producer）は GraphQL cursor pagination の
完全性を証明するため、``source`` に ``collection_mode`` / ``page_size`` /
``page_count`` / ``fetched_count`` / ``has_next_page`` を additive に積む
（#1493 AC1）。``open_pr.py`` の overlap preflight hard gate（consumer）は、
stored evidence と fresh（オンライン再実行）evidence の collection contract
が一致することを検証し、legacy evidence（collection contract 未対応）や
collection_mode drift を fail-closed で拒否する。
"""

from __future__ import annotations

import hashlib
import json
import sys
import tempfile
from pathlib import Path
from typing import Any, Dict

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

import open_pr  # noqa: E402


def _canonical_json(payload: object) -> str:
    return json.dumps(payload, sort_keys=True, ensure_ascii=True, separators=(",", ":"))


def _sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _build_evidence(
    *,
    decision_inputs_sha256: str = "sha256:" + "a" * 64,
    route: str = "proceed",
    current_issue_number: int = 1493,
    source_limit: int = 500,
    repository: str = "squne121/loop-protocol",
    source_extra: Dict[str, Any] | None = None,
) -> Dict[str, Any]:
    source: Dict[str, Any] = {
        "complete": True,
        "saturated": False,
        "limit": source_limit,
        "collected_at": "2026-07-19T00:00:00Z",
        "collection_mode": "exhaustive_cursor_pagination",
        "page_size": 100,
        "page_count": 1,
        "fetched_count": 5,
        "has_next_page": False,
    }
    if source_extra is not None:
        source.update(source_extra)
    body: Dict[str, Any] = {
        "schema": open_pr.OVERLAP_PREFLIGHT_SCHEMA,
        "repository": repository,
        "current_issue": {"number": current_issue_number, "allowed_paths": []},
        "source": source,
        "candidates": [],
        "dependency_resolution": {
            "blocked_by_refs": [],
            "blocking_predecessor": None,
            "closed_predecessors": [],
            "unresolved_refs": [],
        },
        "validation_errors": {},
        "route": route,
        "decision_inputs_sha256": decision_inputs_sha256,
    }
    canonical = _canonical_json(body)
    body["evidence_sha256"] = f"sha256:{_sha256(canonical)}"
    return body


def _write_evidence(evidence: Dict[str, Any]) -> Path:
    handle = tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", suffix=".json", delete=False)
    handle.write(json.dumps(evidence))
    handle.flush()
    handle.close()
    return Path(handle.name)


class _FakeCompletedProcess:
    def __init__(self, returncode: int, stdout: str) -> None:
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = ""


def _run_gate(
    monkeypatch: pytest.MonkeyPatch,
    *,
    stored: Dict[str, Any],
    fresh: Dict[str, Any],
    linked_issue: int = 1493,
    repo: str = "squne121/loop-protocol",
):
    evidence_path = _write_evidence(stored)

    def fake_run(cmd, **kwargs):
        return _FakeCompletedProcess(0, json.dumps(fresh))

    monkeypatch.setattr(open_pr.subprocess, "run", fake_run)
    try:
        return open_pr.run_overlap_preflight_gate(
            repo=repo,
            linked_issue=linked_issue,
            evidence_file=evidence_path,
            expected_evidence_sha256=stored["evidence_sha256"],
            expected_decision_inputs_sha256=stored["decision_inputs_sha256"],
        )
    finally:
        evidence_path.unlink(missing_ok=True)


def test_stored_evidence_missing_collection_contract_fields_is_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    """GIVEN legacy stored evidence（collection contract 未対応、cursor
    pagination 以前の producer が生成）
    WHEN overlap preflight gate を実行する
    THEN E_OVERLAP_PREFLIGHT_EVIDENCE_INVALID で fail-closed に拒否し、
    online 再実行（subprocess）を一度も呼ばない（再収集を要求する）。
    """
    stored = _build_evidence()
    for key in ("collection_mode", "page_size", "page_count", "fetched_count", "has_next_page"):
        del stored["source"][key]
    # embedded hash を legacy shape に合わせて再計算する。
    body = {k: v for k, v in stored.items() if k != "evidence_sha256"}
    stored["evidence_sha256"] = f"sha256:{_sha256(_canonical_json(body))}"

    called = {"count": 0}

    def fail_if_called(cmd, **kwargs):
        called["count"] += 1
        raise AssertionError("legacy stored evidence must block before online recheck")

    monkeypatch.setattr(open_pr.subprocess, "run", fail_if_called)
    evidence_path = _write_evidence(stored)
    try:
        ok, error_code, detail, fresh = open_pr.run_overlap_preflight_gate(
            repo="squne121/loop-protocol",
            linked_issue=1493,
            evidence_file=evidence_path,
            expected_evidence_sha256=stored["evidence_sha256"],
            expected_decision_inputs_sha256=stored["decision_inputs_sha256"],
        )
    finally:
        evidence_path.unlink(missing_ok=True)

    assert ok is False
    assert error_code == open_pr.E_OVERLAP_PREFLIGHT_EVIDENCE_INVALID
    assert "collection contract" in detail
    assert called["count"] == 0


def test_fresh_evidence_missing_collection_contract_fields_is_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    """GIVEN valid stored evidence だが online 再実行が legacy 相当の fresh
    evidence（collection contract 未対応）を返す
    WHEN overlap preflight gate を実行する
    THEN E_OVERLAP_PREFLIGHT_DRIFT で拒否する。
    """
    stored = _build_evidence()
    fresh = json.loads(json.dumps(stored))
    for key in ("collection_mode", "page_size", "page_count", "fetched_count", "has_next_page"):
        del fresh["source"][key]

    ok, error_code, detail, fresh_out = _run_gate(monkeypatch, stored=stored, fresh=fresh)

    assert ok is False
    assert error_code == open_pr.E_OVERLAP_PREFLIGHT_DRIFT
    assert "collection contract" in detail


def test_collection_mode_drift_between_stored_and_fresh_is_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    """GIVEN stored の collection_mode と fresh の collection_mode が異なる
    WHEN overlap preflight gate を実行する
    THEN E_OVERLAP_PREFLIGHT_DRIFT で拒否する（caller は collection contract
    を上書きできない）。
    """
    stored = _build_evidence()
    fresh = json.loads(json.dumps(stored))
    fresh["source"]["collection_mode"] = "single_page_legacy"

    ok, error_code, detail, fresh_out = _run_gate(monkeypatch, stored=stored, fresh=fresh)

    assert ok is False
    assert error_code == open_pr.E_OVERLAP_PREFLIGHT_DRIFT
    assert "collection_mode" in detail


def test_matching_collection_contract_passes(monkeypatch: pytest.MonkeyPatch) -> None:
    """GIVEN stored/fresh の collection contract が完全一致し、route が safe
    WHEN overlap preflight gate を実行する
    THEN gate を通過する（ok=True）。
    """
    stored = _build_evidence()
    fresh = json.loads(json.dumps(stored))

    ok, error_code, detail, fresh_out = _run_gate(monkeypatch, stored=stored, fresh=fresh)

    assert ok is True, (error_code, detail)
    assert error_code is None
