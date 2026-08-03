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
| `delegation_audit_v1` | `.claude/skills/gemini-cli-headless-delegation/scripts/run_gemini_headless.py`（`--audit-log` / `DELEGATION_AUDIT_LOG_PATH` で明示有効化する監査ログ JSONL） | run_gemini_headless.py（`run_delegation()` トップレベル呼び出し） | `.claude/skills/gemini-cli-headless-delegation/tests/test_delegation_audit_schema.py`（本 PR で追加した唯一の consumer。既存 `delegation_result/v1` の consumer とはファイルレベルで分離） | `rg -n "delegation_audit_v1\|DELEGATION_AUDIT_LOG_PATH\|_build_delegation_audit_record" .claude/skills/gemini-cli-headless-delegation` |
| `delegation_fanout_request_v1` | `.claude/skills/gemini-cli-headless-delegation/scripts/fan_out_orchestrator.py`（closed schema; `subtasks[]` は既存 `delegation_request_v1` をそのまま保持し、planner mode は対象外） | `build_fanout_request.py` / 呼び出し元（実装/調査 orchestration の呼び出し側） | `fan_out_orchestrator.py`（`run_fanout()` / `validate_fanout_request()`） | `rg -n "delegation_fanout_request_v1" .claude/skills/gemini-cli-headless-delegation` |
| `delegation_fanout_result_v1` | `.claude/skills/gemini-cli-headless-delegation/scripts/fan_out_orchestrator.py`（`status: success\|partial_success\|failed\|cancelled`、`counts`、`results[]`、`failures[]`、`deduplicated_aliases` を持つ決定的な merge 結果） | `fan_out_orchestrator.py`（`run_fanout()`） | 呼び出し元（実装/調査 orchestration の呼び出し側） | `rg -n "delegation_fanout_result_v1" .claude/skills/gemini-cli-headless-delegation` |
| `REVIEW_COMPACT_VALIDATION_RESULT_V1` | `.claude/skills/issue-refinement-loop/scripts/validate_review_compact_output.py` | validate_review_compact_output.py（`review_compact.validate` 経由で orchestrator が呼び出す） | issue-refinement-loop（Step 2 の validator-first fail-closed routing。#1873: Step 2a Replay Arbitration は撤去済み） | `rg -n "REVIEW_COMPACT_VALIDATION_RESULT_V1\|validation_status\|review_compact.validate" .claude/skills/issue-refinement-loop` |
| `delegation_model_policy/v1` | `.claude/skills/gemini-cli-headless-delegation/scripts/build_request.py`（`model-policy` サブコマンド）、`.claude/skills/gemini-cli-headless-delegation/references/model-routing.md` | build_request.py の `build_model_policy()` / `main_model_policy()`（読み取り専用・副作用なしの dry-run inspector。`run_gemini_headless.py` の `load_model_routing()` / `resolve_model_chain()` / `PROVIDER_AUTO_*` を直接呼び出す） | 人間オペレータ・エージェント（`model-policy` CLI 呼び出しの stdout consumer）、test_build_request_model_policy.py | `rg -n "delegation_model_policy/v1\|model-policy\|build_model_policy" .claude/skills/gemini-cli-headless-delegation` |
| `AGY_CAUSAL_CLAIM_MANIFEST_V1` | `.claude/skills/gemini-cli-headless-delegation/schemas/agy_causal_claim_manifest_v1.schema.json`（JSON Schema draft 2020-12。Issue #1778、CLOSED 済み #1494 の敵対的再監査 follow-up） | `.claude/skills/gemini-cli-headless-delegation/scripts/audit_agy_auth_surface.py`（`agy_permission_policy.py` の認証 surface 露出・`read_only` 命名関数の OS レベル強制有無を静的検出）、`scripts/check_agy_causal_claim_drift.py`（`agy_permission_policy.py` / `run_gemini_headless.py` の `Issue #N` 参照コメントと `references/*.md` frontmatter `status` の整合を検出し fail-close） | 人間レビュワー・エージェント（両スクリプトの stdout JSON consumer）、`test_audit_agy_auth_surface.py`、`test_check_agy_causal_claim_drift.py` | `rg -n "AGY_CAUSAL_CLAIM_MANIFEST_V1" .claude/skills/gemini-cli-headless-delegation scripts docs/dev/schema-governance.md` |
| `AGY_GROUNDING_EVIDENCE_VERDICT_V1` | `.claude/skills/gemini-cli-headless-delegation/scripts/validate_agy_grounding_evidence.py` | validate_agy_grounding_evidence.py（causal claim extraction + evidence binding check） | pr-review-judge（clean-room review の experimental-validity reviewer）、test_validate_agy_grounding_evidence.py | `rg -n "AGY_GROUNDING_EVIDENCE_VERDICT_V1\|validate_agy_grounding_evidence" .` |
| `agy_permission_boundary_e2e/v1` | `.claude/skills/gemini-cli-headless-delegation/schemas/agy_permission_boundary_e2e_v1.schema.json` | permission-boundary runner | artifact validator / reviewer | `rg -n "agy_permission_boundary_e2e/v1" .claude/skills/gemini-cli-headless-delegation docs/dev/schema-governance.md` |
| `agy_preflight_result/v1`（additive `capabilities` matrix, Issue #1941） | `.claude/skills/gemini-cli-headless-delegation/scripts/preflight_agy.py`（`build_capability_matrix()` / `run_preflight(compute_capabilities=True)` が唯一の実装 SSOT） | preflight_agy.py | `setup_check.py`（`agy_capabilities` として additive に surface。既存 `agy_preflight["ok"]` 単一 boolean 消費は不変）、`run_gemini_headless.py` | `rg -n "agy_capability_matrix/v1\|CAPABILITY_PREDICATES\|build_capability_matrix" .claude/skills/gemini-cli-headless-delegation` |

**Compatibility Decision**: `AGY_CAUSAL_CLAIM_MANIFEST_V1` は本 Issue（#1778）で新規追加された schema であり、既存 schema の破壊的変更は含まない（`additive` — 新規 producer 2 件、既存 consumer への影響なし）。`agy_permission_policy.py` / `run_gemini_headless.py` はどちらも read-only の分析対象であり、本 Issue の PR では一切変更されない（behavior change なし）。

**Compatibility Decision**（Issue #1941）: `agy_capability_matrix/v1` は `agy_preflight_result/v1` に対する additive field（`capabilities` / `capability_schema` / `capability_probes`）の追加である。schema バージョンの `v2` bump は行わない。`run_preflight()` のデフォルト呼び出し（`compute_capabilities` 省略）は既存の `ok` boolean 由来 exit `0`/`1` を変更しない。新設の `--require-capability` CLI モードのみが要求 capability 集合に対する `0`/`1`/`77` の exit code taxonomy を持つ。既存 consumer（`setup_check.py`）は `preflight_agy.py` の capability matrix を additive に surface するのみで、独自の version/help parser を実装しない。

**Compatibility Decision（Issue #1941 fix_delta、PR #1982 OWNER review）**: サニタイズ経路のセキュリティ修正として、`agy_preflight_result/v1` の既存フィールド `agy.version` は生の `agy --version` 出力そのものではなく、`version_evidence.version` から導出した正規化済みバージョン文字列を保持する（未 redact な warning 行の生テキストが混入する漏洩経路を閉じるため）。生テキストが必要な consumer 向けに新規 additive field `agy.version_raw_sample`（redact 済み・bounded sample）を追加した。`version_evidence.raw` も同様に redact 済み sample に置き換える。`run_preflight()` の戻り値・`--json`・`--output-file`・`.claude/tmp/` artifact のすべてが同一の sanitizer（`_sanitize_for_artifact()`）を経由するようになった（従来は artifact のみ）。既存 consumer（`setup_check.py`、`run_gemini_headless.py`）はいずれも `agy.version` の生テキスト形状に依存していないため破壊的変更ではないが、`agy.version` の値の意味（生 → 正規化済み）が変わる点を明記する。schema バージョンの `v2` bump は行わない。加えて、`capability_probes` 内の各 predicate が `unavailable` になった場合の `reason_code` として `runtime_probe_cost_unconfirmed`（P1-3 cost-confirmation gate 未確認）、`cli_missing`/`help_unavailable`/`smoke_timed_out`/`smoke_check_failed`/`auth_blocked_probe`/`grounded_research_failed`/`local_asset_contract_invalid`（P2-1 controlled early exit）が additive に追加された。

`agy_permission_boundary_e2e/v1` は #1814 の local/hermetic schema である。producer は `run_agy_permission_boundary_e2e.py`、consumer は同 runner の `validate_artifact()`（`Draft202012Validator` と cross-field invariant）、test consumer は `test_agy_permission_boundary_runner.py`、運用 consumer は reviewer である。schema は documented args を持つ command / write / read / network の unique matrix と同数の attempt、parent runner が記録する correlation／expectation／predicate 集合、identity／child return code を固定する。MCP は実 tool discovery 前は matrix に含めず、固定の `mcp_call` 名を証跡に使用しない。PostToolUse は toolCall を含まない official-shaped payload から run binding・conversation・step・error digest だけを記録し、tool名とargs digestはPreToolUse recordを正本として相関する。parse/logger/non-zero failure は absence と区別し exit 1 の inconclusive とする。`diagnostic_ledger` は isolated runtime cleanup 前に集約する lifecycle 件数だけを保持し、raw payload・args・prompt・absolute path を保持しない。actual AGY deterministic live completion は follow-up #1979 と upstream #728 の責務であり、この schema は hermetic PASS を live completion に昇格させない。exit 77 は unavailable／non-completion／`actual_agy_executed: false` に、cleanup failure は exit 1 に束縛する。runner-local の file mode/readback check は security boundary ではなく fail-closed local guardrail である。secret-safe redaction、`fallback.used: false`、binary / artifact digest も検証する。Compatibility Decision は breaking local-contract correction（旧5 capability artifactのconsumerはこのPRのproducer/validator/testのみ）。artifact digest は `artifact.digest` と `runner.artifact_digest` を null とした canonical UTF-8 JSON（sorted keys、compact separators）の SHA-256 であり、保存ファイル全体の hash ではない。

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
  - .claude/skills/issue-refinement-loop/SKILL.md（Step 2: validator-first fail-closed routing。#1873: Step 2a Replay Arbitration および V2 grammar は撤去済み）
compatibility:
  breaking_changes:
    - validation_status の意味変更（valid/invalid の判定条件変更）
    - envelope_kind の値集合変更
    - normalized_payload のキー削除・rename
  non_breaking_changes:
    - violations[] への新規 code 追加
    - artifact_path_policy への新規フィールド追加
detection_patterns:
  - 'REVIEW_COMPACT_VALIDATION_RESULT_V1'
  - 'validation_status'
  - 'review_compact.validate'
validation_commands:
  - "uv run --locked pytest .claude/skills/issue-refinement-loop/tests/test_validate_review_compact_output.py -q"
  - "uv run --locked pytest .claude/skills/issue-refinement-loop/tests/test_review_compact_registry_entry.py -q"
notes:
  - "producer-failure envelope は構文解析可能だが validation_status は常に invalid（#1165 SSOT）。"
  - "input_sha256 / normalized_payload は format-only 検証であり provenance 証明ではない。"
  - "ARTIFACT の issue namespace 束縛（`--issue-number`）は active issue 以外への読み違いを防ぐが、実ファイル存在確認は行わない（#1472 isolation worktree 境界）。"
  - "#1873: approve envelope と needs_fix envelope は同一の8フィールド形状であり、VERDICT/NEXT_ACTION/BLOCKERS の値のみで区別される（旧 REPLAY_*/PARENT_REPLAY_*/REVIEWER_BLOCKER_CLAIM フィールドは撤去済み）。"
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

## AGY_GROUNDING_EVIDENCE_VERDICT_V1 詳細登録

```yaml
schema_id: AGY_GROUNDING_EVIDENCE_VERDICT_V1
definition: .claude/skills/gemini-cli-headless-delegation/scripts/validate_agy_grounding_evidence.py
related_issue: "#1776"
producer:
  - validate_agy_grounding_evidence.py（`evaluate_grounding_evidence()` / CLI main()。読み取り専用・副作用なし。--output 指定時のみファイル書き込み）
consumer:
  - pr-review-judge（clean-room review の experimental-validity reviewer が一次情報源として使用。.claude/skills/pr-review-judge/SKILL.md#4.7 Clean-Room Review）
  - .claude/skills/gemini-cli-headless-delegation/tests/test_validate_agy_grounding_evidence.py
shape: |
  固定フィールド集合: schema（"AGY_GROUNDING_EVIDENCE_VERDICT_V1" 固定）、
  status（ok | fail_closed。indeterminate は将来の拡張用に予約済みだが
  現行 producer は出力しない）、evidence_bindings[]（各要素: claim,
  paragraph_index, evidence_ref）、unsupported_claims[]（各要素: claim,
  paragraph_index, reason）。
  status は unsupported_claims が非空なら fail_closed、それ以外は ok。
control_flow_order: |
  入力（--diff-file / --pr-body-file の少なくとも一方が必須）を結合し、
  空行区切りの paragraph に分割 → 各 paragraph 内の causal claim 文（
  「〜が原因」「〜により解消/修正/解決/改善」等の正規表現）を抽出 →
  同一 paragraph 内に evidence 参照（backtick 付きファイルパス/URL/
  sha256 ダイジェスト）と evidence キーワード（hook/citation/content
  evidence/ログ/証跡/引用/出典）が共存するかを判定 → 共存すれば
  evidence_bindings、しなければ unsupported_claims に分類する。
compatibility:
  breaking_changes:
    - status の既存値（ok / fail_closed）の意味変更・削除
    - evidence_bindings[] / unsupported_claims[] の必須フィールド削除・rename
    - claim 抽出パターンの後方非互換な狭小化（既存 fixture が unsupported→ok に反転する変更）
  non_breaking_changes:
    - 新規 causal-claim パターンの追加
    - 新規 evidence キーワードの追加
    - indeterminate ステータスの実装追加（値自体は既に予約済み）
detection_patterns:
  - 'AGY_GROUNDING_EVIDENCE_VERDICT_V1'
  - 'validate_agy_grounding_evidence'
  - 'evaluate_grounding_evidence'
validation_commands:
  - "uv run --locked pytest .claude/skills/gemini-cli-headless-delegation/tests/test_validate_agy_grounding_evidence.py -q"
notes:
  - "モデルの自己申告のみに基づく causal claim を検出するための clean-room review 支援ツールであり、merge-blocking 判定自体は pr-review-judge の責務（本 schema は verdict 材料の一つ）。"
```

## OVERLAP_GATE_BYPASS_V1（#1679 により削除・supersede 済み）

`OVERLAP_GATE_BYPASS_V1`（Issue #1776 で導入された、open_pr.py の hard overlap gate を C2a/C3 経由で意図的にバイパスした場合の説明責任 fenced YAML 記録）は、元の overlap hard gate 自体が Issue #1679 で production path から完全に削除されたことに伴い、補助 gate として存続する意味を失ったため同 Issue で削除された。`validate_pr_body.py` の `_validate_overlap_gate_bypass()` / `LP059` / `E_OVERLAP_GATE_BYPASS_SCHEMA_INVALID` と `schemas/catalog.yaml` の該当エントリ、`.claude/skills/open-pr/tests/test_validate_pr_body_overlap_gate_bypass.py` は撤去済み。#1776 は overlap-bypass 補助 gate 部分のみ #1679 により superseded であり、#1776 の他の変更点には影響しない。

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
