---
taxonomy_schema_version: v1
status: draft
related_issue: "#268"
created_at: "2026-05-23"
---

# failure_class Taxonomy and Retry Policy

`gemini-cli-headless-delegation` の preflight および run wrapper の
result JSON で使用する `failure_class` 分類体系と retry policy の仕様。

## Background

Issue #71 の refinement-loop で判明した問題:
- `auth.ok:false` が transient エラー（quota / 一時的 API エラー）と
  真の認証失効を同一フィールドに丸めているため、retry 可否の判断ができない
- config error でも quota error でも同じ fail-close になっている
- caller 側で retry 可否を判断する根拠が result JSON に存在しない

本文書はこの問題を解消するための taxonomy と retry policy を定義する。

後続実装 Issue で `preflight_gemini_headless.py` および `run_gemini_headless.py`
のコードへの反映が行われる。

---

## failure_class の分類体系

`failure_class` フィールドは `nullable: true` で成功時は `null`。

### Non-retryable failures（fail-close）

これらは retry しても同じ結果になる構成・認証・スキーマ問題。
即時 fail-close して human intervention または config 修正を求める。

| `failure_class` | 意味 | 発生レイヤー | Raw Signal 例 |
|---|---|---|---|
| `request_schema_invalid` | `delegation_request_v1` の schema バリデーション失敗 | request_validation | `schema must equal delegation_request_v1` |
| `request_policy_denied` | tool_profile ポリシー違反（`proposal_only` での write 要求など） | request_validation | `proposal_only forbids direct file write/edit requests` |
| `config_invalid` | model_routing YAML が不正 / default_chain が空 | runtime_preflight | `model_routing config error: ...` / `routing_config_invalid` |
| `cli_missing` | `gemini` コマンドが見つからない | cli_process | `FileNotFoundError` / `command not found` |
| `cli_incompatible` | `gemini --help` が required flags を欠いている | cli_process | `gemini --help is missing: --output-format, ...` |
| `trusted_workspace_required` | smoke test で trusted directory エラー検出 | cli_process | stderr: `trusted directory` / `GEMINI_CLI_TRUST_WORKSPACE` |
| `auth_missing_or_expired` | OAuth トークン失効・認証未完了（headless_json 側） | api_backend | stderr: `not authenticated` / `PERMISSION_DENIED` + auth context |
| `gh_auth_required` | `github_research` で全 gh_commands が認証エラーで失敗 | github_preflight | `gh auth status` failed / `all gh_commands failed` |
| `mcp_config_invalid` | `local_asset_research` の Serena MCP 設定不正 | runtime_preflight | `local_asset_research requires .gemini/settings.json mcpServers.serena` |
| `mcp_tool_policy_invalid` | includeTools に危険なツールが含まれている | runtime_preflight | `local_asset_research includes dangerous Serena MCP tools` |
| `github_research_command_denied` | `github_research` で禁止 gh subcommand が検出された | request_validation | `github_research_command_denied` / `is not in the allowed subcommand list` |

### Retryable failures（backoff retry 可）

これらは一時的な状態変化で解消される可能性がある。
exponential backoff retry が有効。

| `failure_class` | 意味 | 発生レイヤー | Raw Signal 例 | `retry_scope` |
|---|---|---|---|---|
| `quota_or_rate_limited` | API quota / rate limit（RPM/TPM/RPD いずれか） | api_backend | HTTP 429 / `RESOURCE_EXHAUSTED` / `MODEL_CAPACITY_EXHAUSTED` / `quota` / `rate limit` / `too many requests` | `same_request_after_backoff` または `next_model` |
| `model_capacity_exhausted` | 特定モデルの処理キャパシティ不足（429 / capacity 系）、model downgrade で回復する場合がある | api_backend | `MODEL_CAPACITY_EXHAUSTED` / `model capacity` / HTTP 429 | `next_model` 優先 |
| `transient_api_error` | API バックエンドの一時障害（HTTP 500 / 503） | api_backend | HTTP 500 / HTTP 503 / `internal error` / `service unavailable` | `same_request_after_backoff` |
| `network_error` | ネットワーク到達不能・ソケットタイムアウト | cli_process | `connection refused` / `socket timeout` / `network unreachable` | `same_request_after_backoff` |
| `timeout` | `timeout_sec` 超過による subprocess タイムアウト | cli_process | `subprocess.TimeoutExpired` / exit code 124 | `same_request_after_backoff`（ただし timeout 拡大を要検討） |

### Terminal / exhausted failures

retry budget 枯渇や model chain 全滅など、これ以上 retry しても意味がない状態。
Human escalation を推奨。

| `failure_class` | 意味 |
|---|---|
| `model_chain_exhausted` | model_chain 内の全モデルが quota / capacity で失敗し chain 全滅 |
| `retry_budget_exhausted` | `RETRY_LIMIT` 回の retry 後も同じ failure が継続 |
| `unknown_cli_failure` | non-zero exit code だが既知パターンにマッチしない |
| `unknown_api_error` | Gemini envelope に `error` オブジェクトが含まれるが既知分類不能 |

### Conditionally retryable

状況依存で retry 可否が変わるクラス。

| `failure_class` | 意味 | retry 方針 |
|---|---|---|
| `output_parse_error` | Gemini CLI の JSON 出力が parse できない | 最大 1 回まで retry。CLI version incompatibility / stdout-stderr 混線の場合は retry 不可なので `classification_confidence: low` + human escalation |
| `empty_response` | `response_text` が空（API 呼び出し自体は成功） | 最大 1 回まで retry |

### ACP transport failure classes（`transport_details.failure_class`）

ACP transport の failure は `transport_details.failure_class` に格納され、
headless_json fallback が可能なものと不可なものが区別されている（`transport-acp.md` 参照）。

| `failure_class` | fallback 可否 | 意味 |
|---|---|---|
| `gemini_not_found` | yes | `gemini --acp` 起動で FileNotFoundError |
| `launch_failed` | yes | subprocess 起動エラー |
| `initialize_failed` | yes | initialize timeout / エラー |
| `session_new_failed` | yes | session/new の non-auth エラー |
| `auth_required` | **no** | session/new で認証要求が検出（fail-close で surface） |
| `prompt_error` | no | session/prompt がエラー応答 |
| `protocol_error` | no | final response 前に EOF / process death |
| `incomplete_response` | no | stopReason が end_turn でない / empty response |
| `timeout` | no | total timeout 超過 |
| `watchdog` | no | HeartbeatWatchdog によるトリップ |
| `contract_bypass` | no | `prepared_prompt` なしで `run_acp()` 呼び出し |

---

## result JSON フィールド仕様（AC2 対応）

### `gemini_headless_preflight_result/v1` への追加フィールド

```yaml
failure_class:
  type: string | null
  nullable: true
  meaning: "最も具体的な失敗分類。成功時は null。"
  values: [上記 taxonomy の全値]

retryable:
  type: boolean
  meaning: "caller がこの failure に対して同一リクエストで retry を試みてよいか。"
  note: "retry_scope も合わせて確認すること。"

retry_scope:
  type: string | null
  nullable: true
  values:
    - none                          # retry 不可（fail-close）
    - same_model                    # 同一モデルで即時 retry
    - next_model                    # model_chain の次モデルへ downgrade
    - same_request_after_backoff    # exponential backoff 後に同一リクエストで retry
    - after_external_state_change   # auth 修正 / config 修正など外部状態変化後のみ retry 可

attempts:
  type: int
  meaning: "Gemini CLI subprocess の総起動回数（全モデル・全 retry を合算）"
  example: 4

last_stderr_summary:
  type: string | null
  nullable: true
  constraints:
    max_chars: 240
    redact:
      - API keys（gho_, github_pat_, sk-, Bearer トークン等）
      - OAuth access tokens
      - absolute home paths（可能な範囲で）
  meaning: "最後の subprocess 実行の stderr（先頭 240 文字。機密情報は redact 済み）"
```

### `delegation_result/v1` への追加フィールド

```yaml
failure_class:
  type: string | null
  nullable: true
  meaning: "最も具体的な失敗分類。成功時は null。"

failure_origin:
  type: string | null
  nullable: true
  values:
    - request_validation    # schema / policy バリデーション失敗
    - runtime_preflight     # CLI / config / MCP 設定チェック失敗
    - cli_process           # subprocess 起動・タイムアウト・exit 非 0
    - api_backend           # Gemini API エラー（quota, transient, auth）
    - output_contract       # JSON parse / empty response
    - github_preflight      # gh_commands 実行失敗
    - acp_transport         # ACP transport 固有エラー
    - post_processing       # post_to_issue_url など後処理失敗

retryable:
  type: boolean
  meaning: "caller が retry を試みてよいか"

retry_scope:
  type: string | null
  nullable: true
  values: [none, same_model, next_model, same_request_after_backoff, after_external_state_change]

attempts:
  type: int
  meaning: "Gemini CLI subprocess の総起動回数（全モデル・全 retry を合算）"

attempts_by_model:
  type: list
  nullable: true
  item:
    model: string
    attempts: int
    final_failure_class: string | null
  example:
    - model: gemini-3-flash-preview
      attempts: 3
      final_failure_class: quota_or_rate_limited
    - model: gemini-2.5-flash
      attempts: 1
      final_failure_class: null   # 成功

last_stderr_summary:
  type: string | null
  nullable: true
  constraints:
    max_chars: 240
    redact: [API keys, OAuth tokens, absolute home paths]

classification_confidence:
  type: string
  values: [high, medium, low]
  meaning: >
    high: 既知の raw signal パターンに明確マッチ。
    medium: 間接的な推定。
    low: unknown / 推測が含まれる。human escalation 推奨。
```

---

## Retry Policy（AC3 対応）

### 基本方針

1. **fail-close group**（Non-retryable）: retry 一切不可。即時 fail-close して caller に返す。
   - 対象: `request_schema_invalid`, `request_policy_denied`, `config_invalid`,
     `cli_missing`, `cli_incompatible`, `trusted_workspace_required`,
     `auth_missing_or_expired`, `gh_auth_required`, `mcp_config_invalid`,
     `mcp_tool_policy_invalid`, `github_research_command_denied`

2. **backoff retry group**（Retryable）: exponential backoff retry 可。
   - 対象: `quota_or_rate_limited`, `model_capacity_exhausted`,
     `transient_api_error`, `network_error`, `timeout`
   - 既存実装: `RETRY_LIMIT = 2`、`time.sleep(min(2**attempt, 4))` で backoff
   - quota / capacity exhaustion 時は model downgrade（`retry_scope: next_model`）

3. **conditional retry group**: `output_parse_error`, `empty_response` は最大 1 回まで retry。
   `classification_confidence: low` の場合は human escalation を推奨。

### ACP transport の特例

`auth_required` は ACP transport の fail-close failure であり、
headless_json へのフォールバックを**行わない**（`transport-acp.md` の設計による）。
これは `auth.ok:false` 的な「aggregate field によるサイレント誤分類」を防ぐための意図的設計。

### Raw signal → failure_class の対応表（fixture table）

以下は実装時のテスト fixture として使用する。

| Raw signal | `failure_class` | `retryable` | `retry_scope` |
|---|---|---|---|
| `FileNotFoundError` on `gemini` launch | `cli_missing` | false | none |
| `gemini --help` missing `--output-format` | `cli_incompatible` | false | none |
| stderr: `trusted directory` / `GEMINI_CLI_TRUST_WORKSPACE` | `trusted_workspace_required` | false | after_external_state_change |
| HTTP 429 in stdout/stderr | `quota_or_rate_limited` | true | next_model |
| `RESOURCE_EXHAUSTED` in stdout/stderr | `quota_or_rate_limited` | true | same_request_after_backoff |
| `MODEL_CAPACITY_EXHAUSTED` in stdout/stderr | `model_capacity_exhausted` | true | next_model |
| HTTP 500 in stdout/stderr | `transient_api_error` | true | same_request_after_backoff |
| HTTP 503 in stdout/stderr | `transient_api_error` | true | same_request_after_backoff |
| `subprocess.TimeoutExpired` | `timeout` | true | same_request_after_backoff |
| exit code 124 | `timeout` | true | same_request_after_backoff |
| `socket timeout` / `connection refused` | `network_error` | true | same_request_after_backoff |
| `json.JSONDecodeError` on envelope | `output_parse_error` | true (max 1回) | same_model |
| `response_text` が空 / exit 0 | `empty_response` | true (max 1回) | same_model |
| model_routing YAML が invalid | `config_invalid` | false | after_external_state_change |
| `all gh_commands failed` | `gh_auth_required` | false | after_external_state_change |
| `github_research_command_denied` | `github_research_command_denied` | false | none |
| `local_asset_research requires mcpServers.serena` | `mcp_config_invalid` | false | after_external_state_change |
| `local_asset_research includes dangerous Serena MCP tools` | `mcp_tool_policy_invalid` | false | after_external_state_change |
| `PERMISSION_DENIED` (非 quota 文脈) | `auth_missing_or_expired` | false | after_external_state_change |

---

## #101 との依存関係

Issue #101 は preflight の per-profile 化を扱う。
現行 `preflight_gemini_headless.py` は `local_asset_research` の Serena 設定を
全 profile に対して検証して `failure_reason` に設定し即 return する問題がある（#101 未解決）。

本 taxonomy は以下の境界を採用する:

- **top-level `failure_class`**: preflight の全体成否を表す。#101 完了前は
  `local_asset_research` 関連の failure のみ `mcp_config_invalid` / `mcp_tool_policy_invalid` に分類。
  他プロファイルで Serena 設定が原因の誤 fail-close が発生した場合は `config_invalid` として扱い、
  #101 完了後に section-local classification に移行する。
- **section-local classification**: 各 section（`local_asset_research`, `gh_cli` 等）は
  `section.failure_class` として独立した failure_class を持つ（将来拡張）。
  top-level `failure_class` は最も重大な failure のみを反映する。

---

## 現行実装との差分（実装 Issue 起票時の参照用）

現行 `run_gemini_headless.py` / `preflight_gemini_headless.py` との主な差分:

1. **preflight の `failure_class`**: `trusted_workspace_required` のみ設定されている。
   `cli_missing`, `cli_incompatible`, `mcp_config_invalid`, `mcp_tool_policy_invalid`,
   `gh_auth_required` は未設定（`failure_reason` は設定されているが `failure_class` がない）。

2. **`retryable` フィールド**: 両スクリプトとも未実装。

3. **`attempts` フィールド**: `run_gemini_headless.py` は `RETRY_LIMIT = 2` の retry loop を
   実装しているが、result JSON に `attempts` を出力していない。
   `attempts_by_model` も未実装。

4. **`last_stderr_summary` フィールド**: 未実装。
   `warnings` 経由で stderr が surfaced されているが、
   caller が読みやすい形式で `last_stderr_summary` を出力していない。

5. **`failure_class` の backoff retry group**: `_is_retryable_capacity_failure()` が
   `MODEL_CAPACITY_EXHAUSTED` / `RESOURCE_EXHAUSTED` / HTTP 429 を検出して
   retry しているが、result に `failure_class: quota_or_rate_limited` を設定していない。
   Model chain exhaustion 時は `reason_code: model_chain_exhausted` が設定されるが、
   `failure_class` は別フィールド。

---

## 後続実装 Issue の分割方針

本 taxonomy を受けた実装は以下の 2 Issue に分割することを推奨する:

### Issue A: `run_gemini_headless.py` の result schema 拡張

対象ファイル:
- `.claude/skills/gemini-cli-headless-delegation/scripts/run_gemini_headless.py`
- `.claude/skills/gemini-cli-headless-delegation/tests/`

追加フィールド:
- `failure_class`（全クラスに対して正しく設定）
- `failure_origin`
- `retryable`
- `retry_scope`
- `attempts`
- `attempts_by_model`
- `last_stderr_summary`

### Issue B: `preflight_gemini_headless.py` の result schema 拡張

対象ファイル:
- `.claude/skills/gemini-cli-headless-delegation/scripts/preflight_gemini_headless.py`
- `.claude/skills/gemini-cli-headless-delegation/tests/test_preflight_gemini_headless.py`

追加フィールド（既存 `failure_class: trusted_workspace_required` に加えて）:
- `cli_missing`
- `cli_incompatible`
- `mcp_config_invalid`
- `mcp_tool_policy_invalid`
- `gh_auth_required`
- `retryable`
- `retry_scope`
- `last_stderr_summary`
- `classification_confidence`

依存関係: #101 完了後に section-local failure_class への移行を検討する。
