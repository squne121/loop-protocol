---
name: issue-contract-review
description: 人間承認後・実装着手直前に、Issue contract（作業計画・コンテクスト）が指定通りで開発フローに沿って AI Agent が安全に着手できるかを **決定論的に** preflight する skill。VC が baseline で fail することと AC が決定論的に検証可能であることを確認する。Issue 内容・文脈レビューは review-issue / issue-refinement-loop の責務で、本 skill では扱わない。branch / PR / worktree を作る前に必ず通す。
---

# Issue Contract Review

実装着手直前の **preflight skill**。Issue 本文・コメント・VC・AC を読んで「このまま `implement-issue` に渡せるか」を決定論的に判定する。

責務範囲は **「指定通りの作業計画 / コンテクスト / 開発フロー適合性」の事前確認** に絞る。Issue 本文の品質（Outcome 抽象性・AC 検証可能性等の構造的レビュー）は `review-issue`、Issue 改善ループは `issue-refinement-loop` の責務であり、本 skill では再判定しない。

## Input

- `Issue番号` または `Issue URL`（必須）

## Procedure

### 1. Issue contract を取得

```bash
gh issue view <番号> --json title,body,labels,comments
```

### 2. 開発フロー適合性チェック（決定論的）

| 確認項目 | 判定 |
|---|---|
| **テンプレ準拠** | `.github/ISSUE_TEMPLATE/{種別}.yml` の必須セクションがすべて存在 |
| **state ラベル** | `state/needs-human` が付いていれば BLOCKED。それ以外の state ラベル（`state/blocked` 等）の有無は着手判定に影響しない |
| **Allowed Paths 明示** | `## Allowed Paths` が存在し、空でない |
| **VC 明示** | `## Verification Commands` が存在し、コマンドが 1 つ以上ある |
| **Stop Conditions 明示**（implementation のみ） | `## Stop Conditions` が 6 定型項目で埋まっている |

1 つでも fail なら **BLOCKED**。issue comment に「contract 不備」を投稿し、`issue-refinement-loop` を呼ぶことを提案する。

注: state ラベルチェックは `state/needs-human` 付与の有無のみを着手判定に用いる。`state/needs-human` が付いていない場合、その他の state ラベル（`state/blocked` 等）の有無は BLOCKED 判定に影響しない。着手可否の source of truth は Step 3 の open blocker / dependency 確認（GitHub native dependency / `Depends on #N` fallback）である。

### 3. blocker / dependency 全 close 確認（決定論的）

```bash
bash .claude/skills/issue-contract-review/scripts/check_blockers.sh <issue_number> <owner>/<repo>
```

| 結果 | 判定 |
|---|---|
| exit 0 / blocker なし | OK — 次ステップへ |
| exit 0 / 全 blocker closed | OK — 次ステップへ |
| exit 1 / blocker open あり | **BLOCKED** — human_escalation |
| exit 1 / native と `Depends on #N` 不一致 | **BLOCKED** — human_escalation |

- native dependency API (`/issues/<N>/dependencies/blocked_by`) を primary として使用する。
- native API が取得不可の場合のみ、Issue 本文の `Depends on #N` を fallback として使用する。
- native と `Depends on #N` が不一致の場合は自動判断せず human escalation とする。
- BLOCKED 時は issue comment に open blocker 番号・不一致内容を記載し、人間による確認を依頼する。go 判定は出さない。

Fallback `Depends on #N` parsing は専用セクション（例: `## Depends On`）の line-anchored 宣言文のみを対象とする:
`^- Depends on #<number>` または `^Depends on #<number>` の形式。
Delivery Rule の条件文・歴史的注記・コメント・Conditional examples は blocker と見なさない。
（実績: #262 で Delivery Rule 内の条件文「コンフリクトリスクがある場合は `Depends on #14 merge`」を fallback blocker と誤検出した事例がある。）

### 3.5. Product Spec Preflight（決定論的）

**実行タイミング**: Step 3 (blocker 確認) 完了後、Step 4 (VC preflight) 開始前。

**適用条件** 次のいずれかが true の場合、本チェックを実行する:

- Allowed Paths に `docs/product/**` を含む
- Issue body に `tasks.md` への言及がある
- Issue body に `.specify/` artifact への言及がある
- Issue body に `generated_task_mentioned: true` または `source_task_id` が存在する
- Issue body に `## Product Spec Context` セクションがある

**実行方法**

```bash
python3 .claude/skills/issue-contract-review/scripts/check_product_spec_contract.py \
  --issue-number <番号> \
  --repo <owner>/<repo>
```

**出力解釈** — JSON 出力を解析し、以下のルール ID で判定:

| Rule ID | Condition | Pass | Blocked | N/A |
|---|---|---|---|---|
| **PS001** | `docs/product/**` 更新が spec delta Issue / spec update Issue を参照しているか | 別 spec/update Issue が linked、または Issue 自体が spec/update | implementation Issue で spec delta リンクなし | 無関連 Issue |
| **PS002** | `tasks.md` は staging artifact に限定されているか | staging artifact として参照し GitHub Issue 化を明記 | direct implementation source / tracking SSOT としての参照 | tasks.md 言及なし |
| **PS003** | `.specify/` は derived workbench に限定されているか | workbench として参照し docs/SSOT 優先を明記 | canonical source / docs より優先する扱い | .specify 言及なし |
| **PS004** | product spec 更新に diff_rationale / changed_requirement_id / affected_sections が存在するか | 上記いずれか 1 件以上が存在 | spec update Issue が存在するが evidence なし | non-spec Issue |
| **PS005** | generated task 由来 Issue が `requirement_id` / `source_task_id` を保持しているか | 両方存在 | 片方以上欠落 | generated task でない |
| **PS006** | generated task dependency が materialize されているか（line-anchored `Depends on #N`） | line-anchored `Depends on #N` を保持（GitHub native dependency 対応は follow-up） | dependency 未 materialize | generated task でない |

**判定規則**

| 結果 | 処置 |
|---|---|
| `applicability: not_applicable` かつ `decision: pass` | Step 4 へ（spec 関連なし） |
| `applicability: applicable` かつ `decision: pass` | Step 4 へ |
| `applicability: applicable` かつ `decision: human_judgment` | BLOCKED → issue comment に根拠を列挙して人間判断を求める |
| `applicability: applicable` かつ `decision: fail` | BLOCKED → issue comment に blocked_reasons（rule_id / source / excerpt）を列挙 |

BLOCKED 時は issue comment に以下を記載:

```markdown
## Product Spec Preflight: BLOCKED

### 判定理由
<判定内容を列挙>

### 該当ルール
<PS001–PS006 の rule_id と excerpt>

### 推奨アクション
`issue-refinement-loop` の起動を推奨します。
```

### 4. VC preflight（決定論的・script-backed）

**前提**: AC は「実装前 baseline で fail し、実装後に pass する」検証スクリプトとして書かれている。

**実行方法**

```bash
# baseline_vc_preflight.py を使用して Issue 本文から VC を AC 別に抽出・実行
python3 .claude/skills/issue-contract-review/scripts/baseline_vc_preflight.py \
  --issue <番号> --repo <owner>/<repo>
```

出力は `baseline_vc_preflight/v1` JSON schema で、各 VC の実行結果と root-cause 分類（category / decision / confidence）を含む。

**判定ルール**

script の出力に基づいて以下で判定:

- `status: pass` → OK（全 VC が `decision: go`）
- `status: blocked` → BLOCKED（1 つ以上の `decision: blocked`）
- `status: human_judgment` → `human_judgment`（1 つ以上の `decision: human_judgment`、blocker なし）

BLOCKED 時は issue comment に該当 VC の分類（category / decision / confidence）と理由を記載し、`issue-refinement-loop` の起動を **人間に提案** する（自動起動しない）。

#### VC failure root cause 分類（baseline_vc_preflight.py による）

script は各 VC 実行結果を以下の category で分類（classify_result 関数）:

| category | 条件 | decision | 説明 |
|---|---|---|---|
| `file_not_found_expected` | `test -f` / `test -d` + exit 1 | go | 期待通り baseline fail |
| `file_not_found_unrunnable` | `No such file or directory` stderr + 実行対象 missing | blocked | 想定外の missing |
| `env_missing_dep` | stderr に `No module named` / `ModuleNotFoundError` / `command not found` / `Permission denied` 等 or exit 126/127 | blocked | 環境不備 |
| `expected_baseline_fail` | grep/rg 系の exit 1（no match）/ test コマンド失敗 | go | 期待通り baseline fail |
| `compound_command_disallowed` | `&&` / `\|\|` / `\|` / `;` / heredoc を含む | blocked | 初期実装で非対応 |
| `timeout` | コマンド実行が timeout | blocked | 実行不可能 |
| `unexpected_pass` | exit_code = 0 | blocked | VC が緩すぎるか既に機能存在 |
| `unknown` | 上記いずれにも該当しない | human_judgment | 分類不能 |

分類ロジック（classify_result 関数）:
1. compound command 検出 → `compound_command_disallowed` / blocked
2. timeout 検出 → `timeout` / blocked
3. exit_code = 0 → `unexpected_pass` / blocked
4. env_missing_dep パターン → `env_missing_dep` / blocked
5. expected baseline fail パターン（rg no match、test -f 不在） → `expected_baseline_fail` / go
6. その他 → `unknown` / human_judgment

既存分類との互換性: JSON を CONTRACT_REVIEW_RESULT_V1.checks.vc_preflight.classifications[] へ写像。

**contract fragment schema (classification item)**

各 classification item は以下のフィールドを含む:

- `ac`: AC label
- `command`: コマンド文字列
- `exit_code`: 実行結果の終了コード（null の場合あり）
- `classification`: expected_fail / unexpected_pass / blocked / expected_pass / skipped
- `category`: file_not_found_expected / expected_baseline_fail / ... / regression_gate など
- `confidence`: high / medium / low
- `scope_class`: baseline_fail_expected / regression_gate / pr_review_only / runtime_only（常に存在）
- `evidence`: stdout_excerpt / stderr_excerpt
- `decision`: go / blocked / human_judgment

**skipped items のみ以下を追加:**

- `verification_owner`: pr-review-judge / impl-review-loop
- `deferred_reason`: スキップ理由の説明
- `runtime_verification_required`: true / false

#### VC scope_class 拡張（baseline_fail_expected / regression_gate / pr_review_only / runtime_only）

baseline_vc_preflight.py は各 VC に top-level フィールド `scope_class` を付与し、VC が baseline で検証対象なのか、回帰ゲートなのか、PR 本文や post-implementation 専用なのかを分類する。

| scope_class | 条件 | classification | decision | 説明 |
|---|---|---|---|---|
| `baseline_fail_expected` | 上記いずれにも該当しない default | expected_fail / blocked / human_judgment | go / blocked / human_judgment | baseline で fail すべき、または expected baseline fail パターン |
| `regression_gate` | `pnpm typecheck / lint / test / build` / `uv run pytest <existing>` | expected_pass / blocked | go / blocked | 既存の回帰テスト。baseline pass ならば expected_pass/go、fail なら blocked/blocked |
| `pr_review_only` | VC 直前に `# preflight-scope: pr_review_only` 明示 | skipped | go | PR 本文・人間レビュー時のみ検証。baseline で実行しない |
| `runtime_only` | VC 直前に `# preflight-scope: runtime_only` 明示 | skipped | go | post-implementation / runtime 専用。baseline で実行しない |

**preflight-scope marker 構文**

VC コマンド直前（通常はコマンド行の 1 行上）に以下の metadata comment を記載する:

```bash
## Verification Commands

# AC1
# preflight-scope: pr_review_only
$ grep "expected string" PR_BODY.txt

# AC2
# preflight-scope: runtime_only
$ run_game_simulation_and_check_physics
```

- marker は `# preflight-scope: <value>` の形式（値は `pr_review_only` / `runtime_only`）
- marker は **VC コマンド行 1 行上**（直前行）に必須配置
- marker が存在しない VC は default の `scope_class: baseline_fail_expected` となる

**skipped result の routing metadata**

`scope_class: pr_review_only` / `runtime_only` の VC は `classification: skipped` / `decision: go` となり、以下の extra フィールドを含む:

- `verification_owner`: `"pr-review-judge"` (pr_review_only) / `"impl-review-loop"` (runtime_only)
- `deferred_reason`: skip 理由を説明する文字列
- `runtime_verification_required`: `true` (runtime_only) / `false` (pr_review_only)

**category vs classification の区別（重要）**

contract fragment では `category` と `classification` の両方が含まれる。

- `category`: VC が失敗した原因の分類（`expected_baseline_fail`, `regression_gate`, `env_missing_dep` など）
- `classification`: VC の検証可能性の総合判定（`expected_fail`, `unexpected_pass`, `blocked`, `expected_pass`, `skipped`）

特に `regression_gate` category では、pass と fail で異なる classification が返される:
- baseline pass → `classification: expected_pass` / `decision: go`
- baseline fail → `classification: blocked` / `decision: blocked`

**Downstream 使用者は `classification` を routing-canonical な pass/fail 信号として使う必要がある。** category は情報目的のみ。

**無効な preflight-scope marker 値（typo）**

`# preflight-scope: pr-reveiw-only` など無効な値が指定された場合、その VC は `classification: human_judgment` / `decision: human_judgment` として分類される。fix_hint に「期待値は pr_review_only または runtime_only」と示される。これにより typo による誤分類の silent 無視を防止する。

### 4.5. 動作検証 AC の実行環境前提チェック

Issue 本文の `## Runtime Verification Applicability` セクションを確認する。

- `decision: not_applicable` が明示されている → 本チェックをスキップ（次へ）
- セクション自体が存在しない:
  - implementation issue の場合 → **BLOCKED** または `human_judgment`（fail-closed）
  - non-implementation / legacy issue の場合 → warning（non-blocking）
- `decision: deferred` → 本チェックをスキップ（後続 Issue/フェーズで確認）
- `decision: immediate` → 以下の実行環境前提チェックを実施する

#### decision: immediate の場合の必須確認項目

contract-snapshot に以下がすべて明示されているかを確認する:

| 確認項目 | 判定 |
|---|---|
| 動作検証の対象 AC と VC が `applicable_acs` に明示されている | 不在 → BLOCKED |
| 動作検証に必要な実行環境（CLI ツール名・認証方法・ネットワーク要件等）が記載されている | 不在 → BLOCKED |
| 実行環境が整っていない場合の停止条件（exit 77 等の SKIP 規約）が記載されている | 不在 → BLOCKED |
| フォールバック経由の成功を PASS としない旨が明示されている | 不在 → BLOCKED（`_*_fallback: true` は PASS と見なさない原則の確認） |
| 証跡要件（artifact 出力先・ファイル名パターン等）が記載されている | 不在 → BLOCKED |

上記のいずれかが不足している場合は **BLOCKED** とし、issue comment に不足項目を列挙して `issue-refinement-loop` の起動を人間に提案する。

```bash
# Issue 本文の Runtime Verification Applicability セクションを確認
gh issue view <番号> --json body --jq '.body' | grep -A 20 "Runtime Verification Applicability"
```

> 動作検証の適用判定（immediate / deferred / not_applicable）の詳細規約は `docs/dev/runtime-verification-policy.md` を参照する。本 skill は「contract snapshot に前提が明示されているか」を決定論的に確認するのみ。実環境での動作確認は `implementation-worker` / `test-runner` の責務。

### 5. AC 検証可能性チェック（決定論的）

| 確認項目 | 判定 |
|---|---|
| **AC が `- [ ]` 形式** | チェックボックス形式で書かれている |
| **AC ⇔ VC 番号一致** | `# AC<N>` コメントが VC 内で AC 番号と一致 |
| **検証スクリプト型** | VC が `grep` / `test -f` / `pnpm test` 等の決定論的判定（exit code / 数値比較）を使っている |
| **意味的評価混入なし** | 「適切に動作する」「品質を改善する」等の主観表現が AC / VC にない |

1 つでも fail → BLOCKED。`issue-refinement-loop` を提案。

### 6. Worktree / Branch 命名 preflight

```bash
EXPECTED_WORKTREE=".claude/worktrees/issue-<番号>-<slug>"
EXPECTED_BRANCH="worktree-issue-<番号>-<slug>"
```

- `git worktree list` で既存 worktree と衝突しないこと
- `git branch --list "$EXPECTED_BRANCH"` で既存 branch がないこと
- `.claude/worktrees/` 配下である（外部配置でない）こと

衝突時は人間判断を求めて停止。

### 7. 出力契約（CONTRACT_REVIEW_RESULT_V1）

```yaml
CONTRACT_REVIEW_RESULT_V1:
  status: go | blocked
  generated_at: <ISO 8601>
  generated_by: issue-contract-review
  issue_url: https://github.com/<owner>/<repo>/issues/<番号>
  checks:
    template_compliance: pass | fail
    # state_label: ok | blocked
    # ok = state/needs-human ラベルなし（着手可能）
    # blocked = state/needs-human ラベルあり（着手不可）
    state_label: ok | blocked
    allowed_paths_present: pass | fail
    vc_present: pass | fail
    stop_conditions_complete: pass | fail | n/a
    vc_preflight:
      passed: true | false
      vc_failed_as_expected: <count>
      vc_passed_unexpectedly: <count>
      vc_unrunnable: <count>
      vc_expected_pass: <count>
      vc_skipped: <count>
      classifications:
        - ac: <AC番号>
          command: "<実行したコマンド>"
          exit_code: <int>
          category: file_not_found_expected | file_not_found_unrunnable | env_missing_dep | expected_baseline_fail | regression_gate | unknown
          scope_class: baseline_fail_expected | regression_gate | pr_review_only | runtime_only
          confidence: high | medium | low
          evidence:
            stdout_excerpt: "<stdout の抜粋>"
            stderr_excerpt: "<stderr の抜粋>"
          decision: go | blocked | human_judgment
          verification_owner: "pr-review-judge" | "impl-review-loop" | "human"  # skipped results only
          deferred_reason: "<reason>"  # skipped results only
          runtime_verification_required: true | false  # skipped results only
    ac_verifiability: pass | fail
    product_spec_check:
      applicability: applicable | not_applicable
      decision: pass | fail | human_judgment
      triggers:
        docs_product_allowed_paths: true | false
        tasks_md_mentioned: true | false
        specify_artifact_mentioned: true | false
        generated_task_mentioned: true | false
        product_spec_context_present: true | false
      conditions:
        docs_product_requires_spec_evidence:
          status: pass | fail | n/a
          evidence: []
        tasks_md_not_direct_source:
          status: pass | fail | n/a
          evidence: []
        specify_not_canonical:
          status: pass | fail | n/a
          evidence: []
        diff_first_rationale_present:
          status: pass | fail | n/a | human_judgment
          evidence: []
        generated_task_trace_present:
          status: pass | fail | n/a
          evidence: []
        task_dependencies_materialized:
          status: pass | fail | n/a | human_judgment
          evidence: []
      blocked_reasons:
        - rule_id: PS001 | PS002 | PS003 | PS004 | PS005 | PS006
          source: issue_body | allowed_paths | dependencies
          excerpt: "<short excerpt>"
    worktree_branch_collision: clear | conflict
  next_action: implement_issue | propose_refinement_loop | human_judgment
  blocked_reasons: []
  warnings: []
```

## Output (GitHub side)

| status | 書くもの | 停止 |
|---|---|---|
| `go` | contract-snapshot コメント（後述）を投稿。続けて `implement-issue` を呼ぶ準備完了 | 人間承認待ち（implement-issue 呼び出しの最終承認）|
| `blocked` | 不足理由 + `issue-refinement-loop` の提案コメントを投稿 | 人間判断待ち |

### contract-snapshot コメントの最小構成

```markdown
## Contract Snapshot ($(date -u +%Y-%m-%dT%H:%M:%SZ))

- Outcome: <1 文>
- Acceptance Criteria: <番号付き>
- Verification Commands: <列挙>
- Allowed Paths: <列挙>
- Worktree: `.claude/worktrees/issue-<番号>-<slug>`
- Branch: `worktree-issue-<番号>-<slug>`
- VC preflight: baseline fail = <count>, 想定外 pass = 0, 実行不可 = 0
```

### blocked コメントの最小構成

```markdown
## Contract Review: BLOCKED ($(date -u +%Y-%m-%dT%H:%M:%SZ))

### 不足項目
- <fail した check と理由>

### 推奨アクション
- `issue-refinement-loop` の起動を推奨します。承認後に実行可能です。
```

## Handoff to implement-issue

`status: go` 時に `implement-issue` へ渡す必須項目:

- Issue 番号
- contract-snapshot comment URL
- Outcome / Acceptance Criteria / Verification Commands / Allowed Paths / Required Skills
- Worktree / Branch 命名（preflight で確定したもの）

## Guardrails

- **Issue 本文の品質判定はしない**（`review-issue` の責務）
- **コード編集を開始しない**（preflight 専用）
- **VC を実装後の動作確認に使わない**（baseline 確認のみ）
- **branch / PR / worktree を本 skill では作らない**（`implement-issue` の責務）
- 不適合検出時は自動修復せず、`issue-refinement-loop` の起動を人間に提案する

## Related

- `docs/dev/dor.md` — Implementation Issue の Definition of Ready (DoR) 基準。本 skill の各チェック項目の正本定義先。
- `.claude/skills/review-issue/SKILL.md` — Issue 本文の構造的品質レビュー（本 skill の前段）
- `.claude/skills/issue-refinement-loop/SKILL.md` — 不適合時に提案する改善ループ
- `.claude/skills/implement-issue/SKILL.md` — `status: go` 時の handoff 先
- `.claude/skills/ssot-discovery/SKILL.md` — Issue 関連 SSOT の探索
- `.github/ISSUE_TEMPLATE/implementation.yml` — 必須セクションの正本

## 出力制約 (OUTPUT_BUDGET_V1)

`docs/dev/agent-skill-boundaries.md#OUTPUT_BUDGET_V1` の制約に従う。routing-critical な機械可読フィールドは削らず、人間向け説明・証跡・diff 再掲のみを削減する。
`CONTRACT_REVIEW_RESULT_V1` の全フィールドは必ず含める（routing 必須フィールド）。
