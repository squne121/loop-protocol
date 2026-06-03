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
    """
    セクションから ```bash ... ``` ブロックを抽出。

    B4: canonical format は ```bash のみ。unlabeled fence (```) は無視する。
    """
    blocks = []
    for match in re.finditer(r"```bash[ \t]*\n(.*?)```", section, re.DOTALL):
        blocks.append(match.group(1).rstrip("\n"))
    return blocks


def find_unlabeled_fenced_blocks(section: str) -> List[str]:
    """
    B4: unlabeled fence (``` without language specifier) を検出。

    戻り値: unlabeled fence の内容リスト（警告用）
    """
    unlabeled = []
    for match in re.finditer(r"```[ \t]*\n(.*?)```", section, re.DOTALL):
        unlabeled.append(match.group(1).rstrip("\n"))
    return unlabeled


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


def extract_vc_regex_intent_annotation(lines: List[str], target_line_idx: int) -> Optional[str]:
    """
    VC コマンド行（target_line_idx）の直前の連続 annotation/comment ブロックから
    `# vc-regex-intent: <value>` annotation を抽出。

    AC3 (Issue #589): backslash-pipe (\\|) を含む regex-bearing command（rg / egrep 等）に対して、
    `literal-pipe-ok` annotation が付与されている場合は regex_literal_pipe_suspected を免除する。

    形式: `# vc-regex-intent: literal-pipe-ok reason="..."`
    戻り値: annotation value（"literal-pipe-ok" 等）または None

    スコープルール（Blocker 1 修正）:
    - annotation は VC コマンド行の直前の連続 annotation/comment ブロック内のみ有効。
    - 途中に空行・$ コマンド行・通常コメントではない行があった時点でブロックを打ち切る。
    - `# preflight-scope:` は同一ブロック内として透過する（coexistence を許す）。
    - 空行や $ コマンド行（コマンド行）を跨ぐことはない。
    """
    found_annotation = None
    # Walk backwards from the line immediately before target_line_idx
    for offset in range(1, target_line_idx + 1):
        line_idx = target_line_idx - offset
        if line_idx < 0:
            break
        line = lines[line_idx].strip()

        # Empty line: stop scanning (annotation scope ended)
        if not line:
            break

        # $ command line: stop scanning (another command intervened)
        if re.match(r"^\$\s+", line) or re.match(r"^\$\s*$", line):
            break

        # vc-regex-intent annotation line: record it and continue scanning the block
        match = re.match(r"^#\s*vc-regex-intent:\s*(\S+)", line)
        if match:
            found_annotation = match.group(1)
            continue

        # preflight-scope marker: transparent (allowed in the same block)
        if re.match(r"^#\s*preflight-scope:\s*\S+", line):
            continue

        # AC marker line (# AC1 etc): transparent (allowed in the same block)
        if re.match(r"^#\s*AC\d+\s*:?\s*$", line):
            continue

        # Any other line (regular comment or non-comment non-command): stop scanning
        break

    return found_annotation


def parse_commands_from_block(block: str) -> List[Tuple[Optional[str], str, int, Optional[str], Optional[str]]]:
    """
    bash ブロックからコマンドを抽出。
    AC マーカーとコマンドの行番号と preflight-scope marker と vc-regex-intent annotation を返す。

    戻り値: [(ac_label, command, line_number, preflight_scope, vc_regex_intent), ...]
      - ac_label: "AC1", "AC2", ... または None
      - command: raw command ($ prefix 除去済み、suffix marker 除去済み)
      - line_number: block 内での行番号
      - preflight_scope: 'pr_review_only' / 'runtime_only' / None
      - vc_regex_intent: 'literal-pipe-ok' / None (AC3: Issue #589)
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

        # vc-regex-intent annotation はスキップ（コマンドではない）
        if re.match(r"^\s*#\s*vc-regex-intent:\s*\S+", line):
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

                # 直前行から vc-regex-intent annotation を抽出 (AC3: Issue #589)
                vc_regex_intent = extract_vc_regex_intent_annotation(lines, i - 1)

                commands.append((current_ac, cmd, i, preflight_scope, vc_regex_intent))

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

    # B1: Check for exact pnpm subcommand allowlist (argv-based, not prefix string)
    try:
        argv_check = shlex.split(command)
    except ValueError:
        argv_check = []
    if (
        argv_check
        and Path(argv_check[0]).name == "pnpm"
        and len(argv_check) >= 2
        and ("pnpm", argv_check[1].lower()) in _ALLOWED_PNPM_SUBCOMMANDS
    ):
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


def _is_discovery_script(argv: List[str]) -> bool:
    """
    Detect if a command invokes a repository-local ssot-discovery script.

    Discovery scripts are identified by:
    - basename being 'match-ssot.sh' or 'match_ssot.py'
    - OR the script path containing 'ssot-discovery/scripts/'

    Handles invocation forms:
    - bash .claude/skills/ssot-discovery/scripts/match-ssot.sh ...
    - .claude/skills/ssot-discovery/scripts/match-ssot.sh ... (direct)
    - python3 .../match_ssot.py ...
    - uv run python3 .../match_ssot.py ...
    """
    if not argv:
        return False

    # Unwrap 'bash' / 'sh' prefix: argv[0] is shell, look for script in rest
    leading = argv[0]
    script_candidates = []

    cmd_basename = Path(leading).name
    if cmd_basename in ("bash", "sh", "zsh"):
        # Script is first non-option argument after shell invocator
        script_candidates = [a for a in argv[1:] if not a.startswith("-")]
    elif cmd_basename in ("python", "python3"):
        # python3 .../match_ssot.py ... or python3 -m <module>
        # For file-based invocation, script is first non-option positional arg
        script_candidates = [a for a in argv[1:] if not a.startswith("-")]
    elif cmd_basename == "uv":
        # uv run [options] python3 .../match_ssot.py ...
        # Unwrap uv run options
        unwrapped = _strip_uv_run_options(argv)
        if unwrapped and Path(unwrapped[0]).name in ("python", "python3"):
            script_candidates = [a for a in unwrapped[1:] if not a.startswith("-")]
        elif unwrapped:
            script_candidates = [unwrapped[0]]
    else:
        # Direct invocation: check argv[0] itself
        script_candidates = [leading]

    for candidate in script_candidates:
        bname = Path(candidate).name
        if bname in ("match-ssot.sh", "match_ssot.py"):
            return True
        if "ssot-discovery/scripts/" in candidate:
            return True

    return False


def _has_arg(argv: List[str], flag: str) -> bool:
    """
    Check if flag (e.g. '--keywords' or '--paths') appears in argv,
    either as a standalone argument or as the key in '--flag=value' form.
    """
    for arg in argv:
        if arg == flag or arg.startswith(flag + "="):
            return True
    return False


def _is_trivially_pass_command(command: str) -> bool:
    """
    Detect trivially-pass VC patterns: repository-local ssot-discovery scripts
    called with BOTH --keywords and --paths options simultaneously.

    Root cause (Issue #201):
      match-ssot.sh / match_ssot.py implements a directory mapping that forces
      --paths targets into matched_documents at low relevance, regardless of
      whether --keywords actually appear in that path. When both --keywords and
      --paths are supplied, the VC can return the target path (exit 0) even
      when the keywords are absent — making the VC trivially-passing.

    Detection logic:
    - The command invokes a discovery script (match-ssot.sh / match_ssot.py /
      any script under ssot-discovery/scripts/)
    - AND the argument list contains BOTH --keywords (or --keywords=...) AND
      --paths (or --paths=...)

    Only the combination of --keywords + --paths creates the trivially-pass
    structure. --keywords alone or --paths alone does not trigger this.

    Note: rg/grep have no '--paths' option. rg uses positional PATH arguments
    and -g/--glob for path filtering. Positional paths in rg/grep are not
    forced-includes and are NOT trivially-pass. The previous implementation
    that flagged rg/grep + '--paths' was based on an incorrect premise and
    caused false positives for queries like 'rg -e "--paths" file' or
    'rg -- "--paths" file'. That logic has been removed.

    Returns True if the command is a trivially-pass pattern, False otherwise.
    """
    try:
        argv = shlex.split(command)
    except ValueError:
        return False

    if not argv:
        return False

    # Step 1: Identify whether this is a discovery script invocation
    if not _is_discovery_script(argv):
        return False

    # Step 2: Check for --keywords AND --paths both present
    # Only the combination creates the forced-include trivially-pass structure.
    has_keywords = _has_arg(argv, "--keywords")
    has_paths = _has_arg(argv, "--paths")
    return has_keywords and has_paths


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


# ---------------------------------------------------------------------------
# Static command classification: allowlist / denylist policy (AC1-AC3)
# ---------------------------------------------------------------------------

# Commands that are explicitly denied (unsafe for baseline preflight)
# Checked by the basename of argv[0]
_DENIED_COMMANDS: frozenset = frozenset([
    # Shell invocations
    "bash", "sh", "zsh", "fish", "dash", "ksh",
    # Inline code execution (python -c / python3 -c / node -e / perl -e / ruby -e)
    # NOTE: python3 is special-cased below: only -m py_compile / -m pytest is allowed
    "python",
    "node", "perl", "ruby",
    # Network access
    "curl", "wget", "nc", "ncat", "ssh", "scp", "rsync",
    # Filesystem mutation
    "rm", "mv", "cp", "chmod", "chown", "touch",
    "rmdir", "ln",
    # Text stream mutation (sed -i is a mutation; all sed uses blocked for safety)
    "sed", "tee",
])

# B3: git read-only subcommand allowlist (exact argv[1] check)
# git -c / --config-env / --exec-path / alias.* flags → blocked via option-flag check
# git worktree, git submodule, git bisect, etc. (not listed here) → blocked by default
_ALLOWED_GIT_SUBCOMMANDS: frozenset = frozenset([
    "status", "diff", "log", "show", "ls-files", "rev-parse", "branch", "tag", "-l",
])

# B3: gh read-only subcommand allowlist (exact argv tuple prefix check)
# gh alias, gh extension, gh auth (mutation), etc. → blocked by default
_ALLOWED_GH_PREFIXES: tuple = (
    # Read-only tuples: (argv[1],) or (argv[1], argv[2])
    ("issue", "view"),
    ("pr", "view"),
    ("pr", "list"),
    ("issue", "list"),
    ("repo", "view"),
    # NOTE: gh api is blocked — mutation potential via POST/PATCH; not needed in VC context
)

# B1: pnpm exact subcommand allowlist (tuple-based)
# Only these exact (argv[0], argv[1]) tuples are allowed for pnpm.
# pnpm exec, pnpm dlx, pnpm run, pnpm add, etc. are blocked.
_ALLOWED_PNPM_SUBCOMMANDS: frozenset = frozenset([
    ("pnpm", "typecheck"),
    ("pnpm", "lint"),
    ("pnpm", "test"),
    ("pnpm", "build"),
])

# Explicitly allowed command basenames for baseline preflight
# Anything NOT in this set is blocked by default (allowlist-closed policy, AC3)
_ALLOWED_COMMANDS: frozenset = frozenset([
    "test",      # test -f / -d / -s (read-only assertions)
    "rg",        # ripgrep (read-only)
    "grep",      # grep (read-only)
    "fgrep",
    "egrep",
    "python3",   # allowed only when _is_allowed_python3_invocation passes
    "uv",        # allowed only for uv run pytest / uv run python3 -m pytest
    "pnpm",      # allowed only for typecheck/lint/test/build subcommands
    "pytest",    # direct pytest invocation
    "git",       # allowed only for read-only subcommands (show, log, diff, etc.)
    "gh",        # allowed only for read-only subcommands (gh issue view, gh pr view)
    "jq",        # read-only JSON filter
    "cat",       # read-only
    "ls",        # read-only
    "find",      # read-only
    "wc",        # read-only
    "sort",      # read-only
    "uniq",      # read-only
    "head",      # read-only
    "tail",      # read-only
    "diff",      # read-only
    "echo",      # safe
    "printf",    # safe
    "true",      # safe
    "false",     # safe
    "realpath",  # safe
    "dirname",   # safe
    "basename",  # safe
    "which",     # safe
    "type",      # safe
    "env",       # safe (read env)
    "printenv",  # safe (read env)
    "pwd",       # safe
    "date",      # safe
    "stat",      # read-only
    # NOTE: mkdir removed from allowlist (B2) — mkdir -p .git/hooks and similar mutations possible
])


def _is_allowed_python3_invocation(argv: List[str]) -> bool:
    """
    python3 is only allowed for:
      - python3 -m py_compile <file>
      - python3 -m pytest ...
    Inline code (-c flag) is NOT allowed (AC2: python3 -c is blocked as unsafe_command).
    """
    if not argv or Path(argv[0]).name not in ("python3",):
        return False
    if len(argv) >= 2 and argv[1] == "-c":
        return False  # inline code not allowed
    if len(argv) >= 3 and argv[1] == "-m" and argv[2] in ("py_compile", "pytest"):
        return True
    return False


def _is_allowed_git_invocation(argv: List[str]) -> bool:
    """
    B3: git read-only allowlist check.
    Returns True only if the git subcommand is in the read-only allowlist.
    Blocks git -c, --config-env, --exec-path option flags, and any unlisted subcommand.
    """
    if not argv or Path(argv[0]).name != "git":
        return False
    if len(argv) < 2:
        return False
    # Block global git option flags (e.g., git -c alias.x='!...')
    second_arg = argv[1]
    if second_arg.startswith("-") and second_arg not in ("-l",):
        return False
    subcommand = second_arg.lower()
    return subcommand in _ALLOWED_GIT_SUBCOMMANDS


def _is_allowed_gh_invocation(argv: List[str]) -> bool:
    """
    B3: gh read-only allowlist check.
    Returns True only if the gh subcommand tuple is in the read-only allowlist.
    Blocks gh alias, gh extension, gh auth (mutations), gh api, and any unlisted subcommand.
    """
    if not argv or Path(argv[0]).name != "gh":
        return False
    if len(argv) < 3:
        return False
    # Check (argv[1], argv[2]) tuple
    sub_tuple = (argv[1].lower(), argv[2].lower())
    return sub_tuple in _ALLOWED_GH_PREFIXES


def _is_allowed_pnpm_invocation(argv: List[str]) -> bool:
    """
    B1: pnpm exact subcommand allowlist check.
    Only (pnpm, typecheck), (pnpm, lint), (pnpm, test), (pnpm, build) are allowed.
    pnpm exec, pnpm dlx, pnpm run, pnpm add, etc. are all blocked.
    """
    if not argv or Path(argv[0]).name != "pnpm":
        return False
    if len(argv) < 2:
        return False
    key = ("pnpm", argv[1].lower())
    return key in _ALLOWED_PNPM_SUBCOMMANDS


# ---------------------------------------------------------------------------
# Regex-bearing command detection for backslash-pipe (regex_literal_pipe_suspected) — Issue #589
# ---------------------------------------------------------------------------


def _is_regex_bearing_command_for_literal_pipe(argv: List[str]) -> bool:
    """
    Return True if the command is regex-bearing and uses a regex engine where
    backslash-pipe is a literal pipe (not alternation).

    Coverage:
    - rg: Rust regex engine, x|y is alternation, backslash-pipe is literal pipe.
      EXCEPT: rg -F / rg --fixed-strings disables regex entirely → return False.
    - egrep: ERE, backslash-pipe is literal pipe character (not alternation) → True.
    - fgrep: fixed-string grep (no regex engine) → always False (Blocker 2 fix).
    - grep -E / grep -P: Extended/Perl regex → True.
      EXCEPT: grep -F / grep --fixed-strings disables regex → return False (Blocker 2 fix).

    Note: grep (basic mode, BRE) also treats \\| as literal, but since BRE
    uses | as literal anyway (alternation needs \\|), this check focuses on
    the cases where \\| is clearly wrong intent (author likely intended |
    as alternation).
    """
    if not argv:
        return False
    cmd_basename = Path(argv[0]).name

    if cmd_basename == "rg":
        # rg -F / --fixed-strings: not a regex-bearing command
        for arg in argv[1:]:
            if arg in ("-F", "--fixed-strings"):
                return False
            # Combined short flags like -Fn, -nF etc.
            if arg.startswith("-") and not arg.startswith("--") and "F" in arg[1:]:
                return False
        return True

    if cmd_basename == "egrep":
        # egrep is always ERE → True
        return True

    if cmd_basename == "fgrep":
        # fgrep is fixed-string grep → not regex-bearing (Blocker 2 fix)
        return False

    if cmd_basename == "grep":
        has_fixed_strings = False
        has_extended_or_perl = False
        for arg in argv[1:]:
            if arg in ("-F", "--fixed-strings"):
                has_fixed_strings = True
            elif arg in ("-E", "-P", "--extended-regexp", "--perl-regexp"):
                has_extended_or_perl = True
            elif arg.startswith("-") and not arg.startswith("--"):
                flags = arg[1:]
                if "F" in flags:
                    has_fixed_strings = True
                if "E" in flags or "P" in flags:
                    has_extended_or_perl = True
        # -F takes precedence: fixed-string mode, not regex-bearing
        if has_fixed_strings:
            return False
        return has_extended_or_perl

    return False


def _command_pattern_contains_backslash_pipe(argv: List[str]) -> bool:
    r"""
    Check if the PATTERN argument (only) in argv contains \\|.

    This detects the case where a user wrote \\| intending regex literal pipe,
    which in most regex engines (rg, egrep, grep -E) is NOT needed — | alone
    is alternation in ERE/Rust regex, and \\| is a literal pipe.

    Blocker 3 fix: Only the PATTERN argument is inspected, not PATH/GLOB/option values.

    For rg:
      - -e PATTERN / --regexp PATTERN → the value is a pattern
      - -g GLOB / --glob GLOB → not inspected (glob, not regex pattern)
      - -F / --fixed-strings → caller already returns False from _is_regex_bearing_command
      - -- PATTERN PATH... → first positional after -- is PATTERN; the rest are PATHs
      - Without -e/--regexp: first non-flag positional argument is PATTERN; rest are PATHs

    For grep / egrep:
      - -e PATTERN / --regexp PATTERN → the value is a pattern
      - Without -e/--regexp: first non-flag positional argument is PATTERN; rest are PATHs
      - File PATH arguments are NOT inspected.
    """
    if not argv:
        return False

    cmd_basename = Path(argv[0]).name
    n = len(argv)

    # Flags that take a value argument for both rg and grep families.
    # We need to skip these flag+value pairs to correctly identify positional args.
    # Value-taking flags common to rg/grep (non-exhaustive; focus on pattern-relevant ones):
    _RG_VALUE_FLAGS = frozenset([
        "-e", "--regexp",          # pattern (handled specially below)
        "-g", "--glob",            # glob (NOT a pattern)
        "-A", "--after-context",
        "-B", "--before-context",
        "-C", "--context",
        "-m", "--max-count",
        "-M", "--max-columns",
        "--max-depth",
        "-l", "--files-with-matches",
        "--color", "--colours",
        "--type", "-t",
        "--type-not", "-T",
        "--encoding", "-E",        # Note: -E in rg is --encoding, not --extended-regexp
        "--field-match-separator",
        "--field-context-separator",
        "--replace", "-r",
        "--pre",
        "--iglob",
    ])

    _GREP_VALUE_FLAGS = frozenset([
        "-e", "--regexp",          # pattern (handled specially below)
        "-f", "--file",
        "-A", "--after-context",
        "-B", "--before-context",
        "-C", "--context",
        "-m", "--max-count",
        "--label",
        "--color", "--colour",
        "--binary-files",
        "-D", "--devices",
        "-d", "--directories",
        "--include",
        "--exclude",
        "--exclude-from",
        "--exclude-dir",
    ])

    # Collect explicit pattern arguments from -e/--regexp
    explicit_patterns: List[str] = []

    if cmd_basename == "rg":
        value_flags = _RG_VALUE_FLAGS
    else:
        # grep, egrep, fgrep
        value_flags = _GREP_VALUE_FLAGS

    i = 1
    after_double_dash = False
    first_positional_seen = False

    while i < n:
        arg = argv[i]

        if arg == "--":
            after_double_dash = True
            i += 1
            continue

        if after_double_dash:
            # After --: first arg is PATTERN (if no -e was given), rest are PATHs
            if not first_positional_seen and not explicit_patterns:
                # This is the PATTERN
                if "\\|" in arg:
                    return True
                first_positional_seen = True
            # Remaining args after -- are PATHs: do NOT inspect
            i += 1
            continue

        # Handle -e PATTERN / --regexp PATTERN (explicit pattern flag)
        if arg in ("-e", "--regexp"):
            if i + 1 < n:
                explicit_patterns.append(argv[i + 1])
            i += 2
            continue

        # Handle --regexp=PATTERN form
        if arg.startswith("--regexp="):
            explicit_patterns.append(arg[len("--regexp="):])
            i += 1
            continue

        # Handle -eXXX (short flag with value concatenated, e.g. -e"foo\|bar")
        if arg.startswith("-e") and len(arg) > 2:
            explicit_patterns.append(arg[2:])
            i += 1
            continue

        # Skip -g / --glob and their values (NOT pattern) for rg
        if cmd_basename == "rg" and arg in ("-g", "--glob", "--iglob"):
            i += 2  # skip flag and value
            continue
        if cmd_basename == "rg" and (arg.startswith("--glob=") or arg.startswith("--iglob=")):
            i += 1  # skip flag=value
            continue

        # Skip other known value-taking flags and their values
        if arg in value_flags:
            i += 2  # skip flag and value
            continue

        # Handle --flag=value forms for value-taking flags
        for flag in value_flags:
            if flag.startswith("--") and arg.startswith(flag + "="):
                i += 1
                arg = None  # consumed
                break
        if arg is None:
            continue

        # Combined short flags (e.g. -nq, -rn): skip (no value taken beyond the flag itself)
        if arg.startswith("-") and not arg.startswith("--") and len(arg) > 1:
            i += 1
            continue

        # Positional argument
        if not first_positional_seen and not explicit_patterns:
            # This is the PATTERN (only if no -e was given)
            if "\\|" in arg:
                return True
            first_positional_seen = True
        # else: it's a PATH → do NOT inspect
        i += 1

    # Check any explicitly collected patterns
    for pat in explicit_patterns:
        if "\\|" in pat:
            return True

    return False


def classify_static_command(
    raw_command: str, cwd: Path
) -> Optional[Tuple[str, str, str, Optional[str], str]]:
    """
    Perform static pre-execution classification of a VC command.

    Returns (classification, category, decision, fix_hint, scope_class) if
    the command is blocked or can be determined statically without execution,
    or None if the command should proceed to run_command().

    This is called BEFORE run_command() to prevent dangerous commands from
    being executed. AC1-AC3 enforcement happens here.
    """
    # 1. Check for unsupported shell syntax: $(...), `...`, ${...}  (AC1)
    if has_command_substitution(raw_command):
        return (
            "blocked",
            "unsupported_shell_syntax",
            "blocked",
            "Shell substitution ($(...), `...`, ${...}) is not supported in VC preflight; "
            "use a direct command without command substitution",
            "baseline_fail_expected",
        )

    # 2. Try to parse with shlex (AC1 edge: malformed shell syntax)
    try:
        argv = shlex.split(raw_command, posix=True)
    except ValueError as e:
        return (
            "blocked",
            "unsupported_shell_syntax",
            "blocked",
            f"Cannot parse command with shlex: {e}; check for unmatched quotes or unsupported syntax",
            "baseline_fail_expected",
        )

    if not argv:
        return (
            "blocked",
            "unsupported_shell_syntax",
            "blocked",
            "Empty command after parsing",
            "baseline_fail_expected",
        )

    cmd_basename = Path(argv[0]).name

    # 3. Check for compound commands (shell operators)
    if detect_compound_command(raw_command):
        return (
            "blocked",
            "compound_command_disallowed",
            "blocked",
            "Compound shell commands are not supported in baseline_vc_preflight/v1",
            "baseline_fail_expected",
        )

    # 3.5. Trivially-pass detection: discovery script with --keywords + --paths.
    # NOTE: This check runs BEFORE denied-command detection (step 4) so that
    # 'bash match-ssot.sh --keywords ... --paths ...' is reported as
    # category: trivially_pass rather than category: unsafe_command.
    # Both results are classification: blocked / decision: blocked / exit_code: None
    # (neither is executed), so moving this check earlier is a category-label
    # correction, not a safety relaxation.
    if _is_trivially_pass_command(raw_command):
        return (
            "blocked",
            "trivially_pass",
            "blocked",
            "Discovery script (match-ssot.sh / match_ssot.py) is called with both "
            "--keywords and --paths; the directory mapping in match-ssot forces --paths "
            "targets into matched_documents regardless of keyword presence, making the VC "
            "trivially pass. Use --keywords only (without --paths) and verify the same path "
            "appears in results to confirm keyword presence.",
            "baseline_fail_expected",
        )

    # 3.6. Regex literal pipe detection: rg/egrep/grep -E with \\| in pattern (AC3: Issue #589)
    # \\| in a regex-bearing command pattern is likely a mistake (intending literal pipe
    # while the engine treats | as alternation and \\| as literal pipe).
    # This is classified as regex_literal_pipe_suspected and blocked unless the caller
    # supplies a literal-pipe-ok annotation (handled at parse/caller level).
    if _is_regex_bearing_command_for_literal_pipe(argv):
        if _command_pattern_contains_backslash_pipe(argv):
            return (
                "blocked",
                "regex_literal_pipe_suspected",
                "blocked",
                "Pattern argument contains \\| in a regex-bearing command (rg/egrep/grep -E). "
                "In ripgrep and ERE-mode grep, | is alternation and \\| is a literal pipe. "
                "If you intend regex alternation, use | (without backslash). "
                "If you truly need a literal pipe in the pattern, add "
                "# vc-regex-intent: literal-pipe-ok reason=\"...\" on the preceding line.",
                "baseline_fail_expected",
            )

    # 4. Check denied commands (unsafe, AC2)
    if cmd_basename in _DENIED_COMMANDS:
        return (
            "blocked",
            "unsafe_command",
            "blocked",
            f"'{cmd_basename}' is not safe for baseline preflight; "
            "shell interpreters, network tools, and filesystem mutators are blocked",
            "baseline_fail_expected",
        )

    # 5. Special case: python3 with -c flag (AC2: python3 -c is blocked)
    if cmd_basename == "python3" and not _is_allowed_python3_invocation(argv):
        # python3 without a recognized safe invocation pattern
        if len(argv) >= 2 and argv[1] == "-c":
            return (
                "blocked",
                "unsafe_command",
                "blocked",
                "python3 -c (inline code) is not allowed in VC preflight; "
                "use python3 -m pytest or python3 -m py_compile instead",
                "baseline_fail_expected",
            )
        # Other python3 invocations not matching safe patterns are command_not_allowed
        return (
            "blocked",
            "command_not_allowed",
            "blocked",
            f"python3 invocation '{raw_command}' is not in the VC preflight allowlist; "
            "use python3 -m pytest or python3 -m py_compile",
            "baseline_fail_expected",
        )

    # 6. Check git: exact read-only allowlist (B3)
    if cmd_basename == "git":
        if not _is_allowed_git_invocation(argv):
            return (
                "blocked",
                "command_not_allowed",
                "blocked",
                f"'git {argv[1] if len(argv) > 1 else ''}' is not in the git read-only allowlist; "
                "allowed: status, diff, log, show, ls-files, rev-parse. "
                "git worktree, git -c, and mutation commands are blocked.",
                "baseline_fail_expected",
            )
        return None  # allowed git read-only command

    # 7. Check gh: exact read-only allowlist (B3)
    if cmd_basename == "gh":
        if not _is_allowed_gh_invocation(argv):
            return (
                "blocked",
                "command_not_allowed",
                "blocked",
                f"'gh {' '.join(argv[1:])}' is not in the gh read-only allowlist; "
                "allowed: gh issue view, gh pr view, gh pr list, gh issue list, gh repo view. "
                "gh api, gh alias, gh extension, and mutation commands are blocked.",
                "baseline_fail_expected",
            )
        return None  # allowed gh read-only command

    # 8. Check pnpm: exact subcommand allowlist (B1)
    if cmd_basename == "pnpm":
        if not _is_allowed_pnpm_invocation(argv):
            return (
                "blocked",
                "command_not_allowed",
                "blocked",
                f"'pnpm {argv[1] if len(argv) > 1 else ''}' is not in the pnpm allowlist; "
                "only pnpm typecheck, pnpm lint, pnpm test, pnpm build are allowed. "
                "pnpm exec, pnpm dlx, pnpm run, pnpm add, etc. are blocked.",
                "baseline_fail_expected",
            )
        return None  # allowed pnpm subcommand

    # 9. Check uv: only allow uv run pytest / uv run python -m pytest / uv run python3 -m pytest
    if cmd_basename == "uv":
        if len(argv) >= 2 and argv[1] == "run":
            unwrapped = _strip_uv_run_options(argv)
            if unwrapped and Path(unwrapped[0]).name in ("pytest",):
                return None  # allowed
            if (
                unwrapped
                and Path(unwrapped[0]).name in ("python", "python3")
                and len(unwrapped) >= 3
                and unwrapped[1] == "-m"
                and unwrapped[2] == "pytest"
            ):
                return None  # allowed
            # uv run <other> is not in allowlist
            inner_cmd = unwrapped[0] if unwrapped else "<unknown>"
            return (
                "blocked",
                "command_not_allowed",
                "blocked",
                f"'uv run {inner_cmd}' is not in the VC preflight allowlist; "
                "only 'uv run pytest' and 'uv run python3 -m pytest' are allowed",
                "baseline_fail_expected",
            )
        else:
            return (
                "blocked",
                "command_not_allowed",
                "blocked",
                f"uv subcommand '{argv[1] if len(argv) > 1 else ''}' is not in the VC preflight allowlist",
                "baseline_fail_expected",
            )

    # 10. Default allowlist check: command not in allowed set → blocked (AC3)
    if cmd_basename not in _ALLOWED_COMMANDS:
        return (
            "blocked",
            "command_not_allowed",
            "blocked",
            f"'{cmd_basename}' is not in the VC preflight allowlist; "
            "only explicitly allowed commands are permitted "
            "(rg, test, grep, uv run pytest, pnpm typecheck/lint/test/build, etc.)",
            "baseline_fail_expected",
        )

    return None  # proceed to execution


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
                compound_command_disallowed | unsupported_shell_syntax |
                unsafe_command | command_not_allowed | unknown | regression_gate
      decision: go | blocked | human_judgment
      fix_hint: nullable hint
      scope_class: baseline_fail_expected | regression_gate | pr_review_only | runtime_only
    """

    # AC6: negated search commands - static classification (BEFORE run_command)
    if _is_negated_search_command(command):
        return "expected_fail", "expected_baseline_fail", "go", None, "baseline_fail_expected"

    # AC1: command substitution detection - blocked (unsupported_shell_syntax), not executed
    if has_command_substitution(command):
        return (
            "blocked",
            "unsupported_shell_syntax",
            "blocked",
            "Shell substitution ($(...), `...`, ${...}) is not supported in VC preflight; "
            "use a direct command without command substitution",
            "baseline_fail_expected",
        )

    # compound command は blocked (default scope_class)
    if detect_compound_command(command):
        return "blocked", "compound_command_disallowed", "blocked", "Compound shell commands are not supported in initial implementation", "baseline_fail_expected"

    # AC4: regression_gate prefix detection AFTER static checks
    # If it's a regression gate, apply special rules
    if _is_regression_gate_command(command, cwd=cwd):
        if exit_code == 0:
            return "expected_pass", "regression_gate", "go", None, "regression_gate"
        else:
            # B5: Check pytest exit codes 4/5 BEFORE regression_gate failure classification
            if _is_pytest_invocation(command):
                combined_lower = f"{stdout}\n{stderr}".lower()

                # pytest exit 4 + file not found → expected_baseline_fail (env/path missing)
                if exit_code == 4 and re.search(r"error:\s+file or directory not found:", combined_lower):
                    return "expected_fail", "expected_baseline_fail", "go", None, "baseline_fail_expected"

                # B5: pytest exit 5 → vc_no_tests_collected / blocked
                # exit 5 is "no tests collected" — -k condition mismatch or wrong path
                if exit_code == 5:
                    return (
                        "blocked",
                        "vc_no_tests_collected",
                        "blocked",
                        "pytest collected 0 tests (exit 5); check -k filter or test path",
                        "baseline_fail_expected",
                    )

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

        # pytest exit 4 + file not found → expected_baseline_fail (path/env missing)
        if exit_code == 4 and re.search(r"error:\s+file or directory not found:", combined_lower):
            return "expected_fail", "expected_baseline_fail", "go", None, "baseline_fail_expected"

        # B5: pytest exit 5 → vc_no_tests_collected / blocked
        # exit 5 = no tests collected (-k mismatch, wrong path, etc.) → not a valid baseline VC
        if exit_code == 5:
            return (
                "blocked",
                "vc_no_tests_collected",
                "blocked",
                "pytest collected 0 tests (exit 5); check -k filter or test path",
                "baseline_fail_expected",
            )

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

    高確度: compound_command_disallowed, unsupported_shell_syntax, unsafe_command, command_not_allowed,
            file_not_found_expected, expected_baseline_fail, env_missing_dep, file_not_found_unrunnable
    中確度: timeout, unexpected_pass
    低確度: unknown
    """
    high_confidence = {
        "compound_command_disallowed",
        "unsupported_shell_syntax",
        "unsafe_command",
        "command_not_allowed",
        "file_not_found_expected",
        "expected_baseline_fail",
        "env_missing_dep",
        "file_not_found_unrunnable",
        "vc_no_tests_collected",
        "trivially_pass",
        "regex_literal_pipe_suspected",  # AC3: Issue #589
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
            "errors": [
                {
                    "kind": "retrieval_error",
                    "rule": "VC000_BODY_RETRIEVAL_FAILED",
                    "message": error_code or "failed_to_retrieve_issue_body",
                    "minimal_context": f"issue={args.issue}, repo={args.repo}",
                    "fix_hint": "Check GitHub credentials (gh auth status) and issue number",
                }
            ],
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
            "errors": [
                {
                    "kind": "extraction_error",
                    "rule": "VC001_NO_VERIFICATION_COMMANDS_SECTION",
                    "message": "Verification Commands section not found",
                    "minimal_context": "body does not contain '## Verification Commands' heading",
                    "fix_hint": "Add a '## Verification Commands' section with fenced bash blocks to the Issue body",
                }
            ],
        }
        print(json.dumps(result, indent=2))
        # C2: exit code 2 for extraction errors
        return 2

    # B4: bash ブロックからコマンドを抽出 (```bash のみ canonical format)
    blocks = extract_fenced_bash_blocks(vc_section)
    commands = []
    for block in blocks:
        commands.extend(parse_commands_from_block(block))

    # B3: 0 件抽出は blocked として返す
    if not commands:
        # B4: check for unlabeled fences to provide better error message
        unlabeled = find_unlabeled_fenced_blocks(vc_section)
        if unlabeled:
            no_cmd_error = {
                "kind": "unsupported_vc_format",
                "rule": "VC003_UNLABELED_FENCE_BLOCK",
                "message": "Unlabeled fenced code blocks (```) found in Verification Commands section; "
                           "only ```bash fenced blocks are the canonical VC format",
                "minimal_context": "unlabeled fence blocks are not extracted as VC commands",
                "fix_hint": "Change ``` to ```bash in Verification Commands fenced blocks; "
                            "```bash ... ``` is the canonical VC format",
            }
        else:
            no_cmd_error = {
                "kind": "extraction_error",
                "rule": "VC002_NO_COMMANDS_EXTRACTED",
                "message": "No verification commands extracted from Verification Commands section",
                "minimal_context": "fenced bash blocks found but no commands extracted",
                "fix_hint": "Add '$ <command>' lines inside fenced bash blocks in the Verification Commands section; "
                            "fenced bash blocks (```bash ... ```) are the canonical VC format",
            }
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
            "errors": [no_cmd_error],
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

    for ac_label, command, line_no, preflight_scope, vc_regex_intent in commands:
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
            # Static classification checks BEFORE run_command (CRITICAL)
            # Negated search commands are statically classified as expected_fail/go
            if _is_negated_search_command(command):
                exit_code, stdout, stderr, duration_ms = None, "", "", 0
                classification, category, decision, fix_hint, scope_class = (
                    "expected_fail",
                    "expected_baseline_fail",
                    "go",
                    None,
                    "baseline_fail_expected",
                )
            else:
                # AC1-AC3: classify_static_command checks unsafe/unsupported commands
                # BEFORE any execution attempt
                static_result = classify_static_command(command, Path(args.cwd))
                # AC3 (Issue #589): If regex_literal_pipe_suspected and literal-pipe-ok
                # annotation is present, skip the blocked classification and proceed to execute.
                if (
                    static_result is not None
                    and static_result[1] == "regex_literal_pipe_suspected"
                    and vc_regex_intent == "literal-pipe-ok"
                ):
                    static_result = None  # annotation exempts from blocked
                if static_result is not None:
                    exit_code, stdout, stderr, duration_ms = None, "", "", 0
                    classification, category, decision, fix_hint, scope_class = static_result
                else:
                    # Safe to run: execute the command
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
