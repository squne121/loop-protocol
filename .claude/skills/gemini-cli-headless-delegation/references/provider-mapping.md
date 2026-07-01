# Provider Mapping

## Canonical Path
- 正本は `.claude/skills/gemini-cli-headless-delegation/` に置く。
- Gemini を直接 ad hoc に叩かず、必ず `scripts/run_gemini_headless.py` を経由する。
- provider 固有の差分は caller 側の request JSON に閉じ込め、wrapper の contract を変えない。

## Common Wrapper Invocation
```bash
uv run python3 .claude/skills/gemini-cli-headless-delegation/scripts/run_gemini_headless.py \
  --request-file request.json \
  --output-file result.json
```

### Headless JSON / model / trusted / sandbox Notes

- `headless JSON` は `request.json` / `result.json` のファイル契約として扱い、`stream-json` を想定しない。wrapper 出力は request/response JSON ファイルです。
- `model` は request の `model` フィールドで明示可能。既定は `gemini-3-flash-preview` で、別 model への自動 fallback はしない。
- `trusted` は preflight で `trusted workspace` と認証状態を検査し、未成立時は `ok: false` で実行停止する（fail-closed）。
- sandbox は `no_tools` / `grounded_research` は `isolated temp cwd`、`local_asset_research` は確認済み MCP 構成時のみ repo root 起動とする。

## Tool Profiles

| Profile | Provider behavior | Boundary |
|---|---|---|
| `no_tools` | Gemini CLI runs from isolated temp cwd with no tools. | Context files only. |
| `grounded_research` | Gemini CLI runs from isolated temp cwd and may use Google Search grounding. | External research only; no repo exploration. |
| `local_asset_research` | Gemini CLI runs from repo root after `.gemini/settings.json` Serena MCP allowlist validation. | WSL-local Serena MCP read-only local asset research only. |
| `proposal_only` | Gemini CLI runs from isolated temp cwd and returns bounded draft text only. | `implementation_draft` / `issue_authoring_draft` / `patch_proposal` / `command_plan` only. Final write stays on Codex side. |

`local_asset_research` is intentionally separate from `grounded_research`: it is not a web research profile and must not be used as a fallback when Serena MCP validation fails. It is also separate from `post_to_issue_url`; the wrapper rejects that field for this profile so GitHub write operations remain outside local asset research.

## Codex CLI Recipe
1. `2 層 delegation 経路`（Codex CLI -> wrapper -> Gemini CLI）として wrapper を呼ぶ。
2. `request.json` を作り、`objective`、`instructions[]`、`tool_profile`、`output_sections[]` を必ず明示する。
3. current validated scope では `no_tools` / `grounded_research` / `local_asset_research` / `proposal_only` のみを扱う。`proposal_only` でも返せるのは draft text のみで、`file write`、`shell edit`、GitHub 書込権限委譲、実装 write 権限委譲は scope 外のまま維持する。
4. Gemini 実行自体は wrapper 経由でのみ行う。

```bash
uv run python3 .claude/skills/gemini-cli-headless-delegation/scripts/run_gemini_headless.py \
  --request-file request.json \
  --output-file result.json
```

## Claude Code Recipe
1. Claude Code で同じ `request.json` を作る。
2. 生成後は wrapper をそのまま呼ぶ。
3. Gemini への直接実行や ad hoc prompt は使わない。

```bash
uv run python3 .claude/skills/gemini-cli-headless-delegation/scripts/run_gemini_headless.py \
  --request-file request.json \
  --output-file result.json
```

## Known Limitations
- `grounded_research` は Google Search grounding を想定するが、shell やファイル編集は許可しない。
- `no_tools` は完全な read-only path として扱う。
- `local_asset_research` は `.gemini/settings.json` の `mcp.allowed == ["serena"]` と `mcpServers.serena.includeTools` read-only allowlist を machine-checkable に確認できる場合だけ使う。危険 tool または未検証 MCP 設定があれば fail-closed する。
- `proposal_only` は実装代行ではなく下書き委譲である。`post_to_issue_url`、file write、shell edit、GitHub mutation を request に含めた場合は fail-closed にする。
- `proposal_only` は `implementation_draft` と `issue_authoring_draft` の両用途で再利用できるが、最終 write owner は常に Codex 側 worker / main thread に残す。
- Gemini CLI は OAuth / Google アカウント認証で使う。headless 実行前に cached credential、trusted workspace、`.env`、MCP 設定が repo-local contract と矛盾しないことを確認する。
- 429 / `MODEL_CAPACITY_EXHAUSTED` は同一 model 内だけ限定回数リトライし、別 model へ自動切替しない。
- `--output-format json` / `stream-json` は Codex 側の契約範囲外。必要なら wrapper 外の別 contract で検討し、現状は `result.json` による headless JSON 契約に限定する。

---

## Provider Matrix: agy (Antigravity CLI)

`agy` は Gemini OAuth 認証終了後の恒久代替 provider である。
Gemini CLI と同様に wrapper 経由で呼び出すが、出力形式・cwd policy・safety mode が異なる。

### AC1: Supported Profiles（provider=agy）

`provider=agy` でサポートするプロファイルは以下のみ。

| Profile | サポート状態 | 説明 |
|---|---|---|
| `no_tools` | supported | isolated temp cwd から agy を呼び出す。ファイル編集・shell 実行なし。 |
| `proposal_only` | supported | isolated temp cwd から agy を呼び出す。返却は draft text のみ。 |
| `grounded_research` | **unsupported_provider_profile** | `provider=agy` の初期実装では未対応。wrapper が Google Search grounding contract を agy に対してまだ検証・公開していない（wrapper サポート境界）。fail-closed。 |
| `local_asset_research` | **unsupported_provider_profile** | agy は Serena MCP 構成を持たない。fail-closed。 |
| `github_research` | **unsupported_provider_profile** | agy は GitHub アクセス機能を持たない。fail-closed。 |

unsupported_provider_profile を request で指定した場合、wrapper は `ok: false` で即時返却する（fail-closed）。
fallback や自動 profile 変換は行わない。

### AC2: cwd/env Policy（provider=agy）

agy を呼び出す際の cwd および環境変数は以下のポリシーに従う。

| 項目 | ポリシー |
|---|---|
| cwd | isolated temp cwd（初期対応では repo root を cwd にしない） |
| repo root 使用 | 不使用。`local_asset_research` のような repo root 起動は agy では行わない |
| env | minimal env（必要最小限の環境変数のみ渡す） |
| env 継承 | `GEMINI_API_KEY` 等の secret を環境変数ごと継承しない |

agy は isolated temp cwd から実行し、repo のファイルシステムに直接アクセスしない。

### AC3 / AC8: Result Normalization と Gemini JSON envelope

`agy` の stdout は Gemini JSON envelope（`_parse_envelope` が解析する `{"response": ...}` 形式）を返さない。

| 項目 | Gemini CLI | agy |
|---|---|---|
| stdout 形式 | Gemini JSON envelope（`{"response": ...}` 等） | plain text |
| normalization | `_parse_envelope` で JSON parse | wrapper が stdout text を直接 `delegation_result/v1` に正規化 |
| `_parse_envelope` 使用 | あり | **なし**（agy では `_parse_envelope` を通さない） |
| delegation_result/v1 | envelope parse 後に生成 | stdout text から直接生成 |

agy の stdout text は wrapper 側で `delegation_result/v1` スキーマに正規化し、Gemini JSON envelope parse（`_parse_envelope`）は使用しない。

### AC6: Unsupported Profile の fail-closed（provider=agy）

以下のプロファイルは `provider=agy` で `unsupported_provider_profile` として fail-closed する。

- `grounded_research`
- `local_asset_research`
- `github_research`

`grounded_research` は `provider=agy` の初期実装では unsupported である。
これは、この wrapper が agy に対して Google Search grounding contract をまだ検証・公開していないためであり、
wrapper サポート境界であって agy 製品の永続的な機能制限ではない。

`local_asset_research` と `github_research` も同様に、agy に対する対応 contract が未定義のため fail-closed とする。

fallback 経路は提供せず `ok: false` で即時終了する。
unsupported_provider_profile エラーは caller に返却し、人間判断または別 provider への切り替えを促す。

### AC7: Safety Mode（provider=agy）

agy の safety mode は `degraded_wrapper_only` として扱う。

| 項目 | 詳細 |
|---|---|
| safety mode | `degraded_wrapper_only` |
| read-only 保証 | guaranteed ではない（wrapper-constrained） |
| --approval-mode plan 相当 | 前提にしない |
| file 書き込み | wrapper が実行しない（agy 側の保証は前提にしない） |

agy の read-only 性は `degraded_wrapper_only / wrapper-constrained` であり、
Gemini CLI の `no_tools` profile のような guaranteed read-only とは異なる。
wrapper 側で実行範囲を constrain することで安全性を担保する設計であり、
agy 自体の --approval-mode plan 相当の動作は前提にしない。

### setup_check.py --provider agy

`setup_check.py --provider agy --json` は `agy` / `python3` / `uv` だけを必須 prerequisite として検査し、
`agy_preflight_result/v1` を `agy_preflight` フィールドへ埋め込む。
この経路では `node` / `gemini` / `uvx` / `~/.gemini/trustedFolders.json` /
`.gemini/settings.json` / Serena MCP / Gemini auth を要求しない。

`--provider auto` は `agy -> gemini` の順に試行し、`selected_provider` と
`provider_attempts` で fallback 順序を machine-readable に返す。

`--provider agy --fix` は fail-closed とし、`.gemini/` や trustedFolders を変更しない。
