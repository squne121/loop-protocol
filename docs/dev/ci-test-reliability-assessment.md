---
title: CI テスト信頼性評価（reliability assessment）
status: draft
related_issue: "#2170"
related_parent_issue: "#2119"
---

# CI テスト信頼性評価（reliability assessment）

このドキュメントは `CI_TEST_RELIABILITY_ASSESSMENT_V1`（`schemas/ci_test_reliability_assessment_v1.schema.json`）が定義する
CI reliability（workflow failure rate / Playwright flaky test rate / Playwright terminal failure rate）の用語・分母・retry semantics・非劣性判定方式を記述する。

## 位置づけ

- 本ドキュメント・schema・validator（`.claude/skills/ci-test-performance/scripts/validate_ci_reliability_assessment_v1.py`）は、既存の `CI_TEST_PERFORMANCE_ASSESSMENT_V2`（`schemas/ci_test_performance_assessment_v2.schema.json`、latency/performance のみを扱う）を置き換えない。両者は独立した schema であり、互いの schema・validator を変更しない。
- 親 Issue #2119 の AC10（「P95 failure/flaky rate の統計的非悪化」）は、本ドキュメントが定義する用語・分母を実データ検証（#2155）で使用する前提の schema/validator を提供する。performance（P50/P95 latency、cohort selector、collector、benchmark manifest）は #2159 のスコープであり、本ドキュメントには含まない。

## reliability_metrics の 3 指標

### workflow_failure_rate（workflow 失敗率）

- 観測単位: GitHub Actions **workflow run**（Playwright test 単位ではない）。
- サンプル対象: `run_attempt == 1` の workflow run のみ（rerun は独立サンプルとして数えない。下記「retry と rerun の区別」参照）。
- 分子: `conclusion == "failure"` の workflow run 数（terminal failure）。
- 分母: `conclusion` が `success` または `failure` の workflow run 数（eligible）。
- `cancelled` / `timed_out` / `action_required` の conclusion は **infrastructure failure** として分類し、分子・分母のいずれにも含めない（terminal failure でも success でもない、独立したカテゴリ）。理由: これらはコード品質のシグナルではなく、インフラ都合（runner timeout、手動 cancel、承認待ち等）による打ち切りであり、reliability の悪化・改善判定に混入させるべきではない。

### playwright_flaky_test_rate（Playwright flaky テスト率）

- 観測単位: **logical test**（Playwright の 1 テストケース）。
- サンプル対象: `run_attempt == 1` の記録のみ。
- 分子: 最終 retry attempt が `passed` で、かつそれより前の attempt に `failed` / `timedOut` / `interrupted` が 1 つ以上ある logical test 数。
- 分母: 最終 attempt の status が `skipped` でない logical test 数（実行された logical test）。

### playwright_terminal_failure_rate（Playwright 終局失敗率）

- 観測単位・サンプル対象: `playwright_flaky_test_rate` と同じ。
- 分子: 最終 retry attempt が `failed` / `timedOut` / `interrupted` の logical test 数（全 retry を使い切っても失敗のまま）。
- 分母: `playwright_flaky_test_rate` と同じ。

## retry と rerun の区別

Playwright の標準用語と GitHub Actions の用語を混同しないための定義:

- **Playwright retry（内部リトライ）**: 同一 workflow run の同一 `run_attempt` 内で、Playwright 自身の `retries` 設定により同一テストが複数回実行されること。`PlaywrightLogicalTestRun.attempts[]` の配列がこれを表す。
- **GitHub Actions rerun（ワークフロー再実行）**: workflow run 全体を GitHub Actions 上で再実行すること。`run_attempt` が 2 以上になる、**別レコード**（`workflow_run_id` は同じだが `run_attempt` が異なる）として記録される。

validator（`validate_ci_reliability_assessment_v1.py`）は `run_attempt == 1` のレコードのみを reliability_metrics の再計算に使用し、`run_attempt > 1`（rerun）のレコードは `raw_attempts` に記録されるが再計算対象から除外する。これにより、rerun で成功した結果を flaky としてカウントする（rerun と retry の混同）ことを構造的に防ぐ。

## サンプル識別（sample_identity）

- `key: "workflow_run_id"` — reliability の独立サンプル単位は `workflow_run_id`（run_attempt 1 時点の attempt-1 terminal outcome）。
- `required_run_attempt: 1` — rerun は記録するが、独立サンプルとして追加でカウントしない。

`(run_id, run_attempt)` の dedupe だけでは rerun 水増しを防げないため、この `workflow_run_id` + `run_attempt == 1` 固定の sample identity が唯一の正規サンプル単位である。

## sample_count_rule（サンプル数規約）

`sample_count_rule.method` は `power_analysis` または `fixed_declared` のいずれかであり、`is_power_derived` で power 由来であることを明示する（`method == "power_analysis"` の場合は schema レベルで `is_power_derived: true` が強制される）。20 run を暗黙の既定値として使わない — 低頻度 failure の非悪化は 20 run では強く証明できない（0 failures / 20 runs でも Clopper-Pearson 両側 95% exact interval の上限は約 16.8%。下記参照）。

## 非劣性判定（non-inferiority evaluation）

### 区間推定方式: Clopper-Pearson exact interval

`non_inferiority_evaluation.interval_method` は `clopper_pearson_exact` に固定する。二項比率の正確な信頼区間（Wald 近似ではなく exact interval）を用いる理由は、before 側が 0 failure の場合でも区間の上限が有限かつ非ゼロになる（0% と誤認しない）ため。

`validate_ci_reliability_assessment_v1.py` は scipy 等の外部依存を持たず、正則不完全ベータ関数（regularized incomplete beta function）を Numerical Recipes の continued fraction アルゴリズムで実装し、二分探索でその分位点（quantile）を求めることで Clopper-Pearson 区間を計算する。

例: 0 failures / 20 runs、信頼水準 95% の場合、片側上限は約 `0.1684`（約 16.8%）であり、0% ではない。

### 非劣性判定ロジック

`before` cohort の実測率（`before.rate`）に事前宣言した `non_inferiority_margin` を加えた閾値と、`after` cohort の Clopper-Pearson 区間上限（`after.ci_upper`）を比較する:

- `after` cohort の denominator が `sample_count_rule.required_sample_count` 未満 → `outcome: inconclusive`（サンプル数不足で判定不能）。
- `after.ci_upper <= before.rate + non_inferiority_margin` → `outcome: non_inferior`（非劣性が確認された）。
- それ以外 → `outcome: inferior`（非劣性が確認できない）。

`before` 側が 0 failure の場合も、上記ロジックは `before.rate == 0.0` として margin をそのまま適用するため、0 failure を「今後も 0% であることが保証されている」とは扱わない（margin 自体が許容幅を表す）。

## cancelled / timed_out / infrastructure failure の扱い

| conclusion | 分類 | workflow_failure_rate への算入 |
|---|---|---|
| `success` | success | 分母に算入（分子には含まない） |
| `failure` | terminal_failure | 分子・分母の両方に算入 |
| `cancelled` | infrastructure_failure | 除外（分子・分母のいずれにも含まない） |
| `timed_out` | infrastructure_failure | 除外 |
| `action_required` | infrastructure_failure | 除外 |
| `skipped` | 除外（excluded） | 除外 |

Playwright logical test の最終 attempt が `skipped` の場合も、`playwright_flaky_test_rate` / `playwright_terminal_failure_rate` の両方の分母から除外する（実行されなかったテストとして扱う）。

## validator の検証範囲

`validate_ci_reliability_assessment_v1.py` は以下を検証する:

1. **structural_valid**: `schemas/ci_test_reliability_assessment_v1.schema.json`（Draft 2020-12）に対する構造検証。
2. **semantic_valid**: `raw_attempts.{before,after}` から `reliability_metrics.{before,after}` の 3 指標（numerator/denominator/rate）を再計算し、自己申告値と一致するか検証する。不一致は `reliability_metric_recomputation_mismatch` エラーとして報告する。
3. `non_inferiority_evaluation` の `before`/`after` の Clopper-Pearson 区間（`ci_lower`/`ci_upper`）と `outcome` を再計算し、自己申告値と一致するか検証する。

自己申告された `reliability_metrics` / `non_inferiority_evaluation` の値は、`raw_attempts` からの独立再計算と一致しない限り信頼されない（fixture-driven semantic validator パターン。`validate_ci_performance_assessment_v2.py` と同様の設計方針）。

## fixture

`fixtures/ci-test-reliability/` に以下の fixture を配置する:

- `valid_workflow_failure_rate_non_inferior.json`（positive）: 3 指標すべてが自己申告値と一致し、rerun・infrastructure failure・skipped test を含みながらも正しく除外されている例。
- `valid_zero_before_failure_clopper_pearson.json`（positive）: before 側 0 failure の Clopper-Pearson zero-failure ケース。
- `invalid_denominator_mismatch.json`（negative）: 分母が実データと不一致。
- `invalid_retry_rerun_confusion.json`（negative）: GitHub Actions rerun の結果を Playwright retry による flaky として誤集計。
- `invalid_cancelled_misclassification.json`（negative）: `cancelled` を terminal failure として誤分類。

## Scope Delta

`schemas/catalog.yaml` へのエントリ追加は `schemas/tests/test_catalog.py` の `EXPECTED_ENTRY_COUNT` と `EXPECTED_SCHEMA_IDS` という catalog 件数・schema_id 集合の契約テストと連動しており、本 Issue の Allowed Paths（`schemas/catalog.yaml` のみ）を厳密には超える 1 行的な機械的更新（カウンタと集合への 1 エントリ追加）を `schemas/tests/test_catalog.py` に対して行った。既存の catalog エントリ追加時の慣例（コミット履歴の `EXPECTED_ENTRY_COUNT` コメントの `#<issue番号>: +<schema_id>` 形式）に従っている。
