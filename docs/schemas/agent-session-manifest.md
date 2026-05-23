---
schema_version: v1
title: Agent Session Manifest
status: active
related_issue: "#243"
---

# Agent Session Manifest (agent_session_manifest/v1)

LOOP_PROTOCOL の Main Loop 各 phase において AI agent session の metadata を記録するための SSOT schema。
`metadata-first / full transcript は疑義発生時のみ` 方針（#136 Decision）に基づく。

後続の hook 実装 Issue はこの schema を参照する。

## 設計目的

PR #81 / #131 振り返りで明らかになった問題（AC 読み落とし・test-runner 呼び出し有無・SKIP exit 0 黙認・PR 本文全面置換・token/context 圧迫）を事後検証するために、各 phase で **どの metadata を GitHub Issue/PR コメントに残すか** を明文化する。

## Schema Fields 定義

### トップレベル必須フィールド

| フィールド | 型 | 説明 |
|---|---|---|
| `schema` | string | `"agent_session_manifest/v1"` 固定 |
| `manifest_id` | string | `"asm-<uuid>"` 形式の一意 ID |
| `recorded_at` | string | ISO 8601 タイムスタンプ（例: `"2026-05-23T12:00:00Z"`） |
| `repository` | string | リポジトリ名（例: `"squne121/loop-protocol"`） |
| `head_sha` | string\|null | 観測時点の HEAD commit SHA（nullable） |
| `actor` | object | セッション実行者の情報（後述） |
| `phase` | object | Main Loop phase と SubAgent Execution Ledger phase の両方（後述） |
| `token_usage` | object | トークン使用量（availability 付き。後述） |
| `invoked_subagents` | array | 呼び出した SubAgent のリスト（後述） |
| `verification` | object | AC 検証結果（`verification.overall` / per-AC 構造体。後述） |
| `evidence` | array | 証拠リスト（後述） |
| `redaction` | object | 機微情報の redaction 状態（後述） |
| `human_intervention` | object | 人間介入の有無と内容 |
| `next_action_issue` | number\|null | 次に実行すべき Issue 番号（nullable） |

### オプショナルフィールド

| フィールド | 型 | 説明 |
|---|---|---|
| `issue_number` | number\|null | 対象 Issue 番号 |
| `pr_number` | number\|null | 対象 PR 番号 |
| `commit_sha` | string\|null | コミット SHA（nullable） |

### `actor` オブジェクト

| フィールド | 型 | 説明 |
|---|---|---|
| `type` | enum | `ai_agent \| human \| github_action` |
| `name` | string | エージェント名または `"human"` |
| `session_id` | string\|null | セッション ID（nullable。人間操作の場合は `null` 可） |

### `phase` オブジェクト

| フィールド | 型 | 説明 |
|---|---|---|
| `main_loop` | enum | Main Loop phase（後述 enum 値参照） |
| `ledger_phase` | string\|null | SubAgent Execution Ledger の対応 phase（optional） |
| `phase_instance_id` | string | `"issue-<N>:<main_loop_phase>:<seq>"` 形式 |

### `token_usage` オブジェクト

| フィールド | 型 | 説明 |
|---|---|---|
| `availability` | enum | `measured \| estimated \| unavailable` |
| `source` | enum | `provider_api \| tool_log \| entire_cli \| manual_report \| none` |
| `prompt` | number\|null | プロンプトトークン数（取得不可なら `null`） |
| `completion` | number\|null | 補完トークン数（取得不可なら `null`） |
| `total` | number\|null | 合計トークン数（取得不可なら `null`） |

**重要**: `availability: unavailable` のとき、数値フィールドを `0` で埋めることを禁止する。
取得手段がない場合は必ず `null` を使用し、`unavailable` を `0` と偽装してはならない。

### `invoked_subagents` リスト（各要素）

| フィールド | 型 | 説明 |
|---|---|---|
| `name` | string | SubAgent 名 |
| `count` | number | 呼び出し回数 |
| `duration_ms` | number\|null | 実行時間 ms（optional） |

### `verification` オブジェクト

| フィールド | 型 | 説明 |
|---|---|---|
| `overall` | enum | `pass \| fail \| partial \| blocked \| not_applicable` |
| `skipped_count` | number | スキップされた AC 数 |
| `fallback_detected` | boolean | フォールバック PASS の検出有無 |
| `ac_results` | array | per-AC 結果リスト（後述） |

**注意**: `skipped_count > 0` の場合、`overall: pass` を使用することを禁止する。その場合は `partial` を使用する。

#### `ac_results` 各要素

| フィールド | 型 | 説明 |
|---|---|---|
| `ac` | string | AC 番号（例: `"AC7"`） |
| `verdict` | enum | `pass \| fail \| skip \| blocked \| not_applicable` |
| `command` | string\|null | 実行した VC コマンド |
| `exit_code` | number\|null | exit code |
| `artifact_ref` | string\|null | 証跡へのパスまたは URL |
| `waiver_ref` | string\|null | 免除根拠 Issue URL（`skip` / `blocked` 時） |

### `evidence` リスト（各要素）

| フィールド | 型 | 説明 |
|---|---|---|
| `source_kind` | enum | `github_comment \| ci_check \| hook_jsonl \| artifact \| transcript \| local_file` |
| `source_ref` | string | 証拠の URL またはパス |
| `source_sha256` | string\|null | ファイルの SHA-256（optional） |

### `redaction` オブジェクト

| フィールド | 型 | 説明 |
|---|---|---|
| `raw_transcript_included` | boolean | raw transcript を含むか |
| `local_paths_included` | boolean | ローカル絶対パスを含むか |
| `secret_scan_status` | string | `not_applicable \| clean \| flagged` |

### `human_intervention` オブジェクト

| フィールド | 型 | 説明 |
|---|---|---|
| `required` | boolean | 人間介入が必要だったか |
| `type` | string | 介入種別（`none \| approval \| correction \| escalation` 等） |
| `summary` | string\|null | 介入内容の概要（nullable） |

## Main Loop Phase Enum

`phase.main_loop` の取り得る値（7 値）:

| 値 | 説明 |
|---|---|
| `issue_create` | Issue 起票フェーズ |
| `issue_review` | Issue レビュー / refinement フェーズ |
| `impl` | 実装フェーズ |
| `pr_open` | PR 起票フェーズ |
| `pr_review` | PR レビューフェーズ |
| `merge` | マージフェーズ |
| `followup_create` | NextAction Issue 起票フェーズ |

## Main Loop Phase と SubAgent Execution Ledger Phase の対応表

| `phase.main_loop` | `phase.ledger_phase` | 備考 |
|---|---|---|
| `issue_create` | `followup_issue_materialization` | Issue 起票時は ledger_phase 任意 |
| `issue_review` | `issue_contract_preflight` | issue-contract-review が実行する phase |
| `impl` | `implementation` + `post_commit_verification` | 実装と検証で 2 エントリ |
| `pr_open` | `pr_body_update` | open-pr skill が実行する phase |
| `pr_review` | `semantic_review` | pr-review-judge が実行する phase |
| `merge` | `pre_merge_judgment` または `github_merge_event` | 人間が UI でマージした場合は `actor.type: human` / `session_id: null` で記録 |
| `followup_create` | `followup_issue_materialization` | post-merge-cleanup が実行する phase |

**注意**: `merge` フェーズで人間が GitHub UI でマージした場合、AI agent session は存在しない。
`actor.type: human` / `session_id: null` で記録し、`session_id` を必須にしない。

## Phase 別 必須 fields / 任意 fields 表

| `phase.main_loop` | 必須 (required) fields | 任意 (optional) fields |
|---|---|---|
| `issue_create` | `schema`, `manifest_id`, `recorded_at`, `repository`, `actor`, `phase.main_loop`, `redaction` | `issue_number`, `head_sha`, `token_usage`, `evidence`, `invoked_subagents` |
| `issue_review` | `schema`, `manifest_id`, `recorded_at`, `repository`, `actor`, `phase.main_loop`, `phase.ledger_phase`, `verification`, `redaction` | `issue_number`, `head_sha`, `token_usage`, `invoked_subagents`, `evidence` |
| `impl` | `schema`, `manifest_id`, `recorded_at`, `repository`, `actor`, `phase.main_loop`, `phase.ledger_phase`, `commit_sha`, `verification`, `evidence`, `redaction` | `pr_number`, `head_sha`, `token_usage`, `invoked_subagents` |
| `pr_open` | `schema`, `manifest_id`, `recorded_at`, `repository`, `actor`, `phase.main_loop`, `pr_number`, `head_sha`, `redaction` | `issue_number`, `token_usage`, `invoked_subagents`, `evidence` |
| `pr_review` | `schema`, `manifest_id`, `recorded_at`, `repository`, `actor`, `phase.main_loop`, `phase.ledger_phase`, `pr_number`, `head_sha`, `verification`, `evidence`, `redaction` | `issue_number`, `token_usage`, `invoked_subagents` |
| `merge` | `schema`, `manifest_id`, `recorded_at`, `repository`, `actor`, `phase.main_loop`, `pr_number`, `head_sha`, `redaction` | `phase.ledger_phase`, `commit_sha`, `token_usage`, `evidence` |
| `followup_create` | `schema`, `manifest_id`, `recorded_at`, `repository`, `actor`, `phase.main_loop`, `redaction` | `next_action_issue`, `issue_number`, `token_usage`, `evidence` |

## GitHub Comment への raw transcript 禁止ポリシー

GitHub Issue/PR コメントに以下の情報を含めることを **MUST NOT** とする:

- raw transcript（AI の完全な会話ログ）
- ローカル絶対パス（例: `/home/user/.claude/...`）
- API キー・トークン・認証情報
- `.env` ファイルの内容
- agent-local 設定ファイルの内容
- `VITE_*` prefix の環境変数（client bundle に露出するリスク）

本 schema は **metadata-only** 方針で設計されている。全 transcript の記録が必要な場合は、
`session-recording-policy.md` および `secret-policy.md`（#241/#242 で整備予定）に従い、
public repo への push 前に人間レビューを必須とする。

**EntireCLI や類似ツールを使用する場合の注意**:
transcripts / prompts / checkpoint metadata が public repo に commit されると internet から参照可能になる。
本 schema はこのリスクを防ぐために metadata のみを GitHub コメントに記録する設計を採用している。

## GitHub Comment テンプレート

Issue/PR コメントに agent_session_manifest を記録する際は、以下の HTML marker 付き fenced code block を使用する:

```markdown
<!-- agent_session_manifest:v1 start -->
```yaml
schema: agent_session_manifest/v1
manifest_id: "asm-<uuid>"
recorded_at: "<ISO8601>"
repository: "squne121/loop-protocol"

issue_number: <N>
pr_number: null
commit_sha: null
head_sha: null

actor:
  type: ai_agent
  name: main
  session_id: "<uuid-or-null>"

phase:
  main_loop: issue_review
  ledger_phase: issue_contract_preflight
  phase_instance_id: "issue-<N>:issue_review:001"

token_usage:
  availability: unavailable
  source: none
  prompt: null
  completion: null
  total: null

invoked_subagents:
  - name: issue-reviewer
    count: 1
    duration_ms: null

verification:
  overall: not_applicable
  skipped_count: 0
  fallback_detected: false
  ac_results: []

evidence:
  - source_kind: github_comment
    source_ref: "https://github.com/squne121/loop-protocol/issues/<N>#issuecomment-..."
    source_sha256: null

human_intervention:
  required: false
  type: none
  summary: null

next_action_issue: null

redaction:
  raw_transcript_included: false
  local_paths_included: false
  secret_scan_status: not_applicable
` ``
<!-- agent_session_manifest:v1 end -->
```

**marker ルール**:
- 開始: `<!-- agent_session_manifest:v1 start -->`
- 終了: `<!-- agent_session_manifest:v1 end -->`
- 中身は YAML の fenced code block とする
- detection_patterns は schema-governance.md に定義済み

## 最小有効例（impl phase）

```yaml
schema: agent_session_manifest/v1
manifest_id: "asm-20260523-001"
recorded_at: "2026-05-23T12:00:00Z"
repository: "squne121/loop-protocol"

issue_number: 243
pr_number: 311
commit_sha: "abc1234"
head_sha: "abc1234"

actor:
  type: ai_agent
  name: implementation-worker
  session_id: "00000000-0000-0000-0000-000000000001"

phase:
  main_loop: impl
  ledger_phase: implementation
  phase_instance_id: "issue-243:impl:001"

token_usage:
  availability: unavailable
  source: none
  prompt: null
  completion: null
  total: null

invoked_subagents: []

verification:
  overall: pass
  skipped_count: 0
  fallback_detected: false
  ac_results:
    - ac: AC1
      verdict: pass
      command: "test -f docs/schemas/agent-session-manifest.md && rg ..."
      exit_code: 0
      artifact_ref: null
      waiver_ref: null

evidence:
  - source_kind: github_comment
    source_ref: "https://github.com/squne121/loop-protocol/issues/243#issuecomment-..."
    source_sha256: null

human_intervention:
  required: false
  type: none
  summary: null

next_action_issue: null

redaction:
  raw_transcript_included: false
  local_paths_included: false
  secret_scan_status: not_applicable
```

## 関連ドキュメント

- `docs/dev/agent-skill-boundaries.md` — SubAgent / Skill 責務境界（Hook-based Ledger Optional Design セクション）
- `docs/dev/schema-governance.md` — Schema governance ルール（本 schema の登録先）
- `docs/dev/runtime-verification-policy.md` — Runtime Verification Applicability 判定スキーマ
- `#136` — metadata-first 方針の anchor decision
- `#44` — SubAgent Execution Ledger 設計（ledger_phase の元定義）
- `#241` — Secret Inventory SSOT 化（session recording 前提条件）
- `#242` — Session Recording Kill Switch policy（session recording 前提条件）
