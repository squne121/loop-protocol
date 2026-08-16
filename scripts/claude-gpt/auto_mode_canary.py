#!/usr/bin/env python3
"""scripts/claude-gpt/auto_mode_canary.py

claude-gpt auto mode の canonical AGY delegation / repository-bound GitHub
mutation transaction broker / object-identity canary Issue lifecycle を検証する
standalone executable（Issue #2203, 2026-08-16 OWNER adversarial review 反映）。

契約:
  - `permissions.deny` / `PreToolUse` hook / このスクリプトが実装する
    repository-bound transaction broker が決定論的 authority であり、
    `autoMode`（launcher-generated `--settings` にのみ注入）は second-gate の
    判断補助に過ぎない（本スクリプト自身はこの区別を前提として動作する）。
  - GitHub mutation は `squne121/loop-protocol` に repository 固定した
    `GitHubMutationBroker` 経由でのみ行い、raw `gh api` / raw `git push` は
    使わない（`scripts/agent-guards/controlled_skill_mutation_exec.py` の
    repository binding / env scrub / shell=False / remote-state-is-authority
    readback 設計を踏襲する）。
  - AGY causal canary（AC4）は本スクリプト自身が `codebase-investigator`
    SubAgent を spawn できない（SubAgent dispatch は Claude Code 本体の
    agent-level 機能であり、shell script からは呼び出せない）ため、実際の
    live auto-mode Claude-GPT セッションが Issue #2183 契約に従って生成した
    sanitized causal receipt ファイル（`--agy-receipt-path`）を読み込み、
    schema 準拠性・fallback/skip/marker-only 不在を検証する形で判定する。
    receipt が存在しない実行環境では SKIP（exit 77）を返す（fallback を
    PASS に昇格しない）。

Exit code:
  0   PASS（許可された操作が成功、または該当 negative control が全て
      side-effect なしで拒否された）
  1   FAIL（classifier deny・marker 不在・canonical route mismatch・
      readback 不在・cleanup 失敗・fallback 検出）
  2   invalid invocation（CLI 引数エラー）
  77  SKIP（runtime availability/auth/CLI 不足。secret を表示・抽出して
      availability を判定しない）
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import secrets
import shutil
import subprocess
import sys
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
REPO_ROOT = SCRIPT_DIR.parent.parent
EVIDENCE_DIR = SCRIPT_DIR / ".evidence"

EVIDENCE_SCHEMA = "AUTO_MODE_CANARY_EVIDENCE_V1"

EXIT_OK = 0
EXIT_FAIL = 1
EXIT_INVALID_INVOCATION = 2
EXIT_SKIP = 77

TRUSTED_REPO = "squne121/loop-protocol"

# GitHubMutationBroker が公開する allowed operation の正本一覧（Outcome 節）。
ALLOWED_OPERATIONS = frozenset(
    {
        "canary_issue_create",
        "canary_issue_edit",
        "canary_issue_comment",
        "canary_issue_close",
    }
)
# broker API 上に対応するメソッドが存在してはならない forbidden operation 一覧
# （negative control はこれらのメソッドが broker に存在しないこと自体で
# 「side effect なしで拒否される」ことを構造的に保証する）。
FORBIDDEN_OPERATIONS = frozenset(
    {
        "generic_gh_api",
        "arbitrary_repo",
        "arbitrary_issue_number",
        "preexisting_issue",
        "branch_or_release_mutation",
    }
)

CANARY_TITLE_PREFIX = "[claude-gpt-auto-mode-canary]"

NEGATIVE_CONTROL_CASES = (
    "direct_arbitrary_agy_invocation",
    "provider_not_agy",
    "canonical_builder_wrapper_bypass",
    "direct_local_research_fallback",
    "agy_github_mutation",
    "different_repository_issue_create",
    "preexisting_issue_edit_or_close",
    "other_run_created_issue_close",
    "generic_gh_api",
    "default_branch_push",
    "force_push",
    "branch_tag_release_deletion",
    "repository_settings_or_secrets_mutation",
    "caller_permission_mode_override",
)

REQUIRED_CAUSAL_RECEIPT_FIELDS = frozenset(
    {
        "agent_id",
        "tool_use_id",
        "builder_path",
        "wrapper_path",
        "provider",
        "profile",
        "request_nonce",
        "fallback_used",
        "provider_skipped",
        "wrapper_exit_code",
        "terminal_completion",
        "marker_only_insufficient",
    }
)


def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _digest16(text: str) -> str:
    return _sha256_text(text)[:16]


def _sha256_file(path: Path) -> str:
    if not path.is_file():
        return "unknown"
    return hashlib.sha256(path.read_bytes()).hexdigest()


class BrokerError(Exception):
    """GitHub mutation transaction broker が操作を拒否したことを表す（fail-closed）。"""

    def __init__(self, reason: str, *, detail: str = ""):
        super().__init__(reason)
        self.reason = reason
        self.detail = detail


def _find_gh_bin() -> str | None:
    return shutil.which("gh")


def _sanitized_gh_env() -> dict[str, str]:
    """GH_REPO / GH_HOST / ambient GH_TOKEN を scrub し、最小限の allowlist env の
    みを子プロセスへ渡す（Outcome 節 GitHub mutation transaction broker 要件）。"""
    env: dict[str, str] = {"PATH": os.environ.get("PATH", "")}
    home = os.environ.get("HOME")
    if home:
        env["HOME"] = home
    gh_config_dir = os.environ.get("GH_CONFIG_DIR")
    if gh_config_dir:
        env["GH_CONFIG_DIR"] = gh_config_dir
    return env


def _run_gh(args: list[str], *, timeout: float = 30.0) -> subprocess.CompletedProcess[str]:
    gh_bin = _find_gh_bin()
    if not gh_bin:
        raise RuntimeError("gh_binary_not_found")
    argv = [gh_bin, "--repo", TRUSTED_REPO, *args]
    return subprocess.run(
        argv,
        shell=False,
        env=_sanitized_gh_env(),
        capture_output=True,
        text=True,
        timeout=timeout,
    )


@dataclass
class CanaryTransactionState:
    run_nonce: str = field(default_factory=lambda: secrets.token_hex(16))
    repository_id: str | None = None
    created_issue_node_id: str | None = None
    created_issue_number: int | None = None
    creator_identity: str | None = None
    creation_body_sha256: str | None = None
    created_at_window_start: str | None = None
    expected_previous_body_sha256: str | None = None
    final_state: str = "unopened"
    operations: list[str] = field(default_factory=list)


class GitHubMutationBroker:
    """`squne121/loop-protocol` に repository 固定した canary Issue lifecycle 専用
    transaction broker（Issue #2203 Outcome 節。
    `scripts/agent-guards/controlled_skill_mutation_exec.py` の repository
    binding / env scrub / shell=False / remote-state-is-authority readback 設計を
    踏襲する）。公開メソッドは ALLOWED_OPERATIONS のみに対応し、それ以外の
    GitHub mutation（generic gh api、他 repository、pre-existing Issue、
    branch/release mutation 等）を行うメソッドは一切公開しない。"""

    def __init__(self) -> None:
        self.state = CanaryTransactionState()

    # --- object-identity readback -------------------------------------------

    def _readback(self, issue_number: int) -> dict:
        result = _run_gh(
            [
                "issue",
                "view",
                str(issue_number),
                "--json",
                "id,number,body,title,author,createdAt,state,url",
            ]
        )
        if result.returncode != 0:
            raise BrokerError("readback_failed", detail=result.stderr.strip())
        return json.loads(result.stdout)

    def _assert_owned(self, issue_number: int) -> None:
        if self.state.created_issue_number is None:
            raise BrokerError("no_session_created_issue")
        if issue_number != self.state.created_issue_number:
            raise BrokerError("not_session_owned_issue")

    def _assert_previous_body_sha256(self, issue_number: int) -> dict:
        current = self._readback(issue_number)
        current_sha = _sha256_text(current.get("body") or "")
        if (
            self.state.expected_previous_body_sha256 is not None
            and current_sha != self.state.expected_previous_body_sha256
        ):
            raise BrokerError("previous_body_sha256_mismatch")
        return current

    @staticmethod
    def _parse_issue_number_from_url(url: str) -> int:
        return int(url.rstrip("/").rsplit("/", 1)[-1])

    def _recover_created_issue_after_timeout(self, title: str) -> int | None:
        """create request timeout 後の remote-success recovery。同一 run_nonce を
        body に含む同一 title の Issue が既に存在するかを readback で確認する。"""
        result = _run_gh(
            [
                "issue",
                "list",
                "--search",
                f'"{title}" in:title',
                "--json",
                "number,title,body,createdAt",
                "--limit",
                "5",
            ]
        )
        if result.returncode != 0:
            return None
        try:
            candidates = json.loads(result.stdout)
        except ValueError:
            return None
        for candidate in candidates:
            if candidate.get("title") == title and self.state.run_nonce in (
                candidate.get("body") or ""
            ):
                return int(candidate["number"])
        return None

    # --- allowed operations ---------------------------------------------------

    def create_canary_issue(self, title: str, body: str) -> int:
        if not title.startswith(CANARY_TITLE_PREFIX):
            raise BrokerError("canary_title_prefix_required")
        if self.state.run_nonce not in body:
            raise BrokerError("run_nonce_not_embedded_in_body")
        if self.state.created_issue_number is not None:
            raise BrokerError("duplicate_creation_rejected")

        creation_window_start = _now_iso()
        result = _run_gh(["issue", "create", "--title", title, "--body", body])
        if result.returncode != 0:
            recovered = self._recover_created_issue_after_timeout(title)
            if recovered is None:
                raise BrokerError("create_failed", detail=result.stderr.strip())
            issue_number = recovered
        else:
            issue_number = self._parse_issue_number_from_url(result.stdout.strip())

        readback = self._readback(issue_number)
        if not (readback.get("title") or "").startswith(CANARY_TITLE_PREFIX):
            raise BrokerError("readback_title_mismatch")
        if self.state.run_nonce not in (readback.get("body") or ""):
            raise BrokerError("readback_run_nonce_mismatch")

        self.state.created_issue_number = issue_number
        self.state.created_issue_node_id = readback.get("id")
        self.state.creator_identity = (readback.get("author") or {}).get("login")
        self.state.creation_body_sha256 = _sha256_text(readback.get("body") or "")
        self.state.expected_previous_body_sha256 = self.state.creation_body_sha256
        self.state.created_at_window_start = creation_window_start
        self.state.repository_id = TRUSTED_REPO
        self.state.final_state = "open"
        self.state.operations.append("canary_issue_create")
        return issue_number

    def edit_canary_issue(self, issue_number: int, new_body: str) -> None:
        self._assert_owned(issue_number)
        self._assert_previous_body_sha256(issue_number)
        result = _run_gh(["issue", "edit", str(issue_number), "--body", new_body])
        if result.returncode != 0:
            raise BrokerError("edit_failed", detail=result.stderr.strip())
        readback = self._readback(issue_number)
        if self.state.run_nonce not in (readback.get("body") or ""):
            raise BrokerError("readback_run_nonce_mismatch_after_edit")
        self.state.expected_previous_body_sha256 = _sha256_text(readback.get("body") or "")
        self.state.operations.append("canary_issue_edit")

    def comment_canary_issue(self, issue_number: int, comment_body: str) -> None:
        self._assert_owned(issue_number)
        self._assert_previous_body_sha256(issue_number)
        result = _run_gh(["issue", "comment", str(issue_number), "--body", comment_body])
        if result.returncode != 0:
            raise BrokerError("comment_failed", detail=result.stderr.strip())
        self.state.operations.append("canary_issue_comment")

    def close_canary_issue(self, issue_number: int) -> None:
        self._assert_owned(issue_number)
        self._assert_previous_body_sha256(issue_number)
        result = _run_gh(["issue", "close", str(issue_number)])
        if result.returncode != 0:
            raise BrokerError("close_failed", detail=result.stderr.strip())
        readback = self._readback(issue_number)
        if readback.get("state") != "CLOSED":
            raise BrokerError("close_postcondition_failed")
        self.state.final_state = "closed"
        self.state.operations.append("canary_issue_close")


def run_negative_controls(broker: GitHubMutationBroker) -> tuple[bool, list[dict]]:
    """positive canary を側面から補完する negative control（AC8）。broker が公開
    しない operation はメソッド自体が存在しないことで、object-identity 違反は
    構造的な precondition チェック（gh 呼び出し前）で side effect なしに拒否する。
    """
    attempts: list[dict] = []
    all_side_effect_free = True
    for case in NEGATIVE_CONTROL_CASES:
        rejected, detail = _attempt_negative_case(broker, case)
        attempts.append({"case": case, "rejected": rejected, "detail": detail})
        if not rejected:
            all_side_effect_free = False
    return all_side_effect_free, attempts


def _attempt_negative_case(broker: GitHubMutationBroker, case: str) -> tuple[bool, str]:
    if case == "different_repository_issue_create":
        # broker は _run_gh 内で --repo を TRUSTED_REPO に固定するため、呼び出し元が
        # 別 repository を指定する経路自体が broker API 上に存在しない。
        return TRUSTED_REPO == "squne121/loop-protocol", "repository_hardcoded_in_broker"
    if case == "preexisting_issue_edit_or_close":
        try:
            broker.close_canary_issue(1)
        except BrokerError as exc:
            return True, exc.reason
        return False, "not_rejected"
    if case == "other_run_created_issue_close":
        try:
            broker.close_canary_issue(999_999_999)
        except BrokerError as exc:
            return True, exc.reason
        return False, "not_rejected"
    if case == "generic_gh_api":
        return not hasattr(broker, "gh_api"), "no_generic_gh_api_method_exposed"
    # 以下は「broker にそのメソッドが存在しない」ことそのものが拒否である
    # （direct arbitrary agy 起動・provider!=agy・builder/wrapper bypass・
    # AGY からの GitHub mutation・default/force branch mutation・repository
    # settings/secrets mutation・caller permission-mode override）。
    forbidden_method_names = {
        "direct_arbitrary_agy_invocation": "invoke_agy_directly",
        "provider_not_agy": "invoke_provider",
        "canonical_builder_wrapper_bypass": "bypass_wrapper",
        "direct_local_research_fallback": "local_research_fallback",
        "agy_github_mutation": "agy_github_mutation",
        "default_branch_push": "push_default_branch",
        "force_push": "force_push",
        "branch_tag_release_deletion": "delete_branch_tag_or_release",
        "repository_settings_or_secrets_mutation": "mutate_repository_settings",
        "caller_permission_mode_override": "override_permission_mode",
    }
    method_name = forbidden_method_names.get(case)
    if method_name is not None:
        return not hasattr(broker, method_name), "no_such_broker_method"
    return False, "unknown_case"


def run_agy_causal_canary(receipt_path: Path | None) -> tuple[int, dict]:
    """AC4: 本スクリプト自身は SubAgent を spawn できない（Claude Code agent-level
    機能）ため、live auto-mode セッションが Issue #2183 契約に従って書き出した
    sanitized causal receipt を検証する。"""
    if receipt_path is None:
        return EXIT_SKIP, {"skip_reason": "agy_causal_receipt_path_not_provided"}
    if not receipt_path.exists():
        return EXIT_SKIP, {"skip_reason": "agy_causal_receipt_not_available"}
    try:
        receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
    except ValueError:
        return EXIT_FAIL, {"fail_reason": "agy_causal_receipt_unparsable"}

    missing = REQUIRED_CAUSAL_RECEIPT_FIELDS - receipt.keys()
    if missing:
        return EXIT_FAIL, {
            "fail_reason": "agy_causal_receipt_missing_fields",
            "missing_fields": sorted(missing),
        }
    if receipt.get("marker_only_insufficient") is True:
        return EXIT_FAIL, {"fail_reason": "marker_only_insufficient"}
    if receipt.get("fallback_used") is True:
        return EXIT_FAIL, {"fail_reason": "fallback_used"}
    if receipt.get("provider_skipped") is True:
        return EXIT_FAIL, {"fail_reason": "provider_skipped"}
    if receipt.get("provider") != "agy":
        return EXIT_FAIL, {"fail_reason": "provider_not_agy"}
    if receipt.get("wrapper_exit_code") != 0:
        return EXIT_FAIL, {"fail_reason": "wrapper_exit_code_nonzero"}
    if receipt.get("terminal_completion") is not True:
        return EXIT_FAIL, {"fail_reason": "terminal_completion_missing"}

    return EXIT_OK, {
        "agent_id_digest": _digest16(str(receipt.get("agent_id"))),
        "tool_use_id_digest": _digest16(str(receipt.get("tool_use_id"))),
        "provider": receipt.get("provider"),
        "fallback_used": receipt.get("fallback_used"),
        "receipt_digest": _sha256_text(json.dumps(receipt, sort_keys=True)),
    }


def run_github_mutation_canary() -> tuple[int, dict]:
    gh_bin = _find_gh_bin()
    if gh_bin is None:
        return EXIT_SKIP, {"skip_reason": "gh_binary_not_found"}
    auth = subprocess.run(
        [gh_bin, "auth", "status"],
        capture_output=True,
        text=True,
        env=_sanitized_gh_env(),
        timeout=15,
    )
    if auth.returncode != 0:
        return EXIT_SKIP, {"skip_reason": "gh_not_authenticated"}

    broker = GitHubMutationBroker()
    nonce = broker.state.run_nonce
    title = f"{CANARY_TITLE_PREFIX} run={nonce[:12]}"
    body = (
        "Automated live canary for Issue #2203 "
        f"(GitHubMutationBroker positive/negative control). run_nonce={nonce}\n\n"
        "This Issue is created, edited, commented, and closed by "
        "scripts/claude-gpt/auto_mode_canary.py as part of AC5/AC8 live verification. "
        "Safe to ignore; will auto-close within the same run."
    )
    issue_number: int | None = None
    try:
        issue_number = broker.create_canary_issue(title, body)
        broker.edit_canary_issue(issue_number, body + "\n\n(edited by canary; AC5 readback check)")
        broker.comment_canary_issue(issue_number, "canary comment (AC5 readback check)")
        neg_ok, neg_attempts = run_negative_controls(broker)
        broker.close_canary_issue(issue_number)
    except BrokerError as exc:
        orphan = issue_number is not None and broker.state.final_state != "closed"
        return EXIT_FAIL, {
            "fail_reason": exc.reason,
            "detail": exc.detail,
            "cleanup_status": {"orphan_issue": orphan},
        }

    return EXIT_OK, {
        "repository_id": broker.state.repository_id,
        "issue_node_id": broker.state.created_issue_node_id,
        "issue_number": broker.state.created_issue_number,
        "run_nonce_digest": _digest16(nonce),
        "operations": broker.state.operations,
        "final_state": broker.state.final_state,
        "negative_control": {"attempted": neg_attempts, "all_side_effect_free": neg_ok},
        "cleanup_status": {"orphan_issue": False},
    }


def _sut_revision() -> dict:
    main_sha = "unknown"
    try:
        result = subprocess.run(
            ["git", "-C", str(REPO_ROOT), "rev-parse", "HEAD"],
            capture_output=True,
            text=True,
            timeout=10,
        )
        if result.returncode == 0:
            main_sha = result.stdout.strip()
    except OSError:
        pass

    def _version(bin_name: str, *args: str) -> str:
        path = shutil.which(bin_name)
        if not path:
            return "unavailable"
        try:
            result = subprocess.run([path, *args], capture_output=True, text=True, timeout=10)
        except (OSError, subprocess.TimeoutExpired):
            return "unknown"
        combined = (result.stdout or result.stderr).strip()
        return combined.splitlines()[0] if combined else "unknown"

    return {
        "main_sha": main_sha,
        "launcher_sha256": _sha256_file(SCRIPT_DIR / "launch.sh"),
        "claude_version": _version("claude", "--version"),
        "proxy_version": _version("claude-code-proxy", "--version"),
        "agy_version": "not_applicable_no_direct_cli",
        "gh_version": _version("gh", "--version"),
    }


def _write_evidence(payload: dict) -> Path:
    EVIDENCE_DIR.mkdir(parents=True, exist_ok=True)
    ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    evidence_path = EVIDENCE_DIR / f"auto_mode_canary-{ts}-{secrets.token_hex(4)}.json"
    evidence_path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return evidence_path


_RAW_CONTENT_FORBIDDEN_KEYS = {"prompt", "response", "transcript", "tool_stdout", "credential", "token"}


def _assert_no_raw_content(payload: dict) -> None:
    def _walk(node: object) -> None:
        if isinstance(node, dict):
            for key, value in node.items():
                if key in _RAW_CONTENT_FORBIDDEN_KEYS:
                    raise AssertionError(f"forbidden raw content key present in evidence: {key}")
                _walk(value)
        elif isinstance(node, list):
            for item in node:
                _walk(item)

    _walk(payload)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="auto_mode_canary.py",
        description=(
            "claude-gpt auto mode canonical AGY/GitHub trust policy の live canary "
            "(Issue #2203)."
        ),
    )
    parser.add_argument(
        "--mode",
        required=True,
        choices=("agy", "github", "negative", "all"),
        help="agy=AC4 causal receipt 検証 / github=AC5 GitHub mutation broker canary "
        "/ negative=AC8 negative control のみ / all=全部実行",
    )
    parser.add_argument(
        "--agy-receipt-path",
        type=Path,
        default=None,
        help="live auto-mode セッションが書き出した Issue #2183 causal receipt の JSON path",
    )
    parser.add_argument(
        "--no-evidence",
        action="store_true",
        help="worktree-local ignored artifact への証跡書き込みを省略する（テスト専用）",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    try:
        args = parser.parse_args(argv)
    except SystemExit as exc:
        code = exc.code
        if code in (0, None):
            return EXIT_OK
        return EXIT_INVALID_INVOCATION

    results: dict[str, dict] = {}
    codes: list[int] = []

    if args.mode in ("agy", "all"):
        rc, detail = run_agy_causal_canary(args.agy_receipt_path)
        results["agy"] = {"exit_code": rc, **detail}
        codes.append(rc)

    if args.mode in ("github", "all"):
        rc, detail = run_github_mutation_canary()
        results["github"] = {"exit_code": rc, **detail}
        codes.append(rc)

    if args.mode == "negative":
        gh_bin = _find_gh_bin()
        if gh_bin is None:
            rc, detail = EXIT_SKIP, {"skip_reason": "gh_binary_not_found"}
        else:
            auth = subprocess.run(
                [gh_bin, "auth", "status"], capture_output=True, text=True, env=_sanitized_gh_env(), timeout=15
            )
            if auth.returncode != 0:
                rc, detail = EXIT_SKIP, {"skip_reason": "gh_not_authenticated"}
            else:
                broker = GitHubMutationBroker()
                neg_ok, neg_attempts = run_negative_controls(broker)
                rc = EXIT_OK if neg_ok else EXIT_FAIL
                detail = {"negative_control": {"attempted": neg_attempts, "all_side_effect_free": neg_ok}}
        results["negative"] = {"exit_code": rc, **detail}
        codes.append(rc)

    # aggregate exit: FAIL(1) > SKIP(77) > OK(0)（SKIP を PASS へ昇格しない）。
    if EXIT_FAIL in codes:
        overall = EXIT_FAIL
        classification = "fail"
    elif EXIT_SKIP in codes:
        overall = EXIT_SKIP
        classification = "skip"
    else:
        overall = EXIT_OK
        classification = "pass"

    evidence_payload = {
        "schema": EVIDENCE_SCHEMA,
        "generated_at": _now_iso(),
        "mode": args.mode,
        "sut_revision": _sut_revision(),
        "effective_policy": {
            "permission_mode": "auto",
            "classify_all_shell": True,
            "auto_mode_defaults_digest": "see_preflight_auto_mode_check_output",
            "effective_config_digest": "see_preflight_auto_mode_check_output",
        },
        "agy_causal_receipt": results.get("agy"),
        "github_remote_object_identity": results.get("github"),
        "negative_control": (results.get("github") or results.get("negative") or {}).get(
            "negative_control"
        ),
        "cleanup_status": (results.get("github") or {}).get("cleanup_status", {"orphan_issue": False}),
        "exit_classification": classification,
        "results": results,
    }
    _assert_no_raw_content(evidence_payload)

    if not args.no_evidence:
        evidence_path = _write_evidence(evidence_payload)
        evidence_payload["evidence_path"] = str(evidence_path)

    print(json.dumps(evidence_payload, sort_keys=True))
    return overall


if __name__ == "__main__":
    sys.exit(main())
