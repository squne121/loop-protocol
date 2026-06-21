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

- `python-test` job は現在 `setup-node-pnpm` / `pnpm install --frozen-lockfile` を実行している（後続 child Issue で削除予定）
- `pytest` は複数の step に分割されて実行されている（後続 child Issue で統合予定）
- `schemas/tests/` は実行されているが `ci_test_selection/v1` の `pytest_args` に未登録
- `ruff` は未導入（#1063 で対応予定）
- `pytest-xdist` は未導入（#1064 で対応予定）

本 Issue (#1060) では CI 実装の変更は行わない。`.github/workflows/ci.yml`、`pyproject.toml`、`uv.lock` の変更は行わない。

## Target Policy

### 4 レーン定義

詳細は `docs/dev/test-lane-policy.md` を参照する。

| レーン | 概要 | 典型的な実行時間 |
|---|---|---|
| `fast_static` | 型チェック・lint・Ruff | < 1分 |
| `python_unit` | pytest（xdist 導入後は並列） | 2-5分 |
| `contract_artifact` | schema・contract・VC スクリプト | 30秒-2分 |
| `integration` | pnpm build・E2E | 5-15分 |

## Procedure

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
      - TEST_VERDICT_MACHINE
      - CI_CHECK_RUN_SCOPED
  follow_up_required: []
```

## 現行で実行可能なコマンド（#1060 時点）

- pnpm typecheck
- pnpm test
- pnpm build
- pnpm lint

## Target Policy（#1063/#1064 以降）

- ruff check . （#1063 で導入後）
- pytest -n auto （#1064 で導入後）

### Ruff 使用に関する注意

```bash
# 正しい使用法
uv run --locked ruff check --select E,F .claude/scripts scripts schemas .claude/skills

# 禁止
ruff check --fix      # 自動修正禁止（コードの意図を変える可能性）
ruff check --exit-zero # CI gate で使用禁止（違反があっても 0 を返す）
ruff check --add-noqa  # 禁止
```

Ruff exit code: 違反なし=0、違反あり=1、設定/CLI/内部エラー=2。

### pytest-xdist 使用に関する注意

```bash
# xdist 導入後の推奨
uv run pytest -n auto --dist loadscope

# 注意点
# - worker 間で test collection の順序・件数が一致しないと壊れる
# - unordered set を使う parametrize は危険
# - fixture の scope (function/module/session) が xdist 対応か確認する
```

## Consumer Routing

詳細な consumer routing は `docs/dev/agent-skill-boundaries.md` の `ci-test-performance Consumer Routing` セクションを参照する。

| Consumer | 使うタイミング |
|---|---|
| `issue-contract-review` | CI 関連 Issue の Required Skills / evidence plan を gate する |
| `implementation-worker` | CI 関連 path 編集前に Skill を読む |
| `test-runner` | VC / runtime artifact を検証し、lane 分類の検証をする |
| `pr-reviewer` | CI 関連 PR で skill output / runtime evidence を確認する |

## hook による advisory suggestion（Out of Scope）（→ #1080）

hook 実装（`FileChanged` / `PreToolUse` による `ci-test-performance` の自動サジェスト）は本 Issue スコープ外とし、#1080 で対応する。

## 関連ドキュメント

- `references/decision-matrix.md`: 詳細判断マトリクスと `CI_TEST_PERFORMANCE_DECISION_V1` 完全スキーマ
- `templates/runtime-delta.md`: runtime delta 記録テンプレート
- `docs/dev/test-lane-policy.md`: CI レーンポリシー（human-readable）
- `docs/dev/agent-skill-boundaries.md`: consumer routing 定義
