"""scripts/claude-gpt/test_transport_log.py

transport_log.py の unit test（Issue #2204 PR #2205 OWNER REQUEST_CHANGES, iteration 2,
P0-2 / owner_minimum_fix_set #1〜#4 対応）。

fixture のイベント形式は実 `claude-code-proxy` v0.1.34 バイナリの live 出力
（`<proxy_state_dir>/claude-code-proxy/proxy.log`）から確認した実スキーマ
（`{"fields": {...}, "msg": "...", ...}`、"request"/"codex_upstream_request_started"/
"request_completed" の 3 イベントを `fields.reqId` で相関）に合わせている。

負例（negative fixture）:
  - HTTP + WebSocket 混在 -> FAIL
  - HTTP + auto 混在 -> FAIL
  - reqId 不一致（response が別 reqId） -> FAIL
  - transport event ゼロ -> FAIL
  - malformed JSON 行（改行なし連結を含む、実ログで観測された破損パターン） -> FAIL
  - 未知 transport 値 -> FAIL
  - status が 200 以外 -> FAIL
  - path が /v1/messages 以外 -> FAIL
  - reqId 欠落（marker だけの偽装を想定） -> FAIL
  - request イベント欠落（path 相関不能） -> FAIL
  - request_completed イベント欠落（status 相関不能） -> FAIL

正例（positive fixture）:
  - 単一 http request が reqId で相関し 200 応答 -> PASS
  - 複数 http request がすべて相関し 200 応答 -> PASS
"""
from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

# --import-mode=importlib（pyproject.toml）配下ではテストファイルの隣接ディレクトリが
# 自動で sys.path に載らないため、他の scripts/**/tests と同様に
# importlib.util.spec_from_file_location でファイルパス直指定ロードする。
# dataclasses が __module__ 解決のため sys.modules 参照を必要とするので、
# exec_module 前に一意名で sys.modules へ登録する（重複ロードによる module identity
# 崩れを避けるため固定の一意モジュール名を使う）。
_MODULE_NAME = "claude_gpt_transport_log"
_MODULE_PATH = Path(__file__).resolve().parent / "transport_log.py"
_SPEC = importlib.util.spec_from_file_location(_MODULE_NAME, _MODULE_PATH)
assert _SPEC is not None and _SPEC.loader is not None
transport_log = importlib.util.module_from_spec(_SPEC)
sys.modules[_MODULE_NAME] = transport_log
_SPEC.loader.exec_module(transport_log)
evaluate_transport_log = transport_log.evaluate_transport_log


def _write_jsonl(path: Path, lines: list[dict]) -> Path:
    path.write_text("\n".join(json.dumps(line) for line in lines) + "\n", encoding="utf-8")
    return path


def _request(req_id: str, path: str = "/v1/messages") -> dict:
    return {"msg": "request", "fields": {"method": "POST", "path": path, "reqId": req_id}}


def _started(req_id: str, transport) -> dict:
    return {
        "msg": "codex_upstream_request_started",
        "fields": {"reqId": req_id, "transport": transport, "model": "gpt-5.6-terra"},
    }


def _completed(req_id: str, status) -> dict:
    return {
        "msg": "request_completed",
        "fields": {"reqId": req_id, "status": status, "model": "gpt-5.6-terra", "provider": "codex"},
    }


def test_single_http_request_confirmed_passes(tmp_path: Path) -> None:
    log = _write_jsonl(
        tmp_path / "proxy.log",
        [_request("r1"), _started("r1", "http"), _completed("r1", 200)],
    )
    verdict = evaluate_transport_log(str(log))
    assert verdict.ok is True
    assert verdict.started_count == 1
    assert verdict.http_count == 1
    assert verdict.websocket_count == 0
    assert verdict.requests[0]["response_ok"] is True


def test_multiple_http_requests_all_confirmed_passes(tmp_path: Path) -> None:
    log = _write_jsonl(
        tmp_path / "proxy.log",
        [
            _request("r1"),
            _started("r1", "http"),
            _completed("r1", 200),
            _request("r2"),
            _started("r2", "http"),
            _completed("r2", 200),
        ],
    )
    verdict = evaluate_transport_log(str(log))
    assert verdict.ok is True
    assert verdict.started_count == 2
    assert verdict.http_count == 2


def test_websocket_mixed_with_http_fails(tmp_path: Path) -> None:
    log = _write_jsonl(
        tmp_path / "proxy.log",
        [
            _request("r1"),
            _started("r1", "http"),
            _completed("r1", 200),
            _request("r2"),
            _started("r2", "websocket"),
            _completed("r2", 200),
        ],
    )
    verdict = evaluate_transport_log(str(log))
    assert verdict.ok is False
    assert verdict.websocket_count == 1


def test_auto_mixed_with_http_fails(tmp_path: Path) -> None:
    log = _write_jsonl(
        tmp_path / "proxy.log",
        [
            _request("r1"),
            _started("r1", "http"),
            _completed("r1", 200),
            _request("r2"),
            _started("r2", "auto"),
            _completed("r2", 200),
        ],
    )
    verdict = evaluate_transport_log(str(log))
    assert verdict.ok is False
    assert verdict.auto_count == 1


def test_unknown_transport_value_fails(tmp_path: Path) -> None:
    log = _write_jsonl(
        tmp_path / "proxy.log",
        [_request("r1"), _started("r1", "quic"), _completed("r1", 200)],
    )
    verdict = evaluate_transport_log(str(log))
    assert verdict.ok is False
    assert verdict.unknown_transport_count == 1


def test_reqid_mismatch_fails(tmp_path: Path) -> None:
    log = _write_jsonl(
        tmp_path / "proxy.log",
        [_request("r1"), _started("r1", "http"), _completed("different-req", 200)],
    )
    verdict = evaluate_transport_log(str(log))
    assert verdict.ok is False
    assert verdict.requests[0]["response_ok"] is False


def test_zero_transport_events_fails(tmp_path: Path) -> None:
    log = _write_jsonl(tmp_path / "proxy.log", [{"msg": "unrelated_event", "fields": {}}])
    verdict = evaluate_transport_log(str(log))
    assert verdict.ok is False
    assert verdict.started_count == 0


def test_malformed_json_line_fails(tmp_path: Path) -> None:
    path = tmp_path / "proxy.log"
    path.write_text(
        json.dumps(_started("r1", "http"))
        + "\n"
        + "{not valid json\n"
        + json.dumps(_request("r1"))
        + "\n"
        + json.dumps(_completed("r1", 200))
        + "\n",
        encoding="utf-8",
    )
    verdict = evaluate_transport_log(str(path))
    assert verdict.ok is False
    assert verdict.malformed_line_count == 1


def test_concatenated_json_objects_on_one_line_is_malformed(tmp_path: Path) -> None:
    """実 proxy ログで観測された破損パターン（複数 JSON object が改行なしで連結）。"""
    path = tmp_path / "proxy.log"
    concatenated = json.dumps(_request("r1")) + json.dumps(_started("r1", "http"))
    path.write_text(concatenated + "\n" + json.dumps(_completed("r1", 200)) + "\n", encoding="utf-8")
    verdict = evaluate_transport_log(str(path))
    assert verdict.ok is False
    assert verdict.malformed_line_count == 1


def test_non_object_json_line_is_malformed(tmp_path: Path) -> None:
    path = tmp_path / "proxy.log"
    path.write_text('[1, 2, 3]\n"just a string"\n', encoding="utf-8")
    verdict = evaluate_transport_log(str(path))
    assert verdict.ok is False
    assert verdict.malformed_line_count == 2


def test_status_not_200_fails(tmp_path: Path) -> None:
    log = _write_jsonl(
        tmp_path / "proxy.log",
        [_request("r1"), _started("r1", "http"), _completed("r1", 500)],
    )
    verdict = evaluate_transport_log(str(log))
    assert verdict.ok is False
    assert verdict.requests[0]["response_ok"] is False


def test_wrong_path_fails(tmp_path: Path) -> None:
    log = _write_jsonl(
        tmp_path / "proxy.log",
        [_request("r1", path="/v1/models"), _started("r1", "http"), _completed("r1", 200)],
    )
    verdict = evaluate_transport_log(str(log))
    assert verdict.ok is False
    assert verdict.requests[0]["response_ok"] is False


def test_missing_reqid_on_started_fails(tmp_path: Path) -> None:
    log = _write_jsonl(
        tmp_path / "proxy.log",
        [
            {"msg": "codex_upstream_request_started", "fields": {"transport": "http"}},
            _request("r1"),
            _completed("r1", 200),
        ],
    )
    verdict = evaluate_transport_log(str(log))
    assert verdict.ok is False
    assert verdict.requests[0]["req_id"] is None


def test_no_completed_event_at_all_fails(tmp_path: Path) -> None:
    log = _write_jsonl(
        tmp_path / "proxy.log",
        [_request("r1"), _started("r1", "http")],
    )
    verdict = evaluate_transport_log(str(log))
    assert verdict.ok is False
    assert verdict.requests[0]["response_ok"] is False


def test_no_request_event_at_all_fails(tmp_path: Path) -> None:
    """path 相関元の "request" イベントが欠落している場合、status=200 が別途
    観測されても path 不明のため PASS してはならない。"""
    log = _write_jsonl(
        tmp_path / "proxy.log",
        [_started("r1", "http"), _completed("r1", 200)],
    )
    verdict = evaluate_transport_log(str(log))
    assert verdict.ok is False
    assert verdict.requests[0]["response_path"] is None
    assert verdict.requests[0]["response_ok"] is False


def test_nonexistent_file_fails(tmp_path: Path) -> None:
    verdict = evaluate_transport_log(str(tmp_path / "does-not-exist.log"))
    assert verdict.ok is False
    assert verdict.started_count == 0


def test_marker_impersonation_without_real_upstream_event_fails(tmp_path: Path) -> None:
    """proxy ログに一切 upstream event が無いのに、別経路（stdout marker のみ）で
    成功を偽装しようとするケースを模した negative fixture。ログに `msg` フィールドを
    持たない任意の JSON 行が並んでいても started_count==0 のまま FAIL する。"""
    log = _write_jsonl(
        tmp_path / "proxy.log",
        [{"note": "canary text observed but no upstream call happened"}],
    )
    verdict = evaluate_transport_log(str(log))
    assert verdict.ok is False
