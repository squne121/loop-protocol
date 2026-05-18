---
name: gemini-cli-headless-delegation
description: Gemini CLI を wrapper 経由で非対話 delegation する shared skill。巨大ログ調査、構造化された技術調査、根拠付き比較を構造化 request 契約で委譲したいときに使う。
---

# Gemini CLI Headless Delegation

> **注意（実装者向け）**: このスキルは `.agents/skills/` で管理されており、`bash scripts/sync-agent-skills.sh` により `.claude/skills/` へ自動同期される。変更時は `.agents/skills/gemini-cli-headless-delegation/` を直接編集すること（`.claude/skills/` への手動コピーは不要）。

## Use When
- 巨大ログ、インシデントログ、ビルドログ、テスト失敗ログを委譲したい
- 構造化された技術調査や根拠付き比較を Gemini に任せたい
- `implement-issue` / `create-issue` 向けに、最終 write を Codex 側に残したまま下書きだけを Gemini に委譲したい
- `gemini-3-flash-preview` を既定にした read-only headless 実行を使いたい
- Codex CLI または Claude Code から同じ wrapper を経由して Gemini を呼びたい（`2 層 delegation 経路`: Codex/Claude Code -> wrapper -> Gemini）

## Do Not Use When
- 曖昧な brainstorming や「いい感じに調べて」だけの依頼
- 実装、編集、削除の代行
- multi-turn の対話セッション前提の作業
- Gemini を ad hoc に直接叩く運用
- file edit / shell edit / GitHub mutation を Gemini 側に実行させたい依頼

## リクエスト JSON 早見表

> **委譲リクエストを作る前に必ずこの表を確認する。** フィールドの誤りは `ok: false` になるまで気づきにくい。
> 詳細な契約定義は `references/usage-contract.md` を参照（usage-contract.md が single source of truth）。

| フィールド | 必須 | 有効値 / 注意点 |
|---|---|---|
| `schema` | **必須** | `"delegation_request_v1"` 固定。省略不可。 |
| `objective` | **必須** | 具体的な目標。曖昧な動詞のみは拒否される。 |
| `instructions` | **必須** | 2 件以上のリスト。 |
| `tool_profile` | **必須** | `"no_tools"` / `"grounded_research"` / `"local_asset_research"` / `"proposal_only"` / `"github_research"` のいずれか。それ以外（例: `"code_analysis"`）は拒否される。 |
| `output_sections` | **必須** | 非空文字列のリスト（`string[]`）。1 件以上。例: `["セクション名1", "セクション名2"]`。オブジェクト形式（`{"id": ..., "title": ...}`）は不可。 |
| `context_files` | **必須** | 1 件以上。**パスは絶対パスを推奨**（下記参照）。 |
| `model` | 任意 | 省略時は `"gemini-3-flash-preview"`（= `default_chain[0]`）。明示指定時はその model のみを使用し、quota 枯渇でも降格しない。 |
| `role` | 任意 | `"web_research"` / `"code_research"` / `"github_research"` / `"implementation"` / `"issue_authoring"` のいずれか。`model` 非指定時に適用される降格チェーンを選択する。詳細は `references/model-routing.md` 参照。 |
| `inline_context` | 任意 | 追加コンテキスト文字列。 |
| `timeout_sec` | 任意 | 省略時は wrapper の既定値。**`grounded_research` 使用時は 300 秒以上を推奨**（Google Search ツール呼び出しで 36〜115 秒以上かかる場合がある）。300 未満を指定すると `warnings` に警告が追加される。 |
| `post_to_issue_url` | 任意 | GitHub Issue または PR の URL（例: `https://github.com/owner/repo/issues/123`）。指定時は調査結果を自動的に GitHub issue コメントとして投稿する。詳細は「GitHub へのコメント自動投稿」参照。 |
| `gh_commands` | 任意 | `[{"argv": ["issue", "view", "123"]}]` 形式の argv ベースコマンドリスト。wrapper が事前実行し結果を `inline_context` に prepend する general field。profile ごとに許可 allowlist が異なる（詳細は `references/usage-contract.md` の「gh_commands general field 仕様」セクション参照）。現状は `github_research` のみ完全実装済み（`local_asset_research` / `proposal_only` は設計済み・実装は別 issue）。 |

返却面の優先順位は `references/result-surface.md` を正本とする。caller / orchestrator は `response_text` 全文よりも `result_surface.summary` / `result_surface.primary_artifact` / `result_surface.next_action` を先に使う。

#### Current validated scope 参照

この skill の delegated 実行は `read-only / report-only / local_asset_research / proposal_only` を想定する。`proposal_only` でも返せるのは実装案・Issue 本文案・patch proposal・command plan の text draft のみで、`file write` や `shell edit`、GitHub 書込操作、`implementation write` は wrapper 契約外のまま維持する。

### `context_files` のパス解決（重要）

wrapper は **isolated temp cwd** から実行されるため、リポジトリ相対パスは解決されない。

- **推奨**: 絶対パスを使う（例: `/home/user/project/logs/build.log`）
- **注意**: `logs/build.log` のようなリポジトリ相対パスは、isolated temp cwd から見つからず `missing_context_file` で失敗する
- **代替**: `request.json` と同じディレクトリに context ファイルをコピーして相対パスを使う（`request_path.parent` 基準で解決される）
- テスト時の `validate_request` / `request_path` の扱いは `references/usage-contract.md` の「`request_path` と相対パス解決」セクションを参照

## Workflow

> **委譲前に必ず Step 1 の preflight を実行する。** preflight の `ok: false` は委譲リクエスト作成より先に解決する。

1. **preflight を実行する（必須）**:
   ```bash
   uv run python3 .agents/skills/gemini-cli-headless-delegation/scripts/preflight_gemini_headless.py \
     --output-file tmp/gemini-headless-preflight.json
   cat tmp/gemini-headless-preflight.json | uv run python3 -c "import sys,json; r=json.load(sys.stdin); print('ok:', r['ok'])"
   ```
   `ok: false` の場合は委譲を中止し、`failure_reason` を確認して修正する。

   **trusted workspace 失敗の復旧手順（`failure_class: "trusted_workspace_required"`）:**

   preflight smoke および本番委譲コマンドには `--skip-trust` が**既定で付与**されており（Issue #1824）、通常は trust エラーが発生しない。それでも出力 JSON に `failure_class: "trusted_workspace_required"` が含まれる場合は、使用中の Gemini CLI バージョンが `--skip-trust` をサポートしていない可能性がある。以下のフォールバック手順を試すこと:

   ```bash
   # フォールバック手順（--skip-trust 非対応バージョン用）
   GEMINI_CLI_TRUST_WORKSPACE=true uv run python3 \
     .agents/skills/gemini-cli-headless-delegation/scripts/preflight_gemini_headless.py \
     --output-file tmp/gemini-headless-preflight.json
   ```

   - `--skip-trust` は `preflight_gemini_headless.py` と `run_gemini_headless.py` の両方に既定で含まれる（Issue #1824）。
   - `recovery_action` フィールド（JSON 出力）および stdout の `[gemini-preflight] recovery:` 行に復旧手順が machine-readable に含まれる。
   - `~/.gemini/trustedFolders.json` は exact path matching のみ対応で wildcard 非対応のため、temp dir の信頼付与には使えない。

2. **`delegation_request_v1` JSON を作る**（上記「リクエスト JSON 早見表」を参照）。

3. `scripts/run_gemini_headless.py` で request を検証・実行する（wrapper は `context_files` を読み込む。`no_tools` / `grounded_research` は isolated temp cwd から Gemini を起動し、`local_asset_research` は machine-checkable な Serena MCP read-only 設定を検証できた場合だけ repo root から起動する）。

4. **stdout から最終報告を確認する**:
   - `ok: true` 時: 後方互換のため `response_text` の内容が stdout に出力される。
   - `ok: true` でも `response_text` が空の場合: `[gemini-headless] warning: response_text is empty` が出力される。
   - `ok: false` 時: 失敗理由（`warnings[0]`）が stdout に出力される。
   - 常に `[gemini-headless] result saved to: <output-file のパス>` が stdout に出力される。
   - orchestrator は `--output-file` の JSON から `result_surface.summary` / `result_surface.primary_artifact` / `result_surface.next_action` を優先して参照し、詳細な long-form evidence が必要な場合だけ `response_text` を読む。

5. **（オプション）GitHub Issue/PR へコメント投稿する**（`post_to_issue_url` が指定された場合のみ）:
   - `post_to_issue_url` フィールドを request JSON に含める。値は投稿先の GitHub Issue / PR の URL（例: `https://github.com/owner/repo/issues/123` または `https://github.com/owner/repo/pull/456`）。
   - wrapper（`run_gemini_headless.py`）が自動的に以下を実行する:
     - Step 4 で `ok: true` かつ `response_text` が存在する場合、内部で以下のコマンドを実行する:
       ```bash
       # jq で JSON から response_text を抽出し、投稿本文を準備する
       RESPONSE=$(jq -r '.response_text' <output-file>)
       # 実際の投稿経路は wrapper / usage-contract の current 実装を参照する
       ```
     - 投稿成功時は `result.json` に `comment_url` フィールドが追加される（例: `https://github.com/owner/repo/issues/123#issuecomment-XXXXXXXXX`）。
     - 投稿失敗時は `warnings` に失敗理由が追加される。`ok` は変わらない（調査自体は成功）。
   - **オーケストレーター受取**: `result.json` の `result_surface.primary_artifact` と `result_surface.summary` を記録する。調査結果全文（`response_text`）は detail が必要な場合にだけ読む。

## Core Rules
- `tool_profile` は必ず明示し、暗黙既定を置かない。有効値は `no_tools` / `grounded_research` / `local_asset_research` / `proposal_only` / `github_research` のみ。
- 既定 model は `gemini-3-flash-preview`（`default_chain[0]`）。別 model は明示 `model` フィールド指定時のみ許可する。
- quota 枯渇（429 / `MODEL_CAPACITY_EXHAUSTED` / `RESOURCE_EXHAUSTED`）時は `role` の `model_chain`（または `default_chain`）に沿って下位 model へ自動降格 retry する。chain を使い切ったら fail-closed し、caller-side fallback（ClaudeCode 直接生成）はその後に発動する。明示 `model` 指定時は降格せずその model のみで試行する。
- `role` フィールド（任意）は `tool_profile` とは独立した新概念であり、quota 枯渇時の降格チェーン選択にのみ使用する。`tool_profile` の明示は引き続き必須。
- model chain と role 別優先列は `config/model_routing.yaml`（任意オーバーライド）で設定可能。詳細は `references/model-routing.md` 参照。PyYAML 未導入環境では YAML override は無視され `DEFAULT_MODEL_ROUTING` が使われる（`RuntimeWarning` が発行される）。
- `--output-format` / `stream-json` はこの skill の既定経路では扱わない。`result.json` ベースの headless JSON 契約を前提にする。
- 実行モードは `--approval-mode plan` を既定とし、read-only 前提を崩さない。
- `tool_profile=grounded_research` の場合だけ Google Search grounding を許可し、`shell` と `ファイル` の編集系操作は禁止する。`timeout_sec` は 300 秒以上を指定すること（300 未満は `warnings` に警告が追加される）。
- `tool_profile=local_asset_research` は Gemini CLI + Serena MCP による WSL-local のローカル資産調査専用。`no_tools` と違い Serena MCP の read-only tool を使えるが、`grounded_research` と違い Google Search は使わない。
- `local_asset_research` は `.gemini/settings.json` の `mcp.allowed == ["serena"]`、`mcpServers.serena.command == "uvx"`、`--project-from-cwd`、`trust: false`、`includeTools` の read-only allowlist を wrapper が machine-checkable に検証できる場合だけ実行する。検証不能・危険 tool・未検証 MCP tool があれば fail-closed にする。
- `local_asset_research` の許可 tool は `find_file` / `find_referencing_symbols` / `find_symbol` / `get_symbols_overview` / `list_dir` / `search_for_pattern` のみ。`shell` 実行、`ファイル` 書き込み、GitHub 書き込み、Serena memory write/read、repo 外の任意読み取りは禁止。
- `local_asset_research` の `context_files` は symlink 解決後の実体パスが repo root 配下にある場合だけ許可する。絶対パス、`../`、symlink が repo 外へ解決される場合は fail-closed にする。
- `local_asset_research` は長文 context を argv に載せない。`run_gemini_headless.py` は prompt を stdin に送り、Gemini CLI の `--prompt ""` と repo root cwd で起動する実装を維持する。
- `local_asset_research` は `post_to_issue_url` を拒否する。関連 Issue コメントは wrapper の自動投稿経路ではなく、人間/実装者が読める役割分担コメントとして別途記録する。
- `local_asset_research` は長い context を扱うため、`run_gemini_headless.py` が `--prompt` 引数に空文字を渡し、実体プロンプトは stdin 経由で送信する。preflight で Gemini CLI の `--help` が `--prompt` の stdin 追記対応を報告できない場合は fail-closed とする。
- `proposal_only` は bounded draft profile であり、返却内容は `implementation_draft` / `issue_authoring_draft` / `patch_proposal` / `command_plan` の text proposal に限定する。最終 file edit / shell 実行 / GitHub mutation は Codex 側 worker または main thread が保持する。
- `proposal_only` は `post_to_issue_url` を拒否する。GitHub への実投稿は Codex 側の `issue-author` / `open-pr` / main thread が行う。
- `proposal_only` の request が direct file edit / shell execution / GitHub mutation を指示している場合、wrapper は fail-closed で拒否する。
- `github_research` は GitHub read-only 調査専用。許可コマンド: `gh issue list/view`、`gh pr list/view/diff`、`gh search issues/prs`、`gh label list`、`gh repo view`、`gh api`（GET のみ — `-X POST/PATCH/PUT/DELETE` および `--method POST/PATCH/PUT/DELETE` は拒否）。禁止コマンドを含む request は `failure_class: "github_research_command_denied"` で fail-closed。
- `github_research` は `post_to_issue_url` を拒否する（write mutation 禁止）。
- `gh_commands` は general optional field であり、wrapper が argv ベースで事前実行し結果を `inline_context` に prepend する。`no_tools` / `grounded_research` を除く profile で利用可能。profile ごとの allowlist が異なり、現状は `github_research` のみ完全実装済み（`local_asset_research` / `proposal_only` の allowlist 設計は `references/usage-contract.md` の「gh_commands general field 仕様」セクション参照、実装は別 issue）。`github_research` では argv 検証が最も厳密な経路であり、text-based は secondary defense として機能する（この defense 層は `github_research` 固有）。
- `github_research` は preflight の `gh_cli` セクション（`gh --version` / `gh auth status`）で `gh` のインストールと認証を確認する。未認証時は `failure_class: "gh_auth_required"` で fail-closed。
- Gemini CLI の認証前提は OAuth / Google アカウント認証。headless 実行前に interactive login 済みで cached credential があること、trusted workspace が通ること、`.env` / MCP 設定がこの repo-local contract と矛盾しないことを preflight の Stop Condition として扱う。
- 429 / `MODEL_CAPACITY_EXHAUSTED` は一時的失敗として扱い、指数バックオフ付きで再試行する。再試行上限に達した場合は、最終エラー・試行回数・待機履歴を構造化して報告する。
- 429 / `MODEL_CAPACITY_EXHAUSTED` に対しても自動 model fallback は行わない。別 model への切替は人間の明示指示がある場合のみ許可する。
- `context_files` が欠ける、参照先が見つからない、`objective` が曖昧すぎる、`instructions` が 2 件未満、`output_sections` が空なら fail-closed にする。
- `GEMINI.md` など ambient context を避けるため、wrapper は isolated temp cwd で Gemini を呼ぶ。
- stderr は破棄せず `warnings` と `stderr` に保持する。
- 再試行上限超過時は `retry_exhausted` 相当の構造化報告にまとめ、原因・試行回数・各遅延時間・最終状態を明示する。
- `post_to_issue_url` が指定された場合、`ok: true` かつ `response_text` が存在するときのみ、issue コメント投稿を試みる。投稿成功時は `comment_url` を結果に含める。投稿失敗時は失敗理由を `warnings` に追加し、`ok` は変わらない（調査自体は成功）。

## 429 / Capacity Exhausted Handling
- `HTTP 429` / `MODEL_CAPACITY_EXHAUSTED` / `RESOURCE_EXHAUSTED` は一時的な容量枯渇として扱う。
- 各 model で指数バックオフ付き同一 model retry（`RETRY_LIMIT` 回）を試みる。
- 同一 model retry を使い切り、かつ quota クラスエラーで、chain に次 model があれば → 次 model へ自動降格し、structured log イベント（`{"event": "model_downgrade", "from": ..., "to": ..., "reason": "quota_model_downgrade"}`）を stderr に出力して継続する。
- chain を使い切ったら fail-closed（`reason_code: "model_chain_exhausted"`）。result JSON に `model_chain`（試行した model リスト）、`model_downgrades`（`{from, to, reason}` リスト）、`actual_model`（最終使用 model）を記録する。
- 明示 `model` 指定時は降格なし（単一 model のみ試行）。
- caller-side fallback（`web-researcher` の ClaudeCode 直接生成）は wrapper が chain-exhausted を返した後に発動する。wrapper 内降格が先に試みられる。

## local_asset_research runtime smoke

production enablement の acceptance gate として、repo root から以下の手順で runtime smoke を実行できる。

```bash
# Step 1: preflight で静的設定と trusted workspace / OAuth を確認する
uv run python3 .agents/skills/gemini-cli-headless-delegation/scripts/preflight_gemini_headless.py \
  --compact \
  --output-file tmp/gemini-preflight.json
# ok: false → failure_reason を確認して Stop Condition として記録する
uv run python3 - <<'PY'
import json
from pathlib import Path
preflight = json.loads(Path("tmp/gemini-preflight.json").read_text(encoding="utf-8"))
assert preflight["local_asset_research"]["prompt_stdin_supported"], "long-context stdin route not available"
PY

# Step 2: local_asset_research smoke fixture を実行する
uv run python3 .agents/skills/gemini-cli-headless-delegation/scripts/run_gemini_headless.py \
  --request-file .agents/skills/gemini-cli-headless-delegation/tests/fixtures/local_asset_research_smoke_request.json \
  --output-file tmp/local-asset-smoke.json \
  --compact
# ok: false は Stop Condition（外部認証要因）として記録し、実装失敗と混同しない

# Step 3: 結果を機械判定する
uv run python3 - <<'PY'
import json
from pathlib import Path
r = json.loads(Path('tmp/local-asset-smoke.json').read_text(encoding='utf-8'))
assert r.get('tool_profile') == 'local_asset_research', f"tool_profile mismatch: {r.get('tool_profile')}"
if not r.get('ok'):
    print('STOP_CONDITION: local_asset_research smoke did not pass:', r.get('failure_reason'), r.get('warnings'))
    raise SystemExit(2)
response = str(r.get('response_text') or '')
assert response.strip(), 'response_text is required for successful local_asset_research smoke'
print('PASS: local_asset_research runtime smoke returned response_text')
PY
```

成功時の機械判定基準:
- `exit_code == 0` かつ `ok == true`
- `tool_profile == "local_asset_research"`
- `response_text` が非空文字列

`ok: false` の場合は実装失敗ではなく外部認証要因（trusted workspace 未成立、OAuth credential 不足）の Stop Condition として扱い、`failure_reason` / `warnings` を記録する。

## Verification
- `python3 .agents/skills/gemini-cli-headless-delegation/scripts/preflight_gemini_headless.py --output-file tmp/gemini-headless-preflight.json`
- `python3 .agents/skills/gemini-cli-headless-delegation/scripts/run_gemini_headless.py --request-file <request.json> --output-file <result.json>`
- `git diff --check`

## References
- `references/model-routing.md`（model routing スキーマ・role テーブル・reason_code 一覧・解決順）
- `references/provider-mapping.md`
- `references/result-surface.md`
- `references/usage-contract.md`
- `references/runtime-portability.md`（Claude Code WSL2 からの実行手順）
- `references/isolated-cwd-rationale.md`（isolated temp cwd の設計根拠）
- `references/delegation-task-classes.md`（R0/R1/R2/R3 定義と評価マトリクス）
- `config/model_routing.yaml`（任意オーバーライド設定ファイル）
