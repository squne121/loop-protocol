---
summary_ja: "本 ADR は Issue #1330 の OWNER 敵対的レビュー決定に基づき、cloud_pilot_success_result/v1 の正本 artifact placement を machine-readable に固定するものである。"
adr_id: "0005"
title: "cloud_pilot_success_result/v1 の正本 artifact placement — hybrid_reference_from_agent_run_report を採用する"
status: accepted
decision_date: "2026-07-06"
confirmed_date: "2026-07-06"
related_issues:
  - "#1330"
  - "#1153"
  - "#1260"
  - "#1326"
  - "#1261"
  - "#1220"
supersedes: []
superseded_by: null
---

# ADR 0005: cloud_pilot_success_result/v1 の正本 artifact placement — hybrid_reference_from_agent_run_report を採用する

## Context
PR #1325 の OWNER レビュー（REQUEST_CHANGES, P2, https://github.com/squne121/loop-protocol/pull/1325#issuecomment-4882061533）は、`cloud_pilot_success_result/v1`（Cloud pilot 実行結果を記録する artifact）の正本配置が未決定のまま `follow_up_issue_required` として残されていることを指摘した。これを受けて起票された Issue #1330 に対する OWNER 敵対的レビュー（https://github.com/squne121/loop-protocol/issues/1330#issuecomment-4892894141）が、本 ADR が固定する決定内容の一次情報源である。

`cloud_pilot_success_contract_v1`（#1260）は Cloud pilot の採用判断（`adopt_cloud` / `adopt_self_host` / `conditional_adoption` / `withdraw`）に必要な多数の metrics・gate・fail-closed 条件を定義している。この決定の実行結果（result）をどこに正本として置くかが未決定だと、以下のいずれかの誤りが起きる。

- `agent_run_report/v1` に result 本体を embed しようとして、既存 schema の `additionalProperties: false` 制約と衝突する
- standalone artifact のみとして扱い、`#1153` の GitHub retrospective 導線から result が孤立する

## Considered Options
**Option A: `embed_in_agent_run_report_v1`** — Cloud pilot result 本体を `agent_run_report/v1` に直接埋め込む
- メリット: 参照が 1 ファイルに閉じる
- デメリット: `docs/schemas/agent-run-report.schema.json` は top-level `additionalProperties: false` であり、`observationSourceMetrics` も `additionalProperties: false`（許可 field は `trace_count` / `span_count` / `prompt_tokens` / `completion_tokens` / `total_tokens` の 5 つのみ）。result 本体を embed するには schema 本体変更が必須になり、#1330 の Stop Condition（agent_run_report/v1 schema 本体変更禁止）と衝突する。**reject**

**Option B: `standalone_cloud_pilot_success_result_v1`** — `agent_run_report/v1` と独立した standalone artifact/comment のみを正本にする
- メリット: `agent_run_report/v1` の schema 変更が一切不要
- デメリット: standalone のみだと #1153 の GitHub retrospective 導線から result artifact が孤立する。単独案として **reject**

**Option C: `hybrid_reference_from_agent_run_report`** — result 本体は standalone の public-safe result artifact/comment に置き、`agent_run_report/v1` には opaque reference（`github_comment` / `observation_projection_digest`）と digest だけを持たせる
- メリット: `agent_run_report/v1` の既存 schema を変更せずに済み、かつ #1153 の GitHub retrospective 導線から参照可能になる
- デメリット: result 本体と reference の 2 つを整合させる digest / upsert 運用が必要になる

## Decision
**Option C（`hybrid_reference_from_agent_run_report`）を採用する。**

```yaml
cloud_pilot_success_result_placement_decision_v1:
  schema: cloud_pilot_success_result_placement_decision/v1
  issue: "#1330"
  parent_issue: "#1153"
  decision_source: "OWNER adversarial review, https://github.com/squne121/loop-protocol/issues/1330#issuecomment-4892894141"
  candidate_options:
    - embed_in_agent_run_report_v1
    - standalone_cloud_pilot_success_result_v1
    - hybrid_reference_from_agent_run_report
  selected_option: hybrid_reference_from_agent_run_report
  decision_required: true
  decision_recorded_as_of_this_issue: true

  canonical_result_artifact:
    schema: cloud_pilot_success_result/v1
    surface: github_issue_comment
    body_kind: public_safe_projection_only

  agent_run_report_integration:
    allowed: true
    mode: opaque_reference_only
    allowed_reference_kinds:
      - github_comment
      - observation_projection_digest
    inline_result_body_allowed: false
    inline_cloud_pilot_metrics_allowed: false
    agent_run_report_schema_body_change_required: false

  rejected_options:
    embed_in_agent_run_report_v1:
      reason: >
        docs/schemas/agent-run-report.schema.json は top-level additionalProperties:false
        かつ observationSourceMetrics も additionalProperties:false（許可フィールドは
        trace_count/span_count/prompt_tokens/completion_tokens/total_tokens の5つのみ）。
        cloud pilot の result 本体を embed するには schema 本体変更が必要になり、
        本 Issue の Stop Condition（agent_run_report/v1 schema 本体変更禁止）と衝突するため reject。
        ただし opaque reference（github_comment / observation_projection_digest）の embed のみは許可する。
    standalone_cloud_pilot_success_result_v1:
      reason: >
        standalone のみだと #1153 の GitHub retrospective 導線から result artifact が
        孤立するため、agent_run_report からの参照が必須。単独案として reject し hybrid を採用。

  cloud_pilot_success_result_checker_follow_up_required: true
  in_this_issue: design_decision_and_docs_only
  requires_follow_up_issue: true
  follow_up_issue_ref: TBD_after_decision
  follow_up_issue_materialization_required_before_close: true

  cloud_adoption_allowed_now: false
  not_adoption_ready_until_1261_1326_and_artifact_placement_complete: true
```

同ファイル内に上記 decision block は **1 個だけ** とする（`rg -c` で 1 件のみであることを検証可能にする）。

### `marker_schema`

GitHub comment を正本とするため、外側の ownership marker と内側の digest marker を分離する（既存 ChatGPT retro marker の二層構造 — `docs/dev/agent-run-report.md` の「ChatGPT retro marker の二層構造」節 — に準拠する）。

```yaml
marker_schema:
  schema: cloud_pilot_success_result_marker/v1
  outer_marker:
    format: "<!-- CLOUD_PILOT_SUCCESS_RESULT_V1 repo=<owner/repo> target=issue:<pilot_issue> parent_issue=1153 result_id=<stable-id> -->"
    purpose: ownership_and_target_identification
  digest_marker:
    format: "<!-- CLOUD_PILOT_SUCCESS_RESULT_DIGEST_V1 sha256=<64hex> -->"
    purpose: payload_integrity
  payload_schema:
    schema: cloud_pilot_success_result/v1
    fenced_as: json_or_yaml
    required_fields:
      - schema
      - result_id
      - parent_issue
      - contract_issue
      - placement_issue
      - gate_refs
      - decision
      - metrics
      - safety
  target_fields:
    - repo
    - target (issue:<N> または pull_request:<N>)
    - parent_issue
    - result_id
  duplicate_handling:
    zero_matches: create
    one_match: update_existing_comment_by_comment_id
    multiple_matches: fail_closed
    stale_digest_mismatch: fail_closed
```

### `schema_path`

```yaml
schema_path:
  reserved_path: docs/schemas/cloud-pilot-success-result.schema.json
  exists_in_this_issue: false
  implementation_follow_up_required: true
```

`docs/schemas/cloud-pilot-success-result.schema.json` は本 Issue では **作成しない**（reserved path のみを固定する）。schema 本体の実装は follow-up Issue の scope である。

### `checker_command`

```yaml
checker_command:
  reserved_command: "pnpm run cloud-pilot-success-result:check"
  exists_in_this_issue: false
  implementation_follow_up_required: true
```

`checker_command` は本 Issue では実装しない（reserved command のみを固定する）。checker 実装は follow-up Issue の scope である。

### `digest_policy`

digest は **public-safe projection の canonical body** に対してのみ計算する。raw trace body、prompt、tool I/O、credential、local path 等を digest 入力に含めることは禁止する。

```yaml
digest_policy:
  digest_algorithm: sha256
  digest_input: canonical_public_safe_projection_json
  canonicalization:
    encoding: utf8
    newline: lf
    object_key_order: lexical
    insignificant_whitespace: stripped
  forbidden_digest_inputs:
    - raw_trace_body
    - raw_prompt
    - raw_tool_input
    - raw_tool_output
    - credential_value
    - local_path
    - argv
    - stdout
    - stderr
  digest_publication_allowed: true
  raw_source_digest_publication_allowed: false
```

既存の `provenance.source_projection_digest` 再計算 fail-closed 方針（`docs/dev/agent-run-report.md` の observation_sources runtime 受け入れ節を参照）と整合させる。

### `github_comment_upsert_policy`

GitHub Issue comment API は list/create/update が分かれており、update は `comment_id` 指定が必須である。したがって upsert は pagination-aware かつ duplicate-safe、stale digest は fail-closed とする。

```yaml
github_comment_upsert_policy:
  target_surface: github_issue_comment
  list_comments:
    pagination_required: true
    per_page: 100
  match_selector:
    exact_outer_marker: CLOUD_PILOT_SUCCESS_RESULT_V1
    match_fields:
      - repo
      - target
      - parent_issue
      - result_id
  zero_matches: create
  one_match: update_existing_comment_by_comment_id
  multiple_matches: fail_closed
  stale_digest_mismatch: fail_closed
  actor_restriction:
    allowed_authors:
      - repository_owner
      - "github-actions[bot]"
  duplicate_comment_creation_allowed: false
```

### `raw_trace_body_publication_forbidden_fields`

既存 `docs/dev/agent-run-report.md` の Forbidden Fields（`raw_transcript` / `transcript_excerpt` / `full_command_output` / `stdout` / `stderr` / `local_path`）に加え、Cloud pilot trace/span 由来の項目を拡張して禁止する。

```yaml
raw_trace_body_publication_forbidden_fields:
  - raw_trace_body
  - raw_span_body
  - raw_event_body
  - span_attributes_raw
  - resource_attributes_raw
  - request_body
  - response_body
  - request_headers
  - response_headers
  - authorization
  - cookie
  - set_cookie
  - api_key
  - credential
  - raw_prompt
  - full_prompt
  - system_prompt
  - tool_input
  - tool_output
  - command_line
  - argv
  - env
  - stdout
  - stderr
  - full_command_output
  - local_path
  - shell_history
  - terminal_scrollback
  - provider_console_url_unredacted
```

public projection は raw span attributes/events をそのまま出さず、count / coverage / latency / digest / closed enum reason code へ落とす。このリストへの参照は `docs/dev/secret-policy.md` にも追記する（AC8）。

## Consequences
### 肯定的影響

- `agent_run_report/v1` の既存 schema（`additionalProperties: false`）を変更せずに Cloud pilot result を GitHub retrospective 導線から参照可能にできる
- result 本体と reference を分離することで、public-safe projection の digest 検証を独立して行える
- `raw_trace_body_publication_forbidden_fields` を拡張することで、trace/span 由来の secret-like / prompt-like フィールドの漏洩を防止できる

### 否定的影響 / トレードオフ

- result 本体（standalone artifact）と reference（`agent_run_report` 内 opaque reference）の 2 つを整合させる digest / upsert 運用が必要になる
- `schema_path` / `checker_command` は本 Issue では reserved のみであり、follow-up Issue が完了するまで checker による machine verification はできない

### 未解決事項（follow-up Issue へ委譲）

- `cloud_pilot_success_result/v1` の checker / schema / negative fixture の実装コード
- `follow_up_issue_ref` は本 Issue のマージ後に採番される新規 Issue 番号で更新する（`TBD_after_decision` のまま Issue を close しない）
- `cloud_adoption_allowed_now: false` は #1326（`cloud_pilot_success_contract_v1` checker 実装）および本 Issue の follow-up checker 実装が完了するまで維持する

## References
- Issue #1330（本 ADR の実装 Issue）
- Issue #1153（parent — Cloud pilot 採用判断の親 Issue）
- Issue #1260（`cloud_pilot_success_contract_v1` 定義 Issue）
- Issue #1326（`cloud_pilot_success_contract_v1` checker/schema/negative fixture 実装 Issue）
- Issue #1261（distribution / argv exposure / remote cleanup evidence gate）
- Issue #1220（Latitude real pilot 例外判断）
- PR #1325（OWNER レビューで follow_up_issue_required を指摘した PR）
- `docs/dev/agent-run-report.md`（`agent_run_report/v1` schema・observation_sources・ChatGPT retro marker 二層構造）
- `docs/dev/secret-policy.md`（Cloud Pilot Success Contract セクション、raw_trace_body_publication_forbidden_fields 参照）
