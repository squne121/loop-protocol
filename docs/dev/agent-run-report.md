# Agent Run Report / エージェントランレポート

このドキュメントは `agent_run_report/v1` schema、`export-chatgpt-context` export surface、
report finalization、review correction、follow-up issue tracking、hook boundary policy の運用手順を定義する。

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

## Phase Stop Conditions / フェーズ停止条件

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

### コマンド責務境界

- `agent-run:finalize` は public-safe な `agent_run_report/v1` artifact を生成し、validation を通す責務を持つ
- `agent-run:post` は **validated `agent_run_report` の GitHub upsert 専用** であり、`CHATGPT_RETRO_CONTEXT_V1` marker の更新責務を含めない
<!-- 検証アンカー verification-anchor: agent-run:post は検証済み agent_run_report の GitHub upsert 専用 -->
- `chatgpt-retro-context:post` は `CHATGPT_RETRO_CONTEXT_V1` / `CHATGPT_RETRO_CONTEXT_DIGEST_V1` の 2-line marker contract に従って marker comment を create / noop / supersede する
- `chatgpt-retro-context:resolve-fixture` は fixture JSON から marker 導線を検証する静的 resolver である
- `chatgpt-retro-context:resolve-live` は issue / pull request target を issue comments endpoint として扱い、marker comment だけでなく参照先 run report / retro index comment まで live fetch して digest chain を再検証する live resolver である
- pull request target では追加で PR review / review comment / review thread の public-safe projection を取得し、pagination 完全性・ID catalog・object catalog・projection digest を返す
- PR review surface を closure proof へ算入するときは、comment chain と review surface の両方が `resolved` であること、`page_budget_exhausted === false` / `reference_page_budget_exhausted === false` であることを同時に満たす
- `chatgpt_retro_execution_proof/v1` で PR review surface を再検証するときは `operation_index_ref.revalidation_mode: live_comment_fetch` を使い、operation index comment を live fetch して marker 抽出、payload digest 再計算、schema/semantic 再検証、tuple（repo / parent_issue / target）照合を fail closed で行う
- `embedded_payload` は互換モードの public-safe snapshot として保持してよいが、PR review live proof の closure 判定は embedded snapshot 単独では満たさない
- PR review surface の durable public artifact は `PR_REVIEW_SURFACE_LIVE_PROOF_V1` marker comment とし、selected object IDs、pagination 完全性、projection digest、operation index の再検証結果、proof target head SHA、contract snapshot URL だけを含める
- `PR_REVIEW_SURFACE_LIVE_PROOF_V1` comment には raw diff hunk、raw API response、review body、secret、trace を含めない
- `chatgpt-retro-context:resolve-live` は `resolved | missing | blocked_duplicate | blocked_malformed | blocked_malformed_marker_syntax | blocked_invalid_reference_chain | blocked_page_budget_exhausted | blocked_stale_write` の structured result を返す
- `chatgpt-retro-context:post` の blocked state は helper 内部では throw / nonzero exit を使うが、CLI surface では `error_code` を持つ machine-readable JSON を stdout に返す
- `chatgpt-retro-context:resolve-live` の結果は issue / pull request の両 target で常に `comment_chain`（コメントチェーンの component object）と `pr_review_surface`（PR review surface の component object。issue target では `status: not_applicable` の placeholder）の total result 構造を返す。トップレベルの `status` はこの 2 つの component status の aggregate である
- `comment_chain.pagination` / `pr_review_surface.pagination` はそれぞれ `comments_complete` / `reference_comments_complete` / `complete` の明示的 boolean 完全性フィールドを持つ。これらが厳密に `true` でない限り、resolver は `resolved` を返さない

### Marker candidate classifier / marker候補判定

- `chatgpt-retro-context-marker-helper.mjs` がエクスポートする `classifyChatgptRetroContextMarkerCandidate(body)` は、コメント本文の **first non-empty line（column 0）だけ** を検査し、`not_marker` / `valid_marker` / `malformed_marker_intent` の3状態を返す共有 classifier である
- prose 中の marker 名の言及、inline code、fenced code（backtick / tilde）、blockquote、list item、4-space/tab indented code 内の marker 文字列は、いずれも first non-empty line が `<!--` で始まらないため `not_marker` になる（candidate として検査されない）
- resolve-live の malformed marker検出、`validateChatgptRetroContextCommentBody`（post/upsert・duplicate検出・fixture resolver・post-write readback が共通で経由する）は、すべてこの共有 classifier を使用する（判定の split-brain を排除する）
- `upsertChatgptRetroContextComment` は create / supersede の live write 後に post-write readback を行い、同一 ownership marker がちょうど1件だけ存在することを再確認する（`blocked_post_write_duplicate` / `blocked_post_write_missing` で fail-closed）

### chatgpt-retro-context:assert-live

- `scripts/assert-chatgpt-retro-context-live.mjs`（`chatgpt-retro-context:assert-live`）は `resolve-live` 結果に対する fail-closed assertion CLI である。純粋な domain assertion 関数（`assertChatgptRetroContextLiveResult`）と、CLI adapter（`execution_profile: live | fixture` の分離・終了コード契約・resolver subprocess の timeout/signal/error 分類）を分離する
- 検証項目: repo / target / parent_issue / marker_comment_url / `comment_chain` の digest・payload_digest・matched_comment_count の identity、`comment_chain.status` と（pull_request target のみ）`pr_review_surface.status` が `resolved` であること、該当する全 pagination completeness field が明示的に `true` であること
- 終了コード: 引数不備・JSON parse失敗・subprocess spawn失敗・timeout・signal終了・GitHub API error は exit 2（`assertion_status: error`）、component mismatch・pagination不完全・identity不一致は exit 1（`assertion_status: fail`）、全項目満たす場合のみ exit 0（`assertion_status: pass`）
- `execution_profile: fixture` は subprocess regression test 専用であり、stdout JSON の `live_evidence_eligible` は execution_profile が `live` かつ `assertion_status: pass` のときだけ `true` になる。fixture profile の成功結果は `#1415` の live evidence として受理されない
- stdout には単一の `chatgpt_retro_context_live_assertion/v1` JSON を出力する。live profile では `checked_at` / `resolver_commit` / `command_args_digest` を追加で記録する

この責務境界により、`agent-run:post` を ChatGPT retro marker や retro index update と混同しない。

### ChatGPT retro marker の二層構造

外側コメントの ownership marker と内側の embedded payload marker は別契約である。混同しないこと。

````text
<!-- CHATGPT_RETRO_CONTEXT_V1 repo=squne121/loop-protocol target=pull_request:1254 parent_issue=1245 -->
<!-- CHATGPT_RETRO_CONTEXT_DIGEST_V1 sha256=<sha256(payload markdown)> -->

<!-- CHATGPT_RETRO_CONTEXT_V1 start -->
```json
{ "schema": "chatgpt_retro_context_marker/v1", "canonicalization": { "payload_digest": "sha256:..." } }
```
<!-- CHATGPT_RETRO_CONTEXT_V1 end -->
````

- outer digest は payload markdown 全体の sha256 である
- inner `canonicalization.payload_digest` は JSON payload の canonical digest である
- `resolve-live` は outer 2-line marker の ownership を確認した後、inner payload・run report comment・retro index comment・source-set digest を live 再検証する
- pull request target の `resolved` は comment chain だけでなく review surface pagination も complete であることを含む

## Review Correction Loop / レビュー修正ループ

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

## Follow-up Issue Creation / フォローアップ Issue 起票

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

## Hook Boundary Policy / Hook 境界ポリシー

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

## agent_run_report/v1 / スキーマ概要

agent run report は `scripts/agent-logs/finalize-agent-run.mjs` が生成する JSON artifact である。
単一の AI agent run に対する public-safe な要約を保持し、主に次の情報を含む。

- `schema`: 常に `"agent_run_report/v1"`
- `public_surface_kind`: report を公開しうる surface（`none`、`github_issue_comment`、`github_pr_comment`）
- `public_safety`: `{ redaction_status, checked_by, validator_version, checked_at, verdict, blocked_reasons, entirecli_safety }`
- `actor`: `{ type, name }`
- `authority`: `{ level, basis, evidence_refs }`
- `token_usage`: `{ availability, source, prompt, completion, total }`
- `manifest_refs`: manifest digest ref の一覧
- `evidence_refs`: evidence ref の一覧（workflow run、PR/Issue URL、artifact digest）
- `commands_summary`: command summary の一覧（`command_label`, `exit_code`, `verdict`, `summary`, `artifact_ref`）
- `docs_read_refs`: doc read ref の一覧

### observation_sources schema admission (C1 runtime) / 観測 source の runtime 受け入れ

`public_safety.observation_sources[]` は observation source admission の canonical field である。
C1 では `finalize-agent-run.mjs` -> `observation-source-adapter` -> `public_safety.observation_sources` の接続が必須で、
public surface では `observation_sources` が runtime で要求される。

この契約は public-safe な allowlist projection のみを対象とする。

- private observation の raw payload は `public` レポートに含めない（`raw_values_emitted` は `false` 限定）
- `provenance.ref.kind` は `observation_projection_digest` 固定で、`schema_ref` など汎用参照は利用しない
- `source_kind` と `capability_verdict` は `docs/dev/agent-observation-capability.md` の SSOT に従う
- `unsupported` と `unverified` はそのまま adapter 入力の availability signal 扱いとし、`availability: unavailable` として扱う
- `availability: unavailable` では metrics はすべて `null`、`reason_codes` には `source_unavailable` を補完
- `public_safety.observation_sources[].safety.reason_codes` は free-form text を禁止し、`source_unavailable` / `partial_projection` / `synthetic_only_evidence` / `host_inventory_only` だけを許す closed allowlist contract とする
- reason code 自体は `^[a-z][a-z0-9_]{0,79}$` と `maxLength: 80` を invariant とし、array 全体は `maxItems: 32` と `uniqueItems: true` を満たす。unknown/free-form/path-like/secret-like/prompt-like value は fail closed にする
- `capability_verdict: partial` かつ `availability: available` では `reason_codes` に `partial_projection` を必須とし、`validate-final-report.mjs` / `retro-index-builder.mjs` は reason code contract violation を digest mismatch とは別 reason として拒否する
- current producer contract は **single-source projection** を前提とする。現時点では aggregate producer を追加していないため、本ドキュメントでは aggregate metrics と呼ばない
- `observation_sources` の `provenance.source_projection_digest` が public projection の canonical JSON sha256 を持つこと
- `validate-final-report.mjs` と `retro-index-builder.mjs` は `provenance.source_projection_digest` と `provenance.ref.digest` を canonicalized public projection から再計算し、一致しない report を fail closed にする
- `token_usage` は観測 source とは別責務。`token_usage` は LLM API トークン集計のみ扱い、observation source projection / 理由・証跡を混同しない

`real_pilot_verified` は observation source の生成や検証出力では禁止とする。`provenance.evidence_mode` は #1220 承認前は `synthetic_only` 固定で、runtime validator / retro builder の両方が fail closed で拒否する。

`agent_run_report/v1` への Cloud pilot 統合は opaque reference のみ（`github_comment` / `observation_projection_digest`）、inline result body / inline metrics は禁止とする。`cloud_pilot_success_result/v1` の正本配置・placement 決定の詳細は `docs/adr/0005-cloud-pilot-success-result-artifact-placement.md`（#1330）を参照する。

### Remaining Parent Gaps / Out of Scope（今回の成功条件に含めない項目）

以下は current repo reality では **#1245 の成功条件に含めない**。

- Latitude metadata keys / saved searches / annotation taxonomy の SSOT 化と運用文書の追加
- public GitHub comments と private telemetry / Entire checkpoints の retention policy 分離
- retrospective result から follow-up Issue を draft / create して retro index へ自動反映する workflow 導線
- observation source の aggregate producer 導入と集計 contract の追加
- `token_usage.source` の enum migration や Latitude 専用 token producer 導入

### entirecli_safety runtime enforcement / EntireCLI 安全性の runtime 強制

`public_safety.entirecli_safety` は **schema-optional**（`#1134` / PR `#1178`）だが、
public surface report（`public_surface_kind !== 'none'`）では **runtime-required** とする。

runtime enforcement は `validate-final-report.mjs`（`validateFinalReport`）と
`retro-index-builder.mjs`（`normalizeSourceComment`）で実装している。

- `validateFinalReport` は schema validation の前に `assertEntireCLISafetyRuntime` を呼び出す
- public surface report に `entirecli_safety` が無い場合は `report.entirecli_safety_missing` を送出する
- `normalizeSourceComment` は、埋め込み report に `entirecli_safety` が無いか
  `verdict: 'blocked'` を含む source comment に対して `kind: 'blocked'` を返す

**checker-produced value だけを受け入れる。**
この field は `scripts/agent-logs/check-entirecli-safety.mjs` の出力をそのまま通す必要があり、
producer 側で `not_applicable` を自動合成してはならない。

schema gate に加えて、runtime で fail-closed となる条件は次のとおり。

| Condition | Error code |
|---|---|
| `entirecli_safety` absent from `public_safety` | `report.entirecli_safety_missing` |
| `schema_version` is not `entirecli_safety_result/v1` | `report.entirecli_safety_unknown_schema_version` |
| `raw_values_emitted === true` | `report.entirecli_safety_raw_values_emitted` |
| `verdict` not in `{ safe, not_applicable }` | `report.entirecli_safety_blocked` |

schema correctness（たとえば `blocked` verdict や unknown key）の canonical admission check は
引き続き schema gate（`validateAgentRunReport`）である。runtime enforcement layer は、
schema 単独では捕捉できない `missing` case に対する defense-in-depth を追加する。

### Forbidden Fields / 禁止フィールド

export pipeline が消費する source JSON には、次の field を **含めてはならない**。

| Field | 理由 |
|---|---|
| `raw_transcript` | セッション全文であり public-safe にできない |
| `transcript_excerpt` | 部分 transcript でも public-safe にできない |
| `full_command_output` | 非 redacted の command stdout/stderr |
| `stdout` | 非 redacted の stdout |
| `stderr` | 非 redacted の stderr |
| `local_path` | 環境依存で秘匿性を持ちうる local filesystem path |

補足: `VC_ADJUDICATION_RESULT_V1` の `stdout` は compact JSON のみを返し、`raw_stdout` / `raw_stderr` / `full_command_output` は private artifact 側に閉じる。public-safe の artifact では local path を出さず、opaque な `artifact_ref` + `artifact_digest` で参照する。

### transcript_hotspot_summary

`transcript_hotspot_summary` は、export source に許可される **唯一の transcript-derived field** である。
利用できるのは `public_safety.redaction_status === "clean"` が検証済みの場合に限る。

## ChatGPT Context Bundle Export / ChatGPT 向けコンテキスト束出力

`export-chatgpt-context` CLI は、retrospective analysis のために ChatGPT へ貼り付けられる
public-safe かつ deterministic な Markdown bundle を生成する。

### Script / 対象スクリプト

```
scripts/agent-logs/export-chatgpt-context.mjs
```

### CLI Usage / CLI 使用例

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

### Options / オプション

| Option | 説明 |
|---|---|
| `--parent-issue-json` | parent issue JSON への path（必須） |
| `--target-issue-json` | target issue JSON への path（必須） |
| `--retro-index-json` | agent retro index JSON への path（必須） |
| `--source-set-json` | source set JSON への path（必須） |
| `--run-report-json` | run report JSON への path（複数指定可） |
| `--evidence-ref-json` | evidence ref JSON への path（複数指定可） |
| `--max-chars` | bundle の文字数 budget（必須） |
| `--max-sections` | section 数の上限（必須） |
| `--generated-at` | bundle header 用の ISO-8601 timestamp（必須） |
| `--output` | output Markdown path（必須、overwrite 不可） |
| `--summary-json-out` | output summary JSON path（必須、overwrite 不可） |

### Section Priority Order (fixed) / 固定 section 優先順位

1. `safety_header` — `SECURITY_BOUNDARY` と `chatgpt_context_bundle/v1` の header
2. `source_manifest` — source file digest の一覧
3. `parent_goal` — parent issue と target issue の要約
4. `priority_signals` — friction、context pollution、human intervention、follow-up の signal
5. `ci_review_loops` — CI / review loop の data
6. `evidence_refs` — dedup 済み evidence ref の一覧
7. `lower_priority_narrative` — run report summary の要約
8. `omission_report` — budget 超過で省略した section

budget 超過時は lower-priority section から先に drop する。
`safety_header` と `priority_signals` を保持できないほど budget が小さい場合、CLI は `budget.too_small` で終了する。

### Security Properties / セキュリティ特性

- external-origin text はすべて `DATA` block または blockquote に閉じ込める
- 最終的に render された Markdown へ injection pattern scan をかけて reject する
- output は atomic に書き出し、partial write や overwrite を許可しない
- source file は処理前に forbidden field scan を通す
- 各 source file の digest を `source_manifest` に pin する

### Library Modules / ライブラリモジュール

| Module | 責務 |
|---|---|
| `lib/chatgpt-context-args.mjs` | CLI argument の parse と validation |
| `lib/chatgpt-context-source-loader.mjs` | source file の load、validate、digest 計算 |
| `lib/chatgpt-context-safety-scan.mjs` | injection scanner と DATA block wrapping |
| `lib/chatgpt-context-dedupe.mjs` | evidence ref の canonicalization と dedup |
| `lib/chatgpt-context-budget.mjs` | 優先順位を考慮した budget 配分 |
| `lib/chatgpt-context-renderer.mjs` | Markdown section renderer |

## EntireCLI Safety Checker / EntireCLI 安全性チェッカー

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

対象キーは `remote.*.url`、`remote.*.pushurl`、`remote.pushDefault`、`branch.*.pushRemote`、
`url.*.insteadOf`、`url.*.pushInsteadOf`、`include.path`、`includeIf.*.path`、
`remote.*.mirror`、`remote.*.push` である。

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

### Library Module / ライブラリモジュール

| Module | 責務 |
|---|---|
| `lib/entirecli-safety.mjs` | verdict 計算ロジック、redaction helper、設定 parser |
| `scripts/agent-logs/check-entirecli-safety.mjs` | CLI entry point（live git/fs 検査） |

## #1221 Agent Observation Capability Boundary / エージェント観測 capability 境界

`agent_observation_capability/v1` matrix（`docs/dev/agent-observation-capability.md`）の capture
capability verdict は synthetic evidence のみで固定する。本節では hook coexistence と canonical gate の
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

## #1405 Parent Closure Proof Contract / #1153 親 Issue クロージャ証明契約

This section defines the parent closure proof contract for #1153（本節は #1153 の親 Issue クロージャ証明契約を定義する）。

`agent_operation_session_index/v1`（`docs/schemas/agent-operation-session-index.schema.json`）は、
1 件の Issue / PR operation と、その operation を裏付ける `agent_run_report/v1` GitHub comment・
`agent_retro_index/v1` GitHub comment・`CHATGPT_RETRO_CONTEXT_V1` marker comment を接続する
public-safe index である。#1405 はこの index と `chatgpt_retro_execution_proof/v1`
（`docs/schemas/chatgpt-retro-execution-proof.schema.json`）を追加し、
「ChatGPT が GitHub connector だけでレトロスペクティブ可能である」ことの
synthetic route proof（`evidence_mode: synthetic_route_proof`）を machine-readable に閉じる。

#1153 の parent closure rule に、以下の `retro_e2e_proof_required` 契約を追加する。

```yaml
retro_e2e_proof_required:
  required: true
  minimum_targets:
    issue_operation: 1
    pull_request_operation: 1
  required_artifacts:
    - agent_operation_session_index/v1
    - agent_run_report/v1 GitHub comment
    - agent_retro_index/v1 GitHub comment
    - CHATGPT_RETRO_CONTEXT_V1 GitHub marker comment
    - chatgpt_retro_execution_proof/v1
    - chatgpt_retrospective_result/v1
  required_resolver_status: resolved
  chatgpt_access_boundary:
    github_connector_only: true
    local_file_access_used: false
    latitude_direct_access_used: false
    raw_trace_access_used: false
  safety:
    raw_values_emitted: false
    forbidden_fields_scan: pass
  real_capture_claim_allowed: false
```

- #1153 を close する前に、少なくとも 1 件の Issue target と 1 件の PR target
  （`operation.kind: pr_comment` に加え、`pr_review_submitted` / `pr_review_comment_created` /
  `pr_review_thread_resolved` を public-safe projection として再検証できること）
  で live GitHub comment chain を作成し、`resolve-live status: resolved` と
  ChatGPT connector-only retrospective result を GitHub 上に残すこと（本 Issue の Runtime Verification
  Applicability は `deferred`。live 検証証跡は PR verification comment として任意に添付し、CI 必須条件にはしない）。
- `evidence_mode: synthetic_route_proof` は real Latitude / EntireCLI / Cloud pilot 証明ではない。
  `real_pilot_verified` は #1220 `LATITUDE_PILOT_EXCEPTION_V1` が `approve_timeboxed_real_pilot` であり、
  かつ #1261 の distribution / argv / remote cleanup gate が machine-verified である場合のみ許可する。
- checker: `pnpm agent-operation-session-index:check` / `pnpm chatgpt-retro-e2e-proof:check`
  （`scripts/check-agent-operation-session-index.mjs` / `scripts/check-chatgpt-retro-e2e-proof.mjs`）
