---
issue: 1752
parent_issue: 1265
related_issue: 1494
status: live_verification_complete_hypothesis_refuted
last_updated: 2026-07-26
note: "本ドキュメントは Issue 1752 の静的調査結果、および Issue 1758 の live agy 実行による Finding 1 仮説検証結果を記録する。"
---

# isolated workspace 内で grounded_research の search_web / read_url_content が発火しない原因の再調査（静的解析）

## 背景

#1749（PR #1751）は「デフォルトモデルでは宣言済みツールを呼ばず hallucination する」問題を
`--model claude-sonnet-4-6` 明示指定で解消したと結論づけた（`references/agy-headless-tool-use-investigation.md`
参照）。しかし #1751 の live 再テストは **isolated workspace（`agy_permission_policy.materialize_isolated_agy_workspace()`
経由）ではなく非隔離環境で行われていた可能性がある**（Issue #1752 本文の指摘）。実際、#1494 の
最終 live E2E fan-out 実行 6 回目試行では、`--model claude-sonnet-4-6` を正しく付与した状態でも
isolated workspace 内の grounded_research subtask で `search_web` 呼び出しが 5 回連続で発生しなかった
（`web_tool_call_count: 0`）。

本ドキュメントは、この isolated-workspace-specific な不発の原因を **静的解析のみ**（本 Issue の
Stop Conditions により live agy 実行は行わない）で再調査した結果を記録する。

## Findings（調査結果）

### Finding 1: isolated workspace は AGY の実際の設定ファイルパスを書き込んでいない（有力な原因候補）

`agy_permission_policy.materialize_isolated_agy_workspace()`（`agy_permission_policy.py`）は、
`tool_profile` に対応する isolated workspace の `HOME` を全く新しい空の一時ディレクトリへ
リダイレクトする（`env["HOME"] = str(workspace_dir)`）。このとき同関数が実際に書き込むファイルは:

- `<workspace>/.antigravity/settings.json`（`build_workspace_permission_policy()` が生成する
  独自スキーマ: `{"permissions": {"default": "deny", "allow": [...], "deny": [...]}}`）
- `<workspace>/.antigravity/workspace_deny_gate.py`（provenance-only hook のソース。実際には
  AGY の `PreToolCall` hook としては未配線 -- `generate_workspace_hook_config()` が別途
  `.agents/hooks.json` を生成する。詳細は Finding 2 参照）
- `<workspace>/xdg-config` / `xdg-cache` / `xdg-state`（空ディレクトリ）
- `<workspace>/.gemini/antigravity-cli/antigravity-oauth-token`（実 OAuth token file への
  symlink。Issue #1740/#1743 で追加）

一方、`references/agy-headless-tool-use-investigation.md`（#1749 の調査結果、AC1 セクション）は
公式ドキュメント（`https://antigravity.google/docs/cli/using`）の live WebFetch で
**AGY の実際の設定ファイルパスは `~/.gemini/antigravity-cli/settings.json` である**ことを
確認済みである。同ドキュメントの「未解決の副次的観察」節でも次の通り明記されている:

> `agy_permission_policy.py` の `build_workspace_permission_policy()` が生成する
> `.antigravity/settings.json` は、実際の AGY CLI が読む設定ファイルパス
> （`~/.gemini/antigravity-cli/settings.json`）ともスキーマ（`toolPermission` enum）とも異なる。

`materialize_isolated_agy_workspace()` は isolated `HOME` 配下に `.gemini/antigravity-cli/` を
作成する（OAuth token symlink のためだけに）が、その配下に **`settings.json` 自体は一切
配置しない**。つまり isolated workspace 内で `agy` を実行すると、`agy` は
`<isolated HOME>/.gemini/antigravity-cli/settings.json` を探すが存在しないため、
公式ドキュメントに記載の `toolPermission` フィールドのビルトインデフォルト値
（`"request-review"`: write/bash/web tool 実行前に確認を要求）にフォールバックすると
推定される。

`toolPermission: "request-review"` は非対話（headless print mode, `agy -p`）実行では
確認を求める相手がいないため、**確認待ち状態のまま無視されてツール呼び出し自体がスキップされ、
モデルが「検索した」体の応答テキストのみを返す**という、#1749 が「デフォルトモデルの
hallucination」として観測した症状と外形的に一致する挙動を isolated workspace が
model 種類に関わらず再現している可能性が高い。

対照的に、`_run_agy()` の非隔離フォールバック分岐（`tool_profile` が
`agy_permission_policy.ALLOWED_PROFILES` に含まれない場合、または直接/モック呼び出し）が使う
`_minimal_agy_env()` は呼び出し元プロセスの **実際の `$HOME`** をそのまま伝播する
（`os.environ.get("HOME")`）。開発者の実 `$HOME` には実際に運用中の
`~/.gemini/antigravity-cli/settings.json` が存在し、そこに設定された `toolPermission` 値
（確実性の高い表現に変えると、開発者が過去に `/permissions` TUI 操作で明示的に許可した `toolPermission` の値。詳細は本 Issue のスコープでは未確認）が
そのまま使われるため、非隔離環境では `search_web` / `read_url_content` の呼び出しが
正常に発生していたと考えられる。これは #1751 の live 再テストが isolated workspace
経由ではなかった可能性を裏付ける状況証拠と整合する。

**この仮説の限界**: 本 Issue のスコープでは live agy 実行による確認を行っていないため
（Stop Conditions 参照）、上記は静的解析から導出した最有力仮説であり、確定的な再現検証は
行っていない。

### Finding 2: `.agents/hooks.json` の配置先が AGY の実際の hook 探索パスと一致するか未確認

`agy_tool_provenance.generate_workspace_hook_config()` は `<workspace>/.agents/hooks.json` を
書き込む。この hook は provenance capture 専用（`{"decision": "allow"}` を常に返す）であり、
permission gate ではない設計である（`agy_tool_provenance.py` docstring 参照）。ただし、
`.agents/hooks.json` という配置パス自体が実際の AGY CLI の hook 探索パス（インストール済み
Antigravity CLI の `hooks.md` に記載のパス、または `~/.gemini/antigravity-cli/hooks.json` 等）と
一致するかどうかは、本 Issue の静的確認だけでは検証できなかった。仮に配置パスが
不一致であれば、`agy_provenance_hook_events` が常に空 list になる（hook 自体が一度も
発火しない）ことの説明にはなるが、それ自体は tool **呼び出し不発**（AGY 自身が
search_web を呼ばない）の直接原因ではなく、あくまで hook provenance 証跡が取得できない
という別の症状の説明候補である。

hook の matcher（`"search_web|read_url_content"`）自体は正規表現として妥当であり、
`timeout: 10`（秒）はワンライン JSON 追記のみを行うラッパースクリプトに対して
十分な値であるため、hook タイムアウトが tool 呼び出しをブロックしている可能性は低いと判断した。

### Finding 3: `PROFILE_ALLOWED_TOOLS` / `GROUNDED_RESEARCH_ALLOWLIST` 自体は静的には正しい

`agy_permission_policy.py` の `GROUNDED_RESEARCH_ALLOWLIST = frozenset({"search_web", "read_url_content"})`
および `PROFILE_ALLOWED_TOOLS[GROUNDED_RESEARCH_PROFILE]` の割り当ては、Issue #1705 の
AC 通りであり、`grounded_research` profile は `search_web` /
`read_url_content` の両方を許可リストに含んでいる。この自作スキーマ自体に
`search_web` / `read_url_content` を「禁止」してしまうような誤りは見つからなかった。
（ただし Finding 1 の通り、この自作スキーマ自体が AGY 本体に実際に consumed
されているかどうかは別問題として未検証のまま残っている。）

### Finding 4: gcloud ADC / OAuth token の read-only 露出は認証エラーの原因ではなさそう

Issue #1726 / #1730 / #1740 / #1743 で段階的に対処された dbus reachability・gcloud ADC・
agy OAuth token の isolated workspace への read-only 露出は、`agy_auth_required` 失敗を
解消する目的で導入されており、静的に見る限り正しく `<isolated HOME>/.gemini/antigravity-cli/antigravity-oauth-token`
へ配置されている。したがって #1494 6 回目試行での不発は認証エラー（`agy_auth_required`）
ではなく、認証は成功した上で tool 呼び出し自体がスキップされている可能性が高い
（`web_tool_call_count: 0` であり、`ok: false` の認証失敗ではなかったとの Issue 本文の記述と整合）。

## Next Action（次のアクション）

1. **最有力候補（Finding 1）の追加修正は本 Issue の Allowed Paths 外**: `materialize_isolated_agy_workspace()`
   が実際の `~/.gemini/antigravity-cli/settings.json` を isolated workspace の
   `<isolated HOME>/.gemini/antigravity-cli/settings.json` へ read-only 露出（#1740 の OAuth token
   symlink パターンと同様の手法）していないことが原因候補である。この修正は `agy_permission_policy.py`
   への変更を要するが、同ファイルは本 Issue の Allowed Paths に含まれない。follow-up Issue で
   以下を検証・実装することを推奨する:
   - 実 `$HOME/.gemini/antigravity-cli/settings.json` が存在する場合、`_expose_agy_oauth_token_read_only()`
     と同じ read-only symlink パターンで isolated workspace の `<isolated HOME>/.gemini/antigravity-cli/settings.json`
     へ露出する
   - 露出した場合でも、`toolPermission` の値がホストの実運用設定に依存してしまう
     （isolated workspace の「常に deny-by-default で明示 allow のみ許可」という設計意図と
     矛盾しうる）ため、`toolPermission: "always-proceed"` を isolated workspace 専用の
     `settings.json` として明示生成し、実 `$HOME` の設定を re-use しない方式（真の isolation を
     保ったまま headless tool 実行を許可する）を優先的に検討する
2. **live 再現検証が必要**: 本 Issue のスコープでは live agy 実行を行っていないため、
   Finding 1 の仮説が実際の不発原因であるかどうかは live 再現でのみ確定できる。
   follow-up Issue の実装後、isolated workspace 内で `agy -p <grounded_research prompt> --model
   claude-sonnet-4-6` を実行し、`agy_provenance_hook_events`（本 Issue の AC1-AC4 で
   `delegation_result/v1` へ配線済み）に `search_web` / `read_url_content` の
   `agy_tool_provenance_v1` イベントが実際に記録されることを確認する。
3. **Finding 2 の hook 探索パス検証も follow-up Issue で扱う**: インストール済み Antigravity CLI の
   `builtin/skills/agy-customizations/docs/hooks.md`（#1708 の調査で既に一度参照済み）を再読し、
   `.agents/hooks.json` が実際の hook 探索パスと一致することを確認する。一致しない場合は
   `generate_workspace_hook_config()` の配置先修正が必要になる（`agy_tool_provenance.py` も
   Allowed Paths 外のため、これも follow-up Issue 化する）。
4. **#1494 側の扱い**: #1494 本体の live E2E 再実行は本 Issue のスコープ外（Out of Scope 参照）。
   本 Issue の hermetic 修正（hook events の `delegation_result/v1` 配線）のマージ後、
   #1494 側で 7 回目の live E2E fan-out 試行を計画する場合は、上記 Next Action 1-3 の
   follow-up Issue が先に必要になる可能性が高い（Finding 1 が真の原因であれば、hook events
   配線だけでは grounded_research 自体の成功は解消しない）。

## 参考: 本 Issue で対処した hermetic 修正との関係

上記 Finding 1-4 はいずれも「isolated workspace 内で `search_web` / `read_url_content` が
実際に呼ばれるか」という問題であり、本 Issue の主目的である「`_normalize_agy_result()` が
`agy_provenance_hook_events` / `agy_provenance_hook_load_error` を `delegation_result/v1` へ
配線する」修正（AC1-AC4）とは独立している。hook events 配線自体は、tool 呼び出しが
成功した場合・失敗した場合のどちらでも正しく機能する（`_run_agy()` は tool 呼び出しの
成否に関わらず `_provenance/hook_events.jsonl` の読み取りを試みるため）。したがって、
本 Issue の修正は Finding 1-4 の根本原因解消を待たずに独立してマージ可能である。

## Finding 1 Live Verification（Issue #1758、live `agy -p` 実行による検証）

### AGY 公式ドキュメントの live 確認結果（AC1）

`https://antigravity.google/docs/cli/reference` および `https://antigravity.google/docs/cli/using`
を再度 live WebFetch し、`#1749`（`references/agy-headless-tool-use-investigation.md`）の記述を
再確認した。

- `toolPermission` の許容値は 4 値 enum: `"request-review"`（デフォルト） /
  `"proceed-in-sandbox"` / `"always-proceed"` / `"strict"`。
- 各値の意味（公式ドキュメント引用）:
  - `"request-review"`: "prompts for write/bash/web tools"（デフォルト）
  - `"proceed-in-sandbox"`: "auto-proceed inside sandbox"
  - `"always-proceed"`: "never prompts"
  - `"strict"`: "prompts for all non-read tools"
- 設定ファイルパスは `~/.gemini/antigravity-cli/settings.json`（"Stored in a plain JSON file
  `~/.gemini/antigravity-cli/settings.json`."）。XDG パスへの言及はない。
- 実機のホスト側 `~/.gemini/antigravity-cli/settings.json` を確認したところ、
  `toolPermission` キー自体が設定されておらず（`colorScheme` / `trustedWorkspaces` のみ）、
  ホスト環境も isolated workspace と同様にビルトインデフォルト `"request-review"` に
  フォールバックしていることが判明した。

### live `agy -p` 実行による仮説検証結果（AC2）

`materialize_isolated_agy_workspace()` が返す isolated `env`（`HOME`/`XDG_*` 隔離環境）と
`agy -p <prompt> --model claude-sonnet-4-6` を用いて、以下 4 パターンを live 実行で比較した
（`run_gemini_headless.py` の `_run_agy()` 経由、または同一 env 構成での直接呼び出し）。

| # | toolPermission 状態 | prompt 内容 | `web_tool_call_count`（hook集計） | 応答内に実在する `vertexaisearch.cloud.google.com/grounding-api-redirect/...` 引用URL |
|---|---|---|---|---|
| 1 | 未設定（デフォルト `request-review`、修正前 baseline） | 「`antigravity.google` の本日のトップ見出しを検索して」 | 0 | なし（ハルシネーション応答） |
| 2 | `always-proceed`（本 Issue の修正後） | 同上（#1と同一 prompt） | 0 | なし（ハルシネーション応答、#1と実質同一の応答内容） |
| 3 | 未設定（デフォルト `request-review`、修正前 baseline） | 「東京の現在の天気を検索して」 | 0 | **あり**（実在する grounding-api-redirect URL、実際の検索結果に基づく回答） |
| 4 | `always-proceed`（本 Issue の修正後） | 同上（#3と同一 prompt） | 0 | **あり**（実在する grounding-api-redirect URL） |

**結論: 仮説は誤り（refuted）**。`toolPermission` を明示 `"always-proceed"` に設定しても、
未設定（デフォルト `"request-review"`）と比較して `search_web` の実行可否に**観測可能な差は
一切なかった**（#1 と #2、#3 と #4 がそれぞれ同一の結果）。実際に検索が行われるかどうかは
`toolPermission` の値ではなく、**prompt の内容（モデルが「検索する価値がある」と判断するか
どうか）に依存していた**: `antigravity.google` はニュースサイトではなく「見出し」を持たない
という事実にモデルが気づくと、`toolPermission` の値に関わらず search_web を呼ばずに
その旨を回答する（#1494 6 回目試行の症状と外形的に一致するが、原因は tool permission の
denial ではなく、モデルが妥当な検索クエリだと判断しなかったこと）。一方、実在する具体的な
事実（東京の天気）を尋ねる prompt では、`toolPermission` の値に関わらず search_web が
確実に呼ばれ、`vertexaisearch.cloud.google.com/grounding-api-redirect/...` 形式の実在する
grounding citation URL を含む応答が返った。

**副次的な発見（重要）**: 上記 #3/#4 のように実際に search_web が呼ばれて grounding が
成功したケースであっても、`agy_provenance_hook_events` は**常に空 list のまま**であり
（`live_agy_run_web_tool_call_count` はホスト側 hook 集計としては常に 0）、
`grounded_research_evidence.grounding_status` は `attempted_no_web_tool_call` のまま
fail-closed 判定される。実際の tool 呼び出しが発生したことは、`parsed_evidence.url_scan`
（応答テキストから正規表現で抽出した実在 grounding URL）というフォールバック証跡でのみ
確認できた。これは Finding 2（`.agents/hooks.json` の hook 探索パス）と関連する別問題であり、
詳細は下記 `## Next Action Update (#1758)` を参照。

### 追加の live ドキュメント確認: hooks.json の探索パス（Finding 2 関連の追加情報）

`https://antigravity.google/docs/hooks` を live WebFetch した結果、hooks.json の想定配置先は
以下の 2 通りと明記されていた:

> "Hooks are configured in a `hooks.json` file located in your customization directory
> (e.g., `.agents/` in your workspace or `~/.gemini/config/`)."
（日本語訳: hooks.json はカスタマイズディレクトリ内に配置される。workspace 内では `.agents/`、ユーザーレベルでは `~/.gemini/config/` の 2 通りが公式に有効なパスとして明記されている。）

`agy_tool_provenance.generate_workspace_hook_config()` が書き込む `<workspace>/.agents/hooks.json`
というパス自体は、この公式記述の「workspace-level: `.agents/`」パターンと**一致している**
（Finding 2 が示唆した「パス不一致」という説明は、この確認だけでは裏付けられなかった）。
イベント名も `PreToolUse`（ドキュメント記載の配列キー名と一致）を使用しており、静的には
誤りが見当たらない。にもかかわらず live 実行で `agy_provenance_hook_events` が常に空になる
理由は、本 Issue のスコープ（`agy_tool_provenance.py` は Allowed Paths 外）では特定できな
かった。候補としては、hook 実行対象プロセスの CWD/HOME 判定条件、`agy` 側のプラグイン
hooks（`~/.gemini/antigravity-cli/plugins/<plugin_name>/hooks.json`）と workspace-level
hooks の優先順位・排他関係、または hook 自体は起動するがラッパースクリプトの書き込み
タイミングと `_run_agy()` の読み取りタイミングの競合、などが考えられるが、いずれも未検証。

## Next Action Update (#1758)（次のアクション更新）

Issue #1758 の live 検証により、Finding 1（`toolPermission` isolated workspace 未設定
仮説）は**誤りと確認された**。したがって、以下の通り方針を更新する。

1. **`toolPermission` 明示設定（`agy_permission_policy.py` の
   `_write_agy_tool_permission_settings()`）は root-cause fix ではないが、維持する**:
   live 検証では `search_web` の実行可否に対する効果は観測されなかったが、副作用もない
   （`.antigravity/settings.json` の deny-by-default allowlist が引き続き tool 呼び出し可否の
   唯一の権威であり、`toolPermission: "always-proceed"` は AGY 自身の冗長な確認ゲートを
   除去するだけで allowlist を広げない。回帰テストで確認済み）。将来 AGY のデフォルト値が
   変わった場合や、非対話環境でのみ確認待ちが発生する未検証の edge case に対する
   defense-in-depth として明示設定を残す。
2. **#1494 側の真の対応**: #1494 の live E2E fan-out で `web_tool_call_count: 0` が観測された
   場合、原因は `toolPermission` ではなく、(a) grounded_research subtask の prompt が
   「検索する価値のある具体的事実」を要求する形になっているか（`antigravity.google` の
   ような検索不能な対象を尋ねる prompt は避ける）、および (b) 後述の hook provenance
   capture の未解決ギャップ、の両方を疑うべきである。
3. **follow-up Issue #1768 で扱うべき事項（本 Issue のスコープ外、`agy_tool_provenance.py`
   が Allowed Paths 外のため）**: 実際に search_web / read_url_content が呼ばれた
   ケース（本ドキュメントの #3/#4 で確認済み）でも `agy_provenance_hook_events` が常に
   空になる問題。hooks.json のパス・イベント名自体は公式ドキュメントと一致することを
   本 Issue で確認済みのため、Finding 2 が想定した「パス不一致」ではなく、別の原因
   （hook 実行タイミング、plugin hooks との優先順位、または agy 側の未文書化の制約）を
   疑って再調査する必要がある。fail-closed evidence gate（#1708）が実際には成功している
   tool 呼び出しを `attempted_no_web_tool_call` として誤判定し続ける限り、#1494 の
   AC（grounded_research 成功の決定論的検証）は達成されない。
4. **#1494 側で 7 回目の live E2E fan-out 試行を計画する場合**: 上記 3 の follow-up Issue
   が先に必要になる可能性が高い（tool 呼び出し自体は成功しうるが、evidence gate が
   それを正しく検出できない限り validator が pass しないため）。
