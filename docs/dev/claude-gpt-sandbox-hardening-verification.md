# claude-gpt launcher sandbox hardening 既定有効化可否 実機検証記録

> 関連: Issue #2173（本検証）、Issue #2158 / PR #2162（`scripts/claude-gpt/` launcher 本体）、
> Parent #2154。OWNER 判断（PR #2162 コメント, 2026-08-14T15:01:36Z）により、本検証は
> `#2162` の pre-merge security gate として扱われる。
>
> 検証実施日: 2026-08-14〜2026-08-15（JST）。実行環境: WSL2（`DESKTOP-TB4VBD9`,
> Linux 6.6.87.2-microsoft-standard-WSL2）上で稼働する Claude Code SubAgent セッション
> （本ドキュメント自体を作成した実装セッション）。`claude 2.1.232` / `claude-code-proxy 0.1.34`。

## 検証対象

`scripts/claude-gpt/launch.sh` の `CLAUDE_GPT_HARDENED_SANDBOX` opt-in フラグが有効化する
2 つの hardening 設定について、実機で破壊再現の有無を切り分けた。

1. `sandbox.enabled: true`（Claude Code session 設定。bubblewrap ベースの OS レベル sandbox）
2. `CLAUDE_CODE_SUBPROCESS_ENV_SCRUB=1`（環境変数。Bash tool 起動サブプロセスへの env 継承を絞る）

## 安全上の実施方法

Issue #2173 の Stop Condition（「ネストした sandbox 実行環境の入れ子構成を実機で操作する際に、
実行中セッション自体の安定性に影響する変更を加える前に必ず一時停止する」）に従い、破壊的検証は
すべて **本実装セッション自身の Bash tool とは完全に別プロセスの、使い捨て claude-gpt launcher
プロセス**（`timeout` でラップした背景実行の子プロセスツリー）として実施した。各破壊的検証の
前後で本セッション自身の Bash tool（`echo` 等の無害なコマンド）が正常応答することを毎回確認した
（`SELF_SESSION_SANITY_CHECK_*_OK` marker、計 6 回）。本セッション自身が異常を来す兆候は
一度も観測されなかった。

## 実行環境がネストしているかどうかの判定

本検証セッション自身の Bash tool 実行プロセスを検査した結果:

- `/proc/self/status` の `Seccomp` フィールドが `2`（`SECCOMP_MODE_FILTER`、フィルタ適用中）
  であることを確認した。これは Claude Code 自身の Bash tool サンドボックス機構が既に適用されて
  いることを示唆する。
- 一方 `/proc/self/mountinfo` の内容は素の WSL2 root filesystem のマウント構成（ext4 root +
  標準 `/dev` `/proc` `/sys` 系マウント、43 行）であり、bwrap 特有の pivot-root / private
  overlay 構成ではなかった。
- `bwrap --ro-bind / / --dev /dev --proc /proc --unshare-all --die-with-parent echo bwrap_ok`
  による単体 self-test は成功した（`claude_gpt_check_sandbox_init()` と同等の chk）。

**結論**: 本セッションが bwrap namespace で完全に入れ子になっているとは断定できないが、Claude
Code 自身の Bash tool 実行に seccomp フィルタが既に適用されている環境である。この状態から
launch.sh 経由で起動した子 `claude -p` プロセスが自身の Bash tool 用にさらに `sandbox.enabled`
/ `CLAUDE_CODE_SUBPROCESS_ENV_SCRUB` を有効化しようとすると、後述の通り確実に破壊が再現した。
これは「非ネスト環境」を明示的に用意できなかったことを意味し、AC1 の限界として下記に記録する。

## AC1: 非ネスト環境での `CLAUDE_GPT_HARDENED_SANDBOX=true` 正常応答確認

**結果: 実施不可（環境の制約）。**

本検証を実施可能な実行環境（本 SubAgent セッションが動作する WSL2 開発機）は、上記の通り
Claude Code 自身の Bash tool に既に seccomp フィルタが適用されている状態であり、これを「非
ネスト環境」として扱うことはできない。本プロジェクトの Claude Code エージェント実行系統
（このリポジトリの実装・レビュー・SubAgent が実際に動作する環境）でこれ以外の実行系統を
用意することは本 Issue のスコープ外（別ホスト・別 CI ランナーの調達が必要）であり、Stop
Condition の「外部サービス利用・権限昇格・...が必要になった場合」に該当しうるため、これ以上の
環境調達は行わなかった。

参考として、AC2 / AC3 で使用したものと同一の launcher 経路を `CLAUDE_GPT_HARDENED_SANDBOX`
**無効**（既定値）で実行した場合は、以下の通り genuine PASS を確認済みである（構造確認 +
対話 runtime 確認の両方、`http_post_v1_messages_confirmed: true`）。

証跡: `scripts/claude-gpt/.evidence/smoke-20260814T151543Z.json`

```json
{
  "schema": "CLAUDE_GPT_SMOKE_RESULT_V1",
  "status": "pass",
  "runtime_conversation_check": {
    "ok": true,
    "text_marker_ok": true,
    "bash_tool_marker_ok": true,
    "subagent_marker_ok": true,
    "http_post_v1_messages_confirmed": true,
    "model_used": "gpt-5.6-terra",
    "provider_used": "codex"
  }
}
```

これは「非ネスト環境での `CLAUDE_GPT_HARDENED_SANDBOX=true` 成功」の直接証拠にはならないが、
launcher 自体（proxy 起動・model alias・MCP 除外・cleanup）が hardening 無効時には安定して
機能していることの裏付けとして記録する。

## AC2: ネスト環境での `sandbox.enabled: true` 破壊再現確認

**結果: 再現した（2/2 試行）。**

`CLAUDE_GPT_HARDENED_SANDBOX=true` を指定して `scripts/claude-gpt/runtime_smoke_test.sh`
（Bash tool / Task tool / plain text の 3 conversation step、Bash step は最大 3 回の
bounded retry）を `timeout 280` でラップして 2 回実行した。

- 1 回目: 280 秒の外側 timeout で強制終了（exit 124）。stdout / stderr とも完全に空
  （3 conversation step のうち最初の 1 つも完了しなかった）。
- 2 回目: 同条件で再現。実行中に子プロセス（`claude -p ... --allowedTools "Bash(echo *)"`）を
  `ps` / `/proc/<pid>/status` で直接観測したところ、`State: R (running)`、`Threads: 19`、
  経過 83 秒に対し CPU 時間 1:28（100% 超、複数コアにまたがるビジー状態）であり、応答を返さず
  スピンし続けていることを確認した。`/proc/<pid>/environ` から `CLAUDE_GPT_HARDENED_SANDBOX=true`
  `CLAUDE_CODE_SUBPROCESS_ENV_SCRUB=1` が実際に当該プロセスへ渡っていたことも確認した。
  最終的に外側 `timeout 280` によって強制終了された（exit 124）。強制終了後、
  `claude-code-proxy` / `claude` の残留プロセスがないことを `ps -ef` で確認した（cleanup 自体は
  outer timeout 経由で最終的に機能した）。

対照として、`CLAUDE_GPT_HARDENED_SANDBOX` を指定しない既定実行（AC1 参照）は同一
`runtime_smoke_test.sh` が約 30 秒で 3 conversation step すべてを完了し genuine PASS した。
このコントラストにより、遅延の原因が同時実行中の他プロセスによる資源競合ではなく
`sandbox.enabled: true` そのものであることを支持する。

PR #2162 実装セッション（2026-08-14）で報告されていた `Bash tool error: Maximum call stack
size exceeded` という明示的なエラーメッセージそのものは今回は観測されなかった。今回観測された
実際の症状は「エラーで終了する」のではなく「応答を一切返さずビジーループし続け、外側 timeout
でしか停止しない」というものであり、エラーメッセージとして表面化しない分、検出がより困難で
実害が大きい破壊パターンであることが判明した（バージョン間差異、または V8 スタックオーバーフロー
の再試行がリトライループとして現れているなど、根本原因の詳細切り分けは本 Issue のスコープ外）。

## AC3: ネスト環境での `CLAUDE_CODE_SUBPROCESS_ENV_SCRUB=1` 単独有効化の破壊再現確認

**結果: 再現した（1/1 試行、`sandbox.enabled` を伴わない独立検証）。**

`launch.sh` の現行実装は `sandbox.enabled` と `CLAUDE_CODE_SUBPROCESS_ENV_SCRUB` を同一の
`CLAUDE_GPT_HARDENED_SANDBOX` フラグで同時に opt-in する構成であり、`CLAUDE_CODE_SUBPROCESS_ENV_SCRUB`
のみを独立に有効化する入口が存在しない。そのため本 AC は `launch.sh` を経由せず、同等の
proxy（`claude-code-proxy serve`、専用 HOME/CCP_CONFIG_DIR で launch.sh と同一構成）を手動で
起動した上で、`claude -p` を `--settings`（`sandbox` キーを含まない、`Read` deny のみの
`settings.local.json`）と `CLAUDE_CODE_SUBPROCESS_ENV_SCRUB=1` のみを設定した環境変数で
直接起動する形で切り分けた（`--strict-mcp-config` / `--mcp-config` / `--allowedTools` は
launch.sh と同一設定）。

結果は AC2 と同一パターンであった。`timeout 90` でラップして実行したところ、子 `claude -p`
プロセスは `State: R`、CPU 100%（`etimes` に対し `TIME` が上回る）でスピンし続け、90 秒の
外側 timeout で強制終了された（exit 124）。stdout は完全に空、stderr には
`"gpt-5.6-terra" is not a model this version of Claude Code recognizes`（context window
推定に関する非致命的な warning。auto-compact の想定 context window 判定用であり本破壊とは
無関係）以外に有意な出力はなかった。強制終了後、残留プロセスがないことを確認した。

**結論**: `sandbox.enabled: true` を伴わずとも、`CLAUDE_CODE_SUBPROCESS_ENV_SCRUB=1` 単独で
同じ破壊（応答なしのビジーループ）が再現する。両者は独立した破壊要因であり、どちらか一方だけを
既定有効化しても安全ではない。

## AC4: `CLAUDE_GPT_HARDENED_SANDBOX` 既定値の判断

**判断: 既定 opt-in 無効を維持する（切り替えない）。**

**根拠**:

- AC2（`sandbox.enabled: true`）・AC3（`CLAUDE_CODE_SUBPROCESS_ENV_SCRUB=1` 単独）のいずれも、
  本検証で利用可能な実行環境において 100% の再現率（2/2, 1/1）で Bash tool 呼び出しを
  完全に無応答化させることを実機確認した。
- AC1（非ネスト環境での成功確認）は、本プロジェクトの実際の Claude Code エージェント実行環境
  そのものが検証時点で「非ネスト」と言い切れない特性（Bash tool 自体への seccomp フィルタ適用）
  を持つため実施できなかった。つまり、本プロジェクトの実際の開発・エージェント実行環境において
  hardening が安全に機能することを示す前向きな証拠は得られていない。
- Bash tool は Claude Code の中核機能であり、これを既定で破壊しうる（しかもエラーではなく
  無応答のビジーループとして症状が出るため検出が困難な）設定を既定有効化することは、
  `credential isolation を強化する` という本来の目的に対して割に合わないリスクである。

したがって `scripts/claude-gpt/launch.sh` / `scripts/claude-gpt/lib.sh` / `scripts/claude-gpt/preflight.sh`
の既存の opt-in 構造（`CLAUDE_GPT_HARDENED_SANDBOX=true` を明示指定した場合のみ有効化、既定は
無効）はそのまま維持する。該当箇所のコメントを本検証結果への参照を含めて更新した（実装差分は
コメント更新のみ。動作は変更していない）。

**OWNER への申し送り**: PR #2162 コメント（2026-08-14T15:01:36Z）の OWNER 判断は、本検証で
「任意 subprocess からの credential isolation」という P0-3 security boundary を解決するか、
OWNER が明示的に reduced security contract を受容するかのいずれかを求めていた。本検証の結果は
前者（解決）を達成できなかったことを示す。したがって後者（OWNER によるリスク受容判断、または
別の緩和策の指示）が必要な状態である。現時点で launcher が提供する credential isolation は、
`Read(//...)` deny rule によるファイルアクセス制限と、proxy 専用 HOME による credential 分離
（いずれも実機確認済み・PR #2162 P0-2/P0-3 参照）であり、`sandbox.enabled` による OS レベルの
任意 subprocess 隔離は提供できていない。

## AC5: upstream（Anthropic）への報告要否

**判断: 報告を推奨する。文面案は以下の通り（実提出は本 Issue のスコープ外）。**

観測された症状は、単なるエラー終了ではなく「応答を一切返さずビジーループし続ける」という点で
利用者にとって診断が難しく、`sandbox.enabled: true` および `CLAUDE_CODE_SUBPROCESS_ENV_SCRUB=1`
のドキュメントにも「Claude Code 自身の Bash tool 実行が既に何らかのサンドボックス機構下にある
場合の既知の非互換」についての記載が見当たらない。再現条件が比較的単純（ネストした実行環境から
`claude -p` を起動し、`--settings` で `sandbox.enabled: true` または環境変数
`CLAUDE_CODE_SUBPROCESS_ENV_SCRUB=1` を設定するだけ）であるため、upstream に報告する価値が
あると判断する。

### 報告文面案（anthropics/claude-code 相当への Issue 起票用ドラフト）

```markdown
Title: sandbox.enabled / CLAUDE_CODE_SUBPROCESS_ENV_SCRUB=1 hang (not error) when Claude Code
itself already runs under a sandboxed/seccomp-restricted Bash tool execution context

## Summary
When Claude Code's own Bash tool execution is already running under a seccomp-filtered /
sandboxed process (observed via `/proc/self/status` `Seccomp: 2` on the invoking shell), starting
a child `claude -p` process with either `sandbox.enabled: true` (session settings) or the
environment variable `CLAUDE_CODE_SUBPROCESS_ENV_SCRUB=1` causes any subsequent Bash tool call
inside that child process to hang indefinitely rather than error out. The child process is
observed at ~100%+ CPU (multiple threads, `R` state) with zero stdout output until forcibly
killed by an external timeout.

## Environment
- Claude Code: 2.1.232
- Host: WSL2 (Linux 6.6.87.2-microsoft-standard-WSL2)
- Outer execution context: a Claude Code Bash tool subprocess (itself seccomp-filtered per
  `/proc/self/status`)

## Repro
1. From within a Claude Code Bash tool session (itself already sandboxed), start a child process:
   `claude --settings settings.json -p "Use the Bash tool to run: echo marker" --allowedTools
   "Bash(echo *)"` where `settings.json` contains `"sandbox": {"enabled": true}`.
2. Observe: no output for the invocation's entire lifetime; the child process consumes ~100%+ CPU
   across multiple threads; the process never returns until killed externally.
3. Same result if `sandbox.enabled` is omitted but `CLAUDE_CODE_SUBPROCESS_ENV_SCRUB=1` is set in
   the child process's environment instead (independently reproducible, not requiring
   `sandbox.enabled`).
4. Without either setting, the same invocation completes normally in ~10-30s with a genuine tool
   response.

## Expected
Either a clear, fast error (rather than an indefinite hang) when this nested-sandbox
incompatibility is detected, or graceful degradation that still allows Bash tool calls to
complete.

## Additional context
A simple `bwrap` self-test (`bwrap --ro-bind / / --dev /dev --proc /proc --unshare-all
--die-with-parent true`) succeeds in the same outer environment, so a naive sandbox-init
preflight check does not detect this incompatibility.
```

## AC6: `preflight.sh` / `runtime_smoke_test.sh` の正常終了確認

本 Issue の変更（コメント更新のみ、動作変更なし）後、以下を確認した。

- `bash -n scripts/claude-gpt/launch.sh` / `bash -n scripts/claude-gpt/lib.sh` /
  `bash -n scripts/claude-gpt/preflight.sh` / `bash -n scripts/claude-gpt/runtime_smoke_test.sh`
  — いずれも構文エラーなし。
- `scripts/claude-gpt/preflight.sh --env-only` — 実行し `exit_code: 0`、
  `binary_available: true`、`chatgpt_auth.available: true` を確認した。
- `scripts/claude-gpt/runtime_smoke_test.sh`（`CLAUDE_GPT_HARDENED_SANDBOX` 既定 = 未指定）
  — genuine PASS（証跡: `scripts/claude-gpt/.evidence/smoke-20260814T151543Z.json`、
  約 30 秒で完了、構造確認 + 対話 runtime 確認の両方が `ok: true`）。

## まとめ

| AC | 結果 |
|---|---|
| AC1 | 実施不可（本プロジェクトの実行環境自体が非ネストと断定できないため）。既定無効時の launcher 自体の正常動作は別途確認済み |
| AC2 | 再現した（2/2）。`sandbox.enabled: true` は Bash tool を無応答のビジーループへ陥らせる |
| AC3 | 再現した（1/1）。`CLAUDE_CODE_SUBPROCESS_ENV_SCRUB=1` は `sandbox.enabled` 非依存で単独再現する |
| AC4 | `CLAUDE_GPT_HARDENED_SANDBOX` 既定 opt-in 無効を維持（切り替えない）。根拠は上記の通り |
| AC5 | upstream 報告を推奨。文面案は上記の通り（実提出は scope 外） |
| AC6 | `preflight.sh` / `runtime_smoke_test.sh` とも変更後も正常終了することを確認した |
