#!/usr/bin/env python3
"""
Baseline Verification Command Preflight

Issue body の `## Verification Commands` セクションから VC を AC 別に抽出して単体実行し、
root-cause 分類（expected_fail / unexpected_pass / blocked / human_judgment）と
category / decision / confidence を含む JSON を返す。
"""

import argparse
import hashlib
import json
import re
import shlex
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import yaml


def get_issue_body(issue_number: int, repo: str) -> Optional[str]:
    """GitHub API から Issue body を取得"""
    try:
        result = subprocess.run(
            [
                "gh",
                "issue",
                "view",
                str(issue_number),
                "--repo",
                repo,
                "--json",
                "body",
            ],
            capture_output=True,
            text=True,
            timeout=10,
        )
        if result.returncode == 0:
            data = json.loads(result.stdout)
            return data.get("body")
    except Exception as e:
        pass
    return None


def read_body_file(path: str) -> Optional[str]:
    """ファイルから Issue body を読み込み"""
    try:
        with open(path, "r", encoding="utf-8") as f:
            return f.read()
    except Exception:
        return None


def extract_verification_commands_section(body: str) -> Optional[str]:
    """body から `## Verification Commands` セクションを抽出"""
    match = re.search(
        r"^##\s+Verification Commands\s*$(.+?)(?=^##|\Z)",
        body,
        re.MULTILINE | re.DOTALL,
    )
    if match:
        return match.group(1)
    return None


def extract_fenced_bash_blocks(section: str) -> List[str]:
    """セクションから ```bash ... ``` ブロックを抽出"""
    blocks = []
    for match in re.finditer(r"```(?:bash)?\s*\n(.*?)\n```", section, re.DOTALL):
        blocks.append(match.group(1))
    return blocks


def parse_commands_from_block(block: str) -> List[Tuple[Optional[str], str, int]]:
    """
    bash ブロックからコマンドを抽出。
    AC マーカーとコマンドの行番号を返す。

    戻り値: [(ac_label, command, line_number), ...]
      - ac_label: "AC1", "AC2", ... または None
      - command: raw command ($ prefix 除去済み、suffix marker 除去済み)
      - line_number: block 内での行番号
    """
    commands = []
    lines = block.split("\n")
    current_ac = None

    for i, line in enumerate(lines, start=1):
        # AC マーカーの抽出: `# AC<N>` または `# AC<N>:` (単独コメント行)
        ac_match = re.match(r"^\s*#\s*AC(\d+)\s*:?\s*$", line)
        if ac_match:
            current_ac = f"AC{ac_match.group(1)}"
            continue

        # コマンド行の抽出（$ prefix 除去）
        cmd_match = re.match(r"^\s*\$\s+(.+)$", line)
        if not cmd_match:
            # $ がない行でも、$ でなく非コメント・非空行の場合は取得
            stripped = line.strip()
            if stripped and not stripped.startswith("#"):
                cmd_match = re.match(r"^\s*(.+)$", line)

        if cmd_match:
            cmd = cmd_match.group(1).strip()
            if cmd and not cmd.startswith("#"):
                # B4: inline suffix `# AC<N>` を検出して ac_label を上書き、suffix を除去
                suffix_match = re.search(r"\s+#\s*AC(\d+)\s*:?\s*$", cmd)
                if suffix_match:
                    current_ac = f"AC{suffix_match.group(1)}"
                    cmd = re.sub(r"\s+#\s*AC\d+\s*:?\s*$", "", cmd).strip()

                commands.append((current_ac, cmd, i))

    return commands


def compute_command_hash(command: str) -> str:
    """コマンドの SHA-256 hash を計算"""
    return hashlib.sha256(command.encode("utf-8")).hexdigest()


def detect_compound_command(command: str) -> bool:
    """コマンドが compound shell syntax を含むか検出"""
    # && || | ; または heredoc を検出
    # shlex.split で parse できるかで判定（実際の parse に委譲）
    compound_indicators = [r'\s&&\s', r'\s\|\|\s', r'\s\|\s', r';\s', r'<<']
    for indicator in compound_indicators:
        if re.search(indicator, command):
            return True
    return False


def run_command(command: str, timeout_seconds: int, cwd: str) -> Tuple[int, str, str, int]:
    """
    コマンドを単体実行。

    戻り値: (exit_code, stdout, stderr, duration_ms)
    """
    try:
        # shlex.split で argv を構築（shell=False で安全に実行）
        argv = shlex.split(command)
    except ValueError:
        # shlex.split に失敗した場合は compound command の可能性
        return -1, "", "shlex.split failed", 0

    start = datetime.now()
    try:
        result = subprocess.run(
            argv,
            capture_output=True,
            text=True,
            timeout=timeout_seconds,
            cwd=cwd,
            shell=False,
        )
        duration_ms = int((datetime.now() - start).total_seconds() * 1000)
        return result.returncode, result.stdout, result.stderr, duration_ms
    except subprocess.TimeoutExpired:
        duration_ms = int((datetime.now() - start).total_seconds() * 1000)
        return -1, "", "timeout", duration_ms
    except Exception as e:
        duration_ms = int((datetime.now() - start).total_seconds() * 1000)
        return -1, "", str(e), duration_ms


def truncate_line_bytes(line: str, max_bytes: int) -> str:
    """
    単一行を byte 単位で切り詰める。

    戻り値: byte で切り詰められた行（UTF-8 safe）
    """
    raw = line.encode("utf-8")
    if len(raw) <= max_bytes:
        return line
    return raw[:max_bytes].decode("utf-8", errors="replace")


def truncate_output(text: str, max_lines: int = 20, bytes_per_line: int = 2048) -> List[str]:
    """
    stdout / stderr を行数とバイト数で切り詰める。

    戻り値: リスト形式の行（JSON 配列化用）
    """
    lines = text.split("\n")[:max_lines]
    result = []
    for line in lines:
        # B7: byte-safe truncation
        result.append(truncate_line_bytes(line, bytes_per_line))
    return result


def classify_result(
    exit_code: int,
    stdout: str,
    stderr: str,
    command: str,
) -> Tuple[str, str, str, Optional[str]]:
    """
    VC 実行結果を分類。

    戻り値: (classification, category, decision, fix_hint)
      classification: expected_fail | unexpected_pass | blocked | human_judgment
      category: file_not_found_expected | expected_baseline_fail | unexpected_pass |
                env_missing_dep | file_not_found_unrunnable | timeout |
                compound_command_disallowed | unknown
      decision: go | blocked | human_judgment
      fix_hint: nullable hint
    """

    # compound command は blocked
    if detect_compound_command(command):
        return "blocked", "compound_command_disallowed", "blocked", "Compound shell commands are not supported in initial implementation"

    # timeout check
    if "timeout" in stderr.lower():
        return "blocked", "timeout", "blocked", "Command exceeded timeout"

    # exit_code = 0 は unexpected_pass / blocked
    if exit_code == 0:
        return "unexpected_pass", "unexpected_pass", "blocked", "Command unexpectedly passed"

    # shlex.split failed
    if "shlex.split failed" in stderr:
        return "blocked", "compound_command_disallowed", "blocked", "Command syntax is not supported"

    # B5: file_not_found_unrunnable - missing script/file being executed
    # e.g., python3 missing.py, node missing.js, ./missing-script
    if (
        ("No such file or directory" in stderr or "can't open file" in stderr)
        and exit_code == 2
        and any(
            cmd_pattern in command
            for cmd_pattern in ["python3 ", "python ", "node ", "./", "../"]
        )
    ):
        return "blocked", "file_not_found_unrunnable", "blocked", "Script or file being executed does not exist"

    # env_missing_dep: command not found (127), permission denied (126), ModuleNotFoundError, etc.
    if exit_code in (126, 127):
        return "blocked", "env_missing_dep", "blocked", "Command not found or permission denied"

    if "command not found" in stderr.lower() or "ModuleNotFoundError" in stderr:
        return "blocked", "env_missing_dep", "blocked", "Dependency or command missing"

    if "Permission denied" in stderr:
        return "blocked", "env_missing_dep", "blocked", "Permission denied"

    if "No such file or directory" in stderr and exit_code == -1:
        return "blocked", "env_missing_dep", "blocked", "Command not found"

    # expected baseline fail patterns
    # rg with no match returns 1
    if "rg " in command and exit_code == 1:
        return "expected_fail", "expected_baseline_fail", "go", None

    # test -f / test -d with non-existent file
    if ("test -f " in command or "test -d " in command) and exit_code == 1:
        return "expected_fail", "file_not_found_expected", "go", None

    # Generic exit_code != 0: try to infer expected_fail for common utilities
    # grep, sed, awk などが no-match で exit 1 を返すことは expected
    if exit_code == 1 and any(util in command for util in ["grep", "rg", "ag", "ack"]):
        return "expected_fail", "expected_baseline_fail", "go", None

    # Unknown: cannot classify
    return "human_judgment", "unknown", "human_judgment", "Unable to automatically classify exit code"


def compute_source_hash(body: str) -> str:
    """body の SHA-256 hash を計算"""
    return hashlib.sha256(body.encode("utf-8")).hexdigest()


def compute_confidence(category: str) -> str:
    """
    B8: category に基づいて confidence を算出.

    高確度: compound_command_disallowed, file_not_found_expected, expected_baseline_fail, env_missing_dep, file_not_found_unrunnable
    中確度: timeout, unexpected_pass
    低確度: unknown
    """
    high_confidence = {
        "compound_command_disallowed",
        "file_not_found_expected",
        "expected_baseline_fail",
        "env_missing_dep",
        "file_not_found_unrunnable",
    }
    medium_confidence = {"timeout", "unexpected_pass"}

    if category in high_confidence:
        return "high"
    elif category in medium_confidence:
        return "medium"
    else:
        return "low"


def generate_contract_review_fragment(status: str, results: List[Dict[str, Any]]) -> Dict[str, Any]:
    """
    B8: JSON results を CONTRACT_REVIEW_RESULT_V1.checks.vc_preflight に対応する YAML fragment に変換.

    戻り値は YAML 形式の dict.
    """
    vc_failed_as_expected = sum(
        1 for r in results if r["classification"] == "expected_fail"
    )
    vc_passed_unexpectedly = sum(
        1 for r in results if r["classification"] == "unexpected_pass"
    )
    vc_unrunnable = sum(
        1 for r in results if r["classification"] == "blocked"
    )

    classifications = []
    for r in results:
        stdout_lines = r["stdout_head"]
        stderr_lines = r["stderr_head"]

        classification_item = {
            "ac": r["ac"],
            "command": r["raw_command"],
            "exit_code": r["exit_code"],
            "category": r["category"],
            "confidence": compute_confidence(r["category"]),
            "evidence": {
                "stdout_excerpt": " ".join(stdout_lines[:5]) if stdout_lines else "",
                "stderr_excerpt": " ".join(stderr_lines[:5]) if stderr_lines else "",
            },
            "decision": "go" if r["decision"] == "go" else "blocked",
        }
        classifications.append(classification_item)

    return {
        "vc_preflight": {
            "passed": status == "pass",
            "vc_failed_as_expected": vc_failed_as_expected,
            "vc_passed_unexpectedly": vc_passed_unexpectedly,
            "vc_unrunnable": vc_unrunnable,
            "classifications": classifications,
        }
    }


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Baseline VC Preflight: extract and classify VCs from Issue body"
    )
    parser.add_argument("--issue", type=int, help="GitHub Issue number")
    parser.add_argument("--repo", default="squne121/loop-protocol", help="GitHub repo (owner/name)")
    parser.add_argument("--body-file", help="Path to Issue body file (for testing)")
    parser.add_argument("--cwd", default=".", help="Working directory for command execution")
    parser.add_argument("--timeout-seconds", type=int, default=30, help="Timeout per command")
    parser.add_argument("--max-head-lines", type=int, default=20, help="Max lines for stdout/stderr")
    # B8: contract-review-fragment format support
    parser.add_argument(
        "--format",
        choices=["json", "contract-review-fragment"],
        default="json",
        help="Output format (json or contract-review-fragment YAML)",
    )

    args = parser.parse_args()

    # Issue body を取得
    body = None
    source_kind = None

    if args.body_file:
        body = read_body_file(args.body_file)
        source_kind = "body_file"
    elif args.issue:
        body = get_issue_body(args.issue, args.repo)
        source_kind = "github_issue"

    if not body:
        result = {
            "schema": "baseline_vc_preflight/v1",
            "issue": args.issue or 0,
            "repo": args.repo,
            "generated_at": datetime.now(timezone.utc).isoformat().replace('+00:00', 'Z'),
            "source": {"kind": "unknown", "body_sha256": ""},
            "status": "blocked",
            "summary": {
                "expected_fail": 0,
                "unexpected_pass": 0,
                "blocked": 0,
                "human_judgment": 0,
                "extraction_errors": 1,
            },
            "results": [],
            "errors": ["Failed to retrieve Issue body"],
        }
        print(json.dumps(result, indent=2))
        return 0

    # Verification Commands セクションを抽出
    vc_section = extract_verification_commands_section(body)
    if not vc_section:
        result = {
            "schema": "baseline_vc_preflight/v1",
            "issue": args.issue or 0,
            "repo": args.repo,
            "generated_at": datetime.now(timezone.utc).isoformat().replace('+00:00', 'Z'),
            "source": {
                "kind": source_kind,
                "body_sha256": f"sha256:{compute_source_hash(body)}",
            },
            "status": "blocked",
            "summary": {
                "expected_fail": 0,
                "unexpected_pass": 0,
                "blocked": 0,
                "human_judgment": 0,
                "extraction_errors": 1,
            },
            "results": [],
            "errors": ["Verification Commands section not found"],
        }
        print(json.dumps(result, indent=2))
        return 0

    # bash ブロックからコマンドを抽出
    blocks = extract_fenced_bash_blocks(vc_section)
    commands = []
    for block in blocks:
        commands.extend(parse_commands_from_block(block))

    # B3: 0 件抽出は blocked として返す
    if not commands:
        result = {
            "schema": "baseline_vc_preflight/v1",
            "issue": args.issue or 0,
            "repo": args.repo,
            "generated_at": datetime.now(timezone.utc).isoformat().replace('+00:00', 'Z'),
            "source": {
                "kind": source_kind,
                "body_sha256": f"sha256:{compute_source_hash(body)}",
            },
            "status": "blocked",
            "summary": {
                "expected_fail": 0,
                "unexpected_pass": 0,
                "blocked": 0,
                "human_judgment": 0,
                "extraction_errors": 1,
            },
            "results": [],
            "errors": ["No verification commands extracted from Verification Commands section"],
        }
        print(json.dumps(result, indent=2))
        return 0

    # 各コマンドを実行して分類
    results = []
    summary = {
        "expected_fail": 0,
        "unexpected_pass": 0,
        "blocked": 0,
        "human_judgment": 0,
        "extraction_errors": 0,
    }

    for ac_label, command, line_no in commands:
        # B1: Detect compound command BEFORE running
        if detect_compound_command(command):
            exit_code, stdout, stderr, duration_ms = None, "", "", 0
            classification, category, decision, fix_hint = (
                "blocked",
                "compound_command_disallowed",
                "blocked",
                "Compound shell commands are not supported in baseline_vc_preflight/v1",
            )
        else:
            exit_code, stdout, stderr, duration_ms = run_command(
                command, args.timeout_seconds, args.cwd
            )

            classification, category, decision, fix_hint = classify_result(
                exit_code, stdout, stderr, command
            )

        result_item = {
            "ac": ac_label or "AC_UNKNOWN",
            "line": line_no,
            "raw_command": command,
            "command_hash": f"sha256:{compute_command_hash(command)}",
            "runner": "exec",
            "exit_code": exit_code,
            "classification": classification,
            "category": category,
            "decision": decision,
            "confidence": "high" if decision != "human_judgment" else "medium",
            "stdout_head": truncate_output(stdout, args.max_head_lines),
            "stderr_head": truncate_output(stderr, args.max_head_lines),
            "duration_ms": duration_ms,
            "fix_hint": fix_hint,
        }
        results.append(result_item)
        summary[classification] += 1

    # B2: status 優先順位を blocked > human_judgment > pass にする
    status = "pass"
    if not results:
        status = "blocked"
    elif any(r["decision"] == "blocked" for r in results):
        status = "blocked"
    elif any(r["decision"] == "human_judgment" for r in results):
        status = "human_judgment"

    output = {
        "schema": "baseline_vc_preflight/v1",
        "issue": args.issue or 0,
        "repo": args.repo,
        "generated_at": datetime.now(timezone.utc).isoformat().replace('+00:00', 'Z'),
        "source": {
            "kind": source_kind,
            "body_sha256": f"sha256:{compute_source_hash(body)}",
        },
        "status": status,
        "summary": summary,
        "results": results,
        "errors": [],
    }

    # B8: Output format selection
    if args.format == "contract-review-fragment":
        fragment = generate_contract_review_fragment(status, results)
        print(yaml.dump(fragment, default_flow_style=False))
    else:
        print(json.dumps(output, indent=2))

    return 0


if __name__ == "__main__":
    sys.exit(main())
