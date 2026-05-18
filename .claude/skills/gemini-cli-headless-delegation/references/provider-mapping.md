# Provider Mapping

## Canonical Path
- 正本は `.agents/skills/gemini-cli-headless-delegation/` に置く。
- Gemini を直接 ad hoc に叩かず、必ず `scripts/run_gemini_headless.py` を経由する。
- provider 固有の差分は caller 側の request JSON に閉じ込め、wrapper の contract を変えない。

## Common Wrapper Invocation
```bash
uv run python3 .agents/skills/gemini-cli-headless-delegation/scripts/run_gemini_headless.py \
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
uv run python3 .agents/skills/gemini-cli-headless-delegation/scripts/run_gemini_headless.py \
  --request-file request.json \
  --output-file result.json
```

## Claude Code Recipe
1. Claude Code で同じ `request.json` を作る。
2. 生成後は wrapper をそのまま呼ぶ。
3. Gemini への直接実行や ad hoc prompt は使わない。

```bash
uv run python3 .agents/skills/gemini-cli-headless-delegation/scripts/run_gemini_headless.py \
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
