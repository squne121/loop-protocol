""".claude/skills/issue-refinement-loop/scripts/tests/test_refinement_only_vs_explicit_implementation_dispatch.py

Issue #2740: `issue-refinement-loop` の `approved` 終了（refinement-only
termination）は `impl-review-loop` Step 1 の implementation dispatch を単独で
は決してトリガーしない。Step 1 dispatch は、ユーザーが実装を明示的に依頼した
invocation（`/impl-review-loop <N>` 相当）を通じて `impl-review-loop` 自身の
`preparation.md` エントリゲートが `root_entry_router.run_root_transition()`
を呼び出したときにのみ発生する。

本ファイルは `test_root_entry_router_workflow_capability.py`（AC15/AC16）の
fake transport / spy パターンを再利用し、以下 2 テストで両者を区別する:

- `test_refinement_only_termination_never_dispatches_implementation`
  (negative): これは **構造的（静的 AST）検査**であり、実行時の振る舞い証明
  ではない。`issue-refinement-loop` の Step 5 termination が実際に呼ぶ
  production スクリプト群（`.claude/skills/issue-refinement-loop/scripts/`
  直下の `root_entry_router.py` 自身を除く非テストモジュール）を静的に検査し、
  いずれも `run_root_transition(` を呼び出していないことを検証する。これは
  #2740 で切断された「Step 5 が root_entry_router を呼ぶ経路」が production
  コードのどこにも存在しないことを構造的にピン留めする（grep VC ではなく AST
  解析 + pytest）。テスト内に構築するローカル spy `invoke_step1` は
  production path のどこにも接続されておらず、その呼び出し回数が 0 である
  ことは AST 検査結果から論理的に導かれる**自明な帰結（tautology）** であっ
  て、それ自体が独立した runtime 振る舞い証拠ではない（PR #2748 OWNER レビ
  ュー P2 指摘、issuecomment-5825945601）。独立した決定的証拠は上記 AST
  assertion のみが担う。
- `test_explicit_implementation_request_invokes_step1_exactly_once_after_fresh_review`
  (positive): `test_root_entry_router_advances_once_on_positive_fixture`
  (AC16) と同型のポジティブフィクスチャで `run_root_transition()` を直接呼び
  出し、fresh review go + live-state 一致の条件で `invoke_step1` が exactly
  once 呼ばれ、`fetch_calls["count"] == 2` という既存不変条件（drift 検出の
  ための2回 fetch）も維持されることを検証する -- これは
  `impl-review-loop` 自身の Root-Owned Synchronous Entry Transition エント
  リゲート（`.claude/skills/impl-review-loop/steps/preparation.md`）が
  ユーザーの明示的な実装依頼に応じて行う呼び出しと同型である。
"""

from __future__ import annotations

import ast
import json
import subprocess
import sys
from pathlib import Path

_SCRIPTS_DIR = Path(__file__).resolve().parent.parent
if str(_SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS_DIR))

import root_entry_router as rer  # noqa: E402

_REPO = "squne121/loop-protocol"


def _fake_workflow_capability_result(decision: str) -> str:
    return json.dumps(
        {
            "schema": "CLAUDE_GPT_WORKFLOW_CAPABILITIES_V1",
            "profile": "issue-to-impl",
            "decision": decision,
            "checks": {
                "uv": {"status": "ok", "reason": "resolved"},
                "spark": {"status": "not_required"},
                "github": {"auth": True, "repo_read": True, "operations": {}},
            },
            "reasons": [],
        }
    )


def _calls_run_root_transition(py_file: Path) -> bool:
    """Return True if the given module contains an AST Call node whose
    callee name (bare name or attribute) is `run_root_transition`."""
    source = py_file.read_text(encoding="utf-8")
    tree = ast.parse(source, filename=str(py_file))
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        name = None
        if isinstance(func, ast.Name):
            name = func.id
        elif isinstance(func, ast.Attribute):
            name = func.attr
        if name == "run_root_transition":
            return True
    return False


# --- negative: refinement-only termination never dispatches implementation ---


def test_refinement_only_termination_never_dispatches_implementation():
    """GIVEN issue-refinement-loop の Step 5 が `approved` 終了する
    refinement-only termination flow（LOOP_HANDOFF_RESULT_V1 marker 出力の
    み、#2740）
    WHEN Step 5 が実際に実行しうる production スクリプト群（
    `.claude/skills/issue-refinement-loop/scripts/*.py` のうち
    `root_entry_router.py` 自身を除く全非テストモジュール -- 特に
    `publish_termination_report.py` や `dependency_materializer.py` を含む）
    を AST 解析で静的検査する
    THEN いずれのモジュールも `run_root_transition(` を呼び出していない --
    refinement-only termination だけでは `impl-review-loop` Step 1 の
    implementation dispatch が構造的に発生し得ないことを静的にピン留めする。

    この assertion は **構造検査（static/AST）のみ**を独立した証拠として扱
    う。以前の実装は、production path に接続されていないローカル spy
    `invoke_step1` の呼び出し回数が 0 であることを追加で assert していたが、
    このカウンタはこのテスト内のどのコードからも呼び出されようがないため、
    AST 検査の結果に関わらず常に真になる tautological な assertion であり、
    独立した振る舞い証拠を追加しなかった（PR #2748 OWNER レビュー P2 指摘、
    issuecomment-5825945601）。誤解を招く自明な assertion として削除した。"""

    offending: list[str] = []
    for py_file in sorted(_SCRIPTS_DIR.glob("*.py")):
        if py_file.name == "root_entry_router.py":
            # The definition site itself; calling itself recursively is not
            # the concern here (and it doesn't).
            continue
        if _calls_run_root_transition(py_file):
            offending.append(py_file.name)

    assert offending == [], (
        "issue-refinement-loop/scripts/*.py must not call "
        "run_root_transition() outside root_entry_router.py itself -- "
        f"Step 5 termination must never wire implementation dispatch (#2740): {offending}"
    )


# --- positive: explicit implementation request dispatches Step 1 exactly once ---


def test_explicit_implementation_request_invokes_step1_exactly_once_after_fresh_review(
    monkeypatch,
):
    """GIVEN a user-initiated explicit implementation request (`/impl-review-loop
    <N>` 相当) が `impl-review-loop` 自身の Root-Owned Synchronous Entry
    Transition エントリゲート（`preparation.md` の `run_root_transition()`
    呼び出し）を駆動する
    WHEN native GitHub auth / repository read / trusted `uv` が揃い（fresh
    workflow capability preflight が `ready` を返し）、fresh current-run
    `issue-contract-review` が `go` を返し、live body/base state が一致する
    THEN `invoke_step1` は exactly once 呼ばれ、`fetch_calls["count"] == 2`
    という既存不変条件（drift 検出のための2回 fetch）も維持される。"""

    def _fake_run(argv, **kwargs):
        if any("workflow_capability_preflight.py" in str(part) for part in argv):
            return subprocess.CompletedProcess(
                argv, 0, stdout=_fake_workflow_capability_result("ready"), stderr=""
            )
        raise AssertionError(f"unexpected subprocess.run call in this test: {argv}")

    monkeypatch.setattr(rer.subprocess, "run", _fake_run)

    transport = rer.GhCliGitHubEntryTransport(repo=_REPO)
    monkeypatch.setattr(transport, "canonical_repository_identity", lambda: _REPO)

    fetch_calls = {"count": 0}
    fixed_body = "## Outcome\nexplicit implementation request fixture body (#2740)"
    fixed_base_sha = "explicit-request-fixture-base-sha"

    def _fake_fetch_live_issue(issue_number):
        fetch_calls["count"] += 1
        return {
            "body": fixed_body,
            "base_sha": fixed_base_sha,
            "identity_ok": True,
            "fetch_ok": True,
        }

    monkeypatch.setattr(transport, "fetch_live_issue", _fake_fetch_live_issue)
    monkeypatch.setattr(
        transport, "post_comment", lambda issue_number, body: {"ok": True, "comment_id": 1}
    )

    expected_body_sha = rer.compute_body_sha256(fixed_body)

    def _fake_contract_reviewer(**_kwargs):
        return {"status": "go", "body_sha256": expected_body_sha}

    invoke_calls = {"count": 0}

    def _invoke_step1():
        invoke_calls["count"] += 1

    result = rer.run_root_transition(
        issue_number=2740,
        repo=_REPO,
        transport=transport,
        contract_reviewer=_fake_contract_reviewer,
        invoke_step1=_invoke_step1,
        expected_repository_identity=_REPO,
        publish_audit=False,
    )

    assert result["route"]["route"] == rer.ROUTE_INVOKE
    assert result["invoked"] is True
    assert invoke_calls["count"] == 1
    # Same invariant asserted by AC16's existing positive fixture test: a
    # single explicit-implementation-request attempt performs exactly two
    # live fetches (pre- and post-review, for drift detection), never a
    # per-retry-iteration loop.
    assert result["route"]["retry_count"] == 0
    assert fetch_calls["count"] == 2
