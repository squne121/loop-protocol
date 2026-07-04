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

## Required LOOP_STATE fields

`LOOP_STATE.anchor_comment` は少なくとも以下を保持すること。

```yaml
anchor_comment:
  url: <string>
  id: <string>
  issue_number: <int>
  html_url: <url>
  api_url: <url>
  user_login: <string>
  author_association: OWNER | MEMBER | COLLABORATOR | CONTRIBUTOR | NONE
  snapshot: <string>
  captured_at: <iso8601>
  fetched_at: <iso8601>
  comment_created_at: <iso8601>
  comment_updated_at: <iso8601>
  preliminary_classification: superseded_by_decision | reframe_in_place | feedback_update_required | human_escalation
  final_classification: superseded_by_decision | reframe_in_place | feedback_update_required | human_escalation | null
  classification_reason: <string | null>
  verified_claims: []
  unresolved_claims: []
  scope_impact: none | amend | replace | ambiguous | null
  requires_fact_check: <bool>
```

`Trusted author policy` は `author_association` に依存するため、省略してはならない。stale comment / untrusted comment 判定に使う `api_url`、`captured_at`、`comment_updated_at`、`snapshot` も同様に必須。

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

## Fact-check contracts (SubAgent-owned)

`anchor_comment` の事実確認（fact-check）に必要な `Input` および `Result` 契約は、`.claude/agents/codebase-investigator.md` の **Fact-check Contract (SubAgent-owned)** セクションを参照すること。

orchestrator は以下の契約を SSOT とし、判定ロジックを再実装しない。

- **Input**: `ANCHOR_COMMENT_CONTEXT_V1`
- **Result**: `ANCHOR_COMMENT_FACT_CHECK_RESULT_V1`

`kind: file` の証跡には `REPO_EVIDENCE_REF_V1` を使用する。

## Trusted author policy

`superseded_by_decision` を確定する人間コメントは `OWNER` / `MEMBER` / `COLLABORATOR` を信頼境界とする。それ以外の投稿者が close / replace を主張する場合は human escalation とする。

## ANCHOR_SCOPE_REFRAME_V1 — artifact 境界と planner 入力境界

`ANCHOR_SCOPE_REFRAME_V1` schema を持つ anchor comment を処理するとき、raw body と planner input の境界を厳密に分離する。

### raw snapshot (artifact 境界)

`.claude/artifacts/issue-refinement-loop/<issue_number>/raw_issue_snapshot.json` に保存する以下のデータは **artifact 境界** に留まる:

- `anchor_comment.snapshot` — raw body テキスト
- `anchor_comment.api_url` — GitHub API URL
- `anchor_comment.captured_at` — 取得日時

これらは直接 planner input に流してはならない。

### planner input (normalized decision のみ)

`run_refinement_preflight.py` が `plan_refinement_loop.py` に渡す `known_context` には、normalized decision のみを含める。

```yaml
# planner input known_context (normalized — raw body NOT included)
known_context:
  anchor_comment_url: <url>          # 所属確認済みの URL
  anchor_comment_hash: <sha256>      # raw body の SHA256 (body 自体は含まない)
  anchor_reframe: true               # ANCHOR_SCOPE_REFRAME_V1 が検出されたフラグ
  classification: feedback_update_required | reframe_in_place
  # raw_body: NG — planner input に含めてはならない
```

### 境界違反の検出

以下のフィールドが planner input の `known_context` に存在する場合は境界違反:

- `raw_body` / `anchor_raw_body` / `raw_anchor_body`
- `snapshot` (anchor comment の raw text)
- comment の JSON 全体を serialize したもの

planner が受け取るのは normalized decision / hash / provenance のみとする。

## Must not

- raw `anchor_comment.snapshot` を Step 4 の `reviewer_feedback_text` に流さない
- `final_classification` の確定責務を SubAgent に委譲しない
- codebase-investigator に mutation を許可しない
- raw anchor comment body を planner input `known_context` に含めない
- `CONTRIBUTOR` / `NONE` / metadata 欠落の comment を trusted anchor として扱わない


## scope_delta_authority_evidence_v1 — freeform human review directive 境界（#1323）

`ANCHOR_SCOPE_REFRAME_V1`（構造化 fenced yaml）専用の `_classify_anchor_scope_reframe()` に加えて、
`run_refinement_preflight.py` の `_build_scope_delta_authority_evidence()` は同じ anchor comment から
**freeform**（構造化 yaml を含まない）な人間レビュー指摘（例: Issue #1270 の Revised Acceptance Criteria 提示）を
`scope_delta_authority_evidence_v1` として正規化する。境界は上記「planner input (normalized decision のみ)」と同じ:

- 渡すのは `directive_markers` / `extracted_directives`（箇条書き行の抽出テキスト）/ `body_sha256` / `boundary_flags` のみ
- raw comment body 全体を `known_context.scope_delta_authority_evidence` に含めない
- anchor URL が対象 Issue の issue comment として構造的に無効な場合（PR review URL との混同、issue 番号不一致等）は
  evidence を生成せず `None` を返す（fail-closed）

詳細な shape は `references/scope-signal-guard.md` の「scope_delta_authority_evidence_v1（正規化済み evidence, AC14）」を参照する。
