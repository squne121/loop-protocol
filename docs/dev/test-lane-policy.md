# CI テストレーンポリシー

このドキュメントは LOOP_PROTOCOL の CI テストレーン設計方針と、AI エージェントが CI 高速化判断を再現可能に実行するためのポリシーを定義する。

## Operative Status（現行実装状態）

このドキュメントは **目標ポリシー（Target Policy）** を定義するものであり、以下の現行実装状態との差分を明確にする。

現行 CI（`.github/workflows/ci.yml`）での実装状態:

- `python-test` job は `setup-python-uv` / `uv python install` / `uv sync --locked --group dev` を実行し、`setup-node-pnpm` と `pnpm install --frozen-lockfile` は実行しない
- `python-test` の `.claude/hooks/tests/` 実行では、Node-backed 2 nodeid を `--deselect=<exact nodeid>` で除外し、Python-only hook tests を継続実行している
- `node-backed-hook-tests` job は `setup-node-pnpm` / `setup-python-uv` / `pnpm install --frozen-lockfile` を行った上で、Node-backed hook test nodeid だけを実行している
- `ci_test_selection/v1` の split evidence は `ci_test_selection_summary_v1.json` で統合され、python-test 側 absent / node-backed 側 exactly 2 / union-disjointness を機械検証している
- `pytest` の python_unit レーンは `.github/ci/python-test-plan.json`（python-test-plan SSOT）を `scripts/ci/python_test_plan.py` loader 経由で消費する単一 step に統合された（#1064）
- `schemas/tests/` は python-test-plan の `targets` に含まれ、`ci_test_selection/v1` の `pytest_argv` と実行対象が一致する（#1064 で drift 解消）
- `ruff` は導入済み（#1063、`pyproject.toml` の dev dependency に登録）
- `pytest-xdist` は導入済み（#1064、`pyproject.toml` の dev dependency に `pytest-xdist>=3.8,<4` を登録）

#1063（Ruff 導入）・#1064（pytest-xdist 導入 + python-test-plan SSOT）は反映済みである。

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
      description: "Python ユニットテスト。pytest による単体・統合テスト。Node-backed hook tests は除く。"
      tools:
        - "uv run --locked pytest $(python3 scripts/ci/python_test_plan.py --emit run-argv --mode parallel)（python-test-plan SSOT 由来）"
        - "uv run --locked pytest -n 4 --dist loadscope（python-test-plan の固定 worker + scheduler 設定）"
      characteristics:
        - 実行時間: 2-5分（xdist 並列化で短縮）
        - 外部依存: なし
        - 並列化: pytest-xdist（worker 数・scheduler は python-test-plan で集中管理）
      trigger:
        - Python ファイル変更
        - schema / contract 変更
        - CI 常設

    - id: contract_artifact
      name: "Contract / Artifact Verification"
      description: "スキーマ・コントラクト・アーティファクトの整合性検証。Node-backed hook tests を含む。"
      tools:
        - "uv run --locked pytest schemas/tests/"
        - "VC スクリプト（Issue 別）"
        - "uv run --locked pytest <node-backed hook test nodeids>"
      characteristics:
        - 実行時間: 30秒-2分
        - 外部依存: 原則なし。ただし Node-backed hook tests は Node.js / pnpm を要求する
        - 並列化: 可能
      trigger:
        - docs/ 変更
        - schema/ 変更
        - .claude/skills/ の SKILL.md 変更
        - Node-backed hook wrapper / artifact contract の検証

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
| Node-backed hook wrapper / artifact contract 変更 | 不要 | 不要 | 必須 | 不要 |
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

### hook による advisory suggestion（スコープ外）（→ #1080）

hook による CI skill suggestion の実装は本 Issue スコープ外とし、#1080 で対応する。
将来的には `FileChanged` / `PreToolUse` hook から `additionalContext` を返して AI エージェントに通知する設計を予定している。

### consumer routing（コンシューマ別ルーティング）

各 consumer が `ci-test-performance` Skill をどのように使うかの設計は `docs/dev/agent-skill-boundaries.md` の `ci-test-performance consumer routing` セクションを参照する。

## 関連ドキュメント

- `.claude/skills/ci-test-performance/SKILL.md`: 詳細な判断手順と `CI_TEST_PERFORMANCE_DECISION_V1` 定義
- `.claude/skills/ci-test-performance/references/decision-matrix.md`: 詳細判断マトリクス
- `docs/dev/agent-skill-boundaries.md`: consumer routing 定義

## CI_TEST_PERFORMANCE_DECISION_V1 (#1192)

```yaml
CI_TEST_PERFORMANCE_DECISION_V1:
  schema: CI_TEST_PERFORMANCE_DECISION_V1
  issue_number: 1192
  pr_number: 1219
  decision_scope: dependency_change
  changed_paths:
    - pyproject.toml
    - uv.lock
    - .github/workflows/ci.yml
    - scripts/ci/runtime_dependency_smoke.py
    - scripts/ci/tests/test_runtime_dependency_smoke.py
    - docs/dev/test-lane-policy.md
  lane_classification:
    fast_static:
      applicable: false
      evidence:
        - "TypeScript/JS 変更なし; pnpm typecheck/lint はAC11/AC12として実行するが本変更起因でないレーン"
      required_commands: []
    python_unit:
      applicable: true
      evidence:
        - "pyproject.toml / uv.lock の dependency partition 変更"
        - "scripts/ci/tests/test_runtime_dependency_smoke.py を新規追加 — python-test-plan.json の scripts/ci/tests/ target に含まれる"
      required_commands:
        - "uv run --locked pytest scripts/ci/tests/test_runtime_dependency_smoke.py -q"
    contract_artifact:
      applicable: true
      evidence:
        - "CI workflow (.github/workflows/ci.yml) に uv lock --check / isolated smoke ステップを追加"
        - "runtime dependency partition を fail-closed に証明する isolated smoke が runtime consumer contract"
      required_commands:
        - "uv lock --check"
        - "uv run --isolated --locked --no-default-groups python scripts/ci/runtime_dependency_smoke.py"
    integration:
      applicable: false
      evidence:
        - "src/ / UI コンポーネント変更なし"
      required_commands: []
  affected_lanes:
    - python_unit
    - contract_artifact
  added_steps:
    - id: uv_lock_check
      job: python-test
      position: before_uv_sync
      command: uv lock --check
      purpose: "lockfile drift を fail-closed に検出 (AC3/AC8)"
    - id: runtime_dependency_smoke
      job: python-test
      position: before_uv_sync
      command: "uv run --isolated --locked --no-default-groups python scripts/ci/runtime_dependency_smoke.py"
      purpose: "fresh isolated 環境で runtime consumer が成立することを検証 (AC4/AC5/AC8)"
  expected_cost:
    uv_lock_check: "<2s (キャッシュ済みなら ~1s)"
    runtime_dependency_smoke: "<10s (isolated 環境構築 + 3 behavioral checks)"
    total_added_per_run: "<15s"
  required_evidence:
    - TEST_VERDICT_MACHINE
    - CI_CHECK_RUN_SCOPED
  target_ssot_changed: false
  python_test_plan_impact:
    python-test:
      new_test_file: "scripts/ci/tests/test_runtime_dependency_smoke.py"
      already_in_targets: true
      target_entry: "scripts/ci/tests/"
      replan_required: false
    contract_artifact:
      new_ci_steps: 2
      position: "python-test job, before uv sync"
  baseline_inputs:
    ci_runtime_baseline_v1_available: false
    run_count: 0
    p50_p95_ready: false
  artifact_consistency:
    ci_test_selection_v1_checked: true
    missing_pytest_args: []
  risk_flags:
    - "isolated smoke は uv が PATH に存在しないと失敗する — python-test job は setup-python-uv で uv を確保済み"
  follow_up_required: []
```

## CI_TEST_PERFORMANCE_DECISION_V1 (#1193)

```yaml
CI_TEST_PERFORMANCE_DECISION_V1:
  schema: CI_TEST_PERFORMANCE_DECISION_V1
  issue_number: 1193
  pr_number: null
  decision_scope: ci_change
  changed_paths:
    - scripts/ci/check_python_invocation_policy.py
    - scripts/ci/python_invocation_policy_exceptions.json
    - scripts/ci/tests/test_python_invocation_policy.py
    - scripts/ci/fixtures/python_invocation_policy/
    - .github/workflows/ci.yml
    - .github/workflows/check-hook-integrity.yml
    - package.json
    - .claude/skills/**/SKILL.md
    - docs/dev/agent-session-hotspots.md
    - docs/dev/agent-skill-boundaries.md
    - docs/dev/schema-governance.md
    - docs/dev/session-recording-policy.md
    - docs/dev/test-lane-policy.md
    - docs/dev/workflow.md
  lane_classification:
    fast_static:
      applicable: true
      evidence:
        - "Ruff/lint 対象の Python checker (scripts/ci/check_python_invocation_policy.py) を追加; docs/markdown 変更を含む"
      required_commands:
        - pnpm typecheck
        - pnpm lint
    python_unit:
      applicable: true
      evidence:
        - "scripts/ci/tests/test_python_invocation_policy.py を新規追加 — python-test-plan.json の scripts/ci/tests/ target に含まれる (SSOT 不変)"
      required_commands:
        - "uv run --locked pytest scripts/ci/tests/test_python_invocation_policy.py -q"
    contract_artifact:
      applicable: true
      evidence:
        - "governed surface (.github/workflows/** / docs/dev/** / .claude/skills/**/SKILL.md / package.json) の invocation 文字列を uv run --locked へ移行"
        - "ci.yml python-test job に invocation policy の static checker step を pytest 群の前に追加"
        - "iteration-1 修正: ci.yml の Kill Switch smoke step / package.json session-recording:smoke / session-recording-policy.md doc 実行例の kill_switch_runtime_smoke.py (import yaml, HARD) と check_session_recording_policy.py (import yaml) の bare 呼び出し 3 件を uv run --locked へ移行。両 script を stdlib_only 例外 registry から削除し、exception 素通りでなく migration で AC7 violation 0 を達成"
      required_commands:
        - "uv run --locked python3 scripts/ci/check_python_invocation_policy.py --strict"
    integration:
      applicable: false
      evidence:
        - "src/ / UI コンポーネント変更なし (pnpm build / pnpm test は回帰確認のために実行するが本変更起因のレーンではない)"
      required_commands: []
  affected_lanes:
    - fast_static
    - python_unit
    - contract_artifact
  added_steps:
    - id: python_invocation_policy_check
      job: python-test
      position: before_pytest
      command: "uv run --locked python3 scripts/ci/check_python_invocation_policy.py --strict"
      purpose: "governed surface の non-locked invocation / 未登録 direct interpreter を fail-closed に検出 (AC7)"
  expected_cost:
    python_invocation_policy_check: "<2s (text scan of ~70 governed files)"
    total_added_per_run: "<2s"
  required_evidence:
    - TEST_VERDICT_MACHINE
    - CI_CHECK_RUN_SCOPED
  target_ssot_changed: false
  python_test_plan_impact:
    python-test:
      new_test_file: "scripts/ci/tests/test_python_invocation_policy.py"
      already_in_targets: true
      target_entry: "scripts/ci/tests/"
      replan_required: false
  baseline_inputs:
    ci_runtime_baseline_v1_available: false
    run_count: 0
    p50_p95_ready: false
  artifact_consistency:
    ci_test_selection_v1_checked: true
    missing_pytest_args: []
  risk_flags:
    - ".claude/settings.json の Bash allowlist は `uv run python3 .claude/skills/<skill>/scripts/*.py` 前提のため、SKILL.md を `uv run --locked` へ移行した結果 auto-approve 被覆が縮退する — settings.json は Allowed Paths 外のため本 PR では未変更。follow-up で allowlist に `--locked` 形を追加する必要がある"
  follow_up_required:
    - ".claude/settings.json の permission allowlist に `Bash(uv run --locked python3 .claude/skills/<edit-issue|post-merge-cleanup|create-issue>/scripts/*.py *)` を追加し、SKILL.md 移行後の auto-approve 被覆を回復する"
```

## invocation policy checker の shell 文法解析方針（#1193 OWNER 強化）

`scripts/ci/check_python_invocation_policy.py` は governed surface 上の 1 行 /
`run:` block を simple command 単位へ分割し（`&&` / `||` / `;` / `|` を quote・
substitution・`${{ }}` 認識のうえ分割）、全 simple command を分類する。

### 外部 shell parser を採用しない理由（fail-closed custom splitter）

- bashlex / mvdan-sh 等の外部 shell parser は **新規 runtime dependency を追加**する。
  本 child の Out of Scope（`pyyaml` / `jsonschema` の runtime 移設）と同様に、
  policy checker のためだけに runtime 依存を増やすのは親 #1145 の dependency 最小化方針に反する。
- checker は CI bootstrap 文脈（uv sync 前）でも `python3` 直実行で走る必要があり、
  解析対象は実コマンドというより「governed surface 上の invocation 文字列」である。
  完全な shell 文法エミュレーションは過剰であり、保守的な custom splitter で十分かつ安全。
- したがって custom splitter は **未対応文法を握りつぶさず fail-closed** にする:
  `shlex.split()` 失敗時の `.split()` fallback は廃止し、launcher を含む解析不能な
  simple command（閉じない command/process substitution 等）は `unsupported_shell_grammar`
  violation として報告する。command/process substitution `$(...)` / `<(...)` /
  backtick は内部コマンドを再帰分類し、隠れた違反が false green にならないようにする。

### direct interpreter / inline code の判定

- direct interpreter 例外は `exact_argv`（argv トークンの完全列）との **exact full-argv 一致**で
  照合する（prefix / glob / regex 不使用）。`scope: stdlib_only` の script 例外は
  `proof: stdlib_import_scan` により対象 script を AST import scan し、非 stdlib import
  （再帰的に stdlib-only と確認できる repo-local module は除く）を含む場合は exception 不成立＝violation とする。
- `python3 -`（heredoc/stdin）/ `python3 -c` は broad-prefix 例外で許可しない。
  heredoc body / `-c` code string を AST import scan し、非 stdlib import を含む場合は violation とする。
  `uv run --locked python -c` / `uv run --locked python -` は lockfile 下で依存解決されるため許可する。
- Markdown では shell 言語（` ```bash ` / ` ```sh ` / 無タグ等）の fenced block のみを
  shell として走査し、` ```yaml ` / ` ```json ` / ` ```markdown ` 等のデータ/prose block は対象外とする。
