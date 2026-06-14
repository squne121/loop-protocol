# CI Performance Baseline

このドキュメントは CI 各 job / step の実測所要時間を記録し、高速化 PR で before/after 比較できる状態を維持するための運用規約を定義する。

関連 Issue: #895
関連スキーマ: `ci_runtime_baseline_v1`（本ドキュメント内で定義）
CI verdict との責務分離: #898 `ci_verdict_summary_v2` が merge-ready 判定を担う（本ドキュメントは duration evidence のみ）

## ci_runtime_baseline_v1 スキーマ

artifact 名: `ci-runtime-baseline-<job>-<run_attempt>`

```json
{
  "schema": "ci_runtime_baseline_v1",
  "run_id": "<github.run_id>",
  "run_attempt": "<github.run_attempt>",
  "head_sha": "<pr head SHA または push SHA>",
  "merge_sha": "<github.sha>",
  "job": "<job name>",
  "runner_image": "<ImageOS>/<ImageVersion>",
  "measurement_method": "date_plus3N_ms",
  "measurements": [
    {
      "step_id": "pnpm_install",
      "phase_id": "pnpm_install",
      "status": 0,
      "elapsed_ms": 12345,
      "run_id": "<github.run_id>",
      "run_attempt": "<github.run_attempt>",
      "head_sha": "<pr head SHA または push SHA>",
      "merge_sha": "<github.sha>",
      "job": "<job name>",
      "runner_image": "<ImageOS>/<ImageVersion>",
      "measurement_method": "date_plus3N_ms"
    }
  ]
}
```

各 measurement record には top-level フィールド（`run_id`, `run_attempt`, `head_sha`, `merge_sha`, `job`, `runner_image`, `measurement_method`）が展開される。これにより artifact を job 単位でフラット化した場合でも各 record が単独で識別可能になる（AC3）。

### フィールド定義

| フィールド | 型 | 説明 |
|---|---|---|
| `schema` | string | `"ci_runtime_baseline_v1"` 固定 |
| `run_id` | string | GitHub Actions の `run_id` |
| `run_attempt` | string | GitHub Actions の `run_attempt` |
| `head_sha` | string | PR head SHA（push 時は `github.sha` と同値） |
| `merge_sha` | string | `github.sha`（merge commit SHA） |
| `job` | string | job 名（`typecheck` / `lint` / `test` / `build` / `e2e` / `python-test` / `actionlint`） |
| `runner_image` | string | `${ImageOS}/${ImageVersion}` |
| `measurement_method` | string | `"date_plus3N_ms"`（`date +%s%3N` による ms 計測） |
| `measurements[].step_id` | string | ステップ識別子（granular; python-test では pytest_edit_issue_tests 等） |
| `measurements[].phase_id` | string | 比較基軸（python-test の pytest 群では `pytest_skills` に統一; 他の job では `step_id` と同値） |
| `measurements[].status` | int | コマンドの exit status |
| `measurements[].elapsed_ms` | int | 経過時間（ミリ秒） |
| `measurements[].run_id` | string | top-level `run_id` を展開（AC3: record 単独での識別を可能にする） |
| `measurements[].run_attempt` | string | top-level `run_attempt` を展開 |
| `measurements[].head_sha` | string | top-level `head_sha` を展開 |
| `measurements[].merge_sha` | string | top-level `merge_sha` を展開 |
| `measurements[].job` | string | top-level `job` を展開 |
| `measurements[].runner_image` | string | top-level `runner_image` を展開 |
| `measurements[].measurement_method` | string | top-level `measurement_method` を展開 |

### stable phase_id 一覧（#896 以降の比較基軸）

| phase_id | 対象コマンド | 対象 job |
|---|---|---|
| `pnpm_install` | `pnpm install --frozen-lockfile` | typecheck / lint / test / build / e2e / python-test |
| `pnpm_typecheck` | `pnpm typecheck` | typecheck |
| `pnpm_lint` | `pnpm lint` | lint |
| `pnpm_manifest_check` | `pnpm manifest:check` | test |
| `pnpm_test` | `pnpm test` | test |
| `pnpm_build` | `pnpm build` | build |
| `pnpm_build_e2e` | `VITE_E2E_MODE=true pnpm build` | e2e |
| `playwright_install` | `pnpm playwright:install:ci` | e2e |
| `test_e2e_ci` | `pnpm test:e2e:ci` | e2e |
| `uv_python_install` | `uv python install` | python-test |
| `uv_sync` | `uv sync --locked --group dev` | python-test |
| `pytest_skills` | pytest（skills 群 14 ステップ合計の stable phase_id） | python-test |
| `actionlint_install` | actionlint バイナリのダウンロード・インストール | actionlint |
| `actionlint` | `actionlint` | actionlint |

### python-test の step_id と phase_id の関係

python-test job では pytest ステップが複数あり、各ステップの `step_id` は granular（例: `pytest_edit_issue_tests`, `pytest_create_issue_tests` 等）だが、
`phase_id` は全て `pytest_skills` に統一されている。これにより #896 以降の比較で pytest 群全体の所要時間を単一 phase_id で集計できる。

| step_id（granular） | phase_id（stable） |
|---|---|
| `pytest_edit_issue_tests` | `pytest_skills` |
| `pytest_create_issue_tests` | `pytest_skills` |
| `pytest_create_issue_scripts` | `pytest_skills` |
| `pytest_ssot_discovery` | `pytest_skills` |
| `pytest_hook_tests` | `pytest_skills` |
| `pytest_issue_contract_review_scripts` | `pytest_skills` |
| `pytest_issue_contract_review_tests` | `pytest_skills` |
| `pytest_pr_review_judge` | `pytest_skills` |
| `pytest_open_pr` | `pytest_skills` |
| `pytest_impl_review_loop` | `pytest_skills` |
| `pytest_review_issue` | `pytest_skills` |
| `pytest_issue_refinement_loop` | `pytest_skills` |
| `pytest_schemas` | `pytest_skills` |
| `pytest_context_mode` | `pytest_skills` |

## run_timed wrapper 仕様

`run_timed <step_id> <command...>` 形式で呼び出す。

- `date +%s%3N` で start_ms を記録する（`SECONDS` は使わない）
- コマンドを `set +e` で実行し、exit status を保存する
- `set -e` を復元する
- elapsed_ms = end_ms - start_ms を計算する
- JSON 行を measurements ファイルに append する
- exit status を変更しない（CI failure を mask しない）

## artifact upload 方針

- `if: ${{ !cancelled() }}` を使い、step failure でも upload する
- `if-no-files-found: warn` でファイル未生成時も CI failure にしない
- `retention-days: 30`

## $GITHUB_STEP_SUMMARY 出力仕様

- 各 job の最終 summary step で 1 回 append する（`>>`）
- e2e job の Visual regression evidence summary を上書きしない（e2e の ci-runtime summary を先に出力し、Visual regression evidence はその後に続く）
- 出力フォーマット: Markdown テーブル（step_id / elapsed_ms / status）

## bootstrap 3 runs と decision baseline 20 runs の使い分け

### bootstrap 3 runs（初期キャリブレーション）

- 目的: スキーマ・計測方式の動作確認
- 実施タイミング: 本 PR マージ後の最初の 3 run
- 判断基準: artifact の構造が正しいこと、所要時間がオーダー感として妥当であること
- この段階では P50 / P95 を決定しない

### decision baseline 20 runs（高速化 PR の比較基準）

- 目的: 安定した before 値の確定
- 実施タイミング: bootstrap 3 runs 完了後、main ブランチ通常 push 20 run 分
- 判断基準:
  - **P50（中央値）**: 高速化効果の主要指標
  - **P95（95 パーセンタイル）**: tail latency の退行検知指標
- 利用方法: 高速化 PR では `before` に 20 runs の P50 / P95 を記録し、`after` と比較する

### 比較レポートフォーマット例

```
| phase_id        | before P50 (ms) | before P95 (ms) | after P50 (ms) | after P95 (ms) | delta P50 |
|---|---|---|---|---|---|
| pnpm_install    | 45000           | 52000           | 12000          | 15000          | -73%      |
```

## 責務分離: ci_runtime_baseline_v1 vs ci_verdict_summary_v2

| 関心事 | 担当 |
|---|---|
| job / step の所要時間記録 | `ci_runtime_baseline_v1`（本ドキュメント、#895） |
| merge-ready 判定（required / advisory / evidence）| `ci_verdict_summary_v2`（#898） |
| CI failure / pass の判断 | `ci_verdict_summary_v2`（#898） |

`ci_runtime_baseline_v1` は duration evidence であり、merge-ready verdict を含まない。
verdict 判定は #898 の `ci_verdict_summary_v2` に委譲する。

## 計測対象外

以下は計測対象外（CI overhead として扱う）:

- `actions/checkout`
- `pnpm/action-setup`
- `actions/setup-node`
- `astral-sh/setup-uv`
- `actions/upload-artifact`（upload 自体の所要時間）

### python-test job の未計測ガード群

python-test job 内の以下のステップは計測対象外（`measurements.jsonl` に記録しない）:

- `Install ripgrep`（`sudo apt-get install -y ripgrep`）
- `Verify Python version`（Python バージョン確認）
- `run: uv run --locked python .claude/scripts/check_secret_policy.py ...`
- `run: uv run --locked python .claude/scripts/check_session_recording_policy.py ...`
- `Check InputCommand docs schema`（`scripts/check_input_command_schema.py`）
- `Check visual artifact pipeline wiring`（`scripts/check-visual-artifact-pipeline.py`）
- `Verify hook test discovery exclusions`（hook テスト discovery 除外確認スクリプト）
- `Kill Switch smoke test`（`kill_switch_runtime_smoke.py`）
- `Secret exposure scan (production scripts)`（`secret_exposure_scanner.py`）
- `Secret exposure scan (clean fixtures)`（`secret_exposure_scanner.py`）
- `Generate ci_test_selection/v1 artifact`（`generate_ci_test_selection_artifact.py`）

これらは CI ガード（整合性チェック・セキュリティスキャン）であり、runtime duration の比較基準とはしない。
