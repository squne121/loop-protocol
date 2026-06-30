# Milestone 運用規約（SSOT）

GitHub Milestone の作成・管理・クローズに関する単一の真実の情報源（SSOT）。
AI エージェント・人間レビュアー双方が参照する。

## SSOT 境界

### この文書が持つもの

- GitHub Milestone の責務定義（何を milestone として立てるか・立てないか）
- Milestone / Parent Issue / Implementation Issue / PR の責務分担
- Milestone 命名規則（title / description / due_on 方針）
- Milestone close 条件（fail-closed な close predicate / descendant traversal 要件を含む）
- AI エージェント操作フロー / 人間 fallback フロー（RACI 含む）
- GitHub REST API エンドポイント参照
- Milestone 識別子規約（`number` vs `id`）
- Key Definitions（direct item / descendant / blocker / closure follow-up / valid defer decision）
- MILESTONE_CLOSE_DECISION_V1 スキーマ（live readback report と人間判断記録の形式）

### この文書が持たないもの

- 個別 Issue のスコープ判断（各 Issue 本文・feature spec が正本）
- 個別 Issue の Milestone 割り当て理由（parent issue comment・対象 milestone description・割当 Issue コメントに記録する。本文書に追記しない）
- ラベル運用規約（`docs/dev/github-ops.md` が正本）
- PR レビュー・マージ手順（`docs/dev/workflow.md` が正本）
- ブランチ保護・権限設定（`docs/dev/github-ops.md` が正本）

---

## Key Definitions / 用語定義

Milestone close predicate で用いる用語の定義。

| 用語 | 定義 |
|---|---|
| **direct item** | GitHub Milestone に `milestone` フィールドで直接割り当てられた Issue / PR |
| **descendant** | direct Issue から GitHub native sub-issues を再帰的に辿って到達する Issue |
| **blocker** | native `blocked_by` の open Issue。native dependency endpoint が利用不能な場合のみ `## Depends On` セクションを fallback source とする |
| **closure follow-up** | parent issue の machine-readable closure ledger に明示登録された Issue。PR 本文・自由記述コメントのみの follow-up は close gate の正本にしない |
| **valid defer decision** | `issue` / `reason` / `residual_risk` / `destination` / `revisit_condition` / `decided_by` / `decided_at` を持つ人間の判断記録 |

### descendant の milestone 判定規則

| depth / milestone 状態 | 判定 |
|---|---|
| depth 0（direct item）、milestone=対象 Milestone | 正常 |
| depth 0（direct item）、milestone=null または別 Milestone | 異常（mismatch） |
| depth ≥ 1（descendant）、milestone=null | **間接所属として正常**（mismatch にしない） |
| depth ≥ 1（descendant）、milestone=別 Milestone に明示割当 | **scope conflict** として人間判断 |
| depth ≥ 1（descendant）、milestone=対象 Milestone | 正常 |

> **重要**: depth ≥ 1 の descendant で `milestone=null` は、parent を通じて間接的に Milestone に属するものとして正常扱いする。一律 mismatch にしない。
> Implementation Issue が同一 Milestone へ直接割当されている場合（例外扱い）は、`#146` の例外理由への参照が必須。

---

## Milestone の責務

### Milestone として立てるもの

- **開発フェーズ（Phase）の区切り**: `M1: Foundation Gate (v0.1.x)` のような、複数の関連 Issue を束ねる里程標
- **リリース目標**: 外部・内部向けに「この機能群をここまでに届ける」という約束を持つ単位
- **品質ゲート**: CI / テストカバレッジ・セキュリティレビューなど、特定条件を満たすことでフェーズが完結する区切り

### Milestone として立てないもの

- 単一 Issue の進捗管理（Issue 自体がその役割を持つ）
- PR 単位の作業追跡（PR は Issue に紐づく成果物であり、Milestone に直接紐づけない）
- 個人タスクの期限管理（チーム・プロジェクト全体の節目でない場合）
- 実験的スパイク・調査系タスクのみで構成される一時的なバケット

---

## Milestone / Parent Issue / Implementation Issue / PR の責務分担と役割整理

| 単位 | 責務 | 関係 |
|---|---|---|
| **Milestone** | 開発フェーズ・リリース目標の区切り。複数の parent issue を束ねる。close 条件は下記「Close 条件（fail-closed な close predicate）」を参照。 | Issue を `milestone` フィールドで紐づける |
| **Parent Issue** | 機能・テーマ単位の追跡。child implementation issues を束ねる。`closure_mode` に従って close する。 | Milestone に割り当てられる / child issues を持つ |
| **Implementation Issue** | 1 PR に対応する具体的な実装タスク。`## Allowed Paths` / `## Acceptance Criteria` を持つ。 | Parent Issue の sub-issue / Milestone に間接的に紐づく |
| **PR** | Implementation Issue の成果物。`Closes #N` で Issue を close する。Milestone には直接紐づけない。 | Implementation Issue を close する |

---

## Milestone 割当対象の規約

### 原則

- **GitHub Milestone に直接割り当てる対象は Parent Issue とする。**
- Implementation Issue は Parent Issue の child として Milestone に間接的に属する。直接割り当てはしない。
- PR は Milestone に直接割り当てない。

### 例外

- Implementation Issue を直接 Milestone に含める場合は、M-A3（#146）の割当判定コメントに理由を記録すること。
- 例外割当された Implementation Issue は milestone rollup の `open_issues` / `closed_issues` 集計に含める。

> Close 条件の `open_issues: 0` は直接割当された Issue 数を参照する。Parent Issue を直接割当する方針では、Implementation Issue の close は Parent Issue の close を通じて間接的に反映される。

---

## 命名規則

### title / タイトル

```
<Phase ID>: <フェーズ名> (<バージョン>)
```

例:
- `M1: Foundation Gate (v0.1.x)`
- `M2: Gameplay Core (v0.2.x)`
- `Q1-2026: Release Candidate`

- Phase ID は大文字英字 + 数字（`M1`, `M2`, `Q1-2026` 等）
- フェーズ名は人間可読な短い説明（20 文字以内を推奨）
- バージョン表記は `vX.Y.z` または `vX.Y.x`（patch 全体をまとめる場合は `x`）

### description / 説明

- 目標（Goal）を 1〜3 文で記述する
- 含める Issue / 除外する Issue の方針を明示する（任意）
- AI エージェントが参照できるよう英語または日本語どちらでも可

### due_on

- 外部コミットメント（リリース日・イベント日）がある場合のみ設定する
- **create 時**: due date を持たない場合は `due_on` フィールドを送らない（省略 = null として作成される）
- **update 時**: 既存 due date を消したい場合は `gh api` での null 指定方法を事前確認し、readback で `due_on == null` を確認する。due_on の変更・削除は human escalation 対象とする
- 設定する場合は ISO 8601 形式（`YYYY-MM-DDTHH:MM:SSZ`、GitHub API は UTC）

---

## Close 条件（fail-closed な close predicate）

Milestone を close するには以下の条件を**すべて**満たすこと（AND 条件。OR ではない）。
この predicate は **fail-closed** であり、descendant traversal の完全性・未解決 blocker・scope conflict・明示的 defer record を含む。

### 通常の自動判定条件（すべて満たす必要がある）

```yaml
# fail-closed close predicate
close_predicate:
  direct_check:
    milestone_open_issues: 0           # direct items の open_issues = 0
    pr_mixed_count: 0                  # PR 混入なし
    assignment_drift: []               # readback drift なし
  descendant_traversal:
    schema: MILESTONE_DESCENDANT_ROLLUP_V1
    partial: false                     # 不完全な descendant traversal は close 不可
    warnings: []                       # warnings 非空の場合は close 不可
    open_blocker_count: 0              # 未解決 blocker は close 不可
    scope_conflict_count: 0            # scope conflict は close 不可
```

詳細:

1. **割り当てた Issue の open 件数が 0 であること**
   `gh api repos/{owner}/{repo}/milestones/{milestone_number}` の `open_issues` が `0` であること

2. **Milestone に直接紐づいた PR が 0 であること**
   PR 混入チェック（[3] 参照）で `pull_request != null` の item が存在しないこと。
   PR を Milestone に直接紐づける運用は許可しない。

   > **例外（open Issue が残る場合の scope 除外）**: Milestone description または parent issue comment に除外理由を明示的に記録すること。この例外は人間のみ適用できる。
   > **PR が Milestone に直接紐づいている場合は close 例外を認めない。** まず PR から Milestone を外し、PR 混入チェックが 0 件になってから close を検討する。PR 混入が残る状態での close は invariant violation とし、理由記録では回避不可。

3. **descendant traversal report の schema が `MILESTONE_DESCENDANT_ROLLUP_V1` であること**
   stale evidence や schema 不一致は close 不可。

4. **`partial=false` であること**
   `partial=true`（traversal が不完全）の場合は close 不可。partial traversal の原因解消後に再実行すること。

5. **`warnings == []` であること**
   warnings 非空の場合は close 不可。各 warning を解消するか、valid defer decision として記録すること。

6. **`open_blocker_count == 0` であること**
   未解決 blocker がある場合は close 不可。blocker の完了または valid defer decision が必要。

7. **scope conflict が 0 であること（review-required なし）**
   別 Milestone に明示割当された descendant がある場合は人間判断が必要。

8. **assignment readback drift が 0 であること**
   live assignment と expected set の drift が 0 であること。

9. **人間による意図的な判断がある**
   Milestone の close は `scope` や `目標達成` の判断を含むため、AI エージェントが自動で close しない。
   人間が `gh api --method PATCH repos/{owner}/{repo}/milestones/{milestone_number} -f state=closed` を実行するか、GitHub UI から close する

10. **Scope 変更なし、または明示的な scope 変更の記録がある**
    含める / 外す Issue の変更があった場合、Milestone description または関連 ADR に記録する

### defer してはいけないもの（defer 禁止リスト）

以下のいずれかが存在する場合は、close 不可であり defer 対象にもできない:

| 状態 | 理由 |
|---|---|
| **PR 混入**（`pr_mixed_count > 0`） | Milestone 運用不変条件違反。まず PR を外すこと |
| **API failure**（認証エラー / rate limit / pagination 不完了 / parse error） | evidence の信頼性が不明のまま close できない |
| **partial traversal**（`partial=true`） | descendant の完全性が保証されない |
| **warnings 非空** | warnings の内容未精査のまま close できない |
| **stale evidence**（`generated_at` が古い / schema 不一致） | 最新の live 状態を反映していない evidence での close 不可 |
| **report schema 不一致**（`MILESTONE_DESCENDANT_ROLLUP_V1` でない） | 正規形式の evidence が揃うまで close 不可 |

### valid defer decision の要件

open blocker や warnings を defer 扱いにするには、以下の全フィールドを持つ人間の判断記録が必要:

```yaml
valid_defer_decision:
  issue: "#N"
  reason: "<defer する理由>"
  residual_risk: "<残存リスクの説明>"
  destination:
    issue: "#M"
    milestone: "<後続 milestone または null>"
  revisit_condition: "<再確認のトリガー>"
  decided_by: "<人間の GitHub ログイン>"
  decided_at: "<ISO 8601>"
```

フィールドが 1 つでも欠けている場合は invalid defer decision として reject する。

## M1_MILESTONE_CLOSE_CHECKLIST_V1（derived_view）

M1 close 判定で参照する checklist は、新規 predicate を作るのではなく `close_predicate` の
**derived view** として定義する。  
ここでは close 判定の実行手順と、実際の AC/VC に必要な要件を文書化する。

```yaml
M1_MILESTONE_CLOSE_CHECKLIST_V1:
  normative_status: derived_view
  derived_view:
    source: close_predicate
    description: >-
      既存の fail-closed close_predicate をそのまま再利用し、M1 判定時の入力・
      必須根拠・運用責任者を明示する。
  authority:
    source_of_truth: docs/dev/milestone-ops.md
    executable_checker: scripts/milestone_rollup.py
    rollup_skill: .claude/skills/milestone-rollup/SKILL.md
    rollup_shell: .claude/skills/milestone-rollup/scripts/milestone_rollup.sh
    note: >-
      この checklist は derived view のみを定義し、実行可能な close readiness
      predicate そのものは置き換えない。
  github_api_version: "2022-11-28"

  required_docs:
    - docs/dev/milestone-ops.md
    - docs/dev/github-ops.md
    - docs/dev/workflow.md

  required_gates:
    - name: m1-open-issues-zero
      source: close_predicate.direct_check.milestone_open_issues
      expected: 0
      required: true
    - name: m1-pr-mix-zero
      source: close_predicate.direct_check.pr_mixed_count
      expected: 0
      required: true
    - name: m1-descendant-report-schema-v1
      source: close_predicate.descendant_traversal.schema
      expected: MILESTONE_DESCENDANT_ROLLUP_V1
      required: true
    - name: m1-descendant-traversal-complete
      source: close_predicate.descendant_traversal.partial
      expected: false
      required: true
    - name: m1-warnings-empty
      source: close_predicate.descendant_traversal.warnings
      expected: []
      required: true
    - name: m1-open-blocker-zero
      source: close_predicate.descendant_traversal.open_blocker_count
      expected: 0
      required: true
    - name: m1-scope-conflict-zero
      source: close_predicate.descendant_traversal.scope_conflict_count
      expected: 0
      required: true
    - name: m1-assignment-drift-zero
      source: close_predicate.direct_check.assignment_drift
      expected: []
      required: true
    - name: m1-evidence-generated-at-recorded
      source: close_predicate.evidence.generated_at
      expected: present
      required: true
    - name: m1-evidence-repository-sha-recorded
      source: close_predicate.evidence.repository_commit_sha
      expected: present
      required: true
    - name: m1-evidence-freshness-confirmed
      source: close_predicate.stale_evidence
      expected: absent
      required: true
    - name: m1-human-close-required
      source: close_predicate.human_close
      expected: "close_predicate step 9"
      required: true
    - name: m1-close-not-gated-by-strict
      description: >
        --strict は close gate として扱わない。
        CI 実行戦略のオプションを close 合否条件に追加しない。
      expected: not_required
      required: false

  required_parent_status:
    description: >-
      以下は PR 作成時点の live status ではなく、M1 close 判断時に満たすべき
      expected status を表す。
    "#131": closed
    "#133": closed
    "#472": closed

  non_deferable_failures:
    - close_predicate.direct_check.pr_mixed_count
    - close_predicate.descendant_traversal.partial
    - close_predicate.descendant_traversal.warnings
    - close_predicate.descendant_traversal.open_blocker_count
    - close_predicate.descendant_traversal.scope_conflict_count
    - close_predicate.stale_evidence
    - close_predicate.schema_mismatch
    - close_predicate.api_failure

  close_actor: "<human-login>"
```

### AI エージェントによる自動 close の禁止

Milestone の close は **人間の意思決定が必要** であり、AI エージェントは自動 close しない。
`open_issues: 0` の検知は AI エージェントが rollup コメントで人間に通知するまでとする。

---

## MILESTONE_CLOSE_DECISION_V1 スキーマ

M1 の live readback report と人間判断記録の保存場所・形式。

このスキーマは Milestone close 判断時に人間が記録するものであり、AI エージェントはこのスキーマに従った
YAML を `MILESTONE_CLOSE_DECISION_V1` として Issue コメント（対象 Milestone の parent tracker issue）に投稿する。
`decided_by` と `decision` フィールドは人間が記入する。

```yaml
MILESTONE_CLOSE_DECISION_V1:
  milestone_number: <int>
  direct_readback:
    open_issues: 0
    pr_mixed_count: 0
    assignment_drift: []
  descendant_report:
    schema: MILESTONE_DESCENDANT_ROLLUP_V1
    partial: false
    warnings: []
    unresolved_blockers: []
    scope_conflicts: []
  deferred:
    - issue: "#N"
      reason: "..."
      residual_risk: "..."
      destination:
        issue: "#M"
        milestone: "<destination milestone or null>"
      revisit_condition: "..."
      decided_by: "<human GitHub login>"
      decided_at: "<ISO 8601>"
  evidence:
    generated_at: "<ISO 8601>"
    repository_commit_sha: "<sha>"
    github_api_version: "2022-11-28"
  human_decision:
    decision: close
    actor: "<human-login>"
    decided_at: "<ISO 8601>"
```

### 保存場所

- **保存先**: 対象 Milestone を tracking する parent issue（例: #131 for M1）のコメント
- **形式**: `MILESTONE_CLOSE_DECISION_V1:` を先頭行とする YAML ブロック（コードフェンスで囲む）
- **記録タイミング**: Milestone close 実行の直前（`human_decision.decision: close` が確定した時点）

---

## AI エージェント操作フロー / 人間 fallback フロー

### RACI 定義

| 操作 | Responsible（実行者） | Accountable（最終責任者） | Consulted（相談先） | Informed（通知先） |
|---|---|---|---|---|
| Milestone 作成 | AI エージェント | 人間（目標・scope・命名の最終承認） | — | — |
| Issue を Milestone に割り当て | AI エージェント | 人間 | 人間（例外時） | — |
| Milestone 進捗 readback | AI エージェント | AI エージェント | 人間 | — |
| 進捗 rollup コメント投稿 | AI エージェント | AI エージェント | — | 人間 |
| Milestone close | 人間 | 人間 | AI エージェント | AI エージェント |
| Scope 変更（Issue の追加・除外） | 人間（承認後） | 人間 | AI エージェント（影響分析） | — |
| 破壊的変更（milestone 削除・rename） | 人間 | 人間 | — | AI エージェント |

> R = Responsible（実行者）, A = Accountable（最終責任者）, C = Consulted（相談先）, I = Informed（通知先）
> **Milestone close は人間が Responsible かつ Accountable。AI エージェントは close しない。**

### Milestone 作成における「人間承認」の定義

Milestone 作成の「人間承認」は、次の条件を満たした時点で充足されたものとする:

- 対応する implementation issue contract（例: #145 等）に `title` / `description` / `due_on` 方針が明記されている
- その issue contract に対して `issue-contract-review` が `status: go` を返している

> **逸脱時の escalation**: title / description / due_on を issue contract から逸脱して変更する場合は、AI エージェントは作成を停止し human escalation とする。

### AI エージェント操作フロー（通常時）

```
[0] 既存 Milestone preflight（create 前に必ず実行）
    └─ gh api --paginate \
         "repos/{owner}/{repo}/milestones?state=all&per_page=100" \
         --jq '.[] | select(.title == "<title>") | {number, id, state, title, due_on, html_url}'
    └─ 同名 0 件 → [1] create へ進む
    └─ 同名 1 件 (state=open) → [1] をスキップし update/readback へ進む
    └─ 同名 1 件 (state=closed) → human escalation（reopen か新規作成かを人間が判断）
    └─ 同名 2 件以上 → human escalation（どれを使うかを人間が判断）

[1] Milestone 作成
    └─ gh api --method POST repos/{owner}/{repo}/milestones \
         -f title="<title>" \
         -f description="<description>" \
         [-f due_on="<ISO8601>"]
    └─ readback: 返却された number・id を記録

[2] Issue を Milestone に割り当て
    # gh CLI 経由: --milestone は milestone の title/name を受け取る（number 不可）
    └─ gh issue edit {issue_number} \
         --milestone "<milestone_title>" \
         --repo {owner}/{repo}
    # REST API 経由: milestone フィールドは milestone の number（integer）を渡す
    └─ gh api --method PATCH repos/{owner}/{repo}/issues/{issue_number} \
         -f milestone={milestone_number}
    └─ readback: gh issue view {issue_number} --json milestone で確認
    └─ readback で milestone が null の場合は silent drop — human escalation

[3] 進捗 rollup
    └─ gh api repos/{owner}/{repo}/milestones/{milestone_number}
    └─ open_issues / closed_issues を取得
    └─ PR 混入チェック: Milestone に PR が直接紐づいていないことを確認
       gh api --paginate \
         "repos/{owner}/{repo}/issues?milestone={milestone_number}&state=all&per_page=100" \
         --jq '.[] | select(.pull_request != null) | {number, title, state, html_url}'
       出力が非空の場合は human escalation（PR 直接紐づけ禁止の運用不変条件違反）
       ※ --paginate で全ページを列挙する（per_page=100 のみでは 101 件目以降が未検査になる）
    └─ 関連 Issue にコメント投稿（github-ops.md の Body File Guidance に従う）

[4] open_issues: 0 を検知したら人間に通知
    └─ close は実行しない
```

### 人間 fallback フロー

以下のいずれかの場合、AI エージェントは操作を停止し人間にエスカレーションする:

| 条件 | 対応 |
|---|---|
| **権限不足**（403 / 404）| 1. Issue コメント投稿を試みる。2. コメント投稿も失敗した場合は Human Escalation コメント本文を標準出力に完全出力し、追加 write 操作を行わず停止する |
| **silent drop**（API 呼び出しは 200 だが実際に反映されない）| readback で確認し、不一致を報告 |
| **SSOT 衝突**（milestone の割り当てが他ドキュメントの方針と矛盾する）| 自動解決せず、矛盾を Issue コメントに記録し人間判断を要求 |
| **Milestone close 判断** | AI は close せず、条件を満たしたことを通知するのみ |
| **Scope 変更（Issue 追加・除外）** | 提案のみ行い、人間の承認後に実行 |

### Human escalation コメントテンプレ

```markdown
## milestone-ops: Human Escalation Required (<timestamp>)

- Milestone: <title> (#<number>)
- 理由: <権限不足 / silent drop / SSOT 衝突 / scope 変更 / close 判断>
- 状況: <具体的なエラー・矛盾の内容>
- 依頼: <人間に実行してほしい操作>
```

---

## GitHub REST API エンドポイント参照

### Milestone CRUD / 作成更新削除

| 操作 | メソッド | エンドポイント |
|---|---|---|
| 一覧取得 | `GET` | `/repos/{owner}/{repo}/milestones` |
| 作成 | `POST` | `/repos/{owner}/{repo}/milestones` |
| 取得 | `GET` | `/repos/{owner}/{repo}/milestones/{milestone_number}` |
| 更新 | `PATCH` | `/repos/{owner}/{repo}/milestones/{milestone_number}` |
| 削除 | `DELETE` | `/repos/{owner}/{repo}/milestones/{milestone_number}` |

### Issue への Milestone 割り当て

| 操作 | メソッド | エンドポイント | パラメータ |
|---|---|---|---|
| Milestone 割り当て・変更 | `PATCH` | `/repos/{owner}/{repo}/issues/{issue_number}` | `milestone`: milestone の `number`（integer） |
| Milestone 解除 | `PATCH` | `/repos/{owner}/{repo}/issues/{issue_number}` | `milestone`: `null` |

### `gh` CLI 等価コマンド

```bash
# Milestone 作成
gh api --method POST repos/{owner}/{repo}/milestones \
  -f title="M1: Foundation Gate (v0.1.x)" \
  -f description="開発基盤・運用ルール・最小仕様正本を固めるフェーズ"

# Issue を Milestone に割り当て（gh CLI 経由: --milestone は title/name を渡す）
gh issue edit {issue_number} \
  --milestone "<milestone_title>" \
  --repo {owner}/{repo}

# Issue を Milestone に割り当て（REST API 経由: milestone フィールドは number を渡す）
gh api --method PATCH repos/{owner}/{repo}/issues/{issue_number} \
  -f milestone={milestone_number}

# Milestone 進捗確認
gh api repos/{owner}/{repo}/milestones/{milestone_number} \
  --jq '{title: .title, open: .open_issues, closed: .closed_issues, state: .state}'

# Milestone 内の PR 混入チェック（全ページ列挙 — PR 直接紐づけ禁止の検証）
gh api --paginate \
  "repos/{owner}/{repo}/issues?milestone={milestone_number}&state=all&per_page=100" \
  --jq '.[] | select(.pull_request != null) | {number, title, state, html_url}'
# 出力が非空の場合は human escalation

# Milestone close
gh api --method PATCH repos/{owner}/{repo}/milestones/{milestone_number} \
  -f state=closed
```

---

## Milestone 識別子規約

GitHub の Milestone には 2 種類の識別子がある。

| 識別子 | フィールド名 | 値の例 | 用途 |
|---|---|---|---|
| **number** | `number` | `1`, `2`, `3` | REST API の path パラメータ。URL に含める識別子。`/repos/{owner}/{repo}/milestones/{number}` の `{number}` に使う |
| **id** | `id` | `12345678` | GitHub database identifier。GraphQL の `node_id` 相当。通常の REST 操作では使わない |

### 規約

- REST API path パラメータには必ず `number` を使う（`id` を path に使うと 404 になる）
- `gh api` / `curl` でエンドポイントを指定する際は `number` を path に埋め込む
- `id` は内部参照・GraphQL・webhook payload での識別に使われる場合があるが、REST path には使わない
- Milestone 作成直後の readback で `number` を取得・記録し、以後の操作に使用する

### readback で number を取得する例

```bash
MILESTONE_NUMBER=$(gh api --method POST repos/{owner}/{repo}/milestones \
  -f title="M1: Foundation Gate (v0.1.x)" \
  --jq '.number')
echo "Milestone number (REST path parameter): $MILESTONE_NUMBER"
```

---

## 関連ドキュメント

- `docs/dev/github-ops.md` — ラベル運用・認証・Body File Guidance・permissions 方針（SSOT）
- `docs/dev/workflow.md` — Issue 駆動開発フロー全体（SSOT）
- `docs/dev/agent-skill-boundaries.md` — SubAgent / Skill 責務境界
- `docs/dev/current-focus.md` — 現在のフェーズと優先順位
