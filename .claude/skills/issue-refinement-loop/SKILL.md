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

## Inputs

- `issue_number`（必須）: 改善対象の Issue 番号
- `max_iterations`（任意、デフォルト 3）: 上限イテレーション数

## Loop Structure

```
[Step 0: 前提確認 / LOOP_STATE 初期化]
        ↓
[Step 1: 調査] → codebase-investigator SubAgent
        ↓
[Step 1b: 外部仕様の事実確認（条件付き）] → web-researcher SubAgent
        ↓
[Step 2: レビュー] → review-issue skill (invoked_as_loop: true)
        ↓
   approve → 終了
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
  termination_reason: null | approved | max_iterations | human_escalation
  web_research:
    required: false          # Step 1b のトリガー条件を満たしたか
    status: null             # ok | failed | skipped
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

### Step 1: 調査（`codebase-investigator` SubAgent）

```
subagent_type: codebase-investigator
inputs:
  issue_number: <LOOP_STATE.issue_number>
  focus_topics: <Issue タイトル + Outcome から抽出したキーワード>
```

SubAgent は Issue 本文に関連するコードベース・既存 ADR・関連 Issue / PR を調査し、構造化レポートを返す。
LOOP_PROTOCOL では `ssot-discovery` skill を併用して `docs/` 配下の関連ドキュメントも列挙する。

### Step 1b: 外部仕様の事実確認（条件付き、`web-researcher` SubAgent）

Issue 本文または対象コメントが **リポジトリ外の一次情報で事実確認すべき主張** を含む場合のみ実行する。それ以外のイテレーションでは省略する（大多数の refinement はコードベース調査だけで足りる）。

トリガー条件（いずれか）:

- Issue 本文 / 対象コメントが外部仕様・公式ドキュメント・公開 API の挙動・ライブラリ / ツールの既定値を主張している
- その主張が Issue の Outcome / In Scope / Out of Scope / AC の妥当性を左右する
- 主張が外部ドキュメントを引用している（引用の正確性を確認したい）

```
subagent_type: web-researcher
inputs:
  claims: <Issue 本文 / 対象コメントから抽出した検証対象の主張リスト>
  purpose: <Issue タイトル + 何の判断の裏付けかを 1 文で>
  context: <Issue 番号 / 対象コメント URL>
```

SubAgent は `gemini-cli-headless-delegation`（`tool_profile: grounded_research`）経由で一次情報を事実確認し、`WEB_RESEARCH_RESULT_V1` 形式で返す。結果は Step 2 のレビュー材料および Step 4 の本文改善（誤った前提の訂正）に渡す。

`WEB_RESEARCH_RESULT_V1` 受信後:
1. `LOOP_STATE.web_research.status` と `result` を更新する
2. `web-researcher` が `status: failed` を返した場合:
   - **non-critical**（`LOOP_STATE.web_research.critical_claims` が空）: その旨を LOOP_STATE に記録し、外部仕様の事実確認なしで Step 2 へ進む
   - **critical**（`critical_claims` に 1 件以上）: `termination_reason: human_escalation` で停止する（Outcome / In Scope / AC を左右する主張の裏付けなしに改善を続けない）

> これは採用しない「spec document review」（= リポジトリ内 `docs/` の網羅レビュー）とは別物であり、外部 web 一次情報の事実確認に限定する。

### Step 2: レビュー（`review-issue` skill）

```
skill: review-issue
inputs:
  issue_number: <LOOP_STATE.issue_number>
  invoked_as_loop: true
```

`review-issue` は `REVIEW_ISSUE_RESULT_V1` を返す:

```yaml
REVIEW_ISSUE_RESULT_V1:
  verdict: approve | needs-fix
  blocking_issues: []
  non_blocking_improvements: []
  diff_proposal:
    add: []
    remove: []
    rewrite: []
```

- `approve` → Step 5（終了処理）へ
- `needs-fix` → `blocking_issues` と `diff_proposal` を LOOP_STATE に記録、Step 4 へ

> Critical Guard: refinement フェーズでは AC を実行しない（review-issue 内で guard 済み）。
> baseline fail は正常動作のため、それを根拠に追加 iteration を要求しない。

### Step 4: 本文改善（`issue-author` SubAgent + `edit-issue` skill）

```
subagent_type: issue-author
inputs:
  task: edit
  issue_number: <LOOP_STATE.issue_number>
  reviewer_feedback_text: <review-issue が返した diff_proposal を整形した文字列>
```

SubAgent は `edit-issue` skill の Procedure を実行し、バックアップ → guards → 本文書き戻し → 変更経緯コメント投稿。

`ISSUE_EDIT_RESULT_V1.status: ok` を確認したら LOOP_STATE.iteration += 1 して Step 1 に戻る。
`failed` の場合は LOOP_STATE.blockers_history に記録し、人間判断（`termination_reason: human_escalation`）。

### Step 5: 終了処理

| termination_reason | アクション |
|---|---|
| `approved` | Issue コメントで「refinement loop 完了」を報告 |
| `max_iterations` | Issue コメントで残存 blockers を提示、人間判断 |
| `human_escalation` | Issue コメントで詳細を提示、人間判断 |

```bash
gh issue comment <issue_number> --body "## issue-refinement-loop: 完了 ($(date -u +%Y-%m-%dT%H:%M:%SZ))

- iteration: <最終 iteration 数>
- verdict: <approve | needs-fix>
- termination_reason: <approved | max_iterations | human_escalation>
- 改善履歴: <improvements_applied の要約>
- 次アクション: <issue-contract-review 起動 / 人間レビュー / 追加 iteration 等>"
```

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

## Guardrails

- 本 skill は control-plane のみ。本文編集は `issue-author` SubAgent + `edit-issue` skill 経由で行う
- `review-issue` から `approve` を受けた時点で即終了（追加 iteration を要求しない）
- `max_iterations` 超過時は必ず fail-close（無限ループ防止）
- baseline fail / 実装前 0 ヒットを誤検知 blocker にしない（Critical Guard 参照）
- adversarial-review は採用しないため、信頼性リスク観点の判定は本ループ範囲外

## Scope 変更シグナル検出（ループ内停止条件）

iteration 中に Issue 本文へ以下が新規追加された場合は、refinement のスコープ拡大兆候として **次イテレーションに進まず即停止**（`termination_reason: human_escalation`）:

- `## In Scope` に新規の機能領域が追加された
- `## Allowed Paths` に新規ディレクトリが追加された（既存と異なるアーキテクチャ層への拡大）
- `## Acceptance Criteria` に新規の検証可能性が低い項目が追加された

## Related

- `.claude/skills/review-issue/SKILL.md` — Step 2 で使う
- `.claude/skills/edit-issue/SKILL.md` — Step 4 で issue-author が使う
- `.claude/skills/ssot-discovery/SKILL.md` — Step 1 で関連 SSOT を探す
- `.claude/skills/issue-contract-review/SKILL.md` — refinement 後の着手前 preflight
- `.claude/agents/codebase-investigator.md` — Step 1 の調査者
- `.claude/agents/web-researcher.md` — Step 1b の外部仕様事実確認者（条件付き）
- `.claude/skills/gemini-cli-headless-delegation/SKILL.md` — Step 1 / 1b の委譲先
- `.claude/agents/issue-author.md` — Step 4 の本文更新者
- `docs/dev/agent-skill-boundaries.md` — オーケストレーター設計原則（control-plane / LOOP_STATE / 人間承認原則）
- `docs/dev/github-ops.md` — GitHub 運用ルール（body-file guard / Parent Mode / コメントテンプレ）
