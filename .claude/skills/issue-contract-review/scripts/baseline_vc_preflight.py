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


def get_issue_body(issue_number: int, repo: str) -> Tuple[Optional[str], Optional[str]]:
    """
    GitHub API から Issue body を取得

    戻り値: (body, error_code)
      error_code: None (成功), "gh_auth_failed", "gh_repo_not_found", "gh_other_error"
    """
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
            return data.get("body"), None
        else:
            stderr_lower = result.stderr.lower()
            if "not authenticated" in stderr_lower or "authentication failed" in stderr_lower:
                return None, "gh_auth_failed"
            elif "not found" in stderr_lower or "could not resolve" in stderr_lower:
                return None, "gh_repo_not_found"
            else:
                return None, "gh_other_error"
    except json.JSONDecodeError:
        return None, "gh_json_parse_error"
    except subprocess.TimeoutExpired:
        return None, "gh_timeout"
    except Exception as e:
        return None, "gh_other_error"


def read_body_file(path: str) -> Tuple[Optional[str], Optional[str]]:
    """
    ファイルから Issue body を読み込み

    戻り値: (body, error_code)
      error_code: None (成功), "body_file_not_found", "body_parse_error"
    """
    try:
        with open(path, "r", encoding="utf-8") as f:
            return f.read(), None
    except FileNotFoundError:
        return None, "body_file_not_found"
    except Exception as e:
        return None, "body_parse_error"


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


def extract_preflight_scope_marker(lines: List[str], target_line_idx: int) -> Optional[str]:
    """
    VC コマンド行（target_line_idx）の直前行から `# preflight-scope: <value>` marker を抽出。

    戻り値: marker value ('pr_review_only' / 'runtime_only' / <invalid-value>) または None

    NB2: If the value does not match the allowed set {pr_review_only, runtime_only},
    it is returned as-is for downstream classification to handle as human_judgment.
    """
    if target_line_idx <= 0:
        return None
    prev_line = lines[target_line_idx - 1].strip()
    match = re.match(r"^\s*#\s*preflight-scope:\s*(\S+)\s*$", prev_line)
    if match:
        return match.group(1)
    return None


def parse_commands_from_block(block: str) -> List[Tuple[Optional[str], str, int, Optional[str]]]:
    """
    bash ブロックからコマンドを抽出。
    AC マーカーとコマンドの行番号と preflight-scope marker を返す。

    戻り値: [(ac_label, command, line_number, preflight_scope), ...]
      - ac_label: "AC1", "AC2", ... または None
      - command: raw command ($ prefix 除去済み、suffix marker 除去済み)
      - line_number: block 内での行番号
      - preflight_scope: 'pr_review_only' / 'runtime_only' / None
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

        # preflight-scope marker はスキップ（コマンドではない）
        if re.match(r"^\s*#\s*preflight-scope:\s*\S+\s*$", line):
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

                # 直前行から preflight-scope marker を抽出
                preflight_scope = extract_preflight_scope_marker(lines, i - 1)

                commands.append((current_ac, cmd, i, preflight_scope))

    return commands


def compute_command_hash(command: str) -> str:
    """コマンドの SHA-256 hash を計算"""
    return hashlib.sha256(command.encode("utf-8")).hexdigest()


def detect_compound_command(command: str) -> bool:
    """
    コマンドが compound shell syntax を含むか検出

    shlex.shlex で正確に tokenize し、shell operator を検出する。
    これにより:
    - cmd&&cmd（空白なし）も検出
    - quoted string 内の | は誤検出しない
    - redirect ( > < >> ) も compound と見なす (fail-closed)
    """
    try:
        # C6: shlex.shlex with punctuation_chars=True for operator detection
        lexer = shlex.shlex(command, posix=True, punctuation_chars=True)
        tokens = list(lexer)
    except ValueError:
        # parse 失敗 = 複雑なコマンド = fail-closed で compound と見なす
        return True

    # shell operators
    operators = {"&&", "||", "|", ";", "&", "<<", "<", ">", ">>", "<<<"}
    return any(t in operators for t in tokens)


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


def truncate_output(text: str, max_lines: int = 20, bytes_per_line: int = 2048) -> Tuple[List[str], bool, int]:
    """
    stdout / stderr を行数とバイト数で切り詰める。

    中リスク 3: truncation 情報を返す。
    戻り値: (lines, was_truncated, original_line_count)
      - lines: truncate されたテキストを行配列で返す（空の場合は []）
      - was_truncated: 行数が max_lines を超えたかどうか
      - original_line_count: 元のテキストの行数
    """
    all_lines = text.split("\n")
    original_line_count = len(all_lines)
    lines = all_lines[:max_lines]
    was_truncated = original_line_count > max_lines

    result = []
    for line in lines:
        # B7: byte-safe truncation
        truncated_line = truncate_line_bytes(line, bytes_per_line)
        if truncated_line or line:  # 中リスク 3: 空行の場合も keep
            result.append(truncated_line)

    # 中リスク 3: 空出力時は [] を返す（[""] ではなく）
    if not result or (len(result) == 1 and result[0] == ""):
        return [], was_truncated, original_line_count

    return result, was_truncated, original_line_count


def _strip_uv_run_options(argv: List[str]) -> List[str]:
    """
    uv run [options...] <cmd...> を unwrap して <cmd...> の argv を返す。

    uv flag (--locked, --with <pkg> など) を取り除く。
    例: ["uv", "run", "--locked", "pytest"] → ["pytest"]
    例: ["uv", "run", "--with", "pytest", "pytest"] → ["pytest"]
    """
    if not argv or argv[0] != "uv":
        return argv

    if len(argv) < 2 or argv[1] != "run":
        return argv

    # argv[2:] から uv flags を取り除く
    result = []
    i = 2
    while i < len(argv):
        arg = argv[i]
        # uv flags that take an argument
        if arg in ("--with", "--extra", "-p", "--python"):
            i += 2  # skip flag and its argument
            continue
        # uv flags that don't take an argument
        if arg.startswith("--"):
            i += 1  # skip flag only
            continue
        # Non-flag argument: this is the start of the command
        result = argv[i:]
        break

    return result if result else argv


def _is_pytest_invocation(command: str) -> bool:
    """
    コマンドが pytest invocation かどうかを argv/token ベースで検出。

    対象パターン:
    - pytest
    - python -m pytest
    - python3 -m pytest
    - uv run pytest
    - uv run --locked pytest
    - uv run --with pytest pytest
    - uv run python -m pytest (等々)

    戻り値: True if pytest invocation, False otherwise
    """
    try:
        argv = shlex.split(command)
    except ValueError:
        # shlex.split 失敗 = compound または複雑なコマンド
        return False

    if not argv:
        return False

    # uv run を unwrap
    unwrapped = _strip_uv_run_options(argv)
    if not unwrapped:
        return False

    # unwrapped の最初の要素の basename が pytest か確認
    first_cmd = unwrapped[0]
    if Path(first_cmd).name == "pytest":
        return True

    # python / python3 -m pytest パターン
    if (
        Path(first_cmd).name in ("python", "python3")
        and len(unwrapped) >= 3
        and unwrapped[1] == "-m"
        and unwrapped[2] == "pytest"
    ):
        return True

    return False


def _is_regression_gate_command(command: str, cwd: Optional[str] = None) -> bool:
    """
    AC4: regression_gate prefix detection.

    Detects: pnpm typecheck / lint / test / build / uv run pytest <existing-path>

    For pytest commands, requires an existing positional path argument.
    Args:
        command: command string to check
        cwd: working directory (defaults to Path.cwd() if None)

    Returns True if command is a regression gate command, False otherwise.
    """
    if cwd is None:
        cwd = str(Path.cwd())

    # Check for exact command prefixes
    prefixes = [
        "pnpm typecheck",
        "pnpm lint",
        "pnpm test",
        "pnpm build",
    ]
    if any(command.startswith(p) for p in prefixes):
        return True

    # Check for uv run pytest with existing test paths
    if "uv run" not in command or "pytest" not in command:
        return False

    # B3: For pytest commands, extract positional path argument
    try:
        argv = shlex.split(command)
    except ValueError:
        return False

    if not argv:
        return False

    # Strip uv run and options to find positional arguments
    unwrapped = _strip_uv_run_options(argv)
    if not unwrapped:
        return False

    # Find pytest invocation
    pytest_idx = -1
    for i, arg in enumerate(unwrapped):
        if Path(arg).name == "pytest":
            pytest_idx = i
            break
        if (
            Path(arg).name in ("python", "python3")
            and i + 2 < len(unwrapped)
            and unwrapped[i + 1] == "-m"
            and unwrapped[i + 2] == "pytest"
        ):
            pytest_idx = i + 2
            break

    if pytest_idx == -1:
        return False

    # B2: Collect positional arguments after pytest (skip options and their values)
    positional_args = []
    i = pytest_idx + 1
    n = len(unwrapped)
    while i < n:
        arg = unwrapped[i]
        # Options that take a separate value
        if arg in ("-k", "-m", "-p", "-W", "--rootdir", "--basetemp", "--maxfail", "--tb",
                   "--lf-name", "--cache-dir", "-c", "-o"):
            i += 2  # skip flag and its argument
            continue
        # Other flags without arguments
        if arg.startswith("-"):
            i += 1
            continue
        # arg is a positional candidate
        positional_args.append(arg)
        i += 1

    # B2: Check if any positional argument exists and is a valid path
    for arg in positional_args:
        # Handle both relative and absolute paths
        if Path(arg).is_absolute():
            test_path = Path(arg)
        else:
            test_path = Path(cwd) / arg
        if test_path.exists():
            return True

    # No valid path found
    return False


def _is_negated_search_command(command: str) -> bool:
    """
    AC6: Detect negated search commands like `! rg -q "pattern" file`.

    Returns True if command starts with `!` and contains rg/grep.
    """
    # Check if command starts with ! (possibly with spaces)
    stripped = command.strip()
    if not stripped.startswith("!"):
        return False
    # Check if rg or grep follows after !
    rest = stripped[1:].strip()
    return any(rest.startswith(util) for util in ["rg", "grep"])


def has_command_substitution(command: str) -> bool:
    """
    B3: Detect command substitution patterns: $(...), `...`, ${...}
    ONLY in unquoted segments.

    Single-quoted segments are excluded — they are literals to subprocess.
    """
    # Scan the original command character by character, tracking quote state.
    # Single-quoted content is literal, double-quoted content may have substitution.
    in_single = False
    in_double = False
    i = 0
    while i < len(command):
        ch = command[i]
        if ch == "'" and not in_double:
            in_single = not in_single
            i += 1
            continue
        if ch == '"' and not in_single:
            in_double = not in_double
            i += 1
            continue
        # Only check for substitution outside of single quotes
        if not in_single:
            # $(...) or ${...} or backtick
            if ch == '$' and i + 1 < len(command) and command[i + 1] in ('(', '{'):
                return True
            if ch == '`':
                return True
        i += 1
    return False


def classify_result(
    exit_code: int,
    stdout: str,
    stderr: str,
    command: str,
    cwd: Optional[str] = None,
) -> Tuple[str, str, str, Optional[str], str]:
    """
    VC 実行結果を分類。

    Args:
        exit_code: command exit code
        stdout: command standard output
        stderr: command standard error
        command: original command string
        cwd: working directory (threaded to _is_regression_gate_command)

    戻り値: (classification, category, decision, fix_hint, scope_class)
      classification: expected_fail | unexpected_pass | blocked | human_judgment | expected_pass | skipped
      category: file_not_found_expected | expected_baseline_fail | unexpected_pass |
                env_missing_dep | file_not_found_unrunnable | timeout |
                compound_command_disallowed | unknown | regression_gate
      decision: go | blocked | human_judgment
      fix_hint: nullable hint
      scope_class: baseline_fail_expected | regression_gate | pr_review_only | runtime_only
    """

    # AC6: negated search commands - static classification (BEFORE run_command)
    if _is_negated_search_command(command):
        return "expected_fail", "expected_baseline_fail", "go", None, "baseline_fail_expected"

    # B2: command substitution detection - static classification without execution
    if has_command_substitution(command):
        return "expected_fail", "expected_baseline_fail", "go", None, "baseline_fail_expected"

    # compound command は blocked (default scope_class)
    if detect_compound_command(command):
        return "blocked", "compound_command_disallowed", "blocked", "Compound shell commands are not supported in initial implementation", "baseline_fail_expected"

    # AC4: regression_gate prefix detection AFTER static checks
    # If it's a regression gate, apply special rules
    if _is_regression_gate_command(command, cwd=cwd):
        if exit_code == 0:
            return "expected_pass", "regression_gate", "go", None, "regression_gate"
        else:
            # B3: Check pytest exit codes 4/5 BEFORE regression_gate failure classification
            if _is_pytest_invocation(command):
                combined_lower = f"{stdout}\n{stderr}".lower()

                # B3: pytest exit 4 + file not found → expected_baseline_fail
                if exit_code == 4 and re.search(r"error:\s+file or directory not found:", combined_lower):
                    return "expected_fail", "expected_baseline_fail", "go", None, "baseline_fail_expected"

                # B3: pytest exit 5 + no tests collected → expected_baseline_fail
                if exit_code == 5 and (
                    "no tests ran" in combined_lower
                    or "no tests collected" in combined_lower
                    or "collected 0 items" in combined_lower
                ):
                    return "expected_fail", "expected_baseline_fail", "go", None, "baseline_fail_expected"

            return "blocked", "regression_gate", "blocked", "Regression gate command failed", "regression_gate"

    # timeout check
    if "timeout" in stderr.lower():
        return "blocked", "timeout", "blocked", "Command exceeded timeout", "baseline_fail_expected"

    # exit_code = 0 で回帰ゲート以外 → unexpected_pass / blocked
    if exit_code == 0:
        return "unexpected_pass", "unexpected_pass", "blocked", "Command unexpectedly passed", "baseline_fail_expected"

    # shlex.split failed
    if "shlex.split failed" in stderr:
        return "blocked", "compound_command_disallowed", "blocked", "Command syntax is not supported", "baseline_fail_expected"

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
        return "blocked", "file_not_found_unrunnable", "blocked", "Script or file being executed does not exist", "baseline_fail_expected"

    # env_missing_dep: command not found (127), permission denied (126), ModuleNotFoundError, etc.
    if exit_code in (126, 127):
        return "blocked", "env_missing_dep", "blocked", "Command not found or permission denied", "baseline_fail_expected"

    if "command not found" in stderr.lower() or "ModuleNotFoundError" in stderr:
        return "blocked", "env_missing_dep", "blocked", "Dependency or command missing", "baseline_fail_expected"

    if "Permission denied" in stderr:
        return "blocked", "env_missing_dep", "blocked", "Permission denied", "baseline_fail_expected"

    if "No such file or directory" in stderr and exit_code == -1:
        return "blocked", "env_missing_dep", "blocked", "Command not found", "baseline_fail_expected"

    # pytest baseline fail patterns (AC2, AC3)
    if _is_pytest_invocation(command):
        combined_lower = f"{stdout}\n{stderr}".lower()

        # AC2: pytest exit 4 + file not found → expected_baseline_fail
        if exit_code == 4 and re.search(r"error:\s+file or directory not found:", combined_lower):
            return "expected_fail", "expected_baseline_fail", "go", None, "baseline_fail_expected"

        # AC3: pytest exit 5 + no tests collected → expected_baseline_fail
        if exit_code == 5 and (
            "no tests ran" in combined_lower
            or "no tests collected" in combined_lower
            or "collected 0 items" in combined_lower
        ):
            return "expected_fail", "expected_baseline_fail", "go", None, "baseline_fail_expected"

    # expected baseline fail patterns
    # rg with no match returns 1
    if "rg " in command and exit_code == 1:
        return "expected_fail", "expected_baseline_fail", "go", None, "baseline_fail_expected"

    # test -f / test -d with non-existent file
    if ("test -f " in command or "test -d " in command) and exit_code == 1:
        return "expected_fail", "file_not_found_expected", "go", None, "baseline_fail_expected"

    # Generic exit_code != 0: try to infer expected_fail for common utilities
    # grep, sed, awk などが no-match で exit 1 を返すことは expected
    # ただし invalid regex や file not found は exclude (medium risk 1)
    if exit_code == 1 and any(util in command for util in ["grep", "rg", "ag", "ack", "fgrep", "egrep"]):
        # grep が invalid regex で fail した場合 expected_fail にしない
        # "grep:" error pattern を検出
        grep_error_patterns = [
            r"grep:.+: No such file or directory",
            r"grep:.+: Invalid regular expression",
            r"egrep:.+",
            r"fgrep:.+"
        ]
        is_likely_grep_error = any(re.search(pattern, stderr) for pattern in grep_error_patterns)
        if is_likely_grep_error:
            return "human_judgment", "unknown", "human_judgment", "grep error classification uncertain", "baseline_fail_expected"
        return "expected_fail", "expected_baseline_fail", "go", None, "baseline_fail_expected"

    # Unknown: cannot classify
    return "human_judgment", "unknown", "human_judgment", "Unable to automatically classify exit code", "baseline_fail_expected"


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


def check_c13_vc_preflight_decision_consistency(classifications: List[Dict[str, Any]]) -> Tuple[bool, List[str]]:
    """
    B4: Validate classifications for schema consistency.

    Checks:
    - All required fields present (classification, scope_class, decision)
    - Enum values valid
    - Cross-field consistency (e.g., skipped requires scope_class pr_review_only or runtime_only)

    Returns: (is_valid, list_of_failures)
    """
    VALID_SCOPE_CLASSES = {"baseline_fail_expected", "regression_gate", "pr_review_only", "runtime_only"}
    VALID_CLASSIFICATIONS = {"expected_fail", "expected_pass", "unexpected_pass", "blocked", "human_judgment", "skipped"}
    VALID_DECISIONS = {"go", "blocked", "human_judgment"}

    failures = []

    for i, item in enumerate(classifications):
        prefix = f"classifications[{i}]"

        # Required fields
        if "classification" not in item:
            failures.append(f"{prefix}: missing classification")
        if "scope_class" not in item:
            failures.append(f"{prefix}: missing scope_class")
        if "decision" not in item:
            failures.append(f"{prefix}: missing decision")

        # Enum validation
        if "scope_class" in item and item["scope_class"] not in VALID_SCOPE_CLASSES:
            failures.append(f"{prefix}: invalid scope_class '{item['scope_class']}' (valid: {VALID_SCOPE_CLASSES})")
        if "classification" in item and item["classification"] not in VALID_CLASSIFICATIONS:
            failures.append(f"{prefix}: invalid classification '{item['classification']}' (valid: {VALID_CLASSIFICATIONS})")
        if "decision" in item and item["decision"] not in VALID_DECISIONS:
            failures.append(f"{prefix}: invalid decision '{item['decision']}' (valid: {VALID_DECISIONS})")

        # Cross-field consistency rules
        if "classification" in item and "scope_class" in item:
            # skipped requires scope_class pr_review_only or runtime_only
            if item["classification"] == "skipped" and item["scope_class"] not in {"pr_review_only", "runtime_only"}:
                failures.append(f"{prefix}: skipped requires scope_class pr_review_only or runtime_only")

        # regression_gate consistency
        if "scope_class" in item and item["scope_class"] == "regression_gate":
            if "decision" in item and "classification" in item:
                if item["decision"] == "go" and item["classification"] != "expected_pass":
                    failures.append(f"{prefix}: regression_gate + go requires classification expected_pass")
                if item["decision"] == "blocked" and item["classification"] != "blocked":
                    failures.append(f"{prefix}: regression_gate + blocked requires classification blocked")

        # Skipped routing metadata
        if "classification" in item and item["classification"] == "skipped":
            for k in ("verification_owner", "deferred_reason", "runtime_verification_required"):
                if k not in item:
                    failures.append(f"{prefix}: skipped requires {k}")

    return len(failures) == 0, failures


def generate_contract_review_fragment(status: str, results: List[Dict[str, Any]]) -> Dict[str, Any]:
    """
    B8: JSON results を CONTRACT_REVIEW_RESULT_V1.checks.vc_preflight に対応する YAML fragment に変換.

    C3: human_judgment を保持する。vc_preflight.passed は status == "pass" のときのみ true。

    B1: Each classification item includes:
      - classification (always)
      - scope_class (always)
      - For skipped items: verification_owner, deferred_reason, runtime_verification_required

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
    vc_human_judgment = sum(
        1 for r in results if r["decision"] == "human_judgment"
    )
    vc_expected_pass = sum(
        1 for r in results if r["classification"] == "expected_pass"
    )
    vc_skipped = sum(
        1 for r in results if r["classification"] == "skipped"
    )

    classifications = []
    for r in results:
        stdout_lines = r["stdout_head"]
        stderr_lines = r["stderr_head"]

        # C3: decision をそのまま渡す（human_judgment を保持）
        decision = r["decision"]

        # B1: classification and scope_class are always included
        classification_item = {
            "ac": r["ac"],
            "command": r["raw_command"],
            "exit_code": r["exit_code"],
            "classification": r["classification"],
            "category": r["category"],
            "confidence": compute_confidence(r["category"]),
            "scope_class": r["scope_class"],
            "evidence": {
                "stdout_excerpt": " ".join(stdout_lines[:5]) if stdout_lines else "",
                "stderr_excerpt": " ".join(stderr_lines[:5]) if stderr_lines else "",
            },
            "decision": decision,
        }

        # B1: For skipped items, add routing metadata
        if r["classification"] == "skipped":
            if "verification_owner" in r:
                classification_item["verification_owner"] = r["verification_owner"]
            if "deferred_reason" in r:
                classification_item["deferred_reason"] = r["deferred_reason"]
            if "runtime_verification_required" in r:
                classification_item["runtime_verification_required"] = r["runtime_verification_required"]

        classifications.append(classification_item)

    return {
        "vc_preflight": {
            "passed": status == "pass",
            "vc_failed_as_expected": vc_failed_as_expected,
            "vc_passed_unexpectedly": vc_passed_unexpectedly,
            "vc_unrunnable": vc_unrunnable,
            "vc_human_judgment": vc_human_judgment,
            "vc_expected_pass": vc_expected_pass,
            "vc_skipped": vc_skipped,
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

    # Issue body を取得 (C2: error code と一緒に返す)
    body = None
    source_kind = None
    error_code = None

    if args.body_file:
        body, error_code = read_body_file(args.body_file)
        source_kind = "body_file"
    elif args.issue:
        body, error_code = get_issue_body(args.issue, args.repo)
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
            "errors": [error_code or "failed_to_retrieve_issue_body"],
        }
        print(json.dumps(result, indent=2))
        # C2: exit code 2 for retrieval/parse errors
        return 2 if error_code else 2

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
        # C2: exit code 2 for extraction errors
        return 2

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
        # C2: exit code 2 for extraction errors
        return 2

    # 各コマンドを実行して分類
    results = []
    summary = {
        "expected_fail": 0,
        "unexpected_pass": 0,
        "blocked": 0,
        "human_judgment": 0,
        "expected_pass": 0,
        "skipped": 0,
        "extraction_errors": 0,
    }

    for ac_label, command, line_no, preflight_scope in commands:
        # AC5: Handle pr_review_only / runtime_only preflight-scope markers
        # NB2: Invalid marker values (typos) → human_judgment
        if preflight_scope is not None:
            if preflight_scope in ("pr_review_only", "runtime_only"):
                classification = "skipped"
                decision = "go"
                category = f"preflight_scope_{preflight_scope}"
                exit_code = None
                stdout, stderr = "", ""
                duration_ms = 0
                fix_hint = None
                scope_class = preflight_scope
                # Routing metadata for skipped results
                verification_owner = "pr-review-judge" if preflight_scope == "pr_review_only" else "impl-review-loop"
                deferred_reason = (
                    "VC marked pr_review_only; verification deferred to PR review"
                    if preflight_scope == "pr_review_only"
                    else "VC marked runtime_only; verification deferred to post-implementation runtime"
                )
                runtime_verification_required = preflight_scope == "runtime_only"
            else:
                # NB2: Invalid preflight-scope marker value
                classification = "human_judgment"
                decision = "human_judgment"
                category = "unknown"
                exit_code = None
                stdout, stderr = "", ""
                duration_ms = 0
                fix_hint = f"Unknown preflight-scope marker value '{preflight_scope}'; expected pr_review_only or runtime_only"
                scope_class = "baseline_fail_expected"
                verification_owner = None
                deferred_reason = None
                runtime_verification_required = None
        else:
            # B2: Static classification checks BEFORE run_command (CRITICAL)
            # Check for command substitution and negated search BEFORE execution
            if has_command_substitution(command):
                exit_code, stdout, stderr, duration_ms = None, "", "", 0
                classification, category, decision, fix_hint, scope_class = (
                    "expected_fail",
                    "expected_baseline_fail",
                    "go",
                    None,
                    "baseline_fail_expected",
                )
            elif _is_negated_search_command(command):
                exit_code, stdout, stderr, duration_ms = None, "", "", 0
                classification, category, decision, fix_hint, scope_class = (
                    "expected_fail",
                    "expected_baseline_fail",
                    "go",
                    None,
                    "baseline_fail_expected",
                )
            elif detect_compound_command(command):
                exit_code, stdout, stderr, duration_ms = None, "", "", 0
                classification, category, decision, fix_hint, scope_class = (
                    "blocked",
                    "compound_command_disallowed",
                    "blocked",
                    "Compound shell commands are not supported in baseline_vc_preflight/v1",
                    "baseline_fail_expected",
                )
            else:
                # B2: Only run command if not statically classified
                exit_code, stdout, stderr, duration_ms = run_command(
                    command, args.timeout_seconds, args.cwd
                )

                classification, category, decision, fix_hint, scope_class = classify_result(
                    exit_code, stdout, stderr, command, cwd=args.cwd
                )

            verification_owner = None
            deferred_reason = None
            runtime_verification_required = None

        # C4: confidence を compute_confidence 経由で統一
        stdout_head, stdout_truncated, stdout_orig_count = truncate_output(stdout, args.max_head_lines)
        stderr_head, stderr_truncated, stderr_orig_count = truncate_output(stderr, args.max_head_lines)

        result_item = {
            "ac": ac_label or "AC_UNKNOWN",
            "line": line_no,
            "raw_command": command,
            "command_hash": f"sha256:{compute_command_hash(command)}",
            "runner": "exec" if preflight_scope is None else "skipped",
            "exit_code": exit_code,
            "classification": classification,
            "category": category,
            "decision": decision,
            "scope_class": scope_class,
            "confidence": compute_confidence(category),  # C4: category ベースで統一
            "stdout_head": stdout_head,
            "stdout_truncated": stdout_truncated,
            "stdout_original_line_count": stdout_orig_count,
            "stderr_head": stderr_head,
            "stderr_truncated": stderr_truncated,
            "stderr_original_line_count": stderr_orig_count,
            "duration_ms": duration_ms,
            "fix_hint": fix_hint,
        }
        # AC5: Add routing metadata for skipped results
        if verification_owner:
            result_item["verification_owner"] = verification_owner
            result_item["deferred_reason"] = deferred_reason
            result_item["runtime_verification_required"] = runtime_verification_required

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
        # C1: lazy import yaml - only when needed for fragment output
        try:
            import yaml
        except ImportError:
            error_result = {
                "schema": "baseline_vc_preflight/v1",
                "issue": args.issue or 0,
                "repo": args.repo,
                "generated_at": datetime.now(timezone.utc).isoformat().replace('+00:00', 'Z'),
                "source": {
                    "kind": source_kind,
                    "body_sha256": f"sha256:{compute_source_hash(body)}",
                },
                "status": "blocked",
                "summary": summary,
                "results": results,
                "errors": ["PyYAML is required for contract-review-fragment format; install pyyaml"],
            }
            print(json.dumps(error_result, indent=2))
            # C2: exit code 2 for missing dependency
            return 2
        fragment = generate_contract_review_fragment(status, results)
        print(yaml.dump(fragment, default_flow_style=False))
    else:
        print(json.dumps(output, indent=2))

    # C2: Exit code contract
    # status: pass → 0, blocked → 1, human_judgment → 3
    if status == "pass":
        return 0
    elif status == "blocked":
        return 1
    elif status == "human_judgment":
        return 3
    else:
        return 0  # default fallback


if __name__ == "__main__":
    sys.exit(main())
