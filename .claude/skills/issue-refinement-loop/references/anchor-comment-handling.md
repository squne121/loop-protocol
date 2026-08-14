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

### `_classify_anchor_scope_reframe()` の三値判定（genuine absence と present-but-invalid の区別、#2156）

`_classify_anchor_scope_reframe()` が ```yaml fenced block を解析できなかった場合、以下の 2 つを区別する:

- **genuine absence**（本文に ```yaml fence 自体が存在しない）: `status: not_applicable`、`reason: no_anchor_scope_reframe_v1_payload` を返す。trusted author の freeform コメントとして `_build_scope_delta_authority_evidence()` の freeform lane（上記）が継続利用できる正規の経路であり、hard stop しない。
- **present-but-invalid**（```yaml fence は存在するが YAML 構文エラー、または `schema_version` が `ANCHOR_SCOPE_REFRAME_V1` と一致しない。blockquote 埋め込みの fence を含む）: `status: fail_closed`、`reason: schema_invalid: ...` を返し、既存の invalid-payload 系 fail-closed 挙動（`_structured_anchor_payload_present_but_invalid()` が `True` を返し freeform evidence へのフォールバックを禁止する）を維持する。

この区別は `_anchor_scope_reframe_fence_present()` が行う。genuine absence と present-but-invalid の判定は fence の存在有無のみで行い、fence の内容が valid かどうかには依存しない。

## Operator-Selected Human-Context 継続と Accepted Trust Model（#2086）

### origin lane は operator が宣言する（Accepted Trust Model）

`preflight.run.with_human_context` / `contract_update.run.with_human_context` の origin は、
GitHub comment metadata（`author_association` を含む）から推定するのではなく、
呼び出し側（main control-plane）が runtime invocation でどの lane（`with_human_context` /
`with_agent_report` / unlabeled `with_anchor`）を選んだかによって決まる
（`run_refinement_preflight.py::_resolve_scope_delta_source_kind()`）。

- `author_association: OWNER/MEMBER/COLLABORATOR` は principal trust の補助情報であり、
  「この comment を物理的に人間が書いた」ことの証明ではない（個人開発の repo では同一
  principal が人間のブラウザ操作にも agent の GitHub mutation にも使われ得る）。
- docs / telemetry はこの assertion を `verified_human` と表現しない。表現は常に
  `operator_asserted_human_context`（またはそれと同義の operator-assertion 表現）を使う。
- `with_human_context` lane に投入された URL のみが `scope_delta_authority` 判定で
  `source_kind: issue_comment` に解決される。`with_agent_report` / unlabeled の同一 URL は
  常に `source_kind: generated_by_agent` に解決され、scope mutation authority を得ない
  （`_resolve_scope_delta_source_kind()`。AC6）。

### 構造化 payload なしの freeform scope expansion（AC1/AC3）

`scope_signal_delta.classify_directive_confidence()` は、`operator_asserted_human_context=True`
（= `source_kind: issue_comment`、つまり `with_human_context` lane 経由）かつ箇条書き
（bullet-list）directive を含む freeform コメントを、既知の `_DIRECTIVE_SECTION_MARKERS`
セクション見出し（「Revised Acceptance Criteria」等）が無くても `confidence: explicit` として
扱う。人間に手書き `ANCHOR_SCOPE_REFRAME_V1` payload や既知の見出しを要求しない
（#2084 comment #5249734344 の failure profile — 構造化 payload が無いことだけを理由に
`no_anchor_scope_reframe_v1_payload` / `human_judgment_required` へ落ちる欠陥の是正）。

### read-only investigation による exact path 導出（AC3/AC4）

freeform directive が architecture/workflow-level の scope 拡張を意味論的に要求しているが、
comment 本文に exact backtick path literal を含まない場合、`classify_scope_delta_authority()`
の `investigation_derived_path_literals` キーワード引数（caller-supplied、
`SCOPE_DELTA_AUTHORITY_EVIDENCE_V1` の schema には含めない — schema は本 Issue の
Allowed Paths 外のため変更しない）に、`codebase-investigator`（read-only）が current-main
から導出した exact repository-relative path を渡すことで、`expands_allowed_paths` boundary
を trusted operator lane に限り解除できる
（`scope_signal_delta._has_investigation_derived_allowed_path_literals()`）。

- 適用対象は `authority_category == "human_review_directive"`（trusted OWNER/MEMBER/COLLABORATOR
  かつ `with_human_context` lane）のみ。`with_agent_report` / unlabeled / untrusted author の
  evidence では一切参照されない（AC6/AC5 は緩和されない）。
  `destructive_or_non_idempotent_operation` / `changes_permission_boundary` /
  `changes_external_service_boundary` / `requires_issue_split` の各 boundary はこの緩和の対象外
  であり、従来どおり fail-closed のまま維持される。
- comment 本文が既に exact backtick literal（安全なもの）を含む場合は従来どおり
  `_has_explicit_exact_allowed_path_expansion()` が優先され、この投資調査由来の緩和は使われない。
- comment 本文の backtick literal が unsafe/malformed（Issue #1952 の "mixed literal" fail-closed
  regression）な場合、その literal を investigation-derived path で「洗浄」することはできない
  （`investigation_derived_path_literals` はあくまで comment 側に literal が全く無いケース専用）。

### section-bound patch が作れない場合（AC7）

`classify_scope_delta_authority()` の `contract_patch_plan.operations` は既知 marker が無い
freeform directive では常に空になる（`derive_contract_patch_operations()` は
`directive_markers` が空の evidence をスキップする）。この場合の消費側の振る舞いは既存の
`NEXT_ACTION: issue_editor_required`（`SKILL.md` の「`NEXT_ACTION: issue_editor_required`」節）
であり、新しい termination lane を追加しない。scope expansion だけを理由に termination report
を出してはならない。

### fresh gate は変わらない（AC4/AC11）

上記のいずれの経路でも `route.action == "contract_update_required"` の `implementation_allowed`
は常に `false` である。contract rewrite が完了しても、fresh body readback → fresh
`preflight.run` → fresh review → fresh readiness が全て成功するまで実装は許可されない
（既存の post-update gate と同一）。

## anchor_context.py — 複数ターン分節・候補抽出・取得完全性（#1891）

`anchor_context.py`（`scripts/anchor_context.py`）は、`run_refinement_preflight.py` が生成した既存 snapshot artifact（`anchor_comment.snapshot`）のみを唯一の入力とする pure analyzer である。独自の GitHub API 呼び出しは持たない。

### segment / candidates（分節と候補抽出）

- `segment`: `# you asked` / `# chatgpt response`（大文字小文字・前後空白差を正規化）マーカーで本文を分節し、各セグメントに `speaker: owner | quoted_assistant | unknown` と `start_line` / `end_line` を付与する。マーカーが存在しない区間の `speaker` は常に `unknown`（`owner` への自動昇格はしない）。
- `candidates`: `segment` の出力から箇条書き・prose directive 候補を `source_span` 付きで抽出する。各候補は `relation: unclassified` を持ち、単一の `final_candidate` を選択するロジックは持たない。セグメント間の意味関係分類（add/replace/retract/confirm/narrow/conditional/explanation/quotation/unknown）は Out of Scope。

### scope_delta_decision への経路（#1891 AC4 / #1950 AC1）

`run_refinement_preflight.py` の `_apply_multi_turn_candidate_route()` は、`segment` が検出したマーカー付きセグメントが 2 つ以上あり、かつ `candidates` が複数候補を返した場合に、`known_context.scope_delta_decision` を更新する。単一ターンの通常レビューコメント（マーカーなし）はこの経路の対象外であり、既存の分類結果を維持する。

分岐は anchor comment の投稿者が trusted OWNER（`anchor_author_association == "OWNER"`）かどうかで変わる:

- **trusted OWNER の場合（advisory route）**: `status` を `warn` にし、`reason: multi_turn_anchor_context_trusted_owner_advisory` を設定する。最後の owner-speaker セグメントの `index` / `start_line` / `end_line` は `latest_owner_turn` として **chronology metadata のみ**を記録する。`latest_owner_turn` は `technical_recommendation`（repository facts・diff・test・外部仕様から control-plane が決定する推奨内容）にも `mutation_authorization`（明示的な owner 承認）にも昇格させない。multi-turn であること自体は hard block の理由にならないが、`implementation_go` は `false` のままであり、単独で実装 go を意味しない。この advisory upgrade は `reason: no_anchor_scope_reframe_v1_payload` の場合に、upstream `status` が `fail_closed` / `not_applicable`（#2156 AC2 以降の genuine absence の既定値）のいずれであっても、取得完全性（source fetch complete / hash verified / source ranges covered）が全て確認できたときにのみ発火する。取得完全性が確認できない場合、upstream `status` が `not_applicable`（非 blocking）であっても `status: fail_closed`（blocking、`implementation_go: false`）へ強制され、`not_applicable` のまま残ることはない（#2156 AC6 — 非 blocking への意味的降格を防ぐ）。
- **trusted OWNER 以外の場合（hard block を維持）**: 従来通り `status: fail_closed`、`reason: multi_turn_anchor_context_requires_human_judgment` を設定し、blockers へ `ANCHOR_MULTI_TURN_FAIL_CLOSED` を伝搬する。untrusted author、取得不完全、hash 不一致、source range 欠落の fail-closed 不変条件はこの advisory route の対象外であり、緩和されない。

**chronology ≠ semantic relation ≠ technical recommendation ≠ mutation authorization** という 4 軸の分離が本節の前提である。最後の owner turn という chronology の事実だけで、残り 3 軸（そのターンが持つ意味関係・技術推奨・mutation authorization）を決定してはならない。

### 競合（material conflict）発生時の owner reaction 手順（#1950 AC3/AC4）

trusted OWNER の multi-turn anchor で advisory route に入った後、候補間に material conflict（相互に矛盾する複数の重い変更提案）が存在する場合、root control-plane は以下の手順で owner reaction を読み取り、対象 mutation だけを保留する。

1. **選択肢の提示**: controlled comment lane（既存の投稿権限を持つ control-plane 経由）で、最大 3 案の選択肢と推奨案を Issue コメントとして投稿する。各選択肢には reaction mapping を明記する:
   - `+1`: 選択肢 1（多くは推奨案）を採用
   - `eyes`: 選択肢 2 を採用
   - `rocket`: 選択肢 3 を採用（3 案未満の場合は未使用）
   - `-1`: いずれも採用せず、再提案を要求
2. **owner reaction の全ページ取得**: 対象コメントの reaction 一覧を pagination で全件取得する。GitHub reaction API は排他的なラジオボタンではなく、reacting user と content を持つレコード一覧であるため、1 principal が複数種類の reaction を残せることを前提にする。
3. **principal の固定**: 有効な principal は stable user ID（login 文字列ではない）で固定する。repository owner の stable user ID と一致する reaction だけを owner reaction の候補とする。
4. **drift readback**: 対象 comment / anchor / Issue body を再取得し、選択肢提示時点の snapshot と一致することを確認する。drift が検出された場合は reaction を決定として消費せず、再評価（選択肢の再提示）に戻す。
5. **untrusted reaction の除外**: repository owner の stable user ID と一致しない reaction（= untrusted reaction）は、どれだけ多くても決定として消費しない。owner 以外の reaction は記録のみで、mutation の可否判断には使わない。
6. **有効 reaction の一意性判定**: 同一 principal（owner）が複数種類の有効 reaction（例: `+1` と `eyes` の両方）を残している場合、一意に解決できないため unresolved として再評価に戻す。一意な有効 reaction が確認できるまで、disputed heavy mutation（close / not planned / replacement Issue creation / dependency removal / parent-child change）は適用しない。
7. **保留 mutation の適用**: 一意な有効 owner reaction が確認できた時点で、対応する選択肢の heavy mutation だけを適用する。material conflict と無関係な non-heavy mutation（body improvement 等）は、この手順を待たずに `warn` で継続してよい。

この手順は新規 decision ledger・独立 schema・publisher を追加せず、既存の Issue コメント投稿と GitHub reaction API readback のみで完結させる。

### known_context の取得完全性フィールド（AC5）

`known_context` には以下の 3 フィールドを追加する（「読了」「理解」を主張しない事実ベースの命名）:

- `source_fetch_complete`（bool）: anchor comment 本文が取得済みであることを示す
- `source_hash_verified`（bool）: 取得直後に計算した sha256 と `scope_delta_decision.anchor_comment_hash` が一致することを示す
- `source_ranges_covered`（bool）: `segment` が処理した行範囲の inclusive interval の和集合が `[1, line_count]` と一致することを示す（`anchor_context.compute_source_ranges_covered()` が単純合計ではなく interval merge で判定し、重複区間による過大評価を防ぐ）

### heavy mutation gate（重大変更ゲート・AC6）

`run_refinement_preflight.py` の `_classify_heavy_mutation_gate()` は、`known_context.mutation_category` が heavy mutation カテゴリ（`close` / `not_planned` / `replacement_issue_creation` / `dependency_removal` / `parent_child_change`）に該当する場合、`scope_delta_decision` が owner 発言由来の明示的決定（`status: approved_by_trusted_anchor` かつ `anchor_author_association: OWNER`）でない限り `status: blocked` / `fail_closed: true` を返す。非 heavy な通常改善・追加調査・review 継続カテゴリは、owner 明示的決定がなくても `status: warn` で継続する。
