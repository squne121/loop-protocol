---
name: worktree-agent-runtime-smoke
description: linked worktree 内で Claude Code の fresh runtime を起動し、structured output（既定・常に direct subprocess）または herdr interactive lane（TUI 固有挙動が必要な場合のみ・常に人間の Herdr session とは分離した isolated named session）で観測し、allowlist-only summary evidence を worktree-local に保存する共有 Skill（native Codex CLI lane は Issue #2161 で撤去済み）。「runtime smoke」「動作検証を実行」「Claude を worktree で起動して確認」のトリガーで使う。
---

# Worktree Agent Runtime Smoke

Claude Code について、linked worktree 内で fresh session を起動し、
structured event（既定・常に direct subprocess）または herdr pane observation
（TUI 固有挙動が必要な場合・常に呼び出し元とは分離した isolated named herdr session）で
runtime evidence を収集する。semantic verdict（hook reason 分類、mutation deny 妥当性、
Skill preload 判定、context budget 評価、review verdict、merge readiness）は callerの責務。

## Trigger（使用条件）

- Issue の `## Runtime Verification Applicability` が `decision: immediate` で、
  Claude Code の実 process / TUI 起動証跡が必要な場合
- pr-review-judge や実装 worker が runtime postcondition（hook lifecycle、session 非永続化、
  worktree cwd binding）を証明する必要がある場合

## Non-trigger（使用しない場合）

- 静的検証（typecheck / lint / test / build）だけで AC を満たせる場合
- semantic な hook reason 分類・mutation deny 妥当性判定・context budget 数値評価を行う場合
  （本 Skill は runtime 起動・観測・証跡収集だけを所有する）
- worktree の新規作成／削除、自動 Issue／PR mutation、自動 approval

## Input（入力）

- `--runtime claude`（native Codex CLI lane は Issue #2161 で撤去済み）
- `--mode structured|interactive`
- `--worktree <linked worktree の絶対パス>`
- `--prompt-file <検証用 prompt ファイル>`（raw prompt を argument interpolation しない）
- `--output-dir <evidence 出力先。既定は worktree 配下の untracked ディレクトリ。排他的作成（既存ディレクトリ／symlink は拒否）>`
- `--timeout-seconds <bounded timeout>`
- `--max-turns <Claude Code の bounded turn 数。既定 30>`
- `--expect-marker <literal>`（repeatable、任意）
- `--expect-ordered-marker <literal>`（repeatable、任意。Issue #2498 AC1。`--expect-marker` とは独立した別引数で、指定しても SubAgent causal-evidence 既定強制を一切トリガーしない。`--mode structured` 限定。caller が宣言した順序どおりの expected-evidence-marker list を、`evaluate_ordered_evidence_match()`（cursor を前進させながら各マーカーを `str.find(marker, cursor)` で順に探す、決定論的な sequential subsequence match）で照合する。`skill-invocation-runtime-smoke` profile の `procedure_steps_executed_in_declared_order` assertion に対応する。runner 自身は任意の SKILL.md の Markdown Procedure を汎用解釈しない — 期待順序リストは caller が明示的に用意する。**照合対象テキストは `extract_claude_main_output_text()`（`type: "assistant"` イベント自身のテキストのみを emission 順に連結したもの）であり、captured stdout の生ブロブ全体ではない（PR #2500 fix_delta P1-2、OWNER REQUEST_CHANGES https://github.com/squne121/loop-protocol/pull/2500#issuecomment-5549720805）** — 終端 `type: "result"` イベントの `result` フィールドは同じ最終応答テキストの replay に過ぎないため意図的に除外する（含めると、同一の最終回答が実際には逆順であっても、assistant イベント自身の occurrence と result イベントの重複 replay の occurrence をまたいで forward-order と誤判定しうる）
- `--output-schema-path <path>`（任意。Issue #2498 AC2。`--mode structured` 限定。caller が指定する既存の単一 canonical JSON Schema ファイル（例: `.claude/skills/review-issue/schemas/review_issue_result_v1.json`）に対し、structured lane の最終 `type: "result"` イベントの `result` フィールド（native の最終応答テキスト）から回収した JSON payload を `jsonschema.validate()` で full schema validation する（`evaluate_output_contract_schema_fields_present()`）。required-key existence のみの独自チェッカーではなく、runner に generic multi-domain schema registry も追加しない。`skill-invocation-runtime-smoke` profile の `output_contract_schema_fields_present` assertion に対応する。**JSON payload の抽出は決定論的（PR #2500 fix_delta P2-2）**: (1) 最終応答テキスト全体が bare JSON object であるか、(2) ` ```json ` フェンス付き Markdown コードブロックが厳密に 1 個だけ含まれる場合のみ、そのブロックの中身を JSON として解釈する — 複数候補をスキーマ検証に通してどれか一つを採用するような曖昧な方式は取らない。**加えて、スキーマファイル自体が構造的に不正な場合（PR #2500 fix_delta P2-3）は runtime 起動前の引数検証段階で `jsonschema.validators.validator_for(...).check_schema()` により検出し、usage error として即座に拒否する** — 起動済みの runtime プロセスが完了した後に `jsonschema.exceptions.SchemaError` の未捕捉例外で落ちることはない）
- `--expect-marker-source main|subagent`（任意。既定 `subagent`。Issue #2498 AC3。`--expect-marker` 使用時の SubAgent causal-evidence 既定強制（Issue #2183 fix-delta）に対する additive な provenance 入力。`subagent`（既定）: 省略時の暗黙強制と完全に同一 — `--expect-marker` を指定する構造化 run は引き続き `causal_evidence_source == hook_id_correlated` を要求され、マーカー探索対象テキストも従来どおり captured stdout+stderr の結合ブロブ全体のまま変化しない。`main`: SubAgent 委譲のない direct Skill invocation を表明し、causal-evidence gate から opt out する。**加えて（PR #2500 fix_delta P1-1、OWNER REQUEST_CHANGES https://github.com/squne121/loop-protocol/pull/2500#issuecomment-5549720805）、`main` 選択時のみマーカー探索対象テキストを `extract_claude_main_output_text()`（`type: "assistant"` イベント自身の `message.content[].text` のみを emission 順に連結したもの）に限定する** — caller が prompt に書いた文字列（`UserPromptExpansion` hook 自身の stdin echo に含まれる `prompt`/`command_args` 等）や hook echo、stderr は一切含まない。これにより caller が prompt に書いただけの文字列がモデル自身の出力を経ずに marker 判定を satisfy することを防ぐ。`--expect-skill-command` との併用が**必須**（併用しない `main` 単独指定は起動前の usage error（parser.error）として拒否される — 無条件の causal-evidence opt-out に退化させないため）。`--require-subagent-causal-evidence`（明示的な独立要求）はこのフラグの値に関わらず有効のまま）
- `--expect-skill-command <name>`（任意。Issue #2498 AC4。`--expect-marker-source main` との必須ペア。`--mode structured` + `--claude-adapter native` 限定。native `UserPromptExpansion` hook イベント（実機 Claude Code 2.1.261 で確認済み: `command:"cat"` hook がその hook 自身の stdin payload — `command_name`/`command_args`/`command_source` を含む — を echo する）の `command_name` フィールド一致によって direct Skill/slash-command invocation の発生を検証する（`extract_claude_user_prompt_expansion_command_names()`）。`command_source` の値域は公式ドキュメント上で列挙されていないため判定根拠にしない。指定時は structured lane の `--settings` に `UserPromptExpansion` hook 登録を追加した拡張版 JSON（`_CLAUDE_SPAWN_HOOK_OBSERVABILITY_WITH_USER_PROMPT_EXPANSION_SETTINGS_JSON`）を使う — 未指定の既存呼び出しは元の `_CLAUDE_SPAWN_HOOK_OBSERVABILITY_SETTINGS_JSON`（`{"SubagentStart", "SubagentStop"}` のみ）のまま変化しない）
- `--require-clean-postcondition`（任意）
- `--require-hook-chain-evidence`（任意。既定 off。`--runtime claude --mode structured --claude-adapter native` 限定。Issue #2663。generic hook-chain evidence capability — 詳細は下記「Generic Hook-Chain Evidence（AC2/AC3、Issue #2663）」節を参照)
- `--task-context-scope <value>` / `--task-context-state-root <absolute path>`（任意。既定 None。Issue #2568 In Scope。structured/interactive 両 lane で、子 runtime プロセス／isolated herdr session へ `LOOP_TASK_CONTEXT_SCOPE`/`LOOP_TASK_CONTEXT_STATE_ROOT` を verbatim で additive に forward する env/carrier passthrough のみ。本 runner はこの値を一切解釈・検証しない — Task Context 固有の semantic（`task-contextctl smoke seed` の scope gate 等）は `scripts/task-context/task_contextctl.py` / `scripts/task-context/task_context_runtime_smoke_verifier.py` 側の責務であり、本 runner には一切埋め込まない。**Issue #2744 normative MUST**: Task Context 自身の runtime 検証を行う場合、caller は本 runner をこれら 2 フラグ付きで direct に unscoped 起動してはならない。generic runner を直接 unscoped で呼ばず、必ず canonical entrypoint（`scripts/task-context/task_context_runtime_smoke_verifier.py::orchestrate_runtime_smoke()`）を経由すること。`orchestrate_runtime_smoke()` は本 runner を呼び出す唯一の canonical 呼び出し元であり、常に `--task-context-scope runtime_smoke` と run-scoped isolated `--task-context-state-root` の両方を自動で付与する。これは、Task Context の acceptance/runtime-verification 手順が本 canonical entrypoint を経由せず generic runner を直接 unscoped で起動した結果、production canonical Task Context DB へ余分な行が書き込まれた実際の事故（Issue #2569、#2570 AC8 検証時に発見）の再発を防ぐための誤用防止規定である）
- `--inspect-session-log-metadata` / `--require-session-log-metadata`（任意。既定では session log を読まない）
- `--agent-type <persona 名>`（任意。static declaration。CLI へ forward しない）
- `--claude-agent-name <persona 名>`（任意。claude runtime + structured mode 限定。実際に `--agent <name>` として CLI へ forward し、main-session identity（`main_agent_identity`）・candidate Agent definition binding（`agent_definition`）・Skill evidence（`skill_evidence`）の evidence source になる。Issue #2046）
- `--hermetic-agent-definition`（任意。`--claude-agent-name` 併用必須。project-discovery の `--agent <name>` lookup ではなく、candidate Agent 定義から決定論的に生成した session-local `--agents` JSON payload（tools は Read のみ固定）と session-local `--settings`（mutation-capable tool を deny）で起動する hermetic no-mutation lane。Issue #2046）
- `--claude-bin <absolute path>`（任意。`--runtime claude` 限定。claude 互換の実行ファイル（例: `scripts/claude-gpt/launch.sh` launcher）の絶対パスを明示指定する。指定時は `shutil.which("claude")` による PATH 解決を bypass し、structured lane はその絶対パスを固定 argv の実行ファイルとして直接使用する。interactive herdr lane では、herdr 自身が `--kind claude` の実行ファイルを常に自分の PATH lookup で再解決するため、isolated session 専用の一時ディレクトリに `claude` という名前の forwarder script（symlink ではない。`exec '<絶対パス>' "$@"` で実際の launcher を exec するシェルスクリプト。symlink だと `$0` が symlink 自身のパスになり、sibling `lib.sh` を `dirname -- "$0"` で source する launcher が壊れるため。PR #2176 OWNER REQUEST_CHANGES Finding 2）を生成し、`herdr workspace create --env PATH=<shim-dir>:<既存PATH>` で Herdr 自身のサーバ／root shell プロセスへ明示的に渡す（Python クライアント側の `PATH` を更新するだけでは Herdr サーバの PTY プロセスへ届かないため。Finding 1）。forwarder は起動直前に run-scoped nonce を 0600 の receipt ファイルへ書き込み、runner はその nonce を run 終了後に readback して指定 launcher が実際に実行されたことを検証する（ambient PATH 上の別 `claude` が実行された場合は receipt が観測できず FAIL する）。未指定時（既定）は既存の `shutil.which("claude")` PATH 解決が変更なく維持される（Issue #2174）。
- `--claude-adapter native|claude-gpt`（任意。既定値 `native`。Issue #2174 AC1 fix_delta、OWNER REQUEST_CHANGES https://github.com/squne121/loop-protocol/issues/2174#issuecomment-5302215173 で追加。`--claude-bin` とは独立した明示入力であり、`bool(--claude-bin)` から launcher 固有挙動を暗黙適用しない。`native`（既定）: `--claude-bin`（指定時）は純粋な binary path override として扱われ、PATH 解決時と同一の固定 argv（`--settings <hook-observability-json>` を含む）で起動する。`claude-gpt`: `--claude-bin` の指定が必須（未指定なら起動前に blocked）。`scripts/claude-gpt/launch.sh` 自身の CLI 契約（自身のオプションの後に literal `--` separator、以降は claude 本体へ素通し）に従い、structured lane の固定 argv 先頭に `--` を挿入し、`--settings <JSON>` の代わりに `CLAUDE_GPT_RUNTIME_SMOKE_HOOKS=subagent-start-stop` 環境変数を設定する（launcher が `--settings` を含む policy-weakening flag を拒否するため）。launcher 自身の `CLAUDE_GPT_LAUNCH_RESULT_V1` 受理/拒否 receipt は evidence の `claude_gpt_launcher_receipt` としてそのまま記録される（Issue #2174 AC8）。claude 以外の `--runtime` 値は argparse 時点で拒否される（PR #2176 OWNER REQUEST_CHANGES Finding 6。native Codex CLI lane は Issue #2161 で撤去済み）。
- `--require-min-subagents <int>`（任意。既定 0 = 未要求。`--runtime claude` 限定。structured lane と interactive lane の両方に適用される。0 より大きい値を指定すると、最低この個数の DISTINCT SubAgent が spawn かつ completion まで agent_id exact pairing で確認されるまで run が FAIL する。structured lane は stdout を、interactive lane は永続化された session transcript を evidence source として同じ `classify_claude_multi_child_lifecycle()` で判定する。Issue #2219 AC3）
- `--require-min-turns <int>`（任意。既定 0 = 未要求。`--runtime claude` 限定。structured lane と interactive lane の両方に適用される。0 より大きい値を指定すると、SAME main session_id が最低このturn 数持続することを要求する。structured lane では単一 invocation 内で `--max-turns` がこの値未満だと `parser.error`（起動前拒否）になる。interactive lane では 1（初回 turn）+ `--additional-prompt` の指定数がこの値未満だと `parser.error` になる。Issue #2219 AC2/AC11、fix_delta iteration 1）
- `--scan-forbidden-markers`（任意。既定 off。`--runtime claude` 限定。structured lane と interactive lane の両方に適用される。`403 WebSocket upgrade` / `WebSocket upgrade was rejected` / `Please run /login` / `early termination` / `context limit` / `auto-compaction failure` の固定 literal allowlist を、structured lane は stdout/stderr から、interactive lane は永続化された session transcript と bounded pane 抜粋から scan し、1 件でも観測されれば FAIL する。Issue #2219 AC6、fix_delta iteration 1）
- `--additional-prompt <prompt>`（任意、repeatable。既定なし。`--mode interactive` かつ `--runtime claude` 限定。指定順に、既に起動済みの SAME herdr agent/session へ追加の prompt turn を送信する。PR #2176 commit 06d8baa9 が prototype し commit 5a44ebf0 で Issue #2174 のスコープ外として revert された `--additional-prompt` 相当の再実装（Issue #2219 fix_delta iteration 1、選択肢 B）。`--require-min-turns` / `--require-min-subagents` / `--scan-forbidden-markers` と組み合わせることで、この lane 自身が書き出す 永続化 session transcript（adapter 固有の projects root（native: `~/.claude/projects`、claude-gpt:
`$CLAUDE_GPT_HOME/claude/projects`。fix_delta iteration 2）配下の
`*/<session_id>.jsonl` -- interactive lane は `--no-session-persistence` を forward
しないため実際に書かれる）を evidence source として同一 session identity・複数
SubAgent lifecycle・forbidden marker 不在を検証できる）

通常の `--mode interactive` は、高エントロピーな isolated named session を新規生成し、ambient `HERDR_*` を strip して、その session だけを start／stop／delete する。pre-existing/human Herdr namespace を enumerate、list、snapshot、operate、message、observe しない。既存の `--require-session-baseline-preservation` は唯一の明示 opt-in であり、指定時だけ before/after の session identity と full workspace/agent/focus snapshot を取得して fail-closed で比較する。`preexisting_herdr_preserved` はこの opt-in の observed evidence に限り、通常実行では unavailable (`null`) のままにする。

`--transport` と `--keep-pane` は存在しない（PR #1921 human OWNER fix-delta）。structured lane は
常に direct subprocess、interactive lane は常に isolated named herdr session であり、
呼び出し元は transport を選択できない。検証 session は常に cleanup 対象であり、残す
オプションは提供しない。

## Invocation-local Claude peer policy（呼び出し単位の peer 方針、Issue #2437）

native Claude の structured / interactive lane は、harness が所有する invocation-local
settings に常に次を固定する。global `.claude/settings*`、`~/.claude`、既存 Herdr
namespace は変更しない。

```json
{
  "crossSessionInbound": "refuse",
  "permissions": {"deny": ["SendMessage", "ListAgents"]}
}
```

- structured lane は既存 direct subprocess の `--settings` overlay に hook observability
  とともに追加する。interactive lane は Herdr が公式に提供する
  `agent start ... -- [AGENT_ARG]...` pass-through で同じ overlay を渡す。
- `SendMessage` deny により spawn 後の peer / subagent messaging や resume coordination
  は要求しない。維持対象は同一 main session の `Agent(...)` spawn と terminal completion
  lifecycle evidence のみである。
- evidence は `peer_policy_configured` と
  `cross_session_inbound_configured_refuse` を configured、
  `outbound_peer_tools_absent`、`agent_spawn_completion_observed`、
  `herdr_namespace_isolated`、`preexisting_herdr_preserved` を observed として扱う。
  inbound peer message の behavioral proof は主張せず、人間または独立 peer を開始・観測・
  送信先にしない。
- Claude-GPT adapter は caller `--settings` を受け取らず、launcher-owned fixed
  runtime-smoke settings channel の同じ policy を使う。SKIP exit 77 は runtime PASS ではない。
  fresh isolated Herdr launch が nested-session policy で拒否された場合の bounded
  reason code は `herdr_isolated_session_unavailable`。通常 lane は snapshot を試行せず、
  opt-in preservation observation の unavailable 結果だけを fail-closed で扱う。

## Lane 選択

### capability 判定の方針(help への非掲載は capability 不足を意味しない)

Claude Code の `--help` 出力は human-oriented な概要であり、network-exhaustive
ではない。`--max-turns` は Claude Code 2.1.220 の `--help` から欠落している
にも関わらず有効な documented print-mode flag として受理される(Issue #1960)。
そのため runner は `claude --help` のテキストから capability を判定しない。
preflight は `claude` 実行ファイルの存在確認のみを行い、structured lane の
capability 判定は実際の fixed-argv invocation 結果に基づく
(unknown/unrecognized option 診断が一致した場合のみ capability SKIP。
`--max-turns` 到達は flag 受理の証拠として bounded turn failure 扱いとし、
capability SKIP には昇格させない)。

structured lane と interactive lane は異なる bounded-execution 保証を持ち、
interactive lane は `--output-format` / `--include-hook-events` /
`--no-session-persistence` / `--max-turns` のような structured-only flag を
forward しない(herdr の wait timeout・process termination・isolated
session の stop／delete と launcher process termination で bounded execution を担保する)。
詳細は `references/claude-code.md` を参照。

### Lane A: structured smoke（既定・非対話ラン）

非対話の fresh process から stream JSON / JSONL event と exit code を取得する。
TUI screen scraping を使わない。herdr を経由しない（常に direct subprocess）。

- Claude Code の起動コマンド例: `claude -p --output-format stream-json --include-hook-events --no-session-persistence --max-turns <n>`

詳細は `references/claude-code.md` を参照。

### Lane B: interactive herdr smoke（必要時のみ）

TUI `/status`、Skill picker、approval 画面、subagent UI、context 表示等、structured lane で
露出しない状態の観測が必要な場合だけ使用する。herdr が必須。

**人間の使用中 Herdr session には一切相乗りしない。** 実行のたびに高エントロピーな
named session を新規生成し、その session 内だけで agent lifecycle を駆動し、
終了時にその session そのものを stop／delete し、両 command の成功と launcher process
termination を確認する（確認できない場合は fail-closed で exit 1）。詳細は `references/herdr.md` を参照。

isolated session を対象とする `workspace create`／`pane run`／`agent start|prompt|get|
explain|read|wait|send-keys` はすべて、`HERDR_SESSION` 環境変数の pin に加えて
`herdr --session "<isolated-name>" ...` を明示的に指定する（Issue #2568 AC1/AC10。
#2571 の real-machine spike finding: 環境変数のみでは確実に route されない）。`session
stop`／`session delete` は対象 session 名を第一引数に取るため、この位置引数自体が
既に明示的なターゲット指定である。

## Main-Session Agent Identity Evidence（メインセッション Agent Identity 証跡、Issue #2046）

`--claude-agent-name` を指定した claude runtime + structured mode の run は、
以下の 5 種類の evidence を `summary.md` に追加で記録する（`references/claude-code.md`
の該当節を参照）:

- `main_agent_identity`: `requested`（runner argv 由来）と `observed`（`SessionStart`
  hook 由来）の分離、`matched`。hook 欠落・不一致は `matched: false` として記録し、
  model の自己申告テキストでは絶対に埋めない
- `agent_definition`: `intended_repo_path` / `intended_sha256`。project-discovery lane
  （既定）は `status: unavailable`（effective source を独立確認できないため）。
  `--hermetic-agent-definition` 指定時は hermetic lane（`binding_mode: hermetic`）となり
  `hermetic_payload_sha256` / `hermetic_agent_name` も記録される
- `skill_evidence`: `declaration`（static frontmatter）／`preload`（常に `unavailable`
  — preload を直接確認する native event channel が存在しないため、`observed` と
  偽装しない）／`canonical_read`（`issue-creator`→`create-issue/SKILL.md`、
  `issue-editor`→`edit-issue/SKILL.md` の Read tool_use/tool_result ペアからのみ
  `observed` になる。marker 文字列や self-report では絶対に `observed` にならない）
- `mutation_boundary`: `--hermetic-agent-definition` 指定時のみ記録。session-local
  settings digest・実効 argv（redact 済み）・mutation-capable tool_use event（`Edit`
  / `MultiEdit` / `Write` / `NotebookEdit` / `Bash` / `Agent`）の件数。1 件でも
  観測されれば run 全体が FAIL（exit 1）
- `settings_provenance`: 実効 settings の source（`session_local_generated` /
  `project_default`）と digest

`production_settings_lane` フィールドは常に記録され、hermetic mutation_boundary
evidence が #1881（pr-reviewer persona の production settings lane）の permission
claim へ昇格しないことを明示する。#1881 未完了時点ではこの hermetic evidence を
production permission の根拠にしない。

## claude-gpt Launcher 向け Multi-Turn / 複数 SubAgent Lifecycle Evidence（同一セッション複数ターン・複数サブエージェント証跡、Issue #2219）

`--claude-adapter claude-gpt`（`scripts/claude-gpt/launch.sh`。本 Skill は launcher の
実装そのものを変更しない — launcher は既に proxy PID/port/log/cleanup-OK を stderr
`KEY=value` 行として emit 済みであり、本 Skill はそれを parse するだけである）を対象に、
以下の evidence を `summary.md` に追加で記録する:

- `resolved_executable_sha256`: preflight で解決した実行ファイル（launcher 自身の
  絶対パス、または PATH 解決された `claude`）のファイル内容 sha256。path 一致だけでなく
  内容一致まで束縛する（Issue #2219 AC1）
- `same_session_across_turns`（`--require-min-turns` 指定時のみ）: native
  `session_id`/`sessionId` フィールドが全 turn を通して単一値のままであることを
  `verify_same_main_session_across_turns()` が検証する。「同一 main session 内で
  最低 2 turn 完了する」の実装方針は、fix_delta iteration 1（pr-reviewer
  REQUEST_CHANGES、PR #2222）で選択肢 A と選択肢 B の両方を実装した:
  - 選択肢 A（structured lane、`--max-turns >= N`）: 単一プロセス・単一 session
    内で Claude Code 自身が駆動する agentic loop の `session_id` 不変性を、
    stdout の stream-json イベントから直接検証する。
  - 選択肢 B（interactive lane、`--additional-prompt`）: PR #2176 が commit
    06d8baa9 で prototype し、Issue #2174 のスコープ外として commit 5a44ebf0 で
    revert した `--additional-prompt` 相当フラグを Issue #2219 の scope で
    再実装した。同一の既に起動済み herdr agent/session へ複数 turn を順次送信し、
    その herdr session 自身が書き出す永続化 session transcript
    （adapter に応じて `_resolve_claude_projects_root()` が解決する root 配下の
    `*/<session_id>.jsonl`（native: `~/.claude/projects`、claude-gpt:
    `$CLAUDE_GPT_HOME/claude/projects`。fix_delta iteration 2、
    references/claude-code.md 参照） -- interactive lane は
    `--no-session-persistence` を forward しないため実際に書かれる。
    `_find_claude_interactive_transcript()` が worktree の `cwd` 一致（先頭
    最大 50 行の window。fix_delta iteration 2）で content-linked に特定する）
    を、選択肢 A と全く同じ
    `verify_same_main_session_across_turns()` / `classify_claude_multi_child_
    lifecycle()` / `verify_no_forbidden_marker()` へそのまま入力する（Option A の
    純粋関数を Option B の共有 building block として再利用しており、
    別ロジックを再発明していない）。（Issue #2219 AC2、fix_delta iteration 1）
- `multi_child_lifecycle`（`--require-min-subagents` 指定時のみ、structured lane・
  interactive lane 共通）: 既存の単一 child 向け
  `classify_claude_child_spawn_agent_id` / `classify_claude_child_completion` を
  集合演算で複数 agent_id へ拡張した `classify_claude_multi_child_lifecycle()` が、
  spawn-only（`orphan_starts`）／stop-only・agent_id mismatch・unknown child
  （`unknown_children`）／duplicate completion（`duplicate_completions`）のいずれかを
  検出したら FAIL とする（Issue #2219 AC3/AC4。interactive lane は永続化された
  session transcript を evidence source として同じ関数で判定する）。completion 判定は
  同期完了（`tool_use_result.status == "completed"`）に加え、async 起動
  （`status: "async_launched"`）が後続で受け取る `<task-notification>` block
  （`<task-id>...<status>completed</status>...`）も第 2 の completion channel
  として認識する。ただし既に spawn 済みと確認できた agent_id にのみ bind し、
  通知単独から spawn を捏造しない（fix_delta iteration 2、live claude-gpt 検証で
  発見。references/claude-code.md 参照）
- `forbidden_marker_scan`（`--scan-forbidden-markers` 指定時のみ、structured lane・
  interactive lane 共通）: 固定 literal allowlist に対する fuzzy match を行わない
  substring scan（Issue #2219 AC6。interactive lane は永続化された session
  transcript と bounded pane 抜粋の両方を scan する、fix_delta iteration 1）
- `claude_gpt_proxy_sidechannel` / `claude_gpt_proxy_cleanup_independent`
  （`--claude-adapter claude-gpt` の全 run で自動記録）: launcher が stderr へ emit
  する `CLAUDE_GPT_PROXY_PORT`/`_LOG`/`_PID`/`CLAUDE_GPT_PROXY_CLEANUP_OK` 行を
  `extract_claude_gpt_proxy_sidechannel()` が parse し、
  `verify_claude_gpt_proxy_cleanup_independent()` が launcher 自身の
  `CLAUDE_GPT_PROXY_CLEANUP_OK` 自己申告を信用せず `kill(pid, 0)` / `ss -ltn` で
  bounded poll-with-retry（既定 3 回、0.5 秒間隔）の独立再確認を行う。自己申告が
  `true` でも独立再確認が FAIL なら run 全体を FAIL とする（Issue #2219 AC7）

### interactive lane の evidence（証拠）設計変遷（選択肢 A から transcript ベースの選択肢 B を経て、現行の hook-event 方式へ移行した経緯）

interactive lane の「同一 session multi-turn / 複数 SubAgent lifecycle」evidence 設計は、
OWNER anchor 決定（Issue #2219 本文、PR #2205/#2222 コメント、2026-08-16）により
in-place で reframe された。過去の設計判断・FAIL 記録は履歴として保持する:

1. **Option A**（structured lane 単一 invocation）を最初に実装したが、
   reviewer が「同一 Herdr session である」ことを要求したため差し戻された。
2. **Option B（transcript ベース）**: 上記「選択肢 B」として、interactive lane
   自身が書き出す永続化 session transcript（`*/<session_id>.jsonl`）を
   `_find_claude_interactive_transcript()` で特定し、Option A と同じ純粋関数
   （`verify_same_main_session_across_turns()` / `classify_claude_multi_child_
   lifecycle()`）へそのまま入力する設計を実装した。しかし live 検証
   （PR #2222 fix_delta iteration 2、2026-08-16）で、herdr-PTY-driven な
   claude-gpt session が構造的にフラットな main transcript を一切書かない
   ことが複数回の live 実行で再現され、この設計は FAIL した（書かれるのは
   `subagents/agent-*.meta.json` という fragmentary な spawn metadata のみで、
   completion field も cwd field も持たない）。
3. **Option 1（hook-event evidence channel、現行）**: `.jsonl` transcript の
   存在有無に依存しない、deterministic な Claude Code hook 機構
   （`UserPromptSubmit`/`Stop`/`StopFailure`/`SubagentStart`/`SubagentStop`
   の 5 種類、すべて既存の `command` 型 hook 経由）へ置き換えた。
   `subagents/*.meta.json` および transcript 存在チェックは advisory/diagnostic
   情報として `summary.md` に残るが、PASS 判定には昇格しない。

現行設計（interactive lane）の要点:

- **Sink**: run-nonce キー付き、O_EXCL で新規作成する append-only JSONL
  ファイル。パスは launcher-owned な定数（claude-gpt adapter:
  `claude_gpt_proxy_state_dir()`/Python 側ミラー
  `claude_gpt_proxy_state_dir_python()`。native adapter: harness が
  `tempfile.mkdtemp()` で生成する専用ディレクトリ）のみから構築し、
  caller-supplied な値（worktree path・CLI 引数）は一切使わない
  （Issue #2219 AC14）。
- **Event set**: 5 種類すべて、固定の hook command 文字列（caller-supplied な
  文字列を埋め込まない）から、launcher-set env var
  （`CLAUDE_GPT_HOOK_SINK_PATH`/`CLAUDE_GPT_HOOK_SINK_NONCE`）経由でのみ
  sink path/nonce を参照する。
- **Record**: `run_nonce` / `event` / `session_id` / `agent_id`（該当時） /
  `ts` / `prompt_digest`（`sha256(run_nonce + prompt)`、raw prompt 本文は
  一切含まない）のみ（Issue #2219 AC13）。
- **multi-turn 判定**: `verify_claude_gpt_hook_sink_multi_turn()` が、同一
  `session_id` を持つ `UserPromptSubmit` record が最低 2 件、各々に対応する
  同一 session の `Stop` record、`StopFailure` record 0 件を AND 条件で検証する。
- **SubAgent lifecycle 判定**: `classify_claude_hook_sink_multi_child_
  lifecycle()` が、structured lane の `classify_claude_multi_child_lifecycle()`
  と**同一の** `_pair_agent_lifecycle()` 集合演算コアを再利用する（別のより
  緩い classifier を新設しない）。`SubagentStop` 前にプロセスが強制終了された
  場合は `orphan_starts` が非空になり fail-closed（Issue #2219 AC17。
  grace window・タイムアウト猶予による自動 PASS 昇格はしない）。
- **Sink 専用 staleness 検証**: `verify_claude_gpt_hook_sink_not_stale()` が、
  settings.json に焼き込まれた nonce・harness がこの実行に期待する nonce・
  sink 内の全 record の `run_nonce` の 3 点一致を検証する。
  `verify_evidence_not_stale()`（repo-state/`tested_head` ベース）とは
  独立したチェックである（Issue #2219 AC16）。
- **Concurrency/atomicity**: hook command は単一の bounded-size `printf`/
  `write()` で 1 record 1 書き込み（PIPE_BUF 未満）とし、
  `SubagentStart` が複数ほぼ同時に発火しても書き込みが interleave/truncate
  しない（Issue #2219 AC15）。
- **Identity**: `agent_id`/`session_id` のみを pairing/identity 判定に使い、
  `agent_type`（model 自己申告で信用できない）は使わない。
- **claude-gpt adapter の narrow launch.sh 変更**: `scripts/claude-gpt/
  launch.sh` の既存 `CLAUDE_GPT_RUNTIME_SMOKE_HOOKS` 固定値ゲート
  （`subagent-start-stop`）に、新しい固定値 `hook-sink-multi-turn` を
  1 つ追加した。この値は `UserPromptSubmit`/`Stop`/`StopFailure`/
  `SubagentStart`/`SubagentStop` の 5 hook すべてを durable JSONL sink へ
  配線する。`lib.sh` は変更していない。`CLAUDE_GPT_FORBIDDEN_EXTRA_FLAGS` の
  緩和や caller-supplied な任意 `--settings`/hook command の受け入れは行って
  いない。
- **native adapter**: `scripts/claude-gpt/**` を一切変更せず、harness 側
  （`run_worktree_agent_runtime_smoke.py` が生成する `CLAUDE_CONFIG_DIR`/
  `settings.json`）のみで完結する。

`verify_evidence_not_stale()`（Issue #2219 AC10）は、過去に書き出した evidence JSON の
`tested_head`/`repo_fingerprint` を fresh worktree HEAD/fingerprint と突き合わせ、
不一致（または fresh 値自体が取得できない場合）を `stale: True` として拒否する明示ガード
関数である。stale evidence の PASS への再利用を防ぐためのユーティリティ関数であり、
runner の CLI からは呼び出されない（呼び出し元が evidence 再利用を判断する場面で使う）。

## PR reviewer 向け: evidence の `tested_head` と live PR head の突き合わせ（照合手順）

`summary.md` の `tested_head` は evidence 生成時点の worktree HEAD SHA である。
PR reviewer は、参照された evidence が対象 PR の **現在の** head に対するものかを
`cleanup_exec.py` の `head_oid_match` パターン（`gh pr view --json headRefOid` で
取得した live headRefOid と、evidence 側が主張する commit SHA を文字列完全一致で
比較する）に倣って独立検証する:

```bash
gh pr view <PR番号> --repo squne121/loop-protocol --json headRefOid --jq .headRefOid
```

この値と evidence の `tested_head` が一致しない場合、その evidence は該当 PR の
現在の head に対する証跡ではない（stale head evidence、Issue #2219 AC10）。
`verify_evidence_not_stale()` はこの突き合わせを worktree ローカルの fresh HEAD に
対して行うプログラム的なガードであり、PR reviewer が行う `headRefOid` 突き合わせは
それと独立した GitHub 側の live 確認である。
## SubAgent 実行の因果関係証跡（SubAgent Causal Evidence、hook ID 相関、Issue #2183）

`subagent_causal_evidence_verdict(stdout, expected_markers=None)`
（`scripts/agent-ops/run_worktree_agent_runtime_smoke.py`）は、SubAgent 実行の
causal evidence を marker 文字列出力のみに依存せず、hook ID 相関（`SubagentStart`/
`SubagentStop` の同一 `agent_id` 相関）で判定する純粋関数。structured lane の
captured stdout（stream-json）を入力とする。

戻り値のフィールド:

- `causal_evidence_source`（enum）: `hook_id_correlated` / `marker_only_insufficient` /
  `no_evidence`。`hook_id_correlated` は、単一の曖昧さのない Start/Stop chain が以下を
  **すべて** 満たした場合にのみ返る（Issue #2183 AC3/AC11 強化、PR #2220 OWNER
  REQUEST_CHANGES P0-2/P0-3/P1-1/P1-2 でさらに強化。いずれか一つでも欠落・不一致・
  曖昧なら fail-closed に `no_evidence`/`marker_only_insufficient` に留まる）:
  1. 同一 `agent_id` を持つ `SubagentStart`/`SubagentStop` ペアが観測され、Start の
     出現順序（`stream_index`）が Stop より構造的に先行している
  2. 両イベントが値を持つ場合の `session_id`／`prompt_id`／`agent_type` が一致する
     （P0-3。値を持たない場合はスキップされる best-effort 契約）。同一 `agent_id`
     に複数の Start または複数の terminal Stop がある場合は候補から除外される
     （P1-2、曖昧な chain を「最初の 1 件」で解決しない）
  3. `SubagentStop` payload の `agent_transcript_path` が回収でき、かつそのパスが
     指すファイルが実在し・シンボリックリンクでなく・（許可 root を指定した場合は）
     その配下であり・サイズ上限以下であり・読み取り可能・非空である
     （`agent_transcript_verified`。P1-1 で symlink 拒否・containment・サイズ上限・
     mtime 整合チェックを追加）
  4. 相関済み `agent_id` が、同一 session/prompt スコープ内の `Agent` tool_use/
     tool_result の tool_use_id 相関で裏付けられ、その tool_result が terminal かつ
     成功（`status == "completed"` かつ非 error）である
     （`tool_invocation_id_correlated == True` かつ `terminal_tool_result_success ==
     True`。P0-3 でエラー・未完了の tool_result を明示的に除外）
  5. `expected_markers` を指定した呼び出しでは、その **すべての** 期待マーカー
     （P0-2 で「いずれか 1 つ」から強化）が、相関済み child 自身の transcript ファイル
     の assistant 発話 record（JSONL パースで user prompt/tool input を除外）、または
     `SubagentStop` の `last_assistant_message` 由来であることが確認できる
     （`marker_provenance_verified`。parent 自身の最終応答文字列や child 自身の
     user prompt/tool input にのみ一致した場合は昇格しない）
  6. 上記すべてを満たす候補 chain が **ちょうど 1 つ** であること（P0-3。複数の
     `agent_id` が独立に全条件を満たす場合はいずれも昇格しない）

  lone `SubagentStart`（`SubagentStop` 欠落）・session/prompt/type 不一致・
  `agent_transcript_path` 欠落・transcript 未実在／空／symlink／範囲外／サイズ
  超過・`tool_invocation_id_correlated == False`・terminal でない tool_result・
  marker provenance 未確認・候補 chain の曖昧さは、いずれも `no_evidence` に
  fail-closed する。hook イベントが一切なく `expected_markers` の文字列のみが
  `stdout` にあれば `marker_only_insufficient` になる
- `tool_invocation_id_correlated`（bool）: 相関済み `agent_id` が `Agent` tool_use の
  `id` と対応する `tool_result` の `tool_use_id` を介して同一の tool 呼び出しに
  紐づいていることを追加検証する（`extract_claude_canonical_read_receipt` と同じ
  tool_use_id 相関パターンを `Agent` tool 呼び出しに適用したもの）。この値は
  `causal_evidence_source` の判定に直接参加する（AC3 強化。フィールドとして
  計算されるだけで判定に無関係、という状態は解消済み）
- `agent_transcript_verified`（bool、AC11）: `agent_transcript_path` が実在し・
  読み取り可能・非空なファイルを指しているか。パス文字列が payload に含まれて
  いるだけでは `True` にならない
- `marker_provenance_verified`（bool、AC11/AC12）: `expected_markers` 指定時のみ
  意味を持つ。マーカーが相関済み child の transcript 内容または
  `last_assistant_message` 由来であれば `True`。`expected_markers` 未指定時は
  常に `True`（provenance の主張自体がないため）
- `agent_id` / `subagent_start_observed` / `subagent_stop_observed` /
  `agent_transcript_path`: 個別の観測事実（すべて fail-closed、推測しない）

**structured lane かつ `--runtime claude`** は `SubagentStart`/`SubagentStop` の
stream-json チャンネルが常に利用可能なため、呼び出し側が `--expect-marker` を指定する
場合、この causal-evidence 要件（`causal_evidence_source == hook_id_correlated`）は
**追加フラグなしで既定強制** される（PR #2220 レビュー fix-delta）。
`--require-subagent-causal-evidence`（runner CLI フラグ、既定 OFF）は、
`--expect-marker` を指定しない `--runtime claude --mode structured` 実行にもこの
要件を課したい場合にのみ使う。

native Codex CLI lane（`--runtime codex`）は Issue #2161 で撤去済みであり、structured lane の既定強制は現在 `args.runtime == "claude"` の唯一の値に対して
常に成立する（PR #2220 OWNER REQUEST_CHANGES P0-1、https://github.com/squne121/loop-protocol/pull/2220#issuecomment-5309790514）。

**interactive lane** は herdr pane のテキストレンダリングにはこの stream-json hook
payload が構造的に現れないため、`causal_evidence_source` は今のところ
`no_evidence`／`marker_only_insufficient` になる想定である。PR #2220 P0-1 fix-delta
より、`--require-subagent-causal-evidence` は `--runtime claude --mode structured`
以外の組み合わせでは `main()` が起動前に `parser.error`（exit 2）で拒否するようになり、
**interactive lane はこのフラグの opt-in 対象ではなくなった**（従来 opt-in だった
経路も含む破壊的変更）。interactive lane 向けの hook 出力チャネル整備は本 Issue の
対象外（follow-up）。

いずれの lane でも判定結果は常に `summary.md` / `schema_summary["subagent_causal_evidence"]`
に記録され、marker のみの PASS だったのか hook ID 相関で PASS したのかを事後に判別できる。

## Generic Hook-Chain Evidence（汎用 hook 連鎖証跡、AC2/AC3、Issue #2663）

`--require-hook-chain-evidence` は、structured native Claude lane に opt-in する
generic hook-chain evidence capability である。bounded closed allowlist（下記の
固定 event/tool/handler の組のみ）だけを受理し、caller-supplied な任意の
command／path／marker／config は一切受理しない。有効化すると invocation-local
additive `--settings` overlay に「`PreToolUse`（matcher `Bash`）」「`Stop`」の
2 つの観測用 `cat` hook を追加登録し、さらに Claude Code 自身の `--setting-sources
project` フラグ（固定値。caller が選択できない）を併用する。

**`--setting-sources project` の正確な効果（PR #2668 fix_delta で訂正）**: 公式
CLI reference（https://code.claude.com/docs/en/cli-reference）が定義するのは
「どの設定ファイル（user／project／local の `settings.json`）を読み込むか」のみ
であり、managed policy／plugin／Skill が独立に登録する hook を除外するもの
**ではない**。`"project"` 固定は user／local の設定ファイル source を除外する
効果のみ確定的に持つ（live 検証: `--setting-sources` 省略時、runner を実行する
ホストのユーザーレベル `~/.claude/settings.json` に登録された PreToolUse/Bash
hook が cohort に紛れ込み `unattributable_extra_hook_execution` を誘発すること
を確認済み）。managed／plugin／Skill が登録した hook はこのフラグの有無に関係
なく引き続き `unattributable_extra_hook_execution`（`unverified`）として扱われる
（後述の「confirmed runtime-capability boundary」参照）。旧版のこの文書と
コードコメントが「user／local／managed／plugin／skill 等の source を除外する」
と記載していたのは誤りで、PR #2668 fix_delta（anchor review
https://github.com/squne121/loop-protocol/pull/2668#issuecomment-5737957277）
で訂正した。

existing hooks/`.claude/settings.json`／`.claude/hooks/**` は一切変更・disable・
置換しない（Out of Scope）。

**confirmed runtime-capability boundary（handler identity は証明できない、PR
#2668 fix_delta P1-1）**: `hook_id` は「この `hook_started` とこの
`hook_response` が同一 invocation である」ことのみを証明し、`hook_name` は
「このレコードがこの event(+tool) グループに属する」ことのみを証明する
（同一 event+matcher に登録された sibling command hook はすべて同一の
`hook_name` を共有する）。settings.json に列挙された **どの具体的な command
hook が実際に実行されたか** を証明する公式チャネルは存在しない
（https://code.claude.com/docs/en/hooks-guide ／
https://code.claude.com/docs/en/agent-sdk/hooks: "all matching hooks run in
parallel ... write each hook to act independently rather than relying on
another hook having run first" — 実行順序も非決定的なため、順序ベースの
heuristic も無効）。したがって `all_matching_hooks_observed` の `pass` は
「期待 cohort と同数の PreToolUse/Bash hook 実行が hook_id で正しく対応付け
られ、orphan／不足／識別不能な超過が無い」ことの証明であり、「settings.json
に列挙された特定の N 個の handler が個別に実行された」ことの証明では **ない**
（例: 期待 handler が A,B,C,D で実際の実行が A,X,C,D（X は B の同数置換で、X
自身も正常な hook_started/hook_response ペアを持つ）の場合、現在の判定は
区別できず `pass` になる — これは既知の、確認済みの runtime capability
の上限であり、実装漏れではない）。

`schema_summary["hook_chain_evidence"]`（`summary.md`／`--evidence-json` の両方に
記録される）は次の 2 つの独立した assertion と、その aggregate を持つ:

- `all_matching_hooks_observed`（AC2）: 対象は `PreToolUse` イベント + `Bash` tool
  のみ（bounded, closed）。expected cohort は tested_head の
  `.claude/settings.json` の `PreToolUse` 内、matcher が `Bash` を含む
  hook group の `command` 数（例: 現行 repo では
  `secret_boundary_guard.sh` / `guard-japanese-prose.sh` /
  `ci_test_performance_advisory.sh` / `root_temporary_residue_advisory.sh`
  の 4 件）。runner 自身が追加する観測用 `cat` hook は、その hook 自身が
  stdin をそのまま echo するという構造的な self-echo 署名（
  `hook_event_name`/`tool_name`/`tool_input`/`tool_use_id` を全て含む
  JSON）で positively 識別され、cohort から明示的に除外される。各 Bash
  tool_use（positive scenario 用・deny scenario 用の両方）ごとに、
  hook_id で対応付けられた `hook_started`+`hook_response` ペアの件数を
  expected cohort 数と照合する。件数不足は `fail`（`missing_handler_
  evidence`）。件数超過（識別できない追加 hook execution）は `fail` では
  なく `unverified`（`unattributable_extra_hook_execution` — user/local/
  managed/plugin/skill source の存在をもって「runtime全体の完全性を
  証明した」とは主張しないため）。positive scenario（deny なし）と deny
  scenario（`exit_code == 2` を観測した hook が最低 1 件）の両方が観測され、
  かつ両方の window が `status: pass` の場合にのみ全体が `pass` になる
  （どちらか一方が欠けている、または全 Bash tool_use がゼロ件の場合は
  `fail`）。**同一 assistant メッセージ内に複数の Bash tool_use block が
  含まれる場合**（公式 Agent SDK が明記する正当な形— 各 block は distinct な
  `tool_use_id` を持つ）、これらは同一 `stream_index` を共有するため、
  従来の「tool_use 自身の行位置」だけでは個別の window に分離できない
  （PR #2668 fix_delta P1-2）。この場合は、観測用 hook 自身の self-echo
  payload に既に必須キーとして含まれる `tool_use_id` の **値**（従来は
  key の存在のみ確認していた）を読み取り、宣言された `tool_use_id` 群と
  一意に対応付けて sub-window を分割する。対応が一意に決まらない場合
  （不足・重複・未知の値）は window ごとに `unverified`
  （`self_echo_tool_use_id_ambiguous`）とし、決して推測しない。
- `sibling_side_effect_inventory_complete`（AC3）: hook の exit code（実行完了）
  とは独立に、実際の post-condition を確認する。対象 handler は
  `.claude/hooks/session_manifest_coordinator.sh`（`Stop` イベント。tested_head
  の `.claude/settings.json` に実際に登録されているかを確認し、未登録なら
  `unverified`）。期待する変化は `artifacts/session-manifest-runtime/manifests/`
  配下への新規ファイル出現（`generate_session_manifest_from_hook.mjs` 自身の
  filename 契約 `private-agent-session-manifest-{eventNameLower}-{timestamp}-
  {stableKeySegment}.json` の `-stop-` タグでのみ判定し、同じディレクトリへ
  書き込む別の非対象 handler — 例えば同 repo の `PostToolUse` debounce hook
  `session_manifest_debounce.mjs` — の書き込みは対象外として無視する。live
  検証で、単一セッションが `posttooluse` タグと `stop` タグの両方の新規
  ファイルを生成することを確認済み）。**この判定に使うディレクトリ snapshot
  自体は、Stop タグでフィルタしてから件数上限を適用する**（PR #2668
  fix_delta P2-1 — 旧実装は「全ファイル列挙 → sort → 先頭 N 件に切り詰め →
  その後で Stop タグを判定」という順序だったため、Stop タグより辞書順で先に
  来る `-posttooluse-` タグの過去ファイルが N 件以上蓄積すると、正当な新規
  Stop manifest が切り詰められて見落とされていた。Stop タグ該当分自体が
  上限を超えた場合のみ `truncated` フラグを立て、呼び出し側は
  `unverified`（`session_manifest_snapshot_truncated`）として扱う — 証跡を
  静かに欠落させない）。**新規ファイルは、埋め込まれた `actor.session_id`
  が現在の run 自身の session id と一致する場合のみ「この run の証跡」として
  カウントする**（PR #2668 fix_delta P1-3(a) — 同じディレクトリに同時実行中
  ／stale な別 session が正規の Stop manifest を書き込んだ場合、それを
  この run 自身の成功として誤カウントしないため。session id はこの manifest
  スキーマ自身が既に持つ `actor.session_id` フィールドと、この runner が
  既に読み取っている stream 自身の session id から比較する。マッチしない、
  またはスキーマにフィールドが無いファイルは対象外）。post-condition の
  読み取りタイミングは「対象 sibling hook 群（この runner が subprocess
  として起動した Claude Code プロセス全体）の実行完了後」— `claude -p` の
  subprocess 自体が終了するまでこの runner は post-condition を読まないため、
  同期 Stop hook の完了は構造的に保証される。**正常な無変更の条件は、観測用
  `Stop` hook の self-echo から回収した `stop_hook_active: true` に加えて、
  対象 `session_manifest_coordinator.sh` 自身が STDERR に出力する
  `SESSION_MANIFEST_COORDINATOR_RESULT_V1={"steps":["stop_guard"],...}`
  完了マーカーの両方が揃った場合のみ**（PR #2668 fix_delta P1-3(b) —
  観測用 hook 自身の入力だけでは「対象 coordinator 自身が実際に no-op を
  完了した」ことを証明できないため。構造化 `hook_response` イベントの
  `stderr` フィールドが hook 自身の stderr を露出することを、このリポジトリ
  自身の bounded local live trial（Claude Code 2.1.277）で確認済み — 従来
  この module は `stdout`/`output` のみ読んでいた）。新規ファイルが 1 件を
  超える場合は overflow として `fail`。新規ファイルなしかつ上記 2 条件が
  揃わない場合は `missing_side_effect` として `fail`。
- aggregate: `hook_chain_evidence.status`（`passed: true|false`）は、上記
  2 つの assertion が **両方とも** `status: pass` の場合にのみ `pass`。

3 値ステータス（`pass` / `fail` / `unverified`）の意味は共通: `unverified` は
「識別できないため PASS を主張できない」であり、`fail`（明確な欠落・overflow・
不一致）とも `pass`（cohort/post-condition を完全に確認できた）とも異なる。
boolean `passed` は `status == "pass"` の場合にのみ `true`。SKIP・fallback・
`unverified` はいずれも PASS の代替にならない。

### canonical live invocation（実行可能な正本コマンド、Issue #2663 AC5）

```bash
uv run --locked python3 scripts/agent-ops/run_worktree_agent_runtime_smoke.py \
  --runtime claude --mode structured \
  --worktree "$WORKTREE" \
  --prompt-file <positive scenario と deny scenario の両方を promptに含む、非 tracked ファイル> \
  --output-dir artifacts/runtime-smoke/hook-chain-current-head \
  --timeout-seconds 180 --max-turns 4 \
  --require-clean-postcondition --require-hook-chain-evidence
```

- exit `0`: 両 assertion が `pass`（かつ他の有効な gate も全て満たした場合）
- exit `1`: いずれかの assertion が `fail`、または `unverified`（cohort/
  post-condition を識別できない、この runner を実行するホスト環境固有の
  user-level hook 混入等）を含む — `unverified` は SKIP ではなく FAIL 側の
  exit code として扱われる（PASS を主張しないことを exit code でも明示する）
- exit `77`: claude 実行ファイル自体が利用不能等、既存の capability SKIP 条件
  （本 flag 固有の SKIP 条件は追加していない）

prompt file は、無害な入力で PreToolUse/Bash hook を最低 1 回発生させる
positive scenario のコマンドと、`secret_boundary_guard.sh` が実際に deny
（exit code 2）を返す状況を発生させる deny scenario のコマンド（例:
`printenv`）の両方を、別々の Bash tool_use として含める必要がある
（AC5 の「空の expected/observed set が一致しただけの PASS を禁止する」
要件、および「hook lifecycle の複数レコードや順序前後を誤判定しない」
要件を満たすため）。

### 開発中 trial と最終 acceptance evidence の区別（AC5）

開発中の trial 実行は、Allowed Paths 内の未 commit candidate に対しても許可
される（失敗時は診断として修正・再実行してよい）。ただし最終 acceptance
evidence は、対象変更を commit した HEAD に対してのみ成立する。`summary.md`
の `tested_head`（commit SHA）と、検証対象 tracked inputs が実際にその HEAD
と一致していること、実行前後で意図しない worktree drift がないこと
（`--require-clean-postcondition` の `postcondition_unexpected_changes`）の
3 点を確認する。`before == after` の fingerprint 一致だけを「開始時点で HEAD
と一致していた」の意味として扱わない。

## 手順

1. **worktree identity を確認する**（root checkout・別 repository・cwd mismatch を実行前に拒否する。runner が自動で検証する）
2. **prompt file を用意する**（raw prompt をコマンドライン引数に直接埋め込まない）
3. **runner を実行する**:
   ```bash
   uv run --locked python3 scripts/agent-ops/run_worktree_agent_runtime_smoke.py \
     --runtime claude --mode structured \
     --worktree "$WORKTREE" \
     --prompt-file tmp/runtime-smoke/claude-structured.md \
     --output-dir artifacts/runtime-smoke/claude-structured \
     --timeout-seconds 180 \
     --require-clean-postcondition
   ```
4. **exit code を確認する**: `0`=成功／`1`=runtime failure・timeout・identity mismatch・postcondition 違反・cleanup 未確認／`77`=SKIP（capability・auth・herdr 不足。PASS へ昇格しない）
5. **evidence を確認する**: `<output-dir>/summary.md`（唯一の永続 evidence ファイル）を caller の PR 本文へ引用する。raw prompt／raw transcript／reasoning／credential／HOME 絶対パスは保存しない（`references/evidence-hygiene.md` 参照）
6. **caller が semantic verdict を判定する**: runner の役割は起動・観測・証跡収集までで終わる

## Safety Boundary（安全境界）

- runner は `shell=False` を基本とし、prompt は file または stdin で渡す
- canonical repository と linked worktree identity を実行前に検証し、root checkout・別 repository・cwd mismatch を拒否する
- `--dangerously-bypass-approvals-and-sandbox` / `--yolo` / `danger-full-access` への自動変更を行わない
- herdr 未検出・`HERDR_ENV` 未設定は `mode=interactive` の SKIP（exit 77）とし、structured lane の失敗へ波及させない
- `blocked` / `unknown` の agent lifecycle state を成功として扱わない
- interactive lane は毎回新規 isolated named session を生成し、人間の使用中 session・pane・
  agent・workspace を enumerate、list、snapshot、operate、message、observe しない。検証終了時は
  その session だけを stop／delete し、両 command の成功と launcher process termination を
  確認できない場合は exit 1 とする（`--keep-pane` 相当の opt-out は存在しない）
- SIGINT／SIGTERM を含む全ての終了経路で isolated session cleanup を実行する
- 新しい schema、digest、receipt、publisher、state store、semantic verdict classifier を追加しない（Issue #2046 で `main_agent_identity` / `agent_definition` / `skill_evidence` / `mutation_boundary` / `settings_provenance` の 5 フィールドが Issue 契約に基づき追加済み — この制約は Issue 契約に基づかない追加の schema/digest/receipt 拡張を禁じるものであり、既存の Issue 契約で明示的に要求された追加を遡って禁止するものではない）

## Reference Map（参照資料の一覧）

- `references/claude-code.md` — Claude Code の structured／interactive invocation 手順を説明する
- `references/herdr.md` — herdr pane／agent API の使い分けを説明する
- `references/evidence-hygiene.md` — evidence hygiene と session-log metadata boundary の扱いを説明する

## Related（関連情報）

- `.claude/skills/issue-refinement-loop/scripts/run_native_session_continuation_canary.py` — Issue #2153。claude-native / claude-gpt の Claude Code session continuation（initial/same-continuation/fresh の 3 段階 lifecycle）を検証する専用 canary。本 Skill の executable/launcher 解決・`--claude-adapter` 選択・timeout/process termination・stdout/stderr capture・worktree identity・runtime-unavailable classification の primitive を reuse するが、3 段階 lifecycle state machine 自体は本 Skill には追加されていない（reuse 境界は Issue #2153 AC6 参照）
- `.claude/skills/implement-issue/SKILL.md` — 動作検証 AC を含む Issue の実装手順（本 Skill を呼び出す側）
- `docs/dev/runtime-verification-policy.md` — Runtime Verification Applicability の全体方針
- `docs/dev/agent-runtime-ops.md` — structured lane 既定・interactive herdr lane 限定利用の運用境界
- `docs/dev/agent-skill-boundaries.md` — SubAgent／Skill 責務境界
