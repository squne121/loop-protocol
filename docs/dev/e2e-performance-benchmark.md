---
title: E2E Performance Benchmark Contract
status: active
related_issue: "#2159"
parent_issue: "#2119"
---

# E2E Performance Benchmark Contract

`#2119` の E2E lane split（`e2e-core` / `e2e-responsive-matrix`）が性能改善の主張（P50/P95 provider critical path shortening、gate-ready latency 非劣化）を実データで検証するための測定器契約。`#2159`（Issue A）でこの測定器自体を再設計した（reliability/failure-flaky-rate は別 Issue B のスコープ）。

## 固定 SHA ベンチマーク設計（#1064 方式）

before/after の比較は、任意の 2 コミットを事後的に比較するのではなく、事前に固定した 2 つの 40-hex commit SHA（`before_sha` / `after_sha`）に対して専用の `workflow_dispatch` benchmark route を起動し、同一条件下で複数回実行して蓄積したサンプル集合を比較する。これにより、比較対象のコミット間で無関係な変更が混入するリスクを排除する。

## サンプル identity: `workflow_run_id`

独立サンプルの単位は GitHub の `workflow_run_id` である。同一 `workflow_run_id` の rerun attempt（`run_attempt` が異なるだけの再実行）は独立サンプルとして数えない（`scripts/ci/collect_e2e_performance_benchmark.py::_dedupe_by_workflow_run_id` / `tests/ci/test_ci_performance_gate.py::_dedupe_by_workflow_run_id`）。これは `(run_id, run_attempt)` を identity にしていた旧設計の rerun 水増し問題を修正したもの。

## Paired critical path 統計

post-split の `e2e-core` / `e2e-responsive-matrix` は並列実行される 2 つの独立ジョブであり、DAG 上の実際の critical path は「同一 `workflow_run_id` の中で両ジョブのうち遅い方」である。したがって provider の P50/P95 は次の式で計算する:

```
critical_path_i = max(core_duration_i, responsive_duration_i)   # 同一 workflow_run_id の i
provider_P50 = nearest_rank_v1(critical_path_1, ..., critical_path_n, percentile=50)
provider_P95 = nearest_rank_v1(critical_path_1, ..., critical_path_n, percentile=95)
```

`max(median(core), median(responsive))`（旧式）は異なる `workflow_run_id` の run を混ぜて計算するため、どの実際の 1 回の実行の wall-clock critical path も再現しない誤った統計量であり、使用しない。

pair の一方が欠損している `workflow_run_id`（例: `e2e-core` は成功したが `e2e-responsive-matrix` が cancel された）は、cohort から黙って除外せず、`evidence_errors` に明示する（`tests/ci/test_ci_performance_gate.py::_pair_by_workflow_run_id`）。

## percentile method: `nearest_rank_v1`

P50/P95 の算出には 1-indexed nearest-rank 法（`rank = ceil(percentile/100 * n)`、ソート済みサンプルの `rank` 番目の値）を使う。この method 名はバージョン管理された識別子であり、`tests/ci/test_ci_performance_gate.py::_nearest_rank_percentile` と `.claude/skills/ci-test-performance/scripts/validate_ci_performance_assessment_v2.py::_nearest_rank_percentile` の両方で同一のセマンティクスを持つ（Allowed Paths の境界上、実装は複製されているが method 名とアルゴリズムは共通）。

## Gate-ready latency: 同一時計

before/after の gate-ready latency は、両アームとも GitHub API の同一時計（`workflow_run.run_started_at` を起点、対応する check run の `completed_at` を終点）から計算する（`tests/ci/test_ci_performance_gate.py::_gate_ready_latency_seconds_same_clock`）。旧設計は before 側を `measurements.jsonl` の手動合算、after 側を GitHub API という異なる時計で測定しており、apples-to-oranges 比較になっていた。

## Comparability fingerprint の三分類

`COMPARABILITY_FINGERPRINT_FIELDS` という単一 tuple による一致判定は、「cohort 内で一致すべき」「before/after 間でも一致すべき」「治療そのものとして意図的に異なる」という 3 種類の異なる意味を持つフィールドを区別できなかった。`tests/ci/test_ci_performance_gate.py` は以下の 3 分類を定義する:

- `WITHIN_COHORT_REQUIRED_EQUAL`: 同一アーム（before または after）の cohort 内の全 run で一致すべきフィールド（`host_runner_image` / `playwright_container_image_digest` / `node_version` / `pnpm_version` / `playwright_version` / `lockfile_hash` / `workflow_digest`）。
- `CROSS_COHORT_REQUIRED_EQUAL`: before/after の両アーム間でも一致すべきフィールド（インフラ・provenance が split とは無関係に変化していないことの保証。`workflow_digest` を除く上記のサブセット）。
- `INTENTIONAL_TREATMENT_DIFFERENCE`: before/after 間で意図的に異なることが期待されるフィールド（`workflow_digest` — split そのものが treatment）。

`host_runner_image`（裸の GitHub Actions runner）と `playwright_container_image_digest`（`mcr.microsoft.com/playwright@sha256:...` の固定コンテナ）は別々の provenance フィールドとして分離されている。

## Placeholder 値の拒否

fingerprint フィールドが欠損しているか、`""` / `null` / `"unknown"` / `"unknown/unknown"` / `"N/A"` のいずれかの placeholder 値を持つ run は、fail-closed で cohort から除外する（`_is_placeholder` / `_fingerprint_has_placeholder`）。2 つの placeholder 値同士が偶然一致するケースを「一致した」と誤判定しない。

## Immutable manifest（`e2e_performance_benchmark_manifest_v1`）

`scripts/ci/collect_e2e_performance_benchmark.py` は before/after 双方の run 群から、artifact ID・artifact digest・head SHA・`workflow_run_id`・job を検証した上で `schemas/e2e_performance_benchmark_manifest_v1.schema.json` 準拠のマニフェストを生成する。このスクリプトはライブ GitHub API 呼び出しを行わない（`scripts/ci/verify_ci_check_conclusions.py` と同じ既存パターンに従い、CI ジョブまたは人間オペレータが `gh api` で事前取得した JSON を入力として受け取る）。これにより hermetic な unit test が可能になる。

各アームは `complete: true/false` を持ち、`min_run_count`（既定 20）に満たない job があれば `false` になる。manifest は書き込み後に再利用されず、再実行は新しいファイルとして生成する（in-place 上書きしない）。

## `CI_TEST_PERFORMANCE_ASSESSMENT_V2` の percentile 再計算（AC8）

`validate_ci_performance_assessment_v2.py` は、`runtime_delta.<cohort>.run_details[].duration_seconds` の raw sample が存在する場合、それらから `nearest_rank_v1` で P50/P95 を再計算し、cohort の自己申告 `p50_seconds` / `p95_seconds` と照合する（`percentile_recomputation_mismatch_p50` / `percentile_recomputation_mismatch_p95`）。`run_details` は `schemas/ci_runtime_delta_v2.schema.json` 上 optional であり、存在しない場合はこのチェックをスキップする（後方互換）。

## `CI_TEST_PERFORMANCE_ASSESSMENT_V2` producer wiring（AC10）

`.github/workflows/ci.yml` の `codex-execpolicy` job に `Generate CI_TEST_PERFORMANCE_ASSESSMENT_V2 artifact` / `Validate CI_TEST_PERFORMANCE_ASSESSMENT_V2 artifact` / `Upload CI_TEST_PERFORMANCE_ASSESSMENT_V2 artifact` の 3 ステップを追加した。この producer は毎回の CI 実行で `claim.kind: none` の assessment を実際に生成・検証・artifact 化する（`if-no-files-found: error`）。固定 before/after SHA ベンチマークによる実際の性能主張（`claim.kind: improvement` 等）は、この毎回実行される producer とは別に、`scripts/ci/collect_e2e_performance_benchmark.py` の専用 benchmark route から out-of-band に作成する。

## Runtime Verification Applicability

Issue #2159 の `decision: immediate` の下、cohort 依存（実 GitHub Actions 20-run history が必要）な統合テストは、本実装セッションのようにライブ history が存在しない環境では `pytest.skip()`（`tests/ci/test_ci_performance_gate.py` の既存 3 テスト）で SKIP する。これは `docs/dev/runtime-verification-policy.md` の SKIP 規約に従うものであり、fabricated PASS ではない。cohort 非依存のロジック（fingerprint 分類、paired critical path 計算、同一時計 latency、percentile 再計算、collector の artifact 検証）は fixture-driven unit test で実挙動を検証済みである。

close-verification（最終ゲート）用途には `EvidenceInsufficientError` / `_evidence_readiness_hard_check` の非 zero exit 経路を使う（AC11）。`pytest.skip()` を close 条件に使用しない。
