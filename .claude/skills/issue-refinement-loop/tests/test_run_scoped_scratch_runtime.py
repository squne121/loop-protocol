#!/usr/bin/env python3
"""Issue #2860 AC5: run-scoped scratch の target-focused runtime entrypoint.

直接 CLI で明示的に opt-in する (通常の pytest lane は live 実行しない):

    uv run --locked python3 .claude/skills/issue-refinement-loop/tests/test_run_scoped_scratch_runtime.py \
        --case claude-gpt-auto-scratch

既存の ``scripts/agent-ops/run_worktree_agent_runtime_smoke.py`` を ``--claude-adapter claude-gpt``
(launcher: ``scripts/claude-gpt/launch.sh``) で呼び、実際の Claude-GPT セッションに
``issue-refinement-loop`` SKILL.md の scratch 規約に沿った producer -> consumer を実行させる。

- producer: SKILL.md の手順で invocation-owned workspace を確立し、``draft.md`` を書き、
  実際の ``run_refinement_preflight.py`` (offline fixture 入力、Issue #2860 の live 本文由来) を実行して
  stdout を workspace の ``preflight_stdout.txt`` へ capture する。
- consumer: 同 workspace の ``preflight_stdout.txt`` から ARTIFACT path を読み、canonical
  ``refinement_preflight_result_v1.json`` を読み、``consumer_readback.json`` へ verbatim に書き、
  最終回答として同 JSON を返す。
- runner の ``--output-schema-path`` で canonical ``refinement_preflight_result_v1.schema.json`` に対する
  jsonschema 検証を行い、さらにこの harness が独立に (a) workspace が canonical repo ``tmp/`` 配下に
  実在すること、(b) producer の stdout capture と on-disk artifact と consumer の readback が互いに一致すること、
  (c) child/runtime evidence に attribution できる固定 ``/tmp`` scratch が新規生成されていないこと、
  を実データで照合する。
  provenance のない OS temp 全体の差分は diagnostic として log に残すだけで、verdict には使わない。
- Auto mode: ``scripts/claude-gpt/launch.sh`` が自身で exactly one の ``--permission-mode auto`` を注入し、
  caller 由来の ``--permission-mode`` は拒否される。よって caller は mode を渡さない。PASS 条件は
  runner evidence から **実観測された main-session permission mode == "auto"** に束縛する。観測不能 (None/欠落) は
  stdout ``SKIP:`` + exit 77、auto 以外は FAIL。declaration / launcher source / argv の静的文字列は観測の代替にしない。

契約 (Issue #2860 AC5):
- 実行不能 (CI / linked worktree 外 / 認証・CLI・launcher・ネットワーク不可 / runner SKIP) は
  stdout ``SKIP: <reason>`` + exit 77。SKIP は PASS ではない。
- fallback-only (claude-gpt launcher 経由でない、marker のみで workspace 実体がない等) は FAIL (exit 1)。
- fake marker・人工 SubAgent・fake ``gh`` で PASS を作らない。native 専用の
  ``--expect-skill-command`` / ``--expect-marker-source main`` は使わない。
- AC6 用の SubAgent case は存在しない (``not_applicable`` 宣言済み)。

制約の開示: canonical ``preflight.run`` executor (``skill_runtime_exec.py``) は ``required_cwd:
canonical_main_root`` / ``required_branch: default_branch`` であり linked worktree では
``exact command class rejected`` になる。そのため本 case は production executor を経由せず、
同じ producer (``run_refinement_preflight.py``) の offline fixture 実行を観測する。これは
production gate 経由の観測ではなく、artifact log にその旨を分類として記録する。

この file には pytest で収集される offline unit test (harness の判定ロジックのみ、live 実行なし) も含む。
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pytest

EXIT_OK = 0
EXIT_FAIL = 1
EXIT_SKIP = 77

CASE_NAME = "claude-gpt-auto-scratch"
REQUIRED_MAIN_PERMISSION_MODE = "auto"
ISSUE_NUMBER = 2860
REPO_SLUG = "squne121/loop-protocol"
WORKSPACE_NAME_RE = re.compile(r"^refinement-%d\.[A-Za-z0-9]{6}$" % ISSUE_NUMBER)
ORDERED_MARKERS = (
    "SCRATCH_WORKSPACE_ESTABLISHED",
    "SCRATCH_PRODUCER_DONE",
    "SCRATCH_CONSUMER_DONE",
)
RUNNER_TIMEOUT_SECONDS = 420
OUTER_TIMEOUT_SECONDS = RUNNER_TIMEOUT_SECONDS + 120
LOG_BOUND_CHARS = 3000

_THIS_FILE = Path(__file__).resolve()
REPO_ROOT = _THIS_FILE.parents[4]
RUNNER = REPO_ROOT / "scripts" / "agent-ops" / "run_worktree_agent_runtime_smoke.py"
LAUNCHER = REPO_ROOT / "scripts" / "claude-gpt" / "launch.sh"
SCHEMA_PATH = (
    REPO_ROOT / ".claude" / "skills" / "issue-refinement-loop" / "schemas"
    / "refinement_preflight_result_v1.schema.json"
)
PREFLIGHT_SCRIPT_REL = ".claude/skills/issue-refinement-loop/scripts/run_refinement_preflight.py"
ARTIFACT_REL = f".claude/artifacts/issue-refinement-loop/{ISSUE_NUMBER}/refinement_preflight_result_v1.json"


class Skip(Exception):
    """実行不能。stdout ``SKIP: <reason>`` + exit 77 に写像する。"""

    def __init__(self, reason_class: str, reason: str) -> None:
        super().__init__(reason)
        self.reason_class = reason_class
        self.reason = reason


# ---------------------------------------------------------------------------
# 純粋な判定ロジック (offline unit test 対象)
# ---------------------------------------------------------------------------


def build_prompt(*, fixture_path: str) -> str:
    """live セッションに渡す prompt。

    scratch 手順 (どの順序で何を作るか) は SKILL.md の規約に従わせ、正解 command を羅列しない。
    必要な入力 (producer script と fixture の path・出力 contract) だけを与える。"""
    return f"""\
あなたはこのリポジトリの issue-refinement-loop Skill の scratch 規約に従って作業する。

1. `.claude/skills/issue-refinement-loop/SKILL.md` の Guardrails にある ad hoc scratch の規約を読み、
   Issue 番号 {ISSUE_NUMBER} のための invocation-owned workspace を、その規約どおりの順序で実際に作成する。
   固定の `/tmp/<name>` や cwd 直下の裸のファイル名には書かない。作成直後、assistant の通常 text として
   1 行 `{ORDERED_MARKERS[0]} <作成した具体 path>` を書く（次の tool call より前。最終回答にまとめない）。
2. producer: workspace 内に `draft.md`（任意の短い本文 draft）を書き、次に read-only の offline refinement preflight
   `{PREFLIGHT_SCRIPT_REL}` を `--issue-number {ISSUE_NUMBER} --repo {REPO_SLUG} --fixture {fixture_path}`
   で 1 回実行し、その stdout を workspace 内の `preflight_stdout.txt` に capture する。
   完了直後、assistant の通常 text として
   1 行 `{ORDERED_MARKERS[1]} exit=<preflight の exit code>` を書く（次の tool call より前）。
3. consumer: `preflight_stdout.txt` の `ARTIFACT:` 節にある `refinement_preflight_result_v1` の path を読み、
   その JSON ファイルを読み、内容を変更せず workspace 内の `consumer_readback.json` に書く。
   完了直後、assistant の通常 text として 1 行 `{ORDERED_MARKERS[2]}` を書き、
   その後の最終回答として同じ JSON を ```json フェンス 1 つだけで返す。

具体 path は Bash call をまたいで環境変数に頼らず、各 call で明示する。workspace を削除しない。
GitHub への書き込み、Issue/PR の変更、リポジトリの tracked file の編集はしない。
"""


def build_runner_argv(
    *, python: str, worktree: Path, prompt_file: Path, output_dir: Path, evidence_json: Path
) -> list[str]:
    """既存 runner の argv。native 専用 flag (--expect-skill-command / --expect-marker-source main) は使わない。"""
    argv = [
        python, str(RUNNER),
        "--runtime", "claude", "--mode", "structured",
        "--claude-adapter", "claude-gpt", "--claude-bin", str(LAUNCHER),
        "--worktree", str(worktree),
        "--prompt-file", str(prompt_file),
        "--output-dir", str(output_dir),
        "--evidence-json", str(evidence_json),
        "--timeout-seconds", str(RUNNER_TIMEOUT_SECONDS),
        "--output-schema-path", str(SCHEMA_PATH),
    ]
    for marker in ORDERED_MARKERS:
        argv += ["--expect-ordered-marker", marker]
    return argv


def observed_main_permission_mode(evidence: dict[str, Any] | None) -> str | None:
    """runner evidence から **実観測された** main-session permission mode を返す。観測不能なら ``None``。

    runner が claude の ``system/init`` event の ``permissionMode`` を載せる surface
    (``permission_mode_observed``: top-level、または ``named_subagent_resume`` evidence 内) だけを読む。
    SubagentStop hook payload 由来の ``observed_runtime_fields.permission_mode`` は SubAgent の event の値で
    main session の mode ではないため採用しない。declaration・launcher source・argv は観測ではない。
    複数 surface が食い違う場合は fail-closed で非 auto 扱い (``conflicting:...``) にする。"""
    if not isinstance(evidence, dict):
        return None
    candidates: list[str] = []
    top = evidence.get("permission_mode_observed")
    if isinstance(top, str) and top:
        candidates.append(top)
    resume = evidence.get("named_subagent_resume")
    if isinstance(resume, dict):
        nested = resume.get("permission_mode_observed")
        if isinstance(nested, str) and nested:
            candidates.append(nested)
    if not candidates:
        return None
    unique = sorted(set(candidates))
    return unique[0] if len(unique) == 1 else "conflicting:" + ",".join(unique)


def attribute_fixed_tmp_entries(
    new_entries: list[str], *, attribution_corpus: str
) -> tuple[list[str], list[str]]:
    """OS temp の新規 entry を ``(attributed, unattributed)`` に分ける。

    attributed = child / session / tool / runtime evidence の text (runner evidence・stdout・stderr) に
    その名前が現れる entry のみ。それ以外は provenance が無く、無関係な並行 process の write と区別できないため
    diagnostic に降格する (verdict には使わない)。"""
    attributed: list[str] = []
    unattributed: list[str] = []
    for name in new_entries:
        (attributed if name and name in attribution_corpus else unattributed).append(name)
    return attributed, unattributed


def cleanup_exact_artifact(artifact: Path, *, started_ns: int, parent_preexisting: bool) -> dict[str, Any]:
    """test 自身が生成させた exact artifact だけを unlink する。recursive 削除はしない。

    - exact file が regular file (symlink でない) かつ run 開始後に更新されている場合だけ unlink する。
    - 親 directory は、run 開始前に存在せず (= この run が作成を確立)、かつ cleanup 時点で空の場合だけ ``rmdir`` する。
      sibling artifact が 1 つでもあれば残す (foreign artifact を巻き込まない)。"""
    result: dict[str, Any] = {"artifact_unlinked": False, "parent_removed": False, "parent_kept_reason": None}
    try:
        stat = artifact.lstat()
    except FileNotFoundError:
        stat = None
    if stat is not None and artifact.is_file() and not artifact.is_symlink() and stat.st_mtime_ns >= started_ns:
        try:
            artifact.unlink()
            result["artifact_unlinked"] = True
        except OSError:
            pass
    parent = artifact.parent
    if parent_preexisting:
        result["parent_kept_reason"] = "parent_preexisted"
    elif not parent.is_dir() or parent.is_symlink():
        result["parent_kept_reason"] = "parent_absent"
    else:
        try:
            parent.rmdir()  # 空でなければ OSError: sibling を巻き込まない
            result["parent_removed"] = True
        except OSError:
            result["parent_kept_reason"] = "parent_not_empty"
    return result


def adjudicate(
    *,
    runner_exit: int,
    evidence: dict[str, Any] | None,
    new_workspaces: list[str],
    workspace_files: dict[str, bytes],
    stdout_capture: str | None,
    artifact_json: dict[str, Any] | None,
    schema_errors: list[str],
    new_fixed_tmp_entries: list[str],
    mtimes_ns: dict[str, int] | None = None,
    observed_permission_mode: str | None = None,
) -> tuple[str, str, str]:
    """(verdict, classification, reason)。verdict は PASS / FAIL / SKIP。

    fail-closed: 観測できなかったものを PASS にしない。``new_fixed_tmp_entries`` は provenance のある
    (child/runtime evidence に attribution できた) entry だけを渡す。``observed_permission_mode`` は runner が
    実観測した main-session mode で、``"auto"`` 以外は PASS にならない (None は SKIP、他は FAIL)。"""
    if runner_exit == EXIT_SKIP:
        return "SKIP", "runner_skip", "runner exited 77 (runtime/auth/launcher/causal observation unavailable)"
    if runner_exit != EXIT_OK:
        return "FAIL", "runner_nonzero", f"runner exit={runner_exit}"
    if not isinstance(evidence, dict):
        return "FAIL", "evidence_missing", "runner exited 0 but --evidence-json was absent or unparsable"
    if evidence.get("claude_adapter") != "claude-gpt":
        return "FAIL", "fallback_only", f"adapter was {evidence.get('claude_adapter')!r}, not claude-gpt"
    sidechannel = evidence.get("claude_gpt_proxy_sidechannel")
    receipt = evidence.get("claude_gpt_launcher_receipt")
    proxy_observed = isinstance(sidechannel, dict) and sidechannel.get("proxy_port") is not None
    if not (proxy_observed or isinstance(receipt, dict)):
        return "FAIL", "fallback_only", "no claude-gpt launcher/proxy observation (launcher not actually exercised)"
    if observed_permission_mode is not None and observed_permission_mode != REQUIRED_MAIN_PERMISSION_MODE:
        return (
            "FAIL",
            "permission_mode_not_auto",
            f"observed main-session permission mode {observed_permission_mode!r} is not "
            f"{REQUIRED_MAIN_PERMISSION_MODE!r}; not Auto evidence",
        )
    ordered = evidence.get("ordered_evidence_match")
    if not (isinstance(ordered, dict) and ordered.get("verified") is True):
        return "FAIL", "ordered_markers_unverified", f"ordered markers not verified: {ordered!r}"
    schema_validation = evidence.get("output_contract_schema_validation")
    if not (isinstance(schema_validation, dict) and schema_validation.get("verified") is True):
        return "FAIL", "output_schema_unverified", f"canonical schema validation not verified: {schema_validation!r}"
    # marker / schema だけでは不十分: 実 workspace の実体と producer->consumer を独立に照合する。
    if len(new_workspaces) != 1:
        return (
            "FAIL",
            "marker_only_no_workspace",
            f"expected exactly 1 new owned workspace under tmp/, got {new_workspaces!r}",
        )
    if not WORKSPACE_NAME_RE.match(new_workspaces[0]):
        return "FAIL", "workspace_name_not_mktemp_shape", new_workspaces[0]
    for required in ("draft.md", "preflight_stdout.txt", "consumer_readback.json"):
        if required not in workspace_files:
            return "FAIL", "scratch_file_missing", f"{required} missing in owned workspace"
    if not stdout_capture or "STATUS:" not in stdout_capture or "ARTIFACT:" not in stdout_capture:
        return "FAIL", "producer_stdout_not_captured", "preflight_stdout.txt lacks STATUS:/ARTIFACT:"
    if artifact_json is None:
        return "FAIL", "artifact_missing", f"{ARTIFACT_REL} was not produced"
    if schema_errors:
        return "FAIL", "artifact_schema_invalid", "; ".join(schema_errors)[:300]
    status_line = re.search(r"^STATUS:\s*(\S+)", stdout_capture, re.MULTILINE)
    next_line = re.search(r"^NEXT_ACTION:\s*(\S+)", stdout_capture, re.MULTILINE)
    if not (status_line and next_line):
        return "FAIL", "producer_stdout_malformed", "STATUS/NEXT_ACTION lines absent"
    if status_line.group(1) != artifact_json.get("status") or next_line.group(1) != artifact_json.get("next_action"):
        return "FAIL", "producer_artifact_mismatch", "stdout STATUS/NEXT_ACTION differ from on-disk artifact"
    try:
        readback = json.loads(workspace_files["consumer_readback.json"].decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return "FAIL", "consumer_readback_unparsable", "consumer_readback.json is not valid JSON"
    if readback != artifact_json:
        return "FAIL", "consumer_readback_mismatch", "consumer_readback.json differs from on-disk artifact"
    # procedure の順序は marker 文字列だけでなく、実ファイルの mtime でも独立に確認する:
    # workspace 内 draft.md -> producer が書いた on-disk artifact -> stdout capture -> consumer readback。
    order = mtimes_ns or {}
    chain = ["draft.md", "artifact", "preflight_stdout.txt", "consumer_readback.json"]
    if any(name not in order for name in chain):
        return "FAIL", "procedure_order_unobserved", f"mtime observations missing: {sorted(set(chain) - set(order))}"
    if any(order[a] > order[b] for a, b in zip(chain, chain[1:])):
        return "FAIL", "procedure_order_violated", f"filesystem order differs from declared order: {order!r}"
    if new_fixed_tmp_entries:
        return "FAIL", "fixed_tmp_scratch_created", f"attributed fixed /tmp entries: {new_fixed_tmp_entries!r}"
    if observed_permission_mode is None:
        # 全ての scratch 観測が揃っても、Auto で動いたと観測できていなければ AC5 は充足しない (Auto を推測しない)。
        return (
            "SKIP",
            "permission_mode_unobserved",
            "main-session permission mode was not observed in runner evidence "
            "(runner structured evidence does not surface init permissionMode); Auto is not assumed",
        )
    return "PASS", "pass", "scratch producer->consumer observed in a real claude-gpt session under observed Auto mode"


def summarize_permissions(evidence: dict[str, Any] | None) -> dict[str, Any]:
    """実測できた permission 状態だけを記録する。未観測を Auto と推測しない。

    launcher route / 観測 mode / permission denial / approval carrier / 失敗層の区別を分けて残す。"""
    if not isinstance(evidence, dict):
        return {
            "observed_main_permission_mode": None,
            "permission_mode_source": "unobserved (no runner evidence)",
            "permission_denials": "unobserved",
            "approval_carrier": "unobserved",
        }
    denials = evidence.get("permission_denials")
    sidechannel = evidence.get("claude_gpt_proxy_sidechannel")
    mode = observed_main_permission_mode(evidence)
    subagentstop = (evidence.get("observed_runtime_fields") or {}).get("permission_mode")
    return {
        "launcher_route": {
            "claude_adapter": evidence.get("claude_adapter"),
            "launcher_receipt_present": isinstance(evidence.get("claude_gpt_launcher_receipt"), dict),
            "proxy_port_observed": isinstance(sidechannel, dict) and sidechannel.get("proxy_port") is not None,
        },
        "observed_main_permission_mode": mode,
        "permission_mode_source": (
            "runner evidence permission_mode_observed (claude system/init permissionMode)"
            if mode is not None
            else "unobserved: runner structured evidence has no main-session permissionMode surface; "
            "launcher source / argv declarations are not observations"
        ),
        "main_permission_mode_is_auto": mode == REQUIRED_MAIN_PERMISSION_MODE,
        "subagentstop_permission_mode_diagnostic_not_main_session": (
            subagentstop.get("value") if isinstance(subagentstop, dict) else None
        ),
        "permission_denials": denials if isinstance(denials, list) else "unobserved",
        "approval_carrier": evidence.get("approval_carrier", "none"),
        "extra_approval_for_routine_processing": (
            "not observed" if isinstance(denials, list) and not denials else "unknown_or_denied"
        ),
    }


def bounded(text: str | None, limit: int = LOG_BOUND_CHARS) -> str:
    if not text:
        return ""
    text = re.sub(r"(?i)(token|secret|password|authorization)[^\n]*", r"\1=[redacted]", text)
    return text if len(text) <= limit else text[:limit] + f"...[truncated {len(text) - limit} chars]"


# ---------------------------------------------------------------------------
# live 実行 (直接 CLI のみ)
# ---------------------------------------------------------------------------


def _git(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(["git", *args], cwd=REPO_ROOT, capture_output=True, text=True, check=False, timeout=60)


def _require_linked_worktree() -> str:
    git_dir = _git("rev-parse", "--absolute-git-dir")
    common = _git("rev-parse", "--path-format=absolute", "--git-common-dir")
    if git_dir.returncode != 0 or common.returncode != 0:
        raise Skip("not_linked_worktree", "git metadata unavailable")
    if Path(git_dir.stdout.strip()).resolve() == Path(common.stdout.strip()).resolve():
        raise Skip("not_linked_worktree", "this checkout is the root checkout, not a linked worktree")
    head = _git("rev-parse", "HEAD").stdout.strip()
    if not re.fullmatch(r"[0-9a-f]{40}", head):
        raise Skip("not_linked_worktree", "cannot resolve HEAD")
    return head


def _porcelain() -> str:
    return _git("status", "--porcelain").stdout


def _preflight_environment() -> str:
    if os.environ.get("CI"):
        raise Skip("ci", "CI environment: live Claude-GPT runtime is not run in CI")
    head = _require_linked_worktree()
    for tool in ("claude", "gh", "uv"):
        if shutil.which(tool) is None:
            raise Skip("cli_unavailable", f"{tool} not found on PATH")
    for path in (RUNNER, LAUNCHER, SCHEMA_PATH):
        if not path.exists():
            raise Skip("harness_unavailable", f"{path.relative_to(REPO_ROOT)} not found")
    return head


def _fetch_fixture(dest: Path) -> None:
    """Issue #2860 の live 本文を read-only で取得して offline fixture 化する (GitHub への書き込みなし)。"""
    proc = subprocess.run(
        ["gh", "issue", "view", str(ISSUE_NUMBER), "--repo", REPO_SLUG, "--json", "title,body,labels"],
        capture_output=True, text=True, check=False, timeout=60,
    )
    if proc.returncode != 0:
        raise Skip("network_or_auth_unavailable", f"gh issue view failed (exit {proc.returncode})")
    live = json.loads(proc.stdout)
    fixture = {
        "schema_version": "refinement_preflight_input/v1",
        "issue_number": ISSUE_NUMBER,
        "repo": REPO_SLUG,
        "now": datetime.now(timezone.utc).isoformat(),
        "issue": {
            "number": ISSUE_NUMBER,
            "title": live["title"],
            "body": live["body"],
            "labels": live.get("labels", []),
        },
        "comments": [],
        "anchor_comment_urls": [],
    }
    dest.write_text(json.dumps(fixture, ensure_ascii=False), encoding="utf-8")


def _tmp_listing() -> set[str]:
    root = REPO_ROOT / "tmp"
    return {p.name for p in root.iterdir()} if root.is_dir() else set()


def _fixed_tmp_listing() -> set[str]:
    return {p.name for p in Path(tempfile.gettempdir()).iterdir()}


def _validate_schema(artifact: dict[str, Any]) -> list[str]:
    import jsonschema

    schema = json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))
    validator = jsonschema.Draft202012Validator(schema)
    return [e.message for e in validator.iter_errors(artifact)]


def _write_artifact_log(
    *, verdict: str, classification: str, reason: str, exit_code: int, head: str | None,
    command: str, runner_stdout: str, runner_stderr: str, observations: dict[str, Any],
    started: datetime,
) -> Path:
    directory = REPO_ROOT / "artifacts"
    directory.mkdir(parents=True, exist_ok=True)
    stamp = started.strftime("%Y%m%dT%H%M%SZ")
    path = directory / f"runtime-verification-AC5-{stamp}.log"
    lines = [
        "=== Runtime Verification Log ===",
        "AC: AC5 (Issue #2860) claude-gpt-auto-scratch",
        f"Timestamp: {started.isoformat()}",
        f"Tested Head: {head or 'unresolved'}",
        "Launch Environment: scripts/claude-gpt/launch.sh via run_worktree_agent_runtime_smoke.py "
        "--claude-adapter claude-gpt --mode structured",
        f"Entrypoint: {command}",
        "",
        "--- Observations (bounded) ---",
        bounded(json.dumps(observations, ensure_ascii=False, indent=2, default=str), 6000),
        "",
        "--- Runner stdout (bounded) ---",
        bounded(runner_stdout),
        "--- Runner stderr (bounded) ---",
        bounded(runner_stderr),
        "",
        "--- Verdict ---",
        f"Result: {verdict}",
        f"Classification: {classification}",
        f"Exit Code: {exit_code}",
        f"Reason: {reason}",
        "Scope note: PASS is a single observation and does not establish stable false-deny resolution; "
        "SKIP is not PASS; production preflight.run executor is not exercised (requires canonical main root).",
        "",
    ]
    path.write_text("\n".join(lines), encoding="utf-8")
    return path


def run_case() -> int:
    started = datetime.now(timezone.utc)
    command = (
        "uv run --locked python3 .claude/skills/issue-refinement-loop/tests/"
        f"test_run_scoped_scratch_runtime.py --case {CASE_NAME}"
    )
    head: str | None = None
    runner_stdout = runner_stderr = ""
    observations: dict[str, Any] = {}
    harness_dir: Path | None = None
    created_workspace: Path | None = None
    artifact_path_abs = REPO_ROOT / ARTIFACT_REL
    artifact_preexisting = artifact_path_abs.exists() or artifact_path_abs.is_symlink()
    artifact_parent_preexisting = artifact_path_abs.parent.exists()
    run_started_ns = time.time_ns()
    try:
        head = _preflight_environment()
        if artifact_preexisting:
            raise Skip("harness_unavailable", f"{ARTIFACT_REL} pre-exists; refusing to overwrite an unrelated artifact")
        # harness 自身も同じ規約で workspace を確立する: canonical tmp/ root を先に materialize してから mkdtemp。
        (REPO_ROOT / "tmp").mkdir(parents=True, exist_ok=True)
        harness_dir = Path(tempfile.mkdtemp(prefix="ac5-harness.", dir=REPO_ROOT / "tmp"))
        fixture_path = harness_dir / "fixture.json"
        _fetch_fixture(fixture_path)
        prompt_file = harness_dir / "prompt.md"
        prompt_file.write_text(build_prompt(fixture_path=str(fixture_path.relative_to(REPO_ROOT))), encoding="utf-8")
        output_dir = harness_dir / "runner-out"
        evidence_json = harness_dir / "evidence.json"

        before_tmp = _tmp_listing()
        before_fixed = _fixed_tmp_listing()
        before_porcelain = _porcelain()

        argv = build_runner_argv(
            python=sys.executable, worktree=REPO_ROOT, prompt_file=prompt_file,
            output_dir=output_dir, evidence_json=evidence_json,
        )
        try:
            proc = subprocess.run(
                argv, cwd=REPO_ROOT, capture_output=True, text=True, check=False, timeout=OUTER_TIMEOUT_SECONDS
            )
        except subprocess.TimeoutExpired as exc:
            runner_stdout = (
                exc.stdout.decode("utf-8", "replace") if isinstance(exc.stdout, bytes) else (exc.stdout or "")
            )
            raise Skip("runner_timeout", f"outer timeout {OUTER_TIMEOUT_SECONDS}s before the runner finished") from exc
        runner_stdout, runner_stderr = proc.stdout, proc.stderr

        evidence: dict[str, Any] | None = None
        if evidence_json.exists():
            try:
                evidence = json.loads(evidence_json.read_text(encoding="utf-8"))
            except json.JSONDecodeError:
                evidence = None
        new_workspaces = sorted(
            name for name in _tmp_listing() - before_tmp
            if name != harness_dir.name and (REPO_ROOT / "tmp" / name).is_dir()
        )
        workspace_files: dict[str, bytes] = {}
        mtimes_ns: dict[str, int] = {}
        if len(new_workspaces) == 1:
            created_workspace = REPO_ROOT / "tmp" / new_workspaces[0]
            for child in created_workspace.iterdir():
                if child.is_file() and not child.is_symlink():
                    workspace_files[child.name] = child.read_bytes()
                    mtimes_ns[child.name] = child.stat().st_mtime_ns
        stdout_capture = (
            workspace_files["preflight_stdout.txt"].decode("utf-8", "replace")
            if "preflight_stdout.txt" in workspace_files else None
        )
        artifact_path = REPO_ROOT / ARTIFACT_REL
        artifact_json: dict[str, Any] | None = None
        schema_errors: list[str] = []
        if artifact_path.is_file() and not artifact_path.is_symlink():
            mtimes_ns["artifact"] = artifact_path.stat().st_mtime_ns
            try:
                artifact_json = json.loads(artifact_path.read_text(encoding="utf-8"))
                schema_errors = _validate_schema(artifact_json)
            except json.JSONDecodeError:
                schema_errors = ["artifact is not valid JSON"]
        candidate_fixed = sorted(
            name for name in _fixed_tmp_listing() - before_fixed
            if re.search(r"issue|readback|anchor|guard_result|body", name, re.IGNORECASE)
        )
        # OS temp 全体の差分は無関係な並行 process の write を含みうる。child/runtime evidence に
        # attribution できる entry だけを dispositive にし、残りは diagnostic として log に残す。
        attribution_corpus = "\n".join(
            [json.dumps(evidence, ensure_ascii=False, default=str) if evidence else "", runner_stdout, runner_stderr]
        )
        new_fixed, unattributed_fixed = attribute_fixed_tmp_entries(
            candidate_fixed, attribution_corpus=attribution_corpus
        )
        porcelain_changed = _porcelain() != before_porcelain

        verdict, classification, reason = adjudicate(
            runner_exit=proc.returncode, evidence=evidence, new_workspaces=new_workspaces,
            workspace_files=workspace_files, stdout_capture=stdout_capture, artifact_json=artifact_json,
            schema_errors=schema_errors, new_fixed_tmp_entries=new_fixed, mtimes_ns=mtimes_ns,
            observed_permission_mode=observed_main_permission_mode(evidence),
        )
        if verdict == "PASS" and porcelain_changed:
            verdict, classification, reason = "FAIL", "tracked_tree_changed", "git status changed during the live run"
        observations = {
            "runner_exit": proc.returncode,
            "new_owned_workspaces": new_workspaces,
            "workspace_files": sorted(workspace_files),
            "producer_stdout_head": bounded(stdout_capture, 600),
            "artifact_status": (artifact_json or {}).get("status"),
            "artifact_next_action": (artifact_json or {}).get("next_action"),
            "artifact_schema_errors": schema_errors,
            "consumer_readback_equals_artifact": (
                bool(workspace_files.get("consumer_readback.json"))
                and artifact_json is not None
                and _safe_json(workspace_files.get("consumer_readback.json")) == artifact_json
            ),
            "attributed_fixed_tmp_entries": new_fixed,
            "unattributed_global_tmp_diff_diagnostic_only": unattributed_fixed,
            "runner_evidence_keys": sorted(evidence) if isinstance(evidence, dict) else None,
            "mtime_order_ns_relative": (
                {k: v - min(mtimes_ns.values()) for k, v in mtimes_ns.items()} if mtimes_ns else {}
            ),
            "git_status_changed": porcelain_changed,
            "permission": summarize_permissions(evidence),
            "runner_evidence_subset": _evidence_subset(evidence),
            "classification_of_failure_layer": {
                "launcher/adapter": "claude_adapter, claude_gpt_launcher_receipt, claude_gpt_proxy_sidechannel",
                "classifier/hook denial": "permission_denials (runner-reported)",
                "permission mode": "observed_main_permission_mode (runner evidence only; None -> SKIP)",
                "harness limitation": (
                    "preflight.run executor needs canonical main root; offline fixture producer observed instead"
                ),
            },
        }
        exit_code = {"PASS": EXIT_OK, "SKIP": EXIT_SKIP}.get(verdict, EXIT_FAIL)
    except Skip as skip:
        verdict, classification, reason, exit_code = "SKIP", skip.reason_class, skip.reason, EXIT_SKIP
        observations.setdefault("skip_reason_class", skip.reason_class)
    finally:
        # 自分が作った directory だけを片付ける (foreign cleanup なし)。
        for owned in (created_workspace, harness_dir):
            if owned is not None and owned.is_dir() and owned.parent == REPO_ROOT / "tmp":
                shutil.rmtree(owned, ignore_errors=True)
        # canonical artifact directory は他 invocation の artifact と共存する。exact file だけを unlink し、
        # 親は run 前に不在かつ cleanup 時点で空の場合に限り rmdir する (recursive 削除は禁止)。
        if not artifact_preexisting:
            cleanup_report = cleanup_exact_artifact(
                artifact_path_abs, started_ns=run_started_ns, parent_preexisting=artifact_parent_preexisting
            )
            observations["artifact_cleanup"] = cleanup_report

    log_path = _write_artifact_log(
        verdict=verdict, classification=classification, reason=reason, exit_code=exit_code, head=head,
        command=command, runner_stdout=runner_stdout, runner_stderr=runner_stderr,
        observations=observations, started=started,
    )
    print(f"artifact: {log_path.relative_to(REPO_ROOT)}")
    if verdict == "SKIP":
        print(f"SKIP: {classification}: {reason}")
    elif verdict == "FAIL":
        print(f"FAIL: {classification}: {reason}", file=sys.stderr)
    else:
        print(f"PASS: {reason}")
    return exit_code


def _safe_json(raw: bytes | None) -> Any:
    try:
        return json.loads(raw.decode("utf-8")) if raw is not None else None
    except (UnicodeDecodeError, json.JSONDecodeError):
        return None


def _evidence_subset(evidence: dict[str, Any] | None) -> dict[str, Any]:
    if not isinstance(evidence, dict):
        return {}
    keys = (
        "claude_adapter", "exit_code", "process_exit_code", "timed_out", "terminal_event_observed",
        "ordered_evidence_match", "output_contract_schema_validation", "claude_gpt_launcher_receipt",
        "claude_gpt_proxy_sidechannel", "main_agent_identity", "capability_decision", "errors",
    )
    return {k: evidence.get(k) for k in keys if k in evidence}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--case", required=True, choices=[CASE_NAME])
    args = parser.parse_args(argv)
    if args.case == CASE_NAME:
        return run_case()
    return EXIT_FAIL  # pragma: no cover


# ---------------------------------------------------------------------------
# offline unit tests (pytest 収集用)。live 実行はしない。判定ロジックの fail-closed だけを確認する。
# ---------------------------------------------------------------------------


def _good_inputs() -> dict[str, Any]:
    artifact = {"status": "pass", "next_action": "proceed", "k": 1}
    return {
        "runner_exit": 0,
        "evidence": {
            "claude_adapter": "claude-gpt",
            "claude_gpt_proxy_sidechannel": {"proxy_port": 1234},
            "ordered_evidence_match": {"verified": True},
            "output_contract_schema_validation": {"verified": True},
        },
        "new_workspaces": ["refinement-2860.AbC123"],
        "workspace_files": {
            "draft.md": b"x",
            "preflight_stdout.txt": b"STATUS: pass\nNEXT_ACTION: proceed\nARTIFACT:\n",
            "consumer_readback.json": json.dumps(artifact).encode(),
        },
        "stdout_capture": "STATUS: pass\nNEXT_ACTION: proceed\nARTIFACT:\n",
        "artifact_json": artifact,
        "schema_errors": [],
        "new_fixed_tmp_entries": [],
        "mtimes_ns": {"draft.md": 1, "artifact": 2, "preflight_stdout.txt": 3, "consumer_readback.json": 4},
        "observed_permission_mode": "auto",
    }


def test_adjudicate_passes_only_with_all_observations() -> None:
    assert adjudicate(**_good_inputs())[0] == "PASS"


def test_adjudicate_runner_exit_77_is_skip_never_pass() -> None:
    inputs = _good_inputs()
    inputs["runner_exit"] = EXIT_SKIP
    assert adjudicate(**inputs)[0] == "SKIP"


def test_adjudicate_marker_only_without_workspace_is_fail() -> None:
    inputs = _good_inputs()
    inputs["new_workspaces"] = []
    verdict, classification, _ = adjudicate(**inputs)
    assert (verdict, classification) == ("FAIL", "marker_only_no_workspace")


def test_adjudicate_fallback_only_is_fail() -> None:
    inputs = _good_inputs()
    inputs["evidence"] = {**inputs["evidence"], "claude_adapter": "native"}
    assert adjudicate(**inputs)[:2] == ("FAIL", "fallback_only")
    inputs = _good_inputs()
    inputs["evidence"] = {**inputs["evidence"], "claude_gpt_proxy_sidechannel": {"proxy_port": None}}
    assert adjudicate(**inputs)[:2] == ("FAIL", "fallback_only")


def test_adjudicate_readback_or_artifact_mismatch_is_fail() -> None:
    inputs = _good_inputs()
    inputs["workspace_files"] = {**inputs["workspace_files"], "consumer_readback.json": b'{"status": "pass"}'}
    assert adjudicate(**inputs)[:2] == ("FAIL", "consumer_readback_mismatch")
    inputs = _good_inputs()
    inputs["stdout_capture"] = "STATUS: needs_fix\nNEXT_ACTION: proceed\nARTIFACT:\n"
    assert adjudicate(**inputs)[:2] == ("FAIL", "producer_artifact_mismatch")


def test_adjudicate_filesystem_order_violation_or_missing_order_is_fail() -> None:
    inputs = _good_inputs()
    inputs["mtimes_ns"] = {**inputs["mtimes_ns"], "consumer_readback.json": 0}
    assert adjudicate(**inputs)[:2] == ("FAIL", "procedure_order_violated")
    inputs = _good_inputs()
    inputs["mtimes_ns"] = None
    assert adjudicate(**inputs)[:2] == ("FAIL", "procedure_order_unobserved")


def test_adjudicate_observed_auto_is_required_for_pass() -> None:
    inputs = _good_inputs()
    inputs["observed_permission_mode"] = "auto"
    assert adjudicate(**inputs)[:2] == ("PASS", "pass")


def test_adjudicate_unobserved_permission_mode_is_skip_never_pass() -> None:
    inputs = _good_inputs()
    inputs["observed_permission_mode"] = None
    verdict, classification, _ = adjudicate(**inputs)
    assert (verdict, classification) == ("SKIP", "permission_mode_unobserved")


@pytest.mark.parametrize(
    "mode", ["default", "acceptEdits", "plan", "bypassPermissions", "conflicting:auto,default", ""]
)
def test_adjudicate_non_auto_observed_mode_is_fail_not_auto_evidence(mode: str) -> None:
    inputs = _good_inputs()
    inputs["observed_permission_mode"] = mode
    assert adjudicate(**inputs)[:2] == ("FAIL", "permission_mode_not_auto")


def test_launcher_declaration_or_argv_string_is_not_a_permission_observation() -> None:
    # launcher source が `--permission-mode auto` を注入する事実や、argv 文字列・自己申告 field は観測ではない。
    declared = {
        "claude_adapter": "claude-gpt",
        "declared_permission_mode": "auto",
        "claude_gpt_launcher_receipt": {"argv": ["--permission-mode", "auto"]},
        "observed_runtime_fields": {"permission_mode": {"value": "auto", "source_hook_event": "SubagentStop"}},
    }
    assert observed_main_permission_mode(declared) is None
    assert observed_main_permission_mode(None) is None
    assert observed_main_permission_mode({"permission_mode_observed": None}) is None
    assert observed_main_permission_mode({"permission_mode_observed": 7}) is None
    inputs = _good_inputs()
    inputs["observed_permission_mode"] = observed_main_permission_mode(declared)
    assert adjudicate(**inputs)[0] == "SKIP"


def test_observed_main_permission_mode_reads_only_runner_init_surfaces() -> None:
    assert observed_main_permission_mode({"permission_mode_observed": "auto"}) == "auto"
    nested = {"named_subagent_resume": {"permission_mode_observed": "default"}}
    assert observed_main_permission_mode(nested) == "default"
    both = {"permission_mode_observed": "auto", "named_subagent_resume": {"permission_mode_observed": "default"}}
    assert observed_main_permission_mode(both) == "conflicting:auto,default"


def test_runner_structured_evidence_has_no_main_permission_mode_surface_today() -> None:
    """runner の根拠: init permissionMode は named-subagent-resume evidence builder にだけ載る。

    structured evidence (schema_summary) の top-level には ``permission_mode_observed`` が書かれない。
    将来 runner がこの surface を追加すれば本 test が検知し、SKIP 経路を PASS 経路へ切り替えられる。"""
    source = RUNNER.read_text(encoding="utf-8")
    assert 'obs["init"]["permission_mode"] = mode if isinstance(mode, str) else None' in source
    assert '"permission_mode_observed": obs["init"]["permission_mode"]' in source
    assert 'schema_summary["permission_mode_observed"]' not in source


def test_summarize_permissions_records_observed_mode_and_layers_without_assuming_auto() -> None:
    unobserved = summarize_permissions({"claude_adapter": "claude-gpt", "permission_denials": []})
    assert unobserved["observed_main_permission_mode"] is None
    assert unobserved["main_permission_mode_is_auto"] is False
    assert "unobserved" in unobserved["permission_mode_source"]
    assert unobserved["launcher_route"]["claude_adapter"] == "claude-gpt"
    observed = summarize_permissions({"claude_adapter": "claude-gpt", "permission_mode_observed": "auto",
                                      "permission_denials": []})
    assert observed["observed_main_permission_mode"] == "auto"
    assert observed["main_permission_mode_is_auto"] is True
    assert summarize_permissions(None)["observed_main_permission_mode"] is None


def test_adjudicate_fixed_tmp_scratch_or_missing_evidence_is_fail() -> None:
    inputs = _good_inputs()
    inputs["new_fixed_tmp_entries"] = ["issue2860_readback.json"]
    assert adjudicate(**inputs)[:2] == ("FAIL", "fixed_tmp_scratch_created")
    inputs = _good_inputs()
    inputs["evidence"] = None
    assert adjudicate(**inputs)[:2] == ("FAIL", "evidence_missing")


def test_unrelated_concurrent_global_tmp_files_are_diagnostic_not_fail() -> None:
    # 無関係な並行 process が OS temp に作った file は child/runtime evidence に attribution できない。
    corpus = json.dumps({"claude_adapter": "claude-gpt", "errors": []}) + "runner stdout without tmp names"
    attributed, unattributed = attribute_fixed_tmp_entries(
        ["contract_review_once_body_ab12.md", "issue_snapshot_zz.json"], attribution_corpus=corpus
    )
    assert attributed == []
    assert unattributed == ["contract_review_once_body_ab12.md", "issue_snapshot_zz.json"]
    inputs = _good_inputs()
    inputs["new_fixed_tmp_entries"] = attributed
    assert adjudicate(**inputs)[0] == "PASS"


def test_global_tmp_entry_attributable_to_child_evidence_is_dispositive_fail() -> None:
    corpus = json.dumps({"permission_denials": [{"tool_input": {"command": "cat > /tmp/issue2860_readback.json"}}]})
    attributed, unattributed = attribute_fixed_tmp_entries(
        ["issue2860_readback.json", "unrelated_body_1.md"], attribution_corpus=corpus
    )
    assert attributed == ["issue2860_readback.json"]
    assert unattributed == ["unrelated_body_1.md"]
    inputs = _good_inputs()
    inputs["new_fixed_tmp_entries"] = attributed
    assert adjudicate(**inputs)[:2] == ("FAIL", "fixed_tmp_scratch_created")


def _artifact_fixture(root: Path, *, parent_exists: bool) -> tuple[Path, Path]:
    parent = root / ".claude" / "artifacts" / "issue-refinement-loop" / str(ISSUE_NUMBER)
    if parent_exists:
        parent.mkdir(parents=True)
    return parent, parent / "refinement_preflight_result_v1.json"


def test_cleanup_keeps_sibling_sentinel_byte_identical_and_unlinks_only_exact_artifact(tmp_path: Path) -> None:
    parent, artifact = _artifact_fixture(tmp_path, parent_exists=True)
    sentinels = {
        parent / "raw_issue_snapshot.json": b'{"foreign": "snapshot"}\n',
        parent / "planner_input.json": b"\x00\x01 binary sentinel",
    }
    for path, content in sentinels.items():
        path.write_bytes(content)
    nested = parent / "snapshots" / "archive.json"
    nested.parent.mkdir()
    nested.write_bytes(b"archived")
    started = time.time_ns()
    artifact.write_text("{}", encoding="utf-8")

    report = cleanup_exact_artifact(artifact, started_ns=started, parent_preexisting=True)

    assert report["artifact_unlinked"] is True and report["parent_removed"] is False
    assert not artifact.exists()
    for path, content in sentinels.items():
        assert path.read_bytes() == content
    assert nested.read_bytes() == b"archived"


def test_cleanup_does_not_remove_a_foreign_sibling_even_when_this_run_created_the_parent(tmp_path: Path) -> None:
    parent, artifact = _artifact_fixture(tmp_path, parent_exists=False)
    started = time.time_ns()
    parent.mkdir(parents=True)
    artifact.write_text("{}", encoding="utf-8")
    foreign = parent / "provenance.json"
    foreign.write_bytes(b"foreign appeared during the run")

    report = cleanup_exact_artifact(artifact, started_ns=started, parent_preexisting=False)

    assert report["artifact_unlinked"] is True and report["parent_removed"] is False
    assert report["parent_kept_reason"] == "parent_not_empty"
    assert foreign.read_bytes() == b"foreign appeared during the run"


def test_cleanup_rmdirs_the_parent_only_when_this_run_created_it_and_it_is_empty(tmp_path: Path) -> None:
    parent, artifact = _artifact_fixture(tmp_path, parent_exists=False)
    started = time.time_ns()
    parent.mkdir(parents=True)
    artifact.write_text("{}", encoding="utf-8")

    report = cleanup_exact_artifact(artifact, started_ns=started, parent_preexisting=False)

    assert report["artifact_unlinked"] is True and report["parent_removed"] is True
    assert not parent.exists()
    assert parent.parent.is_dir(), "親の親 (issue-refinement-loop/) は触らない"


def test_cleanup_never_unlinks_a_stale_or_symlinked_artifact(tmp_path: Path) -> None:
    parent, artifact = _artifact_fixture(tmp_path, parent_exists=True)
    artifact.write_text("{}", encoding="utf-8")
    stale = cleanup_exact_artifact(artifact, started_ns=time.time_ns() + 10**12, parent_preexisting=True)
    assert stale["artifact_unlinked"] is False and artifact.exists()
    artifact.unlink()
    target = tmp_path / "foreign-target.json"
    target.write_bytes(b"keep")
    artifact.symlink_to(target)
    linked = cleanup_exact_artifact(artifact, started_ns=0, parent_preexisting=True)
    assert linked["artifact_unlinked"] is False
    assert target.read_bytes() == b"keep"


def test_harness_source_does_not_recursively_remove_the_canonical_artifact_directory() -> None:
    import ast

    source = _THIS_FILE.read_text(encoding="utf-8")
    tree = ast.parse(source)
    rmtree_args = [
        ast.unparse(arg)
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and (getattr(node.func, "attr", None) == "rmtree" or getattr(node.func, "id", None) == "rmtree")
        for arg in node.args[:1]
    ]
    assert rmtree_args, "owned workspace cleanup の rmtree 呼び出しが見つからない (検査が空振りしていないこと)"
    assert not any("ARTIFACT" in arg.upper() or "artifact" in arg for arg in rmtree_args), rmtree_args
    assert "cleanup_exact_artifact(" in source


def test_runner_argv_uses_claude_gpt_without_native_only_flags() -> None:
    argv = build_runner_argv(
        python="python3", worktree=Path("/w"), prompt_file=Path("/w/p.md"),
        output_dir=Path("/w/o"), evidence_json=Path("/w/e.json"),
    )
    assert argv[argv.index("--claude-adapter") + 1] == "claude-gpt"
    assert "--expect-skill-command" not in argv
    assert "--expect-marker-source" not in argv
    assert argv.count("--expect-ordered-marker") == len(ORDERED_MARKERS)
    # launch.sh が exactly one の --permission-mode auto を注入し caller 由来を拒否するため、caller は渡さない。
    assert "--permission-mode" not in argv
    assert argv[argv.index("--output-schema-path") + 1].endswith("refinement_preflight_result_v1.schema.json")


def test_prompt_does_not_enumerate_the_correct_shell_commands() -> None:
    prompt = build_prompt(fixture_path="tmp/x/fixture.json")
    assert "mktemp" not in prompt and "mkdir" not in prompt


def test_canonical_schema_and_producer_script_exist() -> None:
    assert SCHEMA_PATH.is_file()
    assert (REPO_ROOT / PREFLIGHT_SCRIPT_REL).is_file()


if __name__ == "__main__":
    sys.exit(main())
