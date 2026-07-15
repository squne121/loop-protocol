#!/usr/bin/env python3
"""
Baseline Verification Command Preflight

Issue body の `## Verification Commands` セクションから VC を AC 別に抽出して単体実行し、
root-cause 分類（expected_fail / unexpected_pass / blocked / human_judgment）と
category / decision / confidence を含む JSON を返す。
"""

import argparse
import hashlib
import os
import json
import re
import shlex
import stat
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple


# Shared AC / preflight-scope parser contract (同一の VC grammar 定義を利用)
_REPO_ROOT = Path(__file__).resolve().parents[4]
_VC_SYNTAX_DIR = _REPO_ROOT / ".claude" / "skills" / "issue-contract-review" / "scripts"
if str(_VC_SYNTAX_DIR) not in sys.path:
    sys.path.insert(0, str(_VC_SYNTAX_DIR))

from vc_contract_syntax import (  # noqa: E402
    VALID_PRE_FLIGHT_SCOPE_VALUES,
    parse_ac_marker_line,
    parse_preflight_scope_marker_line,
    extract_baseline_expect_annotation,
    extract_vc_role_annotation,
    parse_verification_commands_section,
)
import pnpm_gate_registry as pnpm_gate_registry  # noqa: E402


# Issue #1333 AC1: per-command timeout の named constant。
# 実測 test_baseline_vc_preflight.py 実行時間（約58秒、real 0m59.376s）を
# 安全マージン込みで上回る値（>= 90）を維持する。--timeout-seconds の
# argparse default はこの定数を参照し、run_contract_review_once.py 側も
# この定数を import して drift を防ぐ（AC2/AC3 参照）。
DEFAULT_TIMEOUT_SECONDS = 90


def collect_current_head_evidence(cwd: str, reviewed_head_sha: str) -> Dict[str, Any]:
    """Observe and certify one clean worktree against a reviewed commit object.

    The caller must provide the full immutable object ID. Git resolution is
    still used to prove that the object exists and is a commit, but aliases,
    symbolic refs, and abbreviated IDs are never canonicalized into acceptance.
    """
    base = {
        "head_sha": None,
        "reviewed_head_sha": reviewed_head_sha or None,
        "clean_before": False,
        "clean_after": None,
        "head_after_sha": None,
        "certified": False,
        "fallback_detected": False,
        "human_review_required": False,
        "stop_condition_triggered": True,
        "errors": [],
    }
    if not reviewed_head_sha or isinstance(reviewed_head_sha, bool):
        base["errors"].append("reviewed_head_sha_missing_or_invalid")
        return base

    def git_text(*arguments: str) -> Tuple[Optional[str], Optional[str]]:
        result = subprocess.run(
            ["git", "-C", cwd, *arguments], capture_output=True, text=True
        )
        if result.returncode != 0:
            return None, result.stderr.strip() or "git_command_failed"
        return result.stdout.strip(), None

    observed, error = git_text("rev-parse", "--verify", "--end-of-options", "HEAD^{commit}")
    if error:
        base["errors"].append(f"head_observation_failed:{error}")
        return base
    canonical_reviewed, error = git_text(
        "rev-parse", "--verify", "--end-of-options", f"{reviewed_head_sha}^{{commit}}"
    )
    if error:
        base["head_sha"] = observed
        base["errors"].append(f"reviewed_head_not_commit:{error}")
        return base
    if (
        reviewed_head_sha != canonical_reviewed
        or re.fullmatch(r"[0-9a-f]+", reviewed_head_sha) is None
    ):
        base["head_sha"] = observed
        base["errors"].append("reviewed_head_sha_not_full_oid")
        return base
    status, error = git_text("status", "--porcelain=v1", "-z", "--untracked-files=all")
    if error:
        base["head_sha"] = observed
        base["reviewed_head_sha"] = canonical_reviewed
        base["errors"].append(f"cleanliness_observation_failed:{error}")
        return base

    base["head_sha"] = observed
    base["reviewed_head_sha"] = canonical_reviewed
    base["clean_before"] = status == ""
    if observed != canonical_reviewed:
        base["errors"].append("reviewed_head_sha_mismatch")
    if status != "":
        base["errors"].append("worktree_not_clean_before_execution")
    if not base["errors"]:
        base["certified"] = True
        base["stop_condition_triggered"] = False
    return base


def finalize_current_head_evidence(cwd: str, evidence: Dict[str, Any]) -> Dict[str, Any]:
    """Verify that a certified worktree did not drift during VC execution."""
    if not evidence.get("certified"):
        return evidence
    result = subprocess.run(
        ["git", "-C", cwd, "rev-parse", "--verify", "--end-of-options", "HEAD^{commit}"],
        capture_output=True,
        text=True,
    )
    status = subprocess.run(
        ["git", "-C", cwd, "status", "--porcelain=v1", "-z", "--untracked-files=all"],
        capture_output=True,
        text=True,
    )
    head_after = result.stdout.strip() if result.returncode == 0 else None
    clean_after = status.returncode == 0 and status.stdout == ""
    evidence["head_after_sha"] = head_after
    evidence["clean_after"] = clean_after
    if head_after != evidence.get("head_sha"):
        evidence["errors"].append("head_drift_during_execution")
    if not clean_after:
        evidence["errors"].append("worktree_not_clean_after_execution")
    evidence["certified"] = not evidence["errors"]
    evidence["stop_condition_triggered"] = not evidence["certified"]
    return evidence


def finalize_current_head_output(result: Dict[str, Any], evidence: Dict[str, Any]) -> None:
    """Make final status and safety flags consistent on every current-head exit path."""
    status = result.get("status")
    if status == "human_judgment":
        evidence["human_review_required"] = True
        evidence["stop_condition_triggered"] = True
        evidence["certified"] = False
    elif status == "blocked":
        evidence["stop_condition_triggered"] = True
        evidence["certified"] = False
    elif status == "pass" and not evidence.get("certified"):
        result["status"] = "blocked"
        evidence["stop_condition_triggered"] = True
    elif status == "pass":
        evidence["human_review_required"] = False
        evidence["stop_condition_triggered"] = False


def exit_code_for_status(status: str) -> int:
    """Map the final emitted status to a fail-closed process exit code."""
    return {"pass": 0, "blocked": 1, "human_judgment": 3}.get(status, 2)


def safety_fields(evidence_mode: str, evidence: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """Return stable bool safety fields for every JSON envelope."""
    if evidence_mode != "current-head":
        return {
            "evidence_mode": "baseline",
            "head_sha": None,
            "reviewed_head_sha": None,
            "fallback_detected": False,
            "human_review_required": False,
            "stop_condition_triggered": False,
        }
    evidence = evidence or {}
    return {
        "evidence_mode": "current-head",
        "head_sha": evidence.get("head_sha"),
        "reviewed_head_sha": evidence.get("reviewed_head_sha"),
        "head_after_sha": evidence.get("head_after_sha"),
        "clean_before": bool(evidence.get("clean_before")),
        "clean_after": evidence.get("clean_after"),
        "fallback_detected": bool(evidence.get("fallback_detected", False)),
        "human_review_required": bool(evidence.get("human_review_required", False)),
        "stop_condition_triggered": bool(evidence.get("stop_condition_triggered", True)),
    }


def emit_json(result: Dict[str, Any], evidence_mode: str, evidence: Optional[Dict[str, Any]] = None) -> None:
    """Emit the legacy envelope, extended only for current-head evidence."""
    if evidence_mode == "current-head":
        finalized_evidence = evidence or {}
        finalize_current_head_output(result, finalized_evidence)
        result.update(safety_fields(evidence_mode, finalized_evidence))
    print(json.dumps(result, indent=2))


def current_head_blocked_result(args: argparse.Namespace, evidence: Dict[str, Any], message: str) -> Dict[str, Any]:
    return {
        "schema": "baseline_vc_preflight/v1",
        "issue": args.issue or 0,
        "repo": args.repo,
        "generated_at": datetime.now(timezone.utc).isoformat().replace('+00:00', 'Z'),
        "source": {"kind": "current_head_validation", "body_sha256": ""},
        "status": "blocked",
        "summary": {
            "expected_fail": 0,
            "unexpected_pass": 0,
            "blocked": 1,
            "human_judgment": 0,
            "extraction_errors": 0,
        },
        "results": [],
        "errors": [message, *evidence.get("errors", [])],
    }


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
    except Exception:
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
    except Exception:
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


_ALLOWED_PATH_BULLET_RE = re.compile(r"^\s*[-+*]\s+")
_ALLOWED_PATH_CODE_RE = re.compile(r"^`([^`]+)`\s*(?:[（(].*)?$")


def _normalize_allowed_path_entry(line: str) -> str:
    """Normalize one Allowed Paths list entry to a bare path string.

    Handles:
    - bullet markers: -, +, * (GFM spec)
    - backtick-wrapped: `path`（注釈）or `path` (description)
    - trailing full-width/half-width parens annotation
    """
    s = line.strip()
    # Strip bullet marker (-, +, *)
    s = _ALLOWED_PATH_BULLET_RE.sub("", s)
    s = s.strip()
    # Strip backtick wrapping with optional annotation
    m = _ALLOWED_PATH_CODE_RE.match(s)
    if m:
        return m.group(1).strip()
    # Strip trailing full-width or half-width paren annotation (no backtick)
    s = re.sub(r"\s*[（(][^）)]*[）)]\s*$", "", s)
    return s.strip()


def extract_allowed_paths(body: str) -> List[str]:
    """
    Parse the `## Allowed Paths` section from an Issue body.

    Returns a list of path strings (stripped, without leading '- ').
    Empty list if section not found or no paths listed.

    Example section:
      ## Allowed Paths
      - .claude/skills/issue-contract-review/scripts/baseline_vc_preflight.py
      - .claude/skills/issue-contract-review/tests/
    """
    match = re.search(
        r"^##\s+Allowed Paths\s*$(.+?)(?=^##|\Z)",
        body,
        re.MULTILINE | re.DOTALL,
    )
    if not match:
        return []

    section = match.group(1)
    paths = []
    for line in section.splitlines():
        stripped = line.strip()
        # Lines starting with a bullet marker (-, +, *) are list items
        if _ALLOWED_PATH_BULLET_RE.match(stripped):
            path = _normalize_allowed_path_entry(stripped)
            if path:
                paths.append(path)
        # Also handle lines without bullet prefix (plain paths or backtick-wrapped)
        elif stripped and not stripped.startswith("#"):
            path = _normalize_allowed_path_entry(stripped)
            if path:
                paths.append(path)

    return paths


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
    marker, _ = parse_preflight_scope_marker_line(prev_line)
    return marker


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
        marker, _ = parse_preflight_scope_marker_line(line)
        if marker is not None:
            continue

        # AC marker line (# AC1 etc): transparent (allowed in the same block)
        ac_label, is_valid = parse_ac_marker_line(line)
        if ac_label is not None and is_valid:
            continue

        # Any other line (regular comment or non-comment non-command): stop scanning
        break

    return found_annotation


def parse_commands_from_block(
    block: str,
) -> List[
    Tuple[
        Optional[str], str, int, Optional[str],
        Optional[str], Optional[str], Optional[str],
        Optional[int], Optional[str],
    ]
]:
    """
    bash ブロックからコマンドを抽出。
    AC マーカーとコマンドの行番号と preflight-scope marker と vc-regex-intent annotation を返す。

    戻り値: [(ac_label, command, line_number, preflight_scope, vc_regex_intent,
               baseline_expect, vc_role, annotation_line_no, annotation_raw), ...]
      - ac_label: "AC1", "AC2", ... または None
      - command: raw command ($ prefix 除去済み、suffix marker 除去済み)
      - line_number: block 内での行番号
      - preflight_scope: 'pr_review_only' / 'runtime_only' / None
      - vc_regex_intent: 'literal-pipe-ok' / None (AC3: Issue #589)
      - baseline_expect: 'pass' / 'fail' / 'deferred' / None (Issue #889)
      - vc_role: role string / None (Issue #889)
      - annotation_line_no: 1-based line number of baseline-expect annotation / None
      - annotation_raw: raw annotation line text / None
    """
    commands = []
    lines = block.split("\n")
    current_ac = None

    for i, line in enumerate(lines, start=1):
        # AC マーカーの抽出: `# AC<N>`（strict）
        ac_label, is_valid = parse_ac_marker_line(line)
        if ac_label is not None:
            if is_valid:
                current_ac = ac_label
            # strict marker lines are annotation, not commands
            if line.strip().startswith("#"):
                continue

        # preflight-scope marker はスキップ（コマンドではない）
        preflight_scope_marker, _ = parse_preflight_scope_marker_line(line)
        if preflight_scope_marker is not None:
            continue

        # vc-regex-intent annotation はスキップ（コマンドではない）
        if re.match(r"^\s*#\s*vc-regex-intent:\s*\S+", line):
            continue

        if line.strip().startswith("#"):
            # その他のコメント行はコマンドではない
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
                suffix_match = re.search(r"\s+#\s*(.+)\s*$", cmd)
                if suffix_match:
                    suffix_label, suffix_is_valid = parse_ac_marker_line(f"# {suffix_match.group(1)}")
                    if suffix_label is not None and suffix_is_valid:
                        current_ac = suffix_label
                        cmd = re.sub(r"\s+#\s*AC\d+\s*$", "", cmd).strip()

            # 直前行から preflight-scope marker を抽出
            preflight_scope = extract_preflight_scope_marker(lines, i - 1)

            # 直前行から vc-regex-intent annotation を抽出 (AC3: Issue #589)
            vc_regex_intent = extract_vc_regex_intent_annotation(lines, i - 1)

            # 直前の連続 annotation ブロックから baseline-expect / vc-role を抽出 (Issue #889)
            baseline_expect, annotation_line_no, annotation_raw = extract_baseline_expect_annotation(lines, i - 1)
            vc_role = extract_vc_role_annotation(lines, i - 1)

            commands.append((
                current_ac, cmd, i, preflight_scope,
                vc_regex_intent, baseline_expect, vc_role,
                annotation_line_no, annotation_raw,
            ))

    return commands


def compute_command_hash(command: str) -> str:
    """コマンドの SHA-256 hash を計算"""
    return hashlib.sha256(command.encode("utf-8")).hexdigest()


def compute_execution_key_hash(
    argv: List[str],
    cwd: str,
    runner_env_delta: Dict[str, str],
    timeout_seconds: int,
    state_epoch: int = 0,
) -> str:
    """Issue #1338: dedup key covering normalized argv + cwd + runner_env_delta + timeout + state_epoch.

    PR #1508 review (P0-1): the execution key MUST also cover a `state_epoch`
    -- a monotonically increasing counter that advances every time a
    "barrier" command (any command that is not a validated pure observation;
    see _is_parallel_eligible_command) executes. Without this, two
    textually-identical, side-effect-free observations (e.g.
    `test -d fixture/__pycache__`) that bracket a stateful command (e.g.
    `python3 -m py_compile fixture/state_probe.py`) would previously hash
    identically and the second observation would be incorrectly replayed
    from the first's stale pre-barrier snapshot instead of being re-executed
    against the new on-disk state.

    Distinct from compute_command_hash() (raw command string hash; existing
    behavior/meaning unchanged). Two commands whose execution_key_hash matches
    are guaranteed to run under identical conditions (same argv / cwd / env /
    timeout / state_epoch), so the second invocation may safely replay the
    first's execution outcome (exit_code/stdout/stderr/duration_ms/
    runner_env_delta) instead of re-launching the subprocess. classify_result()
    /decision are NEVER copied from the source; callers must recompute them
    per-AC using each AC's own raw_command/annotations.
    """
    normalized_cwd = os.path.normpath(os.path.abspath(cwd))
    normalized_env = sorted(runner_env_delta.items())
    payload = json.dumps(
        {
            "argv": list(argv),
            "cwd": normalized_cwd,
            "env": normalized_env,
            "timeout_seconds": timeout_seconds,
            "state_epoch": state_epoch,
        },
        sort_keys=True,
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _rg_operand_is_safe_target(cwd: str, operand: str, allowed_paths: List[str]) -> bool:
    """Issue #1338 P0-2 fix (PR #1508 review): a single `rg` PATH operand is
    only a safe (bounded, non-racy) parallel/dedup target if it is a
    repo-relative, existing, non-special filesystem entry that is either a
    regular file (any repo-relative location) or a directory contained
    within the Issue's `## Allowed Paths` (recursion is bounded to that
    Allowed Path, never to the whole repo). FIFOs, sockets, and device
    files are always rejected; `-` (stdin) is rejected by the caller before
    reaching here.
    """
    if operand == "-":
        return False
    normalized = _normalize_repo_relative_path_strict(operand)
    if normalized is None:
        return False
    try:
        resolved_cwd = Path(cwd).resolve(strict=True)
    except OSError:
        return False
    resolved_target = (resolved_cwd / normalized).resolve(strict=False)
    try:
        resolved_target.relative_to(resolved_cwd)
    except ValueError:
        return False  # escapes the repo root
    try:
        st = os.stat(resolved_target)
    except OSError:
        return False  # must already exist (unverifiable targets are not "pure")
    mode = st.st_mode
    if stat.S_ISFIFO(mode) or stat.S_ISSOCK(mode) or stat.S_ISBLK(mode) or stat.S_ISCHR(mode):
        return False
    if stat.S_ISREG(mode):
        return True
    if stat.S_ISDIR(mode):
        return _rg_path_operands_all_within_allowed_paths([operand], allowed_paths or [])
    return False


def _validate_rg_parallel_command(command: str, cwd: str, allowed_paths: List[str]) -> bool:
    """Issue #1338 P0-2 fix (PR #1508 review): full validation gate for an
    `rg` command before it may be treated as a pure/dedup/parallel-eligible
    observation.

    Requires ALL of:
      - >= 1 explicit PATH operand (a bare `rg PATTERN` with no path is
        rejected: it would search the whole repo AND, with no path operand
        and no piped input, `rg` reads from stdin -- subprocess.Popen
        defaults to inheriting the parent's stdin, so concurrent `rg`
        invocations with no explicit path are a stdin race).
      - no `-` (explicit stdin) operand.
      - every operand is a repo-relative, existing, non-special target,
        validated by _rg_operand_is_safe_target() (rejects absolute paths,
        `..` traversal, repo-external targets, FIFOs/devices/sockets, and
        unbounded directory recursion outside `## Allowed Paths`).
    """
    try:
        argv = shlex.split(command)
    except ValueError:
        return False
    if not argv or Path(argv[0]).name != "rg":
        return False
    path_operands = _rg_extract_path_operands(argv)
    if not path_operands:
        return False
    return all(_rg_operand_is_safe_target(cwd, op, allowed_paths) for op in path_operands)


def _is_parallel_eligible_command(
    command: str, cwd: str = ".", allowed_paths: Optional[List[str]] = None
) -> bool:
    """Issue #1338 AC5, narrowed by PR #1508 review (P0-1/P0-2): predicate
    for bounded-parallel/dedup-safe PURE read-only VC observations.

    Only two shapes are eligible:
      - exact 'test -f|-d|-s PATH' (3 argv tokens)
      - `rg` with a fully validated bounded path operand (see
        _validate_rg_parallel_command)

    `grep` / `egrep` / `fgrep` are INTENTIONALLY EXCLUDED (P0-2 fix): the
    prior basename-only classification allowed them into the parallel/dedup
    pool with no argument-count / path-operand / stdin validation at all,
    and subprocess.Popen defaults to inheriting the parent's stdin, so
    concurrent invocations without an explicit path operand could race on
    shared stdin. They remain executable (still listed in _ALLOWED_COMMANDS
    for serial preflight execution) but are never batched or dedup-replayed.

    Everything else (pnpm, uv run pytest, pytest, gh, git,
    github_metadata_assert, python3 -m py_compile, etc.) is treated as a
    state barrier by the caller: never eligible for dedup or concurrent
    execution (rate-limit / cache-contention / state-mutation concerns).
    """
    try:
        argv = shlex.split(command)
    except ValueError:
        return False
    if not argv:
        return False
    basename = Path(argv[0]).name
    if basename == "test" and len(argv) == 3 and argv[1] in ("-f", "-d", "-s"):
        return True
    if basename == "rg":
        return _validate_rg_parallel_command(command, cwd, allowed_paths or [])
    return False


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


def run_command(command: str, timeout_seconds: int, cwd: str) -> Tuple[int, str, str, int, Dict[str, str]]:
    """
    コマンドを単体実行。

    戻り値: (exit_code, stdout, stderr, duration_ms, runner_env_delta)
    """
    try:
        # shlex.split で argv を構築（shell=False で安全に実行）
        argv = shlex.split(command)
    except ValueError:
        # shlex.split に失敗した場合は compound command の可能性
        return -1, "", "shlex.split failed", 0, {}

    pnpm_evidence = None
    if pnpm_gate_registry.looks_like_pnpm(argv):
        launch_argv, pnpm_evidence, gate_error = pnpm_gate_registry.prepare_launch(argv, cwd)
        if gate_error or launch_argv is None:
            return -1, "", gate_error or "pnpm_gate:launch_preparation_failed", 0, {}
        argv = launch_argv
        env_delta = dict(pnpm_evidence["runner_env_delta"])
    else:
        env_delta = _fixed_env_delta_for_argv(argv)
    run_env = None
    if env_delta:
        run_env = os.environ.copy()
        run_env.update(env_delta)

    start = datetime.now()
    try:
        result = subprocess.run(
            argv,
            capture_output=True,
            text=True,
            timeout=timeout_seconds,
            cwd=cwd,
            shell=False,
            env=run_env,
            # Issue #1338 P0-2 fix (PR #1508 review): explicit stdin=DEVNULL.
            # subprocess.run() otherwise defaults to inheriting the parent's
            # stdin, which is a race hazard when multiple VC subprocesses
            # (e.g. two `rg` invocations) may run concurrently under the
            # bounded-parallel executor.
            stdin=subprocess.DEVNULL,
        )
        duration_ms = int((datetime.now() - start).total_seconds() * 1000)
        return result.returncode, result.stdout, result.stderr, duration_ms, env_delta
    except subprocess.TimeoutExpired:
        duration_ms = int((datetime.now() - start).total_seconds() * 1000)
        return -1, "", "timeout", duration_ms, env_delta
    except Exception as e:
        duration_ms = int((datetime.now() - start).total_seconds() * 1000)
        return -1, "", str(e), duration_ms, env_delta


def _pnpm_gate_evidence_for_command(command: str, cwd: str) -> Optional[Dict[str, Any]]:
    """Produce consumer-verifiable gate evidence without a fallback pathway."""
    try:
        argv = shlex.split(command)
    except ValueError:
        return None
    evidence, error = pnpm_gate_registry.evidence_for_request(argv, cwd)
    return evidence if error is None else None


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


def _is_uv_lock_check(argv: List[str]) -> bool:
    """Return True only for exact `uv lock --check`."""
    return list(argv) == ["uv", "lock", "--check"]


_CANONICAL_RUNTIME_SMOKE_ARGVS: tuple = (
    ["uv", "run", "--isolated", "--locked", "--no-default-groups", "python", "scripts/ci/runtime_dependency_smoke.py"],
    ["uv", "run", "--isolated", "--locked", "--no-default-groups", "python3", "scripts/ci/runtime_dependency_smoke.py"],
)


def _is_uv_runtime_smoke_command(argv: List[str]) -> bool:
    """Return True only for the two canonical runtime smoke command forms."""
    return list(argv) in list(_CANONICAL_RUNTIME_SMOKE_ARGVS)


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


_MISSING_NODE_ID_ERROR_RE = re.compile(
    r"error:\s+not found:\s+(?P<nodeid>\S+::\S+)",
    re.IGNORECASE,
)
_NO_MATCH_IN_MODULE_RE = re.compile(
    r"no match in any of"
)


def _is_existing_file_missing_node_id_error(
    stdout: str, stderr: str, cwd: Optional[str] = None
) -> bool:
    """Issue #1347: detect pytest's "existing file, missing node-id" error shape.

    Distinguishes the case where a pytest node-id references a file that DOES
    exist on disk but a test function (or class::function) within it that does
    NOT exist, e.g.:
        ERROR: not found: /path/existing_file.py::test_new_case
        (no match in any of [<Module existing_file.py>])
    from the genuinely-missing-file case, whose pytest error is instead:
        ERROR: file or directory not found: missing_file.py
    Callers must ensure this is only invoked in a context where the FILE part
    is already known to exist (e.g. after _is_regression_gate_command()
    returned True); this helper additionally re-validates that the file part
    of the node-id actually exists on disk (case-insensitively matching the
    "not found" error text and requiring the "no match in any of" companion
    text), so it does not rely solely on the caller's prior check.

    PR #1366 review (Major 3): the error-message match is now case-insensitive
    and requires BOTH the missing-node-id error text AND the "no match in any
    of" companion text (AND, not OR) before re-deriving file existence from
    the node-id's file part, resolved against `cwd`.
    """
    combined = f"{stdout}\n{stderr}"
    match = _MISSING_NODE_ID_ERROR_RE.search(combined)
    if not match:
        return False
    if not _NO_MATCH_IN_MODULE_RE.search(combined):
        return False

    file_part = match.group("nodeid").split("::", 1)[0]
    p = Path(file_part)
    if not p.is_absolute():
        p = Path(cwd or Path.cwd()) / p
    return p.exists()


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
    if _canonical_pnpm_gate(argv_check) is not None:
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
    # Issue #1347: pytest positional args may be node-ids of the form
    # <file>::<test_name> (or <file>::<Class>::<test_name>). The node-id
    # suffix after "::" is never itself a filesystem path, so it must be
    # stripped before the existence check; otherwise a command referencing a
    # NEW test function on an EXISTING file (e.g. existing_file.py::test_new)
    # is incorrectly treated as "path does not exist" even though the file
    # portion is real.
    for arg in positional_args:
        file_part = arg.split("::", 1)[0]
        # Handle both relative and absolute paths
        if Path(file_part).is_absolute():
            test_path = Path(file_part)
        else:
            test_path = Path(cwd) / file_part
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


def _rg_has_include_option(argv: List[str]) -> bool:
    """
    AC1 (Issue #648): Detect if an `rg` command uses the `--include` / `--include=...` option.

    `rg` does not have an `--include` option (that is a grep option, not ripgrep).
    When a VC author writes `rg --include=...`, it is likely a mistake (confused with grep).

    Rules:
    - Only applies when argv[0] basename is 'rg'
    - Matches '--include' (standalone) or '--include=...' (value-embedded)
    - Does NOT match '--include-zero' (a different option with different semantics)
    - grep --include is NOT matched (only rg is in scope)

    Returns True if the rg command has an --include option, False otherwise.
    """
    if not argv:
        return False
    if Path(argv[0]).name != "rg":
        return False
    for arg in argv[1:]:
        # Match --include or --include=... but NOT --include-zero
        if arg == "--include":
            return True
        if arg.startswith("--include="):
            return True
        # --include-zero: explicitly excluded
    return False


def _rg_extract_path_operands(argv: List[str]) -> List[str]:
    """
    Extract positional PATH operands from an `rg` argv, correctly skipping
    PATTERN, value-taking flags (`-e`/`--regexp`, `-g`/`--glob`, etc.), and
    honoring `--` as an end-of-options marker.

    This is the mature argv parser originally embedded in
    `_rg_has_broad_search_path()` (Issue #648), extracted so it can be reused
    by other callers (e.g. `_candidate_new_allowed_path_target()`, Issue #1328)
    without duplicating the parsing logic.

    Does NOT validate argv[0] is 'rg'; callers are expected to check that
    themselves (e.g. via `Path(argv[0]).name == "rg"`).

    Returns the list of raw (un-normalized) path operand strings, in the
    order they appear in argv. Returns an empty list if no path operand is
    found (i.e. the command searches the entire repo from cwd).
    """
    if not argv:
        return []

    # Collect positional path arguments (non-option, non-pattern args after pattern)
    # For rg: rg [options] PATTERN [PATH ...]
    # We need to identify the positional args that are paths (after the first positional=PATTERN)
    i = 1
    n = len(argv)
    after_double_dash = False
    first_positional_seen = False
    path_args: List[str] = []

    # Value-taking flags for rg (we need to skip them to find positional args).
    # NOTE: -l / --files-with-matches is a BOOLEAN flag (not value-taking).
    # Including it in value-taking flags would cause the next positional (the pattern)
    # to be consumed as a value, breaking path extraction.
    _RG_VALUE_FLAGS_FOR_PATH = frozenset([
        "-e", "--regexp",
        "-g", "--glob",
        "--iglob",
        "-A", "--after-context",
        "-B", "--before-context",
        "-C", "--context",
        "-m", "--max-count",
        "-M", "--max-columns",
        "--max-depth",
        "--color", "--colours",
        "--type", "-t",
        "--type-not", "-T",
        "--encoding",
        "--field-match-separator",
        "--field-context-separator",
        "--replace", "-r",
        "--pre",
        "--include",   # (invalid for rg but skip gracefully)
        "--exclude",
        "--sortr", "--sort",
        "--threads", "-j",
        "--max-filesize",
        "--context-separator",
        "-f", "--file",  # Issue #1328 Blocker 3: -f/--file PATTERNFILE is value-taking
        # NOTE: -l (--files-with-matches) is boolean, intentionally excluded here
    ])

    # Issue #1328 Blocker 3: -f/--file PATTERNFILE supplies the pattern from a
    # file, just like -e/--regexp supplies it inline. In both cases there is
    # no positional PATTERN argument to skip, so all remaining positionals
    # are PATH operands.
    explicit_pattern_given = any(
        arg in ("-e", "--regexp", "-f", "--file")
        or arg.startswith("--regexp=")
        or arg.startswith("--file=")
        or (arg.startswith("-e") and len(arg) > 2)
        or (arg.startswith("-f") and len(arg) > 2 and not arg.startswith("--"))
        for arg in argv[1:]
    )

    while i < n:
        arg = argv[i]

        if arg == "--":
            after_double_dash = True
            i += 1
            continue

        if after_double_dash:
            if not first_positional_seen and not explicit_pattern_given:
                # First arg after -- is PATTERN (skip it)
                first_positional_seen = True
                i += 1
                continue
            else:
                # These are PATHs
                path_args.append(arg)
                i += 1
                continue

        # Handle -e/--regexp: skip value (it's a pattern, not a path)
        if arg in ("-e", "--regexp"):
            explicit_pattern_given = True
            i += 2
            continue
        if arg.startswith("--regexp="):
            explicit_pattern_given = True
            i += 1
            continue
        if arg.startswith("-e") and len(arg) > 2:
            explicit_pattern_given = True
            i += 1
            continue

        # Skip -g/--glob and value
        if arg in ("-g", "--glob", "--iglob"):
            i += 2
            continue
        if arg.startswith("--glob=") or arg.startswith("--iglob="):
            i += 1
            continue

        # Skip other value-taking flags
        if arg in _RG_VALUE_FLAGS_FOR_PATH:
            i += 2
            continue

        # Handle --flag=value forms
        skip_flag_value = False
        for flag in _RG_VALUE_FLAGS_FOR_PATH:
            if flag.startswith("--") and arg.startswith(flag + "="):
                skip_flag_value = True
                break
        if skip_flag_value:
            i += 1
            continue

        # Combined short flags (e.g. -nq, -lq): just a flag, no value taken
        if arg.startswith("-") and not arg.startswith("--") and len(arg) > 1:
            i += 1
            continue

        # Positional argument
        if not first_positional_seen and not explicit_pattern_given:
            # This is the PATTERN (skip)
            first_positional_seen = True
            i += 1
            continue
        else:
            # This is a PATH argument
            path_args.append(arg)
            i += 1
            continue

    return path_args


def _rg_has_broad_search_path(argv: List[str], allowed_paths: Optional[List[str]] = None) -> bool:
    """
    AC2 (Issue #648): Detect if an `rg` command has a broad or unbounded search path.

    Containment-based logic (replaces fixed _BROAD_RG_PATHS list):

    - No positional path argument → broad (ブロック)
    - '.' or '/' path → always broad (ブロック)

    When allowed_paths is given (from Issue body ## Allowed Paths):
    - rg_path == allowed_path (same) → 許可
    - rg_path is a parent of some allowed_path (allowed.startswith(rg_path+"/")) → ブロック (broad)
    - allowed_path is a parent of rg_path (rg_path.startswith(allowed+"/")) → 許可 (narrowed)
    - rg_path not covered by any allowed_path at all → ブロック

    When allowed_paths is None or empty (fallback):
    - Only '.' and '/' are blocked (conservative behavior preserved).

    This function only inspects rg commands (argv[0] basename 'rg').
    grep / egrep / fgrep are NOT in scope.

    Returns True if the rg command has a broad/unbounded search path.
    """
    if not argv:
        return False
    if Path(argv[0]).name != "rg":
        return False

    # Path operand extraction is delegated to _rg_extract_path_operands()
    # (Issue #1328: shared argv parser, no duplicated parsing logic).
    path_args = _rg_extract_path_operands(argv)

    # No path arguments: searches the entire repo → broad
    if not path_args:
        return True

    # Normalize: strip trailing slash for comparison
    def _normalize_path(p: str) -> str:
        return p.rstrip("/")

    # Build allowed path set (normalized) from allowed_paths
    allowed_normalized: List[str] = []
    if allowed_paths:
        for ap in allowed_paths:
            allowed_normalized.append(_normalize_path(ap))

    for path_arg in path_args:
        normalized = _normalize_path(path_arg)

        # '.' or '/' always broad regardless of allowed_paths
        if normalized in (".", "", "/"):
            return True

        if not allowed_normalized:
            # Fallback (no Allowed Paths in Issue): only '.' and '/' are blocked.
            # Any explicit path is conservatively allowed.
            continue

        # Containment check using allowed_paths:
        # - rg_path == allowed_path → allowed (same target)
        # - allowed_path starts with rg_path+"/" → rg_path is a PARENT of allowed → broad (blocked)
        # - rg_path starts with allowed_path+"/" → rg_path is UNDER allowed → allowed (narrowed)
        is_covered = False
        is_parent_of_allowed = False
        for ap in allowed_normalized:
            if normalized == ap:
                is_covered = True
                break
            if normalized != "" and ap.startswith(normalized + "/"):
                # rg_path is a parent directory of some allowed_path → broad
                is_parent_of_allowed = True
            if normalized.startswith(ap + "/"):
                # rg_path is a sub-path of allowed_path → narrowed, covered
                is_covered = True
                break

        if is_parent_of_allowed and not is_covered:
            return True
        if not is_covered and not is_parent_of_allowed:
            # Path not covered by any allowed_path at all → broad
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
# Compatibility names are derived from the shared registry; they are not
# independent allowlists.
_ALLOWED_PNPM_SUBCOMMANDS: frozenset = frozenset(
    gate.request_argv for gate in pnpm_gate_registry.iter_gate_descriptors()
)
_FIXED_ENV_DELTA_BY_COMMAND: Dict[Tuple[str, str], Dict[str, str]] = {
    gate.request_argv: dict(pnpm_gate_registry.RUNNER_ENV_DELTA)
    for gate in pnpm_gate_registry.iter_gate_descriptors()
}

_PNPM_NO_TTY_ERROR_PATTERNS: tuple[str, ...] = (
    "ERR_PNPM_ABORTED_REMOVE_MODULES_DIR_NO_TTY",
    "Aborted removal of modules directory due to no TTY",
)

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
    "env",       # display-only; command wrapper/env injection denied by _is_allowed_env_invocation()
    "printenv",  # safe (read env)
    "pwd",       # safe
    "date",      # safe
    "stat",      # read-only
    # NOTE: mkdir removed from allowlist (B2) — mkdir -p .git/hooks and similar mutations possible
])


def _canonical_pnpm_gate(argv: List[str]) -> Optional[Tuple[str, str]]:
    """Return the canonical 2-token pnpm gate tuple, or None if not exact."""
    descriptor = pnpm_gate_registry.gate_for_request(argv)
    return descriptor.request_argv if descriptor else None


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
    return _canonical_pnpm_gate(argv) is not None


def _is_allowed_env_invocation(argv: List[str]) -> bool:
    """Allow `env` only for read-only display, never as a command wrapper."""
    if not argv or Path(argv[0]).name != "env":
        return False
    if len(argv) == 1:
        return True
    return len(argv) == 2 and argv[1] in ("--help", "--version")


def _fixed_env_delta_for_argv(argv: List[str]) -> Dict[str, str]:
    """Return a fixed runner-side env delta for exact safe commands only."""
    key = _canonical_pnpm_gate(argv)
    if key is None:
        return {}
    return dict(pnpm_gate_registry.RUNNER_ENV_DELTA)


def _is_package_manager_no_tty_prompt(command: str, stdout: str, stderr: str) -> bool:
    """Detect package-manager no-TTY prompts that are tooling/env blockers."""
    try:
        argv = shlex.split(command)
    except ValueError:
        return False

    if _canonical_pnpm_gate(argv) is None:
        return False

    combined = f"{stdout}\n{stderr}"
    return any(pattern in combined for pattern in _PNPM_NO_TTY_ERROR_PATTERNS)


# ---------------------------------------------------------------------------
# ---------------------------------------------------------------------------
# GitHub metadata assertion — Issue #942
# ---------------------------------------------------------------------------

# Allowed milestone metadata fields for github_metadata_assert (Issue #942 review BLOCKER 1).
# A field outside this allowlist (e.g. a 'description' typo like 'descripton') is rejected at
# classify time rather than silently treated as an absent field, which would let not_contains
# false-pass.
_ALLOWED_GITHUB_METADATA_FIELDS = {"description"}


def _is_github_metadata_assert_command(command: str) -> bool:
    """
    Detect if a command is a github_metadata_assert assertion.

    Format: github_metadata_assert <contains|not_contains> <field> <literal> <endpoint> [flags...]

    Returns True if the command starts with 'github_metadata_assert'.
    """
    try:
        argv = shlex.split(command, posix=True)
    except ValueError:
        return False

    if not argv:
        return False

    return Path(argv[0]).name == "github_metadata_assert"


def _is_allowed_github_metadata_assert(argv: List[str]) -> Optional[Tuple[bool, Optional[str]]]:
    """
    Validate a github_metadata_assert command (Issue #942).

    Format (exactly 4 arguments after the command name; NO flags, NO extra positional):
        github_metadata_assert <contains|not_contains> <field> <literal> <endpoint>

    Returns: (is_valid, error_message)
      - (True, None): command is valid
      - (False, error_msg): command is invalid with explanation

    Validation rules:
    - Exactly 5 argv tokens (command + assertion_type + field + literal + endpoint).
      Any extra token is rejected. Because no flag surface is accepted at all, every
      dangerous flag (-f/-F/--field/--raw-field/--input/--header/-H/--include/-i/
      --paginate/--slurp/--cache/--template/--preview/graphql, in any case variant) and
      every mutating method (--method POST/PATCH/PUT/DELETE, -X ..., --method=...) is
      rejected here by construction (review BLOCKER 2 / MAJOR 1).
    - assertion_type must be contains or not_contains.
    - field must be in _ALLOWED_GITHUB_METADATA_FIELDS; a typo or unknown field is rejected
      rather than silently treated as an absent field (review BLOCKER 1 / MAJOR 2).
    - endpoint must match repos/<owner>/<repo>/milestones/<number>; absolute URLs, query
      strings, path traversal and placeholders are rejected (AC2).
    """
    if len(argv) != 5:
        return False, (
            "github_metadata_assert accepts exactly 4 arguments "
            "(assertion_type field literal endpoint) and no flags; got "
            f"{max(len(argv) - 1, 0)}"
        )

    cmd_name = Path(argv[0]).name
    if cmd_name != "github_metadata_assert":
        return False, "Not a github_metadata_assert command"

    assertion_type = argv[1].lower()
    if assertion_type not in ("contains", "not_contains"):
        return False, f"assertion_type must be 'contains' or 'not_contains', got '{argv[1]}'"

    field = argv[2]
    if field not in _ALLOWED_GITHUB_METADATA_FIELDS:
        return False, (
            f"field '{field}' is not allowed; allowed fields: "
            f"{sorted(_ALLOWED_GITHUB_METADATA_FIELDS)}"
        )

    literal = argv[3]
    if not literal:
        return False, "literal argument is missing"

    endpoint = argv[4]

    # Validate endpoint (AC2)
    if endpoint.startswith('http://') or endpoint.startswith('https://') or endpoint.startswith('//'):
        return False, "endpoint must not be an absolute URL; use relative path like 'repos/owner/repo/milestones/1'"

    if '?' in endpoint:
        return False, "endpoint must not contain query strings (?)"

    if '..' in endpoint:
        return False, "endpoint must not contain path traversal (..)"

    if '<' in endpoint or '>' in endpoint:
        return (
            False,
            "endpoint must not contain placeholders (<...>); use actual values like 'repos/owner/repo/milestones/1'"
        )

    if not re.match(r'^repos/[^/]+/[^/]+/milestones/\d+$', endpoint):
        return False, f"endpoint must match 'repos/<owner>/<repo>/milestones/<number>', got '{endpoint}'"

    return True, None


def _check_github_metadata_assertion(
    assertion_type: str,
    field: str,
    literal: str,
    endpoint: str,
    timeout_seconds: int = 10,
) -> int:
    """
    Execute GitHub metadata assertion via gh api.

    Internal implementation for github_metadata_assert.
    Uses fixed argv to avoid shell injection: ["gh", "api", "--method", "GET", f"repos/..."]

    Args:
        assertion_type: "contains" or "not_contains"
        field: field name to check (e.g., "description")
        literal: literal string to search for
        endpoint: full endpoint path (e.g., "repos/owner/repo/milestones/1")
        timeout_seconds: timeout for gh command

    Returns:
        Exit code:
          - 0: assertion passed
          - 1: assertion failed (but gh API succeeded)
          - 2: gh command not found
          - 3: gh authentication failed
          - 4: 404 not found (resource doesn't exist)
          - 5: rate limited (429)
          - 6: timeout
          - 7: invalid JSON response
          - 8: other / unknown gh failure (network, 5xx, secondary rate limit, ...)
          - 9: requested field absent from API response (schema error)

    Raises:
        subprocess.TimeoutExpired: if gh command times out
        json.JSONDecodeError: if gh response is not valid JSON
    """
    argv = ["gh", "api", "--method", "GET", endpoint]

    try:
        result = subprocess.run(
            argv,
            capture_output=True,
            text=True,
            timeout=timeout_seconds,
            shell=False,
        )
    except FileNotFoundError:
        # gh command not found
        return 2
    except subprocess.TimeoutExpired:
        return 6

    # Check for HTTP errors via stderr/exit code
    if result.returncode != 0:
        stderr_lower = result.stderr.lower()
        if "not authenticated" in stderr_lower or "authentication failed" in stderr_lower or "401" in result.stderr:
            return 3
        if "404" in result.stderr or "not found" in stderr_lower:
            return 4
        if "429" in result.stderr or "rate limit" in stderr_lower:
            return 5
        # Any other nonzero gh exit is an environment / transport / API failure
        # (network error, 5xx, 403 secondary rate limit, gh version drift, ...),
        # NOT a semantic assertion failure. Never collapse it to exit 1 (review BLOCKER 3).
        return 8

    # Parse JSON response
    try:
        data = json.loads(result.stdout)
    except json.JSONDecodeError:
        return 7

    # The requested field must be present in the API response. A missing field
    # (schema drift / renamed field) is NOT 'literal absent': collapsing it to an empty
    # string would let not_contains false-pass (review BLOCKER 1). Treat it as a
    # schema/environment error distinct from assertion pass/fail.
    if field not in data:
        return 9
    raw_value = data.get(field)
    field_value = "" if raw_value is None else str(raw_value)

    # Perform assertion
    is_present = literal in field_value

    # Return exit code based on assertion type
    if assertion_type == "contains":
        # contains: present → 0, absent → 1
        return 0 if is_present else 1
    else:  # not_contains
        # not_contains: present → 1, absent → 0
        return 1 if is_present else 0


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
        # NOTE: -l / --files-with-matches is a BOOLEAN flag, NOT value-taking.
        # Do NOT include -l here — it would cause the pattern positional arg to be
        # consumed as the value of -l, breaking pattern detection.
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


def has_unquoted_inline_baseline_expect(command: str) -> bool:
    """AC2/AC12: detect an inline '# baseline-expect:' annotation embedded in a
    command string OUTSIDE any quoted region. Quoted literals such as
    `rg "# baseline-expect: pass" file` must NOT match."""
    in_single = False
    in_double = False
    i = 0
    n = len(command)
    while i < n:
        c = command[i]
        if c == "'" and not in_double:
            in_single = not in_single
        elif c == '"' and not in_single:
            in_double = not in_double
        elif c == "#" and not in_single and not in_double:
            rest = command[i + 1:].lstrip()
            if rest.startswith("baseline-expect:"):
                return True
        i += 1
    return False


def _candidate_new_allowed_path_target(
    command: str, allowed_paths: Optional[List[str]], cwd: Optional[str]
) -> Optional[str]:
    """AC4/AC14: if the command is `test -f|-e|-s PATH` or `rg ... PATH` whose PATH
    is within Allowed Paths and does NOT exist at the baseline cwd, return that
    PATH; otherwise None. Conservative shlex-based extraction; callers gate on
    classify_static_command() returning None first (so unsafe/broad are excluded)."""
    if not allowed_paths:
        return None
    try:
        argv = shlex.split(command)
    except ValueError:
        return None
    if not argv:
        return None
    prog = os.path.basename(argv[0])
    norm_allowed = [p.strip().lstrip("./").rstrip("/") for p in allowed_paths if p.strip()]

    def _in_allowed(p: str) -> bool:
        pp = p.lstrip("./")
        return any(pp == a or pp.startswith(a + "/") for a in norm_allowed)

    candidates: List[str] = []
    if prog == "test":
        for a in argv[1:]:
            if a.startswith("-"):
                continue
            candidates.append(a)
    elif prog == "rg":
        # Issue #1328 (AC7): use the mature argv parser shared with
        # _rg_has_broad_search_path() instead of a naive non_opt[1:] split,
        # so -e/--regexp/--glob/--/value-taking flags are handled correctly.
        candidates.extend(_rg_extract_path_operands(argv))
    else:
        return None

    base = cwd or "."
    for c in candidates:
        if _in_allowed(c) and not os.path.exists(os.path.join(base, c)):
            return c
    return None


def classify_static_command(
    raw_command: str, cwd: Path, allowed_paths: Optional[List[str]] = None
) -> Optional[Tuple[str, str, str, Optional[str], str]]:
    """
    Perform static pre-execution classification of a VC command.

    Returns (classification, category, decision, fix_hint, scope_class) if
    the command is blocked or can be determined statically without execution,
    or None if the command should proceed to run_command().

    This is called BEFORE run_command() to prevent dangerous commands from
    being executed. AC1-AC3 enforcement happens here.

    Args:
        raw_command: the raw command string from the VC block
        cwd: working directory
        allowed_paths: list of Allowed Paths from Issue body ## Allowed Paths section.
            Used by _rg_has_broad_search_path for containment-based broad path detection.
            If None or empty, falls back to conservative behavior (block '.' and '/' only).
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
    # NOTE: This check runs BEFORE broad_search_path_unbounded (3.6b) to preserve #589 behavior:
    # a command with both \\| in pattern AND a broad path is reported as regex_literal_pipe_suspected.
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

    # 3.6a. rg --include option mismatch detection (AC1: Issue #648)
    # rg does not have --include (that is a grep option). Using --include with rg is a VC mistake.
    # --include-zero is a valid rg option and is excluded from detection.
    # grep --include is valid (grep syntax) and is excluded.
    if _rg_has_include_option(argv):
        return (
            "blocked",
            "rg_option_mismatch",
            "blocked",
            "rg does not have --include / --include=... (that is a grep option). "
            "Use -g / --glob for file filtering in ripgrep (e.g., rg -g '*.py' pattern). "
            "If you intended grep, use grep --include instead.",
            "baseline_fail_expected",
        )

    # 3.6b. rg broad search path detection (AC2: Issue #648)
    # rg without path or with repo-root/broad-dir path searches the entire repo,
    # making the VC pass on unrelated existing assets.
    if _rg_has_broad_search_path(argv, allowed_paths=allowed_paths):
        return (
            "blocked",
            "broad_search_path_unbounded",
            "blocked",
            "rg search path is too broad (no path / repo root / broad directory like docs/ or src/). "
            "Narrow the search path to the Allowed Paths or a specific file/directory "
            "(e.g., rg 'pattern' .claude/skills/... or rg 'pattern' docs/product/specific-file.md). "
            "Broad searches may produce false positives from existing assets unrelated to this Issue.",
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

    # 6.5. Check github_metadata_assert: first-class GitHub metadata assertion (Issue #942)
    if _is_github_metadata_assert_command(raw_command):
        is_valid, error_msg = _is_allowed_github_metadata_assert(argv)
        if not is_valid:
            return (
                "blocked",
                "command_not_allowed",
                "blocked",
                f"github_metadata_assert validation failed: {error_msg}",
                "baseline_fail_expected",
            )
        return None  # allowed github_metadata_assert command

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

    # 8.5. Check env: display-only allowlist
    if cmd_basename == "env":
        if not _is_allowed_env_invocation(argv):
            return (
                "blocked",
                "command_not_allowed",
                "blocked",
                "'env' is only allowed for read-only display (env, env --help, env --version). "
                "Do not use env as a wrapper or to inject arbitrary variables into commands. "
                "Shell env prefix forms like 'CI=true pnpm build' are also not allowed.",
                "baseline_fail_expected",
            )
        return None

    # 9. Check uv: only allow uv run pytest / uv run python -m pytest / uv run python3 -m pytest
    if cmd_basename == "uv":
        if len(argv) >= 2 and argv[1] == "lock":
            if _is_uv_lock_check(argv):
                return None
            return (
                "blocked",
                "command_not_allowed",
                "blocked",
                "uv lock subcommand in VC preflight is allowed only as 'uv lock --check'.",
                "baseline_fail_expected",
            )

        if len(argv) >= 2 and argv[1] == "run":
            # Runtime smoke canonical allowlist (raw argv exact shape)
            if _is_uv_runtime_smoke_command(argv):
                return None

            unwrapped = _strip_uv_run_options(argv)
            if (
                unwrapped
                and Path(unwrapped[0]).name in ("python", "python3")
                and len(unwrapped) >= 2
                and unwrapped[1] in ("-m", "-c")
                and any(
                    opt in argv
                    for opt in ("--isolated", "--locked", "--no-default-groups")
                )
            ):
                return (
                    "blocked",
                    "command_not_allowed",
                    "blocked",
                    (
                        "runtime-smoke options require the exact canonical script target; "
                        "python -m/-c runtime forms are not allowed."
                    ),
                    "baseline_fail_expected",
                )
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
                "only 'uv run pytest', 'uv run python3 -m pytest', "
                "'uv run --isolated --locked --no-default-groups python "
                "scripts/ci/runtime_dependency_smoke.py', and "
                "'uv run --isolated --locked --no-default-groups python3 "
                "scripts/ci/runtime_dependency_smoke.py' are allowed.",
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


# Issue #1328: whitelist of stderr patterns that indicate `rg` failed because the
# target PATH does not exist (missing file/directory), as opposed to a
# regex/option/config/permission error.
_RG_STDERR_MISSING_PATH_PATTERNS = (
    re.compile(r"No such file or directory"),
    re.compile(r"\(os error 2\)"),
)

# Issue #1328: blacklist of stderr patterns that indicate `rg` failed for a
# reason OTHER than a missing path (invalid regex / unsupported option /
# config error / permission denied). If any of these match, the failure must
# NOT be treated as a deterministic "missing path" expected_fail, even if a
# missing-path pattern also happens to be present in the same stderr (AC10).
_RG_STDERR_ERROR_NOT_MISSING_PATH_PATTERNS = (
    re.compile(r"regex parse error"),
    re.compile(r"unrecognized flag"),
    re.compile(r"unrecognized option"),
    re.compile(r"error:\s*unclosed"),
    re.compile(r"error:\s*found argument"),
    re.compile(r"error:\s*invalid"),
    re.compile(r"Permission denied"),
    re.compile(r"\(os error 13\)"),
    re.compile(r"bad config"),
)


def _rg_stderr_indicates_missing_path(stderr: str) -> bool:
    """Issue #1328: True if stderr matches a known 'target path does not exist'
    pattern for `rg` (e.g. "No such file or directory (os error 2)")."""
    return any(pattern.search(stderr) for pattern in _RG_STDERR_MISSING_PATH_PATTERNS)


def _rg_stderr_indicates_error_not_missing_path(stderr: str) -> bool:
    """Issue #1328: True if stderr matches a known non-missing-path `rg` error
    pattern (invalid regex / unsupported option / config error / permission
    denied)."""
    return any(pattern.search(stderr) for pattern in _RG_STDERR_ERROR_NOT_MISSING_PATH_PATTERNS)


def _is_rg_missing_path_error(stderr: str) -> bool:
    """Issue #1328: True only if stderr strictly indicates a missing-path error
    (whitelist match) AND does not also match any non-missing-path error
    pattern (blacklist). AC10: whitelist+blacklist co-occurrence returns False
    (conservative; do not treat as deterministic missing-path expected_fail)."""
    return _rg_stderr_indicates_missing_path(stderr) and not _rg_stderr_indicates_error_not_missing_path(stderr)


def _normalize_repo_relative_path_strict(path: str) -> Optional[str]:
    """Issue #1328 (OWNER Blocker 2): strict repo-relative POSIX path
    normalization, mirroring `AllowedPathsMatcher.normalize_path()` in
    `.claude/skills/pr-review-judge/scripts/allowed_paths_review_gate.py`.
    Fail-closed: returns None (reject) for absolute paths, paths containing a
    backslash, `..` segments, and empty segments (e.g. `docs//new/file.md`).
    Reimplemented here (not imported) to keep this script's dependency
    surface self-contained; the two normalization rules are covered by
    parity tests and MUST stay in sync."""
    if not path:
        return None
    if "\\" in path:
        return None
    if path.startswith("/"):
        return None

    normalized = path[2:] if path.startswith("./") else path
    if normalized in ("", "."):
        return None
    segments = normalized.split("/")
    if ".." in segments:
        return None
    if "" in segments:
        return None
    return normalized


def _rg_path_operands_all_within_allowed_paths(path_operands: List[str], allowed_paths: List[str]) -> bool:
    """Issue #1328: True iff every rg path operand is equal to, or nested
    under, one of the (normalized) Allowed Paths. Empty path_operands ->
    False (a broad/whole-repo search is never "within" Allowed Paths).

    OWNER Blocker 2: both path operands and Allowed Paths entries are
    normalized via `_normalize_repo_relative_path_strict()` (fail-closed);
    absolute paths, backslashes, `..` segments, and empty segments are
    rejected instead of being accepted via naive string-prefix matching."""
    if not path_operands:
        return False
    norm_allowed: List[str] = []
    for ap in allowed_paths:
        candidate = ap.strip()
        if not candidate:
            continue
        if candidate != "/":
            candidate = candidate.rstrip("/")
        normalized_ap = _normalize_repo_relative_path_strict(candidate)
        if normalized_ap is not None:
            norm_allowed.append(normalized_ap)
    if not norm_allowed:
        return False

    def _in_allowed(p: str) -> bool:
        normalized_p = _normalize_repo_relative_path_strict(p)
        if normalized_p is None:
            return False
        return any(normalized_p == a or normalized_p.startswith(a + "/") for a in norm_allowed)

    return all(_in_allowed(p) for p in path_operands)


# Issue #1328 (OWNER Blocker 1): matches a single `rg` stderr line of the form
# `rg: <path>: No such file or directory (os error 2)`, capturing <path>, so
# the missing path(s) reported by rg can be compared against the argv path
# operands (preventing false-green on multi-path invocations where one path
# matched and produced stdout while a different path was missing).
_RG_STDERR_MISSING_PATH_LINE = re.compile(
    r"^rg:\s*(.+?):\s*No such file or directory(?:\s*\(os error 2\))?\s*$",
    re.MULTILINE,
)


def _rg_stderr_extract_missing_paths(stderr: str) -> List[str]:
    """Issue #1328 (OWNER Blocker 1): extract path(s) that rg reported as
    missing directly from stderr lines matching `_RG_STDERR_MISSING_PATH_LINE`.
    Returns raw (un-normalized) path strings in first-appearance order,
    deduplicated. Returns an empty list if no such line is found."""
    seen: List[str] = []
    for m in _RG_STDERR_MISSING_PATH_LINE.finditer(stderr):
        candidate = m.group(1).strip()
        if candidate and candidate not in seen:
            seen.append(candidate)
    return seen


# PR #1497 review (Blocker 1): only these commands have a path-operand
# parser implemented for current-head PASS target-scope certification.
# grep / egrep / fgrep / ack / ag / etc. are intentionally excluded until a
# dedicated path parser exists for them (closed allowlist, fail-closed).
_CURRENT_PASS_ELIGIBLE_TEST_FLAGS = frozenset(["-f", "-d", "-s"])


def _certified_path_exists_as_file(resolved_cwd: Path, normalized: str) -> bool:
    try:
        return (resolved_cwd / normalized).is_file()
    except OSError:
        return False


def certify_current_pass_command(
    command: str,
    exit_code: int,
    stdout: str,
    stderr: str,
    cwd: Optional[str],
    allowed_paths: Optional[List[str]],
) -> Tuple[bool, Optional[str]]:
    """PR #1497 review fix (Blocker 1 / Blocker 2): decide whether a
    non-regression-gate command's exit_code == 0 in current-head
    evidence-mode may be certified as a resolved current PASS.

    This is a CLOSED allowlist, not a generic "exit 0 means done" check:

    - Blocker 1 (unbound target scope): only `test -f|-d|-s <PATH>` and
      `rg PATTERN <PATH...>` are eligible. Every path operand MUST normalize
      to a strict repo-relative path (`_normalize_repo_relative_path_strict`)
      contained within `allowed_paths`, and MUST exist as a regular file at
      certification time. Absolute paths, `..`, backslashes, and repo-
      external state (e.g. `/tmp/vc-sentinel`) are rejected outright.
      `grep` / `egrep` / `fgrep` have no path-operand parser here and are
      therefore never certified (they keep the pre-existing
      unexpected_pass / blocked classification even in current-head mode).
    - Blocker 2 (quiet partial success): `stderr` must be empty. `rg -q` /
      `grep -q` can exit 0 (a match was found) while ALSO emitting a
      missing-path / permission-denied error for a different operand on
      stderr; requiring empty stderr rejects this "quiet partial success"
      case even when an error-suppression flag would otherwise hide the
      message from a text-pattern check. Filesystem existence is checked
      independently of stderr content for defense in depth.

    Returns (certified, reason). `reason` is a short machine string
    describing the rejection when `certified` is False; `None` when
    `certified` is True.
    """
    if exit_code != 0:
        return False, "exit_code_not_zero"
    if stderr:
        return False, "nonempty_stderr"
    if not allowed_paths:
        return False, "no_allowed_paths"
    try:
        argv = shlex.split(command, posix=True)
    except ValueError:
        return False, "argv_parse_failed"
    if not argv:
        return False, "empty_argv"

    resolved_cwd = Path(cwd) if cwd else Path(".")
    base = Path(argv[0]).name

    if base == "test":
        if len(argv) != 3 or argv[1] not in _CURRENT_PASS_ELIGIBLE_TEST_FLAGS:
            return False, "test_argv_not_exact_allowlisted_form"
        target = argv[2]
        normalized = _normalize_repo_relative_path_strict(target)
        if normalized is None:
            return False, "test_path_not_repo_relative"
        if not _rg_path_operands_all_within_allowed_paths([target], allowed_paths):
            return False, "test_path_outside_allowed_paths"
        if not _certified_path_exists_as_file(resolved_cwd, normalized):
            return False, "test_path_does_not_exist_at_certification_time"
        return True, None

    if base == "rg":
        path_operands = _rg_extract_path_operands(argv)
        if not path_operands:
            return False, "rg_no_path_operands"
        if not _rg_path_operands_all_within_allowed_paths(path_operands, allowed_paths):
            return False, "rg_path_outside_allowed_paths"
        for operand in path_operands:
            normalized = _normalize_repo_relative_path_strict(operand)
            if normalized is None:
                return False, "rg_path_not_repo_relative"
            if not _certified_path_exists_as_file(resolved_cwd, normalized):
                return False, "rg_path_not_a_regular_file_or_missing"
        return True, None

    # grep / egrep / fgrep / any other command: closed allowlist -- never
    # certified until a dedicated path-operand parser is implemented.
    return False, "command_not_in_current_pass_allowlist"


def _extract_certified_target_paths(command: str) -> List[str]:
    """Best-effort re-extraction of the path operands certified by
    `certify_current_pass_command()`, for inclusion in the producer result
    item (PR #1497 review Blocker 1: "producer 結果に、検証済み target path
    ... を含める"). Only called after certification already succeeded, so
    this does not need to re-validate anything -- it mirrors the same
    per-command extraction rules for observability."""
    try:
        argv = shlex.split(command, posix=True)
    except ValueError:
        return []
    if not argv:
        return []
    base = Path(argv[0]).name
    if base == "test" and len(argv) == 3:
        return [argv[2]]
    if base == "rg":
        return _rg_extract_path_operands(argv)
    return []


def classify_result(
    exit_code: int,
    stdout: str,
    stderr: str,
    command: str,
    cwd: Optional[str] = None,
    runner_env_delta: Optional[Dict[str, str]] = None,
    allowed_paths: Optional[List[str]] = None,
    static_policy_passed: bool = False,
    evidence_mode: str = "baseline",
) -> Tuple[str, str, str, Optional[str], str]:
    """
    VC 実行結果を分類。

    Args:
        exit_code: command exit code
        stdout: command standard output
        stderr: command standard error
        command: original command string
        cwd: working directory (threaded to _is_regression_gate_command)
        allowed_paths: Issue body ## Allowed Paths list (Issue #1328). Required
            (non-empty) for the deterministic `new_file_missing_expected`
            classification; without it, rg exit_code==2 never resolves to that
            category regardless of stderr content (AC9).
        static_policy_passed: whether classify_static_command() already ran and
            returned None (not statically blocked) for this command. Defaults
            to False (PR #1497 review Major 1: this is a safety-relevant flag
            and must fail closed; callers MUST explicitly pass True after
            confirming classify_static_command() returned None).
        evidence_mode: "baseline" (default, backward compatible) or
            "current-head" (Issue #1488). In current-head mode, a
            non-regression-gate VC (rg / test -f / test -s etc.) that passed
            static policy and exited 0 is classified as a certified current
            PASS (expected_pass / go) instead of unexpected_pass / blocked.
            baseline evidence-mode semantics are unchanged (AC1).

    戻り値: (classification, category, decision, fix_hint, scope_class)
      classification: expected_fail | unexpected_pass | blocked | human_judgment | expected_pass | skipped
      category: file_not_found_expected | expected_baseline_fail | unexpected_pass |
                env_missing_dep | file_not_found_unrunnable | timeout |
                compound_command_disallowed | unsupported_shell_syntax |
                unsafe_command | command_not_allowed | unknown | regression_gate |
                new_file_missing_expected | existing_file_missing_node_id_noncanonical |
                expected_pass_resolved_on_current_head
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
        return (
            "blocked",
            "compound_command_disallowed",
            "blocked",
            "Compound shell commands are not supported in initial implementation",
            "baseline_fail_expected"
        )

    # AC4: regression_gate prefix detection AFTER static checks
    # If it's a regression gate, apply special rules
    if _is_regression_gate_command(command, cwd=cwd):
        if exit_code == 0:
            return "expected_pass", "regression_gate", "go", None, "regression_gate"
        else:
            if _is_package_manager_no_tty_prompt(command, stdout, stderr):
                applied_runner_delta = runner_env_delta or {}
                if applied_runner_delta == {"CI": "true"}:
                    fix_hint = (
                        "CI=true was already injected by the runner. This is still a pnpm/node_modules/tooling "
                        "state blocker; do not rewrite the Issue body."
                    )
                else:
                    fix_hint = (
                        "This is a package manager / no-TTY runner environment blocker, not an Issue body defect. "
                        "Do not weaken ACs or rewrite the pnpm gate to runtime_only. "
                        "Inspect pnpm/node_modules/tooling state; if runner_env_delta is missing, "
                        "retry the same canonical pnpm gate with runner-side CI=true."
                    )
                return (
                    "blocked",
                    "package_manager_no_tty_prompt",
                    "blocked",
                    fix_hint,
                    "regression_gate",
                )
            # B5: Check pytest exit codes 4/5 BEFORE regression_gate failure classification
            if _is_pytest_invocation(command):
                combined_lower = f"{stdout}\n{stderr}".lower()

                # pytest exit 4 + file not found → expected_baseline_fail (env/path missing)
                if exit_code == 4 and re.search(r"error:\s+file or directory not found:", combined_lower):
                    return "expected_fail", "expected_baseline_fail", "go", None, "baseline_fail_expected"

                # Issue #1347: pytest exit 4 where the referenced FILE exists
                # (this branch is only reached when _is_regression_gate_command()
                # already confirmed the file portion exists) but the node-id
                # (test function) does not, e.g.:
                #   ERROR: not found: <path>/existing_file.py::test_new_case
                #   (no match in any of [<Module existing_file.py>])
                # This is a non-canonical VC shape (Issue #1285 / PR #1305:
                # existing-file missing node-id is forbidden; the canonical
                # form is a missing-file node-id). It must NOT be classified
                # as expected_baseline_fail/go, and must be distinguishable
                # from the generic `unknown` / `human_judgment` fallback so
                # diagnostics can identify this specific shape.
                if exit_code == 4 and _is_existing_file_missing_node_id_error(stdout, stderr, cwd=cwd):
                    return (
                        "blocked",
                        "existing_file_missing_node_id_noncanonical",
                        "blocked",
                        "This pytest VC references a NEW test function on an EXISTING "
                        "file (<file>::<new_test_name>), which is a non-canonical VC "
                        "shape (Issue #1285 / PR #1305). Rewrite the VC to reference a "
                        "not-yet-created test file (missing_new_test_file.py::test_name) "
                        "instead of adding a new test function to an existing file.",
                        "baseline_fail_expected",
                    )

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

    # exit_code = 0 で回帰ゲート以外
    # Issue #1488: current-head evidence-mode では、静的 policy を通過して
    # 実行された非 regression-gate VC（rg / test -f / test -s 等）の exit 0 は
    # certified current PASS（expected_pass / go）として扱う。baseline
    # evidence-mode の意味論（unexpected_pass / blocked）は変更しない（AC1）。
    #
    # PR #1497 review (Blocker 1 / Blocker 2): a bare `exit_code == 0` is NOT
    # sufficient evidence of "this AC was resolved by the reviewed commit".
    # `certify_current_pass_command()` additionally requires: (a) the command
    # is on a closed allowlist (`test -f|-d|-s <PATH>` / `rg PATTERN
    # <PATH...>` only -- grep/egrep/fgrep are excluded until a path-operand
    # parser exists for them), (b) every path operand is a strictly
    # normalized repo-relative path contained within Allowed Paths (rejecting
    # absolute paths / `..` / repo-external state such as /tmp sentinels),
    # (c) every path operand is verified to exist as a regular file at
    # certification time, and (d) stderr is empty (rejecting `rg -q`/`grep
    # -q` "quiet partial success" where one operand matched while another
    # produced a missing-path/permission error that would otherwise be
    # silently absorbed by exit_code == 0 alone).
    if exit_code == 0:
        if evidence_mode == "current-head" and static_policy_passed:
            certified, _reason = certify_current_pass_command(
                command, exit_code, stdout, stderr, cwd, allowed_paths
            )
            if certified:
                return (
                    "expected_pass",
                    "expected_pass_resolved_on_current_head",
                    "go",
                    None,
                    "baseline_fail_expected",
                )
        return "unexpected_pass", "unexpected_pass", "blocked", "Command unexpectedly passed", "baseline_fail_expected"

    # shlex.split failed
    if "shlex.split failed" in stderr:
        return (
            "blocked",
            "compound_command_disallowed",
            "blocked",
            "Command syntax is not supported",
            "baseline_fail_expected"
        )

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
        return (
            "blocked",
            "file_not_found_unrunnable",
            "blocked",
            "Script or file being executed does not exist",
            "baseline_fail_expected"
        )

    # AC1/AC2/AC3/AC4/AC5/AC9/AC10 (Issue #1328): rg exit_code==2 against a
    # missing file/dir that is fully contained within Allowed Paths, with
    # stderr strictly indicating a "missing path" error (not invalid regex /
    # unsupported option / config error / permission denied), is a
    # deterministic expected_fail. This is a narrow special case: any
    # condition below not satisfied falls through to the existing branches
    # (usually the terminal Unknown fallback -> human_judgment).
    # OWNER Blocker 1: additionally require stdout to be empty (no matches
    # were found anywhere) AND require the stderr-reported missing path(s) to
    # exactly match the full set of argv path operands. Without this, a
    # multi-path invocation where one path matched (non-empty stdout) and a
    # DIFFERENT path was missing would be misclassified as a deterministic
    # baseline fail, even though the VC actually found a match.
    if (
        exit_code == 2
        and static_policy_passed
        and allowed_paths
        and stdout.strip() == ""
    ):
        try:
            _rg_argv_for_missing_path = shlex.split(command)
        except ValueError:
            _rg_argv_for_missing_path = []
        if _rg_argv_for_missing_path and Path(_rg_argv_for_missing_path[0]).name == "rg":
            _rg_path_operands = _rg_extract_path_operands(_rg_argv_for_missing_path)
            if (
                _rg_path_operands_all_within_allowed_paths(_rg_path_operands, allowed_paths)
                and _is_rg_missing_path_error(stderr)
            ):
                _rg_missing_from_stderr = _rg_stderr_extract_missing_paths(stderr)
                _norm_operands = {
                    n for n in (
                        _normalize_repo_relative_path_strict(p) for p in _rg_path_operands
                    ) if n is not None
                }
                _norm_missing = {
                    n for n in (
                        _normalize_repo_relative_path_strict(p) for p in _rg_missing_from_stderr
                    ) if n is not None
                }
                # OWNER Blocker 1: the missing-path set reported by stderr must be
                # non-empty and must equal (not merely overlap with) the full set
                # of path operands -- either a single missing path, or every
                # operand accounted for as missing. This rejects "one operand
                # matched, a different operand was missing" false-green cases.
                if _norm_missing and _norm_operands == _norm_missing:
                    return (
                        "expected_fail",
                        "new_file_missing_expected",
                        "go",
                        None,
                        "baseline_fail_expected",
                    )

    # env_missing_dep: command not found (127), permission denied (126), ModuleNotFoundError, etc.
    if exit_code in (126, 127):
        return (
            "blocked",
            "env_missing_dep",
            "blocked",
            "Command not found or permission denied",
            "baseline_fail_expected"
        )

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

    # test -f / test -d / test -s with non-existent or zero-size file
    # Use shlex.split for argv parse instead of substring matching (AC5)
    try:
        _test_argv = shlex.split(command)
    except ValueError:
        _test_argv = []
    # Handle test -s exit 2 (malformed) regardless of argument count (AC7)
    # Must come before the len==3 guard so `test -s` (no operand, len=2) is also caught.
    if (
        len(_test_argv) >= 2
        and _test_argv[0] == "test"
        and _test_argv[1] == "-s"
        and exit_code == 2
    ):
        return (
            "blocked",
            "unknown",
            "blocked",
            "test -s returned exit 2 (malformed invocation)",
            "baseline_fail_expected"
        )
    # Exact 3-argument predicate: test <flag> <path> — no extra operands allowed (AC5)
    if len(_test_argv) == 3 and _test_argv[0] == "test":
        _flag = _test_argv[1]
        if _flag in ("-f", "-d") and exit_code == 1:
            return "expected_fail", "file_not_found_expected", "go", None, "baseline_fail_expected"
        if _flag == "-s" and exit_code == 1:
            # exit 1: file missing or zero-size — both are expected baseline fail (AC1, AC2)
            return (
                "expected_fail",
                "file_not_found_expected",
                "go",
                "test -s false means missing or zero-size file",
                "baseline_fail_expected",
            )

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
            return (
                "human_judgment",
                "unknown",
                "human_judgment",
                "grep error classification uncertain",
                "baseline_fail_expected"
            )
        return "expected_fail", "expected_baseline_fail", "go", None, "baseline_fail_expected"

    # Unknown: cannot classify
    return (
        "human_judgment",
        "unknown",
        "human_judgment",
        "Unable to automatically classify exit code",
        "baseline_fail_expected"
    )


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
        "rg_option_mismatch",            # AC1: Issue #648
        "broad_search_path_unbounded",   # AC2: Issue #648
        "new_file_missing_expected",     # AC11: Issue #1328
        "expected_pass_resolved_on_current_head",  # PR #1497 review Major 2: Issue #1488
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
    VALID_CLASSIFICATIONS = {
        "expected_fail",
        "expected_pass",
        "unexpected_pass",
        "blocked",
        "human_judgment",
        "skipped"
    }
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
            failures.append(
                f"{prefix}: invalid classification"
                f" '{item['classification']}' (valid: {VALID_CLASSIFICATIONS})"
            )
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

        # PR #1497 review Major 2: expected_pass_resolved_on_current_head
        # consistency (classification / decision / confidence must agree;
        # an "unknown category" fallback to confidence: low would otherwise
        # silently contradict a certified current PASS).
        if item.get("category") == "expected_pass_resolved_on_current_head":
            if item.get("classification") != "expected_pass":
                failures.append(
                    f"{prefix}: expected_pass_resolved_on_current_head requires classification expected_pass"
                )
            if item.get("decision") != "go":
                failures.append(
                    f"{prefix}: expected_pass_resolved_on_current_head requires decision go"
                )
            if "confidence" in item and item["confidence"] != "high":
                failures.append(
                    f"{prefix}: expected_pass_resolved_on_current_head requires confidence high"
                )

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
                "runner_env_delta": r.get("runner_env_delta", {}),
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


def _build_result_item(
    *,
    ac_label,
    line_no,
    command,
    exit_code,
    classification,
    category,
    decision,
    scope_class,
    stdout,
    stderr,
    duration_ms,
    fix_hint,
    runner_env_delta,
    preflight_scope,
    baseline_expect,
    vc_role,
    annotation_line_no,
    annotation_raw,
    verification_owner,
    deferred_reason,
    runtime_verification_required,
    strict_enabled,
    max_head_lines,
    execution_key_hash: Optional[str] = None,
    is_dedup_replay: bool = False,
    source_command_hash: Optional[str] = None,
    source_execution_key_hash: Optional[str] = None,
    source_result_index: Optional[int] = None,
    source_ac: Optional[str] = None,
    source_line: Optional[int] = None,
    certified_target_paths: Optional[List[str]] = None,
    cwd: Optional[str] = None,
) -> Dict[str, Any]:
    """Build one VC result entry.

    Issue #1338: extracted from the main per-command loop so the immediate
    (single-execution) code path and the deferred dedup/bounded-parallel
    execution phase share byte-identical result-shaping logic. New in #1338:
    `execution_key_hash` (dedup key, distinct from the pre-existing
    `command_hash`) and, for dedup-replayed entries, a `dedup` object carrying
    the source AC's command_hash/execution_key_hash. classification/decision
    are always computed by the caller from THIS entry's own
    command/annotations -- never copied from another AC's result.
    """
    stdout_head, stdout_truncated, stdout_orig_count = truncate_output(stdout, max_head_lines)
    stderr_head, stderr_truncated, stderr_orig_count = truncate_output(stderr, max_head_lines)

    runner_value = (
        "skipped" if preflight_scope is not None
        else ("dedup_replay" if is_dedup_replay else "exec")
    )

    result_item = {
        "ac": ac_label or "AC_UNKNOWN",
        "line": line_no,
        "raw_command": command,
        "command_hash": f"sha256:{compute_command_hash(command)}",
        "execution_key_hash": (
            f"sha256:{execution_key_hash}" if execution_key_hash is not None else None
        ),
        "runner": runner_value,
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
        "runner_env_delta": runner_env_delta,
        "duration_ms": duration_ms,
        "fix_hint": fix_hint,
        "certified_target_paths": certified_target_paths,
        "annotations": {
            "baseline_expect": (
                # Expose raw invalid value (without __invalid__: prefix) in annotations
                baseline_expect[len("__invalid__:"):] if (
                    preflight_scope is None
                    and baseline_expect is not None
                    and baseline_expect.startswith("__invalid__:")
                ) else (baseline_expect if preflight_scope is None else None)
            ),
            "vc_role": vc_role if preflight_scope is None else None,
            "missing_baseline_expect": (
                preflight_scope is None
                and baseline_expect is None
                and classification == "unexpected_pass"
            ),
        },
        "annotation_source": {
            "line": annotation_line_no,
            "raw": annotation_raw,
        },
        "strict": {
            "enabled": strict_enabled,
            "violation": category in [
                "inline_baseline_expect_invalid_placement",
                "missing_baseline_expect_for_new_allowed_path"
            ],
            "body_author_fixable": category in [
                "inline_baseline_expect_invalid_placement",
                "missing_baseline_expect_for_new_allowed_path"
            ],
            "needs_fix": category in [
                "inline_baseline_expect_invalid_placement",
                "missing_baseline_expect_for_new_allowed_path"
            ],
            "structured_feedback": {
                "category": category,
                "body_author_fixable": True,
                "category_wide_remediation": True,
            } if category in [(
                "inline_baseline_expect_invalid_placement"
            ), "missing_baseline_expect_for_new_allowed_path"] else None,
        } if strict_enabled or category in [(
            "inline_baseline_expect_invalid_placement"
        ), "missing_baseline_expect_for_new_allowed_path"] else None,
        # strict + repair coordination for inline_baseline_expect and missing_baseline_expect
        "repair": {
            "repairable": category in [
                "inline_baseline_expect_invalid_placement",
                "missing_baseline_expect_for_new_allowed_path"
            ],
            "kind": (
                (
                    "move_inline_baseline_expect_to_preceding_line"
                ) if category == "inline_baseline_expect_invalid_placement"
                else "insert_baseline_expect_fail" if category == "missing_baseline_expect_for_new_allowed_path"
                else None
            ),
            "line_start": line_no,
            "line_end": line_no,
            "reason": (
                (
                    "baseline-expect annotation must be in contiguous preceding comment block"
                ) if category == "inline_baseline_expect_invalid_placement"
                else (
                    "new Allowed Path file requires baseline-expect: fail annotation"
                ) if category == "missing_baseline_expect_for_new_allowed_path"
                else None
            ),
        } if category in [(
            "inline_baseline_expect_invalid_placement"
        ), "missing_baseline_expect_for_new_allowed_path"] else None,
    }
    # "strict" + "repair" payload for inline_baseline_expect and missing_baseline_expect errors
    # AC5: Add routing metadata for skipped results
    if verification_owner:
        result_item["verification_owner"] = verification_owner
        result_item["deferred_reason"] = deferred_reason
        result_item["runtime_verification_required"] = runtime_verification_required

    try:
        _result_argv = shlex.split(command)
    except ValueError:
        _result_argv = []
    pnpm_gate_evidence = _pnpm_gate_evidence_for_command(command, cwd or ".")
    if pnpm_gate_evidence is not None:
        result_item["pnpm_gate_evidence_required"] = True
        result_item["pnpm_gate_evidence"] = pnpm_gate_evidence
    elif pnpm_gate_registry.gate_for_request(_result_argv) is not None:
        # Consumers must fail closed if this producer-declared requirement is
        # present but its companion evidence is missing or malformed.
        result_item["pnpm_gate_evidence_required"] = True

    if is_dedup_replay:
        # AC2/AC3: dedup replay carries provenance of the source execution,
        # distinct from this AC's own command_hash/classification/decision.
        # PR #1508 review (P2-2): source_result_index/source_ac/source_line
        # let a consumer uniquely locate the exact source AC in `results`
        # (command_hash/execution_key_hash alone cannot disambiguate when
        # multiple ACs would legitimately share the same hash).
        result_item["dedup"] = {
            "source_command_hash": source_command_hash,
            "source_execution_key_hash": (
                f"sha256:{source_execution_key_hash}"
                if source_execution_key_hash is not None else None
            ),
            "source_result_index": source_result_index,
            "source_ac": source_ac,
            "source_line": source_line,
        }

    return result_item


def bounded_worker_count(value: str) -> int:
    """argparse type for --max-workers (Issue #1338, PR #1508 review P2-1):
    only accepts integers in the closed range [1, 8]. Values <= 0, non-
    integer strings, and values > 8 are rejected at argument-parsing time
    (fail-closed bound on VC-preflight concurrency width)."""
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        raise argparse.ArgumentTypeError(f"--max-workers must be an integer, got {value!r}")
    if parsed < 1 or parsed > 8:
        raise argparse.ArgumentTypeError(
            f"--max-workers must be between 1 and 8 (inclusive), got {parsed}"
        )
    return parsed


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Baseline VC Preflight: extract and classify VCs from Issue body"
    )
    parser.add_argument("--issue", type=int, help="GitHub Issue number")
    parser.add_argument("--repo", default="squne121/loop-protocol", help="GitHub repo (owner/name)")
    parser.add_argument("--body-file", help="Path to Issue body file (for testing)")
    parser.add_argument("--cwd", default=None, help="Working directory for command execution")
    parser.add_argument(
        "--evidence-mode",
        choices=["baseline", "current-head"],
        default="baseline",
        help="Evidence contract: backward-compatible baseline or producer-certified current-head",
    )
    parser.add_argument(
        "--reviewed-head-sha",
        default=None,
        help="Immutable commit selected by the caller; required in current-head mode",
    )
    parser.add_argument(
        "--timeout-seconds",
        type=int,
        default=DEFAULT_TIMEOUT_SECONDS,
        help="Timeout per command",
    )
    parser.add_argument(
        "--max-workers",
        type=bounded_worker_count,
        default=1,
        help=(
            "Issue #1338: bounded parallel execution width (1-8 inclusive) for safe "
            "read-only VC predicates (rg with a validated bounded path operand, exact "
            "test -f|-d|-s PATH). grep/egrep/fgrep are intentionally excluded (PR #1508 "
            "review P0-2). Default 1 preserves fully serial execution and output "
            "identical to pre-#1338 behavior."
        ),
    )
    parser.add_argument("--max-head-lines", type=int, default=20, help="Max lines for stdout/stderr")
    # B8: contract-review-fragment format support
    parser.add_argument(
        "--format",
        choices=["json", "contract-review-fragment"],
        default="json",
        help="Output format (json or contract-review-fragment YAML)",
    )
    parser.add_argument(
        "--strict",
        action="store_true",
        default=False,
        help="Enable strict mode for annotation enforcement (detect missing annotations as needs_fix)"
    )
    parser.add_argument(
        "--static-only",
        action="store_true",
        default=False,
        help=(
            "#993: Static-only mode. Parse VC section for non-canonical inputs "
            "(unlabeled fence, colon AC marker, non-$ command) and return "
            "blocked results without executing any commands."
        ),
    )

    args = parser.parse_args()
    cwd_supplied = args.cwd is not None
    args.cwd = args.cwd or "."
    current_evidence: Optional[Dict[str, Any]] = None

    if args.evidence_mode == "current-head":
        invalid_reason = None
        if not cwd_supplied:
            invalid_reason = "current-head requires --cwd"
        elif not args.reviewed_head_sha or isinstance(args.reviewed_head_sha, bool):
            invalid_reason = "current-head requires --reviewed-head-sha"
        elif args.format != "json":
            invalid_reason = "current-head requires --format json"
        elif args.static_only:
            invalid_reason = "current-head cannot be combined with --static-only"
        if invalid_reason:
            current_evidence = {
                "reviewed_head_sha": args.reviewed_head_sha,
                "fallback_detected": False,
                "human_review_required": False,
                "stop_condition_triggered": True,
                "errors": [invalid_reason],
            }
            emit_json(
                current_head_blocked_result(args, current_evidence, invalid_reason),
                args.evidence_mode,
                current_evidence,
            )
            return 1
        current_evidence = collect_current_head_evidence(args.cwd, args.reviewed_head_sha)
        if not current_evidence["certified"]:
            emit_json(
                current_head_blocked_result(args, current_evidence, "current-head certification failed"),
                args.evidence_mode,
                current_evidence,
            )
            return 1

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
        emit_json(result, args.evidence_mode, current_evidence)
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
        emit_json(result, args.evidence_mode, current_evidence)
        # C2: exit code 2 for extraction errors
        return 2

    # #993: --static-only mode: parse for non-canonical VC inputs without executing
    if getattr(args, "static_only", False):
        static_parse = parse_verification_commands_section(vc_section)
        static_results = []
        for se in static_parse.static_errors:
            static_results.append({
                "ac": None,
                "command": se.raw_line,
                "classification": "blocked",
                "category": se.kind,
                "decision": "blocked",
                "exit_code": None,
                "errors": [
                    {
                        "kind": se.kind,
                        "rule": se.rule_id or f"VC_STATIC_{se.kind.upper()}",
                        "message": se.fix_hint,
                        "minimal_context": se.raw_line,
                        "fix_hint": se.fix_hint,
                    }
                ],
            })
        static_status = "blocked" if static_parse.static_errors else "ok"
        result = {
            "schema": "baseline_vc_preflight/v1",
            "issue": args.issue or 0,
            "repo": args.repo,
            "generated_at": datetime.now(timezone.utc).isoformat().replace('+00:00', 'Z'),
            "source": {
                "kind": source_kind,
                "body_sha256": f"sha256:{compute_source_hash(body)}",
            },
            "mode": "static_only",
            "status": static_status,
            "summary": {
                "expected_fail": 0,
                "unexpected_pass": 0,
                "blocked": len(static_results),
                "human_judgment": 0,
                "extraction_errors": 0,
            },
            "results": static_results,
            "errors": [],
        }
        emit_json(result, args.evidence_mode, current_evidence)
        return 0 if static_status == "ok" else 1

    # AC2: parse Allowed Paths from Issue body for containment-based broad path detection
    allowed_paths_from_body = extract_allowed_paths(body)

    # B4: bash ブロックからコマンドを抽出 (```bash のみ canonical format)
    # Note: the normal execution path uses the legacy extract_fenced_bash_blocks() /
    # parse_commands_from_block() parsers, NOT the unified parse_verification_commands_section().
    # Only --static-only mode (above) uses the shared parser from vc_contract_syntax.py (#993).
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
        emit_json(result, args.evidence_mode, current_evidence)
        # C2: exit code 2 for extraction errors
        return 2

    # 各コマンドを実行して分類
    # Issue #1338: results is pre-sized so a contiguous parallel batch (below)
    # can write finalized entries back at their original Issue-body command
    # position, regardless of execution completion order (AC7).
    results: List[Optional[Dict[str, Any]]] = [None] * len(commands)
    summary = {
        "expected_fail": 0,
        "unexpected_pass": 0,
        "blocked": 0,
        "human_judgment": 0,
        "expected_pass": 0,
        "skipped": 0,
        "extraction_errors": 0,
    }

    # ---------------------------------------------------------------------
    # Issue #1338 (PR #1508 review P0-1/P0-2/P1-1 fixes)
    #
    # State-epoch-aware execution cache + contiguous pure-observation
    # batching, executed INLINE (in Issue-body order) rather than deferred
    # to a single end-of-loop phase. This fixes two review findings:
    #
    #   P0-1: execution_key_hash previously covered only argv/cwd/env/
    #   timeout, so two textually-identical pure observations that bracket
    #   a stateful command (e.g. `test -d X` .. `python3 -m py_compile Y` ..
    #   `test -d X`) would incorrectly dedup-replay the second against the
    #   first's stale pre-mutation snapshot. `_state_epoch` now advances
    #   every time a non-pure ("barrier") command executes, and is folded
    #   into compute_execution_key_hash(), so the two `test -d X` calls above
    #   get DIFFERENT keys and are never conflated.
    #
    #   P1-1: the previous design deferred ALL "safe to run" commands
    #   (including pnpm/pytest/git) to a single batch phase after the whole
    #   Issue body had been parsed, which could reorder their actual
    #   subprocess launches relative to commands executed immediately in
    #   this same loop (e.g. github_metadata_assert, a live `gh api` call).
    #   Executing inline, right here, preserves the exact Issue-body
    #   execution order for every side-effecting/observing command.
    #
    # Only a CONTIGUOUS run of pure-observation commands (exact
    # `test -f|-d|-s PATH`, or a fully validated `rg` -- see
    # _is_parallel_eligible_command) is ever batched for concurrent
    # execution (only when --max-workers > 1); a barrier command always
    # flushes that batch first, so parallel batching never reorders a
    # barrier relative to its neighbors.
    # ---------------------------------------------------------------------
    _state_epoch = [0]  # boxed int so nested closures can mutate it
    _exec_cache: Dict[str, Dict[str, Any]] = {}
    _cache_meta: Dict[str, Dict[str, Any]] = {}
    _command_hash_by_key: Dict[str, str] = {}
    _parallel_batch: List[Dict[str, Any]] = []
    # Keys that have already been consumed by a FINALIZED (result-emitting)
    # job. Distinct from `_exec_cache` (which may already hold an entry for
    # a key purely because THIS SAME job's own subprocess just populated it
    # -- e.g. inside a freshly-launched parallel batch); only a key that was
    # already in `_dedup_seen_keys` at finalize-time is a true dedup replay.
    _dedup_seen_keys: set = set()

    def _run_and_cache_pure_job(job: Dict[str, Any]) -> None:
        _key = job["_key"]
        _exit_code, _stdout, _stderr, _duration_ms, _runner_env_delta = run_command(
            job["command"], args.timeout_seconds, args.cwd
        )
        # P0-2: fail-closed quiet-success -- a pure observation VC that exits
        # 0 but leaves non-empty stderr is not a trustworthy silent pass.
        if _exit_code == 0 and _stderr.strip():
            _exit_code = 1
        _exec_cache[_key] = {
            "exit_code": _exit_code,
            "stdout": _stdout,
            "stderr": _stderr,
            "duration_ms": _duration_ms,
            "runner_env_delta": _runner_env_delta,
        }
        _cache_meta[_key] = {"idx": job["idx"], "ac": job["ac_label"], "line": job["line_no"]}
        _command_hash_by_key[_key] = f"sha256:{compute_command_hash(job['command'])}"

    def _finalize_and_store_job(job: Dict[str, Any]) -> None:
        _key = compute_execution_key_hash(
            job["argv"], args.cwd, job["env_delta"], args.timeout_seconds,
            state_epoch=_state_epoch[0],
        )
        _is_replay = job["is_pure"] and _key in _dedup_seen_keys
        if _is_replay:
            _outcome = _exec_cache[_key]
        elif job["is_pure"]:
            if _key not in _exec_cache:
                _run_and_cache_pure_job({**job, "_key": _key})
            _outcome = _exec_cache[_key]
            _dedup_seen_keys.add(_key)
        else:
            _exit_code, _stdout, _stderr, _duration_ms, _runner_env_delta = run_command(
                job["command"], args.timeout_seconds, args.cwd
            )
            _outcome = {
                "exit_code": _exit_code,
                "stdout": _stdout,
                "stderr": _stderr,
                "duration_ms": _duration_ms,
                "runner_env_delta": _runner_env_delta,
            }
            # P0-1: this is a state barrier -- advance the epoch so later
            # pure observations of identical argv/cwd/env never dedup
            # against a stale pre-barrier snapshot.
            _state_epoch[0] += 1

        _classification, _category, _decision, _fix_hint, _scope_class = classify_result(
            _outcome["exit_code"],
            _outcome["stdout"],
            _outcome["stderr"],
            job["command"],
            cwd=args.cwd,
            runner_env_delta=_outcome["runner_env_delta"],
            allowed_paths=allowed_paths_from_body,
            static_policy_passed=True,
            evidence_mode=args.evidence_mode,
        )

        _job_baseline_expect = job["baseline_expect"]
        if _job_baseline_expect == "pass":
            if _outcome["exit_code"] == 0 and _classification in ("unexpected_pass", "expected_pass"):
                _classification = "expected_pass"
                _category = "baseline_expect_pass"
                _decision = "go"
                _scope_class = "regression_gate"
                _fix_hint = None
            elif _outcome["exit_code"] is not None and _outcome["exit_code"] != 0:
                if _category == "package_manager_no_tty_prompt":
                    pass
                else:
                    _classification = "human_judgment"
                    _category = "baseline_regression_failed"
                    _decision = "human_judgment"
                    _fix_hint = (
                        "VC annotated baseline-expect: pass but exited non-0; "
                        "the command that was passing at baseline is now failing. "
                        "Investigate regression or update annotation."
                    )
        elif _job_baseline_expect == "fail":
            pass
        else:
            if _classification == "unexpected_pass" and _decision == "blocked":
                _existing_hint = _fix_hint or ""
                _missing_hint = (
                    " [missing_annotation] Consider adding "
                    "# baseline-expect: pass on the preceding line "
                    "if this VC is expected to pass at baseline "
                    "(e.g., for a promotion/refactor Issue)."
                )
                _fix_hint = _existing_hint + _missing_hint

        _source_meta = _cache_meta.get(_key) if _is_replay else None
        _result_item = _build_result_item(
            ac_label=job["ac_label"],
            line_no=job["line_no"],
            command=job["command"],
            exit_code=_outcome["exit_code"],
            classification=_classification,
            category=_category,
            decision=_decision,
            scope_class=_scope_class,
            stdout=_outcome["stdout"],
            stderr=_outcome["stderr"],
            duration_ms=_outcome["duration_ms"],
            fix_hint=_fix_hint,
            runner_env_delta=_outcome["runner_env_delta"],
            preflight_scope=job["preflight_scope"],
            baseline_expect=_job_baseline_expect,
            vc_role=job["vc_role"],
            annotation_line_no=job["annotation_line_no"],
            annotation_raw=job["annotation_raw"],
            verification_owner=None,
            deferred_reason=None,
            runtime_verification_required=None,
            strict_enabled=args.strict,
            max_head_lines=args.max_head_lines,
            execution_key_hash=_key,
            is_dedup_replay=_is_replay,
            source_command_hash=(_command_hash_by_key.get(_key) if _is_replay else None),
            source_execution_key_hash=(_key if _is_replay else None),
            source_result_index=(_source_meta["idx"] if _source_meta else None),
            source_ac=(_source_meta["ac"] if _source_meta else None),
            source_line=(_source_meta["line"] if _source_meta else None),
            certified_target_paths=(
                _extract_certified_target_paths(job["command"])
                if _category == "expected_pass_resolved_on_current_head"
                else None
            ),
            cwd=args.cwd,
        )
        results[job["idx"]] = _result_item
        summary[_result_item["classification"]] += 1

    def _flush_parallel_batch() -> None:
        """Execute (and finalize) every job buffered in `_parallel_batch`,
        preserving the AC5 contract: with --max-workers > 1, distinct
        not-yet-cached execution keys in this contiguous batch are launched
        concurrently through a single ThreadPoolExecutor; every job (cached,
        deduped, or freshly launched) is then finalized in original order."""
        if not _parallel_batch:
            return
        _batch = list(_parallel_batch)
        _parallel_batch.clear()
        if args.max_workers > 1:
            _launch_order: List[str] = []
            _launch_jobs: Dict[str, Dict[str, Any]] = {}
            for _job in _batch:
                _key = compute_execution_key_hash(
                    _job["argv"], args.cwd, _job["env_delta"], args.timeout_seconds,
                    state_epoch=_state_epoch[0],
                )
                if _key in _exec_cache or _key in _launch_jobs:
                    continue
                _job_with_key = dict(_job)
                _job_with_key["_key"] = _key
                _launch_jobs[_key] = _job_with_key
                _launch_order.append(_key)
            if _launch_jobs:
                with ThreadPoolExecutor(max_workers=args.max_workers) as _executor:
                    _future_to_key = {
                        _executor.submit(_run_and_cache_pure_job, _launch_jobs[_key]): _key
                        for _key in _launch_order
                    }
                    for _future in as_completed(_future_to_key):
                        _future.result()
        for _job in _batch:
            _finalize_and_store_job(_job)

    for _cmd_idx, (
        ac_label, command, line_no, preflight_scope, vc_regex_intent,
        baseline_expect, vc_role, annotation_line_no, annotation_raw,
    ) in enumerate(commands):
        runner_env_delta: Dict[str, str] = {}
        # AC5: Handle pr_review_only / runtime_only preflight-scope markers
        # NB2: Invalid marker values (typos) → human_judgment
        if preflight_scope is not None:
            if preflight_scope in VALID_PRE_FLIGHT_SCOPE_VALUES:
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
                fix_hint = (
                    f"Unknown preflight-scope marker value '{preflight_scope}';"
                    " expected pr_review_only or runtime_only"
                )
                scope_class = "baseline_fail_expected"
                verification_owner = None
                deferred_reason = None
                runtime_verification_required = None

        else:
            # AC2/AC12: inline '# baseline-expect:' is an invalid placement — detect
            # BEFORE any execution so the malformed command is never run.
            if has_unquoted_inline_baseline_expect(command):
                exit_code, stdout, stderr, duration_ms = None, "", "", 0
                classification = "blocked"
                category = "inline_baseline_expect_invalid_placement"
                decision = "blocked"
                fix_hint = (
                    "Inline '# baseline-expect:' alters command semantics and is not "
                    "executed; move the annotation to the immediately preceding comment line."
                )
                scope_class = "baseline_fail_expected"
                verification_owner = None
                deferred_reason = None
                runtime_verification_required = None
            # BLOCKER 2 fix: invalid baseline-expect annotation value → human_judgment
            # Sentinel "__invalid__:<raw>" is set by extract_baseline_expect_annotation
            # when the annotation line is present but value is not in VALID_BASELINE_EXPECT_VALUES.
            elif baseline_expect is not None and baseline_expect.startswith("__invalid__:"):
                raw_invalid_value = baseline_expect[len("__invalid__:"):]
                classification = "human_judgment"
                decision = "human_judgment"
                category = "invalid_baseline_expect_annotation"
                exit_code = None
                stdout, stderr = "", ""
                duration_ms = 0
                scope_class = "baseline_fail_expected"
                fix_hint = (
                    f"# baseline-expect: {raw_invalid_value} is not a valid annotation. "
                    "Use pass|fail|deferred."
                )
                verification_owner = None
                deferred_reason = None
                runtime_verification_required = None

            # Issue #889: baseline-expect: deferred → skip (priority after preflight-scope)
            elif baseline_expect == "deferred":
                classification = "skipped"
                decision = "go"
                category = "baseline_expect_deferred"  # MAJOR 3 fix: distinct category
                exit_code = None
                stdout, stderr = "", ""
                duration_ms = 0
                fix_hint = None
                scope_class = "pr_review_only"
                verification_owner = "pr-review-judge"
                deferred_reason = "VC annotated baseline-expect: deferred; verification deferred"
                runtime_verification_required = False

            # Static classification checks BEFORE run_command (CRITICAL)
            # Negated search commands are statically classified as expected_fail/go
            elif _is_negated_search_command(command):
                exit_code, stdout, stderr, duration_ms = None, "", "", 0
                classification, category, decision, fix_hint, scope_class = (
                    "expected_fail",
                    "expected_baseline_fail",
                    "go",
                    None,
                    "baseline_fail_expected",
                )
                verification_owner = None
                deferred_reason = None
                runtime_verification_required = None
            elif (
                args.strict
                and baseline_expect is None
                and classify_static_command(command, Path(args.cwd), allowed_paths=allowed_paths_from_body) is None
                and _candidate_new_allowed_path_target(command, allowed_paths_from_body, args.cwd) is not None
            ):
                # AC4/AC8/AC14: strict mode — VC targets a NEW Allowed Path file that
                # does not exist at baseline and lacks a baseline-expect annotation.
                _missing_target = _candidate_new_allowed_path_target(
                    command, allowed_paths_from_body, args.cwd
                )
                exit_code, stdout, stderr, duration_ms = None, "", "", 0
                classification = "blocked"
                category = "missing_baseline_expect_for_new_allowed_path"
                decision = "blocked"
                fix_hint = (
                    f"VC targets new Allowed Path '{_missing_target}' which does not exist at "
                    "baseline; add '# baseline-expect: fail' on the preceding line."
                )
                scope_class = "baseline_fail_expected"
                verification_owner = None
                deferred_reason = None
                runtime_verification_required = None
            else:
                # AC1-AC3: classify_static_command checks unsafe/unsupported commands
                # BEFORE any execution attempt
                static_result = classify_static_command(
                    command, Path(args.cwd), allowed_paths=allowed_paths_from_body
                )
                # AC3 (Issue #589): If regex_literal_pipe_suspected and literal-pipe-ok
                # annotation is present, skip the blocked classification and proceed to execute.
                if (
                    static_result is not None
                    and static_result[1] == "regex_literal_pipe_suspected"
                    and vc_regex_intent == "literal-pipe-ok"
                ):
                    static_result = None  # annotation exempts from blocked
                if static_result is not None:
                    # no_override_for_blocker: static_blocker takes precedence — baseline-expect does NOT override
                    # static blocks
                    exit_code, stdout, stderr, duration_ms = None, "", "", 0
                    classification, category, decision, fix_hint, scope_class = static_result
                elif _is_github_metadata_assert_command(command):
                    # Issue #942: github_metadata_assert is allowed (static_result is None) but
                    # is NOT a real executable binary. Instead of run_command (which would hit
                    # "No such file or directory"), dispatch the assertion to
                    # _check_github_metadata_assertion and classify by its exit code.
                    # subprocess is invoked there with a fixed read-only argv (gh api --method GET).
                    # PR #1508 review (P1-1): flush any buffered pure-observation
                    # parallel batch first, so this live `gh api` call keeps its
                    # exact Issue-body relative order against the VCs around it.
                    _flush_parallel_batch()
                    try:
                        _assert_argv = shlex.split(command, posix=True)
                    except ValueError:
                        _assert_argv = []
                    # static_result is None implies _is_allowed_github_metadata_assert validated argv,
                    # so positions 1..4 are present and safe to read.
                    _assertion_type = _assert_argv[1]
                    _field = _assert_argv[2]
                    _literal = _assert_argv[3]
                    _endpoint = _assert_argv[4]
                    assert_exit = _check_github_metadata_assertion(
                        _assertion_type, _field, _literal, _endpoint,
                        timeout_seconds=args.timeout_seconds,
                    )
                    # P0-1: an external GitHub API observation is a state
                    # barrier -- advance the epoch so later pure observations
                    # are never dedup-replayed across it.
                    _state_epoch[0] += 1
                    exit_code, stdout, stderr, duration_ms = assert_exit, "", "", 0
                    if assert_exit == 0:
                        # assertion holds (present for contains / absent for not_contains)
                        classification = "expected_pass"
                        category = "github_metadata_assert_pass"
                        decision = "go"
                        scope_class = "regression_gate"
                        fix_hint = None
                    elif assert_exit == 1:
                        # assertion does not hold; same go disposition as other expected_fail VCs
                        classification = "expected_fail"
                        category = "github_metadata_assert_fail"
                        decision = "go"
                        scope_class = "baseline_fail_expected"
                        fix_hint = None
                    else:
                        # exit 2..8: environment error (gh missing / auth / 404 / rate limit /
                        # timeout / invalid JSON / other HTTP). MUST NOT be a false pass (go).
                        classification = "human_judgment"
                        category = "github_metadata_assert_environment_error"
                        decision = "human_judgment"
                        scope_class = "baseline_fail_expected"
                        fix_hint = (
                            "github_metadata_assert hit an environment error "
                            f"(exit {assert_exit}: gh missing / auth / 404 / rate limit / "
                            "timeout / invalid JSON / missing field). This is distinct from assertion "
                            "pass/fail and is not treated as a baseline pass."
                        )
                else:
                    # Issue #1338 (PR #1508 review P0-1/P1-1): execute inline,
                    # in Issue-body order, through the state-epoch-aware
                    # execution cache / contiguous parallel batch defined
                    # above `for _cmd_idx, (...)`. classify_result() and the
                    # baseline-expect re-map are always recomputed from THIS
                    # AC's own command/annotations (never copied from another
                    # AC's classification/decision) inside
                    # _finalize_and_store_job().
                    try:
                        _this_argv = shlex.split(command)
                    except ValueError:
                        _this_argv = []
                    _this_env_delta = _fixed_env_delta_for_argv(_this_argv)
                    _this_is_pure = _is_parallel_eligible_command(
                        command, args.cwd, allowed_paths_from_body
                    )
                    _job = {
                        "idx": _cmd_idx,
                        "ac_label": ac_label,
                        "line_no": line_no,
                        "command": command,
                        "baseline_expect": baseline_expect,
                        "vc_role": vc_role,
                        "annotation_line_no": annotation_line_no,
                        "annotation_raw": annotation_raw,
                        "preflight_scope": preflight_scope,
                        "argv": _this_argv,
                        "env_delta": _this_env_delta,
                        "is_pure": _this_is_pure,
                    }
                    if _this_is_pure and args.max_workers > 1:
                        # AC5/P1-1: only buffer a CONTIGUOUS run of pure
                        # observation VCs; any barrier command flushes this
                        # batch (see the `else` branch just below) before it
                        # executes, so relative order across a barrier is
                        # never disturbed.
                        _parallel_batch.append(_job)
                        verification_owner = None
                        deferred_reason = None
                        runtime_verification_required = None
                        continue
                    _flush_parallel_batch()
                    _finalize_and_store_job(_job)
                    verification_owner = None
                    deferred_reason = None
                    runtime_verification_required = None
                    continue

                verification_owner = None
                deferred_reason = None
                runtime_verification_required = None

        # Issue #1338: result-shaping is shared with the deferred (pending-job)
        # finalization phase below via _build_result_item(), so both the
        # immediate and dedup/parallel-execution code paths produce identical
        # entry shapes.
        result_item = _build_result_item(
            ac_label=ac_label,
            line_no=line_no,
            command=command,
            exit_code=exit_code,
            classification=classification,
            category=category,
            decision=decision,
            scope_class=scope_class,
            stdout=stdout,
            stderr=stderr,
            duration_ms=duration_ms,
            fix_hint=fix_hint,
            runner_env_delta=runner_env_delta,
            preflight_scope=preflight_scope,
            baseline_expect=baseline_expect,
            vc_role=vc_role,
            annotation_line_no=annotation_line_no,
            annotation_raw=annotation_raw,
            verification_owner=verification_owner,
            deferred_reason=deferred_reason,
            runtime_verification_required=runtime_verification_required,
            strict_enabled=args.strict,
            max_head_lines=args.max_head_lines,
            certified_target_paths=(
                _extract_certified_target_paths(command)
                if category == "expected_pass_resolved_on_current_head"
                else None
            ),
            cwd=args.cwd,
        )
        results[_cmd_idx] = result_item
        summary[result_item["classification"]] += 1

    # Issue #1338 (PR #1508 review P1-1): flush any pure-observation batch
    # still buffered after the last Issue-body command (e.g. the body ends
    # with a contiguous run of `rg`/`test` VCs with no trailing barrier).
    _flush_parallel_batch()

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

    if current_evidence is not None:
        current_evidence = finalize_current_head_evidence(args.cwd, current_evidence)
        if not current_evidence["certified"]:
            output["status"] = "blocked"
            output["errors"].extend(current_evidence["errors"])

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
            emit_json(error_result, args.evidence_mode, current_evidence)
            # C2: exit code 2 for missing dependency
            return 2
        fragment = generate_contract_review_fragment(status, results)
        print(yaml.dump(fragment, default_flow_style=False))
    else:
        emit_json(output, args.evidence_mode, current_evidence)

    # C2: Exit code contract
    # status: pass → 0, blocked → 1, human_judgment → 3
    return exit_code_for_status(output["status"])


if __name__ == "__main__":
    sys.exit(main())
