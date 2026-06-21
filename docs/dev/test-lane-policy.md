# CI テストレーンポリシー

このドキュメントは LOOP_PROTOCOL の CI テストレーン設計方針と、AI エージェントが CI 高速化判断を再現可能に実行するためのポリシーを定義する。

## Operative Status（現行実装状態）

このドキュメントは **目標ポリシー（Target Policy）** を定義するものであり、以下の現行実装状態との差分を明確にする。

現行 CI（`.github/workflows/ci.yml`）での実装状態:

- `python-test` job はまだ `setup-node-pnpm` と `pnpm install --frozen-lockfile` を実行している
- `pytest` は複数の step に分割されて実行されている
- `schemas/tests/` は実行されているが、`ci_test_selection/v1` の `pytest_args` からは欠落している
- `ruff` は未導入（`pyproject.toml` に dev dependency として登録されていない）
- `pytest-xdist` は未導入（`pyproject.toml` に dev dependency として登録されていない）

後続の child Issue（#1063 Ruff 導入、#1064 pytest-xdist 導入、#1061 python-test 整理）にて実際の CI 変更が行われる。

## Target Policy（目標ポリシー）

### 4 レーン定義

```yaml
ci_test_lane_policy_v1:
  lanes:
    - id: fast_static
      name: "Fast Static Analysis"
      description: "型チェック・lint・静的解析。コード変更があれば常に実行。"
      tools:
        - pnpm typecheck
        - pnpm lint
        - "ruff check --select E,F ."
      characteristics:
        - 実行時間: 1分以内
        - 外部依存: なし
        - 並列化: 可能
      trigger:
        - 任意のソースコード変更
        - CI 常設

    - id: python_unit
      name: "Python Unit Tests"
      description: "Python ユニットテスト。pytest による単体・統合テスト。"
      tools:
        - "uv run pytest <test-paths>"
        - "uv run pytest -n auto（pytest-xdist 導入後）"
      characteristics:
        - 実行時間: 2-5分（xdist 導入後は短縮見込み）
        - 外部依存: なし
        - 並列化: pytest-xdist 導入後
      trigger:
        - Python ファイル変更
        - schema / contract 変更
        - CI 常設

    - id: contract_artifact
      name: "Contract / Artifact Verification"
      description: "スキーマ・コントラクト・アーティファクトの整合性検証。"
      tools:
        - "uv run pytest schemas/tests/"
        - "VC スクリプト（Issue 別）"
      characteristics:
        - 実行時間: 30秒-2分
        - 外部依存: なし
        - 並列化: 可能
      trigger:
        - docs/ 変更
        - schema/ 変更
        - .claude/skills/ の SKILL.md 変更

    - id: integration
      name: "Integration Tests"
      description: "E2E・ゲームビルド・playwright テスト。ブラウザ・ビルド成果物を必要とする。"
      tools:
        - pnpm build
        - pnpm test（playwright）
      characteristics:
        - 実行時間: 5-15分
        - 外部依存: ブラウザ、Node.js
        - 並列化: 限定的
      trigger:
        - src/ 変更
        - UI コンポーネント変更
        - CI 常設（ブランチ保護）
```

### 判断マトリクス

変更内容に応じたレーン選択の判断基準:

| 変更タイプ | fast_static | python_unit | contract_artifact | integration |
|---|---|---|---|---|
| TypeScript/JS 変更 | 必須 | 不要 | 不要 | 必須 |
| Python スクリプト変更 | 不要 | 必須 | 条件付き | 不要 |
| docs/ 変更 | 不要 | 不要 | 必須 | 不要 |
| schema/ 変更 | 不要 | 必須 | 必須 | 不要 |
| CI workflow 変更 | 必須 | 必須 | 必須 | 必須 |
| Skill / Agent 定義変更 | 不要 | 不要 | 必須 | 不要 |
| pyproject.toml 変更 | 不要 | 必須 | 不要 | 不要 |

## AI エージェント向け運用指針

### いつ ci-test-performance Skill を使うか

以下のコンテキストで `.claude/skills/ci-test-performance/SKILL.md` を参照する:

1. **実装前**: CI 関連パス（`.github/workflows/**`、`pyproject.toml`、`uv.lock`）を編集する前
2. **レビュー時**: CI 関連 PR で `CI_TEST_PERFORMANCE_DECISION_V1` の証跡を確認する時
3. **テスト設計時**: 新しいテストがどのレーンに属するかを判断する時

### hook による advisory suggestion（Out of Scope）（→ #1080）

hook による CI skill suggestion の実装は本 Issue スコープ外とし、#1080 で対応する。
将来的には `FileChanged` / `PreToolUse` hook から `additionalContext` を返して AI エージェントに通知する設計を予定している。

### consumer routing

各 consumer が `ci-test-performance` Skill をどのように使うかの設計は `docs/dev/agent-skill-boundaries.md` の `ci-test-performance consumer routing` セクションを参照する。

## 関連ドキュメント

- `.claude/skills/ci-test-performance/SKILL.md`: 詳細な判断手順と `CI_TEST_PERFORMANCE_DECISION_V1` 定義
- `.claude/skills/ci-test-performance/references/decision-matrix.md`: 詳細判断マトリクス
- `docs/dev/agent-skill-boundaries.md`: consumer routing 定義
