#!/usr/bin/env python3
"""scripts/claude-gpt/transport_log.py

`claude-code-proxy`（exact pinned v0.1.34, Issue #2204）が書き出す構造化 JSONL ログ
（`<proxy_state_dir>/claude-code-proxy/proxy.log`）を厳密パースし、`request` /
`codex_upstream_request_started` / `request_completed` の 3 イベントを `fields.reqId`
で相関して transport policy（Issue #2204）の実 upstream 送出結果を判定する。

ログ 1 行のスキーマ（実バイナリの live 出力から確認, 2026-08-16）:
  {"fields": {...}, "level": "info", "msg": "<event name>", "service": "...", "t": "..."}
  各イベント種別の `fields` 内容:
    - "request"                        : method, path, query, reqId
    - "codex_upstream_request_started" : reqId, transport, model, ...
    - "request_completed"              : reqId, status, model, provider, ...
  （`request_completed` 自体には `path` が含まれないため、`path` は同一 reqId の
  `request` イベントから取得する。従来の grep 実装はこの 3 イベントを同一行内の
  key 共起としてしか確認できず、実際にはファイル全体を対象に 3 つの独立した
  `grep -q` を行っていたため、異なる request 由来の path/status が偶然揃うだけで
  PASS してしまう構造的欠陥があった。）

背景（Issue #2204 PR #2205 OWNER REQUEST_CHANGES, iteration 2, P0-2）:
  従来の `runtime_smoke_test.sh` は各 step の最後の `codex_upstream_request_started` 行
  だけを `tail -n1` で確認しており、1 回でも http が観測されれば PASS になり、他の
  request で websocket / auto が観測されても検出できなかった。

本モジュールは以下を fail-closed で判定する:
  - `codex_upstream_request_started` が 1 件も無ければ FAIL
  - 1 件でも malformed JSON 行があれば FAIL（複数 JSON オブジェクトが改行なしで
    連結された行を含む。実運用ログで実際に観測された破損パターン）
  - 1 件でも transport が `http` 以外（`websocket` / `auto` / 欠落・未知値）であれば FAIL
  - `codex_upstream_request_started` の `reqId` に対応する `request` イベント（`path`）
    と `request_completed` イベント（`status`）の両方が存在し、`path` が
    `/v1/messages`、`status` が `200` でなければ FAIL（reqId 不一致・イベント欠落・
    複数マッチはいずれも FAIL 要因として記録する）

単体実行:
  python3 transport_log.py <structured_log_path>
  終了コード: 0=PASS(ok), 1=FAIL, 2=usage エラー
  stdout に判定結果の JSON を 1 行出力する。
"""
from __future__ import annotations

import json
import sys
from dataclasses import dataclass, field
from typing import Any

REQUEST_MSG = "request"
REQUEST_STARTED_MSG = "codex_upstream_request_started"
REQUEST_COMPLETED_MSG = "request_completed"
EXPECTED_PATH = "/v1/messages"
EXPECTED_STATUS = 200
KNOWN_TRANSPORTS = ("http", "websocket", "auto")


@dataclass
class TransportVerdict:
    ok: bool
    reasons: list[str]
    log_path: str
    malformed_line_count: int
    started_count: int
    http_count: int
    websocket_count: int
    auto_count: int
    unknown_transport_count: int
    requests: list[dict[str, Any]] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema": "CLAUDE_GPT_TRANSPORT_VERDICT_V1",
            "ok": self.ok,
            "reason": "; ".join(self.reasons) if self.reasons else "ok",
            "log_path": self.log_path,
            "malformed_line_count": self.malformed_line_count,
            "transport": {
                "started_count": self.started_count,
                "http_count": self.http_count,
                "websocket_count": self.websocket_count,
                "auto_count": self.auto_count,
                "unknown_count": self.unknown_transport_count,
            },
            "requests": self.requests,
        }


def load_events(log_path: str) -> tuple[list[dict[str, Any]], int]:
    """log_path を 1 行 1 JSON object として読み込む。

    空行は無視する。パース不能な行・オブジェクトでない JSON 値（配列・スカラー等）は
    malformed としてカウントし、events には含めない（fail-closed。読み飛ばして
    started_count が偶然揃うことを避ける — malformed_line_count は独立に判定へ使う）。
    ファイルが存在しない・読めない場合は空 events・malformed=0 を返す
    （呼び出し側が started_count==0 で fail-closed に扱う）。
    """
    events: list[dict[str, Any]] = []
    malformed = 0
    try:
        with open(log_path, encoding="utf-8") as fh:
            raw_lines = fh.readlines()
    except OSError:
        return events, malformed

    for raw_line in raw_lines:
        line = raw_line.strip()
        if not line:
            continue
        try:
            obj = json.loads(line)
        except json.JSONDecodeError:
            malformed += 1
            continue
        if not isinstance(obj, dict):
            malformed += 1
            continue
        events.append(obj)
    return events, malformed


def _fields(event: dict[str, Any]) -> dict[str, Any]:
    raw = event.get("fields")
    return raw if isinstance(raw, dict) else {}


def evaluate_transport_log(log_path: str) -> TransportVerdict:
    events, malformed = load_events(log_path)

    request_events = [e for e in events if e.get("msg") == REQUEST_MSG]
    started_events = [e for e in events if e.get("msg") == REQUEST_STARTED_MSG]
    completed_events = [e for e in events if e.get("msg") == REQUEST_COMPLETED_MSG]

    http_count = 0
    websocket_count = 0
    auto_count = 0
    unknown_count = 0
    requests: list[dict[str, Any]] = []
    reasons: list[str] = []

    if malformed:
        reasons.append(f"malformed_json_lines={malformed}")

    if not started_events:
        reasons.append("no_codex_upstream_request_started_events")

    for event in started_events:
        started_fields = _fields(event)
        transport = started_fields.get("transport")
        req_id = started_fields.get("reqId")

        if transport == "http":
            http_count += 1
        elif transport == "websocket":
            websocket_count += 1
        elif transport == "auto":
            auto_count += 1
        else:
            unknown_count += 1

        if req_id is None:
            reasons.append("started_event_missing_reqId")

        matched_requests = [
            r for r in request_events if req_id is not None and _fields(r).get("reqId") == req_id
        ]
        matched_completed = [
            c
            for c in completed_events
            if req_id is not None and _fields(c).get("reqId") == req_id
        ]

        response_path = None
        response_status = None

        if matched_requests:
            if len(matched_requests) > 1:
                reasons.append(f"multiple_request_events_for_reqId={req_id}")
            response_path = _fields(matched_requests[-1]).get("path")
        else:
            reasons.append(f"no_request_event_match_for_reqId={req_id}")

        if matched_completed:
            if len(matched_completed) > 1:
                reasons.append(f"multiple_request_completed_for_reqId={req_id}")
            response_status = _fields(matched_completed[-1]).get("status")
        else:
            reasons.append(f"no_request_completed_match_for_reqId={req_id}")

        response_ok = response_path == EXPECTED_PATH and response_status == EXPECTED_STATUS
        if (matched_requests or matched_completed) and not response_ok:
            reasons.append(f"unconfirmed_response_for_reqId={req_id}")

        requests.append(
            {
                "req_id": req_id,
                "configured_transport": transport,
                "response_path": response_path,
                "response_status": response_status,
                "response_ok": response_ok,
            }
        )

    all_responses_ok = bool(requests) and all(r["response_ok"] for r in requests)

    ok = (
        malformed == 0
        and len(started_events) >= 1
        and websocket_count == 0
        and auto_count == 0
        and unknown_count == 0
        and all_responses_ok
    )

    return TransportVerdict(
        ok=ok,
        reasons=reasons,
        log_path=log_path,
        malformed_line_count=malformed,
        started_count=len(started_events),
        http_count=http_count,
        websocket_count=websocket_count,
        auto_count=auto_count,
        unknown_transport_count=unknown_count,
        requests=requests,
    )


def main(argv: list[str]) -> int:
    if len(argv) != 2:
        print(json.dumps({"ok": False, "reason": "usage: transport_log.py <log_path>"}))
        return 2
    verdict = evaluate_transport_log(argv[1])
    print(json.dumps(verdict.to_dict()))
    return 0 if verdict.ok else 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
