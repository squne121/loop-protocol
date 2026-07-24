# provider 対応表と運用メモ（Provider Mapping）

## 正本配置（Canonical Path）
- 正本は `.claude/skills/gemini-cli-headless-delegation/` に置く。
- Gemini を直接 ad hoc に叩かず、必ず `scripts/run_gemini_headless.py` を経由する。
- provider 固有の差分は caller 側の request JSON に閉じ込めつつ、wrapper では provider-aware extension として明示管理する。

## 共通 wrapper 呼び出し手順（Common Wrapper Invocation）
共通実行コマンドは次のとおり。
```bash
uv run python3 .claude/skills/gemini-cli-headless-delegation/scripts/run_gemini_headless.py \
  --request-file request.json \
  --output-file result.json
```

### 補足: Headless JSON / model / trusted / sandbox の注意点

- `headless JSON` は `request.json` / `result.json` のファイル契約として扱い、`stream-json` を想定しない。wrapper 出力は request/response JSON ファイルです。
- `model` は request の `model` フィールドで明示可能。**明示 `model` 指定時はそのモデルのみを試行し、quota 枯渇でも別 model へ降格しない**。
  - `role` / `model_chain`（`model` 未指定時）では、quota 枯渇時に同一 provider 内で下位モデルへ自動降格するチェーンが存在する。正本は `references/model-routing.md`。
  - runtime `provider=auto` では、上記 model 降格とは別フェーズとして provider 自体（gemini → agy）を切り替える `provider_auto_policy_v1` が適用される。詳細は本ファイル下部の「runtime `provider=auto`」節を参照。
- `trusted` は preflight で `trusted workspace` と認証状態を検査し、未成立時は `ok: false` で実行停止する（fail-closed）。
- sandbox は `no_tools` / `grounded_research` は `isolated temp cwd`、`local_asset_research` は確認済み MCP 構成時のみ repo root 起動とする。

## ツールプロファイル一覧（Tool Profiles）

| Profile | 振る舞い | 境界 |
|---|---|---|
| `no_tools` | Gemini CLI を isolated temp cwd から起動し、tool は使わない。 | `context_files` と `inline_context` のみ。 |
| `grounded_research` | Gemini CLI を isolated temp cwd から起動し、Google Search grounding を許可する。 | 外部調査のみ。repo 探索はしない。 |
| `local_asset_research` | `.gemini/settings.json` の Serena allowlist を確認したうえで repo root から起動する。 | WSL 上の Serena MCP を使った read-only ローカル資産調査のみ。 |
| `proposal_only` | Gemini CLI を isolated temp cwd から起動し、bounded draft text だけを返す。 | `implementation_draft` / `issue_authoring_draft` / `patch_proposal` / `command_plan` のみ。最終 write は Codex 側で行う。 |
| `github_research` | Gemini 側: wrapper が request の `gh_commands`（argv ベースの許可コマンドリスト）を pre-exec で実行し、その出力を `inline_context` に前置してから Gemini CLI を起動する。AGY 側: `unsupported_provider_profile` として fail-closed（`AGY_SUPPORTED_PROFILES` に含まれない）。 | `gh_commands` は `tool_profile=github_research` でのみ許可される（それ以外の profile で指定すると validation で拒否）。GitHub read-only 調査のみで、書込は許可しない。 |

`local_asset_research` は `grounded_research` とは意図的に分離している。
Web 調査プロファイルではないため、Serena MCP 検証に失敗したときの fallback 先として使ってはならない。
また `post_to_issue_url` とも分離しており、この profile では wrapper がその field を reject するため、GitHub 書き込みは local asset research の外に残る。

## Codex CLI での実行手順（Codex CLI Recipe）
1. `2 層 delegation 経路`（Codex CLI -> wrapper -> Gemini CLI）として wrapper を呼ぶ。
2. `request.json` を作り、`objective`、`instructions[]`、`tool_profile`、`output_sections[]` を必ず明示する。
3. current validated scope では `no_tools` / `grounded_research` / `local_asset_research` / `proposal_only` のみを扱う。`proposal_only` でも返せるのは draft text のみで、`file write`、`shell edit`、GitHub 書込権限委譲、実装 write 権限委譲は scope 外のまま維持する。
4. Gemini 実行自体は wrapper 経由でのみ行う。

実行例:
```bash
uv run python3 .claude/skills/gemini-cli-headless-delegation/scripts/run_gemini_headless.py \
  --request-file request.json \
  --output-file result.json
```

## Claude Code 実行手順（Claude Code Recipe）
1. Claude Code で同じ `request.json` を作る。
2. 生成後は wrapper をそのまま呼ぶ。
3. Gemini への直接実行や ad hoc prompt は使わない。

Claude Code でも同じコマンド形を使う。
```bash
uv run python3 .claude/skills/gemini-cli-headless-delegation/scripts/run_gemini_headless.py \
  --request-file request.json \
  --output-file result.json
```

## 既知の制約（Known Limitations）
- `grounded_research` は Google Search grounding を想定するが、shell やファイル編集は許可しない。
- `no_tools` は完全な read-only path として扱う。
- `local_asset_research` は `.gemini/settings.json` の `mcp.allowed == ["serena"]` と `mcpServers.serena.includeTools` read-only allowlist を machine-checkable に確認できる場合だけ使う。危険 tool または未検証 MCP 設定があれば fail-closed する。
- `proposal_only` は実装代行ではなく下書き委譲である。`post_to_issue_url`、file write、shell edit、GitHub mutation を request に含めた場合は fail-closed にする。
- `proposal_only` は `implementation_draft` と `issue_authoring_draft` の両用途で再利用できるが、最終 write owner は常に Codex 側 worker / main thread に残す。
- Gemini CLI は OAuth / Google アカウント認証で使う。headless 実行前に cached credential、trusted workspace、`.env`、MCP 設定が repo-local contract と矛盾しないことを確認する。
- 429 / `MODEL_CAPACITY_EXHAUSTED` は、明示 `model` 指定時は同一 model 内だけ限定回数リトライし、別 model へ自動切替しない。`role` / `model_chain` 指定時は同一 provider 内で下位モデルへの自動降格が存在する（正本: `references/model-routing.md`）。runtime `provider=auto` の provider 自体の切替は `provider_auto_policy_v1` に従う（別フェーズ、下記参照）。
- `--output-format json` / `stream-json` は Codex 側の契約範囲外。必要なら wrapper 外の別 contract で検討し、現状は `result.json` による headless JSON 契約に限定する。

## agy 対応マトリクス（Provider Matrix: agy / Antigravity CLI）

`agy` は Gemini OAuth 認証終了後の恒久代替 provider である。
Gemini CLI と同様に wrapper 経由で呼び出すが、出力形式・cwd policy・safety mode が異なる。

### AC1: 対応 profile 一覧（provider=agy）

`provider=agy` でサポートするプロファイルは以下のみ。

| Profile | サポート状態 | 説明 |
|---|---|---|
| `no_tools` | supported | isolated temp cwd から agy を呼び出す。ファイル編集・shell 実行なし。 |
| `proposal_only` | supported | isolated temp cwd から agy を呼び出す。返却は draft text のみ。 |
| `grounded_research` | **supported** | AGY native WebSearch/WebGrounding （`agy -p`、Gemini API `google_search` 不使用）を使用。`grounded` 判定には構造化 `tool_calls` トレース（認識済み web tool 名）が必須で、stdout 中の bare URL 文字列だけでは実行証跡と扱わない（トレース欠如は `agy_web_grounding_tool_call_missing` で fail-closed）。quota exhaustion / secret・repo path leakage も専用 failure class で fail-closed する。 |
| `local_asset_research` | supported | wrapper 側だけが pinned SerenaMCP read-only retrieval を実行し、repo-relative JSON evidence envelope だけを prompt-only で AGY に渡す。 |
| `github_research` | **unsupported_provider_profile** | agy は GitHub アクセス機能を持たない。fail-closed。 |

unsupported_provider_profile を request で指定した場合、wrapper は `ok: false` を即時返却する。
fallback や自動 profile 変換は行わず、fail-closed を維持する。

### AC2: 実行境界（agy の cwd / env）

agy を呼び出す際の cwd および環境変数は以下のポリシーに従う。

| 項目 | ポリシー |
|---|---|
| cwd | isolated temp cwd（`tempfile.TemporaryDirectory()` で都度生成。repo root を cwd にしない） |
| repo root 使用 | wrapper-only。`local_asset_research` では wrapper が repo root 内の検証済み context を repo-relative evidence に変換し、agy 側には repo root や absolute path を渡さない |
| env | minimal env（`_minimal_agy_env()` が `PATH` / `HOME` / `LANG` / `LC_ALL` / `TERM` / `XDG_CONFIG_HOME` / `XDG_CACHE_HOME` / `XDG_STATE_HOME` のみ allowlist する） |
| env 継承 | `GEMINI_API_KEY` 等の secret を環境変数ごと継承しない |
| subprocess 起動方式 | `shell=False`（`run_gemini_headless.py` の `_run_agy()` が `subprocess.run(command, cwd=tmp, env=env, shell=False, ...)` で呼び出す。shell injection の余地を排除する） |

agy は isolated temp cwd から実行し、repo のファイルシステムに直接アクセスしない。
実装は `run_gemini_headless.py` の `_run_agy()` を正本とする。

`local_asset_research` の AGY prompt は raw repo dump ではなく、checked-in Serena manifest、`.agents/mcp_config.json` pin、互換確認用 `.gemini/settings.json` pin を照合したうえで、次の provenance を持つ JSON evidence envelope だけを渡す。AGY には repo root、MCP config、direct tool access を渡さない。wrapper は `.agents/mcp_config.json` の pinned SerenaMCP stdio server を使い、`tools/list` と read-only `tools/call` の transcript を live verification evidence として残す。

- `tool_name`: wrapper 側が実行した Serena read-only tool 名。
- `query`: 取得対象を示す query または selector。
- `repo_relative_path`: repo root からの相対パス。
- `line_range`: evidence の行範囲。
- `content_snippet`: AGY へ渡す bounded snippet。
- `byte_size`: snippet の byte 数。
- `sha256`: snippet 内容の hash。
- `redaction_status`: credential-like payload 検査の状態。
- `manifest_id`: `serena-tool-manifest.json` の schema/ref を含む照合元。
- `source_kind`: live SerenaMCP stdio retrieval のみ `serena_mcp_read_only_evidence`。context file の direct read fallback や fake transport と混同してはならない。

context path の repo boundary / symlink / payload 検証で 1 件でも失敗した場合、wrapper は payload の `stat()` / `read_text()` へ進まず fail-closed する。

### AC3 / AC8: JSON envelope と結果正規化の差分

`agy` の stdout は Gemini JSON envelope（`_parse_envelope` が解析する `{"response": ...}` 形式）を返さない。

| 項目 | Gemini CLI | agy |
|---|---|---|
| stdout 形式 | Gemini JSON envelope（`{"response": ...}` 等） | plain text |
| normalization | `_parse_envelope` で JSON parse | wrapper が stdout text を直接 `delegation_result/v1` に正規化 |
| `_parse_envelope` 使用 | あり | **なし**（agy では `_parse_envelope` を通さない） |
| delegation_result/v1 | envelope parse 後に生成 | stdout text から直接生成 |

agy の stdout text は wrapper 側で `delegation_result/v1` スキーマに正規化し、Gemini JSON envelope parse（`_parse_envelope`）は使用しない。

### AC6: 非対応 profile の fail-closed

以下のプロファイルは `provider=agy` で `unsupported_provider_profile` として fail-closed する。

- `github_research` : GitHub 調査契約がないため現状は非対応。

`github_research` は agy 対応 contract が未定義のため fail-closed とする。

fallback 経路は提供せず、`ok: false` で即時終了する。
unsupported_provider_profile エラーは caller に返し、人間判断または別 provider への切り替えを促す。

### AC7: 安全モードの扱い

agy の safety mode は `degraded_wrapper_only` として扱う。

| 項目 | 詳細 |
|---|---|
| safety mode | `degraded_wrapper_only` |
| read-only 保証 | guaranteed ではない。wrapper-constrained として扱う。 |
| --approval-mode plan 相当 | 前提にしない |
| file 書き込み | wrapper が実行しない（agy 側の保証は前提にしない） |

agy の read-only 性は `degraded_wrapper_only / wrapper-constrained` として扱う。
Gemini CLI の `no_tools` profile のような guaranteed read-only ではないため、
wrapper 側で実行範囲を constrain して安全性を担保する。
agy 自体の --approval-mode plan 相当の動作は前提にしない。

### setup_check の provider 切替

`setup_check.py --provider agy --json` は `agy` / `python3` / `uv` を prerequisite として確認し、
`agy_preflight` と `skipped_gemini_checks` を machine-readable に返す。
`setup_check.py --provider auto --json` は `selected_provider` と `provider_attempts` を返し、
agy 優先の fallback 順序を確認できる。
`setup_check.py --provider agy --fix` は `.gemini/` や trustedFolders を変更せず、
`unsupported_provider_option` として fail-closed に扱う。

## runtime `provider=auto`（`provider_auto_policy_v1` ポリシー）

`run_gemini_headless.py` の `provider_auto_dispatch()` は、request の `provider` が `"auto"` のときに使われる
**実行時の provider fallback ポリシー**（`provider_auto_policy_v1`、正本は `config/model_routing.yaml` の
`provider_auto_policy_v1` ブロックと `run_gemini_headless.py` の `PROVIDER_AUTO_*` 定数）である。
これは前節の model downgrade（`model_chain` 内での同一 provider 内の model 降格）とは **別フェーズ** であり、
`provider_auto_dispatch()` は model downgrade ループを再実装せず、各 provider 呼び出しの結果（`failure_class` 等）を観測するだけである。

| 項目 | 値 |
|---|---|
| `runtime_order`（`PROVIDER_AUTO_RUNTIME_ORDER`） | `("gemini", "agy")` — gemini を先に試行する |
| `eligible_profiles`（`PROVIDER_AUTO_ELIGIBLE_PROFILES`） | `{"no_tools", "proposal_only"}` のみ。それ以外の `tool_profile` では provider 試行自体を行わず `provider_profile_unsupported` で即時 fail-closed する |
| `retryable_failure_classes` | gemini: `quota_or_rate_limited` / `model_capacity_exhausted` / `model_chain_exhausted`。agy: `agy_rate_limited` / `agy_capacity_exhausted` / `agy_web_grounding_quota_exhausted`。これら以外の failure（validation / auth / permission 等）は fallback せず即座に停止する（fail-closed デフォルト） |
| `stop_if`（`PROVIDER_AUTO_STOP_IF`） | `request_validation_failed` / `auth_or_permission_failed` / `request_has_post_to_issue_url` / `provider_profile_unsupported`。特に `post_to_issue_url` 指定時は非冪等な GitHub 投稿の重複を避けるため、最初の provider 試行が post-processing に到達した時点で以降の fallback を行わない |
| `fallback_policy_version`（`PROVIDER_AUTO_FALLBACK_POLICY_VERSION`） | `"v1"` |

### result field（`provider=auto` 専用の条件付き field）

`provider_auto_dispatch()` の結果には、通常の `delegation_result/v1` core field に加えて以下が付与される
（`PROVIDER_AUTO_RESULT_FIELDS` / `_provider_auto_finalize()`）。フィールド定義の詳細は `references/usage-contract.md` を参照。

- `selected_provider`: 最終的に採用した provider 名（`"gemini"` / `"agy"`）。provider 未選択（stop_if で即時停止）の場合は `null`
- `provider_attempts`: 試行した各 provider の結果を記録した list（監査可能な履歴）
- `fallback_reason`: fallback が発生した理由、または stop_if による即時停止理由（例: `"stop_if:provider_profile_unsupported"`）
- `fallback_policy_version`: 適用したポリシーの version（`"v1"` 固定）
- `attempts_by_model`: `provider_attempts[]` 内の各 provider が実際に試行した `{model_id: attempt_count}` を集計した map（`_attempts_by_model_from_provider_attempts()` が計算する実測値であり、推定値ではない）

### `setup_check.py --provider auto` と runtime `provider=auto` は別ポリシー

**この 2 つを混同しないこと。**

| 項目 | `setup_check.py --provider auto` | runtime `provider=auto`（`provider_auto_dispatch()`） |
|---|---|---|
| 性質 | 環境 probe（診断のみ、副作用なし） | 実行時 provider fallback（実際に Gemini / agy を呼び出す） |
| 順序 | agy-first（`setup_check_order`） | gemini-first（`runtime_order` / `PROVIDER_AUTO_RUNTIME_ORDER`） |
| 目的 | どちらの provider が使える状態か診断する | quota/capacity 系失敗時に別 provider へ切り替えて委譲を完了させる |
| `--fix` | `unsupported_provider_option` で拒否（副作用対象が曖昧なため） | 該当なし（runtime dispatch に `--fix` 相当の概念はない） |

2 つの順序が意図的に異なる理由: `setup_check_order` は「まず agy が使えるかを優先的に確認したい」という診断上の関心であるのに対し、`runtime_order` は「Gemini を既定 provider として維持しつつ quota/capacity 失敗時のみ agy にフォールバックする」という実行時の安全側デフォルトである。両者は独立したポリシーであり、一致している必要はない（`config/model_routing.yaml` の `provider_auto_policy_v1` ブロックのコメントを参照）。なお `references/model-routing.md` は現時点では model downgrade / role / model_chain のみを扱い、`provider_auto_policy_v1` 自体は未記載であることに注意する（本節が現状の唯一の docs 上の説明）。

## AGY PreToolUse Hook Provenance（Issue #1708 readback）

- installed Antigravity CLI version: `agy --version` → `1.1.5`（2026-07-25 readback）。
- 公式 lifecycle hook 仕様は installed CLI 同梱の
  `builtin/skills/agy-customizations/docs/hooks.md` を正本とする（`.agents/hooks.json`
  配置、`PreToolUse` は `{"toolCall": {"name", "args"}, "stepIdx", "conversationId",
  "transcriptPath", "workspacePaths", "artifactDirectoryPath", "modelName"}` を stdin
  で受け取り、`{"decision": "allow"|"deny"|"ask"|"force_ask", ...}` を stdout へ返す
  contract）。
- canonical web tool 名: **`search_web`**, **`read_url_content`**（installed CLI の
  live `PreToolUse` transcript サンプルで `toolCall.name == "search_web"` を確認
  済み）。AGY fan-out の WebSearch/grounding 成功判定は、この `PreToolUse` hook から
  採取する `agy_tool_provenance_v1` イベント（schema 定義は
  `references/usage-contract.md` の「`agy_tool_provenance_v1` Schema Governance」節
  を参照）を正本とし、AGY stdout の `tool_calls`/marker JSON は非正本の補助情報
  （`stdout_self_report`）として扱う。
- 実装: `.claude/skills/gemini-cli-headless-delegation/scripts/agy_tool_provenance.py`
  （workspace-scoped `.agents/hooks.json` 動的生成、schema validator、
  conversation/run 一致検証、redaction）。
