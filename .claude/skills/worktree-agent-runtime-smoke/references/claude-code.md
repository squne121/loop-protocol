# Claude Code Runtime Reference

## capability 判定の方針(help への非掲載は capability 不足を意味しない)

`claude --help` は human-oriented な概要出力であり、network-exhaustive ではない。
実際に、`--max-turns` は Claude Code 2.1.220 の `--help` 出力から欠落しているにも
関わらず、有効な documented print-mode flag として受理される(Issue #1960)。
そのため runner は `claude --help` のテキストから flag capability を推定しない
(旧 `preflight_claude_flags` は削除済み)。preflight は `claude` 実行ファイルが
PATH 上に存在するかどうかのみを確認する(`preflight_claude_available`)。

structured lane の capability 判定は、実際の fixed-argv invocation 結果
(`classify_claude_structured_outcome`)に基づく:

- runtime が `unknown option` / `unrecognized option` 等の狭く一致する
  parser-level diagnostic を返した場合のみ capability SKIP(exit 77)とする
- `--max-turns` の bound に到達した(`Reached max turns` 等)場合は flag が
  受理された証拠であり、capability SKIP に昇格させず bounded turn failure
  (FAIL 1)として扱う
- それ以外の非ゼロ終了(認証失敗、network 失敗、model 失敗、汎用 runtime
  エラー等)は既存の FAIL 分類のまま変化しない

interactive lane(herdr 経由)は `--output-format` / `--include-hook-events` /
`--no-session-persistence` / `--max-turns` のような structured-only flag に
一切依存しない。structured lane と interactive lane は異なる bounded-execution
保証を持つ:

- structured lane: 実際の fixed-argv invocation 結果(exit code・terminal
  event・capability 分類)
- interactive lane: herdr の wait timeout、process termination、isolated
  session の stop・delete・removal 確認(`herdr session list --json` での
  消失確認)。structured-only flag の forward には依存しない

## Structured lane（既定）

```bash
claude -p \
  --output-format stream-json \
  --include-hook-events \
  --no-session-persistence \
  --max-turns "$MAX_TURNS" \
  --verbose \
  < <(cat "$PROMPT_FILE")
```

- prompt は stdin 経由で渡す（コマンドライン引数へ raw prompt を直接埋め込まない）
- `--no-session-persistence` により session を永続化しない
- `--include-hook-events` により hook lifecycle event を stream JSON へ含める
- `--max-turns` で実行回数を bounded にする（既定 30。PR #1921 P1 fix-delta）
- runner は `claude --help` を preflight し、`--output-format` / `--include-hook-events` /
  `--no-session-persistence` / `--max-turns` のいずれかが存在しない runtime version では
  **silent fallback せず exit 77（SKIP）** を返す
- bounded timeout（`--timeout-seconds`）で fresh process を強制終了する

## Interactive herdr lane（必要時のみ）

呼び出し元の Herdr session ではなく、実行のたびに新規生成する isolated named session の
中で agent lifecycle を駆動する（詳細は `references/herdr.md` 参照）。

```bash
HERDR_SESSION=<isolated-name> herdr workspace create --cwd "$WORKTREE" --no-focus
HERDR_SESSION=<isolated-name> herdr agent start <unique-name> --kind claude --pane <pane-id> -- --max-turns "$MAX_TURNS"
HERDR_SESSION=<isolated-name> herdr agent prompt <unique-name> "<prompt>" --wait --timeout <ms>
HERDR_SESSION=<isolated-name> herdr agent get <unique-name>
HERDR_SESSION=<isolated-name> herdr agent explain <unique-name> --json
HERDR_SESSION=<isolated-name> herdr agent read <unique-name> --source recent-unwrapped --lines <bounded>
herdr session stop <isolated-name> --json && herdr session delete <isolated-name> --json
```

TUI `/status`、Skill picker、approval 画面、subagent UI、context 表示の観測に使う。
`agent get` の `state` が `blocked` の場合は自動承認せず証跡を取得して停止する。
`unknown` state を成功として扱わない。

複数行 prompt は入力欄で `[Pasted text #N +M lines]` として折りたたまれ、
送信用の Enter が paste 終端シーケンスに吸収されて未送信のまま
`agent_prompt_stalled`（herdr 固定の 5000ms 観測窓）になることがある
（Claude Code 固有。詳細は `references/herdr.md` の
`agent_prompt_stalled` 節を参照）。runner はこれを検知した場合のみ
`send-keys enter` による 1 回限りの回復を行う。

## Main-session agent identity / definition binding / Skill evidence（Issue #2046）

`--claude-agent-name <persona>` を指定した structured lane 起動は、`--agent <persona>`
を実際の `claude` 起動 argv へ挿入するのに加えて、以下 2 チャネルを観測する:

- `SessionStart` hook lifecycle event（`--include-hook-events` が既に有効化している
  チャネル）から、main session 自身が実際にどの `agent_type` で起動したかを取得する
  （`extract_claude_session_start_identity`）。`SubagentStart`/`SubagentStop`（spawned
  child 向け、Issue #2021）とは別チャネル
- `Read` tool_use とそれに対応する `tool_use_id` 一致の `tool_result` から、persona ごとの
  canonical Skill body（`issue-creator`→`.claude/skills/create-issue/SKILL.md`、
  `issue-editor`→`.claude/skills/edit-issue/SKILL.md`）が実際に読まれたかを取得する
  （`extract_claude_canonical_read_receipt`）。path 不一致・`tool_use_id` 不一致・
  `is_error: true` の tool_result はすべて `unavailable` として fail-closed になる

### Hermetic no-mutation lane（`--hermetic-agent-definition`）

`--claude-agent-name` と併用すると、project-discovery の `--agent <name>` lookup の
代わりに以下を起動する:

```bash
claude -p \
  --output-format stream-json --include-hook-events --no-session-persistence \
  --max-turns "$MAX_TURNS" --verbose \
  --agent "<persona>-hermetic-<source-sha256[:12]>" \
  --agents "$HERMETIC_AGENTS_JSON_FILE" \
  --settings "$HERMETIC_SETTINGS_JSON_FILE"
```

- `$HERMETIC_AGENTS_JSON_FILE` は candidate Agent 定義の static frontmatter から
  決定論的に生成した session-local JSON（`tools: ["Read"]` 固定）。system temp
  directory に書き、run 終了後に必ず削除する（worktree postcondition に影響しない）
- `$HERMETIC_SETTINGS_JSON_FILE` は `Edit`/`MultiEdit`/`Write`/`NotebookEdit`/`Bash`/
  `Agent` を deny する session-local settings
- `--agents` / `--settings` は `_CLAUDE_FIXED_ARGV_FLAGS` に登録済みのため、実行中の
  Claude Code バージョンがどちらかを認識しない場合は capability SKIP（exit 77）になる
  （crash や汎用 FAIL に落ちない）
- `Edit`/`MultiEdit`/`Write`/`NotebookEdit`/`Bash`/`Agent` の tool_use event が 1 件でも
  観測されれば run 全体が FAIL（exit 1）。deny 設定の有効性を model の自己申告に依存しない

## 観測できる主な evidence

- structured lane: `type: system/init`、`type: result`、hook lifecycle event の件数を確認できる
- interactive lane: `agent explain` から抽出した detected agent／confidence と observed lifecycle state
  （`summary.md` の allowlist フィールドのみ。raw pane transcript は保存しない）
