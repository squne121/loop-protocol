---
issue: 1749
parent_issue: 1265
related_issue: 1494
status: resolved
last_updated: 2026-07-25
note: "本ドキュメントは Issue 1749 の調査結果を日本語で記録する"
---

## 更新済み・廃止済みの注記（Issue #1814）

- 本資料の既存観測を置換せず、permission-boundary runner の artifact schema を追加する。

# AGY headless print mode（`agy -p`）で grounded_research の search_web/read_url_content が発火しない原因調査

## TL;DR（要約）

- 根本原因: `agy -p`（headless print mode, v1.1.7）は **デフォルトモデル（Gemini 3.x 系）を使うと、宣言済みの `search_web`/`read_url_content` ツールを実際には呼び出さず、「検索した」体の hallucination 回答を返す**。CLI フラグ・permission 設定（`--dangerously-skip-permissions` を含む）は無関係で、live 再現でも改善しなかった。
- 対処: `_run_agy()` が `tool_profile=grounded_research` のときのみ `agy -p <prompt> --model claude-sonnet-4-6` を実行するよう変更した。`claude-sonnet-4-6` を明示指定すると、同一 CLI・同一アカウント・同一 workspace 設定のまま実際に `search_web`/`read_url_content` を呼び出し、`vertexaisearch.cloud.google.com/grounding-api-redirect/...` 形式の実在する grounding citation URL を返すことを live 再現で複数回確認した。
- fail-closed evidence gate（#1708 の PreToolUse hook provenance 検証）はこの調査・修正の対象外であり、変更していない。むしろ今回の live 再現で「デフォルトモデルでは hook イベントが生成されない = 実ツール呼び出しなし」という判定が正しかったことを追加で確認した。

## 調査の出発点（#1494 5回目試行で確認済みの事実）

`provider=agy` / `tool_profile=grounded_research` で `agy -p`（headless print mode, v1.1.7）を実行しても、以下 3 通りの独立診断いずれでも実際に `search_web`/`read_url_content` ツールが呼び出されなかった:

1. 通常 prompt
2. search_web 強制指示 prompt
3. `--dangerously-skip-permissions` フラグ付き

AGY は「検索した」体で回答するが、公式ドキュメントのフィールド名と一致しない架空の値を答えており、hallucination であることが確認済み。fail-closed evidence gate（#1708）はこれを正しく拒否している。

## AC1: AGY 公式ドキュメント調査結果

### `agy --help` / `agy -p --help`（v1.1.7、live 取得）

```
Usage of agy:
  --add-dir                       Add a directory to the workspace (repeatable) (default [])
  --agent                         Agent for the current CLI session
  -c                              Short alias for --continue
  --continue                      Continue the most recent conversation
  --conversation                  Resume a previous conversation by ID
  --dangerously-skip-permissions  Auto-approve all tool permission requests without prompting
  --effort                        Reasoning effort for the current CLI session (low|medium|high)
  -i                               Short alias for --prompt-interactive
  --log-file                      Override CLI log file path
  --mode                          Set the agent execution mode for this session (accept-edits, plan)
  --model                         Model for the current CLI session
  --new-project                   Create a new project for this session
  -p                               Short alias for --print
  --print                         Run a single prompt non-interactively and print the response
  --print-timeout                 Timeout for print mode wait (default 5m0s)
  --project                       Project ID for the current CLI session
  --prompt                        Alias for --print
  --prompt-interactive            Run an initial prompt interactively and continue the session
  --sandbox                       Run in a sandbox with terminal restrictions enabled
```

`--tools` という CLI フラグは **存在しない**（Issue 本文が想定していた「不足フラグ」候補の一つは棄却）。`agy agents` / `agy agent` サブコマンドはこの実行環境ではローカル定義の custom agent が 0 件（`Available agents:` のみ出力）で、`--agent` は「ユーザー定義の custom agent 名」を指す機能であり、built-in の web search 有効化スイッチではない。

### 公式ドキュメント（`https://antigravity.google/docs/` 配下、live WebFetch）

- `/docs/cli/using`: 設定ファイルは `~/.gemini/antigravity-cli/settings.json`。`--sandbox` や `--dangerously-skip-permissions` などの CLI フラグで一部設定を launch 時に上書きできる。
- `/docs/cli/reference`: `toolPermission` は `"request-review"`（デフォルト。write/bash/web tool 実行前に確認を要求） / `"proceed-in-sandbox"` / `"always-proceed"` / `"strict"` の 4 値 enum。
- `/docs/cli/commands/permissions`: permission ルールは `/permissions` の Permissions Manager TUI か settings ファイルで管理する。headless/print mode 固有の挙動は明記されていない。
- `/docs/cli/features`: 「subagents have full access to tools such as code search, file editing, terminal commands, and web searches」という記述はあるが、web search tool 自体の有効化条件・model 依存性については記載がない。

### 既知の upstream Issue（`google-antigravity/antigravity-cli` リポジトリ）

- Issue #76: `agy -p` を非 TTY（pipe/subprocess/redirect）で実行すると stdout が完全に空になる（`isatty()` gate）。v1.0.0 時点の既知バグ。**本調査環境（v1.1.7）ではこの症状は再現しない**（stdout は必ず取得できている。後述の hallucination テキストが返る）。CHANGELOG 1.1.1〜1.1.4 で類似の headless 関連バグが複数修正されている。
- Issue #318: 同種の headless hang 問題。`agy-headless-bridge`（PyPI/GitHub）という pty 経由のサードパーティ workaround が存在するほど、非対話的実行の信頼性には既知の課題がある。

## AC2: 現在の `_run_agy()` のコマンド構成と不足フラグ

修正前の `_run_agy()`（`run_gemini_headless.py`）は、`tool_profile` に関わらず一律で以下のみを実行していた:

```python
command = [agy_bin, "-p", prompt]
```

`--model` を含む一切のモデル指定フラグを渡していなかった。この場合 AGY は default model（live 確認時点で `Gemini 3.6 Flash (High)`）を使用する。

### 検討し棄却した仮説

| 仮説 | 検証方法 | 結果 |
|---|---|---|
| `--tools` フラグ不足 | `agy --help` 精査 | そのようなフラグは存在しない（棄却） |
| `--agent` 指定不足 | `agy agents` / `agy agent --help` | ローカル custom agent 概念であり web search 有効化とは無関係（棄却） |
| permission review が headless で prompt をブロック | `--dangerously-skip-permissions` 付き live 再実行 + `cli.log` 確認 | ログに `Print mode: --dangerously-skip-permissions set, auto-approving all tool permissions` が出力され、permission は関与していないことを確認（棄却） |
| isolated workspace の `.antigravity/settings.json` スキーマ不一致（`agy_permission_policy.py` が実際の AGY 設定スキーマ `toolPermission` enum ではなく独自の `permissions.{default,allow,deny}` 形式で書き込んでいる。かつ配置パスも実際の設定ファイルパス `~/.gemini/antigravity-cli/settings.json` と異なる `HOME/.antigravity/settings.json`） | live 再現で isolated workspace を使わない裸の `agy -p` でも同一の hallucination が発生するか確認 | **裸の `agy -p`（実 `$HOME`、実際の設定ファイル使用）でも同一の hallucination が発生**。従って本 Issue の症状の直接原因ではない。ただし `agy_permission_policy.py` が書き込む `.antigravity/settings.json` は実際に AGY が読むファイルではない疑いが残る（別問題として記録するが、本 Issue の Allowed Paths・Scope 外につき修正しない） |

### 確定した根本原因

live 再現（`cli.log` 確認込み）で、デフォルトモデル使用時は：

- モデルへの生 prompt に対する回答テキストには hallucination された（実際のページ内容と一致しない）情報が含まれる
- `cli.log` に `search`/`tool` 呼び出しを示す行が一切出力されない（`agent=false`、`agentScript=false` のまま完了）
- ツール一覧を尋ねると `search_web` / `read_url_content` を含む正しいツール名一覧を返す（宣言はされている）が、実際には呼ばない

一方、`--model claude-sonnet-4-6` を明示指定すると：

- 応答に実際の tool 呼び出しの思考過程（"Let me fetch the page directly...", "Let me grep the content for heading tags..." 等）が現れる
- 実際のページ構造（JS-rendered SPA である旨、20KB truncation 等）に基づいた、検証可能で正直な回答を返す
- 実際の `vertexaisearch.cloud.google.com/grounding-api-redirect/...` 形式の grounding citation URL を返す（`agy --version` の値 `1.1.7` を web search で正しく取得した例で確認）
- `--dangerously-skip-permissions` なしでも動作する（read-class tool のため確認不要）

複数回の live 再実行で結果は一貫しており、「デフォルトモデルは宣言済みツールを実行せず hallucination する」「`claude-sonnet-4-6` を指定すると確実にツールを実行する」という結論は再現性がある。

## AC3/AC4: 対処（適用済み）

`_run_agy()` に、`tool_profile == "grounded_research"` のときのみ `--model claude-sonnet-4-6` を追加するよう修正した。他の tool_profile（`no_tools` / `local_asset_research` / `proposal_only` / `github_research`）のモデルルーティングには一切影響しない。

hermetic pytest（`test_agy_provider.py`）に以下を追加し、実際に subprocess 呼び出しに `--model claude-sonnet-4-6` が渡ることを確認する:

- `test_issue_1749_grounded_research_forces_tool_capable_model`: `_run_agy()` 単体呼び出しレベルで `grounded_research` profile のとき `--model claude-sonnet-4-6` が argv に含まれることを検証
- `test_issue_1749_non_grounded_research_profile_omits_model_flag`: `no_tools` profile では `--model` が付与されないことを検証（回帰防止）
- `test_issue_1749_grounded_research_end_to_end_forces_model_via_run_delegation`: `run_delegation(tool_profile="grounded_research")` の実行経路全体を通しても `--model claude-sonnet-4-6` が実際の `subprocess.run` 呼び出しに渡ることを検証

## fail-closed evidence gate との関係（変更なし）

本修正は `_run_agy()` が AGY に渡す CLI 引数のみを変更するものであり、`agy_tool_provenance.py` の PreToolUse hook provenance 検証ロジック・`evaluate_websearch_provenance()` の fail-closed 判定は一切変更していない。むしろ本調査の live 再現は、デフォルトモデル使用時に hook イベントが生成されない（＝ fail-closed gate が正しく `no web tool call` と判定する）ことを別角度から裏付けている。

## #1494 側での扱い（本 Issue の対処が有効だった場合の記録）

本 Issue の対処により、`_run_agy()` の grounded_research 起動コマンドに `--model claude-sonnet-4-6` が加わった。#1494 の live E2E run では、この変更を含む状態で grounded_research subtask の agy 呼び出しを再実行し、`search_web`/`read_url_content` の PreToolUse hook イベントが実際に生成されることを最終確認する（本 Issue の Runtime Verification Applicability: deferred の消化条件）。

## 未解決の副次的観察（本 Issue のスコープ外・別 Issue 候補）

- `agy_permission_policy.py` の `build_workspace_permission_policy()` が生成する `.antigravity/settings.json` は、実際の AGY CLI が読む設定ファイルパス（`~/.gemini/antigravity-cli/settings.json`）ともスキーマ（`toolPermission` enum）とも異なる。isolated workspace の deny policy が AGY 本体に対して機能しているかどうかは本調査で確認できていない（`workspace_deny_gate.py` hook が別経路で機能している可能性はあるが未検証）。この setting file 自体が実際に consumed されているかの検証は本 Issue の Allowed Paths（`run_gemini_headless.py` / `tests/` / `references/`）の外（`agy_permission_policy.py` は Allowed Paths に含まれない）であり、対処しない。follow-up Issue 化を検討可能。

## Live 確認の追記（Issue #1758）

Issue #1752/#1758 は上記の「実際の AGY CLI が読む設定ファイルパス（`~/.gemini/antigravity-cli/settings.json`）」の記述を live WebFetch で再確認し、`toolPermission` の 4 値 enum（`request-review` デフォルト / `proceed-in-sandbox` / `always-proceed` / `strict`）を確定させた。詳細な live 検証結果と、「isolated workspace が toolPermission 未設定のまま grounded_research を実行できない」という仮説が live `agy -p` 実行の結果として誤りだったことの確認は `references/grounded-research-isolated-workspace-investigation.md` の `## Finding 1 Live Verification` セクションを参照。


## 敵対的再監査への追記（Issue #1778）

CLOSED 済みの #1494（実 AGY/Serena/WebSearch fan-out E2E 検証）に対する control-plane の敵対的再監査（独立監査 3 本 + controlled live experiment 3 本）により、`agy_permission_policy.py` の認証 surface 過剰露出（`agy_oauth_token_path` のみが認証成功に必要十分であることを ablation experiment で実証）と、`_expose_gcloud_adc_read_only()` / `_expose_agy_oauth_token_read_only()` の read-only 未強制（`bwrap` PoC で実際に `OSError: Read-only file system` を発生させることを確認）が新たに判明した。実験そのものの詳細は別途 follow-up Issue で検証済みとして記録し、本ドキュメントの既存本文は変更しない。両者を機械可読に検出する baseline は `.claude/skills/gemini-cli-headless-delegation/scripts/audit_agy_auth_surface.py`（Issue #1778）を参照。

また、本ドキュメント冒頭の `## Live 確認の追記（Issue #1758）` セクションは `status: resolved` の本ドキュメントに包含された結論であるにもかかわらず、`agy_permission_policy.py` 側の `Issue #1758` 参照コメント（複数箇所）に逆参照マーカー（`# SUPERSEDED (Issue #M): ...`）が付いていないことが同監査で判明した。このコメント/ドキュメント間の causal claim drift を機械的に検出する baseline は `scripts/check_agy_causal_claim_drift.py`（Issue #1778）を参照。


## 訂正: capability-driven routing への置換（Issue #1777）

CLOSED 済み #1494 に対する敵対的再監査で、上記「確定した根本原因」節の因果主張（「`claude-sonnet-4-6` を指定すれば確実にツールを実行する」）が、実験的根拠を欠くコード内固定（`run_gemini_headless.py` の `AGY_GROUNDED_RESEARCH_MODEL = "claude-sonnet-4-6"` 定数）のまま運用されている設計 gap として指摘された。#1777 は control-plane が実施した controlled grounding matrix experiment（`model_selector × prompt_template`、各セル 3 反復、計 12 live 実行）でこの因果主張を再検証した。

```yaml
AGY_GROUNDING_MATRIX_V1:
  marginal_summary:
    by_prompt_template:
      minimal_fact_search_v1: "1/6 success (17%)"
      explicit_search_required_v1: "5/6 success (83%)"
    by_model_selector:
      account_default: "3/6 success (50%)"
      current_configured_candidate: "3/6 success (50%)"
```

`account_default`（モデル未指定）+ `explicit_search_required_v1` は 3/3（100%）成功し、`claude-sonnet-4-6` 明示指定 + 同一 prompt の 2/3（67%）を上回った。model 選択には限界効果がなく、prompt 構成（明示的な web 検索指示の有無）が支配的要因であることが実証された。**したがって上記「確定した根本原因」節の「`claude-sonnet-4-6` を指定すれば確実にツールを実行する」という因果主張は、この controlled experiment によって支持されなかった（実験的に反証された）。**

この結果を受け、`_run_agy()` の `grounded_research` モデル選択は次のように置換された:

- `AGY_GROUNDED_RESEARCH_MODEL` 定数は削除され、`config/model_routing.yaml` の `roles.grounded_research.model_chain`（`resolve_agy_grounded_research_model()` 経由）から読み込む capability-driven routing に置換した。
- model 指定は optional 化した。候補が空、または availability preflight（`_agy_model_is_available()`）を全候補が満たさない場合は `--model` フラグなしで `agy -p` を実行する（account_default）。
- 主要な信頼性担保手段を prompt/context contract 側へ移した。`AGY_GROUNDED_RESEARCH_EXPLICIT_SEARCH_INSTRUCTION`（`explicit_search_required_v1` 相当の明示指示）を grounded_research の全呼び出しへ常時付与する。
- hallucination / no-citation 失敗（`agy_web_grounding_tool_call_missing` / `agy_web_grounding_no_citations`）に対して、`AGY_GROUNDED_RESEARCH_RETRY_LIMIT`（既定 2）を上限とした bounded retry を追加した。各 retry は新しい `_run_agy()` subprocess 呼び出し（fresh session）として実行され、前回試行の応答は次回試行へ持ち越さない。
- 上記いずれの変更も、#1708/#1710/#1771 の fail-closed evidence gate（`_build_agy_grounded_research_metadata()` / hook provenance 検証）のロジック自体は変更していない。model 選択の有無に関わらず同一の判定が適用されることを `test_agy_provider.py` の回帰テストで確認済み。

この訂正は、CLOSED 済み #1494 を再オープンするものではない。#1494 に記録された過去の run 結果（PASS）は変更・削除しない。
