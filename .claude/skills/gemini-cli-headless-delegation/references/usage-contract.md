# Usage Contract

## Request Contract
`delegation_request_v1` は provider-aware request contract として扱う。`provider` 省略時は `gemini` とみなし、
`provider=agy` のときだけ prompt-first variant を許可する。

### `provider=gemini` request / Gemini 用 request 定義

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
  "timeout_sec": 600,
  "post_to_issue_url": "https://github.com/owner/repo/issues/123"
}
```

### Required Fields / 必須項目

> **すべて必須。1 つでも欠けるか値が不正なら `ok: false` になる。**

| フィールド | 制約 |
|---|---|
| `schema` | `"delegation_request_v1"` 固定。省略・別値は即拒否。 |
| `objective` | 具体的な目標。曖昧な動詞のみは拒否。 |
| `instructions` | 2 件以上のリスト。 |
| `tool_profile` | **`"no_tools"` / `"grounded_research"` / `"local_asset_research"` / `"proposal_only"` / `"github_research"` のいずれか**。`"code_analysis"` 等の別値は拒否される。 |
| `output_sections` | 1 件以上のリスト。 |
| `context_files` | 1 件以上。パス解決に関する注意は下記参照。 |

### `provider=agy` request variant / agy 用 request 変種定義

`provider=agy` は Gemini の envelope contract をそのまま要求しない。`delegation_request_v1` の provider-aware extension として、
次の制約で受理する。

```json
{
  "schema": "delegation_request_v1",
  "provider": "agy",
  "tool_profile": "no_tools",
  "prompt": "Return exactly: LOOP_AGY_SMOKE_OK"
}
```

| フィールド | 制約 |
|---|---|
| `schema` | `"delegation_request_v1"` 固定。 |
| `provider` | `"agy"` 固定。 |
| `tool_profile` | `"no_tools"`、`"proposal_only"`、`"local_asset_research"`、または `"grounded_research"`。 |
| `prompt` | 必須。空文字・空白のみは `agy_empty_prompt` で拒否。 |
| `context_files` | `local_asset_research` 時は必須。repo 境界とシンボリックリンク境界検証後に wrapper が repo-relative JSON evidence envelope を集約し、AGY へ prompt 注入する。 |
| `model` | 指定禁止。`unsupported_provider_option` で拒否。 |
| `post_to_issue_url` | 指定禁止。`provider_forbids_post_to_issue_url` で拒否。 |
| `grounded_research` | `agy` ネイティブの WebSearch/WebGrounding（`agy -p` 実行）を使用。 Gemini API `google_search` tool や Google Search grounding API は呼ばない（Gemini provider の `grounded_research` とは別の provider-specific 実装）。 |
| `github_research` | 使用禁止。`unsupported_provider_profile` で拒否。 |

`provider=agy + grounded_research` は `implemented_agy_native_websearch_grounding` として扱う。
`wrapper_side_google_search_grounding: forbidden` であり、wrapper は Gemini API Google Search / Google Search grounding API / wrapper-side Web retrieval を呼ばない。
`raw_transcript_included: false`、`raw_credential_included: false`、`repo_absolute_path_included: false` を evidence envelope の不変条件とする。
`redaction_status` は正常時 `checked_no_secret_pattern` を返す。secret-like pattern / repo absolute path / HOME path を実際に runtime scan した結果であり（自己申告の固定値ではない）、検出時は `agy_web_grounding_redaction_failed` で fail-closed する。
stdout に URL 文字列があるだけでは WebSearch 実行証跡として扱わない。machine-verifiable な構造化 `tool_calls` トレース（`web_search` / `browser_navigate` / `url_read` 等の認識済み tool 名を含む）が無い場合は `grounding_status: attempted_no_web_tool_call` / `grounding_backend: none` / `grounding_failure_class: agy_web_grounding_tool_call_missing` として fail-closed する。`web_tool_call_count` は URL 件数から推定しない。
quota exceeded（`RESOURCE_EXHAUSTED` / HTTP 429 / `quota_exhausted` / `Individual quota reached`）は `agy_web_grounding_quota_exhausted`（preflight smoke では `agy_grounded_research_quota_exhausted`）として blocked にし、1 query / 1 URL / timeout / no retry storm を守る。

`objective` / `instructions` / `output_sections` / `context_files` は、既存 caller 互換のため指定されていてもよいが、
`provider=agy` 実行時の primary contract は、`prompt` / `tool_profile` に加え、`context_files` の wrapper-side 検証結果（repo-boundary / drift）を含めて扱う。

### Optional Fields（拡張） / 任意項目

| フィールド | 型 | 説明 |
|---|---|---|
| `role` | string（任意） | quota 枯渇時の降格チェーン選択に使用する。有効値は `roles` マップのキー（例: `web_research` / `implementation` 等）。`tool_profile` とは独立した概念であり、同時指定可能。 |
| `gh_commands` | array（任意） | `[{"argv": [...]}]` 形式の argv ベースコマンドリスト。wrapper が事前実行し結果を `inline_context` に prepend する general field。**`tool_profile=github_research` でのみ許可される**。それ以外の profile で指定すると `validate_request()` が `"gh_commands is only allowed with tool_profile='github_research'"` で fail-closed する（詳細は「gh_commands general field 仕様」セクション参照）。 |

### `tool_profile` の責務境界

| Profile | 入口 | 許可される外部/ローカル能力 | 禁止事項 |
|---|---|---|---|
| `no_tools` | isolated temp cwd | `context_files` と `inline_context` のみ | tools、repo 探索、shell execution、file edit/write |
| `grounded_research` | isolated temp cwd | gemini: Google Search grounding（Gemini API tool）。agy: AGY ネイティブ WebSearch/WebGrounding（`agy -p` 実行、Gemini API 不使用）| shell execution、file edit/write、repo 探索、Serena MCP |
| `local_asset_research` | repo root | Serena MCP の read-only tool による WSL-local ローカル資産調査 | Google Search、shell execution、file edit/write、GitHub write、repo 外の任意読み取り、`post_to_issue_url` |
| `proposal_only` | isolated temp cwd | bounded draft text (`implementation_draft` / `issue_authoring_draft` / `patch_proposal` / `command_plan`) | file edit/write、shell execution、GitHub write / `post_to_issue_url`、repo 探索、実装完了を装う報告 |
| `github_research` | repo root | wrapper が許可コマンドを `gh` で実行し結果を `inline_context` に prepend。Gemini は結果を解釈して報告を返す | `post_to_issue_url`、gh write コマンド（issue comment/edit/create/close 等）、`gh api` 非 GET method、shell 実行、file edit/write |

`local_asset_research` は `no_tools` と違い Serena contract を wrapper 側で検証し、repo 内の read-only ローカル資産調査だけを扱う。AGY 本体には repo root、MCP 設定、direct tool access、absolute path を渡さない。`grounded_research` と違い外部 Web grounding は使わない。

`proposal_only` は `no_tools` と同じ isolated temp cwd で動くが、目的は調査結果そのものではなく「Codex 側 worker が採用・修正・実行できる下書き」を返すことにある。最終 file edit / shell 実行 / GitHub mutation は Codex 側に残し、Gemini 側には proposal text だけを持たせる。

`local_asset_research` の `context_files` は、絶対パス・相対パスのどちらでも `Path.resolve()` 後の symlink 解決済みパスが repo root 配下にある場合だけ許可される。repo 外へ解決される絶対パス、`../` 参照、symlink は `failure_reason` / `warnings` に理由を残して fail-closed する。境界検証に失敗した場合、wrapper は payload bounds や prompt build のための `stat()` / `read_text()` に進まない。

`local_asset_research` は `.agents/mcp_config.json`、互換確認用の `.gemini/settings.json`、checked-in `.claude/skills/gemini-cli-headless-delegation/references/serena-tool-manifest.json` の以下を wrapper が machine-checkable に確認できる場合だけ実行する。AGY 用の MCP 起動設定の正本は `.agents/mcp_config.json` であり、AGY 本体へこの config を渡すのではなく wrapper 側の SerenaMCP stdio probe/retrieval だけが使用する:

- `.gemini/settings.json` の `mcp.allowed` が `["serena"]` である。
- `.agents/mcp_config.json` と `.gemini/settings.json` の `mcpServers.serena.command` が `uvx` で、`args` に `serena`、`--project-from-cwd`、pin 済みの `git+https://github.com/oraios/serena@<commit>` または `serena==<version>` が含まれる。
- `.agents/mcp_config.json` と `.gemini/settings.json` の `mcpServers.serena.trust` が `false` である。
- `.agents/mcp_config.json` と `.gemini/settings.json` の `mcpServers.serena.includeTools` が `find_file` / `find_referencing_symbols` / `find_symbol` / `get_symbols_overview` / `list_dir` / `search_for_pattern` のみである。
- `execute_shell_command`、`write_file`、`read_file`、`read_file_content`、`replace_content`、`replace_in_files`、`rename_symbol`、`safe_delete_symbol`、`read_memory`、`write_memory`、`delete_memory`、`edit_memory` などの危険 tool は `excludeTools` で denylist されている。
- `includeTools` / `excludeTools` / pinned ref は manifest と exact/superset 照合し、unknown tool または drift は fail-closed する。
- `--live-serena` preflight は `.agents/mcp_config.json` の pinned command から SerenaMCP stdio server を起動し、`initialize`、`tools/list`、`find_file`、`search_for_pattern`、`get_symbols_overview` の transcript と evidence count を返す。

AGY prompt に渡す local asset context は、以下を持つ JSON evidence envelope に限定する。

- `tool_name`: wrapper 側が実行した Serena read-only tool 名。
- `query`: 取得対象を示す query または selector。
- `repo_relative_path`: repo root からの相対パス。
- `line_range`: evidence の行範囲。
- `content_snippet`: AGY へ渡す bounded snippet。
- `byte_size`: snippet の byte 数。
- `sha256`: snippet 内容の hash。
- `redaction_status`: credential-like payload 検査の状態。
- `manifest_id`: Serena manifest schema/ref の照合元。
- `source_kind`: live SerenaMCP stdio retrieval のみ `serena_mcp_read_only_evidence`。test double / direct context fallback は `serena_mcp_test_double_evidence` または `manual_context_file_evidence` として扱い、SerenaMCP retrieval success evidence と混同しない。

上記が未検証 MCP 設定、危険 tool、または Windows wrapper / repo 外読み取りを含む場合、wrapper は fail-closed として `ok: false`、`failure_reason`、`warnings` を返す。曖昧に `grounded_research` へ流用してはならない。

Gemini CLI の認証は OAuth / Google アカウント認証を前提にする。headless 実行前に interactive login 済みの cached credential があること、trusted workspace が成立していること、`.env` がこの前提と矛盾する API key / Vertex ADC 前提へ切り替えていないこと、project-scoped `.gemini/settings.json` が Serena MCP 設定として有効であることを Stop Conditions として扱う。

### `provider=agy` の認証前提（OAuth 系 / Google Sign-In）

agy (Antigravity CLI) の認証も OAuth 系（Google Sign-In によるインタラクティブ browser-based ログイン）を前提にする。
Gemini CLI とは別の認証状態を持つため、次を Stop Conditions として扱う:

- `agy` の interactive auth login（Google Sign-In）が事前に完了していること。未完了の場合、non-TTY / pipe / CI 実行では `agy -p` が silent に stdout を drop し得るため、`preflight_agy.py` は `noninteractive_auth_prompt_required` として fail-closed する。
- system keyring / desktop session / dbus / runtime dir に依存する認証状態が不確かな場合、`_minimal_agy_env()` の allowlist env では認証情報にアクセスできず fail-closed し得る。
- API key（`GEMINI_API_KEY` 等）は `provider=agy` の認証前提には使わない。API key 経路は `provider=gemini` の暫定回避専用であり、`provider=agy` には継承しない。

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
- `model`: 使用モデル。省略時は `references/model-routing.md` の `DEFAULT_MODEL_ROUTING.default_chain[0]` を使用する（具体的なモデル名は model-routing.md が正本）。明示指定時はそのモデルのみで試行し、quota 枯渇でも降格しない。
- `timeout_sec`: タイムアウト時間（秒）。省略時は wrapper の既定値。
- `post_to_issue_url`: GitHub Issue URL only（`https://github.com/<owner>/<repo>/issues/<number>` 形式。`/pulls/<number>` は許可しない）。指定時は調査結果を自動的に `gh issue comment` で投稿する（詳細は後述の「`post_to_issue_url` を使ったコメント自動投稿」参照）。
- `gh_commands`: `[{"argv": [...]}]` 形式の argv ベースコマンドリスト（general field）。詳細は「gh_commands general field 仕様」セクション参照。

## Request Rejection Rules / 拒否ルール
- 以下は request validation で即時 reject する fail-closed 条件である。日本語要約として、曖昧 request や許可外 profile はここで止める。
- `objective` is only vague verbs or filler words
  - Exception: objectives containing paths, filenames, or line numbers are accepted regardless of language（言語問わず、パス・ファイル名・行番号を含む objective は受理される）
- `context_files` is missing, empty, or any referenced file is absent
- `instructions` has fewer than 2 entries
- `output_sections` is empty
- `tool_profile` is not explicit
- `schema` is not `delegation_request_v1`
- `tool_profile=local_asset_research` and `post_to_issue_url` is present
- `tool_profile=local_asset_research` and any `context_files` entry resolves outside the repository root
- `tool_profile=local_asset_research` and local asset Serena contract validation fails（unknown / drift / boundaries / payload bounds）
- `tool_profile=proposal_only` and `post_to_issue_url` is present
- `tool_profile=proposal_only` and the request instructs direct file edits, shell execution, or GitHub mutation instead of proposal text

## Result Contract / 結果契約
`delegation_result/v1` には次の core field と条件付き field を含める。

### Core Fields（常に存在）
- `ok`（boolean）: 調査の成功/失敗
- `requested_model`（string）: リクエストで指定したモデル
- `actual_model`（string）: 実際に使用したモデル（`"unknown"` の場合あり）
- `model_chain`（list[str]）: 試行対象だった model のリスト（chain 全体）。常に存在。
- `model_downgrades`（list[{from, to, reason}]）: 降格イベントのリスト。降格なし時は `[]`。常に存在。
- `tool_profile`（string）: `"no_tools"` / `"grounded_research"` / `"local_asset_research"` / `"proposal_only"` / `"github_research"` のいずれか
- `exit_code`（integer）: Gemini CLI の終了コード
- `result_surface`（object）: artifact-first / summary-first の薄い返却面。caller はまずここを見る
- `response_text`（string）: Gemini の調査結果（`ok: true` かつ投稿なし時、または投稿失敗時）
- `stats`（object）: 実行統計（`--compact` 未指定時）
- `stderr`（string）: Gemini CLI の stderr 出力
- `warnings`（array[string]）: 警告・エラーメッセージ
- `raw_command`（string）: 実行した完全な Gemini CLI コマンド（`--compact` 未指定時）
- `schema`（string）: `"delegation_result/v1"` 固定
- `reason_code`（string、エラー時のみ）: fail-closed 理由コード。値: `model_chain_exhausted` / `unknown_role` / `empty_chain` / `routing_config_invalid`

### `post_to_issue_url` 関連フィールド（条件付き）
`post_to_issue_url` がリクエストに含まれ、投稿が試行された場合のみ以下が追加される:

| フィールド | 型 | 必須条件 | 説明 |
|----------|--|---------|------|
| `post_to_issue_url` | string | `post_to_issue_url` がリクエストで指定された場合 | 投稿先 GitHub Issue URL only（リクエストから転記。`/pulls/<number>` は validation で拒否済みのため含まれない） |
| `comment_url` | string &#124; null | 投稿成功時のみ存在 | 投稿されたコメントの URL。失敗時は absent（フィールドが存在しない） |
| `post_result` | string | 投稿試行時のみ存在 | `"success"` または失敗理由（`"failed: <stderr テキスト>"` / `"error: <例外テキスト>"` 形式） |

### `provider=auto` 関連フィールド（条件付き）

request の `provider` が `"auto"` の場合のみ、`run_gemini_headless.py` の `provider_auto_dispatch()`
（`provider_auto_policy_v1`。詳細は `references/provider-mapping.md` の「runtime `provider=auto`」節）が
以下のフィールドを結果に追加する。`provider` が `"gemini"` / `"agy"` の明示指定時はこれらのフィールドは付与されない。

| フィールド | 型 | 必須条件 | 説明 |
|----------|--|---------|------|
| `selected_provider` | string &#124; null | `provider="auto"` の場合のみ存在 | 最終的に採用した provider 名（`"gemini"` / `"agy"`）。`stop_if` 条件により provider 試行自体が行われなかった場合は `null` |
| `provider_attempts` | array&#91;object&#93; | `provider="auto"` の場合のみ存在 | 試行した各 provider の結果を記録した監査用 list。試行がなければ空配列 |
| `fallback_reason` | string &#124; null | `provider="auto"` の場合のみ存在 | fallback が発生した理由、または `stop_if` による即時停止理由（例: `"stop_if:provider_profile_unsupported"`）。fallback が発生しなかった場合は `null` |
| `fallback_policy_version` | string | `provider="auto"` の場合のみ存在 | 適用した provider fallback ポリシーの version（現状 `"v1"` 固定） |
| `attempts_by_model` | object | `provider="auto"` の場合のみ存在 | `provider_attempts[]` 内の各 provider が実際に試行した `{model_id: attempt_count}` を集計した実測値の map |

`provider="auto"` の `eligible_profiles` は `no_tools` / `proposal_only` のみで、それ以外の `tool_profile` を指定した場合は
provider 試行自体を行わず `provider_profile_unsupported`（`fallback_reason: "stop_if:provider_profile_unsupported"`）で即時 fail-closed する。

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
- `provider=gemini` の `response_text` は Gemini envelope `response` field から抽出する。
- `provider=agy` の `response_text` は plain stdout text をそのまま使い、Gemini JSON envelope parse は行わない。
- `result_surface` is derived from `response_text` and, when available, `comment_url`.
- `actual_model` is taken from `stats.models`; otherwise `unknown`.
- stderr is preserved as a warning channel, not discarded.
- `provider=gemini` の `ok` は Gemini exit code と envelope parse result に依存する。
- `provider=agy` の result は `transport: "agy"`, `provider: "agy"`, `safety_mode: "degraded_wrapper_only"`, `actual_model: "agy-default"` を含む。
- `provider=agy` の `failure_class` / `failure_reason` は machine-readable enum を揃える。empty stdout は非 CI で `agy_empty_stdout`、CI では `agy_output_missing`。
- 明示 `model` 指定時は降格なし（そのモデルのみで試行）。`role` ベースの `model_chain` による quota 枯渇時の自動降格は `references/model-routing.md` が正本。
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

## CLI Usage / CLI 利用方法

### `run_gemini_headless.py`

```
run_gemini_headless.py --request-file <path> --output-file <path> [--compact] [--output-format {json,ndjson}]
```

| オプション | 説明 |
|---|---|
| `--request-file` | `delegation_request_v1` JSON ファイルのパス（必須）。 |
| `--output-file` | `delegation_result/v1` JSON を書き出すパス（必須）。 |
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
  - `gemini --model <DEFAULT_MODEL> --approval-mode plan --skip-trust --prompt 'Do not use any tools. Reply with OK only.' --output-format json`
  - `<DEFAULT_MODEL>` は `references/model-routing.md` の `DEFAULT_MODEL_ROUTING.default_chain[0]` を参照する

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
uv run python3 .claude/skills/gemini-cli-headless-delegation/scripts/run_gemini_headless.py \
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
- GitHub Issue URL が無効（validation で reject 済みだが、投稿試行時に URL が指す Issue 自体が既に削除・アクセス不可の場合を含む）
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

`gh_commands` は **general optional field** であり、wrapper が Gemini への委譲前に argv ベースで read-only コマンドを事前実行し、結果を `inline_context` に prepend する。**runtime（`run_gemini_headless.py` の B3 検証）は `tool_profile="github_research"` の request でのみ `gh_commands` を許可し、それ以外の profile で指定した場合は fail-closed する**（`"gh_commands is only allowed with tool_profile='github_research'"`）。

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
| `github_research` | **完全実装済み** | `gh issue list/view`、`gh pr list/view/diff`、`gh search issues/prs`、`gh label list`、`gh repo view`、`gh api`（GET のみ） | write コマンド・`gh api` 非 GET method・`post_to_issue_url` は禁止。詳細は下記「github_research」節の「`gh_commands` field 仕様」参照 |
| `local_asset_research` | **非対応**（fail-closed） | — | `gh_commands` を指定すると `validate_request()` が `"gh_commands is only allowed with tool_profile='github_research'"` で reject する |
| `proposal_only` | **非対応**（fail-closed） | — | `gh_commands` を指定すると `validate_request()` が `"gh_commands is only allowed with tool_profile='github_research'"` で reject する |
| `no_tools` | 非対応（fail-closed） | — | 文脈収集ニーズは `context_files` / `inline_context` で代替 |
| `grounded_research` | 非対応（fail-closed） | — | isolated temp cwd かつ `gh` 認証状態が不確かなため実装困難。Google Search grounding が主要手段であり `gh_commands` の必要性も低い |

**現状の実装注記**:
- `gh_commands` は `tool_profile="github_research"` の request でのみ受理される general field である。`local_asset_research` / `proposal_only` を含むそれ以外の profile で `gh_commands` を指定した場合、wrapper は該当 profile 向けの他の validation より前に `gh_commands` 由来の validation error で request 全体を fail-closed する（B3 検証。allowlist 検証や `warnings` へのスキップ扱いは行わない）。
- `local_asset_research` / `proposal_only` で GitHub 上の Issue/PR 参照が必要な場合は、`gh_commands` ではなく `context_files` / `inline_context` で事前に取得した内容を渡す必要がある。

## `transport` Field (ACP Transport — experimental) / ACP 輸送指定の境界

The optional `transport` field selects the delegation transport.
この節では headless_json と ACP transport の切替境界を説明する。

| Value | Behavior |
|---|---|
| absent or `"headless_json"` | Default. Standard `gemini --output-format json` pathway. |
| `"acp"` | **Experimental** ACP (Agent Client Protocol) transport via `gemini --acp`. JSON-RPC lifecycle with structured events. |

`transport: acp` requests are validated and prompt-built by the **same
delegation contract** as headless_json: `run_delegation()` runs
`validate_request()`, model chain resolution, context loading, and
`build_prompt()` *before* dispatching to the ACP session. An invalid
`delegation_request_v1` fails at validation and never reaches the ACP path.
日本語要約: ACP を選んでも request validation と prompt 構築は通常経路と同じで、無効 request は ACP 実行前に止まる。

At `initialize` the ACP transport declares `clientCapabilities` with
`fs.readTextFile: false`, `fs.writeTextFile: false`, `terminal: false`. This
means only that **this ACP client provides no client-side fs/terminal proxy** —
it does **not** disable Gemini CLI's own native tool registry, `cwd`-resolved
MCP servers, or `approvalMode` (currently sent as `"default"`, so tools are
active). The end-to-end safety-boundary design for ACP delegation is deferred to
**follow-up #112**; real Gemini CLI runtime verification evidence is deferred to
**follow-up #113**. See `references/transport-acp.md` "Capability scope" and
"Known limitations / non-goals".
日本語要約: ここで false にしているのは client 側 proxy だけで、Gemini CLI 自身の native tool や MCP を無効化する意味ではない。したがって安全境界の正本は follow-up 側に残る。

`transport: acp` results are normalized by `run_delegation()` into the standard
`delegation_result/v1` shape (`result_surface`, `requested_model`,
`actual_model`, `exit_code`, `model_chain`, etc.); ACP-specific detail is kept
under a `transport_details` object (`schema: "acp_result_v1"`,
`structured_events`, `failure_class`, `stop_reason`). Fallback results
(`_acp_fallback: true`) are already `delegation_result/v1` and pass through
unchanged.
日本語要約: ACP 固有詳細は `transport_details` に隔離し、caller には通常の `delegation_result/v1` 面を維持する。

### `transport: acp` — additional fields / ACP 追加項目

| フィールド | 型 | 説明 |
|---|---|---|
| `transport` | `"acp"` | Select ACP transport. |
| `approve_edits` | boolean (optional, default `false`) | Legacy flag passed to the **best-effort** ACP permission handler. The `session/request_permission` policy is driven by `tool_profile` + ACP `toolCall.kind`: `no_tools` rejects every kind; read-class profiles allow `read`/`search`/`fetch`/`think` and reject `edit`/`delete`/`move`/`execute`/`other`. `approve_edits=True` does **not** widen this — no `tool_profile` in this skill is write-capable. This is defence in depth only — it does not gate Gemini CLI's native tools / MCP. The end-to-end safety boundary is deferred to #112. |

> `gemini_bin` は request JSON フィールドからは読まない。カスタムバイナリの指定は `GEMINI_BIN` 環境変数のみで行う。

### ACP result fields (when `transport: acp`) / ACP 結果項目

`transport: acp` results are returned as `delegation_result/v1` (same core
fields as headless_json). The following are ACP-specific:
日本語要約: caller は通常の result contract を読み、必要なときだけ ACP 専用 field を参照する。

| フィールド | 型 | 説明 |
|---|---|---|
| `schema` | `"delegation_result/v1"` | Normalized result schema (same as headless_json). |
| `transport` | `"acp"` | Confirms ACP transport was used. |
| `transport_details` | object | ACP-specific detail: `schema` (`"acp_result_v1"`), `structured_events` (list of `session/update` events — snake_case `agent_message_chunk` / `agent_thought_chunk` / `tool_call` / `tool_call_update` — and `session/request_permission` entries), `failure_class`, `stop_reason`. |
| `transport_details.failure_class` | string \| null | Structured failure classifier: `gemini_not_found`, `launch_failed`, `initialize_failed`, `session_new_failed`, `auth_required`, `prompt_error`, `protocol_error`, `timeout`, `watchdog`, `incomplete_response`, `contract_bypass`, or `null` on success. Drives fallback selection. `auth_required` (the Gemini CLI / OAuth session is not pre-authenticated; the ACP `authenticate` handshake is not implemented) is **excluded** from the headless_json fallback set so the auth failure is surfaced honestly rather than masked behind a fallback success. |
| `transport_details.stop_reason` | string \| null | The `stopReason` from the final `session/prompt` response. `ok: true` requires `stop_reason == "end_turn"` and a non-empty `response_text`. |
| `_acp_fallback` | boolean (optional) | Present and `true` when fallback to headless_json occurred. In that case the result keeps the `headless_json` shape and is **not** re-normalized. The verification script `verify_acp_roundtrip.sh` SKIPs with exit 77 when `gemini`/`jq` are absent. |

### Example request with `transport: acp` / ACP request 例示と入力サンプル

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

### ACP transport lifecycle / ACP 実行ライフサイクル

See `references/transport-acp.md` for full lifecycle, timeout design, permission proxy, and fallback documentation.
日本語要約: 詳細な state machine と timeout 設計は専用文書を正本とする。

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

## REPO_EVIDENCE_REF_V1

### 目的

ローカルアセット（リポジトリ内ファイル）の evidence（特に line-specific な excerpt）の精度保証 SSOT。`codebase-investigator` が `gemini-cli-headless-delegation` 経由で検出したファイル参照に対し、commit SHA 固定・excerpt hash・検証状態を含めることで、caller が決定論的に「この evidence は信頼できる」「この evidence は再確認が必要」を判定できる。

### Schema / スキーマ

```yaml
REPO_EVIDENCE_REF_V1:
  type: object
  required:
    - type
    - commit_sha
    - object_format
    - path
    - start_line
    - end_line
    - permalink
    - excerpt_sha256
    - verification_status
    - verification_method
    - verified_at
  properties:
    type:
      const: REPO_EVIDENCE_REF_V1
      description: "Schema identifier constant."
    
    object_format:
      type: string
      enum: [sha1, sha256]
      default: sha1
      description: "Git object format. Determines valid commit_sha length: 40 chars for sha1, 64 chars for sha256."
    
    commit_sha:
      type: string
      pattern: "^[a-f0-9]{40}$|^[a-f0-9]{64}$"
      description: "Commit hash (40-char SHA-1 or 64-char SHA-256, validated against object_format). Identifies exact commit at which excerpt was verified. Mutable branch references (blob/main, tree/develop) are forbidden."
    
    path:
      type: string
      description: "Repository-relative file path (e.g., docs/adr/0001.md, src/main.py). Resolved from repo root."
    
    start_line:
      type: integer
      minimum: 1
      description: "Starting line number (1-indexed). Caller MUST NOT flow caller-provided stale line ranges without re-verification."
    
    end_line:
      type: integer
      minimum: 1
      description: "Ending line number (1-indexed, inclusive). Line range MUST match excerpt_sha256 verification result or verification_status MUST be inconclusive."
    
    permalink:
      type: string
      format: uri
      pattern: "^https://github.com/[^/]+/[^/]+/blob/([a-f0-9]{40}|[a-f0-9]{64})/"
      description: "GitHub permanent link using commit SHA (not branch name). Format: https://github.com/{owner}/{repo}/blob/{commit_sha}/{path}#L{start_line}-L{end_line}. Mutable URLs (blob/main, blob/develop, etc.) are forbidden and MUST be rejected by validator."
    
    excerpt_sha256:
      type: string
      pattern: "^[a-f0-9]{64}$"
      description: "SHA-256 hash of the excerpt bytes (lines start_line..end_line inclusive). Used to detect stale references when line numbers drift due to file edits."
    
    anchor_text:
      type: [string, "null"]
      description: "Optional human fallback: section heading name, symbol name, or other landmark text for manual navigation. NOT authoritative — line numbers are the ground truth. anchor_text is provided for human readability only and MUST NOT be used as primary evidence. Vulnerable to heading renames, section moves, and symbol refactoring."
    
    verification_status:
      type: string
      enum: ["verified", "inconclusive"]
      description: |
        - verified: SHA-256 matched actual file content at commit_sha. Line range and excerpt are correct at specified commit.
        - inconclusive: SHA-256 mismatch, line range unverified, or verification logic itself failed. Caller MUST NOT treat inconclusive evidence as authoritative file:line pair. Re-verification by human or updated verification_method required before use.
    
    verification_method:
      type: string
      enum: ["sha256_hash_match", "sha256_hash_mismatch", "line_range_unverified", "fetch_error"]
      description: "How verification_status was determined. Enables caller-side diagnosis of confidence level."
    
    verified_at:
      type: string
      format: date-time
      description: "ISO 8601 timestamp (UTC) when verification completed. Used to detect verification staleness if reference ages beyond acceptable bounds."

  examples:
    - type: "REPO_EVIDENCE_REF_V1"
      object_format: "sha1"
      commit_sha: "abc123def456abc123def456abc123def456abc1"
      path: "docs/adr/0001-architecture.md"
      start_line: 42
      end_line: 67
      permalink: "https://github.com/squne121/loop-protocol/blob/abc123def456abc123def456abc123def456abc1/docs/adr/0001-architecture.md#L42-L67"
      excerpt_sha256: "1a2b3c4d5e6f7a8b9c0d1e2f3a4b5c6d7e8f9a0b1c2d3e4f5a6b7c8d9e0f1a2b"
      anchor_text: "## Architecture Overview"
      verification_status: "verified"
      verification_method: "sha256_hash_match"
      verified_at: "2026-05-23T15:30:45Z"

    - type: "REPO_EVIDENCE_REF_V1"
      object_format: "sha1"
      commit_sha: "def456abc123def456abc123def456abc123def4"
      path: "src/systems/combat.ts"
      start_line: 100
      end_line: 120
      permalink: "https://github.com/squne121/loop-protocol/blob/def456abc123def456abc123def456abc123def4/src/systems/combat.ts#L100-L120"
      excerpt_sha256: "f0e1d2c3b4a5968778695a4b3c2d1e0f1a2b3c4d5e6f7a8b9c0d1e2f3a4b5cab"
      anchor_text: null
      verification_status: "inconclusive"
      verification_method: "sha256_hash_mismatch"
      verified_at: "2026-05-23T15:31:10Z"
```

### Excerpt Canonicalization / 抜粋 hash 正規化

`excerpt_sha256` の正本は以下のルールで計算される:

- **正本フォーマット**: `git show <commit_sha>:<path>` で取得した **raw blob bytes**（改行コード変換・UTF-8 normalization・BOM 除去をかけない）
- **行境界**: LF (`\n`) のみで slice する（CRLF / CR は LF として扱わない — 含まれていれば raw のまま hash 対象に含める）
- **範囲**: `start_line..end_line` inclusive（1-indexed）
  - 例: `start_line=1, end_line=3` → blob の 1 行目から 3 行目を extract
  - 行は `\n` で分割した配列をスライス：`lines[start_line-1 : end_line]`
  - 各行末の `\n` は excerpt に含める（split が削除したため join で復元）。最終行（end_line）が EOF 行で末尾 LF を持たない場合は含めない
- **計算**: `hashlib.sha256(excerpt_bytes).hexdigest()` で 64 文字の 16 進文字列

### Mutable URL Prohibition（絶対禁止）

**Mutable branch URL（`blob/main` などの branch reference）は file evidence として禁止される。** GitHub の公式ドキュメント（[Getting permanent links to files](https://docs.github.com/en/repositories/working-with-files/using-files/getting-permanent-links-to-files)）で、branch head が進むたびに対応 commit が変わると明記されている。

```
❌ Forbidden (mutable):
  https://github.com/squne121/loop-protocol/blob/main/docs/adr/0001.md#L42-L67
  https://github.com/squne121/loop-protocol/tree/develop/src/

✅ Required (immutable, commit-SHA fixed):
  https://github.com/squne121/loop-protocol/blob/abc123def456abc123def456abc123def456abc1/docs/adr/0001.md#L42-L67
```

Wrapper および validator は以下を enforce する:

- `permalink` フィールドが `blob/<branch-name>` / `tree/<branch-name>` を含む場合、`verification_status` に関わらず **reject** する（**fail-closed**）
- validator fixture / `scripts/validate_repo_evidence_ref.py` で mutable URL を static check する

### anchor_text は Human Fallback（正本ではない）

`anchor_text` は section heading（例: `## Architecture Overview`）や symbol 名（例: `CombatSystem.executeAttack`）を記録する **optional** な human-readable landmark。以下の理由から正本にできない:

- **Heading rename リスク**: セクション見出しが「Architecture Overview」から「System Architecture」に変わると anchor_text は stale になるが、line range は変わらないかもしれない
- **Section move リスク**: markdown ファイルで section の順序が変わると、同じ heading が別の line range に移動
- **Symbol refactoring リスク**: class / function が別ファイルに移動すると anchor_text は指すモノがなくなる

**Caller 責務**: anchor_text を参考材料（「人間が手作業で確認するときはこの見出しを探して」）として提供するが、 **primary evidence は常に commit_sha + path + start_line / end_line + excerpt_sha256** 。anchor_text に基づいて line range を推測・修正することを禁止する。

### Stale Line Range の再確認なし流用を禁止（Caller 責務）

`caller-provided line range`（例：Issue コメントで「docs/dev/agent-skill-boundaries.md L604-L633」と指定されたもの）は、ファイル更新によって stale になっている可能性がある。

**Caller が caller-provided line range を直接流用してはならない**:

```python
# ❌ MUST NOT: caller が受け取った line range をそのまま使う
evidence_line_range = (604, 633)  # caller が Issue から読んだ値
# → この line range は古いかもしれない
code = read_file_excerpt(path, evidence_line_range)

# ✅ MUST: 必ず REPO_EVIDENCE_REF_V1 を通す
evidence_ref = fetch_repo_evidence_ref(path, context=issue_context)
if evidence_ref['verification_status'] == 'verified':
    # → excerpt_sha256 でも証跡が取れている
    code = read_file_excerpt(path, 
                             (evidence_ref['start_line'], 
                              evidence_ref['end_line']))
else:
    # → inconclusive → 人間による再確認が必要
    return {'status': 'inconclusive', 'reason': evidence_ref}
```

### Verification Status と Caller 責務（Fail-Closed） / 検証状態の扱い

Wrapper / delegation skill は以下の fail-closed 規約に従う:

- `verification_status: inconclusive` の evidence は絶対に `verified` に昇格させない（caller-side re-validation による昇格は許さない）
- `verification_status: inconclusive` を返す場合、caller は「この line range は精度不確定」として human escalation か explicit re-delegation で再確認を要求する
- wrapper が excerpt hash を計算できない場合（例：permission エラー、file not found）、強制的に `verification_status: inconclusive` + `verification_method: fetch_error` を返す（success を装わない）

### Caller-Side Re-Validation Rule / caller 側再検証ルール

Caller は REPO_EVIDENCE_REF_V1 インスタンスに対して以下の rules を守る:

- caller は domain-specific reason に基づく独自 reject はできるが、既存 evidence の provenance は壊してはならない。
- **Caller MAY independently validate** a REPO_EVIDENCE_REF_V1 instance and reject it for domain-specific reasons
- **Caller MUST NOT mutate an existing inconclusive evidence object into verified** — in-place upgrade は禁止（provenance preservation）
- **Caller MAY emit a NEW REPO_EVIDENCE_REF_V1 object** with new `verified_at`, `verification_method`, and metadata if re-validation succeeds（既存の inconclusive object は削除せず並行保持）
- **Caller MUST preserve the original inconclusive object** alongside the new verified object for audit trail（inconclusive predecessor を削除しない）

## Fallback Handoff Contract / フォールバック引き継ぎ契約

（`gemini-cli-headless-delegation` wrapper が file evidence を返せない場合の fail-closed パターン）

```yaml
Fallback Handoff Contract:
  schema_version: 1
  status: failed
  failure_reason: "<root cause: e.g. 'file not found', 'commit SHA invalid', 'permission denied'>"
  warnings:
    - "<diagnostic message>"
    - "<recovery suggestion>"
  next_action: "<caller が次に何をするべきか。例：Issue author による file reference 修正、別 commit による retry、manual escalation>"
  
  # 以下は NEVER included（unchecked evidence を返さない）:
  # - unverified REPO_EVIDENCE_REF_V1 instances
  # - fallback excerpt from stale assumptions
  # - implicit line numbers without verification
```

**Wrapper の約束**:
- file evidence を返す場合、**必ず** `REPO_EVIDENCE_REF_V1` 構造を満たす
- verification_status が inconclusive / failed な evidence を返す場合、caller に **inconclusive / failed であることを明示** する（caller-provided line range で fallback を試みない）
- evidence を返せない場合、`status: failed` + `failure_reason` + `next_action` で fail-closed する（未検証の excerpt を捏造しない）

### 使用例（Handoff）

**Wrapper 内で file evidence が取得できた場合** → `delegation_result/v1.response_text` に REPO_EVIDENCE_REF_V1 の YAML / JSON を embed（またはファイル artifact）

**Wrapper 内で file evidence が取得できなかった場合** → Fallback Handoff Contract に従い以下を返す:

```json
{
  "ok": false,
  "failure_reason": "file /path/to/file not found at commit abc123...",
  "warnings": [
    "Requested file path does not exist in repository",
    "Recovery: verify file path and commit SHA with current repo state"
  ],
  "response_text": null
}
```

Caller はこの失敗を見て「file evidence を取得できなかった」と判断し、Issue author に修正を依頼するか、別の evidence source を探す。

## Fan-Out Orchestrator (delegation_fanout_request_v1 / delegation_fanout_result_v1) / 並列実行 fan-out 契約

（Issue #1273）`fan_out_orchestrator.py` は既存の `delegation_request_v1` / `delegation_result/v1` を変更せず、
複数の provider/profile へ**並列**に fan-out する薄いオーケストレーション層を追加する。planner mode
（LLM によるタスク数・分割・provider 選択の動的決定）は v1 の対象外 -- 呼び出し側が `subtasks[]` を明示的に列挙する。

### Request: delegation_fanout_request_v1（closed schema、リクエスト定義）

```yaml
delegation_fanout_request_v1:
  schema: delegation_fanout_request_v1
  subtasks:  # 必須。空リスト不可。各要素は既存 delegation_request_v1 をそのまま保持する。
    - schema: delegation_request_v1
      provider: gemini | agy  # provider: auto は fan-out child では禁止（下記参照）
      tool_profile: no_tools | grounded_research | local_asset_research | proposal_only | github_research
      objective: "..."
      instructions: ["...", "..."]
      output_sections: ["..."]
      context_files: ["..."]
      subtask_id: "<任意。省略時は subtask-<index> が自動採番される>"
      # subtask_id は安全な charset のみ許可（英数字始まり、以降は英数字/_/./- のみ、
      # 最大128文字、制御文字禁止。'..' や絶対パスは拒否される）。呼び出し側から見える
      # 論理IDであり、ファイル名には決して使われない（orchestrator 内部生成の
      # artifact_stem を使う。下記「subtask_id と artifact_stem の分離」参照）。
      # subtasks[] 自体をここに書くことは禁止（再帰 fan-out 不可、planner mode 不可）
  max_workers: 4          # 任意。既定 4。全体の同時実行数上限。
  max_subtasks: 20        # 任意。既定 20。dedupe 後 unique subtask 数の上限。
  max_total_attempts: 20  # 任意。既定 20。v1 は 1 subtask = 1 attempt（orchestrator 側リトライなし）。
  overall_timeout_sec: 300  # 任意。既定 300。到達時に pending を cancel、実行中 child を terminate。
  provider_concurrency: {gemini: 2, agy: 1}  # 任意。provider 別 semaphore 上限（既定は max_workers）。
  profile_concurrency: {github_research: 1}  # 任意。profile 別 semaphore 上限（既定は max_workers）。
```

トップレベルキーは closed set（`schema` / `subtasks` / `max_workers` / `max_subtasks` /
`max_total_attempts` / `overall_timeout_sec` / `provider_concurrency` / `profile_concurrency`）で、
未知キーは fail-closed で拒否される。

#### subtask_id と artifact_stem の分離（Issue #1273 iteration 3 Blocker 1）

`subtask_id` は呼び出し側が指定する**論理**識別子であり、`results[].subtask_id` や
`deduplicated_aliases` に現れる。安全な charset（`^[A-Za-z0-9][A-Za-z0-9_.-]*$`、最大128文字、
制御文字禁止）にバリデーションされるが、**ファイル名や内部プロセスレジストリのキーには一切使われない**。
orchestrator は dedupe 後の各 unique subtask に対して、`{index:04d}-{fingerprint[:16]}` 形式の
`artifact_stem`（完全に orchestrator 生成、caller が制御不能）を別途割り当て、request/result ファイル名
と child process registry key にはこちらのみを使う。これにより `subtask_id` に `"../../outside"` の
ような値を与えても path traversal は構造的に不可能であり、重複 `subtask_id`（バリデーションで拒否済み）
による request/result 競合や process registry 上書きも発生しない。

### 実行前 exact dedupe

provider 呼び出し前に、`provider` / `tool_profile` / `objective` / `instructions` / `output_sections` /
`context_files` の**内容ハッシュ**（パスではなくファイル内容の SHA-256）/ `gh_commands` を canonical JSON 化した
SHA-256 fingerprint で重複判定する。**semantic dedupe（意味的類似度による縮約）は v1 では行わない** -- 上記
フィールドがすべて byte-for-byte 一致した場合のみ縮約される。縮約された subtask の元 `subtask_id` は
`deduplicated_aliases`（`{kept_subtask_id: [alias_subtask_id, ...]}`）に保持され、消えない。

### 実行制御

`max_workers`（全体の同時実行数）、provider 別 semaphore、profile 別 semaphore、`max_total_attempts`
（総 attempt 数上限）、`overall_timeout_sec`（全体タイムアウト）を組み合わせて適用する。上限超過分は
provider を一切起動せず `failed`（`max_subtasks_exceeded` / `max_total_attempts_exceeded`）として扱う。

### provider/profile 互換性 preflight（起動前検証）

provider 起動前に、`SUPPORTED_PROVIDERS` / `AGY_SUPPORTED_PROFILES`
（`run_gemini_headless.py` 正本）と `validate_request()` による完全な delegation_request_v1 検証を通し、
非互換な組合せや無効なリクエストは provider を起動せず `failed`（`provider_profile_incompatible` /
`validation_error`）として扱う。

**`provider: auto` は fan-out child では禁止**（Issue #1273 iteration 3 Blocker 3）。`auto` は
`run_delegation()` 内部で gemini → agy の順に再入し、その内部 fallback attempt は `max_total_attempts` にも provider 別 semaphore にも計上されない（child subprocess は 1 攻撃分の
枠しか消費しないのに実際には 2 provider を呼び得る）。この二重計上/semaphore バイパスを閉じるため、
v1 では `provider: auto` を preflight で一律拒否する。provider fallback が必要な場合は、呼び出し側が
`gemini` / `agy` の subtask を明示的に個別 submit すること。

### Child からの mutation は fail-closed で禁止（AC12）

child subtask は以下をすべて拒否される（provider 起動前の preflight で reject、実行されない）:
- `post_to_issue_url` の指定（GitHub write mutation）
- `gh_commands` の read-only argv allowlist（`_validate_github_research_argv()`）に違反するエントリ
  （github_research 以外の profile にも一律適用される -- delegation_request_v1 には他に構造化された
  shell/file-mutation channel が存在しないため）
- 再帰的な新規 fan-out 呼出し（`subtasks` キーを持つ、または `schema: delegation_fanout_request_v1` を宣言する）

child は結果を parent へ返すのみで、ファイルへの直接書き込みは行わない（parent が single-writer）。

### Result: delegation_fanout_result_v1（結果契約）

```yaml
delegation_fanout_result_v1:
  schema: delegation_fanout_result_v1
  status: success | partial_success | failed | cancelled
  ok: true   # unique subtask が全件 succeeded の場合のみ true（status: success と等価）
  parent_run_id: "<uuid4 hex>"
  counts:
    requested: 5   # 入力 subtasks[] の件数（dedupe 前）
    unique: 3      # dedupe 後の件数
    succeeded: 2
    failed: 1
    cancelled: 0
  results:   # 入力順（＝ dedupe 後の subtask_id 順）で固定。dedupe/preflight/timeout で拒否された
             # subtask も results[] に含まれる（fanout_status: failed | cancelled として）。
    - subtask_id: "..."
      original_ids: ["...", "..."]  # dedupe で縮約された元の subtask_id 群（自身を含む）
      fanout_status: succeeded | failed | cancelled
      result: <delegation_result/v1 または null>
      reasons: []  # failed/cancelled のときのみ非空
  failures: []   # results[] のうち fanout_status != succeeded の要素のサブセット
  deduplicated_aliases: {}
  run_dir: "<run 専用ディレクトリ（0700）>"
  manifest_path: "<run_dir>/manifest.json"  # temporary file へ書いてから os.replace() された最終 manifest
```

`ok` は全 unique subtask が succeeded のときのみ true（`status: success` と等価）。1 件でも失敗/cancel が
あれば `partial_success`（1 件以上 succeeded）または `failed`/`cancelled`（succeeded 0 件）に決定的に分類される。

### Timeout / cancel 契約

`overall_timeout_sec` 到達時、まだ開始していない subtask は provider を一切起動せず `cancelled` になる。
実行中の child は（production の subprocess runner の場合）SIGTERM → grace period 後 SIGKILL を
プロセスグループ単位で送る。timeout 後に遅れて返ってきた child の結果は**破棄**され、`cancelled` として
扱われる（`overall_timeout_late_result_discarded`）。

### CLI Usage（コマンドライン利用例）

```bash
uv run python3 .claude/skills/gemini-cli-headless-delegation/scripts/build_fanout_request.py \
  --subtask-request request-a.json \
  --subtask-request request-b.json \
  --max-workers 4 \
  --overall-timeout-sec 300 \
  --provider-concurrency gemini=2 \
  --provider-concurrency agy=1 \
  --profile-concurrency github_research=1 \
  --output fanout-request.json

uv run python3 .claude/skills/gemini-cli-headless-delegation/scripts/fan_out_orchestrator.py \
  --request-file fanout-request.json \
  --output-file fanout-result.json \
  --audit-log delegation_audit.jsonl
```


## `agy_tool_provenance_v1` のスキーマ統治方針（Issue #1708 対応、Schema Governance）

AGY fan-out 実行時の WebSearch/`read_url_content` 成功判定の正本は、AGY stdout
自己申告（`tool_calls` JSON や `AGY_WEBSEARCH:` 等の marker line）ではなく、AGY
`PreToolUse` lifecycle hook（`.agents/hooks.json`、公式仕様は installed Antigravity
CLI 同梱の `builtin/skills/agy-customizations/docs/hooks.md` を参照）から採取する
`agy_tool_provenance_v1` イベントである。実装は
`.claude/skills/gemini-cli-headless-delegation/scripts/agy_tool_provenance.py`。

### Schema 定義

`schema: "agy_tool_provenance_v1"`, `version: 1`。必須フィールド:

| フィールド | 型 | 説明 |
|---|---|---|
| `schema` / `version` | string / int | 固定値。 |
| `event` | string | `"PreToolUse"`。 |
| `toolCall.name` | string | canonical web tool 名のみ許可（下記）。 |
| `toolCall.args_sha256` | string(hex64) | raw args ではなく canonicalized args の sha256。 |
| `stepIdx` | int | AGY native `PreToolUse` payload 由来。 |
| `conversationId` | string | AGY native `PreToolUse` payload 由来。 |
| `transcript_path_ref` | string | `transcriptPath` の public-safe identifier（`sha256:` prefix）。raw absolute path は含めない。 |
| `transcript_sha256` | string(hex64) | orchestrator 側で計算する transcript hash。 |
| `parent_run_id` / `subtask_id` / `attempt_id` | string | fan-out run binding。 |
| `provider` | string | `"agy"` 固定。 |
| `tool_profile` | string | `no_tools` / `local_asset_research` / `grounded_research` 等。 |
| `monotonic_ns` | int | `time.monotonic_ns()`。 |
| `utc` | string(ISO8601) | UTC タイムスタンプ。 |

Canonical web tool 名: `search_web`, `read_url_content`（installed Antigravity CLI
1.1.5 の `PreToolUse` transcript サンプル `~/.gemini/antigravity-cli/brain/*/.system_generated/logs/transcript.jsonl`
で `toolCall.name == "search_web"` を実 readback 済み）。旧実装（#1266）が誤認識して
いた `web_search` / `websearch` / `browser_navigate` / `browser` / `url_read` /
`read_url` / `fetch_url` / `fetch` は canonical name ではなく、fail-closed
（`unknown_tool_provenance` / `unknown_tool_provenance:legacy_alias`）で拒否する。

### 利用者一覧（Consumer Inventory）

- `agy_tool_provenance.py`: schema の producer（`build_provenance_event()` /
  generated hook wrapper script）であり、かつ唯一の validator/evaluator
  （`validate_provenance_event()`, `evaluate_websearch_provenance()`）。
- `tests/test_agy_tool_provenance.py`, `tests/test_agy_provenance_schema_governance.py`:
  closed-schema tests（下記参照）。
- `run_gemini_headless.py` `_run_agy()`: workspace-scoped hook config
  （`.agents/hooks.json` + wrapper script）を AGY 実行ごとの isolated temp cwd に
  動的生成する producer 側 integration point。
- 他の既存 schema（`delegation_result/v1`, `delegation_audit_v1`,
  `fanout_result/v1` 等）の consumer は `agy_tool_provenance_v1` を直接消費しない
  （2026-07-25 時点で `rg -l "agy_tool_provenance_v1"` の hit は本 schema の
  producer/validator/tests のみ）。

### Compatibility Decision（互換性方針）

- `agy_tool_provenance_v1` は既存の `delegation_audit_v1` とは**別 schema**であり、
  既存 schema のフィールド集合・意味論を変更しない（additive, non-breaking）。
- `agy_tool_provenance_v1` イベントを `delegation_audit_v1` へどう取り込むか
  （embed するか、別 artifact として並置するか）は本 Issue の Out of Scope。
  取り込みが必要になった場合は互換性判断（新フィールド追加 = minor, 既存フィールド
  変更 = 新 schema version）を別 Issue で行う。
- 本 schema の必須フィールド集合はここに記載した 15 フィールドで固定（closed
  schema）。フィールド追加は許可されるが、削除・型変更は breaking change として
  `version` を上げる。

### Closed-Schema Tests（正本テスト）

- `.claude/skills/gemini-cli-headless-delegation/tests/test_agy_tool_provenance.py`
- `.claude/skills/gemini-cli-headless-delegation/tests/test_agy_provenance_schema_governance.py`

両ファイルとも hermetic（fixture 済み hook event・モック AGY 実行のみ、live AGY
バイナリ起動なし）。schema のフィールド集合を変更する場合は、上記 2 ファイルの
`REQUIRED_TOP_FIELDS` / `REQUIRED_TOOL_CALL_FIELDS` 網羅テストを更新すること。
