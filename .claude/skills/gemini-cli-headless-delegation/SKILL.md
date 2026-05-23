---
name: gemini-cli-headless-delegation
description: Gemini CLI を wrapper 経由で非対話 delegation する shared skill。巨大ログ調査、構造化された技術調査、根拠付き比較を構造化 request 契約で委譲したいときに使う。
disable-model-invocation: true
---

# Gemini CLI Headless Delegation

## tool_profile 早見表

| tool_profile | 用途 |
|---|---|
| `no_tools` | ツール不使用・コンテキスト読み取り専用 |
| `grounded_research` | Google Search grounding あり（timeout_sec: 300+ 推奨） |
| `local_asset_research` | Serena MCP read-only によるローカル資産調査 |
| `proposal_only` | 実装案・Issue 本文案・patch proposal のドラフト生成 |
| `github_research` | GitHub read-only 調査（gh コマンド allowlist）|

詳細は `references/usage-contract.md`（SSOT）・`references/model-routing.md`・`references/result-surface.md` を参照。

## Workflow

0. **setup_check で依存ツール・trusted folder・Serena MCP・settings.json を確認する（必須）**:
   ```bash
   uv run python3 .claude/skills/gemini-cli-headless-delegation/scripts/setup_check.py --json
   ```
   `ok: false` → `recovery` に従って対処。副作用ある操作（trustedFolders / .gemini/settings.json 変更）は `--fix` を付ける。

1. **request JSON を build_request.py で生成する（推奨）**:
   ```bash
   uv run python3 .claude/skills/gemini-cli-headless-delegation/scripts/build_request.py \
     --profile github_research --objective 'Issue #N の調査' --output /tmp/gemini/request.json
   ```
   または手動で `delegation_request_v1` JSON を作成する（`references/usage-contract.md` 参照）。

2. **preflight を実行する（必須）**:
   ```bash
   uv run python3 .claude/skills/gemini-cli-headless-delegation/scripts/preflight_gemini_headless.py \
     --output-file tmp/gemini-headless-preflight.json
   ```
   `ok: false` → `failure_reason` / `next_action` を確認して修正する。

3. **`scripts/run_gemini_headless.py` で request を検証・実行する**:
   ```bash
   uv run python3 .claude/skills/gemini-cli-headless-delegation/scripts/run_gemini_headless.py \
     --request-file /tmp/gemini/request.json --output-file /tmp/gemini/result.json
   ```
   validate-only（Gemini CLI 未実行）は `--validate-only` を指定する。結果は `result_surface.summary` / `.primary_artifact` / `.next_action` を優先参照する。

## Core Rules

### Delegation Boundary
- Gemini 側の `file write` / `shell edit` / GitHub mutation は禁止。
- `post_to_issue_url` は wrapper 側の GitHub コメント投稿操作。`no_tools` / `grounded_research` のみ許可。
- 最終 file edit / shell 実行 / GitHub mutation は caller / orchestrator が保持する。

### Request Validation
- `tool_profile` は必ず明示し、暗黙既定を置かない。
- `context_files` が欠ける、参照先が見つからない、`objective` が曖昧すぎる、`instructions` が 2 件未満、`output_sections` が空なら fail-closed にする。
- `GEMINI.md` など ambient context を避けるため、wrapper は isolated temp cwd で Gemini を呼ぶ。

### Model Routing
- モデル選択・降格チェーン・quota retry・role 別優先列の詳細は `references/model-routing.md` を参照。
- 明示 `model` 指定時は降格せずその model のみで試行する。

## References
- `references/index.md`（normative rule の owner file 一覧）
- `references/usage-contract.md`（リクエスト JSON フィールド仕様・profile ルールの SSOT）
- `references/model-routing.md`（model routing スキーマ・role テーブル）
- `references/result-surface.md`、`references/runtime-portability.md`
- `references/isolated-cwd-rationale.md`、`references/delegation-task-classes.md`

## 出力制約 (OUTPUT_BUDGET_V1)

`docs/dev/agent-skill-boundaries.md#OUTPUT_BUDGET_V1` の制約に従う。
