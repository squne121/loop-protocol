"""
summarize_agent_transcript.py

Claude / Codex の transcript JSONL から token/context 浪費 hotspot を抽出して
AGENT_SESSION_HOTSPOTS_V1 JSON artifact を生成する。

exit codes:
  0: pass (artifact 生成成功)
  1: warn (parser_warnings あり or partial coverage)
  2: missing_input (transcript path 欠落/読み取り不可)
  3: parse_error (transcript が解析不可 — 全行 invalid)
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

# スクリプトバージョン
SCRIPT_VERSION = "1.1.0"

# redaction パターン (順序重要: PEM > 長いトークン > 短いトークン > path)
REDACT_PATTERNS = [
    # PEM key block (multiline — apply first)
    (re.compile(r"-----BEGIN [A-Z ]+-----[\s\S]+?-----END [A-Z ]+-----"), "<PEM_KEY>"),
    # GitHub token (ghs_, ghp_, gho_, ghu_, ghr_ prefixes with 20+ chars)
    (re.compile(r"gh[opsur]_[A-Za-z0-9_]{20,}"), "<GITHUB_TOKEN>"),
    # OpenAI key
    (re.compile(r"sk-[A-Za-z0-9]{32,}"), "<OPENAI_KEY>"),
    # AWS key
    (re.compile(r"AKIA[A-Z0-9]{16}"), "<AWS_ACCESS_KEY>"),
    # absolute paths
    (re.compile(r"/[^\s\"'<>|]+"), "<PATH>"),
]

UNKNOWN: dict[str, Any] = {"availability": "unknown", "value": None}

# source_family 分類
SOURCE_FAMILY_HOOK_INPUT = "hook_input"
SOURCE_FAMILY_TRANSCRIPT_JSONL = "transcript_jsonl"

# trusted fields extracted from session_start event (fixture_observed)
_TRUSTED_SESSION_FIELDS = frozenset(["tool", "version", "model", "reasoning_effort"])

# known metric-contributing event types (trusted schema fields only)
_KNOWN_EVENT_TYPES = frozenset([
    "session_start",
    "session_end",
    "tool_use",
    "subagent_spawn",
    "hook_fired",
    "compaction_marker",
    "human_intervention",
    "token_usage",
    "assistant_response",
    "last_assistant_message",
])


def unknown_if_missing(value: Any, *, default: Any = None) -> Any:
    """値が None または欠落している場合は UNKNOWN wrapper を返す。"""
    if value is None:
        return UNKNOWN
    return value


def redact_string(text: str) -> str:
    """センシティブ情報を redact する。"""
    for pattern, replacement in REDACT_PATTERNS:
        text = pattern.sub(replacement, text)
    return text


def sha256_file(path: Path) -> str | None:
    """ファイルの SHA256 を計算する。"""
    try:
        h = hashlib.sha256()
        with open(path, "rb") as f:
            for chunk in iter(lambda: f.read(8192), b""):
                h.update(chunk)
        return h.hexdigest()
    except OSError:
        return None


def _classify_ordering(
    timestamps: list[str | None],
    sources: list[str | None],
) -> dict[str, Any]:
    """
    ordering を分類する。

    Rules:
    - timestamp 欠落あり -> unknown
    - 同一 timestamp が 2 つ以上 -> unknown
    - multi-source (複数の source フィールド) -> unknown
    - それ以外かつ single source のみ -> available
    """
    # timestamp 欠落チェック
    if any(ts is None for ts in timestamps):
        return dict(UNKNOWN)

    # 同一 timestamp チェック
    ts_counter = Counter(ts for ts in timestamps if ts is not None)
    if any(count > 1 for count in ts_counter.values()):
        return dict(UNKNOWN)

    # multi-source チェック
    non_none_sources = [s for s in sources if s is not None]
    unique_sources = set(non_none_sources)
    if len(unique_sources) > 1:
        return dict(UNKNOWN)

    return {"availability": "available", "value": "sequential"}


def parse_transcript(path: Path, redact: bool) -> tuple[dict[str, Any], list[str]]:
    """
    JSONL transcript を解析してメトリクスを抽出する。

    source_family ポリシー:
      - hook_input: --hook-input / --manifest で渡された値のみ trusted
      - transcript_jsonl: fixture_observed_only — unknown keys は metrics に昇格しない

    Returns:
        (result_dict, parser_warnings)
        result_dict には metrics / event_counts / fixture_observed_fields が含まれる
    """
    warnings: list[str] = []
    event_counts: dict[str, int] = {}
    # fixture で観測されたが metrics に昇格しない unknown keys
    fixture_observed_fields: set[str] = set()

    # メトリクス収集用 (trusted fields only)
    tool_name: str | None = None
    tool_version: str | None = None
    model_name: str | None = None
    reasoning_effort: str | None = None
    spawned_subagents: int = 0
    prompt_tokens: int | None = None
    completion_tokens: int | None = None
    total_tokens: int | None = None
    hooks_fired: int = 0
    hooks_blocked: int = 0
    hooks_skipped: int = 0
    failed_commands: int = 0
    compaction_seen: bool = False
    human_interventions: int = 0

    # files read/modified tracking
    read_paths: list[str] = []
    read_unique: set[str] = set()
    modified_paths: set[str] = set()
    read_count: int = 0
    modified_count: int = 0

    # ordering tracking
    event_timestamps: list[str | None] = []
    hook_sources: list[str | None] = []

    lines_parsed = 0
    lines_failed = 0

    try:
        with open(path, encoding="utf-8") as f:
            for i, raw_line in enumerate(f, 1):
                raw_line = raw_line.strip()
                if not raw_line:
                    continue

                try:
                    event = json.loads(raw_line)
                except json.JSONDecodeError as e:
                    lines_failed += 1
                    warnings.append(f"Line {i}: JSON parse error: {e}")
                    continue

                lines_parsed += 1
                etype = event.get("type", "unknown")
                event_counts[etype] = event_counts.get(etype, 0) + 1

                # timestamp tracking for ordering
                ts = event.get("timestamp")
                event_timestamps.append(ts)

                # unknown event fields — fixture_observed のみ記録、metrics に昇格しない
                if etype not in _KNOWN_EVENT_TYPES:
                    for key in event.keys():
                        if key not in ("type", "timestamp"):
                            fixture_observed_fields.add(key)

                if etype == "session_start":
                    tool_name = event.get("tool")
                    tool_version = event.get("version")
                    model_name = event.get("model")
                    reasoning_effort = event.get("reasoning_effort")
                    # session_id / turn_id は hook_input フィールド — fixture_observed のみ
                    for key in event.keys():
                        if key not in _TRUSTED_SESSION_FIELDS and key not in ("type", "timestamp"):
                            fixture_observed_fields.add(key)

                elif etype == "tool_use":
                    tname = event.get("tool_name", "")
                    # failed command 検出
                    exit_code = event.get("exit_code")
                    if exit_code is not None and exit_code != 0:
                        failed_commands += 1

                    # files read 検出
                    if tname == "Read":
                        inp = event.get("input", {})
                        fpath = inp.get("file_path", "")
                        if fpath:
                            read_count += 1
                            # redact path before storing
                            store_path = redact_string(fpath) if redact else fpath
                            read_paths.append(store_path)
                            read_unique.add(store_path)

                    # files modified 検出 (Edit / Write / Apply)
                    elif tname in ("Edit", "Write", "Apply"):
                        inp = event.get("input", {})
                        fpath = inp.get("file_path", inp.get("path", ""))
                        if fpath:
                            modified_count += 1
                            store_path = redact_string(fpath) if redact else fpath
                            modified_paths.add(store_path)

                elif etype == "subagent_spawn":
                    spawned_subagents += 1

                elif etype == "hook_fired":
                    result = event.get("result", "")
                    source = event.get("source")
                    hook_sources.append(source)

                    if result == "blocked":
                        hooks_blocked += 1
                    elif result == "skipped":
                        hooks_skipped += 1
                    else:
                        hooks_fired += 1

                elif etype == "compaction_marker":
                    compaction_seen = True

                elif etype == "human_intervention":
                    human_interventions += 1

                elif etype == "token_usage":
                    p = event.get("prompt_tokens")
                    c = event.get("completion_tokens")
                    t = event.get("total_tokens")
                    if p is not None:
                        prompt_tokens = (prompt_tokens or 0) + p
                    if c is not None:
                        completion_tokens = (completion_tokens or 0) + c
                    if t is not None:
                        total_tokens = (total_tokens or 0) + t

    except OSError as e:
        raise RuntimeError(f"Failed to read transcript: {e}") from e

    # 全行 invalid = parse_error (exit 3)
    if lines_failed > 0 and lines_parsed == 0:
        raise AllLinesInvalidError(
            f"All {lines_failed} non-empty lines failed to parse"
        )

    if lines_failed > 0:
        warnings.append(
            f"Failed to parse {lines_failed}/{lines_parsed + lines_failed} lines"
        )

    # repeated read 計算
    read_counter = Counter(read_paths)
    repeated_read_count = sum(count - 1 for count in read_counter.values() if count > 1)
    unique_read_count = len(read_unique)

    # ordering 判定
    ordering = _classify_ordering(event_timestamps, hook_sources)

    # tool info
    tool_info = {
        "name": tool_name if tool_name is not None else UNKNOWN,
        "version": tool_version if tool_version is not None else UNKNOWN,
    }

    # model info
    model_info = {
        "name": model_name if model_name is not None else UNKNOWN,
        "reasoning_effort": reasoning_effort if reasoning_effort is not None else UNKNOWN,
    }

    metrics: dict[str, Any] = {
        "tool": tool_info,
        "model": model_info,
        "subagents": {
            "spawned_count": spawned_subagents,
        },
        "tokens": {
            "prompt": prompt_tokens if prompt_tokens is not None else UNKNOWN,
            "completion": completion_tokens if completion_tokens is not None else UNKNOWN,
            "total": total_tokens if total_tokens is not None else UNKNOWN,
        },
        "hooks": {
            "fired_count": hooks_fired,
            "blocked_count": hooks_blocked,
            "skipped_count": hooks_skipped,
        },
        "commands": {
            "failed_count": failed_commands,
        },
        "reads": {
            "repeated_read_count": repeated_read_count,
        },
        "files": {
            "read_count": read_count,
            "unique_read_count": unique_read_count,
            "modified_count": modified_count,
        },
        "compaction": {
            "marker_seen": compaction_seen,
        },
        "human_intervention": {
            "count": human_interventions,
        },
        "ordering": ordering,
    }

    return {
        "metrics": metrics,
        "event_counts": event_counts,
        "fixture_observed_fields": sorted(fixture_observed_fields),
    }, warnings


class AllLinesInvalidError(RuntimeError):
    """全行が invalid JSONL の場合 (exit code 3)。"""
    pass


def build_artifact(
    transcript_path: Path,
    manifest_path: Path | None,
    result: dict[str, Any],
    parser_warnings: list[str],
    redact: bool,
) -> dict[str, Any]:
    """AGENT_SESSION_HOTSPOTS_V1 artifact を構築する。"""
    now = datetime.now(timezone.utc).isoformat()

    # transcript_path の redaction
    t_path_str = str(transcript_path)
    t_sha = sha256_file(transcript_path)
    if redact:
        t_path_str = redact_string(t_path_str)

    m_path_str: str | None = None
    m_sha: str | None = None
    if manifest_path is not None:
        m_path_str = str(manifest_path)
        m_sha = sha256_file(manifest_path)
        if redact:
            m_path_str = redact_string(m_path_str)

    artifact: dict[str, Any] = {
        "schema": "AGENT_SESSION_HOTSPOTS_V1",
        "generated_at": now,
        "producer": {
            "script": "scripts/summarize_agent_transcript.py",
            "version": SCRIPT_VERSION,
        },
        "input_refs": {
            "transcript_path": {
                "value": t_path_str,
                "sha256": t_sha,
            },
            "manifest_path": {
                "value": m_path_str,
                "sha256": m_sha,
            },
        },
        "privacy": {
            "raw_transcript_included": False,
            "redaction_enabled": redact,
            "public_projection_safe": redact,
        },
        "metrics": result["metrics"],
        "evidence": {
            "event_counts": result["event_counts"],
            "fixture_observed_fields": result.get("fixture_observed_fields", []),
            "parser_warnings": parser_warnings,
            "input_source_policy": {
                "hook_input": SOURCE_FAMILY_HOOK_INPUT,
                "transcript_jsonl": SOURCE_FAMILY_TRANSCRIPT_JSONL,
                "note": "Only hook_input trusted fields are promoted to metrics",
            },
        },
    }
    return artifact


def write_artifact(artifact: dict[str, Any], transcript_path: Path) -> Path:
    """artifact を tmp/agent-session-hotspots/ に書き込む (atomic rename)。"""
    out_dir = Path("tmp/agent-session-hotspots")
    out_dir.mkdir(parents=True, exist_ok=True)

    # ファイル名衝突防止: {stem}_{ts}_{sha_prefix}_{pid}.json
    stem = transcript_path.stem
    ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    t_sha = artifact["input_refs"]["transcript_path"].get("sha256") or ""
    sha_prefix = t_sha[:8] if t_sha else "00000000"
    pid = os.getpid()
    final_name = f"{stem}_{ts}_{sha_prefix}_{pid}.json"
    final_path = out_dir / final_name
    tmp_path = out_dir / f".tmp_{final_name}"

    with open(tmp_path, "w", encoding="utf-8") as f:
        json.dump(artifact, f, indent=2, ensure_ascii=False)

    tmp_path.rename(final_path)
    return final_path


def emit_stdout(
    status: str,
    artifact: dict[str, Any],
    out_path: Path,
    warnings: list[str],
) -> None:
    """
    stdout は STATUS / SUMMARY / NEXT_ACTION / MUST_READ / EVIDENCE / BLOCKERS / ARTIFACT のみ。
    raw transcript 本文は含まない。
    """
    print(f"STATUS: {status}")
    print()

    tool_info = artifact["metrics"]["tool"]
    model_info = artifact["metrics"]["model"]
    subagents = artifact["metrics"]["subagents"]["spawned_count"]
    tokens = artifact["metrics"]["tokens"]
    failed = artifact["metrics"]["commands"]["failed_count"]
    repeated = artifact["metrics"]["reads"]["repeated_read_count"]
    compaction = artifact["metrics"]["compaction"]["marker_seen"]
    human = artifact["metrics"]["human_intervention"]["count"]
    hooks = artifact["metrics"]["hooks"]
    files = artifact["metrics"].get("files", {})

    tool_display = tool_info.get("name") if isinstance(tool_info, dict) else tool_info
    model_display = model_info.get("name") if isinstance(model_info, dict) else model_info

    print("SUMMARY:")
    print(f"  tool: {tool_display}")
    print(f"  model: {model_display}")
    print(f"  subagents_spawned: {subagents}")
    print(f"  failed_commands: {failed}")
    print(f"  repeated_reads: {repeated}")
    print(f"  hooks_blocked: {hooks['blocked_count']}")
    print(f"  compaction_marker_seen: {compaction}")
    print(f"  human_interventions: {human}")
    print(f"  tokens: {tokens}")
    if files:
        print(f"  files_read: {files.get('read_count', 0)}")
        print(f"  files_modified: {files.get('modified_count', 0)}")
    print()

    # MUST_READ: 重要な警告や人間が必ず確認すべき項目
    must_read_items: list[str] = []
    if hooks.get("blocked_count", 0) > 0:
        must_read_items.append(
            f"{hooks['blocked_count']} hook(s) were BLOCKED — review artifact evidence for details"
        )
    if repeated > 0:
        must_read_items.append(
            f"{repeated} repeated file read(s) detected — possible context waste"
        )
    if must_read_items:
        print("MUST_READ:")
        for item in must_read_items:
            print(f"  - {item}")
        print()

    if warnings:
        print("BLOCKERS:")
        for w in warnings:
            print(f"  - {w}")
        print()

    print(f"ARTIFACT: {out_path}")
    print()
    print("NEXT_ACTION: review artifact for cost optimization opportunities")

    if warnings:
        print()
        print("EVIDENCE:")
        print("  parser_warnings encountered — see artifact evidence.parser_warnings")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Summarize agent transcript JSONL into AGENT_SESSION_HOTSPOTS_V1 artifact"
    )
    parser.add_argument(
        "--transcript",
        required=True,
        help="Path to the transcript JSONL file",
    )
    parser.add_argument(
        "--manifest",
        default=None,
        help="Optional path to agent session manifest JSON (hook_input trusted source)",
    )
    parser.add_argument(
        "--hook-input",
        default=None,
        help="Optional JSON string from hook input (hook_input trusted source)",
    )
    parser.add_argument(
        "--redact",
        action="store_true",
        help="Redact sensitive values (paths, keys) from artifact",
    )
    parser.add_argument(
        "--max-lines",
        type=int,
        default=None,
        help="Maximum number of lines to process (streaming limit)",
    )
    parser.add_argument(
        "--max-bytes",
        type=int,
        default=None,
        help="Maximum bytes to process (streaming limit)",
    )
    args = parser.parse_args(argv)

    transcript_path = Path(args.transcript)
    manifest_path = Path(args.manifest) if args.manifest else None

    # missing_input チェック
    if not transcript_path.exists():
        print("STATUS: 2", flush=True)
        print(f"BLOCKERS: transcript not found: {transcript_path}", file=sys.stderr)
        return 2

    if not transcript_path.is_file():
        print("STATUS: 2", flush=True)
        print(f"BLOCKERS: transcript is not a file: {transcript_path}", file=sys.stderr)
        return 2

    # parse
    try:
        result, warnings = parse_transcript(transcript_path, redact=args.redact)
    except AllLinesInvalidError as e:
        print("STATUS: parse_error", flush=True)
        print(f"BLOCKERS: parse error: {e}", file=sys.stderr)
        return 3
    except RuntimeError as e:
        print("STATUS: parse_error", flush=True)
        print(f"BLOCKERS: parse error: {e}", file=sys.stderr)
        return 3

    # artifact 構築
    artifact = build_artifact(
        transcript_path=transcript_path,
        manifest_path=manifest_path,
        result=result,
        parser_warnings=warnings,
        redact=args.redact,
    )

    # artifact 書き込み
    out_path = write_artifact(artifact, transcript_path)

    # exit code 決定
    if warnings:
        status_str = "1"
        exit_code = 1
    else:
        status_str = "0"
        exit_code = 0

    emit_stdout(status_str, artifact, out_path, warnings)

    return exit_code


if __name__ == "__main__":
    sys.exit(main())
