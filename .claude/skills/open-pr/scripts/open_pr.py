#!/usr/bin/env python3
"""open_pr.py — open-pr skill の Python wrapper.

LOOP_PROTOCOL の PR 起票を決定論的に行う。skill (SKILL.md) の手順を実装する:
- publish ゲート (人間承認)
- Linked Issue 状態確認 + Closes / Refs 自動 downgrade
- changed paths の決定論的解決
- final PR body の validator 実行 (fail-closed)
- Idempotency チェック (既存 PR 検出)
- gh pr create 実行
- KEY=VALUE stdout contract
"""
from __future__ import annotations

import argparse
import hashlib
import json
import subprocess
import sys
import tempfile
from pathlib import Path
import re
from datetime import date

import yaml

_ISSUE_CONTRACT_REVIEW_SCRIPTS_DIR = (
    Path(__file__).resolve().parent.parent.parent / "issue-contract-review" / "scripts"
)
if str(_ISSUE_CONTRACT_REVIEW_SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(_ISSUE_CONTRACT_REVIEW_SCRIPTS_DIR))

import contract_review_result_parser as contract_review_parser  # noqa: E402

E_APPROVAL_MISSING = "E_APPROVAL_MISSING"
E_PR_BODY_VALIDATION_FAILED = "E_PR_BODY_VALIDATION_FAILED"
E_LINKED_ISSUE_STATE_UNKNOWN = "E_LINKED_ISSUE_STATE_UNKNOWN"
E_GH_FAILURE = "E_GH_FAILURE"
E_SCHEMA_CONSUMER_INVENTORY_MISSING = "E_SCHEMA_CONSUMER_INVENTORY_MISSING"
E_PR_BODY_JAPANESE_VALIDATION_FAILED = "E_PR_BODY_JAPANESE_VALIDATION_FAILED"

# --- Overlap preflight hard gate (Issue #1458) ---
E_OVERLAP_PREFLIGHT_EVIDENCE_MISSING = "E_OVERLAP_PREFLIGHT_EVIDENCE_MISSING"
E_OVERLAP_PREFLIGHT_EVIDENCE_INVALID = "E_OVERLAP_PREFLIGHT_EVIDENCE_INVALID"
E_OVERLAP_PREFLIGHT_DRIFT = "E_OVERLAP_PREFLIGHT_DRIFT"
E_OVERLAP_PREFLIGHT_UNSAFE_ROUTE = "E_OVERLAP_PREFLIGHT_UNSAFE_ROUTE"
E_OVERLAP_PREFLIGHT_SOURCE_FAILURE = "E_OVERLAP_PREFLIGHT_SOURCE_FAILURE"

# `check_implementation_overlap.py`（implement-issue 専用の overlap preflight
# adapter）の evidence schema。本ファイルは producer を変更せず subprocess として
# 再実行するのみ（#1458 の Out of Scope）。
OVERLAP_PREFLIGHT_SCHEMA = "IMPLEMENT_SCOPE_COLLISION_PREFLIGHT_V1"
OVERLAP_PREFLIGHT_SAFE_ROUTES = frozenset({"proceed", "proceed_with_collision_evidence"})

# #1477 の contract にだけ許容された、期限付きで固定された readback waiver。
# 任意の Issue / 理由を consumer が受け入れる一般機構にはしない。
OVERLAP_READBACK_WAIVER_ISSUE_NUMBERS = frozenset({519, 520, 1429})
OVERLAP_READBACK_WAIVER_REASON = "human_approved_readback_ignore"
OVERLAP_READBACK_WAIVER_APPROVED_BY = "user_session"
OVERLAP_READBACK_WAIVER_REPOSITORY = "squne121/loop-protocol"
OVERLAP_READBACK_WAIVER_LINKED_ISSUE = 1477

# linked issue がこのラベルを持つ場合、`overlap_preflight` が未指定または
# `required: false` でも gate を省略しない（AC2, bypass-via-omission 対策）。
FORCE_OVERLAP_PREFLIGHT_LABEL = "phase/implementation"

# `.claude/skills/implement-issue/scripts/check_implementation_overlap.py`
# （open-pr の Allowed Paths 外、変更しない。subprocess として再実行するのみ）。
_CHECK_IMPLEMENTATION_OVERLAP_SCRIPT = (
    Path(__file__).resolve().parent.parent.parent
    / "implement-issue" / "scripts" / "check_implementation_overlap.py"
)

# P1-1 (PR #1467 review fix): 旧実装は get_linked_issue_state() が処理前半で
# 一度だけ取得した labels をキャッシュし、`gh pr create` 直前の
# forced_by_label 判定でそのキャッシュを再利用していた。これは
# 「起動時点で label なし → gate 起動判定は stale cache を読む → 処理中に
# 別プロセスが phase/implementation を付与 → gate が起動しないまま
# gh pr create が呼ばれる」という TOCTOU を許した。さらに labels 取得不能
# （None）を「forced ではない」として fail-open していた。
#
# 修正: gate 起動要否 (forced_by_label) の判定はこのキャッシュを一切使わず、
# `gh pr create` 直前で毎回オンライン再取得する（fetch_current_linked_issue_labels）。
# 取得失敗・型不正・JSON不正はすべて「ラベルなし」ではなく fail-closed
# （gate を必ず有効化する）として扱う。


def _classify_validator_errors(errors: list[object]) -> str:
    """Classify validator errors list into an error code.

    Returns E_SCHEMA_CONSUMER_INVENTORY_MISSING if any error is LP050, or
    if any LP052 error references the Schema Consumer Inventory section.
    Returns E_PR_BODY_VALIDATION_FAILED for all other failures.
    """
    for error in errors:
        if not isinstance(error, dict):
            continue
        rule_id = error.get("rule_id", "")
        if rule_id == "LP050":
            return E_SCHEMA_CONSUMER_INVENTORY_MISSING
        if rule_id == "LP052":
            message = error.get("message", "")
            if message.strip() == "Missing required section: Schema Consumer Inventory":
                return E_SCHEMA_CONSUMER_INVENTORY_MISSING
    return E_PR_BODY_VALIDATION_FAILED


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Open PR (LOOP_PROTOCOL open-pr skill wrapper)")
    p.add_argument("--pr-title", required=True)
    p.add_argument("--linked-issue", required=True, type=int)
    p.add_argument("--publish", required=True, help="`yes` で人間承認確認")
    p.add_argument("--pr-body-file", required=True, type=Path)
    p.add_argument("--draft", default="true", help="`true` (default) で Draft PR")
    p.add_argument("--branch", help="head branch 名 (省略時は現在の HEAD)")
    p.add_argument("--repo", help="owner/repo (省略時は git remote から取得)")
    p.add_argument("--dry-run", action="store_true", help="gh pr create を実行しない")
    p.add_argument(
        "--changed-paths",
        nargs="*",
        default=None,
        help="変更ファイルパスのリスト。未指定時は git diff から決定論的に解決する。",
    )
    p.add_argument(
        "--overlap-preflight-required",
        action="store_true",
        help=(
            "overlap_preflight evidence 検証を必須化する（phase/implementation "
            "ラベル付き linked issue は未指定でも自動的に必須化される。AC2）"
        ),
    )
    p.add_argument(
        "--overlap-preflight-evidence-file",
        type=Path,
        default=None,
        help="check_implementation_overlap.py が出力した evidence JSON のパス",
    )
    p.add_argument(
        "--overlap-preflight-expected-evidence-sha256",
        default=None,
        help="stored evidence file の embedded evidence_sha256 と照合する期待値（sha256:...）",
    )
    p.add_argument(
        "--overlap-preflight-expected-decision-inputs-sha256",
        default=None,
        help="オンライン再実行の fresh decision_inputs_sha256 と照合する期待値（sha256:...）",
    )
    return p.parse_args(argv)


def emit_kv(key: str, value: object) -> None:
    s = str(value).replace("\n", "\\n").replace("\r", "\\r")
    print(f"{key}={s}")


def emit_error(code: str, detail: str = "") -> None:
    emit_kv("ERROR", code)
    if detail:
        emit_kv("ERROR_DETAIL", detail)


def run_gh(*args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    cmd = ["gh", *args]
    return subprocess.run(cmd, capture_output=True, text=True, check=check, timeout=60)


def resolve_repo() -> str:
    try:
        result = subprocess.run(
            ["git", "remote", "get-url", "origin"],
            capture_output=True,
            text=True,
            check=True,
            timeout=10,
        )
    except subprocess.SubprocessError:
        return ""
    url = result.stdout.strip()
    match = re.search(r"github\.com[:/]([\w.-]+/[\w.-]+?)(?:\.git)?$", url)
    return match.group(1) if match else ""


def resolve_branch() -> str:
    try:
        result = subprocess.run(
            ["git", "rev-parse", "--abbrev-ref", "HEAD"],
            capture_output=True,
            text=True,
            check=True,
            timeout=10,
        )
    except subprocess.SubprocessError:
        return ""
    return result.stdout.strip()


def _canonicalize_repo_static(repo: object) -> str | None:
    """`owner/name` を小文字化した canonical 形へ静的に正規化する（Issue #1470）。

    `.claude/skills/implement-issue/scripts/check_implementation_overlap.py`
    の `_canonicalize_repo_static`（producer 側、Allowed Paths 外・変更しない）
    と同じ正規化規則を consumer 側で独立に持つ小さな pure function。producer
    が raise する代わりに、こちらは fail-closed で `None` を返す（呼び出し側が
    分岐しやすいように）。owner/name 形式でない、またはいずれかの segment が
    空の場合に `None` を返す。
    """
    if not isinstance(repo, str):
        return None
    raw = repo.strip()
    if "/" not in raw:
        return None
    owner, _, name = raw.partition("/")
    owner = owner.strip()
    name = name.strip()
    if not owner or not name or "/" in name:
        return None
    return f"{owner.lower()}/{name.lower()}"


def resolve_canonical_repository(requested_repo: str) -> str | None:
    """`requested_repo` を GitHub Repository API の canonical `full_name` の
    小文字化形へ一度だけ解決する（Issue #1470）。

    rename / transfer 後の alias もこの単一の API 呼び出しで現在の
    `full_name` へ解決される。producer 側 `_canonicalize_repo(..., online=True)`
    と異なり、consumer 側はオンライン解決に失敗した場合に静的正規化への
    fallback を **行わない**（fresh evidence / gh pr create --repo に使う
    PR mutation target の identity を、GitHub の現在の応答一本に束縛する
    ため）。失敗時は `None` を返し、呼び出し元は停止する。
    """
    static = _canonicalize_repo_static(requested_repo)
    if static is None:
        return None
    try:
        result = run_gh(
            "api",
            f"repos/{static}",
            "-H",
            "Accept: application/vnd.github+json",
            "-H",
            "X-GitHub-Api-Version: 2022-11-28",
        )
    except (subprocess.SubprocessError, OSError):
        return None
    try:
        data = json.loads(result.stdout)
    except json.JSONDecodeError:
        return None
    if not isinstance(data, dict):
        return None
    return _canonicalize_repo_static(data.get("full_name"))


def get_linked_issue_state(repo: str, issue_number: int) -> str | None:
    try:
        result = run_gh(
            "issue", "view", str(issue_number), "--repo", repo, "--json", "state"
        )
        data = json.loads(result.stdout)
    except (subprocess.SubprocessError, json.JSONDecodeError):
        return None
    if not isinstance(data, dict):
        return None
    return data.get("state")


def fetch_current_linked_issue_labels(
    repo: str, issue_number: int
) -> tuple[list[str] | None, str | None]:
    """gate 起動要否 (forced_by_label) の判定 **直前** に labels をオンライン
    再取得する（P1-1: TOCTOU 対策。PR #1467 review fix）。

    `get_linked_issue_state()` の初回取得結果（または他のどのキャッシュ）も
    この security decision には使わない。呼び出しごとに fresh に取得する。

    Returns (label_names, error_detail)。取得に成功した場合は
    `(list[str], None)`（label が無ければ空 list）。認証失敗・JSON不正・
    型不正など取得に失敗した場合は `(None, <detail>)` を返す。呼び出し側は
    これを「ラベルなし」ではなく fail-closed（gate を強制的に有効化する）
    として扱わなければならない。
    """
    try:
        result = run_gh("issue", "view", str(issue_number), "--repo", repo, "--json", "labels")
    except subprocess.SubprocessError as exc:
        return None, f"gh issue view 失敗: {exc}"
    try:
        data = json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        return None, f"gh issue view の JSON parse に失敗: {exc}"
    if not isinstance(data, dict):
        return None, "gh issue view の出力が object ではありません"
    labels = data.get("labels")
    if not isinstance(labels, list):
        return None, "labels フィールドが list ではありません"
    label_names = [
        (lbl.get("name") if isinstance(lbl, dict) else str(lbl)) for lbl in labels
    ]
    return label_names, None


def find_existing_pr(repo: str, branch: str) -> dict | None:
    try:
        result = run_gh(
            "pr",
            "list",
            "--repo",
            repo,
            "--head",
            branch,
            "--state",
            "open",
            "--json",
            "number,url",
        )
        items = json.loads(result.stdout)
    except (subprocess.SubprocessError, json.JSONDecodeError):
        return None
    return items[0] if items else None


def apply_linked_issue_reference(body: str, issue_number: int, link_kind: str) -> str:
    pattern = re.compile(rf"(Closes|Refs|Fixes|Resolves)\s+#{issue_number}\b", re.IGNORECASE)
    if pattern.search(body):
        return pattern.sub(f"{link_kind} #{issue_number}", body, count=1)
    sep = "\n\n" if not body.endswith("\n") else "\n"
    return body + sep + f"{link_kind} #{issue_number}\n"


def resolve_changed_paths(provided_paths: list[str] | None = None) -> list[str] | None:
    if provided_paths is not None:
        return [path for path in provided_paths if path]

    try:
        merge_base = subprocess.run(
            ["git", "merge-base", "main", "HEAD"],
            capture_output=True,
            text=True,
            check=True,
            timeout=10,
        ).stdout.strip()
        if not merge_base:
            return None
        diff = subprocess.run(
            ["git", "diff", "--name-only", f"{merge_base}...HEAD"],
            capture_output=True,
            text=True,
            check=True,
            timeout=20,
        )
    except subprocess.SubprocessError:
        return None

    return [line.strip() for line in diff.stdout.splitlines() if line.strip()]


def _run_pr_body_validator(
    body_text: str,
    changed_paths: list[str] | None,
    linked_issue: int,
) -> dict[str, object]:
    validator_script = (
        Path(__file__).resolve().parent / "validate_pr_body.py"
    )

    body_file = tempfile.NamedTemporaryFile(
        mode="w",
        suffix=".md",
        encoding="utf-8",
        delete=False,
    )
    changed_paths_file = None
    try:
        body_file.write(body_text)
        body_file.flush()
        body_file.close()

        cmd = [
            sys.executable,
            str(validator_script),
            "--body-file",
            body_file.name,
            "--linked-issue",
            str(linked_issue),
        ]

        if changed_paths is not None:
            changed_paths_file = tempfile.NamedTemporaryFile(
                mode="w",
                suffix=".txt",
                encoding="utf-8",
                delete=False,
            )
            changed_paths_file.write("\n".join(changed_paths))
            changed_paths_file.flush()
            changed_paths_file.close()
            cmd.extend(["--changed-paths-file", changed_paths_file.name])

        try:
            cp = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                check=False,
                timeout=60,
            )
        except subprocess.TimeoutExpired as exc:
            return {
                "status": "internal",
                "errors": [],
                "message": "Validator timeout",
                "stderr": (exc.stderr or "").strip() if exc.stderr else "Timeout expired",
            }
        except OSError as exc:
            return {
                "status": "internal",
                "errors": [],
                "message": "Validator spawn error",
                "stderr": str(exc),
            }

        if cp.returncode not in {0, 1}:
            return {
                "status": "internal",
                "errors": [],
                "message": f"Validator error (exit code {cp.returncode})",
                "stderr": (cp.stderr or "").strip(),
            }

        try:
            payload = json.loads(cp.stdout)
        except json.JSONDecodeError:
            return {
                "status": "internal",
                "errors": [],
                "message": "Validator returned non-JSON output",
                "stderr": (cp.stdout or "").strip(),
            }

        # B3: Verify JSON schema integrity
        if payload.get("schema") != "loop_body_lint/v1":
            return {
                "status": "internal",
                "errors": [],
                "message": f"Validator schema mismatch: {payload.get('schema')}",
                "stderr": "",
            }
        if payload.get("target") != "pr":
            return {
                "status": "internal",
                "errors": [],
                "message": f"Validator target mismatch: {payload.get('target')}",
                "stderr": "",
            }
        if payload.get("status") not in {"pass", "fail"}:
            return {
                "status": "internal",
                "errors": [],
                "message": f"Validator status invalid: {payload.get('status')}",
                "stderr": "",
            }
        if not isinstance(payload.get("errors"), list):
            return {
                "status": "internal",
                "errors": [],
                "message": "Validator errors field is not a list",
                "stderr": "",
            }

        # B3: Verify body_sha256
        expected_sha256 = f"sha256:{hashlib.sha256(body_text.encode('utf-8')).hexdigest()}"
        if payload.get("body_sha256") != expected_sha256:
            return {
                "status": "internal",
                "errors": [],
                "message": "Validator body_sha256 mismatch",
                "stderr": f"expected {expected_sha256}, got {payload.get('body_sha256')}",
            }

        return payload
    finally:
        Path(body_file.name).unlink(missing_ok=True)
        if changed_paths_file is not None:
            Path(changed_paths_file.name).unlink(missing_ok=True)



def _run_japanese_content_validator(
    body_text: str,
    threshold: float = 0.1,
) -> dict[str, object]:
    """Run validate_japanese_content.py against body_text.

    Returns dict with keys:
      - status: "pass" | "fail" | "internal"
      - failed_blocks: int
      - aggregate_ratio: float
      - threshold: float
      - body_sha256: str
      - stderr: str (on fail/internal)
    """
    validator_script = (
        Path(__file__).resolve().parent.parent.parent
        / "create-issue" / "scripts" / "validate_japanese_content.py"
    )

    body_sha256 = f"sha256:{hashlib.sha256(body_text.encode('utf-8')).hexdigest()}"

    body_file = tempfile.NamedTemporaryFile(
        mode="w",
        suffix=".md",
        encoding="utf-8",
        delete=False,
    )
    try:
        body_file.write(body_text)
        body_file.flush()
        body_file.close()

        cmd = [
            sys.executable,
            str(validator_script),
            "--file",
            body_file.name,
            "--threshold",
            str(threshold),
            "--verbose",
        ]

        try:
            cp = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                check=False,
                timeout=60,
            )
        except subprocess.TimeoutExpired:
            return {
                "status": "internal",
                "failed_blocks": 0,
                "aggregate_ratio": 0.0,
                "threshold": threshold,
                "body_sha256": body_sha256,
                "stderr": "Timeout expired",
            }
        except OSError as exc:
            return {
                "status": "internal",
                "failed_blocks": 0,
                "aggregate_ratio": 0.0,
                "threshold": threshold,
                "body_sha256": body_sha256,
                "stderr": str(exc),
            }

        stderr_text = (cp.stderr or "").strip()

        if cp.returncode == 0:
            # Parse aggregate_ratio from stderr (verbose mode)
            ratio = 0.0
            for line in stderr_text.splitlines():
                if line.startswith("aggregate_ratio:"):
                    try:
                        ratio = float(line.split(":", 1)[1].strip())
                    except (ValueError, IndexError):
                        pass
            return {
                "status": "pass",
                "failed_blocks": 0,
                "aggregate_ratio": ratio,
                "threshold": threshold,
                "body_sha256": body_sha256,
                "stderr": stderr_text,
            }
        elif cp.returncode == 1:
            # Parse aggregate_ratio and failed_blocks from stderr (verbose mode)
            ratio = 0.0
            failed_blocks = 0
            for line in stderr_text.splitlines():
                if line.startswith("aggregate_ratio:"):
                    try:
                        ratio = float(line.split(":", 1)[1].strip())
                    except (ValueError, IndexError):
                        pass
                elif line.startswith("failed_blocks:"):
                    try:
                        failed_blocks = int(line.split(":", 1)[1].strip())
                    except (ValueError, IndexError):
                        pass
            return {
                "status": "fail",
                "failed_blocks": failed_blocks,
                "aggregate_ratio": ratio,
                "threshold": threshold,
                "body_sha256": body_sha256,
                "stderr": stderr_text,
            }
        else:
            return {
                "status": "internal",
                "failed_blocks": 0,
                "aggregate_ratio": 0.0,
                "threshold": threshold,
                "body_sha256": body_sha256,
                "stderr": stderr_text,
            }
    finally:
        Path(body_file.name).unlink(missing_ok=True)

def _overlap_preflight_evidence_sha256(payload: dict) -> str:
    """producer（`check_implementation_overlap.py`）と同一の canonicalization
    契約（`json.dumps(payload, sort_keys=True, ensure_ascii=True,
    separators=(",", ":"))` を経て sha256 hex 化し `sha256:` を前置）で
    embedded `evidence_sha256` を再計算する。"""
    body = {k: v for k, v in payload.items() if k != "evidence_sha256"}
    canonical = json.dumps(body, sort_keys=True, ensure_ascii=True, separators=(",", ":"))
    return f"sha256:{hashlib.sha256(canonical.encode('utf-8')).hexdigest()}"


def _load_overlap_preflight_evidence(
    evidence_file: Path | None,
) -> tuple[dict | None, str | None]:
    """stored evidence file を読み込み検証する。

    Returns (stored_evidence, error_code)。error_code は成功時 None。
    ファイル欠落は E_OVERLAP_PREFLIGHT_EVIDENCE_MISSING、parse 失敗・
    スキーマ不一致は E_OVERLAP_PREFLIGHT_EVIDENCE_INVALID（AC5/AC7）。
    """
    if evidence_file is None or not evidence_file.exists():
        return None, E_OVERLAP_PREFLIGHT_EVIDENCE_MISSING
    try:
        raw = evidence_file.read_text(encoding="utf-8")
        stored = json.loads(raw)
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return None, E_OVERLAP_PREFLIGHT_EVIDENCE_INVALID
    if not isinstance(stored, dict) or stored.get("schema") != OVERLAP_PREFLIGHT_SCHEMA:
        return None, E_OVERLAP_PREFLIGHT_EVIDENCE_INVALID
    if "evidence_sha256" not in stored or "decision_inputs_sha256" not in stored:
        return None, E_OVERLAP_PREFLIGHT_EVIDENCE_INVALID
    # Issue #1470: repository は required field。欠落・型不正・legacy V1
    # evidence（repository field 自体を持たない旧 schema、正しい legacy hash
    # を持っていても）はここで一律拒否する。
    repository = stored.get("repository")
    if not isinstance(repository, str) or not repository:
        return None, E_OVERLAP_PREFLIGHT_EVIDENCE_INVALID
    return stored, None


def _positive_overlap_source_limit(evidence: dict) -> int | None:
    """evidence の ``source.limit`` を再検証用の正の整数として読む。"""
    source = evidence.get("source")
    if not isinstance(source, dict):
        return None
    limit = source.get("limit")
    if isinstance(limit, bool) or not isinstance(limit, int) or limit <= 0:
        return None
    return limit


# #1493 AC3: producer（check_implementation_overlap.py）が全件性を証明する
# ために additive で積む collection contract の必須 field。stored evidence
# にこれらが欠けている場合は legacy（cursor pagination 以前）の evidence で
# あり、全件性を証明できないため再収集を要求する（fail-closed、caller
# override は許可しない）。
_OVERLAP_COLLECTION_CONTRACT_KEYS = (
    "collection_mode",
    "page_size",
    "page_count",
    "fetched_count",
    "has_next_page",
)


def _overlap_collection_contract_missing_keys(evidence: dict) -> tuple[str, ...]:
    """``source`` に collection contract の必須 field が欠けていればその key を返す。"""
    source = evidence.get("source")
    if not isinstance(source, dict):
        return _OVERLAP_COLLECTION_CONTRACT_KEYS
    return tuple(key for key in _OVERLAP_COLLECTION_CONTRACT_KEYS if key not in source)


# PR #1626 review fix_delta（P2 Blocker）: `_overlap_collection_contract_missing_keys`
# は必須 field の *有無* しか見ておらず、`collection_mode` 以外の field は
# stored/fresh 間でも比較されない。改ざんされた `page_size` / `fetched_count`
# 等の自己矛盾 evidence（例: `fetched_count` が `page_count * page_size` を
# 超過）を素通りさせないため、1 件の evidence 単体で内部整合性を検証する
# strict validator を追加する。stored/fresh 双方に適用する。
_OVERLAP_PAGE_SIZE_UPPER_BOUND = 100


def _overlap_collection_contract_shape_error(source: dict) -> str | None:
    """collection contract の内部整合性を検証する。violation があれば理由
    文字列、なければ None を返す（PR #1626 review fix_delta P2 Blocker）。
    """
    page_size = source.get("page_size")
    if (
        isinstance(page_size, bool)
        or not isinstance(page_size, int)
        or not (1 <= page_size <= _OVERLAP_PAGE_SIZE_UPPER_BOUND)
    ):
        return f"page_size が 1..{_OVERLAP_PAGE_SIZE_UPPER_BOUND} の範囲の整数ではありません: {page_size!r}"
    page_count = source.get("page_count")
    if isinstance(page_count, bool) or not isinstance(page_count, int) or page_count < 1:
        return f"page_count が正の整数ではありません: {page_count!r}"
    fetched_count = source.get("fetched_count")
    if isinstance(fetched_count, bool) or not isinstance(fetched_count, int) or fetched_count < 0:
        return f"fetched_count が非負整数ではありません: {fetched_count!r}"
    if fetched_count > page_count * page_size:
        return (
            f"fetched_count が page_count*page_size を超過しており候補集合と整合しません: "
            f"fetched_count={fetched_count} page_count={page_count} page_size={page_size}"
        )
    has_next_page = source.get("has_next_page")
    if not isinstance(has_next_page, bool):
        return f"has_next_page が bool ではありません: {has_next_page!r}"
    complete = source.get("complete")
    saturated = source.get("saturated")
    if complete is True and (has_next_page is not False or saturated is not False):
        return (
            f"complete=true なのに has_next_page/saturated が矛盾しています: "
            f"has_next_page={has_next_page!r} saturated={saturated!r}"
        )
    if saturated is True and complete is not False:
        return f"saturated=true なのに complete=false ではありません: complete={complete!r}"
    return None


def _extract_waiver_from_live_contract(text: str) -> dict | None:
    """live Issue body の waiver 設定だけを読む。

    CONTRACT_REVIEW_RESULT_V1 の comment 走査は共有 parser の責務であり、ここで
    独自に解釈しない。"""
    blocks = re.findall(r"```yaml\s*\n(.*?)```", text, flags=re.DOTALL | re.IGNORECASE)
    for block in blocks:
        if "overlap_readback_waiver" not in block:
            continue
        try:
            payload = yaml.safe_load(block)
        except yaml.YAMLError:
            continue
        waiver = payload.get("overlap_readback_waiver") if isinstance(payload, dict) else None
        if isinstance(waiver, dict):
            return waiver
    return None


def _load_verified_overlap_readback_waiver(
    repo: str,
    linked_issue: int,
    *,
    today: date | None = None,
) -> tuple[dict | None, str | None]:
    """live body と最新の trusted ``status: go`` snapshot が同一 SHA の waiver を返す。

    Issue 本文だけ、または古い contract snapshot だけでは waiver を有効化しない。
    戻り値の error detail は gate 側で fail-closed の監査情報に使う。
    """
    if (
        repo.strip().lower() != OVERLAP_READBACK_WAIVER_REPOSITORY
        or linked_issue != OVERLAP_READBACK_WAIVER_LINKED_ISSUE
    ):
        return None, "overlap_readback_waiver の対象 repository / linked issue が固定 binding と一致しません"

    try:
        result = run_gh(
            "issue",
            "view",
            str(linked_issue),
            "--repo",
            repo,
            "--json",
            "body,url",
        )
        issue = json.loads(result.stdout)
    except (subprocess.SubprocessError, json.JSONDecodeError) as exc:
        return None, f"contract snapshot の取得に失敗: {exc}"
    if not isinstance(issue, dict):
        return None, "contract snapshot の Issue payload が object ではありません"
    body = issue.get("body")
    url = issue.get("url")
    expected_url = (
        f"https://github.com/{OVERLAP_READBACK_WAIVER_REPOSITORY}/issues/"
        f"{OVERLAP_READBACK_WAIVER_LINKED_ISSUE}"
    )
    if not isinstance(body, str) or url != expected_url:
        return None, "contract snapshot の Issue payload が固定 waiver target と一致しません"

    body_sha256 = f"sha256:{hashlib.sha256(body.encode('utf-8')).hexdigest()}"
    waiver = _extract_waiver_from_live_contract(body)
    if not isinstance(waiver, dict):
        return None, "live contract に overlap_readback_waiver がありません"

    # 決定（Issue #1518）: expires_on は延長しない。この waiver は
    # OVERLAP_READBACK_WAIVER_LINKED_ISSUE（Issue #1477、既に CLOSED）専用
    # binding であり、#1477 が再度 linked_issue になることはないため、期限切れ
    # 後もこの分岐に実際に到達することはない（上の repo/linked_issue 一致
    # チェックが先に落ちる）。#519 / #520 / #1429 は本 Issue 時点でまだ OPEN
    # だが、waiver 対象範囲・トリガー条件の変更は本 Issue の Stop Condition
    # に該当するため、汎用化・再設計は行わない（別 Issue #1509 の scope）。
    expected_waiver = {
        "issue_numbers": [519, 520, 1429],
        "reason": OVERLAP_READBACK_WAIVER_REASON,
        "expires_on": "2026-07-13",
        "approved_by": OVERLAP_READBACK_WAIVER_APPROVED_BY,
    }
    if waiver != expected_waiver:
        return None, "overlap_readback_waiver が固定 contract と完全一致しません"
    if (today or date.today()) > date.fromisoformat(expected_waiver["expires_on"]):
        return None, "overlap_readback_waiver の期限が切れています"

    comments, comments_error = contract_review_parser.fetch_issue_comments(linked_issue, repo)
    if comments_error is not None:
        return None, f"contract snapshot comment readback が不完全です: {comments_error}"
    latest = contract_review_parser.find_latest_result(
        contract_review_parser.parse_contract_review_results(
            comments, expected_issue_url=expected_url
        ),
        trusted_only=True,
    )
    if latest is None:
        return None, "trusted contract snapshot がありません"
    if latest.get("status") != "go":
        return None, "最新 trusted contract snapshot が status: go ではありません"
    inner = latest.get("inner")
    if not isinstance(inner, dict) or inner.get("body_sha256") != body_sha256:
        return None, "最新 trusted status: go contract snapshot の body SHA が live body と一致しません"
    return waiver, None


def _has_only_fixed_readback_incomplete_blockers(fresh: dict) -> bool:
    """固定3件の readback_incomplete だけが route を妨げる場合に限り True。"""
    if fresh.get("route") != "human_review_required":
        return False
    candidates = fresh.get("candidates")
    if not isinstance(candidates, list):
        return False
    incomplete = [
        candidate for candidate in candidates
        if isinstance(candidate, dict) and candidate.get("readback_complete") is False
    ]
    if len(incomplete) != len(OVERLAP_READBACK_WAIVER_ISSUE_NUMBERS):
        return False
    numbers = {candidate.get("issue_number") for candidate in incomplete}
    if numbers != OVERLAP_READBACK_WAIVER_ISSUE_NUMBERS:
        return False
    for candidate in incomplete:
        reasons = candidate.get("reasons")
        if (
            not isinstance(reasons, list)
            or not reasons
            or not all(isinstance(reason, str) and reason.startswith("readback_incomplete") for reason in reasons)
        ):
            return False
    # readback が完了した candidate 側に C2b/C3/unknown が残っていれば、
    # human_review_required の原因をこの waiver だけとは証明できない。
    for candidate in candidates:
        if not isinstance(candidate, dict) or candidate.get("readback_complete") is False:
            continue
        if candidate.get("policy_class") not in {"C1", "C2a"}:
            return False
    return True


def _overlap_preflight_safety_reason(fresh: dict, linked_issue: int) -> str | None:
    """AC4 の安全性 predicate を検証する。violation があれば理由文字列、
    なければ None を返す。"""
    if not isinstance(fresh, dict) or fresh.get("schema") != OVERLAP_PREFLIGHT_SCHEMA:
        return "schema_mismatch"
    if fresh.get("route") not in OVERLAP_PREFLIGHT_SAFE_ROUTES:
        return f"unsafe_route:{fresh.get('route')!r}"
    source = fresh.get("source")
    if not isinstance(source, dict) or source.get("complete") is not True:
        return "source_incomplete"
    if not isinstance(source, dict) or source.get("saturated") is not False:
        return "source_saturated"
    if fresh.get("validation_errors") != {}:
        return "validation_errors_present"
    dependency_resolution = fresh.get("dependency_resolution")
    if not isinstance(dependency_resolution, dict) or dependency_resolution.get("unresolved_refs") != []:
        return "unresolved_refs_present"
    if not isinstance(dependency_resolution, dict) or dependency_resolution.get("blocking_predecessor") is not None:
        return "blocking_predecessor_present"
    current_issue = fresh.get("current_issue")
    if not isinstance(current_issue, dict) or current_issue.get("number") != linked_issue:
        return "current_issue_number_mismatch"
    return None


def run_overlap_preflight_gate(
    *,
    repo: str,
    linked_issue: int,
    evidence_file: Path | None,
    expected_evidence_sha256: str | None,
    expected_decision_inputs_sha256: str | None,
) -> tuple[bool, str | None, str, dict | None]:
    """`gh pr create` 呼び出し直前の overlap preflight hard gate（Issue #1458）。

    `repo` は本関数呼び出し元（`main()`）が `gh pr create --repo` にもそのまま
    渡す同一変数であり、これが AC8 の cross-repo binding mitigation の根拠
    （オンライン再実行と PR 作成に使う `--repo` の一致は単一のソースオブ
    トゥルースとして構造的に保証される。evidence 自体への `repository`
    フィールド追加は #1462 の scope）。

    Returns (ok, error_code, detail, fresh_evidence)。ok=True の場合
    error_code は None、detail は空文字列。
    """
    stored, load_error = _load_overlap_preflight_evidence(evidence_file)
    if load_error is not None:
        detail = (
            f"evidence_file が存在しないか読み込めません: {evidence_file}"
            if load_error == E_OVERLAP_PREFLIGHT_EVIDENCE_MISSING
            else f"evidence_file の parse/schema が不正です: {evidence_file}"
        )
        return False, load_error, detail, None

    recomputed = _overlap_preflight_evidence_sha256(stored)
    stored_sha = stored.get("evidence_sha256")
    if recomputed != stored_sha or stored_sha != expected_evidence_sha256:
        return (
            False,
            E_OVERLAP_PREFLIGHT_EVIDENCE_INVALID,
            f"evidence_sha256 不一致: stored={stored_sha} recomputed={recomputed} "
            f"expected={expected_evidence_sha256}",
            None,
        )

    # Issue #1470 (AC2/AC7): repository binding は他のどの検証よりも先に確認する。
    # `repo`（呼び出し元 main() が resolve_canonical_repository() で一度だけ解決
    # 済みの canonical full_name）を PR mutation target として、stored evidence の
    # repository が canonical 形かつこの target と一致することを、オンライン
    # 再実行（subprocess）より前に検証する。これにより、同じ Issue 番号を持つ
    # 別リポジトリの evidence を誤って再利用しても gh pr create は一度も呼ばれない。
    target_repo = repo
    stored_repository = stored.get("repository")
    if (
        stored_repository != _canonicalize_repo_static(stored_repository)
        or stored_repository != target_repo
    ):
        return (
            False,
            E_OVERLAP_PREFLIGHT_EVIDENCE_INVALID,
            f"stored repository が canonical target と一致しません: "
            f"stored={stored_repository!r} target={target_repo!r}",
            None,
        )

    # P2-1 (PR #1467 review fix): stored evidence の decision_inputs_sha256 を
    # 呼び出し元が指定した expected_decision_inputs_sha256 と接続する
    # provenance チェックを、オンライン再実行の **前** に行う。これを省くと
    # 「evidence_sha256 は正しいが decision_inputs_sha256 が別の preflight
    # collection chain のものである stored artifact」と「expected_decision_
    # inputs_sha256 = fresh 側の値」という組み合わせが、stored/fresh の
    # 独立検証だけでは検出できず通過してしまう（stored の provenance が
    # fresh のそれと結び付けられていない）。
    stored_decision_inputs_sha256 = stored.get("decision_inputs_sha256")
    if stored_decision_inputs_sha256 != expected_decision_inputs_sha256:
        return (
            False,
            E_OVERLAP_PREFLIGHT_EVIDENCE_INVALID,
            f"decision_inputs_sha256 不一致 (stored vs expected): "
            f"stored={stored_decision_inputs_sha256} "
            f"expected={expected_decision_inputs_sha256}",
            None,
        )

    # stored evidence の embedded hash と decision-input provenance を確認した
    # 後にだけ、収集境界を固定する candidate limit を利用する。呼び出し元が
    # 任意値で上書きできないよう、唯一の入力はこの verified evidence とする。
    stored_limit = _positive_overlap_source_limit(stored)
    if stored_limit is None:
        return (
            False,
            E_OVERLAP_PREFLIGHT_EVIDENCE_INVALID,
            "stored source.limit が正の整数ではありません",
            None,
        )

    # #1493 AC3: stored evidence が cursor pagination の collection contract
    # を満たさない legacy evidence の場合、全件性を証明できないため再収集を
    # 要求する（fail-closed）。
    stored_missing_contract_keys = _overlap_collection_contract_missing_keys(stored)
    if stored_missing_contract_keys:
        return (
            False,
            E_OVERLAP_PREFLIGHT_EVIDENCE_INVALID,
            f"stored evidence に collection contract の必須 field がありません "
            f"（再収集が必要です）: {sorted(stored_missing_contract_keys)}",
            None,
        )

    # PR #1626 review fix_delta（P2 Blocker）: 必須 field が揃っていても
    # 改ざんされた自己矛盾 evidence（page_size/fetched_count 等）を通さない。
    stored_source_for_shape = stored.get("source") if isinstance(stored.get("source"), dict) else {}
    stored_shape_error = _overlap_collection_contract_shape_error(stored_source_for_shape)
    if stored_shape_error is not None:
        return (
            False,
            E_OVERLAP_PREFLIGHT_EVIDENCE_INVALID,
            f"stored evidence の collection contract が自己矛盾しています: {stored_shape_error}",
            None,
        )

    cmd = [
        sys.executable,
        str(_CHECK_IMPLEMENTATION_OVERLAP_SCRIPT),
        "--issue-number",
        str(linked_issue),
        "--repo",
        target_repo,
        "--limit",
        str(stored_limit),
    ]
    try:
        cp = subprocess.run(cmd, capture_output=True, text=True, check=False, timeout=90)
    except subprocess.TimeoutExpired as exc:
        return False, E_OVERLAP_PREFLIGHT_SOURCE_FAILURE, f"subprocess timeout: {exc}", None
    except OSError as exc:
        return False, E_OVERLAP_PREFLIGHT_SOURCE_FAILURE, f"subprocess spawn error: {exc}", None

    if cp.returncode != 0:
        return (
            False,
            E_OVERLAP_PREFLIGHT_SOURCE_FAILURE,
            f"check_implementation_overlap.py exit {cp.returncode}: "
            f"{(cp.stderr or cp.stdout or '').strip()[:500]}",
            None,
        )

    try:
        fresh = json.loads(cp.stdout)
    except json.JSONDecodeError:
        return (
            False,
            E_OVERLAP_PREFLIGHT_SOURCE_FAILURE,
            f"non-JSON output: {(cp.stdout or '').strip()[:500]}",
            None,
        )
    if not isinstance(fresh, dict):
        return False, E_OVERLAP_PREFLIGHT_SOURCE_FAILURE, "non-object JSON output", None

    # Issue #1470 (AC3/AC5 ordering): fresh evidence の repository binding は
    # 汎用の decision_inputs_sha256 drift 検査より **前** に確認する。repository
    # 自体が decision hash の入力に含まれるため、repository を書き換えても
    # 偶然 decision_inputs_sha256 が一致してしまう（あるいは caller が誤った
    # expected 値を渡してしまう）可能性を、この明示的な検証で個別に防ぐ。
    fresh_repository = fresh.get("repository")
    if fresh_repository != target_repo:
        return (
            False,
            E_OVERLAP_PREFLIGHT_DRIFT,
            f"fresh repository が canonical target と一致しません: "
            f"fresh={fresh_repository!r} target={target_repo!r}",
            fresh,
        )

    fresh_limit = _positive_overlap_source_limit(fresh)
    if fresh_limit is None:
        return (
            False,
            E_OVERLAP_PREFLIGHT_DRIFT,
            "fresh source.limit が正の整数ではありません",
            fresh,
        )
    if fresh_limit != stored_limit:
        return (
            False,
            E_OVERLAP_PREFLIGHT_DRIFT,
            f"source.limit drift: stored={stored_limit} fresh={fresh_limit}",
            fresh,
        )

    # #1493 AC3: fresh evidence（オンライン再実行）も同じ collection contract
    # を満たすことを検証する。caller は contract / limit を上書きできない
    # （唯一の入力は verified stored evidence の limit を再検証 CLI に渡す
    # ことだけであり、それ自体は上の subprocess 呼び出しで既に行っている）。
    fresh_missing_contract_keys = _overlap_collection_contract_missing_keys(fresh)
    if fresh_missing_contract_keys:
        return (
            False,
            E_OVERLAP_PREFLIGHT_DRIFT,
            f"fresh evidence に collection contract の必須 field がありません: "
            f"{sorted(fresh_missing_contract_keys)}",
            fresh,
        )

    # PR #1626 review fix_delta（P2 Blocker）: fresh evidence（オンライン
    # 再実行の生出力）についても自己矛盾 shape を検証する。
    fresh_source_for_shape = fresh.get("source") if isinstance(fresh.get("source"), dict) else {}
    fresh_shape_error = _overlap_collection_contract_shape_error(fresh_source_for_shape)
    if fresh_shape_error is not None:
        return (
            False,
            E_OVERLAP_PREFLIGHT_DRIFT,
            f"fresh evidence の collection contract が自己矛盾しています: {fresh_shape_error}",
            fresh,
        )
    stored_source = stored.get("source") if isinstance(stored.get("source"), dict) else {}
    fresh_source = fresh.get("source") if isinstance(fresh.get("source"), dict) else {}
    for key in _OVERLAP_COLLECTION_CONTRACT_KEYS:
        if key == "collection_mode":
            # stored（前回のオンライン収集）と fresh（今回のオンライン再収集）
            # は同じ producer 経路を通るため collection_mode は完全一致する
            # ことを要求する。offline fixture 由来の evidence は上の必須
            # field チェックで既に拒否されている。
            if stored_source.get(key) != fresh_source.get(key):
                return (
                    False,
                    E_OVERLAP_PREFLIGHT_DRIFT,
                    f"collection contract drift ({key}): stored={stored_source.get(key)!r} "
                    f"fresh={fresh_source.get(key)!r}",
                    fresh,
                )
    if fresh_source.get("collection_mode") != "exhaustive_cursor_pagination":
        return (
            False,
            E_OVERLAP_PREFLIGHT_DRIFT,
            f"fresh evidence の collection_mode が exhaustive_cursor_pagination ではありません: "
            f"{fresh_source.get('collection_mode')!r}",
            fresh,
        )

    fresh_decision_inputs = fresh.get("decision_inputs_sha256")
    if expected_decision_inputs_sha256 is None or fresh_decision_inputs != expected_decision_inputs_sha256:
        return (
            False,
            E_OVERLAP_PREFLIGHT_DRIFT,
            f"decision_inputs_sha256 drift: expected={expected_decision_inputs_sha256} "
            f"fresh={fresh_decision_inputs}",
            fresh,
        )

    unsafe_reason = _overlap_preflight_safety_reason(fresh, linked_issue)
    if unsafe_reason is not None:
        # #1477: route が human_review_required になった原因が、integrity を
        # 確認できる期限付き contract waiver の固定3件だけであるときに限り、
        # その3 candidate を既存の safe-route 判定から除外する。他の source /
        # dependency / validation failure はこの clone 後にも必ず判定される。
        if _has_only_fixed_readback_incomplete_blockers(fresh):
            waiver, waiver_error = _load_verified_overlap_readback_waiver(repo, linked_issue)
            if waiver_error is not None:
                return (
                    False,
                    E_OVERLAP_PREFLIGHT_EVIDENCE_INVALID,
                    f"overlap_readback_waiver が検証できません: {waiver_error}",
                    fresh,
                )
            effective_fresh = dict(fresh)
            if waiver is not None:
                effective_fresh["route"] = "proceed_with_collision_evidence"
                remaining_unsafe_reason = _overlap_preflight_safety_reason(
                    effective_fresh, linked_issue
                )
                if remaining_unsafe_reason is None:
                    return True, None, "", effective_fresh
        return False, E_OVERLAP_PREFLIGHT_UNSAFE_ROUTE, unsafe_reason, fresh

    return True, None, "", fresh


def create_pr(repo: str, title: str, body_file: Path, branch: str, draft: bool) -> str:
    args = [
        "pr",
        "create",
        "--repo",
        repo,
        "--title",
        title,
        "--body-file",
        str(body_file),
        "--head",
        branch,
        "--base",
        "main",
    ]
    if draft:
        args.append("--draft")
    result = run_gh(*args)
    return result.stdout.strip().splitlines()[-1] if result.stdout.strip() else ""


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)

    if args.publish.strip().lower() != "yes":
        emit_error(E_APPROVAL_MISSING, "publish: yes が指定されていません")
        return 2

    if not args.pr_body_file.exists():
        emit_error(E_PR_BODY_VALIDATION_FAILED, f"pr-body-file が存在しません: {args.pr_body_file}")
        return 2

    original_body = args.pr_body_file.read_text(encoding="utf-8")

    repo = args.repo or resolve_repo()
    if not repo:
        emit_error(E_GH_FAILURE, "git remote から owner/repo を取得できませんでした")
        return 2
    branch = args.branch or resolve_branch()
    if not branch:
        emit_error(E_GH_FAILURE, "現在のブランチ名を取得できませんでした")
        return 2

    state = get_linked_issue_state(repo, args.linked_issue)
    if state is None:
        emit_error(
            E_LINKED_ISSUE_STATE_UNKNOWN,
            f"linked issue #{args.linked_issue} の state を取得できませんでした",
        )
        return 2

    link_kind = "Closes" if state == "OPEN" else "Refs"
    final_body = apply_linked_issue_reference(original_body, args.linked_issue, link_kind)

    changed_paths = resolve_changed_paths(args.changed_paths)
    validator_result = _run_pr_body_validator(final_body, changed_paths, args.linked_issue)
    if validator_result.get("status") != "pass":
        errors = validator_result.get("errors", [])
        rule_ids = ",".join(error.get("rule_id", "") for error in errors if isinstance(error, dict))
        detail = validator_result.get("message", "PR body validation failed")
        if rule_ids:
            detail = f"{detail}; rule_ids={rule_ids}"
            emit_kv("VALIDATOR_RULE_IDS", rule_ids)
        error_code = _classify_validator_errors(errors)
        emit_error(error_code, str(detail))
        return 2

    japanese_result = _run_japanese_content_validator(final_body)
    if japanese_result.get("status") != "pass":
        _jap_status = japanese_result.get("status")
        preflight = {
            "schema": "PR_BODY_PREFLIGHT_RESULT_V1",
            "status": _jap_status if _jap_status in {"fail", "internal"} else "internal",
            "body_sha256": japanese_result.get("body_sha256", ""),
            "failed_blocks": japanese_result.get("failed_blocks", 0),
            "aggregate_ratio": japanese_result.get("aggregate_ratio", 0.0),
            "threshold": japanese_result.get("threshold", 0.1),
        }
        emit_kv("PR_BODY_PREFLIGHT_RESULT_V1", json.dumps(preflight, ensure_ascii=False))
        emit_error(E_PR_BODY_JAPANESE_VALIDATION_FAILED, japanese_result.get("stderr", ""))
        return 2

    existing = find_existing_pr(repo, branch)
    if existing:
        emit_kv("EXISTING", "true")
        emit_kv("PR_URL", existing["url"])
        emit_kv("PR_NUMBER", existing["number"])
        emit_kv("LINKED_ISSUE", args.linked_issue)
        emit_kv("LINK_KIND", link_kind)
        return 0

    draft = str(args.draft).strip().lower() == "true"

    final_body_file = tempfile.NamedTemporaryFile(
        mode="w",
        suffix=".md",
        encoding="utf-8",
        delete=False,
    )
    try:
        final_body_file.write(final_body)
        final_body_file.flush()
        final_body_file.close()
        final_body_path = Path(final_body_file.name)

        if args.dry_run:
            emit_kv("DRY_RUN", "true")
            emit_kv("PR_TITLE_PREVIEW", args.pr_title)
            emit_kv("PR_BODY_PREVIEW_FIRST_LINES", "\\n".join(final_body.splitlines()[:5]))
            emit_kv("LINKED_ISSUE", args.linked_issue)
            emit_kv("LINK_KIND", link_kind)
            emit_kv("DRAFT", str(draft).lower())
            return 0

        # --- Overlap preflight hard gate (Issue #1458) ---
        # gh pr create 直前・既存 PR 検出/dry-run 処理より後に実行する。
        # P1-1 (PR #1467 review fix): forced_by_label の判定はここでオンライン
        # 再取得した fresh labels のみを使う（stale cache は使わない）。取得
        # 失敗は fail-closed（gate を必ず有効化する）として扱う。
        fresh_labels, labels_fetch_error = fetch_current_linked_issue_labels(
            repo, args.linked_issue
        )
        if labels_fetch_error is not None:
            forced_by_label = True
        else:
            forced_by_label = FORCE_OVERLAP_PREFLIGHT_LABEL in (fresh_labels or [])
        overlap_gate_active = bool(args.overlap_preflight_required) or forced_by_label
        pr_create_repo = repo
        if overlap_gate_active:
            # Issue #1470 (AC1): PR mutation target を GitHub Repository API の
            # canonical full_name (小文字化形) として一度だけ解決し、fresh
            # preflight のオンライン再実行と gh pr create --repo の両方に同じ
            # 値を使う。解決に失敗した場合は fallback せず停止する。
            target_repo = resolve_canonical_repository(repo)
            emit_kv("OVERLAP_PREFLIGHT_FORCED_BY_LABEL", str(forced_by_label).lower())
            if labels_fetch_error is not None:
                emit_kv("OVERLAP_PREFLIGHT_LABELS_FETCH_ERROR", labels_fetch_error)
            if target_repo is None:
                emit_error(
                    E_OVERLAP_PREFLIGHT_SOURCE_FAILURE,
                    f"canonical repository を解決できませんでした: {repo}",
                )
                return 2

            # Issue #1470 (Medium 1): canonical repo が raw repo と異なる場合
            # （mixed-case / rename alias）、labels と既存 PR を canonical
            # target で再確認する。以降のすべての repo-scoped 呼び出し
            # （overlap preflight オンライン再実行・gh pr create）は同一の
            # target_repo（canonical）を使うため、この再確認は label
            # forcing 判定と idempotency チェックの canonical target 追従を
            # 保証する追加の安全確認である（label 再取得失敗は fail-closed
            # で forced 継続）。
            if target_repo != repo:
                canonical_labels, canonical_labels_error = fetch_current_linked_issue_labels(
                    target_repo, args.linked_issue
                )
                if canonical_labels_error is not None:
                    forced_by_label = True
                elif FORCE_OVERLAP_PREFLIGHT_LABEL in (canonical_labels or []):
                    forced_by_label = True
                overlap_gate_active = bool(args.overlap_preflight_required) or forced_by_label

                canonical_existing = find_existing_pr(target_repo, branch)
                if canonical_existing:
                    emit_kv("EXISTING", "true")
                    emit_kv("PR_URL", canonical_existing["url"])
                    emit_kv("PR_NUMBER", canonical_existing["number"])
                    emit_kv("LINKED_ISSUE", args.linked_issue)
                    emit_kv("LINK_KIND", link_kind)
                    return 0

            gate_ok, gate_error_code, gate_detail, _fresh_evidence = run_overlap_preflight_gate(
                repo=target_repo,
                linked_issue=args.linked_issue,
                evidence_file=args.overlap_preflight_evidence_file,
                expected_evidence_sha256=args.overlap_preflight_expected_evidence_sha256,
                expected_decision_inputs_sha256=args.overlap_preflight_expected_decision_inputs_sha256,
            )
            if not gate_ok:
                emit_error(gate_error_code or E_OVERLAP_PREFLIGHT_SOURCE_FAILURE, gate_detail)
                return 2
            pr_create_repo = target_repo

        try:
            pr_url = create_pr(pr_create_repo, args.pr_title, final_body_path, branch, draft)
        except subprocess.CalledProcessError as exc:
            emit_error(E_GH_FAILURE, f"gh pr create 失敗: exit {exc.returncode}")
            if exc.stderr:
                emit_kv("COMMAND_STDERR", exc.stderr.strip()[:500])
            return 2

        if not pr_url:
            emit_error(E_GH_FAILURE, "gh pr create が URL を返しませんでした")
            return 2

        match = re.search(r"/pull/(\d+)", pr_url)
        pr_number = match.group(1) if match else ""

        emit_kv("PR_URL", pr_url)
        emit_kv("PR_NUMBER", pr_number)
        emit_kv("LINKED_ISSUE", args.linked_issue)
        emit_kv("LINK_KIND", link_kind)
        emit_kv("EXISTING", "false")
        emit_kv("DRY_RUN", "false")
        return 0
    finally:
        Path(final_body_file.name).unlink(missing_ok=True)


if __name__ == "__main__":
    sys.exit(main())
