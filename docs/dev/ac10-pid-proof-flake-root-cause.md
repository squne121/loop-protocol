# `test_ac10_real_executor_chain_drives_real_preflight_and_planner_with_pid_proof` flake — root cause（Issue #2073）

**状態:** 解決済み（serial lane 分離により対応済み）
**関連 Issue:** #2073

## 症状

`scripts/agent-guards/tests/test_skill_runtime_preflight_bytecode_cache.py::test_ac10_real_executor_chain_drives_real_preflight_and_planner_with_pid_proof`
が、CI の `python-test-core` job（pytest-xdist 4 worker、フルスイート約 1 万件超を
同時実行）でのみ `FileNotFoundError`（
`.claude/artifacts/issue-refinement-loop/1439/pid_proof_planner.json` が存在しない）で
断続的に fail する。ローカル単体実行・ローカル `-n 4` 実行では毎回成功し再現しない。

## 根本原因（Root Cause）

このテストは以下の 3 段の real subprocess chain を駆動する:

```text
test (_run_real_executor)
  -> skill_runtime_exec.py --command-id preflight.run.fixture   (outer subprocess)
       -> uv run python3 run_refinement_preflight.py ...        (preflight subprocess)
            -> python3 plan_refinement_loop.py                  (planner subprocess)
```

このうち **2 か所** に、CPU が極度に飽和した状態（CI ランナーが 2 core しかない
環境で、xdist 4 worker それぞれが同時に別の real subprocess chain を spawn する
full-suite 実行）で予算超過が起きうる固定 timeout がある:

1. `command_registry.py` の `preflight.run.fixture` エントリの
   `timeout_seconds: 120`（`skill_runtime_exec.py` が
   `subprocess.run([..., timeout=timeout_seconds], ...)` で preflight 全体を
   包む outer timeout）。このエントリの `argv` は
   `["uv", "run", "python3", ...]` であり、`uv run` 自体が起動のたびに
   venv 解決・lockfile read を行うオーバーヘッドを持つ。CPU 飽和下で複数の
   `uv run` 起動が同時に走ると、この起動オーバーヘッドだけで数十秒を消費し
   うる。
2. `run_refinement_preflight.py` の `_invoke_planner()` が持つ
   `PLANNER_TIMEOUT = 60`（`subprocess.run([sys.executable, PLANNER_SCRIPT],
   timeout=PLANNER_TIMEOUT, ...)`）。

`_invoke_planner()` は `subprocess.TimeoutExpired` を捕捉した場合、
planner の stdout/proof を一切参照せずに `(None, 3, "planner timeout after
60s", "")` を返す（`run_refinement_preflight.py` 1406-1411 行目）。これは
`_apply_exit_code_mapping()` で `environment_failure` (`exit code 3`) に
写像される — このテストが許容している `(0, 1, 2, 3)` の範囲内であり、かつ
`SKILL_RUNTIME_FAIL` も発生しない、正当な executor 応答である。

つまり、上記 (1)(2) いずれかの timeout に「outer preflight subprocess の
起動から planner subprocess の proof-file 書き込みまで」の実測時間が
CPU 飽和下で到達すると:

- outer `skill_runtime_exec.py` 側の 120s timeout に preflight subprocess
  全体（`uv run` 起動オーバーヘッド + preflight 本体処理 + planner 起動待ち）
  が到達した場合、preflight subprocess ツリー全体（子の planner
  subprocess を含む）が `subprocess.run(..., timeout=...)` によって kill
  される。preflight 自身はすでに自分の `pid_proof_preflight.json` を
  書き込み済みだが、planner はまだ proof を書く前に kill されうる。
- または `_invoke_planner()` 側の 60s timeout に、planner subprocess の
  起動（Python interpreter 起動含む）自体が CPU starvation で遅延した
  場合に到達し、`TimeoutExpired` により planner subprocess が kill される
  （proof 未書き込みのまま）。

いずれの経路でも、`skill_runtime_exec.py` / `run_refinement_preflight.py`
の応答自体は契約上正当（許容 exit code 範囲内、`SKILL_RUNTIME_FAIL` なし）
であり、テストの「executor の安全境界違反がないこと」という本来の検証
意図とは無関係に、テストが無条件で要求する
`pid_proof_planner.json` の存在チェックだけが `FileNotFoundError` で fail
する。これは実装のバグではなく、**このテストが CPU 飽和下で timeout
依存の real subprocess chain を要求する構造そのものが、CI のフル並列
負荷レーンとは非親和的である** という、既存の
`test_baseline_vc_preflight.py::test_issue_393_snapshot_fixture_processed`
（Issue #1788 / PR #1790。固定 90s timeout の real subprocess VC が 4-way
xdist CPU 飽和下で `90.10s` 実測により断続的に timeout していた）と同型の
既知パターンである。

## 解消方針

`run_refinement_preflight.py` / `skill_runtime_exec.py` の timeout 値・
subprocess chain 構造そのものへの変更は、本 Issue の Allowed Paths に
含まれる `command_registry.py` の `argv`（`uv run` オーバーヘッド）を
変更すると Allowed Paths 外（`command_registry.py`）の変更が必要になり、
Stop Condition に該当する。timeout 値の単純な引き上げも、real subprocess
chain 全体を CI のフル並列負荷から独立に安定化させる保証にはならない
（負荷はホスト全体の CPU 飽和に由来し、個々の timeout をどれだけ延ばしても
理論上再現しうる）。

既存の `test_baseline_vc_preflight.py` の precedent（`.github/ci/python-test-plan.json`
の `parallel_exclude` による serial lane 分離）と同じ手法を適用する:
`test_skill_runtime_preflight_bytecode_cache.py` を `parallel_exclude` に
追加し、`-n 0` の serial lane（`python-test-core` job 内の別 step、xdist
CPU 飽和とは独立して実行される）で実行する。`--ignore` は file path
単位でしか指定できないため、対象テストを含むファイル全体が serial lane
に移動する（AC10 以外の AC1-AC9/AC11-AC13 も含む。既存 precedent と同じ
制約）。

これによりテスト自体・実装側コードは変更せず、CPU 飽和という外部要因
から独立した安定実行を確保する。
