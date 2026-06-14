"""
summarize_agent_transcript.py

Claude / Codex の transcript JSONL から token/context 浪費 hotspot を抽出して
AGENT_SESSION_HOTSPOTS_V1 JSON artifact を生成する。

exit codes:
  0: pass (artifact 生成成功)
  1: warn (parser_warnings あり or partial coverage)
  2: missing_input (transcript path 欠落/読み取り不可)
  3: parse_error (transcript が解析不可)
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

# スクリプトバージョン
SCRIPT_VERSION = "1.0.0"

# redaction パターン
REDACT_PATTERNS = [
    # GitHub token (ghs_, ghp_, gho_, ghu_, ghr_ prefixes with 20+ chars)
    (re.compile(r"gh[opsur]_[A-Za-z0-9_]{20,}"), "<GITHUB_TOKEN>"),
    # OpenAI key
    (re.compile(r"sk-[A-Za-z0-9]{32,}"), "<OPENAI_KEY>"),
    # AWS key
    (re.compile(r"AKIA[A-Z0-9]{16}"), "<AWS_ACCESS_KEY>"),
    # PEM key block
    (re.compile(r"-----BEGIN [A-Z ]+-----[\s\S]+?-----END [A-Z ]+-----"), "<PEM_KEY>"),
    # absolute paths
    (re.compile(r"/[^\s\"'<>|]+"), "<PATH>"),
]

UNKNOWN = {"availability": "unknown", "value": None}


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


def parse_transcript(path: Path, redact: bool) -> tuple[dict[str, Any], list[str]]:
    """
    JSONL transcript を解析してメトリクスを抽出する。

    Returns:
        (metrics_dict, parser_warnings)
    """
    warnings: list[str] = []
    event_counts: dict[str, int] = {}

    # メトリクス収集用
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

    # repeated read 検出
    read_paths: list[str] = []

    lines_parsed = 0
    lines_failed = 0

    try:
        with open(path, encoding="utf-8") as f:
            lines = f.readlines()
    except OSError as e:
        raise RuntimeError(f"Failed to read transcript: {e}") from e

    for i, raw_line in enumerate(lines, 1):
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

        if etype == "session_start":
            tool_name = event.get("tool")
            tool_version = event.get("version")
            model_name = event.get("model")
            reasoning_effort = event.get("reasoning_effort")

        elif etype == "tool_use":
            tname = event.get("tool_name", "")
            # failed command 検出
            exit_code = event.get("exit_code")
            if exit_code is not None and exit_code != 0:
                failed_commands += 1

            # repeated read 検出
            if tname == "Read":
                inp = event.get("input", {})
                fpath = inp.get("file_path", "")
                if fpath:
                    read_paths.append(fpath)

        elif etype == "subagent_spawn":
            spawned_subagents += 1

        elif etype == "hook_fired":
            result = event.get("result", "")
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

    if lines_failed > 0:
        warnings.append(
            f"Failed to parse {lines_failed}/{lines_parsed + lines_failed} lines"
        )

    # repeated read 計算
    from collections import Counter
    read_counter = Counter(read_paths)
    repeated_read_count = sum(count - 1 for count in read_counter.values() if count > 1)

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

    metrics = {
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
        "compaction": {
            "marker_seen": compaction_seen,
        },
        "human_intervention": {
            "count": human_interventions,
        },
    }

    return {"metrics": metrics, "event_counts": event_counts}, warnings


def build_artifact(
    transcript_path: Path,
    manifest_path: Path | None,
    metrics: dict[str, Any],
    event_counts: dict[str, int],
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
        "metrics": metrics["metrics"],
        "evidence": {
            "event_counts": event_counts,
            "parser_warnings": parser_warnings,
        },
    }
    return artifact


def write_artifact(artifact: dict[str, Any], transcript_path: Path) -> Path:
    """artifact を tmp/agent-session-hotspots/ に書き込む。"""
    out_dir = Path("tmp/agent-session-hotspots")
    out_dir.mkdir(parents=True, exist_ok=True)

    stem = transcript_path.stem
    ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    out_path = out_dir / f"{stem}-{ts}.json"

    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(artifact, f, indent=2, ensure_ascii=False)

    return out_path


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
        help="Optional path to agent session manifest JSON",
    )
    parser.add_argument(
        "--redact",
        action="store_true",
        help="Redact sensitive values (paths, keys) from artifact",
    )
    args = parser.parse_args(argv)

    transcript_path = Path(args.transcript)
    manifest_path = Path(args.manifest) if args.manifest else None

    # missing_input チェック
    if not transcript_path.exists():
        print(f"STATUS: 2", flush=True)
        print(f"BLOCKERS: transcript not found: {transcript_path}", file=sys.stderr)
        return 2

    if not transcript_path.is_file():
        print(f"STATUS: 2", flush=True)
        print(f"BLOCKERS: transcript is not a file: {transcript_path}", file=sys.stderr)
        return 2

    # parse
    try:
        result, warnings = parse_transcript(transcript_path, redact=args.redact)
    except RuntimeError as e:
        print(f"STATUS: 3", flush=True)
        print(f"BLOCKERS: parse error: {e}", file=sys.stderr)
        return 3

    # artifact 構築
    artifact = build_artifact(
        transcript_path=transcript_path,
        manifest_path=manifest_path,
        metrics=result,
        event_counts=result["event_counts"],
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
