# CI Operations Design

**Status:** normative design / decision record
**Issue:** #894
**Allowed Paths:** `docs/dev/ci-operations.md`

> **Note:** この文書は enforcement 実装ではなく normative design / decision record です。
> `.github/workflows/ci.yml` や `ci_verdict_summary.py` は変更しません。
> 現行コードと設計文書が乖離する箇所は明記し、実装修正スコープを参照します。

---

## 1. 目的

AI エージェントが merge-ready を正確に判定するための CI 運用設計の SSOT を定義する。

GitHub branch protection 上の "required check" と LOOP_PROTOCOL 独自の "agent merge-ready" を明示分離し、`skipped/neutral/head_sha=null` 等の曖昧な状態を誤判定しない契約を記述する。

---

## 2. Scope と責務分界

| スコープ | Issue | 内容 |
|---|---|---|
| Allowed Paths pre-commit gate の実装 | #846 | pre-commit フックで Allowed Paths 違反を検出するゲートの実装スコープ |
| `ci_verdict_summary.py` の実装修正 | #863 | `head_sha=null + skipped` の偽陽性修正・`neutral/skipped/stale` の分類見直し実装スコープ |
| CI 運用設計の normative 文書 | #894 (本 Issue) | 本文書。enforcement 実装ではない |
| `ci_verdict_summary_v2` artifact・schema・workflow 統合 | #898 | V2 artifact/schema の normative 定義・workflow 統合・consumer 指定 |

この文書が定義するポリシーが現行 `ci_verdict_summary.py` と競合する箇所は、各セクションで明記し #863 を参照する。

---

## 3. CI Job Inventory（実 Job 名）

`.github/workflows/ci.yml` 上の実際の job 名を使用する。チェック名を発明しない。

| job | taxonomy | required/advisory/evidence | PR-head evidence required | artifact | merge-ready rule |
|---|---|---|---|---|---|
| `typecheck` | product quality gate | required | yes | なし | expected head で success 必須 |
| `lint` | product quality gate / PR hygiene | required | yes | なし | expected head で success 必須 |
| `test` | product quality gate | required | yes | なし | expected head で success 必須 |
| `build` | product quality gate | required | yes | dist build ログ | expected head で success 必須 |
| `e2e` | runtime / browser evidence gate | required + evidence | yes | `playwright-report`（required）, `test-results`（conditional） | expected head で success 必須。artifact は Section 8.1 参照 |
| `python-test` | agent-ops / skill regression gate | required + evidence | yes | `ci_test_selection` | expected head で success 必須 |
| `actionlint` | agent-ops / CI lint gate | required | yes | なし | expected head で success 必須 |
| `ci-verdict-summary` | aggregator / artifact producer | evidence | yes | `ci_verdict_summary_v2` | expected head で success 必須。artifact は Section 11.3 参照 |

**注意:** `e2e` は単なる runtime gate ではなく、Playwright report / test-results / visual evidence summary を持つ evidence producer でもある。現在の workflow は `playwright-report` と `test-results` を `!cancelled()` 条件で upload し、環境 fingerprint や artifact URL を summary に書く。`test-results` は visual mismatch 等で差分が発生した場合のみ artifact が生成されるため、conditional evidence として扱う（Section 8.1 参照）。

### 3.1 PR Hygiene / Retrospective Checks（Check Japanese Content workflow）

`.github/workflows/ci.yml` 外のチェックも merge-ready 判定に現れる。

| check / job 名 | workflow | taxonomy | required/advisory/evidence | PR-head evidence required | skipped 扱い | merge-ready rule |
|---|---|---|---|---|---|---|
| `PR Body Japanese Check` | `Check Japanese Content` | PR hygiene gate | required | yes | block（条件不一致時 failure） | expected PR head で success 必須 |
| `PR Review Japanese Check (retrospective)` | `Check Japanese Content` | retrospective hygiene | advisory/excluded | no | allowlisted exclude（head_sha=null 可） | required PR-head evidence に数えない |
| `Issue Comment Japanese Check (retrospective)` | `Check Japanese Content` | retrospective hygiene | advisory/excluded | no | allowlisted exclude（head_sha=null 可） | required PR-head evidence に数えない |
| `Issue Body Japanese Check (retrospective)` | `Check Japanese Content` | retrospective hygiene | advisory/excluded | no | allowlisted exclude（head_sha=null 可） | required PR-head evidence に数えない |

> **注意:** `PR Review Japanese Check (retrospective)` / `Issue Comment Japanese Check (retrospective)` / `Issue Body Japanese Check (retrospective)` は PR CI で `skipped` になる。Section 6.1 の allowlist ルールに従い、`head_sha=null + skipped` として required PR-head evidence から除外する。

---

## 4. GitHub Branch Protection Semantics vs Agent Merge-Ready Semantics

### 4.1 GitHub Branch Protection Semantics

GitHub の branch protection 上の "required status check" は、GitHub プラットフォームが merge gate として判断する状態に基づく。

GitHub 公式仕様では、required status check は次の conclusion で満たされ得る:
- `success`
- `skipped`（条件付き）
- `neutral`（設定依存）

ただし **LOOP_PROTOCOL の agent merge-ready ではこの扱いを採用しない**。詳細は 4.2 を参照。

### 4.2 Agent Merge-Ready Semantics（LOOP_PROTOCOL 固有）

AI エージェントが merge-ready と判断してよいのは、以下の条件をすべて満たす場合のみ:

1. required job の全てが expected PR head SHA で `success` を返している
2. `head_sha` が expected PR head SHA と一致している（mismatch は stale として block）
3. `head_sha=null` の check は required PR-head evidence として数えない（section 6 参照）
4. `queued/in_progress/waiting/requested/pending` の check がない（wait_for_ci）
5. evidence artifact が required な job（`e2e`, `python-test`）では artifact が利用可能

```
agent merge-ready = すべての required job が (head_sha == expected_head_sha) AND (conclusion == success)
```

---

## 5. CI Status / Conclusion 定義表

### 5.1 Check Run Status（実行状態）

| status | 意味 | agent merge-ready verdict | next action |
|---|---|---|---|
| `queued` | キューに入っている | block | wait_for_ci |
| `in_progress` | 実行中 | block | wait_for_ci |
| `completed` | 完了（conclusion を参照） | conclusion 依存 | conclusion 表を参照 |
| `waiting` | 依存ジョブ待機中 | block | wait_for_ci |
| `requested` | 実行要求済み | block | wait_for_ci |
| `pending` | pending | block | wait_for_ci |

### 5.2 Check Run Conclusion（完了結論）

| conclusion | GitHub/platform の意味 | agent merge-ready verdict | next action |
|---|---|---|---|
| `success` | 成功 | pass（head_sha が一致する場合） | なし |
| `failure` | 失敗 | block | ログ確認・修正 |
| `timed_out` | タイムアウト | block | 再実行または原因調査 |
| `action_required` | 手動アクション要求 | block | 人間判断 |
| `cancelled` | キャンセル済み | block（stale run の場合は除外可。section 7 参照） | 再実行または wait |
| `neutral` | 中立（GitHub は required check として許容する場合あり） | **not pass**（required evidence には不可） | manual review |
| `skipped` | スキップ（GitHub は required check として許容する場合あり） | **not pass**（required evidence には不可。allowlisted job のみ exclude 可。section 6 参照） | 分類確認 |
| `stale` | 古い結果 | block | head SHA を更新 |

> **現行コードとの乖離（#863 参照）:**
> 現行 `ci_verdict_summary.py` は `neutral` / `skipped` / `stale` を fail bucket に分類し、
> `determine_check_verdict` でも `neutral` / `skipped` を `failed` として扱う。
> これは `head_sha=null + skipped` の retrospective / conditional job（`deploy-main`, `cleanup-pr`,
> `issue-body-japanese` 等）で exit 10 偽陽性を起こす。修正スコープは #863。

---

## 6. `head_sha=null` / `head_sha mismatch` / `expected_head_sha` の扱い

| 観測状態 | 意味 | agent merge-ready verdict | next action |
|---|---|---|---|
| `head_sha == expected_head_sha` かつ `success` | 現在 PR head の証跡あり | pass | なし |
| `head_sha != expected_head_sha`（mismatch） | 別コミットの証跡（stale） | block | head SHA を更新・再実行 |
| `head_sha=null` かつ `conclusion=skipped` | PR head の証跡なし | exclude（allowlisted job のみ） / not pass | section 6.1 参照 |
| `head_sha=null` かつ `conclusion=success` | PR head の証跡なし | block（source 確認要） | 人間または source 調査 |
| `head_sha=null` かつ `conclusion=failure/cancelled/neutral` | PR head の証跡なし | block | 人間または source 調査 |
| `expected_head_sha` 未指定 | race guard 未設定 | block | expected_head_sha を指定してから実行 |

### 6.1 `head_sha=null + skipped` の allowlist ルール

`head_sha=null + skipped` は以下の条件を**すべて**満たす job のみ excluded（除外）とする:

1. **retrospective / conditional job** である（e.g., `deploy-main`, `cleanup-pr` — PR CI では条件によりスキップされる job）
2. **required PR-head evidence job でない**（`typecheck`, `lint`, `test`, `build`, `e2e`, `python-test`, `actionlint` はこの条件を満たさない）
3. **明示的に allowlisted** である（暗黙の除外は禁止）

上記条件を満たさない `head_sha=null + skipped` は required PR-head evidence に数えない。
`head_sha=null + skipped` を pass として扱うことは禁止する。

> **現行コードとの乖離（#863 参照）:**
> 現行 `ci_verdict_summary.py` では `head_sha=null + skipped` の retrospective job が
> 偽陽性（exit 10 失敗）を起こす。allowlist による exclude 実装は #863 のスコープ。

---

## 7. concurrency / cancellation の扱い

現在の `.github/workflows/ci.yml` は `concurrency.cancel-in-progress: true` を設定している。
これにより古い PR head の CI run がキャンセルされ得る。

| 状況 | agent merge-ready verdict | next action |
|---|---|---|
| `cancelled` かつ stale/superseded run（旧 head） | 除外可（当該 head の最新 run を参照） | 最新 run を確認 |
| `cancelled` かつ current head の required job | block | 再実行または新 run 完了待ち |
| `pending` / current head の run が未完了 | block | wait_for_ci |
| workflow / path filter により required check が pending のまま | block（GitHub 上も merge blocking になり得る） | 人間確認または rerun |

---

## 8. Evidence Artifact Policy

### 8.1 Required evidence artifact の missing policy

| job | artifact 名 | if-no-files-found | missing 時の verdict |
|---|---|---|---|
| `e2e` | `playwright-report` | `warn` | 現行: warn のみ（block しない）。設計上は evidence job として artifact 可用性を確認すべき |
| `e2e` | `test-results` | `warn` | 同上 |
| `python-test` | `ci_test_selection` | `ignore` | ignore（optional evidence） |
| `ci-verdict-summary` | `ci_verdict_summary_v2` | `error` | V2 artifact は missing を error とする（Section 11.3 参照） |

> **注意:** 現行の `e2e` は `if-no-files-found: warn` で artifact を upload する。
> `required evidence` と位置づけるなら `error` にすべきだが、現時点では warn 運用。
> 変更スコープは別 Issue。

### 8.2 Retention Policy

| artifact 名 | retention-days | 根拠 |
|---|---|---|
| `playwright-report` | 30 日 | visual regression baseline の参照期間 |
| `test-results` | 30 日 | visual regression baseline の参照期間 |
| `ci_test_selection` | デフォルト（90 日） | agent-ops 証跡。長期保存不要 |
| `ci_verdict_summary_v2` | 30 日 | AI エージェントの CI 判定証跡 |

`!cancelled()` 条件で upload することで、テスト失敗時にも証跡を保持する（success / failure 両方で artifact を取得できる）。

---

## 9. CI 高速化方針

CI 高速化は次の順序で進める:

1. **baseline measurement** — 直近 N=20 CI runs の job duration P50/P95 を計測
2. **setup 重複削減** — 各 job で重複する `pnpm install` 等の最適化
3. **E2E 安定化** — flaky test の検出・修正
4. **cache 拡張** — GitHub 標準 cache の hit/miss と restore-key 設計の最適化

### S3 / external cache 採用条件

S3 / external cache は次を**すべて**満たす別 Issue まで採用しない:

- 直近 N=20 CI runs の job duration P50/P95 を取得済み
- setup / install 部分が全体時間の X% 以上を占めると計測済み
- GitHub 標準 cache（`actions/setup-node` の pnpm cache）の hit/miss と restore-key 設計を確認済み
- cache path に `.env`, token, credentials, generated secrets が含まれないことを確認済み
- 外部サービス利用のため別 Issue で security / cost / invalidation policy のレビューを完了済み

---

## 10. 関連 Issue / ADR

| Issue | スコープ |
|---|---|
| #846 | Allowed Paths pre-commit gate の実装スコープ（pre-commit フックで Allowed Paths 違反を検出） |
| #863 | `ci_verdict_summary.py` の実装修正スコープ（`head_sha=null + skipped` 偽陽性・`neutral/skipped/stale` 分類見直し） |
| #894 | 本文書（CI 運用設計の normative design / decision record） |
| #898 | `ci_verdict_summary_v2` artifact・schema・workflow 統合（Section 11 参照） |

---

## 10.5 Implementation Status Matrix

この文書の設計ポリシーと現行実装の対応状況を示す。`operative now? = no` の行は normative design のみであり、実装済みと誤読しないこと。

| policy | 現行実装 | operative now? | follow-up |
|---|---|---|---|
| `head_sha=null + skipped` を allowlisted retrospective job として exclude | 未実装。現行 `ci_verdict_summary.py` は `skipped` を failed 扱い | **no** | #863 |
| `neutral` を required evidence として not pass だが fail とは分離 | 未実装。現行 `ci_verdict_summary.py` は `neutral` を failed bucket に分類 | **no** | #863 |
| `e2e` の `playwright-report` を required evidence artifact として扱う | 部分実装。`if-no-files-found: warn` のため artifact missing は CI failure にならない | **partial** | 別 Issue |
| `e2e` の `test-results` を conditional evidence artifact として扱う | 部分実装。同上（`if-no-files-found: warn`）。visual mismatch 時のみ artifact が生成される | **partial** | 別 Issue |
| `PR Body Japanese Check` を PR hygiene required gate として扱う | 実装済み。`Check Japanese Content` workflow として運用中 | **yes** | — |
| `PR Review Japanese Check (retrospective)` 等を allowlisted excluded として扱う | 未明示。Section 6.1 の allowlist ルールとして本文書で定義 | **partial（本文書で定義）** | #863 |
| artifact missing を merge-ready gate に含める（`if-no-files-found: error` 化） | 未実装（`e2e`/`python-test`）。`ci_verdict_summary_v2` artifact は `if-no-files-found: error`（#898） | **partial** | #898 実装済み |
| `ci_verdict_summary_v2` artifact を `ci-verdict-summary` job で生成・upload する | 実装済み（#898） | **yes** | — |

---

## 11. ci_verdict_summary_v2 Schema

### 11.1 目的と位置づけ

`ci_verdict_summary_v2` は AI エージェントの CI 待機・判定を安定化するための machine-readable artifact。

- **consumer canonical owner:** `pr-review-judge`（`impl-review-loop` は直接 parse しない）
- **producer:** `ci-verdict-summary` aggregator job（`.github/workflows/ci.yml`）
- **移行方針:** V2 preferred / V1 fallback（Section 12 参照）
- **V1 semantics 不変:** `ci_verdict_summary.py` の V1 verdict semantics は変更しない（#863/#911 の責務）

### 11.2 JSON Schema 定義

```yaml
schema: ci_verdict_summary_v2
schema_version: 2
generated_at: ISO-8601         # UTC timestamp
repository: string             # "owner/repo"
workflow_run_id: integer
workflow_run_attempt: integer
event_name: string             # e.g. "pull_request"
expected_head_sha: string      # race guard: expected PR head SHA
head_sha: string | null        # actual PR head SHA at artifact generation time

overall_status:
  # merge-ready の総合判定
  enum:
    - merge_ready              # 全 required check が expected head で success
    - blocked                  # 1 件以上の required check が fail/cancelled/neutral/skipped
    - pending                  # 1 件以上の required check が queued/in_progress/waiting
    - stale_head_sha           # head_sha が expected_head_sha と不一致
    - gh_error                 # GitHub API エラーまたは未知のステータス
    - no_required_evidence     # required evidence artifact が存在しない

next_action:
  # エージェントへの推奨アクション
  enum:
    - none                             # merge_ready。アクション不要
    - wait_for_ci                      # pending 状態。CI 完了を待つ
    - inspect_failed_log_artifacts     # blocked。ログ・artifact を確認する
    - refresh_head_sha                 # stale_head_sha。expected_head_sha を更新する
    - rerun_failed_check               # cancelled。check を再実行する
    - manual_review_gh_error           # gh_error。人間による確認が必要
    - manual_review_no_required_evidence  # no_required_evidence。人間による確認が必要

checks:
  - name: string               # check run / job 名
    workflow: string           # workflow 名（"ci", "Check Japanese Content" 等）
    check_run_id: integer | null
    status:
      # check run の実行状態
      enum: [queued, in_progress, completed, waiting, requested, pending, null]
    conclusion:
      # check run の完了結論（status=completed 時）
      enum: [success, failure, neutral, skipped, stale, timed_out, cancelled, action_required, null]
    classification:
      # check の役割分類（(workflow, check name) tuple で決定）
      enum:
        - required             # merge-ready に必須。expected head で success 必須
        - advisory             # 参考情報。blocking しない
        - evidence             # required + artifact 生成（e2e, python-test, ci-verdict-summary 等）
        - excluded             # allowlisted retrospective/conditional check（head_sha=null+skipped 除外可）
        - unknown              # 未分類。保守的扱い
    head_sha: string | null    # check run が実行された head SHA
    expected_head_sha: string  # race guard 値（artifact 全体と同一）
    head_sha_match: boolean    # head_sha == expected_head_sha
    blocking_merge_ready: boolean   # このチェックが merge-ready を阻害しているか
    failure_reason:
      # blocking_merge_ready=true の理由（classification と conclusion の組み合わせ）
      enum:
        - none                         # blocking なし
        - failed                       # conclusion=failure/timed_out/action_required
        - pending                      # status が未完了（queued/in_progress 等）
        - cancelled_current_head       # current head で cancelled
        - stale_head_sha               # head_sha が expected_head_sha と不一致
        - skipped_required             # required/evidence check が skipped（not pass）
        - neutral_required             # required/evidence check が neutral（not pass）
        - missing_required_artifact    # evidence artifact が missing
        - gh_error                     # GitHub API エラーまたは未知のステータス
        - no_required_evidence         # required evidence が存在しない
    artifact_refs:
      - name: string
        artifact_id: integer | null
        artifact_url: string | null
        artifact_digest: string | null
        required_for_merge_ready: boolean
        missing_policy:
          enum: [error, warn, ignore]  # artifact missing 時のポリシー
```

### 11.3 invariants（不変条件）

以下は `ci_verdict_summary_v2` の generator が保証しなければならない不変条件。

#### required / evidence invariant

1. `skipped` 結論は `required` / `evidence` check で `pass` とみなさない
   - `failure_reason = skipped_required`、`blocking_merge_ready = true`
2. `neutral` 結論は `required` / `evidence` check で `pass` とみなさない
   - `failure_reason = neutral_required`、`blocking_merge_ready = true`
3. `head_sha=null` の check は required PR-head evidence にカウントしない
   - `required` / `evidence` 分類かつ `head_sha=null` → `blocking_merge_ready = true`
   - `excluded` 分類かつ `head_sha=null + skipped` → `blocking_merge_ready = false`（allowlist のみ）

#### head_sha invariant

4. `head_sha_match = (head_sha is not null) AND (head_sha == expected_head_sha)`
5. `head_sha != expected_head_sha`（mismatch） → `failure_reason = stale_head_sha`

#### overall_status invariant

6. `blocking_merge_ready = true` な check が 1 件でもある → `overall_status != merge_ready`
7. `overall_status = merge_ready` ↔ `all checks: blocking_merge_ready = false`

### 11.4 Step Summary 生成ルール

`$GITHUB_STEP_SUMMARY` への出力は `ci_verdict_summary_v2.json` から生成し、別計算しない。

必須表示項目:
- `overall_status`
- `next_action`
- `blocking_merge_ready = true` な check の一覧（blockers）
- artifact の upload URL / digest（artifact-id / artifact-digest）

---

## 12. CI_VERDICT_SUMMARY_V1 → V2 移行方針

### 12.1 移行ポリシー

| フェーズ | 方針 |
|---|---|
| 移行期間（現在） | **V2 preferred / V1 fallback**。V2 artifact が存在すれば V2 を使用。V2 が存在しない場合（旧 run）は V1 にフォールバック |
| V1 deprecation | V2 が CI に安定稼働し、consumer（pr-review-judge）が V2 に完全移行した後。別 Issue で決定 |
| V1 removal | V2 移行完了・consumer 移行後。別 Issue で決定 |

### 12.2 V1 → V2 migration table

| 項目 | CI_VERDICT_SUMMARY_V1 | ci_verdict_summary_v2 |
|---|---|---|
| schema 識別子 | `CI_VERDICT_SUMMARY_V1`（文書名） | `"schema": "ci_verdict_summary_v2"`, `"schema_version": 2` |
| artifact 名 | なし（V1 は artifact 化されていない） | `ci-verdict-summary-v2-{run_id}-{run_attempt}` |
| check 分類 | なし（V1 は分類概念なし） | `required / advisory / evidence / excluded / unknown` |
| head_sha フィールド | なし | `head_sha`, `expected_head_sha`, `head_sha_match` per-check |
| pending/skipped/neutral 扱い | V1: skipped/neutral を failed 扱い（#863 で修正中） | V2: `skipped_required` / `neutral_required` で明示分離 |
| allowlist | なし | `excluded` 分類で明示 allowlist |
| overall_status | なし（exit code のみ） | `merge_ready / blocked / pending / stale_head_sha / gh_error / no_required_evidence` |
| next_action | なし | `none / wait_for_ci / inspect_failed_log_artifacts / refresh_head_sha / ...` |
| Step Summary | V1 consumer が別生成 | V2 generator が同一 JSON から生成（別計算禁止） |
| consumer | `impl-review-loop`（直接 parse） | `pr-review-judge`（canonical owner）。`impl-review-loop` は直接 parse 禁止 |
| if-no-files-found | 適用外 | `error`（missing は CI failure） |
| retention-days | 適用外 | 30 日 |

### 12.3 フォールバック手順（consumer 向け）

```
1. ci-verdict-summary-v2-{run_id}-{run_attempt} artifact を取得
2. 取得成功 → V2 を使用（canonical）
3. 取得失敗（旧 run / V2 未生成） → CI_VERDICT_SUMMARY_V1 にフォールバック
4. V1 もなければ → gh_error として human_judgment_required
```

---

## 13. 変更ログ

| 日付 | 内容 |
|---|---|
| 2026-06-14 | 初版作成（#894） |
| 2026-06-14 | e2e artifact conditional 化・PR hygiene inventory 追加・Implementation Status Matrix 追加（#894 adversarial review 対応） |
| 2026-06-14 | ci_verdict_summary_v2 schema・invariant・移行方針・migration table を追加（#898） |
