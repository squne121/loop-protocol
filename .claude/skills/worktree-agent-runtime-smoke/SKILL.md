---
name: worktree-agent-runtime-smoke
description: linked worktree 内で Claude Code / Codex CLI の fresh runtime を起動し、structured output（既定）または herdr interactive pane（TUI 固有挙動が必要な場合のみ）で観測し、redacted evidence を worktree-local に保存する共有 Skill。「runtime smoke」「動作検証を実行」「Claude/Codex を worktree で起動して確認」のトリガーで使う。
---

# Worktree Agent Runtime Smoke

Claude Code と Codex CLI について、linked worktree 内で fresh session を起動し、
structured event（既定）または herdr pane observation（TUI 固有挙動が必要な場合）で
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
- `--transport auto|direct|herdr`
- `--worktree <linked worktree の絶対パス>`
- `--prompt-file <検証用 prompt ファイル>`（raw prompt を argument interpolation しない）
- `--output-dir <evidence 出力先。既定は worktree 配下の untracked ディレクトリ>`
- `--timeout-seconds <bounded timeout>`
- `--expect-marker <literal>`（repeatable、任意）
- `--require-clean-postcondition`（任意）
- `--inspect-session-log-metadata` / `--require-session-log-metadata`（任意。既定では session log を読まない）
- `--keep-pane`（任意。既定では検証 pane を閉じる）

## Lane 選択

### Lane A: structured smoke（既定・非対話ラン）

非対話の fresh process から stream JSON / JSONL event と exit code を取得する。
TUI screen scraping を使わない。

- Claude Code の起動コマンド例: `claude -p --output-format stream-json --include-hook-events --no-session-persistence`
- Codex CLI の起動コマンド例: `codex exec -C <worktree> --json --ephemeral <prompt>`

詳細は `references/claude-code.md` / `references/codex-cli.md` を参照。

### Lane B: interactive herdr smoke（必要時のみ）

TUI `/status`、Skill picker、approval 画面、subagent UI、context 表示等、structured lane で
露出しない状態の観測が必要な場合だけ使用する。herdr が必須。

詳細は `references/herdr.md` を参照。

## 手順

1. **worktree identity を確認する**（root checkout・別 repository・cwd mismatch を実行前に拒否する。runner が自動で検証する）
2. **prompt file を用意する**（raw prompt をコマンドライン引数に直接埋め込まない）
3. **runner を実行する**:
   ```bash
   uv run --locked python3 scripts/agent-ops/run_worktree_agent_runtime_smoke.py \
     --runtime claude --mode structured --transport auto \
     --worktree "$WORKTREE" \
     --prompt-file tmp/runtime-smoke/claude-structured.md \
     --output-dir artifacts/runtime-smoke/claude-structured \
     --timeout-seconds 180 \
     --require-clean-postcondition
   ```
4. **exit code を確認する**: `0`=成功／`1`=runtime failure・timeout・identity mismatch・postcondition 違反／`77`=SKIP（capability・auth・herdr 不足。PASS へ昇格しない）
5. **evidence を確認する**: `<output-dir>/summary.md` の redacted excerpt だけを caller の PR 本文へ引用する。raw prompt／raw transcript／reasoning／credential／HOME 絶対パスは保存しない（`references/evidence-hygiene.md` 参照）
6. **caller が semantic verdict を判定する**: runner の役割は起動・観測・証跡収集までで終わる

## Safety Boundary（安全境界）

- runner は `shell=False` を基本とし、prompt は file または stdin で渡す
- canonical repository と linked worktree identity を実行前に検証し、root checkout・別 repository・cwd mismatch を拒否する
- `--dangerously-bypass-approvals-and-sandbox` / `--yolo` / `danger-full-access` への自動変更を行わない
- herdr 未検出・`HERDR_ENV` 未設定は `mode=interactive` の SKIP（exit 77）とし、structured lane の失敗へ波及させない
- `blocked` / `unknown` の agent lifecycle state を成功として扱わない
- caller pane、別 agent、別 workspace を変更しない。検証用 pane だけを閉じる（`--keep-pane` 指定時は残す）
- 新しい schema、digest、receipt、publisher、state store、semantic verdict classifier を追加しない

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
