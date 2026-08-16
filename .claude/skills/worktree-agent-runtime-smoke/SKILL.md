---
name: worktree-agent-runtime-smoke
description: linked worktree 内で Claude Code / Codex CLI の fresh runtime を起動し、structured output（既定・常に direct subprocess）または herdr interactive lane（TUI 固有挙動が必要な場合のみ・常に人間の Herdr session とは分離した isolated named session）で観測し、allowlist-only summary evidence を worktree-local に保存する共有 Skill。「runtime smoke」「動作検証を実行」「Claude/Codex を worktree で起動して確認」のトリガーで使う。
---

# Worktree Agent Runtime Smoke

Claude Code と Codex CLI について、linked worktree 内で fresh session を起動し、
structured event（既定・常に direct subprocess）または herdr pane observation
（TUI 固有挙動が必要な場合・常に呼び出し元とは分離した isolated named herdr session）で
runtime evidence を収集する。semantic verdict（hook reason 分類、mutation deny 妥当性、
Skill preload 判定、context budget 評価、review verdict、merge readiness）は callerの責務。

## Trigger（使用条件）

- Issue の `## Runtime Verification Applicability` が `decision: immediate` で、
  Claude Code または Codex CLI の実 process / TUI 起動証跡が必要な場合
- pr-review-judge や実装 worker が runtime postcondition（hook lifecycle、session 非永続化、
  worktree cwd binding）を証明する必要がある場合

## Non-trigger（使用しない場合）

- 静的検証（typecheck / lint / test / build）だけで AC を満たせる場合
- semantic な hook reason 分類・mutation deny 妥当性判定・context budget 数値評価を行う場合
  （本 Skill は runtime 起動・観測・証跡収集だけを所有する）
- worktree の新規作成／削除、自動 Issue／PR mutation、自動 approval

## Input（入力）

- `--runtime claude|codex`
- `--mode structured|interactive`
- `--worktree <linked worktree の絶対パス>`
- `--prompt-file <検証用 prompt ファイル>`（raw prompt を argument interpolation しない）
- `--output-dir <evidence 出力先。既定は worktree 配下の untracked ディレクトリ。排他的作成（既存ディレクトリ／symlink は拒否）>`
- `--timeout-seconds <bounded timeout>`
- `--max-turns <Claude Code の bounded turn 数。既定 30>`
- `--expect-marker <literal>`（repeatable、任意）
- `--require-clean-postcondition`（任意）
- `--inspect-session-log-metadata` / `--require-session-log-metadata`（任意。既定では session log を読まない）
- `--agent-type <persona 名>`（任意。static declaration。CLI へ forward しない）
- `--claude-agent-name <persona 名>`（任意。claude runtime + structured mode 限定。実際に `--agent <name>` として CLI へ forward し、main-session identity（`main_agent_identity`）・candidate Agent definition binding（`agent_definition`）・Skill evidence（`skill_evidence`）の evidence source になる。Issue #2046）
- `--hermetic-agent-definition`（任意。`--claude-agent-name` 併用必須。project-discovery の `--agent <name>` lookup ではなく、candidate Agent 定義から決定論的に生成した session-local `--agents` JSON payload（tools は Read のみ固定）と session-local `--settings`（mutation-capable tool を deny）で起動する hermetic no-mutation lane。Issue #2046）
- `--claude-bin <absolute path>`（任意。`--runtime claude` 限定。claude 互換の実行ファイル（例: `scripts/claude-gpt/launch.sh` launcher）の絶対パスを明示指定する。指定時は `shutil.which("claude")` による PATH 解決を bypass し、structured lane はその絶対パスを固定 argv の実行ファイルとして直接使用する。interactive herdr lane では、herdr 自身が `--kind claude` の実行ファイルを常に自分の PATH lookup で再解決するため、isolated session 専用の一時ディレクトリに `claude` という名前の forwarder script（symlink ではない。`exec '<絶対パス>' "$@"` で実際の launcher を exec するシェルスクリプト。symlink だと `$0` が symlink 自身のパスになり、sibling `lib.sh` を `dirname -- "$0"` で source する launcher が壊れるため。PR #2176 OWNER REQUEST_CHANGES Finding 2）を生成し、`herdr workspace create --env PATH=<shim-dir>:<既存PATH>` で Herdr 自身のサーバ／root shell プロセスへ明示的に渡す（Python クライアント側の `PATH` を更新するだけでは Herdr サーバの PTY プロセスへ届かないため。Finding 1）。forwarder は起動直前に run-scoped nonce を 0600 の receipt ファイルへ書き込み、runner はその nonce を run 終了後に readback して指定 launcher が実際に実行されたことを検証する（ambient PATH 上の別 `claude` が実行された場合は receipt が観測できず FAIL する）。未指定時（既定）は既存の `shutil.which("claude")` PATH 解決が変更なく維持される（Issue #2174）。
- `--claude-adapter native|claude-gpt`（任意。既定値 `native`。Issue #2174 AC1 fix_delta、OWNER REQUEST_CHANGES https://github.com/squne121/loop-protocol/issues/2174#issuecomment-5302215173 で追加。`--claude-bin` とは独立した明示入力であり、`bool(--claude-bin)` から launcher 固有挙動を暗黙適用しない。`native`（既定）: `--claude-bin`（指定時）は純粋な binary path override として扱われ、PATH 解決時と同一の固定 argv（`--settings <hook-observability-json>` を含む）で起動する。`claude-gpt`: `--claude-bin` の指定が必須（未指定なら起動前に blocked）。`scripts/claude-gpt/launch.sh` 自身の CLI 契約（自身のオプションの後に literal `--` separator、以降は claude 本体へ素通し）に従い、structured lane の固定 argv 先頭に `--` を挿入し、`--settings <JSON>` の代わりに `CLAUDE_GPT_RUNTIME_SMOKE_HOOKS=subagent-start-stop` 環境変数を設定する（launcher が `--settings` を含む policy-weakening flag を拒否するため）。launcher 自身の `CLAUDE_GPT_LAUNCH_RESULT_V1` 受理/拒否 receipt は evidence の `claude_gpt_launcher_receipt` としてそのまま記録される（Issue #2174 AC8）。`--runtime codex` と組み合わせると argparse 時点で拒否される（PR #2176 OWNER REQUEST_CHANGES Finding 6）。

`--mode interactive` は常に（オプトインではなく）、isolated interactive lane 実行の前後で、その時点で running な herdr session すべてに対して明示的に `herdr --session <name> api snapshot`（PR #2176 OWNER REQUEST_CHANGES Finding 4。デフォルト session だけでなく、人間が attach 中の named session も含めすべて明示的に snapshot する）を取得し、full snapshot（agent の kind・`terminal_id`・native `agent_session`、非 agent pane を含む全 pane record、全 tab record、空 workspace を含む全 workspace record、layout の構造）を、この run 自身が作成した isolated session を除いて完全に一致することを検証する（Issue #2174 AC7、PR #2176 OWNER REQUEST_CHANGES Finding 3）。いずれかのフィールドが取得不能な場合も fail-closed で FAIL とする（session 一覧自体が取得不能な場合も同様。空集合として扱わない）。evidence は `herdr_workspace_snapshot_diffs` / `herdr_workspace_snapshot_preserved` として記録される。なお herdr v0.8.0 の公開 `SessionInfo`（`session list`）には name と独立した `session_id` フィールドが存在しないことを実機確認済みであり、本 skill は `agent`（kind）+ `terminal_id` + native `agent_session`（kind/source/value）を実際に利用可能な最強の agent identity として扱う。

`--transport` と `--keep-pane` は存在しない（PR #1921 human OWNER fix-delta）。structured lane は
常に direct subprocess、interactive lane は常に isolated named herdr session であり、
呼び出し元は transport を選択できない。検証 session は常に cleanup 対象であり、残す
オプションは提供しない。

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
session の stop／delete／removal 確認で bounded execution を担保する)。
詳細は `references/claude-code.md` を参照。

### Lane A: structured smoke（既定・非対話ラン）

非対話の fresh process から stream JSON / JSONL event と exit code を取得する。
TUI screen scraping を使わない。herdr を経由しない（常に direct subprocess）。

- Claude Code の起動コマンド例: `claude -p --output-format stream-json --include-hook-events --no-session-persistence --max-turns <n>`
- Codex CLI の起動コマンド例: `codex exec -C <worktree> --json --ephemeral -`（prompt は stdin 経由。argv には出さない）

詳細は `references/claude-code.md` / `references/codex-cli.md` を参照。

### Lane B: interactive herdr smoke（必要時のみ）

TUI `/status`、Skill picker、approval 画面、subagent UI、context 表示等、structured lane で
露出しない状態の観測が必要な場合だけ使用する。herdr が必須。

**人間の使用中 Herdr session には一切相乗りしない。** 実行のたびに高エントロピーな
named session を新規生成し、その session 内だけで agent lifecycle を駆動し、
終了時に session そのものを stop／delete し、`herdr session list --json` で消失を
確認する（確認できない場合は fail-closed で exit 1）。詳細は `references/herdr.md` を参照。

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

**`--runtime codex`（structured lane）** には `SubagentStart`/`SubagentStop` 相当の
stream-json hook channel が存在せず、`causal_evidence` は常に `None` になる。PR #2220
OWNER REQUEST_CHANGES P0-1（https://github.com/squne121/loop-protocol/pull/2220#issuecomment-5309790514）
より、structured lane の既定強制は `args.runtime == "claude"` に明示的にスコープされて
おり、Codex の `--expect-marker` marker-only PASS はこの gate の対象外になる（修正前は
runtime を判別しなかったため、正常終了し marker も出力した Codex 実行が
`causal_evidence is None` を理由に誤って FAIL していた）。

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
  agent・workspace には一切触れない。検証終了時は session 全体を stop／delete し、
  `herdr session list --json` での消失確認が取れない場合は exit 1 とする（`--keep-pane` 相当の
  opt-out は存在しない）
- SIGINT／SIGTERM を含む全ての終了経路で isolated session cleanup を実行する
- 新しい schema、digest、receipt、publisher、state store、semantic verdict classifier を追加しない（Issue #2046 で `main_agent_identity` / `agent_definition` / `skill_evidence` / `mutation_boundary` / `settings_provenance` の 5 フィールドが Issue 契約に基づき追加済み — この制約は Issue 契約に基づかない追加の schema/digest/receipt 拡張を禁じるものであり、既存の Issue 契約で明示的に要求された追加を遡って禁止するものではない）

## Reference Map（参照資料の一覧）

- `references/claude-code.md` — Claude Code の structured／interactive invocation 手順を説明する
- `references/codex-cli.md` — Codex CLI の structured／interactive invocation 手順を説明する
- `references/herdr.md` — herdr pane／agent API の使い分けを説明する
- `references/evidence-hygiene.md` — evidence hygiene と session-log metadata boundary の扱いを説明する

## Related（関連情報）

- `.claude/skills/implement-issue/SKILL.md` — 動作検証 AC を含む Issue の実装手順（本 Skill を呼び出す側）
- `docs/dev/runtime-verification-policy.md` — Runtime Verification Applicability の全体方針
- `docs/dev/agent-runtime-ops.md` — structured lane 既定・interactive herdr lane 限定利用の運用境界
- `docs/dev/agent-skill-boundaries.md` — SubAgent／Skill 責務境界
