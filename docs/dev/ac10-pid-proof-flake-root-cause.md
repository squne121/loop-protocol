# `test_ac10_real_executor_chain_drives_real_preflight_and_planner_with_pid_proof` flake — root cause（Issue #2073）

**状態:** 解決済み（serial lane 分離により対応済み）
**関連 Issue:** #2073

## 症状

`scripts/agent-guards/tests/test_skill_runtime_preflight_bytecode_cache.py::test_ac10_real_executor_chain_drives_real_preflight_and_planner_with_pid_proof`
が、CI の `python-test-core` job（pytest-xdist 4 worker、フルスイート同時実行）でのみ
`FileNotFoundError`（
`.claude/artifacts/issue-refinement-loop/1439/pid_proof_planner.json` が存在しない）で
断続的に fail する。ローカル単体実行・ローカル `-n 4` 実行では毎回成功し再現しない。

## 根本原因（Root Cause）— 未確定の仮説として扱う

このテストは以下の 3 段の real subprocess chain を駆動する:

```text
test (_run_real_executor)
  -> skill_runtime_exec.py --command-id preflight.run.fixture   (outer subprocess)
       -> uv run python3 run_refinement_preflight.py ...        (preflight subprocess)
            -> python3 plan_refinement_loop.py                  (planner subprocess)
```

このうち **2 か所** に固定 timeout があり、CPU 負荷が高い状態（xdist 並列 worker
が同時に別の real subprocess chain を spawn する full-suite 実行）で予算超過が
起きうる:

1. `command_registry.py` の `preflight.run.fixture` エントリの
   `timeout_seconds: 120`（`skill_runtime_exec.py` が
   `subprocess.run([..., timeout=timeout_seconds], ...)` で preflight 全体を
   包む outer timeout）。
2. `run_refinement_preflight.py` の `_invoke_planner()` が持つ
   `PLANNER_TIMEOUT = 60`（`subprocess.run([sys.executable, PLANNER_SCRIPT],
   timeout=PLANNER_TIMEOUT, ...)`）。

この 2 つの timeout 経路のうち、**outer timeout（1）は実装コード上の挙動と
矛盾するため除外できる**。`skill_runtime_exec.py` の outer `subprocess.run()`
が `subprocess.TimeoutExpired` を送出した場合、`_emit_timeout_failure()` が
呼ばれ `SKILL_RUNTIME_FAIL: reason_code=child_process_timeout` を stderr に
出力し exit code 2 を返す（`skill_runtime_exec.py` 1657-1658 行目）。しかし
AC10 テストは `assert "SKILL_RUNTIME_FAIL" not in result.stderr` を通過した
後に `pid_proof_planner.json` の `FileNotFoundError` が発生しており、実際に
観測された failure shape は outer timeout 経路（`SKILL_RUNTIME_FAIL` が
必ず出力される）と矛盾する。

残る **inner timeout（2）** は、証拠と矛盾しない唯一の仮説である。
`_invoke_planner()` は `subprocess.TimeoutExpired` を捕捉した場合、
planner の stdout/proof を一切参照せずに `(None, 3, "planner timeout after
60s", "")` を返す（`run_refinement_preflight.py` 1406-1411 行目）。これは
`_apply_exit_code_mapping()` で `environment_failure` (`exit code 3`) に
写像される — このテストが許容している `(0, 1, 2, 3)` の範囲内であり、かつ
`SKILL_RUNTIME_FAIL` も発生しない、観測事実と整合する executor 応答である。

ただし、**この仮説は確証されていない**。元の CI failure では planner
subprocess の returncode / stdout / stderr が保存されておらず、
`_invoke_planner()` が実際に `TimeoutExpired` を捕捉したことを示す直接証拠は
残っていない。したがって「inner planner timeout（60秒 `PLANNER_TIMEOUT`）が
証拠に矛盾しない唯一の仮説だが、確証はない」という位置づけで扱う。

いずれにせよ、`skill_runtime_exec.py` / `run_refinement_preflight.py`
の応答自体は契約上正当（許容 exit code 範囲内、`SKILL_RUNTIME_FAIL` なし）
であり、テストの「executor の安全境界違反がないこと」という本来の検証
意図とは無関係に、テストが無条件で要求する
`pid_proof_planner.json` の存在チェックだけが `FileNotFoundError` で fail
する。これは実装のバグではなく、**このテストが timeout 依存の real
subprocess chain を要求する構造そのものが、CI のフル並列負荷レーンとは
非親和的である** という、既存の
`test_baseline_vc_preflight.py::test_issue_393_snapshot_fixture_processed`
（Issue #1788 / PR #1790。固定 90s timeout の real subprocess VC が xdist
並列負荷下で `90.10s` 実測により断続的に timeout していた）と同型の
既知パターンである。

## 解消方針

`run_refinement_preflight.py` / `skill_runtime_exec.py` の timeout 値・
subprocess chain 構造そのものへの変更は、本 Issue の Allowed Paths に
含まれる `command_registry.py` の `argv` を変更すると Allowed Paths 外
（`command_registry.py`）の変更が必要になり、Stop Condition に該当する。
timeout 値の単純な引き上げも、real subprocess chain 全体を CI のフル並列
負荷から独立に安定化させる保証にはならない（負荷はホスト全体の CPU 状況に
由来し、個々の timeout をどれだけ延ばしても理論上再現しうる）。

既存の `test_baseline_vc_preflight.py` の precedent（`.github/ci/python-test-plan.json`
の `parallel_exclude` による serial lane 分離）と同じ手法を適用する:
`test_skill_runtime_preflight_bytecode_cache.py` を `parallel_exclude` に
追加し、`-n 0` の serial lane（`python-test-core` job 内の別 step、xdist
並列負荷とは独立して実行される）で実行する。現行の `python-test-plan` の
`parallel_exclude` / serial-lane contract は file-scoped であり、
nodeid-level（`--deselect` 等による個別テスト単位）の serial routing 導入は
別スコープと位置づける。したがって対象テストを含むファイル全体（AC1-AC9/
AC11-AC13 も含む。既存 precedent と同じ制約）が serial lane に移動する。

これによりテスト自体・実装側コードは変更せず、CI のフル並列負荷という
外部要因から独立した安定実行を確保する。
