# Anchor Comment Handling

## Purpose（目的）

`anchor_comment_url` を使うときの snapshot 固定、所属検証、分類、fact-check、rewrite input 正規化の owner file。

## Required flow（必須フロー）

1. URL 末尾から comment id を抽出し、GitHub API で本文・`issue_url`・投稿者 metadata を取得する。
2. `issue_url` から comment の所属 Issue 番号を抽出し、対象 `issue_number` と完全一致を確認する。
3. `LOOP_STATE.anchor_comment` に snapshot と取得 metadata を記録する。
4. `preliminary_classification` を決め、repo / Issue / PR / external spec 事実が絡む場合は `requires_fact_check: true` にする。
5. Step 1 の結果を受けて main thread が `final_classification` を確定する。
6. Step 4 へ渡すのは raw snapshot ではなく、正規化済み `anchor_comment_feedback` のみとする。

## Required LOOP_STATE fields（必須フィールド一覧）

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

## Classification set（分類一覧）

- `superseded_by_decision`
- `reframe_in_place`
- `feedback_update_required`
- `human_escalation`

`superseded_by_decision` は以下をすべて満たすときだけ確定する。

- 人間が close / replace / 前提不採用を明示している
- Outcome を in-place で修正しても目的を維持できない
- 代替先が決定論的に作成または再利用できる

曖昧な場合は fail-closed で `requires_fact_check: true` とする。

## Fact-check contracts (SubAgent-owned)（事実確認契約）

`anchor_comment` の事実確認（fact-check）に必要な `Input` および `Result` 契約は、`.claude/agents/codebase-investigator.md` の **Fact-check Contract (SubAgent-owned)** セクションを参照すること。

orchestrator は以下の契約を SSOT とし、判定ロジックを再実装しない。

- **Input（入力）**: `ANCHOR_COMMENT_CONTEXT_V1`
- **Result（結果）**: `ANCHOR_COMMENT_FACT_CHECK_RESULT_V1`

`kind: file` の証跡には `REPO_EVIDENCE_REF_V1` を使用する。

## Trusted author policy（信頼できる投稿者ポリシー）

`superseded_by_decision` を確定する人間コメントは `OWNER` / `MEMBER` / `COLLABORATOR` を信頼境界とする。それ以外の投稿者が close / replace を主張する場合は human escalation とする。

## ANCHOR_SCOPE_REFRAME_V1 — artifact 境界と planner 入力境界

`ANCHOR_SCOPE_REFRAME_V1` schema を持つ anchor comment を処理するとき、raw body と planner input の境界を厳密に分離する。

### raw snapshot (artifact 境界・生データ保存領域)

`.claude/artifacts/issue-refinement-loop/<issue_number>/raw_issue_snapshot.json` に保存する以下のデータは **artifact 境界** に留まる:

- `anchor_comment.snapshot` — raw body テキスト
- `anchor_comment.api_url` — GitHub API URL
- `anchor_comment.captured_at` — 取得日時

これらは直接 planner input に流してはならない。

### planner input (normalized decision のみ・正規化済み決定のみ渡す)

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

## Must not（禁止事項）

- raw `anchor_comment.snapshot` を Step 4 の `reviewer_feedback_text` に流さない
- `final_classification` の確定責務を SubAgent に委譲しない
- codebase-investigator に mutation を許可しない
- raw anchor comment body を planner input `known_context` に含めない
- `CONTRIBUTOR` / `NONE` / metadata 欠落の comment を trusted anchor として扱わない


## scope_delta_authority_evidence_v1 — freeform human review directive 境界（#1323、自由記述レビュー指摘の境界）

`ANCHOR_SCOPE_REFRAME_V1`（構造化 fenced yaml）専用の `_classify_anchor_scope_reframe()` に加えて、
`run_refinement_preflight.py` の `_build_scope_delta_authority_evidence()` は同じ anchor comment から
**freeform**（構造化 yaml を含まない）な人間レビュー指摘（例: Issue #1270 の Revised Acceptance Criteria 提示）を
`scope_delta_authority_evidence_v1` として正規化する。境界は上記「planner input (normalized decision のみ)」と同じ:

- 渡すのは `directive_markers` / `extracted_directives`（箇条書き行の抽出テキスト）/ `body_sha256` / `boundary_flags` のみ
- raw comment body 全体を `known_context.scope_delta_authority_evidence` に含めない
- anchor URL が対象 Issue の issue comment として構造的に無効な場合（PR review URL との混同、issue 番号不一致等）は
  evidence を生成せず `None` を返す（fail-closed）

詳細な shape は `references/scope-signal-guard.md` の「scope_delta_authority_evidence_v1（正規化済み evidence, AC14）」を参照する。

## anchor_context.py — 複数ターン分節・候補抽出・取得完全性（#1891）

`anchor_context.py`（`scripts/anchor_context.py`）は、`run_refinement_preflight.py` が生成した既存 snapshot artifact（`anchor_comment.snapshot`）のみを唯一の入力とする pure analyzer である。独自の GitHub API 呼び出しは持たない。

### segment / candidates

- `segment`: `# you asked` / `# chatgpt response`（大文字小文字・前後空白差を正規化）マーカーで本文を分節し、各セグメントに `speaker: owner | quoted_assistant | unknown` と `start_line` / `end_line` を付与する。マーカーが存在しない区間の `speaker` は常に `unknown`（`owner` への自動昇格はしない）。
- `candidates`: `segment` の出力から箇条書き・prose directive 候補を `source_span` 付きで抽出する。各候補は `relation: unclassified` を持ち、単一の `final_candidate` を選択するロジックは持たない。セグメント間の意味関係分類（add/replace/retract/confirm/narrow/conditional/explanation/quotation/unknown）は Out of Scope。

### scope_delta_decision への route（AC4）

`run_refinement_preflight.py` の `_apply_multi_turn_candidate_route()` は、`segment` が検出したマーカー付きセグメントが 2 つ以上あり、かつ `candidates` が複数候補を返した場合に限り、`known_context.scope_delta_decision.status` を既存の `fail_closed`（human 判断待ち）に上書きする。単一ターンの通常レビューコメント（マーカーなし）はこの経路の対象外であり、既存の分類結果を維持する。

### known_context の取得完全性フィールド（AC5）

`known_context` には以下の 3 フィールドを追加する（「読了」「理解」を主張しない事実ベースの命名）:

- `source_fetch_complete`（bool）: anchor comment 本文が取得済みであることを示す
- `source_hash_verified`（bool）: 取得直後に計算した sha256 と `scope_delta_decision.anchor_comment_hash` が一致することを示す
- `source_ranges_covered`（bool）: `segment` が処理した行範囲の inclusive interval の和集合が `[1, line_count]` と一致することを示す（`anchor_context.compute_source_ranges_covered()` が単純合計ではなく interval merge で判定し、重複区間による過大評価を防ぐ）

### heavy mutation gate（AC6）

`run_refinement_preflight.py` の `_classify_heavy_mutation_gate()` は、`known_context.mutation_category` が heavy mutation カテゴリ（`close` / `not_planned` / `replacement_issue_creation` / `dependency_removal` / `parent_child_change`）に該当する場合、`scope_delta_decision` が owner 発言由来の明示的決定（`status: approved_by_trusted_anchor` かつ `anchor_author_association: OWNER`）でない限り `status: blocked` / `fail_closed: true` を返す。非 heavy な通常改善・追加調査・review 継続カテゴリは、owner 明示的決定がなくても `status: warn` で継続する。
