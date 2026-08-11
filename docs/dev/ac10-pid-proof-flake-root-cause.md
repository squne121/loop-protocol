# `test_ac10_real_executor_chain_drives_real_preflight_and_planner_with_pid_proof` flake — root cause（Issue #2073）

**状態:** 緩和済み・未解消（serial lane 分離により再現頻度は低下したが、根本原因は未解消。詳細は下記「2026-08 再検証（PR #2068 CI）」参照）
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

**注意（2026-08 訂正）:** 上記の serial lane 分離は、CI フル並列負荷という
外部要因が原因であるという当時の仮説に基づく **緩和策** であり、根本原因を
特定・修正した恒久解決ではない。下記「2026-08 再検証」節が示すとおり、
inner `PLANNER_TIMEOUT`（60秒）仮説は実測により事実上棄却されており、
serial lane 分離後も同一テストが CI で再現している。したがってこの節は
「試みた緩和策の記録」として残すが、これのみで解決済みとは扱わない。

これによりテスト自体・実装側コードは変更せず、CI のフル並列負荷という
外部要因から独立した安定実行を確保する（**ただし後述のとおり、この分離後も
再現が報告されており、根本原因は未解消**）。

## 2026-08 再検証（PR #2068 CI）— inner timeout 仮説の事実上の棄却

PR #2068 の CI 再検証で、上記「解消方針」節の serial lane 分離が根本解決に
なっていなかったことが判明した。serial lane 分離後も
`test_ac10_real_executor_chain_drives_real_preflight_and_planner_with_pid_proof`
が 3 回連続で再現した。

CI job API で実測したところ、**失敗時のテスト実行時間は 0.26 秒**だった
（`pytest` の `slowest 25 durations` 出力より）。これは本文書が唯一の
未確証仮説として挙げていた「inner `PLANNER_TIMEOUT`（60秒）による
planner subprocess の timeout」という仮説と完全に矛盾する。
`PLANNER_TIMEOUT = 60` による `subprocess.TimeoutExpired` が原因であれば、
テストは最低でも 60 秒程度かかるはずだが、実測は 0.26 秒であり、
timeout 経路が発生した可能性は事実上排除される。

したがって、本文書が挙げていた 2 つの timeout 仮説（outer / inner）は
**いずれも証拠と矛盾する**ことが確認され、根本原因は依然として未特定である。

### 新しい観測

`_splice_pid_proof_harness()`
（`scripts/agent-guards/tests/test_skill_runtime_preflight_bytecode_cache.py`
664-699行目）を確認すると、証跡書き込みハーネスは planner スクリプトの
`if __name__ == "__main__":` ブロックの先頭（`main()` 呼び出しより前）に
挿入されており、planner プロセスが起動しさえすれば瞬時に証跡が書かれる
設計になっている。

一方、preflight 側の証跡（`pid_proof_preflight.json`）は正常に書き込まれて
いる（テストは先にこのファイルを読んでおり、そこでは失敗していない）。
これは preflight プロセス自体は正常に起動・実行され、`_invoke_planner()`
の呼び出しまで到達していることを意味する。

以上から、失敗の実体は次のいずれかである可能性が高い:

- planner subprocess 自体が起動していない（`_invoke_planner()` 内部の
  `subprocess.run()` 呼び出し前に何らかの early return / 例外分岐がある）
- planner subprocess は起動したが、ハーネスの harness コードが実行される
  前に即座に終了している（0.26 秒という短時間の失敗と整合する）

この 2 つの可能性のいずれであるかは未確定であり、確定させるには
`_invoke_planner()` 呼び出し直前・直後の状態と planner subprocess 自身の
`returncode` / `stdout` / `stderr` を独立に観測できる診断情報が必要である
（本 Issue の Scope 2「テストへの診断情報追加」を参照）。

### 現時点のステータス

- inner `PLANNER_TIMEOUT`（60秒）仮説: **実測（0.26秒での失敗）により事実上棄却**
- outer timeout（`timeout_seconds: 120`）仮説: 既存文書のとおり `SKILL_RUNTIME_FAIL`
  が観測されないため引き続き除外
- 根本原因: **未解消**。serial lane 分離は再現頻度を下げる緩和策として機能して
  いるが、CI フル並列負荷が唯一の要因ではないことが今回の再検証で判明した
  （serial lane 分離後の再現）。
- 次のアクション: 本 Issue で追加する診断情報（executor の `returncode` /
  `stdout` / `stderr`）を用いた次回の再現時に、planner subprocess が
  起動したかどうかを直接確認する。
