# Agent Run Report

This document describes the `agent_run_report/v1` schema, the `export-chatgpt-context` export surface,
and the operational procedures for report finalization, review correction, follow-up issue tracking,
and hook boundary policy.

## アーティファクト責務差分

`agent_session_manifest`、`agent_run_report`、`agent_retro_index` は互いに補完する 3 つのアーティファクトであり、
同一のエージェントランに対してそれぞれ異なる責務を担う。

| アーティファクト | 責務 | 生成タイミング | public-safe 要件 |
|---|---|---|---|
| `agent_session_manifest` | セッション中の読み取りファイル・ツール呼び出し・コンテキスト境界の記録。内部追跡用。 | セッション中（逐次） | 不要（内部専用） |
| `agent_run_report` (`agent_run_report/v1`) | ランの公開可能な要約。AC 達成状況・コマンド結果・証跡 URL・public-safety 判定を含む。 | セッション終了後（`finalize-agent-run.mjs`） | 必須（`public_safety.verdict: pass` が posting 前提） |
| `agent_retro_index` | 複数ランにまたがる振り返りインデックス。friction パターン・フォローアップ Issue・改善点の集約。 | ラン完了後またはレトロスペクティブ時 | 任意（内容による） |

これら 3 つのアーティファクトの参照順は次のとおり:
1. `agent_session_manifest` でセッション内の raw 追跡を確認する
2. `agent_run_report` で公開可能な要約と AC 達成状況を確認する
3. `agent_retro_index` で横断的なパターンとフォローアップを確認する

詳細は `docs/dev/agent-retro-index.md` を参照。

## Phase Stop Conditions

エージェントランの各フェーズに対して、以下の Stop Conditions が適用される。
Stop Condition に到達する前に次フェーズへ進まない。

### 実装フェーズ

- コード/ドキュメント変更が Allowed Paths 内に収まっている
- 全 AC の VC コマンドが期待する終了コードを返している
- `pnpm typecheck && pnpm lint && pnpm test && pnpm build` が全て pass している

### レポート確定フェーズ

- **`report finalized`**: `agent_run_report/v1` JSON が `finalize-agent-run.mjs` によって生成されており、
  `schema` フィールドが `"agent_run_report/v1"` であることを確認している
- **`public-safe check pass`**: `public_safety.verdict === "pass"` かつ `blocked_reasons` が空であることを確認している
  （`public_safety.redaction_status === "clean"` が前提）
- forbidden fields（`raw_transcript`、`full_command_output`、`stdout`、`stderr`、`local_path` 等）が
  ソース JSON に含まれていないことをスキャンで確認している

### 投稿フェーズ

- **`posting dry-run or upsert done`**: `export-chatgpt-context` の dry-run が成功しているか、
  または GitHub Issue/PR へのコメント upsert が完了している
- 投稿先（`public_surface_kind`）が意図した対象（`github_issue_comment` / `github_pr_comment`）であることを確認している
- 二重投稿防止のため、upsert は既存コメントを上書きする形式を使用している

## Review Correction Loop

CI failure、human correction、または reviewer comment が発生した場合、
`agent_run_report/v1` の以下のフィールドに反映する手順を踏む。

### evidence_refs への反映

`authority.evidence_refs` には、修正を裏付ける証跡を `opaqueReference` 形式で追記する:

```json
"evidence_refs": [
  {
    "kind": "workflow_run",
    "ref": "https://github.com/squne121/loop-protocol/actions/runs/<run-id>",
    "digest": "sha256:<64hex>",
    "validation_verdict": "pass"
  },
  {
    "kind": "github_comment",
    "ref": "https://github.com/squne121/loop-protocol/pull/<pr>#issuecomment-<id>",
    "digest": "sha256:<64hex>",
    "validation_verdict": "pass"
  }
]
```

- CI が fail した場合: 失敗した workflow run を `kind: "workflow_run"` で `evidence_refs` に追記する
- human correction が適用された場合: 修正を指示したコメントを `kind: "github_comment"` で追記する
- reviewer comment による変更の場合: レビューコメントを `kind: "github_comment"` で追記する

`evidence_refs` は `opaqueReference[]` 型であり、URL 文字列を直接格納できない。
スキーマの詳細は `docs/schemas/agent-run-report.schema.json` の `$defs.opaqueReference` を参照。

### commands_summary.summary への反映

`commands_summary` の各エントリの `summary` フィールドに修正内容を記録する:

```json
"commands_summary": [
  {
    "command_label": "pnpm test",
    "exit_code": 0,
    "verdict": "pass",
    "summary": "iteration-1: CI failure (exit_code: 1) 後に <fix> を適用して再実行。pass。",
    "artifact_ref": null
  }
]
```

修正を含むイテレーションでは `summary` に `iteration-N:` プレフィックスを付けて変更点を明示する。

### レポートの再確定

修正後は再度 `finalize-agent-run.mjs` を実行してレポートを再生成し、
`public-safe check pass` Stop Condition を再度確認してから投稿する。

## Follow-up Issue Creation

エージェントランの完了後に follow-up Issue を起票するか否かを判断し、結果を記録する。

### agent_retro_index.entries への記録

起票した follow-up Issue は `agent_retro_index` の対応エントリに記録する。
`entries[].follow_up_issues` は Issue 番号の integer array である:

```json
"follow_up_issues": [941, 942]
```

スキーマ詳細は `docs/schemas/agent-retro-index.schema.json` および `docs/dev/agent-retro-index.md` を参照。

### 起票しない場合の記録

follow-up Issue を起票しない場合は、その理由を termination report またはローカル handoff に留める:

- termination report: `commands_summary` の最後のエントリの `summary` に理由を記載する
- ローカル handoff: `agent_run_report/v1` JSON の `commands_summary` に
  `"command_label": "follow_up_decision"` エントリとして記録する

```json
{
  "command_label": "follow_up_decision",
  "exit_code": 0,
  "verdict": "skip",
  "summary": "スコープ内で解消済み。別 Issue 不要。",
  "artifact_ref": null
}
```

## Hook Boundary Policy

hooks（pre-commit hook、PreToolUse hook 等）は **diagnostic/prevention レイヤー** であり、
セキュリティ境界またはカノニカルゲートではない。

> **post-run verifier が canonical gate である。** hook の通過は AC 達成の証明にならない。
> 最終的な AC 判定は post-run verifier（VC コマンドの実行結果と証跡）に基づく。

具体的な責務分担:

| レイヤー | 責務 | カノニカル判定 |
|---|---|---|
| hook（PreToolUse / PreWrite 等） | 早期警告・local write の防止・環境ガード | **不可**（バイパス可能・環境依存） |
| post-run verifier（VC コマンド群） | AC 達成の検証・証跡生成 | **可（canonical）** |
| `agent_run_report/v1` | 公開可能なランの要約と AC 結果の記録 | 参照可能（verifier 結果を記録） |

hook が fail した場合は Stop Condition として扱い、fix 後に post-run verifier を再実行する。
hook が pass しても post-run verifier を省略しない。詳細は `docs/dev/hook-boundaries.md` を参照。

## agent_run_report/v1

Agent run reports are JSON artifacts produced by `scripts/agent-logs/finalize-agent-run.mjs`.
They capture the public-safe summary of a single AI agent run, including:

- `schema`: always `"agent_run_report/v1"`
- `public_surface_kind`: where the report may be surfaced (`none`, `github_issue_comment`, `github_pr_comment`)
- `public_safety`: `{ redaction_status, checked_by, validator_version, checked_at, verdict, blocked_reasons, entirecli_safety }`
- `actor`: `{ type, name }`
- `authority`: `{ level, basis, evidence_refs }`
- `token_usage`: `{ availability, source, prompt, completion, total }`
- `manifest_refs`: list of manifest digest refs
- `evidence_refs`: list of evidence refs (workflow runs, PR/Issue URLs, artifact digests)
- `commands_summary`: list of command summaries (`command_label`, `exit_code`, `verdict`, `summary`, `artifact_ref`)
- `docs_read_refs`: list of doc read refs

### entirecli_safety runtime enforcement

`public_safety.entirecli_safety` is **schema-optional** (`#1134`/PR `#1178`) but **runtime-required** for
public surface reports (`public_surface_kind !== 'none'`).

The runtime enforcement is implemented in `validate-final-report.mjs` (`validateFinalReport`) and
`retro-index-builder.mjs` (`normalizeSourceComment`):

- `validateFinalReport` calls `assertEntireCLISafetyRuntime` before schema validation.
  A public surface report missing `entirecli_safety` throws `report.entirecli_safety_missing`.
- `normalizeSourceComment` returns `kind: 'blocked'` for source comments whose embedded report
  lacks `entirecli_safety` or carries `verdict: 'blocked'`.

**Only checker-produced values are accepted.** The field must be produced by
`scripts/agent-logs/check-entirecli-safety.mjs` and passed through as-is. Auto-synthesis of
`not_applicable` at the producer level is prohibited.

Conditions that fail-closed at runtime (in addition to the schema gate):

| Condition | Error code |
|---|---|
| `entirecli_safety` absent from `public_safety` | `report.entirecli_safety_missing` |
| `schema_version` is not `entirecli_safety_result/v1` | `report.entirecli_safety_unknown_schema_version` |
| `raw_values_emitted === true` | `report.entirecli_safety_raw_values_emitted` |
| `verdict` not in `{ safe, not_applicable }` | `report.entirecli_safety_blocked` |

The schema gate (`validateAgentRunReport`) remains the canonical admission check for schema
correctness (e.g., `blocked` verdict, unknown keys). The runtime enforcement layer adds
defense-in-depth for the `missing` case which the schema alone cannot catch.

### Forbidden Fields

The following fields **must not** appear in any source JSON file consumed by the export pipeline:

| Field | Reason |
|---|---|
| `raw_transcript` | Full session transcript — never public-safe |
| `transcript_excerpt` | Partial transcript — never public-safe |
| `full_command_output` | Unredacted command stdout/stderr |
| `stdout` | Unredacted stdout |
| `stderr` | Unredacted stderr |
| `local_path` | Local filesystem path — environment-specific and potentially sensitive |

### transcript_hotspot_summary

`transcript_hotspot_summary` is the **only** transcript-derived field permitted in export sources.
It is allowed **only when** `public_safety.redaction_status === "clean"` has been verified.

## ChatGPT Context Bundle Export

The `export-chatgpt-context` CLI generates a public-safe, deterministic Markdown bundle
suitable for pasting into ChatGPT for retrospective analysis.

### Script

```
scripts/agent-logs/export-chatgpt-context.mjs
```

### CLI Usage

```bash
node scripts/agent-logs/export-chatgpt-context.mjs \
  --parent-issue-json artifacts/parent-issue-928.json \
  --target-issue-json artifacts/issue-939.json \
  --retro-index-json artifacts/agent-retro-index.json \
  --source-set-json artifacts/agent-retro-index-source-set.json \
  --run-report-json artifacts/report-1.json \
  --evidence-ref-json artifacts/evidence-refs.json \
  --max-chars 24000 \
  --max-sections 12 \
  --generated-at 2026-06-19T00:00:00.000Z \
  --output artifacts/chatgpt-context.md \
  --summary-json-out artifacts/chatgpt-context-summary.json
```

### Options

| Option | Description |
|---|---|
| `--parent-issue-json` | Path to parent issue JSON (required) |
| `--target-issue-json` | Path to target issue JSON (required) |
| `--retro-index-json` | Path to agent retro index JSON (required) |
| `--source-set-json` | Path to source set JSON (required) |
| `--run-report-json` | Path to run report JSON (repeatable) |
| `--evidence-ref-json` | Path to evidence ref JSON (repeatable) |
| `--max-chars` | Character budget for the bundle (required) |
| `--max-sections` | Maximum number of sections (required) |
| `--generated-at` | ISO-8601 timestamp for bundle header (required) |
| `--output` | Output Markdown path (required, no-overwrite) |
| `--summary-json-out` | Output summary JSON path (required, no-overwrite) |

### Section Priority Order (fixed)

1. `safety_header` — SECURITY_BOUNDARY + chatgpt_context_bundle/v1 header
2. `source_manifest` — source file digests
3. `parent_goal` — parent issue + target issue
4. `priority_signals` — friction, context pollution, human intervention, follow-ups
5. `ci_review_loops` — CI/review loop data
6. `evidence_refs` — deduplicated evidence refs
7. `lower_priority_narrative` — run report summaries
8. `omission_report` — sections dropped due to budget

Lower-priority sections are dropped first when the budget is exceeded.
If the budget is too small to hold `safety_header` + `priority_signals`, the CLI exits with `budget.too_small`.

### Security Properties

- All external-origin text is fenced in `DATA` blocks or blockquotes.
- A final rendered Markdown scan rejects injection patterns.
- Output is written atomically (no partial writes, no overwrite).
- Source files are scanned for forbidden fields before processing.
- Each source file's digest is pinned in the `source_manifest`.

### Library Modules

| Module | Responsibility |
|---|---|
| `lib/chatgpt-context-args.mjs` | CLI argument parsing and validation |
| `lib/chatgpt-context-source-loader.mjs` | Load, validate, and digest source files |
| `lib/chatgpt-context-safety-scan.mjs` | Injection scanner and DATA block wrapping |
| `lib/chatgpt-context-dedupe.mjs` | Evidence ref canonicalization and deduplication |
| `lib/chatgpt-context-budget.mjs` | Priority-aware budget allocation |
| `lib/chatgpt-context-renderer.mjs` | Markdown section renderers |

## EntireCLI Safety Checker

`scripts/agent-logs/check-entirecli-safety.mjs` は EntireCLI の使用状況を検査し、
`agent_run_report` の `public_safety` フィールドへ取り込むための verdict を計算する adapter である。

### Verdict 種別

| verdict | 意味 |
|---|---|
| `not_applicable` | EntireCLI 未使用（binary / `.entire/` / hooks / refs / env / config がすべて不在） |
| `safe` | EntireCLI 使用検出 + 全安全条件を満たす |
| `blocked` | public/unknown push 経路、telemetry 有効、parse error、raw 値漏洩のいずれかを検出 |

### schema_version

`entirecli_safety_result/v1`

### safe 条件（すべて満たす必要あり）

- `strategy_options.push_sessions` が `false`（未設定は `blocked`）
- effective telemetry setting が `false`
  - official top-level `telemetry` を優先する
  - legacy / alternate `strategy_options.telemetry` は互換入力として扱う
  - 未設定は `blocked`
- `checkpoint_remote` が `private_verified` または local-only
- `ENTIRE_CHECKPOINT_TOKEN` 存在時は `checkpoint_remote` が `private_verified`
- public / unknown / non-GitHub / parse error はすべて `blocked`

### 検査対象 git config キー

`remote.*.url`、`remote.*.pushurl`、`remote.pushDefault`、`branch.*.pushRemote`、
`url.*.insteadOf`、`url.*.pushInsteadOf`、`include.path`、`includeIf.*.path`、
`remote.*.mirror`、`remote.*.push`

### Redaction ポリシー

診断出力に raw URL / raw config path / token を含めてはならない。
`reason_code` と redacted fingerprint（`redactFingerprint()` 使用）のみ許可する。
`checked_surfaces.entire_version` は non-authoritative な diagnostic fingerprint であり、
raw version 文字列や release provenance の証明には使わない。

### schema フィールド統合

`agent-run-report.schema.json` には `public_safety.entirecli_safety` の
`EntireCLISafetyResult/v1` admission 契約を追加済みである。
`finalize-agent-run.mjs` は `--entirecli-safety-json` または `--entirecli-safety-file` で
checker-produced value を受け取る。`public_surface_kind !== 'none'` の場合はいずれかが必須。
JSON parse 失敗は fail-closed（exit 1、report 未出力）。

### Library Module

| Module | Responsibility |
|---|---|
| `lib/entirecli-safety.mjs` | verdict 計算ロジック・redaction helper・設定パーサー |
| `scripts/agent-logs/check-entirecli-safety.mjs` | CLI entry point（live git/fs 検査） |

## #1221 Agent Observation Capability Boundary

`agent_observation_capability/v1` matrix（`docs/dev/agent-observation-capability.md`）の capture
capability verdict は synthetic evidence のみで固定する。本節は hook coexistence と canonical gate の
位置づけを再確認する。

- Hook（PreToolUse / async Stop hook 等）は diagnostic / prevention レイヤーであり、canonical gate は
  post-run verifier である。
- async Stop hook / hook exit 0 / hook presence は PASS 証明にならない。
- hook 共存の PASS は以下の closed contract を満たすこと:

```yaml
hook_coexistence_pass_requires:
  expected_handlers_fired_once: true
  duplicate_finalization_absent: true
  duplicate_upload_absent: true
  async_hook_not_used_as_gate: true
  post_run_verifier_observed_final_state: true
  runtime_event_and_capture_artifact_correlated: true
  hook_exit_zero_not_authoritative: true
  raw_values_emitted: false
```

- #1220 の `LATITUDE_PILOT_EXCEPTION_V1` A1 decision gate は本節で変更しない。
- `docs/dev/secret-policy.md` は変更しない。
- real prompt / real trace export / real Cloud pilot は禁止のままとする。
- `unsupported` / `unverified` は失敗ではなく Child C0/C1 の input availability として扱う。
