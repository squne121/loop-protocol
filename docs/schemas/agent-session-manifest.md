---
schema_version: v1
title: Agent Session Manifest
status: active
related_issue: "#243"
---

# Agent Session Manifest (agent_session_manifest/v1)

LOOP_PROTOCOL の Main Loop 各 phase において AI agent session の metadata を記録するための SSOT schema。
`metadata-first / full transcript は疑義発生時のみ` 方針（#136 Decision）に基づく。

機械可読 JSON Schema SSOT: [`docs/schemas/agent-session-manifest.schema.json`](agent-session-manifest.schema.json)（JSON Schema Draft 2020-12）

後続の hook 実装 Issue はこの schema を参照する。

## 設計目的

PR #81 / #131 振り返りで明らかになった問題（AC 読み落とし・test-runner 呼び出し有無・SKIP exit 0 黙認・PR 本文全面置換・token/context 圧迫）を事後検証するために、各 phase で **どの metadata を retention-limited artifact と opaque ref に残すか** を明文化する。

## 1 manifest = 1 ledger phase 原則

`phase.ledger_phase` は **scalar（単一値）** である。1 つの manifest document は 1 つの ledger_phase にのみ対応する。

`impl` フェーズ（`phase.main_loop: impl`）は内部に 2 つの ledger_phase を含むため、**2 つの manifest を残す**:

| manifest | `phase.main_loop` | `phase.ledger_phase` |
|---|---|---|
| impl manifest 1 | `impl` | `implementation` |
| impl manifest 2 | `impl` | `post_commit_verification` |

この分離により、実装と検証の両証跡が独立して追跡可能になる。

## Schema Fields 定義

以下はすべてのフィールドの定義表である。グローバル必須フィールドは JSON Schema の `required` 配列に定義する。phase 別の必須 / 任意の区別は「Phase 別 必須 fields / 任意 fields 表」を参照。

### フィールド定義表（全フィールド）

| フィールド | 型 | グローバル必須 | 説明 |
|---|---|---|---|
| `schema` | string（const） | yes | `"agent_session_manifest/v1"` 固定 |
| `manifest_id` | string（`asm-<UUIDv4>` pattern） | yes | `asm-<UUIDv4>` 形式の一意 ID |
| `recorded_at` | string（ISO 8601） | yes | ISO 8601 タイムスタンプ（例: `"2026-05-24T12:00:00Z"`） |
| `repository` | string | yes | リポジトリ名（例: `"squne121/loop-protocol"`） |
| `actor` | object | yes | セッション実行者の情報（後述） |
| `phase` | object | yes | Main Loop phase と SubAgent Execution Ledger phase（後述） |
| `redaction` | object | yes | 機微情報の redaction 状態（後述） |
| `secret_policy` | object | yes | Secret 非露出の static producer contract と runtime boundary attestation（後述）。root `required`（#549 で必須化） |
| `head_sha` | string\|null（40-hex pattern または null） | no | 観測時点の HEAD commit SHA（nullable） |
| `issue_number` | integer\|null | no | 対象 Issue 番号（optional） |
| `pr_number` | integer\|null | no | 対象 PR 番号（optional） |
| `commit_sha` | string\|null（40-hex pattern または null） | no | コミット SHA（optional, nullable） |
| `producer` | object | no | producer provenance object（後述）。`producer.kind` は self-claim |
| `token_usage` | object | no | トークン使用量（availability 付き。後述） |
| `invoked_subagents` | array | no | 呼び出した SubAgent のリスト（後述） |
| `verification` | object | no | AC 検証結果（`verification.overall` / per-AC 構造体。後述） |
| `evidence` | array | no | 証拠リスト（visibility フィールド付き。後述） |
| `hook_event` | object | no | Claude Code hook イベント情報（optional） |
| `sanitization_status` | string（enum） | no | 機微情報のサニタイズ状態（hook 記録時に設定） |
| `human_intervention` | object | no | 人間介入の有無と内容 |
| `next_action_issue` | integer\|null | no | 次に実行すべき Issue 番号（nullable） |

### `actor` オブジェクト

| フィールド | 型 | 必須 | 説明 |
|---|---|---|---|
| `type` | enum | yes | `ai_agent \| human \| github_action` |
| `name` | string | yes | エージェント名または `"human"` |
| `session_id` | string\|null | no | セッション ID（nullable。人間操作の場合は `null` 可） |

## Producer Provenance

`producer` は optional object であり、既存 manifest との backward compatibility のため `required` には含めない。  
`producer.kind` is a self-claim であり、schema 追加だけで真正性を証明するものではない。真正性は `evidence` linkage、および #378 / #402 で扱う hook / CI wiring で担保する。

### `producer` オブジェクト

| フィールド | 型 | 必須 | 説明 |
|---|---|---|---|
| `kind` | enum | yes | `script_generated \| hook_generated \| github_action_generated` |
| `version` | string\|null | no | producer 実装 version（既知なら設定） |
| `command` | string\|null | no | sanitized invocation command。validator は best-effort pattern scan で secret / expanded env values / unsafe local path を reject する |
| `source_ref` | string\|null | no | git ref / PR / workflow run / artifact reference / hook id など。validator は best-effort pattern scan を適用する |

`human_attested_from_deterministic_evidence` remains outside the schema enum。これは schema の producer kind ではなく、人間が deterministic evidence を確認したという運用上の attestation として扱う。

### `phase` オブジェクト

| フィールド | 型 | 必須 | 説明 |
|---|---|---|---|
| `main_loop` | enum（7 値） | yes | Main Loop phase（後述 enum 値参照） |
| `ledger_phase` | enum\|null | no | SubAgent Execution Ledger の対応 phase（scalar, optional）。1 manifest = 1 ledger phase |
| `phase_instance_id` | string | yes | `issue-<N>:<main_loop_phase>:<seq>` または `ci:<producer_slug>:<run_id>:<run_attempt>` 形式（例: `"issue-243:impl:001"`, `"ci:session-manifest:123456789:1"`） |

### `token_usage` オブジェクト

| フィールド | 型 | 必須 | 説明 |
|---|---|---|---|
| `availability` | enum | yes | `measured \| estimated \| unavailable` |
| `source` | enum | yes | `provider_api \| tool_log \| entire_cli \| manual_report \| none` |
| `prompt` | integer\|null | no | プロンプトトークン数（取得不可なら `null`） |
| `completion` | integer\|null | no | 補完トークン数（取得不可なら `null`） |
| `total` | integer\|null | no | 合計トークン数（取得不可なら `null`） |

**重要**: `availability: unavailable` のとき、数値フィールドを `0` で埋めることを禁止する。
取得手段がない場合は必ず `null` を使用し、`unavailable` を `0` と偽装してはならない。

### `invoked_subagents` リスト（各要素）

| フィールド | 型 | 必須 | 説明 |
|---|---|---|---|
| `name` | string | yes | SubAgent 名 |
| `count` | integer | yes | 呼び出し回数 |
| `duration_ms` | integer\|null | no | 実行時間 ms（optional） |

### `verification` オブジェクト

| フィールド | 型 | 必須 | 説明 |
|---|---|---|---|
| `overall` | enum | yes | `pass \| fail \| partial \| blocked \| not_applicable` |
| `skipped_count` | integer | yes | スキップされた AC 数 |
| `fallback_detected` | boolean | yes | フォールバック PASS の検出有無 |
| `ac_results` | array | yes | per-AC 結果リスト（後述） |

**注意**: `skipped_count > 0` の場合、`overall: pass` を使用することを禁止する。その場合は `partial` を使用する。

#### `ac_results` 各要素

| フィールド | 型 | 必須 | 説明 |
|---|---|---|---|
| `ac` | string | yes | AC 番号（例: `"AC7"`） |
| `verdict` | enum | yes | `pass \| fail \| skip \| blocked \| not_applicable` |
| `command` | string\|null | no | 実行した VC コマンド |
| `exit_code` | integer\|null | no | exit code |
| `artifact_ref` | string\|null | no | 証跡へのパスまたは URL |
| `waiver_ref` | string\|null | no | 免除根拠 Issue URL（`skip` / `blocked` 時） |

### `evidence` リスト（各要素）

| フィールド | 型 | 必須 | 説明 |
|---|---|---|---|
| `source_kind` | enum | yes | `github_comment \| ci_check \| hook_jsonl \| artifact \| transcript \| local_file` |
| `source_ref` | string | yes | 証拠の URL またはパス |
| `source_sha256` | string\|null | no | ファイルの SHA-256（optional） |
| `visibility` | enum | no | `public_github_comment \| private_artifact \| local_only`（`private_artifact` は legacy enum 名であり secret-safe を意味しない。後述制約参照） |

**visibility 制約**: `visibility: public_github_comment` のとき、`source_kind: transcript` および `source_kind: local_file` は禁止。
この制約は JSON Schema の `if/then` 条件で機械的に検証される。

### `hook_event` オブジェクト（optional）

hook イベント情報を記録する。Claude Code hook から生成される manifest で使用する。

| フィールド | 型 | 必須 | 説明 |
|---|---|---|---|
| `event_type` | enum | no | `SubagentStart \| SubagentStop \| PostToolUse \| Stop \| PreToolUse` |
| `hook_id` | string\|null | no | hook 実行 ID（optional） |
| `triggered_at` | string\|null | no | hook トリガー時刻（ISO 8601） |

### `sanitization_status`（optional）

hook 記録時に設定される機微情報のサニタイズ状態。

| 値 | 説明 |
|---|---|
| `not_sanitized` | サニタイズ未実施 |
| `sanitized` | サニタイズ完了 |
| `sanitization_failed` | サニタイズ失敗 |

### `redaction` オブジェクト

| フィールド | 型 | 必須 | 説明 |
|---|---|---|---|
| `raw_transcript_included` | boolean | yes | raw transcript を含むか |
| `local_paths_included` | boolean | yes | ローカル絶対パスを含むか |
| `secret_scan_status` | enum | yes | `not_applicable \| clean \| flagged` |

### `human_intervention` オブジェクト

| フィールド | 型 | 必須 | 説明 |
|---|---|---|---|
| `required` | boolean | yes | 人間介入が必要だったか |
| `type` | enum | yes | 介入種別（`none \| approval \| correction \| escalation`） |
| `summary` | string\|null | no | 介入内容の概要（nullable） |

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

`phase.ledger_phase` は scalar（単一値）であり、1 manifest = 1 ledger phase の原則に従う。
`impl` フェーズは `implementation` と `post_commit_verification` の 2 manifest を別々に残す。

| `phase.main_loop` | `phase.ledger_phase` | 備考 |
|---|---|---|
| `issue_create` | `followup_issue_materialization` | Issue 起票時は ledger_phase 任意 |
| `issue_review` | `issue_contract_preflight` | issue-contract-review が実行する phase |
| `impl` | `implementation` | 実装の manifest（1 manifest 目） |
| `impl` | `post_commit_verification` | 検証の manifest（2 manifest 目） |
| `pr_open` | `pr_body_update` | open-pr skill が実行する phase |
| `pr_review` | `semantic_review` | pr-review-judge が実行する phase |
| `merge` | `pre_merge_judgment` または `github_merge_event` | 人間が UI でマージした場合は `actor.type: human` / `session_id: null` で記録 |
| `followup_create` | `followup_issue_materialization` | post-merge-cleanup が実行する phase |

**注意**: `merge` フェーズで人間が GitHub UI でマージした場合、AI agent session は存在しない。
`actor.type: human` / `session_id: null` で記録し、`session_id` を必須にしない。

**impl phase における 2 manifest 方式の詳細**:

```yaml
# manifest 1: 実装
phase:
  main_loop: impl
  ledger_phase: implementation
  phase_instance_id: "issue-243:impl:001"

# manifest 2: 検証（post_commit_verification）
phase:
  main_loop: impl
  ledger_phase: post_commit_verification
  phase_instance_id: "issue-243:impl:002"
```

CI artifact producer の場合は `phase_instance_id: "ci:session-manifest:<run_id>:<run_attempt>"` を使用する。

## Phase 別 必須 fields / 任意 fields 表

以下の表は各フェーズで推奨される必須フィールドを示す。グローバル必須（`schema`, `manifest_id`, `recorded_at`, `repository`, `actor`, `phase`, `redaction`, `secret_policy`）はすべてのフェーズで必須であり、以下の表では省略する。

| `phase.main_loop` | 追加必須 (required) fields | 追加任意 (optional) fields |
|---|---|---|
| `issue_create` | — | `issue_number`, `head_sha`, `token_usage`, `evidence`, `invoked_subagents` |
| `issue_review` | `verification` | `issue_number`, `head_sha`, `token_usage`, `invoked_subagents`, `evidence` |
| `impl` (implementation) | `commit_sha`, `verification`, `evidence` | `pr_number`, `head_sha`, `token_usage`, `invoked_subagents`, `hook_event`, `sanitization_status` |
| `impl` (post_commit_verification) | `head_sha`, `verification`, `evidence` | `pr_number`, `commit_sha`, `token_usage`, `invoked_subagents`, `hook_event` |
| `pr_open` | `pr_number`, `head_sha` | `issue_number`, `token_usage`, `invoked_subagents`, `evidence` |
| `pr_review` | `pr_number`, `head_sha`, `verification`, `evidence` | `issue_number`, `token_usage`, `invoked_subagents` |
| `merge` | `pr_number`, `head_sha` | `ledger_phase`, `commit_sha`, `token_usage`, `evidence` |
| `followup_create` | — | `next_action_issue`, `issue_number`, `token_usage`, `evidence` |

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

`evidence.visibility: public_github_comment` のとき、`source_kind: transcript` / `source_kind: local_file` を使用することは
JSON Schema によって機械的に禁止されている。

- current live public posting では `agent_session_manifest/v1` の manifest 本文を Issue / PR comment に出さない。公開コメントで許可されるのは `artifact_digest`、`artifact_url`、`schema_ref`、`validation_verdict` などの opaque ref のみである。
- `artifact_url` は retention-limited / auth-dependent / non-canonical locator であり、公開コメントで参照してよいが canonical な永続証跡ではない。永続 identity は `artifact_digest` と schema / marker 側へ寄せる。
- `agent_run_report/v1` / `agent_retro_index/v1` は #935 schema/redaction validator と #937 exact marker upsert guard が揃った後にのみ conditional public comment 可であり、#934 merge 時点では dry-run のみで live public posting は禁止する。
- `private_artifact` は legacy visibility enum 名であり secret-safe を意味しない。public repo では retention-limited non-comment surface 上の content も public-safe でなければならない。

**EntireCLI や類似ツールを使用する場合の注意**:
transcripts / prompts / checkpoint metadata が public repo に commit されると internet から参照可能になる。
本 schema はこのリスクを防ぐために、manifest 本文ではなく metadata への opaque ref のみを GitHub コメントへ出せる public-safe boundary を採用している。

## Historical GitHub Comment Template（legacy / non-current）

以下の template は historical 参照用であり、current live public posting の手順ではない。manifest 本文を Issue / PR comment に貼る運用は non-current であり、not live public posting として扱う。
marker 文字列そのものは legacy parser / detection pattern の説明用に残すが、manifest body の公開許可を意味しない。

````markdown
<!-- agent_session_manifest:v1 start -->
```yaml
schema: agent_session_manifest/v1
manifest_id: "asm-<UUIDv4>"
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

producer:
  kind: script_generated
  version: null
  command: "node scripts/generate-session-manifest.mjs"
  source_ref: null

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
    visibility: public_github_comment

human_intervention:
  required: false
  type: none
  summary: null

next_action_issue: null

redaction:
  raw_transcript_included: false
  local_paths_included: false
  secret_scan_status: not_applicable

secret_policy:
  value_exposed: false
  mode: presence_only
  producer_contract:
    declared: true
    id: presence_only_no_secret_values
    version: v1
    claims:
      secret_values_not_serialized: true
      presence_only: true
  runtime_boundary:
    attested: false
    evidence_ref: null
```
<!-- agent_session_manifest:v1 end -->
````

**marker ルール**:
- 開始: `<!-- agent_session_manifest:v1 start -->`
- 終了: `<!-- agent_session_manifest:v1 end -->`
- 中身は YAML の fenced code block とする
- 外側 fence は 4 backticks、内側は 3 backticks（入れ子 fence 衝突を防ぐため）
- detection_patterns は schema-governance.md に定義済み

## 最小有効例（impl phase）

impl phase では 2 manifest を残す。以下は `implementation` manifest の例:

```yaml
schema: agent_session_manifest/v1
manifest_id: "asm-12345678-1234-4123-89ab-123456789abc"
recorded_at: "2026-05-24T12:00:00Z"
repository: "squne121/loop-protocol"

issue_number: 243
pr_number: 314
commit_sha: "abcdef1234567890abcdef1234567890abcdef12"
head_sha: "abcdef1234567890abcdef1234567890abcdef12"

actor:
  type: ai_agent
  name: implementation-worker
  session_id: "00000000-0000-4000-89ab-000000000001"

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

producer:
  kind: script_generated
  version: null
  command: "node scripts/generate-session-manifest.mjs"
  source_ref: null

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
    visibility: public_github_comment

human_intervention:
  required: false
  type: none
  summary: null

next_action_issue: null

redaction:
  raw_transcript_included: false
  local_paths_included: false
  secret_scan_status: not_applicable

secret_policy:
  value_exposed: false
  mode: presence_only
  producer_contract:
    declared: true
    id: presence_only_no_secret_values
    version: v1
    claims:
      secret_values_not_serialized: true
      presence_only: true
  runtime_boundary:
    attested: false
    evidence_ref: null
```

## 関連ドキュメント

- `docs/schemas/agent-session-manifest.schema.json` — JSON Schema Draft 2020-12（SSOT）
- `docs/schemas/examples/` — valid / invalid fixtures（バリデーションテスト用）
- `tests/agent-session-manifest.test.ts` — vitest バリデーションテスト（`pnpm test` で自動検証）
- `docs/dev/agent-skill-boundaries.md` — SubAgent / Skill 責務境界（Hook-based Ledger Optional Design セクション）
- `docs/dev/schema-governance.md` — Schema governance ルール（本 schema の登録先）
- `docs/dev/runtime-verification-policy.md` — Runtime Verification Applicability 判定スキーマ
- `#136` — metadata-first 方針の anchor decision
- `#44` — SubAgent Execution Ledger 設計（ledger_phase の元定義）
- `#241` — Secret Inventory SSOT 化（session recording 前提条件）
- `#242` — Session Recording Kill Switch policy（session recording 前提条件）

## `secret_policy` オブジェクト（global required, #549 で root required 化）

Secret 値を manifest に含めない static producer contract と、runtime boundary attestation を分離して記録する。
本オブジェクトは root `required` に含まれる**グローバル必須フィールド**であり（#549）、すべての manifest が `secret_policy` を持たなければならない。`secret_policy` を欠く manifest は schema validation で reject される。shape は #412 / PR #537 で確定済み（`producer_contract` / `runtime_boundary` 分離）。

| フィールド | 型 | 必須 | 説明 |
|---|---|---|---|
| `value_exposed` | boolean const false | yes | Secret 値がこの manifest に露出していないこと |
| `mode` | enum `"presence_only"` | yes | Secret は presence-only metadata として扱う |
| `producer_contract` | object | yes | static producer declaration。runtime attestation ではない |
| `runtime_boundary` | object | yes | runtime boundary enforcement の attestation 状態 |

### `producer_contract`

| フィールド | 型 | 必須 | 説明 |
|---|---|---|---|
| `declared` | boolean const true | yes | producer が static contract を明示的に宣言したこと |
| `id` | string const `presence_only_no_secret_values` | yes | machine-readable static contract identifier |
| `version` | string (`^v[0-9]+$`) | yes | static contract version |
| `claims.secret_values_not_serialized` | boolean const true | yes | Secret 値を manifest に serialize しない |
| `claims.presence_only` | boolean const true | yes | presence-only metadata のみを emit する |

### `runtime_boundary`

| フィールド | 型 | 必須 | 説明 |
|---|---|---|---|
| `attested` | boolean | yes | runtime secret boundary enforcement が実際に attestation されたか |
| `evidence_ref` | string \| null | yes | runtime evidence への参照 |

- `attested: true` のとき、`evidence_ref` は非空・非空白 string でなければならない
- `attested: false` のとき、`evidence_ref` は `null` でなければならない
- `boundary_enforced` は廃止。旧 shape は invalid

`value_exposed: false` かつ `mode: presence_only` が manifest の public-safe 要件。
producer 実装の新 shape 追随は Issue #500 / PR #532 側で行う。
