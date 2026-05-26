# Anchor Comment Handling

## Purpose

`anchor_comment_url` を使うときの snapshot 固定、所属検証、分類、fact-check、rewrite input 正規化の owner file。

## Required flow

1. URL 末尾から comment id を抽出し、GitHub API で本文・`issue_url`・投稿者 metadata を取得する。
2. `issue_url` から comment の所属 Issue 番号を抽出し、対象 `issue_number` と完全一致を確認する。
3. `LOOP_STATE.anchor_comment` に snapshot と取得 metadata を記録する。
4. `preliminary_classification` を決め、repo / Issue / PR / external spec 事実が絡む場合は `requires_fact_check: true` にする。
5. Step 1 の結果を受けて main thread が `final_classification` を確定する。
6. Step 4 へ渡すのは raw snapshot ではなく、正規化済み `anchor_comment_feedback` のみとする。

## Classification set

- `superseded_by_decision`
- `reframe_in_place`
- `feedback_update_required`
- `human_escalation`

`superseded_by_decision` は以下をすべて満たすときだけ確定する。

- 人間が close / replace / 前提不採用を明示している
- Outcome を in-place で修正しても目的を維持できない
- 代替先が決定論的に作成または再利用できる

曖昧な場合は fail-closed で `requires_fact_check: true` とする。

## Fact-check contracts

### ANCHOR_COMMENT_CONTEXT_V1

```yaml
ANCHOR_COMMENT_CONTEXT_V1:
  schema_version: 1
  source_issue_number: <int>
  anchor_comment_url: <url>
  snapshot: <string>
  preliminary_classification: superseded_by_decision | reframe_in_place | feedback_update_required | human_escalation
  classification_reason: <string>
  required_checks:
    - claim_id: C1
      description: <what to verify>
      type: repo_fact | issue_pr_fact | external_spec | human_decision
      critical: true | false
```

### ANCHOR_COMMENT_FACT_CHECK_RESULT_V1

```yaml
ANCHOR_COMMENT_FACT_CHECK_RESULT_V1:
  schema_version: 1
  status: ok | inconclusive | failed
  claims:
    - claim_id: C1
      verdict: supported | contradicted | inconclusive | not_checkable
      scope_impact: none | amend | replace | ambiguous
      evidence:
        - kind: file | issue | pr | comment | web
          ref: <REPO_EVIDENCE_REF_V1 or opaque reference>
          summary: <why it matters>
      critical: true | false
  recommended_final_classification: superseded_by_decision | reframe_in_place | feedback_update_required | human_escalation
  unresolved_risks: []
```

`kind: file` の `ref` は `REPO_EVIDENCE_REF_V1` を使う。schema の再定義はせず、owner file を参照する。

## Trusted author policy

`superseded_by_decision` を確定する人間コメントは `OWNER` / `MEMBER` / `COLLABORATOR` を信頼境界とする。それ以外の投稿者が close / replace を主張する場合は human escalation とする。

## Must not

- raw `anchor_comment.snapshot` を Step 4 の `reviewer_feedback_text` に流さない
- `final_classification` の確定責務を SubAgent に委譲しない
- codebase-investigator に mutation を許可しない
