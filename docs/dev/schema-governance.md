---
title: Schema Governance  # スキーマ変更のガバナンスルール定義
status: active
related_issue: "#135"
---

# Schema Governance

このドキュメントは LOOP_PROTOCOL における schema 変更の governance ルール・初期 schema リスト・consumer inventory 義務を定義する SSOT である。

## Schema Definition（schema の定義）

本プロジェクトでいう **schema** は、producer と consumer の境界を越えて parse / validate / serialize される machine-readable contract を指す。

以下のいずれかに該当するものを schema として扱う:

- Markdown 内 YAML フロントマター（契約スキーマとして参照されるもの）
- JSON / YAML / NDJSON ファイルで複数ファイル間のインターフェース境界となるもの
- log artifact / PR comment YAML（例: `LOOP_VERDICT` YAML、`TEST_VERDICT_MACHINE` YAML）
- Markdown table contract（例: SKILL.md 内の入力・出力仕様テーブル）
- シェルスクリプト間の YAML 契約（例: `verify_acp_roundtrip.sh` が読む YAML 構造）

**非 schema（スコープ外）**:

- 内部変数名の変更（単一ファイル内のみ影響）
- コメント・説明文のみの変更

## Initial Known Schemas（初期 schema リスト）

| Schema ID | 定義場所 | Producer | Consumer | Detection patterns |
|---|---|---|---|---|
| `issue_contract/v1` | GitHub Issue 本文（`## Machine-Readable Contract` YAML ブロック） | issue-author skill | issue-contract-review, implement-issue, pr-review-judge | `rg -n "issue_contract\|Machine-Readable Contract\|contract_schema_version" .` |
| `delegation_request_v1` | `.claude/skills/gemini-cli-headless-delegation/` | implement-issue, codebase-investigator | gemini-cli 実行 wrapper | `rg -n "delegation_request_v1\|delegation_request" .claude` |
| `delegation_result/v1` | `.claude/skills/gemini-cli-headless-delegation/` | gemini-cli 実行 wrapper | web-researcher, codebase-investigator, impl-review-loop | `rg -n "delegation_result/v1\|result_surface\|transport_details\|failure_class\|structured_events" .` |
| `acp_result_v1` | `.claude/skills/gemini-cli-headless-delegation/`（delegation_result/v1 正規化前 internal transport） | gemini-cli 実行 wrapper | delegation_result/v1 正規化処理（PR #81 の事故対象） | `rg -n "acp_result_v1\|acp_result\|--acp" .` |
| `LOOP_VERDICT` | `.claude/skills/pr-review-judge/SKILL.md` Verdict コメントテンプレート | pr-review-judge | impl-review-loop | `rg -n "LOOP_VERDICT\|verdict:\|reviewed_head_sha" .` |
| `TEST_VERDICT_MACHINE v1` | `.claude/skills/test-runner/`（または test-runner SubAgent） | test-runner SubAgent | pr-review-judge, impl-review-loop | `rg -n "TEST_VERDICT_MACHINE\|verification_commands_pass\|verification_commands_fail" .` |
| `IMPLEMENT_RESULT_V1` | `.claude/skills/implement-issue/SKILL.md` | implement-issue | impl-review-loop | `rg -n "IMPLEMENT_RESULT_V1\|IMPLEMENT_RESULT" .claude` |
| `contract_schema_version: v1` | GitHub Issue 本文（`## Machine-Readable Contract`） | issue-author skill | issue-contract-review | `rg -n "contract_schema_version" .` |
| `Runtime Verification Applicability` | `docs/dev/runtime-verification-policy.md` | issue-author skill / human | implement-issue, pr-review-judge, impl-review-loop | `rg -n "Runtime Verification Applicability\|runtime_verification_applicability\|decision: immediate\|decision: deferred\|decision: not_applicable" .` |
| `Safety Claim Matrix` | `.github/pull_request_template.md`, `.claude/skills/open-pr/SKILL.md` | PR 作成者 | open-pr, pr-review-judge | `rg -n "Safety Claim Matrix\|Not controlled\|E_SAFETY_CLAIM_MATRIX_MISSING" .claude .github docs` |
| `model_routing.yaml` | `.claude/skills/gemini-cli-headless-delegation/model_routing.yaml`（推定） | model routing 設定管理者 | gemini-cli 実行 wrapper, test_model_routing.py | `rg -n "model_routing\|model_routing\.yaml\|routing_config" .` |
| `runtime-verification artifact log` | `docs/dev/runtime-verification-policy.md` | implement-issue（runtime verification 実行時） | pr-review-judge（Runtime Verification Evidence 確認） | `rg -n "runtime.verification.artifact\|Runtime Verification Evidence\|verification_route" .` |
| `pr_body_schema/schema_change_applicability/v1` | `.github/pull_request_template.md`, `.claude/skills/open-pr/SKILL.md` | PR author / open-pr skill | pr-review-judge, open-pr procedure, future open_pr.py (#170) | `rg -n "Schema Change Applicability\|schema_change_applicability" .` |
| `pr_body_schema/schema_consumer_inventory/v1` | `.github/pull_request_template.md`, `.claude/skills/open-pr/SKILL.md` | PR author / open-pr skill | pr-review-judge, open-pr procedure, future open_pr.py (#170) | `rg -n "Schema Consumer Inventory\|Consumer 更新状況\|Compatibility Decision" .` |
| `agent_session_manifest/v1` | `docs/schemas/agent-session-manifest.md` | Claude Code hook-based ledger, human/AI GitHub Issue or PR comment | pr-review-judge, impl-review-loop, pilot smoke test issue, future aggregation script | `rg -n "agent_session_manifest/v1\|agent_session_manifest:v1\|agent-session-manifest" .` |
| `PR_REVIEW_GATE_RESULT_V1` | `.claude/skills/pr-review-judge/references/pr-review-gate-result-schema.yml` | check_pr_review_gates.py | pr-review-judge, impl-review-loop | `rg -n "PR_REVIEW_GATE_RESULT_V1\|schema_version.*RESULT" .` |
| `temp_residue_classification/v1` | `schemas/temp_residue_classification_v1.schema.json` | `scripts/agent-ops/temp_residue_classifier.py` | post-merge-cleanup（`classify-git-state.py`）、将来の実削除 executor（out of scope） | `rg -n "temp_residue_classification/v1\|temp_residue_classifier" .` |
| `temp_residue_owner/v1` | `schemas/temp_residue_owner_v1.schema.json` | agent session（`self_claim`）または `trusted_materializer` | `scripts/agent-ops/temp_residue_classifier.py`（marker 評価） | `rg -n "temp_residue_owner/v1\|temp_residue_marker" .` |
| `TERMINATION_REPORT_RENDER_RESULT_V1` | `.claude/skills/issue-refinement-loop/scripts/render_termination_report.py` | render_termination_report.py | publish_termination_report.py, issue-refinement-loop Step 5 | `rg -n "TERMINATION_REPORT_RENDER_RESULT_V1" .claude/skills/issue-refinement-loop/` |
| `delegation_audit_v1` | `.claude/skills/gemini-cli-headless-delegation/scripts/run_gemini_headless.py`（`--audit-log` / `DELEGATION_AUDIT_LOG_PATH` で明示有効化する監査ログ JSONL） | run_gemini_headless.py（`run_delegation()` トップレベル呼び出し） | `.claude/skills/gemini-cli-headless-delegation/tests/test_delegation_audit_schema.py`（本 PR で追加した唯一の consumer。既存 `delegation_result/v1` の consumer とはファイルレベルで分離） | `rg -n "delegation_audit_v1\|DELEGATION_AUDIT_LOG_PATH\|_build_delegation_audit_record" .claude/skills/gemini-cli-headless-delegation` |
| `delegation_fanout_request_v1` | `.claude/skills/gemini-cli-headless-delegation/scripts/fan_out_orchestrator.py`（closed schema; `subtasks[]` は既存 `delegation_request_v1` をそのまま保持し、planner mode は対象外） | `build_fanout_request.py` / 呼び出し元（実装/調査 orchestration の呼び出し側） | `fan_out_orchestrator.py`（`run_fanout()` / `validate_fanout_request()`） | `rg -n "delegation_fanout_request_v1" .claude/skills/gemini-cli-headless-delegation` |
| `delegation_fanout_result_v1` | `.claude/skills/gemini-cli-headless-delegation/scripts/fan_out_orchestrator.py`（`status: success\|partial_success\|failed\|cancelled`、`counts`、`results[]`、`failures[]`、`deduplicated_aliases` を持つ決定的な merge 結果） | `fan_out_orchestrator.py`（`run_fanout()`） | 呼び出し元（実装/調査 orchestration の呼び出し側） | `rg -n "delegation_fanout_result_v1" .claude/skills/gemini-cli-headless-delegation` |
| `REVIEW_COMPACT_VALIDATION_RESULT_V1` | `.claude/skills/issue-refinement-loop/scripts/validate_review_compact_output.py` | validate_review_compact_output.py（`review_compact.validate` 経由で orchestrator が呼び出す） | issue-refinement-loop（Step 2/2a routing gate）、build_refinement_phase_state.py（`--review-validation-result-path` 経由の review-phase 構造的ゲート、#1507 AC24） | `rg -n "REVIEW_COMPACT_VALIDATION_RESULT_V1\|validation_status\|review_compact.validate\|review-validation-result-path" .claude/skills/issue-refinement-loop` |
| `PARENT_REPLAY_BINDING_ARTIFACT_V1` | `.claude/skills/issue-refinement-loop/scripts/parent_replay_binding.py` | parent_replay_binding.py（issue-refinement-loop orchestrator が parent-owned inventory と child の bounded `REVIEWER_BLOCKER_CLAIM_V1` claim のみを渡して呼び出す） | validate_review_compact_output.py（`--v2` の required `--binding-artifact-file`。`expected_replay_next_state` / `expected_parent_binding_digest` の照合入力）、reviewer_claim_replay_state_store.py（`--write-v2` の `PARENT_REPLAY_NEXT_STATE` 永続化ソース） | `rg -n "PARENT_REPLAY_BINDING_ARTIFACT_V1\|binding_digest\|parent_replay_binding" .claude/skills/issue-refinement-loop` |
| `REVIEWER_BLOCKER_CLAIM_V1` | `.claude/skills/issue-refinement-loop/scripts/compact_review_result.py`（`REVIEWER_BLOCKER_CLAIM` stdout field） | issue-reviewer SubAgent（`compact_review_result.py` 経由。`{schema, body_sha256, blockers: [...]}` のみ。`findings` / `checker_evidence` / `deterministic_checks` は禁止 — additionalProperties: false で fail-closed 拒否） | parent_replay_binding.py（`validate_reviewer_blocker_claim()` で shape 検証してから replay 入力にする。監査目的でのみ envelope に残る） | `rg -n "REVIEWER_BLOCKER_CLAIM_V1\|REVIEWER_BLOCKER_CLAIM" .claude/skills/issue-refinement-loop` |
| `ISSUE_REVIEW_RESULT_COMPACT_V2` / `REVIEW_COMPACT_VALIDATION_RESULT_V2` | `.claude/skills/issue-refinement-loop/scripts/validate_review_compact_output.py`（`NEEDS_FIX_FIELDS_V2` / `validate_review_compact_output_v2` / `SCHEMA_V2`） | `emit_parent_review_envelope_v2.py`（issue-refinement-loop orchestrator が唯一の呼び出し元。strict 検証済みの child intermediate と `PARENT_REPLAY_BINDING_ARTIFACT_V1` から `PARENT_REPLAY_*` 6行を決定論的に導出し15行 envelope を組み立てる。Issue #1541 — 旧来の orchestrator 手動 f-string assembly は production 経路から廃止） | issue-refinement-loop（Step 2a V2 routing gate。routing は `PARENT_REPLAY_*` のみを参照する）、reviewer_claim_replay_state_store.py（`--write-v2`） | `rg -n "REVIEW_COMPACT_VALIDATION_RESULT_V2\|PARENT_REPLAY_BINDING_DIGEST\|NEEDS_FIX_FIELDS_V2" .claude/skills/issue-refinement-loop` |
| `EMIT_PARENT_REVIEW_ENVELOPE_V2_FAILURE` | `.claude/skills/issue-refinement-loop/scripts/emit_parent_review_envelope_v2.py` | emit_parent_review_envelope_v2.py（`main()` の contract-invalid / runtime-error stderr diagnostic） | issue-refinement-loop orchestrator（Step 2a、emitter 非 0 exit 時の human-readable/machine-readable diagnostic） | `rg -n "EMIT_PARENT_REVIEW_ENVELOPE_V2_FAILURE" .claude/skills/issue-refinement-loop/scripts` |
| `delegation_model_policy/v1` | `.claude/skills/gemini-cli-headless-delegation/scripts/build_request.py`（`model-policy` サブコマンド）、`.claude/skills/gemini-cli-headless-delegation/references/model-routing.md` | build_request.py の `build_model_policy()` / `main_model_policy()`（読み取り専用・副作用なしの dry-run inspector。`run_gemini_headless.py` の `load_model_routing()` / `resolve_model_chain()` / `PROVIDER_AUTO_*` を直接呼び出す） | 人間オペレータ・エージェント（`model-policy` CLI 呼び出しの stdout consumer）、test_build_request_model_policy.py | `rg -n "delegation_model_policy/v1\|model-policy\|build_model_policy" .claude/skills/gemini-cli-headless-delegation` |

### 信頼境界（Trust boundary、Issue #1532）

- `parent_replay_binding.py` は **parent-local replay integrity binding** を提供する。issue-reviewer SubAgent（同一 OS UID の child プロセス）の producer identity・署名・鍵管理・supply-chain provenance を証明するものではない（Safety Claim Matrix の対象外。「provenance attestation」という語は本 Issue の保証範囲を超えて誤解を招くため使用しない）。
- 親が信頼する唯一の child 由来入力は `REVIEWER_BLOCKER_CLAIM_V1`（`body_sha256` と `blockers[].{reviewer_blocker_code,message,line_start,line_end}` のみ）。`findings` / `checker_evidence` / `deterministic_checks` / readiness 結果は child claim から一切受け付けない（`additionalProperties: false` で fail-closed）。
- `deterministic_backed` 判定は常に parent 自身が取得した `readiness_result` / `vc_syntax_result` / `vc_preflight_result` のみを根拠とする。

## temp_residue_classification/v1 と temp_residue_owner/v1 詳細登録

```yaml
schema_id: temp_residue_classification/v1
definition: schemas/temp_residue_classification_v1.schema.json
related_issue: "#1417"
producer:
  - scripts/agent-ops/temp_residue_classifier.py
consumer:
  - .claude/skills/post-merge-cleanup/scripts/classify-git-state.py（temp_residue_classification field）
  - .claude/skills/post-merge-cleanup/SKILL.md
compatibility:
  breaking_changes:
    - remove_required_field
    - rename_field
    - narrow_type
    - change_recommendation_semantics（report_only / eligible_for_delete の意味変更）
detection_patterns:
  - 'temp_residue_classification/v1'
  - 'temp_residue_classifier'
validation_commands:
  - "uv run --locked pytest tests/agent_ops/test_temp_residue_classifier.py -q"
  - "uv run --locked pytest schemas/tests/test_catalog.py -q"
notes:
  - "classifier は read-only。os.unlink / os.rmdir / shutil.rmtree / mutation subprocess を呼ばない。"
  - "recommendation: eligible_for_delete は advisory であり deletion authorization ではない。"

schema_id: temp_residue_owner/v1
definition: schemas/temp_residue_owner_v1.schema.json
related_issue: "#1417"
producer:
  - agent session (self_claim, デフォルト)
  - trusted_materializer（将来の実削除 executor 設計時に導入予定。out of scope）
consumer:
  - scripts/agent-ops/temp_residue_classifier.py（marker evaluate）
compatibility:
  breaking_changes:
    - remove_required_field
    - rename_field
    - narrow_type
    - change_trust_model（accidental isolation → authorization への切替）
detection_patterns:
  - 'temp_residue_owner/v1'
  - 'temp_residue_marker'
validation_commands:
  - "uv run --locked pytest tests/agent_ops/test_temp_residue_classifier.py -q -k owner_marker_schema"
notes:
  - "本 schema は accidental isolation model のみを実装する。marker は deletion authority ではない。"
  - "duplicate JSON key・NaN/Infinity・oversized・symlink・group/other writable marker は invalid として扱う。"
```

## TERMINATION_REPORT_RENDER_RESULT_V1 詳細登録

```yaml
schema_id: TERMINATION_REPORT_RENDER_RESULT_V1
definition: .claude/skills/issue-refinement-loop/scripts/render_termination_report.py
related_issue: "#692"
producer:
  - render_termination_report.py
consumer:
  - publish_termination_report.py (subprocess caller)
  - issue-refinement-loop Step 5 (termination report publish flow)
compatibility:
  breaking_changes:
    - schema field rename or removal
    - schema_version increment
    - publishable semantics change (true/false invariant)
    - body=null when publishable=true (invariant violation)
  non_breaking_changes:
    - adding new optional fields
    - adding new reason_code values
    - adding new termination_reason values
detection_patterns:
  - 'TERMINATION_REPORT_RENDER_RESULT_V1'
  - 'publishable.*true.*body'
  - 'reason_code.*guard_fail'
validation_commands:
  - "rg 'TERMINATION_REPORT_RENDER_RESULT_V1' .claude/skills/issue-refinement-loop/scripts/render_termination_report.py"
  - "rg 'TERMINATION_REPORT_RENDER_RESULT_V1' .claude/skills/issue-refinement-loop/scripts/publish_termination_report.py"
  - "uv run --locked pytest .claude/skills/issue-refinement-loop/tests/test_publish_termination_report.py -q"
notes:
  - "publishable=true requires body to be non-null non-empty string (AC4 invariant)"
  - "publishable=false requires body to be null (AC4 invariant)"
  - "publisher (publish_termination_report.py) must validate schema/schema_version before posting"
  - "stdout: machine JSON only; stderr: diagnostics only (no publishable body to stderr)"
```

## agent_session_manifest/v1 詳細登録

```yaml
schema_id: agent_session_manifest/v1
definition: docs/schemas/agent-session-manifest.md
related_issue: "#243"
producer:
  - Claude Code hook-based ledger
  - human/AI GitHub Issue or PR comment
consumer:
  - pr-review-judge
  - impl-review-loop
  - pilot smoke test issue
  - future aggregation script
detection_patterns:
  - 'agent_session_manifest/v1'
  - 'agent_session_manifest:v1'
  - 'agent-session-manifest'
schema_json: "docs/schemas/agent-session-manifest.schema.json"
fixtures: "docs/schemas/examples/"
test_file: "tests/agent-session-manifest.test.ts"
validation_commands:
  - "rg 'agent_session_manifest/v1' docs/schemas/agent-session-manifest.md"
  - "rg '<!-- agent_session_manifest:v1 start -->' docs/schemas/agent-session-manifest.md"
  - "test -f docs/schemas/agent-session-manifest.schema.json && echo 'schema json exists'"
  - "pnpm test -- --reporter=verbose 2>&1 | grep agent-session-manifest"
notes:
  - "GitHub comment への raw transcript 禁止ポリシー: docs/schemas/agent-session-manifest.md#github-comment-への-raw-transcript-禁止ポリシー"
  - "phase.main_loop と phase.ledger_phase の対応表: docs/schemas/agent-session-manifest.md#main-loop-phase-と-subagent-execution-ledger-phase-の対応表"
  - "token_usage.availability: unavailable を 0 と偽装しないこと（docs/schemas/agent-session-manifest.md 参照）"
```

## REVIEW_COMPACT_VALIDATION_RESULT_V1 詳細登録

```yaml
schema_id: REVIEW_COMPACT_VALIDATION_RESULT_V1
definition: .claude/skills/issue-refinement-loop/scripts/validate_review_compact_output.py
related_issue: "#1507"
producer:
  - validate_review_compact_output.py（`review_compact.validate` registry entry, command_registry.py）
consumer:
  - .claude/skills/issue-refinement-loop/SKILL.md（Step 2 / Step 2a: validator-first fail-closed routing）
  - build_refinement_phase_state.py（`--review-validation-result-path`; review phase 構造的ゲート, AC24）
compatibility:
  breaking_changes:
    - validation_status の意味変更（valid/invalid の判定条件変更）
    - envelope_kind の値集合変更
    - normalized_payload のキー削除・rename
    - REPLAY_VERDICT 5値 enum の値変更（reviewer_claim_replay.py との同期崩れ）
  non_breaking_changes:
    - violations[] への新規 code 追加
    - artifact_path_policy への新規フィールド追加
detection_patterns:
  - 'REVIEW_COMPACT_VALIDATION_RESULT_V1'
  - 'validation_status'
  - 'review_compact.validate'
  - 'review-validation-result-path'
validation_commands:
  - "uv run --locked pytest .claude/skills/issue-refinement-loop/tests/test_validate_review_compact_output.py -q"
  - "uv run --locked pytest .claude/skills/issue-refinement-loop/tests/test_review_compact_registry_entry.py -q"
  - "uv run --locked pytest .claude/skills/issue-refinement-loop/tests/test_refinement_phase_gate_validation_seam.py -q"
notes:
  - "producer-failure envelope は構文解析可能だが validation_status は常に invalid（#1165 SSOT）。"
  - "input_sha256 / normalized_payload は format-only 検証であり provenance 証明ではない。"
  - "ARTIFACT の issue namespace 束縛（`--issue-number`）は active issue 以外への読み違いを防ぐが、実ファイル存在確認は行わない（#1472 isolation worktree 境界）。"
```

## PARENT_REPLAY_BINDING_ARTIFACT_V1 / ISSUE_REVIEW_RESULT_COMPACT_V2 詳細登録

```yaml
schema_id: PARENT_REPLAY_BINDING_ARTIFACT_V1
definition: .claude/skills/issue-refinement-loop/scripts/parent_replay_binding.py
related_issue: "#1532"
producer:
  - parent_replay_binding.py（issue-refinement-loop orchestrator が唯一の呼び出し元。child isolation worktree からは呼ばれない）
consumer:
  - validate_review_compact_output.py（`--v2` の `--binding-artifact-file` 経由。artifact から独立に再計算した expected_replay_next_state / expected_parent_binding_digest を envelope の `PARENT_REPLAY_NEXT_STATE` / `PARENT_REPLAY_BINDING_DIGEST` と exact 照合する）
  - reviewer_claim_replay_state_store.py（`--write-v2 --expected-parent-binding-digest` の `PARENT_REPLAY_NEXT_STATE` 永続化ソース）
trust_boundary:
  - "review_result / readiness_result / vc_syntax_result / vc_preflight_result / previous_state はすべて呼び出し元（orchestrator）が自ら取得・保存・readback した parent-owned inventory であり、child isolation worktree の raw artifact ファイルは一切読まない（#1472 isolation boundary の継承）。"
  - "review_result は child SubAgent（issue-reviewer）が返す bounded `REVIEWER_BLOCKER_CLAIM_V1`（`{schema, body_sha256, blockers: [...]}` のみ、reviewer の blocker 主張）を含み得るが、それ自体は `reviewer_claim_replay.analyze()` の入力の一部として扱われるだけであり、`PARENT_REPLAY_NEXT_STATE` の算出主体は常に parent 側の `analyze()` 呼び出しである（child が直接 `PARENT_REPLAY_*` フィールドを計算・主張することは一切ない — parent-only fields）。"
  - "この binding は `REVIEWER_BLOCKER_CLAIM_V1` を返す child claim（needs-fix claim）に限定される。approve envelope には replay/binding フィールドを一切追加しない（V1 approve grammar は不変）。"
  - "`binding_digest`（envelope 上は `PARENT_REPLAY_BINDING_DIGEST`）は `REPLAY_ARTIFACT_DIGEST`（child stdout digest、#1507/#1519 で導入・意味不変、V1 needs-fix envelope 専用フィールド）とは別 field であり、両者を混同・代替してはならない。"
non_guarantees:
  - "同一 OS UID の child に対する暗号学的な producer identity 証明・署名・鍵管理は行わない（#1532 Out of Scope）。"
  - "外部 attestation service とのバインドは行わない。"
  - "issue-refinement-loop 以外の loop への一般化は保証しない。"
compatibility:
  breaking_changes:
    - binding_digest の計算方式変更（canonical_json_bytes の sort_keys / separators / ensure_ascii 変更）
    - input_digests のキー削除・rename
    - replay_next_state のスキーマ変更（reviewer_claim_replay.py の next_state 形状変更と同期が必要）
  non_breaking_changes:
    - input_digests への新規オプショナルフィールド追加
detection_patterns:
  - 'PARENT_REPLAY_BINDING_ARTIFACT_V1'
  - 'binding_digest'
  - 'parent_replay_binding'
validation_commands:
  - "uv run --locked pytest .claude/skills/issue-refinement-loop/tests/test_parent_replay_binding.py -q"
  - "uv run --locked pytest .claude/skills/issue-refinement-loop/tests/test_review_compact_v2_contract.py -q"
  - "uv run --locked pytest .claude/skills/issue-refinement-loop/tests/test_parent_replay_isolation_runtime.py -q"
notes:
  - "generated_at 相当の wall-clock 値は canonical payload に含めない（iteration_id が同一入力の repeat-run 同一性を担保する）。"
  - "recompute_binding_digest() は tamper 検出用の独立再計算関数であり、artifact 自身の binding_digest フィールドを信用しない検証で使う。"

schema_id: ISSUE_REVIEW_RESULT_COMPACT_V2 / REVIEW_COMPACT_VALIDATION_RESULT_V2
definition: .claude/skills/issue-refinement-loop/scripts/validate_review_compact_output.py
related_issue: "#1532"
producer:
  - issue-refinement-loop orchestrator（child が返す `REVIEWER_BLOCKER_CLAIM_V1` needs-fix claim に、自ら計算した `PARENT_REPLAY_VERDICT` / `PARENT_REPLAY_ROUTING` / `PARENT_REPLAY_SHOULD_CONSUME` / `PARENT_REPLAY_BODY_SHA256` / `PARENT_REPLAY_NEXT_STATE` / `PARENT_REPLAY_BINDING_DIGEST` の6行を追記して15行の V2 envelope を組み立てる。child SubAgent はこれら `PARENT_REPLAY_*` フィールドの producer ではない）
consumer:
  - issue-refinement-loop（Step 2a V2 routing gate。routing は `PARENT_REPLAY_*` のみを参照し、`REVIEWER_BLOCKER_CLAIM` は audit-only で routing には使わない）
  - reviewer_claim_replay_state_store.py（`--write-v2 --expected-parent-binding-digest`）
compatibility:
  breaking_changes:
    - NEEDS_FIX_FIELDS_V2 のフィールド集合・順序変更
    - REPLAY_ARTIFACT_DIGEST（V1 needs-fix envelope の child stdout digest）と PARENT_REPLAY_BINDING_DIGEST（V2 needs-fix envelope の parent 計算 digest）の意味の統合・置換
  non_breaking_changes:
    - approve envelope grammar への影響（V2 では approve に replay/binding field を追加しない、が非破壊的に維持される限り）
detection_patterns:
  - 'REVIEW_COMPACT_VALIDATION_RESULT_V2'
  - 'PARENT_REPLAY_BINDING_DIGEST'
  - 'NEEDS_FIX_FIELDS_V2'
  - 'validate_review_compact_output_v2'
validation_commands:
  - "uv run --locked pytest .claude/skills/issue-refinement-loop/tests/test_validate_review_compact_output.py -q"
  - "uv run --locked pytest .claude/skills/issue-refinement-loop/tests/test_review_compact_v2_contract.py -q"
notes:
  - "V1 envelope（approve/needs_fix/producer_failure）は validate_review_compact_output_v2 経由でも完全に不変（V1 exact match は V1 結果をそのまま返す）。"
  - "expected_replay_next_state / expected_parent_binding_digest は呼び出し元が PARENT_REPLAY_BINDING_ARTIFACT_V1 から独立に計算した値であり、envelope テキスト自身から導出した値と比較することは決してない（自己参照検証の禁止）。"

schema_id: EMIT_PARENT_REVIEW_ENVELOPE_V2 (child intermediate grammar + V2 emission)
definition: .claude/skills/issue-refinement-loop/scripts/emit_parent_review_envelope_v2.py
related_issue: "#1541"
producer:
  - emit_parent_review_envelope_v2.py（issue-refinement-loop orchestrator が command registry `review_compact.emit_v2` 経由で唯一呼び出す。旧来の orchestrator 手動 f-string assembly、テスト専用 `_assemble_v2_envelope()` は production 経路から廃止）
consumer:
  - issue-refinement-loop orchestrator（stdout の完全な15行 V2 envelope を `validate_review_compact_output.py --v2` へそのまま渡す）
trust_boundary:
  - "`validate_child_intermediate()` は child intermediate（8行 approve / 9行 needs-fix、`REVIEWER_BLOCKER_CLAIM` を含む）を V1/V2 final grammar とは別の grammar として strict 検証する。`PARENT_REPLAY_*` はこの grammar では unknown field として拒否される。"
  - "`render_parent_review_envelope_v2()` は pure function（subprocess/I/O なし）。`PARENT_REPLAY_*` 6行は ALWAYS 呼び出し元が既に schema/digest 検証済みの `PARENT_REPLAY_BINDING_ARTIFACT_V1` からのみ導出され、child intermediate 自身の文字列や child が主張する値からは一切導出しない。"
  - "`emit_parent_review_envelope_v2()` は binding artifact の digest 自己整合性・identity（repository/issue/session/iteration/body）・child claim の canonical digest 一致をすべて検証してから envelope を組み立てる。いずれかの不一致は contract-invalid（exit 1）として fail-closed する。"
compatibility:
  breaking_changes:
    - child intermediate grammar（CHILD_APPROVE_FIELDS / CHILD_NEEDS_FIX_FIELDS）のフィールド集合・順序変更
    - render_parent_review_envelope_v2() の byte layout 変更（LF/trailing LF/UTF-8/BOM なし の contract 変更）
  non_breaking_changes:
    - stderr diagnostic の追加フィールド
detection_patterns:
  - 'emit_parent_review_envelope_v2'
  - 'render_parent_review_envelope_v2'
  - 'validate_child_intermediate'
  - 'review_compact.emit_v2'
validation_commands:
  - "uv run --locked pytest .claude/skills/issue-refinement-loop/tests/test_emit_parent_review_envelope_v2.py -q"
  - "uv run --locked pytest .claude/skills/issue-refinement-loop/tests/test_production_v2_command_chain.py -q"
  - "uv run --locked pytest .claude/skills/issue-refinement-loop/tests/test_review_compact_emit_v2_registry_contract.py -q"
notes:
  - "approve envelope（8行）は binding artifact / claim / replay / state write を一切起動しない（AC6）。"
  - "失敗時（contract-invalid / runtime error）は stdout を常に空のまま保ち、部分 envelope を書かない（AC8）。"
```

## delegation_model_policy/v1 詳細登録

```yaml
schema_id: delegation_model_policy/v1
definition: .claude/skills/gemini-cli-headless-delegation/scripts/build_request.py（`model-policy` サブコマンド）、.claude/skills/gemini-cli-headless-delegation/references/model-routing.md
related_issue: "#1269"
producer:
  - build_request.py（`build_model_policy()` / `main_model_policy()`。読み取り専用・副作用なしの dry-run inspector。request file・output file は一切書き込まない）
consumer:
  - 人間オペレータ・エージェント（`build_request.py model-policy` CLI の stdout consumer）
  - .claude/skills/gemini-cli-headless-delegation/tests/test_build_request_model_policy.py
shape: |
  discriminated union（discriminator は `provider` フィールドと `ok`/`failure_class`）。
  全 variant 共通のベースフィールド: schema, provider, role, profile, ok,
  failure_class, failure_reason（成功時は failure_class/failure_reason とも null）。
  - provider が MODEL_POLICY_PROVIDERS 外: ベースのみ（failure_class: "invalid_provider"）。
  - provider="gemini" 成功: + resolved_chain(list[string]), actual_model(null),
    resolver_source(string)。
  - provider="gemini" 失敗（unknown_role/empty_chain）: ベースのみ。
  - provider="gemini"/"auto"(eligible) の config_invalid: + reason_code("routing_config_invalid")。
  - provider="agy"（常に ok:true。load_model_routing() を一切呼ばない）:
    + resolved_chain(null), configured_chain(null), actual_model(null),
    legacy_compatibility_label("agy-default"), wrapper_capability(object),
    upstream_capability(object: probed, documented_explicit_model_selection,
    installed_version, installed_version_probed, note), readiness_checked(false),
    credentials_checked(false), provider_available(null)。--role 指定時のみ
    role_applied(false)/role_note(string) を追加。
  - provider="auto" で --profile 省略: ベースのみ（failure_class: "profile_required_for_auto"）。
  - provider="auto" で profile が PROVIDER_AUTO_ELIGIBLE_PROFILES 外: routing 未読込のまま
    + runtime_order(list[string]), profile_eligible(false), provider_candidates(null),
    consumer_constraints(null)（ok:true）。
  - provider="auto" で profile eligible・成功: + runtime_order(list[string]),
    profile_eligible(true), provider_candidates(list[object] -- 各要素は
    provider フィールドで discriminate: "gemini" は {provider,resolved_chain,actual_model}
    の3キーのみ、"agy" は上記 agy variant と同じキー集合)、
    consumer_constraints({fan_out: {supported:false, reason_code:string},
    agy_fallback_requires_prompt:true, explicit_model_survives_fallback:false})。
control_flow_order: |
  build_model_policy() の分岐順序は run_gemini_headless.py 自身の dispatch 順序を
  鏡写しにする（独自順序を発明しない）: (1) provider を MODEL_POLICY_PROVIDERS と
  照合、(2) provider="agy" は load_model_routing() を一切呼ばずに即座に確定、
  (3) provider="auto" は --profile 有無 → PROVIDER_AUTO_ELIGIBLE_PROFILES 該当有無を
  routing 読込より前に判定（ineligible なら routing 未読込のまま返す）、
  (4) provider="gemini" または auto(eligible) のみ load_model_routing() /
  resolve_model_chain() を呼ぶ。
no_side_effect_guarantee: |
  `_load_run_gemini_headless_module()` は run_gemini_headless.py の動的 import
  前後で sys.dont_write_bytecode を True に設定・復元し、
  scripts/__pycache__/*.pyc の生成を PYTHONDONTWRITEBYTECODE の設定有無に
  依存せず防止する（AC6 の no-side-effect 主張の一部）。
compatibility:
  breaking_changes:
    - schema フィールドの削除・rename
    - discriminator（provider / ok / failure_class の組み合わせ）による variant 判定条件の変更
    - resolved_chain / actual_model の null/非null セマンティクスの変更
    - fan_out の型変更（現在は object; bool への逆行は breaking）
    - failure_class の既存値の意味変更・削除
  non_breaking_changes:
    - 新規 variant の追加（provider 追加等）
    - upstream_capability / consumer_constraints への新規オプショナルフィールド追加
    - 新規 failure_class 値の追加
detection_patterns:
  - 'delegation_model_policy/v1'
  - 'model-policy'
  - 'build_model_policy'
  - 'PROVIDER_AUTO_FAN_OUT_UNSUPPORTED_REASON_CODE'
validation_commands:
  - "uv run --locked pytest .claude/skills/gemini-cli-headless-delegation/tests/test_build_request_model_policy.py -q"
  - "uv run python3 .claude/skills/gemini-cli-headless-delegation/scripts/build_request.py model-policy --provider agy"
notes:
  - "actual_model は全 variant で常に null（dry-run のため観測値を持たない）。実行時の観測値は delegation_result/v1 側の actual_model であり、本 schema とは別 field/別 producer。"
  - "resolved_chain / configured_chain は「設定から解決された候補チェーン」であり「現在実行可能な chain（readiness）」ではない。readiness_checked / credentials_checked / provider_available は live probe 未実装（scope 外）を明示するための常に静的な値。"
  - "fan_out の reason_code は run_gemini_headless.py の PROVIDER_AUTO_FAN_OUT_UNSUPPORTED_REASON_CODE をそのまま参照する（build_request.py 側でハードコードされた別リテラルを持たない）。"
```

## #934 public-surface boundary cleanup note（公開境界クリーンアップ注記）

- #934 は public-surface boundary cleanup であり、`agent_session_manifest/v1` の live public posting を拡張する issue ではない。
- #934 で固定する consumer-facing contract は「manifest 本文は Issue / PR comment に出さない」「公開コメントでは opaque ref のみ許可」「`agent_run_report/v1` / `agent_retro_index/v1` は #935 schema/redaction validator と #937 exact marker upsert guard が揃うまで dry-run only / not live public posting」という境界である。
- `agent_run_report/v1` / `agent_retro_index/v1` の正式な Known Schema 登録、consumer inventory、validation command の追加は #935 で扱う。#934 では historical wording と current boundary の衝突解消のみを行う。

## schema_change_applicability 判定基準

PR が schema を変更するか否かを判定する基準:

| 値 | 判定条件 |
|---|---|
| `schema_change` | 上記 Initial Known Schemas の before/after が PR diff に含まれる、または新規 schema が追加される |
| `not_schema_change` | Allowed Paths 内の変更がすべて内部ロジック・コメント・説明文のみで、consumer 境界をまたぐ contract に変更がない |
| `uncertain` | PR diff を見ただけでは consumer 境界への影響が判断できない場合。fail-closed として schema_change 相当の検査を適用する |

## Schema Consumer Inventory の記載義務

schema を変更する PR（`schema_change` または `uncertain`）では、以下の **Schema Consumer Inventory** を PR 本文に必ず記載しなければならない。

### 必須記載項目

1. **変更対象 schema の ID**（例: `delegation_result/v1`）
2. **before/after 差分**（key 名変更・フィールド追加削除・型変更 等）
3. **consumer 一覧**（`rg` コマンドで列挙した全 consumer ファイルのリスト）
4. **各 consumer の更新有無**（更新済み / 不要（理由）/ 未対応（blocker））

### consumer 列挙コマンド例

```bash
# schema ID またはキー名を rg で検索して consumer ファイルを列挙
rg -l "delegation_result" .
rg -l "LOOP_VERDICT" .
rg -l "issue_contract" .
```

### Consumer Inventory が欠落している場合の扱い

- `schema_change` または `uncertain` の PR で Schema Consumer Inventory が PR 本文に存在しない場合: **APPROVE 禁止（blocker）**
- consumer が更新されていない場合（「未対応」と記載されている場合）: **APPROVE 禁止（blocker）**
- consumer 列挙コマンドの出力結果が PR 本文に含まれていない場合: **APPROVE 禁止（blocker）**

## 参照

- `.claude/skills/pr-review-judge/SKILL.md` — schema_change_applicability 判定と Consumer Inventory 検査ルール
- `.claude/skills/open-pr/SKILL.md` — PR 本文への Schema Consumer Inventory セクション追加手順
- `.github/pull_request_template.md` — PR テンプレート（Schema Change Applicability / Schema Consumer Inventory セクション）
- `docs/dev/workflow.md` — Issue contract を作業計画の正本として扱う条件
