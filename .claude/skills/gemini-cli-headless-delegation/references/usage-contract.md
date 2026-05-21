# Usage Contract

## Request Contract
`delegation_request_v1` は次の形を取る。

```json
{
  "schema": "delegation_request_v1",
  "objective": "Investigate the latest failure mode in the build logs",
  "instructions": [
    "Summarize the failure in plain language.",
    "Identify likely root causes with evidence."
  ],
  "tool_profile": "no_tools",
  "output_sections": [
    "Summary",
    "Findings",
    "Evidence"
  ],
  "context_files": [
    "logs/build.log",
    "notes/context.md"
  ],
  "inline_context": "Optional extra context",
  "model": "gemini-3-flash-preview",
  "timeout_sec": 600,
  "post_to_issue_url": "https://github.com/owner/repo/issues/123"
}
```

### Required Fields

> **すべて必須。1 つでも欠けるか値が不正なら `ok: false` になる。**

| フィールド | 制約 |
|---|---|
| `schema` | `"delegation_request_v1"` 固定。省略・別値は即拒否。 |
| `objective` | 具体的な目標。曖昧な動詞のみは拒否。 |
| `instructions` | 2 件以上のリスト。 |
| `tool_profile` | **`"no_tools"` / `"grounded_research"` / `"local_asset_research"` / `"proposal_only"` / `"github_research"` のいずれか**。`"code_analysis"` 等の別値は拒否される。 |
| `output_sections` | 1 件以上のリスト。 |
| `context_files` | 1 件以上。パス解決に関する注意は下記参照。 |

### Optional Fields（拡張）

| フィールド | 型 | 説明 |
|---|---|---|
| `role` | string（任意） | quota 枯渇時の降格チェーン選択に使用する。有効値は `roles` マップのキー（例: `web_research` / `implementation` 等）。`tool_profile` とは独立した概念であり、同時指定可能。 |
| `gh_commands` | array（任意） | `[{"argv": [...]}]` 形式の argv ベースコマンドリスト。wrapper が事前実行し結果を `inline_context` に prepend する general field。profile ごとに許可 allowlist が異なる（詳細は「gh_commands general field 仕様」セクション参照）。`github_research`・`local_asset_research`・`proposal_only` で完全実装済み。 |

### `tool_profile` の責務境界

| Profile | 入口 | 許可される外部/ローカル能力 | 禁止事項 |
|---|---|---|---|
| `no_tools` | isolated temp cwd | `context_files` と `inline_context` のみ | tools、repo 探索、shell execution、file edit/write |
| `grounded_research` | isolated temp cwd | Google Search grounding | shell execution、file edit/write、repo 探索、Serena MCP |
| `local_asset_research` | repo root | Serena MCP の read-only tool による WSL-local ローカル資産調査 | Google Search、shell execution、file edit/write、GitHub write、repo 外の任意読み取り、`post_to_issue_url` |
| `proposal_only` | isolated temp cwd | bounded draft text (`implementation_draft` / `issue_authoring_draft` / `patch_proposal` / `command_plan`) | file edit/write、shell execution、GitHub write / `post_to_issue_url`、repo 探索、実装完了を装う報告 |
| `github_research` | repo root | wrapper が許可コマンドを `gh` で実行し結果を `inline_context` に prepend。Gemini は結果を解釈して報告を返す | `post_to_issue_url`、gh write コマンド（issue comment/edit/create/close 等）、`gh api` 非 GET method、shell 実行、file edit/write |

`local_asset_research` は `no_tools` と違い Serena MCP を使えるが、対象は repo 内の read-only ローカル資産調査だけである。`grounded_research` と違い外部 Web grounding は使わない。

`proposal_only` は `no_tools` と同じ isolated temp cwd で動くが、目的は調査結果そのものではなく「Codex 側 worker が採用・修正・実行できる下書き」を返すことにある。最終 file edit / shell 実行 / GitHub mutation は Codex 側に残し、Gemini 側には proposal text だけを持たせる。

`local_asset_research` の `context_files` は、絶対パス・相対パスのどちらでも `Path.resolve()` 後の symlink 解決済みパスが repo root 配下にある場合だけ許可される。repo 外へ解決される絶対パス、`../` 参照、symlink は `failure_reason` / `warnings` に理由を残して fail-closed する。

`local_asset_research` は `.gemini/settings.json` の以下を wrapper が machine-checkable に確認できる場合だけ実行する:

- `mcp.allowed` が `["serena"]` である。
- `mcpServers.serena.command` が `uvx` で、`args` に `serena` と `--project-from-cwd` が含まれる。
- `mcpServers.serena.trust` が `false` である。
- `mcpServers.serena.includeTools` が `find_file` / `find_referencing_symbols` / `find_symbol` / `get_symbols_overview` / `list_dir` / `search_for_pattern` のみである。
- `execute_shell_command`、`write_file`、`read_file_content`、`read_memory`、`write_memory` などの危険 tool は `excludeTools` で denylist されている。

上記が未検証 MCP 設定、危険 tool、または Windows wrapper / repo 外読み取りを含む場合、wrapper は fail-closed として `ok: false`、`failure_reason`、`warnings` を返す。曖昧に `grounded_research` へ流用してはならない。

Gemini CLI の認証は OAuth / Google アカウント認証を前提にする。headless 実行前に interactive login 済みの cached credential があること、trusted workspace が成立していること、`.env` がこの前提と矛盾する API key / Vertex ADC 前提へ切り替えていないこと、project-scoped `.gemini/settings.json` が Serena MCP 設定として有効であることを Stop Conditions として扱う。

### `context_files` のパス解決

wrapper は **isolated temp cwd**（`tempfile.mkdtemp()` で生成されたディレクトリ）から Gemini を起動する。このため、`context_files` のパスは以下のルールで解決される:

1. **絶対パス（推奨）**: そのまま使われる。リポジトリの場所に依存しない。
   ```json
   "context_files": ["/home/user/project/logs/build.log"]
   ```

2. **`request.json` からの相対パス**: `request_path.parent` を基準に解決される。
   ```json
   "context_files": ["logs/build.log"]  // request.json と同じディレクトリを基点とする
   ```

3. **リポジトリルートからの相対パス（非推奨・失敗する）**: isolated temp cwd はリポジトリルートではないため、`missing_context_file` で失敗する。
   ```json
   // NG: wrapper の cwd がリポジトリルートとは限らない
   "context_files": [".kiro/specs/kindle-content-ingestion/design.md"]
   ```

**PR #346 での失敗事例**: `.kiro/specs/kindle-content-ingestion/design.md` をリポジトリ相対パスで指定したため、isolated temp cwd から解決できず `missing_context_file` が発生した。

### `request_path` と相対パス解決（テスト時の注意）

> 実行時のパス解決ルール（絶対パス推奨・リポジトリ相対パス禁止）は上記「`context_files` のパス解決」セクションを参照。ここではテストコードでの `validate_request` 呼び出し時の注意を扱う。

`validate_request(request, request_path=...)` の `request_path` 引数は省略可能だが、
省略した場合は `Path.cwd()` を基準に `context_files` の相対パスを解決する。

**テストで `validate_request` を呼ぶ場合の注意**:
- `context_files` に相対パスを指定するときは、必ず `request_path` を渡すか `monkeypatch.chdir` でカレントディレクトリを合わせること。
- `request_path` を渡す場合: `context_files` は `request_path.parent` を基準に解決される。
  CI 環境では `cwd` がリポジトリルートになるため、`monkeypatch.chdir` のみで解決するとパスが変わることがある。
- `run_delegation` も同様に `request_path` を渡すことで相対パスを安定させられる。

### Optional Fields
- `inline_context`: 追加コンテキスト文字列。
- `model`: 使用モデル。省略時は `"gemini-3-flash-preview"`。
- `timeout_sec`: タイムアウト時間（秒）。省略時は wrapper の既定値。
- `post_to_issue_url`: GitHub Issue/PR の URL。指定時は調査結果を自動的に `gh issue comment` で投稿する（詳細は後述の「`post_to_issue_url` を使ったコメント自動投稿」参照）。
- `gh_commands`: `[{"argv": [...]}]` 形式の argv ベースコマンドリスト（general field）。詳細は「gh_commands general field 仕様」セクション参照。

## Request Rejection Rules
- `objective` is only vague verbs or filler words
  - Exception: objectives containing paths, filenames, or line numbers are accepted regardless of language（言語問わず、パス・ファイル名・行番号を含む objective は受理される）
- `context_files` is missing, empty, or any referenced file is absent
- `instructions` has fewer than 2 entries
- `output_sections` is empty
- `tool_profile` is not explicit
- `schema` is not `delegation_request_v1`
- `tool_profile=local_asset_research` and `post_to_issue_url` is present
- `tool_profile=local_asset_research` and any `context_files` entry resolves outside the repository root
- `tool_profile=local_asset_research` and Serena MCP read-only allowlist cannot be verified from `.gemini/settings.json`
- `tool_profile=proposal_only` and `post_to_issue_url` is present
- `tool_profile=proposal_only` and the request instructs direct file edits, shell execution, or GitHub mutation instead of proposal text

## Result Contract
`delegation_result_v1` contains:

### Core Fields（常に存在）
- `ok`（boolean）: 調査の成功/失敗
- `requested_model`（string）: リクエストで指定したモデル
- `actual_model`（string）: 実際に使用したモデル（`"unknown"` の場合あり）
- `model_chain`（list[str]）: 試行対象だった model のリスト（chain 全体）。常に存在。
- `model_downgrades`（list[{from, to, reason}]）: 降格イベントのリスト。降格なし時は `[]`。常に存在。
- `tool_profile`（string）: `"no_tools"`、`"grounded_research"`、または `"local_asset_research"`
- `exit_code`（integer）: Gemini CLI の終了コード
- `result_surface`（object）: artifact-first / summary-first の薄い返却面。caller はまずここを見る
- `response_text`（string）: Gemini の調査結果（`ok: true` かつ投稿なし時、または投稿失敗時）
- `stats`（object）: 実行統計（`--compact` 未指定時）
- `stderr`（string）: Gemini CLI の stderr 出力
- `warnings`（array[string]）: 警告・エラーメッセージ
- `raw_command`（string）: 実行した完全な Gemini CLI コマンド（`--compact` 未指定時）
- `schema`（string）: `"delegation_result_v1"` 固定
- `reason_code`（string、エラー時のみ）: fail-closed 理由コード。値: `model_chain_exhausted` / `unknown_role` / `empty_chain` / `routing_config_invalid`

### `post_to_issue_url` 関連フィールド（条件付き）
`post_to_issue_url` がリクエストに含まれ、投稿が試行された場合のみ以下が追加される:

| フィールド | 型 | 必須条件 | 説明 |
|----------|--|---------|------|
| `post_to_issue_url` | string | `post_to_issue_url` がリクエストで指定された場合 | 投稿先 GitHub Issue/PR の URL（リクエストから転記） |
| `comment_url` | string &#124; null | 投稿成功時のみ存在 | 投稿されたコメントの URL。失敗時は absent（フィールドが存在しない） |
| `post_result` | string | 投稿試行時のみ存在 | `"success"` または失敗理由（`"failed: <stderr テキスト>"` / `"error: <例外テキスト>"` 形式） |

### `result_surface` の形

`result_surface` は `references/result-surface.md` を正本とし、少なくとも以下を含む:

| フィールド | 型 | 説明 |
|---|---|---|
| `mode` | string | `"artifact-first"` 固定 |
| `summary` | string &#124; null | caller 向け 1-2 文の短い要約 |
| `primary_artifact_type` | string | `github_comment_url` / `inline_response_text` / `none` |
| `primary_artifact` | string &#124; null | full report の所在。comment URL か `"response_text"` pointer |
| `next_action` | string | caller が次に何を見るべきかの短い指示 |

### Result Rules
- `response_text` is extracted from the Gemini envelope `response` field.
- `result_surface` is derived from `response_text` and, when available, `comment_url`.
- `actual_model` is taken from `stats.models`; otherwise `unknown`.
- stderr is preserved as a warning channel, not discarded.
- `ok` depends on the Gemini exit code and envelope parse result.
- auto fallback to another model is forbidden.
- `local_asset_research` で `post_to_issue_url` が指定された request は validation で拒否されるため、GitHub 自動投稿は発生しない。
- `post_to_issue_url` が指定されていない場合、`post_to_issue_url`, `comment_url`, `post_result` の 3 フィールドは **absent**（存在しない）。
- `post_to_issue_url` が指定されても、`ok: false` または `response_text` が空の場合は投稿スキップ。この場合、`post_to_issue_url`, `comment_url`, `post_result` はいずれも absent（result に含まれない）。
- 投稿試行時（`ok: true` かつ `response_text` が存在する場合）、投稿成功時は `comment_url` を含める。投稿失敗時は `comment_url` は absent で、`post_result` に失敗理由を記載。
- caller は `response_text` 全文を常に main thread に再注入しない。まず `result_surface.summary` / `result_surface.primary_artifact` / `result_surface.next_action` を使い、detail が必要なときだけ `response_text` を読む。

### stdout 出力仕様

`run_gemini_headless.py` 実行時、JSON ファイルの書き出しに加えて以下の内容が stdout に出力される。

| 条件 | stdout 出力内容 |
|---|---|
| `ok: true` かつ `response_text` が存在する | `response_text` の全内容 |
| `ok: true` かつ `response_text` が空文字列 | `[gemini-headless] warning: response_text is empty` |
| `ok: false` かつ `warnings` が存在する | `warnings[0]`（最初の失敗理由） |
| `ok: false` かつ `warnings` が空 | `[gemini-headless] error: delegation failed (no failure reason available; see result JSON)` |
| 常に（上記に加えて） | `[gemini-headless] result saved to: <output-file のパス>` |

**AI エージェント向け注意**:
- `ok: true` 時は stdout に最終報告が出力されるため、JSON ファイルを Read せずに結果を利用できる。
- JSON ファイルには詳細ログ（`stats`、`raw_command`、全 `warnings`）が保持されており、必要時に参照できる。
- `ok: false` 時は stdout の失敗理由を確認し、`warnings` フィールド全体が必要な場合のみ JSON を Read する。

### jq 抽出パターン集

AI エージェントが JSON ファイルを丸ごと Read せずに必要フィールドを取得するための推奨コマンド例:

```bash
# result surface の summary を取得（通常はこちらを優先）
jq -r '.result_surface.summary' /path/to/result.json

# full report の所在だけ取得（comment URL または response_text pointer）
jq -r '.result_surface.primary_artifact' /path/to/result.json

# response_text のみ取得（detail が必要なときだけ）
jq -r '.response_text' /path/to/result.json

# 軽量サマリー（artifact-first）
jq '{ok, result_surface, warnings, exit_code}' /path/to/result.json

# ok 判定のみ（成功/失敗の確認）
jq -r '.ok' /path/to/result.json
```

### `actual_model = "unknown"` の扱い

**返る条件**: Gemini CLI の `stats.models` が空、形式変更、または stats 自体が存在しない場合
（`run_gemini_headless.py:242-251` の `_extract_actual_model()` 参照）。

**推奨する caller 対処**:
- `actual_model == "unknown"` でも `ok == true` かつ `response_text` が存在すれば、応答自体は有効。
- caller はログに `unknown` を記録し、必要に応じて `requested_model` を代用する。

**`ok == false` かつ `actual_model == "unknown"` の場合**:
- Gemini CLI 自体が応答していない可能性が高い。
- `stderr` / `warnings` フィールドを確認し、preflight の再実行を推奨。

## CLI Usage

### `run_gemini_headless.py`

```
run_gemini_headless.py --request-file <path> --output-file <path> [--compact] [--output-format {json,ndjson}]
```

| オプション | 説明 |
|---|---|
| `--request-file` | `delegation_request_v1` JSON ファイルのパス（必須）。 |
| `--output-file` | `delegation_result_v1` JSON を書き出すパス（必須）。 |
| `--compact` | 出力 JSON から `stats` と `raw_command` を除外する。AI エージェントが `ok`・`response_text`・`warnings` のみを必要とする場面でコンテキストウィンドウを節約する。デフォルトはフル出力（後方互換）。 |
| `--output-format` | 出力形式を指定する。`json`（デフォルト、上書き）または `ndjson`（追記、1行1JSONオブジェクト）。後方互換のためデフォルトは `json`。 |

#### `--compact` 動作（`run_gemini_headless.py`）

- **`--compact` 指定時**: `stats` と `raw_command` が出力 JSON に含まれない。最小フィールドは `ok`, `response_text`, `warnings`, `stderr`, `exit_code`, `actual_model`, `schema`, `requested_model`, `tool_profile`。
- **`--compact` 未指定時**: 従来通り全フィールドを出力（後方互換）。

#### `--output-format ndjson` 動作（`run_gemini_headless.py`）

- **`--output-format ndjson` 指定時**: 出力ファイルに JSON オブジェクトを1行で**追記**（append）する。複数回実行すると各実行結果が1行ずつ蓄積されるため、AI エージェントは `tail -1` で最新結果のみ取得できる。
- **`--output-format json`（デフォルト）**: 従来通り整形された JSON を上書き出力する（後方互換）。

##### NDJSON 利用例

```bash
# NDJSON 形式で実行結果を追記する
python3 run_gemini_headless.py \
  --request-file request.json \
  --output-file results.ndjson \
  --output-format ndjson

# 最新結果のみ取得（コンテキスト節約）
tail -1 results.ndjson | jq '.response_text'

# ok=true の結果のみ抽出する
grep '"ok": true' results.ndjson | tail -1 | jq '.response_text'

# 全結果を配列として読み込む（jq -s）
jq -s '.' results.ndjson

# 追記確認（N回実行後にN行あること）
wc -l results.ndjson

# 各行の有効性確認
while IFS= read -r line; do echo "$line" | jq . > /dev/null && echo "OK" || echo "INVALID"; done < results.ndjson
```

- `--compact` と `--output-format ndjson` は組み合わせ可能。compact 後のオブジェクトを1行で追記する。

### `preflight_gemini_headless.py`

```
preflight_gemini_headless.py --output-file <path> [--compact]
```

| オプション | 説明 |
|---|---|
| `--output-file` | `gemini_headless_preflight_result/v1` JSON を書き出すパス（必須）。 |
| `--compact` | 出力 JSON からデバッグ専用の冗長フィールドを除外する。正常/異常の判断に必要な本質フィールドのみを残し、コンテキストウィンドウを節約する。デフォルトはフル出力（後方互換）。 |

#### `--compact` 動作（`preflight_gemini_headless.py`）

- **`--compact` 指定時**: 各セクションから冗長フィールドを除外する。

  | セクション | 除外フィールド | 保持フィールド |
  |---|---|---|
  | `version` | `stdout`, `stderr` | `ok`, `value` |
  | `help` | `stdout`, `stderr`, `required_flags` | `ok`, `missing_flags` |
  | `smoke` | `command`, `stdout`, `stderr`, `stats` | `ok`, `response_text` |

  トップレベルの `schema`, `ok`, `failure_reason`, `warnings` は保持される。

- **`--compact` 未指定時**: 従来通り全フィールドを出力（後方互換）。

## Preflight Contract
- `gemini --version`
- `gemini --help` exposes `--model`, `--prompt`, `--output-format`, `--approval-mode`
- isolated smoke command:
  - `gemini --model gemini-3-flash-preview --approval-mode plan --skip-trust --prompt 'Do not use any tools. Reply with OK only.' --output-format json`

`--skip-trust` は preflight smoke と本番委譲コマンドの両方に**既定で付与**される。headless / CI 環境では isolated temp cwd が trusted directory と判定されないため、trust 機構を bypass する必要がある（Issue #1824）。

### Preflight stdout 出力仕様

`preflight_gemini_headless.py` 実行時、JSON ファイルの書き出しに加えて以下の内容が stdout に出力される。

| 条件 | stdout 出力内容 |
|---|---|
| `ok: true` | `[gemini-preflight] ok: Gemini CLI <version> is ready` |
| `ok: false` かつ `failure_class: "trusted_workspace_required"` | `[gemini-preflight] error: trusted_workspace_required — <failure_reason>` （改行）`[gemini-preflight] recovery: <recovery_action> (GEMINI_CLI_TRUST_WORKSPACE=true)` |
| `ok: false` かつ `failure_reason` が存在する（上記以外） | `[gemini-preflight] error: <failure_reason>` |
| `ok: false` かつ `failure_reason` が空/None | `[gemini-preflight] error: preflight failed (no failure reason available; see result JSON)` |
| 常に（上記に加えて） | `[gemini-preflight] result saved to: <output-file のパス>` |

**AI エージェント向け注意**:
- `ok: true` 時は stdout でバージョン情報と状態を即座に確認できるため、JSON ファイルを Read する必要がない。
- `ok: false` 時は stdout の失敗理由を確認し、詳細が必要な場合のみ JSON ファイルを Read する。
- `--compact` を指定した場合でも stdout 出力の内容は変わらない（`version.value` は compact 後も保持されるため）。

### trust 回避戦略と --skip-trust 既定化（Issue #1824）

**なぜ isolated temp cwd で trust が問題になるのか:**

preflight smoke は `tempfile.TemporaryDirectory(prefix="gemini-preflight-")` の isolated temp cwd で実行される。本番委譲（`run_gemini_headless.py`）も `tempfile.TemporaryDirectory(prefix="gemini-headless-")` を既定 cwd として使う。このため、repo root を interactive mode で trust していても一時ディレクトリが未 trusted と判定され `FatalUntrustedWorkspaceError` が発生する。

**3 戦略の比較:**

| 戦略 | セキュリティリスク | メンテナンスコスト | 互換性 | 採用判断 |
|---|---|---|---|---|
| `--skip-trust`（既定化） | Gemini CLI の trust 機構を session 単位で bypass。wrapper が明示的に渡す引数なので範囲は制御可能。 | なし（コード 1 行） | `gemini --help` に `--skip-trust` が存在することを preflight の `required_flags` check で検証する | **採用** |
| `GEMINI_CLI_TRUST_WORKSPACE=true`（旧来手順） | env 変数の副作用が広い（プロセス全体に影響）。Caller が明示しなければ動かない。 | 呼び出し側が毎回環境変数を設定する必要があり、自動化で忘れやすい | 問題なし | **廃止（--skip-trust に置き換え）** |
| `~/.gemini/trustedFolders.json` への追記 | ユーザー HOME の永続設定を変更するため副作用が大きい。temp dir は追記できない。 | worktree ごとにパスが変わるため管理困難。temp dir は常に新規生成で対応不可。 | JSON に wildcard 非対応（exact path のみ） | **採用しない** |

**現行の動作（Issue #1824 適用後）:**

- `--skip-trust` は `preflight_gemini_headless.py` の smoke コマンドと `run_gemini_headless.py` の委譲コマンドに**既定で付与**される。
- `GEMINI_CLI_TRUST_WORKSPACE=true` による手動復旧は不要になった。
- `trustedFolders.json` は exact path matching であり wildcard 非対応のため、temp dir を信頼するには不適。

**machine-readable 分類（failure_class / recovery_action）の保持:**

`--skip-trust` を既定化しても、Gemini CLI のバージョンが `--skip-trust` をサポートしない等の予期しない失敗に備えて、preflight は引き続き stderr を検査し以下を返す:

| フィールド | 値 |
|---|---|
| `failure_class` | `"trusted_workspace_required"` |
| `recovery_action` | `"set GEMINI_CLI_TRUST_WORKSPACE=true and rerun preflight"` |

`failure_class == "trusted_workspace_required"` が返った場合は Gemini CLI のバージョンが `--skip-trust` 未対応の可能性がある。`GEMINI_CLI_TRUST_WORKSPACE=true` でのフォールバック再実行を検討すること。

## `post_to_issue_url` を使ったコメント自動投稿

### 背景

大規模な調査タスク（`grounded_research` で Google Search を駆使した 5000 トークン超の報告書など）では、Gemini の `response_text` 全体をオーケストレーター側のコンテキストに取り込むことで、AI エージェント（ClaudeCode サブエージェント等）のコンテキストウィンドウを消費する。`post_to_issue_url` を指定することで、以下のように責務を分離できる:

| 責務 | 実行者 |
|-----|--------|
| 調査対象・手法を決め、Gemini CLI へ委譲する | オーケストレーター（AI エージェント）|
| 実調査を実行し、結果をフォーマットしてコメント投稿する | Gemini CLI + `gh issue comment` |
| コメント URL のみを受け取り、次アクションを判定する | オーケストレーター |

**効果**: Gemini の詳細報告書（数千トークン）がオーケストレーターのコンテキストに入らず、コメント URL（数十トークン）に置き換わる → 数万トークンの消費削減。

### 使い方

#### 責務フロー（重要）
- **オーケストレーター（AI エージェント）の責務**: リクエスト JSON を作成し、`post_to_issue_url` を指定して wrapper に委譲する。
- **wrapper（`run_gemini_headless.py`）の責務**: Gemini CLI で調査を実行し、`ok: true` かつ `response_text` が存在する場合、自動的に `gh issue comment` で GitHub へ投稿する。投稿結果（成功時は `comment_url`、失敗時は `post_result`）を `result.json` に記載する。
- **オーケストレーターの受け取り**: `result.json` から `result_surface` を読み、`summary` / `primary_artifact` / `next_action` だけを main thread に返す。詳細調査時にだけ `comment_url` や `response_text` を読む。

#### 1. Request JSON に `post_to_issue_url` を指定する

```json
{
  "schema": "delegation_request_v1",
  "objective": "Conduct a comprehensive code review of the ingestion service",
  "instructions": [
    "Review for design issues, performance bottlenecks, and security gaps.",
    "Cite specific line numbers and examples."
  ],
  "tool_profile": "grounded_research",
  "output_sections": [
    "Design Review",
    "Performance Analysis",
    "Security Recommendations"
  ],
  "context_files": [
    "/absolute/path/to/ingestion_service.py",
    "/absolute/path/to/design.md"
  ],
  "timeout_sec": 300,
  "post_to_issue_url": "https://github.com/squne121/KindleAudiobookMakeSystem/issues/1234"
}
```

#### 2. 調査を実行する（wrapper が自動投稿を担当）

```bash
uv run python3 .agents/skills/gemini-cli-headless-delegation/scripts/run_gemini_headless.py \
  --request-file request.json \
  --output-file result.json
```

Wrapper は以下の処理を自動実行する:
- Gemini CLI で調査を実行する
- `ok: true` かつ `response_text` が存在する場合、内部で `gh issue comment` で結果を投稿する
- 投稿成功時は `result.json` に `comment_url` を記載
- 投稿失敗時は `post_result` に失敗理由を記載、`warnings` に詳細を追加

#### 3. オーケストレーターはコメント URL のみを受け取る

```bash
# result.json から artifact-first surface を取得
SUMMARY=$(jq -r '.result_surface.summary' result.json)
PRIMARY_ARTIFACT=$(jq -r '.result_surface.primary_artifact' result.json)
NEXT_ACTION=$(jq -r '.result_surface.next_action' result.json)

# 投稿失敗時の確認（post_result が存在する場合）
POST_RESULT=$(jq -r '.post_result // "skipped"' result.json)
```

オーケストレーターは `SUMMARY` / `PRIMARY_ARTIFACT` / `NEXT_ACTION` をログ / 返答に記録する。`response_text` 全文は detail が必要な場合にだけ読む。

### `result.json` への新フィールド仕様

**Caller による Case の区別方法**:
- **Case 1 か Case 2/3 かの判定**: Caller が自身のリクエストで `post_to_issue_url` を指定したかどうかで判定する。`result.json` には Case を区別するフィールドが存在しない（wrapper が `post_to_issue_url` を result に echo back しないため）。
- **Case 2 と Case 3 の判定**: `result.json` に `comment_url` フィールドが存在するかで判定する。`comment_url` が存在 → Case 3（投稿成功）、存在しない → Case 2 か Case 1。

**フィールドの出現ルール**:

**Case 1: `post_to_issue_url` がリクエストに含まれない場合**
- `post_to_issue_url`, `comment_url`, `post_result` はいずれも **absent**（フィールドが存在しない）

**Case 2: `post_to_issue_url` は指定されているが、投稿がスキップされた場合**
- `ok: false` または `response_text` が空
- `post_to_issue_url`, `comment_url`, `post_result` はいずれも **absent**（result に含まれない）
- Caller は自身が送ったリクエストで指定した `post_to_issue_url` を参照すること

**Case 3: `post_to_issue_url` が指定され、投稿が試行された場合（`ok: true` かつ `response_text` 存在）**

| フィールド | 投稿成功時 | 投稿失敗時 |
|----------|--------|--------|
| `post_to_issue_url` | 存在 | 存在 |
| `comment_url` | 存在（コメント URL）| **absent** |
| `post_result` | `"success"` | 失敗理由（`"failed: <stderr テキスト>"` / `"error: <例外テキスト>"` 形式） |

**具体例**:

投稿成功時の result.json の一部:
```json
{
  "ok": true,
  "response_text": "...",
  "post_to_issue_url": "https://github.com/squne121/KindleAudiobookMakeSystem/issues/1234",
  "comment_url": "https://github.com/squne121/KindleAudiobookMakeSystem/issues/1234#issuecomment-1234567890",
  "post_result": "success"
}
```

投稿失敗時（例: `gh` コマンドがない）の result.json の一部:
```json
{
  "ok": true,
  "response_text": "...",
  "post_to_issue_url": "https://github.com/squne121/KindleAudiobookMakeSystem/issues/1234",
  "post_result": "failed: gh: command not found",
  "warnings": ["Failed to post comment: gh command not found"]
}
```

投稿スキップ時（`ok: false`）の result.json の一部:
```json
{
  "ok": false,
  "warnings": ["Gemini CLI exited with code 1"]
}
```

### 投稿スキップ・失敗時の動作

`post_to_issue_url` が指定されていても、以下の場合は投稿が試行されない（スキップ）:

- `ok: false`（調査自体が失敗）
- `response_text` が空文字列

投稿が試行されても失敗する場合（試行中のエラー）:

- `gh` コマンドが環境に存在しない
- GitHub の認証に失敗した（`gh auth status` の失敗）
- Issue/PR URL が無効
- GitHub API のエラー（HTTP エラーなど）

失敗時は `result.json` の `warnings` に失敗理由が追加される。`ok` は変わらない（調査自体は成功）。

### オーケストレーター向けパターン例（ClaudeCode SubAgent）

```python
# step 1: 調査を委譲する
result = run_delegation(request, post_to_issue_url="https://...")
if not result["ok"]:
    return handle_error(result["warnings"])

# step 2: artifact-first surface だけを記録
surface = result["result_surface"]
log.info("Investigation complete. Summary: %s", surface["summary"])
log.info("Primary artifact: %s", surface["primary_artifact"])

# step 3: comment_url をユーザーへ返す
return {
    "status": "complete",
    "summary": surface["summary"],
    "primary_artifact": surface["primary_artifact"],
    "next_action": surface["next_action"],
}
```

## gh_commands general field 仕様

`gh_commands` は **general optional field** であり、wrapper が Gemini への委譲前に argv ベースで read-only コマンドを事前実行し、結果を `inline_context` に prepend する。`no_tools` / `grounded_research` を除く profile で利用可能（profile ごとのサポート状況は下記 allowlist 設計テーブル参照）。

### フィールド形式

```json
{
  "gh_commands": [
    {"argv": ["issue", "view", "2232", "--json", "title,state"]},
    {"argv": ["pr", "list", "--state", "open", "--json", "number,title"]}
  ]
}
```

- `argv` は `gh` コマンドの引数（`gh` 自体を除く）を配列で指定する。
- wrapper は argv allowlist で検証後、`gh` を subprocess で実行し、stdout を `inline_context` に prepend する。
- `gh_commands` 未指定時は wrapper の事前実行は行わない。

### profile ごとの allowlist 設計

| profile | サポート状況 | 許可 allowlist | 備考 |
|---|---|---|---|
| `github_research` | **完全実装済み**（PR #2238） | `gh issue list/view`、`gh pr list/view/diff`、`gh search issues/prs`、`gh label list`、`gh repo view`、`gh api`（GET のみ） | write コマンド・`gh api` 非 GET method・`post_to_issue_url` は禁止。詳細は下記「github_research」節の「`gh_commands` field 仕様」参照 |
| `local_asset_research` | **完全実装済み**（PR #2309） | `github_research` 同等の read-only `gh` コマンド（ローカル資産調査時に Issue/PR 参照が必要な場合） | allowlist 外 argv は `warnings` に記録して skip（`github_research` と異なり validation error にしない）。`post_to_issue_url` 禁止（既存制約を維持）。 |
| `proposal_only` | **完全実装済み**（PR #2309） | `github_research` 同等の read-only `gh` コマンド（提案下書きの文脈収集として使用） | allowlist 外 argv は `warnings` に記録して skip。`post_to_issue_url` 禁止・file edit/write 禁止（既存制約を維持）。 |
| `no_tools` | 対応しない | — | 文脈収集ニーズは `context_files` / `inline_context` で代替 |
| `grounded_research` | 対応しない | — | isolated temp cwd かつ `gh` 認証状態が不確かなため実装困難。Google Search grounding が主要手段であり `gh_commands` の必要性も低い |

**実装ロードマップ注記**:
- `local_asset_research` と `proposal_only` の `gh_commands` 対応は Issue #2255 で仕様設計を確定し、Issue #2309 で完全実装した（PR #2309）。
- `local_asset_research` / `proposal_only` の request に `gh_commands` を指定した場合、wrapper は argv を allowlist 検証し、許可されたコマンドを事前実行して結果を `inline_context` に prepend する。allowlist 外の argv は `warnings` に記録してスキップする（`github_research` と異なり validation error にして fail-close しない）。フォーマット不正な entry（dict でない / argv が文字列リストでない）も同様に `warnings` に記録してスキップする。

## `transport` Field (ACP Transport — experimental)

The optional `transport` field selects the delegation transport:

| Value | Behavior |
|---|---|
| absent or `"headless_json"` | Default. Standard `gemini --output-format json` pathway. |
| `"acp"` | **Experimental** ACP (Agent Client Protocol) transport via `gemini --acp`. JSON-RPC lifecycle with structured events. |

`transport: acp` requests are validated and prompt-built by the **same
delegation contract** as headless_json: `run_delegation()` runs
`validate_request()`, model chain resolution, context loading, and
`build_prompt()` *before* dispatching to the ACP session. An invalid
`delegation_request_v1` fails at validation and never reaches the ACP path.

At `initialize` the ACP transport declares `clientCapabilities` with
`fs.readTextFile: false`, `fs.writeTextFile: false`, `terminal: false`. This
means only that **this ACP client provides no client-side fs/terminal proxy** —
it does **not** disable Gemini CLI's own native tool registry, `cwd`-resolved
MCP servers, or `approvalMode` (currently sent as `"default"`, so tools are
active). The end-to-end safety-boundary design for ACP delegation is deferred to
**follow-up #112**; real Gemini CLI runtime verification evidence is deferred to
**follow-up #113**. See `references/transport-acp.md` "Capability scope" and
"Known limitations / non-goals".

`transport: acp` results are normalized by `run_delegation()` into the standard
`delegation_result/v1` shape (`result_surface`, `requested_model`,
`actual_model`, `exit_code`, `model_chain`, etc.); ACP-specific detail is kept
under a `transport_details` object (`schema: "acp_result_v1"`,
`structured_events`, `failure_class`, `stop_reason`). Fallback results
(`_acp_fallback: true`) are already `delegation_result/v1` and pass through
unchanged.

### `transport: acp` — additional fields

| フィールド | 型 | 説明 |
|---|---|---|
| `transport` | `"acp"` | Select ACP transport. |
| `approve_edits` | boolean (optional, default `false`) | Legacy flag passed to the **best-effort** ACP permission handler. The `session/request_permission` policy is driven by `tool_profile` + ACP `toolCall.kind`: `no_tools` rejects every kind; read-class profiles allow `read`/`search`/`fetch`/`think` and reject `edit`/`delete`/`move`/`execute`/`other`. `approve_edits=True` does **not** widen this — no `tool_profile` in this skill is write-capable. This is defence in depth only — it does not gate Gemini CLI's native tools / MCP. The end-to-end safety boundary is deferred to #112. |

> `gemini_bin` は request JSON フィールドからは読まない。カスタムバイナリの指定は `GEMINI_BIN` 環境変数のみで行う。

### ACP result fields (when `transport: acp`)

`transport: acp` results are returned as `delegation_result/v1` (same core
fields as headless_json). The following are ACP-specific:

| フィールド | 型 | 説明 |
|---|---|---|
| `schema` | `"delegation_result/v1"` | Normalized result schema (same as headless_json). |
| `transport` | `"acp"` | Confirms ACP transport was used. |
| `transport_details` | object | ACP-specific detail: `schema` (`"acp_result_v1"`), `structured_events` (list of `session/update` events — snake_case `agent_message_chunk` / `agent_thought_chunk` / `tool_call` / `tool_call_update` — and `session/request_permission` entries), `failure_class`, `stop_reason`. |
| `transport_details.failure_class` | string \| null | Structured failure classifier: `gemini_not_found`, `launch_failed`, `initialize_failed`, `session_new_failed`, `auth_required`, `prompt_error`, `protocol_error`, `timeout`, `watchdog`, `incomplete_response`, `contract_bypass`, or `null` on success. Drives fallback selection. `auth_required` (the Gemini CLI / OAuth session is not pre-authenticated; the ACP `authenticate` handshake is not implemented) is **excluded** from the headless_json fallback set so the auth failure is surfaced honestly rather than masked behind a fallback success. |
| `transport_details.stop_reason` | string \| null | The `stopReason` from the final `session/prompt` response. `ok: true` requires `stop_reason == "end_turn"` and a non-empty `response_text`. |
| `_acp_fallback` | boolean (optional) | Present and `true` when fallback to headless_json occurred. In that case the result keeps the `headless_json` shape and is **not** re-normalized. The verification script `verify_acp_roundtrip.sh` SKIPs with exit 77 when `gemini`/`jq` are absent. |

### Example request with `transport: acp`

```json
{
  "schema": "delegation_request_v1",
  "transport": "acp",
  "objective": "Summarize the architecture of this project",
  "instructions": [
    "Focus on the key components and their relationships.",
    "Keep the response under 300 words."
  ],
  "tool_profile": "no_tools",
  "output_sections": ["Summary"],
  "context_files": ["/absolute/path/to/README.md"],
  "model": "gemini-2.5-flash",
  "timeout_sec": 300
}
```

### ACP transport lifecycle

See `references/transport-acp.md` for full lifecycle, timeout design, permission proxy, and fallback documentation.

## Failure Modes
- `invalid_request`
- `missing_context_file`
- `json_parse_failed`
- `gemini_non_zero_exit`
- `model_capacity_exhausted`
- `timeout`
- `post_to_issue_failed`（`post_to_issue_url` 指定時の投稿失敗）
- `github_research_command_denied`（`github_research` profile で非許可コマンドを検知）
- `gh_auth_required`（`gh` が未インストール、または `gh auth status` が失敗）

### github_research

`github_research` profile は GitHub read-only 調査専用。wrapper が許可コマンドを `gh` で実行し、結果を `inline_context` に prepend して Gemini に渡す。

#### 許可コマンド一覧

| コマンド | 説明 |
|---|---|
| `gh issue list` | Issue リスト取得 |
| `gh issue view` | Issue 詳細表示 |
| `gh pr list` | PR リスト取得 |
| `gh pr view` | PR 詳細表示 |
| `gh pr diff` | PR diff 表示 |
| `gh search issues` | Issue 検索 |
| `gh search prs` | PR 検索 |
| `gh label list` | ラベル一覧取得 |
| `gh repo view` | リポジトリ情報取得 |
| `gh api` (GET only) | API エンドポイント GET（`--method GET` または method 未指定が既定）|

#### 拒否例（fail-close 条件）

以下のコマンドを含む request は `failure_class: "github_research_command_denied"` で即時 reject される:

- `gh issue comment` / `gh issue edit` / `gh issue create` / `gh issue close` / `gh issue reopen` / `gh issue delete`
- `gh pr create` / `gh pr edit` / `gh pr comment` / `gh pr merge` / `gh pr close` / `gh pr reopen` / `gh pr review` / `gh pr ready` / `gh pr checkout`
- `gh label create` / `gh label edit` / `gh label delete` / `gh label clone`
- `gh release create` / `gh release edit` / `gh release delete` / `gh release upload`
- `gh repo create` / `gh repo edit` / `gh repo delete` / `gh repo fork` / `gh repo clone` / `gh repo sync` / `gh repo archive` / `gh repo rename`
- `gh secret` / `gh variable`
- `gh workflow run` / `gh run cancel`
- `gh api ... -X POST` / `gh api ... -X PATCH` / `gh api ... -X PUT` / `gh api ... -X DELETE`
- `gh api ... --method POST` / `--method PATCH` / `--method PUT` / `--method DELETE`
- `gh auth login` / `gh auth logout`
- `post_to_issue_url` フィールドの指定

#### `gh_commands` field 仕様

> `gh_commands` は general optional field です。全 profile 対応の仕様設計（profile ごとの allowlist 設計・実装ロードマップ）は「## gh_commands general field 仕様」セクションを参照してください。ここでは `github_research` profile での具体的な実装仕様を記載します。

request に `gh_commands` field を追加することで、wrapper が argv ベースで厳密に検証・実行する:

```json
{
  "schema": "delegation_request_v1",
  "tool_profile": "github_research",
  "gh_commands": [
    {"argv": ["issue", "view", "2232", "--json", "title,state"]},
    {"argv": ["api", "repos/owner/repo/issues/2232"]}
  ],
  "objective": "Issue #2232 のタイトルとステートを確認する",
  "instructions": [
    "gh コマンドの実行結果を確認し、Issue のタイトルとステートを報告してください。",
    "フォーマットは JSON で出力してください。"
  ],
  "output_sections": ["IssueInfo"],
  "context_files": ["/path/to/README.md"]
}
```

- `argv` は `gh` コマンドの引数（`gh` 自体を除く）を配列で指定する。
- wrapper は argv allowlist で検証後、`gh` を subprocess で実行し、stdout を `inline_context` に prepend する。
- `gh_commands` 未指定時は text-based secondary defense のみ（objective/instructions に許可コマンドが 1 つも見つからない場合は reject）。

#### preflight 必須条件

`github_research` を使う前に preflight の `gh_cli` セクションを確認すること:
- `gh_cli.ok: true`: `gh --version` と `gh auth status` が成功している
- `gh_cli.ok: false` + `failure_class: "gh_auth_required"`: `gh auth login` で認証が必要
