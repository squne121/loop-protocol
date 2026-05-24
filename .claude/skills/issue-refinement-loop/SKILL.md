---
name: issue-refinement-loop
description: Issue 本文の品質を **調査 → レビュー → 改善ライト** の 3 段ループで反復改善するオーケストレーター。Issue 番号を受け取り、review-issue が `approve` を返した時点でループを終了する。「Issue ◯◯ を改善して」「refinement loop」「Issue を磨いて」のトリガーで使う。
---

# Issue Refinement Loop

Issue 本文を AI Agent が作業できる品質に整える 3 段ループのオーケストレーター。
各イテレーションで:

1. `codebase-investigator` SubAgent で関連コードベース調査（Issue 本文 / 対象コメントが外部仕様の主張を含む場合は `web-researcher` SubAgent で一次情報の事実確認を併用）
2. `review-issue` skill で本文の構造的品質を判定
3. `review-issue` が `needs-fix` を返したら `issue-author` SubAgent で本文を改善 → 次イテレーション
4. `review-issue` が `approve` を返したら終了

> ステップ 3（adversarial-review）と ステップ 1.5（spec document review）は LOOP_PROTOCOL では採用しない（PR #12 / #20 方針）。

## Responsibility Boundary: anchor comment fact-check

| Actor | Responsible for | Must not |
|---|---|---|
| main thread / orchestrator | anchor_comment_url 取得、所属検証、snapshot 固定、requires_fact_check 判定、SubAgent 呼び出し、結果統合、final_classification 確定、mutation 実行判断 | repo / web 事実を推測で確定する |
| codebase-investigator | repo / docs / Issue / PR 事実の claim 検証、evidence 返却 | Issue/PR 編集、close、create、final_classification 確定 |
| web-researcher | 外部仕様・CLI/API・公開ドキュメントの fact-check | repo 内方針判断、Issue/PR mutation |
| review-issue | Issue 本文の構造品質・AC/VC・反映漏れレビュー | anchor comment の最終分類 |
| issue-author | 確定済み feedback を本文へ反映、代替 Issue draft 作成 | close/create/edit の実行判断 |
| create-issue / edit-issue skill | main thread 判断後の実 mutation | 分類・方針決定 |

**codebase-investigator external_spec rule**（external_spec claims は MUST NOT 調査し not_checkable を返す）:

```yaml
codebase-investigator external_spec rule:
  - MUST NOT perform web research
  - if required_checks[].type == external_spec:
      # external_spec claims: MUST NOT attempt resolution — return not_checkable
      return verdict: not_checkable
      evidence: []
      scope_impact: ambiguous
      unresolved_risks:
        - "external_spec claim requires web-researcher"
```

## Inputs

- `issue_number`（必須）: 改善対象の Issue 番号
- `max_iterations`（任意、デフォルト 3）: 上限イテレーション数
- `anchor_comment_url`（任意）: Step 0 コメント分類に使用する対象コメント URL。Issue 前提を覆す人間 Decision が含まれる可能性があるコメントを指定する

## Loop Structure

```
[Step 0: 前提確認 / LOOP_STATE 初期化]
        ↓
[Step 0a: anchor_comment_url 取得 + issue 所属検証（指定時のみ）]
        ↓
[Step 0b: 暫定分類（preliminary classification）]
  ├─ superseded_by_decision（事実確認不要の場合のみ確定）
  │       ↓ Step 0c
  ├─ reframe_in_place / feedback_update_required
  │       ↓ Step 1（codebase/web 検証後に最終分類）
  └─ human_escalation → 停止
        ↓
[Step 1 / 1b: 調査（トリガー条件を満たす場合は並列実行可）]
  ├─ codebase-investigator SubAgent
  └─ web-researcher SubAgent（条件付き）
        ↓
[Step 2: レビュー] → issue-reviewer SubAgent
        ↓
   approve → Step 4.5（child materialization gate）→ Step 5 終了
   needs-fix
        ↓
[Step 4: 本文改善] → issue-author SubAgent (edit-issue skill 経由)
        ↓
   iteration += 1, max 未満 → Step 1 へ
   iteration ≥ max → 人間判断で停止
```

## LOOP_STATE

```yaml
LOOP_STATE:
  issue_number: <int>
  iteration: <int, 0-indexed>
  max_iterations: 3
  last_verdict: approve | needs-fix | null
  blockers_history: []
  improvements_applied: []  # iteration ごとの「修正サマリ」
  removed_state_labels: []  # Step 0-hygiene で削除した state ラベルのリスト（state/blocked / state/queued 等）
  termination_reason: null | approved | max_iterations | human_escalation | superseded_by_decision
  anchor_comment:
    url: null
    id: null
    issue_number: null
    html_url: null
    api_url: null                      # GitHub API URL（comment_json.url）
    user_login: null                   # 投稿者のログイン名
    author_association: null
    snapshot: null
    captured_at: null    # このループが snapshot を固定した時刻（GitHub の created_at ではない）
    fetched_at: null
    comment_created_at: null           # GitHub コメントの created_at
    comment_updated_at: null           # GitHub コメントの updated_at
    preliminary_classification: null   # B3: 暫定分類
    final_classification: null         # B3: 事実確認後の最終分類
    classification_reason: null        # B3: 分類根拠
    verified_claims: []                # B3: 検証済み主張
    unresolved_claims: []              # B3: 未解決の主張
    scope_impact: null                 # B3: none | amend | replace | ambiguous
    requires_fact_check: false         # true の場合のみ Step 1 で codebase-investigator に anchor comment を注入する（classification 種別非依存）
  superseded_decision:                 # B3: superseded_by_decision 分岐用
    decision_summary: null
    alternative_issue_number: null
    alternative_issue_url: null
    close_reason: null
    close_comment_posted: false
  web_research:
    required: false          # Step 1b のトリガー条件を満たしたか
    status: null             # ok | failed | skipped
    failure_class: null      # null | auth_error | capability_unavailable | query_error（status=failed 時のみ設定）
    verification_route: null # null | grounded_research | direct_web | direct_cli | none
    result: null             # WEB_RESEARCH_RESULT_V1 または null
    failure_reason: null     # 失敗時のみ
    critical_claims: []      # Outcome / In Scope / AC を左右すると判断した主張リスト
```

## Procedure

### Step 0: 前提確認

```bash
gh issue view <issue_number> --json title,body,labels --jq '.title + " | " + (.labels | map(.name) | join(","))'
```

- `state/needs-human` ラベル付き → 人間判断待ちで AI 改善不可。停止
- `state/done` → 既に完了。停止
- それ以外 → ループ開始

LOOP_STATE を iteration = 0 で初期化。

#### Step 0d: scope rollup preflight（`plan_issue_scope_rollup.py` 実行）

> **実行タイミング**: Step 0 で対象 Issue が存在し refinement 続行可能と判断した後、かつ Step 0-hygiene（state label 削除）より**前**に実行する。
> scope rollup preflight は mutation-free（Issue 作成・編集・クローズ禁止）。
> `ISSUE_SCOPE_ROLLUP_DECISION_V2` を `LOOP_STATE.scope_rollup_decision` に記録してから Step 0-hygiene に進む。

関連 Issue / PR の統合候補を判定し、同一ファイル・同一 skill family の修正が複数の PR に分散することを防ぐ。

```bash
REPO_FULL_NAME=$(gh repo view --json nameWithOwner --jq .nameWithOwner)

# 対象 Issue を個別取得（current_issue として使用）
gh issue view <issue_number> \
  --repo "$REPO_FULL_NAME" \
  --json number,title,body,labels,state,stateReason,url \
  > /tmp/current_issue.json

# issues と PRs の一覧を全状態（open + closed）で取得（デフォルト 30 件制限を回避するため --limit 1000）
gh issue list \
  --repo "$REPO_FULL_NAME" \
  --state all \
  --limit 1000 \
  --json number,title,body,labels,state,stateReason,url \
  > /tmp/issues_all.json

gh pr list \
  --repo "$REPO_FULL_NAME" \
  --state all \
  --limit 1000 \
  --json number,title,body,labels,state,url,files,closingIssuesReferences \
  > /tmp/prs_all.json

# scope rollup preflight を実行（read-only — mutation なし）
python3 .claude/skills/issue-refinement-loop/scripts/plan_issue_scope_rollup.py \
  --issues-json /tmp/issues_all.json \
  --prs-json /tmp/prs_all.json \
  --current-issue <issue_number> \
  --repo "$REPO_FULL_NAME"
```

出力（`ISSUE_SCOPE_ROLLUP_PLAN_V2`）を `LOOP_STATE.scope_rollup_plan` に格納する。

**orchestrator の判断ルール**:

```yaml
scope_rollup_orchestrator_rules:
  confidence_high:
    rule: candidates[].confidence == "high" の候補が 1 件以上ある場合、
          orchestrator は各候補の suggested_action を確認し、統合実施可否を判断する。
          判断結果を ISSUE_SCOPE_ROLLUP_DECISION_V2 に記録してから Step 0-hygiene に進む。
    security_override: security/auth/permission/sandbox 関連は必ず human_review_required に設定し停止する。
    auto_execute: false  # high でも自動実行しない。orchestrator が明示的に判断する。

  confidence_medium:
    rule: 候補を LOOP_STATE に記録し、推奨アクションを提示するが自動実行はしない。
    auto_execute: false

  confidence_low:
    rule: LOOP_STATE に記録するが、アクション不要（keep_separate_with_reason として記録）。
    auto_execute: false

  security_candidates:
    rule: suggested_action == "human_review_required" の候補が含まれる場合は即時停止。
          termination_reason: human_escalation で停止し、人間が判断する。
    auto_execute: false

  no_candidates:
    rule: candidates が空または全候補が low の場合はそのまま Step 0-hygiene に進む。
```

**`ISSUE_SCOPE_ROLLUP_DECISION_V2` の記録**（統合実施・未実施にかかわらず常時記録）:

```yaml
ISSUE_SCOPE_ROLLUP_DECISION_V2:
  schema_version: 2
  recorded_at: "<ISO8601>"
  rollup_plan_ref:
    body_sha256: "<ISSUE_SCOPE_ROLLUP_PLAN_V2.body_sha256>"
    generated_at: "<ISSUE_SCOPE_ROLLUP_PLAN_V2.generated_at>"
  decision: executed | skipped | deferred | human_review_required
  executed_actions: []
  skipped_reason: null
  candidates_reviewed:
    - kind: "issue|pr"
      number: <int>
      confidence: "high|medium|low"
      suggested_action: "<action>"
      final_decision: "accepted|rejected|deferred|human_review_required"
      rejection_reason: null
```

`LOOP_STATE.scope_rollup_decision` に記録した後、Step 0-hygiene に進む。
詳細は `.claude/skills/issue-refinement-loop/references/scope-rollup-policy.md` を参照。

#### Step 0-hygiene: stale state label 掃除（state label hygiene）

> **実行タイミング**: この hygiene は、Step 0 で対象 Issue が存在し refinement 続行可能と判断した後に実行する。
> `state/needs-human` / `state/done` 確認後（ループ停止条件を確認した後）かつ、anchor validation（Step 0a）および mutation 必要性確定後に行う。
> anchor_comment_url が指定されている場合は Step 0a の issue 所属検証が完了し、本 Issue への refinement 続行が確定した後に実行すること。

対象 Issue に残存する `state/blocked` / `state/queued` を hygiene として削除する。
これらは AI 着手可否の primary signal ではないため、refinement ループ開始時に除去して stale ラベルを掃除する。
代替ラベルは付与しない（`state/queued` を自動付与しない）。
削除したラベルは `LOOP_STATE.removed_state_labels` に記録する。

```bash
REPO_FULL_NAME=$(gh repo view --json nameWithOwner --jq .nameWithOwner)

# 現在のラベルを取得
labels_json=$(gh issue view <issue_number> --repo "$REPO_FULL_NAME" --json labels --jq '.labels | map(.name)')

removed_labels=()

# state/blocked が付いていれば remove-label state/blocked
if echo "$labels_json" | jq -e '.[] | select(. == "state/blocked")' > /dev/null 2>&1; then
  gh issue edit <issue_number> --repo "$REPO_FULL_NAME" --remove-label "state/blocked"
  echo "[hygiene] removed stale label: state/blocked"
  removed_labels+=("state/blocked")
fi

# state/queued が付いていれば remove-label state/queued
if echo "$labels_json" | jq -e '.[] | select(. == "state/queued")' > /dev/null 2>&1; then
  gh issue edit <issue_number> --repo "$REPO_FULL_NAME" --remove-label "state/queued"
  echo "[hygiene] removed stale label: state/queued"
  removed_labels+=("state/queued")
fi

# LOOP_STATE.removed_state_labels に記録する（空配列でも記録）
# LOOP_STATE.removed_state_labels = removed_labels
```

**制約**:
- 削除のみ行う（代替ラベルは付与しない。`state/queued` を自動付与しない）。
- `state/needs-human` / `state/done` / `state/in-progress` は対象外（変更しない）。
- 対象ラベルが存在しない場合はスキップ（エラーにしない）。
- 削除したラベルは `LOOP_STATE.removed_state_labels` に記録する（空配列でも記録）。

#### Step 0a: anchor_comment_url の取得と issue 所属検証（`anchor_comment_url` 指定時のみ）

`anchor_comment_url` が指定されている場合、コメント ID を URL から抽出してコメント本文を取得し `LOOP_STATE.anchor_comment` に格納する。

```bash
# コメント ID を URL から抽出（末尾の数値部分）
COMMENT_ID=$(echo "<anchor_comment_url>" | grep -oE '[0-9]+$')

# コメント本文を取得（issue_url も含めて取得）
REPO_FULL_NAME=$(gh repo view --json nameWithOwner --jq .nameWithOwner)
comment_json=$(gh api "repos/$REPO_FULL_NAME/issues/comments/$COMMENT_ID")
```

**B4: issue 所属検証**: コメントが対象 Issue に属していることを確認する。

```bash
# コメントが属する Issue 番号を抽出
comment_issue_number=$(echo "$comment_json" | jq -r '.issue_url | capture("/issues/(?<n>[0-9]+)$").n')

# 対象 Issue と一致しているか検証
test "$comment_issue_number" = "<issue_number>" || {
  echo "[ERROR] anchor_comment_url は対象 Issue #<issue_number> に属していません（実際の Issue: #$comment_issue_number）"
  # termination_reason: human_escalation で停止
}
```

所属検証に失敗した場合は `termination_reason: human_escalation` で停止する。

取得したコメント情報を以下の順で `LOOP_STATE.anchor_comment` に格納して Step 0b へ進む。取得失敗（404 等）の場合は `termination_reason: human_escalation` で停止する。

```bash
LOOP_STATE.anchor_comment.url             = "<anchor_comment_url>"
LOOP_STATE.anchor_comment.id              = "$COMMENT_ID"
LOOP_STATE.anchor_comment.issue_number    = "$comment_issue_number"
LOOP_STATE.anchor_comment.html_url        = comment_json.html_url
LOOP_STATE.anchor_comment.api_url         = comment_json.url
LOOP_STATE.anchor_comment.user_login      = comment_json.user.login
LOOP_STATE.anchor_comment.author_association = comment_json.author_association
LOOP_STATE.anchor_comment.snapshot        = comment_json.body
LOOP_STATE.anchor_comment.captured_at     = "$(date -u +%Y-%m-%dT%H:%M:%SZ)"
LOOP_STATE.anchor_comment.fetched_at      = "$(date -u +%Y-%m-%dT%H:%M:%SZ)"
LOOP_STATE.anchor_comment.comment_created_at = comment_json.created_at
LOOP_STATE.anchor_comment.comment_updated_at = comment_json.updated_at
```

#### Step 0b: 暫定分類（preliminary classification）（`anchor_comment_url` 指定時のみ）

`LOOP_STATE.anchor_comment.snapshot` を以下の4分類で **暫定（preliminary）** 分類する。この段階の分類は確定ではない。コメントが repo 実装事実・外部仕様・既存 Issue/PR の事実を主張している場合は、Step 1 の `codebase-investigator` / `web-researcher` による検証後に最終分類（`final_classification`）を確定する。

| 分類 | 条件（人間 Decision シグナル） | 分岐 |
|---|---|---|
| `superseded_by_decision` | 人間が明示的に「close」「この Issue は実装しない」「前提不採用」「代替方針へ置換」と宣言しており、かつ以下の AND 条件をすべて満たす | Step 0c（Decision 分岐）へ（事実確認不要と判断できる場合のみ即時確定） |
| `reframe_in_place` | 前提の一部は誤りだが Issue 目的は維持可能（部分的修正・誤解の訂正） | requires_fact_check 判定後、必要な場合は ANCHOR_COMMENT_CONTEXT_V1 を Step 1 へ渡して通常ループへ（Step 1 で最終分類） |
| `feedback_update_required` | コメントが AC/VC/In Scope の追加・修正を要求している（否決ではなく更新指示） | close せず requires_fact_check 判定後、必要な場合は ANCHOR_COMMENT_CONTEXT_V1 を Step 1 へ渡して本文更新ループへ（Step 1 で最終分類） |
| `human_escalation` | close か修正か判定不能、または複合シグナル | 人間判断で停止 |

**`superseded_by_decision` の確定条件（AND 条件）**:

```yaml
superseded_by_decision:
  required:
    - 人間が明示的に「close」「この Issue は実装しない」「前提不採用」「代替方針へ置換」と宣言している
    - Issue の Outcome を in-place で修正することが不可能
    - 代替先（Alternative Issue/方針）が決定論的に作成または再利用できる
```

上記 AND 条件を満たさない場合、`superseded_by_decision` に暫定分類してよいが、Step 1 の codebase/web 検証を経て最終分類を確定する。

**Step 0b 処理順序（ordering）**:

```yaml
Step 0b ordering:
  1. preliminary_classification を決める
  2. classification_reason を記録する
  3. requires_fact_check を true/false に確定する（true_if_any / false_only_if 規則適用）
  4. requires_fact_check == true なら Step 0c に進まず Step 1/1b へ
  5. Step 0c に進めるのは final_classification == superseded_by_decision が確定済みの場合のみ
     （requires_fact_check == false かつ preliminary_classification == superseded_by_decision）

# requires_fact_check == false かつ superseded_by_decision の場合の即時確定代入
if requires_fact_check == false
and preliminary_classification == superseded_by_decision
and false_only_if is satisfied:
  LOOP_STATE.anchor_comment.final_classification = superseded_by_decision
  LOOP_STATE.anchor_comment.scope_impact = replace
  proceed_to: Step 0c
```

fail-closed: `requires_fact_check` が不明瞭な場合は `true` とみなして Step 1/1b へ進む。

暫定分類根拠と判定した分類を `LOOP_STATE.anchor_comment.preliminary_classification` および `LOOP_STATE.anchor_comment.classification_reason` に記録してから分岐する。

**`requires_fact_check` 設定規則**:

```yaml
requires_fact_check:
  true_if_any:
    - anchor comment が repo 実装事実を主張している
    - anchor comment が既存 Issue / PR / コメントの事実を主張している
    - anchor comment が外部仕様・CLI/API 現行仕様・移行スケジュールを主張している
    - preliminary_classification が superseded_by_decision だが、確定条件の一部が事実確認に依存している
  false_only_if:
    - 人間 Decision として十分に明示されており、repo/web/Issue/PR 事実確認を必要としない
```

**trusted author policy**:

```yaml
human_decision_trust:
  trusted_author_association:
    - OWNER
    - MEMBER
    - COLLABORATOR
  rule:
    - if final_classification would become superseded_by_decision
      and author_association is not in trusted_author_association:
        requires_fact_check = true  # 信頼できない投稿者の Decision 主張は事実確認必須
        termination_reason: human_escalation  # 事実確認後も human escalation
        reason: untrusted_anchor_comment_author
```

`requires_fact_check == true` の場合は Step 1 へ `ANCHOR_COMMENT_CONTEXT_V1` を渡す。
Step 4 へは main thread が `final_classification` 確定後に生成した `anchor_comment_feedback` のみ渡し、
raw anchor comment snapshot は渡さない。

**`reframe_in_place` / `feedback_update_required`**: `requires_fact_check` の設定規則に従い `LOOP_STATE.anchor_comment.requires_fact_check` を設定した後、通常ループ（Step 1）へ進む。Step 1 終了後に `final_classification` を確定する。

**`human_escalation`**: `termination_reason: human_escalation` で停止する。

#### Step 0c: superseded_by_decision 分岐

人間 Decision により Issue の前提が覆されている場合、以下のステップを順に実行する。

**Step 0c-0: Idempotency preflight（冪等性確認）**

```text
以下を順に確認し、既存の代替 Issue が存在する場合は新規起票を行わない:

1. 元 Issue のコメント履歴に termination_reason: superseded_by_decision が既にあるか確認
   - 既に「代替 Issue: #N」が記録されている場合 → #N を LOOP_STATE.superseded_decision.alternative_issue_number に設定して再利用し、新規起票をスキップ
2. 同一 anchor_comment_url を Background に持つ OPEN Issue を検索
   - 既存 destination が見つかった場合 → create-issue skill を呼ばず、その Issue 番号を代替 Issue として採用
3. 上記いずれも見つからない場合 → 新規起票（Step 0c-2）へ進む
```

1. **情報収集**: 対象 Issue 本文・`LOOP_STATE.anchor_comment.snapshot`・Decision 要約を取得する
2. **代替 Issue 本文案の作成**: `issue-author` SubAgent に、Decision 要約と元 Issue を渡して「Decision に沿った代替 Issue 本文案」を `ALTERNATIVE_ISSUE_DRAFT_V1` 形式で作成させる（Idempotency preflight で既存 Issue が見つかった場合はスキップ）

   **`ALTERNATIVE_ISSUE_DRAFT_V1` 出力契約**:

   ```yaml
   ALTERNATIVE_ISSUE_DRAFT_V1:
     title: string
     body: string
     rationale:
       source_issue: <issue_number>
       anchor_comment_url: string
       decision_summary: string
     validation:
       preserves_parent_goal: true | false
       allowed_paths_defined: true | false
       ac_vc_complete: true | false
   ```

   main thread は `ALTERNATIVE_ISSUE_DRAFT_V1` を受け取った後、`create-issue` skill を実行して代替 Issue を起票する。

3. **代替 Issue の起票**: main thread が `create-issue` skill を実行して代替 Issue を起票し、Issue 番号を取得する（Idempotency preflight で既存 Issue が見つかった場合はスキップ）
4. **元 Issue のクローズ**: 代替 Issue 番号を取得できた場合のみ、以下を実行する:

```bash
# 元 Issue を not planned でクローズし、close コメントを同時に投稿（B5: 単一コマンドに統合）
gh issue close <issue_number> \
  --reason "not planned" \
  --comment "## issue-refinement-loop: クローズ ($(date -u +%Y-%m-%dT%H:%M:%SZ))

- termination_reason: superseded_by_decision
- anchor_comment: <anchor_comment_url>
- Decision 要約: <Decision 要約>
- 代替 Issue: #<代替 Issue 番号>
- 次アクション: 代替 Issue #<代替 Issue 番号> を起点に refinement を再開してください"
```

代替 Issue 番号が取得できなかった場合（`create-issue` 失敗等）は `gh issue close` を実行せず、`termination_reason: human_escalation` で停止する。

Step 0c 完了後は `LOOP_STATE.superseded_decision.close_comment_posted = true` を記録し、`termination_reason: superseded_by_decision` で終了処理（Step 5）へ進む。

### Step 1: 調査（`codebase-investigator` SubAgent）

```yaml
subagent_type: codebase-investigator
inputs:
  issue_number: <LOOP_STATE.issue_number>
  focus_topics: <Issue タイトル + Outcome から抽出したキーワード>
```

**B2: anchor comment 注入条件（`requires_fact_check == true` の場合のみ）**:

`LOOP_STATE.anchor_comment.snapshot != null` かつ `LOOP_STATE.anchor_comment.requires_fact_check == true` の場合、以下を追加入力として渡す。この条件は classification 種別（`reframe_in_place` / `feedback_update_required` / `superseded_by_decision`）に非依存であり、`requires_fact_check` フラグのみで制御する。

注入する入力は `ANCHOR_COMMENT_CONTEXT_V1` 形式で渡す:

```yaml
ANCHOR_COMMENT_CONTEXT_V1:
  schema_version: 1
  source:
    url: <LOOP_STATE.anchor_comment.url>
    comment_id: <LOOP_STATE.anchor_comment.id>
    issue_number: <LOOP_STATE.anchor_comment.issue_number>
    author_association: <OWNER | MEMBER | COLLABORATOR | CONTRIBUTOR | NONE | null>
    user_login: <投稿者のログイン名>
    html_url: <comment html_url>
    api_url: <GitHub API URL>
    captured_at: <iso8601>
    comment_created_at: <GitHub コメントの created_at>
    comment_updated_at: <GitHub コメントの updated_at>
  issue_snapshot:
    title: <Issue タイトル>
    body: <Issue body 全文またはコンテキスト上限内の抜粋>
    labels: []
    outcome: <Issue Outcome セクション>
    in_scope: <Issue In Scope セクション>
    acceptance_criteria: <Issue AC リスト>
    out_of_scope: <Issue Out of Scope セクション>
    verification_commands: <Issue VC セクション>
  anchor_comment_snapshot: <LOOP_STATE.anchor_comment.snapshot>
  preliminary:
    classification: <LOOP_STATE.anchor_comment.preliminary_classification>
    classification_reason: <分類の根拠>
  required_checks:
    - claim_id: C1
      description: コメントが Outcome / In Scope / AC を無効化しているか検証する
      type: repo_fact | issue_pr_fact | external_spec | human_decision
      critical: true | false
    - claim_id: C2
      description: 事実誤りと scope 置換を区別する
      type: repo_fact | issue_pr_fact | external_spec | human_decision
      critical: true | false
```

`codebase-investigator` は上記入力を受け取り、claim ごとの verdict と evidence のみを返す。**mutation（Issue/PR の編集・クローズ・作成）を行ってはいけない（MUST NOT）**。

`codebase-investigator` の出力は `ANCHOR_COMMENT_FACT_CHECK_RESULT_V1` 形式で返す:

```yaml
ANCHOR_COMMENT_FACT_CHECK_RESULT_V1:
  schema_version: 1
  status: ok | inconclusive | failed
  claims:
    - claim_id: <C1, C2, ...>
      claim: <検証対象の主張テキスト>
      type: repo_fact | issue_pr_fact | external_spec | human_decision
      verdict: supported | contradicted | inconclusive | not_checkable
      evidence:
        - kind: file | issue | pr | comment | web
          ref: <path#line | issue# | pr# | comment_url | web citation>
          summary: <why this evidence supports/refutes the claim>
      scope_impact: none | amend | replace | ambiguous
      critical: true | false
  recommended_final_classification: superseded_by_decision | reframe_in_place | feedback_update_required | human_escalation
  unresolved_risks: []
```

**main thread の責務**: `codebase-investigator` から `ANCHOR_COMMENT_FACT_CHECK_RESULT_V1` を受け取った後、`recommended_final_classification` を参考に main thread / orchestrator が `LOOP_STATE.anchor_comment.final_classification` を確定する。`final_classification` の確定責務は main thread にあり、SubAgent に委譲してはならない。

SubAgent は Issue 本文に関連するコードベース・既存 ADR・関連 Issue / PR を調査し、構造化レポートを返す。
LOOP_PROTOCOL では `ssot-discovery` skill を併用して `docs/` 配下の関連ドキュメントも列挙する。

Step 1 完了後、`requires_fact_check == true` だった場合は main thread が `ANCHOR_COMMENT_FACT_CHECK_RESULT_V1` を統合して `LOOP_STATE.anchor_comment.final_classification` を確定し、`verified_claims` と `unresolved_claims` を更新する。

### Step 1b: 外部仕様の事実確認（条件付き、`web-researcher` SubAgent）

Step 1 と独立しているため、トリガー条件を満たす場合は **Step 1 と並列実行**してよい。

トリガー条件（いずれか）:

- Issue 本文 / 対象コメントが外部仕様・公式ドキュメント・公開 API の挙動・ライブラリ / ツールの既定値・CLI 引数・認証方式・移行スケジュールを主張している
- Issue の実装方針が特定ツール / サービス / 配布物の現在仕様に依存している
- Claude Code が示した技術情報や実装アプローチをエビデンスで裏付けたい（ハルシネーション切り分け）
- 人間が「Web 情報と照合してほしい」と明示した

条件を満たさないイテレーションでは省略してよい（大多数の refinement はコードベース調査だけで足りる）。

`critical_claims`（Outcome / In Scope / Out of Scope / AC / VC を左右する主張）は `critical: true` として `web-researcher` に渡す。

```yaml
subagent_type: web-researcher
inputs:
  claims: <Issue 本文 / 対象コメントから抽出した検証対象の主張リスト>
  purpose: <Issue タイトル + 何の判断の裏付けかを 1 文で>
  context: <Issue 番号 / 対象コメント URL>
```

SubAgent は `gemini-cli-headless-delegation`（`tool_profile: grounded_research`）経由で一次情報を事実確認し、`WEB_RESEARCH_RESULT_V1` 形式で返す。結果は Step 2 のレビュー材料および Step 4 の本文改善（誤った前提の訂正）に渡す。

**external_spec claim のルーティング規則**:

```yaml
external_spec routing:
  if ANCHOR_COMMENT_CONTEXT_V1.required_checks[].type == external_spec:
    pass the same claim_id / description / critical to web-researcher as WEB_RESEARCH_REQUEST
  final_classification:
    wait_for:
      - ANCHOR_COMMENT_FACT_CHECK_RESULT_V1 (repo_fact / issue_pr_fact / human_decision)
      - WEB_RESEARCH_RESULT_V1 (external_spec)
    fail_closed_if:
      - any critical external_spec claim is inconclusive or failed in WEB_RESEARCH_RESULT_V1
```

`WEB_RESEARCH_RESULT_V1` の schema:

```yaml
WEB_RESEARCH_RESULT_V1:
  schema_version: 1
  status: ok | inconclusive | failed
  failure_class: null | auth_error | capability_unavailable | query_error
  verification_route: grounded_research | direct_web | direct_cli | none
  claims:
    - claim_id: <C3>
      type: external_spec
      critical: true | false
      verdict: supported | contradicted | inconclusive
      evidence:
        - kind: web
          ref: <citation>
          summary: <string>
  unresolved_risks: []
```

`WEB_RESEARCH_RESULT_V1` 受信後（`WEB_RESEARCH_RESULT_V1 handling`）:

```yaml
WEB_RESEARCH_RESULT_V1 handling:
  if status == ok:
    proceed_to: Step 2

  if status == inconclusive:
    if any claims[].critical == true and claims[].verdict == inconclusive:
      termination_reason: human_escalation
      reason: critical_external_spec_inconclusive
    else:
      record unresolved_risks in LOOP_STATE
      proceed_to: Step 2

  if status == failed:
    if any critical claims exist:
      termination_reason: human_escalation
      reason: web_research_failed_critical
    else:
      record failure_class in LOOP_STATE
      proceed_to: Step 2
```

詳細処理:
1. `LOOP_STATE.web_research.status`、`failure_class`、`verification_route`、`result` を更新する（`verification_route` は `WEB_RESEARCH_RESULT_V1.verification_route` の値をそのまま設定する）
2. `web-researcher` が `status: ok` を返した場合: Step 2 へ進む
3. `web-researcher` が `status: inconclusive` を返した場合:
   - `claims[].critical == true` かつ `claims[].verdict == inconclusive` の claim が 1 件以上: `termination_reason: human_escalation`（`reason: critical_external_spec_inconclusive`）で停止する
   - 上記に該当しない場合: `unresolved_risks` を `LOOP_STATE` に記録し、Step 2 へ進む
4. `web-researcher` が `status: failed` を返した場合:
   - `LOOP_STATE.web_research.failure_class` に `WEB_RESEARCH_RESULT_V1.failure_class`（`auth_error | capability_unavailable | query_error`）を記録する
   - **non-critical**（`LOOP_STATE.web_research.critical_claims` が空）: その旨を LOOP_STATE に記録し、外部仕様の事実確認なしで Step 2 へ進む
   - **critical**（`critical_claims` に 1 件以上）: `termination_reason: human_escalation` で停止する（Outcome / In Scope / AC を左右する主張の裏付けなしに改善を続けない。`query_error` または fallback 失敗の場合も同様）

> これは採用しない「spec document review」（= リポジトリ内 `docs/` の網羅レビュー）とは別物であり、外部 web 一次情報の事実確認に限定する。

### Step 2: レビュー（`issue-reviewer` SubAgent）

<!-- ORCHESTRATOR_IO_BOUNDARY_V1 準拠: orchestrator は issue-reviewer SubAgent を呼ぶ（直接 Skill 呼び出し禁止） -->

```yaml
subagent_type: issue-reviewer
inputs:
  issue_number: <LOOP_STATE.issue_number>
```

`issue-reviewer` SubAgent は内部で `review-issue` skill を実行し、`REVIEW_ISSUE_RESULT_V1` を返す。

orchestrator は `REVIEW_ISSUE_RESULT_V1` の `verdict` と `status` のみを routing 判断に使う。
`blocking_issues` の詳細テキストや `diff_proposal` の内容は main thread で再解釈しない（domain judgment は SubAgent に委ねる）。

<!-- domain judgment の一部（anchor comment stale approval invalidation 等）は現在のスコープでは
main thread に残る。以下の B8 条件分岐はオーケストレーターが issue-reviewer を呼ぶ前に処理する。 -->

**B8: anchor comment による stale approval 無効化**（issue-reviewer 呼び出し前の main thread 処理）:
`anchor_comment.snapshot != null` の場合、以下の条件でフローを制御する:

```yaml
Step 2 stale approval invalidation（anchor_comment がある場合）:
  if anchor_comment.snapshot != null:
    # fact-check 未完了ガード
    if LOOP_STATE.anchor_comment.requires_fact_check == true
    and LOOP_STATE.anchor_comment.final_classification == null:
      termination_reason: human_escalation
      reason: anchor_comment_fact_check_not_completed

    # final_classification に基づく処理分岐
    if LOOP_STATE.anchor_comment.final_classification == feedback_update_required
    or LOOP_STATE.anchor_comment.final_classification == reframe_in_place:
      invalidate last_verdict
      pass only anchor_comment_feedback (NOT raw snapshot) to Step 4

    if LOOP_STATE.anchor_comment.final_classification == superseded_by_decision:
      proceed_to: Step 0c

    # 絶対禁止
    raw anchor_comment.snapshot MUST NOT be used as reviewer_feedback_text
```

anchor comment が以下のいずれかを含む場合、`issue-reviewer` の既存 approve を無効化して `needs-fix` として扱う:

```text
- No-Go as-is
- Revise before implementation
- AC追加 / VC追加 / In Scope変更
- 本文に未反映の adversarial review
→ last_verdict を無効化し、reviewer_feedback_text に anchor comment を正規化して issue-author に渡す
```

`issue-reviewer` SubAgent（内部で `review-issue` skill を実行）は `REVIEW_ISSUE_RESULT_V1` を返す:

```yaml
REVIEW_ISSUE_RESULT_V1:
  verdict: approve | needs-fix
  status: ok | failed
  failure_class: null | gh_auth | permission_denied | issue_not_found | schema_invalid | unknown  # status: failed 時のみ設定
  error_summary: null | <エラーの概要>  # status: failed 時のみ設定
  review_result_ref:
    kind: agent_transcript | hook_artifact | github_comment
    ref: null  # path-or-url（取得可能な場合のみ設定、null 可）
  detail_payload_policy: opaque_ref_only
  deterministic_checks: { ... }  # C1〜C11 全フィールド
  blocking_issues: []
  non_blocking_improvements: []
  diff_proposal:
    add: []
    remove: []
    rewrite: []
  update_applied: false
  comment_url: null
```

orchestrator の routing 判断:

```yaml
orchestrator routing:
  if status == failed:
    termination_reason: human_escalation
    reason: issue_reviewer_failed
    failure_class: <REVIEW_ISSUE_RESULT_V1.failure_class>  # gh_auth | permission_denied | issue_not_found | schema_invalid | unknown
  if status == ok and verdict == approve:
    # approve → Step 4.5 (mandatory gate) → Step 5 に統一
    # delivery-rollup + child-complete 以外の Issue は Step 4.5 で即 Step 5 へ通過する
    proceed_to: Step 4.5
  if status == ok and verdict == needs-fix:
    blocking_issues と diff_proposal を LOOP_STATE に記録（opaque forwarding payload として）
    proceed_to: Step 4
```

> Critical Guard: refinement フェーズでは AC を実行しない（review-issue 内で guard 済み）。
> baseline fail は正常動作のため、それを根拠に追加 iteration を要求しない。

### Step 4: 本文改善（`issue-author` SubAgent + `edit-issue` skill）

```yaml
subagent_type: issue-author
inputs:
  task: edit
  issue_number: <LOOP_STATE.issue_number>
  reviewer_feedback_text: <REVIEW_ISSUE_RESULT_V1 から転送する opaque forwarding payload（orchestrator は再解釈しない）>
  # reviewer_feedback_text は REVIEW_ISSUE_RESULT_V1.blocking_issues / diff_proposal を
  # orchestrator が再解釈せず opaque のまま issue-author に転送する。
  # orchestrator はこのフィールドを routing 判断に使わない（domain judgment は issue-author に委ねる）。
```

**B2: anchor_comment が feedback_update_required / reframe_in_place の場合の追加入力**:

Step 4 は raw anchor comment（`LOOP_STATE.anchor_comment.snapshot`）を直接受け取らない。main thread が `final_classification` を確定した後に正規化した `anchor_comment_feedback` のみを受け取る。

`edit-issue` skill の Inputs に渡す際は、`title_update` を top-level フィールドとして渡す（`anchor_comment_feedback.title_update` ではなく top-level `title_update`）。

```yaml
# anchor_comment_url が指定されており、
# final_classification が feedback_update_required / reframe_in_place の場合、以下を追加する
Step 4 inputs（anchor_comment が feedback_update_required / reframe_in_place の場合）:
  reviewer_feedback_text: <review-issue diff_proposal + anchor_comment の正規化フィードバック>
  current_issue_title: <現在の GitHub Issue タイトル（title_update.required 判定に使用）>
  anchor_comment_feedback:
    # final_classification 後の正規化済み情報のみ渡す。raw anchor comment snapshot は含めない。
    # raw anchor comment snapshot を title 生成・reviewer_feedback_text に直接使ってはならない（MUST NOT）。
    # main thread が final_classification 確定後に生成した正規化済み anchor_comment_feedback のみを渡す。
    classification: reframe_in_place | feedback_update_required   # main thread が確定した final_classification
    required_edits: <final_classification から導いた必要編集内容>
    scope_impact: <LOOP_STATE.anchor_comment.scope_impact>
  # edit-issue skill への top-level 入力として渡す（anchor_comment_feedback のネスト内ではない）
  edit_issue_inputs:
    issue_number: <LOOP_STATE.issue_number>
    reviewer_feedback_text: <normalized feedback>
    title_update: <anchor_comment_feedback から導出した title_update — top-level として渡す>
      # required: true | false
      # proposed_title: <新しいタイトル文字列 | null>   # required=true の場合のみ設定
      # reason: <タイトル変更が必要な理由 | null>       # required=true の場合のみ設定
```

**title_update の設定規則**:

```yaml
title_update.required:
  # current_issue_title（Step 4 inputs に含む）を参照して判定する
  true_if_any:
    - final_classification == reframe_in_place かつ Goal / Outcome / In Scope の意味が変わる場合
    - final_classification == feedback_update_required かつ Goal / Outcome / In Scope の意味が変わる場合
    - body 更新後の Outcome / In Scope が current_issue_title の主要語と矛盾する場合
    - anchor_comment が明示的にタイトル変更を要求している場合
  false_if_all:  # すべての条件を満たす場合のみ false にする
    - Goal / Outcome / In Scope の意味が変わらない
    - structural-only needs-fix または AC/VC 整形のみ
```

**title_update 対象外ケース（title_update.required を false にする）**:
- `superseded_by_decision`: タイトル更新対象外。元 Issue はクローズし、代替 Issue 起票で対応する
- title に影響しない structural-only `needs-fix`（AC/VC 整形のみ、In Scope・Outcome の実質変更なし）: タイトル更新対象外
- AC/VC の整形・番号付けのみの変更: タイトル更新対象外

**title 品質基準**（`title_update.required == true` の場合、`proposed_title` は以下を満たすこと）:

```yaml
title_quality:
  must:
    - 更新後の Goal / Outcome / In Scope を反映する
    - 旧スコープ固有語（意味が変わった用語）を残さない
    - 既存の prefix（例: "実装:" / "改善:" / "fix:" 等）を維持する
    - AC/VC の細部ではなく成果物・運用上の到達点を表す
  must_not:
    - body 更新後の Outcome / In Scope と矛盾する旧主要語を含む
  escalate_if:
    - proposed_title の判断が不能な場合 → human_escalation または non-blocking note にする
```

SubAgent は `edit-issue` skill の Procedure を実行し、バックアップ → guards → 本文書き戻し → 変更経緯コメント投稿。

`ISSUE_EDIT_RESULT_V1.status: ok` を確認したら LOOP_STATE.iteration += 1 して Step 1 に戻る。
`failed` の場合は LOOP_STATE.blockers_history に記録し、人間判断（`termination_reason: human_escalation`）。

### Step 4.5: Delivery-rollup Parent の Child Materialization Gate（`approve` 直前）

Step 2 で `verdict: approve` が返った後、終了処理（Step 5）に進む前に以下を実行する。対象 Issue が `parent_mode: delivery-rollup` かつ `closure_mode: child-complete` の parent Issue である場合のみ適用する。

```yaml
delivery_rollup_gate:
  trigger:
    issue_kind: parent
    parent_mode: delivery-rollup
    closure_mode: child-complete
  gate:
    action: run_child_materialization_plan
    command: |
      uv run python3 .claude/skills/create-issue/scripts/plan_child_materialization.py \
        --repo <owner>/<repo> \
        --issue <issue_number>
    on_missing_children:
      # action=create_issue の child が 1 件以上 → mandatory_follow_up として記録
      severity: mandatory_follow_up
      add_to_follow_up_issue_requests: true
      approve_without_materialization: prohibited
    on_stale_body_only:
      # action=reuse_and_update_parent → edit-issue skill に委譲して parent body を修正
      severity: mandatory_follow_up
      delegate_to: edit-issue (delivery-rollup-parent-update mode)
    on_ambiguous:
      severity: human_escalation
      reason: child_state_unknown
    on_no_children:
      # plan に children が 0 件 → gate をパス（warnings をログに記録）
      pass: true
```

**判定フロー**:

1. `plan_child_materialization.py` を実行して `CHILD_MATERIALIZATION_PLAN_V2` を取得する（read-only）。
2. `action: create_issue` のエントリが存在する場合:
   - `mandatory_follow_up` として `FOLLOW_UP_ISSUE_REQUEST_V1` を生成する
   - `severity: mandatory_follow_up` のため、APPROVE 確定前に materialize が必要
   - main thread が `issue-author` / `create-issue` 経由で起票する
3. `action: reuse_and_update_parent` のエントリが存在する場合:
   - `edit-issue` skill の `delivery-rollup-parent-update` mode に `CHILD_MATERIALIZATION_PLAN_V2` を渡して parent body を修正する
4. `action: human_escalation` のエントリが存在する場合:
   - `termination_reason: human_escalation` で停止する
5. `issue-author` の `CHILD_MATERIALIZATION_RESULT_V2` が `partial_failure` / `failed` / `human_escalation` を返した場合（AC8）:
   - `partial_failure`: 一部の child 起票に失敗 → `termination_reason: human_escalation` で停止し、失敗した child ID と理由をコメントに記録する
   - `failed`: 全件失敗 → `termination_reason: human_escalation` で停止する
   - `human_escalation`: 起票不可（parser_gap 低確信度・API error 等） → `termination_reason: human_escalation` で停止する（escalation_items をコメントに記録）
   - いずれの場合も「approve で即終了」とせず、人間が残作業を確認できる状態で停止する
6. すべての mandatory_follow_up が materialize 済みになったら Step 5（終了処理）に進む。

**注意**: `plan_child_materialization.py` の実行は read-only（GitHub Issue を変更しない）。mutation は `create-issue` / `edit-issue` skill が担う。

### Step 5: 終了処理

| termination_reason | アクション |
|---|---|
| `approved` | Issue コメントで「refinement loop 完了」を報告 |
| `max_iterations` | Issue コメントで残存 blockers を提示、人間判断 |
| `human_escalation` | Issue コメントで詳細を提示、人間判断 |
| `superseded_by_decision` | Issue クローズ（`--reason "not planned"`）+ 代替 Issue 起票 + close コメントに代替 Issue 番号と termination_reason を記録（Step 0c で実行済み） |

```bash
gh issue comment <issue_number> --body "## issue-refinement-loop: 完了 ($(date -u +%Y-%m-%dT%H:%M:%SZ))

- iteration: <最終 iteration 数>
- verdict: <approve | needs-fix>
- termination_reason: <approved | max_iterations | human_escalation>
- 改善履歴: <improvements_applied の要約>
- 次アクション: <issue-contract-review 起動 / 人間レビュー / 追加 iteration 等>

\`\`\`yaml
FOLLOW_UP_MATERIALIZATION_RESULT_V1:
  follow_up_issues:
    - request_dedupe_key: \"...\"
      issue_number: 123
      issue_url: \"https://github.com/...\"
      status: created | reused_open | skipped_closed_duplicate | skipped_closed_not_planned | skipped_closed_completed

  note_only_observations:
    - dedupe_key: \"...\"
      source_url: \"...\"
      source_note_id: \"...\"
      summary: \"...\"
\`\`\`"
```

### 派生改善候補の自動起票

refinement ループ中に codebase-investigator / web-researcher / review-issue が「この Issue スコープ外だが改善すべき事項」を発見した場合、main thread は `termination_reason: approved` 確定後に以下を実行する:

- `LOOP_STATE.improvements_applied` または各 SubAgent の出力中に含まれる **派生改善候補**（scope 外の改善提案・技術的負債・関連 docs 更新等）を `issue-author` / `create-issue` 経由で**自動起票**する。
- 起票前に **dedupe チェックを dedupe_key ベースで実施する**（title 検索ではなく `FOLLOW_UP_ISSUE_REQUEST_V1.dedupe_key` で既存 Issue（open / closed すべて）を検索して重複を確認）:
  ```
  gh issue list --repo squne121/loop-protocol --state all \
    --search '"<dedupe_key>"' --json number,title,url,state,stateReason,labels
  ```
  - open の重複が見つかった場合はスキップ（`status: reused_open`）
  - closed（not_planned / completed / duplicate）の重複が見つかった場合は起票せずスキップ（`status: skipped_closed_*`）
  - closed Issue を open に差し戻す場合は human escalation が必要（自動起票不可）
  - 重複なしの場合は `## Source` セクション（`dedupe_key` を含む）を Issue 本文に付与して起票する
- 起票・スキップした情報を終了報告コメントに列挙する（`FOLLOW_UP_MATERIALIZATION_RESULT_V1` 形式、詳細スキーマは `docs/dev/agent-skill-boundaries.md` 参照）。

**派生改善候補の自動起票は Out of Scope 拡大ではない**。本 Issue の refinement スコープを変えず、観察された改善を別 Issue として分離することで `1 Issue = 1 PR` 原則を維持する。

## Critical Guard: 実装前の状態に関する誤検知パターン

refinement フェーズでは Issue 本文の **構造的品質** だけを判定する。以下のパターンは誤検知として除外する。

### パターン 1: Verification Commands 0 ヒットの誤検知

VC を実装前 baseline に対して実行して 0 ヒット → 「実装未着手」と誤判定しない。0 ヒットは正常（実装後に pass する設計）。

### パターン 2: 現行コードの実装状態を blocker として誤報告

「現行コードには変更がない」を blocker として報告しない。refinement は本文の構造品質のみを見る。

### パターン 3: Stop Conditions の時系列制約の誤分類

`## Stop Conditions` に「実装中に X を検出したら停止」と書かれていても、refinement では実装していないため X の有無を判定しない。

## ループ終了判定

| 条件 | termination_reason |
|---|---|
| Step 2 で `verdict: approve` | `approved` |
| `iteration >= max_iterations` | `max_iterations` |
| 各 Step で `human_review_required: true` | `human_escalation` |
| `LOOP_STATE.anchor_comment.final_classification == superseded_by_decision` かつ Step 0c 完了 | `superseded_by_decision` |

## Guardrails

- 本 skill は control-plane のみ。本文編集は `issue-author` SubAgent + `edit-issue` skill 経由で行う
- `review-issue` から `approve` を受けた後は必ず Step 4.5（child materialization gate）を経由してから終了（`delivery-rollup + child-complete` 以外の Issue は Step 4.5 で即 Step 5 へ通過する）
- `max_iterations` 超過時は必ず fail-close（無限ループ防止）
- baseline fail / 実装前 0 ヒットを誤検知 blocker にしない（Critical Guard 参照）
- adversarial-review は採用しないため、信頼性リスク観点の判定は本ループ範囲外

## Scope 変更シグナル検出（ループ内停止条件）

iteration 中に Issue 本文へ以下が新規追加された場合は、refinement のスコープ拡大兆候として **次イテレーションに進まず即停止**（`termination_reason: human_escalation`）:

- `## In Scope` に新規の機能領域が追加された
- `## Allowed Paths` に新規ディレクトリが追加された（既存と異なるアーキテクチャ層への拡大）
- `## Acceptance Criteria` に新規の検証可能性が低い項目が追加された

## Out of Scope

- **Agent SDK 化**: `ANCHOR_COMMENT_CONTEXT_V1` / `ANCHOR_COMMENT_FACT_CHECK_RESULT_V1` を Agent SDK の typed tool として実装することは別 Issue で扱う。本 Issue #127 では Skill 上の protocol contract 固定に限定し、SDK 化は行わない。

## Verification Commands

```bash
# B3: anchor_comment に拡張フィールドが存在する
rg "preliminary_classification|final_classification|verified_claims|scope_impact" .claude/skills/issue-refinement-loop/SKILL.md && echo "PASS: B3 schema" || echo "FAIL: B3 schema"

# B4: issue 所属検証の記述確認
rg "issue_url.*capture|comment_issue_number|対象 Issue.*に属" .claude/skills/issue-refinement-loop/SKILL.md && echo "PASS: B4 validation" || echo "FAIL: B4 validation"

# B5: 統合 close コマンドの記述確認
rg "gh issue close.*--comment|--comment.*--reason" .claude/skills/issue-refinement-loop/SKILL.md && echo "PASS: B5 atomic close" || echo "FAIL: B5 atomic close"

# B6: 冪等性 preflight の記述確認
rg "Idempotency|冪等|既存.*代替 Issue|alternative.*existing" .claude/skills/issue-refinement-loop/SKILL.md && echo "PASS: B6 idempotency" || echo "FAIL: B6 idempotency"

# B7: ALTERNATIVE_ISSUE_DRAFT_V1 の記述確認
rg "ALTERNATIVE_ISSUE_DRAFT_V1" .claude/skills/issue-refinement-loop/SKILL.md && echo "PASS: B7 contract" || echo "FAIL: B7 contract"

# AC1: ANCHOR_COMMENT_CONTEXT_V1 の記述確認
rg "ANCHOR_COMMENT_CONTEXT_V1" .claude/skills/issue-refinement-loop/SKILL.md && echo "PASS: AC1" || echo "FAIL: AC1"

# AC2: requires_fact_check の記述確認
rg "requires_fact_check" .claude/skills/issue-refinement-loop/SKILL.md && echo "PASS: AC2" || echo "FAIL: AC2"

# AC3: ANCHOR_COMMENT_FACT_CHECK_RESULT_V1 の記述確認
rg "ANCHOR_COMMENT_FACT_CHECK_RESULT_V1" .claude/skills/issue-refinement-loop/SKILL.md && echo "PASS: AC3" || echo "FAIL: AC3"

# AC4: codebase-investigator の mutation 禁止記述確認
rg "codebase-investigator.*(mutation|edit|close|create|してはいけない|MUST NOT)" .claude/skills/issue-refinement-loop/SKILL.md && echo "PASS: AC4" || echo "FAIL: AC4"

# AC5: final_classification 確定責務が main thread にあることの確認
rg "final_classification.*main thread|main thread.*final_classification|orchestrator.*final_classification" .claude/skills/issue-refinement-loop/SKILL.md && echo "PASS: AC5" || echo "FAIL: AC5"

# AC6: anchor_comment_feedback / 正規化済み feedback の記述確認
rg "anchor_comment_feedback|正規化.*feedback|final_classification.*feedback" .claude/skills/issue-refinement-loop/SKILL.md && echo "PASS: AC6" || echo "FAIL: AC6"

# AC7: Agent SDK Out of Scope の記述確認
rg "Agent SDK" .claude/skills/issue-refinement-loop/SKILL.md && echo "PASS: AC7" || echo "FAIL: AC7"

# iter 4: raw anchor comment snapshot を Step 4 / reviewer_feedback_text に直渡しする旧経路がないこと
# （禁止ルール宣言行 "MUST NOT" を含む行はガード記述であり誤検知対象外のため除外する）
! rg "anchor comment の内容を.*(Step 4|reviewer_feedback_text)|raw anchor comment.*reviewer_feedback_text|snapshot.*reviewer_feedback_text" \
  .claude/skills/issue-refinement-loop/SKILL.md \
  | rg -v "MUST NOT|iter 4|rg " \
  && echo "PASS: no raw Step 4 injection" \
  || { echo "FAIL: raw anchor comment injection path remains"; exit 1; }

# iter 3: requires_fact_check の true/false 規則と fail-closed があること
rg "true_if_any|false_only_if|fail.closed|requires_fact_check が不明瞭" \
  .claude/skills/issue-refinement-loop/SKILL.md && echo "PASS: requires_fact_check-rules" || echo "FAIL: requires_fact_check-rules"

# iter 3: Step 0b ordering と Step 0c gate があること
rg "Step 0b ordering|requires_fact_check == true.*Step 0c|final_classification == superseded_by_decision" \
  .claude/skills/issue-refinement-loop/SKILL.md && echo "PASS: step0b-ordering" || echo "FAIL: step0b-ordering"

# iter 3: WEB_RESEARCH_RESULT_V1 が top-level schema fields を持つこと
rg "WEB_RESEARCH_RESULT_V1:|status: ok.*inconclusive.*failed|failure_class|verification_route" \
  .claude/skills/issue-refinement-loop/SKILL.md && echo "PASS: web-research-schema" || echo "FAIL: web-research-schema"

# iter 3: trusted author policy があること
rg "trusted_author_association|untrusted_anchor_comment_author" \
  .claude/skills/issue-refinement-loop/SKILL.md && echo "PASS: trusted-author-policy" || echo "FAIL: trusted-author-policy"

# iter 3: hidden/bidi Unicode 文字がないこと
python3 - <<'PY'
from pathlib import Path
import unicodedata
p = Path(".claude/skills/issue-refinement-loop/SKILL.md")
bad = []
for lineno, line in enumerate(p.read_text(encoding="utf-8").splitlines(), 1):
    for col, ch in enumerate(line, 1):
        if unicodedata.bidirectional(ch) in {"LRE","RLE","LRO","RLO","PDF","LRI","RLI","FSI","PDI"}:
            bad.append((lineno, col, f"U+{ord(ch):04X}", unicodedata.name(ch, "")))
if bad:
    for row in bad: print(row)
    raise SystemExit("FAIL: hidden/bidi control characters found")
print("PASS: no hidden/bidi control characters")
PY

# issue-227 AC3: issue-reviewer SubAgent への変更確認（Loop Structure + Procedure Step 2 の両方）
awk '/^## Loop Structure/{f=1} /^## LOOP_STATE/{f=0} f' .claude/skills/issue-refinement-loop/SKILL.md | grep -q "issue-reviewer SubAgent" && echo "PASS: Loop Structure updated to issue-reviewer SubAgent" || echo "FAIL: Loop Structure not updated"
awk '/^### Step 2: レビュー/{f=1} /^### Step 4:/{f=0} f' .claude/skills/issue-refinement-loop/SKILL.md | grep -q "issue-reviewer" && echo "PASS: Procedure Step 2 updated to issue-reviewer" || echo "FAIL: Procedure Step 2 not updated"

# issue-227 AC7 smoke test: issue-refinement-loop Step 2→Step 4 フローの構造検証
# （transcript assertion の最低要件: 設計上の保証を静的構造チェックで代替）
# 1. Step 2 が issue-reviewer SubAgent として定義されている
awk '/^### Step 2: レビュー/{f=1} /^### Step 4:/{f=0} f' .claude/skills/issue-refinement-loop/SKILL.md \
  | grep -q "subagent_type: issue-reviewer" \
  && echo "PASS: AC7-1 Step 2 uses issue-reviewer SubAgent" \
  || { echo "FAIL: AC7-1 Step 2 does not use issue-reviewer SubAgent"; exit 1; }

# 2. Step 2 に review-issue Skill tool の直接呼び出しがない（Skill tool は issue-reviewer 内部で実行）
if awk '/^### Step 2: レビュー/{f=1} /^### Step 4:/{f=0} f' \
  .claude/skills/issue-refinement-loop/SKILL.md \
  | grep -E "^[[:space:]]*(skill:|Skill tool)" | grep -v "^#"
then
  echo "FAIL: AC7-2 Step 2 has direct Skill tool call in main transcript"
  exit 1
fi
echo "PASS: AC7-2 No direct Skill tool call in Step 2"

# 3. verdict: needs-fix 後に Step 4 issue-author が定義されている（フロー保証）
awk '/^### Step 4: 本文改善/{f=1} /^### Step 5:/{f=0} f' .claude/skills/issue-refinement-loop/SKILL.md \
  | grep -q "subagent_type: issue-author" \
  && echo "PASS: AC7-3 Step 4 uses issue-author SubAgent after needs-fix" \
  || { echo "FAIL: AC7-3 Step 4 does not use issue-author SubAgent"; exit 1; }

# 4. issue-reviewer の disallowedTools に Skill が含まれている（mutation 禁止構造）
grep -q "Skill" .claude/agents/issue-reviewer.md \
  && echo "PASS: AC7-4 issue-reviewer disallowedTools includes Skill" \
  || { echo "FAIL: AC7-4 issue-reviewer disallowedTools missing Skill"; exit 1; }

# issue-227 AC4-a: Loop Structure 内に review-issue skill / skill: review-issue / invoked_as_loop の直接参照がないこと
if awk '/^\[Step 2: レビュー\]/{f=1} /^\[Step 4:/{f=0} f' \
  .claude/skills/issue-refinement-loop/SKILL.md \
  | rg "review-issue skill|skill:[[:space:]]*review-issue|invoked_as_loop"
then
  echo "FAIL: Loop Structure has direct review-issue Skill reference"
  exit 1
fi
echo "PASS: Loop Structure no direct Skill call"

# issue-227 AC4-b: Procedure Step 2 内に skill: review-issue / review-issue skill / invoked_as_loop の直接参照がないこと
if awk '/^### Step 2: レビュー/{f=1} /^### Step 4:/{f=0} f' \
  .claude/skills/issue-refinement-loop/SKILL.md \
  | rg "skill:[[:space:]]*review-issue|review-issue skill|invoked_as_loop"
then
  echo "FAIL: Procedure Step 2 has direct review-issue Skill reference"
  exit 1
fi
echo "PASS: Procedure Step 2 no direct Skill call"
```

## Related

- `.claude/agents/issue-reviewer.md` — Step 2 の loop worker SubAgent（review-issue skill を内部実行）
- `.claude/skills/review-issue/SKILL.md` — issue-reviewer SubAgent が内部で使う手順
- `.claude/skills/edit-issue/SKILL.md` — Step 4 で issue-author が使う
- `.claude/skills/ssot-discovery/SKILL.md` — Step 1 で関連 SSOT を探す
- `.claude/skills/issue-contract-review/SKILL.md` — refinement 後の着手前 preflight
- `.claude/agents/codebase-investigator.md` — Step 1 の調査者
- `.claude/agents/web-researcher.md` — Step 1b の外部仕様事実確認者（条件付き）
- `.claude/skills/gemini-cli-headless-delegation/SKILL.md` — Step 1 / 1b の委譲先
- `.claude/agents/issue-author.md` — Step 4 の本文更新者
- `docs/dev/agent-skill-boundaries.md` — オーケストレーター設計原則（ORCHESTRATOR_IO_BOUNDARY_V1 / control-plane / LOOP_STATE / 人間承認原則）
- `docs/dev/github-ops.md` — GitHub 運用ルール（body-file guard / Parent Mode / コメントテンプレ）

## 出力制約 (OUTPUT_BUDGET_V1)

`docs/dev/agent-skill-boundaries.md#OUTPUT_BUDGET_V1` の制約に従う。routing-critical な機械可読フィールドは削らず、人間向け説明・証跡・diff 再掲のみを削減する。
