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

> **注意:** 現行の `e2e` は `if-no-files-found: warn` で artifact を upload する。
> `required evidence` と位置づけるなら `error` にすべきだが、現時点では warn 運用。
> 変更スコープは別 Issue。

### 8.2 Retention Policy

| artifact 名 | retention-days | 根拠 |
|---|---|---|
| `playwright-report` | 30 日 | visual regression baseline の参照期間 |
| `test-results` | 30 日 | visual regression baseline の参照期間 |
| `ci_test_selection` | デフォルト（90 日） | agent-ops 証跡。長期保存不要 |

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
| artifact missing を merge-ready gate に含める（`if-no-files-found: error` 化） | 未実装 | **no** | 別 Issue |

---

## 11. 変更ログ

| 日付 | 内容 |
|---|---|
| 2026-06-14 | 初版作成（#894） |
| 2026-06-14 | e2e artifact conditional 化・PR hygiene inventory 追加・Implementation Status Matrix 追加（#894 adversarial review 対応） |
