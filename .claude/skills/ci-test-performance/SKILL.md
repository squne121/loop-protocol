---
name: ci-test-performance
description: CI / テストパフォーマンスのレーン分類・hotspot 分析・意思決定を行う。GitHub Actions、python-test、Ruff、pytest-xdist、ci_runtime_baseline_v1、ci_test_selection/v1 に関する変更前にこの Skill を読む。PR レビューで CI 最適化の証跡が必要な時にも使う。
paths:
  - ".github/workflows/**"
  - "pyproject.toml"
  - "uv.lock"
  - "docs/dev/ci-performance.md"
  - "docs/dev/test-lane-policy.md"
  - ".claude/skills/ci-test-performance/**"
  - ".codex/agents/**"
  - ".claude/agents/**"
  - ".agents/skills/ci-test-performance/**"
  - "schemas/**"
  - "docs/dev/agent-skill-boundaries.md"
---

# CI Test Performance Skill

CI テストパフォーマンスの判断手順を定義する。
詳細な判断マトリクスは `references/decision-matrix.md` を参照する。
runtime delta テンプレートは `templates/runtime-delta.md` を参照する。

## Operative Status（現行実装状態）

この Skill が定義する Target Policy と現行 CI 実装の差分:

- Issue #1760: `python-test` job は `python-test-core`（Python-only、`setup-python-uv` / `uv python install` / `uv sync --locked --group dev` のみ）と `codex-execpolicy`（Node/npm/codex CLI + execpolicy matrix + `tests/codex/test_local_main_branch_guard.py` 専用）に分割され、`python-test` は required status check 名を維持したまま `needs: [python-test-core, codex-execpolicy]` + `if: always()` の集約 job になった
- `python-test-core` job は現在 `setup-python-uv` / `uv python install` / `uv sync --locked --group dev` を実行し、`setup-node-pnpm` / `pnpm install --frozen-lockfile` は実行しない
- `scripts/ci/verify_python_test_lane.py` が `python-test-core` の Python-only invariant と `python-test` 集約 job の `needs`/`if` 契約を、`scripts/ci/verify_ci_check_conclusions.py` が実 CI check conclusion + AC6 sentinel artifact を検証する
- `python-test` の hook pytest は Python-only hook tests を継続実行し、Node-backed 2 nodeid は `--deselect=<exact nodeid>` で除外している
- `node-backed-hook-tests` job が Node.js / pnpm 依存の hook wrapper 検証を専用に実行している
- `ci_test_selection/v1` の split evidence は `ci_test_selection_summary_v1.json` で統合され、python-test 側 absent / node-backed 側 exactly 2 / union-disjointness を機械検証している
- `pytest` の python_unit レーンは `.github/ci/python-test-plan.json`（python-test-plan SSOT）を `scripts/ci/python_test_plan.py` loader 経由で消費する単一 step に統合され、pytest 実行と `ci_test_selection/v1` artifact 生成の双方が同一 plan を参照する（#1064）
- `schemas/tests/` は python-test-plan の `targets` に含まれ、`ci_test_selection/v1` の `pytest_argv` と実行対象が一致する（#1064 で drift 解消）
- `ruff` は導入済み（#1063）
- `pytest-xdist` は導入済みで、python-test-plan の `xdist.workers` / `xdist.dist` で worker 数・scheduler を集中管理する（#1064）

この Skill 自体はポリシー定義であり、特定 Issue に閉じない。実装時は current workflow の lane 分類と evidence の整合を優先する。

### python-test-plan SSOT（#1064 で追加した単一の正本）

`.github/ci/python-test-plan.json` は pytest target / `--ignore` / `--deselect` / worker 数 / scheduler を集約した machine-readable plan であり、`scripts/ci/python_test_plan.py` loader が shell `eval` を避けて NUL 区切り / JSON で argv を emit する。target set を変える場合は workflow や generator を直接編集せず python-test-plan を編集する。`generate_ci_test_selection_artifact.py` は loader 経由で plan の scope argv を `pytest_argv` に記録し、collect 非 0 / timeout / nodeid 0 件で fail-close する。

## Target Policy（目標ポリシー）

### 4 レーン定義

詳細は `docs/dev/test-lane-policy.md` を参照する。

| レーン | 概要 | 典型的な実行時間 |
|---|---|---|
| `fast_static` | 型チェック・lint・Ruff | < 1分 |
| `python_unit` | pytest（xdist 導入後は並列、Node-backed hook tests を除く） | 2-5分 |
| `contract_artifact` | schema・contract・VC スクリプト・Node-backed hook tests | 30秒-2分 |
| `integration` | pnpm build・E2E | 5-15分 |

## Procedure（手順）

### Step 1: 変更パスの分類

1. 変更されたファイルパスを列挙する
2. `references/decision-matrix.md` の変更タイプ→レーン対応表でレーンを特定する
3. `CI_TEST_PERFORMANCE_DECISION_V1.lane_classification` を構築する

### Step 2: ci_runtime_baseline_v1 の確認

```bash
# CI runtime artifact の確認（存在する場合）
ls .claude/artifacts/ci_runtime_baseline_v1.json 2>/dev/null || echo "baseline not found"
```

- baseline が存在する場合: P50/P95 を確認して hotspot を特定する
- baseline が存在しない場合: `ci_runtime_baseline_v1_available: false` を記録する
- bootstrap 3 runs と decision baseline 20 runs を区別する
- 1 回の CI 実行のみで「高速化成功」と判定しない（P50/P95 が必要）

### Step 3: artifact_consistency チェック

```bash
# ci_test_selection/v1 と実際の pytest 実行の整合性確認
rg "pytest_args" .claude/skills/ docs/ 2>/dev/null | head -20
```

- `ci_test_selection/v1` と実際の pytest step の差分を検出する
- `schemas/tests/` が `pytest_args` から欠落している場合は `risk_flags` に記録する
- `python-test` と `node-backed-hook-tests` の collect-only / artifact が lane 分離後の実行対象と矛盾しないか確認する
- reviewer gate に使う artifact は upload 前に `test -s` を通し、`if-no-files-found: error` で silent-fail を避ける

### Step 4: CI_TEST_PERFORMANCE_DECISION_V1 の出力

詳細なスキーマは `references/decision-matrix.md` を参照する。

```yaml
CI_TEST_PERFORMANCE_DECISION_V1:
  schema: CI_TEST_PERFORMANCE_DECISION_V1
  issue_number: <int>
  pr_number: <int | null>
  decision_scope: docs_only | ci_change | dependency_change | review_only
  changed_paths: []
  lane_classification:
    fast_static:
      applicable: true | false
      evidence: []
      required_commands: []
    python_unit:
      applicable: true | false
      evidence: []
      required_commands: []
    contract_artifact:
      applicable: true | false
      evidence: []
      required_commands: []
    integration:
      applicable: true | false
      evidence: []
      required_commands: []
  baseline_inputs:
    ci_runtime_baseline_v1_available: true | false
    run_count: <int>
    p50_p95_ready: true | false
  artifact_consistency:
    ci_test_selection_v1_checked: true | false
    missing_pytest_args: []
  risk_flags: []
  reviewer_gate:
    approve_allowed: true | false
    required_evidence:
      - CI_CHECK_RUN_SCOPED  # authoritative（Issue #1856 evidence authority cutover）
      # TEST_VERDICT_MACHINE は required_evidence に含めない（advisory のみ、Issue #1856）。
      # 詳細な優先順位は `.claude/skills/pr-review-judge/references/evidence-policy.md` を参照。
  follow_up_required: []
```

## 現行で実行可能なコマンド（#1060 時点）

- pnpm typecheck（型チェック）
- pnpm test（テスト）
- pnpm build（ビルド）
- pnpm lint（静的解析）

## Target Policy（#1063/#1064 以降の目標ポリシー）

- uv run --locked ruff check .claude/scripts scripts schemas .claude/skills （#1063 で導入済み）
- pytest -n auto （#1064 で導入済み。worker 数・scheduler は `.github/ci/python-test-plan.json` で集中管理）

### Ruff 使用に関する注意

```bash
# 正しい使用法
uv run --locked ruff check .claude/scripts scripts schemas .claude/skills

# 禁止
ruff check --fix      # 自動修正禁止（コードの意図を変える可能性）
ruff check --exit-zero # CI gate で使用禁止（違反があっても 0 を返す）
ruff check --add-noqa  # 禁止
```

Ruff exit code: 違反なし=0、違反あり=1、設定/CLI/内部エラー=2。

### pytest-xdist 使用に関する注意

```bash
# xdist 導入後の推奨
uv run --locked pytest -n auto --dist loadscope

# 注意点
# - worker 間で test collection の順序・件数が一致しないと壊れる
# - unordered set を使う parametrize は危険
# - fixture の scope (function/module/session) が xdist 対応か確認する
```

## Consumer Routing（コンシューマルーティング）

詳細な consumer routing は `docs/dev/agent-skill-boundaries.md` の `ci-test-performance Consumer Routing` セクションを参照する。

| Consumer | 使うタイミング |
|---|---|
| `issue-contract-review` | CI 関連 Issue の Required Skills / evidence plan を gate する |
| `implementation-worker` | CI 関連 path 編集前に Skill を読む |
| `test-runner` | VC / runtime artifact を検証し、lane 分類の検証をする |
| `pr-reviewer` | CI 関連 PR で skill output / runtime evidence を確認する |

## hook による advisory suggestion（スコープ外）（→ #1080）

hook 実装（`FileChanged` / `PreToolUse` による `ci-test-performance` の自動サジェスト）は本 Issue スコープ外とし、#1080 で対応する。

## CI_TEST_PERFORMANCE_ASSESSMENT_V2（#1724 で追加した versioned addition）

`CI_TEST_PERFORMANCE_DECISION_V1`（本 Skill が既に定義するレーン判定契約）とは別に、`CI_TEST_PERFORMANCE_ASSESSMENT_V2` を追加する。V1 は in-place で変更しない（V1 の enum・required key はそのまま維持）。V2 は「CI 高速化を主張しているか（claim）」と「その主張を裏付ける証拠が揃っているか（evidence）」を直交させて意味検証するための追加契約であり、性能主張のない correctness/provisioning 変更を 20-run baseline 不足で誤ブロックしないために使う。

### 4 軸分離

- `claim.kind`: `none | improvement | non_regression | absolute_budget`（主張者の意図。`absolute_budget` は `metric` / `maximum_value_ms` 等の threshold を伴う）
- `performance_evidence.status`: `not_required | not_instrumented | unavailable | insufficient_samples | incomparable_cohort | complete`（証拠の充足状態）
- `observation.outcome`: `not_observed | improved | regressed | equivalent_within_threshold | budget_met | budget_exceeded | inconclusive`（実測結果）
- `claim_evaluation.outcome`: `not_applicable | satisfied | not_satisfied | inconclusive`（claim が実測で成立したか。validator/reviewer が導出する）

`declared_impact` は diff から検証されていない自己申告値であることを明示するフィールド名（旧 `impact` から改名）。trusted base/head SHA 由来の `diff_evidence` による真の diff-derived 検証は Out of Scope。

`functional_evidence` は `.claude/skills/pr-review-judge/scripts/ci_verdict_summary_v2.py`（canonical consumer は pr-review-judge）の provenance を再利用し、`proof_level: check_run_only` / `coverage_bound: false` で保証範囲を明示する。green Check Run だけでは「期待したコマンドが実際に実行された」証明にならないため、この保証範囲を超えない。

`risk_acknowledgement` は `reference.source_kind` / `reference.source_id`（リスクが提起された場所へのポインタ）と `verification_status: unverified` を持つ。`accepted` / `actor` のような自己申告フィールドは持たない。

### validator CLI（検証コマンド）

```bash
uv run --locked python3 .claude/skills/ci-test-performance/scripts/validate_ci_performance_assessment_v2.py \
  --assessment <path-to-assessment.json> \
  --output <path-to-result.json> \
  --ci-verdict-summary <path-to-ci_verdict_summary_v2.json> \
  --expected-head-sha <trusted PR head SHA> \
  --expected-artifact-digest <sha256:...>
```

`pnpm policy:check:ci-performance` からも同じ validator の pytest スイートを実行できる。CLI 単体は `pnpm policy:validate:ci-performance -- --assessment ... --output ...` からも実行できる。

- exit code: `0=valid`（structural_valid かつ semantic_valid） / `2=structural または semantic invalid` / `3=operational failure`（ファイル不在・strict JSON parse 失敗）
- JSON parsing は重複キー・`NaN`・`Infinity` を拒否する strict parsing
- `--output` に書き出す結果は `structural_valid` / `semantic_valid` / `approval_eligible` / `errors` / `blockers` / `warnings` を持つ。「assessment としての妥当性」（structural_valid/semantic_valid）と「reviewer gate 上の承認可否」（approval_eligible）は別軸であり、意味論的に valid でも証拠不足・functional evidence 不足で `approval_eligible: false` になり得る
- `decision` は assessment の *入力* スキーマには存在しない。producer が虚偽の decision を埋め込めないよう、`decision` 相当の判定は常に `--output` の validation result（`CI_TEST_PERFORMANCE_ASSESSMENT_V2_VALIDATION_RESULT`）としてのみ出力される
- `functional_evidence.ci_verdict_summary_ref.selected_checks` は自己申告であり単独では `approval_eligible: true` の根拠にならない。`--ci-verdict-summary` で canonical `ci_verdict_summary_v2` artifact を、`--expected-head-sha` で trusted head SHA（`git rev-parse HEAD` / gh CLI 由来）を渡し、validator が artifact 側の `expected_head_sha` 一致・`overall_status: merge_ready`・required check 集合の completeness（`status: completed` / `conclusion: success` / 正の `check_run_id` / `head_sha_match: true` / 非 synthetic provenance）を独立に再検証したときのみ `approval_eligible: true` になり得る。`--expected-artifact-digest` を渡すと artifact ファイルの sha256 digest も照合する
- `performance_evidence.status: complete` の場合、`runtime_delta` を伴わないと `complete_status_missing_runtime_delta_evidence` で承認 blocked になる。`runtime_delta.delta` の値は before/after の `p50_seconds`/`p95_seconds` から validator が再計算した値と一致しないと `delta_recomputation_mismatch` で invalid になる

詳細スキーマは `schemas/ci_test_performance_assessment_v2.schema.json` / `schemas/ci_runtime_delta_v2.schema.json` を参照する。fixture は `.claude/skills/ci-test-performance/scripts/fixtures/` を参照する。

### 実 production gate（#2159 OWNER scope-authority ruling issuecomment-5299412215, items 2/P0-8・3/P1-3/AC11）

毎回の CI 実行で走る `claim.kind: none` の schema smoke producer（`codex-execpolicy` job 内）とは別に、`.github/workflows/ci.yml` の `e2e-performance-benchmark-assessment-gate` job（`workflow_dispatch` opt-in、通常 PR には配線しない）が `tests/ci/test_ci_performance_gate.py --cohort-fixture <path> --output <path>` を実 CLI として呼び出し、AC11 の `_evidence_readiness_hard_check_post_filter`（証拠不足時に非 zero exit）と P0-8 の `build_assessment_from_percentile_cohorts`（実 claim 計算）を 1 つの実行可能パスへ配線する。詳細は `docs/dev/e2e-performance-benchmark.md` の「実 production gate の配線」を参照する。

## 関連ドキュメント

- `references/decision-matrix.md`: 詳細判断マトリクスと `CI_TEST_PERFORMANCE_DECISION_V1` 完全スキーマ
- `templates/runtime-delta.md`: runtime delta 記録テンプレート
- `docs/dev/test-lane-policy.md`: CI レーンポリシー（human-readable）
- `docs/dev/agent-skill-boundaries.md`: consumer routing 定義
- `docs/dev/ci-performance.md`: `CI_TEST_PERFORMANCE_ASSESSMENT_V2` の意味検証契約の詳細
- `schemas/ci_test_performance_assessment_v2.schema.json` / `schemas/ci_runtime_delta_v2.schema.json`: V2 JSON Schema
- `.claude/skills/ci-test-performance/scripts/validate_ci_performance_assessment_v2.py`: V2 semantic validator
