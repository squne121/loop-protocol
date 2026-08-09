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

## Main-Session Agent Identity Evidence（Issue #2046）

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
