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
- overlay の ``autoMode.allow`` は ``"$defaults"`` と、exact command・fixture
  installer・fixture home を名指しする固定 rule の 2 要素だけを持つ。broad allow は
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

# carrier と併用できない runner flag (closed list)。overlay の派生 variant を使う、
# または第二の ``--settings`` / ``--setting-sources`` を足す flag は合成規則を
# 定義せず fail-closed とする。
INCOMPATIBLE_RUNNER_FLAGS = (
    "expect_skill_command",
    "require_hook_chain_evidence",
    "hermetic_agent_definition",
)

_BLOB_HEX_RE = re.compile(r"^[0-9a-f]{40}$")


@dataclass(frozen=True)
class ApprovalProfile:
    profile_id: str
    repair_command: str
    fixture_relpath: str
    allow_rule: str


_REPAIR_PROXY_ALLOW_RULE = (
    "Running the exact command `bash scripts/claude-gpt/repair_proxy.sh` is allowed in this "
    "verification session. It executes only the repository-tracked local fixture installer "
    "`.claude/skills/impl-review-loop/tests/fixtures/fake_proxy_installer.sh` through a file:// URL "
    "(it is not a download-and-execute of an external URL), it writes only under the fixture "
    "directory that CLAUDE_GPT_HOME points at inside artifacts/runtime-smoke/ of this worktree, "
    "and it uses no network installer."
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
_PROFILE_FLAG_RE = re.compile(r"--approval-profile(?:=|\s+)(\S+)")
_ADAPTER_GPT_RE = re.compile(r"--claude-adapter(?:=|\s+)claude-gpt\b")
_AC_COMMENT_RE = re.compile(r"^#\s*(AC\d+)\b")
_ENV_TOKEN_RE = re.compile(r"^([A-Za-z_][A-Za-z0-9_]*)=(\S*)$")
# 解釈できない shell 構文。runner 行の周辺にあれば fail-closed で non_executable。
_UNPARSEABLE_RE = re.compile(r"(;|&&|\|\||\||`|\$\(|\bbash\s+-c\b|\beval\b|>|<)")
_HOME_PREFIX_RE = re.compile(r"^\$PWD/artifacts/runtime-smoke/([A-Za-z0-9._-]+)$")

ROWS = (
    "row0_unparseable_runner_line",
    "row1_not_applicable",
    "row2_flag_or_signature_without_declaration",
    "row3_declaration_invalid",
    "row4_declared_without_runner_line",
    "row5_flag_line_not_attached_to_ac",
    "row6_flag_with_claude_gpt_adapter",
    "row7_env_prefix_missing_or_mismatch",
    "row7a_unknown_or_undeclared_flag_value",
    "row8_executable",
)


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
    """(宣言行があるか, profile id のリスト。行はあるが grammar 違反なら None)。"""
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
        return True, [item.strip().strip("'\"") for item in inner.split(",")]
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


def _analyze_runner_line(cmd: str) -> dict[str, Any]:
    tokens = cmd.split()
    env_tokens = {}
    idx = 0
    while idx < len(tokens):
        m = _ENV_TOKEN_RE.match(tokens[idx])
        if not m:
            break
        env_tokens[m.group(1)] = m.group(2)
        idx += 1
    rest = " ".join(tokens[idx:])
    # runner の引数部だけを構文検査の対象にする。env prefix 内の $PWD は許可する。
    unparseable = bool(_UNPARSEABLE_RE.search(rest)) or bool(
        re.search(r"\$(?!PWD\b)", rest)
    )
    profiles = _PROFILE_FLAG_RE.findall(rest)
    return {
        "env": env_tokens,
        "profiles": profiles,
        "adapter_gpt": bool(_ADAPTER_GPT_RE.search(rest)),
        "unparseable": unparseable,
    }


def _env_prefix_ok(env: Mapping[str, str], fixture_relpath: str) -> bool:
    home = env.get(HOME_ENV)
    installer = env.get(INSTALLER_ENV)
    if home is None or installer is None:
        return False
    if not _HOME_PREFIX_RE.match(home):
        return False
    if installer != f"file://$PWD/{fixture_relpath}":
        return False
    return not any(var in env for var in OVERRIDE_ENV_VARS)


def check_runtime_vc_approval_contract(issue_body: str) -> dict[str, Any]:
    """Issue 本文の ``approval_required_actions`` 宣言と VC command 行を照合し、
    ``executable`` / ``non_executable`` / ``not_applicable`` を返す (判定表の行 0〜8)。

    行 (7a) は判定表の補完: 宣言に無い、または registry に無い ``--approval-profile`` 値を
    持つ runner 行を ``non_executable`` にする (run 時に argparse が拒否する契約を
    executable と誤判定しないため)。
    """
    if not isinstance(issue_body, str):
        raise TypeError("issue_body must be str")
    rva = _section(issue_body, "## Runtime Verification Applicability")
    has_decl, declared = _parse_declaration(rva)
    all_lines = _runner_lines(issue_body)
    runner_lines = [
        {**item, **_analyze_runner_line(item["command"])}
        for item in all_lines
        if RUNNER_FILENAME in item["command"]
    ]
    signature = any(
        "repair_proxy.sh" in item["command"] or f"{INSTALLER_ENV}=" in item["command"]
        for item in all_lines
    )
    flag_lines = [item for item in runner_lines if item["profiles"]]

    def result(status: str, row: str, reasons: list[str]) -> dict[str, Any]:
        return {"status": status, "row": row, "reason_codes": reasons}

    if any(item["unparseable"] for item in runner_lines):
        return result("non_executable", ROWS[0], ["unparseable_runner_line"])
    if not has_decl and not flag_lines and not signature:
        return result("not_applicable", ROWS[1], [])
    if not has_decl:
        return result("non_executable", ROWS[2], ["flag_or_signature_without_declaration"])
    if (
        declared is None
        or not declared
        or len(set(declared)) != len(declared)
        or any(d not in _APPROVAL_PROFILE_REGISTRY for d in declared)
    ):
        return result("non_executable", ROWS[3], ["declaration_invalid"])
    for profile_id in declared:
        if not any(profile_id in item["profiles"] for item in flag_lines):
            return result("non_executable", ROWS[4], [f"missing_runner_line:{profile_id}"])
    if any(item["ac"] is None for item in flag_lines):
        return result("non_executable", ROWS[5], ["flag_line_not_attached_to_ac"])
    if any(item["adapter_gpt"] for item in flag_lines):
        return result("non_executable", ROWS[6], ["flag_with_claude_gpt_adapter"])
    for item in flag_lines:
        for profile_id in item["profiles"]:
            profile = _APPROVAL_PROFILE_REGISTRY.get(profile_id)
            if profile is not None and not _env_prefix_ok(item["env"], profile.fixture_relpath):
                return result("non_executable", ROWS[7], [f"env_prefix_mismatch:{profile_id}"])
    for item in flag_lines:
        for profile_id in item["profiles"]:
            if profile_id not in _APPROVAL_PROFILE_REGISTRY or profile_id not in declared:
                return result("non_executable", ROWS[8], [f"unknown_or_undeclared_flag_value:{profile_id}"])
    return result("executable", ROWS[9], [])


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
