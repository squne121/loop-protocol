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
- `model` は request の `model` フィールドで明示可能。既定は `gemini-3-flash-preview` で、別 model への自動 fallback はしない。
- `trusted` は preflight で `trusted workspace` と認証状態を検査し、未成立時は `ok: false` で実行停止する（fail-closed）。
- sandbox は `no_tools` / `grounded_research` は `isolated temp cwd`、`local_asset_research` は確認済み MCP 構成時のみ repo root 起動とする。

## ツールプロファイル一覧（Tool Profiles）

| Profile | 振る舞い | 境界 |
|---|---|---|
| `no_tools` | Gemini CLI を isolated temp cwd から起動し、tool は使わない。 | `context_files` と `inline_context` のみ。 |
| `grounded_research` | Gemini CLI を isolated temp cwd から起動し、Google Search grounding を許可する。 | 外部調査のみ。repo 探索はしない。 |
| `local_asset_research` | `.gemini/settings.json` の Serena allowlist を確認したうえで repo root から起動する。 | WSL 上の Serena MCP を使った read-only ローカル資産調査のみ。 |
| `proposal_only` | Gemini CLI を isolated temp cwd から起動し、bounded draft text だけを返す。 | `implementation_draft` / `issue_authoring_draft` / `patch_proposal` / `command_plan` のみ。最終 write は Codex 側で行う。 |

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
- 429 / `MODEL_CAPACITY_EXHAUSTED` は同一 model 内だけ限定回数リトライし、別 model へ自動切替しない。
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
| `grounded_research` | **unsupported_provider_profile** | `provider=agy` の初期実装では未対応。wrapper が Google Search grounding contract を agy に対してまだ検証・公開していない（wrapper サポート境界）。fail-closed。 |
| `local_asset_research` | supported | wrapper 側だけが pinned SerenaMCP read-only retrieval を実行し、repo-relative JSON evidence envelope だけを prompt-only で AGY に渡す。 |
| `github_research` | **unsupported_provider_profile** | agy は GitHub アクセス機能を持たない。fail-closed。 |

unsupported_provider_profile を request で指定した場合、wrapper は `ok: false` を即時返却する。
fallback や自動 profile 変換は行わず、fail-closed を維持する。

### AC2: 実行境界（agy の cwd / env）

agy を呼び出す際の cwd および環境変数は以下のポリシーに従う。

| 項目 | ポリシー |
|---|---|
| cwd | isolated temp cwd（初期対応では repo root を cwd にしない） |
| repo root 使用 | wrapper-only。`local_asset_research` では wrapper が repo root 内の検証済み context を repo-relative evidence に変換し、agy 側には repo root や absolute path を渡さない |
| env | minimal env（必要最小限の環境変数のみ渡す） |
| env 継承 | `GEMINI_API_KEY` 等の secret を環境変数ごと継承しない |

agy は isolated temp cwd から実行し、repo のファイルシステムに直接アクセスしない。

`local_asset_research` の AGY prompt は raw repo dump ではなく、checked-in Serena manifest と `.gemini/settings.json` pin を照合したうえで、次の provenance を持つ JSON evidence envelope だけを渡す。AGY には repo root、MCP config、direct tool access を渡さない。

- `tool_name`: wrapper 側が実行した Serena read-only tool 名。
- `query`: 取得対象を示す query または selector。
- `repo_relative_path`: repo root からの相対パス。
- `line_range`: evidence の行範囲。
- `content_snippet`: AGY へ渡す bounded snippet。
- `byte_size`: snippet の byte 数。
- `sha256`: snippet 内容の hash。
- `redaction_status`: credential-like payload 検査の状態。
- `manifest_id`: `serena-tool-manifest.json` の schema/ref を含む照合元。
- `source_kind`: `serena_mcp_read_only_evidence`。context file の direct read fallback と混同してはならない。

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

- `grounded_research` : Google Search grounding 向けで現状は非対応。
- `github_research` : GitHub 調査契約がないため現状は非対応。

`grounded_research` は `provider=agy` の初期実装では unsupported である。
理由は、この wrapper が agy に対する Google Search grounding contract をまだ検証・公開していないためであり、
agy 製品そのものの永続的な制限とみなしてはいけない。

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
