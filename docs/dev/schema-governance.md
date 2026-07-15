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
