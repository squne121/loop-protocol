---
issue: 1752
parent_issue: 1265
related_issue: 1494
status: static_investigation_complete_fix_out_of_scope
last_updated: 2026-07-25
note: "本ドキュメントは Issue 1752 の isolated workspace 内 grounded_research tool 呼び出し不発の静的再調査結果を記録する。Stop Conditions により live agy 実行は本 Issue のスコープでは行っていない。"
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
