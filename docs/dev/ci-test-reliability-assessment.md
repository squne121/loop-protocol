---
title: CI テスト信頼性評価（Reliability V1）
status: draft
related_issue: "#2432"
related_parent_issue: "#2424"
---

# CI テスト信頼性評価（Reliability V1）

`CI_TEST_RELIABILITY_ASSESSMENT_V1` は、CI reliability close evidence の固定契約である。
Performance V2 を置き換えず、#2424 の production reporter / wiring / artifact publication も定義しない。

## 固定 power design

唯一の repo-static design は
`newcombe_wilson_hybrid_exact_binomial_power_v1` である。schema の `$defs.power_designs`
と validator の `POWER_DESIGNS` が同じ表を固定し、観測 JSON は別の design、power input、allocation、
oracle を渡せない。

| key | fixed value |
| --- | --- |
| alpha / confidence quantile | `0.05` / one-sided `0.95` |
| target power | `0.80` |
| margin | `0.20` |
| assumed before / after rate | `0.05` / `0.05` |
| allocation / count semantics | equal `1:1` / per arm |
| maximum | 100 runs per arm |
| over budget | `design_infeasible` |

全 arm・全 metric の `sample_provenance.<arm>.<metric>.design_id` と
`sample_count_rule.design_id` はこの ID に固定される。`required_sample_count`、
`is_power_derived`、producer が指定する baseline / alternative / alpha / margin / allocation は
V1 に存在しない。`required_sample_count_per_arm` は validator が列挙した値と一致しなければならない。

## outcome と actual power の実際値検証

close-evidence outcome の唯一の decision function は、保持された
`newcombe_wilson_hybrid_mover_v1` である。各 arm の one-sided Wilson score interval から
Newcombe/Wilson hybrid MOVER により `p_after - p_before` の `ci_upper` を求め、
`ci_upper <= 0.20` を non-inferior とする。Clopper-Pearson interval は各 arm の audit 表示であり、
outcome の根拠ではない。

`evaluate_non_inferiority()` は、`required_sample_count_per_arm` 以上という分母チェックだけでなく、
before/after の分母が完全に一致する（`equal_1_to_1` 契約）ことと、実際に観測された `n` における
`exact_power_for_n(n)` が `target_power=0.80` 以上であることの両方を追加で要求する。分母が不均衡な
cohort（例: before=25, after=40、両方が個別には `required_sample_count_per_arm` を満たす場合でも）や、
`n` 単体では `required_sample_count_per_arm` を満たしていても実際の power が `target_power` に届かない
場合は、count 不足時と同じ `inconclusive` outcome にフォールバックし、`non_inferior` / `inferior` を
計算しない。power は `n` について単調ではない（`n=20` は `n=21` より power が高い）ため、
`required_sample_count_per_arm` を上回っているという事実だけでは実際の power 充足を保証しない。

actual power は別 family の oracle ではない。validator は `(n, n)` を `n=1` から `100` まで
**順に**調べ、全 `(x_before, x_after)` 組についてこの同一の MOVER predicate を評価し、

```text
Binomial(n, 0.05; x_before) * Binomial(n, 0.05; x_after)
```

を pass 組だけ合計する。`target_power >= 0.80` となる最初の `n` が required count である。
単調性を仮定する binary search は使わない。stdlib の log-PMF を使い、`p=0` / `p=1` の
境界は質量 1 を該当 count にだけ置く。V1 golden vector では `n=20` の power は
`0.790213011479415`、`n=21` は `0.7787023565542808`、初めて qualifying する `n=22` は
`0.8454900944198372` である。比較は丸め前の float 値で行い、test の表示比較は `1e-12`
tolerance を使う。

Farrington-Manning、SAS、statsmodels、alternate allocation、budget relaxation はこの contract
の外であり、V1 result に代入してはならない。100 まで qualifying しなければ
`design_infeasible` であり、run 数を増やす許可にはならない。

### alpha=0.05 の位置づけ

`alpha=0.05` は保持された Newcombe/Wilson hybrid decision function の nominal パラメータ（one-sided
95% critical quantile への入力）であり、全 boundary point にわたって一様に較正された worst-case
Type-I error 保証ではない。exact enumeration が計算するのは、この既に固定された predicate の実際の
power であり、alpha の再導出や calibration ではない。本 spec は、全 boundary point にわたる一様な
worst-case Type-I error 5% を主張しない。`alpha` というキー名・型はこの契約のままであり、
`nominal_alpha` への rename や別 family の test への置き換えは行わない。

## workflow-run provenance と分母

独立 sample identity は `workflow_run_id` かつ `run_attempt: 1` だけである。`workflow_records`
が canonical workflow record、`playwright_test_cases` が official Playwright
`TestCase.outcome` record、`sample_provenance` が各 arm / metric の included binary observation
である。validator は次を hard-fail する。

- orphan Playwright workflow ID、workflow record と provenance の arm mismatch、同一 run ID の cross-arm 使用（孤立参照・不一致・重複使用を検出する）
- duplicate workflow ID、duplicate canonical test case、non-attempt-1 sample、retry/rerun sample inclusion（重複や再試行混入を検出する）
- included eligible record の欠落、ineligible record の inclusion、canonical classification mismatch（対象記録の欠落や誤分類を検出する）

`success` / `failure` / `timed_out` の attempt-1 workflow record は全 metric に一つずつ provenance
observation を持つ。各 denominator はその validated unique workflow-run set の cardinality であり、
同じ run の logical test 数や raw retry attempt 数で増えない。`cancelled`、`action_required`、
`skipped` は eligible close-evidence sample ではない。

`workflow_failure_rate` は workflow conclusion が `failure` または `timed_out` の run が affected
である。この eligible set 拡張は3 metric 共有のため、`timed_out` run の provenance observation は
Playwright 2 metric（`playwright_flaky_test_rate` / `playwright_terminal_failure_rate`）でも
受理・完全性検証の対象になるが、Playwright の分類式自体（下記の official `TestCase.outcome` 判定）
は変更されない。`timed_out` は `workflow_failure_rate` の分類にのみ影響し、Playwright observation を
それだけで `affected` にはしない。
歴史的 field 名を残す Playwright metrics は run-level indicator であり、同じ run に official
`TestCase.outcome == flaky` が一つでもあれば `playwright_flaky_test_rate`、
`outcome == unexpected` が一つでもあれば `playwright_terminal_failure_rate` の affected sample
となる。`raw_attempts` は retry audit のみで、primary classification や分母に使用しない。

この検証は provenance で確認できる**構造的** independence（unique attempt-1 ID、arm disjointness、
one run / one observation）だけを主張する。確率的 independence は証明しない。prose、boolean、
enum の independence claim / ledger は schema にないため evidence として拒否される。

## fixtures と検証

`fixtures/ci-test-reliability/valid_fixed_design_workflow_runs.json` は 22 unique runs per arm、
fixed design ID、official TestCase outcomes、audit-only raw attempt を持つ positive golden fixture
である。validator tests は power threshold / boundary、post-hoc contract rejection、provenance
failures、logical-test denominator false green、official outcome-only classification を固定する。
新規の regression test は、equal-cohort 契約と実際の power 充足の両方を独立に検証し、既存の golden
vector（`n=20`/`n=21`/`n=22`）や stdlib-only enumeration の結果を変更しない。

```bash
uv run --locked pytest \
  .claude/skills/ci-test-performance/scripts/tests/test_validate_ci_reliability_assessment_v1.py \
  .claude/skills/ci-test-performance/scripts/tests/test_timed_out_reliability_regression.py \
  -q
```

validator exit code は `0` が structural + semantic valid、`2` が contract invalid、`3` が file /
strict JSON operational failure である。semantic validity は close outcome の PASS を意味しない。
count 不足時の outcome は `inconclusive` であり、close evidence として受理されない。分母が
before/after で不均衡な場合（`equal_1_to_1` 契約違反）や、実際に観測された `n` における power が
`target_power` に届かない場合も、同じ `inconclusive` outcome に fold される。

## #2424 production wiring 契約

本節は #2424（実 CI raw attempts から Reliability V1 close evidence を production 生成する）が
所有する `scripts/ci/build_ci_reliability_assessment_v1.py` と `.github/workflows/ci.yml` の wiring
契約を定義する。上記の schema / validator / power design は #2432/#2507 owner surface のままであり、
本節はこれらを変更しない consumer 契約として追記する。

### 3-run architecture

1. **monolith measured run** — `.github/workflows/ci.yml` を `benchmark_layout=monolith` かつ
   `reliability_evidence=true` で `workflow_dispatch` する。`e2e-core` job が core lane と
   sequential responsive lane の Playwright JSON evidence を additive に生成し、artifact として
   upload する。
2. **split measured run** — 同じく `benchmark_layout=split` かつ `reliability_evidence=true` で
   dispatch する。`e2e-core`（core lane）と `e2e-responsive-matrix`（responsive lane）がそれぞれ
   JSON evidence を生成する。
3. **assessment run** — 上記 2 run が両方 `completed` した後にのみ、`benchmark_layout=
   reliability_assessment` で別の `workflow_dispatch` を行う。`reliability_monolith_run_id` /
   `reliability_split_run_id`（trusted binding、`trusted_ci_verdict_summary_artifact_id` と同じ
   operator-supplied pattern）を指定し、`reliability-assessment` job のみが起動する。measured run
   自身の builder/validator/aggregate が自分自身の final conclusion を循環的に決定することはない
   （assessment run は measured run とは別 run であり、測定後にしか起動できない）。

`benchmark_layout` は既存の `monolith|split` 自由記述文字列 input（#2422 由来）を
`reliability_assessment` 値へ additive に拡張したものであり、`collect_e2e_performance_benchmark.py`
（#2422 owner）のスキーマ・producer 自体は変更しない。既存の `github.event.inputs.benchmark_layout
== ''` ガードは `reliability_assessment` dispatch でも他ジョブを正しくスキップする。

### Playwright JSON evidence route（AC2）

`playwright.config.ts` は `PLAYWRIGHT_JSON_OUTPUT_FILE` が設定されている場合のみ、追加で
`['json', { outputFile: PLAYWRIGHT_JSON_OUTPUT_FILE }]` reporter を有効化する（html/list は常に
維持）。同時に、以下の環境変数から `metadata`（Playwright 公式の config-level metadata。JSON report
の `config.metadata` にそのまま serialize される）を構成する。

| env var | metadata key |
| --- | --- |
| `RELIABILITY_EXPERIMENT_IDENTITY` | `experiment_identity` |
| `GITHUB_RUN_ID` | `workflow_run_id` |
| `GITHUB_RUN_ATTEMPT` | `run_attempt` |
| `RELIABILITY_BENCHMARK_LAYOUT` | `benchmark_layout` |
| `RELIABILITY_INVOCATION_ID` | `invocation_id` |
| `RELIABILITY_LANE` | `lane` |
| `RELIABILITY_WORKFLOW_SHA` | `workflow_sha` |

`ci.yml` は `benchmark_layout in (monolith, split)` かつ `reliability_evidence == 'true'` の
ときだけ、各 Playwright invocation の直前にこれらの env を設定する。`PLAYWRIGHT_JSON_OUTPUT_FILE`
は `${GITHUB_WORKSPACE}/reliability-evidence/<invocation_id>.json` の full path とする。invocation
ID は固定 3 種類:

| invocation_id | lane | 発生 job / layout |
| --- | --- | --- |
| `e2e-core` | `core` | `e2e-core` job（monolith / split 共通、通常の `pnpm test:e2e:ci`） |
| `e2e-core-responsive` | `responsive` | `e2e-core` job、`benchmark_layout=monolith` のときのみ（sequential responsive workload） |
| `e2e-responsive` | `responsive` | `e2e-responsive-matrix` job、`benchmark_layout=split` のときのみ |

monolith run は `{e2e-core, e2e-core-responsive}`、split run は `{e2e-core, e2e-responsive}` の
2 invocation を生成する（両 arm とも core+responsive の同一 cohort lane をカバーする）。各 JSON は
`ci-reliability-${workflow_run_id}-a${run_attempt}-${invocation_id}` という一意な artifact 名で
`if-no-files-found: error` を使い upload される。

### builder が消費する evidence 契約

`scripts/ci/build_ci_reliability_assessment_v1.py` は以下 4 種類の入力ファイルを読む consumer で
あり、いずれも #2422/#2423 の producer 自体を re-import/re-implement しない。

1. `--manifest`: `e2e_performance_benchmark_manifest_v2`（#2422 owner schema）。
   `frozen_non_treatment.expected_playwright_invocations` は #2424 が定義する opaque pass-through
   フィールドであり、各要素は `{"invocation_id", "lane", "evidence_file",
   "benchmark_layout_only"?}`（`benchmark_layout_only` を持たない要素は両 arm 共通）。
   `frozen_non_treatment.expected_test_count` は全 invocation を跨いだ canonical unique
   `TestCase` 件数の合計期待値（AC5）。`blocks[].runs[]` から各 `benchmark_layout` の
   `workflow_run_id` 集合を展開する。
2. `--receipt`: #2423 の `CI_PERFORMANCE_CLOSE_GRADE_RESULT_V1`
   （`arms.monolith.workflow_run_ids` / `arms.split.workflow_run_ids` / `run_set_digest` /
   `manifest_sha256` / `materialization_policy` / `evidence_errors`）。展開された
   `workflow_run_id` membership が manifest 側の展開集合と exact 一致することのみ確認し、
   `manifest.experiment_run_set_digest` と `receipt.run_set_digest` の文字列 equality は要求
   しない。
3. `--workflow-evidence`: `{"monolith": {"workflow_run_id", "run_attempt", "conclusion",
   "jobs": [{"name", "conclusion", "status"}]}, "split": {...}}`。GitHub Actions
   `GET /repos/{repo}/actions/runs/{run_id}` と `.../jobs` の authoritative evidence を
   `ci.yml` の assessment job が `gh api` で取得し、そのまま渡す。
4. `--playwright-json-dir`: `<dir>/<monolith|split>/<invocation_id>.json`
   に配置された、公式 Playwright JSON reporter の生出力。

### canonical single output（AC11）

`ci_reliability_close_grade_result_v1.json` は builder/aggregate が deterministic に生成する唯一の
canonical output であり、最低限次を含む: `schema`（`CI_RELIABILITY_CLOSE_GRADE_RESULT_V1`）、
`schema_version`、`experiment_identity`、`manifest_digest`、`canonical_workflow_run_ids.
{monolith,split}`、3 assessment それぞれの `content_digest`、3 validator result、
`composite_envelope_digest`、各 expected invocation の `artifact_id`/`digest`、`aggregate.
{complete, semantic_valid, sample_satisfied, all_non_inferior, exit_code}`。この canonical output
自身の digest（`canonical_output_digest`）は `experiment_run_set_digest`（#2422 owner）・
`run_set_digest`（#2423 owner）とは別アルゴリズム・別入力で算出し、いずれとも文字列 equality を
要求しない。#2486 はこの digest を consume する downstream owner であり、#2424 は #2486 自体を
mutation しない。
