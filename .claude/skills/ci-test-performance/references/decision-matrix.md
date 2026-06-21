# CI Test Performance 判断マトリクス

`ci-test-performance` Skill の `references/` ドキュメント。
詳細な判断マトリクスと `CI_TEST_PERFORMANCE_DECISION_V1` 完全スキーマを定義する。

## 変更タイプ → レーン対応表

| 変更パス | fast_static | python_unit | contract_artifact | integration |
|---|---|---|---|---|
| `.github/workflows/**` | 必須 | 必須 | 必須 | 必須 |
| `pyproject.toml` | 不要 | 必須 | 不要 | 不要 |
| `uv.lock` | 不要 | 必須 | 不要 | 不要 |
| `src/**/*.ts` / `src/**/*.tsx` | 必須 | 不要 | 不要 | 必須 |
| `scripts/**/*.py` | 不要 | 必須 | 条件付き | 不要 |
| `schemas/**` | 不要 | 必須 | 必須 | 不要 |
| `docs/dev/test-lane-policy.md` | 不要 | 不要 | 必須 | 不要 |
| `docs/dev/agent-skill-boundaries.md` | 不要 | 不要 | 必須 | 不要 |
| `docs/dev/**`（その他） | 不要 | 不要 | 条件付き（CI/skill/schema/artifact contract 変更の場合のみ） | 不要 |
| `.claude/skills/**/SKILL.md` | 不要 | 不要 | 必須 | 不要 |
| `.codex/agents/*.toml` | 不要 | 不要 | 必須 | 不要 |
| `.agents/skills/**/SKILL.md` | 不要 | 不要 | 必須 | 不要 |

## CI_TEST_PERFORMANCE_DECISION_V1 完全スキーマ定義

```yaml
CI_TEST_PERFORMANCE_DECISION_V1:
  schema: CI_TEST_PERFORMANCE_DECISION_V1
  issue_number: <int>                     # 関連 Issue 番号
  pr_number: <int | null>                 # 関連 PR 番号（PR レビュー時）
  decision_scope: docs_only | ci_change | dependency_change | review_only
    # docs_only: docs / skill / agent 定義のみ変更
    # ci_change: .github/workflows を含む変更
    # dependency_change: pyproject.toml / uv.lock を含む変更
    # review_only: PR レビューのみ（実装者でない場合）
  changed_paths:
    - "<変更されたファイルパス>"

  lane_classification:
    fast_static:
      applicable: true | false
      evidence:
        - "<適用/非適用の根拠>"
      required_commands:
        - "<実行が必要なコマンド>"
    python_unit:
      applicable: true | false
      evidence:
        - "<適用/非適用の根拠>"
      required_commands:
        - "<実行が必要なコマンド>"
    contract_artifact:
      applicable: true | false
      evidence:
        - "<適用/非適用の根拠>"
      required_commands:
        - "<実行が必要なコマンド>"
    integration:
      applicable: true | false
      evidence:
        - "<適用/非適用の根拠>"
      required_commands:
        - "<実行が必要なコマンド>"

  baseline_inputs:
    ci_runtime_baseline_v1_available: true | false
    run_count: <int>                      # 利用可能な baseline 実行数
    p50_p95_ready: true | false           # P50/P95 統計が利用可能か
    # 注: bootstrap 3 runs と decision baseline 20 runs を区別する
    # 注: 1 回の CI 実行のみで「高速化成功」と判定しない

  artifact_consistency:
    ci_test_selection_v1_checked: true | false
    missing_pytest_args:
      - "<ci_test_selection/v1 pytest_args に未登録のテストパス>"
    # 現行状態: schemas/tests/ は ci_test_selection/v1 から欠落中

  risk_flags:
    # 以下から該当するものを列挙
    - xdist_collection_order_risk      # pytest-xdist での collection 順序リスク
    - ruff_exit_zero_forbidden         # --exit-zero を使っている場合
    - docs_only_no_runtime_delta       # docs-only 変更で runtime delta なし
    - baseline_insufficient            # baseline run 数が不足（< 20）
    - schemas_tests_missing_from_selection  # schemas/tests/ が ci_test_selection から欠落

  reviewer_gate:
    approve_allowed: true | false
    required_evidence:
      - TEST_VERDICT_MACHINE            # test-runner による TEST_VERDICT_MACHINE/v1
      - CI_CHECK_RUN_SCOPED             # GitHub CI check の成功
      # ci_change の場合はさらに必要:
      # - ci_runtime_baseline_v1       # baseline との比較（ci_change 時）
    approve_denied_reason: null | "<理由>"
    # approve_allowed: false の場合、理由を記載する

  follow_up_required:
    - null                             # 必要なし
    # または follow-up Issue タイトル候補を記載
```

## lanes_affected 形式（runtime-delta.md での使用）

`templates/runtime-delta.md` での `lanes_affected` は mapping 形式を使う:

```yaml
lanes_affected:
  fast_static: false
  python_unit: false
  contract_artifact: true
  integration: false
```

## risk_flags の説明

| flag | 意味 | 対処 |
|---|---|---|
| `xdist_collection_order_risk` | pytest-xdist 導入時に collection 順序不一致のリスクがある | `--dist loadscope` または `--dist loadfile` を使う |
| `ruff_exit_zero_forbidden` | `ruff check --exit-zero` を使っている | CI gate では `--exit-zero` 禁止 |
| `docs_only_no_runtime_delta` | docs-only の変更で runtime への影響がない | test-runner の skip waiver が必要 |
| `baseline_insufficient` | CI runtime baseline の run 数が 20 未満 | bootstrap runs を増やしてから P50/P95 判定する |
| `schemas_tests_missing_from_selection` | `schemas/tests/` が `ci_test_selection/v1` の pytest_args から欠落 | #1064 または専用 Issue で修正する |

## reviewer_gate の判定基準

PR レビュアーが `approve_allowed: false` を設定すべき条件:

1. `lane_classification` に `applicable: true` のレーンが存在するが、対応する `required_commands` の実行証跡がない
2. `ci_change` で `ci_runtime_baseline_v1` との比較が提出されていない
3. `risk_flags` に `ruff_exit_zero_forbidden` が含まれる
4. `baseline_inputs.p50_p95_ready: false` で「高速化成功」と主張している
