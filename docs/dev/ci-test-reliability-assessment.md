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

`success` / `failure` の attempt-1 workflow record は全 metric に一つずつ provenance observation
を持つ。各 denominator はその validated unique workflow-run set の cardinality であり、同じ run
の logical test 数や raw retry attempt 数で増えない。`cancelled`、`timed_out`、`action_required`、
`skipped` は eligible close-evidence sample ではない。

`workflow_failure_rate` は workflow conclusion が `failure` の run が affected である。
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
uv run --locked pytest .claude/skills/ci-test-performance/scripts/tests/test_validate_ci_reliability_assessment_v1.py -q
```

validator exit code は `0` が structural + semantic valid、`2` が contract invalid、`3` が file /
strict JSON operational failure である。semantic validity は close outcome の PASS を意味しない。
count 不足時の outcome は `inconclusive` であり、close evidence として受理されない。分母が
before/after で不均衡な場合（`equal_1_to_1` 契約違反）や、実際に観測された `n` における power が
`target_power` に届かない場合も、同じ `inconclusive` outcome に fold される。
