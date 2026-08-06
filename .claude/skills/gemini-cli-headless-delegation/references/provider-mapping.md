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
| `github_research` | Gemini 側: wrapper が request の `gh_commands`（argv ベースの許可コマンドリスト）を pre-exec で実行し、その出力を `inline_context` に前置してから Gemini CLI を起動する。AGY 側（Issue #1920）: `run_agy_github_research_e2e.py` が最大 8 回、AGY の判断に応じて単一 `gh` invocation を反復実行する。gh 実行は `run_agy_github_research_broker.py`（GH_TOKEN を保有する唯一のプロセス）が担い、AGY プロセス自体には GH_TOKEN を渡さない。 | Gemini の `gh_commands` は `tool_profile=github_research` でのみ許可される（それ以外の profile で指定すると validation で拒否）。AGY 側は `agy_permission_policy.PROFILE_ALLOWED_TOOLS["github_research"]` が空集合であり、AGY 自身のネイティブ tool-call は一切許可されない（broker が外部プロセスとして単一 gh invocation のみを実行する）。両側とも GitHub read-only 調査のみで、書込は許可しない。 |

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
| `github_research` | **supported**（Issue #1920） | `run_agy_github_research_e2e.py` に委譲。AGY はネイティブ tool-call を一切持たず（`PROFILE_ALLOWED_TOOLS` が空集合）、単一 `gh` invocation の選択をテキスト応答のみで行う。実行は GH_TOKEN を保有する `run_agy_github_research_broker.py` が担う。agy CLI / GH_TOKEN / read-only 認証のいずれかが利用不可な場合は exit 77 の structured SKIP を返す（SKIP は PASS ではない）。 |

`github_research` 以外で unsupported_provider_profile を request で指定した場合、wrapper は `ok: false` を即時返却する。
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

### `evidence_targets`（targeted-evidence 契約、Issue #1638）

`local_asset_research` request は `context_files` の代わりに `evidence_targets`（repo-relative path
+ bounded selector のリスト）を宣言できる。この経路は live SerenaMCP retrieval を使わず、wrapper が
declared target ごとに実ファイルを直接 read-only で読み、selector（現状 `line_range` のみ）が示す
行範囲そのものを source text として evidence envelope に含める。

- `path` / `selector`（`kind: "line_range"`, `start_line`, `end_line`）を宣言する。
- wrapper は repo 境界・symlink 越境・selector 上限（400 行/target、8 target まで）を検証し、
  違反があれば AGY を起動せず fail-close する。
- 生成される envelope は `repo_relative_path` / `selector` / `line_range` / `sha256` /
  `source_kind: "wrapper_read_only_targeted_evidence"` / `content`（実ソーステキスト）を持つ。
  `content` を持たない、または空の envelope を成功として扱うことはない（metadata-only fail-close）。
- target のファイル長超過、空 evidence、byte 数上限超過、credential-like content は、いずれも
  AGY subprocess 起動前に fail-close する。
- prompt に注入されるのは上記 envelope のみで、repo 絶対パス、`.agents/mcp_config.json` の内容、
  direct tool access 手順は含まれない（既存の prompt-only 境界をそのまま継承する）。

`evidence_targets` は legacy `context_files` + live SerenaMCP retrieval 経路とは排他的であり、
Serena MCP upstream の manifest allowlist 拡張は本契約の scope 外のままとする。
`github_research` の AGY 対応は Issue #1920 で実装済み（別契約、`run_agy_github_research_e2e.py` /
`run_agy_github_research_broker.py`）であり、`evidence_targets` / SerenaMCP 経路とは独立している。

### fan-out task-linked hash chain（Issue #1706 の相関ハッシュ連鎖）

`fan_out_orchestrator.run_fanout()` が `parent_run_id` / `subtask_id` / `attempt_id` を
stamp した子 subtask request に限り、上記 `evidence_targets` 契約の上に hash chain
（`objective_sha256` / `target_contract_sha256` / `request_sha256` / `evidence_sha256` /
`prompt_envelope_sha256` / `result_binding_sha256`）と actor 区別（`retrieval_actor:
wrapper_serena_mcp` / `analysis_actor: antigravity_cli` / `agy_direct_mcp_access: false`）を
追加する。詳細な hash 定義・格納先・fail-close 条件は `usage-contract.md` の
「fan-out task-linked Serena evidence hash chain」節を正本とする。単発（非 fan-out）の
`evidence_targets` request にはこの拡張は一切適用されない。

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

Issue #1920 で `github_research` が `provider=agy` に実装されたことにより、
`AGY_SUPPORTED_PROFILES` は `ALLOWED_TOOL_PROFILES`（`no_tools` / `grounded_research` /
`local_asset_research` / `proposal_only` / `github_research`）の全件と一致し、
現時点で `provider=agy` が `unsupported_provider_profile` を返す既知プロファイルは存在しない。

未知の `tool_profile` 値（`ALLOWED_TOOL_PROFILES` に存在しない値）は引き続き validation で
拒否される。fallback 経路は提供せず、`ok: false` で即時終了する。
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

## AGY PreToolUse フックの来歴記録（Issue #1708 の実機 readback 調査結果）

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


## GeminiCLI Legacy化判断根拠

`.claude/skills/gemini-cli-headless-delegation/config/profile_provider_contract_matrix.yaml`
（Issue #1806、schema: `profile_provider_contract_matrix/v1`）が示す実測状態を根拠に、
GeminiCLI を default provider から外す（legacy 化する）べきかどうかを、可観測性・
テスト密度・認証堅牢性の 3 観点で評価する。

### 可観測性

- agy 側は `agy_tool_provenance_v1`（`references/usage-contract.md` の
  「`agy_tool_provenance_v1` Schema Governance」節）により、`PreToolUse` hook から
  採取した構造化イベントを正本とし、stdout の自己申告（`stdout_self_report`）を
  非正本の補助情報に格下げする可観測性契約が既に整備されている
  （`scripts/agy_tool_provenance.py`）。
- GeminiCLI 側には同等の hook-based provenance 契約が存在せず、`_parse_envelope`
  による stdout JSON envelope の parse 結果をそのまま正本として扱っている
  （本ファイル「AC3 / AC8: JSON envelope と結果正規化の差分」節）。grounding の
  実行証跡を hook 経由で独立検証する仕組みは GeminiCLI 側に未実装であり、
  agy 側と比べて可観測性が相対的に弱い。

### テスト密度

- `.claude/skills/gemini-cli-headless-delegation/tests/` には agy 固有のテストが
  多数存在する（`test_agy_provider.py` / `test_agy_permission_policy*.py`
  （5 ファイル）/ `test_agy_provenance_grounding_wiring.py` /
  `test_agy_provenance_schema_governance.py` / `test_agy_local_asset_research_contract.py` /
  `test_agy_targeted_evidence.py` / `test_agy_serena_fanout_correlation.py` /
  `test_agy_tool_provenance*.py`（2 ファイル）/
  `test_agy_isolated_workspace_tool_permission.py` /
  `test_agy_invocation_argv_allowlist.py` /
  `test_agy_fanout_e2e_validator*.py`（2 ファイル）/ `test_audit_agy_auth_surface.py` 等、
  20 ファイル超）。GeminiCLI 固有のテストは `test_run_gemini_headless.py` /
  `test_quota_fallback.py` / `test_preflight_gemini_headless.py` /
  `test_golden_tasks.py` 等が中心で、profile x provider の 10 セル中
  `profile_provider_contract_matrix.yaml` が `implemented` と判定した 9 セルは
  GeminiCLI 側でも実装済みだが、agy 側で最近追加された provenance / permission
  boundary 系の専用テスト密度には及ばない。
- Issue #1920 以前は GeminiCLI のみが `no_tools` / `proposal_only` /
  `grounded_research` / `local_asset_research` / `github_research` の全 5
  profile を `implemented` としており、agy は `github_research` が
  `unsupported_by_design` だったため profile カバレッジで GeminiCLI が上回って
  いた。Issue #1920 で agy 側の `github_research` も `implemented` になり
  （`profile_provider_contract_matrix.yaml` 参照）、両 provider の profile
  カバレッジは同等になった。下記 `legacy_decision` の `blocking_gap` /
  `reevaluate_when` はこの変化を前提とした再評価が必要（本 Issue 自体は
  `legacy_decision.state` の変更を行わない — 別スコープの判断のため）。

### 認証堅牢性

- agy は `_minimal_agy_env()` による allowlist 環境変数（`PATH` / `HOME` / `LANG` /
  `LC_ALL` / `TERM` / `XDG_CONFIG_HOME` / `XDG_CACHE_HOME` / `XDG_STATE_HOME` のみ）と
  `shell=False` subprocess 起動（本ファイル「AC2: 実行境界（agy の cwd / env）」節）
  により、secret 環境変数の継承を構造的に遮断している。
- GeminiCLI は OAuth / Google アカウント認証に依存し、cached credential・trusted
  workspace・`.env`・MCP 設定の整合性を都度確認する運用（本ファイル「既知の制約」節）
  であり、agy の allowlist 方式ほど構造的に閉じた認証境界を持たない。
- **限定**: 上記の「agy は allowlist 方式で secret 環境変数の継承を構造的に遮断している」
  という主張は env var 継承境界に限定されたものであり、agy の認証手続き自体
  （OAuth / Google アカウント認証）が GeminiCLI より堅牢であることは意味しない。
  agy も GeminiCLI と同様に WSL2 環境で system keyring（D-Bus session bus）に
  到達できず OAuth 再認証が silent に失敗する既知の問題を抱える
  （`.claude/skills/gemini-cli-headless-delegation/SKILL.md` の
  「AGY 認証診断・既知の環境課題（WSL2 / non-TTY）（Issue #1267）」節、
  `auth.keyring.failure_class: system_keyring_unavailable` 参照）。したがって
  「認証堅牢性は agy が優位」という評価は env var isolation の構造に限定した比較
  であり、WSL2 実行環境における OAuth 到達性・再認証の脆さという観点では
  両 provider に共通の既知課題が残る。

### `legacy_decision:`

```yaml
legacy_decision:
  state: blocked
  reason: >-
    profile_provider_contract_matrix.yaml が示す通り、agy は github_research
    が unsupported_by_design（upstream の Antigravity CLI 自体に GitHub アクセス
    能力が無いという主張ではなく、repo wrapper のポリシー — AGY_SUPPORTED_PROFILES,
    scripts/run_gemini_headless.py — が github_research を対象 profile の集合に
    含めていないという設計判断が原因で fail-closed）であり、GeminiCLI が唯一
    github_research をサポートする provider である。GeminiCLI を default から
    外す/legacy 化すると github_research profile が provider 非依存で利用不能に
    なり、機能同等性のギャップが生じる。可観測性（hook-based provenance）と
    テスト密度は agy が優位、認証堅牢性は agy の allowlist 方式（env var
    isolation に限定）が優位だが、github_research の provider parity が未解消の
    間は legacy 化を承認しない。
  effective_scope: >-
    この legacy_decision は「GeminiCLI を default provider から外す/legacy 化
    するかどうか」の判断単位ごとに、以下の状態を個別に評価する（単一の
    all-or-nothing 判断ではない）。
  effective_scope_by_unit:
    runtime_auto_priority: >-
      blocked ではない。実コード上の `PROVIDER_AUTO_RUNTIME_ORDER`
      （`.claude/skills/gemini-cli-headless-delegation/scripts/run_gemini_headless.py`）
      は既に `("agy", "gemini")` という agy-first 順序であり（PR #1798 /
      Issue #1692 で反転済み）、`eligible_profiles` が
      `{"no_tools", "proposal_only"}` のみで github_research を含まないため、
      本 legacy_decision の blocking_gap（github_research の provider parity
      未解消）によって blocking されない。この事実確認（runtime auto が実コード
      上は既に agy-first であること）は github_research の legacy 化 blocking
      判断（`blocking_gap`）自体には影響しない（github_research は runtime
      auto の対象 profile ではないため）。なお本ファイル上部「runtime
      `provider=auto`」節の 216/240/244 行目付近（`runtime_order` =
      `("gemini", "agy")` という gemini-first の記述・比較表・理由説明）は、
      この実コードの現況（agy-first）と矛盾した記述のまま本 PR（#1823）では
      変更していない。当該箇所の修正は Issue #1804 が別途担当する（下記
      「#1804 との関係」参照）。読者は本節の事実確認と、本ファイル上部の未更新
      の記述を混同しないこと。
    builder_default: >-
      blocked ではない。`build_request.py` 等での既定 provider 選択で
      github_research を要求しない呼び出し経路については、agy-first 化を
      本 legacy_decision は妨げない。
    documented_recommendation: >-
      blocked ではない。docs 上で「新規呼び出しは agy を優先的に検討する」旨を
      推奨として記載することは、github_research profile が必要な場合の
      GeminiCLI 使用を排除しない限り妨げない。
    gemini_fallback_only: >-
      blocked ではない。GeminiCLI を「agy で対応できない profile
      （github_research）専用の fallback provider」として位置づけることは、
      本 legacy_decision が要求する機能同等性を満たしたまま legacy 化に相当する
      縮退運用であり、blocking_gap を解消する現実的な移行経路になり得る。
    gemini_implementation_removal: >-
      blocked。GeminiCLI の実装・provider 選択肢自体を削除することは
      github_research profile を provider 非依存で利用不能にするため、
      blocking_gap（github_research の agy 対応、provider parity 未確立）が
      解消されるまで承認しない。
  blocking_gap: >-
    github_research の agy 対応（現状 unsupported_by_design。follow-up #1821
    の capability tier 分割検討を含め、provider parity 確立が legacy 化判断の
    前提条件）
  reevaluate_when: >-
    github_research が agy で implemented になった時点、または
    github_research 自体が明示的な意図的除外として承認された場合
    （`deferred` は機能同等性のギャップを解消しないため、単独では reevaluate
    条件にならない）
  provenance:
    observed_at: "2026-07-27"
    repo_sha: d529c062f7eb12bd6124b619911627153ef3fa63
    gemini_cli_version: "0.52.0"
    agy_cli_version: "1.1.7"
    auth_mode: >-
      観測環境では GeminiCLI は既存の cached OAuth credential
      （`~/.gemini/gemini-credentials.json`）で認証済み、agy は Issue #1267 の
      WSL2 keyring 到達性問題が未解消の場合 `auth_mode: unknown` /
      `system_keyring_unavailable` になり得る（本ファイル「認証堅牢性」節の
      限定を参照）。両 CLI のバージョン・認証状態は流動的であり、本
      `legacy_decision` の再評価時には再観測が必要。
```

### `legacy_decision:` と Issue #1804 との関係

`references/provider-mapping.md` の「runtime `provider=auto`」節にある
`runtime_order`（216/240/244 行目付近、`PROVIDER_AUTO_RUNTIME_ORDER` =
`("gemini", "agy")` という gemini-first の記述・比較表・理由説明）は、実
コード上の `PROVIDER_AUTO_RUNTIME_ORDER`（既に `("agy", "gemini")` の
agy-first、PR #1798 / Issue #1692 で反転済み）と矛盾した記述のままである。
この既存記述の修正は Issue #1804 が対象とするスコープであり、本 PR（#1823 /
Issue #1806）ではその記述（該当箇所の値・比較表・理由説明）を変更しない。
上記 `effective_scope_by_unit.runtime_auto_priority` は、legacy 化判断の
観点から runtime auto が実コード上は既に agy-first であるという事実を記録
し、それが本 legacy_decision の blocking_gap によって妨げられないことを
明確化するものであり、github_research の legacy 化 blocking 判断
（`blocking_gap`）自体には影響しない。上記 docs 記述矛盾（216/240/244
行目）の解消そのものは Issue #1804 側で扱う。
