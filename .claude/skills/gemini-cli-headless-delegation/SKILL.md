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
     --profile github_research \
     --objective 'Issue #313 と PR #321 を gh issue view / gh pr view で調査する' \
     --context-file .claude/skills/gemini-cli-headless-delegation/references/usage-contract.md \
     --gh-issue 313 --gh-pr 321 \
     --output /tmp/gemini/request.json
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

## Grounded Research Retry Policy（governance）

このセクションは `tool_profile: grounded_research` における **wrapper-level retry の運用境界**を定義する governance 文書である。wrapper script (`scripts/run_gemini_headless.py`) の実 retry 挙動・retry 回数・`delegation_result_v1` の attempt フィールド仕様は `references/usage-contract.md` および `references/model-routing.md` を SSOT とし、本セクションはそれらと衝突しない範囲の運用方針のみを定める。

- 対象（運用境界として retry が妥当な失敗）:
  - `auth_error`
  - transient CLI failure
- 運用境界:
  - 同一 request の retry は wrapper 内に閉じ、caller / orchestrator 側で重ねて retry しない
  - retry 中に `tool_profile` / request body を変更しない
  - credential mutation なし、再ログインなし
- 再試行後も失敗した場合:
  - wrapper は失敗を隠さず返す
  - caller は `failure_class`（および wrapper が提供する attempt 情報があれば）を読んで fallback / escalation を判断する

この policy は wrapper-level retry の governance のみを扱う。grounding quality の判定、critical claim ごとの direct fallback、`WEB_RESEARCH_RESULT_V1` の最終分類は `web-researcher` の責務とする。wrapper script の挙動変更が必要になった場合は、本ファイルではなく `references/usage-contract.md` / `scripts/run_gemini_headless.py` / tests を更新する別 Issue を起票する。


## Gemini OAuth 終了・API key 暫定運用・agy 移行

Google ログイン経由の Gemini CLI 認証が終了した場合、`setup_check.py` は `auth.status: oauth_sunset`
または `auth.status: ineligible_tier` を返す。

### 暫定回避: GEMINI_API_KEY

API key が利用可能な場合は `GEMINI_API_KEY` 環境変数に設定することで暫定的に Gemini 経路を継続できる。

- **API key は暫定回避であり、恒久対応ではない。**
- key の値は stdout / stderr / JSON 出力に絶対に含めない（`setup_check.py` が existence のみ検出する）。
- key を `.env` / コードベース / PR 本文に commit しない。

```bash
# 暫定運用例（セッション内のみ有効）
export GEMINI_API_KEY=<your-key>
uv run python3 .claude/skills/gemini-cli-headless-delegation/scripts/setup_check.py --json
# → auth.status: "authenticated_api_key"
```

### 恒久対応: agy (Antigravity CLI) への移行

**恒久対応は Antigravity CLI (agy) への移行**である。parent Issue #104 を参照。

- API key 暫定運用は #104 が完了するまでのブリッジであり、agy 移行が完了したら不要になる。
- agy 移行の進捗は `docs/dev/current-focus.md` および #104 を参照。

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
