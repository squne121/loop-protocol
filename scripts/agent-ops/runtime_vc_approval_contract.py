#!/usr/bin/env python3
"""runtime_vc_approval_contract.py -- Issue #2839.

Runtime VC approval carrier (single definition) and its static checker.

Background: 対話 session で operator が承認しても、runner
(``run_worktree_agent_runtime_smoke.py``) が起動する独立した ``claude -p`` には
その承認が届かない (公式仕様: classifier は同一 session の transcript だけを読む)。
この module は、runtime VC が承認を必要とする場合に、その承認 context を
invocation 単位で bounded / auditable に子 session へ materialize するための
closed enum の profile registry と、materialize できない VC 契約を決定論的に
``non_executable`` と検出する checker を提供する。

設計上の不変条件 (Issue #2839 の Design と AC):

- profile は closed enum。overlay の内容は module 定数であり、caller が
  文字列・JSON・path を渡す入口を持たない (generic settings passthrough 不在)。
  ``build_approval_overlay_json()`` が受け取るのは ``profile_id`` と runner の
  固定 base overlay 定数だけである。
- overlay の ``autoMode.allow`` は ``"$defaults"`` と、固定 rule の 2 要素だけを持つ。rule が許可する
  action は exact な 2 つだけである: (1) exact command・fixture installer・fixture home
  (永続的な install 先) を名指しする repair command、(2) repository-tracked の
  ``classify_runtime_migration.py`` の read-only な ``pre-repair-check`` subcommand (flag は 2 つに固定。
  Issue #2810 の operator decision)。broad allow は
  含めず、``soft_deny`` / ``hard_deny`` / ``environment`` の key も overlay に含めない。
  base overlay の ``permissions.deny`` と hooks はそのまま保持する。
- carrier が与える authority は、repository でレビューされた registry 定数と Issue の
  宣言 (``approval_required_actions``) と PR review から成る。live な operator 承認の
  検証は行わない。
- 親 transcript / 親承認が子 ``claude -p`` に継承される前提は置かない。
- 実 classifier が carrier を受理するかは、この module の pytest では検証しない。
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType
from typing import Any, Mapping

RUNNER_FILENAME = "run_worktree_agent_runtime_smoke.py"

REPAIR_COMMAND = "bash scripts/claude-gpt/repair_proxy.sh"
HOME_ENV = "CLAUDE_GPT_HOME"
INSTALLER_ENV = "CLAUDE_GPT_REPAIR_INSTALLER_URL"
OVERRIDE_ENV_VARS = ("CLAUDE_CODE_PROXY_INSTALL_DIR", "CLAUDE_CODE_PROXY_VERSION")
# fixture home が収まるべき worktree 相対の親 directory。
FIXTURE_HOME_PARENT_RELPATH = "artifacts/runtime-smoke"

PROFILE_REPAIR_PROXY_HERMETIC_FIXTURE = "repair_proxy_hermetic_fixture"

# carrier と併用できない runner flag (closed list)。overlay の派生 variant (UserPromptExpansion
# hook 追加) を使う、または第二の ``--settings`` を足す flag は合成規則を定義せず fail-closed
# とする。``--require-hook-chain-evidence`` は PR #2844 の OWNER review で狭く併用可へ訂正した:
# runner が実際に選択した固定 observation overlay (hook-chain 用の PreToolUse / Stop を含む)
# の **同じ object** に carrier の ``autoMode`` を足し、子へ ``--settings`` を 1 個だけ渡す。
INCOMPATIBLE_RUNNER_FLAGS = (
    "expect_skill_command",
    "hermetic_agent_definition",
)

_BLOB_HEX_RE = re.compile(r"^[0-9a-f]{40}$")


@dataclass(frozen=True)
class ApprovalProfile:
    profile_id: str
    repair_command: str
    fixture_relpath: str
    allow_rule: str


# repair 前に worker が必須とする read-only な identity 確認 (Issue #2810)。classifier script の
# repository-tracked path と subcommand、許可する flag は固定定数で、caller が差し替える入口は無い。
PRE_REPAIR_CHECK_SCRIPT_RELPATH = ".claude/skills/impl-review-loop/scripts/classify_runtime_migration.py"
PRE_REPAIR_CHECK_SUBCOMMAND = "pre-repair-check"
PRE_REPAIR_CHECK_COMMAND_PREFIX = (
    f"uv run --locked python3 {PRE_REPAIR_CHECK_SCRIPT_RELPATH} {PRE_REPAIR_CHECK_SUBCOMMAND}"
)
PRE_REPAIR_CHECK_FLAGS = ("--expected-claude-gpt-home", "--pre-repair-evidence-json")

_REPAIR_PROXY_ALLOW_RULE = (
    "Exactly two actions are allowed in this verification session. "
    "Action 1: running the exact command `bash scripts/claude-gpt/repair_proxy.sh`. "
    "It executes only the repository-tracked local fixture installer "
    "`.claude/skills/impl-review-loop/tests/fixtures/fake_proxy_installer.sh` through a file:// URL "
    "(it is not a download-and-execute of an external URL) and it uses no network installer. "
    "The persistent install destination is limited to the fixture directory that CLAUDE_GPT_HOME "
    "points at inside artifacts/runtime-smoke/ of this worktree; while repairing, the script also "
    "creates and removes local temporary files and directories and verifies the result locally. "
    "The real ~/.claude-gpt is never modified. "
    f"Action 2: running the read-only `{PRE_REPAIR_CHECK_SUBCOMMAND}` subcommand of the repository-tracked "
    f"`{PRE_REPAIR_CHECK_SCRIPT_RELPATH}` as "
    f"`{PRE_REPAIR_CHECK_COMMAND_PREFIX}` "
    f"with exactly the two flags `{PRE_REPAIR_CHECK_FLAGS[0]}` (the absolute path of the fixture home "
    "directory that CLAUDE_GPT_HOME points at inside artifacts/runtime-smoke/ of this worktree) and "
    f"`{PRE_REPAIR_CHECK_FLAGS[1]}` (inline JSON holding claude_gpt_home_absolute_path and repo_head). "
    "This action is read-only: it only reads the effective CLAUDE_GPT_HOME and the output of "
    "`git rev-parse HEAD`, creates or changes no files, uses no network, and performs no install. "
    "Nothing else is allowed by this rule: no other subcommand of that script, no other script, "
    "no other uv or python invocation, no other shell command, no shell chaining, "
    "no listing of environment variables, and no additional file or system mutation."
)

# closed enum。profile を増やすには別 Issue で registry と test を更新する。
_APPROVAL_PROFILE_REGISTRY: Mapping[str, ApprovalProfile] = MappingProxyType({
    PROFILE_REPAIR_PROXY_HERMETIC_FIXTURE: ApprovalProfile(
        profile_id=PROFILE_REPAIR_PROXY_HERMETIC_FIXTURE,
        repair_command=REPAIR_COMMAND,
        fixture_relpath=".claude/skills/impl-review-loop/tests/fixtures/fake_proxy_installer.sh",
        allow_rule=_REPAIR_PROXY_ALLOW_RULE,
    ),
})


def approval_profile_ids() -> tuple[str, ...]:
    """closed enum の profile id (ソート済み)。argparse の choices と checker が使う。"""
    return tuple(sorted(_APPROVAL_PROFILE_REGISTRY))


def get_approval_profile(profile_id: str) -> ApprovalProfile:
    if not isinstance(profile_id, str) or profile_id not in _APPROVAL_PROFILE_REGISTRY:
        raise ValueError(f"unknown_approval_profile:{profile_id!r}")
    return _APPROVAL_PROFILE_REGISTRY[profile_id]


# ---------------------------------------------------------------------------
# overlay
# ---------------------------------------------------------------------------


def build_approval_overlay_json(profile_id: str, base_settings_json: str) -> str:
    """runner の固定 base overlay に、profile 固有の固定 ``autoMode.allow`` を足した
    ``--settings`` 用 JSON 文字列を返す。

    受け取る値は ``profile_id`` と runner の固定 base overlay 定数だけで、caller 由来の
    dict / 文字列を overlay に混ぜる引数は存在しない。base overlay が既に ``autoMode`` を
    持つ場合は合成規則を定義せず拒否する。
    """
    profile = get_approval_profile(profile_id)
    if not isinstance(base_settings_json, str):
        raise TypeError("base_settings_json must be str")
    base = json.loads(base_settings_json)
    if not isinstance(base, dict) or "autoMode" in base:
        raise ValueError("base_overlay_not_composable")
    merged = dict(base)
    merged["autoMode"] = {"allow": ["$defaults", profile.allow_rule]}
    return json.dumps(merged)


def overlay_sha256(overlay_json: str) -> str:
    """``--settings`` に渡した JSON 文字列の UTF-8 bytes の sha256 (hex 64 桁)。"""
    return hashlib.sha256(overlay_json.encode("utf-8")).hexdigest()


# ---------------------------------------------------------------------------
# precondition (runner が launch 前に決定論的に検証する)
# ---------------------------------------------------------------------------


def _git(worktree: str, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", "-C", worktree, *args],
        capture_output=True, text=True, timeout=30, check=False,
    )


def _fail(reason_code: str) -> dict[str, Any]:
    return {"ok": False, "reason_code": reason_code}


def verify_approval_carrier_preconditions(
    profile_id: str,
    *,
    worktree: str,
    env: Mapping[str, str],
    claude_adapter: str,
    mode: str,
    incompatible_flags: Mapping[str, bool] | None = None,
) -> dict[str, Any]:
    """carrier 使用の precondition を検証する。不成立なら ``ok: False`` と
    ``reason_code`` を返す (呼び出し側は子 session を起動せず fail-closed で終了する)。

    成立時の戻り値は ``ok: True`` と、audit 用の検証済み要約
    (worktree 相対 path と hash だけ。raw の環境値は含めない) を持つ。
    """
    try:
        profile = get_approval_profile(profile_id)
    except ValueError:
        return _fail("unknown_profile")
    if claude_adapter != "native":
        return _fail("adapter_not_native")
    if mode != "structured":
        return _fail("mode_not_structured")
    for name in INCOMPATIBLE_RUNNER_FLAGS:
        if (incompatible_flags or {}).get(name):
            return _fail(f"incompatible_flag:{name}")

    wt = os.path.realpath(worktree)
    if not os.path.isdir(wt):
        return _fail("worktree_not_directory")

    # --- fixture installer ---
    fixture_path = os.path.join(wt, profile.fixture_relpath)
    if os.path.islink(fixture_path):
        return _fail("fixture_is_symlink")
    if not os.path.isfile(fixture_path):
        return _fail("fixture_missing")
    fixture_real = os.path.realpath(fixture_path)
    if os.path.commonpath([wt, fixture_real]) != wt or fixture_real != fixture_path:
        return _fail("fixture_outside_worktree")
    if _git(wt, "ls-files", "--error-unmatch", "--", profile.fixture_relpath).returncode != 0:
        return _fail("fixture_not_tracked")
    if _git(wt, "diff", "--quiet", "HEAD", "--", profile.fixture_relpath).returncode != 0:
        return _fail("fixture_modified")
    blob = _git(wt, "rev-parse", f"HEAD:{profile.fixture_relpath}")
    blob_hash = blob.stdout.strip()
    if blob.returncode != 0 or not _BLOB_HEX_RE.match(blob_hash):
        return _fail("fixture_blob_unresolved")
    head = _git(wt, "rev-parse", "HEAD")
    repo_head = head.stdout.strip()
    if head.returncode != 0 or not _BLOB_HEX_RE.match(repo_head):
        return _fail("repo_head_unresolved")

    # --- installer URL: file:// + fixture の realpath と完全一致 ---
    if env.get(INSTALLER_ENV) != "file://" + fixture_real:
        return _fail("installer_url_mismatch")

    # --- fixture home ---
    home = env.get(HOME_ENV)
    if not home:
        return _fail("claude_gpt_home_missing")
    if not os.path.isabs(home) or ".." in Path(home).parts:
        return _fail("claude_gpt_home_escape")
    allowed_parent = os.path.join(wt, *FIXTURE_HOME_PARENT_RELPATH.split("/"))
    if os.path.realpath(allowed_parent) != allowed_parent:
        return _fail("claude_gpt_home_parent_symlink")
    home_norm = os.path.normpath(home)
    if os.path.realpath(home_norm) != home_norm:
        return _fail("claude_gpt_home_symlink")
    if home_norm == allowed_parent or not home_norm.startswith(allowed_parent + os.sep):
        return _fail("claude_gpt_home_outside_allowed")

    # --- override 変数 ---
    for var in OVERRIDE_ENV_VARS:
        if var in env:
            return _fail(f"override_env_present:{var}")

    return {
        "ok": True,
        "reason_code": None,
        "profile_id": profile.profile_id,
        "repo_head": repo_head,
        "fixture_relpath": profile.fixture_relpath,
        "fixture_git_blob_hash": blob_hash,
        "fixture_real": fixture_real,
        "claude_gpt_home_relpath": os.path.relpath(home_norm, wt),
    }


def build_approval_child_env(env: Mapping[str, str], verified: Mapping[str, Any]) -> dict[str, str]:
    """carrier 使用時に子へ渡す env を明示的に組み立てる。``env`` の複製に、検証済みの値を
    再設定し、override 変数を除去する。"""
    if not verified.get("ok"):
        raise ValueError("preconditions_not_verified")
    child = {k: v for k, v in env.items() if k not in OVERRIDE_ENV_VARS}
    child[INSTALLER_ENV] = "file://" + str(verified["fixture_real"])
    # verified 時点の home は env にある値と同じ (precondition が検証済み)。
    child[HOME_ENV] = env[HOME_ENV]
    return child


def fixture_content_blob_hash(worktree: str, fixture_relpath: str) -> str | None:
    """working tree 上の fixture の現在の内容 hash (git blob hash)。run 後の再確認用。"""
    path = os.path.join(os.path.realpath(worktree), fixture_relpath)
    if os.path.islink(path) or not os.path.isfile(path):
        return None
    res = _git(os.path.realpath(worktree), "hash-object", "--", fixture_relpath)
    out = res.stdout.strip()
    return out if res.returncode == 0 and _BLOB_HEX_RE.match(out) else None


def build_approval_carrier_evidence(
    verified: Mapping[str, Any], *, overlay_json: str, fixture_hash_after_run: str | None,
) -> dict[str, Any]:
    """evidence に載せる ``approval_carrier`` (worktree 相対 path と hash だけ)。"""
    return {
        "profile_id": verified["profile_id"],
        "repo_head": verified["repo_head"],
        "overlay_sha256": overlay_sha256(overlay_json),
        "fixture_git_blob_hash": verified["fixture_git_blob_hash"],
        "fixture_unchanged": fixture_hash_after_run == verified["fixture_git_blob_hash"],
        "preconditions": {
            "adapter": "native",
            "mode": "structured",
            "fixture_relpath": verified["fixture_relpath"],
            "claude_gpt_home_relpath": verified["claude_gpt_home_relpath"],
            "installer_url_matches_fixture": True,
            "override_env_absent": True,
        },
    }


# ---------------------------------------------------------------------------
# checker (Issue 本文の宣言と VC command 行を静的に照合する)
# ---------------------------------------------------------------------------

_DECLARATION_RE = re.compile(r"^approval_required_actions:\s*(.*?)\s*$")
_FLOW_LIST_RE = re.compile(r"^\[(.*)\]$")
_DECLARED_ID_RE = re.compile(r"""^(["']?)([A-Za-z0-9_.-]+)\1$""")
_AC_COMMENT_RE = re.compile(r"^#\s*(AC\d+)\b")
_ENV_ASSIGN_HEAD_RE = re.compile(r"^([A-Za-z_][A-Za-z0-9_]*)=")
_SHELL_COMMANDS = ("bash", "sh", "zsh", "dash", "ksh")
_SHELL_DASH_C_RE = re.compile(r"^-[A-Za-z]*c$")
# ``$PWD`` が展開された位置を表す placeholder。quote 由来の意味 (bare / 二重引用符は展開、
# 単引用符・backslash escape は literal) を、text から復元できる形で保持する。
_PWD = "\x00"
_HOME_PREFIX_RE = re.compile(r"^\x00/artifacts/runtime-smoke/([A-Za-z0-9._-]+)$")
_PWD_VAR_RE = re.compile(r"\$(?:PWD(?![A-Za-z0-9_])|\{PWD\})")

ROWS = (
    "row0_unparseable_runner_line",
    "row1_not_applicable",
    "row2_flag_or_signature_without_declaration",
    "row3_declaration_invalid",
    "row4_declared_without_runner_line",
    "row4a_signature_runner_line_without_flag",
    "row5_flag_line_not_attached_to_ac",
    "row6_flag_with_claude_gpt_adapter",
    "row6a_flag_with_non_structured_mode",
    "row7_env_prefix_missing_or_mismatch",
    "row7a_unknown_or_undeclared_flag_value",
    "row8_executable",
)
_ROW = {name.split("_", 1)[0]: name for name in ROWS}


def _section(body: str, heading: str) -> list[str]:
    lines = body.splitlines()
    out: list[str] = []
    active = False
    for line in lines:
        if line.startswith("## "):
            active = line.strip() == heading
            continue
        if active:
            out.append(line)
    return out


def _parse_declaration(section_lines: list[str]) -> tuple[bool, list[str] | None]:
    """(宣言行があるか, profile id のリスト。行はあるが grammar 違反なら None)。

    各 item は bare の識別子、または対になった単引用符 / 二重引用符で囲んだ識別子だけを
    受け付ける。それ以外は grammar 違反として ``None`` を返す。
    """
    for line in section_lines:
        m = _DECLARATION_RE.match(line.strip())
        if not m:
            continue
        flow = _FLOW_LIST_RE.match(m.group(1).strip())
        if not flow:
            return True, None
        inner = flow.group(1).strip()
        if not inner:
            return True, []
        ids: list[str] = []
        for item in inner.split(","):
            im = _DECLARED_ID_RE.match(item.strip())
            if not im:
                return True, None
            ids.append(im.group(2))
        return True, ids
    return False, []


def _runner_lines(body: str) -> list[dict[str, Any]]:
    """Verification Commands の fenced block から runner 行を論理行として抽出する。"""
    vc_lines = _section(body, "## Verification Commands")
    logical: list[tuple[str | None, str]] = []
    current_ac: str | None = None
    buf = ""
    in_fence = False
    for raw in vc_lines:
        line = raw.rstrip()
        if line.strip().startswith("```"):
            in_fence = not in_fence
            current_ac = None
            buf = ""
            continue
        if not in_fence:
            continue
        stripped = line.strip()
        if not stripped:
            current_ac = None
            continue
        m = _AC_COMMENT_RE.match(stripped)
        if m and not buf:
            current_ac = m.group(1)
            continue
        if stripped.startswith("#") and not buf:
            continue
        cmd = stripped[2:] if stripped.startswith("$ ") and not buf else stripped
        if cmd.endswith("\\"):
            buf += cmd[:-1].rstrip() + " "
            continue
        logical.append((current_ac, (buf + cmd).strip()))
        buf = ""
    result = []
    for ac, cmd in logical:
        result.append({"ac": ac, "command": cmd})
    return result


def _lex_words(cmd: str) -> tuple[list[dict[str, Any]], list[str]]:
    """サポートする小さな shell grammar だけを対象にした quote-aware な字句解析。

    戻り値は ``(words, unsupported)``。``unsupported`` は解釈できない構文の理由リスト
    (空なら解析成功)。各 word は次を持つ:

    - ``template``: quote を外した値。展開される位置 (bare / 二重引用符内) の ``$PWD`` は
      placeholder ``_PWD`` に置き換え、単引用符内・backslash escape の ``$PWD`` は literal の
      ``$PWD`` のまま残す (``"$PWD/x"`` と ``'$PWD/x'`` を区別するため)。
    - ``bare_head``: word 先頭から最初の quote / escape までの bare な部分 (env assignment の
      ``NAME=`` が quote されていないかの判定に使う)。

    shell 全体の意味解釈は行わない。unquoted の ``;`` ``&`` ``|`` ``<`` ``>`` ``(`` ``)``、
    引用符の外または二重引用符内の command substitution (`` ` `` / ``$(``)、``$PWD`` 以外の
    変数展開は ``unsupported`` として報告する。
    """
    words: list[dict[str, Any]] = []
    unsupported: list[str] = []
    n = len(cmd)
    i = 0
    cur: list[str] | None = None  # 現在の word の template 文字列片
    head: list[str] = []
    head_open = True

    def start() -> None:
        nonlocal cur, head, head_open
        if cur is None:
            cur, head, head_open = [], [], True

    def emit(text: str, *, bare: bool) -> None:
        nonlocal head_open
        start()
        cur.append(text)  # type: ignore[union-attr]
        if head_open and bare:
            head.append(text)
        else:
            head_open = False

    def flush() -> None:
        nonlocal cur
        if cur is not None:
            words.append({"template": "".join(cur), "bare_head": "".join(head)})
            cur = None

    def expansion(at: int) -> tuple[str, int] | None:
        """``at`` は ``$`` の位置。展開を解釈できれば (placeholder, 次の位置)。"""
        m = _PWD_VAR_RE.match(cmd, at)
        if m:
            return _PWD, m.end()
        return None

    while i < n:
        ch = cmd[i]
        if ch in " \t":
            flush()
            i += 1
        elif ch == "'":
            end = cmd.find("'", i + 1)
            if end == -1:
                unsupported.append("unterminated_single_quote")
                i = n
                break
            emit(cmd[i + 1:end], bare=False)  # 空文字列 '' でも word として残る
            i = end + 1
        elif ch == '"':
            start()
            i += 1
            closed = False
            buf: list[str] = []
            while i < n:
                c = cmd[i]
                if c == '"':
                    closed = True
                    i += 1
                    break
                if c == "\\" and i + 1 < n and cmd[i + 1] in '$`"\\':
                    buf.append(cmd[i + 1])
                    i += 2
                elif c == "`":
                    unsupported.append("command_substitution")
                    i += 1
                elif c == "$":
                    if cmd.startswith("$(", i):
                        unsupported.append("command_substitution")
                        i += 2
                        continue
                    exp = expansion(i)
                    if exp is None:
                        unsupported.append("variable_expansion")
                        i += 1
                    else:
                        buf.append(exp[0])
                        i = exp[1]
                else:
                    buf.append(c)
                    i += 1
            if not closed:
                unsupported.append("unterminated_double_quote")
            emit("".join(buf), bare=False)
        elif ch == "\\":
            if i + 1 < n:
                emit(cmd[i + 1], bare=False)
                i += 2
            else:
                emit("\\", bare=False)
                i += 1
        elif ch in ";&|<>()":
            unsupported.append(f"operator:{ch}")
            flush()
            i += 1
        elif ch == "`":
            unsupported.append("command_substitution")
            i += 1
        elif ch == "$":
            if cmd.startswith("$(", i):
                unsupported.append("command_substitution")
                i += 2
                continue
            exp = expansion(i)
            if exp is None:
                unsupported.append("variable_expansion")
                i += 1
            else:
                emit(exp[0], bare=True)
                i = exp[1]
        else:
            emit(ch, bare=True)
            i += 1
    flush()
    return words, unsupported


def _analyze_runner_line(cmd: str) -> dict[str, Any]:
    """1 行の command を quote-aware に解析し、承認の判定に必要な事実だけを返す。"""
    words, unsupported = _lex_words(cmd)
    env: dict[str, str] = {}
    idx = 0
    while idx < len(words):
        m = _ENV_ASSIGN_HEAD_RE.match(words[idx]["bare_head"])
        if not m:
            break
        env[m.group(1)] = words[idx]["template"][m.end():]
        idx += 1
    args = [w["template"] for w in words[idx:]]
    # command 位置の ``eval`` / ``bash -c`` は解釈できない構文として扱う。quote された値の中の
    # 同名の文字列 (例: ``--expect-marker "eval"``) は対象にしない。
    if args:
        command = os.path.basename(args[0])
        if command == "eval":
            unsupported.append("eval")
        elif command in _SHELL_COMMANDS and any(_SHELL_DASH_C_RE.match(a) for a in args[1:]):
            unsupported.append("shell_dash_c")
    runner_idx = next(
        (k for k, a in enumerate(args) if os.path.basename(a) == RUNNER_FILENAME), None
    )
    flag_args = args[runner_idx + 1:] if runner_idx is not None else []
    profiles: list[str] = []
    adapter: str | None = None
    mode: str | None = None
    for k, a in enumerate(flag_args):
        name, eq, inline = a.partition("=")
        if name not in ("--approval-profile", "--claude-adapter", "--mode"):
            continue
        if eq:
            value: str | None = inline
        else:
            value = flag_args[k + 1] if k + 1 < len(flag_args) else None
        if name == "--approval-profile":
            profiles.append(value if value is not None else "")
        elif name == "--claude-adapter":
            adapter = value
        else:
            mode = value
    return {
        "env": env,
        "profiles": profiles,
        "adapter": adapter,
        "mode": mode,
        "adapter_gpt": adapter == "claude-gpt",
        "is_runner": runner_idx is not None,
        "unsupported": unsupported,
        "unparseable": bool(unsupported),
        "signature": INSTALLER_ENV in env,
    }


def _raw_relevant(cmd: str) -> bool:
    """解析できない行の relevance を、raw text の保守的な scan で判定する。"""
    return "--approval-profile" in cmd or f"{INSTALLER_ENV}=" in cmd or "repair_proxy.sh" in cmd


def _env_prefix_ok(env: Mapping[str, str], fixture_relpath: str) -> bool:
    home = env.get(HOME_ENV)
    installer = env.get(INSTALLER_ENV)
    if home is None or installer is None:
        return False
    if not _HOME_PREFIX_RE.match(home):
        return False
    if installer != f"file://{_PWD}/{fixture_relpath}":
        return False
    return not any(var in env for var in OVERRIDE_ENV_VARS)


def check_runtime_vc_approval_contract(issue_body: str) -> dict[str, Any]:
    """Issue 本文の ``approval_required_actions`` 宣言と VC command 行を照合し、
    ``executable`` / ``non_executable`` / ``not_applicable`` を返す (判定表の行 0〜8)。

    carrier の適用は runner invocation (VC の runner 行) ごとに判定する。承認に関係する
    invocation とは、signature (``CLAUDE_GPT_REPAIR_INSTALLER_URL`` の env prefix) を持つ、
    または ``--approval-profile`` を持つものである。関係する invocation にだけ厳格な grammar
    (行 0) と carrier の検査 (行 4a〜7a) を適用し、承認に関係しない runner 行は判定に影響しない。

    行 (4a / 6a / 7a) は判定表の補完: signature を持つが carrier の無い runner 行、carrier が
    structured 以外の mode と併用されている行、宣言に無い、または registry に無い
    ``--approval-profile`` 値を持つ行を ``non_executable`` にする (run 時に拒否される、または
    承認が届かない契約を executable と誤判定しないため)。
    """
    if not isinstance(issue_body, str):
        raise TypeError("issue_body must be str")
    rva = _section(issue_body, "## Runtime Verification Applicability")
    has_decl, declared = _parse_declaration(rva)
    all_lines = _runner_lines(issue_body)
    runner_lines: list[dict[str, Any]] = []
    for item in all_lines:
        if RUNNER_FILENAME not in item["command"]:
            continue
        analysis = _analyze_runner_line(item["command"])
        if analysis["unparseable"]:
            relevant = _raw_relevant(item["command"])
        else:
            if not analysis["is_runner"]:
                continue  # runner を実行しない行 (rg / pytest 等) は対象外
            relevant = bool(analysis["profiles"]) or analysis["signature"]
        runner_lines.append({**item, **analysis, "relevant": relevant})
    relevant_lines = [item for item in runner_lines if item["relevant"]]
    signature = any(
        "repair_proxy.sh" in item["command"] or f"{INSTALLER_ENV}=" in item["command"]
        for item in all_lines
    )
    flag_lines = [item for item in relevant_lines if item["profiles"]]

    def result(status: str, row: str, reasons: list[str]) -> dict[str, Any]:
        return {"status": status, "row": _ROW[row], "reason_codes": reasons}

    if any(item["unparseable"] for item in relevant_lines):
        return result("non_executable", "row0", ["unparseable_runner_line"])
    if not has_decl and not flag_lines and not signature:
        return result("not_applicable", "row1", [])
    if not has_decl:
        return result("non_executable", "row2", ["flag_or_signature_without_declaration"])
    if (
        declared is None
        or not declared
        or len(set(declared)) != len(declared)
        or any(d not in _APPROVAL_PROFILE_REGISTRY for d in declared)
    ):
        return result("non_executable", "row3", ["declaration_invalid"])
    for profile_id in declared:
        if not any(profile_id in item["profiles"] for item in flag_lines):
            return result("non_executable", "row4", [f"missing_runner_line:{profile_id}"])
    if any(not item["profiles"] for item in relevant_lines):
        return result("non_executable", "row4a", ["signature_runner_line_without_flag"])
    if any(item["ac"] is None for item in flag_lines):
        return result("non_executable", "row5", ["flag_line_not_attached_to_ac"])
    if any(item["adapter_gpt"] for item in flag_lines):
        return result("non_executable", "row6", ["flag_with_claude_gpt_adapter"])
    if any(item["mode"] != "structured" for item in flag_lines):
        return result("non_executable", "row6a", ["flag_with_non_structured_mode"])
    for item in flag_lines:
        for profile_id in item["profiles"]:
            profile = _APPROVAL_PROFILE_REGISTRY.get(profile_id)
            if profile is not None and not _env_prefix_ok(item["env"], profile.fixture_relpath):
                return result("non_executable", "row7", [f"env_prefix_mismatch:{profile_id}"])
    for item in flag_lines:
        for profile_id in item["profiles"]:
            if profile_id not in _APPROVAL_PROFILE_REGISTRY or profile_id not in declared:
                return result("non_executable", "row7a", [f"unknown_or_undeclared_flag_value:{profile_id}"])
    return result("executable", "row8", [])


def _main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Check whether an Issue body's approval-requiring runtime VC contract is executable.",
    )
    parser.add_argument("--issue-body-file", required=True)
    args = parser.parse_args(argv)
    try:
        body = Path(args.issue_body_file).read_text(encoding="utf-8")
    except OSError as exc:
        print(json.dumps({"error": "issue_body_unreadable", "detail": str(exc)}), file=sys.stderr)
        return 2
    verdict = check_runtime_vc_approval_contract(body)
    print(json.dumps(verdict, ensure_ascii=False))
    return 1 if verdict["status"] == "non_executable" else 0


if __name__ == "__main__":
    raise SystemExit(_main())
